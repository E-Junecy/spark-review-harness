"""领域层：语言本身的不变式。

这些用例不碰网络、不碰进程、不碰磁盘。它们挂了，说明"对话 / 工具 / 上下文 / 结论"这四个概念
被改坏了，上层所有测试的失败都只是连带症状。最后一组是分层纪律测试：领域层一旦开始 import
外部世界的库，上面这些就都只是巧合。
"""

from __future__ import annotations

import json
import re

from conftest import BASE

from domain.context import Context
from domain.roles import Assistant, Decision, ToolCallReviewer
from domain.session import (
    AssistantMessage,
    Session,
    ToolCall,
    ToolResult,
    UserMessage,
    json_dict,
    new_id,
)
from domain.tool import Tool


def test_new_ids_are_unique_and_short():
    assert len({new_id() for _ in range(200)}) == 200
    assert len(new_id()) == 12


def test_session_appends_in_order_and_exposes_the_goal():
    session = Session()
    session.append(UserMessage("创建 report.md"))
    session.append(AssistantMessage("好的"))
    assert [type(m).__name__ for m in session.messages] == ["UserMessage", "AssistantMessage"]
    assert session.goal == "创建 report.md"


def test_goal_is_empty_before_the_first_user_turn():
    assert Session().goal == ""
    assert Session(messages=[AssistantMessage("hi")]).goal == ""


def test_describe_is_the_one_line_every_reviewer_and_log_reads():
    call = ToolCall("c1", "python", {"code": "print('hi')"})
    assert call.describe() == "python " + json.dumps({"code": "print('hi')"}, ensure_ascii=False)
    # id 不参与：判决缓存与硬规则扫描认的是"同一个请求"，不是"同一次请求"
    assert ToolCall("other", "python", {"code": "print('hi')"}).describe() == call.describe()


def test_arguments_from_never_raises_at_the_model():
    assert ToolCall.arguments_from('{"command": "dir"}') == {"command": "dir"}
    assert ToolCall.arguments_from("{oops") == {}
    assert ToolCall.arguments_from("") == {}
    assert ToolCall.arguments_from(None) == {}
    assert ToolCall.arguments_from('"裸字符串"') == {"value": "裸字符串"}
    assert ToolCall.arguments_from({"already": "dict"}) == {"already": "dict"}
    assert json_dict("[1, 2]") == {"value": [1, 2]}


def test_observation_fields_default_to_harmless_values():
    """ts / review_ms 是观测字段：没人打点时安静待着，不许影响相等比较。"""
    assert UserMessage("x").ts == 0.0
    assert AssistantMessage().ts == 0.0
    assert ToolResult("c1", "ok").review_ms is None
    assert ToolResult("c1", "ok") == ToolResult("c1", "ok")


def test_decision_label_records_who_approved_and_says_why():
    assert Decision(True, "只读操作").label("reviewer") == "allowed by reviewer: 只读操作"
    assert Decision(False, "命中硬规则").label("policy") == "denied by policy: 命中硬规则"
    assert Decision(True, None).label("reviewer") == "allowed by reviewer"  # 不留空冒号
    assert Decision(False, "").label("policy") == "denied by policy"


def test_assistant_context_carries_the_conversation_itself():
    class Noop:
        name = "noop"
        description = ""
        parameters = {}

        def execute(self, arguments):
            return ""

    assert Tool._is_protocol  # 工具是形状，不是拿来实例化的东西
    tools = (Noop(),)
    session = Session(messages=[UserMessage("干活"), ToolResult("c1", "ok")])
    context = Assistant("指令", tools).context(session)
    assert isinstance(context, Context) and context.output_type is AssistantMessage
    assert context.instruction == "指令" and context.tools == tools
    assert context.messages is session.messages  # 同一份历史，不复制


def test_assistant_tool_lookup_is_by_name():
    class Echo:
        name = "echo"
        description = ""
        parameters = {}

        def execute(self, arguments):
            return "ok"

    assistant = Assistant("", (Echo(),))
    assert assistant.tool("echo").name == "echo"
    assert assistant.tool("nope") is None


def test_reviewer_context_is_exactly_task_plus_pending_call():
    """不带完整历史是刻意的：省 token，且实测 spark 会看任务判是否跑题。"""
    session = Session(messages=[UserMessage("在工作目录创建 report.md"), AssistantMessage("先看一眼")])
    call = ToolCall("c1", "shell", {"command": "Get-ChildItem"})
    context = ToolCallReviewer("指令").context(session, call)
    assert context.output_type is Decision and context.tools == ()
    assert len(context.messages) == 1
    text = context.messages[0].text
    assert "在工作目录创建 report.md" in text and call.describe() in text


# === 分层纪律 ==============================================================

FORBIDDEN_IMPORTS = ("openai", "ollama", "subprocess", "http", "jsonl", "typer", "rich",
                     "dotenv", "jinja2", "string", "pathlib", "adapters", "app", "prompts")


def test_domain_layer_imports_nothing_from_the_outside_world():
    """只看 import 行：docstring 里提到 PowerShell / OpenAI 是讲解，import 它们才是破戒。"""
    for path in sorted((BASE / "domain").glob("*.py")):
        lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
                 if re.match(r"^\s*(from|import)\s", line)]
        bad = [line for line in lines if any(name in line for name in FORBIDDEN_IMPORTS)]
        assert not bad, f"{path.name} 越界 import: {bad}"
