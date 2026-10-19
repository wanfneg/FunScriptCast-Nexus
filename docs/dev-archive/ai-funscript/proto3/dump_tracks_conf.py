# -*- coding: utf-8 -*-
"""全片一次性解码 + 额外收集 conf 轨道（官方 predict_shots 没收集它）。

背景：JepaModel.forward 返回 (v, conf, gate)，但 forward.predict_shots 里
是 "v, _conf, _gate = model(...)" —— conf 被丢掉，tracks 只有
level/env/blo/bhi/plat/rev/vmarg。

本脚本 = dump_tracks_full.py 的全部逻辑 + conf 收集：
  1) 先用官方 predict_shots(collect_tracks=True) 跑一遍，得到官方轨道；
  2) 再用与 forward.py 逐行等价的本地循环跑一遍，额外收 conf；
  3) 断言两遍的 vmarg 一致（不一致就报错，不静默）；
  4) 用官方样式解码出 action 列表（与 dump_tracks_full 同参数）。

用法：
    python dump_tracks_conf.py <video> <out前缀>
产物：
    <out>_tracks.npz   （含 conf）
    <out>_stream.funscript
    <out>_meta.json
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
VISION = Path(r"E:/Development/FunScriptCast-Nexus/vendor/vision")
if str(VISION) not in sys.path:
    sys.path.insert(0, str(VISION))

from goblintrain import boundaries as gbnd  # noqa: E402
from goblintrain import common            # noqa: E402
from goblintrain import forward as fwd    # noqa: E402
from goblintrain import jepa_infer        # noqa: E402
from goblintrain.jepa_train import load_model  # noqa: E402

from stream_engine import ROW_HZ, DIM, GRID, INT8_SCALE  # noqa: E402

CKPT = Path(r"E:/Development/FunScriptCast-Nexus/vendor/vision/weights/checkpoints/v0.6.0.pt")


class _LatDS:
    row_hz = ROW_HZ
    feat_scale = INT8_SCALE
    masked = False
    h0 = None

    def __init__(self, lat, total_rows):
        import torch
        self.lat = lat
        self.total_rows = total_rows
        self.vel = np.zeros(total_rows, np.float32)
        self.cut = torch.zeros(total_rows, dtype=torch.float32)
        self.times_ms = np.arange(total_rows) / ROW_HZ * 1000.0

    def fwin(self, s, e):
        import torch
        return torch.from_numpy(np.array(self.lat[s:e]))


def _predict_with_conf(model, ds, shots, device, max_frames, ctx):
    """forward.predict_shots 的逐行副本 + conf（语义必须保持一致）。"""
    import torch
    T = len(ds.vel)
    full = np.full(T, np.nan)
    conf = np.full(T, np.nan)
    lo_all = min(lo for lo, _ in shots)
    hi_all = max(hi for _, hi in shots)
    spans = [(s, min(s + max_frames, hi_all)) for s in range(lo_all, hi_all, max_frames)]
    mr = common.rows_at(2.0, ds.row_hz)
    with torch.no_grad():
        for s, e in spans:
            fs = s if e - s >= mr else max(lo_all, e - mr)
            fs = max(lo_all, min(fs, s) - ctx)
            fe = min(hi_all, e + ctx)
            if e - fs < mr:
                continue
            off = s - fs
            oe = off + (e - s)
            model.env_row0 = int(fs)
            model.env_rows = int(oe)
            x = ds.fwin(fs, fe)[None].to(device).float()
            if ds.feat_scale != 1.0:
                x.mul_(ds.feat_scale)
            cut = ds.cut[fs:fe][None].to(device)
            v, c, _gate = model(x, cut=cut)
            full[s:e] = v[0].float().cpu().numpy()[off:oe]
            conf[s:e] = c[0].float().cpu().numpy()[off:oe]
    return full, conf


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
        raise SystemExit(f"缺 latent 缓存：{lat_path}")

    device = "cuda"
    # --- 真实镜头切点：在转码产物上跑 TransNetV2（与 stream_engine 同源语义）
    trans = out.parent / (vp.stem + ".480p30.mp4")
    src = trans if trans.is_file() else Path(video)
    cuts_path = out.parent / (vp.stem + "_cuts.npy")
    if cuts_path.is_file():
        cuts_ms = [float(x) for x in np.load(cuts_path)]
        print(f"[conf] 复用切点 {cuts_path.name} ({len(cuts_ms)} 个)", flush=True)
    else:
        print(f"[conf] TransNetV2 切分 {src.name} …", flush=True)
        tn = gbnd.load_transnet(device)
        dur = (lat_path.stat().st_size // (DIM * GRID * GRID)) / ROW_HZ * 1000.0
        payload = gbnd.analyze_video("live", str(src), dur, tn, device)
        cuts_ms = [float(c) for c in payload["cuts_ms"]]
        np.save(cuts_path, np.asarray(cuts_ms))
        print(f"[conf] 切点 {len(cuts_ms)} 个 -> {cuts_path.name}", flush=True)
    print("[conf] 加载模型…", flush=True)
    model, ck = load_model(str(CKPT), device)
    model.pos_temp = jepa_infer.POS_TEMP
    model.env_seed = 0

    row_bytes = DIM * GRID * GRID
    total_rows = lat_path.stat().st_size // row_bytes
    print(f"[conf] latent {lat_path.name} ({lat_path.stat().st_size/1e9:.2f} GB, {total_rows} 行)", flush=True)
    lat = np.memmap(lat_path, dtype=np.int8, mode="r", shape=(total_rows, DIM, GRID, GRID))
    times_ms = (np.arange(total_rows) / ROW_HZ * 1000.0).astype(np.float64)
    # 与官方 jepa_infer.run() 一致：按缓存行率重绑模块全局 FPS/DT。
    # 不重绑则 FPS 停在默认 15Hz，DT 翻倍 —— 行程/速度/平滑窗全错 2 倍。
    jepa_infer.DT = float(np.median(np.diff(times_ms))) / 1000.0
    jepa_infer.FPS = 1.0 / jepa_infer.DT
    print(f"[conf] 重绑 jepa_infer FPS={jepa_infer.FPS:.1f} DT={jepa_infer.DT:.6f}", flush=True)

    chunk = common.rows_at(common.DECODE_CHUNK_S, ROW_HZ)
    ctx = common.rows_at(common.DECODE_CTX_S, ROW_HZ)
    ds = _LatDS(lat, total_rows)
    shots = [(0, total_rows)]

    # 切点 -> 行号（与 stream_engine._edges_rows 同语义：searchsorted + MIN_GAP 合并）
    import torch as _torch
    cut_rows = []
    t_max = float(times_ms[-1])
    for c in cuts_ms:
        cut_rows.append(int(np.searchsorted(times_ms, c)) if c <= t_max
                        else int(round(c / 1000.0 * ROW_HZ)))
    gap = common.rows_at(gbnd.MIN_GAP_S, ROW_HZ)
    merged = [r for i, r in enumerate(cut_rows) if i == 0 or r - cut_rows[i - 1] >= gap]
    cut_rows = sorted({r for r in merged if 0 < r < total_rows})
    cut_arr = np.zeros(total_rows, np.float32)
    if cut_rows:
        cut_arr[cut_rows] = 1.0
    ds.cut = _torch.from_numpy(cut_arr)
    edges = np.array([0] + cut_rows + [total_rows])
    print(f"[conf] 切点行 {len(cut_rows)} 个 -> {len(edges) - 1} 段", flush=True)

    t0 = time.time()
    pred, tracks = fwd.predict_shots(model, ds, shots, device, chunk,
                                     collect_tracks=True, ctx=ctx)
    print(f"[conf] 官方前向 {time.time()-t0:.0f}s", flush=True)

    t1 = time.time()
    v2, conf = _predict_with_conf(model, ds, shots, device, chunk, ctx)
    print(f"[conf] 带 conf 前向 {time.time()-t1:.0f}s", flush=True)

    def _cmp(x, y, label):
        x = np.nan_to_num(np.asarray(x, float)); y = np.nan_to_num(np.asarray(y, float))
        d = np.abs(x - y)
        c = float(np.corrcoef(x, y)[0, 1]) if x.std() > 0 and y.std() > 0 else float("nan")
        print(f"[conf] 校验 {label}: max|delta|={d.max():.3e} corr={c:.6f}", flush=True)
        if d.max() > 1e-4:
            raise SystemExit(f"本地前向与官方不一致（{label} max|delta|={d.max()}）")

    _cmp(pred, v2, "v(重组后)")

    tracks["conf"] = conf
    tracks["times_ms"] = times_ms
    tracks["row_hz"] = np.float32(ROW_HZ)

    has_rev = bool(ck["arch"].get("rev_head", False))
    frc = [] if has_rev else None
    sub: dict = {}
    # 与官方 jepa_infer.run() 一致：速度必须换到 pos/s（乘 v_std）再进样式；
    # 归一化值直接喂会把每个片段都判成 still，只出小碎步（旧离线脚本的错）。
    vstd = float(ck["v_std"])
    vel = np.nan_to_num(tracks["vmarg"]) * vstd
    for lo_e, hi_e in zip(edges[:-1], edges[1:]):
        if hi_e - lo_e >= 2 and np.isfinite(tracks["vmarg"][hi_e - 2]):
            vel[hi_e - 1] = vel[hi_e - 2]     # 镜头末行沿用前行速度（官方 seam hold）
    rev_bias = jepa_infer.fit_rev_bias(tracks, vel, vp.stem) if has_rev else 0.0
    p = jepa_infer.style_positions_composed(
        vel, tracks["level"], tracks["env"] * vstd, edges,
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
    st: dict = {}
    actions = jepa_infer.extrema_actions(
        p, times_ms, edges, sub=sub if sub else None, force=frc,
        cut_ease=jepa_infer.CUT_EASE, rdp_eps=jepa_infer.RDP_EPS, stats=st)
    print(f"[conf] 动作 {len(actions)} 个", flush=True)

    np.savez(str(out) + "_tracks.npz", **tracks)
    fs = {"actions": [{"at": int(round(a["at"])), "pos": int(round(a["pos"]))} for a in actions]}
    (Path(str(out) + "_stream.funscript")).write_text(json.dumps(fs, ensure_ascii=False), encoding="utf-8")
    (Path(str(out) + "_meta.json")).write_text(json.dumps({
        "video": video, "rows": int(total_rows), "row_hz": ROW_HZ,
        "n_actions": len(actions), "rev_bias": float(np.mean(rev_bias)),
        "v_std": float(ck["v_std"]), "stats": st,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[conf] 完成 -> {out}_tracks.npz", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
