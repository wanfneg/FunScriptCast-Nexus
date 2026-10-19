# -*- coding: utf-8 -*-
"""一次性全片解码导出（官方 jepa_infer.run 的等价做法）。

!!! 2026-10-19 更正：本脚本有三处与官方语义不一致，产物不能当“官方参照” !!!
  1) 传给 style_positions_composed 的速度是未乘 v_std 的归一化值（官方是 pos/s）；
  2) 没有按缓存行率重绑 jepa_infer.FPS/DT；
  3) edges=[0,total_rows] 用单 shot 近似真实镜头切点。
  三者叠加使动作数只有官方的 1/4。已修正的版本见 dump_tracks_conf.py。

与 export_tracks.py 的区别：
  * export_tracks 走流式 StreamEngine，每次发布都**重跑全前缀样式**（O(n²)），
    长片上会退化到几乎不动（实测 K1drsp 卡在 247s）。
  * 本脚本一次前向 + 一次样式，与官方离线解码同构，1300s 的片子几分钟跑完。

前提：latent 缓存已存在（StreamEngine 首次运行会生成 <stem>.lat.bin）。
若不存在，先跑一次 export_tracks.py 让它转码+提取（可以中途 Ctrl-C，
只要 .lat.bin 完整即可）。

用法：
    python dump_tracks_full.py <video> <out前缀>

注意：本脚本的三处口径偏差已在 dump_tracks_conf.py 修正，仅作历史对照，
新分析请用 dump_tracks_conf.py。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# vendored 识别模块（goblintrain 在 vendor/vision 下）
REPO = HERE.parents[1]
VISION = REPO / "vendor" / "vision"
if str(VISION) not in sys.path:
    sys.path.insert(0, str(VISION))
CKPT = VISION / "weights" / "checkpoints" / "v0.6.0.pt"

from goblintrain import common            # noqa: E402
from goblintrain import jepa_infer        # noqa: E402
from goblintrain import project as gproject  # noqa: E402
from goblintrain import forward as fwd    # noqa: E402
from goblintrain.jepa_train import load_model  # noqa: E402
from goblintrain.encoders import Encoder  # noqa: E402
from goblintrain.extract import Basis     # noqa: E402

from stream_engine import ROW_HZ, DIM, GRID, INT8_SCALE  # noqa: E402


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    video = sys.argv[1]
    out = Path(sys.argv[2])
    out.parent.mkdir(parents=True, exist_ok=True)

    vp = Path(video)
    lat_path = out.parent / (vp.stem + ".lat.bin")
    if not lat_path.is_file():
        raise SystemExit(f"缺 latent 缓存：{lat_path}\n先跑 export_tracks.py 生成它")

    device = "cuda"
    print(f"[dump] 加载模型…", flush=True)
    model, ck = load_model(str(CKPT), device)
    model.pos_temp = jepa_infer.POS_TEMP
    model.env_seed = 0

    row_bytes = DIM * GRID * GRID          # int8 ⇒ 1 byte/元素
    total_rows = lat_path.stat().st_size // row_bytes
    print(f"[dump] latent {lat_path.name} "
          f"({lat_path.stat().st_size/1e9:.2f} GB, {total_rows} 行)", flush=True)
    lat = np.memmap(lat_path, dtype=np.int8, mode="r",
                    shape=(total_rows, DIM, GRID, GRID))
    times_ms = (np.arange(total_rows) / ROW_HZ * 1000.0).astype(np.float64)

    # 与官方同构：一次前向，collect_tracks=True
    chunk = common.rows_at(common.DECODE_CHUNK_S, ROW_HZ)
    ctx = common.rows_at(common.DECODE_CTX_S, ROW_HZ)
    ds = _LatDS(lat, total_rows)
    shots = [(0, total_rows)]
    t0 = time.time()
    pred, tracks = fwd.predict_shots(model, ds, shots, device, chunk,
                                     collect_tracks=True, ctx=ctx)
    print(f"[dump] 前向完成 {time.time()-t0:.0f}s", flush=True)

    tracks["phase"] = pred
    tracks["times_ms"] = np.asarray(times_ms, dtype=np.float64)
    tracks["row_hz"] = np.float32(ROW_HZ)
    np.savez(str(out) + "_tracks.npz", **tracks)

    # 样式解码（一次，全片）
    t1 = time.time()
    has_rev = bool(ck.get("arch", {}).get("rev_head", False))
    frc = [] if has_rev else None
    sub: dict = {}
    rev_bias = jepa_infer.fit_rev_bias(tracks, tracks["vmarg"], vp.stem) \
        if has_rev else 0.0
    edges = np.array([0, total_rows])
    p = jepa_infer.style_positions_composed(
        tracks["vmarg"], tracks["level"], tracks["env"] * ck["v_std"], edges,
        band=(tracks["blo"], tracks["bhi"]),
        still_eps=jepa_infer.STILL_EPS,
        plat=(tracks["plat_top"], tracks["plat_bot"]),
        plat_thr=jepa_infer.PLAT_THR, plat_lo=jepa_infer.PLAT_LO,
        plat_peak=jepa_infer.PLAT_PEAK, plat_veto=jepa_infer.PLAT_VETO,
        plat_soft=jepa_infer.PLAT_SOFT, plat_rail_track=True,
        plat_shift_cap=jepa_infer.PLAT_SHIFT_CAP,
        rev=(tracks["rev_top"], tracks["rev_bot"]),
        rev_snap=common.rows_at(common.REV_SNAP_S, ROW_HZ),
        times_ms=times_ms, subframe=jepa_infer.SUBFRAME, sub_out=sub,
        ext_snap=jepa_infer.EXT_SNAP, amp_cap_x=jepa_infer.AMP_CAP_X,
        rev_source=jepa_infer.REV_SOURCE, rev_bias=rev_bias,
        rev_gap=common.rows_at(common.EVENT_GAP_S, ROW_HZ),
        force_out=frc)
    print(f"[dump] 样式完成 {time.time()-t1:.0f}s", flush=True)

    st: dict = {}
    actions = jepa_infer.extrema_actions(
        p, times_ms, edges, sub=sub if sub else None, force=frc,
        cut_ease=jepa_infer.CUT_EASE, rdp_eps=jepa_infer.RDP_EPS, stats=st)
    print(f"[dump] 动作 {len(actions)} 个", flush=True)

    fs = {"actions": [{"at": int(round(a["at"])), "pos": int(round(a["pos"]))}
                      for a in actions]}
    (Path(str(out) + "_stream.funscript")).write_text(
        json.dumps(fs, ensure_ascii=False), encoding="utf-8")
    np.save(str(out) + "_pos.npy", p)
    (Path(str(out) + "_meta.json")).write_text(json.dumps({
        "video": video, "rows": int(total_rows), "row_hz": ROW_HZ,
        "n_actions": len(actions), "rev_bias": float(np.mean(rev_bias)),
        "stats": st,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[dump] 完成 → {out}_tracks.npz / _stream.funscript", flush=True)
    return 0


class _LatDS:
    """predict_shots 需要的最小数据集接口（与 stream_engine 的 _DS 同构）。"""

    row_hz = ROW_HZ
    feat_scale = INT8_SCALE
    masked = False
    h0 = None

    def __init__(self, lat, total_rows):
        import torch
        self.lat = lat
        self.total_rows = total_rows
        self.vel = np.zeros(total_rows, np.float32)   # 只用 len
        self.cut = torch.zeros(total_rows, dtype=torch.float32)
        self.times_ms = (np.arange(total_rows) / ROW_HZ * 1000.0)

    def fwin(self, s, e):
        import torch
        return torch.from_numpy(np.array(self.lat[s:e]))   # int8 → tensor


if __name__ == "__main__":
    raise SystemExit(main())
