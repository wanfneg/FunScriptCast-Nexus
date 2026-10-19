"""Funscript 解析与同步计算 —— 逐行对齐 FunScriptCast 的 Funscript.kt。

同步数学（与 Kotlin 端一致）:
    t = mediaTime + offset
    n = search_for_index_after(t)
    speed = |value[n] - value[n-1]| * 100 / (position[n] - position[n-1])
    move_to(kround(value[n] * 100), kround(speed * playbackRate))
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass


def kround(x: float) -> int:
    """Java Math.round 语义（.5 恒向上），本场景输入恒非负。"""
    return math.floor(x + 0.5)


@dataclass(frozen=True)
class Keyframe:
    position: float  # 秒
    value: float     # 0..1


class Funscript:
    def __init__(self, keyframes: list[Keyframe]):
        self._kfs = keyframes

    @property
    def count(self) -> int:
        return len(self._kfs)

    def keyframes(self) -> list[Keyframe]:
        return list(self._kfs)

    def search_for_index_after(self, t: float) -> int:
        """第一个 position > t 的下标（0 = 脚本未开始，count = 已结束）。"""
        kfs = self._kfs
        if not kfs or t < kfs[0].position:
            return 0
        if t >= kfs[-1].position:
            return len(kfs)
        lo, hi = 0, len(kfs) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            if kfs[mid].position <= t:
                lo = mid + 1
            else:
                hi = mid - 1
        return lo

    def index_after(self, t: float) -> int:
        return self.search_for_index_after(t)

    def compute_move(
        self, media_time_sec: float, offset_sec: float = 0.0, playback_rate: float = 1.0
    ) -> tuple[int, int] | None:
        """按段计算 (targetPercent 0..100, speed)。脚本范围外返回 None。"""
        kfs = self._kfs
        if not kfs:
            return None
        t = media_time_sec + offset_sec
        n = self.search_for_index_after(t)
        if n < 1 or n > len(kfs) - 1:
            return None
        r, i = kfs[n - 1], kfs[n]
        dt = i.position - r.position
        if dt <= 0:
            return None
        speed = abs(i.value - r.value) * 100 / dt
        return kround(i.value * 100), kround(speed * playback_rate)

    @classmethod
    def from_json(cls, text: str) -> "Funscript":
        actions = json.loads(text).get("actions", [])
        kfs: list[Keyframe] = []
        for a in actions:
            at_sec = a.get("at", float("nan")) / 1000.0
            raw = a.get("pos", float("nan"))
            # 对齐 Kotlin：`!(at >= 0)` 连 NaN 一起拦
            if not (at_sec >= 0) or math.isnan(raw):
                continue
            v = min(max(raw / 100.0, 0.0), 1.0)
            kfs.append(Keyframe(at_sec, v))
        kfs.sort(key=lambda k: k.position)  # 防御性排序，保留重复时间戳
        return cls(kfs)
