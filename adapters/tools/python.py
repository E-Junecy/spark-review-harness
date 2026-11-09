"""适配层·python 工具：模型写一段代码，用钉死的解释器跑。

脚本落在 `<workspace>/.harness_tmp/run_N.py` 而不是 `-c`：多行代码走命令行参数会被
PowerShell 再解析一遍，落文件才只受 Python 自己的语法约束。
"""

from __future__ import annotations

from pathlib import Path

from adapters.tools.windows import SubprocessTool, conda_python
from domain.tool import Tool


class PythonTool(SubprocessTool, Tool):
    name = "python"
    parameters = {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}

    def __init__(self, workspace: Path):
        super().__init__(workspace)
        # 解释器在构造时才解析：import 本模块不该要求 conda 环境存在
        self.description = f"Execute a Python snippet with the pinned interpreter {conda_python()}."
        self._seq = 0

    def execute(self, arguments: dict) -> str:
        code = str(arguments.get("code", "")).strip()
        if not code:
            return self.observe(-1, "missing 'code'")
        temp = self.workspace / ".harness_tmp"
        temp.mkdir(parents=True, exist_ok=True)
        self._seq += 1
        script = temp / f"run_{self._seq}.py"
        script.write_text(code, encoding="utf-8")
        return self.observe(*self.run([conda_python(), "-X", "utf8", str(script)]))
