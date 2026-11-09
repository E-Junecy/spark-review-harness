"""实拍：用真 service（在线 assistant + 本地 spark 审核）驱动 adapters/tui.py，抓三帧。

和 _tmp/shot.py 的区别只有一个：这里的 chat 真的会发请求，所以底栏的 token 账、审核秒数、
硬规则标签都来自真实运行，不是脚本编的。
"""

import asyncio
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from textual.widgets import Input  # noqa: E402

import main  # noqa: E402
from adapters.tui import HarnessApp  # noqa: E402

OUT = Path("_tmp/shots")
WS = Path("_tmp/t04real")
WS.mkdir(parents=True, exist_ok=True)
TASK = "在工作目录写 note.txt，内容是 hello，然后读回来确认，不要做别的事"


def dump(name: str) -> None:
    """SVG 里每个 <text> 片段带 x/y，按 y 分组拼回一屏，好在不支持图片的环境里核对内容。"""
    svg = (OUT / f"{name}.svg").read_text(encoding="utf-8")
    rows: dict[float, list[tuple[float, str]]] = {}
    for m in re.finditer(r'<text[^>]*?x="([\d.]+)"[^>]*?y="([\d.]+)"[^>]*>(.*?)</text>', svg, re.S):
        rows.setdefault(float(m.group(2)), []).append((float(m.group(1)), re.sub(r"<[^>]+>", "", m.group(3))))
    lines = ["".join(t for _, t in sorted(rows[y])) for y in sorted(rows)]
    print(f"\n===== {name} =====")
    print("\n".join(line.rstrip() for line in lines if line.strip()))


async def grab(app, name: str) -> None:
    (OUT / f"{name}.svg").write_text(app.export_screenshot(title=name), encoding="utf-8")
    subprocess.run(["rsvg-convert", "-w", "1568", "-b", "#101010", "-o", f"{OUT / name}.png",
                    f"{OUT / name}.svg"], check=True)
    dump(name)


async def run() -> None:
    service = main.build(WS, steps=6, review="local")
    app = HarnessApp(service, workspaces=[WS], base=Path("."))
    async with app.run_test(size=(112, 30)) as pilot:
        await pilot.pause()
        box = app.query_one("#prompt", Input)
        started = time.perf_counter()
        box.value = TASK
        box.post_message(Input.Submitted(box, box.value))
        frames, grabbed = [], 0
        while time.perf_counter() - started < 180:
            await asyncio.sleep(1.0)
            await pilot.pause()
            frames.append(not app.busy)
            if grabbed == 0 and app.steps >= 1:           # 第一帧：已经有审核结论落地
                await grab(app, "7-真实运行-进行中")
                grabbed = 1
            if frames[-1] and len(frames) > 4 and all(frames[-4:]):
                break                                     # 连续 4 秒不忙 = 这一轮收完了
        await asyncio.sleep(0.4)
        await grab(app, "8-真实运行-整轮结束")
        print(f"\n真实运行用时 {time.perf_counter() - started:.1f}s · 累计 {app.total_steps} 步 · "
              f"闸门标签「{app.guard_label()}」")


asyncio.run(run())
