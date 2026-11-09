"""适配层·TUI 的无头用例：全部用假 service，零网络、零真模型、零真命令。

驱动方式是 textual 的 `App.run_test()` + Pilot。本机环境里没装 pytest-asyncio（实测 `import
pytest_asyncio` 直接 ModuleNotFoundError），所以每个用例自己 `asyncio.run(...)` 包一层——
`run_test()` 本来就是异步生成器，包一层比给测试环境加插件便宜，也不污染别的用例。

断言尽量落在"屏幕上真出现的文本"上：`rendered()` 把底栏、轮次标题、轮次正文、散落的分隔行与红字行拼成
一坨纯文本；样式改了不该让测试变红，文本没了才是真坏了。要观察"这一轮还在跑"的中间态就用闸门停住后台
线程——按 `sleep` 猜时长是 flaky 的来源，实测假 chat 快到一个 `pilot.pause()` 都没走完就已经结束。
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.text import Text
from textual.command import CommandPalette
from textual.events import MouseScrollUp, Paste
from textual.geometry import Offset
from textual.widget import Widget
from textual.widgets import Input, OptionList, Static, TextArea

from adapters.locks import WorkspaceLock
from adapters.models.no_review import NoReviewClient
from adapters.tui import HarnessApp, Marquee, PromptArea, TrajectoryScreen, ValuePrompt, transcript
from app.policy import REVIEW_MODES, hard_deny, no_guard
from domain.roles import Decision
from domain.session import AssistantMessage, Session, ToolCall, ToolResult, UserMessage

BASE = Path(__file__).resolve().parents[1]
# 工作区名字只当"标签"用（假 service 不真的进目录），所以写成仓库相对路径，不写死 E:\harness：
# 克隆到别的盘、别人的机器上，界面用例照样跑。
WORKSPACE = BASE / "quests" / "tui"
OTHER = BASE / "quests" / "other"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    """残留的 HARNESS_* 会让"默认从 local 起步"这类断言随机翻脸：用例期间一律清掉。"""
    monkeypatch.delenv("HARNESS_REVIEW", raising=False)
    monkeypatch.delenv("HARNESS_CONTEXT_BUDGET", raising=False)


# === 假 service：按事件契约一步一步回调 ======================================


@dataclass
class FakeStore:
    sessions: dict[str, Session] = field(default_factory=dict)

    def list(self) -> list[Session]:
        return list(self.sessions.values())

    def load(self, session_id: str) -> Session:
        return self.sessions[session_id]

    def save(self, session: Session) -> None:
        self.sessions[session.id] = session


@dataclass
class FakeService:
    """一步一停的假编排：user → assistant（可带工具调用）→ review → result。

    `usage` 是每步的 (prompt, completion, cached)，默认给真数——全 0 的假数据测不出成本账。
    `gate` 给了就在发完 user 事件后卡住后台线程，用来观察"进行中"。`segment=""` 复刻旧格式轨迹。
    """

    session_id: str = "tui-1a2b3c4d"
    max_iterations: int = 12
    guard: object = None
    steps: int = 2
    deny_at: int | None = 1
    usage: tuple = ((1000, 50, 800), (1200, 300, 400))
    segment: str = "seg-one"
    output: str = "2024-01-01"
    closing: str = ""
    store: FakeStore = field(default_factory=FakeStore)
    gate: threading.Event | None = None
    calls: list = field(default_factory=list)

    @property
    def session_store(self) -> FakeStore:
        return self.store

    def new_session(self) -> Session:
        return self.store.sessions.setdefault(self.session_id, Session(self.session_id))

    def chat(self, session_id: str, user_message: str, on_event=None) -> list:
        notify = on_event or (lambda event: None)
        self.calls.append((session_id, user_message))
        user = UserMessage(user_message, ts=time.time(), segment=self.segment)
        notify({"kind": "user", "message": user, "segment": self.segment})
        if self.gate is not None:
            self.gate.wait(5.0)
        for index in range(self.steps):
            call = ToolCall(f"c{index}", "shell", {"command": f"Get-Date #{index}"})
            prompt, completion, cached = self.usage[index % len(self.usage)]
            reply = AssistantMessage("" if index == 0 else "先看一眼", (call,), ts=time.time(),
                                     prompt_tokens=prompt, completion_tokens=completion,
                                     cached_tokens=cached)
            notify({"kind": "assistant", "message": reply, "segment": self.segment})
            allow = index != self.deny_at
            notify({"kind": "review", "call": call, "source": "reviewer" if allow else "policy",
                    "decision": Decision(allow, None if allow else "命中硬规则 del，不经模型直接拒绝"),
                    "review_ms": 812.0 if allow else 0.4, "segment": self.segment})
            body = {"review": "allowed by reviewer" if allow else "denied by policy: 命中硬规则",
                    "output": self.output if allow else ""}
            notify({"kind": "result", "call": call, "segment": self.segment,
                    "result": ToolResult(call.id, json.dumps(body, ensure_ascii=False),
                                         ts=time.time(), review_ms=1.0)})
        if self.closing:                       # 真 service 的每一轮都以"不再交命令的那一句"收尾
            prompt, completion, cached = self.usage[-1]
            notify({"kind": "assistant", "segment": self.segment,
                    "message": AssistantMessage(self.closing, (), ts=time.time(), prompt_tokens=prompt,
                                                completion_tokens=completion, cached_tokens=cached)})
        return []


@dataclass
class FakeTool:
    """只带 `workspace` 的假工具：界面向 service 问工作目录时读的就是这个字段。"""

    workspace: Path
    name: str = "shell"


def make_app(service: FakeService | None = None, **kwargs) -> tuple[HarnessApp, FakeService]:
    service = service or FakeService(guard=hard_deny)
    return HarnessApp(service, workspaces=kwargs.pop("workspaces", (WORKSPACE,)), **kwargs), service


def plain(content: object) -> str:
    return getattr(content, "plain", None) or str(content)


def rendered(app: HarnessApp) -> str:
    """屏幕上真出现过的文本：底栏 + 每个轮次的标题与正文 + 散落的分隔行与红字行。"""
    chunks = [app.status_text(), plain(app.query_one("#title", Static).content)]
    chunks += [f"{plain(r.title)}\n{plain(r.lines)}" for r in app.rounds]
    return "\n".join(chunks + [plain(w.content) for w in app.query(Static)])


async def wait_until(pilot, predicate, why: str, timeout: float = 5.0) -> None:
    """无头测试里等一个状态，而不是等一个时长：跑得快慢都不该让用例翻脸。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await pilot.pause(0.02)
    raise AssertionError(f"等不到：{why}")


async def settle(app: HarnessApp, pilot) -> None:
    await wait_until(pilot, lambda: not app.busy and bool(app.rounds), "这一轮没跑完")


async def submit(app: HarnessApp, pilot, text: str) -> None:
    box = app.query_one("#prompt", PromptArea)
    box.focus()
    box.load_text(text)
    await pilot.press("enter")


