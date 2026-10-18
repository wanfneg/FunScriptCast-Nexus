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

# vendored: 本模块自带 goblintrain(官方 MIT 代码的推理子集), 不依赖外部仓库。
# 模型权重在 weights/checkpoints/, 首次使用由 tools/fetch_vision.ps1 拉取。
BASE = Path(__file__).resolve().parent
GTG = BASE
import sys

sys.path.insert(0, str(BASE))

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

# rev_bias(反转发射先验)的流式标定节奏。官方 fit_rev_bias 的窗是
# min(T, rows_at(BIAS_FIT_S, FPS)), 而 BIAS_FIT_S 是**秒**(8000s) —— 对任何
# 实际片长都不封顶, 即官方是用全片轨道标定的。流式没有全片, 只能在"当前
# 前缀"上标定并随前缀增长重拟合(唯一能边播边收敛的做法)。前缀越短拟合越
# 便宜, 所以早期标定密、后期疏。
# 注意: bias=0 会让联合 Viterbi 大幅欠发射(官方 export_bundle 记
# "a bias of 0 under-emits badly"), 实测表现为整段镜头被样式机 park 成
# still —— 发布密度掉到官方的 ~30% 且出现 200s 级空洞。
BIAS_MIN_S = 30.0             # 前缀够 30s 就先标定一次, 别让开场裸跑
BIAS_REFIT_MIN_S = 30.0       # 重标定间隔下限
BIAS_REFIT_DIV = 2            # 间隔 = max(下限, 前缀行数/2) —— 即"前缀每长 50%
                              # 重标定一次"(几何级数, 全片约 10 次)。拟合成本
                              # O(前缀)且与间隔同阶, 所以总开销有界(实测 ~6%);
                              # 而后期重标定几乎不改先验(实测两 band 只动
                              # 0.01~0.06), 密集重标定是纯浪费。


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

    def replace_all(self, new_actions: list[dict], gen_until_ms: float) -> None:
        """官方式全前缀重样式: 每次用完整重算的动作整体替换缓冲。
        前缀随内容增长单调收敛于官方全片解码的结果。"""
        with self.lock:
            self.actions = sorted(new_actions, key=lambda a: a["at"])
            self.gen_until_ms = gen_until_ms

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


def _open_chain(video: str, fps: float, start_s: float, w: int, h: int, hw: bool,
                pre_scaled: bool = False):
    """hw=True 走 NVDEC+scale_cuda(GPU 缩放, 只回传 384² 小帧), 否则 CPU swscale。
    pre_scaled=True: 输入已是 480p30 转码产物, 直接一步缩到目标尺寸。"""
    vf = (f"fps={fps},scale_cuda=-2:480:interp_algo=2,scale_cuda={w}:{h}:interp_algo=2,"
          f"hwdownload,format=nv12,format=rgb24") if hw else \
         (f"fps={fps},scale={w}:{h}" if pre_scaled else
          f"fps={fps},scale=-2:480:bicubic,scale={w}:{h}")
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


