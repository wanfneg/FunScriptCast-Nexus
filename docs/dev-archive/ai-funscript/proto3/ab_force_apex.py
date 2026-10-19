"""A/B: 官方 rev_source="viterbi" 的 apex 强制顶点(force_out/force)缺失的代价。

纯 numpy, 不需要 GPU: 直接读某次会话已保存的轨道(tracks.npz), 用官方
style_positions_composed + extrema_actions 复现流式的样式解码, 比较两种模式:

  A) force=None            -> 流式引擎当前的做法(丢掉 Viterbi 调用的顶点)
  B) force_out=frc, force=frc -> 官方 jepa_infer.run() 的做法

对照: 官方离线产物 proto2/ai_scripts/K1drsp.funscript(同视频真值基准)。

运行: <GTG venv python> ab_force_apex.py [session_dir]
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
V_STD = 193.57327270507812              # v0.6.0 checkpoint 的 v_std
OFFICIAL = ROOT.parent / "proto2" / "ai_scripts" / "K1drsp.funscript"
SESSION = Path(sys.argv[1]) if len(sys.argv) > 1 else \
    ROOT / "sessions" / "20261009_205246"


def stats_of(actions: list[dict]) -> dict:
    if len(actions) < 3:
        return {"n": len(actions), "act_per_s": 0.0, "mean_amp": 0.0}
    span = (actions[-1]["at"] - actions[0]["at"]) / 1000.0
    amp = float(np.mean([abs(b["pos"] - a["pos"])
                         for a, b in zip(actions, actions[1:]) if b["at"] > a["at"]]))
    return {"n": len(actions), "span_s": round(span, 1),
            "act_per_s": round(len(actions) / span, 3) if span else 0.0,
            "mean_amp": round(amp, 1)}


def main() -> int:
    z = np.load(SESSION / "tracks.npz")
    times = z["times_ms"].astype(np.float64)
    T = int(np.sum(times > 0))
    print(f"会话 {SESSION.name}: {T} 行 ({T / ROW_HZ:.0f}s 视频)")

    jepa_infer.DT = 1.0 / ROW_HZ
    jepa_infer.FPS = ROW_HZ

    trk = {k: z[k][:T].astype(np.float64) for k in
           ("vmarg", "level", "env", "blo", "bhi",
            "plat_top", "plat_bot", "rev_top", "rev_bot")}
    vel_all = np.nan_to_num(trk["vmarg"]) * V_STD

    # 官方 run() 语义: 每个真实剪缝前的末行保留前行速度(这里无 cut 列表, 单段)
    edges_full = np.array([0, T])

    # 官方 run(): has_rev=True -> frc = [] 并交给 style_positions_composed 填充
    rev_bias = jepa_infer.fit_rev_bias(
        {"rev_top": trk["rev_top"], "rev_bot": trk["rev_bot"]}, vel_all, "ab")
    print(f"rev_bias: {'array' if np.ndim(rev_bias) else 'scalar'} = {rev_bias if not np.ndim(rev_bias) else np.shape(rev_bias)}")

    sub: dict = {}
    frc: list[int] = []
    phase = {"apexes": 0, "in_runs": 0}
    p = jepa_infer.style_positions_composed(
        vel_all, trk["level"], trk["env"] * V_STD, edges_full,
        band=(trk["blo"], trk["bhi"]),
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
        force_out=frc, phase_out=phase)
    print(f"Viterbi 调用的顶点(apex)数 = {len(frc)}  (phase={phase})")

    out = {}
    for label, force in (("A_force_None(现状)", None),
                         ("B_force_apex(官方)", frc)):
        st: dict = {}
        actions = jepa_infer.extrema_actions(
            p, times[:T], edges_full, sub=sub if sub else None,
            force=force, cut_ease=jepa_infer.CUT_EASE,
            rdp_eps=jepa_infer.RDP_EPS, stats=st)
        spd = [abs(y["pos"] - x["pos"]) * 1000.0 / (y["at"] - x["at"])
               for x, y in zip(actions, actions[1:]) if y["at"] > x["at"]]
        n_over = sum(1 for s in spd if s > common.MAX_POS_RATE)
        if n_over:
            cl = common.clamp_speed([(a["at"], a["pos"]) for a in actions])
            for a, (_t, pos) in zip(actions, cl):
                a["pos"] = int(pos)
        r = stats_of(actions)
        r["rdp_raw"] = st.get("raw")
        r["rdp_kept"] = st.get("rdp")
        r["over600"] = n_over
        r["clamped"] = bool(n_over)
        r["clamped_stats"] = stats_of(actions) if n_over else None
        out[label] = r
        print(f"{label:<22} n={r['n']:<6} {r['act_per_s']:>6.2f}/s  "
              f"meanAmp={r['mean_amp']:>6.1f}  rdp={st.get('raw')}->{st.get('rdp')}"
              f"  over600={n_over}")

    off = json.load(open(OFFICIAL, encoding="utf-8-sig"))["actions"]
    off = [{"at": a["at"], "pos": a["pos"]} for a in off if a["at"] <= times[T - 1]]
    ro = stats_of(off)
    print(f"{'官方离线 AI(基准)':<22} n={ro['n']:<6} {ro['act_per_s']:>6.2f}/s  "
          f"meanAmp={ro['mean_amp']:>6.1f}")
    a, b = out["A_force_None(现状)"], out["B_force_apex(官方)"]
    print(f"\n修复预期: 动作点 {a['n']} -> {b['n']} ({b['n'] / max(a['n'], 1):.2f}x), "
          f"密度 {a['act_per_s']:.2f} -> {b['act_per_s']:.2f}/s, "
          f"幅度 {a['mean_amp']:.1f} -> {b['mean_amp']:.1f}")
    print(f"对官方基准达成率: 现状 {a['act_per_s'] / ro['act_per_s']:.0%} -> "
          f"修复后 {b['act_per_s'] / ro['act_per_s']:.0%}")

    (ROOT / "ab_force_apex_result.json").write_text(
        json.dumps({"session": SESSION.name, "rows": T,
                    "apexes_called": len(frc), "phase": phase,
                    "A_current": a, "B_official": b, "official_offline": ro},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
