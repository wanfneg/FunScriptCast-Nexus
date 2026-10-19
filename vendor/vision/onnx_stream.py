# -*- coding: utf-8 -*-
"""ONNX 流式引擎：边播边编码 + 边解码，不依赖 PyTorch 做推理。

与 PyTorch 版 stream_engine.StreamEngine 的公开接口保持一致
（buffer / times_ms / n_rows / _trk / rev_bias_log / set_playhead ...），
便于 main_stream.py 通过 FSC_VISION_BACKEND=onnx 切换。

与官方 GoblinScript 的流式书签一致：
  * 编码按 64 帧分组（4 个对齐窗口），EOF 补静态尾帧；
  * 解码 chunk=1024、两侧 ctx=1024；包络状态每块从 0 重新播种；
  * 样式在累积轨道上整体重算（与现引擎相同），保证随前缀增长收敛。
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
import warnings
from pathlib import Path

import numpy as np

try:
    from .onnx_backend import OnnxBundle
except ImportError:                      # 作为顶层模块导入时（开发/冒烟）
    from onnx_backend import OnnxBundle

warnings.filterwarnings("ignore", category=FutureWarning)

ROW_HZ = 30.0
FPS_GRID = 30.0
K = 2
CLIP_LEN = 32
N_ALIGN = 4
GROUP = (CLIP_LEN // 2) * 2 * K          # 64
W = H = 384
DEFAULT_BUNDLE = Path(__file__).resolve().parent / "bundles" / "v0.6.0"


class ActionBuffer:
    """动作缓冲：发布侧整体替换，播放侧只读快照。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.actions: list[dict] = []
        self.gen_until_ms = 0.0
        self.state = "idle"                 # idle/warming/running/done/error
        self.note = ""

    def replace_all(self, new_actions: list[dict], gen_until_ms: float) -> None:
        with self.lock:
            self.actions = sorted(new_actions, key=lambda a: a["at"])
            self.gen_until_ms = gen_until_ms

    def snapshot(self) -> tuple[list[dict], float]:
        with self.lock:
            return list(self.actions), self.gen_until_ms


