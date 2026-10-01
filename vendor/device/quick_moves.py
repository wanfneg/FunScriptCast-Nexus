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
        import time
        self._last_external = time.time()
        if self.is_slow:
            # 手机端 onAnyMove()：cancel 正在跑的缓动循环并重新计时，**缓动保持启用**
            self._gen_s += 1
            self._slow_task = None

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

    async def _orgasm_loop(self, gen: int) -> None:
        s = self.orgasm
        lo, hi = int(s.min_percent), int(s.max_percent)
        span = max(1, abs(hi - lo))
        speed = max(1, int(s.max_speed))
        interval = span / float(speed)          # 手机端：每 (max-min)/maxSpeed 秒一次
        pos = lo
        try:
            while self.is_orgasm and gen == self._gen_o:
                if not self.ch.state.allow_move or speed <= 0:
                    await asyncio.sleep(1.0)      # 手机端：急停期间 delay(1000) 空转
                    continue
                pos = hi if pos == lo else lo
                self._self_moving = True
                await self.ch.move_to(pos, speed, raw=True)
                self._self_moving = False
                await asyncio.sleep(max(0.05, interval))   # 手机端下限 50ms
        except asyncio.CancelledError:
            pass
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
        self._gen_s += 1
        self._spawn(self._idle_watch(self._gen_s))
        return self.state()

    def stop_slow(self, reason: str = "") -> dict:
        self.is_slow = False
        self._gen_s += 1        # 空闲计时与缓动循环同时失效
        self._slow_task = None
        self._idle_task = None
        return self.state()

    async def _idle_watch(self, gen: int) -> None:
        import time
        secs = max(1, int(self.slow.idle_detect_seconds))
        if self._last_external == 0.0:
            self._last_external = time.time()
        try:
            while self.is_slow and gen == self._gen_s:
                await asyncio.sleep(0.25)
                if not self.ch.state.allow_move:
                    continue
                if time.time() - self._last_external >= secs and self._slow_task is None:
                    self._slow_task = True
                    self.loop.create_task(self._slow_loop(gen))   # 已在循环线程内
        except asyncio.CancelledError:
            pass

    async def _slow_loop(self, gen: int) -> None:
        s = self.slow
        lo, hi = int(s.min_percent), int(s.max_percent)
        speed = max(1, int(s.max_speed))
        try:
            while self.is_slow and gen == self._gen_s:
                self._slow_index += 1
                target = lo if self._slow_index % 2 else hi
                if not self.ch.state.allow_move:
                    # 纵深防御（急停正常路径已停会话）：缓动空转不写帧，等闸门开
                    await asyncio.sleep(0.25)
                    continue
                self._self_moving = True
                await self.ch.move_to(target, speed, raw=True)
                self._self_moving = False
                # 手机端 QuickMoves.kt:340：间隔 = 行程×1000/速度 ms，下限 100ms。
                # 旧版写死 1.0s——默认值下恰好也是 1s，"运动速度"滑轨成了装饰。
                interval_ms = abs(hi - lo) * 1000.0 / speed
                await asyncio.sleep(max(0.1, interval_ms / 1000.0))
        except asyncio.CancelledError:
            pass
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

    def resume_for_player(self) -> dict:
        if getattr(self, "_orgasm_resume", False):
            self._orgasm_resume = False
            self.start_orgasm()
        if getattr(self, "_slow_resume", False):
            self._slow_resume = False
            self.start_slow()
        return self.state()

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
        # 关联主行程/主限速（手机端 rangeProvider / maxSpeedProvider）
        if self.orgasm.link_percent:
            self.orgasm.min_percent, self.orgasm.max_percent = int(self.ch.range_lo), int(self.ch.range_hi)
        if self.orgasm.link_speed:
            self.orgasm.max_speed = int(self.ch.max_speed)
        if self.slow.link_percent:
            self.slow.min_percent, self.slow.max_percent = int(self.ch.range_lo), int(self.ch.range_hi)

    def state(self) -> dict:
        return {
            "slow": self.is_slow, "orgasm": self.is_orgasm, "stop": not self.ch.state.allow_move,
            "orgasm_settings": self.orgasm.__dict__, "slow_settings": self.slow.__dict__,
        }
