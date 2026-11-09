"""重设计之后的界面截图：驱动 adapters/tui.py 的真渲染路径，把新交互面的每一种状态导出成 SVG。

覆盖的是这一轮改出来的东西，不是旧图：状态两行新格式（`本轮 x/上限`、整轮秒数，没有 tok/s 与缓存）、
Ctrl+P 面板（标题里写当前值）、闸门二次确认、步数一次到位的输入屏、历史段列表、轨迹原文只读屏、
焦点在状态栏时的可见边框、以及身份行放不下时的悬停滚动。

用法（在本仓库根目录）：
    C:\\conda\\envs\\llm_learn\\python.exe screenshots\\_tools\\shot_v2.py
出 SVG 后再转 PNG：
    rsvg-convert -w 1568 -b "#101010" -o screenshots\\10-xxx.png screenshots\\_tmp\\10-xxx.svg
"""

import asyncio
import json
import sys
import time
from functools import partial
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from textual.events import Paste  # noqa: E402
from textual.widgets import Input  # noqa: E402

from adapters.stores.jsonl import JsonlSessionStore  # noqa: E402
from adapters.tui import HarnessApp, Marquee, PromptArea, SegmentPicker, ValuePrompt  # noqa: E402
from app.policy import hard_deny  # noqa: E402
from domain.roles import Decision  # noqa: E402
from domain.session import (  # noqa: E402
    AssistantMessage,
    Session,
    ToolCall,
    ToolResult,
    UserMessage,
)

OUT = Path("_tmp/shots2")
OUT.mkdir(parents=True, exist_ok=True)
BASE = OUT / "base"                       # 截图专用根：轨迹文件写在这里，绝不碰真的 sessions/
WS = Path("E:/harness/quests/03_review_latency")
SID = "03_review_latency-9f3c21aa"

CALL_OK = ToolCall("c1", "shell", {"command": "python bench\\review_latency.py --runs 5"})
CALL_BAD = ToolCall("c2", "shell", {"command": r'Set-Content -Path C:\Users\x\note.txt -Value "pwn"'})


class FakeStore:
    def __init__(self):
        self.session = Session(SID, [])

    def load(self, session_id):
        return self.session


class FakeService:
    """按脚本回放事件；`sleep` 用 time（run_turn 在 worker 线程里跑，真 service 也是同步的）。"""

    def __init__(self, gap=0.4):
        self.session_store, self.max_iterations = FakeStore(), 12
        self.session_id, self.gap, self.turn = SID, gap, 0
        self.guard = partial(hard_deny, workspace=str(WS))       # 装成 main.build 给的样子

    def new_session(self):
        return self.session_store.session

    def chat(self, session_id, text, on_event=None):
        self.turn += 1
        seg = f"6a1f{self.turn:04d}"

        def emit(kind, **kw):
            payload = kw.get("message") if kind in ("user", "assistant") else kw.get("result")
            if payload is not None:                       # 攒进 session：轨迹屏读的就是这份，不另造假数据
                self.session_store.session.messages.append(payload)
            on_event({"kind": kind, "segment": seg, **kw})

        emit("user", message=UserMessage(text))
        time.sleep(self.gap)
        emit("assistant", message=AssistantMessage("先测一轮延迟，再和上次的数据对齐。", (CALL_OK,),
                                                   prompt_tokens=5850, completion_tokens=180))
        emit("review", call=CALL_OK, decision=Decision(True, "只读跑基准脚本"), review_ms=820.0)
        emit("result", call=CALL_OK, result=ToolResult(CALL_OK.id, json.dumps(
            {"review": "allowed by reviewer", "output": "median 0.82 s  p95 1.31 s"}), review_ms=820.0))
        time.sleep(self.gap)
        if self.turn == 1:
            emit("assistant", message=AssistantMessage("", (CALL_BAD,),
                                                       prompt_tokens=6020, completion_tokens=95))
            emit("review", call=CALL_BAD, source="policy", review_ms=0.3,
                 decision=Decision(False, r"写入路径 C:\Users\x\note.txt 在工作目录之外（命中 \\\Users\\）"))
            emit("result", call=CALL_BAD, result=ToolResult(CALL_BAD.id, json.dumps(
                {"review": "denied by policy: 越界写", "output": ""}), review_ms=0.3))
            time.sleep(self.gap)
        emit("assistant", message=AssistantMessage("中位数从 2.69 s 降到 0.82 s，结论写在 runs\\ab.md。", (),
                                                   prompt_tokens=6180, completion_tokens=64))
        return []


def build(**kwargs):
    return HarnessApp(FakeService(**kwargs.get("svc", {})),
                      workspaces=(WS, Path("E:/harness/quests/t04"), Path("E:/harness/quests/t05")),
                      base=BASE, **{k: v for k, v in kwargs.items() if k != "svc"})


async def shot(app, name):
    (OUT / f"{name}.svg").write_text(app.export_screenshot(title=name), encoding="utf-8")


async def settle(app, pilot, timeout=10.0):
    deadline = time.monotonic() + timeout
    while app.busy and time.monotonic() < deadline:
        await pilot.pause(0.1)
    await pilot.pause()


async def send(app, pilot, text):
    box = app.query_one("#prompt", PromptArea)
    box.focus()
    box.load_text(text)
    await pilot.press("enter")
    await settle(app, pilot)


