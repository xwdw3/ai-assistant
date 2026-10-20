# AI 个人助理（最小可用版）

一个命令行小工具：输入一句话任务，模型先拆成 3~5 步计划，你确认后它再带着「本地笔记工具」逐步执行，最后把结果写进 `notes/`。

同时提供一层 Web 外壳（`webapp.py`），可以部署成在线页面，功能完全相同。

## 目录结构

```
ai-assistant/
├── app.py            # 命令行入口：输入任务 → 展示计划 → 确认 → 执行
├── webapp.py         # Web 入口：单页应用，流式推送执行进度，监听 $PORT
├── set_key.py        # 配置 API Key：写入 .env 并验证连通性
├── web/
│   └── index.html    # 前端页面（原生 HTML/JS，无构建、无 CDN 依赖）
├── agent.py          # 核心逻辑：make_plan() 规划、execute() 带工具执行
├── tools.py          # 本地工具实现 + 工具注册表（TOOL_MAP / TOOLS）
├── config.py         # 集中配置，支持环境变量 / .env 覆盖
├── tests/            # pytest 回归测试
│   ├── conftest.py   #   公共夹具：临时 notes 目录、假模型客户端
│   ├── test_tools.py #   工具层
│   └── test_agent.py #   执行循环、四道刹车、异常映射
├── requirements.txt
├── requirements-dev.txt
├── .env.example      # 配置模板，复制成 .env 使用
└── notes/            # 运行时自动创建，存生成的笔记
```

## 快速开始

```bash
pip install -r requirements.txt
cp .env.example .env      # Windows: copy .env.example .env
# 编辑 .env，填入自己的 DEEPSEEK_API_KEY

python app.py             # 命令行版
python webapp.py          # Web 版，打开 http://127.0.0.1:8000
```

示例：

```
你想让我做什么？
> 帮我把这周的三个工作要点整理成一份周报
```

## 配置 API Key

三种方式，任选一种：

**1. 用脚本配（推荐，输入不回显、不进终端历史）**

```bash
python set_key.py          # 粘贴 Key，自动写入 .env 并验证连通性
python set_key.py --check  # 只验证现有 Key，不改文件
```

**2. 手动编辑 `.env`**

```bash
cp .env.example .env       # Windows: copy .env.example .env
# 把 DEEPSEEK_API_KEY= 后面换成真实 Key
```

**3. 用系统环境变量**

```bash
# 优先级高于 .env，适合部署环境注入
export DEEPSEEK_API_KEY=sk-xxx     # Windows: set DEEPSEEK_API_KEY=sk-xxx
```

`config.py` 固定读取**项目根目录**的 `.env`（不依赖启动目录），且 `override=False`，
所以真实环境变量永远优先于文件。`.env` 已在 `.gitignore` 中，不会被提交。

**Web 版还有第四种**：什么都不配，让访客在页面上填自己的 Key。服务端没 Key 时页面会提示需要填写，
Key 只存在浏览器 `localStorage`，不落服务端。


## Web 版

`webapp.py` 只用标准库，不额外引入依赖。它把 `agent.execute()` 的 `on_event` 回调转成
NDJSON 流式响应，页面因此能一条条看到「第 N 轮调用了什么」而不是干等到结束。

| 接口 | 作用 |
| --- | --- |
| `GET /` | 单页界面 |
| `GET /healthz` | 健康检查，返回模型名 |
| `GET /api/config` | 只返回「服务端有没有配 Key」，**不回传 Key 本身** |
| `POST /api/plan` | `{task, apiKey}` → `{plan, usage}` |
| `POST /api/run` | `{task, plan, apiKey}` → NDJSON 流：`start` / `event` / `done` / `error` |

**API Key 的两种来源**：服务端配了 `DEEPSEEK_API_KEY` 就直接用；没配则由访客在页面上填自己的 Key，
存在浏览器 `localStorage`，随请求转发给模型服务，不落服务端、不写日志。每个请求用
`agent.bind_client()` 绑定到自己的线程，因此多人同时使用不会串 Key。

