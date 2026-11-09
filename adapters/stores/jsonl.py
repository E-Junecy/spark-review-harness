"""适配层·jsonl 存储：Session <-> 一行行 JSON。

第 1 行是 `{"kind":"session","id":…}`，之后每条消息一行 `{"kind":"message","type":…线格式…}`。
行的形状与重构前**完全兼容**：旧 `sessions/*.jsonl` 照样能读能回放（有测试拿真旧轨迹钉住）。
新增的 `ts` / `review_ms` / `segment` / 三个 token 计数都是可选观测字段，旧文件里没有就取默认值。

每条消息产生时整体重写：文件很小，换来"文件永远等于当前状态"，`load` 回来就是完整现场，
`-r` 续聊因此不需要任何额外逻辑。

会话文件名由 `session_slug()` 从工作区算出（`<目录名>-<路径哈希>`）：一工作区一条轨迹，
所以同一个目录里的多次对话天然排在同一个文件里，界面上也认得出是哪件事的轨迹。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from adapters.wire import from_wire, message_row, session_row
from app.ports import SessionStore
from domain.session import Session


def session_slug(workspace: Path) -> str:
    """工作区目录 → 轨迹文件名。`resolve()` 之后大小写、`..`、8.3 短名四种写法都会归一，
    所以同一个目录不会裂成两条轨迹。残留：映射盘符与 UNC 互为别名时仍会分裂，已知边界。"""
    absolute = Path(workspace).resolve()
    readable = re.sub(r"[^\w.\u4e00-\u9fff-]+", "_", absolute.name).strip("_.") or "root"
    digest = hashlib.sha1(str(absolute).lower().encode("utf-8")).hexdigest()[:8]
    return f"{readable[:40]}-{digest}"


class JsonlSessionStore(SessionStore):
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, session_id: str) -> Path:
        return self.root / f"{session_id}.jsonl"

    def list(self) -> list[Session]:
        return [self.load(p.stem) for p in sorted(self.root.glob("*.jsonl"))]

    def save(self, session: Session) -> None:
        rows = [session_row(session)] + [message_row(m) for m in session.messages]
        self.path(session.id).write_text(
            "\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in rows) + "\n", encoding="utf-8")

    def load(self, session_id: str) -> Session:
        """读回对话。存不进这三种类型的东西不该存在，所以这里不兜底。"""
        text = self.path(session_id).read_text(encoding="utf-8")
        rows = (json.loads(line) for line in text.splitlines() if line.strip())
        return Session(session_id, [from_wire(r) for r in rows if r["kind"] == "message"])
