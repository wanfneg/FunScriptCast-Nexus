# -*- coding: utf-8 -*-
"""验证 max_new_tokens 是否是 25s 尖峰的成因。

直接加载 ASR 模型，对同一段音频用不同 max_new_tokens 跑，比较耗时与输出。
用法： .venv\\Scripts\\python.exe tests\\asr_maxtok.py <视频> <偏移秒>
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
SUB = APP_DIR / "vendor" / "subtitle"
LOG = APP_DIR / "tests" / "_maxtok_log.txt"


def emit(line: str) -> None:
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def main() -> int:
    video = Path(sys.argv[1])
    off = float(sys.argv[2]) if len(sys.argv) > 2 else 600.0
    ffmpeg = shutil.which("ffmpeg") or r"C:\Users\admin\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0-full_build\bin\ffmpeg.exe"

    sys.path.insert(0, str(SUB))
    os.environ.setdefault("ASR_MODEL", str(APP_DIR / "models" / "Qwen3-ASR-0.6B"))
    import numpy as np
    from qwen_asr import Qwen3ASRModel

    model_dir = str(APP_DIR / "models" / "Qwen3-ASR-0.6B")
    aligner_dir = str(APP_DIR / "models" / "Qwen3-ForcedAligner-0.6B")
    emit(f"加载模型 {model_dir}")
    t0 = time.perf_counter()
    m = Qwen3ASRModel.from_pretrained(
        model_dir, dtype="bfloat16", device_map="cuda:0",
        forced_aligner=aligner_dir,
        forced_aligner_kwargs=dict(dtype="bfloat16", device_map="cuda:0"),
    )
    emit(f"模型加载 {round(time.perf_counter()-t0,1)}s")

    # 抽 25s 音频
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-ss", f"{off:.3f}", "-i", str(video),
           "-vn", "-ac", "1", "-ar", "16000", "-t", "25", "-f", "s16le", "-"]
    p = subprocess.run(cmd, capture_output=True)
    pcm = np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    emit(f"PCM {len(pcm)/16000:.1f}s")

    for mt in (512, 128, 64):
        m.max_new_tokens = mt
        t0 = time.perf_counter()
        r = m.transcribe(audio=(pcm, 16000), context="", language="Japanese",
                         return_time_stamps=True)[0]
        dt = round(time.perf_counter() - t0, 1)
        txt = (r.text or "").strip()
        n_ts = len(getattr(r, "time_stamps", []) or [])
        emit(f"max_new_tokens={mt:4d}  耗时={dt:6.1f}s  文本长度={len(txt)}  "
             f"时间戳段={n_ts}  文本={txt[:80]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
