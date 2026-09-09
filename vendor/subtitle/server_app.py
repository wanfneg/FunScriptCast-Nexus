# -*- coding: utf-8 -*-
"""AI 字幕服务端（FastAPI）

接口：
  GET  /health                    服务与模型状态
  POST /transcribe?lang=ja&video_start_ms=45000&keep_from_ms=46000&translate=true
       body: 裸 PCM（16kHz / 单声道 / s16le 小端）
       → {"language": "...", "segments": [{"start_ms","end_ms","text","translation"}],
          "asr_ms": 3120, "mt_ms": 430, "skipped": false}

设计要点：
  - 无状态：每次请求自带语言与时间基，服务端不保存会话（便于客户端重连/重试）
  - video_start_ms：本块音频第一帧在视频里的时间 → 返回的 start_ms/end_ms 是视频绝对时间
  - keep_from_ms：重叠区去重（客户端传 chunk 起点 + overlap/2，服务端丢弃中点早于它的句子）
  - ASR 与翻译串行执行；ASR 占 GPU，翻译默认走 Ollama/云端（8GB 卡上两者不能同时驻留）
"""

import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
from fastapi import FastAPI, Request
from starlette.concurrency import run_in_threadpool

from asr_engine import SR, AsrEngine
from glossary import Glossary
from translate_engine import Translator

BASE = Path(__file__).resolve().parent
CFG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))


def _abs_path(p: str) -> str:
    """相对路径按本文件所在目录解析，避免 cwd 不同导致找不到模型。"""
    if not p:
        return p
    q = Path(p)
    return str(q if q.is_absolute() else (BASE / q).resolve())


# config.json 是模型路径的唯一真相（可切 0.6B / 1.7B）。
# 环境变量只在 config 没写时兜底，避免「改了 config 却不生效」。
for _k in ("model", "aligner"):
    if CFG.get("asr", {}).get(_k):
        CFG["asr"][_k] = _abs_path(CFG["asr"][_k])
if not CFG.get("asr", {}).get("model") and os.environ.get("ASR_MODEL"):
    CFG["asr"]["model"] = os.environ["ASR_MODEL"]
if os.environ.get("TRANSLATE_BACKEND"):
    CFG["translate"]["backend"] = os.environ["TRANSLATE_BACKEND"]

state = {"asr": None, "translator": None, "glossary": None}


@asynccontextmanager
async def lifespan(_app):
    state["glossary"] = Glossary(CFG.get("glossary", {}), base_dir=BASE)
    state["asr"] = AsrEngine(CFG.get("asr", {}), state["glossary"])
    state["translator"] = Translator(CFG.get("translate", {}), state["glossary"])
    print(f"[server] ASR 就绪（加载 {state['asr'].load_s:.1f}s），"
          f"翻译后端={state['translator'].backend}，"
          f"术语表 ja={state['glossary'].size('ja')} en={state['glossary'].size('en')}")
    yield


app = FastAPI(title="VRFunScriptCast AI Subtitle Server", version="0.1", lifespan=lifespan)


def _gpu_used_gb() -> float:
    try:
        import torch
        return round(torch.cuda.max_memory_allocated() / 1e9, 2)
    except Exception:
        return 0.0


@app.get("/health")
def health():
    asr = state["asr"]
    return {
        "ok": asr is not None,
        "asr_model": CFG["asr"]["model"],
        "device": CFG["asr"].get("device"),
        "vad": asr.vad is not None if asr else False,
        "aligner": asr.use_aligner if asr else False,
        "gpu_used_gb": _gpu_used_gb(),
        "translate_backend": state["translator"].backend if state["translator"] else None,
        "glossary": {k: state["glossary"].size(k) for k in state["glossary"].langs()} if state["glossary"] else {},
    }


@app.get("/glossary")
def glossary_get(lang: str = "ja"):
    """查看当前术语表（PC 端管理程序/调试用）。"""
    g = state["glossary"]
    return {"lang": lang, "count": g.size(lang), "terms": g.raw(lang)}


@app.post("/glossary/reload")
def glossary_reload():
    """强制重新读取术语表文件（正常情况下按 mtime 自动热加载，此接口用于手动触发）。"""
    g = state["glossary"]
    changed = g.reload(force=True)
    return {"changed": changed, "sizes": {k: g.size(k) for k in g.langs()}}


@app.post("/transcribe")
async def transcribe(request: Request, lang: str = "ja", video_start_ms: int = 0,
                     keep_from_ms: int = 0, translate: bool = True):
    body = await request.body()
    if len(body) < 3200:  # <0.1s 音频，直接返回
        return {"language": None, "segments": [], "asr_ms": 0.0, "mt_ms": 0.0,
                "skipped": True, "reason": "音频过短"}
    if len(body) % 2:
        body = body[:-1]
    pcm = np.frombuffer(body, dtype=np.int16).astype(np.float32) / 32768.0

    t0 = time.perf_counter()
    result = await run_in_threadpool(
        state["asr"].transcribe, pcm, lang, video_start_ms, keep_from_ms,
        CFG.get("vad", {}), CFG.get("segment", {}),
    )
    mt_ms = 0.0
    if translate and result["segments"]:
        t1 = time.perf_counter()
        await run_in_threadpool(state["translator"].translate_segments, result["segments"], lang)
        mt_ms = round((time.perf_counter() - t1) * 1000, 1)
    result["mt_ms"] = mt_ms
    result["total_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    preview = " | ".join((s.get("translation") or s["text"])[:18] for s in result["segments"][:3])
    print(f"[transcribe] lang={lang} start={video_start_ms}ms bytes={len(body)} "
          f"skip={result['skipped']} segs={len(result['segments'])} "
          f"asr={result['asr_ms']}ms mt={mt_ms}ms total={result['total_ms']}ms"
          + (f"\n             {preview}" if preview else ""), flush=True)
    return result
