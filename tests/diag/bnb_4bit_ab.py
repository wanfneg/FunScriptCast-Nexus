# -*- coding: utf-8 -*-
"""Qwen3-ASR-1.7B：bf16 vs bitsandbytes 4-bit 对比（显存 / 耗时 / 输出）。

用法： .venv\\Scripts\\python.exe tests\\diag\\bnb_4bit_ab.py <视频> [偏移秒,偏移秒,...]
"""
from __future__ import annotations

import gc
import shutil
import subprocess
import sys
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent.parent
LOG = APP_DIR / "tests" / "_bnb_ab_log.txt"
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


def vram_gb() -> float:
    import torch

    return torch.cuda.memory_allocated() / 1024 ** 3


def main() -> int:
    video = Path(sys.argv[1])
    offsets = [float(x) for x in (sys.argv[2].split(",") if len(sys.argv) > 2 else ["120", "600", "900"])]
    ffmpeg = shutil.which("ffmpeg") or r"C:\Users\admin\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0-full_build\bin\ffmpeg.exe"

    import torch
    from transformers import BitsAndBytesConfig
    from qwen_asr import Qwen3ASRModel

    mdir = str(APP_DIR / "models" / "Qwen3-ASR-1.7B")
    aligner = str(APP_DIR / "models" / "Qwen3-ForcedAligner-0.6B")

    clips = {off: extract_pcm(ffmpeg, video, off, 25.0) for off in offsets}
    emit(f"视频 {video.name} | 片段 {offsets}（各 25s）| 模型 Qwen3-ASR-1.7B")
    emit(f"GPU {torch.cuda.get_device_name(0)}  显存 {torch.cuda.get_device_properties(0).total_memory/1024**3:.1f} GB")

    configs = {
        "bf16": {},
        "int4_fp16": {"quantization_config": BitsAndBytesConfig(
            load_in_4bit=True, bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.float16, bnb_4bit_use_double_quant=True)},
        "int4_bf16": {"quantization_config": BitsAndBytesConfig(
            load_in_4bit=True, bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16, bnb_4bit_use_double_quant=True)},
    }

    results: dict = {}
    for name, kwargs in configs.items():
        emit(f"\n===== {name} =====")
        torch.cuda.empty_cache()
        gc.collect()
        try:
            t0 = time.perf_counter()
            m = Qwen3ASRModel.from_pretrained(
                mdir,
                device_map="cuda:0" if name == "bf16" else {"": 0},
                dtype=torch.bfloat16 if name == "bf16" else None,
                forced_aligner=aligner,
                forced_aligner_kwargs=dict(dtype="bfloat16", device_map="cuda:0"),
                **kwargs,
            )
            load_s = time.perf_counter() - t0
            emit(f"加载 {load_s:.1f}s | 显存 {vram_gb():.2f} GB")
        except Exception as e:
            emit(f"加载失败：{type(e).__name__}: {e}")
            results[name] = None
            continue

        rows = {}
        for off in offsets:
            torch.cuda.empty_cache()
            t0 = time.perf_counter()
            r = m.transcribe(audio=(clips[off], SR), context="", language="Japanese",
                             return_time_stamps=True)[0]
            dt = time.perf_counter() - t0
            txt = (r.text or "").strip()
            rows[off] = (txt, dt)
            emit(f"  [{off:6.0f}s] {dt:5.1f}s  显存 {vram_gb():.2f} GB  {txt}")
        results[name] = rows
        del m
        gc.collect()
        torch.cuda.empty_cache()

    emit("\n===== 逐段对比 =====")
    for off in offsets:
        emit(f"[{off:6.0f}s]")
        for name in configs:
            r = results.get(name)
            if not r:
                emit(f"   {name:5s}: (未加载)")
                continue
            txt, dt = r[off]
            emit(f"   {name:5s}: {dt:5.1f}s  {txt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
