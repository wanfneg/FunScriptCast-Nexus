"""流式识别引擎 —— 边播边推理。

管线（全部复用 goblintrain 的组件与常量, 前向分块对齐官方 deploy 语义）:
  ffmpeg 解码(NVDEC+scale_cuda, 回退 CPU; vf=fps=30,scale=-2:480,scale=384:384)
    → V-JEPA 滑窗提取(extract.py 的对齐交织逻辑, 64帧组 → 64行, batch=2)
    → latent int8 memmap(磁盘, 绝对行号) + h0 增量缓存(前端逐点输出)
    → 分块 trunk+heads(单 span [kept-ctx, kept+保留+ctx], TCN 不冷启动)
    → style_positions_composed + extrema_actions(jepa_infer 原函数;
      调用前必须重绑 jepa_infer.FPS/DT 为缓存真实行率)
    → ActionBuffer(单调水位 gen_until_ms, 播放侧只消费稳定前缀)

延迟预算(本机 RTX 4070 Laptop): 编码 2.5x 实时 + 开场 TransNetV2 ~30s ∥ 提取,
首个动作块约 开场后 40~60s 出现, 此后缓冲单调增长(2.5x > 1x 播放)。
seek 向前越过 gen 头 → 解码流 -ss 跳转, 从目标点继续(洞=未生成, 玩具闲置)。

v1 已知取舍:
  - rev_bias 用前缀流式拟合(官方 fit_rev_bias 的前缀等价, 见 REV_BIAS_*),
    前缀不足 REV_BIAS_WARMUP_S 时维持 0
  - 转码语义用单 vf 链近似(少一次 x264 往返), 与官方解码有轻微像素差
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
import warnings
from pathlib import Path

import numpy as np

warnings.filterwarnings("ignore", category=FutureWarning)  # timm/sdp 弃用提示刷屏

GTG = Path(__file__).resolve().parent.parent / "_third_party" / "GoblinTrainingGrounds"
import sys

sys.path.insert(0, str(GTG))

from goblintrain import common                     # noqa: E402
from goblintrain import jepa_infer                 # noqa: E402
from goblintrain import project as gproject        # noqa: E402
from goblintrain.extract import Basis, CLIP_LEN    # noqa: E402
from goblintrain.jepa_train import load_model      # noqa: E402
from goblintrain.encoders import Encoder           # noqa: E402

FPS_GRID = 30.0
K = 2                  # tubelet_stride
N_ALIGN = 4
ROW_HZ = FPS_GRID * N_ALIGN / (2.0 * K)     # 30
GROUP = (CLIP_LEN // 2) * 2 * K            # 64 帧每组
DIM, GRID = 64, 24
INT8_SCALE = 8.0 / 127.0
THROTTLE_AHEAD_MS = 240_000   # 缓冲领先播放头超过 4 分钟就让提取睡觉
WINS_PER_BATCH = 4            # B 维批 4 窗实测峰值 ~1.3GB(旧"溢出"结论是 T 维堆叠语义)
PERF = {"flush": 0.0, "h0": 0.0, "decode": 0.0}   # 粗粒度耗时打点(秒)
REV_BIAS_WARMUP_S = 90.0      # rev_bias 首次拟合的最小前缀(秒)
REV_BIAS_REFIT_S = 30.0       # 两次重拟合的最小行距(秒)


class ActionBuffer:
    """动作缓冲: 播放侧消费稳定前缀, 生成侧只追加/替换水位之后的部分。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.actions: list[dict] = []       # 按 at 升序, 全部 < gen_until_ms
        self.gen_until_ms = 0.0
        self.state = "idle"                 # idle/warming/running/done/error
        self.note = ""

    def publish(self, new_actions: list[dict], stable_ms: float) -> None:
        """合并新解码出的动作(只接收 at < stable_ms 的), 推进水位。"""
        with self.lock:
            have = {a["at"] for a in self.actions}
            for a in new_actions:
                if a["at"] < stable_ms and a["at"] not in have:
                    self.actions.append(a)
            self.actions.sort(key=lambda a: a["at"])
            self.gen_until_ms = max(self.gen_until_ms, stable_ms)

    def query(self, t_ms: float) -> int | None:
        """返回 t 之前最近动作点的下标语义(index_after), 无覆盖返回 None。"""
        with self.lock:
            if t_ms >= self.gen_until_ms:
                return None
            import bisect
            ats = [a["at"] for a in self.actions]
            i = bisect.bisect_right(ats, t_ms)
            return i if 0 < i < len(ats) else None

    def snapshot(self) -> tuple[list[dict], float]:
        with self.lock:
            return list(self.actions), self.gen_until_ms


