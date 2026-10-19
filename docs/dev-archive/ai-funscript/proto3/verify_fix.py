"""修复校验: 修复前会话 / 修复后会话 / 官方离线基准 —— 密度、空洞、逐窗。

用法: <GTG venv python> verify_fix.py <修复后会话目录> [修复前会话目录]

判定标准(见诊断结论):
  * 密度应接近官方离线(3.19 点/s), 而不是修复前的 ~0.45 点/s
  * >10s 空洞总量应从数百秒掉到几十秒
  * 用新会话自己的 tracks 离线解码(官方参数: 全长 bias + apex 强制)应与
    引擎实际发布的结果一致 —— 若一致, 说明"识别轨道→发布"这一段已经对齐官方
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
OLD = ROOT / "sessions" / "20261009_205246"
OFFICIAL = ROOT.parent / "proto2" / "ai_scripts" / "K1drsp.funscript"
WINS = list(range(0, 1366, 273))


def load_actions(d: Path) -> list[dict]:
    return json.load(open(d / "actions.json", encoding="utf-8"))["actions"]


def summary(actions: list[dict]) -> dict:
    ats = np.array([a["at"] for a in actions], dtype=np.float64) / 1000.0
    span = float(ats[-1] - ats[0]) if len(ats) > 1 else 0.0
    gaps = np.diff(ats)
    amp = float(np.mean([abs(b["pos"] - a["pos"])
                         for a, b in zip(actions, actions[1:])
                         if b["at"] > a["at"]])) if len(actions) > 2 else 0.0
    return {"n": len(actions), "span_s": round(span, 1),
            "per_s": round(len(actions) / span, 3) if span else 0.0,
            "mean_amp": round(amp, 1),
            "max_gap": round(float(gaps.max()), 1) if len(gaps) else 0.0,
            "n_big": int((gaps > 10).sum()) if len(gaps) else 0,
            "sum_big": round(float(gaps[gaps > 10].sum()), 0) if len(gaps) else 0.0}


def windows(actions: list[dict]) -> list[int]:
    ats = np.array([a["at"] for a in actions], dtype=np.float64) / 1000.0
    return [int(((ats >= a) & (ats < b)).sum()) for a, b in zip(WINS, WINS[1:])]


def report(label: str, actions: list[dict]) -> dict:
    s = summary(actions)
    print(f"{label:<26} n={s['n']:<6} {s['per_s']:>5.2f}/s  amp={s['mean_amp']:>5.1f}  "
          f"max_gap={s['max_gap']:>6.1f}s  >10s 空洞 {s['n_big']:>2} 个/"
          f"{s['sum_big']:>5.0f}s")
    return s


def offline_arm(sess: Path, label: str) -> dict:
    """用该会话自己的 tracks 离线跑官方样式解码(bias 全长 + apex 强制)。"""
    z = np.load(sess / "tracks.npz")
    times = z["times_ms"].astype(np.float64)
    T = int(np.sum(times > 0))
    trk = {k: z[k][:T].astype(np.float64) for k in
           ("vmarg", "level", "env", "blo", "bhi", "plat_top", "plat_bot",
            "rev_top", "rev_bot")}
    jepa_infer.DT, jepa_infer.FPS = 1.0 / ROW_HZ, ROW_HZ
    vel = np.nan_to_num(trk["vmarg"]) * V_STD
    bias = jepa_infer.fit_rev_bias(
        {"rev_top": trk["rev_top"], "rev_bot": trk["rev_bot"]}, vel, "verify")
    sub, frc = {}, []
    p = jepa_infer.style_positions_composed(
        vel, trk["level"], trk["env"] * V_STD, np.array([0, T]),
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
        rev_source=jepa_infer.REV_SOURCE, rev_bias=bias,
        rev_gap=common.rows_at(common.EVENT_GAP_S, ROW_HZ),
        force_out=frc, phase_out=None)
    acts = jepa_infer.extrema_actions(
        p, times[:T], np.array([0, T]), sub=sub if sub else None, force=frc,
        cut_ease=jepa_infer.CUT_EASE, rdp_eps=jepa_infer.RDP_EPS, stats={})
    s = report(label, acts)
    s["rows"] = T
    s["apexes"] = len(frc)
    return s


def main() -> int:
    new = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if new is None or not (new / "actions.json").exists():
        print("用法: verify_fix.py <修复后会话目录>"); return 2
    old = Path(sys.argv[2]) if len(sys.argv) > 2 else OLD

    print("=== 动作点密度与空洞 ===")
    before = report("修复前(实时会话)", load_actions(old))
    after = report("修复后(实时会话)", load_actions(new))
    off = report("官方离线 AI(基准)", json.load(
        open(OFFICIAL, encoding="utf-8-sig"))["actions"])
    # 官方基准只覆盖到实测覆盖点, 粗略同窗比较
    print(f"\n密度达成率: 修复前 {before['per_s'] / off['per_s']:.0%} -> "
          f"修复后 {after['per_s'] / off['per_s']:.0%} (对官方基准)")
    print(f"空洞总量  : 修复前 {before['sum_big']:.0f}s -> "
          f"修复后 {after['sum_big']:.0f}s")

    print("\n=== 逐 273s 窗口动作数 ===")
    print(f"{'':<26} " + " ".join(f"{a:>5d}" for a in WINS[:-1]))
    for lbl, acts in (("修复前(实时会话)", load_actions(old)),
                      ("修复后(实时会话)", load_actions(new)),
                      ("官方离线 AI(基准)", json.load(
                          open(OFFICIAL, encoding="utf-8-sig"))["actions"])):
        print(f"{lbl:<26} " + " ".join(f"{x:>5d}" for x in windows(acts)))

    print("\n=== 修复后会话: 引擎实发 vs 其自身轨道离线复现(官方参数) ===")
    print(f"{'引擎实际发布':<26} n={after['n']:<6} {after['per_s']:>5.2f}/s")
    arm = offline_arm(new, "同轨道离线复现(全长bias+apex)")
    print(f"→ 一致度: 实发/复现 = {after['n'] / max(arm['n'], 1):.2f}"
          f"  (接近 1.0 说明轨道→发布这一段已对齐官方解码)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
