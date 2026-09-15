# -*- coding: utf-8 -*-
"""把一段裸 PCM 按**旧头显分块模式**推给 /transcribe（离线端点），汇出同构 JSON。

对照流式评测的"天花板"参考：25s 大块 + 2s 重叠 + keep_from_ms 去重，
与 AiSubtitleEngine 分块模式的上传行为一致（keep_from = 块起点 + overlap/2，
服务端丢弃起点早于 keep_from 的句子）。输出与 stream_to_json.py 同构。

用法：
  .venv/Scripts/python.exe tests/offline_to_json.py <in.pcm> <out.json> \
      [--chunk-sec 25] [--overlap-sec 2] [--start-ms 0]
"""
from __future__ import annotations

import argparse
import http.client
import json
import time
import urllib.parse

SR = 16000
BYTES_PER_SAMPLE = 2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pcm")
    ap.add_argument("out")
    ap.add_argument("--url", default="http://127.0.0.1:8756")
    ap.add_argument("--chunk-sec", type=int, default=25)
    ap.add_argument("--overlap-sec", type=int, default=2)
    ap.add_argument("--lang", default="ja")
    ap.add_argument("--start-ms", type=int, default=0)
    args = ap.parse_args()

    pcm = open(args.pcm, "rb").read()
    if len(pcm) % 2:
        pcm = pcm[:-1]
    chunk_bytes = args.chunk_sec * SR * BYTES_PER_SAMPLE
    stride_bytes = max(args.chunk_sec - args.overlap_sec, 1) * SR * BYTES_PER_SAMPLE
    u = urllib.parse.urlparse(args.url)

    segs: list[dict] = []
    timings = []
    n_chunk = 0
    t_all = time.perf_counter()
    off = 0
    while off < len(pcm):
        piece = pcm[off:off + chunk_bytes]
        if len(piece) < BYTES_PER_SAMPLE * SR // 10:
            break
        video_start_ms = args.start_ms + (off // BYTES_PER_SAMPLE) * 1000 // SR
        keep_from = video_start_ms + args.overlap_sec * 500
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=600)
        body = (f"/transcribe?lang={args.lang}&video_start_ms={video_start_ms}"
                f"&keep_from_ms={keep_from}&translate=true")
        t_req = time.perf_counter()
        conn.request("POST", body, piece, {"Content-Type": "application/octet-stream"})
        resp = conn.getresponse()
        buf = resp.read().decode("utf-8", "replace")
        timings.append(time.perf_counter() - t_req)
        conn.close()
        n_chunk += 1
        try:
            data = json.loads(buf)
        except Exception:
            print(f"[chunk@{video_start_ms}ms] bad response: {buf[:120]}")
            off += stride_bytes
            continue
        for s in data.get("segments") or []:
            segs.append({"start_ms": int(s.get("start_ms") or 0),
                         "end_ms": int(s.get("end_ms") or 0),
                         "text": (s.get("text") or "").strip(),
                         "translation": (s.get("translation") or "").strip()})
        off += stride_bytes

    segs.sort(key=lambda s: s["start_ms"])
    json.dump({"segments": segs,
               "meta": {"chunk_sec": args.chunk_sec, "overlap_sec": args.overlap_sec,
                        "chunks": n_chunk, "path": "offline"}},
              open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    timings.sort()
    med = timings[len(timings) // 2] if timings else 0
    print(f"chunks={n_chunk} segments={len(segs)} "
          f"empty_zh={sum(1 for s in segs if not s['translation'])} "
          f"req_time med={med:.1f}s max={max(timings):.1f}s "
          f"wall={time.perf_counter() - t_all:.1f}s -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