**并发限制**：`MAX_CONCURRENT_RUNS`（默认 2）个名额，拿不到直接返回 429，避免一个分享链接被打满额度。
任务描述上限 2000 字，请求体上限 64KB，计划步骤上限 12 步。


## 配置项

全部在 `config.py`，可用环境变量或 `.env` 覆盖：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 空 | 必填，缺失时启动会明确报错 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | 兼容 OpenAI 协议的服务都可换 |
| `DEEPSEEK_MODEL` | `deepseek-chat` | 模型名 |
| `MAX_STEPS` | `8` | 执行阶段最多几轮工具调用 |
| `STALL_LIMIT` | `2` | 连续几轮“调用+结果”完全相同算无进展，触发干预 |
| `MAX_TOKENS` | `60000` | 单次运行累计 token 上限（规划+执行一起算） |
| `STEP_TIMEOUT` | `60` | 单次模型请求超时，单位秒 |
| `MAX_TOOL_RESULT` | `4000` | 单条工具结果进上下文的字数上限，超出截断 |
| `PRICE_INPUT` | `0` | 输入单价，元/百万 token，填了才估算费用 |
| `PRICE_OUTPUT` | `0` | 输出单价，元/百万 token |
| `MAX_COST` | `0` | 单次运行费用上限（元），`0` 表示不限 |
| `TEMPERATURE` | `0.3` | 采样温度 |

## 内置工具

| 工具 | 作用 |
| --- | --- |
| `save_note(filename, content, overwrite=False)` | 写入 `notes/`。文件名会被收敛到目录内防越界、自动补 `.txt`；**同名默认不覆盖**，改为另存 `名字_1.txt`，`overwrite=True` 才替换原文件 |
| `read_note(filename)` | 读取，超 2000 字截断 |
| `list_notes()` | 列出所有 `.txt` |
| `get_date()` | 返回今天日期 |

新增工具只需三步：在 `tools.py` 写函数 → 加进 `TOOL_MAP` → 在 `TOOLS` 补一段声明。`agent.py` 不用改。

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest tests -q
```

覆盖：文件名的路径收敛与后缀处理、同名避让与显式覆盖、计划解析的多种不规整输入、
执行循环的消息回填、四道刹车（步数 / 无进展 / 预算 / 超时）、以及 401·429·400 的异常映射。
测试用假客户端和临时目录，不联网、不写真实 `notes/`。

## 执行保护

`execute()` 有四道刹车，任何一个触发都会带着原因退出，而不是硬撑到最后：

1. **步数上限**：跑到 `MAX_STEPS` 轮强制退出。
2. **无进展检测**：把每轮的「工具调用 + 返回结果」压成一个指纹，与上一轮完全相同即视为原地打转。
   连续 `STALL_LIMIT` 轮重复时，先插入一条提醒让模型换做法（只提醒一次）；若再重复，
   立刻中止并说明原因。
3. **成本上限**：`Budget` 会累计每次响应的 `usage`，规划与执行共用同一本账。
   超过 `MAX_TOKENS` 或 `MAX_COST` 时**在发下一次请求之前**就停下，不会再多花一分钱。
   配置了 `PRICE_INPUT` / `PRICE_OUTPUT` 才会估算费用，否则只按 token 卡。
4. **单步超时**：每次模型请求都有 `STEP_TIMEOUT` 秒上限，超时抛 `AgentError`，
   由 `app.py` 打印成「运行错误」并输出已消耗用量。

判据是“动作和结果都没变”，所以能兜住这些情况：反复查同一个日期、反复调用不存在的工具、
反复读同一份文件拿到相同内容。

**成功与失败的区分**：`execute()` 只有正常完成才返回文本；被上面任何一道刹车拦住时抛 `AgentError`。
`app.py` 因此会打印 `[运行错误]` 并以退出码 1 结束，不会把“没做完”显示成“完成！”。

## 已知限制

- 单轮任务，无多轮对话记忆（每次运行独立）。
- 笔记仅存为纯文本 `.txt`，无检索/索引。
- 工具无沙箱，`save_note` 会真实写盘。Web 版所有访客共用服务端的同一个 `notes/` 目录。
- Web 版没有登录态，限制靠「并发名额 + 访客自带 Key」，不适合直接暴露敏感数据。
