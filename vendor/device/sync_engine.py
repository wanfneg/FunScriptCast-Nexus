"""脚本同步（对应手机端 sync/SyncEngine.kt 的核心职责）。

**帧语义与手机端逐条对齐**（本次机制级修复，"动作一顿一顿"的根因）：
  · 手机端主循环 20ms 跑一圈，用本地时钟外推播放进度、只在**段边界**发帧；
  · 发帧条件：t 落入新动作段（`n != lastIndex`）——**每段只发一帧**；
  · 帧内容 = **段末位置 + 段速度**（computeMove：target = keyframes[n]，
    speed = 段斜率 × 播放倍速），之后的**整段行程由设备固件按速度插值走完**；
  · 首/末动作点之外一帧不发；急停（allowMove）不发；发送失败不推进段索引（下拍重试）。

PC 早期实现是"每 250ms 发一次**当前插值位置**"——帧率低且抖动（浏览器 timeupdate +
BLE 写入延迟）时，设备每帧只被喂一小步、走到就停等下一帧，必然走走停停
（慢段还叠加"位移<1% 不发"的去重，最慢；快段段边界迟发；倍速无补偿）。
改为手机同构后每帧都给足整段行程，运动才连续。
"""
from __future__ import annotations

import asyncio
import bisect
import json
from pathlib import Path


class SyncEngine:
    def __init__(self, channel, loop: asyncio.AbstractEventLoop) -> None:
        self.ch = channel
        self.loop = loop
        self.active = False
        self.path = ""
        self.actions: list[list[float]] = []
        self._times: list[float] = []        # actions 时间轴缓存（bisect 用）
        self.delay_ms = 0
        # 对齐手机端默认：skipIdleEnabled=false / 阈值 60s（AppViewModel.kt:896/899）。
        self.skip_idle = False
        self.idle_threshold = 60.0
        self.sent = 0
        self.skipped = 0
        self._last_n: int | None = None      # 已发送的段索引（手机端 lastIndex）
        self._skip_n: int | None = None      # 已判定过"静止段跳播"的段索引（防重复 seek）
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
        self._times = [a[0] for a in acts]
        self.path = str(sp)
        self._last_n = None
        self._skip_n = None
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
        self._last_n = None

    def stop(self) -> dict:
        self.active = False
        self._seek_to = None
        return {"ok": True}

    # ---- 进度驱动 ----
    def tick(self, t_sec: float, rate: float = 1.0) -> None:
        """HTTP 线程调用：投递到 BLE 事件循环，不阻塞请求。
        rate = 播放倍速（手机端 computeMove 的 playbackRate，速度要乘它）。"""
        if not self.active:
            return
        asyncio.run_coroutine_threadsafe(self._apply(float(t_sec), float(rate or 1.0)), self.loop)

    async def _apply(self, t_sec: float, rate: float = 1.0) -> None:
        if not self.active or not self.actions:
            return
        if not self.ch.state.connected or not self.ch.state.allow_move:
            return
        ms = t_sec * 1000.0 - self.delay_ms
        acts, times = self.actions, self._times
        # 手机端 indexAfter：第一个 at > ms 的索引 = 当前段上界 n（t ∈ [acts[n-1], acts[n])）
        n = bisect.bisect_right(times, ms)
        if n < 1 or n >= len(acts):
            return                          # 首/末动作点之外：一帧不发（对齐 SyncEngine.kt:285-289）
        a, b = acts[n - 1], acts[n]
        dt_s = max(0.001, (b[0] - a[0]) / 1000.0)
        dist = abs(b[1] - a[1])
        slope = dist / dt_s                 # %/秒
        if self.skip_idle and slope < 1.0 and dt_s >= self.idle_threshold:
            # 手机端 rebuildIdleGaps 口径：|Δval|/dt < 0.01（value 0..1 域）= 1 %/秒；
            # 段长超阈值 → 跳过该段，让前端把视频快进到段末（Q2 裁定：seek 语义）。
            # 判定**独立于段去重**（手机端 maybeSkipIdle 每一拍都判）；每段只发布一次 seek。
            if n != self._skip_n:
                self._skip_n = n
                self.skipped += 1
                self._seek_to = (b[0] + self.delay_ms) / 1000.0
            return
        if n == self._last_n:
            return                          # 段没变：手机端只在段边界发帧（n != lastIndex）
        # 手机端 computeMove：target = 段末位置；speed = 段斜率 × 播放倍速
        speed = int(round(slope * rate))
        try:
            ok = await self.ch.move_to(b[1], speed)   # 整段行程交给设备固件按速度插值
            if ok:
                self.sent += 1
                self._last_n = n            # 手机端：仅发送成功才推进 lastIndex（失败下拍重试）
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
