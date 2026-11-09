"""适配层·Anthropic 方言的空位。

骨架的适配器清单里列了这个，本机目前**没有任何可验证的通路**（实测：`anthropic` SDK 未装、
`ANTHROPIC_API_KEY` 不存在、Ollama 0.40.13 不提供兼容端点——`POST /api/anthropic/v1/messages` 返回 404）。
所以它是一个会老实报错的空位，而不是凭文档写的死代码。

要补全时照 `openai_client.py` 的两半写，注意 Anthropic 与 OpenAI 方言的三处不同：
`system` 是请求体的独立参数而不是 messages 里的一条；工具调用块叫 `tool_use`、结果叫 `tool_result`；
`max_tokens` 必填。`Effort` 在这一家翻成 `output_config.effort`。轨迹形状（`adapters/wire.py`）不受牵连。
"""

from __future__ import annotations

from app.ports import ModelClient, ModelSettings
from domain.context import Context

MISSING = ("本机没有 Anthropic 通路：装 SDK（pip install anthropic）并给出 ANTHROPIC_API_KEY，"
           "再照 adapters/models/openai_client.py 补 payload / parse 两半")


class AnthropicClient(ModelClient):
    def __init__(self, base_url: str = "https://api.anthropic.com", api_key: str = "", **_kwargs: object):
        self.base_url, self.api_key = base_url, api_key

    def generate(self, context: Context, model_settings: ModelSettings) -> object:
        raise NotImplementedError(MISSING)
