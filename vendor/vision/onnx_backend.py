# -*- coding: utf-8 -*-
"""Nexus 视觉模块的 ONNX 推理后端（不依赖 PyTorch）。

对应官方 GoblinScript 的 5 张图（bundle 由 goblintrain export 生成）：
  vjepa_pca.onnx  帧窗口 -> 白化 int8 latent 行
  transnet.onnx   帧 -> 镜头切点概率
  head.onnx       int8 latent + cut -> vmarg/level/ext/plat/rev/conf + 主干特征 h
  env_step.onnx   包络自回归的一步
  mask.onnx       逐行注意力门（编码期 ROI，本后端暂不用）

分块与包络语义逐行对照 src/heads.rs（MIT）：
  * chunk=1024，两侧 ctx=1024，短尾用 min_chunk 回看；
  * 包络状态每个 chunk 从 0 重新播种，base draw 按**绝对行号**取；
  * shot 末行沿用前行速度；未覆盖的行给各自的中性值。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import onnxruntime as ort

_M64 = (1 << 64) - 1
_DRAW_G = 0x9E3779B97F4A7C15


def env_base_draw(row: int, seed: int, hi: float) -> np.float32:
    """common.env_base_draw 的逐位移植（SplitMix64 finalizer）。"""
    z = (int(row) + (int(seed) * _DRAW_G & _M64) + _DRAW_G) & _M64
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _M64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _M64
    z ^= z >> 31
    return np.float32((z >> 11) * (2.0 ** -53) * float(hi))


@dataclass
class Pack:
    name: str
    v_std: float
    chunk: int
    min_chunk: int
    ctx: int
    env_state: int
    env_flow: bool
    env_seed: int
    env_base_hi: float
    pos_temp: float
    still_eps: float
    heads: dict


def _providers() -> list[str]:
    """默认优先 DirectML（Windows 普及率最高，也是官方编码器的目标后端）。"""
    want = os.environ.get("FSC_VISION_EP", "").strip().lower()
    avail = ort.get_available_providers()
    def _dml() -> list[str] | None:
        for p in ("DmlExecutionProvider", "DirectMLExecutionProvider"):
            if p in avail:
                return [p, "CPUExecutionProvider"]
        return None
    if want in ("cpu",):
        return ["CPUExecutionProvider"]
    if want in ("dml", "directml"):
        return _dml() or ["CPUExecutionProvider"]
    if want == "cuda":
        if "CUDAExecutionProvider" in avail:
            return ["CUDAExecutionProvider", "CPUExecutionProvider"]
        return _dml() or ["CPUExecutionProvider"]
    return _dml() or (["CUDAExecutionProvider", "CPUExecutionProvider"]
                      if "CUDAExecutionProvider" in avail else ["CPUExecutionProvider"])


class OnnxBundle:
    """一个 ONNX bundle（官方 six-pack 或我们自己的导出）。"""

    def __init__(self, bundle_dir: str | Path, pack: str | None = None,
                 providers: list[str] | None = None):
        self.root = Path(bundle_dir)
        man = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        self.manifest = man
        self.dim = int(man["dim"])
        self.grid = int(man["grid"])
        self.int8_scale = float(man["int8_scale"])
        self.providers = providers or _providers()
        self._so = ort.SessionOptions()
        self._so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        packs = man.get("packs", {})
        name = pack or man.get("default") or next(iter(packs))
        p = packs[name]
        self.pack = Pack(
            name=name, v_std=float(p["v_std"]), chunk=int(p["chunk"]),
            min_chunk=int(p["min_chunk"]), ctx=int(p["ctx"]),
            env_state=int(p["env_state"]), env_flow=bool(p.get("env_flow", False)),
            env_seed=int(p.get("env_seed", 0)), env_base_hi=float(p.get("env_base_hi", 6.0)),
            pos_temp=float(p.get("pos_temp", 1.0)), still_eps=float(p.get("still_eps", 22.0)),
            heads=dict(p.get("heads", {})),
        )
        g = p["graphs"]
        packdir = self.root / "packs" / name
        self.packdir = packdir
        # head/env 走 CPU：int8 输入在 DirectML 上会崩（实测 access violation），
        # 而 CPU 跑整片只要 ~34s；编码器才需要 GPU（packed-QKV 注意力）。
        head_prov = os.environ.get("FSC_VISION_HEAD_EP", "cpu").strip().lower()
        self.head_providers = (["DmlExecutionProvider", "CPUExecutionProvider"]
                               if head_prov in ("dml", "directml")
                               else ["CPUExecutionProvider"])
        self.head = self._session(packdir / g["head"], self.head_providers)
        self.env = self._session(packdir / g["env_step"], self.head_providers)
        self._enc_path = self.root / man["graphs"]["encoder"]
        self._tn_path = self.root / man["graphs"]["transnet"]
        self._enc: ort.InferenceSession | None = None
        self._tn: ort.InferenceSession | None = None

    def _session(self, path: Path, providers: list[str] | None = None) -> ort.InferenceSession:
        return ort.InferenceSession(str(path), sess_options=self._so,
                                    providers=providers or self.providers)

    @property
    def encoder(self) -> ort.InferenceSession:
        if self._enc is None:
            self._enc = self._session(self._enc_path)
        return self._enc

    @property
    def transnet(self) -> ort.InferenceSession:
        if self._tn is None:
            self._tn = self._session(self._tn_path)
        return self._tn

    # ---- 编码 ----
    def encode_video(self, video_path: str | Path, out_path: str | Path,
                     total_rows: int, fps: float | None = None,
                     res: int = 384, width: int | None = None) -> int:
        """整片编码到 int8 latent（与官方 extract.ensure 同语义）。

        分组：GROUP = (CLIP_LEN//2)*2*K = 64 帧；每次 flush 产 64 行。
        EOF 时把末帧补 K 份再排空 —— 对应官方 extract.end()，保证最后 2~65 帧
        仍然拥有自己的行（否则尾部缺行）。
        """
        import subprocess

        f = float(fps or self.manifest["grid_fps"])
        W = res
        K, CLIP, N_ALIGN = 2, 32, 4
        GROUP = (CLIP // 2) * 2 * K
        scale = int(self.manifest.get("int8_scale", 8.0 / 127.0))
        int8_scale = float(self.manifest["int8_scale"])
        total_rows = int(total_rows)
        out_path = Path(out_path)
        mm = np.memmap(out_path, dtype=np.int8, mode="w+",
                       shape=(total_rows, self.dim, self.grid, self.grid))
        cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", str(video_path),
               "-vf", f"fps={f},scale={W}:{W}", "-f", "rawvideo",
               "-pix_fmt", "rgb24", "-"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        fb = W * W * 3
        buf: list[np.ndarray] = []
        written = 0

        def flush() -> int:
            nonlocal written
            limit = min(GROUP, len(buf) - K)
            if limit <= 0:
                return 0
            wins, aa_all = [], []
            for j in range(N_ALIGN):
                aa = list(range(j, limit, 2 * K))[:CLIP // 2]
                if not aa:
                    continue
                idx = [i for a in aa for i in (a, a + K)]
                wins.append(self.encode_window(np.stack([buf[i] for i in idx])))
                aa_all.append(np.asarray(aa))
            if not wins:
                return 0
            order = np.argsort(np.concatenate(aa_all), kind="stable")
            q = np.concatenate(wins)[order]
            q = np.clip(np.round(q / int8_scale), -127, 127).astype(np.int8)
            n = min(len(q), total_rows - written)
            if n > 0:
                mm[written:written + n] = q[:n]
                written += n
            del buf[:limit]
            return limit

        try:
            while written < total_rows:
                raw = proc.stdout.read(fb)
                if len(raw) < fb:
                    break
                buf.append(np.frombuffer(raw, np.uint8).reshape(W, W, 3).copy())
                if len(buf) >= GROUP + K:
                    flush()
            # EOF：补满一整组再 flush（定长图；官方 Rust 用窗口后滑，这里近似）
            if buf and written < total_rows:
                while len(buf) < GROUP + K:
                    buf.append(buf[-1])
                flush()
        finally:
            try:
                proc.stdout.close()
            except OSError:
                pass
            if proc.poll() is None:
                proc.terminate()
        mm.flush()
        return written

    def encode_window(self, frames: np.ndarray) -> np.ndarray:
        """(32,384,384,3) uint8 -> (16,64,24,24) float32 白化特征。"""
        x = np.ascontiguousarray(frames[None, ...], dtype=np.uint8)
        y = self.encoder.run(None, {"frames": x})[0]
        return np.asarray(y, dtype=np.float32)

    # ---- 切点 ----
    def transnet_probs(self, frames: np.ndarray) -> np.ndarray:
        """(1,100,27,48,3) uint8 -> (100,) 概率。"""
        x = np.ascontiguousarray(frames, dtype=np.uint8)
        return np.asarray(self.transnet.run(None, {"frames": x})[0]).reshape(-1)

    def transnet_cuts(self, video_path: str | Path, fps: float | None = None,
                      max_s: float | None = None) -> list[float]:
        """整片切点（毫秒）。逐行对照官方 src/boundaries.rs：
        30fps 解码到 27x48，前补 25 帧、尾补齐到步长整数倍；
        窗口 100、步长 50，只保留每个窗口输出的第 25..75 行；
        连续 prob>thr 的一段取峰值帧作为切点，按 min_gap 合并。
        """
        import subprocess

        man = self.manifest
        t = man["transnet"]
        w, h = int(t["input_w"]), int(t["input_h"])
        window, step, lead = int(t["window"]), int(t["step"]), 25
        thr, gap_ms = float(t["thr"]), float(t["min_gap_s"]) * 1000.0
        f = float(fps or man["grid_fps"])
        cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", str(video_path)]
        if max_s:
            cmd += ["-t", f"{max_s:.3f}"]
        cmd += ["-vf", f"fps={f},scale={w}:{h}", "-f", "rawvideo",
                "-pix_fmt", "rgb24", "-"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        fb = w * h * 3
        frames = []
        try:
            while True:
                raw = proc.stdout.read(fb)
                if len(raw) < fb:
                    break
                frames.append(np.frombuffer(raw, np.uint8).reshape(h, w, 3))
        finally:
            try:
                proc.stdout.close()
            except OSError:
                pass
            if proc.poll() is None:
                proc.terminate()
        n = len(frames)
        if n == 0:
            return []
        tail = lead + step - (step if n % step == 0 else n % step)
        padded = lead + n + tail

        def frame_at(pi: int) -> np.ndarray:
            if pi < lead:
                return frames[0]
            if pi >= lead + n:
                return frames[n - 1]
            return frames[pi - lead]

        preds: list[float] = []
        ptr = 0
        while ptr + window <= padded:
            x = np.stack([frame_at(ptr + k) for k in range(window)])[None, ...]
            p = self.transnet_probs(x)
            preds.extend(float(v) for v in p[lead:lead + step])
            ptr += step
        preds = preds[:n]

        cuts: list[float] = []
        i = 0
        while i < len(preds):
            if preds[i] <= thr:
                i += 1
                continue
            j, peak = i, i
            while j < len(preds) and preds[j] > thr:
                if preds[j] > preds[peak]:
                    peak = j
                j += 1
            time_ms = peak / f * 1000.0
            if not cuts or time_ms - cuts[-1] >= gap_ms:
                cuts.append(time_ms)
            i = j
        return cuts

    # ---- 解码 ----
    def forward_chunk(self, lat: np.ndarray, cut: np.ndarray,
                      fs: int, s: int, e: int, fe: int) -> dict[str, np.ndarray]:
        """单块前向 + 包络 AR；返回 [s, e) 的原始轨道（未乘 v_std）。"""
        pack = self.pack
        n, off, keep = fe - fs, s - fs, e - s
        x = np.ascontiguousarray(np.asarray(lat[fs:fe])[None, ...], dtype=np.int8)
        res = self.head.run(None, {"x_i8": x, "cut": cut[fs:fe][None, :]})
        got = {o.name: r for o, r in zip(self.head.get_outputs(), res)}
        out = {k: np.empty(keep, np.float64) for k in
               ("vmarg", "level", "env", "blo", "bhi",
                "plat_top", "plat_bot", "rev_top", "rev_bot", "conf")}
        for key, src in (("vmarg", "vmarg"), ("level", "level"),
                         ("blo", "ext_lo"), ("bhi", "ext_hi"),
                         ("plat_top", "plat_top"), ("plat_bot", "plat_bot"),
                         ("rev_top", "rev_top"), ("rev_bot", "rev_bot")):
            out[key] = got[src][0, off:off + keep].astype(np.float64)
        out["conf"] = (got["conf"][0, off:off + keep].astype(np.float64)
                       if "conf" in got else np.full(keep, np.nan))
        if not pack.env_flow:
            out["env"] = got.get("eg", np.zeros((1, n)))[0, off:off + keep]
            return out
        h = got["h"][0]
        c = h.shape[0]
        state = np.zeros((1, pack.env_state), np.float32)
        eg = np.empty(keep, np.float64)
        for i in range(n):
            ht = np.ascontiguousarray(h[:, i].reshape(1, c, 1), np.float32)
            e0 = np.array([[env_base_draw(fs + i, pack.env_seed, pack.env_base_hi)]],
                          np.float32)
            ev = self.env.run(None, {"h_t": ht, "state": state, "e0": e0})
            val = float(np.asarray(ev[0]).reshape(-1)[0])
            if off <= i < off + keep:
                eg[i - off] = val
            state = np.ascontiguousarray(np.asarray(ev[1], np.float32).reshape(1, -1))
        out["env"] = eg
        return out

    def decode_latents(self, lat: np.ndarray, n_rows: int,
                       cut_rows: list[int] | np.ndarray) -> dict[str, np.ndarray]:
        """按官方 chunk/ctx 语义跑 head + env，返回**原始**轨道（未乘 v_std）。"""
        pack = self.pack
        n_rows = int(n_rows)
        cuts = np.asarray(sorted({int(r) for r in cut_rows if 0 < int(r) < n_rows}), np.int64)
        cut = np.zeros(n_rows, np.float32)
        cut[cuts] = 1.0

        out: dict[str, np.ndarray] = {
            k: np.full(n_rows, np.nan, np.float64)
            for k in ("vmarg", "level", "env", "blo", "bhi",
                      "plat_top", "plat_bot", "rev_top", "rev_bot", "conf")
        }

        def forward(fs: int, s: int, e: int, fe: int) -> None:
            chunk = self.forward_chunk(lat, cut, fs, s, e, fe)
            for k, v in chunk.items():
                out[k][s:e] = v

        nxt = 0
        while n_rows > nxt + pack.chunk:
            s, e = nxt, nxt + pack.chunk
            fs = max(0, s - pack.ctx)
            fe = min(e + pack.ctx, n_rows)
            forward(fs, s, e, fe)
            nxt = e
        if n_rows > nxt:
            s, e = nxt, n_rows
            mc = pack.min_chunk
            fs = s if (e - s) >= mc else max(0, s - (mc - (e - s)))
            fs = max(0, fs - pack.ctx)
            if e - fs >= mc:
                forward(fs, s, e, e)

        # shot 末行沿用前行速度（在原始速度上做，与官方一致）
        edges = [0] + [int(c) for c in cuts] + [n_rows]
        for lo, hi in zip(edges[:-1], edges[1:]):
            if hi - lo >= 2 and np.isfinite(out["vmarg"][hi - 2]):
                out["vmarg"][hi - 1] = out["vmarg"][hi - 2]
        return out
