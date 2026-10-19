# 决定性实验: 官方样式合成跑在我们的累积轨道上 → 幅度是否恢复
import json
import sys
import numpy as np

sys.path.insert(0, r"E:\Development\AI Funscript\_third_party\GoblinTrainingGrounds")
from goblintrain import common, jepa_infer

CK = r"E:\Development\AI Funscript\_third_party\GoblinTrainingGrounds\weights\checkpoints\v0.6.0.pt"
ck = common and __import__("torch").load(CK, map_location="cpu", weights_only=False)

trk = np.load(r"compare\K1cztm_stream_final_tracks.npz")
gen_end = 597 * 30  # 597s 覆盖
w1 = min(gen_end, 16380)

vel_pred = np.nan_to_num(trk["vmarg"][:w1]) * ck["v_std"]
level = np.nan_to_num(trk["level"][:w1])
env = np.nan_to_num(trk["env"][:w1]) * ck["v_std"]
blo = np.nan_to_num(trk["blo"][:w1])
bhi = np.nan_to_num(trk["bhi"][:w1])
rev_t = np.nan_to_num(trk["rev_top"][:w1])
rev_b = np.nan_to_num(trk["rev_bot"][:w1])
pt = np.nan_to_num(trk["plat_top"][:w1])
pb = np.nan_to_num(trk["plat_bot"][:w1])
times_ms = np.arange(w1) * (1000.0 / 30.0)
edges = np.array([0, w1])

jepa_infer.DT = 1.0 / 30.0
jepa_infer.FPS = 30.0
sub = {}
p = jepa_infer.style_positions_composed(
    vel_pred, level, env, edges,
    band=(blo, bhi), still_eps=jepa_infer.STILL_EPS,
    plat=(pt, pb),
    plat_thr=jepa_infer.PLAT_THR, plat_lo=jepa_infer.PLAT_LO,
    plat_peak=jepa_infer.PLAT_PEAK, plat_veto=jepa_infer.PLAT_VETO,
    plat_soft=jepa_infer.PLAT_SOFT, plat_rail_track=True,
    plat_shift_cap=jepa_infer.PLAT_SHIFT_CAP,
    rev=(rev_t, rev_b),
    rev_snap=common.rows_at(common.REV_SNAP_S, 30.0),
    times_ms=times_ms, subframe=jepa_infer.SUBFRAME, sub_out=sub,
    ext_snap=jepa_infer.EXT_SNAP, amp_cap_x=jepa_infer.AMP_CAP_X,
    rev_source=jepa_infer.REV_SOURCE, rev_bias=0.0,
    rev_gap=common.rows_at(common.EVENT_GAP_S, 30.0),
    force_out=None, phase_out=None)
actions = jepa_infer.extrema_actions(
    p, times_ms, edges, sub=sub if sub else None, force=None,
    cut_ease=jepa_infer.CUT_EASE, rdp_eps=jepa_infer.RDP_EPS, stats={})
import statistics
amps = [abs(b["pos"] - a["pos"]) for a, b in zip(actions, actions[1:]) if b["at"] > a["at"]]
print(f"离线样式(我们的轨道): {len(actions)} 动作, 平均幅度 {statistics.mean(amps):.1f}")
print(f"(对照: 官方二进制同窗 幅度 ~52, 我们流式 24.5)")
