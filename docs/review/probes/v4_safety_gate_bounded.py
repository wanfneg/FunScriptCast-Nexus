"""R04 只读探针 5（有界）：急停安全闸 / 缓动看门狗 / 空闲计时（R01-A3/A4/A5/A17）。

用假通道精确复刻 channel.py:248-275 的语义（allow_move 闸 + force 绕过 + 成功后回调 on_move），
在**真实事件循环**里跑真实 QuickMoves / PresetPlayer 代码。

只读：不启动 GUI/服务器，不写文件。总时长约 16s，30s 看门狗强制收尾。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "vendor"))

from device.preset_player import PresetPlayer  # noqa: E402
from device.quick_moves import QuickMoves      # noqa: E402

PHASE = {"v": "boot"}
RESULT: list[str] = []


def watchdog() -> None:
    time.sleep(30.0)
    print(f"[watchdog] 30s 到（phase={PHASE['v']}）—— 强制退出", flush=True)
    os._exit(9)


class FakeChannel:
    """复刻 channel.py 的闸门与回调语义（不做 BLE）。"""

    def __init__(self):
        self.state = SimpleNamespace(allow_move=True, connected=True, moves=0)
        self.max_speed = 500
        self.range_lo, self.range_hi = 0.0, 100.0
        self.reversed = False
        self.on_move = None
        self.frames: list[tuple] = []

    def set_allow_move(self, allow: bool) -> None:
        self.state.allow_move = bool(allow)

    async def move_to(self, percent, speed=None, force=False, raw=False):
        if not self.state.allow_move and not force:      # channel.py:252-253
            return False
        self.frames.append((round(float(percent), 2), speed, force, raw, time.monotonic()))
        self.state.moves += 1
        if self.on_move:                                  # channel.py:269-274
            self.on_move(percent, speed)
        return True


async def wait_frames(ch: FakeChannel, n: int, timeout: float) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if len(ch.frames) >= n:
            return True
        await asyncio.sleep(0.05)
    return False


async def main() -> None:
    # ---- A3：急停（allow_move=False）挡不住待机缓动 ----
    PHASE["v"] = "A3 缓动 + 急停"
    ch = FakeChannel()
    q = QuickMoves(ch, asyncio.get_running_loop())
    q.slow.idle_detect_seconds = 1
    q.start_slow()
    got = await wait_frames(ch, 1, 3.0)
    n0 = len(ch.frames)
    q.set_stop(True)                                      # 模拟 UI「一键急停」的 set_allow_move(False)
    await asyncio.sleep(2.2)
    after = ch.frames[n0:]
    RESULT.append(
        f"A3 缓动起来={'是' if got else '否'}；急停后 allow_move={ch.state.allow_move}；"
        f"急停后 2.2s 内仍写出 {len(after)} 帧 -> {after}")
    RESULT.append(f"A3 [断言] 急停挡不住缓动（>0 帧且全部 force=True）: "
                  f"{'PASS' if after and all(f[2] for f in after) else 'FAIL'}")
    q.stop_slow()

    # ---- A4：急停挡不住预设播放 ----
    PHASE["v"] = "A4 预设 + 急停"
    ch2 = FakeChannel()
    ch2.state.allow_move = False                          # 先急停
    q2 = QuickMoves(ch2, asyncio.get_running_loop())      # 装 on_move 回调（与真实一致）
    p = PresetPlayer(ch2, asyncio.get_running_loop(), ROOT / "ui" / "presets.json")
    p.select("normal")
    p.start()
    await asyncio.sleep(1.5)
    RESULT.append(f"A4 已急停(allow_move=False)期间，预设 'normal' 1.5s 内写出 {len(ch2.frames)} 帧 "
                  f"-> {[(f[0], f[1], f[2]) for f in ch2.frames]}")
    RESULT.append(f"A4 [断言] 急停挡不住预设（>0 帧）: {'PASS' if ch2.frames else 'FAIL'}")
    p.stop()

    # ---- A5：一次外部动作后缓动永不重启 ----
    PHASE["v"] = "A5 外部帧后缓动重启"
    ch3 = FakeChannel()
    q3 = QuickMoves(ch3, asyncio.get_running_loop())
    q3.slow.idle_detect_seconds = 1
    q3.start_slow()
    await wait_frames(ch3, 1, 3.0)
    gen_before = q3._gen_s
    n3 = len(ch3.frames)
    ch3.on_move(42, 100)                                  # 等价脚本/手动/爆发的一帧
    await asyncio.sleep(3.0)
    after3 = ch3.frames[n3:]
    RESULT.append(f"A5 外部帧前 _gen_s={gen_before} -> 外部帧后 _gen_s={q3._gen_s}, "
                  f"is_slow={q3.is_slow}, _slow_task={q3._slow_task}")
    RESULT.append(f"A5 外部帧后 3.0s 内新帧数 = {len(after3)}；"
                  f"is_slow 仍为 {q3.is_slow} -> 缓动确实没有重启")
    RESULT.append(f"A5 [断言] is_slow=True 但缓动不再恢复: "
                  f"{'PASS' if q3.is_slow and not after3 else 'FAIL'}")
    q3.stop_slow()

    # ---- A17：暂停很久后 start_slow，空闲计时不重置 => 立刻抢跑 ----
    PHASE["v"] = "A17 start_slow 不重置空闲计时"
    ch4 = FakeChannel()
    q4 = QuickMoves(ch4, asyncio.get_running_loop())
    q4.slow.idle_detect_seconds = 5
    q4._last_external = time.time() - 600                 # 等价"暂停了 10 分钟再恢复"
    t0 = time.monotonic()
    q4.start_slow()
    ok4 = await wait_frames(ch4, 1, 3.0)
    dt = time.monotonic() - t0
    RESULT.append(f"A17 idle_detect_seconds=5 但第一帧在 {dt:.2f}s 就出现（应为 5s）")
    RESULT.append(f"A17 [断言] 抢跑（dt < 1.5s）: {'PASS' if ok4 and dt < 1.5 else 'FAIL'}")
    q4.stop_slow()


threading.Thread(target=watchdog, daemon=True).start()
try:
    asyncio.run(main())
    print("\n".join(RESULT))
    print("[probe done]")
finally:
    pass