def seed(service: FakeService, legacy: bool) -> FakeService:
    """往轨迹里预放一轮：旧格式就是 segment 留空（`from_wire` 对没有该字段的旧行给的正是 ""）。"""
    segment = "" if legacy else "seg-old"
    call = ToolCall("c9", "shell", {"command": "Get-Date"})
    service.store.sessions[service.session_id] = Session(service.session_id, [
        UserMessage("旧任务", ts=1_700_000_000.0, segment=segment),
        AssistantMessage("", (call,), ts=1_700_000_001.0, prompt_tokens=500, completion_tokens=20,
                         cached_tokens=100),
        ToolResult("c9", json.dumps({"review": "denied by policy: 命中硬规则", "output": ""},
                                    ensure_ascii=False), ts=1_700_000_002.0, review_ms=3.0),
    ])
    return service


# === 回车之后：事件一条条落地，一轮结束折叠 =================================


def test_enter_streams_review_lines():
    async def scene():
        app, service = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await submit(app, pilot, "创建 notes.txt")
            await settle(app, pilot)
            return app, service, rendered(app)

    app, service, shown = asyncio.run(scene())
    assert service.calls == [(service.session_id, "创建 notes.txt")]       # 文本真送到了 service
    assert "＃ 创建 notes.txt" in shown
    assert "审核 放行 0.8s" in shown                                        # 812 ms → 0.8s
    assert "审核 拒绝 · 命中硬规则 del，不经模型直接拒绝" in shown
    assert '→ shell {"command": "Get-Date #0"}' in shown                    # 命令原文进轨迹


def test_empty_assistant_text_is_not_blank():
    """实测"空 text + 一个工具调用"是常态：界面要写成人话，不许留一行空白。"""
    async def scene():
        app, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await submit(app, pilot, "干活")
            await settle(app, pilot)
            return plain(app.rounds[0].lines)

    assert "这一步没说话，直接要执行命令" in asyncio.run(scene())


def test_result_output_is_snipped_to_200_chars():
    """result 事件只显示 output 前 200 字：审核结论与整坨 stdout 都不该刷屏。"""
    async def scene():
        app, _ = make_app(FakeService(guard=hard_deny, output="长" * 900))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await submit(app, pilot, "干活")
            await settle(app, pilot)
            return plain(app.rounds[0].lines)

    body = asyncio.run(scene())
    assert "↳ " + "长" * 200 in body and "长" * 201 not in body
    assert "allowed by reviewer" not in body                               # 审核那块内容不重复贴


def test_round_stays_open_after_it_ends_so_the_answer_is_visible():
    """跑完一轮：整轮**不**折（折的话最终回答就看不见了），思考块各自折起。"""
    async def scene():
        gate = threading.Event()
        app, _ = make_app(FakeService(guard=hard_deny, gate=gate, closing="完成，写在 runs\\ok.md"))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await submit(app, pilot, "干活")
            await wait_until(pilot, lambda: bool(app.rounds), "轮次没建起来")
            mid = (len(app.rounds), app.rounds[0].collapsed, plain(app.rounds[0].title), app.busy)
            gate.set()
            await settle(app, pilot)
            round_ = app.rounds[0]
            return (mid, round_.collapsed, plain(round_.title), [b.collapsed for b in round_.blocks],
                    plain(round_.answered.content), plain(round_.question))

    mid, open_, title, blocks, answer, question = asyncio.run(scene())
    assert mid == (1, False, "第 1 轮 · 进行中", True)                       # 进行中：整轮摊着
    assert open_ is False and title.startswith("第 1 轮 · 3 步 · ")            # 收尾：整轮仍然摊着
    assert title.endswith("· 拒 1") and question.startswith("＃ 干活")
    assert blocks and all(blocks)                                            # 每一次思考都自己折了
    assert answer == "完成，写在 runs\\ok.md"                    # 最终回答没有跟着过程一起折掉


def test_each_thought_folds_when_the_next_one_starts():
    """折叠的正确时机：下一次思考一开始，上一次那块就折起——不等整轮跑完一股脑收。"""
    async def scene():
        app, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            round_ = await app._begin_round("seg-x")
            first = await round_.begin_step()
            first.add(Text("想第一步"))
            first.verdict = "放行 0.5s"
            was_open = first.collapsed
            second = await round_.begin_step()
            second.add(Text("想第二步"))
            mid_second = second.collapsed
            round_.answer(Text("这是最终回答"))
            await pilot.pause()
            return was_open, first.collapsed, mid_second, plain(first.title), plain(round_.lines)

    was_open, first_now, second_open, first_title, lines = asyncio.run(scene())
    assert (was_open, first_now, second_open) == (False, True, False)        # 起了第二步，第一步折起
    assert first_title == "思考 1 · 放行 0.5s"                                # 折起来仍看得见那次做了什么
    assert "想第一步" in lines and "这是最终回答" in lines                       # 折的是显示，不是内容


def test_busy_turn_says_so_in_the_status_bar():
    async def scene():
        gate = threading.Event()
        app, _ = make_app(FakeService(guard=hard_deny, gate=gate))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await submit(app, pilot, "干活")
            await wait_until(pilot, lambda: app.busy, "busy 没亮起来")
            running = app.status_text()
            gate.set()
            await settle(app, pilot)
            return running, app.status_text()

    running, done = asyncio.run(scene())
    assert "· 进行中" in running and "· 进行中" not in done                   # 卡住时得有个"还在跑"的证据


def test_ctrl_b_collapses_and_expands_all_rounds():
    async def scene():
        app, service = make_app()
        states = []
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await submit(app, pilot, "第一件事")
            await settle(app, pilot)
            service.segment = "seg-two"                                     # 换段 = 新对话
            await submit(app, pilot, "第二件事")
            await settle(app, pilot)
            states.append([r.collapsed for r in app.rounds])
            for _ in range(3):
                await pilot.press("ctrl+b")
                await pilot.pause()
                states.append([r.collapsed for r in app.rounds])
            return states, rendered(app)

    states, shown = asyncio.run(scene())
    # 起点是"上一轮收着、刚跑完那轮摊着"；Ctrl+B 三次依次是 全收 → 全摊 → 全收。
    assert states == [[True, False], [True, True], [False, False], [True, True]]
    assert "── 新对话 ──" in shown                                          # 段与段之间画了分隔
    assert "已全部折叠" in shown                                             # 最后一次按的是折叠


# === 底栏：成本账、档位、步数 ================================================


def test_status_bar_costs_match_the_fake_usage():
    async def scene():
        app, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await submit(app, pilot, "干活")
            await settle(app, pilot)
            return app.status_text()

    status = asyncio.run(scene())
    # usage 两步：prompt 1000+1200=2200，completion 50+300=350，cached 800+400=1200
    assert "本轮 2/12" in status and "累计 2 步" in status
    assert "tokens 2,550 (2,200+350)" in status
    assert "上下文 0.9%" in status                                            # 1200 / 128000
    assert "缓存" not in status and "tok/s" not in status                     # 两个虚荣指标都撤了
    assert "未启用计量" not in status


def test_context_budget_env_is_read_at_call_time(monkeypatch):
    async def scene():
        app, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await submit(app, pilot, "干活")
            await settle(app, pilot)
            return app.status_text()

    monkeypatch.setenv("HARNESS_CONTEXT_BUDGET", "4000")                    # 分母 1200/4000
    assert "上下文 30.0%" in asyncio.run(scene())


