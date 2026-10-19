"""流式识别整段视频并导出为 funscript。
用法: stream_to_script.py VIDEO OUT_FUNSCRIPT [RUN_SEC]
自动清场(main_stream/mpv), 模拟 1x 播放头, 结束导出已发布动作。
"""
import json
import numpy as np
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from stream_engine import ROW_HZ, StreamEngine  # noqa: E402


def ffprobe_ms(video: str) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration", "-of", "csv=p=0", video],
                       capture_output=True, text=True)
    return float(r.stdout.strip()) * 1000.0


def clear_gpu_instances() -> None:
    subprocess.run(["taskkill", "/F", "/IM", "mpv.exe"], capture_output=True)
    ps = ("Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | "
          "Where-Object { $_.CommandLine -match 'main_stream' } | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)
    time.sleep(2)


def main() -> int:
    video = sys.argv[1]
    out_path = Path(sys.argv[2])
    run_sec = int(sys.argv[3]) if len(sys.argv) > 3 else 600
    duration_ms = ffprobe_ms(video)

    clear_gpu_instances()
    lat = Path(video).with_suffix(".stream_lat.bin")
    lat.unlink(missing_ok=True)

    def note(s: str) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {s}", flush=True)

    eng = StreamEngine(video, duration_ms, device="cuda", on_note=note)
    eng.start()
    t0 = time.time()
    last_line = ""
    while time.time() - t0 < run_sec:
        time.sleep(5)
        ph = max(0.0, (time.time() - t0) - 12.0) * 1000.0  # 模拟 1x 播放头
        eng.set_playhead(ph)
        actions, gen_until = eng.buffer.snapshot()
        line = (f"rows={eng.n_rows} ({eng.n_rows / ROW_HZ:.0f}s) "
                f"state={eng.buffer.state} actions={len(actions)} "
                f"gen={gen_until / 1000:.0f}s ph={ph / 1000:.0f}s")
        if line != last_line:
            print(f"[{time.time() - t0:6.0f}s] {line}", flush=True)
            last_line = line
        if eng.buffer.state in ("done", "error"):
            break

    actions, gen_until = eng.buffer.snapshot()
    # 导出最近一次前向的轨道(与官方 diff 用)
    lt = getattr(eng, "_trk", None)
    if lt:
        np.savez(str(out_path.parent / (out_path.stem + "_tracks.npz")),
                 **{k: v for k, v in lt.items() if k != "times_ms"})
    out_path.parent.mkdir(parents=True, exist_ok=True)
    json.dump({"version": "1.0", "inverted": False, "range": 100,
               "actions": [{"at": int(round(a["at"])), "pos": int(a["pos"])}
                           for a in actions]},
              open(out_path, "w", encoding="utf-8"))
    print(f"EXPORTED: {out_path} ({len(actions)} 动作, 覆盖至 "
          f"{gen_until / 1000:.0f}s, 用时 {time.time() - t0:.0f}s)", flush=True)
    if eng.buffer.state == "error":
        print(f"引擎错误: {eng.buffer.note}", file=sys.stderr)
    eng.stop(cleanup=False)  # 保留 latent 以便断点续跑/排查
    return 0 if actions else 1


if __name__ == "__main__":
    sys.exit(main())
