"""适配层·Gemini 方言的空位。

骨架的适配器清单里列了这个，本机同样没有通路（实测：`google-genai` SDK 未装、
`GOOGLE_API_KEY` / `GEMINI_API_KEY` 都不存在），所以它是一个会老实报错的空位。

要补全时照 `openai_client.py` 的两半写：这一家的函数调用是 `functionCall` / `functionResponse`，
系统提示走 `systemInstruction`（不是 messages 里的一条），`Effort` 翻成 `thinking_level`。
轨迹形状（`adapters/wire.py`）不受牵连。
"""

from __future__ import annotations

from app.ports import ModelClient, ModelSettings
from domain.context import Context

MISSING = ("本机没有 Gemini 通路：装 SDK（pip install google-genai）并给出 GOOGLE_API_KEY，"
           "再照 adapters/models/openai_client.py 补 payload / parse 两半")


class GoogleClient(ModelClient):
    def __init__(self, api_key: str = "", model: str = "gemini-2.0-flash", **_kwargs: object):
        self.api_key, self.model = api_key, model

    def generate(self, context: Context, model_settings: ModelSettings) -> object:
        raise NotImplementedError(MISSING)
