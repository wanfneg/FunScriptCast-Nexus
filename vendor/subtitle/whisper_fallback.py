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
            # 国内网络：未显式配置镜像时默认走 hf-mirror（模型 ~800MB）
            if self.model_ref.startswith(("kotoba-tech/",)) and not os.environ.get("HF_ENDPOINT"):
                os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
            from faster_whisper import WhisperModel
            self._model = WhisperModel(self.model_ref, device=self.device,
                                       compute_type=self.compute_type)

    def transcribe(self, pcm_f32: np.ndarray, sr: int = 16000, lang: str = "ja") -> list:
        """pcm_f32: [-1,1] float32 单声道。返回 [{"start_ms","end_ms","text"}]（可能为空）。

        幻觉防护：whisper 系对喘息/非语音有名的幻觉问题——vad_filter 过滤 +
        no_speech_prob > 0.6 时整块丢弃（调用方主引擎也是零输出才走到这，
        误丢的代价只是维持现状）。"""
        self._ensure()
        with self._lock:
            segments, info = self._model.transcribe(
                pcm_f32, language=lang if lang != "zh" else "zh",
                beam_size=5, vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 300},
            )
            if getattr(info, "no_speech_prob", 0) > 0.6:
                return []
            out = []
            for s in segments:
                txt = (s.text or "").strip()
                if txt:
                    out.append({"start_ms": int(s.start * 1000),
                                "end_ms": int(s.end * 1000),
                                "text": txt})
            return out
