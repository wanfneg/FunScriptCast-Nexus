"""脚本同步（对应手机端 sync/SyncEngine.kt 的核心职责）。

手机端是自己跑循环、从播放器拿进度；PC 侧播放器在 WebView 里，所以由界面把
当前进度 tick 过来（内置播放器 5Hz；mpv 由宿主的 1s 轮询带）。这里只做：
  进度 → 在 funscript 动作点之间插值出 (位置, 斜率速度) → 下发设备。
让路规则与手机一致：设备侧统一处理行程范围/速度上限/反转；allow_move=false（急停）时不发。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path


class SyncEngine:
    def __init__(self, channel, loop: asyncio.AbstractEventLoop) -> None:
        self.ch = channel
        self.loop = loop
        self.active = False
        self.path = ""
        self.actions: list[list[float]] = []
        self.delay_ms = 0
        self.skip_idle = True
        self.idle_threshold = 3.0     # 动作点间隔超过它就算"空闲段"，不推设备
        self.sent = 0
        self.skipped = 0
        self._last_pos: int | None = None

    # ---- 脚本 ----
    def resolve_script(self, video_path: str) -> Path | None:
        """视频同目录同名的 .funscript（大小写不敏感）。"""
        if not video_path:
            return None
        p = Path(video_path)
        for cand in (p.with_suffix(".funscript"), p.with_suffix(".FunScript")):
            if cand.is_file():
                return cand
        try:
            stem = p.stem.lower()
            for f in p.parent.iterdir():
                if f.is_file() and f.suffix.lower() == ".funscript" and f.stem.lower() == stem:
                    return f
        except Exception:
            pass
        return None

    def load(self, video_path: str) -> tuple[bool, str]:
        sp = self.resolve_script(video_path)
        if sp is None:
            return False, "这个视频没有配套脚本"
        try:
            data = json.loads(sp.read_text(encoding="utf-8", errors="ignore"))
            acts = [[float(a["at"]), float(a["pos"])] for a in data.get("actions", [])]
            acts.sort(key=lambda a: a[0])
        except Exception as e:
            return False, f"脚本解析失败：{e}"
        if len(acts) < 2:
            return False, "脚本动作点不足"
        self.actions = acts
        self.path = str(sp)
        self._last_pos = None
        return True, ""

    def start(self, video_path: str) -> dict:
        ok, err = self.load(video_path)
        if not ok:
            self.active = False
            return {"ok": False, "error": err}
        self.active = True
        self.sent = 0
        self.skipped = 0
        return {"ok": True, "script": self.path, "actions": len(self.actions)}

    def stop(self) -> dict:
        self.active = False
        return {"ok": True}

    # ---- 进度驱动 ----
    def tick(self, t_sec: float) -> None:
        """HTTP 线程调用：投递到 BLE 事件循环，不阻塞请求。"""
        if not self.active:
            return
        asyncio.run_coroutine_threadsafe(self._apply(float(t_sec)), self.loop)

    async def _apply(self, t_sec: float) -> None:
        if not self.active or not self.actions:
            return
        if not self.ch.state.connected or not self.ch.state.allow_move:
            return
        ms = t_sec * 1000.0 - self.delay_ms
        acts = self.actions
        if ms <= acts[0][0]:
            target, speed = acts[0][1], 0.0
        elif ms >= acts[-1][0]:
            target, speed = acts[-1][1], 0.0
        else:
            lo, hi = 0, len(acts) - 1
            while hi - lo > 1:
                mid = (lo + hi) // 2
                if acts[mid][0] <= ms:
                    lo = mid
                else:
                    hi = mid
            a, b = acts[lo], acts[hi]
            span = max(1.0, b[0] - a[0])
            slope = abs(b[1] - a[1]) / (span / 1000.0)
            # 手机端 rebuildIdleGaps：斜率 < 0.01 视为"无动作段"；skipIdle 开启且段长超阈值就跳过
            if self.skip_idle and slope < 0.01 and span / 1000.0 >= self.idle_threshold:
                self.skipped += 1
                return                      # 空闲段：设备不动（手机端 skip idle 同义）
            k = (ms - a[0]) / span
            target = a[1] + (b[1] - a[1]) * k
            speed = abs(b[1] - a[1]) / (span / 1000.0)     # %/秒（与热力图同口径）
        pos = int(max(0, min(100, round(target))))
        if self._last_pos is not None and abs(pos - self._last_pos) < 1:
            return                          # 位置没变就别刷 BLE
        self._last_pos = pos
        try:
            await self.ch.move_to(target, int(min(speed, self.ch.max_speed)) or None, force=True)
            self.sent += 1
        except Exception:
            pass

    def state(self) -> dict:
        return {"active": self.active, "script": Path(self.path).name if self.path else "",
                "actions": len(self.actions), "sent": self.sent, "skipped": self.skipped,
                "delay_ms": self.delay_ms, "skip_idle": self.skip_idle,
                "idle_threshold": self.idle_threshold}
