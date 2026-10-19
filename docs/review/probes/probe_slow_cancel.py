# -*- coding: utf-8 -*-
"""复现"点待机缓动立即被取消"：视频在播 → 点缓动 → 看护把脚本拉回来 → 缓动被顶掉。

一键回放链路（假通道，无需真机）：
  1) 视频在播、脚本同步活跃
  2) 用户点"待机缓动" → arbiter.start_slow()
  3) 旧代码 start_slow 会停 sync（_stop_all_except），sync.active 变 false
  4) 前端看护（pollDev）看到 want && !active → syncStart → arbiter.start_script()
  5) 旧代码 start_script 停 slow → is_slow 变 False ⇒ "点缓动立马自动取消"
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")
sys.path.insert(0, "vendor")

from device.arbiter import DeviceArbiter            # noqa: E402
from device.channel import ChannelState             # noqa: E402
from device.preset_player import PresetPlayer       # noqa: E402
from device.quick_moves import QuickMoves           # noqa: E402
from device.sync_engine import SyncEngine           # noqa: E402


class FakeChannel:
    def __init__(self):
        self.state = ChannelState()
        self.state.connected = True
        self.max_speed = 350
        self.range_lo, self.range_hi = 0.0, 100.0
        self.calls = []
        self.on_move = None

    async def move_to(self, percent, speed=None, raw=False, *, bypass_estop=False):
        if not self.state.allow_move and not bypass_estop:
            return False
        self.calls.append((float(percent), speed))
        if self.on_move:
            try:
                self.on_move(percent, speed)
            except Exception:
                pass
        return True

    def set_allow_move(self, v):
        self.state.allow_move = bool(v)


async def main():
    d = Path(tempfile.mkdtemp(prefix="probe-slow-cancel-"))
    (d / "presets.json").write_text(json.dumps(
        [{"id": "normal", "name": "n", "segments": [[0, 100, 100]]}]), encoding="utf-8")
    (d / "video.mp4").write_bytes(b"x")
    (d / "video.funscript").write_text(json.dumps(
        {"actions": [{"at": 0, "pos": 10}, {"at": 1000, "pos": 90}, {"at": 2000, "pos": 10}]}),
        encoding="utf-8")
    loop = asyncio.get_running_loop()
    ch = FakeChannel()
    quick = QuickMoves(ch, loop)
    preset = PresetPlayer(ch, loop, d / "presets.json")
    sync = SyncEngine(ch, loop)
    arb = DeviceArbiter(ch, quick, preset, sync)
    vp = str(d / "video.mp4")

    arb.start_script(vp)
    print("1) 脚本同步活跃:", sync.active, "（视频在播、want=true）")
    arb.start_slow()
    print("2) 点待机缓动后: is_slow =", quick.is_slow, " sync.active =", sync.active)
    if not sync.active:
        res = arb.start_script(vp)          # 前端看护 pollDev 的等价调用
        print("3) 看护把脚本拉起来:", {k: res.get(k) for k in ("ok", "deferred")},
              " → is_slow =", quick.is_slow)
        print("判定:", "❌ 缓动被顶掉（'点一下立马自动取消'）" if not quick.is_slow else "✓ 缓动保住")
    else:
        print("3) sync 仍活跃 → 看护不触发 → 缓动保住 ✓")


asyncio.run(main())
