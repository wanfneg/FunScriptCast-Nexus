"""只读探针：/api/quick 的 5 个分支与 allow_move 状态机的真实行为。

断言目标（对应 host_server.py:2545-2569 的每个分支）：
  1) pause  → pause_for_player()：停 orgasm/slow 并记下"待恢复"，**不动 allow_move**
  2) resume → resume_for_player()：恢复 orgasm/slow，**不动 allow_move**
  3) stop(on=True)  → allow_move=False（全局闸门）
  4) stop(on=False) → allow_move=True + reset_last_index()
  5) 急停期间 prompt 预设/缓动是否仍写帧（第一轮已确认会，这里只做交叉验证）

同时验证 `QuickMoves.set_stop()` 的返回结构 —— host_server.py:2551 读的是 `q.is_stop`（不存在）。
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "vendor"))

from device.quick_moves import QuickMoves  # noqa: E402


class FakeChannel:
    """只记录调用的假通道；不连蓝牙。"""

    def __init__(self):
        self.max_speed = 500
        self.range_lo, self.range_hi = 0.0, 100.0
        self.calls = []
        self._allow = True
        self.state = type("S", (), {"allow_move": True, "connected": True})()

    def set_allow_move(self, allow):
        self._allow = bool(allow)
        self.state.allow_move = bool(allow)
        self.calls.append(("set_allow_move", bool(allow)))

    async def move_to(self, percent, speed=None, force=False, raw=False):
        self.calls.append(("move_to", round(float(percent), 1), speed, force, raw))
        await asyncio.sleep(0.001)
        return True


def run_scenario(name, script) -> dict:
    """script(ch, q) 在事件循环里跑；返回调用记录与最终状态。"""
    loop = asyncio.new_event_loop()
    ch = FakeChannel()
    q = QuickMoves(ch, loop)
    out = {}

    async def main():
        script(ch, q)
        await asyncio.sleep(1.2)          # 给循环时间发帧
        out["frames"] = [c for c in ch.calls if c[0] == "move_to"]
        out["gate_calls"] = [c for c in ch.calls if c[0] == "set_allow_move"]

    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    fut = asyncio.run_coroutine_threadsafe(main(), loop)
    fut.result(timeout=20)
    loop.call_soon_threadsafe(loop.stop)
    out["allow_move_end"] = ch.state.allow_move
    out["is_stop_attr"] = (hasattr(q, "is_stop"),
                           getattr(q, "is_stop", "<missing>"))
    out["state_stop_field"] = q.state().get("stop")
    return {"scenario": name, **out}


def scenario_orgasm_then_pause_resume(ch, q):
    q.start_orgasm()
    q.pause_for_player()      # 视频暂停
    q.resume_for_player()     # 视频继续


def scenario_emergency_then_preset(ch, q):
    q.set_stop(True)          # 一键急停
    # 急停期间启动爆发（看它是否还写帧）
    q.start_orgasm()


def scenario_pause_only(ch, q):
    q.pause_for_player()      # 视频暂停（此前没有爆发/缓动在跑）


def scenario_slow_gate_release(ch, q):
    q.start_slow()
    q.set_stop(True)
    q.set_stop(False)         # 急停→继续


if __name__ == "__main__":
    res = []
    for nm, fn in (("orgasm→pause→resume", scenario_orgasm_then_pause_resume),
                   ("急停后启动爆发", scenario_emergency_then_preset),
                   ("只暂停(无爆缓动)", scenario_pause_only),
                   ("缓动→急停→继续", scenario_slow_gate_release)):
        res.append(run_scenario(nm, fn))
    print(json.dumps(res, ensure_ascii=False, indent=2))
