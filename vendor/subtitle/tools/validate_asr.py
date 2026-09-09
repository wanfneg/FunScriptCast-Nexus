#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Qwen3-ASR 服务端最小验证脚本（VRFunScriptCast AI 字幕 · 阶段 0）

做三件事：
  1. ffmpeg 抽音频 → 16kHz 单声道 PCM
  2. 按 chunk/overlap 切块，逐块送 Qwen3-ASR 转录（可选词级时间戳对齐）
  3. 输出带时间轴的 JSON + SRT，并打印耗时报告（RTF、单块延迟、显存峰值、lead 建议）

用法示例：
  python validate_asr.py --input D:/movie.mkv --lang ja \
      --model models/Qwen3-ASR-0.6B --aligner models/Qwen3-ForcedAligner-0.6B \
      --chunk 25 --overlap 2 --max-minutes 5
"""

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

SR = 16000
LANG_MAP = {"zh": "Chinese", "en": "English", "ja": "Japanese", "ko": "Korean", "yue": "Cantonese"}
SENT_END = "。！？!?…；;"


def log(*a):
    print(*a, flush=True)


def ffmpeg_exe(name="ffmpeg"):
    exe = shutil.which(name)
    if not exe:
        sys.exit(f"找不到 {name}，请先安装 ffmpeg 并加入 PATH")
    return exe


def probe_duration(path):
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    p = subprocess.run(
        [exe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(p.stdout.strip())
    except ValueError:
        return None


def extract_pcm(path, offset=0.0, duration=None):
    """ffmpeg 抽取 16kHz 单声道 float32 PCM。"""
    cmd = [ffmpeg_exe(), "-v", "error", "-nostdin"]
    if offset > 0:
        cmd += ["-ss", f"{offset:.3f}"]
    cmd += ["-i", str(path), "-vn", "-ac", "1", "-ar", str(SR)]
    if duration:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-f", "s16le", "-"]
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0:
        sys.exit("ffmpeg 抽音频失败: " + p.stderr.decode("utf-8", "ignore")[:500])
    if not p.stdout:
        sys.exit("ffmpeg 没有输出音频（文件无音轨？）")
    return np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def plan_chunks(total_sec, chunk_s, overlap_s):
    """返回 [(start, end), ...]，相邻块重叠 overlap_s；尾巴不足 1s 并入最后一块。"""
    step = max(0.5, chunk_s - overlap_s)
    out, t = [], 0.0
    while t < total_sec - 0.05:
        end = min(t + chunk_s, total_sec)
        out.append((t, end))
        if total_sec - end < 1.0:
            break
        t += step
    return out


def srt_time(t):
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def join_tokens(parts):
    """拼接 ASR token：中日文直接相连，英文/数字之间补空格。"""
    out = ""
    for t in parts:
        if not t:
            continue
        if out and out[-1].isascii() and out[-1].isalnum() and t[0].isascii() and t[0].isalnum():
            out += " "
        out += t
    return out


def group_sentences(stamps, base, max_dur=6.0, max_chars=40):
    """把词/字级时间戳按标点分句 → [(start, end, text), ...]（绝对时间，秒）。
    无标点的语言（如日语 ASR 输出）退化为按 max_dur / max_chars 切分。"""
    segs, buf, buf_start = [], [], None
    for st in stamps:
        if buf_start is None:
            buf_start = st.start_time
        buf.append(st.text)
        joined = join_tokens(buf).strip()
        hit_end = bool(st.text) and st.text[-1] in SENT_END
        if hit_end or (st.end_time - buf_start) >= max_dur or len(joined) >= max_chars:
            segs.append((base + buf_start, base + st.end_time, joined))
            buf, buf_start = [], None
    if buf:
        segs.append((base + buf_start, base + stamps[-1].end_time, join_tokens(buf).strip()))
    return [s for s in segs if s[2]]


def write_srt(segments, path):
    lines = []
    for i, (start, end, text) in enumerate(segments, 1):
        lines += [str(i), f"{srt_time(start)} --> {srt_time(end)}", text, ""]
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="Qwen3-ASR 分块转录验证")
    ap.add_argument("--input", required=True, help="视频/音频文件路径")
    ap.add_argument("--model", default="models/Qwen3-ASR-0.6B")
    ap.add_argument("--aligner", default="models/Qwen3-ForcedAligner-0.6B")
    ap.add_argument("--context", default="", help="热词/上下文提示（专有名词、行业术语），减少识别错误")
    ap.add_argument("--no-aligner", action="store_true", help="关闭词级时间戳对齐（只用块级时间）")
    ap.add_argument("--lang", default=None, help="zh/en/ja/ko…；留空自动识别")
    ap.add_argument("--chunk", type=float, default=25.0, help="块长（秒）")
    ap.add_argument("--overlap", type=float, default=2.0, help="块间重叠（秒）")
    ap.add_argument("--offset", type=float, default=0.0, help="从片源的第几秒开始测")
    ap.add_argument("--max-minutes", type=float, default=5.0, help="最多处理多少分钟音频")
    ap.add_argument("--device", default="cuda:0")
    ap.add_argument("--aligner-device", default=None,
                    help="对齐器单独指定设备（如 cpu：省约1.7GB显存，代价每块多几秒）")
    ap.add_argument("--dtype", default="bf16", choices=["bf16", "fp16", "fp32"])
    ap.add_argument("--out", default="out")
    ap.add_argument("--plan-only", action="store_true", help="只打印分块计划，不加载模型")
    args = ap.parse_args()

    src = Path(args.input)
    if not src.exists():
        sys.exit(f"输入文件不存在: {src}")

    total = probe_duration(src)
    log(f"[1/4] 片源 {src.name}  总时长 {total:.1f}s" if total else f"[1/4] 片源 {src.name}")

    take = args.max_minutes * 60.0
    if total:
        take = min(take, max(0.0, total - args.offset))
    log(f"      抽取 {args.offset:.1f}s → {args.offset + take:.1f}s 的音频（16kHz mono）")
    t0 = time.perf_counter()
    pcm = extract_pcm(src, args.offset, take)
    audio_sec = len(pcm) / SR
    log(f"      PCM 长度 {audio_sec:.1f}s，抽取耗时 {time.perf_counter() - t0:.1f}s")

    chunks = plan_chunks(audio_sec, args.chunk, args.overlap)
    log(f"[2/4] 分块: {len(chunks)} 块（chunk={args.chunk}s, overlap={args.overlap}s）")
    if args.plan_only:
        for i, (s, e) in enumerate(chunks):
            log(f"      #{i:02d}  {args.offset + s:8.2f}s → {args.offset + e:8.2f}s")
        return

    import torch
    from qwen_asr import Qwen3ASRModel

    dtype = {"bf16": torch.bfloat16, "fp16": torch.float16, "fp32": torch.float32}[args.dtype]
    kwargs = dict(dtype=dtype, device_map=args.device)
    use_aligner = not args.no_aligner and bool(args.aligner)
    if use_aligner:
        kwargs["forced_aligner"] = args.aligner
        kwargs["forced_aligner_kwargs"] = dict(
            dtype=dtype, device_map=args.aligner_device or args.device)
    if args.device.startswith("cuda"):
        torch.cuda.reset_peak_memory_stats()

    log(f"[3/4] 加载模型 {args.model}（aligner={use_aligner}）…")
    t0 = time.perf_counter()
    model = Qwen3ASRModel.from_pretrained(args.model, **kwargs)
    load_s = time.perf_counter() - t0
    log(f"      模型加载完成 {load_s:.1f}s")

    lang = LANG_MAP.get((args.lang or "").lower(), args.lang)
    segments, chunk_stats, texts = [], [], []
    for i, (cs, ce) in enumerate(chunks):
        piece = pcm[int(cs * SR):int(ce * SR)]
        t0 = time.perf_counter()
        r = model.transcribe(
            audio=(piece, SR),
            context=args.context,
            language=lang,
            return_time_stamps=use_aligner,
        )[0]
        dt = time.perf_counter() - t0
        chunk_stats.append({"idx": i, "start": args.offset + cs, "end": args.offset + ce,
                            "infer_s": round(dt, 3), "chars": len(r.text or ""),
                            "language": r.language})
        texts.append(r.text or "")
        log(f"      #{i:02d} [{args.offset + cs:7.2f}s] {dt:5.2f}s  {r.text}")

        if use_aligner and getattr(r, "time_stamps", None):
            new = group_sentences(r.time_stamps, args.offset + cs)
        else:
            new = [(args.offset + cs, args.offset + ce, (r.text or "").strip())]
        # 重叠区去重：每块只收"自己独占区"（chunk_start + overlap/2 之后）的内容
        own_start = args.offset + cs + (args.overlap / 2.0 if i > 0 else 0.0)
        for s, e, txt in new:
            if (s + e) / 2.0 >= own_start:
                segments.append((s, e, txt))

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    stem = f"{src.stem}_{Path(args.model).name}_chunk{int(args.chunk)}"
    json_path, srt_path = outdir / f"{stem}.json", outdir / f"{stem}.srt"
    json_path.write_text(json.dumps({
        "input": str(src), "offset": args.offset, "audio_sec": round(audio_sec, 2),
        "model": args.model, "aligner": args.aligner if use_aligner else None,
        "chunk": args.chunk, "overlap": args.overlap, "lang": args.lang,
        "segments": [{"start": round(s, 3), "end": round(e, 3), "text": t} for s, e, t in segments],
        "chunk_stats": chunk_stats,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    write_srt(segments, srt_path)

    infer = [c["infer_s"] for c in chunk_stats]
    total_infer = sum(infer)
    rtf = audio_sec / total_infer if total_infer else 0.0
    p90 = sorted(infer)[int(len(infer) * 0.9) - 1] if infer else 0.0
    vram = (torch.cuda.max_memory_allocated() / 1e9) if args.device.startswith("cuda") else 0.0
    lead = max(infer) + max(3.0, 0.15 * args.chunk)  # 单块最大延迟 + 翻译/网络余量

    log("")
    log("=" * 56)
    log("验证报告")
    log("=" * 56)
    log(f"音频时长      {audio_sec:.1f}s（{len(chunks)} 块）")
    log(f"模型加载      {load_s:.1f}s   显存峰值 {vram:.2f} GB")
    log(f"单块延迟      平均 {total_infer / len(infer):.2f}s  最小 {min(infer):.2f}s  "
        f"最大 {max(infer):.2f}s  p90 {p90:.2f}s")
    log(f"推理总耗时    {total_infer:.1f}s   RTF {rtf:.1f}x 实时")
    log(f"lead 建议     ≥ {lead:.0f}s（单块最大延迟 + 翻译/网络余量；RTF<1.5 时不可用）")
    log(f"输出          {json_path}")
    log(f"              {srt_path}")


if __name__ == "__main__":
    main()
