"""只读探针：验证「只有 keyframes 的预设」会不会把 asyncio 事件循环饿死。

背景：ui/presets.json 有 24 个预设，其中 22 个只有 keyframes、segments 为空数组。
PresetPlayer._play_one_loop 只遍历 pr["segments"]；空列表 ⇒ 循环体一次都不执行
⇒ 整个 _play_one_loop 里没有任何 await ⇒ 外层 `while self.playing` 变成**纯 CPU 自旋**，
而它跑在 BLE 那条唯一的 asyncio 事件循环上。

判据：让 mock 播放一个 keyframes-only 预设，同时在该循环上跑一个"心跳"协程。
  心跳 tick 数 ≈ 0 且 _run 永不返回  ⇒ 确认事件循环被锁死。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "vendor"))

from device.preset_player import PresetPlayer  # noqa: E402

TICKS = {"n": 0}


class FakeChannel:
    max_speed = 500
    range_lo = 0.0
    range_hi = 100.0

    def __init__(self, loop):
        self.loop = loop
        self.frames = 0

    async def move_to(self, percent, speed=None, force=False, raw=False):
        self.frames += 1
        await asyncio.sleep(0.001)
        return True


async def heartbeat():
    while True:
        TICKS["n"] += 1
        await asyncio.sleep(0.05)


async def probe(preset_id: str, seconds: float) -> dict:
    loop = asyncio.get_running_loop()
    TICKS["n"] = 0
    ch = FakeChannel(loop)
    player = PresetPlayer(ch, loop, ROOT / "ui" / "presets.json")
    player.select(preset_id)
    beat = loop.create_task(heartbeat())
    player.start()
    await asyncio.sleep(seconds)
    alive = not beat.done()
    beat.cancel()
    stuck = player.playing and ch.frames == 0
    return {
        "preset": preset_id,
        "heartbeat_ticks": TICKS["n"],
        "frames_written": ch.frames,
        "player_still_playing": player.playing,
        "event_loop_starved": TICKS["n"] == 0 and alive,
        "run_returns": None,
    }


async def main() -> int:
    presets = json.loads((ROOT / "ui" / "presets.json").read_text(encoding="utf-8"))
    by_id = {p["id"]: p for p in presets}
    print("preset 'classic': segments=%r keyframes=%d"
          % (by_id["classic"].get("segments"), len(by_id["classic"].get("keyframes") or [])))
    print("preset 'normal' : segments=%r" % (by_id["normal"].get("segments"),))
    for pid in ("normal", "classic"):
        try:
            res = await asyncio.wait_for(probe(pid, 1.0), timeout=3.0)
        except asyncio.TimeoutError:
            res = {"preset": pid, "heartbeat_ticks": TICKS["n"], "frames_written": -1,
                   "event_loop_starved": TICKS["n"] == 0, "note": "probe 自身也被饿死/超时"}
        print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
