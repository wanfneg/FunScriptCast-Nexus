# -*- coding: utf-8 -*-
"""Gate 0：把当前 Python ONNX 流式引擎的产出落盘，供与官方 goblinscript 对拍。

用法：
    python tools/vision/gate0_dump.py <video> --out OUT.json [--duration-s N]
                                      [--bundle DIR] [--cache DIR] [--ep dml]

产出 JSON：
    { video, duration_ms, rows, cuts_ms, actions:[[at,pos],...],
      tracks: {name: [每 STEP 行的值]}, track_step, state, note }

设计要点：
  * 与官方对拍时，**整片**才有意义（短片段 rev_bias 会失真，见 VISION-ONNX.md §6）。
  * tracks 按步长抽样，足以判相关性，又不会把 JSON 撑到几百 MB。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
VISION = REPO / "vendor" / "vision"
if str(VISION) not in sys.path:
    sys.path.insert(0, str(VISION))

from onnx_stream import OnnxStreamEngine  # noqa: E402

TRACK_KEYS = ("vmarg", "level", "env", "blo", "bhi", "plat_top", "plat_bot",
              "rev_top", "rev_bot", "conf")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--out", required=True)
    ap.add_argument("--duration-s", type=float, default=0.0,
                    help="0 = 整片")
    ap.add_argument("--bundle", default=str(VISION / "bundles" / "v0.6.0"))
    ap.add_argument("--cache", default=str(Path(os.environ.get("TEMP", ".")) / "fsc_gate0_cache"))
    ap.add_argument("--ep", default="dml")
    ap.add_argument("--step", type=int, default=10, help="轨道抽样步长")
    ap.add_argument("--transcoded", action="store_true",
                    help="输入已是 480p30 转码产物，跳过再次转码")
    a = ap.parse_args()

    os.environ["FSC_VISION_EP"] = a.ep
    video = str(Path(a.video).resolve())
    cache = Path(a.cache)
    cache.mkdir(parents=True, exist_ok=True)

    # 时长
    import subprocess
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", video], capture_output=True, text=True)
    total_s = float(out.stdout.strip() or 0)
    dur_s = a.duration_s if a.duration_s > 0 else total_s
    dur_ms = dur_s * 1000.0
    print(f"[gate0] 视频 {Path(video).name} 总长 {total_s:.1f}s，本次处理 {dur_s:.1f}s", flush=True)

    notes = []
    eng = OnnxStreamEngine(video, dur_ms, cache_dir=cache, bundle=a.bundle,
                           on_note=lambda s: (notes.append(s), print("[engine]", s, flush=True)))
    if a.transcoded:
        eng._transcode = lambda: video
    t0 = time.time()
    eng.start()
    limit = dur_s * 4.0 + 600.0          # 给足转码 + 编码余量
    while time.time() - t0 < limit:
        time.sleep(2)
        # 播放头跟到最新，避免引擎空等
        eng.set_playhead(max(0.0, (time.time() - t0) - 5.0) * 1000.0)
        if eng.buffer.state in ("done", "error"):
            break
    wall = time.time() - t0

    acts, gu = eng.buffer.snapshot()
    n = int(eng.n_rows)
    step = max(1, int(a.step))
    tracks = {}
    for k in TRACK_KEYS:
        try:
            arr = np.asarray(eng._trk[k][:n], dtype=float)[::step]
            tracks[k] = [None if not np.isfinite(v) else round(float(v), 5) for v in arr]
        except Exception as exc:                       # noqa: BLE001
            print(f"[gate0] 轨道 {k} 取样失败: {exc}", flush=True)

    payload = {
        "video": video,
        "duration_ms": dur_ms,
        "rows": n,
        "state": eng.buffer.state,
        "gen_until_ms": gu,
        "wall_s": round(wall, 1),
        "cut_rows": list(map(int, eng.cut_rows)),
        "actions": [[float(x["at"]), float(x["pos"])] for x in acts],
        "track_step": step,
        "tracks": tracks,
        "note": eng.buffer.note,
        "notes": notes[-40:],
    }
    Path(a.out).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"[gate0] state={eng.buffer.state} rows={n} actions={len(acts)} "
          f"cuts={len(eng.cut_rows)} wall={wall:.0f}s -> {a.out}", flush=True)
    eng.stop(cleanup=False)
    return 0 if eng.buffer.state == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
