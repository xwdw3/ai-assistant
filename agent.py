"""Agent 核心：先规划，再带着工具去执行。

对外接口：
    make_plan(task, budget=None) -> list[str]
    execute(task, plan, on_event=None, budget=None) -> str
    Budget()                用量账本，规划与执行可共用
    ConfigError / AgentError  配置问题 / 运行期问题

约定：execute 正常完成才返回文本；因预算、无进展、步数用尽被中止时抛 AgentError，
这样调用方不会把「没做完」误当成「做完了」。
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from typing import Callable

from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    OpenAI,
    RateLimitError,
)

from config import (
    API_KEY,
    BASE_URL,
    MAX_COST,
    MAX_STEPS,
    MAX_TOKENS,
    MAX_TOOL_RESULT,
    MODEL,
    PRICE_INPUT,
    PRICE_OUTPUT,
    STALL_LIMIT,
    STEP_TIMEOUT,
    TEMPERATURE,
)
from tools import TOOLS, TOOL_MAP

PLAN_SYSTEM_PROMPT = """你是一个任务规划助手。把用户的任务拆成 3 到 5 个具体、可执行的步骤，
每个步骤一句话，动词开头。

可用工具只有：save_note（保存笔记）、read_note（读取笔记）、list_notes（列出笔记）、get_date（获取日期）。

只输出 JSON 字符串数组，不要任何解释、前后缀或 Markdown 代码块，例如：
["第一步...", "第二步..."]"""

EXEC_SYSTEM_PROMPT = """你是个人助理，正在执行下面这个任务。

任务：
{task}

已确定的执行计划：
{plan}

