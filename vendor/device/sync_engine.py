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
        # 对齐手机端默认：skipIdleEnabled=false / 阈值 60s（AppViewModel.kt:896/899）。
        # 旧默认 True/3s 会让脚本一静止 3 秒设备就停、视频照播。
        self.skip_idle = False
        self.idle_threshold = 60.0
        self.sent = 0
        self.skipped = 0
        self._last_pos: int | None = None
        self._seek_to: float | None = None   # 「跳过无动作」判定出的视频快进目标（秒）

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

    def reset_last_index(self) -> None:
        """手机端 SyncEngine.resetLastIndex()：任何"急停 → 继续"都强制重发当前段目标帧，
        否则同一段内位置没变（<1% 不刷）会一直不发，设备停在半路。"""
        self._last_pos = None

    def stop(self) -> dict:
        self.active = False
        self._seek_to = None
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
        if ms <= acts[0][0] or ms >= acts[-1][0]:
            # 播放头在首/末动作点之外：一帧不发（对齐手机端 SyncEngine.kt:285-289）。
            # 旧实现此处 speed=0 → int(0) or None → 通道取 max_speed，变成一次满速冲刺。
            return
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
        # 手机端 rebuildIdleGaps：斜率 < 0.01 视为"无动作段"；skipIdle 开启且段长超阈值，
        # 语义跟手机端一致——**把视频快进过这段**（seek 到下一动作点），而不是只停设备
        if self.skip_idle and slope < 0.01 and span / 1000.0 >= self.idle_threshold:
            self.skipped += 1
            self._seek_to = (b[0] + self.delay_ms) / 1000.0
            return
        k = (ms - a[0]) / span
        target = a[1] + (b[1] - a[1]) * k
        speed = abs(b[1] - a[1]) / (span / 1000.0)     # %/秒（与热力图同口径）
        pos = int(max(0, min(100, round(target))))
        if self._last_pos is not None and abs(pos - self._last_pos) < 1:
            return                          # 位置没变就别刷 BLE
        self._last_pos = pos
        # 速度钳制对齐手机端（BleDeviceService.moveTo: clamp(scaled, 0, maxSpeed)）：
        # 慢段（斜率<1%/s）取整为 0 就发 0，让设备按自己的最低速爬行。
        # 旧写法 `int(…) or None` 把 0 变 None → 通道取 max_speed——脚本里的慢段/平段
        # 全变成满速冲刺，这就是"脚本模式设备动作异常"的根因。
        sp = max(0, min(int(round(speed)), int(self.ch.max_speed)))
        try:
            await self.ch.move_to(target, sp)
            self.sent += 1
        except Exception:
            pass

    def pop_seek(self) -> float | None:
        """取走待执行的视频快进目标（秒）；无则 None。经 /api/sync/tick 响应带回界面。"""
        sk = self._seek_to
        self._seek_to = None
        return sk

    def state(self) -> dict:
        return {"active": self.active, "script": Path(self.path).name if self.path else "",
                "actions": len(self.actions), "sent": self.sent, "skipped": self.skipped,
                "delay_ms": self.delay_ms, "skip_idle": self.skip_idle,
                "idle_threshold": self.idle_threshold}
