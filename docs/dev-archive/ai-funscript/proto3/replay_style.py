# 离线复现: 用会话轨道跑官方样式, 对比动作数
import json
import statistics
import sys

import numpy as np

sys.path.insert(0, r"E:\Development\AI Funscript\_third_party\GoblinTrainingGrounds")
import torch
from goblintrain import common, jepa_infer

SESS = r"sessions\20261009_211457"
CK = r"E:\Development\AI Funscript\_third_party\GoblinTrainingGrounds\weights\checkpoints\v0.6.0.pt"

ck = torch.load(CK, map_location="cpu", weights_only=False)
trk = np.load(SESS + r"\tracks.npz")
acts = json.load(open(SESS + r"\actions.json"))["actions"]

# 轨道有效范围: times_ms 非零的末行
tm = trk["times_ms"]
valid = np.where(tm > 0)[0]
sty = int(valid[-1]) + 1 if len(valid) else 0
print(f"轨道有效行 {sty} = {sty/30:.0f}s | 会话动作 {len(acts)} 覆盖 "
      f"{acts[-1]['at']/1000:.0f}s" if acts else f"轨道有效行 {sty}")

vel = np.nan_to_num(trk["vmarg"][:sty]) * ck["v_std"]
level = trk["level"][:sty]
env = trk["env"][:sty] * ck["v_std"]
blo, bhi = trk["blo"][:sty], trk["bhi"][:sty]
pt, pb = trk["plat_top"][:sty], trk["plat_bot"][:sty]
rt, rb = trk["rev_top"][:sty], trk["rev_bot"][:sty]
times = tm[:sty]

jepa_infer.DT = float(np.median(np.diff(times))) / 1000.0
jepa_infer.FPS = 1.0 / jepa_infer.DT
edges = np.array([0, sty])

# rev_bias: 全前缀拟合(官方语义)
bias = 0.0
try:
    bias = jepa_infer.fit_rev_bias({"rev_top": rt, "rev_bot": rb}, vel, "offline")
    print(f"rev_bias = {bias if np.ndim(bias)==0 else np.mean(bias):.4f}")
except Exception as e:
    print("rev_bias 失败:", e)

for label, b in (("bias=拟合", bias), ("bias=0", 0.0)):
    sub = {}
    frc: list = []
    p = jepa_infer.style_positions_composed(
        vel, level, env, edges, band=(blo, bhi),
        still_eps=jepa_infer.STILL_EPS, plat=(pt, pb),
        plat_thr=jepa_infer.PLAT_THR, plat_lo=jepa_infer.PLAT_LO,
        plat_peak=jepa_infer.PLAT_PEAK, plat_veto=jepa_infer.PLAT_VETO,
        plat_soft=jepa_infer.PLAT_SOFT, plat_rail_track=True,
        plat_shift_cap=jepa_infer.PLAT_SHIFT_CAP, rev=(rt, rb),
        rev_snap=common.rows_at(common.REV_SNAP_S, jepa_infer.FPS),
        times_ms=times, subframe=jepa_infer.SUBFRAME, sub_out=sub,
        ext_snap=jepa_infer.EXT_SNAP, amp_cap_x=jepa_infer.AMP_CAP_X,
        rev_source=jepa_infer.REV_SOURCE, rev_bias=b,
        rev_gap=common.rows_at(common.EVENT_GAP_S, jepa_infer.FPS),
        force_out=frc, phase_out=None)
    a = jepa_infer.extrema_actions(
        p, times, edges, sub=sub if sub else None, force=frc,
        cut_ease=jepa_infer.CUT_EASE, rdp_eps=jepa_infer.RDP_EPS, stats={})
    amps = [abs(y["pos"] - x["pos"]) for x, y in zip(a, a[1:]) if y["at"] > x["at"]]
    print(f"{label}: {len(a)} 动作, apex {len(frc)}, 平均幅度 "
          f"{statistics.mean(amps):.1f}" if amps else f"{label}: {len(a)} 动作")
