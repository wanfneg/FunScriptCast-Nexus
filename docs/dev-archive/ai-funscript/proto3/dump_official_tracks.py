# 官方路径轨道导出: drafts 项目缓存的 K1cztm(000001) → tracks npz
import sys
import numpy as np

sys.path.insert(0, r"E:\Development\AI Funscript\_third_party\GoblinTrainingGrounds")
from goblintrain import common, jepa_infer
from goblintrain.jepa_train import JepaClip, load_model
from goblintrain import forward as fwd

PROJ = r"E:\Development\AI Funscript\_third_party\GoblinTrainingGrounds\projects\drafts"
CLIP = "000001"

model, ck = load_model(common.DEFAULT_CKPT, "cuda")
model.pos_temp = jepa_infer.POS_TEMP
model.env_seed = 0

ds = JepaClip(PROJ, CLIP, 384, 192,
              feat_dir=ck.get("feat_dir", common.LATENTS_DIR),
              masks_dir=ck.get("masks_dir"),
              apply_lag=False, row_hz=ck.get("row_hz"))
T = len(ds.vel)
jepa_infer.DT = float(np.median(np.diff(ds.times_ms))) / 1000.0
jepa_infer.FPS = 1.0 / jepa_infer.DT
chunk_rows = common.rows_at(common.DECODE_CHUNK_S, jepa_infer.FPS)
ctx_rows = common.rows_at(common.DECODE_CTX_S, jepa_infer.FPS)
shots = [(0, T)]
pred, tracks = fwd.predict_shots(model, ds, shots, "cuda", chunk_rows,
                                 collect_tracks=True, ctx=ctx_rows)
# 注意: predict_shots 返回的 pred 是**重组后**的相位轨, 而 tracks["vmarg"] 才是
# 官方解码实际使用的 marginal(官方 jepa_infer.run 里是 `pred = tracks["vmarg"]`,
# 方向相反)。曾经这里写成 tracks["vmarg"] = pred, 把两件事弄反了 —— 于是此前
# 所有"流式 vmarg vs 官方 vmarg"的对比都是苹果比橘子(vmarg/env 会越出模型的
# clamp(-3,3))。重组轨另存为 phase, 不要再覆盖 vmarg。
tracks["phase"] = pred
tracks["times_ms"] = np.asarray(ds.times_ms, dtype=np.float64)
np.savez(r"E:\Development\AI Funscript\proto3\compare\official_tracks.npz", **tracks)
print(f"saved official_tracks.npz: T={T} rows, "
      f"env absmax={np.nanmax(np.abs(tracks['env'])):.3f}, "
      f"level range={np.nanmin(tracks['level']):.0f}-{np.nanmax(tracks['level']):.0f}, "
      f"vmarg absmax={np.nanmax(np.abs(tracks['vmarg'])):.3f}")
