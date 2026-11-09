r"""应用层·策略：审核链里不需要劳驾模型的那两环。

`hard_deny` 排在模型前面。理由写在 docs/spark-harness-design.md：本地那个 4B 是无对齐模型，
注入一句就吐出系统提示词，拿它兜底 `rm -rf` / `format` 是假安全——危险形状先由正则拦下，
未命中的灰区才交给 reviewer。

`audit` 是回给模型的那句人话：审核出处和工具观察一起写进 `ToolResult.content`，
审计因此不需要第二份日志，拒绝也照样有理由可读。

`outside_write`（写在 `hard_deny` 里）是**路径监狱**，实验 03 逼出来的：关掉思维链之后 spark
会放行 `Set-Content -Path ..\out.txt` 这种越界写，而"路径在不在工作目录里"本来就是代码能算准
的事，不该问模型。监狱只管**写**——读工作目录之外是正当需求（实验轨迹本身就在读 `sessions/*.jsonl`）。

可替换性靠 `AgentService.guard` 这个字段：换一套策略不必动编排；组装根用 `partial` 把工作目录绑进去。

`REVIEW_MODES` 是 `main.py --review` 的三个档位：`local` 本地模型审、`online` 在线模型审、
`off` 不审（只留代码硬规则与路径监狱）。它放在应用层是因为"审核链有几个档"属于策略，不属于某个前端。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable

from domain.roles import Decision
from domain.session import ToolCall

REVIEW_MODES = ("local", "online", "off")

DENY_PATTERNS = (
    r"\brm\s+-[rf]{1,2}\b", r"\bdel\s+/[sq]", r"\brd\s+/s", r"remove-item\b.*-(recurse|force)\b",
    r"\bformat(\.exe)?\b\s+[a-z]:", r"\bdiskpart\b", r"\bshutdown\b", r"\breg\s+delete\b",
    r"\bnet\s+user\b", r"\bvssadmin\b", r"\bcipher\s+/w", r"git\s+push\s.*(--force|-f)\b",
    r"invoke-expression", r"\biex\s*\(", r"curl\b.*\|\s*(sh|bash|pwsh|iex)", r"wget\b.*\|\s*(sh|bash|pwsh|iex)",
    r"\bshutil\.rmtree\s*\(\s*[\"'](?:[a-z]:\\\\?|/)[\"']", r"\bos\.system\s*\(\s*[\"'](rm|del|format)",
    # 系统状态变更：实验 03 实测关掉思维链的 spark 会放行 Set-Date / Set-MpPreference，
    # 而这类形状代码判得比模型准，所以也归硬规则。
    r"\bset-date\b", r"\bw32tm\b.*(/config|/unregister)", r"\bset-service\b.*-(startuptype|status)\b",
    r"\bset-mppreference\b", r"\breg\s+add\b.*\\run(once)?\b",
)
DENY_RULES = tuple(re.compile(p, re.I) for p in DENY_PATTERNS)

# 路径监狱。只认"写"这个动作 + 一个明确指向目录外的路径，两者同时出现才拦：
# 读工作目录之外是正当需求（实验轨迹本身就在读 sessions/*.jsonl），管读不管写才是这里的边界。
WRITE_VERB = re.compile(
    r"(?:set-content|add-content|out-file|\btee\b|new-item|\bni\b|remove-item|\bri\b|\brm\b|\bdel\b|\brd\b"
    r"|move-item|\bmi\b|\bmv\b|copy-item|\bcp\b|\bci\b|mkdir|\bmd\b|write_text|write_bytes"
    r"|shutil\.(?:rmtree|move|copy\w*)|os\.(?:remove|rename|mkdir|rmdir|unlink)"
    r"|open\s*\([^)]*[\"'][wax])", re.I)
PATHISH = re.compile(r"""((?:[A-Za-z]:[\\/]|\\\\|~[\\/]|\.\.[\\/])[^\s"'`;|)\]]*)""")

Guard = Callable[[ToolCall], Decision | None]


def outside(candidate: str, workspace: str) -> bool:
    """归一化之后不在工作目录里面就算越界；认不出来的路径按越界处理（宁可保守）。

    只用 `os.path` 的**词法**函数（`normpath` 吃掉 `..`），不碰文件系统——被审的那条命令要写的
    路径本来就可能还不存在。也因此不必把工作目录当成 `Path` 递进应用层：分层纪律测试就是这么要求的。
    """
    try:
        raw = os.path.expanduser(candidate.strip())
        if not (os.path.isabs(raw) or os.path.splitdrive(raw)[0]):
            raw = os.path.join(workspace, raw)
        target, root = os.path.normpath(raw).lower(), os.path.normpath(workspace).lower()
    except (OSError, ValueError):
        return True
    return target != root and not target.startswith(root + os.sep)


def outside_write(text: str, workspace: str) -> str | None:
    """第一个"写在工作目录之外"的路径；没有写动作或都落在目录内则返回 None。"""
    if not WRITE_VERB.search(text):
        return None
    return next((raw for raw in PATHISH.findall(text) if outside(raw, workspace)), None)


def scan_text(call: ToolCall) -> str:
    """闸门扫的是**解码后的参数值**，不是 `describe()` 那段 JSON 文本。

    这条是被冒烟测试逼出来的：`describe()` 把反斜杠转义成 `\\\\`，于是
    `Set-Content -Path .\\note.txt` 里的 `\\note.txt` 长得像 UNC 绝对路径，工作区内的正常写入
    被判成越界（模型白烧一步）。正则读的应该是值，不是序列化形式。
    """
    return "\n".join((call.tool_name, *(str(value) for value in call.arguments.values())))


def hard_deny(call: ToolCall, workspace: str | None = None) -> Decision | None:
    """命中即拒、不劳模型；未命中返回 None，交给 reviewer。

    `workspace` 由组装根用 `partial` 绑进来：绑上了才启用路径监狱，没绑（测试里那种）就只做正则。
    """
    text = scan_text(call)
    for rule in DENY_RULES:
        if rule.search(text):
            return Decision(False, f"命中硬规则 {rule.pattern}，不经模型直接拒绝")
    if workspace is not None and (escaped := outside_write(text, workspace)) is not None:
        root = os.path.normpath(workspace)
        # 理由里必须写清"到底是哪个目录"：实测模型收到"请改写到工作目录里面"却不被告知工作目录是
        # 哪儿，会把同一个 `README.md` 连着交 4 次（sessions/experiment_3-4ded7f5f.jsonl 里 35 行
        # 有 7 行是策略拒绝），每一次都要重问一遍模型，纯烧 token。
        return Decision(False, f"写入路径 {escaped} 在工作目录之外，不经模型直接拒绝。"
                               f"工作目录只有 {root}：要写就改到 {root} 里面，别再交同一个路径")
    return None


def no_guard(call: ToolCall, workspace: str | None = None) -> Decision | None:
    """`--no-guard`：把整道代码闸门（硬规则 + 路径监狱）拆掉，只该用于对照实验。"""
    return None


def audit(decision: Decision, source: str, output: str) -> str:
    """{"review": 出处+理由, "output": 工具观察}：一条文本同时承担审计与告知。"""
    return json.dumps({"review": decision.label(source), "output": output}, ensure_ascii=False)
