# -*- coding: utf-8 -*-
"""ONNX 流式引擎冒烟：模拟 1x 播放头跑一段，检查状态 / 动作数 / 轨道一致性。

用法：
    python smoke_onnx_stream.py <video> [--duration-s 120] [--bundle DIR]
                               [--ep cuda|dml|cpu] [--cache DIR] [--transcoded]

  <video>       原始视频（引擎内部 ffmpeg 转码到 480p30）；若已是转码产物，
                加 --transcoded 跳过转码。
  --bundle      默认仓库内 vendor/vision/bundles/v0.6.0。
  --ep          写入 FSC_VISION_EP（默认沿用环境变量，未设为 CPU）。
  --cache       转码/latent 缓存目录，默认系统临时目录下 fsc_onnx_smoke_cache。

验收：state=done、动作数非 0、轨道与离线解码一致（corr > 0.999）。
退出码：0 = 通过，1 = 不通过。

CUDA 示例（需 CUDA 12 + cuDNN 9 运行库）：
    $env:PATH = "<cuda-runtime-dir>;" + $env:PATH
    python smoke_onnx_stream.py "<video>" --ep cuda --duration-s 120
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
VISION = REPO / "vendor" / "vision"
if str(VISION) not in sys.path:
    sys.path.insert(0, str(VISION))

from onnx_stream import OnnxStreamEngine  # noqa: E402

DEFAULT_BUNDLE = VISION / "bundles" / "v0.6.0"
DEFAULT_CACHE = Path(tempfile.gettempdir()) / "fsc_onnx_smoke_cache"


def main() -> int:
    ap = argparse.ArgumentParser(description="ONNX 流式引擎冒烟")
    ap.add_argument("video", help="原始视频；--transcoded 时可直接给 480p30 产物")
    ap.add_argument("--duration-s", type=float, default=120.0,
                    help="模拟时长（秒），默认 120")
    ap.add_argument("--bundle", default=str(DEFAULT_BUNDLE),
                    help="ONNX bundle 目录（默认仓库 vendor/vision/bundles/v0.6.0）")
    ap.add_argument("--ep", default=None, choices=["cuda", "dml", "cpu"],
                    help="执行后端，写入 FSC_VISION_EP；默认沿用环境变量")
    ap.add_argument("--cache", default=str(DEFAULT_CACHE), help="缓存目录")
    ap.add_argument("--transcoded", action="store_true",
                    help="输入已是 480p30 转码产物，跳过转码")
    a = ap.parse_args()

    video = str(Path(a.video))
    if not Path(video).is_file():
        raise SystemExit(f"输入视频不存在：{video}")
    if a.ep:
        os.environ["FSC_VISION_EP"] = a.ep
    bundle = str(Path(a.bundle))
    if not (Path(bundle) / "manifest.json").is_file():
        raise SystemExit(f"bundle 不完整（缺 manifest.json）：{bundle}")
    cache = Path(a.cache)
    cache.mkdir(parents=True, exist_ok=True)
    dur_ms = float(a.duration_s) * 1000.0

    notes = []
    eng = OnnxStreamEngine(video, dur_ms, cache_dir=cache, bundle=bundle,
                           on_note=lambda s: (notes.append(s),
                                              print("[engine]", s, flush=True)))
    if a.transcoded:
        eng._transcode = lambda: video
    t0 = time.time()
    eng.start()
    limit = max(300.0, a.duration_s * 3.0)
    last_state = None
    while time.time() - t0 < limit:
        time.sleep(2)
        ph = max(0.0, (time.time() - t0) - 20.0) * 1000.0
        eng.set_playhead(ph)
        acts, gu = eng.buffer.snapshot()
        st = (eng.buffer.state, len(acts), int(gu // 5000))
        if st != last_state:
            print("[%5.0fs] state=%s actions=%d gen_until=%.0fs rows=%d" %
                  (time.time() - t0, eng.buffer.state, len(acts), gu / 1000,
                   eng.n_rows), flush=True)
            last_state = st
        if eng.buffer.state in ("done", "error"):
            break
    acts, gu = eng.buffer.snapshot()
    wall = time.time() - t0
    print("FINAL state=%s actions=%d gen_until=%.1fs rows=%d wall=%.0fs" %
          (eng.buffer.state, len(acts), gu / 1000, eng.n_rows, wall))
    if eng.buffer.note:
        print("note:", eng.buffer.note)

    ok = eng.buffer.state == "done" and len(acts) > 0
    # 轨道一致性：与离线 decode_latents 同前缀对比（流式解码必须与离线等价）
    corr_min = None
    if eng.n_rows > 2000:
        from onnx_backend import OnnxBundle
        b = OnnxBundle(bundle)
        n = int(eng.n_rows)
        ref = b.decode_latents(eng.lat[:n], n, eng.cut_rows)
        corrs = []
        for k in ("vmarg", "level", "env", "conf"):
            x = np.asarray(eng._trk[k][:n], float)
            r = np.asarray(ref[k], float)
            m = np.isfinite(x) & np.isfinite(r)
            corr = (float(np.corrcoef(x[m], r[m])[0, 1])
                    if m.sum() > 10 and x[m].std() > 0 and r[m].std() > 0
                    else float("nan"))
            corrs.append(corr)
            print("%-8s corr %.6f  (rows %d)" % (k, corr, int(m.sum())))
        finite = [c for c in corrs if np.isfinite(c)]
        corr_min = min(finite) if finite else float("nan")
        ok = ok and bool(finite) and corr_min > 0.999
        print("轨道最小相关 %.6f（阈值 0.999）" % corr_min)
    else:
        print("⚠ 行数不足（%d），跳过轨道一致性检查" % eng.n_rows)
    eng.stop(cleanup=False)
    print("冒烟结果：%s" % ("通过" if ok else "不通过"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
