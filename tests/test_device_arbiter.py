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
        self.calls: list[tuple[str, float, int | None]] = []   # (owner, percent, speed)
        self.owner = "test"
        self.on_move = None

    async def move_to(self, percent, speed=None, raw=False, *, bypass_estop=False):
        if not self.state.allow_move and not bypass_estop:
            return False
        self.calls.append((self.owner, float(percent), None if speed is None else int(speed)))
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


# ---------------------------------------------------------------- 第 2 批：F14 / F16 / F17
class TestBatch2(Base):
    GAP_SCRIPT = {"actions": [{"at": 0, "pos": 10}, {"at": 1000, "pos": 90}, {"at": 2000, "pos": 10},
                              {"at": 92000, "pos": 10}, {"at": 94000, "pos": 90}]}

    def _load_gap_script(self, sync):
        (self.dir / "video.funscript").write_text(json.dumps(self.GAP_SCRIPT), encoding="utf-8")
        ok, err = sync.load(str(self.dir / "video.mp4"))
        assert ok, err
        sync.active = True

    async def test_f16_no_frames_outside_action_range(self):
        """播放头在首/末动作点之外：一帧不发（旧版在此发 max_speed 满速帧）。"""
        ch, quick, preset, sync, arb = self.make_world()
        self._load_gap_script(sync)
        n0 = len(ch.calls)
        await sync._apply(-1.0)                 # 首动作点之前
        self.assertEqual(len(ch.calls), n0)
        await sync._apply(95.0)                 # 末动作点之后
        self.assertEqual(len(ch.calls), n0)
        await sync._apply(0.5)                  # 区间内正常发
        self.assertEqual(len(ch.calls), n0 + 1)

    async def test_slow_segment_speed_zero_not_max(self):
        """斜率<1%/s 的慢段：发 speed=0（设备自己爬），不是 None→max_speed 冲刺。
        手机端 moveTo: clamp(scaledSpeed, 0, maxSpeed)；旧写法 `int(…) or None`
        把 0 变 None → 通道取 max_speed——脚本慢段全变满速冲刺（用户报"动作异常"）。"""
        ch, quick, preset, sync, arb = self.make_world()
        (self.dir / "video.funscript").write_text(json.dumps({"actions": [
            {"at": 0, "pos": 10}, {"at": 30000, "pos": 15}      # 5%/30s ≈ 0.17 %/s
        ]}), encoding="utf-8")
        ok, err = sync.load(str(self.dir / "video.mp4"))
        self.assertTrue(ok, err)
        sync.active = True
        await sync._apply(10.0)
        self.assertEqual(len(ch.calls), 1)
        self.assertIsNotNone(ch.calls[0][2], "慢段速度不得为 None（None=取 max_speed）")
        self.assertEqual(ch.calls[0][2], 0, "斜率<1%/s 应取整为 0（对齐手机端 clamp 下限）")

    async def test_f17_skip_idle_defaults_and_seek_semantics(self):
        ch, quick, preset, sync, arb = self.make_world()
        # 默认对齐手机端：关 / 60s
        self.assertFalse(sync.skip_idle)
        self.assertEqual(sync.idle_threshold, 60.0)
        self._load_gap_script(sync)
        n0 = len(ch.calls)
        await sync._apply(0.5)                  # 正常段：发帧
        self.assertEqual(len(ch.calls), n0 + 1)
        await sync._apply(30.0)                 # 平段（默认关）：定位一次后 dedup 静默，不 seek
        await sync._apply(40.0)
        self.assertEqual(len(ch.calls), n0 + 2)
        self.assertIsNone(sync.pop_seek())
        sync.skip_idle = True                   # 开启（用户在设置页打开才生效）
        sync._last_pos = None
        n1 = len(ch.calls)
        await sync._apply(30.0)
        self.assertEqual(len(ch.calls), n1)     # 静止段一帧不发
        sk = sync.pop_seek()
        self.assertIsNotNone(sk)
        self.assertAlmostEqual(sk, 92.0, delta=0.01)   # 快进到下一动作点
        self.assertIsNone(sync.pop_seek())      # 取走即清，防 seek 环

    async def test_f14_slow_interval_follows_speed(self):
        """缓动间隔 = 行程×1000/速度（下限 100ms），不再是写死 1s。
        行程 20、速度 100 → 200ms/拍：0.75s 内应至少 3 拍（旧版只可能 1 拍）。"""
        ch, quick = self.make_world()[:2]
        quick.slow.min_percent, quick.slow.max_percent = 0, 20
        quick.slow.max_speed = 100
        quick.is_slow = True
        quick._gen_s += 1
        task = asyncio.get_running_loop().create_task(quick._slow_loop(quick._gen_s))
        await asyncio.sleep(0.75)
        quick.stop_slow()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        self.assertGreaterEqual(len(ch.calls), 3, "缓动速度滑轨没生效（仍是 1s/拍？）")

    async def test_slow_starts_only_after_full_idle_countdown(self):
        """点击待机缓动后必须计满空闲秒数才开跑（手机端 startSlow→restartSlowIdle）。
        旧版用陈旧的 _last_external 时间戳，点击瞬间就开跑（用户报"点击立马开始"）。"""
        ch, quick = self.make_world()[:2]
        ch.calls.clear()
        quick.slow.idle_detect_seconds = 1
        quick._last_external = time.time() - 999     # 陈旧时间戳：旧版会立刻开跑
        quick.start_slow()
        await asyncio.sleep(0.5)
        self.assertEqual(len(ch.calls), 0, "缓动在空闲秒数未满时就开始了")
        await asyncio.sleep(0.9)                     # 计满 1s 后应开跑
        self.assertGreaterEqual(len(ch.calls), 1)
        quick.stop_slow()

    async def test_external_move_restarts_slow_countdown(self):
        """缓动跑着时来外部动作：掐掉循环、重新计满（手机端 onAnyMove→restartSlowIdle）。"""
        ch, quick = self.make_world()[:2]
        ch.calls.clear()
        quick.slow.idle_detect_seconds = 1
        quick.start_slow()
        await asyncio.sleep(1.3)                     # 第一轮已开跑
        self.assertGreaterEqual(len(ch.calls), 1)
        n = len(ch.calls)
        quick._self_moving = False
        quick._on_move(50, 100)                      # 外部动作（模拟脚本帧）
        await asyncio.sleep(0.6)                     # 新倒计时未满：不得有新帧
        self.assertEqual(len(ch.calls), n, "外部动作后缓动没有重新等待空闲")
        await asyncio.sleep(0.9)                     # 计满后恢复
        self.assertGreater(len(ch.calls), n)
        quick.stop_slow()


