"""适配层·体检：逐项确认这台机器能不能把整条链跑起来。

十项彼此独立：没有在线 key 时，本地 reviewer 与审核链照样验得完——体检的目的就是分清哪一环断了。

这里要碰具体类（`OpenAIClient`、`ShellTool`、`conda_python`）和端点表。端点表属于组装根，
所以由 `main.py` 把零件装进 `Rig` 交进来，本模块绝不 import `main`：
否则 `main → cli → doctor → main` 就成环了（这正是当初把 CLI 搬进适配层要解决的问题）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from adapters.models.openai_client import OpenAIClient
from adapters.tools.shell import ShellTool
from adapters.tools.windows import conda_env, conda_python
from app.policy import hard_deny
from app.ports import Effort, ModelSettings
from domain.context import Context
from domain.roles import Decision, ToolCallReviewer
from domain.session import AssistantMessage, Session, ToolCall, UserMessage

MOUSE_WHY = ("想让鼠标生效：本仓库的 harness.bat 已用 wt -w new 起新窗（实测 wt 1.24 拒收 -N / -n / --new-window），"
             "或在系统设置里把默认终端设为 Windows Terminal。conhost 这条路不是没接好——"
             "它根本不往输入流写鼠标序列，而 textual 8.2.8 的 win32 驱动也没置 ENABLE_MOUSE_INPUT、"
             "ReadConsoleInput 循环只认 KEY_EVENT 与 RESIZE")


@dataclass
class Rig:
    """体检需要的零件，全部由组装根注入。"""

    base: Path                        # 仓库根：runs/ 写在这里
    workspace: Path                   # 试执行用的工作目录
    endpoint: Callable[[str], dict]   # 角色 -> {url, model_id, key_env, effort}
    key_for: Callable[[dict], str]    # 端点 -> api key（允许为空：本地端点不鉴权）
    reviewer_instruction: str         # 审核指令（组装根渲染好再交进来）


def _require(condition: bool, detail: str) -> str:
    """不成立就拿 detail 说清差在哪：只有 ok/no 的体检报告没有信息量。"""
    if not condition:
        raise RuntimeError(detail)
    return detail


def run_checks(rig: Rig) -> list[dict]:
    """跑完十项，返回机器可读的报告（`cli` 负责打印与落盘）。"""
    rig.workspace.mkdir(parents=True, exist_ok=True)
    a, r = rig.endpoint("assistant"), rig.endpoint("reviewer")
    key = rig.key_for(a)
    reviewer = OpenAIClient(r["url"], "", timeout=180.0)
    shell = ShellTool(rig.workspace)

    def powershell() -> str:
        ps = shutil.which("pwsh") or shutil.which("powershell.exe")
        if not ps:
            raise RuntimeError("powershell not found")
        return ps

    def git_version() -> str:
        git = shutil.which("git")
        if not git:
            raise RuntimeError("git not found")
        return subprocess.run([git, "--version"], capture_output=True, text=True).stdout.strip()

    def hard_rules() -> str:
        blocked = hard_deny(ToolCall("i", "shell", {"command": r"rm -rf C:\\"}))
        passed = hard_deny(ToolCall("i", "shell", {"command": "Get-ChildItem"}))
        return _require(blocked is not None and not blocked.should_allow and passed is None,
                        f"rm -rf -> {blocked} · Get-ChildItem -> {passed}")

    def console_host() -> str:
        """宿主与鼠标可用性。conhost 里鼠标是"拿不到"，不是"没接好"，所以值得写进体检而不是靠人现场猜。

        三层判据，从最可靠到最兜底：WT 注入的 `WT_SESSION` → `HKCU\\Console\\%%Startup` 里那两个委派值
        → `GetConsoleMode` 能不能打开标准输入句柄（管道/无 TTY 时它直接失败，此时"宿主"这个问题没有答案）。
        永远 ok：这不是故障，是能力边界，红叉会让人去修一个不需要修的东西。
        """
        import winreg  # Windows-only；这台机器的体检本来就是 Windows 视角，跨平台时这一项自己会 FAIL

        delegated = ""
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Console\%%Startup") as key:
                delegated = str(winreg.QueryValueEx(key, "DelegationTerminal")[0])
        except OSError:
            delegated = ""
        if os.environ.get("WT_SESSION"):
            return f"宿主 Windows Terminal（WT_SESSION 已设）· 鼠标与悬停可用 · {MOUSE_WHY}"
        if delegated:
            return f"委派终端 {delegated} · 鼠标可用性以实际窗口为准 · {MOUSE_WHY}"
        return f"宿主 conhost（无 WT_SESSION、HKCU\\Console\\%%Startup 未设委派值）· 鼠标不可用 · {MOUSE_WHY}"

    def reviewer_verdict() -> str:
        probe = Session(id="doctor", messages=[UserMessage("在工作目录写一个文件")])
        context = ToolCallReviewer(rig.reviewer_instruction).context(
            probe, ToolCall("i", "shell", {"command": "Get-Date"}))
        decision = reviewer.generate(context, ModelSettings(r["model_id"], r["effort"]))
        return _require(isinstance(decision, Decision), f"spark -> {decision}")

    def assistant_online() -> str:
        if not key:
            raise RuntimeError(f"{a['key_env']} 未设置（写进 .env）；没 key 也能本地跑："
                               "把 HARNESS_ASSISTANT_URL 指到 Ollama")
        # 只 list models 不算数：DashScope 的模型列表不鉴权也返回。必须真发一次极小补全。
        ping = OpenAIClient(a["url"], key, timeout=60.0).generate(
            Context("Reply with exactly one word.", (), [UserMessage("ping")], AssistantMessage),
            ModelSettings(a["model_id"], Effort.LOW))
        return _require(isinstance(ping, AssistantMessage),
                        f"{a['url']} model_id={a['model_id']} key=****{key[-4:]} reply={ping.text[:40]!r}")

    checks: dict[str, Callable[[], str]] = {
        "python": lambda: _require(sys.version_info >= (3, 10),
                                   f"{sys.version.split()[0]} at {sys.executable} "
                                   f"(conda env: {conda_env()})"),
        "powershell": powershell,
        "git": git_version,
        "python_pinned": lambda: _require(Path(conda_python()).is_file(),
                                          f"conda env {conda_env()} -> {conda_python()}"),
        "tool_execution": lambda: _require("ok" in json.loads(shell.execute({"command": "echo ok"}))["output"],
                                           "shell echo ok"),
        "hard_deny": hard_rules,
        "console_host": console_host,
        "reviewer_ollama": lambda: _require(r["model_id"] in [m.id for m in reviewer.client.models.list().data],
                                            f"{r['url']} model_id={r['model_id']}"),
        "reviewer_verdict": reviewer_verdict,
        "assistant_online": assistant_online,
    }
    report = []
    for name, check in checks.items():
        try:
            report.append({"name": name, "ok": True, "detail": check()})
        except Exception as exc:  # 一项坏了不影响其余
            report.append({"name": name, "ok": False, "detail": f"{type(exc).__name__}: {exc}"})
    return report
