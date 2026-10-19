"""proto2：视频联动 —— mpv 真实媒体时钟 + funscript + 设备（BLE/模拟）。

    python main_video.py --video E:\\testvideo\\K1drsp.mp4 --device sim
    python main_video.py --video E:\\testvideo\\K1drsp.mp4 --device vorze --paused

- 脚本默认自动发现：视频同目录同名 .funscript（--script 可覆盖）
- 时钟语义 = SyncEngine：本地 monotonic 推进，mpv time-pos 上报锚定
  （偏差>0.5s 吸附 = seek；否则 10% 渐进校正）
- 键盘: 空格=播放/暂停  a/d=seek±5s  x=急停  q=退出（mpv 窗口里的空格/方向键同样生效，
  本地状态会在 200ms 内被轮询拉齐）
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "proto1"))

from clock import PlaybackClock           # noqa: E402  (proto1 键盘模拟时钟, sim 兜底用)
from device import PROFILES, SimDevice, ToyDevice  # noqa: E402
from funscript import Funscript           # noqa: E402
from media_clock import MediaClock        # noqa: E402
from mpv_player import IpcDead, MpvPlayer  # noqa: E402

LOOP_SEC = 0.020
REPORT_INTERVAL_SEC = 0.2     # mpv time-pos 轮询周期
SEEK_STEP_SEC = 5.0


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--video", required=True)
    ap.add_argument("--script", default=None, help="默认: 视频同目录同名 .funscript")
    ap.add_argument("--device", choices=["sim", *PROFILES.keys()], default="sim")
    ap.add_argument("--address", default=None)
    ap.add_argument("--invert", action="store_true")
    ap.add_argument("--min", type=int, default=0)
    ap.add_argument("--max", type=int, default=100)
    ap.add_argument("--max-speed", type=int, default=350)
    ap.add_argument("--delay-ms", type=int, default=0)
    ap.add_argument("--start", type=float, default=None, help="起播位置（秒）")
    ap.add_argument("--paused", action="store_true", help="启动不自动播放")
    ap.add_argument("--no-video", action="store_true", help="mpv 只出声不出画（无头验证）")
    ap.add_argument("--duration", type=float, default=None, help="N 秒后自动退出")
    return ap.parse_args()


async def main() -> int:
    args = parse_args()
    script_path = args.script or os.path.splitext(args.video)[0] + ".funscript"
    script = Funscript.from_json(open(script_path, encoding="utf-8").read())
    print(f"视频: {args.video}")
    print(f"脚本: {script_path}  动作点 {script.count} 个")

    if args.device == "sim":
        dev: SimDevice | ToyDevice = SimDevice()
        await dev.connect()
    else:
        dev = ToyDevice(args.device, address=args.address, min_percent=args.min,
                        max_percent=args.max, invert=args.invert, max_speed=args.max_speed)
        if not await dev.connect():
            return 1

    extra = ["--vo=null", "--ao=null", "--mute=yes"] if args.no_video else None
    mpv = MpvPlayer()
    print("启动 mpv ...", flush=True)
    mpv.launch(args.video, start=args.start, paused=True, extra=extra)
    print("mpv IPC 已连接", flush=True)

    mclock = MediaClock()
    offset = args.delay_ms / 1000.0
    last_index = -1
    frames = 0
    last_report_poll = 0.0
    sim_clock = PlaybackClock()   # mpv 死掉后的兜底（一般用不到）
    if not args.paused:
        mpv.set_pause(False)

    print("控制: 空格=播放/暂停  a/d=seek±5s  x=急停  q=退出")
    try:
        while True:
            await asyncio.sleep(LOOP_SEC)

            # ---- 键盘（仅交互控制台）----
            if sys.platform == "win32" and sys.stdin.isatty():
                import msvcrt
                while msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch in ("q", "Q", "\x03"):
                        return 0
                    elif ch == " ":
                        mpv.set_pause(not mpv.paused())
                    elif ch in ("a", "A"):
                        mpv.seek_abs(max(0.0, mclock.clock - SEEK_STEP_SEC))
                        last_index = -1
                    elif ch in ("d", "D"):
                        mpv.seek_abs(mclock.clock + SEEK_STEP_SEC)
                        last_index = -1
                    elif ch in ("x", "X"):
                        dev.allow_move = not dev.allow_move
                        last_index = -1

            if args.duration and loop_t() > args.duration:
                return 0
            if not mpv.is_running():
                print("\nmpv 已退出")
                return 0

            # ---- 时钟：本地推进 + mpv 上报锚定 ----
            playing = not mpv.paused()
            rate = mpv.rate()
            t = mclock.tick(playing, rate)
            now = loop_t()
            if now - last_report_poll >= REPORT_INTERVAL_SEC:
                last_report_poll = now
                reported = mpv.poll_pos()
                if reported is not None:
                    mclock.on_report(reported, playing)
                    t = mclock.clock

            # ---- 同步主循环（= proto1 = SyncEngine）----
            reason = ""
            if not dev.connected:
                reason = "设备未连接"
            elif not dev.allow_move:
                reason = "急停中"
            elif not playing:
                reason = "已暂停"

            if reason:
                last_index = -1
                print(f"\r[t={t:8.2f}s] {reason:<10}          ", end="", flush=True)
                continue

            st = t + offset
            n = script.index_after(st)
            if n < 1 or n > script.count - 1:
                txt = "等待脚本开始" if n < 1 else "脚本已结束"
                print(f"\r[t={t:8.2f}s] {txt:<10}          ", end="", flush=True)
                last_index = -1
                continue

            if n != last_index:
                move = script.compute_move(t, offset, rate)
                if move is not None:
                    target, speed = move
                    if await dev.move_to(target, speed):
                        frames += 1
                        last_index = n
                    print(f"\r[t={t:8.2f}s] 段#{n:<5} pos={target:>3}% speed={speed:>4} 帧={frames}   ",
                          end="", flush=True)
    finally:
        print()
        mpv.close()
        await dev.disconnect()
        print(f"退出。共发送 {frames} 帧。")
    return 0


import time as _time
_T0 = None


def loop_t() -> float:
    global _T0
    if _T0 is None:
        _T0 = _time.monotonic()
    return _time.monotonic() - _T0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
