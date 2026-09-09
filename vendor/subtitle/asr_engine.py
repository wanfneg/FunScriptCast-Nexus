# -*- coding: utf-8 -*-
"""ASR 引擎：Qwen3-ASR + ForcedAligner + Silero VAD + 停顿切句

对外只暴露 transcribe(pcm, ...) → 带绝对时间戳的句子列表。
时间基：返回的 start_ms/end_ms 已加上调用方给的 video_start_ms。
"""

import time

import numpy as np
import torch

from qwen_asr import Qwen3ASRModel

SR = 16000
SENT_END = "。！？!?…；;"

LANG_MAP = {"zh": "Chinese", "en": "English", "ja": "Japanese", "ko": "Korean", "yue": "Cantonese"}


def join_tokens(parts):
    """拼接 ASR token：中日文直接相连，英文/数字之间补空格。"""
    out = ""
    for t in parts:
        if not t:
            continue
        if out and out[-1].isascii() and out[-1].isalnum() and t[0].isascii() and t[0].isalnum():
            out += " "
        out += t
    return out


def _has_repetition_loop(text: str, max_run: int = 5) -> bool:
    """检测同一字符连续重复过多（小模型的死循环退化）。"""
    run = 1
    for i in range(1, len(text)):
        if text[i] == text[i - 1]:
            run += 1
            if run > max_run:
                return True
        else:
            run = 1
    return False


