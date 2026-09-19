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
  - keep_from_ms：重叠区去重（客户端传 chunk 起点 + overlap/2）。判据只丢"整句
    基本都落在重叠区"的段——起点在保留区之后，**或**句尾越过 keep_from 300ms 以上
    才保留（跨块长句在两边 start 都早于各自 keep_from，旧判据会把整句扔两次，
    实测 192.8s 那句就这么消失）。判据只有一份：`text_filters.keep_segment`，
    PyTorch 与 audiocpp 两个后端共用
  - ASR 与翻译串行执行；ASR 占 GPU，翻译默认走 Ollama/云端（8GB 卡上两者不能同时驻留）
"""

import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

# ⚠️ **必须排在下面那批重量级 import 之前**：`asr_engine` / `translate_engine` 会连带
# import transformers/torch，而它们又会 import huggingface_hub —— hub 的缓存路径是在
# import 时**读成常量**的（HF_HUB_CACHE），之后再改环境变量一概无效。
# 实测踩中：whisper_backend 是惰性导入的，等它设 HF_HOME 时 hub 早已冻结在
# `%USERPROFILE%\.cache\huggingface`（C 盘），于是模型搬进安装目录后 whisper 找不到模型、
# **静默回落 audiocpp**（配置写着 whisper，实际在跑 Qwen3）。
# 缓存位置的规则只在 user_paths 里写一份；这里只负责"尽早执行"。
import user_paths  # noqa: E402

user_paths.apply_hf_env()

import asyncio  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402

import numpy as np  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from starlette.concurrency import run_in_threadpool  # noqa: E402

from asr_engine import AsrEngine  # noqa: E402
from whisper_fallback import WhisperFallback  # noqa: E402
from translate_engine import Translator  # noqa: E402

# 配置从**数据目录**读（首次运行自动从历史位置迁移，见 user_paths 模块注释）。
# 安装目录里那份从此只是模板：升级覆盖它不再影响用户的 key。
CFG = user_paths.load_config(BASE)


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
for _sec in ("asr", "translate", "server", "vad", "segment"):
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
# ASR 后端同名机制：评测/排障时用环境变量切后端（NEXUS_ASR_BACKEND=whisper），
# 不用动 config.json——它要么是仓库模板（改了会进 git）、要么是运行配置（有 key）。
if os.environ.get("NEXUS_ASR_BACKEND"):
    CFG["asr"]["backend"] = os.environ["NEXUS_ASR_BACKEND"]

state = {"asr": None, "translator": None}

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


class _AsrUnavailable:
    """识别链终态兜底：三个后端都不可用（典型：全新安装还没下载识别模型）。

    此前最后一级 PyTorch 引擎缺依赖会直接抛异常 → lifespan 崩 → 字幕服务永远起不来，
    用户连"下载模型"的入口都进不去。现在保证服务能起、/health 能看到原因；
    /transcribe 返回带 error 的空结果，界面的「模型」区负责引导下载。
    """

    backend_kind = "unavailable"
    vad = None
    use_aligner = False
    load_s = 0.0
    model = ""
    error = "识别模型未安装：请在 PC 端「识别与翻译」卡下载模型（或重启字幕服务）"

    def stop_server(self) -> None:
        pass

    def transcribe(self, *args, **kwargs) -> dict:
        return {"language": None, "segments": [], "asr_ms": 0.0, "skipped": True,
                "error": self.error}


def _make_asr(cfg: dict):
    """按 asr.backend 选引擎。

    - "pytorch"（默认）：原来的 Qwen3-ASR + torch 实现
    - "audiocpp"：audiocpp 常驻服务（CPU 后端显存 0 占用；必须先 VAD 裁剪语音段，
      否则长音频会退化出成百上千连重复——见 audiocpp_backend 模块注释）
    - "whisper"：faster-whisper/CTranslate2 进程内引擎（R44 实测覆盖率 +10%、
      时序更好、快 3×；R45 起可配，见 whisper_backend 模块注释——**不给 prompt**）
    """
    kind = str(cfg.get("backend", "pytorch") or "pytorch").lower()
    if kind in ("whisper", "faster-whisper", "kotoba"):
        try:
            from whisper_backend import WhisperBackend

            be = WhisperBackend(cfg.get("whisper", {}) or {},
                                drop_latin=bool(cfg.get("drop_latin_hallucination", True)))
            be.ensure_model()     # 启动期加载（+2.5~3.9s），首次请求不再付这个代价
            return be
        except Exception as e:
            # 回落 audiocpp（生产验证过的默认）而不是 PyTorch：轻量运行时没有 torch，
            # 掉进 PyTorch 分支 = lifespan 直接炸 = 整场零字幕。回落链必须落在
            # "能跑"的那一级；/health 的 asr_backend 会如实显示实际生效的引擎。
            print(f"[server] ⚠️ whisper 不可用（{type(e).__name__}: {e}），回落 audiocpp（Qwen3）")
    if kind in ("audiocpp", "cpp", "ggml", "whisper", "faster-whisper", "kotoba"):
        # whisper 配置失败也会走到这里（见上）：回退链 whisper → audiocpp → pytorch
        try:
            from audiocpp_backend import AudioCppBackend

            be = AudioCppBackend(
                cfg.get("audiocpp", {}) or {},
                # 拉丁幻觉过滤在 asr 段（与引擎选择同源），默认开
                drop_latin=bool(cfg.get("drop_latin_hallucination", True)),
            )
            be.ensure_server()
            print(f"[server] ASR 后端 = audiocpp（{be.backend}, {be.threads} 线程, "
                  f"端口 {be.port}）")
            return be
        except Exception as e:
            print(f"[server] audiocpp 不可用（{type(e).__name__}: {e}），回退 PyTorch")
    try:
        engine = AsrEngine(cfg)
        print(f"[server] ASR 后端 = pytorch（加载 {engine.load_s:.1f}s）")
        return engine
    except Exception as e:
        print(f"[server] ⚠️ PyTorch 引擎不可用（{type(e).__name__}: {e}），进入未就绪模式"
              f"（下载识别模型后重启字幕服务即恢复）")
        return _AsrUnavailable()


@asynccontextmanager
async def lifespan(_app):
    state["asr"] = _make_asr(CFG.get("asr", {}))
    state["translator"] = Translator(CFG.get("translate", {}))
    print(f"[server] 管线代码签名 code_sig={CODE_SIG}（陈旧实例排障用）", flush=True)
    print(f"[server] 翻译后端={state['translator'].backend}")
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

# ---------------------------------------------------------------- 局域网暴露面收紧
# 8756 绑 0.0.0.0（头显要从局域网直接推音频），但这个进程的 config.json 里可能
# 带着云端翻译的**明文 API Key**。头显只用到 /transcribe* 与 /health；
# 其余接口（selftest / stats）一律收紧到本机回环——
# 等于"把翻译额度开放给整个局域网"的口子被焊死，头显侧协议零改动。
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

_LAN_OPEN_PREFIXES = ("/transcribe", "/health")
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


class _LoopbackGuard(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        if not path.startswith(_LAN_OPEN_PREFIXES):
            client = request.client.host if request.client else ""
            # 判据写成 `not in`（而不是 `client and client not in`）：拿不到来源地址时
            # 必须**拒绝**而不是放行。后者在 client 为 None/空串时跳过整个检查，
            # 等于给"取不到来源"的请求开了一条直通管理接口的路——门卫要 fail-closed。
            if client not in _LOOPBACK:
                return JSONResponse({"error": "该接口仅限本机访问"}, status_code=403)
        return await call_next(request)


app.add_middleware(_LoopbackGuard)


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
    # asr_model 优先显示后端自报的身份（whisper 是 HF 模型名而非本机路径；
    # PyTorch 引擎的 .model 是模型对象，不能直接回）——评测日志靠它区分 A/B 两边
    m = getattr(asr, "model", None)
    if not isinstance(m, str) or not m:
        m = (CFG.get("asr", {}) or {}).get("model")
    return {
        "ok": asr is not None,
        "code_sig": CODE_SIG,
        # 进程身份：宿主靠它判断 8756 上跑的到底是不是自己拉起来的那个。
        # 只看"端口开着"会被上次异常退出残留的进程骗过去，然后报一个假的 ready，
        # 头显就会跳过等待、把音频发给一个陈旧进程（实测踩过）。
        "pid": os.getpid(),
        "started_at": _BOOT_TS,
        "asr_backend": getattr(asr, "backend_kind", None)
                       or str((CFG.get("asr") or {}).get("backend", "pytorch")),
        "asr_model": m,
        # 识别是否真的可用（模型没下载时服务照常起，但这里为 False，头显端据此不误报就绪）
        "asr_ready": getattr(asr, "backend_kind", "unavailable") != "unavailable",
        "device": (CFG.get("asr", {}) or {}).get("device"),
        "vad": asr.vad is not None if asr else False,
        "aligner": asr.use_aligner if asr else False,
        "gpu_used_gb": _gpu_used_gb(),
        "translate_backend": state["translator"].backend if state["translator"] else None,
        "translate": state["translator"].describe() if state["translator"] else None,
    }


@app.get("/translate/stats")
def translate_stats():
    """翻译层累计统计（批量/纠错轮数/兜底）。用于 PC 端诊断页。"""
    t = state["translator"]
    if t is None:
        return {"ready": False}
    return {"ready": True, "describe": t.describe(),
            "stats": dict(t.stats)}


@app.get("/translate/selftest")
def translate_selftest(text: str = "こんにちは、いい天気ですね。"):
    """用**当前配置**真跑一句，把译文与上游原始报错一起回给调用方（UI 的「测试」按钮）。

    为什么要有这个接口：云端后端（DashScope 等）出错时，翻译层的兜底会把失败吞成
    "空译文"，用户只看到没字幕，无从判断是 key 错、余额不足还是模型名错。这里分成两段报：

      · raw      —— 直接调 _chat（不经过批量/兜底），失败时把上游响应体带出来
      · pipeline —— 走完整 translate_segments（含纠错/兜底），反映真实产出
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