def _ffmpeg_decode(video: str, fps: float, start_s: float, w: int, h: int,
                   pre_scaled: bool = False):
    """输出契约同 goblintrain.perception.decode_frames(绝对毫秒, rgb24 帧)。
    优先 GPU 链路(NVDEC+scale_cuda); 首帧读不到(滤镜缺失等)退回 CPU 链。"""
    for hw in (False,):   # A/B 定论: scale_cuda 像素与训练分布不符致幅度减半, 暂回 swscale
        gen_fn, proc = _open_chain(video, fps, start_s, w, h, hw, pre_scaled)
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
                 ckpt: str | None = None, on_note=None,
                 cache_dir: str | Path | None = None):
        self.video = video
        self.duration_ms = duration_ms
        self.device = device
        self.total_rows = int(duration_ms / 1000.0 * ROW_HZ) + 8
        _local_ckpt = BASE / "weights" / "checkpoints" / "v0.6.0.pt"
        self.ckpt = Path(ckpt) if ckpt else (_local_ckpt if _local_ckpt.is_file()
                                             else Path(common.DEFAULT_CKPT))
        self.buffer = ActionBuffer()
        self.on_note = on_note or (lambda s: None)
        # 共享状态。缓存默认落在 cache_dir(宿主传数据目录), 不写用户媒体目录:
        # latent 有 ~36KB/行(22 分钟片 ≈ 1.4GB), 转码产物另算。
        self.cache_dir = Path(cache_dir) if cache_dir else Path(video).parent
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        stem = Path(video).stem
        self.lat_path = self.cache_dir / f"{stem}.lat.bin"
        self.lat = np.memmap(self.lat_path, dtype=np.int8, mode="w+",
                             shape=(self.total_rows, DIM, GRID, GRID))
        self.times_ms = np.zeros(self.total_rows, np.float64)
        self._trk_keys = ("vmarg", "level", "env", "blo", "bhi",
                          "plat_top", "plat_bot", "rev_top", "rev_bot")
        self._trk = {k: np.full(self.total_rows, np.nan, np.float32)
                     for k in self._trk_keys}
        self.cuts_ms: list[float] = []
        self.extract_video: str | None = None
        self.n_rows = 0                # 已提取行数
        self.lock = threading.Lock()
        self.stop_flag = threading.Event()
        self._jump_target_ms: float | None = None
        self._threads: list[threading.Thread] = []
        # 优化: h0 增量缓存 / 并行切分 / 节流
        self.cuts_done = threading.Event()
        self._needs_retrack = False       # 真 cut 到位后需要重算前缀轨道
        # GPU 前向互斥: 模型内部有 CUDA graph capture(_env_graph_capture), 它
        # 要求独占 CUDA 流; 切分线程的 h0 回填(frontend 前向)与主线程的首次
        # capture 并发会让 capture 失效并抛 AcceleratorError。这把锁只保护
        # "前向"这一小段, 不阻塞提取/读帧 —— 比恢复 cuts_done.wait() 精确得多。
        self.gpu_lock = threading.Lock()
        # 模型首次前向会做 CUDA graph capture(_env_graph_capture), 期间必须独占
        # 流; 切分线程的 TransNet 推理要等它做完再上 GPU(见 _shot_cuts)。
        self.capture_ready = threading.Event()
        # 切分线程正在独占 GPU 跑整片 TransNet 时为 True: 主线程在忙期内跳过
        # 解码(提取照常推进), 避免两条 CUDA 前向互相踩流。
        self.cut_gpu_busy = False
        self.h0_t = None                  # torch (T, h_dim) fp32 CPU
        self.h0_rows = 0                  # 已构建 h0 的行数(单调)
        self.model = None
        self._playhead_ms = 0.0
        # rev_bias 流式拟合状态(官方 run() 的 fit_rev_bias 前缀等价)
        self._rev_bias: float | np.ndarray = 0.0   # 逐行发射先验; 0.0=未校准
        self._rev_bias_rows = 0                    # 上次标定时的前缀行数
        self.rev_bias_log: list[str] = []          # 标定/失败记录(导出诊断用)

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
            # device=cuda 但 torch 是 CPU 版（复用了系统 Python 的 torch）时，
            # 继续跑只会在第一个 kernel 上抛 "Torch not compiled with CUDA enabled"。
            # 这里直接降级到 CPU 并在状态里说明——CPU 慢（约 0.3x 实时）但能用，
            # 比整条链路失败好。用户看到提示可以换一个 CUDA 版解释器。
            if str(self.device).startswith("cuda") and not torch.cuda.is_available():
                msg = ("torch 不支持 CUDA（当前 %s），已降级到 CPU 推理（约慢 3-5 倍）。"
                       "要 GPU 加速请在设置里指定带 CUDA 的 torch 解释器。"
                       % torch.__version__)
                self.device = "cpu"
                self.note(msg)
            # 从第一秒就置 warming：转码 + 加载模型要 1-3 分钟，期间若 state 仍是
            # idle，UI 会以为"还没开始"，预热遮罩的进度和阶段提示全都不更新
            # （宿主把 idle 当"未运行"，只显示一句"正在启动识别服务"）。
            with self.buffer.lock:
                self.buffer.state = "warming"
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

            # 官方两段式提取: 先按项目 TRANSCODE 参数转码(480p30 x264 bicubic
            # +faststart, media.transcode 官方实现), 再从转码产物解码 —— 消除
            # 直解原片与官方管线的像素域差异(幅度减半的根因)
            self.extract_video = self._transcode_official()

            # 切分与提取并行: cut 旗标只在解码块用, 提取是 cut-blind 的。
            # 必须放在转码之后 —— TransNetV2 要跑在**转码产物**上(官方同源),
            # 否则既浪费(1080p 解到 27x48 的输入)又测出不同的切点集合。
            t = threading.Thread(target=self._cuts_worker,
                                 args=(self.extract_video,), daemon=True)
            t.start()

            self._stream(model, ck, enc, basis)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            with self.buffer.lock:
                self.buffer.state = "error"
                self.buffer.note = str(e)

    def _transcode_official(self) -> str:
        """官方两段式第一段: 按 project.TRANSCODE 转码到 480p30 x264。
        产物缓存复用(存在即跳过); 失败时回退直解原片。"""
        from goblintrain import media as gmedia
        tmp = self.cache_dir / f"{Path(self.video).stem}.480p30.mp4"
        if tmp.exists():
            self.note("复用已有转码: " + tmp.name)
            return str(tmp)
        self.note("官方两段式: 转码 480p30 x264 bicubic (约 9x 实时)...")
        reason = gmedia.transcode(self.video, str(tmp), gproject.TRANSCODE)
        if reason:
            self.note(f"转码失败({reason}), 回退直解原片")
            tmp.unlink(missing_ok=True)
            return self.video
        self.note("转码完成")
        return str(tmp)

    def _cuts_worker(self, video: str) -> None:
        self.cuts_ms = self._shot_cuts(video)
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
        # h0 回填完毕(自回填起点之后的行本就是用真 cut 建的); 真 cut 生效后
        # **轨道**仍是 cut=0 时算的, 必须重算前缀: 首次下界为 0, 此后由
        # _stream 消费该标志(见 _retrack_prefix 的调用点)。
        with self.lock:
            self._needs_retrack = True
        self.cuts_done.set()
        self.note("cut 旗标回填完成, 识别进入全速")

    def _retrack_prefix(self, model, ck, kept: int) -> int:
        """真 cut 到位后重算 [0, kept) 的轨道(此前用 cut=0 算的)。

        cut 旗标进的是 frontend(h0 已回填) 与样式分段边界, 所以轨道本身必须
        带着真 cut 重新前向一次。代价是一次 [0, kept) 的前向, 只在切分完成
        的那一刻发生一次; 换来的是首块解码不必再阻塞等待切分(开场那 80~113s
        的"零动作"死等就是这么来的)。
        """
        import torch
        with self.lock:
            gen_end = min(self.n_rows, self.total_rows)
            # 只能用 h0 已经建好的行: flush 里 lat 先写、h0 后建, 两者之间
            # 会有短暂的 n_rows > h0_rows 窗口, 直接用会读到未初始化(frontend
            # 返回全 0 或 NaN)的 h0。
            gen_end = min(gen_end, self.h0_rows)
        if kept <= 0 or gen_end <= 0:
            self._needs_retrack = False
            return kept
        rows = min(kept, gen_end)
        ctx_rows = common.rows_at(common.DECODE_CTX_S, ROW_HZ)
        t0 = time.time()
        # 按 ctx 分块重算, 避免一次性前向过长(与 _decode_chunk 同一套语义)
        step = max(256, common.rows_at(common.DECODE_CHUNK_S, ROW_HZ))
        with self.gpu_lock, torch.no_grad():
            for s in range(0, rows, step):
                e = min(s + step, rows)
                fs = max(0, s - ctx_rows)
                fe = min(gen_end, e + ctx_rows)
                if fe - fs < 2:
                    continue
                class _DS:
                    row_hz = ROW_HZ
                    feat_scale = INT8_SCALE
                    masked = False

                    def __init__(self, eng):
                        self.eng = eng
                        self.vel = np.zeros(eng.total_rows, np.float32)

                    def fwin(self, a, b):
                        return torch.from_numpy(np.array(self.eng.lat[a:b]))

                    cut = None

                ds = _DS(self)
                ds.times_ms = self.times_ms[:gen_end]
                ds.cut = torch.from_numpy(self._cut_arr(gen_end))
                ds.h0_t = self.h0_t
                _, tracks = _predict_shots_live(model, ds, [(fs, fe)],
                                                self.device, kept_end=e)
                for k in self._trk_keys:
                    self._trk[k][s:e] = tracks[k][s:e]
        self._needs_retrack = False
        self.note(f"真 cut 重算前缀轨道完成: {rows} 行, {time.time() - t0:.1f}s")
        return kept

    def _shot_cuts(self, video: str) -> list[float]:
        """TransNetV2 整片切点(毫秒), 在**转码产物**上跑。

        官方 boundaries.ensure 跑的是 project.video_path() 即 480p30 转码产物,
        而这里曾经跑 self.video(原始 1080p50)。收益是省掉在 1080p 上做
        27x48 输入的浪费(TransNetV2 的输入只有 27x48), 并降低与 V-JEPA 的
        GPU 争抢 —— 实测同一部片上"原片 vs 转码产物"的切点**逐点完全相同**
        (verify_cuts.py), 所以换源不影响切点结果, 纯粹是成本收益。
        """
        try:
            # 等主线程完成模型首次前向(CUDA graph capture 需要独占流;
            # TransNet 推理若在 capture 期间并发会让 capture 失效)。
            # 只等这一次, capture 之后就再无此约束, 所以不会退化成"死等切分"。
            self.capture_ready.wait(timeout=120.0)
            from goblintrain.boundaries import analyze_video, load_transnet
            # 整片 TransNet 推理要独占 GPU 约 80s。这里**不能**持 gpu_lock 跑完,
            # 否则主线程解码被挡住 = 回到"开场无动作"。改为: 置位 cut_gpu_busy,
            # 主线程在忙期内跳过解码(提取照常推进), TransNet 完成后立即恢复。
            with self.lock:
                self.cut_gpu_busy = True
            try:
                tn = load_transnet(self.device)
                payload = analyze_video("live", video, self.duration_ms,
                                        tn, self.device)
            finally:
                with self.lock:
                    self.cut_gpu_busy = False
            return [float(c) for c in payload["cuts_ms"]]
        except Exception as e:  # noqa: BLE001
            self.note(f"TransNetV2 失败({e}), 退化为无切点")
            return []

    def _edges_rows(self) -> list[int]:
        cuts = sorted(self.cuts_ms)
        # 切点行号换算与官方一致: jepa_train 用 np.searchsorted(times, cuts)
        # (ceil 语义), 这里曾用 int(round(...)) —— 每个切点最多差 1 行(33ms),
        # 会让 cut 通道与段界错位一行。
        # 注意 searchsorted 需要一个已填充的时间轴前缀: 提取尚未开始(n_rows=0)
        # 时退回按行率换算, 那时 cuts 通常是空的(切分线程还没产出)。
        with self.lock:
            n = self.n_rows
        if n > 1:
            times = self.times_ms[:n]
            # times_ms 未填充处为 0.0, 直接 searchsorted 会把超出已提取范围
            # 的切点映到错误行号 -> 只对 <= 已填充最大时间 的切点用 searchsorted,
            # 其余按行率换算(两者在 30Hz 栅格上一致到 ±1 行)。
            t_max = float(times[-1])
            rows = [int(np.searchsorted(times, c)) if c <= t_max
                    else int(round(c / 1000.0 * ROW_HZ)) for c in cuts]
        else:
            rows = [int(round(c / 1000.0 * ROW_HZ)) for c in cuts]
        from goblintrain.boundaries import MIN_GAP_S
        merged = [r for i, r in enumerate(rows)
                  if i == 0 or r - rows[i - 1] >= common.rows_at(MIN_GAP_S, ROW_HZ)]
        return [0] + merged + [self.total_rows]

    # ---- 提取(生成线程推进 n_rows)与推理共用锁 ----
    def _stream(self, model, ck, enc, basis) -> None:
        import torch
        kept = 0                     # 已发布动作的稳定行界
        publish_rows = common.rows_at(8.5333, ROW_HZ)   # 256 行 = 8.5s/批(前向 span 仍含 1024 官方 kept 语义)
        ctx_rows = common.rows_at(common.DECODE_CTX_S, ROW_HZ)
        guard = common.rows_at(17.0667, ROW_HZ)         # 512 行 = 17s 发布保护
        first = True
        gen_iter, proc = _ffmpeg_decode(self.extract_video, FPS_GRID, 0.0,
                                        384, 384, pre_scaled=True)
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
                # 真 cut 到位 -> 重算一次前缀轨道(cut=0 时算的那些作废)。
                # 放在提取循环里而不是阻塞首块解码: 开场不再有 80~113s 的
                # "等切分"死等, 代价只是这一次前缀重算。
                if getattr(self, "_needs_retrack", False):
                    with self.lock:
                        busy = self.cut_gpu_busy
                    if not busy:
                        kept = self._retrack_prefix(model, ck, kept)
                buf.append(frame)
                tbuf.append(t_ms)
                if len(buf) - K < GROUP:
                    continue
                n_new = self._flush_group(buf, tbuf, enc, basis, torch)
                del buf[:GROUP], tbuf[:GROUP]
                if n_new:
                    # 切分线程正在独占 GPU 跑 TransNet 时跳过解码(提取不受影响,
                    # 所以不会退化成开场死等); 它一结束立刻恢复。
                    with self.lock:
                        busy = self.cut_gpu_busy
                    # 攒够一个解码块就推一节(P0-2 回退后右侧 ctx 不再参与触发)
                    if not busy and (self.n_rows - kept >= publish_rows
                                     or (first and self.n_rows >= 384)):
                        kept = self._decode_chunk(
                            model, ck, kept, publish_rows, ctx_rows, first)
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
                               first, final=True)
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

    def _wait_gpu_free(self) -> None:
        """等切分线程的 TransNet 整片推理(独占 GPU, 约数十秒)结束。

        只在这一个窗口内有意义; TransNet 一结束 cut_gpu_busy 立刻复位。
        """
        while True:
            with self.lock:
                if not self.cut_gpu_busy or self.stop_flag.is_set():
                    return
            time.sleep(0.05)

    def _build_h0(self, n0: int, n1: int, q: np.ndarray) -> None:
        """对 [n0,n1) 的 latent 行跑一次点状前端, 存 fp32 h0(官方 live 路径)。"""
        import torch
        if n1 <= n0 or self.model is None:
            return
        t0 = time.time()
        x = torch.from_numpy(q[:n1 - n0]).to(self.device).float()
        x.mul_(INT8_SCALE)
        cut = torch.from_numpy(self._cut_arr(max(n1, 1))[:n1][n0:]).to(self.device)
        # 与主线程的解码前向互斥: 模型首次前向会做 CUDA graph capture,
        # 要求独占流; 并发的 frontend 前向会让 capture 失效(AcceleratorError)。
        # 同时等 TransNet 的 GPU 忙期过去 —— 它整片推理期间独占 GPU。
        self._wait_gpu_free()
        with self.gpu_lock:
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

    def _fit_rev_bias_prefix(self, gen_end: int, vel_all: np.ndarray):
        """反转发射先验的流式标定(官方 fit_rev_bias 的前缀等价)。

        官方窗 = min(前缀行数, rows_at(BIAS_FIT_S, FPS)), BIAS_FIT_S 是秒并且
        大得不封顶, 所以官方实际用全片轨道标定 —— 流式只能对"当前前缀"标定,
        并随前缀增长按 BIAS_REFIT_* 节奏重拟合, 逐步收敛到官方读数。

        旧实现写的是 `gen_end >= rows_at(BIAS_FIT_S, ROW_HZ)`(= 240000 行, 把
        秒当成了行), 而全片只有 ~40960 行 → 条件永不成立, `_rev_bias` 恒为 0,
        整个 run 都在用欠发射的先验解码。
        """
        rows = gen_end
        step = max(common.rows_at(BIAS_REFIT_MIN_S, ROW_HZ),
                   rows // BIAS_REFIT_DIV)
        due = (rows >= common.rows_at(BIAS_MIN_S, ROW_HZ)
               and (not self._rev_bias_rows
                    or rows - self._rev_bias_rows >= step))
        if due:
            try:
                t0 = time.time()
                self._rev_bias = jepa_infer.fit_rev_bias(
                    {"rev_top": self._trk["rev_top"][:rows],
                     "rev_bot": self._trk["rev_bot"][:rows]},
                    vel_all[:rows], "stream")
                msg = (f"rev_bias 已标定(前缀 {rows} 行 = {rows / ROW_HZ:.0f}s, "
                       f"{time.time() - t0:.1f}s)")
                self.note(msg)
                self.rev_bias_log.append(msg)
            except Exception as e:  # noqa: BLE001
                import traceback as _tb
                msg = (f"rev_bias 标定失败(前缀 {rows} 行): "
                       f"{type(e).__name__}: {e}")
                self.note(msg)
                self.rev_bias_log.append(msg + " | " + _tb.format_exc()[-800:])
            self._rev_bias_rows = rows
        bias = self._rev_bias
        if np.ndim(bias) and len(bias) < rows:
            # 标定后前缀又长了(两次标定之间): 新增行沿用已标定的均值先验。
            # 这里**必须**补齐到 rows —— 样式机是按镜头段切 rev_bias[lo:hi] 的,
            # 长度短了会在 alternating_events 里广播失败(744 vs 488)。
            # 用 0 补齐则等于让这些行回到欠发射的先验(旧实现掉密度的原因)。
            bias = np.concatenate([bias, np.full(rows - len(bias),
                                                 float(np.mean(bias)))])
        return bias

    def _decode_chunk(self, model, ck, kept,
                      publish_rows, ctx_rows, first, final=False) -> int:
        """对 [kept, gen_end) 做一次带上下文的前向 + 样式解码, 发布稳定动作."""
        # 注: 首块**不再** cuts_done.wait()。等待切分曾把开场预热拖到切分完成
        # (实测 80~113s 内零动作), 而切分线程与提取同线程互相拖累。现在改为
        # "先出动作, 真 cut 到位后由 _stream 触发一次前缀轨道重算"(_retrack_prefix)。
        import torch
        t0 = time.time()
        with self.lock:
            gen_end = self.n_rows
        gen_end = min(gen_end, self.total_rows)
        s = kept
        # 发布切片 = [s, e): 取当前提取头的前 publish_rows 行。
        #
        # 【已确认但暂不启用】e 与 gen_end 相等时, 下面的 fe = min(gen_end, e+ctx)
        # 退化成 fe == e, 即**被发布行的右侧真实行数为 0**(官方每个发布行有
        # ±1023 行上下文, TCN 感受野 = 1+2*Σdilations(1..512))。离线判决实验
        # (ab_right_ctx.py / ab_right_margin.py, 官方 latent 缓存, 只改这一项):
        #   右侧 0 行    -> 合成位置轨相关 0.706, level 相关 0.835
        #   右侧 512 行  -> p 相关 0.964
        #   右侧 1024 行 -> p 相关 1.000(官方)
        # 即轨道保真度确实随右侧预热单调提升。但全链路复测显示: 补齐右侧 ctx
        # **并不能提高最终动作点密度**(2.29 -> 2.28 点/s, 与修复前持平), 而代价
        # 是发布水位被推低一个 ctx(34s), 播放头更容易跑到水位前面导致玩具闲置。
        # 因此 P0-2 的发布改动暂不启用, 留待"轨道保真度"成为瓶颈时再开。
        # 若将来要启用: 把 e 改成 min(gen_end - ctx_rows, kept + publish_rows),
        # 同时 (a) 触发条件加 ctx_rows, (b) kept 随 e 推进, (c) 启动策略必须
        # 先攒够 >= ctx_rows 的余量再开播, 否则玩具会频繁闲置。
        # 当前保持"贴提取头发布 + 水位=已样式末行"。
        e = min(gen_end, kept + publish_rows) if not final else gen_end
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

        # 与切分线程的 h0 回填前向互斥(见 gpu_lock 注释: CUDA graph capture
        # 要求独占流, 并发前向会让 capture 失效)。
        with self.gpu_lock:
            pred, tracks = _predict_shots_live(
                model, ds, [(fs, fe)], self.device, kept_end=e)
        # 首次解码前向已完成 -> 模型的 graph capture 已就绪, 放行切分线程的
        # TransNet GPU 工作(此前它一直在等这个事件)。
        if not self.capture_ready.is_set():
            self.capture_ready.set()
        # home 值进累积轨道(每行只属于一个块的保留切片, 无跨块覆盖跳变);
        # 样式合成改为 官方全前缀语义 —— 对 [0, sty) 的累积 home 轨道做
        # 完整样式合成(真实剪缝全时间线分段), 动作缓冲整体替换。流式输出
        # 随前缀增长单调收敛于官方全片解码的结果。
        #
        # sty = 本块**真正有轨道数据**的末行: [e, gen_end) 那些行还没进过前向
        # (轨道是 NaN), 若把它们(经 nan_to_num -> 0 = 静止)一起送进样式机,
        # 既污染末段的 still 门/包络, 又让水位虚高到未计算区域。
        sty = e if not final else gen_end
        for k in self._trk_keys:
            self._trk[k][s:e] = tracks[k][s:e]
        vel_all = np.nan_to_num(self._trk["vmarg"][:sty]) * ck["v_std"]
        # 官方 run() 语义: 每个真实剪缝前的末行保留前行速度(缝上不铸造幻影交叉)
        for r in self._edges_rows()[1:-1]:
            if r < sty and r >= 2 and np.isfinite(vel_all[r - 2]):
                vel_all[r - 1] = vel_all[r - 2]
        jepa_infer.DT = float(np.median(np.diff(self.times_ms[:sty]))) / 1000.0
        jepa_infer.FPS = 1.0 / jepa_infer.DT
        # 水位 = **已算出轨道并参与样式合成**的末行(sty), 不是提取末行(gen_end)。
        # 旧写法用 gen_end, 而 [e, gen_end) 这些行还没进过前向(轨道仍是 NaN),
        # 会被 nan_to_num 当成"静止"合成, 却和水位一起标记为"已覆盖" —— 播放头
        # 正好落在这一段时玩具就僵住。P0-2 之后 e 与 gen_end 相差一整个右侧
        # ctx, 这个洞会变得很大, 所以必须同步收紧水位。
        stable_ms = (float(self.times_ms[sty - 1]) if sty > 0 else 0.0)
        bias = self._fit_rev_bias_prefix(sty, vel_all)
        _bm = (float(np.mean(bias)) if np.ndim(bias) else float(bias))
        if not self.rev_bias_log or self.rev_bias_log[-1].startswith("used:") is False:
            self.rev_bias_log.append(f"used: rows={sty} bias_mean={_bm:.4f}")
        edges_full = np.array(
            [0] + [r for r in self._edges_rows()[1:-1] if r < sty] + [sty])
        sub: dict = {}
        # 官方 run(): has_rev -> frc = [] (非 None), 样式机把 Viterbi 判定的
        # apex 行回填进它, 再由 extrema_actions 强制写成顶点 —— 否则那 3 行
        # 平滑器"正好抹掉这些被找回的快速冲程"(jepa_infer.extrema_actions
        # 的 docstring 原话), 实测还会丢掉 ~5% 动作点并且反转时刻不被重新定位。
        has_rev = bool(ck.get("arch", {}).get("rev_head", False))
        frc: list[int] | None = [] if has_rev else None
        phase = {"apexes": 0, "in_runs": 0} if has_rev else None
        p = jepa_infer.style_positions_composed(
            vel_all, self._trk["level"][:sty],
            self._trk["env"][:sty] * ck["v_std"], edges_full,
            band=(self._trk["blo"][:sty], self._trk["bhi"][:sty]),
            still_eps=jepa_infer.STILL_EPS,
            plat=(self._trk["plat_top"][:sty], self._trk["plat_bot"][:sty]),
            plat_thr=jepa_infer.PLAT_THR, plat_lo=jepa_infer.PLAT_LO,
            plat_peak=jepa_infer.PLAT_PEAK, plat_veto=jepa_infer.PLAT_VETO,
            plat_soft=jepa_infer.PLAT_SOFT, plat_rail_track=True,
            plat_shift_cap=jepa_infer.PLAT_SHIFT_CAP,
            rev=(self._trk["rev_top"][:sty], self._trk["rev_bot"][:sty]),
            rev_snap=common.rows_at(common.REV_SNAP_S, ROW_HZ),
            times_ms=self.times_ms[:sty],
            subframe=jepa_infer.SUBFRAME, sub_out=sub,
            ext_snap=jepa_infer.EXT_SNAP, amp_cap_x=jepa_infer.AMP_CAP_X,
            rev_source=jepa_infer.REV_SOURCE, rev_bias=bias,
            rev_gap=common.rows_at(common.EVENT_GAP_S, ROW_HZ),
            force_out=frc, phase_out=phase)
        actions = jepa_infer.extrema_actions(
            p, self.times_ms[:sty], edges_full,
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
        # 官方式整体替换: 水位 = 已解码末行(前缀增长单调收敛于官方全片输出)
        self.buffer.replace_all(actions, stable_ms)
        PERF["decode"] += time.time() - t0
        if self.buffer.state == "warming":
            self.buffer.state = "running"
            self.note(f"首批动作已出({len(actions)} 个, apex "
                      f"{len(frc) if frc else 0} 个), 进入跟随模式")
        # kept 推进到本块发布末行。P0-2 回退后 e = kept + publish_rows(或提取头),
        # 两者一致, 所以这里等价于旧写法; 写成 e 是为了与发布范围严格对齐 ——
        # 若哪天改了 e 的取法, kept 必须跟着走, 否则会留下永久空洞。
        return e

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