def test_status_bar_says_metering_disabled_without_usage():
    async def scene():
        app, _ = make_app(FakeService(guard=hard_deny, usage=((0, 0, 0), (0, 0, 0))))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await submit(app, pilot, "干活")
            await settle(app, pilot)
            return app.status_text()

    status = asyncio.run(scene())
    assert "未启用计量" in status and "0.0%" not in status


def test_local_review_mode_warns_about_the_serial_reviewer_in_the_palette():
    """串行提醒挪进面板条目的 help：常驻状态只留两行（用户要求"外面只看到当前状态，精简界面"）。"""
    async def scene():
        app, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            local = app.status_text()
            help_text = [c.help for c in app.get_system_commands(app.screen)
                         if c.title.startswith("设置 · 审核模式")][0]
            app.tweak("review", 1)                                          # local → online
            return local, help_text, app.status_text()

    local, help_text, online = asyncio.run(scene())
    assert "串行" in help_text and "排队" in help_text
    assert len(local.splitlines()) == 2                                      # 常态就是两行，不多一行
    assert "待生效" in online.splitlines()[1]                                 # 挂在第二行末尾
    assert "重启对话才生效" in online.splitlines()[2]                          # 第三行只在有事要说时出现


def test_review_mode_cycles_and_steps_bump_via_the_focused_bar():
    async def scene():
        app, service = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.query_one("#status").focus()
            await pilot.pause()
            seen = []
            for _ in range(4):                                              # local→online→off→local→online
                await pilot.press("plus")
                await pilot.pause()
                seen.append(app.review_mode)
            await pilot.press("minus")                                      # 退一格：online→local
            await pilot.pause()
            seen.append(app.review_mode)
            await pilot.press("right_square_bracket")
            await pilot.press("right_square_bracket")
            await pilot.press("left_square_bracket")
            await pilot.press("h")
            await pilot.pause()
            return seen, service.max_iterations, app.status_text()

    seen, steps, status = asyncio.run(scene())
    assert seen == ["online", "off", "local", "online", "local"]             # 循环，不撞墙
    assert steps == 13 and "本轮 0/13" in status                            # 上限写进"本轮 x/上限"那一格
    assert "闸门 开 · 再执行一次才真关" in status                              # h 是二次动作，第一次只武装


def test_steps_are_clamped_to_one_through_forty():
    async def scene():
        app, service = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            service.max_iterations = 40
            for _ in range(5):
                app.tweak("steps", 1)                                       # 越界不许把循环打开
            high = service.max_iterations
            service.max_iterations = 1
            for _ in range(5):
                app.tweak("steps", -1)
            await pilot.pause()
            return high, service.max_iterations, app.status_text()

    high, low, status = asyncio.run(scene())
    assert (high, low) == (40, 1) and "步数上限 1" in status


def test_control_keys_do_not_steal_typing_from_the_input():
    """`+`/`h`/`[` 属于底栏，不属于输入框：焦点在输入框时它们是打字。"""
    async def scene():
        app, service = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            box = app.query_one("#prompt", PromptArea)
            box.focus()
            await pilot.pause()
            await pilot.press("h", "plus", "left_square_bracket")
            await pilot.pause()
            return box.text, app.review_mode, service.max_iterations, app.note

    value, mode, steps, note = asyncio.run(scene())
    assert (value, mode, steps, note) == ("h+[", "local", 12, "")


def test_guard_off_is_reported_honestly():
    async def scene():
        app, _ = make_app(FakeService(guard=no_guard))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            return app.status_text()

    assert "闸门 关 · 模型说什么就干什么" in asyncio.run(scene())


# === 轨迹读得回来：旧格式、坏文件、抢锁 =====================================


def test_legacy_trajectory_without_segment_still_renders():
    """旧 uuid 命名、没有 segment 的轨迹：读得出来，对话号标"未绑定"，不许崩。"""
    async def scene():
        app, service = make_app(seed(FakeService(guard=hard_deny), legacy=True))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            return app, service, rendered(app)

    app, service, shown = asyncio.run(scene())
    assert "对话 未绑定" in shown
    assert "＃ 旧任务" in shown and "审核 拒绝" in shown
    assert app.rounds[0].collapsed is False                    # 回放出来的对话要真看得见，不是只剩一行标题
    assert app.rounds[0].denials == 1 and service.calls == []                 # 回放不经过 chat


def test_bound_trajectory_labels_its_segment():
    async def scene():
        app, _ = make_app(seed(FakeService(guard=hard_deny), legacy=False))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            return plain(app.query_one("#title", Static).content)

    assert "对话 seg-old" in asyncio.run(scene())


def test_unreadable_trajectory_does_not_take_the_ui_down():
    """坏轨迹（load 抛异常）就当空的：界面照常能用。"""
    service = FakeService(guard=hard_deny)

    def broken(session_id: str) -> Session:
        raise FileNotFoundError(session_id)

    service.store.load = broken

    async def scene():
        app, _ = make_app(service)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await submit(app, pilot, "照样能发")
            await settle(app, pilot)
            return len(app.rounds)

    assert asyncio.run(scene()) == 1


def test_busy_lock_shows_red_text_instead_of_crashing(workdir: Path):
    """抢不到 `WorkspaceLock`：红字说明谁在占用，界面不崩，输入框里的文本还留着。"""
    service = FakeService(guard=hard_deny)
    (workdir / "runs").mkdir(parents=True, exist_ok=True)
    holder = workdir / "runs" / f"{service.session_id}.lock"
    holder.write_text(json.dumps({"pid": os.getpid(), "workspace": str(workdir),
                                  "cmd": "harness run"}), encoding="utf-8")
    app, _ = make_app(service, base=workdir)

    async def scene():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await submit(app, pilot, "别挤")
            await wait_until(pilot, lambda: not app.busy, "抢锁失败也算跑完")
            await pilot.pause()
            return rendered(app), app.query_one("#prompt", PromptArea).text

    try:
        shown, left = asyncio.run(scene())
    finally:
        WorkspaceLock(holder, workdir, "x").release()                        # 本进程"持有"的假锁，手动清掉
    assert "这个工作区已有进程在跑：" in shown
    assert app.lock_error and "还在运行" in app.lock_error                    # 占用者判活也如实带上
    assert service.calls == [] and left == "别挤"                             # 没送到 service，文本不许丢


def test_lock_is_held_while_the_turn_runs_and_released_after(workdir: Path):
    async def scene():
        service = FakeService(guard=hard_deny, steps=1, deny_at=None)
        app, _ = make_app(service, base=workdir)
        path = workdir / "runs" / f"{service.session_id}.lock"
        gate = threading.Event()
        service.gate = gate
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await submit(app, pilot, "干活")
            await wait_until(pilot, path.exists, "跑起来了却没拿到锁")
            gate.set()
            await settle(app, pilot)
            return path.exists()

    assert asyncio.run(scene()) is False


