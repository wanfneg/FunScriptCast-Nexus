"""定位: 同一份轨道, 为什么实时会话只发布 1286 个动作, 离线复现却有 3840 个?

三个假设各自量化:
  H1 分段(shot_edges)敏感: 官方式样式解码按镜头段重置状态机, 段数一变结果就变
  H2 会话的 actions.json 不是"最终全前缀重样式"的结果(被截断/中途退出)
  H3 动作列表本身有大段空洞(某几段整段被 park 成 still)

纯 numpy, 无 GPU。
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

ROW_HZ = 30.0
V_STD = 193.57327270507812
SESSION = ROOT / "sessions" / "20261009_205246"


def act_stats(actions):
    if len(actions) < 3:
        return dict(n=len(actions))
    span = (actions[-1]["at"] - actions[0]["at"]) / 1000.0
    amp = float(np.mean([abs(b["pos"] - a["pos"])
                         for a, b in zip(actions, actions[1:]) if b["at"] > a["at"]]))
    return dict(n=len(actions), span_s=round(span, 1),
                per_s=round(len(actions) / span, 3) if span else 0.0,
                mean_amp=round(amp, 1))


def decode(trk, times, T, edges, rev_bias, force_apex, label):
    sub: dict = {}
    frc: list[int] = []
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
        rev_source=jepa_infer.REV_SOURCE, rev_bias=rev_bias,
        rev_gap=common.rows_at(common.EVENT_GAP_S, ROW_HZ),
        force_out=frc, phase_out=None)
    st: dict = {}
    actions = jepa_infer.extrema_actions(
        p, times[:T], np.asarray(edges), sub=sub if sub else None,
        force=frc if force_apex else None, cut_ease=jepa_infer.CUT_EASE,
        rdp_eps=jepa_infer.RDP_EPS, stats=st)
    r = act_stats(actions)
    r["apexes"] = len(frc)
    r["rdp"] = f"{st.get('raw')}->{st.get('rdp')}"
    print(f"{label:<34} n={r['n']:<6} {r.get('per_s', 0):>6.2f}/s  "
          f"amp={r.get('mean_amp', 0):>6.1f}  apexes={len(frc):<5} rdp={r['rdp']}")
    return actions, r


def main() -> int:
    z = np.load(SESSION / "tracks.npz")
    times = z["times_ms"].astype(np.float64)
    T = int(np.sum(times > 0))
    trk = {k: z[k][:T].astype(np.float64) for k in
           ("vmarg", "level", "env", "blo", "bhi", "plat_top", "plat_bot",
            "rev_top", "rev_bot")}
    jepa_infer.DT, jepa_infer.FPS = 1.0 / ROW_HZ, ROW_HZ

    live = json.load(open(SESSION / "actions.json", encoding="utf-8"))["actions"]
    meta = json.load(open(SESSION / "meta.json", encoding="utf-8"))
    print(f"实时会话发布: {act_stats(live)}  gen_until={meta and json.load(open(SESSION / 'actions.json', encoding='utf-8')).get('gen_until_ms', 0) / 1000:.0f}s")

    # H3: 实时动作列表的空洞结构
    ats = np.array([a["at"] for a in live], dtype=np.float64)
    gaps = np.diff(ats) / 1000.0
    print(f"H3 空洞: max_gap={gaps.max():.1f}s  p95={np.percentile(gaps, 95):.1f}s  "
          f">10s 的空洞 {int((gaps > 10).sum())} 个, 总计 {gaps[gaps > 10].sum():.0f}s")
    big = np.where(gaps > 20)[0]
    for i in big[:12]:
        print(f"    空洞 {ats[i]/1000:.0f}s -> {ats[i+1]/1000:.0f}s ({gaps[i]:.0f}s)")

    print("\n--- H1: 分段敏感度(固定轨道, 只改 edges) ---")
    rev_bias = jepa_infer.fit_rev_bias(
        {"rev_top": trk["rev_top"], "rev_bot": trk["rev_bot"]},
        np.nan_to_num(trk["vmarg"]) * V_STD, "h1")
    for k, lbl in ((1, "单段 [0,T]"), (2, "均分 2 段"), (6, "均分 6 段"),
                   (12, "均分 12 段"), (30, "均分 30 段"), (120, "均分 120 段")):
        edges = np.linspace(0, T, k + 1).astype(int).tolist()
        decode(trk, times, T, edges, rev_bias, False, lbl)

    print("\n--- 叠加 apex 强制(官方式) ---")
    decode(trk, times, T, [0, T], rev_bias, True, "单段 + force_apex")
    decode(trk, times, T, np.linspace(0, T, 13).astype(int).tolist(),
           rev_bias, True, "均分 12 段 + force_apex")

    print(f"\n实时会话: n={len(live)}  |  离线单段: 见上")
    return 0


if __name__ == "__main__":
    sys.exit(main())
