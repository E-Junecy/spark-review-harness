"""适配层·Windows 执行环境：PowerShell 5.1 与钉死的解释器。

这一堆细节都是实测踩出来的，不是防御性冗余：

- `pwsh` 本机没装，落到 `WindowsPowerShell\\v1.0\\powershell.exe`（5.1）。
- 命令用 `-EncodedCommand` + UTF-16LE 传，绕开引号嵌套与 GBK 地狱。
- 5.1 把重定向的 stderr 序列化成 CLIXML，`_strip_clixml` 还原成文本行**且保留退出码**
  （曾用 `2>&1 | Out-String` 包一层，退出码就没了，已回滚）。
- 解释器由代码钉死（`conda_python()`），模型不能选；路径在**调用时**解析，
  所以 `.env` 晚于 import 加载也生效，查找结果按环境名缓存。
- `windows_tools()` 不在这里：`shell.py` / `python.py` 要从本模块取 `SubprocessTool`，
  反向 import 会成环。挑哪几个工具是组装根的事，写在 `main.py`。
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

PS_PREFIX = ("$ProgressPreference='SilentlyContinue';$WarningPreference='SilentlyContinue';"
             "[Console]::OutputEncoding=[Text.UTF8Encoding]::new();$OutputEncoding=[Text.UTF8Encoding]::new();")


def conda_env() -> str:
    return os.getenv("HARNESS_CONDA_ENV", "llm_learn")


def _interpreter(root: str) -> Path:
    """conda env 在 Windows 上是 <env>/python.exe，venv 才是 <env>/Scripts/python.exe：两种都认。"""
    env_root = Path(root.strip())
    for candidate in (env_root / "python.exe", env_root / "Scripts" / "python.exe", env_root / "bin" / "python"):
        if candidate.is_file():
            return candidate
    return env_root / "python.exe"


@lru_cache(maxsize=8)
def _locate(env: str) -> str:
    """顺序：当前进程 -> conda 环境登记簿 -> conda CLI。"""
    current = Path(sys.executable)
    if env in (current.parent.name, current.parent.parent.name):
        return str(current)
    registered = Path.home() / ".conda" / "environments.txt"
    if registered.is_file():
        for line in registered.read_text(encoding="utf-8", errors="replace").splitlines():
            if Path(line.strip()).name == env and _interpreter(line).is_file():
                return str(_interpreter(line))
    if conda := os.environ.get("CONDA_EXE") or shutil.which("conda"):
        try:
            listing = subprocess.run([conda, "env", "list"], capture_output=True, text=True).stdout
        except OSError:
            listing = ""
        for line in listing.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == env and _interpreter(parts[-1]).is_file():
                return str(_interpreter(parts[-1]))
    raise RuntimeError(f"找不到 conda 环境 {env} 的 python（设 HARNESS_PYTHON 可覆盖）")


def conda_python() -> str:
    """钉死解释器：模型不能选解释器，路径由代码决定，写进提示词只作告知。"""
    if override := os.getenv("HARNESS_PYTHON", "").strip():
        return override
    return _locate(conda_env())


def _strip_clixml(text: str) -> str:
    """把 PowerShell 5.1 的 CLIXML 错误流还原成文本行：模型读得懂错误，才不会乱重试。"""
    head, separator, blob = text.partition("#< CLIXML")
    if separator:
        records = re.findall(r'<S S="(?:Error|Warning|Information)"[^>]*>(.*?)</S>', blob, re.S)
        lines = [html.unescape(re.sub(r"<[^>]+>", "", r)).strip() for r in records]
        head += "\n".join(line for line in lines if line) + ("\n" if any(lines) else "")
    return re.sub(r"_x([0-9A-Fa-f]{4})_", lambda m: chr(int(m.group(1), 16)), head).replace("\r\n", "\n")


class SubprocessTool:
    """两个具体工具共享的执行细节：cwd 钉在 workspace，超时能杀进程树，
    失败变成文本观察而不是把异常抛进循环，超长输出截断（spark 的 32K 是硬墙）。"""

    timeout = 120.0
    max_output_chars = 8000
    kill_grace = 5.0  # 杀完树再给管道 5s 收尸；不能无限等，否则"超时守卫"自己会被拖住

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace)

    def run(self, argv: list[str]) -> tuple[int, str]:
        # 把钉死的环境目录顶在子进程 PATH 最前：模型在 shell 里敲 `python` 也只能拿到同一个解释器
        env = {**os.environ, "PYTHONUTF8": "1",
               "PATH": str(Path(conda_python()).parent) + os.pathsep + os.environ.get("PATH", "")}
        proc = subprocess.Popen(argv, cwd=str(self.workspace), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                env=env, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        try:
            out, _ = proc.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            # 第一步先杀整棵树；第二步给收尸设上限——实测 taskkill 偶尔杀不掉（沙箱/句柄未释放），
            # 这时若不设上限，`communicate()` 会一直等到子进程自然结束（30s 的 sleep 就等 30s），
            # 于是"超时守卫生效了"这件事变成假的：它只是陪跑到了对面自己退出。
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
            try:
                out, _ = proc.communicate(timeout=self.kill_grace)
                note = ""
            except subprocess.TimeoutExpired:
                out, note = "", "（进程树未在收尸期内退出，已放弃等待）"
            return -1, out + f"\n[timeout after {self.timeout:g}s]{note}"
        return proc.returncode or 0, out

    def observe(self, returncode: int, output: str) -> str:
        output = _strip_clixml(output)
        if len(output) > self.max_output_chars:
            head = self.max_output_chars // 2
            tail = self.max_output_chars - head
            output = f"{output[:head]}\n... [{len(output) - self.max_output_chars} chars omitted] ...\n{output[-tail:]}"
        return json.dumps({"returncode": returncode, "output": output}, ensure_ascii=False)
