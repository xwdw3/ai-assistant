"""agent.py 的单元测试：规划解析、执行循环、四道刹车、异常映射。"""

import json

import httpx
import pytest
from openai import APIConnectionError, AuthenticationError, BadRequestError, RateLimitError

import agent
from conftest import message, tool_call, usage


class TestParsePlan:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ('["甲","乙"]', ["甲", "乙"]),
            ('```json\n["甲","乙"]\n```', ["甲", "乙"]),
            ("1. 甲\n2. 乙", ["甲", "乙"]),
            ("- 甲\n- 乙", ["甲", "乙"]),
            ("", ["直接完成任务"]),
        ],
    )
    def test_各种不规整输出(self, raw, expected):
        assert agent._parse_plan(raw) == expected

    def test_忽略空元素(self):
        assert agent._parse_plan('["甲","","乙"]') == ["甲", "乙"]


class TestExecuteNormal:
    def test_正常完成返回文本(self, wire):
        wire([message(content="全部完成")])
        assert agent.execute("任务", ["步骤"]) == "全部完成"

    def test_工具结果回填且角色链正确(self, wire):
        fake = wire([
            message(tool_calls=[tool_call("c1", "get_date", {})]),
            message(tool_calls=[tool_call("c2", "list_notes", {})]),
            message(content="完成"),
        ])
        events = []
        agent.execute("任务", ["步骤"], on_event=events.append)

        assert len(events) == 2
        assert fake.calls == 3

    def test_空工具结果不崩(self, wire):
        """回归：read_note 读空文件返回空串时，曾因 splitlines()[0] 抛 IndexError。"""
        from tools import save_note

        save_note("空.txt", "")
        wire([
            message(tool_calls=[tool_call("c1", "read_note", {"filename": "空.txt"})]),
            message(content="完成"),
        ])
        events = []
        assert agent.execute("任务", ["步骤"], on_event=events.append) == "完成"
        assert any("（空结果）" in e for e in events)

    def test_未知工具不崩(self, wire):
        wire([
            message(tool_calls=[tool_call("c1", "不存在的工具", {})]),
            message(content="完成"),
        ])
        assert agent.execute("任务", ["步骤"]) == "完成"

    def test_参数非法JSON不崩(self, wire):
        from types import SimpleNamespace

        bad = SimpleNamespace(
            id="c1", function=SimpleNamespace(name="save_note", arguments="{不是 JSON")
        )
        wire([message(tool_calls=[bad]), message(content="完成")])

        events = []
        assert agent.execute("任务", ["步骤"], on_event=events.append) == "完成"
        assert any("不是合法 JSON" in e for e in events)


class TestToolResultCap:
    def test_超长结果被截断(self, monkeypatch):
        monkeypatch.setitem(agent.TOOL_MAP, "_long", lambda: "字" * 9000)
        result = agent._call_tool("_long", "{}")
        assert len(result) <= agent.MAX_TOOL_RESULT + 40
        assert "已截断" in result


class TestBudget:
    def test_token上限提前中止(self, wire):
        fake = wire([message(tool_calls=[tool_call(f"b{i}", "get_date", {})], usage=usage(60, 60)) for i in range(6)])
        with pytest.raises(agent.AgentError, match="已停止"):
            agent.execute("任务", ["步骤"], budget=agent.Budget(max_tokens=100))
        assert fake.calls == 1  # 超限后不再发请求

    def test_费用上限提前中止(self, wire, monkeypatch):
        monkeypatch.setattr(agent, "PRICE_INPUT", 1000)
        monkeypatch.setattr(agent, "PRICE_OUTPUT", 1000)
        wire([message(tool_calls=[tool_call(f"p{i}", "get_date", {})], usage=usage(60, 60)) for i in range(4)])

        with pytest.raises(agent.AgentError, match="费用"):
            agent.execute("任务", ["步骤"], budget=agent.Budget(max_tokens=0, max_cost=0.01))

    def test_规划阶段也拦预算(self):
        budget = agent.Budget(max_tokens=10)
        budget.total_tokens = 999
        with pytest.raises(agent.AgentError, match="已停止"):
            agent.make_plan("任务", budget=budget)

    def test_记账含入参出参(self):
        budget = agent.Budget()
        budget.record(usage(100, 50))
        budget.record(None)  # 兼容不返回 usage 的服务
        assert budget.total_tokens == 150
        assert "150" in budget.summary()


