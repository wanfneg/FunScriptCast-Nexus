# -*- coding: utf-8 -*-
"""第 2 轮审查第 1 批修复的验收测试（F1-F6，判据 A1/A2/A7/A8 + F5/F6 回归）。

用假通道跑，无需真机：python -m unittest tests.test_device_arbiter -v
对应清单：review/最终修复清单-R2.md
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))          # vendor.device.* 包导入
sys.path.insert(0, str(APP_DIR / "vendor"))

from device.arbiter import DeviceArbiter                     # noqa: E402
from device.channel import ChannelState, DeviceChannel       # noqa: E402
from device.preset_player import PresetPlayer, load_presets, to_segments  # noqa: E402
from device.quick_moves import QuickMoves                    # noqa: E402
from device.sync_engine import SyncEngine                    # noqa: E402


# ---------------------------------------------------------------- 假件
class FakeChannel:
    """记录**成功落下去的帧**；急停闸门语义与真通道一致（拒写不记录）。"""

    def __init__(self):
        self.state = ChannelState()
        self.state.connected = True
        self.max_speed = 500
        self.range_lo, self.range_hi = 0.0, 100.0
        self.calls: list[tuple[str, float]] = []
        self.owner = "test"
        self.on_move = None

    async def move_to(self, percent, speed=None, raw=False, *, bypass_estop=False):
        if not self.state.allow_move and not bypass_estop:
            return False
        self.calls.append((self.owner, float(percent)))
        if self.on_move:
            try:
                self.on_move(percent, speed)
            except Exception:
                pass
        return True

    def set_allow_move(self, v: bool):
        self.state.allow_move = bool(v)


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="nexus-arbiter-"))
        (self.dir / "presets.json").write_text(json.dumps([
            {"id": "normal", "name": "标准往复", "segments": [[0, 100, 100], [100, 0, 100]]},
            {"id": "classic", "name": "温柔正弦",
             "keyframes": [[10, 0], [50, 555], [90, 1110], [10, 1665]]},
        ], ensure_ascii=False), encoding="utf-8")
        (self.dir / "video.mp4").write_bytes(b"x")
        (self.dir / "video.funscript").write_text(json.dumps(
            {"actions": [{"at": 0, "pos": 10}, {"at": 1000, "pos": 90}, {"at": 2000, "pos": 10}]}),
            encoding="utf-8")

    def make_world(self):
        loop = asyncio.get_running_loop()
        ch = FakeChannel()
        quick = QuickMoves(ch, loop)
        preset = PresetPlayer(ch, loop, self.dir / "presets.json")
        sync = SyncEngine(ch, loop)
        arb = DeviceArbiter(ch, quick, preset, sync)
        return ch, quick, preset, sync, arb


# ---------------------------------------------------------------- F2 / A7
class TestPresetSegments(Base):
    def test_to_segments_matches_phone(self):
        # 手机端语义：speed=round(dist*1000/dur) 下限 1；durationMs=max(0,dur)；dist=0 只等待
        segs = to_segments([[10, 0], [50, 555], [50, 800], [10, 1665]])
        self.assertEqual(segs[0], [10.0, 50.0, 72, 555.0])      # 40*1000/555 → 72
        self.assertEqual(segs[1][2], 1)                          # dist=0 → speed 兜底 1
        self.assertEqual(segs[1][3], 245.0)                      # 只等待、不发帧
        self.assertEqual(segs[2], [50.0, 10.0, 46, 865.0])       # 40*1000/865 → 46
        # dur<=0 脏数据不出天文数字
        self.assertEqual(to_segments([[0, 100], [100, 100]])[0][2], 1)

    def test_all_24_real_presets_have_play_segments(self):
        prs = load_presets(APP_DIR / "ui" / "presets.json")
        self.assertEqual(len(prs), 24)
        wave = 0
        for pr in prs:
            if not pr.get("segments") and pr.get("keyframes"):
                pr["_play"] = to_segments(pr["keyframes"])
                wave += 1
            else:
                pr["_play"] = list(pr.get("segments") or [])
            self.assertTrue(pr["_play"], f"{pr['id']} 派生后仍是空段（会锁死事件循环）")
        self.assertEqual(wave, 22)     # 2 段式 + 22 波形式，与手机端构成一致

    async def test_keyframes_only_preset_yields(self):
        """原缺陷：空段列表让 _run 纯 CPU 自旋、事件循环整个锁死。
        修好后 _play_one_loop 必须在段间让出（wait_for 的超时回调能跑起来）。"""
        _, _, preset, _, _ = self.make_world()
        pr = preset.by_id("classic")
        preset.selected = "classic"
        preset.start()               # 走正常启动：_gen 自增，_can 才放行
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(preset._play_one_loop(pr, preset._gen), timeout=1.0)
        preset.stop()


# ---------------------------------------------------------------- F3 / A1
class TestArbiter(Base):
    async def test_a1_single_writer_matrix(self):
        ch, quick, preset, sync, arb = self.make_world()
        vp = str(self.dir / "video.mp4")
        # 任一时刻至多一个会话 active：后启动的把前面的停掉
        arb.start_preset()
        self.assertTrue(preset.playing)
        self.assertFalse(sync.active or quick.is_orgasm or quick.is_slow)
        arb.start_orgasm()
        self.assertTrue(quick.is_orgasm)
        self.assertFalse(preset.playing or quick.is_slow or sync.active)
        arb.start_slow()
        self.assertTrue(quick.is_slow)
        self.assertFalse(preset.playing or quick.is_orgasm or sync.active)
        # 脚本启动 → 其余三方全停
        res = arb.start_script(vp)
        self.assertTrue(res["ok"] and sync.active)
        self.assertFalse(preset.playing or quick.is_orgasm or quick.is_slow)
        # 反向：预设启动 → 清脚本 + 停快捷动作
        arb.start_preset()
        self.assertTrue(preset.playing)
        self.assertFalse(sync.active or quick.is_orgasm or quick.is_slow)
        # 爆发接管 → 停脚本/预设/缓动
        arb.start_script(vp)
        arb.start_slow()
        arb.start_orgasm()
        self.assertTrue(quick.is_orgasm)
        self.assertFalse(quick.is_slow or preset.playing or sync.active)
        # 脚本加载失败：不动现有会话（避免"停一片却没跑起来"）
        arb.start_preset()
        res = arb.start_script(str(self.dir / "nofile.mp4"))
        self.assertFalse(res["ok"])
        self.assertTrue(preset.playing)

    async def test_a2_estop_stops_all_sessions_and_no_auto_resume(self):
        ch, quick, preset, sync, arb = self.make_world()
        vp = str(self.dir / "video.mp4")
        arb.start_script(vp)
        arb.start_preset()
        arb.start_orgasm()
        arb.start_slow()
        # 急停（路由语义）：先全停会话，再关闸门
        arb.stop_all()
        ch.set_allow_move(False)
        self.assertFalse(sync.active or preset.playing or quick.is_orgasm or quick.is_slow)
        n = len(ch.calls)
        await asyncio.sleep(0.6)                      # 急停期间零帧
        self.assertEqual(len(ch.calls), n)
        ch.set_allow_move(True)                       # 解除：只开闸门
        await asyncio.sleep(0.6)
        self.assertEqual(len(ch.calls), n)            # 不自动恢复任何会话（I6）
        self.assertFalse(sync.active or preset.playing or quick.is_orgasm or quick.is_slow)


# ---------------------------------------------------------------- F5 / F1 / A8
class TestEstopGate(Base):
    async def test_f5_channel_gate_and_defense(self):
        ch, quick = self.make_world()[:2]
        # 闸门：急停中拒写（含 raw=True）；旧 force 参数已不存在
        ch.set_allow_move(False)
        self.assertFalse(await ch.move_to(50, 100))
        self.assertFalse(await ch.move_to(50, 100, raw=True))
        ch.set_allow_move(True)
        self.assertTrue(await ch.move_to(50, 100))
        # 爆发循环的纵深防御：会话在、闸门关 → 空转不写帧
        quick.start_orgasm()
        ch.set_allow_move(False)
        n = len(ch.calls)
        await asyncio.sleep(0.6)
        self.assertEqual(len(ch.calls), n)
        quick.stop_orgasm()

    async def test_a8_estop_toggle_path(self):
        ch, quick = self.make_world()[:2]
        # 路由修复后的等价逻辑：was 从通道状态读（旧 q.is_stop 属性不存在 → 500）
        quick.set_stop(True)
        self.assertFalse(ch.state.allow_move)
        self.assertTrue(quick.state()["stop"])
        was = not ch.state.allow_move
        quick.set_stop(False)
        self.assertTrue(was)
        self.assertTrue(ch.state.allow_move)
        self.assertFalse(quick.state()["stop"])


# ---------------------------------------------------------------- F6
class TestSessionBoundary(Base):
    async def test_f6_submit_timeout_cancels_coroutine(self):
        ch = FakeChannel()
        ch._loop = asyncio.get_running_loop()
        flag = {"cancelled": False}

        async def stubborn():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                flag["cancelled"] = True
                raise

        t0 = time.monotonic()
        # 模拟生产形态：HTTP 线程调 submit（跑在执行器里），不在循环线程上阻塞
        with self.assertRaises((asyncio.TimeoutError, concurrent.futures.TimeoutError)):
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: DeviceChannel.submit(ch, stubborn(), timeout=0.2))
        self.assertLess(time.monotonic() - t0, 2.0)
        await asyncio.sleep(0.2)
        self.assertTrue(flag["cancelled"], "超时后协程必须被取消（否则迟到帧照写）")

    async def test_f6_cleanup_resets_session_state(self):
        ch = DeviceChannel()
        ch.state.allow_move = False
        ch.state.recent = [1, 2, 3]
        ch.state.limit_min, ch.state.limit_speed = 10, 200
        ch._ready = True
        await ch._cleanup()          # 无 client 也能走完
        self.assertTrue(ch.state.allow_move)
        self.assertFalse(ch._ready)
        self.assertEqual(ch.state.recent, [])
        self.assertEqual(ch.state.limit_min, 0)
        self.assertEqual(ch.state.limit_speed, 0)
        ch._loop.call_soon_threadsafe(ch._loop.stop)   # 收掉构造函数起的常驻循环


if __name__ == "__main__":
    unittest.main()
