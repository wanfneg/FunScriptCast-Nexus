"""只读探针（有界版）：确认「keyframes-only 预设」会把事件循环锁死到什么程度。

用守护线程在 8 秒后强制 os._exit，避免探针自身被自旋饿死而挂住。
两次实验各用一个**全新的**事件循环：
  A) 预热 > 检查段数 > start() > sleep(1.0) > 打印心跳
  B) 同 A，但预设换成有 segments 的 'normal'
判据：A 的 sleep(1.0) 之后的打印语句永不执行（连 wait_for 的定时器都跑不到），
      B 正常返回 —— 差别只来自预设定义，即可确认是 _play_one_loop 的空转。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "vendor"))

from device.preset_player import PresetPlayer  # noqa: E402

PHASE = {"v": "boot"}


def watchdog() -> None:
    time.sleep(8.0)
    sys.stdout.write(f"[watchdog] 8s 到，仍在 phase={PHASE['v']!r} —— 事件循环被饿死（进程强制退出）\n")
    sys.stdout.flush()
    os._exit(9)


class FakeChannel:
    max_speed = 500
    range_lo = 0.0
    range_hi = 100.0

    def __init__(self):
        self.frames = 0

    async def move_to(self, percent, speed=None, force=False, raw=False):
        self.frames += 1
        await asyncio.sleep(0.001)
        return True


async def experiment(preset_id: str) -> None:
    presets = json.loads((ROOT / "ui" / "presets.json").read_text(encoding="utf-8"))
    pr = next(p for p in presets if p["id"] == preset_id)
    PHASE["v"] = f"{preset_id}: 预热事件循环"
    await asyncio.sleep(0.05)                     # 证明循环本来是活的
    ticks = 0

    async def beat():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.02)

    b = asyncio.get_running_loop().create_task(beat())
    PHASE["v"] = f"{preset_id}: start()"
    player = PresetPlayer(FakeChannel(), asyncio.get_running_loop(), ROOT / "ui" / "presets.json")
    player.select(preset_id)
    player.start()
    PHASE["v"] = f"{preset_id}: sleep(1.0) 等待"
    await asyncio.sleep(1.0)                      # ← 'classic' 到不了这里
    PHASE["v"] = f"{preset_id}: 已越过 sleep"
    b.cancel()
    print(json.dumps({"preset": preset_id, "segments": pr.get("segments"),
                      "heartbeat_ticks_in_1s": ticks, "player_still_playing": player.playing},
                     ensure_ascii=False))


def run_one(preset_id: str) -> None:
    loop = asyncio.new_event_loop()
    PHASE["v"] = f"{preset_id}: 新建循环"
    try:
        loop.run_until_complete(experiment(preset_id))
        print(f"[ok] {preset_id} 正常返回")
    finally:
        loop.close()


if __name__ == "__main__":
    threading.Thread(target=watchdog, daemon=True).start()
    run_one("normal")     # 有 segments：应当正常
    run_one("classic")    # 只有 keyframes：预期在此卡死
    print("两次实验都返回了")
