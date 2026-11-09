"""适配层·终端界面（textual）：双击 `harness.bat` 就进对话，不用浏览器、不用 conda activate。

为什么用 textual 而不是自己拿 ctypes 画控制台：参考过的 `C:\\SellingKidney\\console.py` 是绝对定位、格子
写死 120x30、长文本不折行，还和 rich 抢同一个 stdout。textual 给的是差分渲染、跟随缩放，外加
`App.run_test()` 的 Pilot 无头驱动——"界面"这件事因此第一次可测（本文件的用例全部零网络）。

分层纪律：本模块**不 import 组装根**（`main.py`），也不 import 任何具体模型客户端。service 由外面注进来，
形状是鸭子类型：`chat(session_id, text, on_event)` / `new_session()` / `max_iterations`（可读可写）/
`guard`（可读可写）/ `session_id` / `session_store.load(id)`。界面要加东西也不许往 `app/` 层塞字段。

屏幕就三块：顶部身份行（放不下时悬停滚动，见 `Marquee`）、按轮折叠的轨迹、输入框下方两条状态行
（`_cost_line` 是账，`_settings_line` 是态）。**功能一律收在 `Ctrl+P` 面板里**，屏幕上不常驻键位表。

三样旋钮的生效时机不一样，界面上的说法必须和代码里的事实一致（来路见 README「根据实际情况改了什么」16–22）：

* **步数上限**：当场生效——`app/service.py` 的循环每轮读一次。
* **闸门**：也当场生效（`app/service.py:119` 每次工具调用现读 `self.guard(call)`）。界面**不装配**新闸门：
  `__init__` 暂存注进来的原物（`main.build()` 用 `partial(hard_deny, workspace=…)` 装配的，工作区绑定在里面），
  关掉换 `no_guard`、打开装回原物；`--no-guard` 起的界面只说"要开得重启"。粘滞的只有同一段的判决缓存。
* **审核档位**：审核客户端在构造时定死，界面记新值并标"待生效"，落地走 `跳转 · 重启界面` 或 `换工作区`
  （置 `restart_request`，装配权仍在组装根手里）。

鼠标只在 Windows Terminal 里有（conhost 不往输入流写鼠标字节，textual 8.2.8 的 win32 驱动也没置
`ENABLE_MOUSE_INPUT`），所以 `harness.bat` 用 `wt.exe -w new` 起窗。点标题折叠、点面板条目、悬停、滚轮都是
textual 内建，换到 WT 自动生效，界面代码一行不改。
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

from rich.cells import cell_len
from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.command import CommandPalette
from textual.containers import VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.widgets import Collapsible, Input, OptionList, Static, TextArea
from textual.widgets._option_list import Option

from adapters.locks import WorkspaceLock, WorkspaceLocked
from app.policy import REVIEW_MODES, no_guard, outside_write
from domain.roles import Decision
from domain.session import AssistantMessage, ToolResult, UserMessage, json_dict

STEP_MIN, STEP_MAX, OUTPUT_CHARS = 1, 40, 200
SILENT = "这一步没说话，直接要执行命令"          # 实测"空 text + 一个工具调用"是常态，不许显示成空白
UNBOUND = "未绑定"                             # uuid 命名的旧轨迹没有 segment
HINT = "Ctrl+P 命令与设置 · Ctrl+Q 退出"      # 散键不写在这里：那行长提示正是"界面不精简"的元凶
ARMIN = "再执行一次才真关"                    # 闸门关闭是二次动作，武装态同时出现在条目与状态行
OFFGUARD = "模型说什么就干什么"                # 关着的闸门必须一直挂在屏幕上，不能是个可能忘掉的小标签

# 键位清单是单独一屏（「帮助 · 键位清单」打开，Esc 回对话），所以排成两列，不再挤成一行状态栏文字。
KEY_ROWS = [
    ("Ctrl+P", "命令面板：闸门、档位、步数、工作区、历史会话、轨迹、帮助都在里面"),
    ("Ctrl+Q", "退出"),
    ("Ctrl+B", "折叠或展开全部轮次"),
    ("Enter", "发送输入框里那段话"),
    ("Shift+Enter", "输入框里换行。有些终端分不出它，那就按 Ctrl+J；粘贴多行本来就完整"),
    ("Tab", "把焦点挪到下面那两条状态行。挪过去之后："),
    ("+ 或 -", "换审核档位，换完要点「跳转 · 重启界面」才落地"),
    ("[ 或 ]", "加减步数上限，下一个请求生效"),
    ("h", "开关闸门，关要按两次"),
    ("滚轮", "翻对话"),
    ("点轮次标题", "展开或收起那一轮。鼠标只有 Windows Terminal 里有，键盘照常能走完"),
]


def keys_screen() -> Text:
    """「帮助」那一屏的正文：左边键、右边干什么，一屏看完，Esc 回对话。"""
    width = max(len(key) for key, _ in KEY_ROWS) + 2
    out = Text()
    for key, what in KEY_ROWS:
        out.append(f"{key:<{width}}", style="bold cyan")
        out.append(what)
        out.append("\n")
    return out


def context_budget() -> int:
    """上下文分母在调用时读 `HARNESS_CONTEXT_BUDGET`（默认 128000）：改了不必重启界面。"""
    try:
        return max(1, int(os.getenv("HARNESS_CONTEXT_BUDGET", "128000")))
    except ValueError:
        return 128_000


def guess_mode(service) -> tuple[str, str]:
    """没被告知档位时，从注进来的审核客户端反推（`off` 是 NoReviewClient，`local` 的 base_url 在本机）。

    反推比读环境变量诚实：`main.py tui --review online` 造出来的 service 真挂着在线客户端，而
    `HARNESS_REVIEW` 可能压根没设——那时底栏写 "local" 就是骗人。认不出来返回空串，调用方自己回落。
    """
    client = getattr(service, "reviewer_client", None)
    if client is None:
        return "", ""
    if type(client).__name__ == "NoReviewClient":      # 按类名认，不 import 具体客户端：分层不破
        return "off", "推断"
    url = str(getattr(client, "base_url", ""))
    if not url.startswith("http"):
        return "", ""                              # 认不出来的客户端就老实说不知道，不猜一个档位出来
    local = url.startswith(("http://127.0.0.1", "http://localhost"))
    return ("local" if local else "online"), "推断"


def transcript(messages: Sequence) -> Text:
    """把轨迹渲染成一坨只读文本，给「查看轨迹对话」那一屏用。

    和 `_step` 那套 widget 渲染是两条路，字段口径刻意一致：一步 = 一次 assistant 回复，审核结论从
    `ToolResult` 的审计 JSON 里还原（轨迹里存的就是它，判决对象本身不落盘）。
    """
    out, round_ = Text(), 0
    for message in messages:
        if isinstance(message, UserMessage):
            round_ += 1
            out.append(f"\n── 第 {round_} 轮 ──\n＃ {message.text[:200]}\n", style="bold")
        elif isinstance(message, AssistantMessage):
            if message.text.strip():
                out.append(message.text + "\n", style="bright_blue")
            for call in message.tool_calls:
                out.append(f"  → {call.describe()[:400]}\n", style="yellow")
        elif isinstance(message, ToolResult):
            audit = str(json_dict(message.content).get("review", ""))
            output = str(json_dict(message.content).get("output", ""))[:OUTPUT_CHARS]
            out.append(f"  审核 {audit}" + (f" · {message.review_ms:.0f}ms" if message.review_ms else "") + "\n",
                       style="green" if audit.startswith("allowed") else "red")
            if output.strip():
                out.append(f"  ↳ {output.replace(chr(10), ' ')}\n", style="dim")
    return out


class Step(Collapsible):
    """一次思考 = 一个折叠块：模型那一段话、它交出来的命令、审核结论、工具回声。

    下一次思考一开始，这一块就自己折起来，标题留成"思考 2 · shell · 放行 0.8s"这样一行账。
    这是用户点名的读法：一路看得见 agent 在想什么，但屏幕不会被全过程一直摊着。
    """

    def __init__(self, index: int) -> None:
        self.index, self.lines, self.body = index, Text(), Static()
        self.tool, self.verdict = "", ""
        super().__init__(self.body, title=Text(f"思考 {index} · 进行中"), collapsed=False)

    def on_mount(self) -> None:
        self.tooltip = "Enter 或点击这里展开/收起这一次思考"

    def add(self, line: Text) -> None:
        if self.lines.plain:
            self.lines.append("\n")
        self.lines.append(line)
        self.body.update(self.lines)

    def close(self) -> None:
        bits = [f"思考 {self.index}", self.tool, self.verdict]
        self.title = Text(" · ".join(bit for bit in bits if bit))
        self.collapsed = True


class Round(Collapsible):
    """一轮 = 一条 user 消息到它这一轮结束，里面是"问题 + 每次思考各一块 + 最终回答"。

    跑完只折过程（那些 `思考 k`），**最终回答留在屏幕上**；要等下一轮开始，这一整轮才收起来。
    先前是整轮一股脑折叠，把该给人看的最终输出也一起折进去了——这一条就是那件事的改正。
    """

    def __init__(self, index: int) -> None:
        self.index, self.steps, self.denials, self.t0, self.t1 = index, 0, 0, 0.0, 0.0
        self.segment, self.first, self.sealed = "", "", False
        self.blocks: list[Step] = []           # 每次思考一块，按发生顺序
        self.current: Step | None = None       # 正在往里加行的那一块
        self.question, self.final = Text(), Text()
        self.asked, self.answered = Static(), Static()
        self.contents: Collapsible.Contents | None = None   # 挂在 on_mount 里，见上
        super().__init__(title=Text(f"第 {index} 轮 · 进行中"), collapsed=False)

    async def on_mount(self) -> None:
        self.tooltip = "Enter 或点击这里展开/收起这一轮"
        # `Collapsible` 自己不是容器：内容住在它内部的 `Contents` 里，折叠规则是
        # `&.-collapsed > Contents {display:none}`。后挂的 widget 必须挂进 `Contents`，挂在
        # Collapsible 本体会永远露在外面——截图抓到过"标题写着折叠，里面的思考过程还在屏上"。
        self.contents = self.query_one(Collapsible.Contents)
        await self.contents.mount(self.asked)   # 问题在最上，最终回答在最下，思考块插在两者之间
        await self.contents.mount(self.answered)

    @property
    def lines(self) -> Text:
        """问题 + 各次思考 + 最终回答，拼成一坨纯文本：测试与"这轮到底说过什么"都读这一个口径。"""
        out = Text()
        for chunk in (self.question, *[block.lines for block in self.blocks], self.final):
            if chunk.plain:
                out.append(chunk)
                out.append("\n")
        return out

    def ask(self, text: Text) -> None:
        self.question, _ = text, self.asked.update(text)

    async def begin_step(self) -> Step:
        """新一次思考开始：先把上一块折起来，新一块插在最终回答之前。"""
        self.close_step()
        self.current = block = Step(len(self.blocks) + 1)
        self.blocks.append(block)
        await (self.contents or self).mount(block, before=self.answered)
        return block

    def close_step(self) -> None:
        if self.current is not None:
            self.current.close()
            self.current = None

    def answer(self, text: Text) -> None:
        """这一轮要说给人听的那句话：不进思考块，不跟着过程一起折。"""
        self.close_step()
        self.final, _ = text, self.answered.update(text)

    def seal(self, elapsed: float | None = None) -> None:
        """标题写"第 N 轮 · 3 步 · 4.2s · 拒 1"，过程全折、最终回答留着。

        回放没有整轮计时，秒数就退化成轨迹里的 ts 差；折叠这一整轮交给"下一轮开始"那一刻，
        不在这里做——在这里做就是把用户要看的东西折进去了。
        """
        self.close_step()
        seconds = max(0.0, self.t1 - self.t0) if elapsed is None else elapsed
        self.title = Text(f"第 {self.index} 轮 · {self.steps} 步"
                          + (f" · {seconds:.1f}s" if seconds else "") + f" · 拒 {self.denials}")
        self.sealed = True


class Log(VerticalScroll, can_focus=False):
    """对话区。`VerticalScroll` 默认**可聚焦**（`containers.py:32`），实测 Tab 环里多这一站：焦点落上去
    既没有样式也没有任何键能用，用户按 Tab 就"焦点神秘失踪"。摘掉它，环只剩真正有事可做的三站。"""


class Marquee(Static):
    """顶部身份行：平时一个字不动，悬停时循环滚动播全文（身份行不许压缩、不许折行，超宽只能裁）。

    只有内容真放不下时才启动计时器，否则白白重绘；`on_unmount` 一定停表——换工作区会重建界面，留着旧表
    就有一行已经下场的文字还在跳。属性**不能叫 `offset`**：`Widget.offset` 是真 property，`self.offset = 0`
    会炸成"compose() 返回了无效结果"，一眼看不出是名字撞了库（踩过）。
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.text_full, self.shift, self.hover_at = Text(), 0, 0.0
        self._timer = None

    def show(self, text: Text) -> None:
        if text.plain != self.text_full.plain:
            self.shift, self.hover_at = 0, 0.0                   # 内容变了就回到开头，别接着上次的位移跳
        self.text_full = text
        if self._timer is None:
            self.update(self._window())

    def on_mouse_move(self, event) -> None:
        self.hover_at = time.monotonic()
        if not self._overflowing():
            return                                     # 放得下就不滚：滚动是补救，不是装饰
        if self._timer is None:
            self._timer = self.set_interval(0.22, self._advance)

    def _overflowing(self) -> bool:
        return cell_len(self.text_full.plain) > max(1, self.size.width - 2)

    def _advance(self) -> None:
        if time.monotonic() - self.hover_at > 1.5:    # 手一离开就回到原位，不留半截
            self.stop_scroll()
            return
        self.shift += 1
        self.update(self._window())

    def _window(self) -> Text:
        """按字轮动。rich 的 `Text` 切片保留 span，所以滚动时那三段颜色不会并成一坨。

        （原来这里调 `get_style_at_offset(0, 0)`——那位第一参数是 `Console`，不是 offset，
        直接把 int 传进去炸的是 rich 内部，报错看着像样式系统坏了。）
        """
        total = len(self.text_full.plain)
        start = self.shift % total if total else 0
        if not start or not self._overflowing():
            return self.text_full
        return self.text_full[start:] + Text("    ") + self.text_full[:start]

    def stop_scroll(self) -> None:
        self.shift, self.hover_at = 0, 0.0
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self.update(self.text_full)

    def on_unmount(self) -> None:
        self.stop_scroll()


