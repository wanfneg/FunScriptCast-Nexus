"""定位之二: 实时发布列表的空洞从哪来?

同一份轨道, 三种 rev_bias 处理 + 逐窗对比:
  full   : 官方式, 用全长 track 拟合 -> 全长 bias(本次复现用)
  zeropad: 流式引擎实际做法 —— 在 gen_end>=8000 行时拟合一次,
           之后 gen_end 继续增长, 用 zeros 补齐到 gen_end
逐窗对比 live / 复现 / 官方离线, 找出差别落在哪些时间区间。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
GTG = ROOT.parent / "_third_party" / "GoblinTrainingGrounds"
sys.path.insert(0, str(GTG))

from goblintrain import common          # noqa: E402
from goblintrain import jepa_infer      # noqa: E402

ROW_HZ, V_STD = 30.0, 193.57327270507812
SESSION = ROOT / "sessions" / "20261009_205246"
OFFICIAL = ROOT.parent / "proto2" / "ai_scripts" / "K1drsp.funscript"


def decode(trk, times, T, rev_bias, force_apex, edges=None):
    if edges is None:
        edges = [0, T]
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
        rev_source=jepa_infer.REV_SOURCE, rev_bias=rev_bias,
        rev_gap=common.rows_at(common.EVENT_GAP_S, ROW_HZ),
        force_out=frc, phase_out=None)
    return jepa_infer.extrema_actions(
        p, times[:T], np.asarray(edges), sub=sub if sub else None,
        force=frc if force_apex else None, cut_ease=jepa_infer.CUT_EASE,
        rdp_eps=jepa_infer.RDP_EPS, stats={})


def holes(actions, label, win=10.0):
    ats = np.array([a["at"] for a in actions], dtype=np.float64) / 1000.0
    g = np.diff(ats)
    big = g[g > win]
    print(f"  {label:<28} n={len(actions):<6} max_gap={g.max() if len(g) else 0:>6.1f}s  "
          f">10s 空洞 {len(big)} 个 / 合计 {big.sum():>5.0f}s")


def windows(actions, edges_s, label):
    ats = np.array([a["at"] for a in actions], dtype=np.float64) / 1000.0
    c = [int(((ats >= a) & (ats < b)).sum()) for a, b in zip(edges_s, edges_s[1:])]
    print(f"  {label:<28} " + " ".join(f"{x:>5d}" for x in c))


def main() -> int:
    z = np.load(SESSION / "tracks.npz")
    times = z["times_ms"].astype(np.float64)
    T = int(np.sum(times > 0))
    trk = {k: z[k][:T].astype(np.float64) for k in
           ("vmarg", "level", "env", "blo", "bhi", "plat_top", "plat_bot",
            "rev_top", "rev_bot")}
    jepa_infer.DT, jepa_infer.FPS = 1.0 / ROW_HZ, ROW_HZ

    live = json.load(open(SESSION / "actions.json", encoding="utf-8"))["actions"]
    off = [a for a in json.load(open(OFFICIAL, encoding="utf-8-sig"))["actions"]]

    vel = np.nan_to_num(trk["vmarg"]) * V_STD
    bias_full = jepa_infer.fit_rev_bias(
        {"rev_top": trk["rev_top"], "rev_bot": trk["rev_bot"]}, vel, "full")
    print(f"fit_rev_bias 全长返回: type={type(bias_full).__name__} "
          f"shape={np.shape(bias_full)}")

    # 流式引擎做法: 在 gen_end=8064 行处拟合, 再用 0 补到全长
    cut = 8064
    b_cut = jepa_infer.fit_rev_bias(
        {"rev_top": trk["rev_top"][:cut], "rev_bot": trk["rev_bot"][:cut]},
        vel[:cut], "cut")
    print(f"在 {cut} 行拟合: shape={np.shape(b_cut)}")
    bias_zeropad = b_cut
    if np.ndim(b_cut) and len(b_cut) < T:
        bias_zeropad = np.concatenate([b_cut, np.zeros(T - len(b_cut))])
        print(f"  -> zeros 补齐到 {T} (流式引擎行为)")

    print("\n--- 动作点数与空洞 ---")
    holes(live, "实时会话(实际发布)")
    A = decode(trk, times, T, bias_full, False)
    holes(A, "复现: 全长 bias, 无 force")
    B = decode(trk, times, T, bias_full, True)
    holes(B, "复现: 全长 bias + force")
    C = decode(trk, times, T, bias_zeropad, False)
    holes(C, "复现: zeros 补齐 bias")
    D = decode(trk, times, T, 0.0, False)
    holes(D, "复现: bias=0(完全未校准)")
    holes(off, "官方离线 AI(基准)")

    edges = list(range(0, 1366, 273))
    print("\n--- 逐 273s 窗口动作数 ---")
    print(f"  {'窗口':<28} " + " ".join(f"{a:>5d}" for a in edges[:-1]))
    windows(live, edges, "实时会话(实际发布)")
    windows(A, edges, "复现: 全长 bias, 无 force")
    windows(B, edges, "复现: 全长 bias + force")
    windows(C, edges, "复现: zeros 补齐 bias")
    windows(off, edges, "官方离线 AI(基准)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
