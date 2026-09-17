# -*- coding: utf-8 -*-
"""二次识别兜底：kotoba-whisper（日语特化蒸馏 Whisper，faster-whisper/CTranslate2 格式）。

定位：主 ASR（audio.cpp Qwen3-ASR 0.6B）对某块**零输出**、但音频里确有语音能量时
（实测：气声/耳语台词 916.6/978.0/995.5s 三处，纯净 ±1.5s 音频单独推流也是零输出），
用本引擎做二次识别。只做兜底，不做主引擎。

参考：
  https://huggingface.co/kotoba-tech/kotoba-whisper-v2.0-faster （CTranslate2 转换版）
  https://huggingface.co/kotoba-tech/kotoba-whisper-v2.0 （原版，含精度对比）

依赖（可选，未装时本类不可用，调用方跳过）：pip install faster-whisper
模型首次使用时自动从 HF 下载（国内设 HF_ENDPOINT=https://hf-mirror.com），
下载后会缓存到 HF 缓存目录，可离线。
"""
from __future__ import annotations

import os
import threading

import numpy as np


class WhisperFallback:
    """faster-whisper 封装：懒加载 + 线程安全 + 最小依赖。"""

    def __init__(self, cfg: dict | None = None):
        cfg = cfg or {}
        self.model_ref = str(cfg.get("model", "kotoba-tech/kotoba-whisper-v2.0-faster"))
        self.device = str(cfg.get("device", "cpu"))
        self.compute_type = str(cfg.get("compute_type", "int8"))
        self.language = str(cfg.get("language", "ja"))
        # 默认**禁止**在请求里联网下载（见 _ensure 的说明）
        self.allow_download = bool(cfg.get("allow_download", False))
        self._model = None
        self._lock = threading.Lock()
        self.last_error = ""

    def available(self) -> bool:
        try:
            import faster_whisper  # noqa: F401
            return True
        except ImportError:
            self.last_error = "faster-whisper 未安装"
            return False

    def _ensure(self):
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            from faster_whisper import WhisperModel
            # 先用**本地缓存**加载，绝不在一次 /transcribe 里临时下载模型。
            # 真炸点：本兜底由识别请求同步触发（server_app._transcribe_impl 里
            # run_in_threadpool 调用），此前首次使用会直接去 HF 拉约 800MB ——
            # 播放中途卡住数分钟，且很可能超过头显 uploadChunk 的 180s readTimeout，
            # 于是"兜底"反而把正常块也拖丢。模型应随安装目录预置（见模块注释）。
            try:
                self._model = WhisperModel(self.model_ref, device=self.device,
                                           compute_type=self.compute_type,
                                           local_files_only=True)
                return
            except Exception as e:
                self.last_error = f"本地无缓存：{type(e).__name__}: {e}"
            if not self.allow_download:
                raise RuntimeError(
                    f"二次识别兜底模型不在本地（{self.model_ref}）：{self.last_error}；"
                    "已跳过兜底（不影响主链路）。要启用请先手工下载该模型，"
                    "或设 asr.whisper_fallback.allow_download=true（会在请求内联网下载约 800MB）")
            # 国内网络：未显式配置镜像时默认走 hf-mirror（模型 ~800MB）
            if self.model_ref.startswith(("kotoba-tech/",)) and not os.environ.get("HF_ENDPOINT"):
                os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
            print(f"[asr] whisper 兜底模型本地缺失，开始下载 {self.model_ref}"
                  f"（约 800MB，将阻塞本次识别请求）", flush=True)
            self._model = WhisperModel(self.model_ref, device=self.device,
                                       compute_type=self.compute_type)

    def transcribe(self, pcm_f32: np.ndarray, sr: int = 16000, lang: str = "ja") -> list:
        """pcm_f32: [-1,1] float32 单声道。返回 [{"start_ms","end_ms","text"}]（可能为空）。

        vad_filter 过滤非语音段（faster-whisper 标准行为）。"""
        self._ensure()
        with self._lock:
            segments, info = self._model.transcribe(
                pcm_f32, language=lang if lang != "zh" else "zh",
                beam_size=5, vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 300},
            )
            out = []
            for s in segments:
                txt = (s.text or "").strip()
                if txt:
                    out.append({"start_ms": int(s.start * 1000),
                                "end_ms": int(s.end * 1000),
                                "text": txt})
            return out
