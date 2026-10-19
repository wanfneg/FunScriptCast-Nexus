"""P0-2 判决性实验: 发布行的"右侧真实上下文"到底值多少?

背景
----
官方 forward.predict_shots 每个 chunk 是 `chunk_rows(1024) + 两侧 ctx(1024)`,
只发布中间那 1024 行 —— 所以**每个发布行都有 ±1023 行真实上下文**(TCN 感受野
= 1+2*Σdilations(1..512) = ±1023, 这正是 DECODE_CTX_S=1024 的由来)。

流式引擎 stream_engine.py:
    e  = min(gen_end, kept + publish_rows)     # 触发条件 n_rows-kept>=256
    fe = min(gen_end, e + ctx_rows)
而 n_rows 每 64 行递增, 触发时 n_rows-kept **恰好** == 256, 于是 e == gen_end,
`fe = min(gen_end, e+ctx) = e` —— **右侧 ctx 恒为 0**。即 100% 的已发布行都在
"冷右沿"上被解码, 只有 0~255 行右邻(官方是 1023 行)。

这个实验用**官方现成的 latent 缓存**(projects/drafts, clip 000001, 无需 GPU 重跑
抽取)在同一段视频、同一模型上只改"发布行的右侧真实上下文行数", 量化:
  A) right_ctx = 1024  (官方式)
  B) right_ctx = 0     (流式现状)
对轨道与最终动作点的影响。若差异大 -> P0-2 是剩下差距的主因, 值得付"启动余量
≥43s"的代价; 若差异小 -> 省掉一次架构改动。

运行: <GTG venv python> ab_right_ctx.py [--rows 4096] [--chunk 256]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
GTG = ROOT.parent / "_third_party" / "GoblinTrainingGrounds"
sys.path.insert(0, str(GTG))

from goblintrain import common                      # noqa: E402
from goblintrain import forward as fwd              # noqa: E402
from goblintrain import jepa_infer                  # noqa: E402
from goblintrain.jepa_train import JepaClip, load_model   # noqa: E402

PROJ = GTG / "projects" / "drafts"
CLIP = "000001"
ROW_HZ = 30.0
V_STD = 193.57327270507812
KEYS = ("vmarg", "level", "env", "blo", "bhi",
        "plat_top", "plat_bot", "rev_top", "rev_bot")


def run_chunked(model, ds, lo, hi, chunk_rows, right_ctx, T):
    """按 chunk_rows 平铺 [lo,hi), 每块只发布中间 chunk_rows 行,
    右侧真实上下文 = right_ctx 行(左侧固定 ctx_rows, 与官方一致)。"""
    ctx = right_ctx
    tracks = {k: np.full(T, np.nan) for k in KEYS}
    with __import__("torch").no_grad():
        for s in range(lo, hi, chunk_rows):
            e = min(s + chunk_rows, hi)
            fs = max(lo, s - ctx)
            fe = min(T, e + ctx)
            if e - fs < 2:
                continue
            off = s - fs
            oe = off + (e - s)
            model.env_row0 = int(fs)
            model.env_rows = int(oe)
            x = ds.fwin(fs, fe)[None].to("cuda").float()
            if ds.feat_scale != 1.0:
                x.mul_(ds.feat_scale)
            cut = ds.cut[fs:fe][None].to("cuda")
            v, _conf, _gate = model(x, cut=cut)
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
        np.asarray(edges),
        band=(trk["blo"], trk["bhi"]), still_eps=jepa_infer.STILL_EPS,
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


def act_summary(actions):
    if len(actions) < 3:
        return {"n": len(actions), "per_s": 0.0, "amp": 0.0}
    span = (actions[-1]["at"] - actions[0]["at"]) / 1000.0
    amp = float(np.mean([abs(b["pos"] - a["pos"])
                         for a, b in zip(actions, actions[1:]) if b["at"] > a["at"]]))
    return {"n": len(actions), "span_s": round(span, 1),
            "per_s": round(len(actions) / span, 3) if span else 0.0,
            "amp": round(amp, 1)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=4096, help="实验窗总行数")
    ap.add_argument("--start", type=int, default=0, help="起始行")
    ap.add_argument("--chunk", type=int, default=256, help="发布粒度行数")
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
    edges = [e for e in ds.shot_edges if e <= args.start + args.rows]
    if not edges:
        edges = [args.start, args.start + args.rows]
    print(f"clip {CLIP}: T={T} 行 ({T / ROW_HZ:.0f}s), 官方切点 "
          f"{len(ds.shot_edges) - 2} 个")
    print(f"实验窗 [{args.start}, {args.start + args.rows}), 发布粒度 "
          f"{args.chunk} 行, 段边界 {edges}")

    out = {}
    for label, rctx in (("A 右侧 ctx=1024 (官方)", 1024),
                        ("B 右侧 ctx=0    (流式现状)", 0)):
        trk = run_chunked(model, ds, args.start, args.start + args.rows,
                          args.chunk, rctx, T)
        acts, p = decode(trk, times, edges, T)
        s = act_summary(acts)
        out[label] = {"tracks": trk, "actions": acts, "stats": s, "p": p}
        print(f"{label:<26} 动作 {s['n']:<5} {s['per_s']:>5.2f}/s  amp={s['amp']:>5.1f}")

    print("\n=== 轨道级差异 (A vs B, 共同窗) ===")
    A, B = out["A 右侧 ctx=1024 (官方)"], out["B 右侧 ctx=0    (流式现状)"]
    print(f"{'轨道':<10}{'corr':>8}{'A_std':>9}{'B_std':>9}{'std比':>8}"
          f"{'A_absmax':>10}{'B_absmax':>10}")
    for k in KEYS:
        a = np.nan_to_num(A["tracks"][k][args.start:args.start + args.rows])
        b = np.nan_to_num(B["tracks"][k][args.start:args.start + args.rows])
        c = np.corrcoef(a, b)[0, 1] if a.std() > 0 and b.std() > 0 else 0.0
        print(f"{k:<10}{c:>8.3f}{a.std():>9.3f}{b.std():>9.3f}"
              f"{b.std() / max(a.std(), 1e-9):>8.3f}"
              f"{np.abs(a).max():>10.3f}{np.abs(b).max():>10.3f}")
    pa = np.nan_to_num(A["p"][args.start:args.start + args.rows])
    pb = np.nan_to_num(B["p"][args.start:args.start + args.rows])
    print(f"\n合成位置轨 p: corr={np.corrcoef(pa, pb)[0, 1]:.3f}  "
          f"平均|差|={np.abs(pa - pb).mean():.2f} pos单位  "
          f"最大|差|={np.abs(pa - pb).max():.1f}")
    sa, sb = A["stats"], B["stats"]
    print(f"\n动作点: {sa['n']} -> {sb['n']} ({sb['n'] / max(sa['n'], 1):.2f}x), "
          f"密度 {sa['per_s']:.2f} -> {sb['per_s']:.2f}/s, "
          f"幅度 {sa['amp']:.1f} -> {sb['amp']:.1f}")

    # 块边界伪影: 行号 % chunk == 0 处的跳变富集度
    print("\n=== 块边界伪影 (|Δ| > p95 的占比, 均匀=5%) ===")
    for label in ("A 右侧 ctx=1024 (官方)", "B 右侧 ctx=0    (流式现状)"):
        lv = np.nan_to_num(out[label]["tracks"]["level"])
        d = np.abs(np.diff(lv)); thr = np.percentile(d, 95)
        m = np.arange(len(d)) % args.chunk
        print(f"{label:<26} m=0: {100 * (d[m == 0] > thr).mean():>5.1f}%   "
              f"m=chunk/2: {100 * (d[m == args.chunk // 2] > thr).mean():>5.1f}%   "
              f"其余: {100 * (d[(m != 0) & (m != args.chunk // 2)] > thr).mean():>5.1f}%")

    np.savez(ROOT / "ab_right_ctx_result.npz",
             **{f"A_{k}": A["tracks"][k] for k in KEYS},
             **{f"B_{k}": B["tracks"][k] for k in KEYS},
             A_p=A["p"], B_p=B["p"], times_ms=times)
    (ROOT / "ab_right_ctx_result.json").write_text(json.dumps(
        {"clip": CLIP, "window": [args.start, args.start + args.rows],
         "chunk": args.chunk,
         "official_right_ctx": sa, "stream_right_ctx_0": sb},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n结果已存: ab_right_ctx_result.npz / .json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
