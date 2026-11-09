"""适配层·OpenAI 方言：Context <-> chat/completions。

DashScope（在线 assistant）和 Ollama（本地 reviewer）都说这一种方言，所以一个类两个实例。
`Effort` 在这里翻译成温度档位；`thinking=False` 翻译成 `reasoning_effort: "none"`——这两个
端点实测都认这个字段（非法值回 400，并列出 `none/minimal/low/medium/high/xhigh/max`），所以
只发 `"none"` 这一个值。以前注释里写的"都不吃 `reasoning_effort`"是错的，实验 03 推翻了它。
接 Anthropic（`output_config.effort`）/ Gemini（`thinking_level`）时改的还是这一处——翻译只发生在适配器里。

输出形状由 `context.output_type` 决定：要 `Decision` 就约定一行 JSON，要 `AssistantMessage`
就带 `tools` + `tool_choice="auto"`。请求体里的消息一律走 `wire.request_message()`，
存储私有键不许漏进去。
"""

from __future__ import annotations

import re

from openai import OpenAI

from adapters.wire import request_message, to_wire
from app.ports import Effort, ModelClient, ModelSettings
from domain.context import Context
from domain.roles import Decision
from domain.session import AssistantMessage, ToolCall, new_id

LOCAL_KEY = "sk-local"  # 本地端点不鉴权，但客户端要求非空

EFFORT_TEMPERATURE = {Effort.DEFAULT: 0.2, Effort.LOW: 0.0, Effort.MEDIUM: 0.2,
                      Effort.HIGH: 0.4, Effort.MAX: 0.6}


class OpenAIClient(ModelClient):
    def __init__(self, base_url: str, api_key: str, timeout: float = 120.0):
        self.base_url = base_url
        self.client = OpenAI(base_url=base_url, api_key=api_key or LOCAL_KEY, timeout=timeout)

    def generate(self, context: Context, model_settings: ModelSettings) -> object:
        completion = self.client.chat.completions.create(**self.payload(context, model_settings))
        return self.parse(completion.choices[0].message, context.output_type, completion.usage)

    def payload(self, context: Context, settings: ModelSettings) -> dict:
        body = {"model": settings.model_id,
                "messages": [{"role": "system", "content": context.instruction},
                             *(request_message(to_wire(m)) for m in context.messages)],
                "temperature": EFFORT_TEMPERATURE[settings.effort]}
        if settings.thinking is False:
            body["reasoning_effort"] = "none"
        if context.output_type is Decision:
            # 审核只要一行 JSON。不指望各家 structured output 字段名一致，直接约定文本 JSON 协议。
            body["response_format"] = {"type": "json_object"}
        else:
            body["tools"] = [{"type": "function", "function":
                              {"name": t.name, "description": t.description, "parameters": t.parameters}}
                             for t in context.tools]
            body["tool_choice"] = "auto"
        return body

    @staticmethod
    def parse(reply, output_type: type, usage=None) -> object:
        if output_type is Decision:
            return parse_decision(reply.content or "")
        prompt_tk, completion_tk, cached_tk = token_counts(usage)
        return AssistantMessage(text=reply.content or "",
                                tool_calls=tuple(ToolCall(call.id or new_id(), call.function.name,
                                                           ToolCall.arguments_from(call.function.arguments))
                                                  for call in (reply.tool_calls or ())),
                                prompt_tokens=prompt_tk, completion_tokens=completion_tk,
                                cached_tokens=cached_tk)


def token_counts(usage) -> tuple[int, int, int]:
    """从 usage 里取三个数。dict 还是对象都认：Ollama 给 dict，DashScope 给对象，都不该炸。"""
    if usage is None:
        return 0, 0, 0

    def pick(container: object, name: str) -> int:
        value = container.get(name) if isinstance(container, dict) else getattr(container, name, None)
        return int(value or 0)

    details = getattr(usage, "prompt_tokens_details", None)
    return (pick(usage, "prompt_tokens"), pick(usage, "completion_tokens"),
            pick(details, "cached_tokens") if details else 0)


def parse_decision(text: str) -> Decision:
    """模型可能不守 JSON 约定；解析不出来就按拒绝处理（宁可保守）。"""
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return Decision(False, f"审核输出无法解析为 JSON: {text[:80]!r}")
    data = ToolCall.arguments_from(match.group(0))
    if "allow" not in data:
        return Decision(False, f"审核输出缺少 allow 字段: {text[:80]!r}")
    return Decision(bool(data["allow"]), str(data.get("reason") or "") or None)