def test_a_second_submit_while_busy_is_refused_not_written_twice():
    async def scene():
        gate = threading.Event()
        app, service = make_app(FakeService(guard=hard_deny, gate=gate))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await submit(app, pilot, "第一条")
            await wait_until(pilot, lambda: app.busy, "第一条没跑起来")
            box = app.query_one("#prompt", PromptArea)
            box.load_text("第二条")
            await pilot.press("enter")
            await pilot.pause()
            note, queued = app.note, len(service.calls)
            gate.set()
            await settle(app, pilot)
            return note, queued, box.text

    note, queued, left = asyncio.run(scene())
    assert "还在跑" in note and queued == 1
    assert left == "第二条"                                                   # 被挡住的那条不丢文本


# === 工作区可选：面板入口（Ctrl+N 已撤，见 test_ctrl_n_is_no_longer_bound） ===


def test_picker_calls_on_exit_with_the_chosen_workspace():
    chosen: list[Path] = []

    async def scene():
        app, _ = make_app(workspaces=(WORKSPACE, OTHER), on_exit=chosen.append)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.workspace == WORKSPACE
            app.action_pick_workspace()
            await wait_until(pilot, lambda: type(app.screen).__name__ == "WorkspacePicker", "选择屏没出来")
            await pilot.pause()                                             # 屏上了栈还得等 compose 走完才有 #wsopts
            app.screen.query_one("#wsopts").focus()
            await pilot.pause()
            await pilot.press("down")                                        # 第二个候选
            await pilot.pause()
            await pilot.press("enter")

    asyncio.run(scene())
    assert chosen == [OTHER]


def test_picker_accepts_a_typed_path():
    chosen: list[Path] = []

    async def scene():
        app, _ = make_app(on_exit=chosen.append)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.action_pick_workspace()
            await wait_until(pilot, lambda: type(app.screen).__name__ == "WorkspacePicker", "选择屏没出来")
            await pilot.pause()                    # 同上：#wsman 要等 compose 完成才存在
            box = app.screen.query_one("#wsman", Input)
            box.focus()
            box.value = str(OTHER / "手输")
            await pilot.press("enter")

    asyncio.run(scene())
    assert chosen == [OTHER / "手输"]


def test_escape_closes_the_picker_and_changes_nothing():
    chosen: list[Path] = []

    async def scene():
        app, _ = make_app(on_exit=chosen.append)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.action_pick_workspace()
            await wait_until(pilot, lambda: len(app.screen_stack) == 2, "选择屏没压上栈")
            await pilot.press("escape")
            await wait_until(pilot, lambda: len(app.screen_stack) == 1, "Esc 没关掉选择屏")
            return app.workspace

    assert asyncio.run(scene()) == WORKSPACE
    assert chosen == []


def test_switching_workspace_without_a_callback_only_records_it():
    """没给 on_exit 就只记账并说清 service 没动——界面不假装换了目录就真换了。"""
    async def scene():
        app, service = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._workspace_picked(OTHER)
            await pilot.pause()
            return app.workspace, app.status_text(), service.calls

    workspace, status, calls = asyncio.run(scene())
    assert workspace == OTHER and calls == []
    assert "换工作区没生效" in status


def test_the_workspace_row_shows_the_services_directory_not_the_first_candidate():
    """屏幕上那行"工作区"必须来自 service 自己：cli 传的是排过序的全部候选目录，取第一个就是骗人。

    这条不是措辞问题——用户照着标签判断"我在哪个目录写文件"，标签停在 `01_env_report` 而 service 早已
    绑到别的目录，于是 7 次越界写入全被硬规则拦掉，白花 token。
    """
    async def scene(mode: str):
        service = FakeService(guard=hard_deny)
        if mode == "tool":
            service.assistant = SimpleNamespace(tools=(FakeTool(WORKSPACE),))
        elif mode == "guard":
            service.guard = partial(hard_deny, workspace=str(WORKSPACE))
        app = HarnessApp(service, workspaces=(OTHER, WORKSPACE),
                         workspace=WORKSPACE if mode == "param" else None)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            return plain(app.query_one("#title", Static).content), app.service_workspace()

    for mode in ("tool", "guard"):                        # service 自己带得出真值的两条路
        shown, live = asyncio.run(scene(mode))
        assert live == WORKSPACE and str(WORKSPACE) in shown               # 真值在行上
        assert str(OTHER) not in shown                                     # 候选列表第一个不算数
    shown, live = asyncio.run(scene("param"))             # 问不出真值，才退回调用方显式给的那个
    assert live is None and str(WORKSPACE) in shown and str(OTHER) not in shown


def test_the_picker_marks_the_directory_the_service_is_actually_in():
    """换工作区那屏要把"当前"标在真值上，否则用户在一串候选里根本不知道自己站在哪儿。"""
    async def scene():
        service = FakeService(guard=partial(hard_deny, workspace=str(WORKSPACE)))
        app = HarnessApp(service, workspaces=(OTHER, WORKSPACE))
        async with app.run_test(size=(120, 40)) as pilot:
            app.action_pick_workspace()
            await pilot.pause()
            listed = app.screen.query_one(OptionList)
            return [str(option.prompt) for option in listed.options]

    rows = asyncio.run(scene())
    assert len(rows) == 2 and "当前" in rows[1] and "当前" not in rows[0]


def test_a_multiline_prompt_goes_out_whole_and_the_box_grows_then_resets():
    """多行文本进得来也发得出：单行 `Input` 粘贴只留第一行，长 prompt 静默掉一截。

    键位按你要的来：Shift+Enter 换行、Enter 发送；`Ctrl+J` 是同一动作的兜底键（传统 conhost 把
    修饰键和 Enter 压成同一个字节，分不出来，Ctrl+J 是独立的 0x0A）。
    """
    async def scene(newline: str):
        app, service = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            box = app.query_one("#prompt", PromptArea)
            box.focus()
            await box._on_paste(Paste(text="第一段\n第二段\n第三段"))
            await pilot.pause()
            pasted = box.text
            await pilot.press(newline)
            await pilot.pause()
            before_send = (box.text, len(service.calls))
            await pilot.press("enter")
            await settle(app, pilot)
            return pasted, before_send, service.calls, box.text, box._rows

    for newline in ("shift+enter", "ctrl+j"):
        pasted, before_send, calls, after, rows = asyncio.run(scene(newline))
        assert pasted == "第一段\n第二段\n第三段"                     # 粘贴的三行一个字没丢
        assert before_send[0] == pasted + "\n" and before_send[1] == 0  # 换行键只换行，不发送
        assert [call[1] for call in calls] == [pasted]                    # 整段原样交给 service
        assert (after, rows) == ("", 1)                                   # 发完清空并缩回一行


def test_the_prompt_box_stops_growing_at_five_rows():
    """输入框随内容长高，但最多五行：再多就把上面的对话挤没了，那不是"精简界面"。"""
    async def scene(lines: int):
        app, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            box = app.query_one("#prompt", PromptArea)
            box.load_text("\n".join(["写点什么"] * lines))
            box.post_message(TextArea.Changed(box))
            await pilot.pause()
            return box._rows, box.styles.height.value

    assert asyncio.run(scene(1)) == (1, 3)             # 一行话：正文 1 行 + 上下边框 2 行
    rows, height = asyncio.run(scene(12))
    assert (rows, height) == (5, 7)                       # 十二行也只给五行正文，多出来的自己滚


