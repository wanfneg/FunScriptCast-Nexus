"""媒体时钟 —— 逐行移植 SyncEngine 的"本地时钟跟随播放器上报"语义。

- 播放中：monotonic 推进 clock（×倍速），被播放器 currentTime 锚定：
  偏差 > 0.5s 直接吸附（seek），否则 10% 渐进校正（消除漂移）。
- 暂停中：时钟停走，有上报则直接对齐。
"""
from __future__ import annotations

import time

SNAP_THRESHOLD_SEC = 0.5
REPORT_CORRECTION = 0.1


class MediaClock:
    def __init__(self) -> None:
        self.clock = 0.0
        self._last_tick: float | None = None

    def tick(self, playing: bool, rate: float = 1.0) -> float:
        now = time.monotonic()
        if self._last_tick is not None:
            dt = now - self._last_tick
            if playing:
                self.clock += dt * (rate if rate > 0 else 1.0)
        self._last_tick = now
        return self.clock

    def on_report(self, current_time: float, playing: bool) -> None:
        """播放器 currentTime 上报锚定（对齐 SyncEngine.loop 的时钟段）。"""
        if playing:
            if abs(current_time - self.clock) > SNAP_THRESHOLD_SEC:
                self.clock = current_time
            else:
                self.clock += (current_time - self.clock) * REPORT_CORRECTION
        else:
            self.clock = current_time
        self.clock = max(0.0, self.clock)

    def nudge(self, seconds: float) -> None:
        """seek 后强制对齐（等于 nudgeClock）。"""
        self.clock = max(0.0, seconds)
        self._last_tick = None
