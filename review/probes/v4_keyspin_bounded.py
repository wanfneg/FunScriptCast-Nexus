"""R04 只读探针 4（有界）：keyframes-only 预设是否把事件循环锁死（R01-A2）。

判据（不依赖任何实现细节假设）：
  · 起一个纯 OS 线程做**观察者**（不受 asyncio 影响，只受 GIL 调度影响），
    每秒打印一次 phase 与 asyncio 心跳计数；
  · 主线程跑真实 PresetPlayer：
      'normal'（有 segments）应在 1 秒内越过 await asyncio.sleep(1.0) 并正常打印；
      'classic'（只有 keyframes）预期永远越不过去 —— 观察者会看到心跳计数冻结；
  · 观察者 4 秒后打印判词并 os._exit(9) 强制收尾，探针自身绝不会挂死。

只读：不启动 GUI/服务器，不写文件。
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
TICKS = {"n": 0}


def observer() -> None:
    for sec in (1, 2, 3):
        time.sleep(1.0)
        print(f"[observer t={sec}s] phase={PHASE['v']!r} asyncio心跳累计={TICKS['n']}", flush=True)
    print("[观察者判词] 4 秒内主线程始终没越过 await —— 事件循环被饿死，"
          "所有 asyncio 回调（含 wait_for 超时、HTTP 路由里的 submit）都无法执行", flush=True)
    os._exit(9)


class FakeChannel:
    max_speed = 500
    range_lo, range_hi = 0.0, 100.0

    def __init__(self):
        self.frames = 0
        self.on_move = None

    async def move_to(self, percent, speed=None, force=False, raw=False):
        self.frames += 1
        return True


async def experiment(preset_id: str, pr: dict) -> dict:
    await asyncio.sleep(0.05)                 # 先证明循环本来是活的
    ticks = 0

    async def beat():
        nonlocal ticks
        while True:
            ticks += 1
            TICKS["n"] = ticks
            await asyncio.sleep(0.02)

    t = asyncio.get_running_loop().create_task(beat())
    ch = FakeChannel()
    player = PresetPlayer(ch, asyncio.get_running_loop(), ROOT / "ui" / "presets.json")
    player.select(preset_id)
    PHASE["v"] = f"{preset_id}: 已 start()，准备 await sleep(1.0)"
    player.start()
    await asyncio.sleep(1.0)                  # ← 'classic' 到不了这里
    PHASE["v"] = f"{preset_id}: 越过 sleep(1.0)"
    t.cancel()
    return {"preset": preset_id,
            "segments": pr.get("segments"),
            "segments_len": len(pr.get("segments") or []),
            "keyframes_len": len(pr.get("keyframes") or []),
            "心跳次数(1s内)": ticks,
            "写帧数": ch.frames,
            "player.playing": player.playing}


presets = json.loads((ROOT / "ui" / "presets.json").read_text(encoding="utf-8"))
by_id = {p["id"]: p for p in presets}
which = sys.argv[1] if len(sys.argv) > 1 else "normal"

threading.Thread(target=observer, daemon=True).start()
loop = asyncio.new_event_loop()
try:
    res = loop.run_until_complete(experiment(which, by_id[which]))
    print("RESULT " + json.dumps(res, ensure_ascii=False), flush=True)
    print(f"[断言] {which} 正常返回: {'PASS' if res['心跳次数(1s内)'] > 10 else 'FAIL（心跳过少）'}", flush=True)
finally:
    loop.close()
