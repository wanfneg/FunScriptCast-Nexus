# -*- coding: utf-8 -*-
"""ASR 引擎：Qwen3-ASR + ForcedAligner + Silero VAD + 停顿切句

对外只暴露 transcribe(pcm, ...) → 带绝对时间戳的句子列表。
时间基：返回的 start_ms/end_ms 已加上调用方给的 video_start_ms。
"""

import threading
import time

import numpy as np

from glossary import context_with_keys
from text_filters import has_repetition_loop, is_glossary_echo, join_tokens, keep_segment

# torch / qwen_asr 只被 **PyTorch 回退引擎** 用到（audiocpp 主路径完全不需要，
# 两者合计约 5 GB）。改为懒加载：模块导入不再要求安装它们——这样字幕服务可以
# 跑在自包含安装包的轻量运行时上（fastapi+uvicorn+numpy 约 40 MB）。
# 用到 torch/qwen_asr 的入口在 PyTorch 引擎构造处，缺依赖会在那里给出可读报错。
try:
    import torch
    from qwen_asr import Qwen3ASRModel
    _HEAVY_DEPS_OK = True
except ImportError:
    torch = None
    Qwen3ASRModel = None
    _HEAVY_DEPS_OK = False

SR = 16000
SENT_END = "。！？!?…；;"

LANG_MAP = {"zh": "Chinese", "en": "English", "ja": "Japanese", "ko": "Korean", "yue": "Cantonese"}

# join_tokens 已下沉到 text_filters（audiocpp 后端合并短句也要用同一份实现，
# 那边原来是字符串直接相加，英文会粘成一坨）。这里继续从本模块导出该名字，
# 保持 `from asr_engine import join_tokens` 的既有用法可用。

# 重复退化判据统一在 text_filters.has_repetition_loop（与 audiocpp 后端共用，
# 且对「えーーーっと」这类合法拖长音放宽），此处不再本地实现。
# 跨块去重判据同理：text_filters.keep_segment（此前只有 audiocpp 侧修了容差）。


