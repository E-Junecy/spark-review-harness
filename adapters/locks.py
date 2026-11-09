"""适配层·工作区互斥锁：一工作区一条轨迹，两个窗口同时写同一文件就会互相覆盖。

`save()` 是整体重写而不是追加，所以并发写的后果不是顺序混乱，是真的丢数据。锁就干一件事：
让同一个工作区在任一时刻只被一个进程持有。

判活这一条被实测纠正过一次。**`OpenProcess` 成功只证明"进程对象还存在"，不证明"它在跑"**：
Windows 只要还有任何一个句柄引用，退出的进程对象就一直留着（父进程没 `wait()` 的 subprocess、
服务、崩溃后被继承的句柄，全都满足）。本机对照实验：子进程已经退了（`Popen.poll()` 返回 0），
`OpenProcess` 照样成功，而同一个句柄上 `GetExitCodeProcess` 给出 `exit=0`（不是 259）。
所以判据换成"开得到句柄 **且** 退出码还是 STILL_ACTIVE"。

两个方向搞反就是事故，所以分得很清：

- **看不见**（`OpenProcess` 失败且 `GetLastError()==5`，进程存在但不归我管，比如提权进程）→
  保守**不抢**。宁可让人手动删锁，也不能让两个进程写同一个轨迹。
- **确证已退**（退出码不是 STILL_ACTIVE）或 **压根不存在**（`gle==87`）→ 回收。
- **锁内容读不出来**（手改过、写一半崩了）→ 按 mtime 给 30 分钟宽限，超过才回收。

还没做的一件事，如实记着：**pid 复用**。占用者退了、pid 又被别人家进程拿去用，这里会判成"还在运行"
而拒不停（方向是安全的，只是烦）。彻底治它要在锁里再记一份进程创建时间（`GetProcessTimes`）比对。

不做的事：不等待、不自旋、不跨机。同一台机器上抢不到就直接报"谁在占用"，让人换个窗口或换档位。
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import sys
import time
from pathlib import Path

QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259  # GetExitCodeProcess 的这个值才代表"仍在运行"
ERROR_ACCESS_DENIED = 5
UNKNOWN_LOCK_GRACE = 1800.0  # 内容读不出来的锁，mtime 超过 30 分钟才敢回收


def process_state(pid: int) -> str:
    """`running` / `exited` / `hidden` / `gone`。调用方按方向决定抢还是不抢。"""
    if pid <= 0:
        return "gone"
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except PermissionError:  # 存在但不归我管
            return "hidden"
        except OSError:
            return "gone"
        return "running"
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return "hidden" if kernel32.GetLastError() == ERROR_ACCESS_DENIED else "gone"
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return "running"  # 问不出来就当作还活着：判不准的时候一律保守
        return "running" if code.value == STILL_ACTIVE else "exited"
    finally:
        kernel32.CloseHandle(handle)


def pid_alive(pid: int) -> bool:
    """确实在跑，或者存在但我看不见（后者也算活着，绝不因为看不见就抢）。"""
    return process_state(pid) in ("running", "hidden")


class WorkspaceLocked(RuntimeError):
    """工作区已被另一个进程占用。`holder` 是锁文件里记录的占用者信息，`state` 是判活结论。"""

    SAYS = {"running": "还在运行", "hidden": "进程在但我看不见（受保护/提权），保守不抢",
            "gone": "没有这个进程", "exited": "已经退了", "unknown": "锁内容读不出"}

    def __init__(self, path: Path, holder: dict, state: str = "unknown"):
        self.path, self.holder, self.state = path, holder, state
        says = self.SAYS.get(state, state)
        super().__init__(f"{holder.get('pid', '?')} 号进程占用着 {holder.get('workspace', '?')}"
                         f"（{says}）：{str(holder.get('cmd', ''))[:60]}")


class WorkspaceLock:
    """`with WorkspaceLock(path, workspace):` 包住一次运行；抢不到抛 `WorkspaceLocked`。"""

    def __init__(self, path: Path, workspace: Path, note: str = ""):
        self.path, self.workspace = Path(path), Path(workspace)
        self.note = note or " ".join(sys.argv[:3])
        self.held = False

    def acquire(self) -> WorkspaceLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):  # 一次重试：死锁回收后要能立刻抢上
            try:
                handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                holder, state = self._holder()
                if state == "unknown" and self._too_old():
                    state = "exited"  # 读不出内容，但已经放了半小时：当作陈旧，回收
                if state in ("running", "hidden", "unknown"):
                    raise WorkspaceLocked(self.path, holder, state) from None
                with contextlib.suppress(OSError):
                    self.path.unlink()  # 占用者确证已退（或压根没这个 pid）：回收
                continue
            try:
                os.write(handle, json.dumps({"pid": os.getpid(), "at": time.time(),
                                             "workspace": str(self.workspace), "cmd": self.note},
                                            ensure_ascii=False).encode("utf-8"))
            finally:
                os.close(handle)
            self.held = True
            return self
        raise WorkspaceLocked(self.path, {"pid": "?", "workspace": str(self.workspace)},
                              "锁文件反复存在，无法回收")

    def _holder(self) -> tuple[dict, str]:
        """锁文件内容 + 判活结论。读不出内容一律 `unknown`（不敢抢，除非老过宽限期）。"""
        try:
            holder = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}, "unknown"
        return holder, process_state(int(holder.get("pid", -1)))

    def _too_old(self) -> bool:
        try:
            return time.time() - self.path.stat().st_mtime > UNKNOWN_LOCK_GRACE
        except OSError:
            return False

    def release(self) -> None:
        if self.held:
            with contextlib.suppress(OSError):
                self.path.unlink()
            self.held = False

    def __enter__(self) -> WorkspaceLock:
        return self.acquire()

    def __exit__(self, *_) -> None:
        self.release()
