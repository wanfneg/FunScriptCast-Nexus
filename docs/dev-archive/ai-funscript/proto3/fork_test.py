# 最终分叉实验: 官方 latent → 我们的前向调用 + 官方样式 → 幅度?
import sys
import statistics
import numpy as np
import torch

sys.path.insert(0, r"E:\Development\AI Funscript\_third_party\GoblinTrainingGrounds")
from goblintrain import common, jepa_infer
from goblintrain.jepa_train import JepaClip, load_model

PROJ = r"E:\Development\AI Funscript\_third_party\GoblinTrainingGrounds\projects\drafts"
CLIP = "000001"
W1 = 16380  # 0-546s

model, ck = load_model(common.DEFAULT_CKPT, "cuda")
model.pos_temp = jepa_infer.POS_TEMP
model.env_seed = 0
jepa_infer.DT = 1.0 / 30.0
jepa_infer.FPS = 30.0

ds = JepaClip(PROJ, CLIP, 384, 192,
              feat_dir=ck.get("feat_dir", common.LATENTS_DIR),
              masks_dir=ck.get("masks_dir"),
              apply_lag=False, row_hz=ck.get("row_hz"))
T = len(ds.vel)
e = min(T, W1)
fs = 0

with torch.no_grad():
    x = ds.fwin(fs, e)[None].to("cuda").float()
    x.mul_(ds.feat_scale)
    cut = ds.cut[fs:e][None].to("cuda")
    model.env_row0 = fs
    model.env_rows = e - fs
    v, _conf, _gate = model(x, cut=cut)

vmarg = v[0].float().cpu().numpy()
print(f"x stats: mean={x.mean():.4f} std={x.std():.4f} absmax={x.abs().max():.2f}")
print(f"vmarg(头输出) absmax={np.abs(vmarg).max():.3f} std={vmarg.std():.4f}")

level = model.level[0].float().cpu().numpy()
env = model.eg[0].float().cpu().numpy()
blo = model.ext_lo[0].float().cpu().numpy()
bhi = model.ext_hi[0].float().cpu().numpy()
rp = model.rev_logits.softmax(1)[0].float().cpu().numpy()
rt, rb = rp[1], rp[2]

vel = np.nan_to_num(vmarg) * ck["v_std"]
times_ms = np.arange(e) * (1000.0 / 30.0)
edges = np.array([0, e])
sub = {}
p = jepa_infer.style_positions_composed(
    vel, level, env * ck["v_std"], edges,
    band=(blo, bhi), still_eps=jepa_infer.STILL_EPS,
    rev=(rt, rb),
    rev_snap=common.rows_at(common.REV_SNAP_S, 30.0),
    times_ms=times_ms, subframe=jepa_infer.SUBFRAME, sub_out=sub,
    ext_snap=jepa_infer.EXT_SNAP, amp_cap_x=jepa_infer.AMP_CAP_X,
    rev_source=jepa_infer.REV_SOURCE, rev_bias=0.0,
    rev_gap=common.rows_at(common.EVENT_GAP_S, 30.0),
    force_out=None, phase_out=None)
actions = jepa_infer.extrema_actions(
    p, times_ms[:e], edges, sub=sub if sub else None, force=None,
    cut_ease=jepa_infer.CUT_EASE, rdp_eps=jepa_infer.RDP_EPS, stats={})
amps = [abs(b["pos"] - a["pos"]) for a, b in zip(actions, actions[1:]) if b["at"] > a["at"]]
print(f"官方latent + 我们的前向/样式: {len(actions)} 动作, 平均幅度 "
      f"{statistics.mean(amps):.1f} (官方同窗 27.6, 我们流式 9.9)")