class StatusBar(Static, can_focus=True):
    """状态栏（在输入框下方）。键**只在它有焦点时**生效：Input 有焦点时 `+`/`h`/`[` 是打字，不该被界面偷走。

    焦点必须看得出，否则 Tab 过去了也不知道、键按下去"没反应"——那就是"步数上限根本调不了"的直接原因。
    可见性用背景 + 粗体下划线 + 行首 `▸`，不用 `outline`：实测 30 行高的窗口里它把第一行那行账挤出屏幕。
    """

    BINDINGS = [
        Binding("plus,right", "tweak('review',1)", "档位+"),
        Binding("minus,left", "tweak('review',-1)", "档位-"),
        Binding("right_square_bracket", "tweak('steps',1)", "步数+"),      # textual 的键名，不是 "[" "]"
        Binding("left_square_bracket", "tweak('steps',-1)", "步数-"),
        Binding("h", "toggle_guard", "闸门开关"),
    ]

    FOCUS_MARK = "▸ "             # 焦点标记。底色/下划线在无色终端与部分主题下几乎看不出，字形不会骗人

    marked = False                  # 焦点标记的状态：不读 has_focus，因为 on_focus 触发时它可能还没翻

    def on_focus(self, event) -> None:
        self.marked = True
        self.app._refresh()          # 标记由 `_refresh` 拼进正文：8.2.8 的 render() 返回 Content 而非 Text，
                                    # 在这儿改文案要打架，正文统一从一个地方出更好核对

    def on_blur(self, event) -> None:
        self.marked = False
        self.app._refresh()

    def action_tweak(self, what: str, delta: int) -> None:
        self.app.tweak(what, delta)

    def action_toggle_guard(self) -> None:
        self.app.toggle_guard()


