"""领域层·上下文：一次要发给模型的全部东西。

`instruction` + `tools` + `messages` + `output_type` 就是设计期定下的四个字段。
泛型 `output_type` 是这个设计里最值钱的一格：同一个 Context 结构，回复用户时声明要
`AssistantMessage`，审核工具调用时声明要 `Decision`——**要什么输出写在上下文里**，
调用方因此不需要为不同任务写两套流程。

这里没有 model_id、没有温度、没有各家字段名：那些是"怎么调"，属于应用层的端口
（`app/ports.py`）与适配层的翻译。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from domain.session import Message
from domain.tool import Tool

T = TypeVar("T")


@dataclass
class Context(Generic[T]):
    instruction: str
    tools: tuple[Tool, ...]
    messages: list[Message]
    output_type: type[T]
