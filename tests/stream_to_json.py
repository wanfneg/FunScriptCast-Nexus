# -*- coding: utf-8 -*-
"""把一段裸 PCM 按**头显的真实方式**推给 /transcribe/stream，汇出与离线路径同构的 JSON。

与头显 AiSubtitleEngine 的行为对齐（否则评测结果不代表真机）：
  · 分块：chunk_sec 一块、overlap_sec 重叠，步进 = (chunk-overlap)，即相邻块共享
    overlap 秒音频（头显 Chunker 的实现方式）；
  · --vad 模式（借鉴 sub-title 项目）：不用定长块，改用 RMS 能量检测静音，
    在**自然停顿处切分句**——缓冲 ≥min_sec 且尾部静音 ≥silence_sec 就切，
    连续说话超过 max_sec 才硬切，纯静音段直接丢弃不推流。切点落在静音里，
    所以不需要重叠、也不会切断句子；
  · 去重：重叠/VAD 模式下相邻块的重复句，用与 addSegmentLocked 相同的判据
    （时间重叠 + LCS≥6 且 ≥30% 短句长度 → 保留更长一条）合并；
  · 输出：[{"start_ms","end_ms","text"(ja),"translation"(zh)}]。

用法：
  .venv/Scripts/python.exe tests/stream_to_json.py <in.pcm> <out.json> \
      [--chunk-sec 10] [--overlap-sec 2] [--vad] [--start-ms 0]
"""
from __future__ import annotations

import argparse
import http.client
import json
import time
import urllib.parse

import numpy as np

SR = 16000
BYTES_PER_SAMPLE = 2


