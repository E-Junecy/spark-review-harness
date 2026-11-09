"""适配层·shell 工具：模型在 Windows 上敲命令的那只手。"""

from __future__ import annotations

import base64
import shutil

from adapters.tools.windows import PS_PREFIX, SubprocessTool
from domain.tool import Tool


class ShellTool(SubprocessTool, Tool):
    name = "shell"
    description = "Run a Windows PowerShell command in the workspace; returns output and exit code."
    parameters = {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}

    def execute(self, arguments: dict) -> str:
        command = str(arguments.get("command", "")).strip()
        if not command:
            return self.observe(-1, "missing 'command'")
        # -EncodedCommand + UTF-16LE：绕开引号嵌套与 GBK 地狱
        encoded = base64.b64encode((PS_PREFIX + command).encode("utf-16-le")).decode("ascii")
        return self.observe(*self.run([shutil.which("pwsh") or "powershell.exe", "-NoProfile",
                                       "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                       "-EncodedCommand", encoded]))
