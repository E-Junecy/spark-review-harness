"""领域层·角色与审核结论。

`Assistant` 和 `ToolCallReviewer` 是同一件事的两半：都是"有指令、按需带工具、把 Session 组织成
Context"的角色，只是一个产出 `AssistantMessage`、一个产出 `Decision`。设计期把它们
划在领域层是有道理的——它们只搬运领域概念，不调模型、不执行工具，没有任何 I/O。

`Decision` 是审核结论：能不能放行 + 被拒时的短理由。理由必须存在，否则模型只会原样重试。
"""

from __future__ import annotations

from dataclasses import dataclass

from domain.context import Context
from domain.session import AssistantMessage, Session, ToolCall, UserMessage
from domain.tool import Tool


@dataclass
class Decision:
    should_allow: bool
    reason: str | None = None

    def label(self, source: str) -> str:
        """审计出处写进回给模型的文本：轨迹里因此自带"谁批准的、为什么"。理由为空时不留空冒号。"""
        return f"{'allowed' if self.should_allow else 'denied'} by {source}" + \
               (f": {self.reason}" if self.reason else "")


@dataclass
class Assistant:
    """回复用户的角色。`tool(name)` 是查表：模型报来的工具名只能在这里落地成具体工具。"""

    instruction: str = ""
    tools: tuple[Tool, ...] = ()

    def context(self, session: Session, with_tools: bool = True) -> Context[AssistantMessage]:
        """`with_tools=False` 是撞满步数时的收尾一问：请求里不带工具定义，模型就只能用自然语言回答。"""
        return Context(self.instruction, self.tools if with_tools else (), session.messages, AssistantMessage)

    def tool(self, name: str) -> Tool | None:
        return next((t for t in self.tools if t.name == name), None)


@dataclass
class ToolCallReviewer:
    """审核工具调用的角色：有指令、不带工具，产出 Context[Decision]。

    上下文只带"任务目标 + 待审请求"两行，不带完整历史：省 token，也免得 4B 模型在长上下文里抖。
    判据现在只剩"不可逆破坏"和"越界写"两件事。实验 03 之前它还兼判"是否跑题"，实测关掉思维链后
    它会因为"这条命令和写 note.txt 无关"拒绝 `New-Item draft.md` 这种正常中间步骤，于是那条判据
    从提示词里删了——而越界写本来就能由代码算准（见 `app/policy.py` 的路径监狱），不必问模型。
    """

    instruction: str = ""

    def context(self, session: Session, call: ToolCall) -> Context[Decision]:
        pending = UserMessage(f"任务目标：{session.goal}\n待审核请求：{call.describe()}")
        return Context(self.instruction, (), [pending], Decision)
