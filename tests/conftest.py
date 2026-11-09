"""pytest 设施：路径、live 标记、假的双端，以及工作目录的落点。

工作目录自己管（`workdir`），不用 pytest 的 `tmp_path`：本机沙箱会给"用过一轮"的临时目录补上
deny 的 DACL，pytest 收尾的 listdir / rmtree 于是拿到 WinError 5，整套用例会以 ERROR 收场而不是
真的失败。改成每次运行换一个全新目录（实测：刚被 PowerShell 子进程当过 cwd 的目录也删得掉），
session 结束尽力清空。
"""

from __future__ import annotations

import itertools
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from app.ports import ModelSettings  # noqa: E402
from app.service import AgentService  # noqa: E402
from domain.roles import Assistant, Decision, ToolCallReviewer  # noqa: E402
from domain.session import AssistantMessage, Session, ToolCall  # noqa: E402
from prompts import reviewer_instruction  # noqa: E402

RUN_ROOT = BASE / "_tmp" / f"tests-{os.getpid()}-{int(time.time())}"
SETTINGS = ModelSettings("fake-model")
_SLOTS = itertools.count()


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "live: 需要在线 assistant 或本地 Ollama")


def pytest_sessionfinish(session: pytest.Session, exitstatus) -> None:
    shutil.rmtree(RUN_ROOT, ignore_errors=True)


@pytest.fixture
def workdir() -> Path:
    """每个用例一个独立工作目录，落在本次运行的 RUN_ROOT 下。"""
    path = RUN_ROOT / f"w{next(_SLOTS)}"
    path.mkdir(parents=True)
    return path


def win_tools(workspace: Path) -> tuple:
    """本机（Windows）的两个工具。挑工具本来是组装根的事，测试照着 `main.build` 抄一份。"""
    from adapters.tools.python import PythonTool
    from adapters.tools.shell import ShellTool
    return (ShellTool(workspace), PythonTool(workspace))


# === 假的双端：应用层的测试因此不需要认识任何适配器 ===========================


@dataclass
class FakeClient:
    """实现 ModelClient 协议。assistant 用法按脚本吐 AssistantMessage（不够就重复最后一条），
    reviewer 用法看 output_type 吐 Decision。seen 记下每次收到的 Context，
    "到底发了什么"因此可以断言，而不必联真模型。"""

    replies: list[AssistantMessage] = field(default_factory=list)
    decision: Decision = field(default_factory=lambda: Decision(True, "fake allow"))
    seen: list = field(default_factory=list)
    n: int = 0

    def generate(self, context, model_settings):
        self.seen.append(context)
        if context.output_type is Decision:
            return self.decision
        self.n += 1
        return self.replies[min(self.n - 1, len(self.replies) - 1)]


@dataclass
class FakeStore:
    """实现 SessionStore 协议：内存版。saves 计数用来验证"每条消息当场落盘"。"""

    sessions: dict[str, Session] = field(default_factory=dict)
    saves: int = 0

    def list(self) -> list[Session]:
        return list(self.sessions.values())

    def save(self, session: Session) -> None:
        self.saves += 1
        self.sessions[session.id] = Session(session.id, list(session.messages))

    def load(self, session_id: str) -> Session:
        return self.sessions[session_id]


def reply(*calls: ToolCall, text: str = "") -> AssistantMessage:
    return AssistantMessage(text=text, tool_calls=calls)


@pytest.fixture
def make_service():
    """装配一个只依赖接口的 AgentService，返回 (service, assistant_client, reviewer_client)。

    工具用真的 Windows 工具：审核链与应用编排在这里测，工具自身的行为归 test_adapters。"""

    def factory(workdir, replies=None, decision=None, **overrides):
        assistant, reviewer = FakeClient(replies or []), FakeClient()
        if decision is not None:
            reviewer.decision = decision
        kwargs = dict(
            model_client=assistant,
            reviewer_client=reviewer,
            session_store=FakeStore(),
            assistant=Assistant("do the task", win_tools(workdir)),
            reviewer=ToolCallReviewer(reviewer_instruction(str(workdir))),
            assistant_model_settings=SETTINGS,
            reviewer_model_settings=SETTINGS,
        )
        kwargs.update(overrides)
        return AgentService(**kwargs), assistant, reviewer

    return factory
