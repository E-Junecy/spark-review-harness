"""适配层·线格式：领域消息 <-> 可序列化 dict，全系统只有一处定义。

形状 = OpenAI chat/completions 的消息形状（`role` / `content` / `tool_calls` / `tool_call_id`），
再加几个只属于存储的私有键：`kind`、`type`（轨迹行是哪种东西）、`ts`、`review_ms`（观测字段）。
`stores/jsonl.py` 写全形状，`models/openai_client.py` 发请求前用 `request_message()` 剥掉私有键。

为什么必须剥：多一个键就多改一次发给模型的字节，本地 spark 的前缀缓存实测一个字之差
从 0.08s 变 6.3s（docs/spark-harness-design.md）。这条有专门测试钉住。

以后接 Anthropic 怎么办：它的消息形状不同（`system` 是独立参数、工具块叫 `tool_use`），
到时候在 `anthropic_client.py` 里自己翻译，轨迹形状不受牵连。
"""

from __future__ import annotations

import json

from domain.session import AssistantMessage, Message, Session, ToolCall, ToolResult, UserMessage

STORE_ONLY_KEYS = ("kind", "type", "ts", "review_ms", "segment",
                   "prompt_tokens", "completion_tokens", "cached_tokens")


def to_wire(message: Message) -> dict:
    if isinstance(message, AssistantMessage):
        body: dict = {"role": "assistant", "content": message.text or "", "ts": message.ts,
                      "prompt_tokens": message.prompt_tokens,
                      "completion_tokens": message.completion_tokens,
                      "cached_tokens": message.cached_tokens}
        if message.tool_calls:
            body["tool_calls"] = [{"id": call.id, "type": "function",
                                   "function": {"name": call.tool_name,
                                                "arguments": json.dumps(call.arguments, ensure_ascii=False)}}
                                  for call in message.tool_calls]
        return body
    if isinstance(message, ToolResult):
        return {"role": "tool", "tool_call_id": message.tool_call_id, "content": message.content,
                "ts": message.ts, "review_ms": message.review_ms}
    return {"role": "user", "content": message.text, "ts": message.ts, "segment": message.segment}


def from_wire(row: dict) -> Message:
    """私有键缺失就当没填：旧轨迹（没有 ts / review_ms）照样能读、能回放。"""
    ts = float(row.get("ts", 0.0))
    if row["role"] == "assistant":
        return AssistantMessage(row.get("content") or "",
                                tuple(ToolCall(call["id"], call["function"]["name"],
                                               ToolCall.arguments_from(call["function"]["arguments"]))
                                      for call in row.get("tool_calls") or ()), ts,
                                int(row.get("prompt_tokens", 0)), int(row.get("completion_tokens", 0)),
                                int(row.get("cached_tokens", 0)))
    if row["role"] == "tool":
        return ToolResult(row["tool_call_id"], row["content"], ts, row.get("review_ms"))
    return UserMessage(row["content"], ts, row.get("segment") or "")


def request_message(row: dict) -> dict:
    """存储形状 -> 请求形状：私有键一律不许进请求体。"""
    return {k: v for k, v in row.items() if k not in STORE_ONLY_KEYS}


def session_row(session: Session) -> dict:
    return {"kind": "session", "id": session.id}


def message_row(message: Message) -> dict:
    return {"kind": "message", "type": type(message).__name__, **to_wire(message)}


__all__ = ["STORE_ONLY_KEYS", "from_wire", "message_row", "request_message",
           "session_row", "to_wire"]
