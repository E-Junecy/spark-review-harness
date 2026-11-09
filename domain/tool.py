"""领域层·工具：模型能伸手碰到的外部动作，长什么样。

模型只知道三件事：`name`（用来指名）、`description`（用来理解用途）、`parameters`（用来构造入参）。
工具存在的意义是被执行：`execute` 收 `ToolCall` 的 arguments，回 `ToolResult` 的 content（一段文本）。

怎么真的执行属于适配层：Windows 上是 PowerShell 与钉死的解释器。这里只规定形状。
"""

from __future__ import annotations

from typing import Any, Protocol


class Tool(Protocol):
    """工具的形状。`ShellTool` / `PythonTool` 显式继承它，写漏方法会被测试抓住。"""

    name: str
    description: str
    parameters: dict[str, Any]

    def execute(self, arguments: dict[str, Any]) -> str: ...
