"""模拟播放时钟 —— 对齐 SyncEngine 的本地时钟语义（简化版）。

真实集成时这个类被替换成"跟随播放器上报"的完整实现（monotonic 推进 +
播放器 currentTime 渐进校正/吸附）；原型里用键盘模拟 play/pause/seek。
"""
from __future__ import annotations

import time


class PlaybackClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.playing = False
        self.rate = 1.0
        self._last: float | None = None

    def play(self) -> None:
        self.playing = True
        self._last = time.monotonic()

    def pause(self) -> None:
        self.tick()
        self.playing = False
        self._last = None

    def toggle(self) -> None:
        self.pause() if self.playing else self.play()

    def seek(self, delta_sec: float) -> None:
        self.tick()
        self.t = max(0.0, self.t + delta_sec)

    def set(self, seconds: float) -> None:
        self.t = max(0.0, seconds)

    def tick(self) -> float:
        now = time.monotonic()
        if self.playing and self._last is not None:
            self.t += (now - self._last) * self.rate
        self._last = now
        return self.t
