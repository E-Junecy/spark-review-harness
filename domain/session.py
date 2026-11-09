"""领域层·对话：整个系统的表现形式。

`Session` 是一条对话，`Message` 是它身上的一来一回，`ToolCall` 是模型想动用工具的那句请求。
这一层不认识 jsonl，也不认识 chat/completions——两边的翻译在适配层。

`ts` 与 `review_ms` 是观测字段：由应用层打点、由存储适配器写进轨迹，发给模型前一律剥掉。
它们存在只为让人在 `show` 里看出"哪一步烧了 11 秒"，不该进上下文（一个字之差就会打废
本地模型的前缀缓存）。设计期那份字段清单因此多出两项，README 落点表已标注。

后来又加了三项同类的观测字段，理由一样：`UserMessage.segment`（哪一次对话）、
`AssistantMessage.prompt_tokens / completion_tokens / cached_tokens`（这次请求花了多少 token，
实测在线端点每次审核偷偷生成 94 个思维链 token，不记下来就永远看不见钱去哪了）。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def json_dict(text: Any) -> dict[str, Any]:
    """模型写的 JSON 只能降级、不能炸循环：坏文本给空 dict，非对象的 JSON 包一层留着。"""
    if isinstance(text, dict):
        return text
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {"value": data}


@dataclass
class ToolCall:
    id: str
    tool_name: str
    arguments: dict[str, Any]

    def describe(self) -> str:
        """给人和审核模型看的一行文本。审核键、硬规则扫描、CLI 显示都用它，所以只有一处定义。"""
        return f"{self.tool_name} {json.dumps(self.arguments, ensure_ascii=False)}"

    @classmethod
    def arguments_from(cls, text: Any) -> dict[str, Any]:
        """从模型给的原始参数（通常是 JSON 字符串）构造 arguments。"""
        return json_dict(text)


@dataclass
class UserMessage:
    text: str
    ts: float = 0.0
    # 一次 chat 就是一次对话（段）。判决缓存与重复计数都按段隔离：spark 判的是"这条命令与
    # 当前任务相不相关"，目标换了还沿用上一段的批条就是错的，而这不是性能问题，是正确性问题。
    segment: str = field(default_factory=new_id)


@dataclass
class AssistantMessage:
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    ts: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0


@dataclass
class ToolResult:
    tool_call_id: str  # 指向某次 ToolCall 的 id
    content: str
    ts: float = 0.0
    review_ms: float | None = None  # 本次判决耗时；硬规则命中时接近 0


Message = UserMessage | AssistantMessage | ToolResult


@dataclass
class Session:
    id: str = field(default_factory=new_id)
    messages: list[Message] = field(default_factory=list)

    def append(self, message: Message) -> None:
        self.messages.append(message)

    @property
    def goal(self) -> str:
        """最近一轮任务：最后一条用户消息。审核器靠它判断"是否跑题"。"""
        return next((m.text for m in reversed(self.messages) if isinstance(m, UserMessage)), "")

    @property
    def current_segment(self) -> str:
        """当前对话段的标识；空会话返回空串。缓存键与界面折叠都靠它。"""
        return next((m.segment for m in reversed(self.messages) if isinstance(m, UserMessage)), "")
