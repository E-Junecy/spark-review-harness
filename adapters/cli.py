"""适配层·命令行前端：`run` / `sessions` / `show` / `serve` / `doctor`。

本模块不认识具体模型客户端、不认识 jsonl，也**不 import `main`**：service、store、体检零件都由
组装根通过 `Ports` 注进来（依赖方向 `main → cli`，否则 `main → cli → doctor → main` 就成环）。
它只负责把人话变成一次调用、把结果打印出来。换前端（换网页、换 GUI）只换这个文件。

Windows 控制台默认 GBK：模型爱写 ✅，任务已经跑完却在打印那一步抛 `UnicodeEncodeError`。
所以起手把 stdout/stderr 设成 `errors="replace"`——只降级编不出的字符，中文照旧。

`run` / `serve` 带两个审核开关（本模块只做转述与校验，怎么造客户端由组装根决定）：
`--review local|online|off` 选谁审核，`--no-guard` 把代码闸门（硬规则 + 路径监狱）也摘掉。

`run` 是实时的：`service.chat(..., on_event=live)` 每步回调一次，所以审核结论一落地就打印，
不必等整轮跑完。另有一层 `runs/<slug>.lock`：一工作区一条轨迹，两个窗口同时写会互相覆盖。
"""

from __future__ import annotations

import contextlib
import json
import sys
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import typer
from rich.console import Console
from rich.text import Text

from adapters.api.web import WebApi
from adapters.doctor import Rig, run_checks
from adapters.locks import WorkspaceLock, WorkspaceLocked
from app.policy import REVIEW_MODES
from app.ports import SessionStore
from app.service import AgentService
from domain.session import AssistantMessage, ToolResult, UserMessage

for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        _stream.reconfigure(errors="replace")


@dataclass
class Ports:
    """组装根注入的零件：怎么造 service、怎么造 store、怎么造体检零件、仓库根在哪。"""

    make_service: Callable[..., AgentService]
    make_store: Callable[[], SessionStore]
    make_rig: Callable[[Path], Rig]
    base: Path


def render(console: Console, message) -> None:
    """动态内容一律走 Text：rich 会把模型输出里的 `[xxx]` 当样式标签吞掉。"""
    match message:
        case AssistantMessage(text=text, tool_calls=calls):
            if text:
                console.print(Text(text, style="bright_blue"))
            for call in calls:
                console.print(Text(f"→ {call.describe()[:400]}", style="yellow"))
        case ToolResult(content=content, review_ms=review_ms):
            note = "" if review_ms is None else f"  [{review_ms:.0f} ms 审核]"
            console.print(Text(f"← {content[:600]}{note}", style="dim"))
        case UserMessage(text=text):
            console.print(Text(f"＃ {text[:200]}", style="bold"))