def test_the_prompt_is_not_a_single_line_input_anymore():
    """结构约束：`#prompt` 不再是 textual 的 `Input`，否则那条 `splitlines()[0]` 又把多行砍掉。"""
    async def scene():
        app, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            box = app.query_one("#prompt", Widget)
            return type(box).__name__, isinstance(box, Input), box.placeholder

    name, is_input, placeholder = asyncio.run(scene())
    assert (name, is_input) == ("PromptArea", False)
    assert "Enter 发送" in placeholder and "Shift+Enter 换行" in placeholder


def test_the_help_screen_lists_the_newline_keys():
    """键位屏要能查到换行键，否则没人知道 Shift+Enter / Ctrl+J 的存在。"""
    async def scene():
        app, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.action_show_keys()
            await wait_until(pilot, lambda: len(app.screen.query(Static)) > 1, "帮助屏没起来")
            body = "".join(str(static.content) for static in app.screen.query(Static))
            return body

    body = asyncio.run(scene())
    assert "Enter" in body and "Shift+Enter" in body and "Ctrl+J" in body


def test_a_task_naming_a_path_outside_the_workspace_is_flagged_before_it_runs():
    """任务里写了别的目录，就在发问那一刻说清"往那儿写会被拦"，而不是让模型撞第七次硬规则。

    实测过代价：`sessions/experiment_3-4ded7f5f.jsonl` 35 行里 7 行是策略拒绝，同一个 `README.md`
    被连交 4 次——每一次都是一整轮模型请求。
    """
    async def scene(text: str, guard=hard_deny):
        service = FakeService(guard=guard)
        service.assistant = SimpleNamespace(tools=(FakeTool(WORKSPACE),))
        app = HarnessApp(service, workspaces=(WORKSPACE,))
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await submit(app, pilot, text)
            await settle(app, pilot)
            return app.status_text()

    away = asyncio.run(scene(r"把 C:\Users\me\Documents\实验三\README.md 写好"))
    back = asyncio.run(scene(f"把 {WORKSPACE / 'README.md'} 写好"))
    loose = asyncio.run(scene(r"把 C:\Users\me\Documents\实验三\README.md 写好", no_guard))
    assert "会被硬规则拦下" in away and "换工作区" in away
    assert "会被硬规则拦下" not in back                             # 写在自己目录里就不该啰嗦
    assert "会被硬规则拦下" not in loose                             # 闸门关了没人拦，别撒谎


def test_collapsing_a_round_actually_hides_its_children():
    """折叠不是改个标志就完事：里面的东西必须在屏幕上没有位置。

    `Collapsible` 的本体不是容器，内容住在内部 `Contents` 里，隐藏规则是 `-collapsed > Contents`。
    后挂的 widget 一旦挂在本体上就永远露在外面——截图抓到过"标题写折叠、思考过程还在屏上"。
    """
    async def scene():
        app, _ = make_app(FakeService(guard=hard_deny, closing="第一件事完了"))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await submit(app, pilot, "第一件事")
            await settle(app, pilot)
            round_ = app.rounds[0]
            open_ = (round_.asked.region.height, round_.answered.region.height)
            round_.collapsed = True
            await pilot.pause()
            return open_, (round_.asked.region.height, round_.answered.region.height)

    open_, hidden = asyncio.run(scene())
    assert all(height > 0 for height in open_)                     # 摊开时问题与回答都占行
    assert hidden == (0, 0)                                        # 折起来就得真的没位置


def test_the_previous_round_disappears_when_the_new_one_starts():
    """上一整轮折起来之后，屏幕上不该还留着它的问题、思考和回答。"""
    async def scene():
        app, service = make_app(FakeService(guard=hard_deny, closing="这一轮说完了"))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await submit(app, pilot, "第一件事")
            await settle(app, pilot)
            service.segment = "seg-two"
            await submit(app, pilot, "第二件事")
            await settle(app, pilot)
            old, new = app.rounds
            return (old.collapsed, old.asked.region.height, old.answered.region.height,
                    new.collapsed, new.asked.region.height, new.answered.region.height)
    old_collapsed, old_ask, old_answer, new_collapsed, new_ask, new_answer = asyncio.run(scene())
    assert (old_collapsed, old_ask, old_answer) == (True, 0, 0)     # 旧轮真的收干净了
    assert (new_collapsed, new_ask > 0, new_answer > 0) == (False, True, True)


def test_review_mode_survives_for_the_caller_to_read():
    """`main.py tui` 靠 `getattr(harness, "review_mode")` 决定要不要重建 service：属性名是契约。"""
    async def scene():
        app, _ = make_app()
        assert app.review_mode == REVIEW_MODES[0]                    # 没 HARNESS_REVIEW 时从 local 起步
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.tweak("review", 1)
            app.tweak("review", 1)
            return app.review_mode

    assert asyncio.run(scene()) == "off"


def test_review_mode_starts_from_the_caller_and_the_env(monkeypatch):
    """档位初值：调用方显式传的 > `HARNESS_REVIEW` > local。认不得的值不许带病开局。"""
    monkeypatch.setenv("HARNESS_REVIEW", "online")
    assert make_app()[0].review_mode == "online"                  # main.py tui --review online 传下来的
    assert HarnessApp(FakeService(guard=hard_deny), review_mode="off").review_mode == "off"
    monkeypatch.setenv("HARNESS_REVIEW", "没这种档位")
    assert make_app()[0].review_mode == "local"


def test_tier_is_back_derived_from_the_injected_reviewer():
    """`--review online` 只交给 `main.build`，界面不知道档位——那就去看 service 挂的审核客户端。

    写死 "local" 比留空更糟：那种底栏会让人以为审核在本地跑，而真链子正在花在线 token。
    反推出来了就标一句"推断"，让人知道这值不是启动参数给的。
    """
    async def scene(url: str, reviewer: object) -> tuple[str, str, str]:
        service = FakeService(guard=hard_deny)
        service.reviewer_client = reviewer or object()
        app, _ = make_app(service)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            start, before = app.review_mode, app.status_text()
            app.tweak("review", 1)                                  # 人肉改过就不再说"推断"
            await pilot.pause()
            return start, before, app.tier_source

    class Client:                                                   # 只借 base_url 这一个属性
        def __init__(self, base_url: str) -> None:
            self.base_url = base_url

    mode, status, source = asyncio.run(scene("https://dashscope.aliyuncs.com/compatible-mode/v1",
                                             Client("https://dashscope.aliyuncs.com/x")))
    assert (mode, source) == ("online", "")
    assert "档位 online · 推断" in status
    mode, status, _ = asyncio.run(scene("http://127.0.0.1:11434/v1", Client("http://127.0.0.1:11434/v1")))
    assert mode == "local" and "档位 local · 推断" in status
    mode, status, _ = asyncio.run(scene("off", NoReviewClient()))
    assert mode == "off" and "档位 off · 推断" in status
    mode, status, _ = asyncio.run(scene("不认识", None))              # 认不出来就回落到 local，但绝不标推断
    assert mode == "local" and "推断" not in status


