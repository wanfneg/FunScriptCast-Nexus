# -*- coding: utf-8 -*-
"""Gate 0b：离线（整片一次）解码 + 整片一次样式，用于与官方 goblinscript 对拍。

与 gate0_dump.py 的区别：**不流式**。一次 decode_latents，一次 style_positions_composed。
用来分离两类差异：
  · 若离线 = 官方，但流式 ≠ 官方 -> 差异来自流式调度，样式机本身一致；
  · 若离线 ≠ 官方        -> 样式机或轨道本身有实质差异。

用法：
    python tools/vision/gate0_offline.py <latent.bin> --rows N --cuts 6.3,57.0,...
                                        --out OUT.json [--bundle DIR]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
VISION = REPO / "vendor" / "vision"
for p in (str(VISION), str(VISION / "goblintrain")):
    if p not in sys.path:
        sys.path.insert(0, p)

from onnx_backend import OnnxBundle                       # noqa: E402
from goblintrain import common, jepa_infer                # noqa: E402

ROW_HZ = 30.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("latent")
    ap.add_argument("--rows", type=int, required=True)
    ap.add_argument("--cuts", default="", help="切点毫秒，逗号分隔")
    ap.add_argument("--out", required=True)
    ap.add_argument("--bundle", default=str(VISION / "bundles" / "v0.6.0"))
    a = ap.parse_args()

    bundle = OnnxBundle(a.bundle)
    dim = int(bundle.manifest.get("dim") or 64)
    grid = int(bundle.manifest.get("grid") or 24)
    n = int(a.rows)
    lat = np.memmap(a.latent, dtype=np.int8, mode="r", shape=(n, dim, grid, grid))

    cuts_ms = [float(x) for x in a.cuts.split(",") if x.strip()]
    cut_rows = sorted({int(round(c / 1000.0 * ROW_HZ)) + 1 for c in cuts_ms
                       if 0 < int(round(c / 1000.0 * ROW_HZ)) + 1 < n})

    trk = bundle.decode_latents(lat, n, cut_rows)
    print(f"[offline] rows={n} cuts={len(cut_rows)}", flush=True)

    vstd = bundle.pack.v_std
    times = [(i + 1) * 1000.0 / ROW_HZ for i in range(n)]
    jepa_infer.DT = float(np.median(np.diff(times))) / 1000.0
    jepa_infer.FPS = 1.0 / jepa_infer.DT

    vel = np.nan_to_num(trk["vmarg"]) * vstd
    edges = np.array([0] + cut_rows + [n])
    for lo, hi in zip(edges[:-1], edges[1:]):
        if hi - lo >= 2 and np.isfinite(trk["vmarg"][hi - 2]):
            vel[hi - 1] = vel[hi - 2]

    bias = jepa_infer.fit_rev_bias({"rev_top": trk["rev_top"], "rev_bot": trk["rev_bot"]},
                                   vel, "stream")
    if np.ndim(bias) and len(bias) < n:
        bias = np.concatenate([bias, np.full(n - len(bias), float(np.mean(bias)))])

    has_rev = bool(bundle.pack.heads.get("rev"))
    frc = [] if has_rev else None
    sub = {}
    p = jepa_infer.style_positions_composed(
        vel, trk["level"], trk["env"] * vstd, edges,
        band=(trk["blo"], trk["bhi"]),
        still_eps=jepa_infer.STILL_EPS,
        plat=(trk["plat_top"], trk["plat_bot"]),
        plat_thr=jepa_infer.PLAT_THR, plat_lo=jepa_infer.PLAT_LO,
        plat_peak=jepa_infer.PLAT_PEAK, plat_veto=jepa_infer.PLAT_VETO,
        plat_soft=jepa_infer.PLAT_SOFT, plat_rail_track=True,
        plat_shift_cap=jepa_infer.PLAT_SHIFT_CAP,
        rev=(trk["rev_top"], trk["rev_bot"]),
        rev_snap=common.rows_at(common.REV_SNAP_S, ROW_HZ),
        times_ms=times, subframe=jepa_infer.SUBFRAME, sub_out=sub,
        ext_snap=jepa_infer.EXT_SNAP, amp_cap_x=jepa_infer.AMP_CAP_X,
        rev_source=jepa_infer.REV_SOURCE, rev_bias=bias,
        rev_gap=common.rows_at(common.EVENT_GAP_S, ROW_HZ), force_out=frc)
    actions = jepa_infer.extrema_actions(
        p, times, edges, sub=sub if sub else None, force=frc,
        cut_ease=jepa_infer.CUT_EASE, rdp_eps=jepa_infer.RDP_EPS, stats={})

    spd = [(x, y) for x, y in zip(actions, actions[1:]) if y["at"] > x["at"]]
    if spd:
        over = [abs(y["pos"] - x["pos"]) * 1000.0 / (y["at"] - x["at"]) for x, y in spd]
        if max(over) > common.MAX_POS_RATE:
            cl = common.clamp_speed([(x["at"], x["pos"]) for x in actions])
            for act, (_t, pos) in zip(actions, cl):
                act["pos"] = int(pos)

    out = {"rows": n, "cut_rows": cut_rows,
           "actions": [[float(x["at"]), float(x["pos"])] for x in actions]}
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"[offline] actions={len(actions)} -> {a.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
