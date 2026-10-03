"""预设播放引擎 —— **逐条对齐手机端 sync/PresetPlayer.kt**（不再自行发挥）。

手机端语义（照抄）：
  · select(id)：选中预设并**退出随机**。
  · togglePlay()：在播就停，否则开始；未选中时兜底 Normal。
  · start()：先在跑就直接返回；取消上一个 Job 后起循环：
        while(playing) { playOneLoop(def); if(!playing) break;
                         if(random) currentId = randomOtherThan(id) }
  · toggleRandom()：已开 → 关（**保持当前预设继续播**）；
                    未开 → 开，且**若在播就立即跳转**到随机预设。
  · effectiveSpeed()：v = boost ? 500 : 滑块值；clamp 到 [1,500] 再 min(设备速度上限)。
  · playOneLoop(def)：对 def.playSegments 每一段：
        dist   = |end - start|
        speed  = effectiveSpeed()                     ← 用滑块/BOOST 的绝对速度，不是段速度
        naturalMs = durationMs ?? dist*1000/segSpeed
        waitMs = naturalMs * segSpeed / speed          ← 倍率=自然速度/实际速度
        if (dist > 0) 先发指令(段末位置, speed)         ← **先发后等**
        然后按 20ms 步进等待 waitMs（可被停止打断）
  · 保持段（dist=0）只等待、不发指令。
  · 位置映射/限速/反转都由通道 moveTo 内部处理（这里不 raw、不自算）。
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from pathlib import Path

from .protocols import kotlin_round

BOOST_SPEED = 500
DEFAULT_ID = "normal"
TICK_MS = 20


def load_presets(path: Path) -> list[dict]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return []


def to_segments(kfs: list) -> list[list]:
    """波形式 keyframes → 段式（**逐条对齐手机端 PresetDefs.toSegments**，无损转换）：
    相邻关键帧 = 一段，speed = 距离÷时长（下限 1），durationMs 记录原始时间轴。
    24 个预设里 22 个只有 keyframes —— 手机端 `playSegments = segments 非空 ? segments
    : toSegments(keyframes)`（PresetDefs.kt:66-67），缺了这步派生就是空段列表，
    `_play_one_loop` 里一个 await 都没有，纯 CPU 自旋锁死整条 BLE 事件循环。"""
    out = []
    for a, b in zip(kfs, kfs[1:]):
        dist = abs(float(b[0]) - float(a[0]))
        dur = float(b[1]) - float(a[1])
        speed = max(1, kotlin_round(dist * 1000.0 / dur)) if (dist > 0 and dur > 0) else 1
        out.append([float(a[0]), float(b[0]), speed, max(0.0, dur)])
    return out


class PresetPlayer:
    def __init__(self, channel, loop: asyncio.AbstractEventLoop, presets_path: Path) -> None:
        self.ch = channel
        self.loop = loop
        self.presets = load_presets(presets_path)
        # 播放用段式列表：段式预设直接用；波形式**加载时一次性派生**（手机端 PresetDef
        # 构造时同款）。派生结果放在独立键里，原 segments/keyframes 原样保留。
        for pr in self.presets:
            if not pr.get("segments") and pr.get("keyframes"):
                pr["_play"] = to_segments(pr["keyframes"])
            else:
                pr["_play"] = list(pr.get("segments") or [])
        self.selected: str | None = None
        self.playing = False
        self.random_mode = False
        self.boost = False
        self.speed = 100          # speedControlProvider（预设速度滑轨）
        self._gen = 0             # 世代：停止/重开让旧循环必死，绝不并发
        self._boost_prev_speed = None   # BOOST 前速度（取消时恢复，手机端 boostPrevSpeed）
        self._self_moving = False

    # ---- 查询 ----
    def by_id(self, pid: str | None) -> dict | None:
        for pr in self.presets:
            if pr.get("id") == pid:
                return pr
        return None

    def _random_other(self, pid: str | None) -> str:
        pool = [p["id"] for p in self.presets if p.get("id") != pid]
        return random.choice(pool) if pool else DEFAULT_ID

    def _effective_speed(self) -> int:
        cap = max(1, int(getattr(self.ch, "max_speed", 500) or 500))
        v = BOOST_SPEED if self.boost else int(self.speed)
        return min(max(1, min(v, 500)), cap)

    def _spawn(self, coro) -> None:
        self.loop.call_soon_threadsafe(lambda: self.loop.create_task(coro))

    # ---- 控件（与手机端同名同义）----
    def select(self, pid: str | None) -> dict:
        if pid and not self.by_id(pid):
            return {"ok": False, "error": "未知预设"}
        self.selected = pid
        self.random_mode = False                       # 点网格卡片 = 退出随机
        return {"ok": True, "state": self.state()}

    def toggle_play(self) -> dict:
        return self.stop() if self.playing else self.start()

    def start(self) -> dict:
        if self.playing:
            return {"ok": True, "state": self.state()}
        if not self.selected:
            self.selected = DEFAULT_ID
        if not self.by_id(self.selected):
            return {"ok": False, "error": "没有可用预设"}
        self.playing = True
        self._gen += 1
        self._spawn(self._run(self._gen))
        return {"ok": True, "state": self.state()}

    def stop(self) -> dict:
        self.playing = False
        self._gen += 1
        return {"ok": True, "state": self.state()}

    def toggle_random(self) -> dict:
        if self.random_mode:
            self.random_mode = False                   # 关：保持当前预设继续播
            return {"ok": True, "state": self.state()}
        self.random_mode = True
        if self.playing:                               # 开：在播就立即跳转
            self.selected = self._random_other(self.selected)
            self._gen += 1
            self._spawn(self._run(self._gen))
        return {"ok": True, "state": self.state()}

    def toggle_boost(self) -> dict:
        """手机端 togglePresetBoost：激活时记住当前速度并把滑块跳到 500；取消时恢复原速。"""
        if self.boost:
            self.boost = False
            if self._boost_prev_speed is not None:
                self.speed = self._boost_prev_speed
                self._boost_prev_speed = None
        else:
            self._boost_prev_speed = self.speed
            self.boost = True
            self.speed = BOOST_SPEED
        return {"ok": True, "state": self.state()}

    def set_speed(self, v: int) -> dict:
        self.speed = max(1, min(500, int(v)))
        if self.boost:
            self._boost_prev_speed = self.speed   # 手机端：BOOST 期间拖滑块 → 取消后恢复该值
        return {"ok": True, "state": self.state()}

    # ---- 循环 ----
    def _can(self, gen: int, pid: str | None = None) -> bool:
        if not self.playing or gen != self._gen:
            return False
        return pid is None or self.selected == pid

    async def _run(self, gen: int) -> None:
        try:
            while self.playing and gen == self._gen:
                pid = self.selected or DEFAULT_ID
                pr = self.by_id(pid) or self.by_id(DEFAULT_ID)
                if not pr:
                    break
                await self._play_one_loop(pr, gen)
                if not (self.playing and gen == self._gen):
                    break
                if self.random_mode:
                    self.selected = self._random_other(pid)      # 每个循环结束换下一个
        except asyncio.CancelledError:
            pass
        except Exception as e:
            # R125 修复（全项目审查中危）：预设循环意外死亡必须复位 playing——
            # 旧代码 UI 显示播放中而设备不动、start() 无法自启（卡死）。
            print(f"[preset] 预设循环异常终止：{type(e).__name__}: {e}", flush=True)
            self.playing = False

    async def _play_one_loop(self, pr: dict, gen: int) -> None:
        for sg in (pr.get("_play") or pr.get("segments") or []):
            if not self._can(gen, pr.get("id")):
                return
            s, e = float(sg[0]), float(sg[1])
            seg_speed = max(1.0, float(sg[2] or 100))
            dur = sg[3] if len(sg) > 3 else None
            dist = abs(e - s)
            speed = self._effective_speed()
            natural_ms = float(dur) if dur else (dist * 1000.0 / seg_speed)
            wait_ms = natural_ms * seg_speed / max(1, speed)
            if dist > 0 and self.ch.state.allow_move:
                # allow_move 检查是纵深防御：急停正常路径已把会话停掉（arbiter），
                # 这里兜的是"会话外残留调用"——不发帧但照常等待，节奏不乱。
                self._self_moving = True
                try:
                    await self.ch.move_to(e, speed)   # 先发
                finally:
                    self._self_moving = False
            t0 = time.monotonic()                                 # 后等（20ms 步进，可打断）
            while (time.monotonic() - t0) * 1000.0 < wait_ms and self._can(gen, pr.get("id")):
                await asyncio.sleep(TICK_MS / 1000.0)

    def state(self) -> dict:
        return {"playing": self.playing, "selected": self.selected, "random": self.random_mode,
                "boost": self.boost, "speed": self.speed, "count": len(self.presets)}
