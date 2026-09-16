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
import urllib.request
import wave
from pathlib import Path

import numpy as np

from text_filters import has_repetition_loop, is_glossary_echo, is_prompt_echo

SR = 16000
AUDIOCPP_DIR = Path(os.environ.get("AUDIOCPP_DIR", r"E:\audiocpp-portable"))

# 请求的 lang_key → audiocpp 的语言名。此前请求体写死构造时的 self.language，
# `/transcribe?lang=en` 在本路径下会被静默按日语解码。
AUDIOCPP_LANG = {"ja": "Japanese", "en": "English", "zh": "Chinese",
                 "ko": "Korean", "yue": "Cantonese"}


class AudioCppError(RuntimeError):
    pass


class AudioCppBackend:
    """封装 audiocpp_server 的 ASR + silero_vad。"""

    def __init__(self, cfg: dict, glossary=None, use_context: bool = True,
                 context_max_chars: int = 0):
        self.dir = Path(cfg.get("dir") or AUDIOCPP_DIR)
        self.backend = str(cfg.get("backend", "cpu"))          # cpu | cuda
        self.threads = int(cfg.get("threads", max(1, (os.cpu_count() or 4) - 1)))
        self.port = int(cfg.get("port", 8083))
        self.host = str(cfg.get("host", "127.0.0.1"))
        self.model = str(cfg.get("model") or (self.dir / "models" / "Qwen3-ASR-0.6B"))
        self.vad_model = str(cfg.get("vad_model") or
                             (self.dir / "assets" / "framework" / "models" / "silero_vad"))
        self.language = str(cfg.get("language", "Japanese"))
        self.pad_sec = float(cfg.get("pad_sec", 0.25))
        self.merge_gap = float(cfg.get("merge_gap_sec", 0.5))
        self.min_speech_ms = int(cfg.get("min_speech_ms", 250))
        # 单段最长秒数：超过此长度的语音段先切开再送 ASR（防退化）
        self.max_span_sec = float(cfg.get("max_span_sec", 8.0))
        # 热词/上下文偏置。
        #
        # ⚠️ 这里曾经是**死配置**：`asr.use_glossary_context` 只写在 PyTorch 引擎的
        # `AsrEngine.transcribe(context=...)` 里，而 server_app 走 audiocpp 时调的是
        # `AudioCppBackend.transcribe(...)`，`transcribe_wav` 的请求体只有
        # {model, audio, language}——热词从来没发出去过。实测确认 audiocpp **支持**
        # `context`（解码确定：同一请求三次哈希一致；带 context 时输出稳定地不同；
        # 而 `prompt`/`hotwords` 是被忽略的），所以这里补上转发。
        self.glossary = glossary
        self.use_context = bool(use_context)
        self.context_max_chars = int(context_max_chars)
        self._proc: subprocess.Popen | None = None
        self._job_handle = None       # Windows Job Object 句柄（父进程崩溃时带走子进程）
        self._lock = threading.Lock()
        self.echo_retries = 0        # 热词复读触发无热词重试的次数（诊断用）
        self.last_vad_error = ""     # 最近一次 VAD 失败原因（"" = 正常）

    def _build_context(self, lang_key: str) -> str:
        """本语言的 ASR 热词提示；未启用/无术语表时返回空串。"""
        if not self.use_context or self.glossary is None:
            return ""
        try:
            return self.glossary.asr_context(lang_key, self.context_max_chars)
        except Exception as e:
            print(f"[asr] 热词提示构建失败（忽略）：{type(e).__name__}: {e}", flush=True)
            return ""

    def _is_glossary_echo(self, text: str, lang_key: str) -> bool:
        """热词表被当台词复读的检测。

        判据统一在 text_filters.is_glossary_echo（与 PyTorch 引擎共用同一实现，
        此前两边各写一份已出现单向漂移：覆盖率重复计数的旧算法在一边修掉了、
        另一边还留着，嵌套键能把覆盖率算出 >1 而误杀正常句子）。"""
        if self.glossary is None:
            return False
        try:
            keys = self.glossary.keys(lang_key)
        except Exception:
            return False
        return is_glossary_echo(text, keys)

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

    def ensure_server(self, wait_s: float = 60.0) -> bool:
        """确保常驻服务在跑；已在跑则直接返回。

        ⚠️ 复用判定只看 /health 的 status=ok，**不校验加载的模型**——如果
        8083 上残留着一个加载了旧模型的服务，会被静默复用。换过模型/参数
        后请先 stop_server() 或手工结束旧进程再启动。
        """
        with self._lock:
            if self.probe():
                print(f"[asr] audiocpp 端口 {self.port} 已有服务在跑，直接复用"
                      "（注意：不校验其加载的模型是否与本配置一致）", flush=True)
                return True
            exe = self.exe_dir / "audiocpp_server.exe"
            if not exe.exists():
                raise AudioCppError(f"找不到 audiocpp_server.exe：{exe}")
            cfg_path = Path(tempfile.gettempdir()) / f"audiocpp_asr_{self.port}.json"
            cfg_path.write_text(json.dumps({
                "host": self.host, "port": self.port,
                "backend": "cuda" if self.backend != "cpu" else "cpu",
                "device": 0, "threads": self.threads,
                "models": [{"id": "qwen3-asr", "family": "qwen3_asr",
                            "path": self.model, "task": "asr", "mode": "offline"}],
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
                    raise AudioCppError(f"audiocpp_server 启动即退出（code {self._proc.returncode}）")
                time.sleep(0.5)
            raise AudioCppError(f"audiocpp_server 未在 {wait_s}s 内就绪")

    def _attach_kill_on_close(self, proc: subprocess.Popen) -> None:
        """把子进程加入 Job Object（KILL_ON_JOB_CLOSE）：本进程崩溃/被强杀时，
        句柄随进程关闭，audiocpp_server（约 2.8GB）跟着被带走，不再变孤儿。
        纯 ctypes 实现，失败时静默降级为旧行为（下次启动 reap 兜底）。"""
        if os.name != "nt":
            return
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
            print(f"[asr] Job Object 保护不可用（{e}），跳过", flush=True)

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
            self._job_handle = None   # 进程已死，关闭 Job 句柄（幂等，OS 兜底回收）

    # ------------------------------------------------------------------ VAD
    def speech_spans(self, pcm: np.ndarray, tmpdir: Path | None = None) -> list | None:
        """写临时 wav → audiocpp VAD → [(start_sec, end_sec)]（相对本块）。

        返回 **None 表示 VAD 本身失败**（CLI 缺失/崩溃/超时），[] 才是"真没有
        语音"。旧实现把所有异常一律吞成 []，上层判 skipped 后静默丢块——CLI
        一坏整片字幕无声消失，且与真静音完全不可区分。失败原因存
        self.last_vad_error 供上层带回 error 字段。
        """
        exe = self.exe_dir / "audiocpp_cli.exe"
        d = Path(tmpdir or tempfile.gettempdir())
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
            )
            if r.returncode != 0:
                self.last_vad_error = (f"VAD CLI 退出码 {r.returncode}："
                                       f"{(r.stderr or '').strip()[:200] or '无 stderr'}")
                print(f"[asr] {self.last_vad_error}", flush=True)
                return None
            if not out_json.exists():
                self.last_vad_error = "VAD CLI 未产出 chunks 文件（可能被秒退）"
                print(f"[asr] {self.last_vad_error}", flush=True)
                return None
            data = json.loads(out_json.read_text(encoding="utf-8"))
            spans = []
            for c in data:
                a = float(c["start_sample"]) / SR
                b = float(c["end_sample"]) / SR
                if b > a:
                    spans.append((a, b))
            self.last_vad_error = ""
            return spans
        except Exception as e:
            self.last_vad_error = f"VAD 失败：{type(e).__name__}: {e}"
            print(f"[asr] {self.last_vad_error}", flush=True)
            return None
        finally:
            for f in (wav_in, out_json):
                try:
                    f.unlink()
                except Exception:
                    pass

    # ------------------------------------------------------------- 转写
    def _transcribe_span(self, wav, context: str, lang_key: str, echo_ref: str = "") -> str:
        """转写单个语音段；热词导致复读时**改用无热词重试一次**。

        为什么必须重试而不是丢弃：audiocpp 的 Qwen3-ASR 在喘息/气声这类
        非清晰语音上，有相当高的概率把 `context` 整段复读出来当结果
        （实测 5 个窗口里 3 个中招，输出就是 `ゆあ、女子アナ、ソープ嬢、…`）。
        直接丢弃的后果是**整块字幕凭空消失**（实测 92-117s / 299-322s /
        506-529s 三块全没了，全片 205 段掉到 190 段）。改成无热词重试，
        热词就变成"有收益就吃、有副作用就退回去"的纯增益开关。

        回显判定有两路：`_is_glossary_echo`（术语表复读）与 `is_prompt_echo`
        （上一句转写 echo_ref 的回显——v1.6.12 起热词里追加了上一句原文，
        静音段把上一句吐出来的情况与术语表复读同性质）。

        另外这也解释了为什么开热词会慢 2.5 倍：模型把 130 个热词一个个生成
        出来（约 130 token）才被丢弃，纯属白烧 CPU。
        """
        r = self.transcribe_wav(wav, context, lang_key)
        text = (r.get("text") or "").strip()
        if not context or not (self._is_glossary_echo(text, lang_key)
                               or is_prompt_echo(text, echo_ref)):
            return text
        print(f"[asr] 热词表复读，改用无热词重试：{text[:40]!r}", flush=True)
        with self._lock:
            self.echo_retries += 1
        try:
            r2 = self.transcribe_wav(wav, "", lang_key)
        except Exception:
            return ""
        t2 = (r2.get("text") or "").strip()
        if not t2 or self._is_glossary_echo(t2, lang_key) or is_prompt_echo(t2, echo_ref):
            return ""
        return t2

    def transcribe_wav(self, wav: Path, context: str = "", lang_key: str = "") -> dict:
        """调常驻服务转写单个 wav，返回 {text, rtf, wall_s}。

        `context` 是热词/领域词偏置。实测 audiocpp 只认 `context` 这个字段名：
        `prompt`、`hotwords` 都被静默忽略（同一段音频、同一份解码设置下输出
        逐字节相同）。语言按请求的 lang_key 传（AUDIOCPP_LANG 映射），映射不到
        时退回构造配置里的 self.language。
        """
        body = {"model": "qwen3-asr", "audio": str(wav),
                "language": AUDIOCPP_LANG.get(lang_key, self.language)}
        if context:
            body["context"] = context
        req = urllib.request.Request(self._url("/v1/audio/transcriptions"),
                                     data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=600) as r:
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
        d = Path(tmpdir or tempfile.gettempdir())

        spans = self.speech_spans(pcm, tmpdir=d)
        if spans is None:
            # VAD 本身失败（区别于"真没有语音"）：明确带回错误，绝不静默丢块
            return {"language": lang_key, "segments": [], "asr_ms": 0.0,
                    "skipped": True, "backend": "audiocpp",
                    "error": self.last_vad_error or "vad_failed"}
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
        context = self._build_context(lang_key)
        if extra_context:
            # 上一句转写结果作为热词补充（借鉴 realtime-subtitle 的 context carryover）：
            # 治人名/专名跨块听错。echo_ref 单独保存，复读重试判定要区分
            # "术语表回显"与"上一句回显"。
            context = (context + " " + extra_context).strip()
        segs = []
        for i, (s, e) in enumerate(merged):
            a = max(0, int((s - pad) * SR))
            b = min(len(pcm), int((e + pad) * SR))
            if b - a < int(0.2 * SR):
                continue
            # 文件名带线程 id：并发时同 pid 同毫秒互覆
            wav = d / f"asr_{os.getpid()}_{threading.get_ident()}_{int(time.time()*1000)}_{i}.wav"
            try:
                self._write_wav(wav, pcm[a:b])
                text = self._transcribe_span(wav, context, lang_key, echo_ref=extra_context)
            except Exception as ex:
                segs.append({"start_ms": video_start_ms + int(round(a / SR * 1000)),
                             "end_ms": video_start_ms + int(round(b / SR * 1000)),
                             "text": "", "error": f"{type(ex).__name__}: {ex}"})
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
            # 旧判据 "start < keep_from 即丢" 会把**跨块长句在两个块里都扔掉**：
            # 句子横跨块 A 尾/块 B 头时，两边的 start 都落在各自的 keep_from 之前
            # （实测 192.8s "今天特别破例让你看看哦" 整句消失）。
            # 容差 300ms：句尾恰好在重叠区内但主体在新区块的句子保留。
            segs = [x for x in segs
                    if x["start_ms"] >= keep_from_ms
                    or x["end_ms"] > keep_from_ms + 300]
        return {"language": lang_key, "segments": segs,
                "asr_ms": round((time.perf_counter() - t0) * 1000, 1),
                "skipped": False, "backend": "audiocpp",
                "context_chars": len(context)}

    @staticmethod
    def _merge_short_segments(segs: list, seg_cfg: dict) -> list:
        """把过短的碎片并进相邻句（判据与 AsrEngine._merge_short 一致：
        时长 < min_sec 或字数 < min_chars、与前句间隔 < 0.8s、合并后不超长）。"""
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
                    prev["text"] = prev["text"] + s["text"]
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
                    nxt["text"] = first["text"] + nxt["text"]
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


