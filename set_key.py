"""配置 API Key：写入项目根目录的 .env，并可顺手验证能不能连通。

用法：
    python set_key.py              # 交互式输入（不回显，不进 shell 历史）
    python set_key.py sk-xxxx      # 直接传入（会留在命令行历史里，不推荐）
    python set_key.py --check      # 只验证现有 Key，不修改任何文件
"""

from __future__ import annotations

import getpass
import sys

import config
from agent import Budget, _request

ENV_PATH = config.BASE_DIR / ".env"
KEY_NAME = "DEEPSEEK_API_KEY"


def mask(key: str) -> str:
    """只露头尾，避免整串 Key 出现在终端或日志里。"""
    if len(key) <= 12:
        return "***"
    return f"{key[:6]}...{key[-4:]}（长度 {len(key)}）"


def read_env_lines() -> list[str]:
    if not ENV_PATH.exists():
        return []
    return ENV_PATH.read_text(encoding="utf-8").splitlines()


def write_key(key: str) -> str:
    """写入或替换 .env 里的 Key，其他配置项原样保留。"""
    lines = read_env_lines()
    if not lines:
        example = config.BASE_DIR / ".env.example"
        if example.exists():  # 从模板起步，保留可选配置项
            lines = example.read_text(encoding="utf-8").splitlines()

    new_line = f"{KEY_NAME}={key}"
    replaced = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{KEY_NAME}="):
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        lines.insert(0, new_line)

    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return "已更新" if replaced else "已写入"


def check(key: str) -> bool:
    """发一次最小请求，确认 Key 真的能用。"""
    import agent

    agent.bind_client(agent.build_client(key))
    try:
        response = _request(
            Budget(), model=config.MODEL, messages=[{"role": "user", "content": "hi"}], max_tokens=1
        )
        reply = response.choices[0].message.content or ""
        print(f"验证通过，模型 {config.MODEL} 正常响应（返回 {reply[:20]!r}）")
        return True
    except Exception as exc:
        print(f"验证失败：{type(exc).__name__}: {exc}")
        return False
    finally:
        agent.bind_client(None)


def main() -> int:
    args = [a for a in sys.argv[1:] if a.strip()]

    if "--check" in args:
        key = config.API_KEY
        if not key:
            print("当前没有读到 Key。检查 .env 是否存在、变量名是否为 DEEPSEEK_API_KEY。")
            return 1
        print(f"当前 Key：{mask(key)}")
        return 0 if check(key) else 1

    if args and not args[0].startswith("-"):
        key = args[0].strip()
    else:
        print(f"将写入 {ENV_PATH}")
        print("粘贴你的 DeepSeek API Key（输入内容不会显示）：")
        try:
            key = getpass.getpass("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消。")
            return 1

    if not key:
        print("Key 不能为空。")
        return 1
    if not key.startswith("sk-"):
        print("提示：DeepSeek 的 Key 通常以 sk- 开头，请确认没有粘贴错内容。")

    action = write_key(key)
    print(f"{action} {KEY_NAME} = {mask(key)}")

    gitignore = config.BASE_DIR / ".gitignore"
    if gitignore.exists() and ".env" not in gitignore.read_text(
        encoding="utf-8", errors="ignore"
    ):
        print("警告：.gitignore 里没有 .env，注意别把 Key 提交进仓库。")

    print("\n正在验证连通性...")
    if not check(key):
        print("\nKey 已写入，但验证没通过。检查 Key 是否有效、账户是否有余额、网络是否可达。")
        return 1
    print("\n可以用了：python app.py 或 python webapp.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
