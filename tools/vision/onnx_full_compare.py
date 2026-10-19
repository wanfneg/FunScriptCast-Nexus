# -*- coding: utf-8 -*-
"""整片验收：ONNX 编码 + ONNX 解码 vs torch 管线（轨道 + 动作 + 静止区）。

用法：
    python onnx_full_compare.py --base <数据目录> [--name K1cztm] [--rows N]
                                [--bundle DIR]

  --base  目录，需含 <name>_lat_onnx_full.bin / <name>_cuts.npy；
          对照参考 <name>_tracks.npz 与 <name>_stream.funscript 也在其中。
  --rows  缺省由 bin 大小反推（64*24*24 字节/行）。
  --bundle 默认仓库内 vendor/vision/bundles/v0.6.0。
产物：<base>/<name>_tracks_onnx_full.npz、<name>_stream_onnx_full.funscript
"""
from __future__ import annotations

import argparse
import json
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
from goblintrain import common, jepa_infer  # noqa: E402

FPS = 30.0
V_STD = 193.57327270507812
ROW_BYTES = 64 * 24 * 24


def main() -> int:
    ap = argparse.ArgumentParser(description="整片 ONNX 对比验收")
    ap.add_argument("--base", required=True, help="数据目录（latent/cuts/tracks/funscript）")
    ap.add_argument("--name", default="K1cztm")
    ap.add_argument("--rows", type=int, default=0)
    ap.add_argument("--bundle", default=str(DEFAULT_BUNDLE))
    a = ap.parse_args()

    base = Path(a.base)
    name = a.name
    lat_path = base / f"{name}_lat_onnx_full.bin"
    if not lat_path.is_file():
        raise SystemExit(f"缺 latent：{lat_path}")
    rows = a.rows or int(lat_path.stat().st_size // ROW_BYTES)

    b = OnnxBundle(a.bundle)
    lat = np.memmap(str(lat_path), dtype=np.int8, mode="r", shape=(rows, 64, 24, 24))
    times = np.arange(rows) / FPS * 1000.0
    cuts = np.load(base / f"{name}_cuts.npy")
    cut_rows = []
    for c in cuts:
        r = int(np.searchsorted(times, c)) if c <= times[-1] else int(round(c / 1000.0 * FPS))
        if 0 < r < rows:
            cut_rows.append(r)

    t0 = time.time()
    trk = b.decode_latents(lat, rows, cut_rows)
    print("decode %.1fs  rows=%d  cuts=%d" % (time.time() - t0, rows, len(cut_rows)))
    np.savez(base / f"{name}_tracks_onnx_full.npz", **{k: trk[k] for k in trk})

    ref_path = base / f"{name}_tracks.npz"
    if ref_path.is_file():
        ref = np.load(ref_path)
        print("%-10s %12s %12s %12s" % ("track", "corr", "max|d|", "p99|d|"))
        for k in ("vmarg", "level", "env", "blo", "bhi", "plat_top",
                  "plat_bot", "rev_top", "rev_bot", "conf"):
            x0 = trk[k]
            c = np.asarray(ref[k], float)[:rows]
            m = np.isfinite(x0) & np.isfinite(c)
            x, y = x0[m], c[m]
            cr = (float(np.corrcoef(x, y)[0, 1])
                  if x.std() > 0 and y.std() > 0 else float("nan"))
            d = np.abs(x - y)
            print("%-10s %12.6f %12.3e %12.3e" % (k, cr, d.max(), np.percentile(d, 99)))
    else:
        print("（无 %s，跳过轨道对照）" % ref_path.name)

    edges = np.array([0] + cut_rows + [rows])
    jepa_infer.DT = 1.0 / FPS
    jepa_infer.FPS = FPS
    vel = np.nan_to_num(trk["vmarg"]) * V_STD
    for lo, hi in zip(edges[:-1], edges[1:]):
        if hi - lo >= 2 and np.isfinite(trk["vmarg"][hi - 2]):
            vel[hi - 1] = vel[hi - 2]
    bias = jepa_infer.fit_rev_bias(trk, vel, "onnx-full")
    frc = []
    sub = {}
    p = jepa_infer.style_positions_composed(
        vel, trk["level"], trk["env"] * V_STD, edges, band=(trk["blo"], trk["bhi"]),
        still_eps=jepa_infer.STILL_EPS, plat=(trk["plat_top"], trk["plat_bot"]),
        plat_thr=jepa_infer.PLAT_THR, plat_lo=jepa_infer.PLAT_LO,
        plat_peak=jepa_infer.PLAT_PEAK, plat_veto=jepa_infer.PLAT_VETO,
        plat_soft=jepa_infer.PLAT_SOFT, plat_rail_track=True,
        plat_shift_cap=jepa_infer.PLAT_SHIFT_CAP, rev=(trk["rev_top"], trk["rev_bot"]),
        rev_snap=common.rows_at(common.REV_SNAP_S, FPS), times_ms=times,
        subframe=jepa_infer.SUBFRAME, sub_out=sub, ext_snap=jepa_infer.EXT_SNAP,
        amp_cap_x=jepa_infer.AMP_CAP_X, rev_source=jepa_infer.REV_SOURCE,
        rev_bias=bias, rev_gap=common.rows_at(common.EVENT_GAP_S, FPS), force_out=frc)
    acts = jepa_infer.extrema_actions(p, times, edges, sub=sub if sub else None,
                                      force=frc, cut_ease=jepa_infer.CUT_EASE,
                                      rdp_eps=jepa_infer.RDP_EPS, stats={})
    print("ONNX 动作:", len(acts))

    ref_fs = base / f"{name}_stream.funscript"
    if ref_fs.is_file():
        rf = json.loads(ref_fs.read_text(encoding="utf-8"))["actions"]
        print("torch 动作:", len(rf))
        ta = np.array([x["at"] for x in acts], float)
        ra = np.array([x["at"] for x in rf], float)

        def cover(x, y, tol=100):
            hit = 0
            for t in y:
                i = int(np.searchsorted(x, t))
                cand = [j for j in (i - 1, i) if 0 <= j < len(x)]
                if cand and min(abs(x[j] - t) for j in cand) <= tol:
                    hit += 1
            return hit / max(1, len(y))

        print("torch 点被 ONNX 覆盖(±100ms): %.1f%%" % (100 * cover(ta, ra)))
        print("ONNX 点被 torch 覆盖(±100ms): %.1f%%" % (100 * cover(ra, ta)))
    else:
        print("（无 %s，跳过动作对照）" % ref_fs.name)

    out_fs = base / f"{name}_stream_onnx_full.funscript"
    out_fs.write_text(json.dumps({"actions": [
        {"at": int(round(x["at"])), "pos": int(round(x["pos"]))} for x in acts]}),
        encoding="utf-8")
    print("saved ->", out_fs.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
