#!/usr/bin/env python3
"""组装根：全系统唯一知道所有具体类的地方。

    conda run --no-capture-output -n llm_learn python main.py doctor
    conda run --no-capture-output -n llm_learn python main.py run "创建 notes.txt" -w E:\\harness\\quests\\t01

分工：在线模型当 assistant 干活，本地 Ollama 的 spark 当 reviewer 把关，代码硬规则排在模型前面。
审核档位由 `--review` 选（`local` / `online` / `off`），`--no-guard` 再把硬规则也摘掉；
四种组合只是为了做对照实验，默认仍是"本地审核 + 硬规则"。

端点只出现在下面 `ENDPOINTS` 这张表里；命令怎么执行、对话怎么存、体检查什么，都由这里把具体类
交出去（`Ports` / `Rig`），所以 `domain/` `app/` `adapters/` 里没有一条 import 指向这里。
"""

from __future__ import annotations

import os
import sys
from functools import partial
from pathlib import Path

from dotenv import load_dotenv

import prompts
from adapters.cli import Ports, build_app
from adapters.doctor import Rig
from adapters.models.no_review import NoReviewClient
from adapters.models.openai_client import OpenAIClient
from adapters.stores.jsonl import JsonlSessionStore, session_slug
from adapters.tools.python import PythonTool
from adapters.tools.shell import ShellTool
from adapters.tools.windows import conda_python
from app.policy import REVIEW_MODES, hard_deny, no_guard
from app.ports import Effort, ModelClient, ModelSettings
from app.service import AgentService
from domain.roles import Assistant, ToolCallReviewer

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")  # 配置边界在这里；之后各处读环境变量都在调用时读，覆盖值照样生效

# 在线的当 assistant（要 key），本地的当 reviewer（免费，且符合"审核用更便宜更快的模型"）。
ENDPOINTS = {
    "assistant": {"url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                  "model_id": "qwen3.8-flash", "key_env": "DASHSCOPE_API_KEY", "effort": Effort.HIGH},
    "reviewer": {"url": "http://127.0.0.1:11434/v1",
                 "model_id": "spark-x2.6-4b-abliterated:q6", "key_env": "", "effort": Effort.LOW},
}


def endpoint(role: str) -> dict:
    """`HARNESS_<ROLE>_URL/MODEL` 覆盖表里的默认值。"""
    spec = ENDPOINTS[role]
    prefix = f"HARNESS_{role.upper()}_"
    return {"url": os.getenv(prefix + "URL", spec["url"]),
            "model_id": os.getenv(prefix + "MODEL", spec["model_id"]),
            "key_env": spec["key_env"], "effort": spec["effort"], "role": role}


def key_of(config: dict) -> str:
    """读 key，不抛异常：体检要能如实报告"没 key"。已设的同名环境变量优先于 .env。"""
    return os.environ.get(config["key_env"], "").strip() if config["key_env"] else ""


def reviewer_parts(review: str, assistant: dict, local: dict, key: str,
                   reviewer_model: str | None) -> tuple[ModelClient, ModelSettings]:
    """审核档位 → (审核客户端, 审核用的模型设置)。三个档位只在"谁来看"上不同。

    两个模型档位都带 `thinking=False`：实测走 `chat/completions` 时端点会顺手生成思维链
    （在线一次 94 个 reasoning token，本地一次 342–797 字），而我们只读 `content`。
    实验 03：判决中位 2.69 s → 0.82 s，二十条命令 16 条判决不变；漏掉的三条
    （`Set-Date`、`Set-MpPreference`、`Set-Content ..\\`）现在由硬规则与路径监狱兜住。
    超时按"关思考后中位 0.82 s"的十倍留：本地 8 s（含模型冷加载约 3 s），在线 20 s。
    """
    if review == "off":  # 不发请求，永远放行；硬规则与路径监狱仍在 guard 那一道
        return NoReviewClient(), ModelSettings(reviewer_model or "none", Effort.LOW, thinking=False)
    if review == "online":  # 审核也交给在线端点：同一把 key，默认同一个模型，低温求确定性
        return (OpenAIClient(assistant["url"], key, timeout=20.0),
                ModelSettings(reviewer_model or assistant["model_id"], Effort.LOW, thinking=False))
    return (OpenAIClient(local["url"], "", timeout=8.0),
            ModelSettings(reviewer_model or local["model_id"], local["effort"], thinking=False))


def build(workspace: Path, steps: int = 12, assistant_model: str | None = None,
          reviewer_model: str | None = None, review: str = "local",
          guard: bool = True) -> AgentService:
    """设计期那张装配清单那四行的展开：造客户端、造存储、造 service。

    `review`：`local` 本地模型审（默认） / `online` 在线模型审 / `off` 不审。
    `guard`：False 时把代码闸门整个摘掉（硬规则 + 路径监狱），只用于"完全无审核"的对照实验。
    工作目录在这绑进 `guard`（`partial`），并算成轨迹文件名——编排层因此仍然不认识路径。
    """
    if review not in REVIEW_MODES:
        raise SystemExit(f"--review 只接受 {' / '.join(REVIEW_MODES)}，收到 {review!r}")
    a, r = endpoint("assistant"), endpoint("reviewer")
    key = key_of(a)
    if not key and not a["url"].startswith(("http://127.0.0.1", "http://localhost")):
        raise SystemExit(f"{a['key_env']} 未设置：写进 {BASE / '.env'} 或在终端 set 它")
    reviewer_client, reviewer_settings = reviewer_parts(review, a, r, key, reviewer_model)
    if not guard:
        print("[警告] --no-guard：硬规则与路径监狱也关了，任何工具调用都不会被拦（只该用于对照实验）",
              file=sys.stderr)
    return AgentService(
        model_client=OpenAIClient(a["url"], key),
        reviewer_client=reviewer_client,
        session_store=JsonlSessionStore(BASE / "sessions"),
        assistant=Assistant(prompts.assistant_instruction(str(workspace), conda_python()),
                            (ShellTool(workspace), PythonTool(workspace))),  # 本机是 Windows：没有 BashTool
        reviewer=ToolCallReviewer(prompts.reviewer_instruction(str(workspace))),
        assistant_model_settings=ModelSettings(assistant_model or a["model_id"], a["effort"]),
        reviewer_model_settings=reviewer_settings,
        max_iterations=steps,
        guard=partial(hard_deny, workspace=str(workspace)) if guard else no_guard,
        session_id=session_slug(workspace),
    )


def rig(workspace: Path) -> Rig:
    """体检零件：端点怎么解析、key 怎么读、审核指令是什么。"""
    return Rig(base=BASE, workspace=workspace, endpoint=endpoint, key_for=key_of,
               reviewer_instruction=prompts.reviewer_instruction(str(workspace)))


app = build_app(Ports(make_service=build, make_store=lambda: JsonlSessionStore(BASE / "sessions"),
                      make_rig=rig, base=BASE))

if __name__ == "__main__":
    app()