# === 闸门是真开关：h 与面板条目都真写 service.guard ==========================


def test_guard_really_flips_and_flips_back_without_restarting():
    """老实现的 `h` 只写了一句"换它得重启 harness"，压根没赋值——那是缺口，不是设计。

    现在的纪律：关要走二次确认（第一次只武装），开一下就行；开回去装的是**启动时注进来的原物**，
    界面不自己 partial 一个新闸门（装配权在组装根，工作区绑定也在那个原物里）。
    """
    async def scene():
        app, service = make_app(FakeService(guard=hard_deny))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.toggle_guard()
            armed = app.guard_state(), app.status_text()
            app.toggle_guard()
            off = service.guard, app.guard_state(), app.status_text()
            app.toggle_guard()
            await pilot.pause()
            return armed, off, service.guard, app.status_text()

    (state, armed_text), (guard_off, state_off, text_off), guard_on, text_on = asyncio.run(scene())
    assert state == "on" and "再执行一次才真关" in armed_text                  # 第一次只是武装
    assert guard_off is no_guard and state_off == "off"
    assert "闸门 关 · 模型说什么就干什么" in text_off                           # 关着必须一直亮着
    assert guard_on is hard_deny and "闸门 开" in text_on                       # 装回原物，当场生效


def test_ui_refuses_to_fabricate_a_guard_it_was_not_given():
    """启动时 --no-guard：界面只说"要开得重启"，不假装自己装上了闸门。"""
    async def scene():
        app, service = make_app(FakeService(guard=no_guard))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.toggle_guard()
            await pilot.pause()
            return service.guard, app.note

    guard, note = asyncio.run(scene())
    assert guard is no_guard and "启动时就没开" in note


def test_ctrl_n_is_no_longer_bound_because_windows_terminal_owns_it():
    """WT 默认把 Ctrl+N 绑成"新建窗口"：留着它就是给用户多开一扇窗，而不是切工作区。"""
    keys = [key for binding in HarnessApp.BINDINGS for key in binding.key.split(",")]
    assert "ctrl+n" not in keys
    assert {"ctrl+q", "ctrl+b"} <= set(keys)


# === 焦点：看不见 = 调不动，所以两件事都要有证据 ==============================


def test_the_log_takes_no_focus_and_the_bar_shows_when_it_has_it():
    """`VerticalScroll` 默认**可聚焦**（containers.py:32），实测 Tab 环里因此多一个"无样式、无可用键"的站。

    摘掉它；底栏聚焦改看 `background` + `text_style`。原来用 `outline`，截图实测在 30 行高的窗口里
    它画在控件外的两行会把状态栏第一行那行账挤出屏幕：焦点看得见了，步数反而没了。
    不押注样式类型，只比"聚焦前后变了没"——样式细节是 textual 的事，可见性才是这里的要求。
    """
    def look(bar) -> str:
        return f"{bar.styles.background}/{bar.styles.text_style}"

    async def scene():
        app, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            log, bar = app.query_one("#log"), app.query_one("#status")
            before, drawn_off = look(bar), app.status_text()
            stops = []
            for _ in range(3):
                await pilot.press("tab")
                stops.append(app.focused.id)
            await pilot.pause()
            return log.can_focus, stops, before != look(bar), drawn_off, app.status_text()

    focusable, stops, changed, drawn_off, drawn_on = asyncio.run(scene())
    assert focusable is False and stops == ["status", "prompt", "status"]
    assert changed, "聚焦底栏时屏幕上看不出任何变化"
    assert "▸" not in drawn_off.splitlines()[0]                      # 没焦点就没标记
    assert drawn_on.splitlines()[0].startswith("▸")                  # 底色/下划线之外还有个字形，无色终端也看得出


def test_step_keys_are_typing_until_focus_moves():
    """"步数上限根本调不了"的真相：键没坏、钳制没坏、显示没坏，是焦点在输入框。转正成用例，别再当玄学。"""
    async def scene():
        app, service = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("right_square_bracket")
            await pilot.pause()
            typed = app.query_one("#prompt", PromptArea).text, service.max_iterations
            await pilot.press("tab")
            await pilot.press("right_square_bracket")
            await pilot.pause()
            return typed, service.max_iterations, app.status_text()

    (value, before), after, status = asyncio.run(scene())
    assert (value, before) == ("]", 12)                                   # 没挪焦点就是打字
    assert after == 13 and "步数上限 13" in status                          # 挪一下就能调


# === Ctrl+P 面板：功能都在里面，标题写的就是此刻的真值 ========================


def own_titles(app) -> list[str]:
    """只取我们自己的条目（前缀 `设置 ·/视图 ·/跳转 ·/文件 ·/帮助 ·`）。

    8.2.8 的 `SystemCommand` 只有 (title, help, callback, discover)，没有 category/priority，
    排序纯按模糊匹配分数——分类只能靠标题命名，这是实测出来的边界，不是偷懒。
    """
    mine = ("设置", "视图", "跳转", "文件", "帮助")
    return [command.title for command in app.get_system_commands(app.screen)
            if command.title.split(" · ")[0] in mine]


def test_palette_lists_every_control_with_its_current_value():
    async def scene():
        app, service = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            before = own_titles(app)
            app.set_steps("30")                                           # 面板那条写的是上限，不是"上次改到"
            return before, own_titles(app), service.max_iterations

    before, after, limit = asyncio.run(scene())
    assert "设置 · 闸门 开" in before and "设置 · 步数上限 12" in before
    assert "设置 · 步数上限 30" in after and limit == 30
    assert not any("±" in title or "步数上限 +" in title for title in after)   # 逐格调那条按用户要求删了
    assert "帮助 · 键位清单" in after and "文件 · 打开 sessions 目录" in after
    assert "跳转 · 重启界面" not in " ".join(after)                          # 档位没换就不该冒出这条


def test_a_pending_tier_shows_the_restart_entry_and_the_restart_flag():
    """档位换不当场生效，那就必须有一条路把它落地，而不是让人对着一行小字猜"到底生效了没"。"""
    async def scene():
        app, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            empty = app.pending_note()
            app.tweak("review", 1)
            titles, status = own_titles(app), app.status_text()
            request = app.restart_request
            app.action_restart_ui()
            return empty, titles, status, request, app.restart_request

    empty, titles, status, before, after = asyncio.run(scene())
    assert empty == "" and before is False
    assert any(title.startswith("跳转 · 重启界面 · 应用档位 online") for title in titles)
    assert "待生效" in status and "重启界面" in status
    assert after is True                                                  # cli 据此重建 service 再开一次