class AsrEngine:
    def __init__(self, cfg: dict, glossary):
        if not _HEAVY_DEPS_OK:
            raise RuntimeError(
                "PyTorch 引擎依赖未安装（torch / qwen_asr）。轻量运行时只支持 audiocpp 后端；"
                "要使用 PyTorch 引擎请安装完整依赖（约 5 GB）。")
        self.cfg = cfg
        self.glossary = glossary
        # /transcribe 走 FastAPI 线程池，可能并发进来。共享模型上每次请求都要
        # 改 max_new_tokens、silero VAD 带内部状态（LSTM）——并发调用会互相
        # 覆盖预算/污染状态，GPU 上并发 forward 还会显存翻倍。串行化整个转写。
        self._lock = threading.Lock()
        self.dtype = {"bf16": torch.bfloat16, "fp16": torch.float16, "fp32": torch.float32}[
            cfg.get("dtype", "bf16")]
        device = cfg.get("device", "cuda:0")
        # 生成上限：非语音/音乐段模型会退化生成直到上限（0.6B 约 20 tok/s，
        # 默认 512 就是 25 秒/块）。按音频时长估算，实测 25s 音频从 25.4s 降到 3.2s，
        # 文本不变。可用 config.json 的 asr.max_new_tokens_* 覆盖。
        self.tok_per_sec = float(cfg.get("max_new_tokens_per_sec", 4.0))
        self.tok_min = int(cfg.get("max_new_tokens_min", 48))
        self.tok_max = int(cfg.get("max_new_tokens_max", 256))
        kwargs = dict(dtype=self.dtype, device_map=device,
                      max_new_tokens=int(cfg.get("max_new_tokens", self.tok_max)))
        # 4-bit 量化（bitsandbytes）：1.7B 从 5.53GB 降到 3.15GB，速度还更快。
        # 注意 compute_dtype 必须 fp16——音频塔 conv 权重会被转成 fp16，
        # 用 bf16 会在 F.conv2d 处报 dtype 不匹配。
        q = str(cfg.get("quantization", "") or "").lower()
        if q in ("4bit", "int4", "nf4"):
            try:
                from transformers import BitsAndBytesConfig
                kwargs["quantization_config"] = BitsAndBytesConfig(
                    load_in_4bit=True,
                    bnb_4bit_quant_type=str(cfg.get("bnb_quant_type", "nf4")),
                    bnb_4bit_compute_dtype=torch.float16,
                    bnb_4bit_use_double_quant=bool(cfg.get("bnb_double_quant", True)),
                )
                kwargs["device_map"] = {"": 0}   # 量化加载需要显式设备映射
                kwargs.pop("dtype", None)        # 量化时不能再传 dtype
            except Exception as e:
                print(f"[asr] 4-bit 量化不可用，回退 bf16: {e}")
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

    def _budget_tokens(self, n_samples: int) -> int:
        """按音频时长估算生成上限（秒数 × 每秒 token 数，夹在 min/max 之间）。"""
        secs = n_samples / float(SR)
        return int(max(self.tok_min, min(self.tok_max, secs * self.tok_per_sec)))

    # ---------------------------------------------------------------- VAD
    def speech_spans(self, pcm: np.ndarray, threshold=0.5, min_speech_ms=250) -> list:
        """返回语音区间 [(start_sec, end_sec)]（相对本块音频）。

        只送语音段给 ASR 能大幅省时间：实测 25s 块里只有 1.1s 语音时，
        整块送要 5.3s，裁剪后 1s 以内。
        """
        if self.vad is None:
            return []
        from silero_vad import get_speech_timestamps
        ts = get_speech_timestamps(
            torch.from_numpy(pcm), self.vad, sampling_rate=SR,
            threshold=threshold, min_speech_duration_ms=min_speech_ms,
            return_seconds=True,
        )
        return [(float(s["start"]), float(s["end"])) for s in ts]

    @staticmethod
    def _crop_to_spans(pcm: np.ndarray, spans: list, pad_sec: float = 0.25):
        """把语音区间拼成一段音频，并返回「裁剪后时间 → 原块时间」的映射。

        返回 (cropped_pcm, map_fn)，map_fn(t) 把裁剪音频里的秒数换算回原块秒数。
        """
        if not spans:
            return pcm, (lambda t: t)
        pad = int(pad_sec * SR)
        pieces, bounds = [], []
        for s, e in spans:
            a = max(0, int(s * SR) - pad)
            b = min(len(pcm), int(e * SR) + pad)
            if b <= a:
                continue
            pieces.append(pcm[a:b])
            bounds.append((a, b))
        if not pieces:
            return pcm, (lambda t: t)
        cropped = np.concatenate(pieces)
        # 裁剪时间 → 原块时间：逐段平移
        offsets = []
        acc = 0
        for a, b in bounds:
            offsets.append((acc, a, b - a))
            acc += b - a

        def map_fn(t_sec: float) -> float:
            idx = int(t_sec * SR)
            for start_i, orig_a, length in offsets:
                if start_i <= idx < start_i + length:
                    return (orig_a + (idx - start_i)) / float(SR)
            return t_sec  # 越界时退化为原值

        return cropped, map_fn

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
                   keep_from_ms: int = 0, vad_cfg=None, seg_cfg=None,
                   extra_context: str = "") -> dict:
        """pcm: float32 [-1,1] @16k mono；返回 {"language","segments","asr_ms","skipped"}

        整个转写串行（self._lock）：模型与 VAD 都不允许并发进入。"""
        with self._lock:
            return self._transcribe_locked(pcm, lang_key, video_start_ms,
                                           keep_from_ms, vad_cfg, seg_cfg, extra_context)

    def _transcribe_locked(self, pcm: np.ndarray, lang_key: str, video_start_ms: int = 0,
                           keep_from_ms: int = 0, vad_cfg=None, seg_cfg=None,
                           extra_context: str = "") -> dict:
        vad_cfg = vad_cfg or {}
        seg_cfg = seg_cfg or {}
        t0 = time.perf_counter()

        # VAD：先切出语音区间，只把语音段送 ASR（含少量前后 padding 防切头）
        spans = self.speech_spans(pcm, vad_cfg.get("threshold", 0.5),
                                  vad_cfg.get("min_speech_ms", 250))
        if self.vad is not None and not spans:
            return {"language": None, "segments": [], "asr_ms": 0.0, "skipped": True}
        if spans:
            pcm_asr, tmap = self._crop_to_spans(
                pcm, spans, float(vad_cfg.get("pad_sec", 0.25)))
        else:
            pcm_asr, tmap = pcm, (lambda t: t)   # VAD 不可用时保持原行为

        context = ""
        context_keys: list = []
        if self.cfg.get("use_glossary_context", True):
            # 同时取回"真正进提示词的键"：复读判据只认这批键，不能拿整张术语表
            # （本路径与既有行为一致，不补领域词：asr_context 的 max_chars 默认 0）
            context, context_keys = context_with_keys(self.glossary, lang_key)
        if extra_context:
            # 上一句转写结果作为热词补充（与 audiocpp 后端同策略，治跨块人名听错）
            context = (context + " " + extra_context).strip()

        # 按送进去的音频时长收紧生成上限：退化生成不会再跑满全局上限
        self.model.max_new_tokens = self._budget_tokens(len(pcm_asr))
        r = self.model.transcribe(
            audio=(pcm_asr, SR),
            context=context,
            language=LANG_MAP.get(lang_key, lang_key),
            return_time_stamps=self.use_aligner,
        )[0]

        if self.use_aligner and getattr(r, "time_stamps", None):
            # 时间戳是相对裁剪音频的，先映射回原块时间再按 video_start_ms 偏移。
            # ForcedAlignItem 是 frozen dataclass，必须 replace 重建而不是就地赋值。
            import dataclasses
            mapped = [dataclasses.replace(st, start_time=tmap(st.start_time),
                                          end_time=tmap(st.end_time))
                      for st in r.time_stamps]
            segs = self._split_segments(mapped, video_start_ms, seg_cfg)
        else:
            text = (r.text or "").strip()
            if text:
                # 无 aligner 时没有词级时间戳，但 VAD 的语音区间是原块时间，
                # 直接可用：start 用首个语音段起点、end 用末个语音段终点。
                # 旧实现 start 用块起点、end 用裁剪后时长——字幕会同时
                # 提前出现、提前消失（裁掉的静音没算回去）。
                if spans:
                    start_sec, end_sec = spans[0][0], spans[-1][1]
                else:
                    start_sec, end_sec = 0.0, len(pcm_asr) / SR
                segs = [{"start_ms": video_start_ms + int(round(start_sec * 1000)),
                         "end_ms": video_start_ms + int(round(end_sec * 1000)),
                         "text": text}]
            else:
                segs = []

        # 重叠区去重：只丢"整句基本都在重叠区"的句子（判据与 audiocpp 后端共用，
        # 见 text_filters.keep_segment）。旧判据 start < keep_from 即丢，跨块长句
        # 在两个块里都扔（两边 start 都早于各自的 keep_from）。
        if keep_from_ms:
            segs = [s for s in segs
                    if keep_segment(s["start_ms"], s["end_ms"], keep_from_ms)]

        # 幻觉过滤：非语音段（音乐/静音）时模型会把热词表当台词吐出来。
        # 先整块判定（合并文本覆盖率过高 → 整块丢弃），再单句判定。
        # **没开热词/热词为空时不做这个判定**：判据与 context 无关，关掉热词
        # 也照丢句子（正常领域句被整块丢光），且这时提示词里根本没有可复读的东西。
        if context:
            joined = "".join(s["text"] for s in segs)
            if self._is_glossary_echo(joined, context_keys):
                print(f"[asr] 疑似热词表幻觉，整块丢弃 {len(segs)} 段", flush=True)
                segs = []
            else:
                segs = [s for s in segs
                        if not self._is_glossary_echo(s["text"], context_keys)]
        # 重复退化过滤：小模型偶发 "才才才才才才…" 这类死循环（判据与
        # audiocpp 后端共用，见 text_filters.has_repetition_loop）
        before = len(segs)
        segs = [s for s in segs if not has_repetition_loop(s["text"])]
        if len(segs) != before:
            print(f"[asr] 重复退化，丢弃 {before - len(segs)} 段", flush=True)

        return {"language": r.language, "segments": segs,
                "asr_ms": round((time.perf_counter() - t0) * 1000, 1), "skipped": False}

    def _is_glossary_echo(self, text: str, keys: list) -> bool:
        """判定该文本是否只是把热词表复读出来（判据见 text_filters.is_glossary_echo，
        与 audiocpp 后端共用同一实现，避免两边漂移）。

        `keys` 必须是调用方从 context_with_keys 拿到的"真正进了提示词的键"，
        不再是 glossary.keys()（整张术语表当判据会把正常台词误杀）。
        """
        return is_glossary_echo(text, keys)
