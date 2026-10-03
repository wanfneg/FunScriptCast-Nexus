"""快捷动作（逐条对应手机端 sync/QuickMoves.kt）。

- 一键急停：allow_move=False —— 脚本/爆发/缓动全部"待命不动作"。
- 一键爆发：在 min/max 之间以 maxSpeed 交替，每 (max-min)/maxSpeed 秒一次；
  用 raw 移动（尊重反转，跳过重映射/限幅）。
- 待机缓动：设备 idle_detect_seconds 没有动作后，才以 slowSpeed 在 min/max 间轻缓交替；
  外部动作（脚本/手动/爆发）会重置空闲计时并**立刻取消**正在跑的缓动循环。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass
class OrgasmSettings:
    min_percent: int = 0
    max_percent: int = 100
    max_speed: int = 500
    link_percent: bool = False
    link_speed: bool = False


@dataclass
class SlowSettings:
    min_percent: int = 0
    max_percent: int = 100
    max_speed: int = 100
    idle_detect_seconds: int = 5
    link_percent: bool = False


class QuickMoves:
    def __init__(self, channel, loop: asyncio.AbstractEventLoop) -> None:
        self.ch = channel
        self.loop = loop
        self.orgasm = OrgasmSettings()
        self.slow = SlowSettings()
        self.is_orgasm = False
        self.is_slow = False
        self._orgasm_task: asyncio.Task | None = None
        self._slow_task: asyncio.Task | None = None
        self._idle_task: asyncio.Task | None = None
        self._slow_index = 0
        self._gen_o = 0        # 爆发/缓动各自世代：停止后旧循环必死（同 preset_player 的理由）
        self._gen_s = 0
        self._last_external = 0.0
        # 设备每写一帧都会回调这里（含脚本帧）；缓动循环自己的帧不算"外部动作"
        self._self_moving = False
        self.ch.on_move = self._on_move

    # ---- 线程安全地往事件循环里塞协程 ----
    # 这些 start_*/stop_* 都是 HTTP 线程调用的；asyncio 的 loop.create_task()
    # **不能跨线程用**（会静默失败/污染循环）—— 之前"待机缓动""预设播放"点了没反应
    # 就是这个原因：任务压根没被创建。统一走 call_soon_threadsafe。
    def _spawn(self, coro):
        self.loop.call_soon_threadsafe(lambda: self.loop.create_task(coro))

    # ---- 外部动作记账（缓动让路） ----
    def _on_move(self, percent, speed) -> None:
        if self._self_moving:
            return
        if self.is_slow:
            # 手机端 onAnyMove → restartSlowIdle：外部动作（脚本/手动/预设）随时
            # 重置空闲倒计时，并掐掉正在跑的缓动循环——"缓动保持启用，等下一轮空闲"。
            self._restart_slow_idle()
        else:
            import time
            self._last_external = time.time()

    def note_external(self) -> None:
        import time
        self._last_external = time.time()

    def mark_idle_ok(self) -> None:
        """外部动作结束后，从"现在"重新计时。"""
        self.note_external()

    # ---- 急停 ----
    def set_stop(self, stopped: bool) -> dict:
        self.ch.set_allow_move(not stopped)   # 手机端：只切 allowMove，循环继续空转
        return self.state()

    # ---- 爆发 ----
    def start_orgasm(self) -> dict:
        if self.is_orgasm:
            return self.state()
        if self.is_slow:
            self.stop_slow()                      # 手机端：爆发/缓动互斥
        self.is_orgasm = True
        self._gen_o += 1
        self._spawn(self._orgasm_loop(self._gen_o))
        return self.state()

    def stop_orgasm(self) -> dict:
        self.is_orgasm = False
        self._gen_o += 1
        return self.state()

    def _orgasm_params(self) -> tuple[int, int, int]:
        """手机端 linkedOrgasm()：**每拍实时读**（含关联解析）——播放中改范围/速度
        立即生效；取消关联后用户自定义值不会被改写（apply() 不再回写）。"""
        s = self.orgasm
        lo = int(self.ch.range_lo) if s.link_percent else int(s.min_percent)
        hi = int(self.ch.range_hi) if s.link_percent else int(s.max_percent)
        speed = int(self.ch.max_speed) if s.link_speed else int(s.max_speed)
        return lo, hi, speed

    async def _orgasm_loop(self, gen: int) -> None:
        # 逐条对齐手机端 orgasmLoop：实时参数 / idx0→min 起步 /
        # 间隔 (max-min)*1000/speed ms 下限 50ms / 急停或速度为 0 时 1s 空转
        idx = 0
        try:
            while self.is_orgasm and gen == self._gen_o:
                lo, hi, speed = self._orgasm_params()
                if speed <= 0 or not self.ch.state.allow_move:
                    await asyncio.sleep(1.0)
                    continue
                target = lo if idx % 2 == 0 else hi
                idx += 1
                self._self_moving = True
                await self.ch.move_to(target, speed, raw=True)   # raw：不夹到速度上限
                self._self_moving = False
                await asyncio.sleep(max(0.05, (hi - lo) * 1000.0 / speed / 1000.0))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            # R125 修复（全项目审查中危）：循环死于意外异常时状态位必须复位——
            # 旧代码 is_orgasm 只在 start/stop 入口复位，循环静默死亡后爆发卡死
            # （脚本同步被永久 defer、UI 显示运动中而设备不动）。channel._write
            # 已兜住 BLE 断连，这里兜其余意外并如实上报。
            print(f"[quick] 爆发循环异常终止：{type(e).__name__}: {e}", flush=True)
            self.is_orgasm = False
        finally:
            self._self_moving = False

    # ---- 缓动 ----
    def start_slow(self) -> dict:
        if self.is_slow:
            return self.state()
        if self.is_orgasm:
            self.stop_orgasm()                    # 手机端：互斥
        self.is_slow = True
        self._slow_index = 0
        # 手机端 startSlow → restartSlowIdle()：从**点击时刻**重新计满空闲秒数，
        # 绝不因为时间戳陈旧而立刻开跑（旧版秒启的根因）。
        self._restart_slow_idle()
        return self.state()

    def stop_slow(self, reason: str = "") -> dict:
        self.is_slow = False
        self._gen_s += 1        # 空闲计时与缓动循环同时失效
        self._slow_task = None
        self._idle_task = None
        return self.state()

    def _restart_slow_idle(self) -> None:
        """手机端 restartSlowIdle：代际 +1 掐掉旧循环/旧计时，倒计时从现在重新计满。"""
        self._gen_s += 1
        self._slow_task = None
        self.note_external()
        self._spawn(self._idle_watch(self._gen_s))

    async def _idle_watch(self, gen: int) -> None:
        import time
        try:
            while self.is_slow and gen == self._gen_s:
                await asyncio.sleep(0.25)
                if not self.ch.state.allow_move:
                    continue
                # 每轮都重读设置：等待期间改"空闲判定秒数"立即生效
                # （手机端 setSlowSettings：还在等空闲窗口就按新秒数重新计时）
                secs = max(1, int(self.slow.idle_detect_seconds))
                if time.time() - self._last_external >= secs and self._slow_task is None:
                    self._slow_task = True
                    self.loop.create_task(self._slow_loop(gen))   # 已在循环线程内
        except asyncio.CancelledError:
            pass

    def _slow_params(self) -> tuple[int, int, int]:
        """手机端 linkedSlow()：每拍实时读（缓动只有范围关联，没有速度关联）。"""
        s = self.slow
        lo = int(self.ch.range_lo) if s.link_percent else int(s.min_percent)
        hi = int(self.ch.range_hi) if s.link_percent else int(s.max_percent)
        return lo, hi, int(s.max_speed)

    async def _slow_loop(self, gen: int) -> None:
        # 逐条对齐手机端 slowLoop：实时参数 / idx0→min / 间隔下限 100ms /
        # 急停或速度为 0 时 1s 空转
        idx = 0
        try:
            while self.is_slow and gen == self._gen_s:
                lo, hi, speed = self._slow_params()
                if speed <= 0 or not self.ch.state.allow_move:
                    await asyncio.sleep(1.0)
                    continue
                target = lo if idx % 2 == 0 else hi
                idx += 1
                self._self_moving = True
                await self.ch.move_to(target, speed, raw=True)   # raw：不夹到速度上限
                self._self_moving = False
                await asyncio.sleep(max(0.1, (hi - lo) * 1000.0 / speed / 1000.0))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            # R125 修复：同 _orgasm_loop——循环意外死亡必须复位状态位（缓动卡死）。
            print(f"[quick] 缓动循环异常终止：{type(e).__name__}: {e}", flush=True)
            self.is_slow = False
        finally:
            self._self_moving = False

    # ---- 播放器联动（手机端 pauseForPlayer / resumeForPlayer）----
    def pause_for_player(self) -> dict:
        if self.is_orgasm:
            self._orgasm_resume = True
            self.stop_orgasm()
        if self.is_slow:
            self._slow_resume = True
            self.stop_slow()
        return self.state()

    def take_resume_flags(self) -> tuple[bool, bool]:
        """取走"暂停前在跑"的旗标（是否爆发、是否缓动）并清空——供宿主经仲裁恢复，
        避免 resume 直连 start_* 绕过单一写者（脚本写帧没让路 → 两个写者抢设备）。"""
        o = bool(getattr(self, "_orgasm_resume", False))
        s = bool(getattr(self, "_slow_resume", False))
        self._orgasm_resume = False
        self._slow_resume = False
        return o, s

    def discard_resume(self) -> dict:
        """丢弃暂停恢复旗标：预设接管设备时调用，否则视频再播放会把爆发/缓动
        拉起来与预设抢设备（§三#7：保留自动恢复，但预设播放中不让位外的都恢复）。"""
        self._orgasm_resume = False
        self._slow_resume = False
        return self.state()

    # ---- 设置 ----
    def apply(self, orgasm: dict | None, slow: dict | None) -> None:
        if orgasm:
            for k, v in orgasm.items():
                if hasattr(self.orgasm, k) and v is not None:
                    setattr(self.orgasm, k, int(v) if isinstance(getattr(self.orgasm, k), int) else bool(v))
        if slow:
            for k, v in slow.items():
                if hasattr(self.slow, k) and v is not None:
                    setattr(self.slow, k, int(v) if isinstance(getattr(self.slow, k), int) else bool(v))
        # 关联（手机端 linkedOrgasm/linkedSlow）由 _orgasm_params/_slow_params **每拍实时解析**。
        # 旧版在这里把主范围/上限写回设置对象——会毁掉用户自定义值（取消关联后拿回的是被覆盖的数）。

    def state(self) -> dict:
        return {
            "slow": self.is_slow, "orgasm": self.is_orgasm, "stop": not self.ch.state.allow_move,
            "orgasm_settings": self.orgasm.__dict__, "slow_settings": self.slow.__dict__,
        }