def test_value_commands_reopen_the_palette_so_editing_feels_local():
    """改值类命令执行后回到面板、停在原位：改一个数要重开一次面板，就是用户抱怨的那 half 成本。"""
    async def scene(reopen: bool):
        app, service = make_app()
        app.reopen_panel = reopen
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            command = [c for c in app.get_system_commands(app.screen)
                       if c.title.startswith("设置 · 审核模式")][0]
            command.callback()
            await pilot.pause()
            if reopen:
                await wait_until(pilot, lambda: isinstance(app.screen, CommandPalette), "面板没重开")
            else:
                await pilot.pause(0.2)              # 给定时器一次机会：不该重开就是真的不重开
            return isinstance(app.screen, CommandPalette), app.review_mode, service.max_iterations

    open_again, mode, _ = asyncio.run(scene(True))
    assert (open_again, mode) == (True, "online")                     # 面板还开着，值已经换了
    assert asyncio.run(scene(False))[0] is False                      # 关掉重开就不弹：给用例留的开关


def test_step_prompt_sets_a_value_in_one_shot_and_empty_input_changes_nothing():
    async def scene(give: str):
        app, service = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.push_screen(ValuePrompt("步数上限（1–40）", "12", "回车确认"), app._steps_prompted)
            await wait_until(pilot, lambda: type(app.screen).__name__ == "ValuePrompt", "输入屏没出来")
            await pilot.pause()                    # compose 之后才有 #vman
            box = app.screen.query_one("#vman", Input)
            box.value = give
            await pilot.press("enter")
            await wait_until(pilot, lambda: type(app.screen).__name__ == "Screen", "没退回对话")
            return service.max_iterations, app.note

    assert asyncio.run(scene("9"))[0] == 9
    steps, note = asyncio.run(scene("不是数字"))
    assert steps == 12 and "不是整数" in note                               # 手滑打错不改变任何东西


# === 只读轨迹屏与 sessions 目录 ==============================================


def test_trajectory_screens_are_read_only_and_escape_returns(workdir):
    async def scene(raw: bool):
        app, service = make_app(FakeService(guard=hard_deny), base=workdir)
        (workdir / "sessions").mkdir(parents=True, exist_ok=True)          # 同一个用例里跑两遍原文/对话
        (workdir / "sessions" / f"{service.session_id}.jsonl").write_text(
            '{"role":"user","text":"数一数"}\n{"role":"assistant","text":"好"}\n', encoding="utf-8")
        seed(service, legacy=False)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._view_trajectory(raw)
            await wait_until(pilot, lambda: isinstance(app.screen, TrajectoryScreen), "轨迹屏没出来")
            heading, rounds = app.screen.heading, len(app.rounds)
            await pilot.press("escape")
            await wait_until(pilot, lambda: type(app.screen).__name__ == "Screen", "Esc 没回到对话")
            return heading, rounds, len(app.screen_stack)

    raw, rounds, depth = asyncio.run(scene(True))
    assert "轨迹原文" in raw and raw.rstrip().endswith(".jsonl")            # 标题里就是那个文件
    assert rounds == 1
    heading, _, depth = asyncio.run(scene(False))
    assert "轨迹对话 · 3 条消息" in heading and depth == 1                  # 条数来自轨迹，不是屏上轮次


def test_transcript_renders_the_trajectory_without_a_ui():
    """`transcript` 是纯函数：只读屏用的是它，字段口径和 `_step` 一致（一步 = 一次 assistant 回复）。"""
    call = ToolCall("c1", "shell", {"command": "Get-Date"})
    body = transcript([UserMessage("干活", ts=1.0, segment="s"),
                       AssistantMessage("", (call,), ts=2.0, prompt_tokens=10, completion_tokens=2),
                       ToolResult("c1", json.dumps({"review": "allowed by reviewer", "output": "ok"},
                                                   ensure_ascii=False), ts=3.0, review_ms=1.5)])
    assert "── 第 1 轮 ──" in body.plain and "→ shell" in body.plain
    assert "审核 allowed by reviewer · 2ms" in body.plain and "↳ ok" in body.plain


def test_opening_the_sessions_folder_says_where_it_was(workdir, monkeypatch):
    """没有 os.startfile 或目录不存在都要留话，不许静默失败——静默的"打开目录"就是"点了没反应"。

    这里不用 pytest 的 `tmp_path`：本机它的根在 `C:\\Windows\\Temp\\dsh-*`，沙箱里创建会
    `PermissionError [WinError 5]`，用 `workdir`（RUN_ROOT 下）自己开子目录。
    """
    opened: list[str] = []
    monkeypatch.setattr(os, "startfile", lambda path: opened.append(path))

    async def scene(base: Path):
        app, _ = make_app(base=base)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.action_open_sessions()
            await pilot.pause()
            return app.note

    (workdir / "sessions").mkdir()
    note = asyncio.run(scene(workdir))
    assert "已在资源管理器里打开" in note and Path(opened[-1]) == workdir / "sessions"
    assert "还不存在" in asyncio.run(scene(workdir / "这个目录压根没有"))


# === 状态行不提宿主；面板里不许有没要求过的英文条目 ==========================


def test_the_status_line_says_nothing_about_the_host(monkeypatch):
    """宿主与鼠标归 `doctor` 第十项，不归常驻状态行：那一格本来是档位/闸门/上下文的位置。

    原来这里挂着「本宿主无鼠标（详见 doctor）」，是**我自己加的**，你只要求过"外面看到工作区、统计、
    token、状态"。现在无论 `WT_SESSION` 在不在，状态行都不许提它。
    """
    async def scene():
        app, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            plain = app.status_text()
            monkeypatch.setenv("WT_SESSION", "bld666")
            app._refresh()
            return plain, app.status_text()

    conhost, wt = asyncio.run(scene())
    for status in (conhost, wt):
        assert "宿主" not in status and "鼠标" not in status
    assert "上下文" in conhost and "Ctrl+P 命令与设置" in conhost      # 该在的两行还在


def test_the_palette_carries_no_english_builtins_because_none_were_asked_for():
    """`Ctrl+P` 顶上那几行英文是 `App.get_system_commands` 白送的，一条都没要过，所以不调 `super()`。"""
    async def scene():
        app, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            return "\n".join(c.title for c in app.get_system_commands(app.screen))

    titles = asyncio.run(scene())
    for english in ("Theme", "Quit", "Keys", "Maximize", "Minimize", "Screenshot"):
        assert english not in titles, english
    assert "设置 · 闸门 开" in titles and "文件 · 打开 sessions 目录" in titles
    assert "最近一轮" not in titles       # 单轮折叠由标题上的 ▶/▼ 与 Ctrl+B 管，面板里不占一条（没要求过）


def test_the_palette_search_box_is_not_english_either():
    """面板本体是 textual 的，但那句 `Search for commands…` 不是我们界面的语言，所以用 `Palette` 子类改。

    留着的一处英文：搜不到匹配时 textual 在私有方法里现建 `No matches found` 那条灰字
    （`command.py:889`），子类接不上——乱打字才看得见，常态屏上没有。
    """
    async def scene():
        app, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            app.action_command_palette()
            await pilot.pause()
            return app.screen.query_one(Input).placeholder

    placeholder = asyncio.run(scene())
    assert "输入要执行的命令" in placeholder and "Search" not in placeholder