@app.post("/transcribe")
async def transcribe(request: Request, lang: str = "ja", video_start_ms: int = 0,
                     keep_from_ms: int = 0, translate: bool = True):
    global _LAST_REQ_TS, _INFLIGHT
    # 先看 Content-Length 再读体：超大请求直接拒收，不先把几百 MB 读进内存。
    # 没有 Content-Length（分块传输）时由 read_capped_body 兜底——它边读边累加，
    # 超限立刻中断，与 /transcribe/stream 共用同一实现（两个入口必须同一口径）。
    cl = request.headers.get("content-length", "")
    oversize = {"language": None, "segments": [], "asr_ms": 0.0, "mt_ms": 0.0,
                "skipped": True, "error": f"请求体超过上限 {MAX_BODY_BYTES // (1024*1024)}MB"}
    if cl.isdigit() and int(cl) > MAX_BODY_BYTES:
        return oversize
    from stream_bridge import read_capped_body
    body = await read_capped_body(request, MAX_BODY_BYTES)
    if body is None:
        return oversize
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
    # 源语言 == 目标语言时跳过翻译。头显的语言枚举里含"中文"（LangCodes=ja/en/ko/zh），
    # 而 PC 的 target_lang 就是 zh：不跳过就会中→中再翻一遍——白烧一次请求，
    # 还可能把本来就正确的中文字幕改坏。lang 可能带地区后缀（zh-CN），取主语言比。
    _src = (lang or "").split("-")[0].strip().lower()
    _tgt = str((CFG.get("translate") or {}).get("target_lang", "zh") or "zh").split("-")[0].lower()
    if translate and _src and _src == _tgt:
        print(f"[transcribe] 源语言({lang})与目标语言({_tgt})相同，跳过翻译", flush=True)
        translate = False
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
        #    计数而不是"有没有汉字"——整句日文掺两个汉字（人名等）就绕过了
        #    汉字判据（实测开头第一句天天上屏日文）
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