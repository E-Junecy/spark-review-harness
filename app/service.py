"""应用层·编排：一次对话的完整流程。

字段形状照抄设计期的 `AgentService`，前端（CLI / WebApi）只能用它的三个方法：
新建对话、列出对话、发消息。

这一层不许认识外部世界：只 import 领域层与本层的端口/策略。测试因此塞进假客户端、假存储
就能把整条审核链跑完（见 `tests/test_app.py`），换模型、换存储、换执行环境都不必动这里。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from app.policy import Guard, audit, hard_deny
from app.ports import ModelClient, ModelSettings, SessionStore
from domain.roles import Assistant, Decision, ToolCallReviewer
from domain.session import AssistantMessage, Message, Session, ToolCall, ToolResult, UserMessage, new_id

REPEAT_THRESHOLD = 3  # docs/spark-harness-design.md §6.5：第 3 次起提醒，前两次是正当的复查
# 策略拒绝（硬规则/路径监狱）和"重复的正当复查"不是一回事：模型收不到具体目录，就会抱着同一条路径
# 连交几次。实测 `sessions/experiment_3-4ded7f5f.jsonl` 35 行里 7 行是策略拒绝，`README.md` 那一条被
# 连交 4 次——每交一次都要重问一遍模型，纯烧 token。到点就停手，把问题交回给人。
DENY_STOP = 3          # 同一条拒绝理由出现这么多次就停
DENY_TURN_STOP = 6     # 一轮里被闸门拒这么多次，多半是工作目录选错了，也停


@dataclass
class AgentService:
    model_client: ModelClient
    session_store: SessionStore
    assistant: Assistant
    reviewer: ToolCallReviewer
    assistant_model_settings: ModelSettings
    reviewer_model_settings: ModelSettings
    max_iterations: int = 12
    # 设计期只有一个 model_client，前提是同一客户端能按 model_id 服务两个模型。实际情况是
    # assistant 在公网（要 key、base_url 不同）、reviewer 在本地 Ollama，所以是两个实例；
    # 缺省回落到 model_client，字段形状不变。
    reviewer_client: ModelClient | None = None
    guard: Guard = hard_deny
    # 一工作区一条轨迹：id 由组装根按工作区算出并绑在这里。不给就退回随机 id（测试与旧用法照旧）。
    session_id: str | None = None
    # 实测 spark 判定不可复现（同一句 Get-Date 先放行后拒绝），所以同一段对话内同一请求只审一次。
    # 键里带对话段：换任务就不再沿用旧批条。
    verdicts: dict[str, tuple[Decision, str]] = field(default_factory=dict)
    repeats: dict[str, int] = field(default_factory=dict)
    policy_denials: dict[str, int] = field(default_factory=dict)   # 一轮内：拒绝理由 → 出现次数

    @property
    def _reviewer_client(self) -> ModelClient:
        return self.reviewer_client or self.model_client

    def new_session(self) -> Session:
        """打开或创建这次对话。绑定了 `session_id` 且轨迹已存在就是打开——"一工作区一条轨迹"
        意味着同一个目录再来一次，接着往同一条轨迹里写，而不是新开一个文件。"""
        if self.session_id:
            existing = next((s for s in self.session_store.list() if s.id == self.session_id), None)
            if existing is not None:
                return existing
        session = Session(id=self.session_id or new_id())
        self.session_store.save(session)
        return session

    def list_sessions(self) -> list[Session]:
        return self.session_store.list()

    def chat(self, session_id: str, user_message: str, on_event=None) -> list[Message]:
        """组织 Context → 模型回复 → 审核 → 执行 → 回填，循环到没有工具调用或撞满 max_iterations。

        `on_event` 每步回调一个 dict（`user` / `assistant` / `review` / `result`），前端靠它做实时
        输出；不给就是 None，行为与从前一样。粒度是"一步"，不是"一个 token"：本地 spark 判决一次
        约 0.8 s，逐 token 只会把界面变成打字机，不会更早给出有用的信息。
        """
        notify = on_event or (lambda event: None)
        session = self.session_store.load(session_id)
        self.policy_denials = {}                 # 一轮一清：这轮拒了几次是这轮的事
        produced: list[Message] = [self._commit(session, UserMessage(user_message))]
        notify({"kind": "user", "message": produced[0], "segment": session.current_segment})
        spinning = ""
        for _ in range(self.max_iterations):
            reply: AssistantMessage = self.model_client.generate(
                self.assistant.context(session), self.assistant_model_settings)
            produced.append(self._commit(session, reply))
            notify({"kind": "assistant", "message": reply, "segment": session.current_segment})
            for call in reply.tool_calls:
                produced.append(self._commit(session, self._handle(session, call, notify)))
            if not reply.tool_calls:
                break
            if spinning := self._spinning():     # 抱着同一条越界路径反复交：停下来，别再烧 token
                break
        else:  # 撞满步数：补一次不许调用工具的收尾，别让轨迹以工具结果结尾（用户看到的就是沉默）
            closing = self._finalize(session)
            produced.append(self._commit(session, closing))
            notify({"kind": "assistant", "message": closing, "segment": session.current_segment})
        if spinning:
            stop = self._commit(session, AssistantMessage(spinning, ()))
            produced.append(stop)
            notify({"kind": "assistant", "message": stop, "segment": session.current_segment})
        return produced

    def _spinning(self) -> str:
        """模型在原地打转的判据：同一条拒绝理由反复出现，或一轮里被闸门拒到多半是目录选错了。

        这里只负责"停"，不负责"判"——判决仍在 guard 与 verdicts 里，这条不放宽任何一道闸。
        """
        worst = max(self.policy_denials.items(), key=lambda kv: kv[1], default=("", 0))
        if worst[1] >= DENY_STOP:
            return (f"已停止这一步：同一条拒绝连着出现 {worst[1]} 次 —— {worst[0]}\n"
                    "再交一次也只是重问一遍模型。要写的文件请放进工作目录；目标目录不在当前工作区，"
                    "就用 `main.py tui -w <那个目录>` 换过去再继续。")
        if sum(self.policy_denials.values()) >= DENY_TURN_STOP:
            return (f"已停止这一步：这一轮被代码闸门拒了 {sum(self.policy_denials.values())} 次，"
                    "多半是工作目录选错了。用 `main.py tui -w <目录>` 或界面里「跳转 · 换工作区」换过去。")
        return ""

    def _handle(self, session: Session, call: ToolCall, notify) -> ToolResult:
        """审核 → 执行。被拒也照样回一条带理由的结果：不理由，模型只会原样重试。"""
        key = self._key(session, call)
        started = time.perf_counter()
        decision, source = self._decide(session, call, key)
        review_ms = round((time.perf_counter() - started) * 1000, 1)
        notify({"kind": "review", "call": call, "decision": decision, "source": source,
                "review_ms": review_ms, "segment": session.current_segment})
        output = "" if not decision.should_allow else self._execute(call)
        if not decision.should_allow and source == "policy":      # 只在代码闸门这一侧数圈圈
            reason = decision.reason or "无理由"
            self.policy_denials[reason] = self.policy_denials.get(reason, 0) + 1
        result = ToolResult(call.id, audit(decision, source, self._note_repeats(key, output)),
                            review_ms=review_ms)
        notify({"kind": "result", "call": call, "result": result, "segment": session.current_segment})
        return result

    @staticmethod
    def _key(session: Session, call: ToolCall) -> str:
        """判决与计数的依据：对话段 + 工具名 + 规范化参数。

        段必须在键里。审核器判的是"这一步安不安全"，前提却是那一次任务；换了任务还沿用上一段的
        批条就不是省时间，是拿旧任务的许可证批新命令（实验 03 的误拦清单正是这么来的）。
        """
        return (f"{session.current_segment}\n{call.tool_name}\n"
                f"{json.dumps(call.arguments, sort_keys=True, ensure_ascii=False)}")

    def _decide(self, session: Session, call: ToolCall, key: str) -> tuple[Decision, str]:
        if key not in self.verdicts:
            self.verdicts[key] = self._review(session, call)
        return self.verdicts[key]

    def _review(self, session: Session, call: ToolCall) -> tuple[Decision, str]:
        if (blocked := self.guard(call)) is not None:
            return blocked, "policy"
        context, settings = self.reviewer.context(session, call), self.reviewer_model_settings
        failure = ""
        for _ in range(2):  # 超时/抖动重试一次；再失败按拒绝处理：既不静默放行，也不带走整轮
            try:
                decision = self._reviewer_client.generate(context, settings)
                break
            except Exception as exc:  # noqa: BLE001 审核器的任何故障都不许变成放行
                failure = f"{type(exc).__name__}: {str(exc)[:140]}"
        else:
            return Decision(False, f"审核器不可用（{failure}），重试一次仍失败，本条保守拒绝"), "reviewer"
        if not isinstance(decision, Decision):
            return Decision(False, f"审核器返回了非预期对象 {decision!r}"), "reviewer"
        return decision, "reviewer"

    def _finalize(self, session: Session) -> AssistantMessage:
        """只许说话、不许调用工具的那一问（撞满步数时才发）。"""
        return self.model_client.generate(self.assistant.context(session, with_tools=False),
                                          self.assistant_model_settings)

    def _execute(self, call: ToolCall) -> str:
        tool = self.assistant.tool(call.tool_name)
        available = ", ".join(t.name for t in self.assistant.tools)
        return tool.execute(call.arguments) if tool else f"no such tool: {call.tool_name!r} (可用: {available})"

    def _note_repeats(self, key: str, output: str) -> str:
        """只追加提醒，不参与判决：判决走 verdicts 缓存，两者混在一起会互相打架。"""
        self.repeats[key] = count = self.repeats.get(key, 0) + 1
        if count < REPEAT_THRESHOLD:
            return output
        return f"{output}\n[提示] 这个请求在本次会话已出现 {count} 次：别再重复同一条命令，换个做法或直接收尾。"

    def _commit(self, session: Session, message: Message) -> Message:
        """每条消息产生的当下就打时间戳并落盘：崩了也留完整现场，断点恢复因此免费。"""
        message.ts = time.time()
        session.append(message)
        self.session_store.save(session)
        return message