class WorkspacePicker(ModalScreen[Path]):
    """选择屏：候选列表 + 一个手输路径的输入框。Esc 关掉，什么都不动。从 `Ctrl+P` 面板进。"""

    BINDINGS = [Binding("escape", "dismiss", "关闭", priority=True)]

    def __init__(self, workspaces: Sequence[Path] = (), current: Path | None = None) -> None:
        super().__init__()
        self.workspaces = tuple(Path(w) for w in workspaces)
        self.current = None if current is None else Path(current)

    def compose(self) -> ComposeResult:
        yield Static(Text("  换工作区 · ↑↓ 选 · Enter 确认 · Esc 取消", style="bold"))
        # "当前"标在真值那一条上：这一屏以前只列候选，切换后屏幕上那行工作区还是别的目录，
        # 谁都不知道现在到底站在哪儿（见 HarnessApp.service_workspace）。
        yield OptionList(*[Option(Text(str(w) + ("　◀ 当前" if w == self.current else "")), id=f"w{i}")
                           for i, w in enumerate(self.workspaces)], id="wsopts", markup=False)
        yield Input(placeholder="或者手输工作区绝对路径，回车", id="wsman")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if self.workspaces:
            self.dismiss(self.workspaces[min(event.option_index, len(self.workspaces) - 1)])

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.value.strip():
            self.dismiss(Path(event.value.strip()))


class SegmentPicker(ModalScreen[int]):
    """`跳转 · 历史会话`：只列**当前工作区这条轨迹**里的对话段，选中就滚到那一轮。

    不做跨工作区列表：换了目录就得重建 service（工作目录绑着闸门与工作区，文件名绑着会话 id），热切会骗人，
    所以跨工作区只有 `换工作区` 那一条路，界面上不藏第二条。
    """

    BINDINGS = [Binding("escape", "dismiss", "关闭", priority=True)]

    def __init__(self, rows: Sequence[tuple[int, str, str]]) -> None:
        super().__init__()
        self.rows = list(rows)

    def compose(self) -> ComposeResult:
        yield Static(Text("  本工作区的对话段 · ↑↓ 选 · Enter 定位 · Esc 取消", style="bold"))
        yield OptionList(*[Option(Text(f"第 {index} 轮 · {segment[:8] or UNBOUND} · {ask}"), id=f"s{i}")
                           for i, (index, segment, ask) in enumerate(self.rows)],
                         id="segs", markup=False)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_index < len(self.rows):
            self.dismiss(self.rows[event.option_index][0])


class ValuePrompt(ModalScreen[str | None]):
    """一次输入一个值（步数上限）。`Ctrl+P` 里逐格 ±1 太慢，这里一次到位。"""

    BINDINGS = [Binding("escape", "dismiss", "关闭", priority=True)]

    def __init__(self, title: str, initial: str, hint: str = "") -> None:
        super().__init__()
        self.title_text, self.initial, self.hint = title, initial, hint

    def compose(self) -> ComposeResult:
        yield Static(Text(f"  {self.title_text}" + (f" · {self.hint}" if self.hint else ""), style="bold"))
        yield Input(value=self.initial, placeholder="输入数字，回车确认 · Esc 取消", id="vman")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)


class TrajectoryScreen(ModalScreen[None]):
    """一屏只读文本，Esc 回对话，主界面的状态一个字不动。轨迹原文、轨迹对话、键位清单都走这一屏。

    这里的滚动容器用**可聚焦**的 `VerticalScroll`，不是主界面的 `Log`：只读屏里键盘翻页是主要动作，
    主界面摘掉 `#log` 焦点是因为它"焦点落上去没有键可用"，只读屏恰恰相反。
    """

    BINDINGS = [Binding("escape", "dismiss", "关闭", priority=True)]

    def __init__(self, heading: str, body: Text) -> None:
        super().__init__()
        self.heading, self.body = heading, body

    def compose(self) -> ComposeResult:
        yield Static(Text(f"  {self.heading} · Esc 返回对话", style="bold"))
        yield VerticalScroll(id="traj")

    def on_mount(self) -> None:
        self.query_one("#traj", VerticalScroll).mount(Static(self.body))
        self.query_one("#traj", VerticalScroll).focus()


class Palette(CommandPalette):
    """textual 命令面板本体，只把那句英文占位提示换掉——不改 site-packages，也不自己重写面板。

    残留一处英文：搜不到东西时 textual 自己插的 `No matches found` 那条灰字（`command.py:889`，
    在私有方法里现建的 Option，子类没地方接）。常态看不见它，乱打字才会冒出来。
    """

    def __init__(self, **kwargs: object) -> None:
        kwargs.setdefault("placeholder", "输入要执行的命令")
        super().__init__(**kwargs)