class AsrEngine:
    def __init__(self, cfg: dict, glossary):
        self.cfg = cfg
        self.glossary = glossary
        self.dtype = {"bf16": torch.bfloat16, "fp16": torch.float16, "fp32": torch.float32}[
            cfg.get("dtype", "bf16")]
        device = cfg.get("device", "cuda:0")
        kwargs = dict(dtype=self.dtype, device_map=device)
        aligner = cfg.get("aligner")
        self.use_aligner = bool(aligner)
        if self.use_aligner:
            kwargs["forced_aligner"] = aligner
            kwargs["forced_aligner_kwargs"] = dict(dtype=self.dtype, device_map=device)

        t0 = time.perf_counter()
        self.model = Qwen3ASRModel.from_pretrained(cfg["model"], **kwargs)
        self.load_s = time.perf_counter() - t0

        self.vad = None
        if cfg.get("vad_enabled", True):
            try:
                from silero_vad import load_silero_vad
                self.vad = load_silero_vad()
            except Exception as e:  # VAD 只是优化，不可用时退化为全量识别
                print("[asr] VAD 不可用，已跳过静音检测:", e)

    # ---------------------------------------------------------------- VAD
    def has_speech(self, pcm: np.ndarray, threshold=0.5, min_speech_ms=250) -> bool:
        if self.vad is None:
            return True
        from silero_vad import get_speech_timestamps
        ts = get_speech_timestamps(
            torch.from_numpy(pcm), self.vad, sampling_rate=SR,
            threshold=threshold, min_speech_duration_ms=min_speech_ms,
            return_seconds=True,
        )
        return len(ts) > 0

    # ------------------------------------------------------------ 分句
    def _split_segments(self, stamps, base_ms, seg_cfg):
        """按 停顿 / 标点 / 长度上限 切句，返回绝对时间的句子列表。"""
        max_sec = seg_cfg.get("max_sec", 8.0)
        max_chars = seg_cfg.get("max_chars", 50)
        pause_sec = seg_cfg.get("pause_sec", 0.6)

        out, buf, buf_start, prev_end = [], [], None, None

        def flush(end_time):
            nonlocal buf, buf_start
            text = join_tokens(buf).strip()
            if text:
                out.append({
                    "start_ms": int(round(base_ms + buf_start * 1000)),
                    "end_ms": int(round(base_ms + end_time * 1000)),
                    "text": text,
                })
            buf, buf_start = [], None

        for st in stamps:
            if buf_start is None:
                buf_start = st.start_time
            # 停顿切句：与上一个词之间有 >= pause_sec 的静默
            if prev_end is not None and (st.start_time - prev_end) >= pause_sec and buf:
                flush(prev_end)
                buf_start = st.start_time
            buf.append(st.text)
            joined = join_tokens(buf).strip()
            if st.text and st.text[-1] in SENT_END:
                flush(st.end_time)
            elif (st.end_time - buf_start) >= max_sec or len(joined) >= max_chars:
                flush(st.end_time)
            prev_end = st.end_time
        if buf:
            flush(prev_end if prev_end is not None else buf_start)
        return self._merge_short(out, seg_cfg)

    def _merge_short(self, segs, seg_cfg):
        """把过短的碎片并进相邻句（停顿切句会切出"た""そして"这类碎片）。"""
        min_sec = seg_cfg.get("min_sec", 1.2)
        min_chars = seg_cfg.get("min_chars", 4)
        max_sec = seg_cfg.get("max_sec", 8.0) * 1.5
        merged = []
        for s in segs:
            if merged:
                prev = merged[-1]
                dur = (s["end_ms"] - s["start_ms"]) / 1000
                gap = (s["start_ms"] - prev["end_ms"]) / 1000
                if (dur < min_sec or len(s["text"]) < min_chars) and gap < 0.8 and \
                        (s["end_ms"] - prev["start_ms"]) / 1000 <= max_sec:
                    prev["end_ms"] = s["end_ms"]
                    prev["text"] = join_tokens([prev["text"], s["text"]])
                    continue
            merged.append(dict(s))
        # 首句过短则并进下一句
        if len(merged) >= 2:
            first = merged[0]
            if (first["end_ms"] - first["start_ms"]) / 1000 < min_sec or len(first["text"]) < min_chars:
                nxt = merged[1]
                if (nxt["end_ms"] - first["start_ms"]) / 1000 <= max_sec:
                    nxt["start_ms"] = first["start_ms"]
                    nxt["text"] = join_tokens([first["text"], nxt["text"]])
                    merged.pop(0)
        return merged

    # -------------------------------------------------------- 主入口
    def transcribe(self, pcm: np.ndarray, lang_key: str, video_start_ms: int = 0,
                   keep_from_ms: int = 0, vad_cfg=None, seg_cfg=None) -> dict:
        """pcm: float32 [-1,1] @16k mono；返回 {"language","segments","asr_ms","skipped"}"""
        vad_cfg = vad_cfg or {}
        seg_cfg = seg_cfg or {}
        t0 = time.perf_counter()

        if not self.has_speech(pcm, vad_cfg.get("threshold", 0.5), vad_cfg.get("min_speech_ms", 250)):
            return {"language": None, "segments": [], "asr_ms": 0.0, "skipped": True}

        context = ""
        if self.cfg.get("use_glossary_context", True):
            context = self.glossary.asr_context(lang_key)

        r = self.model.transcribe(
            audio=(pcm, SR),
            context=context,
            language=LANG_MAP.get(lang_key, lang_key),
            return_time_stamps=self.use_aligner,
        )[0]

        if self.use_aligner and getattr(r, "time_stamps", None):
            segs = self._split_segments(r.time_stamps, video_start_ms, seg_cfg)
        else:
            text = (r.text or "").strip()
            segs = ([{"start_ms": video_start_ms, "end_ms": video_start_ms + int(len(pcm) / SR * 1000),
                      "text": text}] if text else [])

        # 重叠区去重：只保留起点在保留区之后的句子（客户端传 video_start_ms + overlap）
        if keep_from_ms:
            segs = [s for s in segs if s["start_ms"] >= keep_from_ms]

        # 幻觉过滤：非语音段（音乐/静音）时模型会把热词表当台词吐出来。
        # 先整块判定（合并文本覆盖率过高 → 整块丢弃），再单句判定。
        joined = "".join(s["text"] for s in segs)
        if self._is_glossary_echo(joined, lang_key):
            print(f"[asr] 疑似热词表幻觉，整块丢弃 {len(segs)} 段", flush=True)
            segs = []
        else:
            segs = [s for s in segs if not self._is_glossary_echo(s["text"], lang_key)]
        # 重复退化过滤：小模型偶发 "才才才才才才…" 这类死循环
        before = len(segs)
        segs = [s for s in segs if not _has_repetition_loop(s["text"])]
        if len(segs) != before:
            print(f"[asr] 重复退化，丢弃 {before - len(segs)} 段", flush=True)

        return {"language": r.language, "segments": segs,
                "asr_ms": round((time.perf_counter() - t0) * 1000, 1), "skipped": False}

    def _is_glossary_echo(self, text: str, lang_key: str) -> bool:
        """判定该文本是否只是把热词表复读出来（术语覆盖率过高）。"""
        keys = self.glossary.keys(lang_key)
        if not keys or len(text) < 4:
            return False
        hits = [k for k in keys if k in text]
        if len(hits) < 3:
            return False
        covered = sum(len(k) for k in hits)
        return covered / max(1, len(text)) > 0.6
