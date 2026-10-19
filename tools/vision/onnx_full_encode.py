# -*- coding: utf-8 -*-
"""整片 ONNX 编码：ffmpeg 流式取帧 -> vjepa_pca.onnx -> int8 latent 缓存。

用法：
    python onnx_full_encode.py <video> <out.bin> [--rows N] [--bundle DIR]

  <video>   原始视频或 480p30 转码产物（按 480p30 重新缩放取帧，二者皆可）。
  <out.bin> int8 latent 缓存输出路径。
  --rows    目标行数；缺省按 ffprobe 时长 * 30 估算。
  --bundle  默认仓库内 vendor/vision/bundles/v0.6.0。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
VISION = REPO / "vendor" / "vision"
DEFAULT_BUNDLE = VISION / "bundles" / "v0.6.0"
if str(VISION) not in sys.path:
    sys.path.insert(0, str(VISION))

from onnx_backend import OnnxBundle  # noqa: E402


def probe_rows(video: str, fps: float) -> int:
    pr = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                         "format=duration", "-of", "default=nw=1:nk=1", video],
                        capture_output=True, text=True)
    dur = float((pr.stdout or "0").strip() or 0)
    if dur <= 0:
        raise SystemExit(f"ffprobe 读不到时长：{pr.stderr[:300]}")
    return int(dur * fps)


def main() -> int:
    ap = argparse.ArgumentParser(description="整片 ONNX 编码")
    ap.add_argument("video")
    ap.add_argument("out")
    ap.add_argument("--rows", type=int, default=0, help="目标行数，默认按时长估算")
    ap.add_argument("--bundle", default=str(DEFAULT_BUNDLE))
    a = ap.parse_args()

    W = H = 384
    FPS = 30.0
    K = 2
    CLIP = 32
    N_ALIGN = 4
    GROUP = (CLIP // 2) * 2 * K
    INT8_SCALE = 8.0 / 127.0
    rows = a.rows or probe_rows(a.video, FPS)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    b = OnnxBundle(a.bundle)
    print("providers:", b.providers, "rows:", rows, flush=True)
    mm = np.memmap(str(out), dtype=np.int8, mode="w+", shape=(rows, 64, 24, 24))

    cmd = ["ffmpeg", "-v", "error", "-ss", "0.000", "-i", a.video,
           "-vf", f"fps={FPS},scale={W}:{H}", "-f", "rawvideo",
           "-pix_fmt", "rgb24", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    nbytes = W * H * 3
    buf = []
    written = 0
    t0 = time.time()
    while written < rows:
        raw = proc.stdout.read(nbytes)
        if len(raw) < nbytes:
            break
        buf.append(np.frombuffer(raw, np.uint8).reshape(H, W, 3).copy())
        if len(buf) < GROUP + K:
            continue
        wins, aa_all = [], []
        for j in range(N_ALIGN):
            aa = list(range(j, GROUP, 2 * K))[:CLIP // 2]
            idx = [i for a2 in aa for i in (a2, a2 + K)]
            wins.append(b.encode_window(np.stack([buf[i] for i in idx])))
            aa_all.append(np.asarray(aa))
        order = np.argsort(np.concatenate(aa_all), kind="stable")
        q = np.clip(np.round(np.concatenate(wins)[order] / INT8_SCALE),
                    -127, 127).astype(np.int8)
        n = min(len(q), rows - written)
        mm[written:written + n] = q[:n]
        written += n
        del buf[:GROUP]
        if written % (GROUP * 60) == 0:
            el = time.time() - t0
            print("rows %d/%d  %.1fs  %.1f fps" % (written, rows, el, written / el),
                  flush=True)
    proc.terminate()
    mm.flush()
    print("done rows=%d  %.1fs" % (written, time.time() - t0), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
