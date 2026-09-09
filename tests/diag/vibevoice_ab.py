# -*- coding: utf-8 -*-
"""VibeVoice-ASR-HF 4-bit 实测：显存 / 耗时 / 转写质量。

独立环境运行（transformers>=5.3 + torch cu128）：
  .venv-vibevoice\\Scripts\\python.exe tests\\diag\\vibevoice_ab.py <视频> [偏移秒,...]
"""
from __future__ import annotations

import gc
import shutil
import subprocess
import sys
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent.parent
LOG = APP_DIR / "tests" / "_vv_ab_log.txt"
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
    model_dir = sys.argv[3] if len(sys.argv) > 3 else str(APP_DIR / "models" / "hf" / "hub" /
                                                          "models--microsoft--VibeVoice-ASR-HF" /
                                                          "snapshots" / "f22241c2062b3b25272bf117397e03d73381037a")

    import torch
    from transformers import AutoProcessor, BitsAndBytesConfig, VibeVoiceAsrForConditionalGeneration

    clips = {off: extract_pcm(ffmpeg, video, off, 25.0) for off in offsets}
    emit(f"视频 {video.name} | 片段 {offsets}（各 25s）")
    emit(f"模型 {model_dir}")
    emit(f"GPU {torch.cuda.get_device_name(0)} 显存 {torch.cuda.get_device_properties(0).total_memory/1024**3:.1f} GB")

    processor = AutoProcessor.from_pretrained(model_dir)
    emit("processor 加载完成")

    configs = {
        "bf16": {},
        "int4": {"quantization_config": BitsAndBytesConfig(
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
            if kwargs:
                model = VibeVoiceAsrForConditionalGeneration.from_pretrained(
                    model_dir, device_map={"": 0}, **kwargs)
            else:
                model = VibeVoiceAsrForConditionalGeneration.from_pretrained(
                    model_dir, dtype=torch.bfloat16, device_map="cuda:0")
            load_s = time.perf_counter() - t0
            emit(f"加载 {load_s:.1f}s | 显存 {torch.cuda.memory_allocated()/1024**3:.2f} GB")
        except Exception as e:
            emit(f"加载失败：{type(e).__name__}: {e}")
            results[name] = None
            continue

        rows = {}
        for off in offsets:
            try:
                t0 = time.perf_counter()
                inputs = processor(audio=clips[off], sampling_rate=SR, return_tensors="pt")
                inputs = {k: (v.to(model.device) if hasattr(v, "to") else v) for k, v in inputs.items()}
                with torch.no_grad():
                    out = model.generate(**inputs, max_new_tokens=1024)
                txt = processor.batch_decode(out, skip_special_tokens=True)[0]
                dt = time.perf_counter() - t0
                rows[off] = (txt, dt)
                emit(f"  [{off:6.0f}s] {dt:5.1f}s  显存 {torch.cuda.memory_allocated()/1024**3:.2f} GB")
                emit(f"      {txt[:400]}")
            except Exception as e:
                emit(f"  [{off:6.0f}s] 失败：{type(e).__name__}: {e}")
                rows[off] = ("<error>", 0.0)
        results[name] = rows
        del model
        gc.collect()
        torch.cuda.empty_cache()

    emit("\n===== 汇总 =====")
    for off in offsets:
        emit(f"[{off:6.0f}s]")
        for name in configs:
            r = results.get(name)
            if not r or off not in r:
                emit(f"   {name:5s}: (未跑)")
                continue
            txt, dt = r[off]
            emit(f"   {name:5s}: {dt:5.1f}s  {txt[:200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