def build_app(ports: Ports) -> typer.Typer:
    """造出 typer app。所有具体知识都从 `ports` 进来，本函数不 import 组装根。"""
    app = typer.Typer(add_completion=False, help="在线 assistant + 本地 spark reviewer 的最小 harness")
    console = Console(highlight=False)

    @app.command()
    def run(
        task: str = typer.Argument(..., show_default=False, help="要交给 assistant 的任务"),
        workspace: Path = typer.Option(ports.base / "quests" / "default", "-w", help="工具的工作目录"),
        steps: int = typer.Option(12, "-s", help="单轮最多让模型走几步"),
        resume: str = typer.Option(None, "-r", "--resume", help="继续某个 session id"),
        assistant_model: str = typer.Option(None, "-m", "--assistant-model"),
        reviewer_model: str = typer.Option(None, "--reviewer-model"),
        review: str = typer.Option(REVIEW_MODES[0], "--review",
                                   help="审核档位：local 本地模型 / online 在线模型 / off 不审（只留硬规则）"),
        guard: bool = typer.Option(True, "--guard/--no-guard",
                                   help="--no-guard 把代码闸门（硬规则 + 路径监狱）也关掉，只用于对照实验"),
    ) -> None:
        """发一条任务：工具调用先过审核，轨迹写进 sessions/<工作区>.jsonl，过程实时打印。"""
        if review not in REVIEW_MODES:
            raise typer.BadParameter(f"只能是 {' / '.join(REVIEW_MODES)}", param_hint="--review")
        workspace.mkdir(parents=True, exist_ok=True)
        service = ports.make_service(workspace, steps, assistant_model, reviewer_model, review, guard)
        store = service.session_store
        session = store.load(resume) if resume else service.new_session()
        started = time.time()

        def live(event: dict) -> None:
            match event:
                case {"kind": "review", "decision": verdict, "review_ms": ms}:
                    console.print(Text(f"  审核 {'放行' if verdict.should_allow else '拒绝'} · {ms:.0f} ms"
                                       + (f" · {verdict.reason}" if verdict.reason else ""),
                                       style="green" if verdict.should_allow else "red"))
                case {"kind": "result", "result": result}:
                    render(console, result)
                case _:
                    render(console, event["message"])

        try:
            with WorkspaceLock(ports.base / "runs" / f"{session.id}.lock", workspace, "harness run"):
                produced = service.chat(session.id, task, on_event=live)
        except WorkspaceLocked as busy:
            console.print(Text(f"这个工作区已经有进程在跑，换窗口或先等它结束：{busy}", style="red"))
            raise typer.Exit(2) from None
        tail = store.load(session.id)
        tokens = sum(m.prompt_tokens + m.completion_tokens for m in tail.messages
                     if isinstance(m, AssistantMessage))
        total = len(tail.messages)  # chat 在存储侧重建会话，条数得重读
        console.print(f"\n[bold]{session.id}[/] · 本轮 {len(produced)} 条新消息 · "
                      f"{time.time() - started:.1f}s · 累计 {total} 条 · 用量 {tokens:,} tok")
        console.print(f"Trajectory: [green]{ports.base / 'sessions' / (session.id + '.jsonl')}[/]")

    @app.command()
    def sessions() -> None:
        """列出所有对话。只读本地文件，所以不需要任何模型可用。"""
        rows = ports.make_store().list()
        if not rows:
            # 一条都没有也必须出声：TUI 的空屏就写着「全部轨迹：python main.py sessions」，
            # 而新克隆的仓库里 sessions/ 是空的——一声不响看着就像命令坏了。
            console.print(f"[yellow]还没有轨迹[/]：{ports.base / 'sessions'} 是空的。"
                          "跑一次 `python main.py run \"...\" -w <工作目录>` 或 `python main.py tui` "
                          "聊一句，轨迹就落在那儿（文件名按工作区算，一工作区一条）。")
            return
        for session in rows:
            console.print(Text(f"  {session.id}  {len(session.messages):>3} msgs  {session.goal[:70]}"))

    @app.command()
    def show(session_id: str) -> None:
        """回放一条轨迹：谁请求了什么工具、谁批准的、结果如何、审核花了多久。"""
        for message in ports.make_store().load(session_id).messages:
            render(console, message)

    @app.command()
    def serve(
        host: str = typer.Option("127.0.0.1", "--host"),
        port: int = typer.Option(8000, "--port"),
        workspace: Path = typer.Option(ports.base / "quests" / "web", "-w"),
        steps: int = typer.Option(12, "-s"),
        review: str = typer.Option(REVIEW_MODES[0], "--review", help="同 run：local / online / off"),
    ) -> None:
        """WebApi 前端：GET /（最简 webui）、GET /sessions、POST /chat {session_id?, message}。"""
        if review not in REVIEW_MODES:
            raise typer.BadParameter(f"只能是 {' / '.join(REVIEW_MODES)}", param_hint="--review")
        workspace.mkdir(parents=True, exist_ok=True)
        WebApi(ports.make_service(workspace, steps, None, None, review, True), host, port).run()

    @app.command()
    def doctor() -> None:
        """体检。每项彼此独立：没有在线 key 时，本地 reviewer 与审核链照样验得完。"""
        report = run_checks(ports.make_rig(ports.base / "quests" / "doctor"))
        for row in report:
            console.print(Text(f"  {'OK ' if row['ok'] else 'FAIL'} {row['name']:<18} {row['detail'][:200]}",
                               style="green" if row["ok"] else "red"))
        (ports.base / "runs").mkdir(exist_ok=True)
        path = ports.base / "runs" / "doctor.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        console.print(f"\n{'all passed' if all(row['ok'] for row in report) else 'some failed'} -> "
                      f"[green]{path}[/]")

    @app.command()
    def tui(
        workspace: Path | None = typer.Option(None, "-w", help="进去就锁定这个工作区；不给就在界面里选"),
        steps: int = typer.Option(12, "-s", help="每次回答的循环上限（界面里还能调）"),
        review: str = typer.Option(REVIEW_MODES[0], "--review", help="同 run：local / online / off"),
        guard: bool = typer.Option(True, "--guard/--no-guard"),
    ) -> None:
        """终端界面（textual）：选工作区 → 连续对话 → 实时审核与按轮折叠。一键入口是 `harness.bat`。

        界面自己只认识 `AgentService` 那几个方法（构造时注进来），所以这块不 import 组装根；
        换工作区是"退出当前界面 + 用新目录重建 service"，因为工作目录绑在闸门与轨迹名上，热切会骗人。
        """
        if review not in REVIEW_MODES:
            raise typer.BadParameter(f"只能是 {' / '.join(REVIEW_MODES)}", param_hint="--review")
        from adapters.tui import HarnessApp  # 惰性导入：没装 textual 时其余命令照样能用

        current = workspace or ports.base / "quests" / "default"

        def last_words(harness: object, note: str) -> None:
            """无论怎么退，都在 runs/tui-last.log 留一行摘要。

            窗口"一闪就没"这种事，没有现场就只能猜：是 Ctrl+Q 了、换工作区了、还是抛异常前就退了。
            harness.bat 异常分支会把这个文件一起打出来。

            **读的是 service / harness 的当前值，不是 typer 的启动原值**：界面里把上限调到 13、把闸门关了，
            摘要还写"上限 12 · 闸门关（启动参数给的）"就是撒谎——而这行日志正是"到底生效了没"的唯一现场。
            """
            service = getattr(harness, "service", None)
            limit = getattr(service, "max_iterations", steps) if service is not None else steps
            gate = getattr(harness, "guard_label", lambda: f"闸门 {'开' if guard else '关'}")()
            lines = [f"{time.strftime('%Y-%m-%d %H:%M:%S')} 退出 · {note}",
                     f"工作区 {current} · 会话 {getattr(harness, 'session_id', '?')}",
                     f"对话段 {getattr(harness, 'segment', '') or '（没发过问）'} · "
                     f"本轮 {getattr(harness, 'steps', 0)} 步 · 累计 {getattr(harness, 'total_steps', 0)} 步",
                     f"档位 {getattr(harness, 'review_mode', review)} · {gate} · 步数上限 {limit}"]
            logs = ports.base / "runs"
            logs.mkdir(exist_ok=True)
            (logs / "tui-last.log").write_text("\n".join(lines) + "\n", encoding="utf-8")

        while True:
            current.mkdir(parents=True, exist_ok=True)
            # 候选目录每轮重算：手输的工作区多半不在 `quests/` 里，不换新一轮它就不在列表中。
            candidates = sorted({current, *(p for p in (ports.base / "quests").glob("*") if p.is_dir())})
            service = ports.make_service(current, steps, None, None, review, guard)
            chosen: list[Path] = []
            harness = HarnessApp(service, workspaces=candidates, base=ports.base,
                                 review_mode=review, on_exit=chosen.append, workspace=current)
            try:
                harness.run()
            except Exception as exc:  # noqa: BLE001  界面挂了要留下现场给 harness.bat，而不是闪退
                logs = ports.base / "runs"
                logs.mkdir(exist_ok=True)
                (logs / "tui-error.log").write_text(traceback.format_exc(), encoding="utf-8")
                last_words(harness, f"崩了 {type(exc).__name__}: {exc}")
                console.print(Text(f"界面挂了：{type(exc).__name__}: {exc}\n现场在 {logs / 'tui-error.log'}",
                                   style="red"))
                raise typer.Exit(1) from None
            restart = bool(getattr(harness, "restart_request", False))
            last_words(harness, "换了工作区" if chosen else
                       ("重启界面应用新档位" if restart else "正常退出（没发过问就是没发过问）"))
            if not chosen and not restart:
                break
            steps = service.max_iterations        # 界面里调过的上限带走，不让启动参数把它盖回去
            if chosen:
                current = chosen[-1]
            review = getattr(harness, "review_mode", review)

    return app
