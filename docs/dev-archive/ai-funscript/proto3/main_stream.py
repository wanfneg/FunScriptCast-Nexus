"""proto3 主程序 —— 边播边推理端到端：mpv 播放 + 流式识别 + 设备直驱。

    python main_stream.py --video "E:\\testvideo\\K1drsp.mp4" --device sim
    python main_stream.py --video ... --device vorze --paused

与 proto2 的差别：位置源不再是完整 funscript 文件，而是 StreamEngine 的
ActionBuffer（识别流水线实时产出）。播放头越过 gen_until 时玩具安全闲置，
屏幕显示"AI 预热中/跟上中"。

依赖环境: GTG venv（torch）+ bleak。在 GTG venv 里运行:
    _third_party\\GoblinTrainingGrounds\\venv\\Scripts\\python.exe main_stream.py ...
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "proto1"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "proto2"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from device import PROFILES, SimDevice, ToyDevice          # noqa: E402
from funscript import kround                                # noqa: E402
from media_clock import MediaClock                          # noqa: E402
from mpv_player import MpvPlayer                            # noqa: E402
from stream_engine import ROW_HZ, StreamEngine              # noqa: E402

LOOP_SEC = 0.020
REPORT_INTERVAL_SEC = 0.2
SEEK_STEP_SEC = 5.0


def ffprobe_duration_ms(video: str) -> float:
    import shutil
    import time as _t
    ffprobe = shutil.which("ffprobe") or r"C:\Users\admin\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0-full_build\bin\ffprobe.exe"
    last = ""
    for attempt in (1, 2, 3):
        r = subprocess_run([ffprobe, "-v", "error", "-show_entries",
                            "format=duration", "-of", "csv=p=0", video])
        if r.strip():
            return float(r.strip()) * 1000.0
        # 失败时抓真实原因(文件被杀软/索引器短暂锁住等)
        err = subprocess_run([ffprobe, "-v", "error", video])
        last = err.strip()[-300:] or f"ffprobe 无输出(第 {attempt} 次)"
        print(f"[ffprobe] 第 {attempt} 次失败: {last}", flush=True)
        _t.sleep(2)
    raise RuntimeError(f"ffprobe 无法读取视频: {video}\n最后错误: {last}")


def subprocess_run(cmd):
    import subprocess
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--video", required=True)
    ap.add_argument("--device", choices=["sim", *PROFILES.keys()], default="sim")
    ap.add_argument("--address", default=None)
    ap.add_argument("--invert", action="store_true")
    ap.add_argument("--min", type=int, default=0)
    ap.add_argument("--max", type=int, default=100)
    ap.add_argument("--max-speed", type=int, default=None,
                    help="速度上限 %%/s(默认跟随设备上报值, 通常 500)")
    ap.add_argument("--delay-ms", type=int, default=0)
    ap.add_argument("--start", type=float, default=None)
    ap.add_argument("--paused", action="store_true")
    ap.add_argument("--no-auto-start", action="store_true",
                    help="预热完成后不自动开播(默认自动)")
    ap.add_argument("--no-video", action="store_true")
    ap.add_argument("--duration", type=float, default=None)
    ap.add_argument("--ckpt", default=None, help="默认官方 v0.6.0")
    return ap.parse_args()


async def main() -> int:
    args = parse_args()
    duration_ms = ffprobe_duration_ms(args.video)

    if args.device == "sim":
        dev: SimDevice | ToyDevice = SimDevice()
        await dev.connect()
    else:
        dev = ToyDevice(args.device, address=args.address, min_percent=args.min,
                        max_percent=args.max, invert=args.invert,
                        max_speed=args.max_speed)
        if not await dev.connect():
            return 1

    def note(s: str) -> None:
        print(f"[engine] {s}", flush=True)

    auto_start = not args.no_auto_start
    engine = StreamEngine(args.video, duration_ms, device="cuda",
                          ckpt=args.ckpt, on_note=note)
    engine.start()

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
    last_reconnect = -99.0
    user_paused = False
    playing = not args.paused          # mpv paused/rate 缓存, 0.2s 轮询刷新
    rate = 1.0
    next_tick = loop_t() + LOOP_SEC
    # 设备位置估计(0-100): 最后目标按限速推进。冻结/掉帧后设备落后于脚本,
    # 换段时用 积压距离/段剩余时间 的追赶速度补上 —— 否则滞后逐段累积成蠕动
    est = 50.0
    last_target = 50.0
    last_speed = 0

    if not args.paused:
        mpv.set_pause(False)

    print("控制: 空格=播放/暂停  a/d=seek±5s  x=急停  q=退出")
    print("提示: mpv 视频窗口请保持打开; 预热完成后自动开播(默认)。", flush=True)
    try:
        while True:
            # 节拍补偿: 绝对 deadline(body 耗时自动从睡眠里扣), 睡眠提前 8ms
            # 收尾、剩余用 sleep(0) 让出式等齐, 绕开 Windows 定时器粒度。
            # 本机实测(GTG venv): sleep(0.020) mean=31.3ms; 纯 deadline 补偿
            # mean=28.0ms; 此方案 mean=21.9ms。
            delay = next_tick - loop_t()
            if delay > 0.008:
                await asyncio.sleep(delay - 0.008)
            while loop_t() < next_tick:
                await asyncio.sleep(0)
            next_tick = max(next_tick + LOOP_SEC, loop_t())

            if sys.platform == "win32" and sys.stdin.isatty():
                import msvcrt
                while msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch in ("q", "Q", "\x03"):
                        return 0
                    elif ch == " ":
                        mpv.set_pause(not playing)
                        user_paused = playing
                    elif ch in ("a", "A"):
                        nt = max(0.0, mclock.clock - SEEK_STEP_SEC)
                        mpv.seek_abs(nt)
                        mclock.nudge(nt)   # 立即对齐时钟, 别等 0.2s 后的 on_report
                        last_index = -1
                    elif ch in ("d", "D"):
                        nt = mclock.clock + SEEK_STEP_SEC
                        mpv.seek_abs(nt)
                        mclock.nudge(nt)
                        last_index = -1
                        engine.request_jump(nt * 1000.0)
                    elif ch in ("x", "X"):
                        dev.allow_move = not dev.allow_move
                        last_index = -1

            if args.duration and loop_t() > args.duration:
                return 0
            if not mpv.is_running():
                print("\nmpv 已退出")
                return 0

            now = loop_t()
            if now - last_report_poll >= REPORT_INTERVAL_SEC:
                last_report_poll = now
                # paused/rate/poll_pos 三次阻塞 IPC 合并到 0.2s 轮询: 原来每
                # tick 两次往返(timeout=2s), mpv 忙时拖停整个循环令设备无指令
                playing = not mpv.paused()
                rate = mpv.rate()
                t = mclock.tick(playing, rate)
                reported = mpv.poll_pos()
                if reported is not None:
                    mclock.on_report(reported, playing)
                    t = mclock.clock
            else:
                t = mclock.tick(playing, rate)
            engine.set_playhead(t * 1000.0)

            # 位置估计按限速向最后目标推进(BLE 写失败时估计照走, 追赶逻辑兜底)
            cap = last_speed * LOOP_SEC
            est += max(-cap, min(cap, last_target - est))

            st_ms = (t + offset) * 1000.0
            actions, gen_until = engine.buffer.snapshot()
            reason = ""
            if not dev.connected:
                reason = "设备未连接"
                if loop_t() - last_reconnect >= 5.0:
                    last_reconnect = loop_t()
                    print(f"\r[设备] 断线, 尝试重连(第 {dev.write_failures} 次失败后)...",
                          flush=True)
                    if await dev.reconnect():
                        last_index = -1
                        print("[设备] 重连成功", flush=True)
            elif not dev.allow_move:
                reason = "急停中"
            elif not playing:
                reason = "已暂停"
            elif st_ms >= gen_until:
                reason = ("AI 预热中" if engine.buffer.state == "warming"
                          else "AI 跟上中")

            if reason:
                last_index = -1
                # 预热完成自动开播: 用户不需要知道 warmup 的存在
                if (auto_start and not user_paused
                        and engine.buffer.state == "running" and not playing):
                    mpv.set_pause(False)
                    print("[auto] 预热完成, 自动开始播放", flush=True)
                print(f"\r[t={t:8.2f}s] {reason:<10} 动作 {len(actions):<5}     ",
                      end="", flush=True)
                continue

            ats = [a["at"] for a in actions]
            n = bisect.bisect_right(ats, st_ms)     # 第一个 at > t 的动作
            if n < 1 or n >= len(actions):
                txt = "等待脚本开始" if n < 1 else "缓冲末尾"
                print(f"\r[t={t:8.2f}s] {txt:<10} 动作 {len(actions):<5}     ",
                      end="", flush=True)
                last_index = -1
                continue

            if n != last_index:
                a_prev, a_cur = actions[n - 1], actions[n]
                dt = (a_cur["at"] - a_prev["at"]) / 1000.0
                if dt <= 0:
                    continue
                # 追赶补偿: 冻结/掉帧后设备位置(est)落后于脚本, 用
                # 积压距离/段剩余时间 的追赶速度与段均速取大者, 滞后不跨段累积
                remain = max(0.05, a_cur["at"] / 1000.0 - t)
                catchup = abs(a_cur["pos"] - est) / remain
                norm = abs(a_cur["pos"] - a_prev["pos"]) / dt
                speed = kround(max(norm, catchup) * rate)
                if await dev.move_to(a_cur["pos"], speed):
                    # est 推进用设备实际执行的速度(被 max_speed 钳过), 否则
                    # >500%/s 的段 est 领先真机造成不可观测偏差
                    last_target = float(a_cur["pos"])
                    last_speed = min(speed, dev.max_speed)
                    frames += 1
                    last_index = n
                print(f"\r[t={t:8.2f}s] 段#{n:<5} pos={a_cur['pos']:>3}% "
                      f"speed={speed:>4} 帧={frames} 败={dev.write_failures} "
                      f"生成至{gen_until/1000:.0f}s   ",
                      end="", flush=True)
    finally:
        print()
        # 会话数据导出: 每次运行都留下可离线分析的数据
        try:
            sess_dir = Path(__file__).resolve().parent / "sessions" / \
                datetime.now().strftime("%Y%m%d_%H%M%S")
            sess_dir.mkdir(parents=True, exist_ok=True)
            import numpy as np
            trk = getattr(engine, "_trk", None)
            if trk:
                np.savez(sess_dir / "tracks.npz", **trk,
                         times_ms=np.asarray(engine.times_ms))
            actions, gen_until = engine.buffer.snapshot()
            json.dump(
                {"video": args.video, "device": args.device,
                 "n_actions": len(actions), "gen_until_ms": gen_until,
                 "coverage_s": actions[-1]["at"] / 1000.0 if actions else 0.0,
                 "actions": actions},
                open(sess_dir / "actions.json", "w", encoding="utf-8"),
                ensure_ascii=False, indent=1)
            meta = {"video": args.video, "delay_ms": args.delay_ms,
                    "frames_sent": frames, "engine_state": engine.buffer.state,
                    "note": engine.buffer.note,
                    "rev_bias_log": getattr(engine, "rev_bias_log", [])}
            json.dump(meta, open(sess_dir / "meta.json", "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
            print(f"会话数据已存: {sess_dir}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"会话导出失败(不影响退出): {e}", flush=True)
        engine.stop(cleanup=True)
        mpv.close()
        await dev.disconnect()
        print(f"退出。共发送 {frames} 帧。")
    return 0


_T0 = None


def loop_t() -> float:
    import time
    global _T0
    if _T0 is None:
        _T0 = time.monotonic()
    return time.monotonic() - _T0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
    except SystemExit:
        raise
    except BaseException:
        import traceback
        from pathlib import Path as _P
        from datetime import datetime as _dt
        try:
            log = _P(__file__).resolve().parent / "sessions" /                 ("crash_" + _dt.now().strftime("%Y%m%d_%H%M%S") + ".log")
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(traceback.format_exc(), encoding="utf-8")
            print(f"崩溃日志: {log}", flush=True)
        except Exception:
            pass
        raise
