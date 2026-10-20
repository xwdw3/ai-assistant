"""pytest 公共配置与夹具。"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# 让 tests/ 能 import 到项目根目录下的模块
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent
import tools


@pytest.fixture(autouse=True)
def isolated_notes(tmp_path, monkeypatch):
    """每个用例都用自己的临时 notes 目录，互不干扰，也不污染项目。"""
    monkeypatch.setattr(tools, "NOTES_DIR", str(tmp_path))
    return tmp_path


def message(content=None, tool_calls=None, usage=None):
    """造一个最简的 chat.completion 响应。"""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))],
        usage=usage,
    )


def tool_call(call_id, name, arguments):
    return SimpleNamespace(
        id=call_id, function=SimpleNamespace(name=name, arguments=json.dumps(arguments))
    )


def usage(prompt=30, completion=30):
    return SimpleNamespace(
        prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion
    )


class FakeCompletions:
    """按顺序吐出预设响应，并记录被调用次数。"""

    def __init__(self, turns):
        self.turns = list(turns)
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if not self.turns:
            raise AssertionError("响应预设用完了，说明循环比预期多跑了一轮")
        return self.turns.pop(0)


@pytest.fixture
def wire(monkeypatch):
    """把 agent.get_client 替换成假客户端，用法：wire([响应1, 响应2])。"""

    def _wire(turns):
        fake = FakeCompletions(turns)
        monkeypatch.setattr(
            agent,
            "get_client",
            lambda: SimpleNamespace(chat=SimpleNamespace(completions=fake)),
        )
        monkeypatch.setattr(agent, "API_KEY", "sk-test")
        return fake

    return _wire
