# -*- coding: utf-8 -*-
"""AI 实时模式（live）链路的回归测试。

覆盖本轮修的两条断点（交接文档 2026-10-19 §4.2 的 1/2 条）：
  · SyncEngine.start_live 之后，动作表只能靠 feed_live 灌入；
    "设备连着、识别在跑、却没进 live 会话"会让 tick 永远空转。
  · feed_live 的幂等与 _last_n 复位语义（时间轴变长必须重发）。

运行：python -m unittest tests.test_vision_live_sync -v
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))          # vendor.device.* 包导入
sys.path.insert(0, str(APP_DIR / "vendor"))

from device.arbiter import DeviceArbiter                        # noqa: E402
from device.channel import ChannelState                          # noqa: E402
from device.preset_player import PresetPlayer                    # noqa: E402
from device.quick_moves import QuickMoves                        # noqa: E402
from device.sync_engine import SyncEngine                        # noqa: E402


class FakeChannel:
    """记录成功落下去的帧；急停闸门语义与真通道一致（拒写不记录）。"""

    def __init__(self):
        self.state = ChannelState()
        self.state.connected = True
        self.max_speed = 500
        self.range_lo, self.range_hi = 0.0, 100.0
        self.calls: list[tuple[float, int | None]] = []
        self.on_move = None

    async def move_to(self, percent, speed=None, raw=False, *, bypass_estop=False):
        if not self.state.allow_move and not bypass_estop:
            return False
        self.calls.append((float(percent), None if speed is None else int(speed)))
        if self.on_move:
            try:
                self.on_move(percent, speed)
            except Exception:  # noqa: BLE001
                pass
        return True

    def set_allow_move(self, v: bool):
        self.state.allow_move = bool(v)


class TestLiveSession(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.ch = FakeChannel()
        self.quick = QuickMoves(self.ch, self.loop)
        self.preset = PresetPlayer(self.ch, self.loop, None)
        self.sync = SyncEngine(self.ch, self.loop)
        self.arb = DeviceArbiter(self.ch, self.quick, self.preset, self.sync)

    def tearDown(self):
        self.quick.stop_orgasm()
        self.quick.stop_slow()
        self.loop.close()

    def _pump(self, t, rate=1.0, rounds=6):
        """跑一拍 tick 并让 BLE 事件循环执行完投递的 _apply。"""
        self.sync.tick(t, rate)
        self.loop.run_until_complete(asyncio.sleep(0))
        for _ in range(rounds):
            self.loop.run_until_complete(asyncio.sleep(0))

    # ---------------------------------------------------------------- 基本链路
    def test_start_live_then_feed_produces_moves(self):
        r = self.arb.start_live("v.mp4")
        self.assertTrue(r.get("ok"))
        self.assertTrue(self.sync.live)
        self.assertEqual(self.sync.actions, [])
        self._pump(1.0)
        self.assertEqual(self.ch.calls, [], "没有动作表时不应写帧")

        n = self.sync.feed_live(
            [{"at": 0, "pos": 10}, {"at": 1000, "pos": 90}, {"at": 2000, "pos": 10}], 3000.0)
        self.assertEqual(n, 3)
        self._pump(0.5)
        self.assertEqual(len(self.ch.calls), 1, "动作表灌入后应下发首段")
        self.assertEqual(self.ch.calls[0][0], 90.0, "段末位置 = 段上界 b.pos")
        self._pump(1.5)
        self.assertEqual(self.ch.calls[-1][0], 10.0, "进入下一段应下发新的段末位置")

    def test_same_segment_is_not_resent(self):
        self.arb.start_live("v.mp4")
        self.sync.feed_live([{"at": 0, "pos": 0}, {"at": 4000, "pos": 100}], 4000.0)
        self._pump(1.0)
        self._pump(1.5)
        self._pump(2.0)
        self.assertEqual(len(self.ch.calls), 1, "段没变不重复发帧（手机端 n != lastIndex 同款）")

    def test_growing_coverage_resets_last_index(self):
        self.arb.start_live("v.mp4")
        self.sync.feed_live([{"at": 0, "pos": 0}, {"at": 4000, "pos": 100}], 4000.0)
        self._pump(1.0)
        self.assertEqual(len(self.ch.calls), 1)
        # 引擎前缀重算：时间轴变长 → _last_n 复位，同一段里也要重发一次
        self.sync.feed_live([{"at": 0, "pos": 0}, {"at": 4000, "pos": 55},
                             {"at": 8000, "pos": 100}], 8000.0)
        self.assertIsNone(self.sync._last_n)
        self._pump(1.0)
        self.assertEqual(len(self.ch.calls), 2)
        self.assertEqual(self.ch.calls[-1][0], 55.0, "重算后的段末位置应立刻生效")

    def test_tick_outside_action_range_sends_nothing(self):
        self.arb.start_live("v.mp4")
        self.sync.feed_live([{"at": 1000, "pos": 0}, {"at": 2000, "pos": 100}], 5000.0)
        self._pump(0.2)       # 首个动作点之前
        self._pump(3.0)       # 末个动作点之后
        self.assertEqual(self.ch.calls, [])

    # ---------------------------------------------------------------- 闸门
    def test_estop_blocks_live_frames(self):
        self.arb.start_live("v.mp4")
        self.sync.feed_live([{"at": 0, "pos": 0}, {"at": 1000, "pos": 100}], 2000.0)
        self.ch.set_allow_move(False)
        self._pump(0.5)
        self.assertEqual(self.ch.calls, [], "急停时 live 帧必须被拒写")
        self.ch.set_allow_move(True)
        self._pump(0.6)
        self.assertEqual(len(self.ch.calls), 1, "解除急停后下一段应恢复下发")

    def test_arbiter_start_live_stops_preset(self):
        self.preset.playing = True
        self.arb.start_live("v.mp4")
        self.assertFalse(self.preset.playing, "live 会话开始必须清预设（单一写者 I1）")

    def test_stop_clears_live(self):
        self.arb.start_live("v.mp4")
        self.sync.feed_live([{"at": 0, "pos": 0}, {"at": 1000, "pos": 100}], 2000.0)
        self.sync.stop()
        self.assertFalse(self.sync.live)
        self.assertFalse(self.sync.active)
        self._pump(0.5)
        self.assertEqual(self.ch.calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
