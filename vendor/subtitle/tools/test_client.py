#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""服务端测试客户端：把视频按块抽音频 POST 到 /transcribe，打印结果与耗时

  .venv/Scripts/python.exe test_client.py --input "E:/testvideo/VRTEST.mp4" --lang ja \
      --url http://127.0.0.1:8756 --chunk 25 --overlap 2 --max-minutes 2

模拟 Quest 端行为：每块带 video_start_ms / keep_from_ms，最后合并成 SRT。
"""

import argparse
import difflib
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

SR = 16000


def extract_pcm(path, offset, duration):
    cmd = ["ffmpeg", "-v", "error", "-nostdin"]
    if offset > 0:
        cmd += ["-ss", f"{offset:.3f}"]
    cmd += ["-i", str(path), "-vn", "-ac", "1", "-ar", str(SR)]
    if duration:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-f", "s16le", "-"]
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0:
        sys.exit("ffmpeg 失败: " + p.stderr.decode("utf-8", "ignore")[:300])
    return p.stdout


def post(url, lang, video_start_ms, keep_from_ms, pcm_bytes, translate=True, timeout=300):
    q = f"?lang={lang}&video_start_ms={video_start_ms}&keep_from_ms={keep_from_ms}&translate={str(translate).lower()}"
    req = urllib.request.Request(url.rstrip("/") + "/transcribe" + q, data=pcm_bytes,
                                 headers={"Content-Type": "application/octet-stream"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    data["wall_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return data


def _similar(a, b):
    if a in b or b in a:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _lcs_len(a, b):
    """最长公共子串长度（字幕文本很短，DP 足够）。"""
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


def add_segments(all_segs, new_segs, sim_threshold=0.6):
    """跨块去重（时间重叠才算候选）：
    1) 文本高度相似 或 2) 最长公共子串够长 → 判定为同一句，
       保留"更长"的那条（跨块续句通常是更完整的一版）。
    """
    for s in new_segs:
        drop_new = False
        for i, p in enumerate(all_segs):
            if not (s["start_ms"] < p["end_ms"] and p["start_ms"] < s["end_ms"]):
                continue
            a, b = p["text"], s["text"]
            lcs = _lcs_len(a, b)
            dup = (_similar(a, b) >= sim_threshold or
                   (lcs >= 6 and lcs >= 0.3 * min(len(a), len(b))))
            if dup:
                if len(b) > len(a):
                    s["start_ms"] = min(p["start_ms"], s["start_ms"])
                    all_segs[i] = s
                drop_new = True
                break
        if not drop_new:
            all_segs.append(s)


def srt_time(ms):
    h, ms = divmod(int(ms), 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--lang", default="ja")
    ap.add_argument("--url", default="http://127.0.0.1:8756")
    ap.add_argument("--chunk", type=float, default=25.0)
    ap.add_argument("--overlap", type=float, default=2.0)
    ap.add_argument("--offset", type=float, default=0.0)
    ap.add_argument("--max-minutes", type=float, default=2.0)
    ap.add_argument("--no-translate", action="store_true")
    ap.add_argument("--out", default="out")
    args = ap.parse_args()

    with urllib.request.urlopen(args.url.rstrip("/") + "/health", timeout=10) as r:
        print("health:", json.dumps(json.loads(r.read().decode("utf-8")), ensure_ascii=False))

    total_sec = min(args.max_minutes * 60.0, 3600.0)
    step = max(0.5, args.chunk - args.overlap)
    all_segs, t_start = [], args.offset
    t = 0.0
    idx = 0
    while t < total_sec - 0.05:
        dur = min(args.chunk, total_sec - t)
        pcm = extract_pcm(args.input, args.offset + t, dur)
        video_start_ms = int((args.offset + t) * 1000)
        # 服务端不去重（keep_from_ms=0）：跨块去重交给客户端，避免误删跨块续句
        keep_from_ms = 0
        r = post(args.url, args.lang, video_start_ms, keep_from_ms, pcm,
                 translate=not args.no_translate)
        segs = r.get("segments", [])
        add_segments(all_segs, segs)
        print(f"#{idx:02d} [{video_start_ms / 1000:7.1f}s] asr={r.get('asr_ms')}ms "
              f"mt={r.get('mt_ms')}ms wall={r.get('wall_ms')}ms 段数={len(segs)}"
              f"{' [静音跳过]' if r.get('skipped') else ''}")
        for s in segs:
            print(f"      {s['start_ms'] / 1000:7.1f}  {s['text']}")
            if s.get("translation"):
                print(f"                 → {s['translation']}")
        idx += 1
        t += step

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    stem = f"{Path(args.input).stem}_{args.lang}_server"
    srt = []
    for i, s in enumerate(all_segs, 1):
        srt += [str(i), f"{srt_time(s['start_ms'])} --> {srt_time(s['end_ms'])}",
                s.get("translation", ""), s["text"], ""]
    (outdir / f"{stem}.srt").write_text("\n".join(srt), encoding="utf-8")
    (outdir / f"{stem}.json").write_text(
        json.dumps(all_segs, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n共 {len(all_segs)} 段 → {outdir / (stem + '.srt')}")


if __name__ == "__main__":
    main()
