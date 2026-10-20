"""命令行入口：输入任务 -> 看计划 -> 确认 -> 执行。"""

import sys

from agent import AgentError, Budget, ConfigError, execute, make_plan


def main() -> int:
    try:
        task = input("你想让我做什么？\n> ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。")
        return 1

    if not task:
        print("任务不能为空。")
        return 1

    # 规划与执行共用一个账本，成本上限覆盖整个流程
    budget = Budget()

    print("\n正在规划...")
    try:
        plan = make_plan(task, budget=budget)
    except KeyboardInterrupt:
        print("\n已中断。")
        return 1
    except ConfigError as exc:
        print(f"\n[配置错误] {exc}")
        return 1
    except AgentError as exc:
        print(f"\n[运行错误] {exc}")
        return 1
    except Exception as exc:  # 其他未预期的异常
        print(f"\n[规划失败] {type(exc).__name__}: {exc}")
        return 1

    print("\n我的计划是：")
    for i, step in enumerate(plan, 1):
        print(f"  {i}. {step}")

    try:
        input("\n按回车开始执行（Ctrl+C 可中断）")
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。")
        return 1

    print("\n开始执行...")
    try:
        result = execute(
            task, plan, on_event=lambda msg: print(f"  {msg}"), budget=budget
        )
    except AgentError as exc:
        print(f"\n[运行错误] {exc}")
        print(f"\n本次已消耗：{budget.summary()}")
        return 1
    except KeyboardInterrupt:
        print("\n\n已中断，已保存的笔记不会丢失。")
        print(f"本次已消耗：{budget.summary()}")
        return 1
    except Exception as exc:
        print(f"\n[执行失败] {type(exc).__name__}: {exc}")
        print(f"\n本次已消耗：{budget.summary()}")
        return 1

    print("\n完成！\n")
    print(result)
    print(f"\n本次用量：{budget.summary()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