def _open_chain(video: str, fps: float, start_s: float, w: int, h: int, hw: bool):
    """hw=True 走 NVDEC+scale_cuda(GPU 缩放, 只回传 384² 小帧), 否则 CPU swscale。"""
    vf = (f"fps={fps},scale_cuda=-2:480:format=nv12,scale_cuda={w}:{h}:format=nv12,"
          f"hwdownload,format=nv12,format=rgb24") if hw else \
         f"fps={fps},scale=-2:480:bicubic,scale={w}:{h}"
    cmd = ["ffmpeg", "-v", "error"]
    if hw:
        cmd += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    cmd += ["-ss", f"{max(0.0, start_s):.3f}", "-i", video,
            "-vf", vf, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    nbytes = h * w * 3
    fidx0 = int(round(start_s * fps))
    state = {"i": 0}

    def gen():
        assert proc.stdout is not None
        try:
            while True:
                raw = proc.stdout.read(nbytes)
                if len(raw) < nbytes:
                    break
                yield (fidx0 + state["i"]) / fps * 1000.0, \
                    np.frombuffer(raw, np.uint8).reshape(h, w, 3)
                state["i"] += 1
        finally:
            # 生成器被放弃(如 transnet 只读到 max_s)时也要收掉 ffmpeg
            try:
                proc.stdout.close()
            except OSError:
                pass
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

    return gen, proc


def _ffmpeg_decode(video: str, fps: float, start_s: float, w: int, h: int):
    """输出契约同 goblintrain.perception.decode_frames(绝对毫秒, rgb24 帧)。
    优先 GPU 链路(NVDEC+scale_cuda); 首帧读不到(滤镜缺失等)退回 CPU 链。"""
    for hw in (True, False):
        gen_fn, proc = _open_chain(video, fps, start_s, w, h, hw)
        g = gen_fn()
        first = next(g, None)
        if first is not None:
            def chained(first=first, g=g):
                yield first
                yield from g
            return chained(), proc
        try:
            proc.terminate()
        except OSError:
            pass
    raise RuntimeError(f"ffmpeg 解码链路不可用: {video}")


class StreamEngine:
    def __init__(self, video: str, duration_ms: float, device: str = "cuda",
                 ckpt: str | None = None, on_note=None):
        self.video = video
        self.duration_ms = duration_ms
        self.device = device
        self.total_rows = int(duration_ms / 1000.0 * ROW_HZ) + 8
        self.ckpt = Path(ckpt) if ckpt else Path(common.DEFAULT_CKPT)
        self.buffer = ActionBuffer()
        self.on_note = on_note or (lambda s: None)
        # 共享状态
        self.lat_path = Path(video).with_suffix(".stream_lat.bin")
        self.lat = np.memmap(self.lat_path, dtype=np.int8, mode="w+",
                             shape=(self.total_rows, DIM, GRID, GRID))
        self.times_ms = np.zeros(self.total_rows, np.float64)
        self.cuts_ms: list[float] = []
        self.n_rows = 0                # 已提取行数
        self.lock = threading.Lock()
        self.stop_flag = threading.Event()
        self._jump_target_ms: float | None = None
        self._threads: list[threading.Thread] = []
        # 优化: h0 增量缓存 / 并行切分 / 节流
        self.cuts_done = threading.Event()
        self.h0_t = None                  # torch (T, h_dim) fp32 CPU
        self.h0_rows = 0                  # 已构建 h0 的行数(单调)
        self.model = None
        self._playhead_ms = 0.0
        # rev_bias 流式拟合状态(官方 run() 的 fit_rev_bias 前缀等价)
        self._rev_bias: float | np.ndarray = 0.0   # 逐行发射先验; 0.0=未校准
        self._rev_bias_at = 0                      # 上次拟合时的 gen_end(行)

    def set_playhead(self, t_ms: float) -> None:
        """播放侧推送当前播放头(节流判据)。"""
        self._playhead_ms = t_ms

    # ---- 生命周期 ----
    def start(self) -> None:
        t = threading.Thread(target=self._run, daemon=True, name="stream-engine")
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

    def note(self, s: str) -> None:
        self.on_note(s)

    # ---- 主流程 ----
    def _run(self) -> None:
        try:
            import torch
            self.note("加载模型(官方 v0.6.0 checkpoint)...")
            model, ck = load_model(str(self.ckpt), self.device)
            model.pos_temp = jepa_infer.POS_TEMP
            model.env_seed = 0
            self.model = model
            if not (ck["arch"].get("pos_head", False)
                    and ck["arch"].get("gen_env", False)):
                raise RuntimeError("checkpoint 缺 pos/gen_env 头")
            self.note("加载 V-JEPA 编码器(torch.hub)...")
            enc = Encoder(gproject.PERCEPTION["encoder"], self.device)
            basis = Basis(gproject.PERCEPTION, self.device)
            self.h0_t = torch.zeros(self.total_rows, model.inp.out_channels)

            # 切分与提取并行: cut 旗标只在解码块用, 提取是 cut-blind 的
            t = threading.Thread(target=self._cuts_worker, daemon=True)
            t.start()
            self._stream(model, ck, enc, basis)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            with self.buffer.lock:
                self.buffer.state = "error"
                self.buffer.note = str(e)

    def _cuts_worker(self) -> None:
        self.cuts_ms = self._shot_cuts()
        self.note(f"镜头切分完成: {len(self.cuts_ms)} 个切点, 回填早期 h0 的 cut 旗标...")
        # 切分前算的 h0 用的是 cut=0: 从 latent(memmap) 重算这些行的 h0
        with self.lock:
            snapshot = self.h0_rows
        r0 = 0
        step = 512
        while r0 < snapshot and not self.stop_flag.is_set():
            r1 = min(r0 + step, snapshot)
            with self.lock:
                q = np.array(self.lat[r0:r1])
            self._build_h0(r0, r1, q)
            r0 = r1
        self.cuts_done.set()
        self.note("cut 旗标回填完成, 识别进入全速")

    def _shot_cuts(self) -> list[float]:
        """TransNetV2 整片切点(毫秒)。解码走 GPU 链(和提取同一套, 带 CPU 回退)。"""
        try:
            from goblintrain.boundaries import analyze_video, load_transnet
            import goblintrain.perception as P
            orig = P.decode_frames

            def hw_decode(video_path, w=P.DECODE_W, h=P.DECODE_H,
                          fps=P.FPS, start_s=0.0, max_s=None, crop=None):
                if crop or max_s is not None:
                    return orig(video_path, w=w, h=h, fps=fps,
                                start_s=start_s, max_s=max_s, crop=crop)
                gen, proc = _open_chain(str(video_path), fps, start_s, w, h, True)
                first = next(gen(), None)
                if first is None:
                    try:
                        proc.terminate()
                    except OSError:
                        pass
                    return orig(video_path, w=w, h=h, fps=fps, start_s=start_s)
                def chained():
                    yield first
                    yield from gen()
                return chained()

            P.decode_frames = hw_decode
            try:
                tn = load_transnet(self.device)
                payload = analyze_video("live", self.video,
                                        self.duration_ms, tn, self.device)
                return [float(c) for c in payload["cuts_ms"]]
            finally:
                P.decode_frames = orig
        except Exception as e:  # noqa: BLE001
            self.note(f"TransNetV2 失败({e}), 退化为无切点")
            return []

    def _edges_rows(self) -> list[int]:
        cuts = sorted(self.cuts_ms)
        rows = [int(round(c / 1000.0 * ROW_HZ)) for c in cuts]
        from goblintrain.boundaries import MIN_GAP_S
        merged = [r for i, r in enumerate(rows)
                  if i == 0 or r - rows[i - 1] >= common.rows_at(MIN_GAP_S, ROW_HZ)]
        return [0] + merged + [self.total_rows]

    # ---- 提取(生成线程推进 n_rows)与推理共用锁 ----
    def _stream(self, model, ck, enc, basis) -> None:
        import torch
        kept = 0                     # 已发布动作的稳定行界
        publish_rows = common.rows_at(8.5333, ROW_HZ)   # 256 行 = 8.5s 视频/批
        ctx_rows = common.rows_at(common.DECODE_CTX_S, ROW_HZ)
        guard = common.rows_at(1.0667, ROW_HZ)          # 32 行 = 1.07s 发布保护
        first = True
        gen_iter, proc = _ffmpeg_decode(self.video, FPS_GRID, 0.0, 384, 384)
        buf: list[np.ndarray] = []
        tbuf: list[float] = []
        self.buffer.state = "warming"
        self.note("识别流水线启动(编码约 2x 实时, 切分并行)...")
        t0 = time.time()
        try:
            while not self.stop_flag.is_set():
                # 节流: 缓冲领先播放头太多时暂停读帧(管道背压让 ffmpeg 停下)
                if self.buffer.state == "running":
                    ahead = self.buffer.gen_until_ms - self._playhead_ms
                    if ahead > THROTTLE_AHEAD_MS:
                        time.sleep(1.0)
                        continue
                item = next(gen_iter, None)
                if item is None:
                    break
                t_ms, frame = item
                buf.append(frame)
                tbuf.append(t_ms)
                if len(buf) - K < GROUP:
                    continue
                n_new = self._flush_group(buf, tbuf, enc, basis, torch)
                del buf[:GROUP], tbuf[:GROUP]
                if n_new:
                    # 攒够一个解码块就推一节
                    if self.n_rows - kept >= publish_rows or (first and self.n_rows >= 384):
                        kept = self._decode_chunk(
                            model, ck, kept, publish_rows, ctx_rows, guard, first)
                        first = False
        finally:
            try:
                proc.terminate()
            except OSError:
                pass
        # EOF 收尾(官方 extract.end() 的流式等价, extract.py:204-215): 末 K
        # 帧补静态对('the final k frames still own a row')并排空提取缓冲,
        # 最后 2~65 帧的行不再丢; final 解码放开 2s 下限(官方对 <2s 尾段用
        # lookback 前向, forward.py:60-67, 这里由左 ctx 承担)
        if buf and not self.stop_flag.is_set():
            for _i in range(K):
                buf.append(buf[-1])
                tbuf.append(tbuf[-1] + 1000.0 / FPS_GRID)
            while True:
                limit = min(GROUP, len(buf) - K)
                if limit <= 0:
                    break
                self._flush_group(buf, tbuf, enc, basis, torch)
                del buf[:limit], tbuf[:limit]
        # 解码进程异常退出 != 正常 EOF: 官方特意防"截断产出被当整片"
        rc = proc.poll()
        if rc not in (0, None) and not self.stop_flag.is_set():
            self.note(f"警告: 解码进程异常退出 rc={rc}, 产出可能截断")
            with self.buffer.lock:
                self.buffer.state = "error"
                self.buffer.note = f"decoder rc={rc}"
        # 尾段解码: 全部剩余行
        with self.lock:
            tail = self.n_rows
        if tail > kept:
            self._decode_chunk(model, ck, kept, publish_rows, ctx_rows,
                               0, first, final=True)
        with self.buffer.lock:
            self.buffer.state = "done"
        self.note(f"全片识别完成, 用时 {time.time()-t0:.0f}s")

    def _flush_group(self, buf, tbuf, enc, basis, torch) -> int:
        """extract.py flush() 的滚动版(批处理): 4 个对齐窗口合成一个 batch
        过编码器, 64 帧 → 64 行, 交织回视频顺序。"""
        limit = min(GROUP, len(buf) - K)
        if limit <= 0:
            return 0
        wins, metas, aa_all = [], [], []
        for j in range(N_ALIGN):
            aa = list(range(j, limit, 2 * K))[:CLIP_LEN // 2]
            if not aa:
                continue
            idx = [i for a in aa for i in (a, a + K)]
            wins.append(np.stack([buf[i] for i in idx]))
            metas.append(np.array([(tbuf[a] + tbuf[a + K]) / 2.0 for a in aa]))
            aa_all.append(np.asarray(aa))
        if not wins:
            return 0
        n_win = len(wins)
        t0 = time.time()
        y_list = []
        for b0 in range(0, n_win, WINS_PER_BATCH):
            sub = wins[b0:b0 + WINS_PER_BATCH]
            m = len(sub)
            x = torch.from_numpy(np.concatenate(sub)).to(self.device)  # (m*32, H, W, 3)
            with torch.no_grad():
                # 官方 tokens() 把窗口沿时间维 [None] 堆叠(B=1, T=m*32): 注意力
                # 对 T 平方。改沿批维 (B, 3, 32, H, W) —— 同帧实测 -34% 耗时,
                # 归一化/RoPE补丁/输出形状与 tokens() 逐值一致。
                v = x.permute(0, 3, 1, 2).float() / 255.0
                v = ((v - enc._mean) / enc._std).half()
                yr = enc.model(v.view(m, CLIP_LEN, 3, enc.res, enc.res)
                               .permute(0, 2, 1, 3, 4))
                if isinstance(yr, (tuple, list)):
                    yr = yr[-1]
            y = yr.reshape(m, CLIP_LEN // 2, enc.grid, enc.grid, enc.dim)
            for i in range(m):
                ya = (y[i] - basis.mu) @ basis.W    # (16, 64, 24, 24)
                y_list.append(ya.permute(0, 3, 1, 2).cpu().numpy().astype(np.float16))
        order = np.argsort(np.concatenate(aa_all), kind="stable")
        feats = np.concatenate(y_list)[order]
        times = np.concatenate(metas)[order]
        q = np.clip(np.round(feats.astype(np.float32) / INT8_SCALE), -127, 127).astype(np.int8)
        with self.lock:
            n = min(len(q), self.total_rows - self.n_rows)
            n0 = self.n_rows
            self.lat[n0:n0 + n] = q[:n]
            self.times_ms[n0:n0 + n] = times[:n]
            self.n_rows += n
        # h0 增量: 每组立刻算(cut 旗标未就绪时先按 0, 切分完成后回填重算)
        if n > 0:
            self._build_h0(n0, n0 + n, q[:n])
        PERF["flush"] += time.time() - t0
        return n

    def _build_h0(self, n0: int, n1: int, q: np.ndarray) -> None:
        """对 [n0,n1) 的 latent 行跑一次点状前端, 存 fp32 h0(官方 live 路径)。"""
        import torch
        if n1 <= n0 or self.model is None:
            return
        t0 = time.time()
        x = torch.from_numpy(q[:n1 - n0]).to(self.device).float()
        x.mul_(INT8_SCALE)
        cut = torch.from_numpy(self._cut_arr(max(n1, 1))[:n1][n0:]).to(self.device)
        with torch.no_grad():
            h, _gate = self.model.frontend(x[None], cut[None])
        # frontend 返回 (b, 256, w) 通道优先; 存储布局按 (w, 256) 行主序
        self.h0_t[n0:n1] = h[0].transpose(0, 1).float().cpu()
        with self.lock:
            self.h0_rows = max(self.h0_rows, n1)
        PERF["h0"] += time.time() - t0

    def _cut_arr(self, upto: int) -> np.ndarray:
        arr = np.zeros(upto, np.float32)
        for r in self._edges_rows()[1:-1]:
            if r < upto:
                arr[r] = 1.0
        return arr

    def _flush_pending_h0(self) -> None:
        pass  # 已被 _cuts_worker 的回填逻辑取代

    def _decode_chunk(self, model, ck, kept,
                      publish_rows, ctx_rows, guard, first, final=False) -> int:
        """对 [kept, gen_end) 做一次带上下文的前向 + 样式解码, 发布稳定动作。"""
        import torch
        t0 = time.time()
        with self.lock:
            gen_end = self.n_rows
        gen_end = min(gen_end, self.total_rows)
        s, e = kept, (gen_end if final else min(gen_end, kept + publish_rows))
        if not final and e - s < common.rows_at(2.0, ROW_HZ):
            return kept  # 非 final 的碎段跳过; final 尾段再小也要解码
        # 前向: 单 span、ctx 两侧(官方分块语义, TCN 不冷启动)。
        fs = max(0, s - ctx_rows)
        fe = min(gen_end, e + ctx_rows)
        if fe - fs < common.rows_at(2.0, ROW_HZ):
            return kept
        # 样式分段只用真实剪缝(官方: still 门/Viterbi/refractory/ease/slew 都
        # 按镜头段重置, 伪边界每 8.5s 劈开冲程并重置状态机)。fs 并入首段:
        # [fs, s) 的轨道真值在数组里, 动作照样合成, 发布仍由 stable 过滤。
        edges = [min(r, e) for r in self._edges_rows() if fs <= r <= e]
        if not edges or edges[0] > fs:
            edges = [fs] + edges
        if edges[-1] < e:
            edges.append(e)
        edges = sorted(set(edges))

        class _DS:
            """predict_shots 需要的最小数据集协议(取 latents memmap)。"""
            row_hz = ROW_HZ
            feat_scale = INT8_SCALE
            masked = False
            times_ms = None

            def __init__(self, eng):
                self.eng = eng
                self.vel = np.zeros(eng.total_rows, np.float32)  # 只用 len

            def fwin(self, fs, fe):
                a = np.array(self.eng.lat[fs:fe])          # int8 (W,64,24,24)
                return torch.from_numpy(a)

            cut = None

        ds = _DS(self)
        ds.times_ms = self.times_ms[:gen_end]
        ds.cut = torch.from_numpy(self._cut_arr(gen_end))
        ds.h0_t = self.h0_t

        pred, tracks = _predict_shots_live(
            model, ds, [(fs, fe)], self.device, kept_end=e)

        pred = tracks["vmarg"]
        # 官方 run() 语义(jepa_infer.py:1479-1484): 每个真实剪缝前的末行
        # 保留前行速度, 缝上不铸造幻影交叉。只认真实剪缝行(_edges_rows 内部
        # 边界), 不含块游标 s/e 这类假边界。
        for r in self._edges_rows()[1:-1]:
            if s < r <= e and r >= 2 and np.isfinite(pred[r - 2]):
                pred[r - 1] = pred[r - 2]
        vel_pred = np.nan_to_num(pred) * ck["v_std"]
        stable_row = e - (0 if final else guard)
        # 官方(jepa_infer.py:1501/1524/1530): viterbi 判定的 apex 行收集进
        # frc, 经 force 交给 extrema_actions 强制成顶点, 平滑/RDP 不得抹去
        frc: list = []
        sub: dict = {}
        # 关键: 样式解码的模块全局 FPS/DT 必须按缓存真实行率重绑(官方 run()
        # 每 clip 都做; 默认 15Hz 会让 30Hz 网格上行程/速度全部错 2 倍)
        jepa_infer.DT = float(np.median(np.diff(self.times_ms[:gen_end]))) / 1000.0
        jepa_infer.FPS = 1.0 / jepa_infer.DT
        # 官方 run()(jepa_infer.py:1503-1505)有 rev 头时以 fit_rev_bias 校准
        # 交替解码发射先验(bias=0 会 under-emits, 慢行程被并进保持段)。
        # 流式等价: 前缀足够长即拟合, 每 REV_BIAS_REFIT_S 重拟合; 必须在
        # FPS 重绑之后调(函数内部用模块全局 FPS 换算行距)。
        behind = self.buffer.gen_until_ms - self._playhead_ms
        if (gen_end >= common.rows_at(REV_BIAS_WARMUP_S, ROW_HZ)
                and gen_end - self._rev_bias_at >= common.rows_at(REV_BIAS_REFIT_S, ROW_HZ)
                and behind >= 60_000   # 缓冲富余 <60s 时跳过, 拟合不再停摆水位
                and np.isfinite(tracks["rev_top"][:gen_end]).any()):
            try:
                # 拟合窗口裁到最近 200s(6000 行): 官方 BIAS_FIT_S 本就是上限
                # 语义; 全前缀成本线性增长(T=22000 行实测 7.4s)会周期性停摆水位
                w0 = max(0, gen_end - common.rows_at(200.0, ROW_HZ))
                self._rev_bias = jepa_infer.fit_rev_bias(
                    {"rev_top": tracks["rev_top"][w0:gen_end],
                     "rev_bot": tracks["rev_bot"][w0:gen_end]},
                    vel_pred[w0:gen_end], "stream")
                self._rev_bias_at = gen_end
                self.note(f"rev_bias 已拟合(前缀 {gen_end} 行)")
            except Exception as e:  # noqa: BLE001
                self.note(f"rev_bias 拟合失败, 维持先验 0: {e}")
        bias = self._rev_bias
        if np.ndim(bias) and len(bias) < gen_end:
            bias = np.concatenate([bias, np.zeros(gen_end - len(bias))])
        p = jepa_infer.style_positions_composed(
            vel_pred, tracks["level"], tracks["env"] * ck["v_std"],
            np.array(edges),
            band=(tracks.get("blo"), tracks.get("bhi")),
            still_eps=jepa_infer.STILL_EPS,
            plat=((tracks.get("plat_top"), tracks.get("plat_bot"))),
            plat_thr=jepa_infer.PLAT_THR, plat_lo=jepa_infer.PLAT_LO,
            plat_peak=jepa_infer.PLAT_PEAK, plat_veto=jepa_infer.PLAT_VETO,
            plat_soft=jepa_infer.PLAT_SOFT, plat_rail_track=True,
            plat_shift_cap=jepa_infer.PLAT_SHIFT_CAP,
            rev=(tracks.get("rev_top"), tracks.get("rev_bot")),
            rev_snap=common.rows_at(common.REV_SNAP_S, ROW_HZ),
            times_ms=self.times_ms[:gen_end],
            subframe=jepa_infer.SUBFRAME, sub_out=sub,
            ext_snap=jepa_infer.EXT_SNAP, amp_cap_x=jepa_infer.AMP_CAP_X,
            rev_source=jepa_infer.REV_SOURCE, rev_bias=bias,
            rev_gap=common.rows_at(common.EVENT_GAP_S, ROW_HZ),
            force_out=frc, phase_out=None)
        actions = jepa_infer.extrema_actions(
            p, self.times_ms[:gen_end], np.array(edges),
            sub=sub if sub else None, force=frc,
            cut_ease=jepa_infer.CUT_EASE, rdp_eps=jepa_infer.RDP_EPS,
            stats={})
        spd = [(x, y) for x, y in zip(actions, actions[1:]) if y["at"] > x["at"]]
        if spd:
            over = [abs(y["pos"] - x["pos"]) * 1000.0 / (y["at"] - x["at"])
                    for x, y in spd]
            if max(over) > common.MAX_POS_RATE:
                cl = common.clamp_speed([(a["at"], a["pos"]) for a in actions])
                for a, (_t, pos) in zip(actions, cl):
                    a["pos"] = int(pos)
        # 防越界: VFR/实际帧数多于 ffprobe 时长估计时 n_rows 可达 total_rows,
        # final 块 stable_row=e=total_rows 会使 times_ms[stable_row] 越界
        stable_ms = (float(self.times_ms[min(stable_row, len(self.times_ms) - 1)])
                     if stable_row > 0 else 0.0)
        self.buffer.publish(actions, stable_ms)
        PERF["decode"] += time.time() - t0
        if self.buffer.state == "warming":
            self.buffer.state = "running"
            self.note(f"首批动作已出({len(actions)} 个), 进入跟随模式")
        # 游标只推进到已发布边界: margin 区留给下一块带新上下文重推,
        # 否则每个块的 margin 段永久丢动作(表现为周期性无动作)
        return max(kept, stable_row)

    # ---- seek: 跳转解码流(洞 = 未生成) ----
    def request_jump(self, t_ms: float) -> None:
        """v1: 记录跳转目标, 提取线程在下一组边界 -ss 重开。"""
        self._jump_target_ms = t_ms
        self.note(f"seek → {t_ms/1000:.0f}s, 识别流跳转")


def _predict_shots_live(model, ds, spans, device, kept_end=None):
    """forward.predict_shots 的流式等价: 单 span 已含两侧 ctx, 全段保留轨道。"""
    import torch
    T = len(ds.vel)
    tracks: dict[str, np.ndarray] = {
        "level": np.full(T, np.nan), "env": np.full(T, np.nan),
        "blo": np.full(T, np.nan), "bhi": np.full(T, np.nan),
        "plat_top": np.full(T, np.nan), "plat_bot": np.full(T, np.nan),
        "rev_top": np.full(T, np.nan), "rev_bot": np.full(T, np.nan),
        "vmarg": np.full(T, np.nan),
    }
    with torch.no_grad():
        for s, e in spans:
            fs, fe = s, e
            if e - fs < 2:
                continue
            model.env_row0 = int(fs)
            # 包络递归止于保留边界(kept_end): 尾部 ctx 只喂 TCN, 不进包络
            model.env_rows = int((kept_end if kept_end is not None else e) - fs)
            h0 = getattr(ds, "h0_t", None)
            if h0 is not None:
                # h0 路径: 前端逐点输出已缓存, 这里只跑 TCN+heads
                front = (h0[fs:fe][None].to(device).float().transpose(1, 2), None)
                v, _conf, _gate = model(front=front)
            else:
                x = ds.fwin(fs, fe)[None].to(device).float()
                x.mul_(ds.feat_scale)
                cut = ds.cut[fs:fe][None].to(device)
                v, _conf, _gate = model(x, cut=cut)
            sl = slice(0, e - s)   # fs==s, 已裁好 ctx, 全段保留
            tracks["level"][s:e] = model.level[0].float().cpu().numpy()[sl]
            tracks["env"][s:e] = model.eg[0].float().cpu().numpy()[sl]
            tracks["blo"][s:e] = model.ext_lo[0].float().cpu().numpy()[sl]
            tracks["bhi"][s:e] = model.ext_hi[0].float().cpu().numpy()[sl]
            pp = model.plat_logits.softmax(1)[0].float().cpu().numpy()
            tracks["plat_top"][s:e] = pp[1, sl.start:sl.stop]
            tracks["plat_bot"][s:e] = pp[2, sl.start:sl.stop]
            rp = model.rev_logits.softmax(1)[0].float().cpu().numpy()
            tracks["rev_top"][s:e] = rp[1, sl.start:sl.stop]
            tracks["rev_bot"][s:e] = rp[2, sl.start:sl.stop]
            tracks["vmarg"][s:e] = \
                model.v_marginal[0].float().cpu().numpy()[sl]
    return tracks["vmarg"], tracks
