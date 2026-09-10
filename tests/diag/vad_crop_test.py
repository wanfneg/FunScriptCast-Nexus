# -*- coding: utf-8 -*-
"""验证：只把 VAD 语音段送 ASR，能否消掉重复退化。

流程：audiocpp VAD 出 chunk 窗口 → ffmpeg 抽出每段 → 逐段 audiocpp ASR
      → 统计重复率，与原「整块送」的结果对比。

用法： .venv\\Scripts\\python.exe tests\\diag\\vad_crop_test.py <音频wav> [输出目录]
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

AUDIOCPP = Path(r"E:\audiocpp-portable")
CLI = AUDIOCPP / "cpu" / "audiocpp_cli.exe"
ASR_MODEL = AUDIOCPP / "models" / "Qwen3-ASR-0.6B"
VAD_MODEL = AUDIOCPP / "assets" / "framework" / "models" / "silero_vad"
SR = 16000
LOG = Path(__file__).resolve().parent.parent / "_vadcrop_log.txt"


def emit(line: str) -> None:
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def run(cmd: list, timeout: int = 1800) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def repetition_stats(text: str) -> dict:
    runs = [m for m in re.finditer(r"(.)\1{9,}", text)]
    longest = max((len(m.group(0)) for m in runs), default=0)
    return {"len": len(text), "runs_ge10": len(runs), "longest_run": longest}


def main() -> int:
    audio = Path(sys.argv[1])
    outdir = Path(sys.argv[2]) if len(sys.argv) > 2 else AUDIOCPP / "tmp" / "vadcrop"
    ffmpeg = shutil.which("ffmpeg") or r"C:\Users\admin\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0-full_build\bin\ffmpeg.exe"
    outdir.mkdir(parents=True, exist_ok=True)

    emit(f"输入音频 {audio.name}")

    # 1) VAD
    chunks_json = outdir / "chunks.json"
    t0 = time.perf_counter()
    r = run([str(CLI), "--task", "vad", "--family", "silero_vad", "--model", str(VAD_MODEL),
             "--backend", "cpu", "--audio", str(audio), "--vad-chunks-out", str(chunks_json)])
    vad_s = time.perf_counter() - t0
    if not chunks_json.exists():
        emit("VAD 失败：" + (r.stderr or r.stdout)[-400:])
        return 1
    chunks = json.loads(chunks_json.read_text(encoding="utf-8"))
    speech_s = sum((c["end_sample"] - c["start_sample"]) for c in chunks) / SR
    total_s = subprocess.run([ffmpeg, "-v", "error", "-i", str(audio), "-f", "null", "-"],
                             capture_output=True)
    emit(f"VAD: {len(chunks)} 块 / 语音 {speech_s:.1f}s / 耗时 {vad_s:.1f}s")

    # 2) 逐块抽音频 + ASR
    texts = []
    asr_s = 0.0
    for i, c in enumerate(chunks):
        a = c["start_sample"] / SR
        dur = (c["end_sample"] - c["start_sample"]) / SR
        seg = outdir / f"seg{i:03d}.wav"
        run([ffmpeg, "-v", "error", "-nostdin", "-ss", f"{a:.3f}", "-i", str(audio),
             "-t", f"{dur:.3f}", "-ar", str(SR), "-ac", "1", "-y", str(seg)])
        if not seg.exists():
            continue
        txt_out = outdir / f"seg{i:03d}.txt"
        t0 = time.perf_counter()
        run([str(CLI), "--task", "asr", "--family", "qwen3_asr", "--model", str(ASR_MODEL),
             "--backend", "cpu", "--threads", "8", "--audio", str(seg),
             "--language", "Japanese", "--text-out", str(txt_out)])
        asr_s += time.perf_counter() - t0
        txt = txt_out.read_text(encoding="utf-8").strip() if txt_out.exists() else ""
        if txt:
            texts.append(txt)
            emit(f"  [{i:03d}] {a:7.1f}s +{dur:4.1f}s  {txt}")

    joined = "".join(texts)
    st = repetition_stats(joined)
    emit("")
    emit("===== 结果 =====")
    emit(f"  块数            {len(texts)}")
    emit(f"  合并文本长度     {st['len']} 字符")
    emit(f"  ≥10 连重复串     {st['runs_ge10']}")
    emit(f"  最长连续重复     {st['longest_run']}")
    emit(f"  ASR 总耗时       {asr_s:.1f}s（语音净时长 {speech_s:.1f}s，RTF {asr_s/max(0.01,speech_s):.3f}）")
    emit("")
    emit("  合并文本：")
    emit("  " + joined[:800])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
