# -*- coding: utf-8 -*-
"""audiocpp 常驻服务 ASR 后端。

为什么用它：
  - 显存占用 0（CPU 后端），把 8GB GPU 全留给翻译/更大模型
  - 无 Python/torch 依赖，常驻进程 ~2.8GB 内存
  - 自带标点

为什么必须配 VAD 裁剪（实测结论）：
  整块 600s 音频直接送 ASR → 出现 6 个 ≥10 连重复串（「啊啊啊…」×509），
  有效日文仅 56 字符；先 VAD 出语音段再逐段送 → 重复串 0、文本干净。

用法（作为库）：
    from audiocpp_backend import AudioCppBackend
    be = AudioCppBackend(cfg)          # cfg 见 server 配置
    be.ensure_server()                 # 拉起常驻服务（幂等）
    spk = be.speech_spans(pcm)         # VAD → [(start_sec, end_sec)]
    segs = be.transcribe_spans(pcm, spans, video_start_ms, lang)   # → 分句
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

import numpy as np

from text_filters import (has_repetition_loop, is_latin_hallucination,
                          is_prompt_echo, join_tokens, keep_segment)

SR = 16000
BASE_DIR = Path(__file__).resolve().parent          # vendor/subtitle
# 运行时目录：安装目录 vendor\audiocpp（安装包自带 CPU 版，R69 打通全新安装）。
# 旧默认是开发机绝对路径 E:\audiocpp-portable——别的机器上必然不存在，且模板
# 里也写着它，全新装机识别必然不可用（R69 修正）。环境变量仍可覆盖（开发机
# 指向 E 盘大本营用）。
AUDIOCPP_DIR = Path(os.environ.get("AUDIOCPP_DIR")
                    or (BASE_DIR.parent / "audiocpp"))

# 请求的 lang_key → audiocpp 的语言名。此前请求体写死构造时的 self.language，
# `/transcribe?lang=en` 在本路径下会被静默按日语解码。
# 语言收窄（R65，用户拍板）：AI 字幕源语言只保留日语和英语可选（ko/zh/yue 移除；
# 翻译目标语言固定中文，不受影响）。
AUDIOCPP_LANG = {"ja": "Japanese", "en": "English"}

# 流式模型的 id：头显的实时字幕按这个 id 请求，必须与 stream_bridge.ASR_MODEL 一致。
STREAM_MODEL_ID = "qwen3-asr-stream"

# audiocpp_server 的缺省端口。**只在这里定义一份**：stream_bridge 也从这里取。
# 此前 stream_bridge 硬编码 :8081 而本模块缺省是 8083 —— 配置里 port 一旦缺失
# （UI 切换识别模型时曾把 asr.audiocpp 整段替换掉，见 host_server.save_subtitle_config），
# 离线路径去 8083、流式路径去 8081，流式字幕整条失效。
DEFAULT_PORT = 8083


def _run_dir() -> Path:
    """运行时临时目录 `<安装目录>\\run`（子进程配置、VAD 转储）。

    原先一律落 `%TEMP%`——那在系统盘上，而 VAD 转储的 wav 可能很大（整块音频），
    装到 D 盘的用户往往正是 C 盘紧张。规则见 user_paths.run_dir()；取不到才退回 %TEMP%。
    """
    try:
        import user_paths
        return Path(user_paths.run_dir())
    except Exception:
        return Path(tempfile.gettempdir())


def _install_models_dir() -> Path:
    """安装目录的 `models\\`（与宿主 `MODELS_DIR = APP_DIR/models` 同源）。

    ⚠ 缺省模型路径曾写成 `self.dir/"models"/…` = `vendor\\audiocpp\\models\\Qwen3-ASR-0.6B`
    —— 那个目录**根本不存在**（实测 `vendor\\audiocpp` 只有 assets/cpu/LICENSE），而模型
    下载器把它下到 `<安装目录>\\models\\Qwen3-ASR-0.6B`（评审 F04）。配置里写了
    `asr.audiocpp.model` 时走配置，这条只是"配置没写"时的兜底。
    """
    try:
        import user_paths
        return Path(user_paths.models_dir())
    except Exception:
        return BASE_DIR.parents[1] / "models"


class AudioCppError(RuntimeError):
    pass


class AudioCppBackend:
    """封装 audiocpp_server 的 ASR + silero_vad。"""

    # /health 的 asr_ready 靠它区分"真引擎"与"未就绪兜底"（缺属性会被误判为未就绪）
    backend_kind = "audiocpp"

    def __init__(self, cfg: dict, drop_latin: bool = True):
        # dir 支持相对路径（相对 vendor/subtitle 解析，与 config 既有约定一致）；
        # 兼容历史绝对路径（开发机 E:\audiocpp-portable 的活配置照常工作）。
        _d = Path(str(cfg.get("dir") or AUDIOCPP_DIR))
        self.dir = _d if _d.is_absolute() else (BASE_DIR / _d).resolve()
        # R97：识别只跑 GPU，backend 不再是配置项（config 里残留的 cpu 键被忽略）。
        # gpu\ 运行时缺失时**立刻报错**而不是悄悄降级 CPU——静默回退会让 VAD CLI
        # 一起消失、字幕块块 skipped 且界面毫无征兆（R96 的教训反面）。在此 fail-fast，
        # 服务起不来 → /health error → 手机端明确提示去模型列表下载。
        self.backend = "cuda"
        if not (self.dir / "gpu" / "audiocpp_server.exe").is_file():
            raise AudioCppError(
                "GPU 识别运行时未安装：请在模型列表下载「识别运行时 · CUDA」（约 1.1GB，需 NVIDIA 显卡）")
        self.threads = int(cfg.get("threads", max(1, (os.cpu_count() or 4) - 1)))
        self.port = int(cfg.get("port", DEFAULT_PORT))
        self.host = str(cfg.get("host", "127.0.0.1"))
        # 模型路径：config 里按既有约定写相对路径（../../models/Qwen3-ASR-0.6B = 安装目录
        # 的 models\），但 audio.cpp 是拿**它自己那份配置文件的所在目录**去解析的 ——
        # 实测传相对路径时上游注册成 <临时目录>\..\..\models\... 找不到文件：
        # 流式返回 segments=0、离线整段空译文。所以这里先解析成绝对路径再写进临时配置。
        # 模型路径优先级：config 的 asr.audiocpp.model > 环境变量 ASR_MODEL（宿主在
        # "配置完全没写"时注入的绝对路径 —— 此前那是**死代码**，全仓无人读它，评审 F04）
        # > 安装目录 models\Qwen3-ASR-0.6B。
        _m = Path(str(cfg.get("model")
                      or os.environ.get("ASR_MODEL")
                      or (_install_models_dir() / "Qwen3-ASR-0.6B")))
        self.model = str(_m if _m.is_absolute() else (BASE_DIR / _m).resolve())
        self.vad_model = str(cfg.get("vad_model") or
                             (self.dir / "assets" / "framework" / "models" / "silero_vad"))
        self.language = str(cfg.get("language", "Japanese"))
        self.pad_sec = float(cfg.get("pad_sec", 0.25))
        self.merge_gap = float(cfg.get("merge_gap_sec", 0.5))
        self.min_speech_ms = int(cfg.get("min_speech_ms", 250))
        # 单段最长秒数：超过此长度的语音段先切开再送 ASR（防退化）
        self.max_span_sec = float(cfg.get("max_span_sec", 8.0))
        # 热词/上下文偏置只保留"上一句原文"（transcribe 的 extra_context）。
        # audiocpp 只认 `context` 这个字段名：`prompt`/`hotwords` 都被静默忽略
        # （同一段音频、同一份解码设置下输出逐字节相同）。
        # 拉丁幻觉过滤（判据见 text_filters.is_latin_hallucination）：日语音频里
        # "一个日文字符都没有"的短输出直接丢。实测 Whisper 会吐 `.`/`Thank`/`I`/`you`
        # 并当台词上屏；日语外来语写片假名不写拉丁字母，所以这类短输出不可能是真实台词。
        # 留成可关：万一某片里真出现拉丁字母的短台词（如 OK），关掉即可。
        self.drop_latin = bool(drop_latin)
        self._proc: subprocess.Popen | None = None
        self._job_handle = None       # Windows Job Object 句柄（父进程崩溃时带走子进程）
        # **必须是 RLock**：ensure_server 全程持锁，失败收尾要在锁内调
        # stop_server 回收刚拉起的子进程（否则它继续加载模型、和回退后的
        # PyTorch 引擎抢显存）；不可重入的 Lock 在那里会自锁死——与
        # llama_backend.py 的既有事故同一类（超时收尾调 stop_server 挂死所有线程）。
        self._lock = threading.RLock()
        self.echo_retries = 0        # 热词复读触发无热词重试的次数（诊断用）
        self.last_vad_error = ""     # 最近一次 VAD 失败原因全文（"" = 正常）
        # 回给客户端的**错误类别**（last_vad_error 的全文含绝对路径/用户名，
        # 只能进本地日志——见 _error_kind / _vad_fail 的说明）
        self.last_vad_error_kind = ""

    # ---- 与 PyTorch 引擎对齐的属性（server_app 的 /health 会读）----
    @property
    def vad(self):
        """VAD 由 audiocpp 的 silero_vad 承担；返回非 None 表示可用。"""
        return True

    @property
    def use_aligner(self) -> bool:
        """本后端暂不做逐字对齐（时间戳来自 VAD 语音段边界）。"""
        return False

    @property
    def load_s(self) -> float:
        return 0.0

    @property
    def model(self):
        return self._model_path

    @model.setter
    def model(self, v):
        self._model_path = str(v)

    # ---------------------------------------------------------------- 服务
    @property
    def exe_dir(self) -> Path:
        return self.dir / ("cpu" if self.backend == "cpu" else "gpu")

    def _url(self, path: str) -> str:
        return f"http://{self.host}:{self.port}{path}"

    def probe(self, timeout: float = 2.0) -> bool:
        try:
            with urllib.request.urlopen(self._url("/health"), timeout=timeout) as r:
                d = json.loads(r.read().decode("utf-8"))
            return d.get("status") == "ok"
        except Exception:
            return False

    def _ensure_alive(self, reason: str = "") -> bool:
        """上游还活着吗？不活就**重拉一次**（评审 F12 的自愈闭环）。

        为什么必须有这条路径：`ensure_server()` 此前只在 lifespan 启动时调一次，
        而 audiocpp_server.exe 运行中崩溃/被杀之后，没有任何请求会重拉它——
        VAD 走的是一次性 CLI 所以照常成功，只有逐段 POST 全线失败，于是**每一段都空**：
        用户看到"字幕突然全没了"，宿主侧看不到异常（旧 /health 的 asr_ready 是类属性，
        恒为 True）。这里失败重拉，成功返回 True；重拉也失败就返回 False，
        调用方据此把"整块都废了"如实报出去，而不是静默给空字幕。

        RLock 可重入，且本方法不持有锁调 ensure_server，避免与 lifespan 抢锁。
        """
        if self.probe():
            return True
        print(f"[asr] 上游 audiocpp 服务无响应（{reason}）→ 重拉一次", flush=True)
        try:
            self.ensure_server()
        except Exception as ex:
            print(f"[asr] 重拉上游失败：{type(ex).__name__}: {ex}", flush=True)
            return False
        return True

    def _registered_models(self, timeout: float = 3.0) -> list | None:
        """上游 /v1/models 里已注册的模型 id；查不到返回 None（不阻断主流程）。"""
        try:
            with urllib.request.urlopen(self._url("/v1/models"), timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
            return [str(m.get("id")) for m in (data.get("data") or [])]
        except Exception:
            return None

    def _warn_if_stream_model_missing(self) -> None:
        """复用到"只注册了 offline 模型"的旧实例时，喊出可执行的告警。

        这种实例本后端的离线路径完全正常，但头显的 /transcribe/stream 会整条 500，
        而两边的日志都不明显（上游只回 500）——必须在这里点名并给出处置办法。
        """
        ids = self._registered_models()
        if ids is None or STREAM_MODEL_ID in ids:
            return
        print(f"[asr] ⚠️ 端口 {self.port} 上的 audiocpp 没注册流式模型 {STREAM_MODEL_ID}"
              f"（已注册：{ids}）→ 头显实时字幕会整条 500。"
              "处置：结束该进程后重启字幕服务（或按 _ref\\audiocpp-asr-stream.json 手工启动）",
              flush=True)

    def ensure_server(self, wait_s: float = 60.0) -> bool:
        """确保常驻服务在跑；已在跑则直接返回。

        ⚠️ 复用判定只看 /health 的 status=ok，**不校验加载的模型**——如果
        8083 上残留着一个加载了旧模型的服务，会被静默复用。换过模型/参数
        后请先 stop_server() 或手工结束旧进程再启动。

        复用前额外核对一次模型注册表（见 _check_stream_model）：少了流式模型
        时，本后端的 offline 路径照样正常，但头显的 /transcribe/stream 会整条 500。
        """
        with self._lock:
            if self.probe():
                self._warn_if_stream_model_missing()
                print(f"[asr] audiocpp 端口 {self.port} 已有服务在跑，直接复用"
                      "（注意：不校验其加载的模型是否与本配置一致）", flush=True)
                return True
            exe = self.exe_dir / "audiocpp_server.exe"
            if not exe.exists():
                raise AudioCppError(f"找不到 audiocpp_server.exe：{exe}")
            cfg_path = _run_dir() / f"audiocpp_asr_{self.port}.json"
            cfg_path.write_text(json.dumps({
                "host": self.host, "port": self.port,
                "backend": "cuda" if self.backend != "cpu" else "cpu",
                "device": 0, "threads": self.threads,
                # 只注册**一个**模型，且必须是 mode=streaming：
                #   · 头显的实时字幕走 stream_bridge → 同一个 :8081 上的
                #     qwen3-asr-stream（offline 模型收到 stream=true 会被上游拒：
                #     "requires a model configured with mode=streaming"）；
                #   · 本后端的离线请求用同一个 id 也**照样能跑**（实测 6s 音频
                #     234ms、rtf 0.039）——所以不需要再注册一个 offline 模型。
                # 为什么不能两个都注册（2026-09-16 实测）：同一份权重要驻留两遍
                # （audiocpp 内存 737MB→2507MB、总显存 7745/8188 MiB），把
                # llama-server 挤到 CPU 上：流式中位 0.29s→2.34s、整段 wall
                # 37.7s→238.8s；改用 --max-loaded-models 1 又变成两模式反复装卸
                # （离线 3s 音频要 17.6s、出现 33 次 503）。单注册两个问题都没有。
                # lazy_load 与 _ref\audiocpp-asr-stream.json 一致：按需加载。
                "lazy_load": True,
                "models": [
                    {"id": STREAM_MODEL_ID, "family": "qwen3_asr",
                     "path": self.model, "task": "asr", "mode": "streaming"},
                ],
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            self._proc = subprocess.Popen(
                [str(exe), "--config", str(cfg_path), "--host", self.host,
                 "--port", str(self.port), "--backend",
                 "cuda" if self.backend != "cpu" else "cpu", "--threads", str(self.threads)],
                cwd=str(self.dir),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self._attach_kill_on_close(self._proc)
            t0 = time.time()
            while time.time() - t0 < wait_s:
                if self.probe():
                    return True
                if self._proc.poll() is not None:
                    code = self._proc.returncode
                    self.stop_server()     # 收尾：清掉 _proc/_job_handle 并确认进程已死
                    raise AudioCppError(f"audiocpp_server 启动即退出（code {code}）")
                time.sleep(0.5)
            # 失败必须回收：调用方随即回退 PyTorch 引擎并加载模型，剩一个还在
            # 启动/加载的 audiocpp 会和它抢显存（8GB 卡上直接 OOM）。
            # 这里在锁内调 stop_server —— 靠 self._lock 是 RLock（见 __init__ 注释）。
            self.stop_server()
            raise AudioCppError(f"audiocpp_server 未在 {wait_s}s 内就绪")

    def _attach_kill_on_close(self, proc: subprocess.Popen) -> None:
        """把子进程加入 Job Object（KILL_ON_JOB_CLOSE）：本进程崩溃/被强杀时，
        句柄随进程关闭，audiocpp_server（约 2.8GB）跟着被带走，不再变孤儿。
        纯 ctypes 实现，失败时静默降级为旧行为（下次启动 reap 兜底）。"""
        if os.name != "nt":
            return
        job = None
        try:
            import ctypes
            from ctypes import wintypes

            class IO_COUNTERS(ctypes.Structure):
                # ctypes.wintypes 没有 ULONGLONG（AttributeError 会让 Job Object
                # 保护静默失效 → 父进程退出后 audiocpp_server 变孤儿、显存不释放）
                _fields_ = [(n, ctypes.c_ulonglong) for n in (
                    "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                    "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

            class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                    ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            k32 = ctypes.windll.kernel32
            # 句柄原型：不声明 restype 时 ctypes 默认按 c_int 返回（32 位），
            # 0x100000000 以上的句柄值会被截断——之后 CloseHandle 拿到的就是
            # 一个错句柄（关不掉真句柄，还可能误关同值对象）。
            k32.CreateJobObjectW.restype = ctypes.c_void_p
            k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
            k32.SetInformationJobObject.restype = ctypes.c_int
            k32.SetInformationJobObject.argtypes = [
                ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
            k32.AssignProcessToJobObject.restype = ctypes.c_int
            k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            k32.CloseHandle.restype = ctypes.c_int
            k32.CloseHandle.argtypes = [ctypes.c_void_p]
            job = k32.CreateJobObjectW(None, None)
            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = 0x2000   # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(
                    job, 9, ctypes.byref(info), ctypes.sizeof(info)):  # 9 = ExtendedLimitInformation
                raise OSError("SetInformationJobObject 失败")
            if not k32.AssignProcessToJobObject(job, proc._handle):
                raise OSError("AssignProcessToJobObject 失败")
            self._job_handle = job    # 句柄保持打开；进程结束由 OS 回收
        except Exception as e:
            # 失败路径同样要关句柄：job 已在上面创建但 SetInformationJobObject /
            # AssignProcessToJobObject 失败时，旧实现直接抛出 → 句柄无人关闭。
            try:
                if job:
                    ctypes.windll.kernel32.CloseHandle(job)
            except Exception:
                pass
            print(f"[asr] Job Object 保护不可用（{e}），跳过", flush=True)

    def _close_job_handle(self) -> None:
        """关闭 Job Object 句柄（幂等；非 Windows 或句柄为空时跳过）。

        根因：stop_server 旧实现只把 `_job_handle` 置 None，注释写着"关闭 Job
        句柄"却**没有** CloseHandle —— 每次启停泄漏一个内核句柄（Job 对象也因
        句柄未关而不被回收）。`_job_handle` 可能是 None（非 Windows / 创建失败）
        或 0（CreateJobObjectW 返回 NULL），必须先判空再关。
        """
        h = self._job_handle
        self._job_handle = None
        if not h or os.name != "nt":
            return
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            k32.CloseHandle.restype = ctypes.c_int
            k32.CloseHandle.argtypes = [ctypes.c_void_p]
            k32.CloseHandle(ctypes.c_void_p(h))
        except Exception as e:
            print(f"[asr] 关闭 Job 句柄失败（由 OS 兜底回收）：{type(e).__name__}: {e}",
                  flush=True)

    def stop_server(self) -> None:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=5)
                except Exception:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
            self._proc = None
            # 真正关闭 Job 句柄（旧实现只是置 None → 每次启停泄漏一个内核句柄）。
            # 句柄是最后一个引用时 KILL_ON_JOB_CLOSE 生效，顺带保证子进程被带走。
            self._close_job_handle()
            # 启动时写的 audiocpp_asr_{port}.json 含安装路径/端口，旧实现从不删除：
            # 长期残留，且换端口后每次多留一份。现在写在 <安装目录>\run\ 下，顺带把
            # 老版本落在 %TEMP% 的那份也清掉（升级后不会自己消失）。
            for _p in (_run_dir() / f"audiocpp_asr_{self.port}.json",
                       Path(tempfile.gettempdir()) / f"audiocpp_asr_{self.port}.json"):
                try:
                    _p.unlink(missing_ok=True)
                except OSError as e:
                    print(f"[asr] 清理临时配置失败（忽略）：{type(e).__name__}: {e}", flush=True)

    # ------------------------------------------------------------ 错误脱敏
    @staticmethod
    def _error_kind(ex: BaseException) -> str:
        """异常 → 可回给客户端的**错误类别**；详细文本一律只进本地日志。

        根因：详细文本里带本机绝对路径与 Windows 用户名。实测样例
        （VAD CLI 超时）：
          Command '['E:\\audiocpp-portable\\cpu\\audiocpp_cli.exe', ...,
                   'C:\\Users\\admin\\AppData\\Local\\Temp\\vad_in_...wav']'
          timed out after 30 seconds
        这段文本经 transcribe() 的 error 字段回到响应里，而 /transcribe 按
        server.host=0.0.0.0 对局域网开放 ⇒ 同网段任意主机都能拿到本机的安装
        目录、临时目录与 Windows 用户名。所以对外只保留类别，全文 print 到服务日志。
        """
        if isinstance(ex, (subprocess.TimeoutExpired, TimeoutError)):
            return "asr_timeout"
        if isinstance(ex, (urllib.error.URLError, ConnectionError, FileNotFoundError)):
            return "backend_unavailable"
        return "asr_error"

    def _vad_fail(self, kind: str, detail: str) -> None:
        """记录一次 VAD 失败：类别回客户端，全文只进本地日志（见 _error_kind）。"""
        self.last_vad_error = detail
        self.last_vad_error_kind = kind
        print(f"[asr] VAD 失败（{kind}）：{detail}", flush=True)

    # ------------------------------------------------------------------ VAD
    def speech_spans(self, pcm: np.ndarray, tmpdir: Path | None = None) -> list | None:
        """写临时 wav → audiocpp VAD → [(start_sec, end_sec)]（相对本块）。

        返回 **None 表示 VAD 本身失败**（CLI 缺失/崩溃/超时），[] 才是"真没有
        语音"。旧实现把所有异常一律吞成 []，上层判 skipped 后静默丢块——CLI
        一坏整片字幕无声消失，且与真静音完全不可区分。失败原因存
        self.last_vad_error（全文，供日志），回给客户端的只有 last_vad_error_kind
        这个类别（全文含路径/用户名，见 _error_kind）。
        """
        exe = self.exe_dir / "audiocpp_cli.exe"
        d = Path(tmpdir) if tmpdir else _run_dir()
        # 临时文件名带线程 id：并发时同 pid 同毫秒会互覆
        wav_in = d / f"vad_in_{os.getpid()}_{threading.get_ident()}_{int(time.time()*1000)}.wav"
        out_json = wav_in.with_suffix(".chunks.json")
        # 超时按音频时长收紧：旧实现固定 300s，CLI 挂死时请求线程干等 5 分钟
        duration = max(0.1, len(pcm) / float(SR))
        timeout = max(30.0, min(300.0, duration * 2.0))
        try:
            self._write_wav(wav_in, pcm)
            r = subprocess.run(
                [str(exe), "--task", "vad", "--family", "silero_vad",
                 "--model", self.vad_model, "--backend", "cpu",
                 "--audio", str(wav_in), "--vad-chunks-out", str(out_json)],
                cwd=str(self.dir), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout,
                # 字幕服务被宿主以无窗口方式拉起时，不带这个标志每次 VAD 都会
                # 闪一个命令行黑窗（识别 3s 一块 = 每秒都在闪）。
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if r.returncode != 0:
                self._vad_fail("vad_cli_failed",
                               f"VAD CLI 退出码 {r.returncode}："
                               f"{(r.stderr or '').strip()[:200] or '无 stderr'}")
                return None
            if not out_json.exists():
                self._vad_fail("vad_no_chunks", "VAD CLI 未产出 chunks 文件（可能被秒退）")
                return None
            data = json.loads(out_json.read_text(encoding="utf-8"))
            spans = []
            for c in data:
                a = float(c["start_sample"]) / SR
                b = float(c["end_sample"]) / SR
                if b > a:
                    spans.append((a, b))
            self.last_vad_error = ""
            self.last_vad_error_kind = ""
            return spans
        except subprocess.TimeoutExpired as e:
            # 全文（含完整命令行与临时文件路径）只进本地日志
            self._vad_fail("vad_timeout",
                           f"VAD CLI 超时（{timeout:.0f}s）：{type(e).__name__}: {e}")
            return None
        except FileNotFoundError as e:
            self._vad_fail("vad_cli_missing", f"VAD CLI 不存在：{exe}（{e}）")
            return None
        except Exception as e:
            self._vad_fail("vad_error", f"{type(e).__name__}: {e}")
            return None
        finally:
            for f in (wav_in, out_json):
                try:
                    f.unlink()
                except Exception:
                    pass

    # ------------------------------------------------------------- 转写
    def _transcribe_span(self, wav, context: str, lang_key: str,
                         echo_ref: str = "", timeout: float = 600.0) -> str:
        """转写单个语音段；热词导致复读时**改用无热词重试一次**。

        `timeout` 由调用方按本段音频时长收紧后传入（见 transcribe；默认 600s
        只用于不关心时长的直接调用）。
        为什么必须重试而不是丢弃：audiocpp 的 Qwen3-ASR 在喘息/气声这类
        非清晰语音上，有相当高的概率把 `context`（上一句原文）整段复读出来
        当结果（静音/音乐段尤甚）。直接丢弃的后果是**整块字幕凭空消失**
        （实测 92-117s / 299-322s / 506-529s 三块全没了，全片 205 段掉到 190 段）。
        改成无热词重试，热词就变成"有收益就吃、有副作用就退回去"的纯增益开关。

        回显判定用 `is_prompt_echo(text, echo_ref)`：echo_ref 就是本次随请求
        发出去的上一句原文（见 transcribe）。
        """
        r = self.transcribe_wav(wav, context, lang_key, timeout=timeout)
        text = (r.get("text") or "").strip()
        if not context or not is_prompt_echo(text, echo_ref):
            return text
        print(f"[asr] 热词回显，改用无热词重试：{text[:40]!r}", flush=True)
        with self._lock:
            self.echo_retries += 1
        try:
            r2 = self.transcribe_wav(wav, "", lang_key, timeout=timeout)
        except Exception:
            return ""
        return (r2.get("text") or "").strip()

    def transcribe_wav(self, wav: Path, context: str = "", lang_key: str = "",
                       timeout: float = 600.0) -> dict:
        """调常驻服务转写单个 wav，返回 {text, rtf, wall_s}。

        `context` 是热词/领域词偏置。实测 audiocpp 只认 `context` 这个字段名：
        `prompt`、`hotwords` 都被静默忽略（同一段音频、同一份解码设置下输出
        逐字节相同）。语言按请求的 lang_key 传（AUDIOCPP_LANG 映射），映射不到
        时退回构造配置里的 self.language。

        `timeout` 是**单段请求**的超时，由调用方按音频时长收紧后传入：旧实现写死
        600s，小于 8s 的一段音频最坏也能把请求线程钉住 10 分钟；3s 节奏下请求
        堆积，且 server_app 的空闲回收看到 _INFLIGHT>0 就一直不触发。
        """
        body = {"model": STREAM_MODEL_ID, "audio": str(wav),
                "language": AUDIOCPP_LANG.get(lang_key, self.language)}
        if context:
            body["context"] = context
        req = urllib.request.Request(self._url("/v1/audio/transcriptions"),
                                     data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
        d["wall_s"] = time.perf_counter() - t0
        return d

    @staticmethod
    def _write_wav(path: Path, pcm: np.ndarray) -> None:
        data = np.clip(pcm, -1.0, 1.0)
        ints = (data * 32767.0).astype("<i2")
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(ints.tobytes())

    def transcribe(
        self,
        pcm: np.ndarray,
        lang_key: str = "ja",
        video_start_ms: int = 0,
        keep_from_ms: int = 0,
        vad_cfg: dict | None = None,
        seg_cfg: dict | None = None,
        extra_context: str = "",
        tmpdir: Path | None = None,
    ) -> dict:
        """接口对齐 PyTorch 引擎：pcm(float32 @16k) → {segments, asr_ms, skipped}。

        seg_cfg 目前只实现 min 侧（过短段合并，见 _merge_short_segments）：
        本后端没有词级时间戳，max_chars/pause_sec 无法按词实现。max_sec 由
        max_span_sec（硬切）承担。"""
        t0 = time.perf_counter()
        seg_cfg = seg_cfg or {}
        min_ms = int((vad_cfg or {}).get("min_speech_ms", self.min_speech_ms))
        d = Path(tmpdir) if tmpdir else _run_dir()

        spans = self.speech_spans(pcm, tmpdir=d)
        if spans is None:
            # VAD 本身失败（区别于"真没有语音"）：明确带回错误类别，绝不静默丢块。
            # 只回类别不回全文：全文含绝对路径/用户名（见 _error_kind），而
            # /transcribe 对局域网开放；全文已经 print 到服务日志。
            return {"language": lang_key, "segments": [], "asr_ms": 0.0,
                    "skipped": True, "backend": "audiocpp",
                    "error": self.last_vad_error_kind or "vad_failed"}
        if not spans:
            return {"language": lang_key, "segments": [], "asr_ms": 0.0,
                    "skipped": True, "backend": "audiocpp"}
        merged_raw = spans

        # 合并过近的语音段（避免切出大量 0.x 秒碎片）
        merged: list = []
        for s, e in merged_raw:
            if merged and s - merged[-1][1] <= self.merge_gap:
                merged[-1] = (merged[-1][0], e)
            else:
                merged.append((s, e))
        # 过滤低于阈值的碎段
        merged = [(s, e) for s, e in merged if (e - s) * 1000 >= min_ms]
        # 长段再切：VAD 合并出的长段（>max_span_sec）会让模型退化出成百连重复，
        # 实测有一段 11.5s 吐出 342 连「あ」。切成 <=max_span_sec 后消失。
        capped: list = []
        for s, e in merged:
            cur = s
            while e - cur > self.max_span_sec:
                capped.append((cur, cur + self.max_span_sec))
                cur += self.max_span_sec
            if e - cur > 0.05:
                capped.append((cur, e))
        merged = capped

        pad = self.pad_sec
        # 热词 = 上一句转写原文（借鉴 realtime-subtitle 的 context carryover）：
        # 治人名/专名跨块听错。
        context = (extra_context or "").strip()
        # 进循环前先确认上游活着，必要时重拉一次（评审 F12 的自愈闭环）。
        # 放在这里而不是只在 lifespan：上游崩了之后**没有任何请求路径**会重拉，
        # 于是后面每一段都失败、每段都空，用户看到的是"字幕突然全没了"。
        self._ensure_alive("转写前")
        segs = []
        healed = False          # 本次转写是否已经重拉过一次上游
        failed_spans = 0        # 连接级/超时级别的失败段数（用于判断"整块都废了"）
        for i, (s, e) in enumerate(merged):
            a = max(0, int((s - pad) * SR))
            b = min(len(pcm), int((e + pad) * SR))
            if b - a < int(0.2 * SR):
                continue
            # 文件名带线程 id：并发时同 pid 同毫秒互覆
            wav = d / f"asr_{os.getpid()}_{threading.get_ident()}_{int(time.time()*1000)}_{i}.wav"
            # 单段请求超时按**本段音频时长**收紧（与 VAD 侧 min(300, duration*2)
            # 同一意图）：旧实现写死 600s，≤8s 的一段最坏把请求线程钉 10 分钟，
            # 3s 节奏下请求堆积，_INFLIGHT>0 还会让空闲回收一直不触发。
            # 下界 60s：CPU 后端 rtf≈0.04，放大 30× 已有充足余量。
            span_timeout = max(60.0, min(600.0, (b - a) / float(SR) * 30.0))
            try:
                self._write_wav(wav, pcm[a:b])
                text = self._transcribe_span(wav, context, lang_key,
                                             echo_ref=extra_context,
                                             timeout=span_timeout)
            except Exception as ex:
                kind = self._error_kind(ex)
                # 回给客户端的只有错误类别：详细文本含临时 wav 绝对路径与本机
                # 用户名（见 _error_kind），而 /transcribe 对局域网开放。
                # 全文留在服务日志里，诊断信息不丢。
                print(f"[asr] 单段转写失败（{kind}）：{type(ex).__name__}: {ex}", flush=True)
                # 连接级失败 = 上游 audiocpp_server 很可能已经崩了（本后端的 VAD 走
                # 一次性 CLI，所以 speech_spans 照常成功、只有这里的 POST 全线失败）。
                # 旧实现只会把每一段都 append 成空段 ⇒ 头显看到**字幕静默全空**，而
                # /health 的 asr_ready 依旧 True（评审 F12）。这里重拉一次并给这一段
                # 一次机会；每次转写最多重拉一次，避免对着坏模型反复拉起进程。
                if kind == "backend_unavailable" and not healed:
                    healed = True
                    if self._ensure_alive("单段连接失败后"):
                        try:
                            self._write_wav(wav, pcm[a:b])
                            text = self._transcribe_span(wav, context, lang_key,
                                                         echo_ref=extra_context,
                                                         timeout=span_timeout)
                            ex = None
                        except Exception as ex2:
                            ex = ex2
                            kind = self._error_kind(ex2)
                            print(f"[asr] 重拉上游后单段仍失败（{kind}）："
                                  f"{type(ex2).__name__}: {ex2}", flush=True)
                if ex is not None:
                    segs.append({"start_ms": video_start_ms + int(round(a / SR * 1000)),
                                 "end_ms": video_start_ms + int(round(b / SR * 1000)),
                                 "text": "", "error": kind})
                    failed_spans += 1
                    continue
            finally:
                try:
                    wav.unlink()
                except Exception:
                    pass
            if not text:
                continue
            # 重复退化过滤：判据与 PyTorch 后端共用（text_filters.has_repetition_loop，
            # 含合法拖长音「ー」放宽），不再用本地 8 连正则——两后端阈值曾差一倍
            if has_repetition_loop(text):
                continue
            # 拉丁幻觉过滤：日语音频里"没有假名也没有汉字"的短输出（实测 Whisper 会吐
            # `.`/`Thank`/`I`/`you` 并当台词上屏）。判据见 text_filters.is_latin_hallucination。
            if self.drop_latin and is_latin_hallucination(text, lang_key):
                continue
            segs.append({
                "start_ms": video_start_ms + int(round(a / SR * 1000)),
                "end_ms": video_start_ms + int(round(b / SR * 1000)),
                "text": text,
            })

        # seg_cfg 的 min 侧：过短碎片并进相邻句（对齐 PyTorch 引擎的 _merge_short；
        # 本后端无词级时间戳，max_chars/pause_sec 无法按词实现，见方法 docstring）
        segs = self._merge_short_segments(segs, seg_cfg)

        # 逐段已在 _transcribe_span 里做过"复读→无热词重试"，这里不再整块丢弃——
        # 整块丢弃会一次损失整段音频，代价远大于它的收益。

        if keep_from_ms:
            # 去重判据：只丢"整句基本都在重叠区"的段（句尾也早于 keep_from+300ms）。
            # 判据已抽到 text_filters.keep_segment 与 PyTorch 引擎共用——此前
            # 只在本侧修了容差，PyTorch 回退路径仍是 "start < keep_from 即丢"，
            # 跨块长句在那条路径上照样两块都丢。
            segs = [x for x in segs
                    if keep_segment(x["start_ms"], x["end_ms"], keep_from_ms)]
        out = {"language": lang_key, "segments": segs,
               "asr_ms": round((time.perf_counter() - t0) * 1000, 1),
               "skipped": False, "backend": "audiocpp",
               "context_chars": len(context)}
        # 整块都废了就别装作"这块没有语音"（评审 F12）：只要**所有**尝试过的段都失败、
        # 且一段文本都没拿到，就把错误类别如实带出去。否则调用方（与用户）看到的是
        # 一次"正常但空"的结果——上游全崩与真静音完全不可区分。
        if failed_spans and failed_spans >= len(segs) and not any(x.get("text") for x in segs):
            out["error"] = "backend_unavailable"
            out["skipped"] = True
            print(f"[asr] 本块 {failed_spans} 段全部失败（上游不可用），已如实上报", flush=True)
        return out

    @staticmethod
    def _merge_short_segments(segs: list, seg_cfg: dict) -> list:
        """把过短的碎片并进相邻句（与原 PyTorch 引擎的 _merge_short 同判据：
        时长 < min_sec 或字数 < min_chars、与前句间隔 < 0.8s、合并后不超长）。

        拼接用 join_tokens（与 PyTorch 侧同一实现）：旧实现是字符串直接相加，
        英文/数字两段会被粘成一坨（"hello"+"world" → "helloworld"）。
        """
        if not seg_cfg or len(segs) < 2:
            return segs
        min_sec = float(seg_cfg.get("min_sec", 1.2))
        min_chars = int(seg_cfg.get("min_chars", 4))
        max_sec = float(seg_cfg.get("max_sec", 8.0)) * 1.5
        merged: list = []
        for s in segs:
            if merged and not s.get("error"):
                prev = merged[-1]
                if prev.get("error"):
                    merged.append(dict(s))
                    continue
                dur = (s["end_ms"] - s["start_ms"]) / 1000
                gap = (s["start_ms"] - prev["end_ms"]) / 1000
                if (dur < min_sec or len(s["text"]) < min_chars) and gap < 0.8 \
                        and (s["end_ms"] - prev["start_ms"]) / 1000 <= max_sec:
                    prev["end_ms"] = s["end_ms"]
                    prev["text"] = join_tokens([prev["text"], s["text"]])
                    continue
            merged.append(dict(s))
        # 首句过短则并进下一句
        if len(merged) >= 2 and not merged[0].get("error"):
            first = merged[0]
            if (first["end_ms"] - first["start_ms"]) / 1000 < min_sec \
                    or len(first["text"]) < min_chars:
                nxt = merged[1]
                if not nxt.get("error") \
                        and (nxt["end_ms"] - first["start_ms"]) / 1000 <= max_sec:
                    nxt["start_ms"] = first["start_ms"]
                    nxt["text"] = join_tokens([first["text"], nxt["text"]])
                    merged.pop(0)
        return merged


if __name__ == "__main__":
    # 自检：对一段音频跑 VAD + 转写
    import sys
    be = AudioCppBackend({"backend": "cpu"})
    print("服务就绪:", be.ensure_server())
    wav = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if wav:
        import wave as _w
        with _w.open(str(wav), "rb") as w:
            raw = w.readframes(w.getnframes())
        pcm = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        t0 = time.time()
        out = be.transcribe(pcm, "ja", 0, 0, {"min_speech_ms": 250},
                            {"max_sec": 8.0, "max_chars": 50, "pause_sec": 0.8})
        print(f"耗时 {time.time()-t0:.1f}s  段数 {len(out['segments'])}")
        for s in out["segments"]:
            print(f"  {s['start_ms']/1000:7.1f}  {s['text']}")