class TestBatch3(unittest.TestCase):
    def test_f24_clamp_video_link(self):
        """range_min>range_max 曾让全部动作速度静默归零（二次确认 #1）。"""
        try:
            import host_server as hs
        except Exception as e:                      # 完整宿主环境之外的机器上跳过
            self.skipTest(f"host_server 需要完整运行环境：{e}")
        out = hs._clamp_video_link({"range_min": 90, "range_max": 10, "max_speed": 99999})
        self.assertEqual(out["range_min"], 10)      # 顺序被纠正
        self.assertEqual(out["range_max"], 90)
        self.assertEqual(out["max_speed"], 500)     # 夹紧到 0-500
        out2 = hs._clamp_video_link({"idle_speed": "abc", "burst_min": 120})
        self.assertNotIn("idle_speed", out2)        # 乱值删键回退默认，不留雷
        self.assertEqual(out2["burst_min"], 100)
        out3 = hs._clamp_video_link({"theme": "x"})   # 无关键原样保留
        self.assertEqual(out3, {"theme": "x"})


# ---------------------------------------------------------------- 第 6 批：爆发/缓动引擎对齐手机端
class TestQuickMovesEngine(Base):
    async def test_raw_move_not_clamped_to_device_limit(self):
        """爆发/缓动帧速度**不得**按设备速度上限夹紧（手机端 forceMoveToInverted：
        no remap/clamp）。夹紧后设备比间隔假设的慢 → 行程被截短且随抖动漂移（飘忽）。
        脚本路径（raw=False）照旧夹紧——那是手机端 forceMoveTo 就有的语义。"""
        from device.channel import DeviceChannel
        from device.protocols import convert_speed
        ch = DeviceChannel()
        try:
            ch._ready = True
            ch.max_speed = 350                    # 实机值：上限低于爆发速度 500
            ch.range_lo, ch.range_hi = 0.0, 100.0
            sent = []

            async def fake_write(payload):
                sent.append(payload)
                return True

            ch._write = fake_write
            await ch.move_to(100, 500, raw=True)
            self.assertEqual(sent[-1][2], convert_speed(500),
                             "raw 帧速度被夹到上限了（爆发行程会被截短）")
            await ch.move_to(100, 500)
            self.assertEqual(sent[-1][2], convert_speed(350),
                             "脚本路径应夹紧到设备速度上限")
        finally:
            ch._loop.call_soon_threadsafe(ch._loop.stop)

    async def test_orgasm_cadence_speed_consistent(self):
        """帧速度与间隔同源（都是滑块 500，不再一个 500 一个 350）；
        首帧=lo、两目标交替（对齐手机端 idx0→min）。"""
        ch, quick = self.make_world()[:2]
        ch.max_speed = 350                        # 上限低于爆发速度
        quick.orgasm.max_speed = 500
        ch.calls.clear()
        quick.start_orgasm()
        await asyncio.sleep(0.55)
        quick.stop_orgasm()
        self.assertGreaterEqual(len(ch.calls), 2)
        self.assertEqual(ch.calls[0][1], 0.0, "首帧应为 lo（手机端 idx0→min）")
        self.assertEqual({c[1] for c in ch.calls[:2]}, {0.0, 100.0})
        for _, _, sp in ch.calls:
            self.assertEqual(sp, 500, "帧速度与间隔必须同源（被夹紧→行程截短飘忽）")

    async def test_orgasm_params_live_and_links(self):
        """linkedOrgasm 语义：关联勾选实时解析（范围/速度上限），取消后回自定义值。"""
        ch, quick = self.make_world()[:2]
        quick.orgasm.link_percent = True
        quick.orgasm.link_speed = True
        ch.range_lo, ch.range_hi = 20.0, 80.0
        ch.max_speed = 350
        self.assertEqual(quick._orgasm_params(), (20, 80, 350))
        quick.orgasm.link_percent = False
        quick.orgasm.link_speed = False
        self.assertEqual(quick._orgasm_params(), (0, 100, 500))

    async def test_script_defers_while_orgasm_runs(self):
        """手机端 setExternalControl：爆发活动期间脚本同步**让路**——不掐爆发、
        不抢设备；爆发停止后脚本才能起来。'加载脚本把爆发顶掉'是方向反了。"""
        ch, quick, preset, sync, arb = self.make_world()
        vp = str(self.dir / "video.mp4")
        arb.start_orgasm()
        res = arb.start_script(vp)
        self.assertTrue(res.get("ok") and res.get("deferred"))
        self.assertTrue(quick.is_orgasm, "脚本启动把爆发掐掉了")
        self.assertFalse(sync.active, "脚本在爆发期间抢了设备")
        quick.stop_orgasm()
        res = arb.start_script(vp)
        self.assertTrue(res["ok"] and not res.get("deferred"))
        self.assertTrue(sync.active)


from contextlib import suppress  # noqa: E402

if __name__ == "__main__":
    unittest.main()
