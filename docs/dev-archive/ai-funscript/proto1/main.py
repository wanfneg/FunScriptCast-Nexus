"""快速验证原型：funscript -> 时钟 -> 同步计算 -> 设备（BLE 直连或模拟）。

对齐 FunScriptCast SyncEngine 的核心语义：
  - 20ms 循环
  - 本地时钟单调推进（原型用键盘模拟播放器；真实集成替换为播放器上报）
  - 事件驱动发送：只在跨过动作点（段下标变化）时发一次 move
  - 暂停/急停等"不发送"状态会复位 lastIndex，恢复后强制重发当前段

按键:
  空格   播放/暂停
  a / d  （或 ←/→）后退/前进 5 秒
  x      急停切换（急停期间不发帧，设备停在最后位置）
  q      退出
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from clock import PlaybackClock
from device import PROFILES, SimDevice, ToyDevice
from funscript import Funscript

LOOP_INTERVAL_MS = 0.020
SEEK_STEP_SEC = 5.0


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--script", default="demo.funscript", help=".funscript 路径")
    ap.add_argument("--device", choices=["sim", *PROFILES.keys()], default="sim")
    ap.add_argument("--address", default=None, help="BLE 地址（跳过扫描）")
    ap.add_argument("--invert", action="store_true", help="反转方向")
    ap.add_argument("--min", type=int, default=0, help="行程下限 %%")
    ap.add_argument("--max", type=int, default=100, help="行程上限 %%")
    ap.add_argument("--max-speed", type=int, default=350, help="速度上限（设备单位）")
    ap.add_argument("--delay-ms", type=int, default=0, help="脚本偏移，正=滞后")
    ap.add_argument("--rate", type=float, default=1.0, help="播放倍速")
    ap.add_argument("--paused", action="store_true", help="启动后不自动播放，按空格开始")
    ap.add_argument("--duration", type=float, default=None, help="N 秒后自动退出（无头测试用）")
    return ap.parse_args()


async def main() -> int:
    args = parse_args()
    script = Funscript.from_json(open(args.script, encoding="utf-8").read())
    print(f"脚本: {args.script}  动作点 {script.count} 个")

    if args.device == "sim":
        dev: SimDevice | ToyDevice = SimDevice()
        await dev.connect()
    else:
        dev = ToyDevice(args.device, address=args.address, min_percent=args.min,
                        max_percent=args.max, invert=args.invert, max_speed=args.max_speed)
        if not await dev.connect():
            return 1

    clock = PlaybackClock()
    clock.rate = args.rate
    offset = args.delay_ms / 1000.0
    last_index = -1
    frames = 0
    start_wall = asyncio.get_event_loop().time()

    print("控制: 空格=播放/暂停  a/d=seek±5s  x=急停  q=退出")
    if not args.paused:
        clock.play()
    try:
        while True:
            await asyncio.sleep(LOOP_INTERVAL_MS)

            # ---- 键盘（Windows 控制台，非阻塞；无 TTY 时跳过）----
            if sys.platform == "win32" and sys.stdin.isatty():
                import msvcrt
                while msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch in ("q", "Q", "\x03"):
                        return 0
                    elif ch == " ":
                        clock.toggle()
                    elif ch in ("a", "A") or (ch == "\xe0" and msvcrt.getwch() == "K"):
                        clock.seek(-SEEK_STEP_SEC)
                        last_index = -1
                    elif ch in ("d", "D") or (ch == "\xe0" and msvcrt.getwch() == "M"):
                        clock.seek(SEEK_STEP_SEC)
                        last_index = -1
                    elif ch in ("x", "X"):
                        dev.allow_move = not dev.allow_move
                        last_index = -1

            if args.duration and asyncio.get_event_loop().time() - start_wall > args.duration:
                return 0

            # ---- 同步主循环（对齐 SyncEngine.loop）----
            t = clock.tick()
            reason = ""
            if not dev.connected:
                reason = "设备未连接"
            elif not dev.allow_move:
                reason = "急停中"
            elif not clock.playing:
                reason = "已暂停"

            if reason:
                if last_index != -1 or True:
                    last_index = -1
                status = f"\r[{args.device}] t={t:7.2f}s  {reason:<10}      "
                print(status, end="", flush=True)
                continue

            st = t + offset
            n = script.index_after(st)
            if n < 1 or n > script.count - 1:
                txt = "等待脚本开始" if n < 1 else "脚本已结束"
                print(f"\r[{args.device}] t={t:7.2f}s  {txt:<10}      ", end="", flush=True)
                last_index = -1
                continue

            if n != last_index:
                move = script.compute_move(t, offset, clock.rate)
                if move is not None:
                    target, speed = move
                    ok = await dev.move_to(target, speed)
                    if ok:
                        frames += 1
                        last_index = n
                    print(f"\r[{args.device}] t={t:7.2f}s  段#{n:<5} "
                          f"pos={target:>3}%  speed={speed:>4}  帧={frames}     ",
                          end="", flush=True)
    finally:
        print()
        await dev.disconnect()
        print(f"退出。共发送 {frames} 帧。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