def test_the_help_entry_opens_its_own_screen_and_closes_again():
    """「帮助 · 键位清单」开单独一屏，Esc 回对话；再执行一次这条就是把那屏关掉，状态行一个字都不改。

    上一版我把清单写进状态行的 note，于是"出现了快捷键却消不掉"——临时说明挂进常驻区，就得再造一个
    撤销动作，两头都是白活。现在借用只读轨迹屏那个类：多一屏，但不多一套机制。
    """
    async def scene():
        app, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            before = app.status_text()
            app.action_show_keys()
            await wait_until(pilot, lambda: isinstance(app.screen, TrajectoryScreen), "键位屏没开")
            await wait_until(pilot, lambda: len(app.screen.query(Static)) > 1, "键位正文没挂上")
            body = "\n".join(plain(w.content) for w in app.screen.query(Static))
            app.action_show_keys()                            # 再执行一次这条：那屏该消失
            await wait_until(pilot, lambda: type(app.screen).__name__ == "Screen", "再点一次没消掉")
            app.action_show_keys()
            await wait_until(pilot, lambda: isinstance(app.screen, TrajectoryScreen), "第二次开不出来")
            await pilot.press("escape")
            await wait_until(pilot, lambda: type(app.screen).__name__ == "Screen", "Esc 没回对话")
            return body, before == app.status_text()

    body, untouched = asyncio.run(scene())
    assert "Ctrl+P" in body and "Tab" in body and "开关闸门，关要按两次" in body
    assert not any(word in body for word in ("Quit", "Maximize", "preserve_screen"))   # 不是那份英文表
    assert untouched is True                                  # 说明没借状态行的地方写


def test_replaying_a_trajectory_opens_the_most_recent_segment():
    """换工作区重启起来，屏幕上得真是那个目录聊过的话：最近一段摊开，更早的收成一行标题。

    全折叠看着就像"对话没了"（用户原话），全展开又会被旧内容占满屏——回放的分寸就在这两难中间。
    """
    async def scene():
        service = FakeService(guard=hard_deny)
        service.store.sessions[service.session_id] = Session(service.session_id, [
            UserMessage("第一段的问题", ts=1_700_000_000.0, segment="seg-A"),
            AssistantMessage("第一段答完", (), ts=1_700_000_001.0, prompt_tokens=500, completion_tokens=20),
            UserMessage("第二段的问题", ts=1_700_000_100.0, segment="seg-B"),
            AssistantMessage("第二段答完", (), ts=1_700_000_101.0, prompt_tokens=600, completion_tokens=30),
        ])
        app, _ = make_app(service)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            return ([r.collapsed for r in app.rounds], rendered(app), plain(app.rounds[-1].lines),
                    app.segment, plain(app.rounds[0].title))

    collapsed, shown, last, segment, first_title = asyncio.run(scene())
    assert collapsed == [True, False]                    # 最近这段整轮摊开，更早的收成一行标题
    assert "进行中" not in first_title                                  # 回放没 done 事件，标题自己得收尾
    assert segment == "seg-B" and "第二段答完" in last
    assert "第一段答完" not in last and "── 新对话 ──" in shown                      # 段与段之间仍画分隔


def test_a_workspace_without_any_trajectory_says_so():
    """这个目录一条消息都没有，就写一行说明，别让人对着空白猜是没读到还是真没聊过。"""
    async def scene():
        app, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            first = rendered(app)
            await submit(app, pilot, "第一件事")
            await settle(app, pilot)
            return first, rendered(app)

    first, later = asyncio.run(scene())
    assert "这个工作区还没有对话" in first and "main.py sessions" in first
    assert "还没有对话" not in later                          # 真开始聊了，那句说明就该消失


def test_segments_lists_every_round_and_jump_opens_it():
    """"切换轨迹要保留上下文"的落点：同工作区只切段，屏幕不清空，滚动定位并展开那一轮。"""
    async def scene():
        app, service = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await submit(app, pilot, "第一件事")
            await settle(app, pilot)
            service.segment = "seg-two"
            await submit(app, pilot, "第二件事")
            await settle(app, pilot)
            rows = app.segments()
            collapsed = [round_.collapsed for round_ in app.rounds]
            app._jump_to_round(1)
            await pilot.pause()
            return rows, collapsed, app.rounds[0].collapsed, app.note

    rows, collapsed, opened, note = asyncio.run(scene())
    assert [row[0] for row in rows] == [1, 2] and rows[0][1] == "seg-one"
    assert collapsed == [True, False] and opened is False                 # 最近一轮本来就摊着
    assert "已定位到第 1 轮" in note and rows[0][2] == "第一件事"


def test_identity_line_only_marquees_when_it_cannot_fit():
    """滚动是补救，不是装饰：放得下就不许动，否则白白每 0.22 s 重绘一次顶行。"""
    async def scene():
        app, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            box = app.query_one("#title", Marquee)
            box.show(Text(" 短 "))
            box.on_mouse_move(None)
            short_timer = box._timer
            box.show(Text("工作区 " + "E:\\harness\\quests\\非常长的一个目录名" * 4))
            box.on_mouse_move(None)
            await pilot.pause(0.3)
            moved, full = box.shift, box.text_full
            box.stop_scroll()
            return short_timer, moved, box.shift, full

    short_timer, moved, after_stop, full = asyncio.run(scene())
    assert short_timer is None and moved > 0
    assert after_stop == 0 and full.plain.startswith("工作区 ")


def test_the_marquee_timer_dies_with_the_widget():
    """换工作区会重建整个界面：留着旧表就有一行已经下场的文字还在跳。"""
    async def scene():
        app, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            box = app.query_one("#title", Marquee)
            box.show(Text("工作区 " + "E:\\长" * 60))
            box.on_mouse_move(None)
            running = box._timer is not None
            await box.remove()
            await pilot.pause()
            return running, box._timer

    assert asyncio.run(scene()) == (True, None)


def test_the_wheel_scrolls_the_log_even_though_it_never_takes_focus():
    """`tui.py` 开头写"滚轮是 textual 内建的，换到 WT 自动生效"，这条不能只靠说。

    日志区 `can_focus=False`（Tab 环里没它），但滚轮事件按**指针位置**派发：`Widget
    ._on_mouse_scroll_up` → `_scroll_up_for_pointer`，一格 2 行（scroll_sensitivity），
    到顶就停。所以"焦点不在日志上"不等于"滚不动"——这正是把散键撤进 `Ctrl+P` 之后
    还需要留下的一条滚动路。
    """
    async def scene():
        app, _ = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            log = app.query_one("#log")
            for i in range(40):
                await log.mount(Static(Text(f"第 {i} 行")))
            await pilot.pause()
            log.scroll_end(animate=False)
            await pilot.pause()
            bottom = log.scroll_y
            tick = MouseScrollUp(Offset(6, 6), 6, 6, 0, 3, 0, False, False, False)
            log.post_message(tick)
            await pilot.pause()
            await pilot.pause()
            one = log.scroll_y
            for _ in range(60):                       # 一直滚到顶，越界不该变负
                log.post_message(tick)
                await pilot.pause()
            return log.can_focus, bottom, one, log.scroll_y

    focused, bottom, after_one, after_many = asyncio.run(scene())
    assert focused is False and bottom > 0            # 不聚焦，但滚得动
    assert after_one == bottom - 2                    # 一格 2 行，实测值
    assert after_many == 0                            # 到顶停住