class TestGuardRails:
    def test_无进展会中止(self, wire):
        wire([message(tool_calls=[tool_call(f"r{i}", "get_date", {})]) for i in range(6)])
        with pytest.raises(agent.AgentError, match="无进展"):
            agent.execute("任务", ["步骤"], budget=agent.Budget(max_tokens=0, max_cost=0))

    def test_无进展先提醒再中止(self, wire):
        """第一次发现重复应插提醒，第二次才中止。"""
        fake = wire([message(tool_calls=[tool_call(f"r{i}", "get_date", {})]) for i in range(6)])
        seen = []
        with pytest.raises(agent.AgentError):
            agent.execute(
                "任务", ["步骤"],
                on_event=seen.append,
                budget=agent.Budget(max_tokens=0, max_cost=0),
            )
        assert any("已提醒模型换做法" in s for s in seen)
        assert fake.calls == agent.STALL_LIMIT + 2

    def test_交替动作不误判(self, wire):
        wire([
            message(tool_calls=[tool_call("d1", "list_notes", {})]),
            message(tool_calls=[tool_call("d2", "save_note", {"filename": "甲", "content": "1"})]),
            message(tool_calls=[tool_call("d3", "list_notes", {})]),
            message(content="完成"),
        ])
        assert agent.execute("任务", ["步骤"]) == "完成"

    def test_步数用尽会中止(self, wire):
        wire([
            message(tool_calls=[tool_call(f"m{i}", "save_note", {"filename": f"n{i}", "content": f"c{i}"})])
            for i in range(20)
        ])
        with pytest.raises(agent.AgentError, match="最大步数"):
            agent.execute("任务", ["步骤"], budget=agent.Budget(max_tokens=0, max_cost=0))


class TestErrorMapping:
    def _client_raising(self, monkeypatch, exc):
        monkeypatch.setattr(
            agent,
            "get_client",
            lambda: type("C", (), {"chat": type("X", (), {"completions": type("Y", (), {"create": staticmethod(lambda **k: (_ for _ in ()).throw(exc))})()})()})(),
        )

    def test_超时转AgentError(self, monkeypatch):
        req = httpx.Request("POST", "https://api.deepseek.com/x")
        from openai import APITimeoutError

        self._client_raising(monkeypatch, APITimeoutError(request=req))
        with pytest.raises(agent.AgentError, match="秒没有返回"):
            agent._request(agent.Budget())

    def test_连不上转AgentError(self, monkeypatch):
        req = httpx.Request("POST", "https://api.deepseek.com/x")
        self._client_raising(monkeypatch, APIConnectionError(request=req))
        with pytest.raises(agent.AgentError, match="连不上"):
            agent._request(agent.Budget())

    def test_401转ConfigError(self, monkeypatch):
        req = httpx.Request("POST", "https://api.deepseek.com/x")
        err = AuthenticationError("bad", response=httpx.Response(401, request=req), body=None)
        self._client_raising(monkeypatch, err)
        with pytest.raises(agent.ConfigError, match="401"):
            agent._request(agent.Budget())

    def test_429转AgentError(self, monkeypatch):
        req = httpx.Request("POST", "https://api.deepseek.com/x")
        err = RateLimitError("slow", response=httpx.Response(429, request=req), body=None)
        self._client_raising(monkeypatch, err)
        with pytest.raises(agent.AgentError, match="429"):
            agent._request(agent.Budget())

    def test_400转AgentError(self, monkeypatch):
        req = httpx.Request("POST", "https://api.deepseek.com/x")
        err = BadRequestError("bad", response=httpx.Response(400, request=req), body=None)
        self._client_raising(monkeypatch, err)
        with pytest.raises(agent.AgentError, match="400"):
            agent._request(agent.Budget())


class TestConfig:
    def test_缺Key抛ConfigError(self, monkeypatch):
        monkeypatch.setattr(agent, "_client", None)
        monkeypatch.setattr(agent, "API_KEY", "")
        with pytest.raises(agent.ConfigError, match="API Key"):
            agent.make_plan("任务")

    def test_两类异常都继承RuntimeError(self):
        assert issubclass(agent.ConfigError, RuntimeError)
        assert issubclass(agent.AgentError, RuntimeError)
        assert not issubclass(agent.AgentError, agent.ConfigError)