PROMPT_MAX_ROWS, PROMPT_SHOWN_LINES, PROMPT_CHROME = 5, 6, 2
# 输入框最多长到五行、用户那段话最多摊开六行；CHROME = 上下边框各占一行，算高度要加上


def shown_text(text: str) -> Text:
    """屏幕上怎么显示用户发出去的那段话：多行原样摊开，太长就留前几行并说清还剩几行。"""
    lines = text.splitlines() or [""]
    if len(lines) <= PROMPT_SHOWN_LINES:
        body = "\n".join(lines)[:1200]
    else:
        left = len(lines) - PROMPT_SHOWN_LINES
        body = "\n".join(lines[:PROMPT_SHOWN_LINES]) + f"\n……还有 {left} 行，全文在这条轨迹里"
    return Text(f"＃ {body}", style="bold")


class PromptArea(TextArea):
    """多行输入框。换掉单行 `Input` 是因为它粘贴时只留第一行——`_input.py:758` 那句
    `event.text.splitlines()[0]`，长 prompt 贴进去就静默丢了一截。

    键位：**Enter 发送**、**Shift+Enter 换行**；宿主分不出 Shift+Enter 时（传统 conhost 把修饰键与
    Enter 压成同一个字节）**Ctrl+J 照样换行**。粘贴多行在两种宿主里都完整保留，因为走的是括号粘贴。
    """

    class Submitted(Message):
        """`TextArea` 没有"提交"这一说：Enter 由我们解释，攒下的整段文本用这条消息交回界面。"""

        def __init__(self, box: PromptArea) -> None:
            super().__init__()
            self.box = box

    def __init__(self, **kwargs) -> None:
        self._rows = 1
        super().__init__(**kwargs)

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.post_message(PromptArea.Submitted(self))
        elif event.key in ("shift+enter", "ctrl+j"):
            event.stop()
            event.prevent_default()
            self.insert("\n")                      # 换行走文档 API，不自己拼字符串
        else:
            await super()._on_key(event)

    async def on_text_area_changed(self, event: TextArea.Changed) -> None:
        self._grow(min(PROMPT_MAX_ROWS, self.text.count("\n") + 1))

    def _grow(self, rows: int) -> None:
        """常态一行，贴了多行才长高；上限之外文档内部滚，不许把上面的对话挤没。"""
        if rows != self._rows:
            self._rows = rows
            self.styles.height = rows + PROMPT_CHROME

    def wipe(self) -> None:
        """发出去之后清空并缩回一行。`clear()` 不一定发 Changed，所以高度自己收。"""
        self.clear()
        self._grow(1)


