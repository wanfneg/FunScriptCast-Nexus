# -*- coding: utf-8 -*-
"""纯转录 A/B（R64）：kotoba-whisper vs Qwen3-ASR-0.6B,只对比日文原文,不跑翻译。

用户口径:切句一致（whisper_scheme 的 cut_rms,与 R61-R63 同一定界口径）,
两个 ASR 各转一遍,逐段做文本差异分析——谁的日文更准没法自动判（无日文参考）,
但能量化:一致率、分歧段数、长度差、复读段数,并把最大分歧段吐出来供人工定性。

用法（kotoba 需本地 HF 缓存）:
  HF_HOME=models/hf-cache HF_HUB_CACHE=models/hf-cache/hub \
    .venv/Scripts/python.exe tests/compare_asr_ja.py --video sivr002
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import subprocess
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "vendor" / "subtitle"))

from whisper_scheme import cut_rms  # noqa: E402  同一定界口径

SR = 16000
DEFAULT_PCM = Path(r"E:/Development/_ref/eval/sivr002.pcm")
EVAL_DIR = Path(r"E:/Development/_ref/eval")
ACPP = Path(r"E:/audiocpp-portable/gpu/audiocpp_cli.exe")
QWEN_MODEL = Path(r"E:/audiocpp-portable/models/Qwen3-ASR-0.6B")
KOTOBA = "kotoba-tech/kotoba-whisper-v2.0-faster"


def wav_bytes(pcm) -> bytes:
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((pcm * 32767.0).astype("<i2").tobytes())
    return buf.getvalue()


def qwen_asr(pcm, tmp: Path) -> str:
    wav = tmp / f"cmp_{int(time.time() * 1000)}.wav"
    wav.write_bytes(wav_bytes(pcm))
    out = wav.with_suffix(".txt")
    r = subprocess.run(
        [str(ACPP), "--task", "asr", "--family", "qwen3_asr",
         "--model", str(QWEN_MODEL), "--backend", "cuda",
         "--language", "Japanese",
         "--audio", str(wav), "--text-out", str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=max(30.0, len(pcm) / SR * 2.0))
    try:
        if r.returncode != 0:
            raise RuntimeError(f"rc={r.returncode} {(r.stderr or '')[:150]}")
        return out.read_text(encoding="utf-8").strip()
    finally:
        for f in (wav, out):
            try:
                f.unlink()
            except OSError:
                pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pcm", default=str(DEFAULT_PCM))
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--sec", type=int, default=1254)
    ap.add_argument("--beam", type=int, default=1, help="kotoba 的 beam（生产=1）")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--top", type=int, default=20, help="输出最大分歧段数")
    args = ap.parse_args()

    import numpy as np
    raw = Path(args.pcm).read_bytes()
    lo, hi = args.start * SR * 2, (args.start + args.sec) * SR * 2
    raw = raw[lo:hi]
    pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    print(f"[1/4] 音频 {len(pcm) / SR:.1f}s", flush=True)

    spans = [(a, b) for a, b in cut_rms(pcm, np)]
    print(f"[2/4] RMS 定界 → {len(spans)} span", flush=True)

    from faster_whisper import WhisperModel
    model = WhisperModel(KOTOBA, device=args.device, compute_type="float16",
                         local_files_only=True)
    print(f"[3/5] 模型就绪：{KOTOBA}", flush=True)

    def norm(t: str) -> str:
        return (t or "").strip().replace(" ", "").replace("、", "").replace("。", "").replace("，", "")

    tmp = EVAL_DIR
    rows = []
    t0 = time.time()
    for si, (a, b) in enumerate(spans):
        sp = pcm[a:b]
        base_ms = (args.start * SR + a) * 1000 // SR
        # kotoba（生产参数：beam 1、无 prompt、不过 vad_filter 保留原始转写）
        gen, _ = model.transcribe(sp, language="ja", beam_size=args.beam,
                                  vad_filter=False, condition_on_previous_text=False,
                                  initial_prompt=None)
        kt = "".join((s.text or "").strip() for s in gen).strip()
        # qwen3-0.6B（audio.cpp 离线 asr,日语默认）
        qt = qwen_asr(sp, tmp).strip()
        ratio = difflib.SequenceMatcher(None, norm(kt), norm(qt)).ratio()
        rows.append({"i": si, "start_ms": base_ms, "dur_s": round(len(sp) / SR, 2),
                     "kotoba": kt, "qwen": qt, "ratio": round(ratio, 3)})
        if si % 40 == 0:
            print(f"  … {si}/{len(spans)}（{time.time() - t0:.0f}s）", flush=True)
    print(f"[4/5] 转写完成 {time.time() - t0:.1f}s", flush=True)

    n = len(rows)
    same = sum(1 for r in rows if norm(r["kotoba"]) == norm(r["qwen"]))
    near = sum(1 for r in rows if r["ratio"] >= 0.9)
    divergent = sorted(rows, key=lambda r: r["ratio"])[: args.top]
    kt_chars = sum(len(r["kotoba"]) for r in rows)
    qt_chars = sum(len(r["qwen"]) for r in rows)
    both_empty = sum(1 for r in rows if not r["kotoba"] and not r["qwen"])
    only_k = sum(1 for r in rows if r["kotoba"] and not r["qwen"])
    only_q = sum(1 for r in rows if r["qwen"] and not r["kotoba"])

    stats = {"spans": n, "一致(归一后)": same, "近似≥0.9": near,
             "平均相似度": round(sum(r["ratio"] for r in rows) / n, 3),
             "kotoba 总字数": kt_chars, "qwen 总字数": qt_chars,
             "仅 kotoba 有文本": only_k, "仅 qwen 有文本": only_q, "两者皆空": both_empty}
    print(json.dumps(stats, ensure_ascii=False, indent=1))
    print("\n===== 最大分歧段（供人工定性）=====")
    for r in divergent:
        print(f"@{r['start_ms'] / 1000:.1f}s ({r['dur_s']}s, 相似 {r['ratio']})")
        print(f"  kotoba: {r['kotoba'][:50]!r}")
        print(f"  qwen  : {r['qwen'][:50]!r}")

    out = EVAL_DIR / f"asr_ja_cmp_{args.start}_{args.sec}.json"
    out.write_text(json.dumps({"stats": stats, "rows": rows}, ensure_ascii=False),
                   encoding="utf-8")
    print(f"[5/5] 写出 {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