async def main():
    app = build()
    async with app.run_test(size=(112, 30)) as pilot:
        await pilot.pause()
        box = app.query_one("#prompt", PromptArea)
        box.focus()
        box.load_text("把工作目录里的基准脚本跑 5 轮，把中位数写进 runs\\ab.md")
        await pilot.press("enter")
        await pilot.pause(0.7)
        await shot(app, "10-实时进行中-状态两行新格式")            # 本轮进行中 + 审核结论逐条落地
        await settle(app, pilot)
        await shot(app, "11-一轮结束已折叠")

        await send(app, pilot, "顺便把 p95 也补上")
        await shot(app, "12-两段对话与整轮秒数")

        app.action_command_palette()                                # 它是同步 action，别 await
        await pilot.pause()
        await shot(app, "13-Ctrl-P-面板标题写当前值")
        palette_box = app.screen.query_one(Input)                   # 面板自己的搜索框
        palette_box.value = "设置"
        await pilot.pause()
        await shot(app, "22-Ctrl-P-输入设置只剩三条控制项")
        await pilot.press("escape")
        await pilot.pause()

        app.toggle_guard()                                        # 只武装，不真关
        await pilot.pause()
        await shot(app, "14-闸门二次确认")
        app.toggle_guard()                                        # 真关
        await pilot.pause()
        await shot(app, "15-闸门已关常亮红字")
        app.toggle_guard()                                        # 装回原物
        await pilot.pause()

        app.action_pick_workspace()
        await pilot.pause()
        await shot(app, "16-换工作区-面板之后的入口")
        await pilot.press("escape")

        app.push_screen(_value_prompt(app))
        await pilot.pause()
        await shot(app, "17-步数上限一次到位")
        await pilot.press("escape")

        app.push_screen(_segment_picker(app))
        await pilot.pause()
        await shot(app, "18-历史会话-本工作区的段列表")
        await pilot.press("escape")

        JsonlSessionStore(BASE / "sessions").save(app.service.session_store.session)   # 真序列化器写的真文件
        app._view_trajectory(True)
        await pilot.pause()
        await shot(app, "19-轨迹原文只读屏")
        await pilot.press("escape")

        app.action_show_keys()                                   # 帮助：单独一屏中文键位，Esc 回对话
        await pilot.pause()
        await shot(app, "23-帮助单独一屏")
        await pilot.press("escape")

        app.query_one("#status").focus()                          # 焦点可见性：背景 + 粗体 + 行首 ▸
        await pilot.pause()
        await shot(app, "20-焦点在状态行看得见")

    replay = build(svc={"gap": 0.0})
    old = replay.service.session_store.session.messages          # 预先塞好两段旧对话：界面起来该读回来
    old.extend([
        UserMessage("上次：跑 5 轮基准，把抖动记下来", ts=1_700_000_000.0, segment="6a1d0001"),
        AssistantMessage("跑完了，中位数 2.69 s，抖动主要来自首次加载模型。", (), ts=1_700_000_012.0,
                         prompt_tokens=5200, completion_tokens=41),
        UserMessage("上次：thinking off 再跑一遍", ts=1_700_000_600.0, segment="6a1d0002"),
        AssistantMessage("这次只读基准脚本。", (CALL_OK,), ts=1_700_000_601.0,
                         prompt_tokens=5400, completion_tokens=18),
        ToolResult(CALL_OK.id, json.dumps({"review": "allowed by reviewer", "output": "median 0.82 s"}),
                   ts=1_700_000_603.0, review_ms=810.0),
        AssistantMessage("0.82 s，结论写在 runs\\ab.md。", (), ts=1_700_000_604.2,
                         prompt_tokens=5600, completion_tokens=22),
    ])
    async with replay.run_test(size=(112, 30)) as pilot:          # 换工作区起来时屏幕上有什么
        await pilot.pause()
        await shot(replay, "24-切到别的工作区读回那段对话")

    narrow = build(svc={"gap": 0.05})
    async with narrow.run_test(size=(76, 26), tooltips=True) as pilot:   # tooltips=True 才跑悬停那条链
        box = narrow.query_one("#prompt", PromptArea)
        box.focus()
        box.load_text("先不动，只截悬停" + "\n补一行，看输入框长高")
        await pilot.press("enter")
        await settle(narrow, pilot)
        marquee = narrow.query_one("#title", Marquee)
        marquee.show(narrow._identity())                          # 段号写全，好让这行放不下
        fits = not marquee._overflowing()
        await pilot.hover("#title")
        await pilot.pause(0.5)
        await shot(narrow, "21-身份行放不下-悬停滚动")
        print(f"悬停滚动：overflow={marquee._overflowing()} shift={marquee.shift} 窗口太宽={fits}")

    paste = build(svc={"gap": 0.0})                                        # 26：多行贴进输入框还活着
    async with paste.run_test(size=(112, 30)) as pilot:
        await pilot.pause()
        box = paste.query_one("#prompt", PromptArea)
        box.focus()
        await box._on_paste(Paste(text="按下述提纲写实验报告：\n一、环境与版本\n二、步骤与命令\n三、结论与抖动"))
        await pilot.pause()
        await shot(paste, "26-多行粘贴进输入框")
        rows, shown = box._rows, box.text.count("\n") + 1
        print(f"多行输入框：高度 {rows} 行 · 文档 {shown} 行")

    jail = build(svc={"gap": 0.0})                                    # 25：任务里写了工作区之外的目录
    async with jail.run_test(size=(112, 30)) as pilot:
        await pilot.pause()
        await send(jail, pilot, r"把 C:\Users\pu\Documents\实验三\README.md 按提纲写好")
        await shot(jail, "25-越界写在发问前就被提醒")


def _value_prompt(app):
    return ValuePrompt("步数上限 1 到 40", str(app.service.max_iterations), "回车确认 · Esc 取消")


def _segment_picker(app):
    return SegmentPicker(app.segments())


asyncio.run(main())
print("SVG 已导出:", sorted(p.name for p in OUT.glob("*.svg")))