class HarnessApp(App):
    """终端里的对话：顶部身份行、中间按轮折叠的轨迹、输入框、输入框下方两行状态。功能收进 `Ctrl+P`。"""

    CSS = """
    #title { height: 1; padding: 0 1; background: $panel; }
    #log { height: 1fr; }
    #status { height: auto; padding: 0 1; background: $boost; }
    #status:focus { background: $primary; text-style: bold underline; }
    #prompt { height: 3; max-height: 7; }
    """
    BINDINGS = [Binding("ctrl+q", "quit", "退出", priority=True),
                Binding("ctrl+b", "toggle_rounds", "折叠", priority=True)]
    # Ctrl+N 撤了：Windows Terminal 默认把 Ctrl+N 绑成"新建窗口"，留着它就是给你多开一扇窗而不是切工作区。
    # 工作区入口在 Ctrl+P 面板里（跳转 · 换工作区），散键不复活。

    class Note(Message):
        """后台线程搬到主线程的一条消息。`{"kind": "done"}` 是一轮结束，其余就是事件契约那四种。"""

        def __init__(self, event: dict) -> None:
            super().__init__()
            self.event = event

    def __init__(self, service, workspaces: Sequence[Path] = (), base: Path | None = None,
                 on_exit: Callable[[Path], None] | None = None, review_mode: str | None = None,
                 workspace: Path | None = None) -> None:
        super().__init__()
        self.service, self.workspaces = service, tuple(Path(w) for w in workspaces)
        self.base = None if base is None else Path(base)
        self.on_exit = on_exit
        # 屏幕上那行"工作区"只能来自 service 自己（工具真在哪个目录跑），不能取 `workspaces[0]`：
        # cli 传进来的是**排过序的全部候选目录**，于是标签永远显示字典序第一个（`01_env_report`），
        # 哪怕 service 早就绑在 `experiment_3` 上。用户照着这行字判断自己站在哪儿，就被骗去别的目录，
        # 结果是 7 次越界写入被硬规则拦掉、白烧 token。
        self.workspace = self.service_workspace() or (
            Path(workspace) if workspace is not None else
            (self.workspaces[0] if self.workspaces else Path.cwd()))
        # 档位的来路按诚实排序：调用方明说（或 .env 里设了）> 从注进来的审核客户端反推 > local。
        # 底栏那个数字必须和真实审核链一致：`main.py tui --review online` 造的 service 真挂着在线客户端，
        # 那时写 "档位 local" 就是骗人；反推出来了就标一句"推断"，让人知道这值不是启动参数给的。
        mode = review_mode or os.getenv("HARNESS_REVIEW", "")
        self.tier_source = ""
        if mode not in REVIEW_MODES:
            mode, self.tier_source = guess_mode(self.service)
        if mode not in REVIEW_MODES:
            mode = REVIEW_MODES[0]
        self.built_mode = mode                    # service 造出来时真正挂着的档位
        self.review_mode = mode
        # 闸门：暂存原物，不自己装配。关 = 换上 no_guard，开 = 把这个原物装回去。
        self.guard_default = getattr(service, "guard", None)
        self.armed_to_close = False
        self.restart_request = False              # 界面里换了档位，调用方据此重建 service
        self.reopen_panel = True                  # 改值类命令执行后重开面板，停在原位（用例可以关掉它）
        self.session_id, self.note, self.lock_error = "", "", ""
        self.segment: str | None = None           # 当前对话段：变了就画一条分隔
        self.round: Round | None = None
        self.rounds: list[Round] = []
        self.empty_hint: Static | None = None        # "这个工作区还没有对话"那一行，第一次发消息就撤掉
        self.turn, self.busy = 0, False
        self.steps = self.total_steps = 0         # 本轮 / 累计步数（一步 = 一次 assistant 回复）
        self.prompt = self.completion = self.cached = self.last_prompt = 0
        self.elapsed = 0.0                        # 整轮耗时（perf_counter），状态行那个秒数

    def compose(self) -> ComposeResult:
        yield Marquee(id="title")
        yield Log(id="log")
        yield PromptArea(id="prompt", placeholder="说点什么，Enter 发送 · Shift+Enter 换行",
                         soft_wrap=True, tab_behavior="focus")
        yield StatusBar(id="status")

    async def on_mount(self) -> None:
        try:
            self.session_id = self.service.new_session().id   # 一工作区一条轨迹：TUI 不发明 id
        except Exception:                                     # 假 service 只有 session_id 也得能用
            self.session_id = getattr(self.service, "session_id", "") or "?"
        await self._load_history()
        self._refresh()
        self.query_one("#prompt", PromptArea).focus()

    async def _load_history(self) -> None:
        """把这条轨迹已有的对话画出来——换了工作区就该看见那个工作区聊过什么。

        旧文件（uuid 命名、没有 segment）照样读，段号如实标"未绑定"。最近一次对话段摊开、更早的段收成
        标题一行：全展开会满屏旧内容，全折叠看着像"对话没了"，两种都不对。
        一条消息都没有就留一行说明——别让人对着一片空白猜是没读到还是这个目录真没聊过。
        """
        store = getattr(self.service, "session_store", None)
        try:
            messages = list(store.load(self.session_id).messages) if store and self.session_id else []
        except Exception:                                     # 读不出就算空轨迹：坏文件不许带走界面
            messages = []
        for message in messages:
            segment = getattr(message, "segment", self.segment or "")
            if isinstance(message, ToolResult):               # 轨迹里只存了审计文本，判决从里面还原
                audit = str(json_dict(message.content).get("review", ""))
                await self._step({"kind": "review", "segment": segment, "review_ms": message.review_ms,
                                  "decision": Decision(not audit.startswith("denied"), audit)})
                await self._step({"kind": "result", "result": message, "segment": segment})
            else:
                await self._step({"kind": "user" if isinstance(message, UserMessage) else "assistant",
                                  "message": message, "segment": segment})
        if self.round is not None:
            self.round.seal()
        if not self.rounds:
            self.empty_hint = Static(Text("这个工作区还没有对话 · 全部轨迹：python main.py sessions",
                                          style="dim"))
            await self._mount(self.empty_hint)
            return
        recent = self.segment or self.rounds[-1].segment
        for round_ in self.rounds:
            round_.collapsed = round_.segment != recent        # 只有最近这一段摊开给人看
        self.query_one("#log", Log).scroll_end(animate=False)

    async def on_harness_app_note(self, message: HarnessApp.Note) -> None:
        await self._step(message.event)

    async def _step(self, event: dict) -> None:
        """渲染一步。实时事件与历史回放共用这条路：界面上"看到的"和轨迹里"存下的"因此一致。"""
        kind, round_ = event.get("kind"), self.round
        if kind == "done":
            await self._end_turn(event)
            return
        if kind == "user":
            round_ = await self._begin_round(event.get("segment", ""))
            user = event["message"]
            round_.t0 = round_.t1 = user.ts
            round_.first = user.text[:60].replace("\n", " ")
            round_.ask(shown_text(user.text))
            self.query_one("#prompt", PromptArea).wipe()      # 发送成功才清空：抢锁失败时文本还得留着
            self._refresh()  # 顶栏的对话号要在这一帧就换，否则新对话开头几秒还挂着上一段的号
            return
        round_ = round_ or await self._begin_round(event.get("segment", ""))
        observed = event.get("message") or event.get("result")
        round_.t1 = max(round_.t1, getattr(observed, "ts", 0.0))
        if kind == "assistant":
            message = event["message"]
            self.prompt += message.prompt_tokens
            self.completion += message.completion_tokens
            self.cached += message.cached_tokens
            self.last_prompt = message.prompt_tokens or self.last_prompt
            self.steps, self.total_steps, round_.steps = self.steps + 1, self.total_steps + 1, round_.steps + 1
            if message.tool_calls:
                # 还要接着干活 = 一次思考：起一块，上一次的那一块在这一下折起来。
                block = await round_.begin_step()
                block.add(Text(message.text, style="bright_blue") if message.text.strip()
                          else Text(SILENT, style="dim"))
                for call in message.tool_calls:
                    block.tool = call.tool_name
                    block.add(Text(f"  → {call.describe()[:400]}", style="yellow"))
            else:
                # 没有工具调用 = 这一轮要说给人听的最终内容：单独一行，跟着过程一起折是不对的。
                round_.answer(Text(message.text or "这条回复没有文字", style="bold"))
        elif kind == "review":
            decision: Decision = event["decision"]
            ms = float(event.get("review_ms") or 0.0)
            block = round_.current or await round_.begin_step()
            if decision.should_allow:
                block.add(Text(f"  审核 放行 {ms / 1000:.1f}s", style="green"))
                block.verdict = f"放行 {ms / 1000:.1f}s"
            else:
                reason = decision.reason or "无理由"
                block.add(Text(f"  审核 拒绝 · {reason}", style="bold red"))
                block.verdict = "拒"          # 折起来的标题只写"拒"：理由整条在块里，别让标题变长句
                round_.denials += 1
        elif kind == "result":
            block = round_.current or await round_.begin_step()
            output = str(json_dict(event["result"].content).get("output", ""))[:OUTPUT_CHARS]
            if output.strip():
                block.add(Text(f"  ↳ {output.replace(chr(10), ' ')}", style="dim"))
        self._refresh()

    async def _begin_round(self, segment: str) -> Round:
        """一轮开始：先收尾上一轮，段号变了就在中间画一条分隔。"""
        if self.empty_hint is not None:
            await self.empty_hint.remove()                    # 真开始聊了，那句"还没有对话"就该消失
            self.empty_hint = None
        if self.round is not None:
            if not self.round.sealed:
                # 回放没有 `done` 事件：上一轮在被下一轮顶掉那一刻才结束，不然标题会永远停在"进行中"
                self.round.seal()
            self.round.collapsed = True
        if segment != self.segment and (self.turn or not segment):
            await self._mount(Static(Text("── 新对话 ──" if segment else f"── {UNBOUND} ──", style="dim")))
        self.segment, self.turn = segment, self.turn + 1
        self.round = round_ = Round(self.turn)
        round_.segment = segment
        self.rounds.append(round_)
        await self._mount(round_)
        return round_

    async def _end_turn(self, event: dict) -> None:
        self.busy, self.elapsed = False, float(event.get("elapsed", 0.0))
        error = str(event.get("error", ""))
        self.lock_error = error if "工作区已有进程在跑" in error else ""
        if error:
            await self._mount(Static(Text(error, style="bold red")))
            if self.round is not None and self.round.steps == 0:
                # 这一轮压根没跑起来：不留下"第 N 轮 · 进行中"的空壳，也不留下一个 0 步的折叠块
                self.rounds.remove(self.round)
                await self.round.remove()
                self.round = None
            elif self.round is not None:
                self.round.seal()                 # 中途炸了：已经发生的几步照样收进一轮，别丢
        elif self.round is not None:
            self.round.seal(self.elapsed)
        self._refresh()
        self.query_one("#prompt", PromptArea).focus()

    async def _mount(self, widget: Static | Round) -> None:
        log = self.query_one("#log", Log)
        await log.mount(widget)
        log.scroll_end(animate=False)

    def service_workspace(self) -> Path | None:
        """从 service 身上问出"程序到底在哪个目录跑"。工具的 cwd 是最硬的证据，其次才是闸门绑的根。

        界面不 import 组装根，所以这里只用鸭子类型读公开字段：`assistant.tools[*].workspace`
        （`adapters/tools/windows.py:93`，`subprocess` 的 `cwd` 就是它）与 `guard.keywords["workspace"]`
        （`main.build()` 用 `partial` 绑进来的那道路径监狱）。两个都问不到才退回界面自己记的值。
        """
        for tool in getattr(getattr(self.service, "assistant", None), "tools", None) or ():
            root = getattr(tool, "workspace", None)
            if root:
                return Path(root)
        root = getattr(getattr(self.service, "guard", None), "keywords", {}).get("workspace")
        return Path(root) if root else None

    def _outside_hint(self, text: str) -> str:
        """任务里写了工作区之外的绝对路径，发问之前就提醒一句。

        起因是实测：`sessions/experiment_3-4ded7f5f.jsonl` 35 行里 7 行是策略拒绝。用户问的是
        "往 C:\\Users\\…\\实验三 写"，工作区却在别处，模型每次都得撞一次硬规则才知道回不来，
        一次一次问模型就是白烧 token。这一行不等撞、先说清楚。
        """
        live = self.service_workspace()
        escaped = outside_write(text, str(live)) if live is not None and self.guard_state() != "off" else None
        if not escaped:
            return ""
        return (f"注意：任务里写了 {escaped}，工作区是 {live}，往那儿写的命令会被硬规则拦下。"
                "要用那个目录就「跳转 · 换工作区」，或退出后 main.py tui -w 指过去")

    def _identity(self) -> Text:
        """顶部身份行：一个字都不压缩，放不下就悬停滚动。工作区取 service 的真值，不取界面记的账。"""
        live = self.service_workspace() or self.workspace
        return Text.assemble(
            Text(" spark harness ", style="bold cyan"), Text(f"工作区 {live}", style="bold"),
            Text(f" · 会话 {self.session_id} · 对话 {(self.segment[:8] if self.segment else UNBOUND)}",
                 style="dim"))

    def _refresh(self) -> None:
        """身份行与状态栏都从这一处出：一条地方算数，人和测试看到的是同一个数。"""
        identity = self._identity()
        box = self.query_one("#title", Marquee)
        box.show(identity)
        box.tooltip = identity.plain                               # 悬停也有全文，双保险
        bar = [self._cost_line(), self._settings_line()]
        # 常驻就这两行（用户要求"外面只看到当前状态，精简"）。原先这里还挂两句提示，都挪走了：
        # "本地审核串行，其他窗口会排队"是档位的性质，写进面板 `设置 · 审核模式` 的 help；
        # "本轮进行中"是这一轮的属性，并进第一行末尾当一个字段。剩下的行只在该有事要说时出现。
        for text, style in ((self.lock_error, "bold red"), (self.note, "bold")):
            if text:
                bar.append(Text(text, style=style))
        status = self.query_one("#status", StatusBar)
        if status.marked:
            bar[0] = Text(StatusBar.FOCUS_MARK, style="bold") + bar[0]      # 行首那个 ▸
        status.update(Text("\n").join(bar))

    def _cost_line(self) -> Text:
        """状态第一行（账）：`本轮 x/上限 · 累计 n 步 · 本轮 s 秒 · tokens 总(输入+输出)`。

        原来这里挂着 `N tok/s`，它算的是 (输入+输出) ÷ 整轮墙钟，**不是解码速度**（实测真解码速度约 51–53
        tk/s），挂在屏幕上一直在误导人，所以换成整轮秒数。缓存命中率不常看，挪进 `doctor`/轨迹文件里按需查。
        """
        limit = getattr(self.service, "max_iterations", "?")
        line = Text.assemble(Text(f"本轮 {self.steps}/{limit}", style="bold"),
                             Text(f" · 累计 {self.total_steps} 步", style="bold"))
        if self.elapsed:
            line += Text(f" · 本轮 {self.elapsed:.1f} s")
        tokens = self.prompt + self.completion
        if not tokens and not self.cached:
            line += Text(" · 未启用计量", style="dim")
        else:
            line += Text(f" · tokens {tokens:,} ({self.prompt:,}+{self.completion:,})")
        if self.busy:
            line += Text(" · 进行中", style="cyan")     # 模型卡住时屏幕不动，这一格就是"还在跑"的证据
        return line

    def _settings_line(self) -> Text:
        """状态第二行（态）：`档位 · 闸门 · 上下文% · Ctrl+P 命令与设置 · Ctrl+Q 退出`。

        宿主与鼠标可用性**不占这一行**了：那是 `doctor` 第十项该说的话，挂在常驻状态上只会挤掉真状态。
        待生效与武装确认只在有值时点亮，不占常态行。
        """
        tier = f"档位 {self.review_mode}" + (f" · {self.tier_source}" if self.tier_source else "")
        guard = Text(self.guard_label(), style="bold" if self.guard_state() == "on" else "bold red")
        used = f"{100.0 * self.last_prompt / context_budget():.1f}%" if self.last_prompt else "—"
        row = [Text(tier, style="bold"), guard, Text(f"上下文 {used}"), Text(HINT, style="dim")]
        pending = self.pending_note()
        if pending:
            row.append(Text(pending, style="yellow"))
        return Text(" · ").join(r for r in row if r.plain)

    def pending_note(self) -> str:
        """档位记的新值和 service 真挂的不一致，就必须挂在屏幕上，不许静默半生效。"""
        if self.review_mode == self.built_mode:
            return ""
        return f"待生效：档位 {self.review_mode}，点跳转 · 重启界面落地"

    def guard_state(self) -> str | None:
        """'on' / 'off' / None。判据是对象本身：`partial(hard_deny, …)` 的 `.func` 不是 `no_guard` 就算开。"""
        guard = getattr(self.service, "guard", None)
        if guard is None:
            return None
        return "off" if getattr(guard, "func", guard) is no_guard else "on"

    def guard_label(self) -> str:
        """闸门开关的文案。关着的时候不许看起来像正常状态。"""
        state = self.guard_state()
        if state is None:
            return "闸门 未知，service 没挂 guard"
        if state == "off":
            return f"闸门 关 · {OFFGUARD}"
        return "闸门 开" + (f" · {ARMIN}" if self.armed_to_close else "")

    def status_text(self) -> str:
        """状态栏的纯文本：断言它比去猜某个 widget 的样式便宜。"""
        return str(getattr(self.query_one("#status", StatusBar).content, "plain", ""))

    # === 三样旋钮 ===========================================================

    def tweak(self, what: str, delta: int) -> None:
        """步数与档位。闸门走 `toggle_guard`（它要二次确认，不是"加减一格"那种动作）。"""
        if what == "steps":
            steps = int(getattr(self.service, "max_iterations", STEP_MIN)) + delta
            self.service.max_iterations = min(STEP_MAX, max(STEP_MIN, steps))
            self.note = f"步数上限 {self.service.max_iterations}，下一个请求生效"
        elif what == "guard":                          # 老按键路径：h 走这里，和面板条目同一个实现
            self.toggle_guard()
            return
        else:
            index = REVIEW_MODES.index(self.review_mode) + delta
            self.review_mode = REVIEW_MODES[index % len(REVIEW_MODES)]
            self.tier_source = ""               # 现在是界面里人肉记的档位，不是反推来的
            self.note = f"档位记为 {self.review_mode}：审核客户端在 service 构造时定死，重启对话才生效"
        self._refresh()

    def set_steps(self, raw: str) -> None:
        try:
            value = int(str(raw).strip())
        except ValueError:
            self.note = f"步数上限没改：{raw!r} 不是整数"
            self._refresh()
            return
        self.service.max_iterations = min(STEP_MAX, max(STEP_MIN, value))
        self.note = f"步数上限 {self.service.max_iterations}，下一个请求生效"
        self._refresh()

    def toggle_guard(self) -> None:
        """闸门开↔关，真写 `service.guard`：`app/service.py:119` 每次工具调用现读它，所以当场生效。

        关要走二次确认（手滑关掉最后一道保险，代价是一次覆盖轨迹或一次真删文件）；开不需要，往安全方向
        挪不该有摩擦。启动时就没装闸门（`--no-guard`）的话界面不自己造一个——装配权在组装根。
        """
        state = self.guard_state()
        if state is None or not hasattr(self.service, "guard"):
            self.note = "闸门：这个 service 没挂 guard，界面不动它"
        elif state == "on":
            if self.armed_to_close:
                self.service.guard, self.armed_to_close = no_guard, False
                self.note = (f"闸门已关：硬规则与路径监狱都不拦了 · {OFFGUARD}。"
                             "本段已判过的命令沿用旧判决，新对话段起全部重判")
            else:
                self.armed_to_close = True
                self.note = f"闸门：{ARMIN}——关 = 硬规则与路径监狱全部摘掉，再执行一次「设置 · 闸门」才真关"
        elif self.guard_default is None or getattr(self.guard_default, "func", self.guard_default) is no_guard:
            self.note = "闸门：启动时就没开 --no-guard，界面不合成新闸门，要开得重启带上 --guard"
        else:
            self.service.guard, self.armed_to_close = self.guard_default, False
            self.note = "闸门已开：装回启动时装配好的那道，硬规则与路径监狱都在"
        self._refresh()

    # === Ctrl+P 面板：功能都收在这里，标题里直接写当前值 =====================

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        """面板条目每次搜索都重新求值，所以标题里那个值就是此刻的真值（不是"上次我改成了…"）。

        前缀 `设置 ·/视图 ·/跳转 ·/文件 ·/帮助 ·` 是分类的唯一手段：8.2.8 的 `SystemCommand`
        只有 (title, help, callback, discover)，没有 category/priority，排序纯按模糊匹配分数。

        **这里不调 `super()`**：父类会塞进 `Theme` / `Quit` / `Keys` / `Maximize` / `Screenshot` 五条
        英文条目，五件都没要求过，退出走自己的 `Ctrl+Q`、面板靠 `Ctrl+P`，用不着它们在列表顶上挡路。
        """
        yield SystemCommand("设置 · " + self.guard_label(),
                            "硬规则 + 路径监狱，当场生效。关要按两次，本段已判过的沿用旧判决",
                            lambda: self._panel(self.toggle_guard))
        yield SystemCommand(f"设置 · 审核模式 {self.review_mode}"
                            + ("" if self.review_mode == self.built_mode else f" · 待生效，现挂 {self.built_mode}"),
                            "local 本地模型审，串行、免费但别的窗口会排队 · online 在线模型审，花 API 额度 "
                            "· off 不审，只留闸门。换档要点跳转 · 重启界面才落地",
                            lambda: self._panel(lambda: self.tweak("review", 1)))
        yield SystemCommand(f"设置 · 步数上限 {getattr(self.service, 'max_iterations', '?')}",
                            f"一次输入到位，范围 {STEP_MIN} 到 {STEP_MAX}，下一个请求生效",
                            lambda: self.push_screen(ValuePrompt(f"步数上限 {STEP_MIN} 到 {STEP_MAX}",
                                                                 str(getattr(self.service, "max_iterations", 12)),
                                                                 "回车确认 · Esc 取消"), self._steps_prompted))
        yield SystemCommand("视图 · 折叠全部", "把所有轮次收起来，只看标题那行账",
                            lambda: self._panel(lambda: self._set_all_rounds(True)))
        yield SystemCommand("视图 · 展开全部", "把所有轮次摊开",
                            lambda: self._panel(lambda: self._set_all_rounds(False)))
        yield SystemCommand("跳转 · 换工作区", "选定后退出界面、用新目录重建 service：工作目录绑着闸门与轨迹文件名",
                            self.action_pick_workspace)
        yield SystemCommand(f"跳转 · 历史会话 {len(self.segments())} 段",
                            "只列当前工作区这条轨迹的对话段，选中就滚到那一轮", self.action_pick_segment)
        if self.pending_note():
            yield SystemCommand(f"跳转 · 重启界面 · 应用档位 {self.review_mode}",
                                "界面里换的档位要落地就走这里：正常退出、由组装根用新档位重建 service",
                                self.action_restart_ui)
        yield SystemCommand("文件 · 查看轨迹对话", "只读回放，Esc 回到对话，屏幕状态不动",
                            lambda: self._view_trajectory(False))
        yield SystemCommand("文件 · 查看轨迹原文", "jsonl 一行一条消息：交报告时数条数、看审计字段就在这儿",
                            lambda: self._view_trajectory(True))
        yield SystemCommand("文件 · 打开 sessions 目录", "轨迹文件都在 base/sessions 下，文件名就是工作区",
                            self.action_open_sessions)
        yield SystemCommand("帮助 · 键位清单", "开一屏中文键位清单，Esc 返回对话；再执行一次这条就把那屏关掉",
                            self.action_show_keys)      # 不走 _panel：重开面板会把那一屏盖住

    def _panel(self, action: Callable[[], None]) -> None:
        """改值类命令：执行完重开面板、停在同一个位置，手感接近"就地编辑"。

        重开必须排队到当前消息处理完（面板此刻正在 dismiss，同一个循环里再 push 会撞上）。别用
        `set_timer(0.0, …)`：8.2.8 的 `Timer._run` 会 `int(…/0.0)` 直接 ZeroDivisionError 并把定时器吞掉，
        表现就是"面板没重开"；`call_later` 才是这个语义的正当通道（`message_pump.py:490`）。
        """
        action()
        if self.reopen_panel:
            self.call_later(self.action_command_palette)

    def _steps_prompted(self, raw: str | None) -> None:
        if raw is not None:
            self.set_steps(raw)

    def segments(self) -> list[tuple[int, str, str]]:
        """(第几轮, 段号, 首条提问)。屏幕不清空，所以历史段本来就在屏上，这里只是给个把手。"""
        return [(round_.index, round_.segment, round_.first or "没有提问文本") for round_ in self.rounds]

    # === 动作 ==============================================================

    def _set_all_rounds(self, collapse: bool) -> None:
        if not self.rounds:
            self.note = "还没有轮次"
        else:
            for round_ in self.rounds:
                round_.collapsed = collapse
            self.note = "已全部折叠" if collapse else "已全部展开"
        self._refresh()

    def action_show_keys(self) -> None:
        """「帮助」开单独一屏，Esc 回对话；再执行一次这条就是把那屏关掉。

        走 `TrajectoryScreen`（轨迹只读屏同一个类）而不是往状态行写一行：状态行是账和态，
        借它显示键位就等于把临时说明常驻在常驻区里，还得再想一个把它消掉的法子。
        """
        top = self.screen
        if isinstance(top, TrajectoryScreen) and top.heading == "键位":
            top.dismiss(None)
        else:
            self.push_screen(TrajectoryScreen("键位", keys_screen()))

    def action_toggle_rounds(self) -> None:
        """`Ctrl+B`：全都收着就展开，有展开的就全收。方向别写反——反了就是"按了没反应"。"""
        if not self.rounds:
            return
        self._set_all_rounds(not all(round_.collapsed for round_ in self.rounds))

    def action_command_palette(self) -> None:
        """`Ctrl+P` 用自己的 `Palette` 子类（占位提示是中文）。`id="--command-palette"` 是 textual
        判"面板已开"的凭据，换掉它就会重复 push 一层。"""
        if not Palette.is_open(self):
            self.push_screen(Palette(id="--command-palette"))

    def action_pick_workspace(self) -> None:
        self.push_screen(WorkspacePicker(self.workspaces, self.service_workspace() or self.workspace),
                         self._workspace_picked)

    def action_pick_segment(self) -> None:
        self.push_screen(SegmentPicker(self.segments()), self._jump_to_round)

    def _jump_to_round(self, index: int | None) -> None:
        if index is None:
            return
        for round_ in self.rounds:
            if round_.index == index:
                round_.collapsed = False
                round_.scroll_visible(animate=False)
                self.note = f"已定位到第 {index} 轮 · {round_.segment[:8] or UNBOUND}"
                break
        self._refresh()

    def _view_trajectory(self, raw: bool) -> None:
        """只读轨迹屏。读不到就老实说读不到，不许弹一个空屏让人以为轨迹丢了。"""
        path = None if self.base is None else Path(self.base) / "sessions" / f"{self.session_id}.jsonl"
        if raw:
            try:
                body = Text(path.read_text(encoding="utf-8") if path else "没有 sessions 目录", style="dim")
                heading = f"轨迹原文 · {path}"
            except OSError as exc:
                body, heading = Text(f"读不出来：{type(exc).__name__}: {exc}", style="red"), "轨迹原文"
        else:
            store = getattr(self.service, "session_store", None)
            try:
                messages = list(store.load(self.session_id).messages) if store else []
                body = transcript(messages)
                if not body.plain:
                    body = Text("这条轨迹还没有消息", style="dim")
                heading = f"轨迹对话 · {len(messages)} 条消息 · {self.session_id}"
            except Exception as exc:                      # 坏文件不许带走界面
                body, heading = Text(f"读不出来：{type(exc).__name__}: {exc}", style="red"), "轨迹对话"
        self.push_screen(TrajectoryScreen(heading, body))

    def action_open_sessions(self) -> None:
        """交给资源管理器。没有 `os.startfile`（非 Windows）或目录不存在就退回一句说明，不静默失败。"""
        folder = None if self.base is None else Path(self.base) / "sessions"
        if folder is None or not folder.is_dir():
            self.note = f"sessions 目录还不存在：{folder}"
            self._refresh()
            return
        starter = getattr(os, "startfile", None)
        if starter is None:
            self.note = f"这个系统没有 os.startfile，手动打开：{folder}"
            self._refresh()
            return
        try:
            starter(str(folder))
            self.note = f"已在资源管理器里打开 {folder}"
        except OSError as exc:
            self.note = f"打不开 {folder}：{type(exc).__name__}: {exc}"
        self._refresh()

    def action_restart_ui(self) -> None:
        """应用界面里换过的档位：置标志后正常退出，由 `cli.tui` 用新档位重建 service 再开一次。"""
        self.restart_request = True
        self.exit()

    def _workspace_picked(self, path: Path | None) -> None:
        if path is not None:                     # Esc 回来的是 None：什么都不动
            self.workspace = Path(path)
            self._refresh()
            if self.on_exit is not None:         # 告知调用方按这个目录重建 service，然后让路
                self.on_exit(self.workspace)
                self.exit()
            else:                                # 没给回调就只记账，service 仍指着旧目录——界面不骗人
                live = self.service_workspace()
                self.note = (f"换工作区没生效：service 仍在 {live}" if live else
                             f"换工作区没生效：{path} 只是记下了，没有 on_exit 回调，service 没动")
                self._refresh()

    def on_prompt_area_submitted(self, event: PromptArea.Submitted) -> None:
        """Enter 落地。弹层里那些单行 `Input`（选工作区、填步数）有自己的处理器，不经过这里。"""
        text = event.box.text.strip()
        if not text:
            return
        if self.busy:                            # 轨迹是一条文件，同一时刻只该有一个写入者
            self.note = "这一轮还在跑，等它收尾再发"
            self._refresh()
            return
        self.busy, self.note, self.steps = True, "", 0
        self.note = self._outside_hint(text)      # 越界写这种事先说，别等硬规则拦第七次
        self._refresh()
        self.run_turn(text)

    @work(thread=True, group="turn")
    def run_turn(self, text: str) -> None:
        """后台线程只做一件事：把 `service.chat` 的事件搬到主线程。这里绝不碰 widget。"""
        started, error = time.perf_counter(), ""
        try:
            if self.base is None:
                self.service.chat(self.session_id, text, on_event=self._emit)
            else:                                # 一工作区一条轨迹：并发窗口会整体重写同一个文件
                with WorkspaceLock(self.base / "runs" / f"{self.session_id}.lock",
                                   self.service_workspace() or self.workspace, "harness tui"):
                    self.service.chat(self.session_id, text, on_event=self._emit)
        except WorkspaceLocked as busy:
            error = f"这个工作区已有进程在跑：{busy}"
        except Exception as exc:                 # 异常不许变成崩溃，也不许变成静默
            error = f"{type(exc).__name__}: {str(exc)[:200]}"
        self.post_message(HarnessApp.Note({"kind": "done", "elapsed": time.perf_counter() - started,
                                           "error": error}))

    def _emit(self, event: dict) -> None:
        """`post_message` 从别的线程调用会走 `call_soon_threadsafe`，渲染因此都落在主线程。"""
        self.post_message(HarnessApp.Note(event))
