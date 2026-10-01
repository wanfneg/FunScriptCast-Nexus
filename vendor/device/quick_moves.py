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
        self._last_external = 0.0
        # 设备每写一帧都会回调这里（含脚本帧）；缓动循环自己的帧不算"外部动作"
        self._self_moving = False
        self.ch.on_move = self._on_move

    # ---- 外部动作记账（缓动让路） ----
    def _on_move(self, percent, speed) -> None:
        if self._self_moving:
            return
        import time
        self._last_external = time.time()
        if self.is_slow:
            self.stop_slow(reason="外部动作接管")

    def note_external(self) -> None:
        import time
        self._last_external = time.time()

    def mark_idle_ok(self) -> None:
        """外部动作结束后，从"现在"重新计时。"""
        self.note_external()

    # ---- 急停 ----
    def set_stop(self, stopped: bool) -> dict:
        self.ch.set_allow_move(not stopped)
        if stopped:
            self.stop_orgasm()
            self.stop_slow()
        return self.state()

    # ---- 爆发 ----
    def start_orgasm(self) -> dict:
        if self.is_orgasm:
            return self.state()
        self.is_orgasm = True
        self._orgasm_task = self.loop.create_task(self._orgasm_loop())
        return self.state()

    def stop_orgasm(self) -> dict:
        self.is_orgasm = False
        if self._orgasm_task:
            self._orgasm_task.cancel()
            self._orgasm_task = None
        return self.state()

    async def _orgasm_loop(self) -> None:
        s = self.orgasm
        lo, hi = int(s.min_percent), int(s.max_percent)
        span = max(1, abs(hi - lo))
        speed = max(1, int(s.max_speed))
        interval = span / float(speed)          # 手机端：每 (max-min)/maxSpeed 秒一次
        pos = lo
        try:
            while self.is_orgasm:
                pos = hi if pos == lo else lo
                self._self_moving = True
                await self.ch.move_to(pos, speed, force=True, raw=True)
                self._self_moving = False
                await asyncio.sleep(max(0.02, interval))
        except asyncio.CancelledError:
            pass
        finally:
            self._self_moving = False

    # ---- 缓动 ----
    def start_slow(self) -> dict:
        if self.is_slow:
            return self.state()
        self.is_slow = True
        self._slow_index = 0
        self._idle_task = self.loop.create_task(self._idle_watch())
        return self.state()

    def stop_slow(self, reason: str = "") -> dict:
        self.is_slow = False
        for t in (self._slow_task, self._idle_task):
            if t:
                t.cancel()
        self._slow_task = None
        self._idle_task = None
        return self.state()

    async def _idle_watch(self) -> None:
        import time
        secs = max(1, int(self.slow.idle_detect_seconds))
        if self._last_external == 0.0:
            self._last_external = time.time()
        try:
            while self.is_slow:
                await asyncio.sleep(0.25)
                if not self.ch.state.allow_move:
                    continue
                if time.time() - self._last_external >= secs and self._slow_task is None:
                    self._slow_task = self.loop.create_task(self._slow_loop())
        except asyncio.CancelledError:
            pass

    async def _slow_loop(self) -> None:
        s = self.slow
        lo, hi = int(s.min_percent), int(s.max_percent)
        speed = max(1, int(s.max_speed))
        try:
            while self.is_slow:
                self._slow_index += 1
                target = lo if self._slow_index % 2 else hi
                self._self_moving = True
                await self.ch.move_to(target, speed, force=True, raw=True)
                self._self_moving = False
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass
        finally:
            self._self_moving = False

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
