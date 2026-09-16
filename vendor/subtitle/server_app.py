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
  - keep_from_ms：重叠区去重（客户端传 chunk 起点 + overlap/2，服务端丢弃
    **起点**早于它的句子——注意是起点判据不是中点，跨重叠边界、起点略早
    于 keep_from 的整句会被丢弃）
  - ASR 与翻译串行执行；ASR 占 GPU，翻译默认走 Ollama/云端（8GB 卡上两者不能同时驻留）
"""

import asyncio
import hashlib
import json
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
from fastapi import FastAPI, Request
from starlette.concurrency import run_in_threadpool

from asr_engine import AsrEngine
from glossary import Glossary
from whisper_fallback import WhisperFallback
from translate_engine import Translator

BASE = Path(__file__).resolve().parent
CFG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))


def _code_signature() -> str:
    """管线源码签名（vendor/subtitle 顶层 *.py 内容哈希）。

    排障利器：/health 与启动日志都会带上它——"改了代码但表现没变"的陈旧
    实例问题（2026-09-16 曾浪费半天），对比两份 /health 的 code_sig 即可
    一眼判定是否跑的是同一份代码。
    """
    h = hashlib.sha256()
    for f in sorted(BASE.glob("*.py")):
        if f.name == Path(__file__).name:
            h.update(f.read_bytes())
        else:
            h.update(f.name.encode("utf-8"))
            h.update(f.read_bytes())
    return h.hexdigest()[:12]


CODE_SIG = _code_signature()

# config 缺段防御：宁可补空段也不能 import 即崩（裸 KeyError 报错信息极差）
for _sec in ("asr", "translate", "server", "vad", "segment", "glossary"):
    if not isinstance(CFG.get(_sec), dict):
        CFG[_sec] = {}

# /transcribe 请求体上限：裸 PCM 25s 块约 800KB；宿主误传/恶意传超大块时，
# body + int16 视图 + float32 拷贝峰值内存约 3× 字节数，必须设上限挡住
MAX_BODY_BYTES = 100 * 1024 * 1024


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

# 进程启动时刻：/health 回给宿主，用来识别"这是不是我刚拉起来的那个进程"
_BOOT_TS = time.time()

# 最后一次真正干活的时刻（只有 /transcribe 算，/health 不算）。
# 空闲回收靠它判断——如果把 /health 也算作活动，只要 PC 界面开着轮询，
# 服务就永远回收不掉，等于又变回常驻。
# ⚠️ 打点在请求**结束**（finally）而不是开始：一次超长请求（可能超过
# idle_release_min）若只打开始点，reaper 会在它处理到一半时把它杀掉。
_LAST_REQ_TS = _BOOT_TS
_INFLIGHT = 0                    # 正在处理中的 /transcribe 数
_INFLIGHT_LOCK = threading.Lock()

# 上一块的字幕上下文：① 上一句原文进 ASR 热词（治人名/专名跨块听错，借鉴
# realtime-subtitle 的 context carryover）② 上一句原文+译文进翻译提示词
# （治代词/场景断裂）。按语言键区分；仅内存态，服务重启即清零。
_LAST_CTX = {"lang": "", "src": "", "zh": ""}
_CTX_LOCK = threading.Lock()


def _idle_release_min() -> float:
    """空闲多久自动释放模型并退出（分钟）。0 或负数 = 不自动释放。

    这是"按需加载"的另一半：只在用的时候加载，不用了得还回去。
    只加载不释放的话，看过一次片之后 audiocpp 那约 3 GB 就一直挂着，
    和"常驻"没有区别，只是发生得晚一点。
    """
    try:
        return float((CFG.get("server") or {}).get("idle_release_min", 15) or 0)
    except Exception:
        return 15.0


async def _idle_reaper() -> None:
    """空闲到点就把自己关掉，并把 audiocpp 后端一起带走。

    必须显式 stop_server() 再退出，不能直接 os._exit：后者跳过 lifespan 收尾，
    audiocpp 会变成孤儿继续占着内存——这正是之前 sub_stop 踩过的坑。

    双重栅栏防误杀在飞请求：idle 时间按"最后一次请求**结束**"算，且 _INFLIGHT > 0 时
    一律跳过。决定退出后的最终检查与 os._exit **同锁**完成——此前检查与退出之间存在
    窗口，请求刚好进来就会撞上"模型正在被释放"的半死状态（ASR 500）。
    """
    global _INFLIGHT
    while True:
        await asyncio.sleep(30)
        mins = _idle_release_min()
        if mins <= 0:
            continue
        with _INFLIGHT_LOCK:
            inflight = _INFLIGHT
        if inflight > 0:
            continue
        idle = time.time() - _LAST_REQ_TS
        if idle < mins * 60:
            continue
        with _INFLIGHT_LOCK:      # 复核 + 退出原子化：在飞请求的打点会被这把锁挡住
            if _INFLIGHT > 0:
                continue
            print(f"[server] 空闲 {idle / 60:.1f} 分钟 ≥ {mins:.0f} 分钟，"
                  f"释放模型并退出（下次要用会由头显重新拉起）", flush=True)
            try:
                if hasattr(state["asr"], "stop_server"):
                    state["asr"].stop_server()
            except Exception as e:
                print(f"[server] 释放 audiocpp 失败：{type(e).__name__}: {e}", flush=True)
            os._exit(0)


def _make_asr(cfg: dict, glossary):
    """按 asr.backend 选引擎。

    - "pytorch"（默认）：原来的 Qwen3-ASR + torch 实现
    - "audiocpp"：audiocpp 常驻服务（CPU 后端显存 0 占用；必须先 VAD 裁剪语音段，
      否则长音频会退化出成百上千连重复——见 audiocpp_backend 模块注释）
    """
    kind = str(cfg.get("backend", "pytorch") or "pytorch").lower()
    if kind in ("audiocpp", "cpp", "ggml"):
        try:
            from audiocpp_backend import AudioCppBackend

            be = AudioCppBackend(
                cfg.get("audiocpp", {}) or {},
                glossary=glossary,
                # 热词开关在 asr 段（与 PyTorch 引擎同源），不是 audiocpp 段的
                use_context=bool(cfg.get("use_glossary_context", True)),
                context_max_chars=int(cfg.get("context_max_chars", 0)),
            )
            be.ensure_server()
            ctx = be._build_context("ja")
            print(f"[server] ASR 后端 = audiocpp（{be.backend}, {be.threads} 线程, "
                  f"端口 {be.port}，热词 {len(ctx)} 字符）")
            return be
        except Exception as e:
            print(f"[server] audiocpp 不可用（{type(e).__name__}: {e}），回退 PyTorch")
    engine = AsrEngine(cfg, glossary)
    print(f"[server] ASR 后端 = pytorch（加载 {engine.load_s:.1f}s）")
    return engine


@asynccontextmanager
async def lifespan(_app):
    _gl_cfg = CFG.get("glossary", {}) or {}
    state["glossary"] = Glossary(_gl_cfg, base_dir=BASE, extra=_gl_cfg.get("extra"))
    state["asr"] = _make_asr(CFG.get("asr", {}), state["glossary"])
    state["translator"] = Translator(CFG.get("translate", {}), state["glossary"])
    print(f"[server] 管线代码签名 code_sig={CODE_SIG}（陈旧实例排障用）", flush=True)
    print(f"[server] 翻译后端={state['translator'].backend}，"
          f"术语表 ja={state['glossary'].size('ja')} en={state['glossary'].size('en')}")
    _reaper = asyncio.create_task(_idle_reaper())
    print(f"[server] 空闲回收：{_idle_release_min():.0f} 分钟无识别请求后释放模型"
          if _idle_release_min() > 0 else "[server] 空闲回收：已关闭（idle_release_min=0）")
    yield
    _reaper.cancel()
    try:
        if hasattr(state["asr"], "stop_server"):
            state["asr"].stop_server()
    except Exception:
        pass


app = FastAPI(title="VRFunScriptCast AI Subtitle Server", version="0.1", lifespan=lifespan)


def _gpu_used_gb() -> float:
    """当前显存占用（GB）。注意：audiocpp 路径下本进程不加载 torch 模型，
    恒为 0——监控 audiocpp 的内存要看系统内存而不是这个值。"""
    try:
        import torch
        # memory_allocated() 是当前占用；此前用 max_memory_allocated()（启动以来
        # 峰值）只升不降，做监控会得到假数据
        return round(torch.cuda.memory_allocated() / 1e9, 2)
    except Exception:
        return 0.0


@app.get("/health")
def health():
    asr = state["asr"]
    return {
        "ok": asr is not None,
        "code_sig": CODE_SIG,
        # 进程身份：宿主靠它判断 8756 上跑的到底是不是自己拉起来的那个。
        # 只看"端口开着"会被上次异常退出残留的进程骗过去，然后报一个假的 ready，
        # 头显就会跳过等待、把音频发给一个陈旧进程（实测踩过）。
        "pid": os.getpid(),
        "started_at": _BOOT_TS,
        "asr_model": (CFG.get("asr", {}) or {}).get("model"),
        "device": (CFG.get("asr", {}) or {}).get("device"),
        "vad": asr.vad is not None if asr else False,
        "aligner": asr.use_aligner if asr else False,
        "gpu_used_gb": _gpu_used_gb(),
        "translate_backend": state["translator"].backend if state["translator"] else None,
        "translate": state["translator"].describe() if state["translator"] else None,
        "glossary": {k: state["glossary"].size(k) for k in state["glossary"].langs()} if state["glossary"] else {},
    }


@app.get("/translate/stats")
def translate_stats():
    """翻译层累计统计（批量/缓存命中/纠错轮数/兜底）。用于 PC 端诊断页。"""
    t = state["translator"]
    if t is None:
        return {"ready": False}
    return {"ready": True, "describe": t.describe(), "cache_dir": t.cache_dir,
            "stats": dict(t.stats)}


@app.get("/translate/selftest")
def translate_selftest(text: str = "こんにちは、いい天気ですね。"):
    """用**当前配置**真跑一句，把译文与上游原始报错一起回给调用方（UI 的「测试」按钮）。

    为什么要有这个接口：云端后端（DashScope 等）出错时，翻译层的兜底会把失败吞成
    "空译文"，用户只看到没字幕，无从判断是 key 错、余额不足还是模型名错。这里分成两段报：

      · raw      —— 直接调 _chat（不经过批量/兜底），失败时把上游响应体带出来
      · pipeline —— 走完整 translate_segments（含术语表/纠错/兜底），反映真实产出
    """
    t = state["translator"]
    if t is None:
        return {"ok": False, "error": "翻译器未初始化"}

    out = {"backend": t.backend, "describe": t.describe(), "text": text,
           "raw": None, "raw_error": "", "pipeline": None, "pipeline_error": ""}

    # ① 直连后端（最能暴露 key/网络/模型名问题）
    try:
        out["raw"] = t._chat(t.mt_system, (t.mt_user_prefix or "") + text)
    except Exception as e:
        out["raw_error"] = f"{type(e).__name__}: {e}"

    # ② 完整管线（真实产出）
    segs = [{"start_ms": 0, "end_ms": 2000, "text": text}]
    t0 = time.perf_counter()
    try:
        t.translate_segments(segs, "ja")
    except Exception as e:
        out["pipeline_error"] = f"{type(e).__name__}: {e}"
    out["pipeline"] = segs[0].get("translation") or ""
    out["ms"] = round((time.perf_counter() - t0) * 1000, 1)
    out["ok"] = bool((out["raw"] or out["pipeline"])) and not out["raw_error"]
    return out


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
    global _LAST_REQ_TS, _INFLIGHT
    # 先看 Content-Length 再读体：超大请求直接拒收，不先把几百 MB 读进内存
    cl = request.headers.get("content-length", "")
    if cl.isdigit() and int(cl) > MAX_BODY_BYTES:
        return {"language": None, "segments": [], "asr_ms": 0.0, "mt_ms": 0.0,
                "skipped": True, "error": f"请求体超过上限 {MAX_BODY_BYTES // (1024*1024)}MB"}
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        return {"language": None, "segments": [], "asr_ms": 0.0, "mt_ms": 0.0,
                "skipped": True, "error": f"请求体超过上限 {MAX_BODY_BYTES // (1024*1024)}MB"}
    with _INFLIGHT_LOCK:
        _INFLIGHT += 1
    try:
        return await _transcribe_impl(body, lang, video_start_ms, keep_from_ms, translate)
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT -= 1
        _LAST_REQ_TS = time.time()   # 请求结束才刷新空闲时钟（见 _LAST_REQ_TS 注释）


async def _transcribe_impl(body: bytes, lang: str, video_start_ms: int,
                           keep_from_ms: int, translate: bool) -> dict:
    if len(body) < 3200:  # <0.1s 音频，直接返回
        return {"language": None, "segments": [], "asr_ms": 0.0, "mt_ms": 0.0,
                "skipped": True, "reason": "音频过短"}
    if len(body) % 2:
        body = body[:-1]
    pcm = np.frombuffer(body, dtype=np.int16).astype(np.float32) / 32768.0

    with _CTX_LOCK:
        prev = dict(_LAST_CTX) if _LAST_CTX["lang"] == lang else {"lang": lang, "src": "", "zh": ""}
    asr_extra = prev["src"][:60]   # 上一句原文截断后作热词补充（echo 有回显重试兜底）

    t0 = time.perf_counter()
    result = await run_in_threadpool(
        state["asr"].transcribe, pcm, lang, video_start_ms, keep_from_ms,
        CFG.get("vad", {}), CFG.get("segment", {}), asr_extra,
    )

    # 二次识别兜底：主 ASR 零输出、但块里确有语音能量（气声/耳语台词是
    # 0.6B 模型的盲区，实测 916/978/995s 三处纯净音频也零输出）时，
    # 用 kotoba-whisper（CPU int8）重试一次。纯静音块直接跳过不浪费算力。
    if not result.get("segments") and not result.get("skipped"):
        peak = float(np.abs(pcm).max()) if len(pcm) else 0.0
        if peak > 0.02:
            try:
                state.setdefault("whisper_fb", WhisperFallback(
                    (CFG.get("asr", {}) or {}).get("whisper_fallback") or {}))
                fb = state["whisper_fb"]
                if fb.available():
                    fb_segs = await run_in_threadpool(
                        fb.transcribe, pcm, 16000,
                        "ja" if lang.startswith("ja") else lang)
                    if fb_segs:
                        video0 = video_start_ms
                        for s in fb_segs:
                            s["start_ms"] += video0
                            s["end_ms"] += video0
                        result["segments"] = fb_segs
                        result["backend"] = "kotoba-whisper"
                        print(f"[asr] 主引擎零输出 → kotoba-whisper 兜底 {len(fb_segs)} 段",
                              flush=True)
            except Exception as e:
                print(f"[asr] whisper 兜底失败（忽略）: {type(e).__name__}: {e}", flush=True)
    mt_ms = 0.0
    if translate and result["segments"]:
        t1 = time.perf_counter()
        tr_ctx = ""
        if prev["src"] or prev["zh"]:
            tr_ctx = ("以下是上一句的原文与译文，仅供理解剧情衔接；不要翻译或输出它们：\n"
                      f"上一句原文：{prev['src']}\n上一句译文：{prev['zh']}")
        await run_in_threadpool(state["translator"].translate_segments,
                                result["segments"], lang, tr_ctx)
        mt_ms = round((time.perf_counter() - t1) * 1000, 1)
        # 更新滚动上下文：取时间上最后一条（下一块用它做翻译承接 + ASR 热词）
        if result["segments"]:
            last = result["segments"][-1]
            with _CTX_LOCK:
                _LAST_CTX.update({"lang": lang,
                                  "src": (last.get("text") or "")[:80],
                                  "zh": (last.get("translation") or "")[:80]})
    if result["segments"]:
        # 后处理与流式桥同一套显示策略（判据在 text_filters / stream_bridge，勿重复实现）。
        # 无论走不走翻译都要做：此前清洗挂在 translate 分支里，translate=false 时
        # 引号/碎片原样下发，与流式路径行为不一致。
        # ① 引号剥离：ASR 会在行首尾带出引号类符号（实测 …想让你看呢。"）
        # ② 块内碎片去重：同一响应里互为子串的段保留更长一条（重叠区前缀碎片，
        #    实测 "今日。"/"今日は。"——头显侧 LCS≥6 判据接不住这种短碎片）
        # ③ 整句复读置空：error=untranslated_leak 且译文假名 ≥2。判据必须是假名
        #    计数而不是"有没有汉字"——术语表修补把人名换成汉字（悠亜→悠亚），
        #    整句日文掺两个汉字就绕过了汉字判据（实测开头第一句天天上屏日文）
        from stream_bridge import _display_zh
        from text_filters import strip_wrap_quotes
        segs = result["segments"]
        for s in segs:
            s["text"] = strip_wrap_quotes(s.get("text") or "")
            s["translation"] = strip_wrap_quotes(s.get("translation") or "")
        kept = []
        for s in sorted(segs, key=lambda x: len(x.get("text") or ""), reverse=True):
            ct = s.get("text") or ""
            if ct and any(ct in (k.get("text") or "") for k in kept):
                continue
            kept.append(s)
        result["segments"] = sorted(kept, key=lambda x: x.get("start_ms") or 0)
        for s in result["segments"]:
            s["translation"] = _display_zh(s)
    result["mt_ms"] = mt_ms
    result["total_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    preview = " | ".join((s.get("translation") or s["text"])[:18] for s in result["segments"][:3])
    print(f"[transcribe] lang={lang} start={video_start_ms}ms bytes={len(body)} "
          f"skip={result['skipped']} segs={len(result['segments'])} "
          f"asr={result['asr_ms']}ms mt={mt_ms}ms total={result['total_ms']}ms"
          + (f"\n             {preview}" if preview else ""), flush=True)
    return result

# ---------------------------------------------------------------------------
# AI 字幕 —— 流式转写（把"分块离线"换成"流式增量"，见 stream_bridge.py）
# 头显侧在 streaming 模式下会打这个端点；非流式仍走上面的 /transcribe，互不影响。
# ---------------------------------------------------------------------------
@app.post("/transcribe/stream")
async def transcribe_stream(request: Request, lang: str = "ja", translate: bool = True, video_start_ms: int = 0):
    # 空闲回收的打点与在飞计数必须覆盖流式路由：此前只有 /transcribe 打点，
    # 连续看片 15 分钟后 reaper 会在播放中途把服务杀掉（流式请求再密也救不了）。
    global _LAST_REQ_TS, _INFLIGHT
    with _INFLIGHT_LOCK:
        _INFLIGHT += 1
    try:
        from stream_bridge import transcribe_stream as _impl   # 同目录，复用已验证实现
        return await _impl(request, lang, translate, video_start_ms)
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT -= 1
        _LAST_REQ_TS = time.time()   # 请求结束才刷新空闲时钟（与 /transcribe 同语义）