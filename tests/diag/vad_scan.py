# -*- coding: utf-8 -*-
"""VAD 阈值扫描：同一段音频在不同 threshold 下判定是否有语音。

用法： .venv\\Scripts\\python.exe tests\\vad_scan.py <视频> <偏移秒>
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
LOG = APP_DIR / "tests" / "_vad_log.txt"


def emit(line: str) -> None:
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def main() -> int:
    video = Path(sys.argv[1])
    offs = [float(x) for x in (sys.argv[2].split(",") if len(sys.argv) > 2 else ["600", "120"])]
    ffmpeg = shutil.which("ffmpeg") or r"C:\Users\admin\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0-full_build\bin\ffmpeg.exe"

    import numpy as np
    from silero_vad import load_silero_vad, get_speech_timestamps

    emit("加载 silero VAD")
    vad = load_silero_vad()

    for off in offs:
        cmd = [ffmpeg, "-v", "error", "-nostdin", "-ss", f"{off:.3f}", "-i", str(video),
               "-vn", "-ac", "1", "-ar", "16000", "-t", "25", "-f", "s16le", "-"]
        p = subprocess.run(cmd, capture_output=True)
        pcm = np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32) / 32768.0
        import torch
        t = torch.from_numpy(pcm)
        emit(f"--- 偏移 {off:.0f}s  音频 {len(pcm)/16000:.1f}s")
        for th in (0.3, 0.5, 0.6, 0.7, 0.8):
            t0 = time.perf_counter()
            stamps = get_speech_timestamps(t, vad, sampling_rate=16000, threshold=th,
                                           min_speech_duration_ms=250)
            speech_ms = sum((s["end"] - s["start"]) for s in stamps) / 16.0
            emit(f"    threshold={th:.1f}  语音片段={len(stamps)}  语音总时长={speech_ms:6.0f}ms  "
                 f"耗时={round((time.perf_counter()-t0)*1000)}ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
