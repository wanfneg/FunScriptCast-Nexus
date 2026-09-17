# -*- coding: utf-8 -*-
"""Whisper 系 ASR 后端（faster-whisper / CTranslate2）——与 audiocpp 并列的可选主引擎。

为什么值得接入（R44 整片实测，SIVR-001 全片，同素材/同翻译/同评分工具，
见 tests/whisper_scheme.py 与 iteration_shturl.md Round 44）：
  只把 ASR 从 audio.cpp Qwen3-0.6B 换成 kotoba-whisper-v2.0-faster、**上传协议不变**：
    · 内容覆盖率 0.488 → 0.538（+10%）
    · 时序中位 −90ms → +30ms
    · ASR 快约 3×（CTranslate2 CUDA float16，整片 1254s 音频 rtf≈0.007）
  切句方式（内容自适应 RMS vs 定长块）在同一 ASR 下覆盖率持平 ⇒ 杠杆在 ASR 本身，
  这也是本后端"沿用生产 3s/1s 定长块协议"的依据。

三条铁律（都来自整片实测，别当冗余"优化"掉）：
  1. **不给 initial_prompt**——热词、上一句回传都不给。实测：给 prompt 的整片会吐
     6~7 段裸英文幻觉（`I`/`you`/`Thank` 直接当台词上屏），不给则 0 段（坑 #39）；
     且 A/B 显示热词没有可测的内容收益（三档覆盖率在同一噪声带）。因此本后端
     **刻意忽略 extra_context**，也不读术语表。
  2. `condition_on_previous_text=False`：跨块携带上文会连带幻觉，关掉（与实测同参）。
  3. 模型默认**只用本地 HF 缓存**（local_files_only，~1.4GB）：绝不在请求/启动里
     联网拉模型（坑 #31 同源——请求内下载会撞头显 180s readTimeout）。换机器先
     手工下载，或显式配 asr.whisper.allow_download=true（只在**启动**时下载）。

时间戳口径：faster-whisper 开 vad_filter 后返回的 start/end 仍是**相对输入音频**的
秒数（它内部会从裁剪后的语音段映射回原时间轴），这里直接加 video_start_ms 得到
视频绝对时间——R44 的 --cut blocks 档即按此口径算出 +30ms 中位偏差，已验证。

接口对齐 audiocpp / PyTorch 后端：
    transcribe(pcm, lang_key, video_start_ms, keep_from_ms, vad_cfg, seg_cfg,
               extra_context) → {language, segments, asr_ms, skipped}
vad_cfg/seg_cfg 刻意不消费：静音过滤由 faster-whisper 内建 vad_filter 承担
（本后端没有"先 VAD 出语音段再逐段送"的外层结构，无需 min_speech/合并参数）；
段级时间戳由模型给出，不需要 seg_cfg 的分句上限。
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

# 必须在 faster_whisper（连带 huggingface_hub）import 之前设置：hub 的镜像端点
# 在 import 时读成常量，运行期改环境变量不再生效（审查 P2-2）。
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

from text_filters import (has_repetition_loop, is_latin_hallucination,
                          keep_segment, strip_wrap_quotes)

LANG_DEFAULT = "ja"


class WhisperBackend:
    """faster-whisper 主 ASR 后端：进程内模型、串行推理、零外部进程。"""

    def __init__(self, cfg: dict | None = None, drop_latin: bool = True):
        cfg = cfg or {}
        self.model_ref = str(cfg.get("model", "kotoba-tech/kotoba-whisper-v2.0-faster"))
        self.device = str(cfg.get("device", "cuda"))
        self.compute_type = str(cfg.get("compute_type", "float16"))
        self.beam_size = int(cfg.get("beam_size", 5))
        self.language = str(cfg.get("language", LANG_DEFAULT))
        self.min_silence_ms = int(cfg.get("min_silence_duration_ms", 300))
        self.allow_download = bool(cfg.get("allow_download", False))
        self.drop_latin = bool(drop_latin)
        self.backend_kind = "whisper"
        self.load_s = 0.0
        self._model = None
        # CTranslate2 默认 num_workers=1，官方不建议并发进同一模型；与 AsrEngine
        # 同思路串行化（/transcribe 走线程池，可能并发进来）。
        self._lock = threading.Lock()

    # ---- /health 兼容属性（server_app 会读 vad/use_aligner/model）----
    @property
    def vad(self) -> bool:
        """VAD 由 faster-whisper 内建 vad_filter 承担。"""
        return True

    @property
    def use_aligner(self) -> bool:
        return False

    @property
    def model(self) -> str:
        """/health 的 asr_model 直接显示模型名（不是本机路径）。"""
        return self.model_ref

    # ---------------------------------------------------------------- 模型
    def ensure_model(self) -> None:
        """加载模型（幂等）。正常在 lifespan 里调用（启动期 +2.5~3.9s，实测）；
        transcribe 里再调一次只是兜底。"""
        # 锁内做加载：transcribe 是先调本方法再拿推理锁，不构成重入；
        # 并发首调若无锁会双载 1.4GB 模型（审查 P2-5）
        with self._lock:
            self._ensure_locked()

    def _ensure_locked(self) -> None:
        if self._model is not None:
            return
        # 自愈历史损坏：refs/main 带换行会让 faster-whisper 解析出带换行的
        # snapshot 目录名而永远找不到模型（审查 P0-1 实测复现）
        try:
            from huggingface_hub.constants import HF_HUB_CACHE
            repo = Path(HF_HUB_CACHE) / ("models--" + self.model_ref.replace("/", "--"))
            ref = repo / "refs" / "main"
            if ref.exists():
                txt = ref.read_text(encoding="utf-8").strip()
                if txt and txt != ref.read_text(encoding="utf-8"):
                    ref.write_text(txt, encoding="utf-8")
        except Exception:
            pass
        t0 = time.perf_counter()
        from faster_whisper import WhisperModel
        try:
            self._model = WhisperModel(self.model_ref, device=self.device,
                                       compute_type=self.compute_type,
                                       local_files_only=True)
        except Exception as cuda_err:
            # 无 N 卡/驱动不全时自动降级 CPU（kotoba-whisper 本就有 CPU 兜底先例），
            # 不能让"下了 1.4GB 模型却起不来"成为死胡同（审查 P1-2）
            if str(self.device).startswith("cuda"):
                print(f"[asr] CUDA 不可用（{type(cuda_err).__name__}），降级 CPU/int8 重试",
                      flush=True)
                self.device, self.compute_type = "cpu", "int8"
                self._model = WhisperModel(self.model_ref, device=self.device,
                                           compute_type=self.compute_type,
                                           local_files_only=True)
            else:
                raise
        if self._model is None:
            if not self.allow_download:
                raise RuntimeError(
                    f"whisper 模型不在本地 HF 缓存（{self.model_ref}）："
                    f"{type(e).__name__}: {e}。请先手工下载（huggingface.co/kotoba-tech/"
                    "kotoba-whisper-v2.0-faster，约 1.4GB，国内可设 "
                    "HF_ENDPOINT=https://hf-mirror.com），或显式配 "
                    "asr.whisper.allow_download=true 允许**启动时**联网下载")
            if self.model_ref.startswith("kotoba-tech/") and not os.environ.get("HF_ENDPOINT"):
                os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
            print(f"[asr] whisper 模型本地缺失，启动期下载 {self.model_ref}（约 1.4GB）",
                  flush=True)
            self._model = WhisperModel(self.model_ref, device=self.device,
                                       compute_type=self.compute_type)
        self.load_s = time.perf_counter() - t0
        print(f"[asr] whisper 模型就绪：{self.model_ref}（{self.device}/{self.compute_type}，"
              f"加载 {self.load_s:.1f}s）", flush=True)

    def stop_server(self) -> None:
        """接口兼容（idle reaper / lifespan 收尾会调）。模型在本进程内，
        随进程退出由 OS 释放，无需显式处理。"""
        pass

    # ---------------------------------------------------------------- 转写
    @staticmethod
    def _keep(text: str, lang_key: str, drop_latin: bool) -> bool:
        """段级过滤。不给 prompt ⇒ 不存在"热词回显/上一句回显"，那两个判据
        在这里没有输入条件，不写（写了反而是坑 #29 式的误杀源）。"""
        t = (text or "").strip()
        if not t:
            return False
        if has_repetition_loop(t):
            return False
        if drop_latin and is_latin_hallucination(t, lang_key):
            return False
        return True

    def transcribe(self, pcm, lang_key: str = "ja", video_start_ms: int = 0,
                   keep_from_ms: int = 0, vad_cfg=None, seg_cfg=None,
                   extra_context: str = "") -> dict:
        """pcm: float32 [-1,1] @16k mono（与另两个后端同约定）。

        extra_context 刻意忽略——见模块注释"三条铁律"第 1 条。
        skipped=True 仅在"模型 + VAD 一段都没给出"时置位：语义是"整块没有语音"，
        同时让 server_app 的 whisper 二次兜底跳过（主引擎已经是 whisper，
        换 CPU int8 把同一段音频再跑一遍纯属浪费）。
        """
        t0 = time.perf_counter()
        self.ensure_model()
        # faster-whisper 只认 2 字母码，"zh-CN" 这类带地区后缀会直接 ValueError
        lang = ((lang_key or self.language).split("-")[0].strip().lower()
                or self.language)
        segs: list[dict] = []
        with self._lock:
            # 迭代生成器必须在持锁期间完成（faster-whisper 的 transcribe 返回惰性生成器）
            segments, _info = self._model.transcribe(
                pcm, language=lang,
                beam_size=self.beam_size,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": self.min_silence_ms},
                condition_on_previous_text=False,
            )
            for s in segments:
                text = strip_wrap_quotes((s.text or "").strip())
                if not text or not self._keep(text, lang, self.drop_latin):
                    continue
                s0 = video_start_ms + int(round(s.start * 1000))
                s1 = video_start_ms + int(round(s.end * 1000))
                # 重叠区去重与生产共用同一判据（text_filters.keep_segment）
                if not keep_segment(s0, s1, keep_from_ms):
                    continue
                segs.append({"start_ms": s0, "end_ms": s1, "text": text})
        return {"language": lang, "segments": segs,
                "asr_ms": round((time.perf_counter() - t0) * 1000, 1),
                "skipped": not segs, "backend": self.backend_kind}
