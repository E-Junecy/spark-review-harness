"""适配层·空审核器：`--review off` 时顶替 reviewer 客户端。

它**不发任何请求**，永远返回 `Decision(True, REASON)`。为什么要有这个类、而不是在编排里
"跳过审核"：`guard → reviewer → verdicts` 三段保持不动，于是每条 `ToolResult` 里仍然带着
`"review": "allowed by reviewer: 审核已关闭（--review off）…"` —— 事后能从轨迹里一眼区分
"没人审、直接放行"和"审过了、没问题"，这对实验报告的对照数据是必要的。

它实现 `ModelClient` 只为"同一个位置能插进去"，只有 `output_type=Decision` 的上下文才有意义。
"""

from __future__ import annotations

from app.ports import ModelClient, ModelSettings
from domain.context import Context
from domain.roles import Decision

REASON = "审核已关闭（--review off），本条只经过硬规则"


class NoReviewClient(ModelClient):
    def __init__(self, reason: str = REASON) -> None:
        self.reason = reason

    def generate(self, context: Context, model_settings: ModelSettings) -> Decision:
        return Decision(True, self.reason)