def vad_slice(pcm: bytes, sr: int = 16000, max_sec: float = 15.0, min_sec: float = 1.0,
              silence_sec: float = 0.35, thresh: float = 0.01):
    """RMS 能量 VAD 切分（sub-title 模式）：返回 [(start_byte, end_byte)]。

    - frame=100ms 算一次 RMS；尾部连续静音 ≥silence_sec 且已缓冲 ≥min_sec → 切；
    - 连续语音超过 max_sec → 硬切（不等静音）；
    - 整段无语音帧（全是静音）→ 丢弃，不产生请求。
    """
    frame = int(0.1 * sr)
    samples = np.frombuffer(pcm[:len(pcm) // (2 * frame) * 2 * frame], dtype=np.int16).astype(np.float32) / 32768.0
    n_frames = len(samples) // frame
    rms = np.sqrt(np.mean(samples[:n_frames * frame].reshape(n_frames, frame) ** 2, axis=1))
    silence_frames = int(round(silence_sec / 0.1))
    min_frames = int(round(min_sec / 0.1))
    max_frames = int(round(max_sec / 0.1))

    chunks = []
    start_f = 0
    run_silence = 0
    has_speech = False
    for i in range(n_frames):
        if rms[i] >= thresh:
            run_silence = 0
            has_speech = True
        else:
            run_silence += 1
        buf = i - start_f + 1
        if (has_speech and buf >= min_frames and run_silence >= silence_frames) or buf >= max_frames:
            if has_speech:
                chunks.append((start_f * frame * 2, (i + 1) * frame * 2))
            start_f = i + 1
            run_silence = 0
            has_speech = False
    if has_speech and n_frames - start_f > min_frames // 2:
        chunks.append((start_f * frame * 2, n_frames * frame * 2))
    return chunks


def lcs_len(a: str, b: str) -> int:
    """与 AiSubtitleEngine.lcsLen 相同的最长公共子串（字幕短，DP 足够）。"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ca = a[i - 1]
        for j in range(1, len(b) + 1):
            if ca == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def dedup(segs: list[dict]) -> list[dict]:
    """addSegmentLocked 的 Python 版：时间重叠 + 高相似 → 保留更长一条。"""
    out: list[dict] = []
    for s in segs:
        a = (s.get("text") or "").strip()
        sa, ea = s["start_ms"], s["end_ms"]
        replaced = False
        for i in range(len(out) - 1, -1, -1):
            p = out[i]
            pb = (p.get("text") or "").strip()
            if not (sa < p["end_ms"] and p["start_ms"] < ea):
                continue
            lcs = lcs_len(a, pb)
            if lcs >= 6 and lcs >= 0.3 * min(len(a), len(pb)):
                if len(a) > len(pb):
                    out[i] = s
                replaced = True
                break
        if not replaced:
            out.append(s)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pcm")
    ap.add_argument("out")
    ap.add_argument("--url", default="http://127.0.0.1:8756")
    ap.add_argument("--chunk-sec", type=float, default=10.0)
    ap.add_argument("--overlap-sec", type=float, default=2.0)
    ap.add_argument("--vad", action="store_true",
                    help="RMS 静音切句（sub-title 模式）代替定长块")
    ap.add_argument("--vad-max-sec", type=float, default=15.0)
    ap.add_argument("--vad-min-sec", type=float, default=2.5)
    ap.add_argument("--vad-silence-sec", type=float, default=0.5)
    ap.add_argument("--vad-thresh", type=float, default=0.012)
    ap.add_argument("--lang", default="ja")
    ap.add_argument("--start-ms", type=int, default=0, help="这段音频在视频里的起点")
    args = ap.parse_args()

    pcm = open(args.pcm, "rb").read()
    if len(pcm) % 2:
        pcm = pcm[:-1]
    u = urllib.parse.urlparse(args.url)

    if args.vad:
        spans = vad_slice(pcm, max_sec=args.vad_max_sec, min_sec=args.vad_min_sec,
                          silence_sec=args.vad_silence_sec, thresh=args.vad_thresh)
    else:
        chunk_bytes = int(args.chunk_sec * SR) * BYTES_PER_SAMPLE
        stride_bytes = int(max(args.chunk_sec - args.overlap_sec, 0.5) * SR) * BYTES_PER_SAMPLE
        spans = [(off, min(off + chunk_bytes, len(pcm)))
                 for off in range(0, len(pcm), stride_bytes)]

    segs: list[dict] = []
    timings: list[float] = []
    t_all = time.perf_counter()
    n_chunk = 0
    for sb, eb in spans:
        piece = pcm[sb:eb]
        if len(piece) < BYTES_PER_SAMPLE * SR // 10:  # <0.1s 的尾巴直接丢
            continue
        video_start_ms = args.start_ms + (sb // BYTES_PER_SAMPLE) * 1000 // SR
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=300)
        body = (f"/transcribe/stream?lang={args.lang}&translate=true"
                f"&video_start_ms={video_start_ms}")
        t_req = time.perf_counter()
        conn.request("POST", body, piece, {"Content-Type": "application/octet-stream"})
        resp = conn.getresponse()
        buf = resp.read().decode("utf-8", "replace")
        timings.append(time.perf_counter() - t_req)
        conn.close()
        n_chunk += 1
        for line in buf.splitlines():
            line = line.strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            try:
                ev = json.loads(line[6:])
            except Exception:
                continue
            if ev.get("type") == "delta":
                segs.append({"start_ms": ev["start_ms"], "end_ms": ev["end_ms"],
                             "text": (ev.get("ja") or "").strip(),
                             "translation": (ev.get("zh") or "").strip()})
            elif ev.get("type") == "error":
                print(f"[chunk@{video_start_ms}ms] error: {ev.get('error')}")

    segs = dedup(segs)
    segs.sort(key=lambda s: s["start_ms"])
    json.dump({"segments": segs,
               "meta": {"chunk_sec": args.chunk_sec, "overlap_sec": args.overlap_sec,
                        "vad": args.vad, "chunks": n_chunk}},
              open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    timings.sort()
    med = timings[len(timings) // 2] if timings else 0
    print(f"chunks={n_chunk} segments={len(segs)} "
          f"empty_zh={sum(1 for s in segs if not s['translation'])} "
          f"req_time med={med:.2f}s max={max(timings):.2f}s "
          f"wall={time.perf_counter() - t_all:.1f}s -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
