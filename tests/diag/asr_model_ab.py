# -*- coding: utf-8 -*-
"""对比 Qwen3-ASR 0.6B vs 1.7B：同一段音频的识别质量与耗时。

用法： .venv\\Scripts\\python.exe tests\\diag\\asr_model_ab.py <视频> [偏移秒,偏移秒,...]
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent.parent
LOG = APP_DIR / "tests" / "_model_ab_log.txt"
SR = 16000


def emit(line: str) -> None:
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def extract_pcm(ffmpeg: str, path: Path, offset: float, dur: float):
    import numpy as np

    cmd = [ffmpeg, "-v", "error", "-nostdin"]
    if offset > 0:
        cmd += ["-ss", f"{offset:.3f}"]
    cmd += ["-i", str(path), "-vn", "-ac", "1", "-ar", str(SR), "-t", f"{dur:.3f}",
            "-f", "s16le", "-"]
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "ignore")[:300])
    return np.frombuffer(p.stdout, dtype=np.int16).astype("float32") / 32768.0


def main() -> int:
    video = Path(sys.argv[1])
    offsets = [float(x) for x in (sys.argv[2].split(",") if len(sys.argv) > 2 else ["120", "600", "900"])]
    ffmpeg = shutil.which("ffmpeg") or r"C:\Users\admin\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0-full_build\bin\ffmpeg.exe"

    import torch
    from qwen_asr import Qwen3ASRModel

    models = {
        "0.6B": APP_DIR / "models" / "Qwen3-ASR-0.6B",
        "1.7B": APP_DIR / "models" / "Qwen3-ASR-1.7B",
    }
    aligner = str(APP_DIR / "models" / "Qwen3-ForcedAligner-0.6B")

    # 预抽音频，保证两个模型看的是同一段
    clips = {off: extract_pcm(ffmpeg, video, off, 25.0) for off in offsets}
    emit(f"视频 {video.name}  片段 {offsets}（各 25s）")

    results = {}
    for name, mdir in models.items():
        if not mdir.exists():
            emit(f"跳过 {name}：目录不存在 {mdir}")
            continue
        emit(f"\n===== 加载 {name} ({mdir.name}) =====")
        t0 = time.perf_counter()
        m = Qwen3ASRModel.from_pretrained(
            str(mdir), dtype="bfloat16", device_map="cuda:0",
            forced_aligner=aligner,
            forced_aligner_kwargs=dict(dtype="bfloat16", device_map="cuda:0"),
        )
        load_s = time.perf_counter() - t0
        emit(f"加载耗时 {load_s:.1f}s  显存 {torch.cuda.memory_allocated()/1024**3:.2f} GB")
        for off in offsets:
            pcm = clips[off]
            t0 = time.perf_counter()
            r = m.transcribe(audio=(pcm, SR), context="", language="Japanese",
                             return_time_stamps=True)[0]
            dt = time.perf_counter() - t0
            txt = (r.text or "").strip()
            results.setdefault(off, {})[name] = (txt, dt)
            emit(f"  [{off:6.0f}s] {dt:5.1f}s  {txt}")
        del m
        torch.cuda.empty_cache()

    emit("\n===== 逐段对比 =====")
    for off in offsets:
        a = results.get(off, {}).get("0.6B")
        b = results.get(off, {}).get("1.7B")
        emit(f"[{off:6.0f}s]")
        emit(f"   0.6B: {a[0] if a else '-'}")
        emit(f"   1.7B: {b[0] if b else '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
