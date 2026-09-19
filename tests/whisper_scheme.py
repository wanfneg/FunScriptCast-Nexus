# -*- coding: utf-8 -*-
"""Whisper 系 ASR + 内容自适应切句 —— 与"定长块"管线做 A/B 的正式工具。

## 它测的是什么

参考项目 realtime-subtitle（https://github.com/Vanyoo/realtime-subtitle）的方案：
ASH 层换成 **Whisper 系**，切句不用定长块，而是**在静音处切、并有最短/最长限制**，
翻译逐句带上下文。它的切句规则读自 `main.py:150-217` + `config.py` 默认值：

    is_silence   = 缓冲尾部 1.0s 的 RMS < 0.01
    standard_cut = is_silence 且 缓冲 > 2.0s          ← 最常见
    hard_cut     = 缓冲 > max_phrase_duration(5.0s)   ← 兜底
    （文档里的 soft limit(>6.0s) 永远不会触发：hard 5.0s 先到，是上游的死代码）
    整块 RMS < 阈值 → 整块跳过（防静音幻觉）

为什么值得单独做成工具：现有链路的"定长块 + 服务端 VAD"框架下，3s/25s 两档各有取舍
（3s 时序准但云端追不上、25s 云端能用但延迟 30s+）。**内容自适应切句把这两档的矛盾
直接消掉** —— 块边界由内容决定，不再需要在"延迟"和"追得上"之间选。

## 用法

    # 它的方案（默认：RMS 切句 + 幻觉过滤 + 上一句作 prompt），跑完直接打分
    python tests/whisper_scheme.py --tag R43whisper --score

    # 消融：prompt 回传内容的影响（实测 both 会诱发 Whisper 的拉丁幻觉）
    python tests/whisper_scheme.py --tag R43noprompt --prompt-mode none --score

    # 切句方式对照：Whisper 自带 VAD 整片切（粗切，段长可达十几秒）
    python tests/whisper_scheme.py --tag R43vad --cut vad --score

产物：`<eval-dir>/eval_<tag>.json`（与 run_eval.py 同一格式，可被
compare_with_reference.py 直接评分、也可与历史结果并排比较）。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUB = ROOT / "vendor" / "subtitle"
sys.path.insert(0, str(SUB))

SR = 16000
DEFAULT_PCM = Path(r"E:/Development/_ref/eval/sivr001.pcm")
DEFAULT_SRT = Path(r"E:/testvideo/SIVR-001-002 Yua Mikami/SIVR-001.srt")
DEFAULT_EVAL_DIR = Path(r"E:/Development/_ref/eval")

# ---- realtime-subtitle 的默认参数（config.py），改这里就是改"它的口径" ----
SILENCE_THRESHOLD = 0.01      # RMS，float [-1,1]
SILENCE_DURATION = 1.0        # 尾部静音判据长度
MIN_PHRASE_SEC = 2.0          # standard_cut 的最短块
MAX_PHRASE_SEC = 5.0          # hard_cut 的上限
STEP_SEC = 0.2                # 它的累积步进（streaming_step_size）


def cut_rms(pcm, np):
    """它的切句规则 → [(start_sample, end_sample)]。"""
    cuts, buf_start, i = [], 0, 0
    step_n = int(STEP_SEC * SR)
    while i < len(pcm):
        i += step_n
        end = min(i, len(pcm))
        dur = (end - buf_start) / SR
        if dur <= 0:
            continue
        is_sil = False
        if dur > SILENCE_DURATION:
            tail = pcm[end - int(SILENCE_DURATION * SR):end]
            is_sil = float(np.sqrt(np.mean(tail ** 2))) < SILENCE_THRESHOLD
        if ((is_sil and dur > MIN_PHRASE_SEC) or dur > MAX_PHRASE_SEC) and dur > 0.5:
            chunk = pcm[buf_start:end]
            # 整块都是静音就跳过：它的原话是 "Prevent infinite loop of repeating prompt"
            if float(np.sqrt(np.mean(chunk ** 2))) >= SILENCE_THRESHOLD:
                cuts.append((buf_start, end))
            buf_start = end
    if len(pcm) - buf_start > int(0.5 * SR):
        cuts.append((buf_start, len(pcm)))
    return cuts


def cut_blocks(pcm, np, chunk_sec: int, overlap_sec: int):
    """**生产等价的定长块**（3s/1s 重叠）→ [(start_sample, end_sample, keep_from_ms)]。

    为什么要单独有这一档：头显现在按固定块长上传，PC 不知道"下一句什么时候来"，
    所以切句只能在客户端做。如果**只换 ASR、不动上传协议**就能拿到大部分收益，
    那就完全不用改头显；这一档就是用来回答这个问题的。
    keep_from_ms 与生产同义：块起点 + overlap/2，交给 keep_segment 去重。
    """
    out = []
    step = int((chunk_sec - overlap_sec) * SR)
    span = int(chunk_sec * SR)
    half_ms = int(overlap_sec * 1000 / 2)
    i = 0
    while i < len(pcm):
        end = min(i + span, len(pcm))
        if (end - i) / SR < 0.1:
            break
        out.append((i, end, int(i / SR * 1000) + half_ms))
        i += step
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pcm", default=str(DEFAULT_PCM))
    ap.add_argument("--srt", default=str(DEFAULT_SRT))
    ap.add_argument("--eval-dir", default=str(DEFAULT_EVAL_DIR))
    ap.add_argument("--start", type=int, default=0, help="起点（秒）")
    ap.add_argument("--sec", type=int, default=1254, help="时长（秒）")
    ap.add_argument("--tag", default="whisper")
    ap.add_argument("--model", default="kotoba-tech/kotoba-whisper-v2.0-faster",
                    help="faster-whisper 模型；kotoba 是日文特化（本机已缓存）")
    ap.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
    ap.add_argument("--compute-type", default="float16")
    ap.add_argument("--cut", default="rms", choices=["rms", "blocks", "vad"],
                    help="rms=它的内容自适应切句；blocks=生产等价的定长块（模拟头显现有"
                         "上传协议，用来回答'只换 ASR 不动头显能否拿到收益'）；"
                         "vad=Whisper 自带 VAD 整片切（粗切对照）")
    ap.add_argument("--chunk-sec", type=int, default=3, help="--cut blocks 的块长（生产=3）")
    ap.add_argument("--overlap-sec", type=int, default=1, help="--cut blocks 的重叠（生产=1）")
    ap.add_argument("--no-filter", action="store_true",
                    help="关掉幻觉过滤（复读/热词回显/拉丁幻觉），用于量化过滤的贡献")
    ap.add_argument("--prompt-mode", default="none", choices=["none", "both"],
                    help="ASR prompt 回传内容：none=不给（生产铁律）；"
                         "both=上一句原文（热词机制）。实测 both 会诱发 Whisper 的"
                         "拉丁幻觉（`.`, `Thank`, `I`, `you`），none 则一段都没有")
    ap.add_argument("--score", action="store_true", help="跑完直接调 compare_with_reference.py 打分")
    args = ap.parse_args()

    import numpy as np
    eval_dir = Path(args.eval_dir)
    out_json = eval_dir / f"eval_{args.tag}.json"

    # ---------------------------------------------------------------- 读音频
    raw = Path(args.pcm).read_bytes()
    lo, hi = args.start * SR * 2, (args.start + args.sec) * SR * 2
    raw = raw[lo:hi]
    pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    print(f"[1/5] 音频 {len(pcm)/SR:.1f}s（{args.start}s 起）", flush=True)

    # ---------------------------------------------------------------- 切句
    # spans: [(起, 止, keep_from_ms)]；None = Whisper 自带 VAD 整片切
    if args.cut == "rms":
        spans = [(a, b, 0) for a, b in cut_rms(pcm, np)]
        durs = [(b - a) / SR for a, b, _ in spans]
        print(f"[2/5] 它的 RMS 切句 → {len(spans)} 块 中位={np.median(durs):.1f}s "
              f"[{min(durs):.1f},{max(durs):.1f}]", flush=True)
    elif args.cut == "blocks":
        spans = cut_blocks(pcm, np, args.chunk_sec, args.overlap_sec)
        print(f"[2/5] 生产等价定长块 {args.chunk_sec}s/{args.overlap_sec}s 重叠 → "
              f"{len(spans)} 块（模拟头显现有的上传协议，PC 侧不参与切句）", flush=True)
    else:
        spans = None
        print("[2/5] Whisper 自带 VAD 整片切（粗切对照）", flush=True)

    # ---------------------------------------------------------------- ASR
    from faster_whisper import WhisperModel
    t0 = time.time()
    model = WhisperModel(args.model, device=args.device,
                         compute_type=args.compute_type, local_files_only=True)
    print(f"[3/5] 模型加载 {time.time()-t0:.1f}s  {args.model}", flush=True)

    # 术语表已整体移除（Round 53）：prompt 只剩"上一句原文"（--prompt-mode both）。
    CFG = json.loads((SUB / "config.json").read_text(encoding="utf-8"))
    ctx_terms = ""

    from text_filters import (has_repetition_loop, is_latin_hallucination,
                              is_prompt_echo, strip_wrap_quotes)

    t0 = time.time()
    segs: list[dict] = []
    dropped = {"repetition": 0, "prompt_echo": 0, "latin": 0}
    prev_text = ""          # 上一句终稿 → 作为 Whisper initial_prompt（它的 last_final_text）

    def transcribe_span(a: int, b: int, base_s: float):
        prompt = ""
        if args.prompt_mode == "both" and prev_text:
            prompt = prev_text[:200]
        out, _ = model.transcribe(
            pcm[a:b], language="ja", beam_size=5,
            vad_filter=(args.cut != "rms"),
            vad_parameters={"min_silence_duration_ms": 300} if args.cut != "rms" else None,
            initial_prompt=prompt or None,
            condition_on_previous_text=False)
        return list(out)

    from text_filters import keep_segment            # 跨块去重判据（与生产共用）

    if spans is None:
        for s in transcribe_span(0, len(pcm), 0.0):
            segs.append({"start_ms": int(s.start * 1000), "end_ms": int(s.end * 1000),
                         "text": (s.text or "").strip()})
    else:
        for a, b, keep_from in spans:
            base_s = a / SR
            for s in transcribe_span(a, b, base_s):
                s0 = int((base_s + s.start) * 1000)
                s1 = int((base_s + s.end) * 1000)
                # 定长块有重叠 ⇒ 必须走生产的同一套去重判据，否则重叠区的句子出两次
                if not keep_segment(s0, s1, keep_from):
                    continue
                segs.append({"start_ms": s0, "end_ms": s1,
                             "text": (s.text or "").strip()})
    asr_s = time.time() - t0

    # -------- 幻觉过滤（与生产同一套判据；生产里这层在 ASR 后端内部）
    kept = []
    for s in segs:
        t = strip_wrap_quotes(s["text"])
        if not t:
            continue
        if not args.no_filter:
            if has_repetition_loop(t):
                dropped["repetition"] += 1
                continue
            # ⚠ 只在**真的把 prev_text 当 prompt 发出去**时才判回显：--prompt-mode none
            # 时模型根本没看到它，拿它做判据会把"恰巧与上一句同尾"的正常短句丢掉
            # （audiocpp 侧犯过同一个错，R42 已修）。
            if args.prompt_mode == "both" and prev_text and is_prompt_echo(t, prev_text):
                dropped["prompt_echo"] += 1
                continue
            # 拉丁幻觉：日语音频里"没有假名也没有汉字"的短输出。实测本片 Whisper 会吐
            # `.`/`Thank`×2/`I`/`you`/`2`×2 —— 而且**只在开了 prompt 回传时出现**
            # （关掉后同一音频 0 段），是 initial_prompt 把解码器往字幕腔上带的结果。
            if is_latin_hallucination(t, "ja"):
                dropped["latin"] += 1
                continue
        s["text"] = t
        kept.append(s)
        if len(t.split()) > 1 or len(t) > 8:      # 与它一致的"够长才更新上下文"
            prev_text = t[:80]
    segs = kept
    print(f"[3/5] 转写 {asr_s:.1f}s → 保留 {len(segs)} 段；过滤丢弃 "
          f"复读{dropped['repetition']} "
          f"prompt回显{dropped['prompt_echo']} 拉丁幻觉{dropped['latin']}", flush=True)
    if dropped["latin"]:
        print("      ⚠ initial_prompt 会诱发 Whisper 的英文幻觉（实测：给 prompt 约 6~7 段/片，"
              "不给则 0 段）。要根治请用 --prompt-mode none。", flush=True)

    # ---------------------------------------------------------------- 翻译
    # 沿用生产同一套 Sakura MT（保持"翻译"这个变量不变，只变 ASR 与切句）
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
            print(f"    批 {i//batch} 翻译异常：{type(e).__name__}: {e}", flush=True)
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
        "meta": {"tag": args.tag, "scheme": "realtime-subtitle 复现",
                 "asr": args.model, "device": f"{args.device}/{args.compute_type}",
                 "cut": args.cut, "filter": not args.no_filter,
                 "prompt_mode": args.prompt_mode,
                 "translate": tr.backend, "asr_sec": round(asr_s, 1),
                 "mt_sec": round(mt_s, 1), "empty_zh": empty,
                 "dropped": dropped},
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
