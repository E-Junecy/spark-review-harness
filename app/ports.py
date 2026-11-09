"""应用层·端口：应用需要外部提供什么。

四个名字都是设计期定下的：`SessionStore`（怎么存对话）、`ModelClient`（怎么调模型）、
`ModelSettings`（用哪个模型）、`Effort`（想多深）。

写成 `typing.Protocol`：Python 认形状不认血统，适配器不继承也算实现。本项目仍把继承写出来
（`class OpenAIClient(ModelClient)`）以便一眼看出谁实现谁——代价是 Protocol 的方法体是 `...`，
漏写一个方法会静默返回 `None` 而不报错，所以 `tests/test_adapters.py` 里逐个端口点名测了实现。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from domain.context import Context
from domain.session import Session


class Effort(str, Enum):
    """思考深度的统一档位。各家字段名不同（OpenAI 的 `reasoning_effort`、Anthropic 的
    `output_config.effort`、Gemini 的 `thinking_level`），翻译写在各自适配器里。"""

    DEFAULT = "default"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    MAX = "max"


@dataclass
class ModelSettings:
    """用哪个模型、想多深、要不要让它把话想完再说：三个都成对出现，所以包在一起。

    `thinking` 单独立一个字段，因为它和 `effort` 是两个轴：`effort` 是我们的温度档位，
    `thinking` 是端点自己的思维链开关。实测审核请求走 `chat/completions` 会顺手生成思维链
    （在线一次 94 个 reasoning token，本地一次 342–797 字），而 `parse_decision` 只读
    `content`——花钱买从不读的推理。实验 03 量到关掉它判决耗时中位 2.69s → 0.82s，
    二十条命令里 16 条判决不变（见 `quests/03_review_latency/`）。
    """

    model_id: str
    effort: Effort = Effort.DEFAULT
    thinking: bool = True


class ModelClient(Protocol):
    def generate(self, context: Context, model_settings: ModelSettings) -> object:
        """把 Context 发出去，把回复翻译成 context.output_type 指定的领域对象。"""
        ...


class SessionStore(Protocol):
    def list(self) -> list[Session]: ...

    def save(self, session: Session) -> None: ...

    def load(self, session_id: str) -> Session: ...
