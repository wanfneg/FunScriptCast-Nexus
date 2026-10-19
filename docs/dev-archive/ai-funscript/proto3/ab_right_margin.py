"""P0-2 修法的离线判决: 发布行距前向窗右边界的距离 -> 轨道/动作差异。

ab_right_ctx.py 已证明:
  chunk=256/右侧0    -> p 相关 0.470, 密度 0.69x, 幅度 1.83x  (流式旧行为)
  chunk=1024/右侧0   -> 与官方几乎一致                          (官方行为)
即关键变量是"发布行离窗右边界多远", 不是 chunk 大小本身。

本脚本把该距离直接扫一遍(同一段 latent, 同一模型), 用来确认 P0-2 的修法
(把发布切片右移出 ctx_rows 行)确实单调逼近官方式结果, 而不是只在某个
特定取值上凑巧变好。

运行: <GTG venv python> ab_right_margin.py [--rows 3072] [--chunk 256]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parent
GTG = ROOT.parent / "_third_party" / "GoblinTrainingGrounds"
sys.path.insert(0, str(GTG))

from goblintrain import common                        # noqa: E402
from goblintrain import jepa_infer                    # noqa: E402
from goblintrain.jepa_train import JepaClip, load_model   # noqa: E402

PROJ = GTG / "projects" / "drafts"
CLIP = "000001"
ROW_HZ, V_STD = 30.0, 193.57327270507812
KEYS = ("vmarg", "level", "env", "blo", "bhi",
        "plat_top", "plat_bot", "rev_top", "rev_bot")


def sweep(model, ds, lo, hi, chunk, right, T, ctx):
    """按 chunk 平铺, 每块发布中间 chunk 行, 右侧 right 行真实上下文。

    right = 发布末行之后的真实行数。官方式 = ctx(1024, 吃满两侧);
    流式旧行为 = 0(发布切片正好顶到前向窗右沿)。
    """
    tracks = {k: np.full(T, np.nan) for k in KEYS}
    with torch.no_grad():
        for s in range(lo, hi, chunk):
            e = min(s + chunk, hi)
            fs = max(lo, s - ctx)
            fe = min(T, e + right)
            if e - fs < 2 or fe - e < min(right, 1):
                continue
            off = s - fs
            oe = off + (e - s)
            model.env_row0 = int(fs)
            model.env_rows = int(oe)
            x = ds.fwin(fs, fe)[None].to("cuda").float()
            if ds.feat_scale != 1.0:
                x.mul_(ds.feat_scale)
            cut = ds.cut[fs:fe][None].to("cuda")
            _v, _c, _g = model(x, cut=cut)
            sl = slice(off, oe)
            tracks["vmarg"][s:e] = model.v_marginal[0].float().cpu().numpy()[sl]
            tracks["level"][s:e] = model.level[0].float().cpu().numpy()[sl]
            tracks["env"][s:e] = model.eg[0].float().cpu().numpy()[sl]
            tracks["blo"][s:e] = model.ext_lo[0].float().cpu().numpy()[sl]
            tracks["bhi"][s:e] = model.ext_hi[0].float().cpu().numpy()[sl]
            pp = model.plat_logits.softmax(1)[0].float().cpu().numpy()
            tracks["plat_top"][s:e] = pp[1, sl]
            tracks["plat_bot"][s:e] = pp[2, sl]
            rp = model.rev_logits.softmax(1)[0].float().cpu().numpy()
            tracks["rev_top"][s:e] = rp[1, sl]
            tracks["rev_bot"][s:e] = rp[2, sl]
    return tracks


def decode(trk, times, edges, T):
    sub, frc = {}, []
    p = jepa_infer.style_positions_composed(
        np.nan_to_num(trk["vmarg"]) * V_STD, trk["level"], trk["env"] * V_STD,
        np.asarray(edges), band=(trk["blo"], trk["bhi"]),
        still_eps=jepa_infer.STILL_EPS,
        plat=(trk["plat_top"], trk["plat_bot"]),
        plat_thr=jepa_infer.PLAT_THR, plat_lo=jepa_infer.PLAT_LO,
        plat_peak=jepa_infer.PLAT_PEAK, plat_veto=jepa_infer.PLAT_VETO,
        plat_soft=jepa_infer.PLAT_SOFT, plat_rail_track=True,
        plat_shift_cap=jepa_infer.PLAT_SHIFT_CAP,
        rev=(trk["rev_top"], trk["rev_bot"]),
        rev_snap=common.rows_at(common.REV_SNAP_S, ROW_HZ),
        times_ms=times[:T], subframe=jepa_infer.SUBFRAME, sub_out=sub,
        ext_snap=jepa_infer.EXT_SNAP, amp_cap_x=jepa_infer.AMP_CAP_X,
        rev_source=jepa_infer.REV_SOURCE, rev_bias=0.0,
        rev_gap=common.rows_at(common.EVENT_GAP_S, ROW_HZ),
        force_out=frc, phase_out=None)
    return jepa_infer.extrema_actions(
        p, times[:T], np.asarray(edges), sub=sub if sub else None, force=frc,
        cut_ease=jepa_infer.CUT_EASE, rdp_eps=jepa_infer.RDP_EPS, stats={}), p


def stats(acts):
    if len(acts) < 3:
        return {"n": len(acts), "per_s": 0.0, "amp": 0.0}
    span = (acts[-1]["at"] - acts[0]["at"]) / 1000.0
    amp = float(np.mean([abs(b["pos"] - a["pos"])
                         for a, b in zip(acts, acts[1:]) if b["at"] > a["at"]]))
    return {"n": len(acts), "per_s": round(len(acts) / span, 3) if span else 0.0,
            "amp": round(amp, 1)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=3072)
    ap.add_argument("--chunk", type=int, default=256)
    args = ap.parse_args()

    model, ck = load_model(common.DEFAULT_CKPT, "cuda")
    model.pos_temp = jepa_infer.POS_TEMP
    model.env_seed = 0
    ds = JepaClip(str(PROJ), CLIP, 384, 192,
                  feat_dir=ck.get("feat_dir", common.LATENTS_DIR),
                  masks_dir=ck.get("masks_dir"),
                  apply_lag=False, row_hz=ck.get("row_hz"))
    T = len(ds.vel)
    jepa_infer.DT = float(np.median(np.diff(ds.times_ms))) / 1000.0
    jepa_infer.FPS = 1.0 / jepa_infer.DT
    times = np.asarray(ds.times_ms, dtype=np.float64)
    ctx = common.rows_at(common.DECODE_CTX_S, ROW_HZ)
    edges = [e for e in ds.shot_edges if e <= args.rows]
    if not edges or edges[-1] < args.rows:
        edges = sorted(set(edges + [args.rows]))

    print(f"clip {CLIP}, 窗 [0,{args.rows}), chunk={args.chunk}, 官方 ctx={ctx}")
    ref = None
    rows = []
    for right in (0, 128, 256, 512, 1024):
        trk = sweep(model, ds, 0, args.rows, args.chunk, right, T, ctx)
        acts, p = decode(trk, times, edges, T)
        s = stats(acts)
        if right == 1024:
            ref = (trk, p)
        rows.append((right, s, trk, p))
        print(f"  右侧 {right:>5} 行 -> 动作 {s['n']:<4} {s['per_s']:>5.2f}/s "
              f"amp={s['amp']:>5.1f}")

    if ref is not None:
        print("\n=== 各档 vs 官方式(右侧 1024) ===")
        rtrk, rp = ref
        for right, s, trk, p in rows:
            a = np.nan_to_num(rp[:args.rows])
            b = np.nan_to_num(p[:args.rows])
            c = np.corrcoef(a, b)[0, 1] if a.std() > 0 and b.std() > 0 else 0.0
            d = np.abs(a - b).mean()
            lv_a = np.nan_to_num(rtrk["level"][:args.rows])
            lv_b = np.nan_to_num(trk["level"][:args.rows])
            cl = np.corrcoef(lv_a, lv_b)[0, 1] if lv_a.std() and lv_b.std() else 0.0
            print(f"  右侧 {right:>5}: p相关={c:>6.3f} p平均差={d:>6.2f} "
                  f"level相关={cl:>6.3f}  动作={s['n']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
