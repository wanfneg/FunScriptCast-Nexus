"""预设播放引擎（对应手机端 sync/PresetPlayer.kt）。

- 与脚本同步相互独立；设备侧已应用行程范围/速度上限/反转，这里只算波形位置与斜率速度。
- RANDOM：未播放时仅高亮；播放中每次循环结束随机跳下一个（排除当前）。
- BOOST：保持当前预设，速度覆盖 500；没选预设时点播放兜底 Normal。
- 实际等待 = 自然时长 × (100 / 实际速度)。
"""
from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path

BOOST_SPEED = 500
DEFAULT_PRESET = "normal"


def load_presets(path: Path) -> list[dict]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return []


def preset_points(pr: dict) -> list[tuple[float, float]]:
    """预设 → [(t_ms, pos)]，自然速度 100 下的时间轴。"""
    pts: list[tuple[float, float]] = []
    kfs = pr.get("keyframes") or []
    segs = pr.get("segments") or []
    if kfs:
        for kf in kfs:
            at, pos = float(kf[0]), float(kf[1])
            pts.append((at, pos))
        return pts
    if not segs:
        return pts
    t = 0.0
    pos = float(segs[0][0])
    pts.append((0.0, pos))
    for sg in segs:
        start, end, speed = float(sg[0]), float(sg[1]), float(sg[2] or 100)
        speed = speed if speed > 0 else 100.0
        dt = abs(end - start) * 1000.0 / speed
        t += dt
        pos = end
        pts.append((t, pos))
    return pts


class PresetPlayer:
    def __init__(self, channel, loop: asyncio.AbstractEventLoop, presets_path: Path) -> None:
        self.ch = channel
        self.loop = loop
        self.presets = load_presets(presets_path)
        self.selected: str | None = None
        self.playing = False
        self.random_mode = False
        self.boost = False
        self.speed = 100
        self._task: asyncio.Task | None = None
        self._self_moving = False

    def _spawn(self, coro):
        """HTTP 线程 → 事件循环（loop.create_task 跨线程不安全，见 quick_moves 注释）。"""
        self.loop.call_soon_threadsafe(lambda: self.loop.create_task(coro))

    def by_id(self, pid: str | None) -> dict | None:
        if not pid:
            return None
        for pr in self.presets:
            if pr.get("id") == pid:
                return pr
        return None

    def select(self, pid: str | None) -> dict:
        if pid and not self.by_id(pid):
            return {"ok": False, "error": "未知预设"}
        self.selected = pid
        if pid:
            self.random_mode = False      # 手机端：点选网格预设 = 退出随机
            if self.playing:              # 播放中点选 → 立即切到新预设（手机端同款）
                self._restart()
        return {"ok": True, "state": self.state()}

    def _restart(self) -> None:
        if self._task is not None:
            try:
                self.loop.call_soon_threadsafe(self._cancel_task)
            except Exception:
                pass
        self._task = True
        self._spawn(self._run())

    def _cancel_task(self) -> None:
        t = self._task
        if hasattr(t, "cancel"):
            t.cancel()

    def toggle_random(self) -> dict:
        self.random_mode = not self.random_mode
        return {"ok": True, "state": self.state()}

    def toggle_boost(self) -> dict:
        self.boost = not self.boost
        return {"ok": True, "state": self.state()}

    def set_speed(self, v: int) -> dict:
        self.speed = max(1, min(500, int(v)))
        return {"ok": True, "state": self.state()}

    def play(self) -> dict:
        if self.playing:
            return {"ok": True, "state": self.state()}
        if not self.selected:
            self.selected = DEFAULT_PRESET     # 没选预设 → 兜底 Normal（BOOST 后点播放的场景）
        if not self.by_id(self.selected):
            return {"ok": False, "error": "没有可用预设"}
        self.playing = True
        self._task = True
        self._spawn(self._run())
        return {"ok": True, "state": self.state()}

    def stop(self) -> dict:
        self.playing = False
        if self._task:
            self._task.cancel()
            self._task = None
        return {"ok": True, "state": self.state()}

    async def _run(self) -> None:
        try:
            while self.playing:
                pr = self.by_id(self.selected)
                if not pr:
                    break
                pts = preset_points(pr)
                if len(pts) < 2:
                    break
                for i in range(1, len(pts)):
                    if not self.playing:
                        break
                    t_prev, _ = pts[i - 1]
                    t_cur, pos = pts[i]
                    natural = max(0.0, t_cur - t_prev) / 1000.0
                    speed = BOOST_SPEED if self.boost else self.speed
                    wait = natural * (100.0 / max(1, speed))
                    await asyncio.sleep(max(0.01, wait))
                    if not self.playing:
                        break
                    self._self_moving = True
                    await self.ch.move_to(pos, speed, force=True, raw=True)
                    self._self_moving = False
                if self.random_mode and self.playing:
                    others = [p["id"] for p in self.presets if p.get("id") != self.selected]
                    if others:
                        self.selected = random.choice(others)
        except asyncio.CancelledError:
            pass
        finally:
            self._self_moving = False

    def state(self) -> dict:
        return {
            "playing": self.playing, "selected": self.selected, "random": self.random_mode,
            "boost": self.boost, "speed": self.speed, "count": len(self.presets),
        }
