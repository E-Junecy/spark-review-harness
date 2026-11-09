"""把 textual 导出的 SVG 还原成画面文本：我这个模型看不了图，只能靠文字核对渲染内容。

rich 的 SVG 每个字符片段是一个 <text ...>，带 y 坐标；按 y 分组、按 x 排序拼回一行，就是终端那一屏。
"""

import html
import os
import re
import sys
from pathlib import Path

OUT = Path(os.environ.get("SHOTS_DIR", "_tmp/shots"))
WANTED = sys.argv[1:] or ["1-实时进行中", "3-多段对话与成本账"]

for name in WANTED:
    svg = (OUT / f"{name}.svg").read_text(encoding="utf-8")
    rows: dict[float, list[tuple[float, str]]] = {}
    for m in re.finditer(r'<text[^>]*?x="([\d.]+)"[^>]*?y="([\d.]+)"[^>]*>(.*?)</text>', svg, re.S):
        x, y, chunk = float(m.group(1)), float(m.group(2)), re.sub(r"<[^>]+>", "", m.group(3))
        rows.setdefault(y, []).append((float(x), html.unescape(chunk)))
    print(f"\n===== {name} =====")
    for y in sorted(rows):
        line = "".join(text for _, text in sorted(rows[y]))
        if line.strip():
            print(line.rstrip())
