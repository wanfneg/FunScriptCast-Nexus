"""定位: 我们的流式解码与官方解码, 差别到底来自哪一步?

用官方现成 latent 缓存(projects/drafts, clip 000001) + 同一模型, 把
"官方整片解码"与"我们的分块解码"逐层拆开对比。四个 arm 依次叠加我们的做法:

  O  官方式: chunk=1024, 两侧 ctx=1024, 发布整块           <- 真值
  S1 = O 但 chunk=256                                     <- 只改块长
  S2 = S1 但右侧 ctx=0 (发布行贴窗沿)                       <- 叠加"贴窗沿"
  S3 = S2 但逐块独立(不共用 h0 缓存, 每块重算 frontend)      <- 叠加前端重算

每个 arm 输出: 合成位置轨与 O 的相关/平均差, 以及最终动作点数。
这样就能回答"差别来自哪一步", 而不是靠猜。

运行: <GTG venv python> ab_layer_isolation.py [--rows 6144]
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


def run_arm(model, ds, T, rows, chunk, left, right, use_h0, h0_t):
    """按 chunk 平铺 [0,rows); 发布块内 rows, 左 left/右 right 行真实上下文。

    use_h0=True  : 走 h0 缓存前端(我们的做法), 只跑 TCN+heads
    use_h0=False : 每块喂原始 latent 给模型(官方 live 路径)
    """
    tracks = {k: np.full(T, np.nan) for k in KEYS}
    with torch.no_grad():
        for s in range(0, rows, chunk):
            e = min(s + chunk, rows)
            fs = max(0, s - left)
            fe = min(T, e + right)
            if e - fs < 2 or fe - e < 1:
                continue
            off = s - fs
            oe = off + (e - s)
            model.env_row0 = int(fs)
            model.env_rows = int(oe)
            cut = ds.cut[fs:fe].unsqueeze(0).to("cuda")
            if use_h0:
                front = (h0_t[fs:fe][None].to("cuda").float()
                         .transpose(1, 2), None)
                _v, _c, _g = model(front=front)
            else:
                x = ds.fwin(fs, fe)[None].to("cuda").float()
                if ds.feat_scale != 1.0:
                    x.mul_(ds.feat_scale)
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


def n_actions(acts, lo, hi):
    return sum(1 for a in acts if lo <= a["at"] / 1000.0 < hi)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=6144)
    args = ap.parse_args()
    R = args.rows

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

    # h0 缓存: 整段前端一次算好(我们的引擎是增量算, 数值等价 —— frontend 逐点)
    print(f"clip {CLIP}, 窗 [0,{R}), 官方 ctx={ctx}")
    print("预先构建 h0 前端缓存 ...", flush=True)
    h0 = torch.zeros(T, model.inp.out_channels)
    with torch.no_grad():
        for a in range(0, R, 4096):
            b = min(a + 4096, R)
            x = torch.from_numpy(np.array(ds.fwin(a, b))).to("cuda").float()
            if ds.feat_scale != 1.0:
                x.mul_(ds.feat_scale)
            cut = ds.cut[a:b].unsqueeze(0).to("cuda")
            h, _g = model.frontend(x[None], cut)
            h0[a:b] = h[0].transpose(0, 1).float().cpu()

    edges = [e for e in ds.shot_edges if e <= R]
    if not edges or edges[-1] < R:
        edges = sorted(set(edges + [R]))

    arms = [
        ("O  官方: chunk=1024, 左右ctx=1024", 1024, ctx, ctx, True),
        ("S1 只改块长: chunk=256", 256, ctx, ctx, True),
        ("S2 再叠加右侧ctx=0(贴窗沿)", 256, ctx, 0, True),
        ("S3 再叠加每块重算前端(非官方)", 256, ctx, 0, False),
    ]
    ref_p = None
    print(f"\n{'arm':<34}{'动作数':>7}{'p相关':>8}{'p平均差':>9}"
          f"{'level相关':>10}{'vmarg相关':>11}")
    for label, chunk, left, right, use_h0 in arms:
        trk = run_arm(model, ds, T, R, chunk, left, right, use_h0, h0)
        acts, p = decode(trk, times, edges, T)
        if ref_p is None:
            ref_p, ref_trk = p, trk
        a = np.nan_to_num(ref_p[:R])
        b = np.nan_to_num(p[:R])
        c = np.corrcoef(a, b)[0, 1] if a.std() and b.std() else 0.0
        lv_a = np.nan_to_num(ref_trk["level"][:R])
        lv_b = np.nan_to_num(trk["level"][:R])
        cl = np.corrcoef(lv_a, lv_b)[0, 1] if lv_a.std() and lv_b.std() else 0.0
        vm_a = np.nan_to_num(ref_trk["vmarg"][:R])
        vm_b = np.nan_to_num(trk["vmarg"][:R])
        cm = np.corrcoef(vm_a, vm_b)[0, 1] if vm_a.std() and vm_b.std() else 0.0
        print(f"{label:<34}{len(acts):>7}{c:>8.3f}{np.abs(a - b).mean():>9.2f}"
              f"{cl:>10.3f}{cm:>11.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