请逐步完成。需要落地内容时调用 save_note 保存；不确定日期时调用 get_date，
不要凭空编造。某一步不需要工具时，直接产出内容即可。
全部完成后，用一段话汇报你做了什么、结果存在哪里。"""

# 检测到原地打转时插入的提醒，只给模型一次改正机会
STALL_NUDGE = (
    "注意：你刚才的工具调用和上一轮完全相同，也没有拿到任何新信息。"
    "请换一种做法（换工具、改参数、换个思路），或者直接基于已有信息给出结论。"
    "重复同样的调用不会产生不同结果。"
)

_client: OpenAI | None = None
# Web 端每个请求跑在独立线程里，用自己的 Key，不能共用全局客户端
_local = threading.local()


class ConfigError(RuntimeError):
    """配置有问题，原地重试没用，得改配置。"""


class AgentError(RuntimeError):
    """运行期错误（超时、连不上），换个时间或调参数可能恢复。"""


def build_client(api_key: str) -> OpenAI:
    """按指定 Key 造一个客户端，供 Web 端按访客隔离使用。"""
    return OpenAI(api_key=api_key, base_url=BASE_URL, timeout=STEP_TIMEOUT)


def bind_client(client: OpenAI | None) -> None:
    """把客户端绑定到当前线程；传 None 表示解绑，回到全局默认。"""
    _local.client = client


def get_client() -> OpenAI:
    """惰性创建客户端，缺少 Key 时给出明确提示而不是静默失败。"""
    bound = getattr(_local, "client", None)
    if bound is not None:  # Web 端为本次请求绑定的客户端优先
        return bound

    global _client
    if _client is None:
        if not API_KEY:
            raise ConfigError(
                "缺少 API Key：请复制 .env.example 为 .env，并填入 DEEPSEEK_API_KEY"
            )
        _client = OpenAI(api_key=API_KEY, base_url=BASE_URL, timeout=STEP_TIMEOUT)
    return _client


@dataclass
class Budget:
    """一次运行的用量账本：累计 token、估算费用，并判断是否已超上限。"""

    max_tokens: int = MAX_TOKENS
    max_cost: float = MAX_COST
    total_tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def record(self, usage) -> None:
        """把一次响应的 usage 记进账本。部分兼容服务不回 usage，这里容忍缺席。"""
        if usage is None:
            return
        prompt = getattr(usage, "prompt_tokens", 0) or 0
        completion = getattr(usage, "completion_tokens", 0) or 0
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.total_tokens += getattr(usage, "total_tokens", 0) or (prompt + completion)

    @property
    def cost(self) -> float:
        """按配置单价估算费用（元）。单价为 0 时恒为 0。"""
        return (
            self.prompt_tokens * PRICE_INPUT + self.completion_tokens * PRICE_OUTPUT
        ) / 1_000_000

    def exceeded(self) -> str | None:
        """超出任一上限就返回原因，否则返回 None。"""
        if self.max_tokens and self.total_tokens >= self.max_tokens:
            return f"累计 token 已达上限（{self.total_tokens}/{self.max_tokens}）"
        if self.max_cost and self.cost >= self.max_cost:
            return f"费用已达上限（约 {self.cost:.4f}/{self.max_cost} 元）"
        return None

    def summary(self) -> str:
        text = (
            f"tokens {self.total_tokens}"
            f"（入 {self.prompt_tokens} / 出 {self.completion_tokens}）"
        )
        if PRICE_INPUT or PRICE_OUTPUT:
            text += f"，约 {self.cost:.4f} 元"
        return text


def _request(budget: Budget, **kwargs):
    """所有模型请求的统一出口：加单步超时、累计用量、把各种异常转成人话。"""
    try:
        response = get_client().chat.completions.create(timeout=STEP_TIMEOUT, **kwargs)
    except APITimeoutError as exc:
        raise AgentError(
            f"单次请求超过 {STEP_TIMEOUT:g} 秒没有返回，已放弃。"
            f"可以调大 config.STEP_TIMEOUT，或把任务拆小一点。"
        ) from exc
    except APIConnectionError as exc:
        raise AgentError(f"连不上模型服务（{BASE_URL}），请检查网络或 BASE_URL") from exc
    except AuthenticationError as exc:
        raise ConfigError("API Key 无效或已过期（401），请重新检查 DEEPSEEK_API_KEY") from exc
    except RateLimitError as exc:
        raise AgentError("触发了服务端限流（429），请稍后重试或降低调用频率") from exc
    except BadRequestError as exc:
        raise AgentError(f"请求被服务端拒绝（400），通常是模型名或参数不合法：{exc}") from exc

    budget.record(getattr(response, "usage", None))
    return response


def _parse_plan(text: str) -> list[str]:
    """从模型输出里尽力抠出步骤列表，容忍代码块、编号、多余说明。"""
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?|```$", "", cleaned, flags=re.MULTILINE).strip()

    match = re.search(r"\[.*\]", cleaned, flags=re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            steps = [str(s).strip() for s in data if str(s).strip()]
            if steps:
                return steps
        except json.JSONDecodeError:
            pass

    # 兜底：按行拆，去掉 "1." "-" 之类的前缀
    steps = []
    for line in cleaned.splitlines():
        line = re.sub(r"^\s*(?:[-*•]|\d+[.、)])\s*", "", line).strip()
        if line and not line.startswith("["):
            steps.append(line)
    return steps or ["直接完成任务"]


def make_plan(task: str, budget: Budget | None = None) -> list[str]:
    """第一步：让模型把任务拆成步骤。用量记进 budget（不传则内部新建）。"""
    budget = budget or Budget()
    reason = budget.exceeded()
    if reason:
        raise AgentError(f"已停止：{reason}。规划阶段就没有可用预算，请调大上限后重试。")

    response = _request(
        budget,
        model=MODEL,
        messages=[
            {"role": "system", "content": PLAN_SYSTEM_PROMPT},
            {"role": "user", "content": task},
        ],
        temperature=TEMPERATURE,
    )
    return _parse_plan(response.choices[0].message.content or "")


def _call_tool(name: str, raw_arguments: str) -> str:
    """执行单个工具调用，任何异常都转成文本回给模型，不让循环崩掉。"""
    func = TOOL_MAP.get(name)
    if func is None:
        return f"错误：不存在名为 {name} 的工具"

    try:
        kwargs = json.loads(raw_arguments) if raw_arguments else {}
    except json.JSONDecodeError as exc:
        return f"错误：参数不是合法 JSON（{exc}）"

    try:
        text = str(func(**kwargs))
    except Exception as exc:  # 工具内部异常也要让模型知道
        return f"错误：{name} 执行失败（{exc}）"

    # 工具结果会整段塞进上下文，太长会把窗口吃光，这里做硬截断
    if len(text) > MAX_TOOL_RESULT:
        cut = len(text) - MAX_TOOL_RESULT
        text = text[:MAX_TOOL_RESULT] + f"\n...(结果过长，已截断 {cut} 字)"
    return text


def _round_signature(tool_calls, results: list[str]) -> tuple:
    """把一轮的「调用了什么 + 拿回了什么」压成指纹，用于比较是否在原地打转。"""
    calls = tuple(
        sorted((call.function.name, call.function.arguments or "") for call in tool_calls)
    )
    return calls, tuple(results)


def execute(
    task: str,
    plan: list[str],
    on_event: Callable[[str], None] | None = None,
    budget: Budget | None = None,
) -> str:
    """第二步：带着工具按计划执行，直到模型不再调用工具或触碰到任一上限。"""
    notify = on_event or (lambda _msg: None)
    budget = budget or Budget()

    plan_text = "\n".join(f"{i}. {s}" for i, s in enumerate(plan, 1))
    messages: list[dict] = [
        {
            "role": "system",
            "content": EXEC_SYSTEM_PROMPT.format(task=task, plan=plan_text),
        },
        {"role": "user", "content": "请开始执行。"},
    ]

    # 无进展检测的状态
    last_signature: tuple | None = None
    stall_count = 0        # 连续几轮动作+结果完全相同
    nudged = False         # 是否已经提醒过模型（只提醒一次）

    for step in range(MAX_STEPS):
        # 先查预算再发请求，超了连一次请求都不再发
        reason = budget.exceeded()
        if reason:
            notify(f"[预算] {reason}，已停止")
            raise AgentError(
                f"已停止：{reason}。本次累计用量 {budget.summary()}。"
                f"任务未执行完，可调大 config.MAX_TOKENS / MAX_COST 后重试。"
            )

        response = _request(
            budget,
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
            temperature=TEMPERATURE,
        )
        message = response.choices[0].message

        if not message.tool_calls:
            return (message.content or "").strip() or "（模型没有返回内容）"

        # 官方 SDK 的对象不能直接塞回 messages，手工转成 dict
        messages.append(
            {
                "role": "assistant",
                "content": message.content or "",
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                        },
                    }
                    for call in message.tool_calls
                ],
            }
        )

        results: list[str] = []
        for call in message.tool_calls:
            name = call.function.name
            result = _call_tool(name, call.function.arguments)
            results.append(result)
            # 工具可能返回空串（比如读了空文件），splitlines() 会是空列表
            first_line = (result.splitlines() or [""])[0][:80] or "（空结果）"
            notify(f"[第 {step + 1} 轮] 调用 {name} -> {first_line}")
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": result}
            )

        # ---- 无进展检测 ----
        # 同样的调用 + 同样的结果 = 这一轮什么也没得到，连续出现就是在原地打转
        signature = _round_signature(message.tool_calls, results)
        if signature == last_signature:
            stall_count += 1
        else:
            stall_count = 0
            nudged = False
        last_signature = signature

        if stall_count >= STALL_LIMIT:
            if not nudged:
                notify(f"[无进展] 第 {step + 1} 轮与上一轮完全相同，已提醒模型换做法")
                messages.append({"role": "user", "content": STALL_NUDGE})
                nudged = True
            else:
                raise AgentError(
                    f"检测到无进展：第 {step + 1} 轮的工具调用和结果与上一轮完全相同，"
                    f"已提前中止（不是步数用尽）。"
                    f"建议换个说法重述任务，或确认现有工具能否满足需求。"
                )

    raise AgentError(
        f"已达到最大步数 {MAX_STEPS}，任务可能未完成。"
        f"可调大 config.MAX_STEPS 后重试，或把任务拆得更小。"
    )
