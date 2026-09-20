# -*- coding: utf-8 -*-
"""混合切句原型（R62）：RMS 静音定界 + silero VAD 语音起点赋时。

R61 实测出的两条"单项冠军"：RMS 的句界质量（长度比 1.00 / 零越界污染）与
生产 VAD 的时序（+30~50ms，来自 silero 实测语音起点）。本工具把两者拼在一起，
离线量出"混合方案"的实测天花板，决定值不值得做进服务端：

  1. cut_rms（import 自 whisper_scheme，与 R61 完全同一定界口径）切出句子 span；
  2. 每个 span 用生产同款 silero VAD（audiocpp CLI，默认参数）量语音区；
  3. faster-whisper 转 span，**不开 vad_filter**（R61：块内 VAD 修剪是气声漏识
     的机制候选，RMS 档不修剪所以 sivr001 多捞 3 句——这里沿用不修剪）；
  4. 赋时：whisper 段按顺序对到 VAD 语音组（间隙<0.3s 的区先合并），起止取
     VAD 实测值；对不上时兜底 whisper 预测时间并计数。

用法（HF 缓存必须指仓库 models/hf-cache，R61 教训）：
  HF_HOME=models/hf-cache HF_HUB_CACHE=models/hf-cache/hub \
    .venv/Scripts/python.exe tests/hybrid_scheme.py --tag r62hybrid --score
产物 eval_<tag>.json 与 run_eval.py 同格式，可直接 compare_with_reference.py 评分。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "vendor" / "subtitle"))

from whisper_scheme import cut_rms  # noqa: E402  同一切句实现，定界口径与 R61 一致

SR = 16000
DEFAULT_PCM = Path(r"E:/Development/_ref/eval/sivr001.pcm")
DEFAULT_SRT = Path(r"E:/testvideo/SIVR-001-002 Yua Mikami/SIVR-001.srt")
DEFAULT_EVAL_DIR = Path(r"E:/Development/_ref/eval")
ACPP = Path(r"E:/audiocpp-portable/gpu/audiocpp_cli.exe")
VAD_MODEL = Path(r"E:/audiocpp-portable/assets/framework/models/silero_vad")
REGION_MERGE_GAP = 0.3   # 语音区间隙小于此值并成同一句（秒），句内短停顿不拆


def vad_regions(pcm, tmp: Path) -> list:
    """生产同款 silero VAD → [(start_sec, end_sec)]，相对本片段。"""
    wav = tmp / f"hyb_{int(time.time() * 1000)}.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((pcm * 32768.0).astype("<i2").tobytes())
    out = wav.with_suffix(".chunks.json")
    try:
        r = subprocess.run(
            [str(ACPP), "--task", "vad", "--family", "silero_vad",
             "--model", str(VAD_MODEL), "--backend", "cpu",
             "--audio", str(wav), "--vad-chunks-out", str(out)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=max(30.0, len(pcm) / SR * 2.0))
        if r.returncode != 0 or not out.exists():
            raise RuntimeError(f"rc={r.returncode} {(r.stderr or '')[:200]}")
        data = json.loads(out.read_text(encoding="utf-8"))
    finally:
        for f in (wav, out):
            try:
                f.unlink()
            except OSError:
                pass
    return [(float(c["start_sample"]) / SR, float(c["end_sample"]) / SR)
            for c in data if float(c["end_sample"]) > float(c["start_sample"])]


def merge_regions(regions, gap: float = REGION_MERGE_GAP) -> list:
    if not regions:
        return []
    out = [list(regions[0])]
    for a, b in regions[1:]:
        if a - out[-1][1] <= gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [tuple(x) for x in out]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pcm", default=str(DEFAULT_PCM))
    ap.add_argument("--srt", default=str(DEFAULT_SRT))
    ap.add_argument("--eval-dir", default=str(DEFAULT_EVAL_DIR))
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--sec", type=int, default=1254)
    ap.add_argument("--tag", default="hybrid")
    ap.add_argument("--model", default="kotoba-tech/kotoba-whisper-v2.0-faster")
    ap.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
    ap.add_argument("--compute-type", default="float16")
    ap.add_argument("--no-filter", action="store_true")
    ap.add_argument("--score", action="store_true")
    args = ap.parse_args()

    import numpy as np
    eval_dir = Path(args.eval_dir)
    out_json = eval_dir / f"eval_{args.tag}.json"

    raw = Path(args.pcm).read_bytes()
    lo, hi = args.start * SR * 2, (args.start + args.sec) * SR * 2
    raw = raw[lo:hi]
    pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    print(f"[1/5] 音频 {len(pcm) / SR:.1f}s（{args.start}s 起）", flush=True)

    spans = [(a, b) for a, b in cut_rms(pcm, np)]
    durs = [(b - a) / SR for a, b in spans]
    print(f"[2/5] RMS 定界 → {len(spans)} span 中位={np.median(durs):.1f}s "
          f"[{min(durs):.1f},{max(durs):.1f}]", flush=True)

    from faster_whisper import WhisperModel
    t0 = time.time()
    model = WhisperModel(args.model, device=args.device,
                         compute_type=args.compute_type, local_files_only=True)
    print(f"[3/5] 模型加载 {time.time() - t0:.1f}s  {args.model}", flush=True)

    from text_filters import has_repetition_loop, is_latin_hallucination, strip_wrap_quotes

    t_vad = 0.0
    segs = []
    dropped = {"repetition": 0, "latin": 0}
    n_retimed = n_fallback = n_notext = 0
    t0 = time.time()
    for si, (a, b) in enumerate(spans):
        sp = pcm[a:b]
        base = a / SR
        t1 = time.time()
        try:
            groups = merge_regions(vad_regions(sp, eval_dir))
        except Exception as e:
            print(f"    span{si} VAD 异常：{type(e).__name__}: {e}", flush=True)
            groups = []
        t_vad += time.time() - t1

        gen, _ = model.transcribe(sp, language="ja", beam_size=5, vad_filter=False,
                                  condition_on_previous_text=False, initial_prompt=None)
        wsegs = [(float(s.start), float(s.end), (s.text or "").strip())
                 for s in gen if (s.text or "").strip()]
        if not wsegs:
            n_notext += 1
            continue

        def abs_ms(rel: float) -> int:
            return int((base + rel) * 1000)

        pieces = []
        if len(groups) == len(wsegs):
            for (ga, gb), (_, _, t) in zip(groups, wsegs):
                pieces.append((abs_ms(ga), abs_ms(gb), t))
            n_retimed += len(pieces)
        elif len(groups) == 1:
            ga, gb = groups[0]
            pieces.append((abs_ms(ga), abs_ms(gb), "".join(t for _, _, t in wsegs)))
            n_retimed += 1
        else:
            # 段/组数对不上：兜底用 whisper 预测时间（计数，供评估赋时器成熟度）
            for wa, wb, t in wsegs:
                pieces.append((abs_ms(wa), abs_ms(wb), t))
            n_fallback += len(pieces)

        for s0, s1, t in pieces:
            t = strip_wrap_quotes(t)
            if not t:
                continue
            if not args.no_filter:
                if has_repetition_loop(t):
                    dropped["repetition"] += 1
                    continue
                if is_latin_hallucination(t, "ja"):
                    dropped["latin"] += 1
                    continue
            segs.append({"start_ms": s0, "end_ms": s1, "text": t})
    asr_s = time.time() - t0
    print(f"[3/5] VAD+转写 {asr_s:.1f}s（其中 VAD {t_vad:.1f}s）→ 保留 {len(segs)} 段；"
          f"赋时 VAD={n_retimed} 兜底whisper={n_fallback} 空span={n_notext}；过滤丢弃 "
          f"复读{dropped['repetition']} 拉丁幻觉{dropped['latin']}", flush=True)

    # ---------------------------------------------------------------- 翻译
    # 与生产/whisper_scheme 同一套 Sakura MT + 上一句上下文，保持翻译变量不变
    CFG = json.loads((ROOT / "vendor" / "subtitle" / "config.json").read_text(encoding="utf-8"))
    from translate_engine import Translator
    from stream_bridge import _display_zh
    tr = Translator(CFG.get("translate", {}))
    batch = max(1, int((CFG.get("translate") or {}).get("batch_size", 10)))
    last_src = last_zh = ""
    t0 = time.time()
    for i in range(0, len(segs), batch):
        grp = segs[i:i + batch]
        ctx = ""
        if last_src or last_zh:
            ctx = ("以下是上一句的原文与译文，仅供理解剧情衔接；不要翻译或输出它们：\n"
                   f"上一句原文：{last_src}\n上一句译文：{last_zh}")
        try:
            tr.translate_segments(grp, "ja", ctx)
        except Exception as e:
            print(f"    批 {i // batch} 翻译异常：{type(e).__name__}: {e}", flush=True)
        for s in grp:
            s["translation"] = _display_zh(s)
        for s in reversed(grp):
            if (s.get("translation") or "").strip():
                last_src, last_zh = (s.get("text") or "")[:80], (s.get("translation") or "")[:80]
                break
    mt_s = time.time() - t0
    empty = sum(1 for s in segs if not (s.get("translation") or "").strip())
    print(f"[4/5] 翻译 {mt_s:.1f}s  空译文 {empty}/{len(segs)}", flush=True)

    eval_dir.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps({
        "segments": segs,
        "meta": {"tag": args.tag, "scheme": "hybrid RMS定界+VAD赋时",
                 "asr": args.model, "device": f"{args.device}/{args.compute_type}",
                 "filter": not args.no_filter,
                 "translate": tr.backend, "asr_sec": round(asr_s, 1),
                 "vad_sec": round(t_vad, 1), "mt_sec": round(mt_s, 1),
                 "empty_zh": empty, "dropped": dropped,
                 "retimed_vad": n_retimed, "fallback_whisper": n_fallback,
                 "spans": len(spans)},
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[5/5] 写出 {out_json}", flush=True)

    if args.score:
        print("\n" + "=" * 60, flush=True)
        subprocess.run([str(ROOT / ".venv" / "Scripts" / "python.exe"),
                        str(ROOT / "tests" / "compare_with_reference.py"),
                        args.srt, str(out_json)], cwd=str(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