class OnnxStreamEngine:
    def __init__(self, video: str, duration_ms: float, device: str = "cpu",
                 ckpt: str | None = None, on_note=None,
                 cache_dir: str | Path | None = None,
                 bundle: str | Path | None = None,
                 still_gate: str | None = None,
                 still_conf_thr: float = 0.70,
                 still_vm_thr: float = 50.0,
                 still_score_thr: float = 0.70):
        self.video = video
        self.duration_ms = float(duration_ms)
        self.device = device
        # 置信度+速度静止门（research: still_gate.py），默认 combo。
        # FSC_VISION_STILL_GATE=off 可关闭；and/or 为另外两种口径。
        self.still_gate = str(
            still_gate if still_gate is not None
            else os.environ.get("FSC_VISION_STILL_GATE", "combo")).strip().lower()
        self.still_conf_thr = float(still_conf_thr)
        self.still_vm_thr = float(still_vm_thr)
        self.still_score_thr = float(still_score_thr)
        self.buffer = ActionBuffer()
        self.on_note = on_note or (lambda s: None)
        self.cache_dir = Path(cache_dir) if cache_dir else Path(video).parent
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.bundle_path = Path(bundle) if bundle else Path(
            os.environ.get("FSC_VISION_BUNDLE") or DEFAULT_BUNDLE)
        self.total_rows = int(duration_ms / 1000.0 * ROW_HZ) + 8
        self.lat_path = self.cache_dir / (Path(video).stem + ".onnx.lat.bin")
        self.lat = np.memmap(self.lat_path, dtype=np.int8, mode="w+",
                             shape=(self.total_rows, 64, 24, 24))
        self.times_ms = np.zeros(self.total_rows, np.float64)
        self._trk_keys = ("vmarg", "level", "env", "blo", "bhi",
                          "plat_top", "plat_bot", "rev_top", "rev_bot", "conf")
        self._trk = {k: np.full(self.total_rows, np.nan, np.float32)
                     for k in self._trk_keys}
        self.n_rows = 0
        self.cuts_ms: list[float] = []
        self.cut_rows: list[int] = []
        self.cuts_done = threading.Event()
        self.lock = threading.Lock()
        self.stop_flag = threading.Event()
        self._playhead_ms = 0.0
        self._next = 0                       # 下一个待前向的行
        self._threads: list[threading.Thread] = []
        self.bundle: OnnxBundle | None = None
        self.rev_bias_log: list[str] = []
        self._rev_bias = 0.0
        self._rev_bias_rows = 0

    # ---- 生命周期 ----
    def start(self) -> None:
        t = threading.Thread(target=self._run, daemon=True, name="onnx-stream")
        t.start()
        self._threads.append(t)

    def stop(self, cleanup: bool = False) -> None:
        self.stop_flag.set()
        if cleanup:
            try:
                self.lat._mmap.close()
            except (AttributeError, OSError, ValueError):
                pass
            try:
                self.lat_path.unlink(missing_ok=True)
            except PermissionError:
                pass

    def set_playhead(self, t_ms: float) -> None:
        self._playhead_ms = t_ms

    def request_jump(self, t_ms: float) -> None:
        # v1：不支持跳转（洞不回填），与现引擎的 v1 行为一致
        self.note(f"seek → {t_ms/1000:.0f}s（ONNX v1 不回收空间）")

    def note(self, s: str) -> None:
        self.on_note(s)

    # ---- 主流程 ----
    def _run(self) -> None:
        try:
            if not self.bundle_path or not Path(self.bundle_path).is_dir():
                raise RuntimeError("缺少 ONNX bundle：设 FSC_VISION_BUNDLE 为 bundle 目录")
            self.buffer.state = "warming"
            self.note("加载 ONNX bundle…")
            self.bundle = OnnxBundle(self.bundle_path)
            self.note(f"bundle {self.bundle.pack.name}（{self.bundle.providers[0]}）")
            trans = self._transcode()
            # 官方顺序：先找切点，再编码/解码。DirectML 在并发创建新会话时
            # 会 E_FAIL（DmlGraphFusionHelper），串行既稳又和官方一致。
            self._cuts_worker(trans)
            self._extract(trans)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            with self.buffer.lock:
                self.buffer.state = "error"
                self.buffer.note = str(e)

    def _transcode(self) -> str:
        from goblintrain import media as gmedia
        from goblintrain import project as gproject
        tmp = self.cache_dir / f"{Path(self.video).stem}.480p30.mp4"
        if tmp.exists():
            self.note("复用已有转码: " + tmp.name)
            return str(tmp)
        self.note("转码 480p30 x264（约 9x 实时）…")
        reason = gmedia.transcode(self.video, str(tmp), gproject.TRANSCODE)
        if reason:
            self.note(f"转码失败({reason})，回退直解原片")
            tmp.unlink(missing_ok=True)
            return self.video
        return str(tmp)

    def _cuts_worker(self, video: str) -> None:
        try:
            cuts = self.bundle.transnet_cuts(video, max_s=self.duration_ms / 1000.0)
            self.cuts_ms = list(cuts)
            self.cut_rows = self._cuts_to_rows(cuts)
            self.note(f"切点 {len(cuts)} 个")
        except Exception as e:  # noqa: BLE001
            self.note(f"TransNet 失败({e})，退化为无切点")
        self.cuts_done.set()

    def _cuts_to_rows(self, cuts: list[float]) -> list[int]:
        rows = []
        for c in cuts:
            r = int(round(c / 1000.0 * ROW_HZ)) + 1   # 官方行钟：(i+1)/hz
            if 0 < r < self.total_rows:
                rows.append(r)
        return sorted(set(rows))

    # ---- 编码 ----
    def _ffmpeg_frames(self, video: str):
        cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", video,
               "-vf", f"fps={FPS_GRID},scale={W}:{H}", "-f", "rawvideo",
               "-pix_fmt", "rgb24", "-"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        fb = W * H * 3
        i = 0
        try:
            while True:
                raw = proc.stdout.read(fb)
                if len(raw) < fb:
                    break
                yield i / FPS_GRID * 1000.0 + 1000.0 / FPS_GRID,                     np.frombuffer(raw, np.uint8).reshape(H, W, 3)
                i += 1
        finally:
            try:
                proc.stdout.close()
            except OSError:
                pass
            if proc.poll() is None:
                proc.terminate()

    def _flush_group(self, buf, tbuf) -> int:
        limit = min(GROUP, len(buf) - K)
        if limit <= 0:
            return 0
        wins, metas, aa_all = [], [], []
        for j in range(N_ALIGN):
            aa = list(range(j, limit, 2 * K))[:CLIP_LEN // 2]
            if not aa:
                continue
            idx = [i for a in aa for i in (a, a + K)]
            wins.append(self.bundle.encode_window(np.stack([buf[i] for i in idx])))
            metas.append(np.array([(tbuf[a] + tbuf[a + K]) / 2.0 for a in aa]))
            aa_all.append(np.asarray(aa))
        if not wins:
            return 0
        order = np.argsort(np.concatenate(aa_all), kind="stable")
        feats = np.concatenate(wins)[order]
        times = np.concatenate(metas)[order]
        q = np.clip(np.round(feats / (8.0 / 127.0)), -127, 127).astype(np.int8)
        with self.lock:
            n = min(len(q), self.total_rows - self.n_rows)
            n0 = self.n_rows
            self.lat[n0:n0 + n] = q[:n]
            self.times_ms[n0:n0 + n] = times[:n]
            self.n_rows += n
        return n

    def _extract(self, trans: str) -> None:
        self.note("流式编码启动（ONNX，约 1.8x 实时）…")
        buf: list[np.ndarray] = []
        tbuf: list[float] = []
        for t_ms, frame in self._ffmpeg_frames(trans):
            if self.stop_flag.is_set():
                break
            buf.append(frame)
            tbuf.append(t_ms)
            if len(buf) - K < GROUP:
                continue
            self._flush_group(buf, tbuf)
            del buf[:GROUP], tbuf[:GROUP]
            self._pump()
            if self.n_rows >= self.total_rows:
                break           # 视频比任务长（例如传了整片转码）：够了就停
        if buf and not self.stop_flag.is_set() and self.n_rows < self.total_rows:
            # 定长图：补满一整组再 flush（官方 Rust 是窗口后滑，这里用重复末帧近似，
            # 只影响最后 ~2 秒的上下文）
            while len(buf) < GROUP + K:
                buf.append(buf[-1])
                tbuf.append(tbuf[-1] + 1000.0 / FPS_GRID)
            self._flush_group(buf, tbuf)
        self._pump(final=True)
        with self.buffer.lock:
            self.buffer.state = "done"

    # ---- 解码（增量 chunk + 样式） ----
    def _pump(self, final: bool = False) -> None:
        pack = self.bundle.pack
        if not self.cuts_done.is_set():
            return
        while True:
            with self.lock:
                n = self.n_rows
            if not final:
                if n < self._next + pack.chunk + pack.ctx:
                    return
                s, e = self._next, self._next + pack.chunk
                fs, fe = max(0, s - pack.ctx), min(e + pack.ctx, n)
            else:
                if n <= self._next:
                    break
                s, e = self._next, n
                mc = pack.min_chunk
                fs = s if (e - s) >= mc else max(0, s - (mc - (e - s)))
                fs = max(0, fs - pack.ctx)
                if e - fs < mc:
                    break
                fe = e
            cut = np.zeros(n, np.float32)
            for r in self.cut_rows:
                if r < n:
                    cut[r] = 1.0
            chunk = self.bundle.forward_chunk(self.lat, cut, fs, s, e, fe)
            with self.lock:
                for k, v in chunk.items():
                    self._trk[k][s:e] = v
            self._next = e
            self._style(e if not final else n)
            if final:
                break

    def _apply_still_gate(self, actions, times, n: int, vstd: float):
        """置信度+速度静止门：静音区不出点（research/vendor/vision/still_gate.py）。"""
        mode = self.still_gate
        if mode in ("", "off", "none", "0", "false"):
            return actions
        from still_gate import signals, gate_actions
        trk = {"vmarg": self._trk["vmarg"][:n], "conf": self._trk["conf"][:n]}
        vm, conf = signals(trk, vstd)
        # still_gate.keep_flags 用"秒"取窗（视频 at 是 ms，内部再 /1000）；
        # times 必须同样给秒，否则窗口落在 5ms 处、conf/速度全取 0，动作会被全删。
        kept, _stats = gate_actions(
            actions, np.asarray(times, float) / 1000.0, vm, conf,
            conf_thr=self.still_conf_thr, vm_thr=self.still_vm_thr,
            mode=mode, score_thr=self.still_score_thr)
        if len(kept) != len(actions):
            self.rev_bias_log.append(
                f"still_gate[{mode}] {len(actions)}->{len(kept)}")
        return kept

    def _style(self, sty: int) -> None:
        import sys
        here = Path(__file__).resolve().parent
        if str(here) not in sys.path:
            sys.path.insert(0, str(here))
        from goblintrain import common, jepa_infer

        vstd = self.bundle.pack.v_std
        n = int(sty)
        if n < 2:
            return
        times = self.times_ms[:n]
        jepa_infer.DT = float(np.median(np.diff(times))) / 1000.0
        jepa_infer.FPS = 1.0 / jepa_infer.DT
        vel = np.nan_to_num(self._trk["vmarg"][:n]) * vstd
        edges = np.array([0] + [r for r in self.cut_rows if r < n] + [n])
        for lo, hi in zip(edges[:-1], edges[1:]):
            if hi - lo >= 2 and np.isfinite(self._trk["vmarg"][hi - 2]):
                vel[hi - 1] = vel[hi - 2]
        # 前缀 rev_bias（与 torch 引擎同节奏）
        rows = n
        step = max(common.rows_at(30.0, ROW_HZ), rows // 2)
        if rows >= common.rows_at(30.0, ROW_HZ) and (
                not self._rev_bias_rows or rows - self._rev_bias_rows >= step):
            try:
                self._rev_bias = jepa_infer.fit_rev_bias(
                    {"rev_top": self._trk["rev_top"][:rows],
                     "rev_bot": self._trk["rev_bot"][:rows]}, vel[:rows], "stream")
                msg = f"rev_bias 已标定(前缀 {rows} 行 = {rows/ROW_HZ:.0f}s)"
                self.note(msg)
                self.rev_bias_log.append(msg)
            except Exception as e:  # noqa: BLE001
                self.rev_bias_log.append(f"标定失败: {e}")
            self._rev_bias_rows = rows
        bias = self._rev_bias
        if np.ndim(bias) and len(bias) < rows:
            bias = np.concatenate([bias, np.full(rows - len(bias), float(np.mean(bias)))])

        has_rev = bool(self.bundle.pack.heads.get("rev"))
        frc: list[int] | None = [] if has_rev else None
        sub: dict = {}
        p = jepa_infer.style_positions_composed(
            vel, self._trk["level"][:n], self._trk["env"][:n] * vstd, edges,
            band=(self._trk["blo"][:n], self._trk["bhi"][:n]),
            still_eps=jepa_infer.STILL_EPS,
            plat=(self._trk["plat_top"][:n], self._trk["plat_bot"][:n]),
            plat_thr=jepa_infer.PLAT_THR, plat_lo=jepa_infer.PLAT_LO,
            plat_peak=jepa_infer.PLAT_PEAK, plat_veto=jepa_infer.PLAT_VETO,
            plat_soft=jepa_infer.PLAT_SOFT, plat_rail_track=True,
            plat_shift_cap=jepa_infer.PLAT_SHIFT_CAP,
            rev=(self._trk["rev_top"][:n], self._trk["rev_bot"][:n]),
            rev_snap=common.rows_at(common.REV_SNAP_S, ROW_HZ),
            times_ms=times, subframe=jepa_infer.SUBFRAME, sub_out=sub,
            ext_snap=jepa_infer.EXT_SNAP, amp_cap_x=jepa_infer.AMP_CAP_X,
            rev_source=jepa_infer.REV_SOURCE, rev_bias=bias,
            rev_gap=common.rows_at(common.EVENT_GAP_S, ROW_HZ), force_out=frc)
        actions = jepa_infer.extrema_actions(
            p, times, edges, sub=sub if sub else None, force=frc,
            cut_ease=jepa_infer.CUT_EASE, rdp_eps=jepa_infer.RDP_EPS, stats={})
        actions = self._apply_still_gate(actions, times, n, vstd)
        spd = [(x, y) for x, y in zip(actions, actions[1:]) if y["at"] > x["at"]]
        if spd:
            over = [abs(y["pos"] - x["pos"]) * 1000.0 / (y["at"] - x["at"])
                    for x, y in spd]
            if max(over) > common.MAX_POS_RATE:
                cl = common.clamp_speed([(a["at"], a["pos"]) for a in actions])
                for a, (_t, pos) in zip(actions, cl):
                    a["pos"] = int(pos)
        stable = float(times[-1])
        self.buffer.replace_all(actions, stable)
        if self.buffer.state == "warming":
            self.buffer.state = "running"
            self.note(f"首批动作已出({len(actions)} 个)，进入跟随模式")
