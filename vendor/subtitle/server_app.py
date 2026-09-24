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

# ⚠️ **必须排在下面那批重量级 import 之前**：`translate_engine`（连带 huggingface_hub）
# 会连带 import huggingface_hub —— hub 的缓存路径是在 import 时**读成常量**的
# （HF_HUB_CACHE），之后再改环境变量一概无效。
# 实测踩中：whisper_backend 是惰性导入的，等它设 HF_HOME 时 hub 早已冻结在
# `%USERPROFILE%\.cache\huggingface`（C 盘），于是模型搬进安装目录后 whisper 找不到模型、
# **静默回落 audiocpp**（配置写着 whisper，实际在跑 Qwen3）。
# 缓存位置的规则只在 user_paths 里写一份；这里只负责"尽早执行"。
import user_paths  # noqa: E402

user_paths.apply_hf_env()

import asyncio  # noqa: E402
import hashlib  # noqa: E402
import hmac  # noqa: E402
import json  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import urllib.parse  # noqa: E402  （跨站栅栏要解析 Origin）
from contextlib import asynccontextmanager  # noqa: E402

import numpy as np  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from starlette.concurrency import run_in_threadpool  # noqa: E402

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

# /transcribe 请求体上限：裸 PCM 25s 块约 800KB（云端档位的推荐块长）；宿主误传/恶意传
# 超大块时，body + int16 视图 + float32 拷贝峰值内存约 3× 字节数，必须设上限挡住。
# 100MB（旧值）是正常块的 50 倍余量、够同网设备一次打满内存（评审 F16）——收到 8MB，
# 仍是 25s 块的 10 倍。可用 server.max_body_mb 覆盖。
try:
    MAX_BODY_BYTES = max(1, int((CFG["server"].get("max_body_mb") or 8))) * 1024 * 1024
except Exception:
    MAX_BODY_BYTES = 8 * 1024 * 1024

# 并发上限（评审 F16）：/transcribe* 对局域网开放且没有限流，同网任意设备可以并发打满
# GPU/内存并烧云端翻译额度。这里只做**快速失败**：超过上限立刻回 503 并附档位建议，
# 而不是排队（排队会让每一块都等到超时，头显体验比直接失败更差）。
try:
    MAX_INFLIGHT = max(1, int((CFG["server"].get("max_inflight") or 4)))
except Exception:
    MAX_INFLIGHT = 4


# 翻译后端的环境变量兜底：config 没写时才生效，避免「改了 config 却不生效」。
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
#
# 两个时钟（评审 F18）：**空闲判定必须用单调钟**——墙钟被 NTP 校正/双系统回拨时
# `idle` 会变负 ⇒ 那约 3GB 的模型显存一直驻留到墙钟追回来；前跳超过 idle_release_min
# 又会在两块请求之间把**正在使用**的服务杀掉。对外展示的 `last_req_ts` 必须仍是
# **墙钟**（PC 界面按 `Date.now()/1000 - last_req_ts` 算"多久以前"），所以两个都打。
_LAST_REQ_MONO = time.monotonic()   # 只用于空闲回收判定
_LAST_REQ_WALL = _BOOT_TS           # 只用于 /health 对外展示
_INFLIGHT = 0                    # 正在处理中的 /transcribe 数
_INFLIGHT_LOCK = threading.Lock()


def _touch_request_clock() -> None:
    """一次识别请求收尾时同时刷新两个时钟（见上面两个变量的注释）。"""
    global _LAST_REQ_MONO, _LAST_REQ_WALL
    _LAST_REQ_MONO = time.monotonic()
    _LAST_REQ_WALL = time.time()


# ---- 单客户端独占（评审 F11）-------------------------------------------------
# 会话状态是**进程级单份**：`_HYBRID` 混合缓冲、`_LAST_CTX`、以及 stream_bridge 的
# `_recent_ja` / `_last_ctx`，全都按"同一时刻只有一个客户端在推流"设计
# （hybrid_segmenter 的注释也自认这是既有事实）。但 8756 绑 0.0.0.0，头显与手机都会
# 直连：两设备并发推流时音频交错进同一个缓冲、上下文互相串台（热词与剧情承接用错对白），
# 两边字幕全乱；默认无鉴权时第二台设备还能把任意文本注入下一句的 ASR 热词与云端提示词。
# 这里把那个隐含假设**变成显式约束**：第一个开始推流的来源独占会话，别的来源在独占期内
# 收到 503（带档位建议）。最后一个请求过去 _SESSION_TAKEOVER_SEC 秒后自动释放，避免
# "客户端崩了/换设备之后永久占着"。要明知会串台也恢复多客户端：server.single_client=false。
_SESSION = {"ip": "", "ts": 0.0}
_SESSION_LOCK = threading.Lock()
_SESSION_TAKEOVER_SEC = 30.0


def _single_client_enabled() -> bool:
    v = (CFG.get("server") or {}).get("single_client", True)
    if isinstance(v, str):
        return v.strip().lower() not in ("false", "0", "no", "off")
    return bool(v)


def _claim_session(request) -> "str | None":
    """认领会话。返回 None = 可以服务；返回字符串 = 拒绝原因（回 503）。"""
    if not _single_client_enabled():
        return None
    try:
        ip = (request.client.host if request.client else "") or ""
    except Exception:
        ip = ""
    now = time.monotonic()
    with _SESSION_LOCK:
        owner, ts = _SESSION["ip"], _SESSION["ts"]
        if owner and owner != ip and (now - ts) < _SESSION_TAKEOVER_SEC:
            left = _SESSION_TAKEOVER_SEC - (now - ts)
            return (f"字幕会话正被另一台设备使用（{owner}）：同一时刻只支持一个客户端，"
                    f"多设备并发会让字幕串台。若那台已停止，约 {left:.0f} 秒后可接管。")
        _SESSION["ip"], _SESSION["ts"] = ip, now
    return None


# 翻译预热状态（R63.2 的 mt_warm / F23 的代次失效）现在由 Translator 自己持有：
# `Translator.warm_info()` 是 /health 三个 mt_warm* 字段的唯一来源。放在那边是因为
# **换模型**这件事只有翻译层知道（按源语言路由 → use_model 重启 llama-server），
# 而"热没热"必须跟着模型走：新权重没有那次 prefill 的账。这里不再存一份副本
# （两处状态 = 两处判据，正是评审反复点的问题）。
_LAST_PARTIAL_TS = 0.0           # 上次 partial 临时稿的发出时刻（monotonic 秒）。
                                 # partial 每块都跑一遍 ASR 太费 GPU（连续说话时
                                 # 切句周期间能挤进 2~3 次），2s 节流后 partial
                                 # 频率与块节拍持平，定稿出字不受影响（R68）
_HYBRID_LOCK = threading.Lock()  # _HYBRID/_HYBRID_VAD 懒初始化锁（R68）：并发首请求
                                 # 此前可能各建一份缓冲/VAD 后端，后建者覆盖先建者

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
        return float((CFG.get("server") or {}).get("idle_release_min", 5) or 0)
    except Exception:
        return 5.0


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
        # 10s 粒度（R54）：回收阈值改成了分钟级的小值（用户要求"没用就尽快清显存"），
        # 30s 粒度下实际回收延迟最坏 = 阈值 + 30s，体验跟不上阈值本身的意义。
        await asyncio.sleep(10)
        mins = _idle_release_min()
        if mins <= 0:
            continue
        with _INFLIGHT_LOCK:
            inflight = _INFLIGHT
        if inflight > 0:
            continue
        # 单调钟（评审 F18）：墙钟被回拨时 idle 会变负而永远不回收；前跳超过阈值
        # 又会在两块请求之间把正在用的服务杀掉。判定只看单调钟。
        idle = time.monotonic() - _LAST_REQ_MONO
        if idle < mins * 60:
            continue
        with _INFLIGHT_LOCK:      # 复核 + 退出原子化：在飞请求的打点会被这把锁挡住
            if _INFLIGHT > 0:
                continue
            print(f"[server] 空闲 {idle / 60:.1f} 分钟 ≥ {mins:g} 分钟，"
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

    R66：kotoba/whisper 转录方案整体剔除（用户拍板）——识别引擎只有
    audiocpp（Qwen3-ASR，日英双语已实测）。回退链 = audiocpp → 未就绪兜底。
    """
    kind = str(cfg.get("backend", "audiocpp") or "audiocpp").lower()
    if kind not in ("audiocpp", "cpp", "ggml", "qwen3"):
        print(f"[server] ⚠️ 未知识别引擎 {kind!r}，回落 audiocpp（Qwen3）")
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
        print(f"[server] ⚠️ audiocpp 不可用（{type(e).__name__}: {e}），进入未就绪模式"
              f"（下载识别模型后重启字幕服务即恢复）")
        be = _AsrUnavailable()
        be.error = str(e)    # R97：实例级覆盖兜底文案——缺 GPU 运行时 ≠ 缺模型，原因要如实透出
        return be


def _warm_translator() -> None:
    """翻译引擎预热（R54 + R63.2）：拉起本地 llama-server，再用**真实系统提示词**
    喂一条极短请求，把 prefill 与首包 CUDA 路径的账在启动期付掉。

    `mt_warm` 的记账不在这里（评审 F23）：那次成功请求经 `_chat_local` 时，
    `Translator` 自己会把"当前模型代次已热"记下来，`/health` 从 `warm_info()`
    读——这样**换模型**（按源语言路由）时标记会自动失效，不需要两个地方各记一份。
    """
    t = state["translator"]
    try:
        if t is None or t.disabled or t.backend != "local":
            return
        from translate_engine import _local_backend
        be = _local_backend(t.cfg.get("local") or {})
        be.ensure_server()
        print(f"[server] 翻译引擎已预热：{be.base_url}（{be.model.name}）", flush=True)
    except Exception as e:
        print(f"[server] 翻译引擎预热失败（翻译请求时会重试并如实报错）："
              f"{type(e).__name__}: {e}", flush=True)
        return
    try:
        _t0 = time.perf_counter()
        t._chat(t.mt_system, (t.mt_user_prefix or "") + "テスト")
        _dt = time.perf_counter() - _t0
        print(f"[server] 翻译预热完成（含提示词首包）：{_dt:.1f}s", flush=True)
    except Exception as e:
        print(f"[server] 翻译预热请求失败（不影响服务，首条翻译会照常重试）："
              f"{type(e).__name__}: {e}", flush=True)


@asynccontextmanager
async def lifespan(_app):
    state["translator"] = Translator(CFG.get("translate", {}))
    print(f"[server] 翻译后端={state['translator'].backend}")

    # R63.3 选项②：预热线程先起（与 whisper 加载并行，ready 时点不叠加）；
    # 本地翻译时**等预热（含提示词首包）完成再放行 /health**——ready 从
    # 「识别加载完」升级为「识别+翻译全热」，头显晚几秒推流换首句全速。
    # 12s 兜底：llama 起不来也绝不卡死服务（超时后线程继续在后台试，行为
    # 退回 R54 的后台预热）。头显只在推流前轮询 ready，ready 晚 = 推流晚，
    # 不会丢音频（播放本身不等字幕）。
    _warm = threading.Thread(target=_warm_translator, daemon=True,
                             name="translator-warmup")
    _warm.start()
    state["asr"] = _make_asr(CFG.get("asr", {}))
    print(f"[server] 管线代码签名 code_sig={CODE_SIG}（陈旧实例排障用）", flush=True)
    if (CFG.get("translate") or {}).get("backend") == "local":
        _warm.join(timeout=12.0)
    _reaper = asyncio.create_task(_idle_reaper())
    print(f"[server] 空闲回收：{_idle_release_min():g} 分钟无识别请求后释放模型"
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


# ---------------------------------------------------------------- 访问令牌（可选鉴权）
# 8756 的 /transcribe* 对整个局域网开放，而 config.json 里可能带着云端翻译的明文
# API Key——不设防的话同网任意设备都能借 /transcribe?translate=true 烧额度。
# server.auth_token **非空**时，/transcribe 与 /transcribe/stream 必须携带同值请求头
# `X-FSC-Subtitle-Token`，否则 401；/health 保持开放（宿主与头显的探测不携带令牌）。
# auth_token 为空串（默认）时行为与过去完全一致——不校验，向后兼容。
_AUTH_HEADER = "X-FSC-Subtitle-Token"


def _auth_token() -> str:
    try:
        return str((CFG.get("server") or {}).get("auth_token") or "")
    except Exception:
        return ""


class _TokenGuard(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if _auth_token() and request.url.path.startswith("/transcribe"):
            # 常数时间比较：普通 != 按前缀逐字节短路，会泄漏最长匹配前缀的长度。
            if not hmac.compare_digest((request.headers.get(_AUTH_HEADER) or "").encode("utf-8"),
                                       _auth_token().encode("utf-8")):
                return JSONResponse({"error": "字幕服务令牌缺失或不匹配"},
                                    status_code=401)
        return await call_next(request)


app.add_middleware(_TokenGuard)


class _OriginGuard(BaseHTTPMiddleware):
    """跨站栅栏（评审 F16）：浏览器发起的跨站 POST **一定**带 Origin 头，而头显
    (OkHttp) / curl / 本机脚本都不带。8756 的 /transcribe 收裸 PCM，属**免预检的简单
    请求**，所以用户浏览器里的任意网页都能用 no-cors 直接打过来（PNA 只救得了新 Chrome）
    ——同网的恶意页面可以借它烧云端额度、占满 GPU。8790 早就有这道理（见
    host_server 的 CSRF 栅栏），8756 一直漏配。

    放行规则：**没有 Origin 就放行**（非浏览器客户端）；有 Origin 但主机是回环（任意端口，
    含 PC 界面所在页面）也放行。其余一律 403。
    """

    async def dispatch(self, request, call_next):
        origin = (request.headers.get("Origin") or "").strip()
        if origin:
            host = urllib.parse.urlparse(origin).hostname or ""
            if host not in _LOOPBACK:
                return JSONResponse({"error": "跨站请求被拒绝"}, status_code=403)
        return await call_next(request)


app.add_middleware(_OriginGuard)

# 在飞请求计数（评审 F16：过载快速失败）。与空闲回收用的 _INFLIGHT 分开：
# 那个统计的是"有请求在处理"，这个只管"是否超过并发上限"。
_OVERLOAD = {"n": 0}
_OVERLOAD_LOCK = threading.Lock()


class _OverloadGuard(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if not request.url.path.startswith("/transcribe"):
            return await call_next(request)
        with _OVERLOAD_LOCK:
            if _OVERLOAD["n"] >= MAX_INFLIGHT:
                busy = _OVERLOAD["n"]
            else:
                _OVERLOAD["n"] += 1
                busy = 0
        if busy:
            # 快速失败 + 告诉客户端该怎么降载（契约 B 的档位建议本来就随应答下发）
            return JSONResponse(
                {"error": f"字幕服务繁忙（{busy}/{MAX_INFLIGHT} 在飞），请降低推流频率",
                 "recommended_chunk_sec": _recommended_chunk_sec()},
                status_code=503)
        try:
            return await call_next(request)
        finally:
            with _OVERLOAD_LOCK:
                _OVERLOAD["n"] = max(0, _OVERLOAD["n"] - 1)


app.add_middleware(_OverloadGuard)


def _recommended_chunk_sec() -> int:
    """档位建议（秒，契约 B）：云端翻译单块实测 6.6–15s，客户端固定 3s 块会结构性
    追不上（块尾落后即被丢弃）。与宿主 headset_status() 同一口径：云端 25 / 其余 3。
    每次 /transcribe 的应答都带上，客户端据此自适应分块时长。"""
    backend = str((CFG.get("translate") or {}).get("backend") or "")
    t = state.get("translator")
    if t is not None and getattr(t, "backend", ""):
        backend = t.backend            # 归一后的实际后端（cloud/dashscope → openai）
    return 25 if backend in ("openai", "cloud") else 3


def _gpu_used_gb() -> float:
    """（已废弃，恒 0）原为 PyTorch 引擎的 torch 显存监控；两个现役引擎
    （whisper=CTranslate2 / audiocpp=独立进程）的显存都不归 torch 管，
    监控看宿主 /api/state 的 gpu 段（NVML 总量）。字段保留仅为兼容旧客户端。"""
    return 0.0


# 显存档位表（R66）：按模型路径特征估算 VRAM 占用（MB）。文件大小 ≈ 权重体积，
# 运行时开销（CUDA 上下文/KV/音频运行时）单列 FIXED。数字是实测+余量的粗估，
# 用于 UI 的"选组合 → 提示显存"，不是精确值。
_VRAM_FIXED_MB = 900        # audio.cpp 运行时 + llama.cpp 运行时 + CUDA 上下文 + 杂项
_VRAM_TIERS = [
    ("1.7B", 3600),         # Qwen3-ASR-1.7B（bf16 权重 ~3.4GB）
    ("0.6B", 1300),         # Qwen3-ASR-0.6B
    ("Hy-MT2-7B", 4700),    # Hy-MT2-7B Q4_K_M（4.6GB 文件 + 激活）
    ("Hy-MT2-1.8B", 1300),  # Hy-MT2-1.8B Q4_K_M
    ("7b-qwen2.5", 4400),   # Sakura-7B iq4xs
    ("1.5b-qwen2.5", 1400), # Sakura-1.5B q5ks
]


def _tier_mb(path: str) -> int:
    low = str(path or "").lower().replace("\\", "/")
    for key, mb in _VRAM_TIERS:
        if key.lower() in low:
            return mb
    # 清单外自装模型（R72）：档表没命中就按模型文件实际大小估（+~10% 运行开销）。
    # 识别是模型目录里的单文件；翻译可能是目录也可能是裸 GGUF 文件。
    try:
        p = (Path(__file__).resolve().parent / str(path or "")).resolve()
        if p.is_file():
            return int(p.stat().st_size / (1024 * 1024) * 1.1)
        f = p / "model.safetensors"
        if not f.is_file():
            ggufs = sorted(p.glob("*.gguf")) if p.is_dir() else []
            if not ggufs:
                return 0
            f = ggufs[0]
        return int(f.stat().st_size / (1024 * 1024) * 1.1)
    except Exception:
        pass
    return 0


def _vram_estimate() -> dict:
    """按当前配置估算显存占用（R66）。翻译模型按语言热切换、**同时只驻留一个**，
    所以翻译取 ja/en 两档的较大值；转录模型单独驻留。"""
    asr = (CFG.get("asr", {}) or {})
    asr_mb = 0
    if str(asr.get("backend", "")).lower() == "audiocpp":
        asr_mb = _tier_mb(((asr.get("audiocpp", {}) or {}).get("model", "")))
    local = (CFG.get("translate", {}) or {}).get("local", {}) or {}
    ja_mb = _tier_mb(str(local.get("model", "")))
    en_mb = _tier_mb(((local.get("model_by_lang") or {}).get("en", "")))
    mt_mb = max(ja_mb, en_mb)
    total = asr_mb + mt_mb + _VRAM_FIXED_MB
    return {"asr_mb": asr_mb, "mt_ja_mb": ja_mb, "mt_en_mb": en_mb,
            "mt_active_mb": mt_mb, "fixed_mb": _VRAM_FIXED_MB,
            "total_mb": total}


# /health 的活体探测缓存（评审 F12）。/health 是 1s 轮询，而探测要发一次 HTTP；
# 缓存 2 秒让并发轮询共用一次结果，也让"上游挂死"最多每 2 秒吃掉一个 1s 超时。
_HEALTH_PROBE: dict = {"ts": 0.0, "ok": None}


def _asr_ready(asr) -> bool:
    """识别现在**真的**能用吗（评审 F12）。

    旧判据 `backend_kind != "unavailable"` 查的是**类属性**：audiocpp_server.exe
    崩溃/被杀之后它照样是 "audiocpp"，于是 /health 恒报 ready —— 头显以为一切正常、
    继续推流，而每一段转写都在失败，用户看到的是"字幕静默全空"、宿主侧毫无异常。
    现在对支持 probe() 的后端**真的探一次**（audiocpp 的 /health，1s 超时）。

    语义边界：probe 反映的是**上游进程活着且就绪**（模型的 lazy_load 由上游自己管，
    服务进程在就绪前也不会报 status=ok），所以这里就是头显该看到的"能不能推流"。
    """
    if asr is None or getattr(asr, "backend_kind", "unavailable") == "unavailable":
        return False
    probe = getattr(asr, "probe", None)
    if not callable(probe):          # 没有活体探测的后端：退回"已构造即就绪"
        return True
    now = time.time()
    if _HEALTH_PROBE["ok"] is not None and (now - _HEALTH_PROBE["ts"]) < 2.0:
        return _HEALTH_PROBE["ok"]
    try:
        ok = bool(probe(1.0))
    except Exception:
        ok = False
    _HEALTH_PROBE["ts"], _HEALTH_PROBE["ok"] = now, ok
    return ok


def _model_label(m) -> str:
    """把模型标识收敛成"不含本机路径"的形式（评审 F19）。

    `/health` 属 `_LAN_OPEN_PREFIXES`（局域网可读），而 audiocpp 后端的 `.model` 是
    **解析后的绝对路径**——便携安装里还带着 Windows 用户名（实测回出
    `E:\\...\\models\\Qwen3-ASR-0.6B`）。项目自己对错误文本早有脱敏标准
    （audiocpp_backend._error_kind 专门把绝对路径摘掉），漏的就是这个字段。

    绝对路径 → 只留最后两段（`models/Qwen3-ASR-0.6B`），比纯 basename 有信息量、又不含
    盘符与用户名；whisper 那种 HF 仓名（`kotoba-tech/kotoba-whisper-...`）原样返回。
    """
    s = str(m or "")
    if not s:
        return s
    try:
        p = Path(s)
        if not p.is_absolute():
            return s
        parts = [x for x in p.parts
                 if x not in (p.anchor, "\\", "/") and not x.endswith(":\\")]
        return "/".join(parts[-2:]) if parts else p.name
    except Exception:
        return s


@app.get("/health")
def health():
    asr = state["asr"]
    # asr_model 优先显示后端自报的身份（whisper 是 HF 模型名而非本机路径；
    # PyTorch 引擎的 .model 是模型对象，不能直接回）——评测日志靠它区分 A/B 两边
    m = getattr(asr, "model", None)
    if not isinstance(m, str) or not m:
        m = (CFG.get("asr", {}) or {}).get("model")
    # 脱敏（评审 F19）：本接口对局域网开放，绝对路径含盘符与用户名
    m = _model_label(m)
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
        # 切句口径（R63）：chunk=3s/1s 定长块（现役）；hybrid=RMS 静音定界+VAD 赋时。
        # 评测与排障靠它区分跑的是哪套切段——config 换档不换代码签名（R60 教训）。
        "segmentation": "hybrid" if _hybrid_mode() else "chunk",
        "asr_model": m,
        # 识别是否真的可用（评审 F12）：不能只看 backend_kind —— 那是类属性，
        # 上游崩了它照样是 "audiocpp"，于是这里恒 True、头显误以为就绪。
        "asr_ready": _asr_ready(asr),
        "device": (CFG.get("asr", {}) or {}).get("device"),
        "vad": asr.vad is not None if asr else False,
        "aligner": asr.use_aligner if asr else False,
        "gpu_used_gb": _gpu_used_gb(),
        "translate_backend": state["translator"].backend if state["translator"] else None,
        "translate": state["translator"].describe() if state["translator"] else None,
        # 空闲回收透明化（R54）：PC 端界面用这两个字段显示"最近活动 + 还有多久
        # 自动回收"，用户能看出服务为什么自己停了（而不是像凭空消失）。
        # last_req_ts 必须是**墙钟**（界面按 Date.now()/1000 - last_req_ts 算"多久以前"），
        # 而空闲判定用的是单调钟（F18）——两者在 _touch_request_clock() 里一起打点。
        "last_req_ts": _LAST_REQ_WALL,
        "idle_sec": round(time.monotonic() - _LAST_REQ_MONO, 1),
        "idle_release_min": _idle_release_min(),
        # 翻译是否已用真实提示词预热（R63.2）：ready 只保证识别模型加载完，
        # 这里的时间戳非 0 才说明首句翻译不会再付 prefill 的账。
        # **状态从 Translator 读**（评审 F23）：它是按模型代次记账的 —— 按源语言
        # 路由换过权重后自动变 false（新权重没付过那次 prefill），新模型跑完第一句
        # 又自动变 true。旧实现在这里存了一份"只置位不复位"的副本 ⇒ 换过模型照样
        # 报热，用户按这个字段判断"翻译热了没"会被骗。
        **(state["translator"].warm_info() if state["translator"] else
           {"mt_warm": False, "mt_warm_ts": None, "mt_warm_epoch": None}),
        # 显存档位估算（R66）：UI 的「识别与翻译」卡据此显示占用与推荐组合
        "vram_estimate": _vram_estimate(),
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
        # route=False（评审 F23）：这是**诊断**请求，不该按语言路由本地模型 ——
        # 头显在看英语内容（Hy-MT2）时，用户在 PC 端点一下「测试」，旧实现会把
        # 模型切回日语（stop_server + 6.3s 冷启动），而且掐掉在飞请求、累积假熔断。
        t.translate_segments(segs, "ja", route=False)
    except Exception as e:
        out["pipeline_error"] = f"{type(e).__name__}: {e}"
    out["pipeline"] = segs[0].get("translation") or ""
    out["ms"] = round((time.perf_counter() - t0) * 1000, 1)
    out["ok"] = bool((out["raw"] or out["pipeline"])) and not out["raw_error"]
    return out


# ---------------------------------------------------------------- 混合切句（R63 立项）
# asr.segmentation = "chunk"（默认，3s/1s 定长块+逐块 VAD，现役口径）| "hybrid"
# （服务端 RMS 静音定界 + silero VAD 赋时，R62 原型实测两片零漏识+句界三件套）。
# 头显协议零改动：音频照旧按块 POST /transcribe，重切句全在 PC 侧。
_HYBRID = None       # HybridBuffer 单例（单客户端与 _LAST_CTX 同一假设）
_HYBRID_VAD = None   # AudioCppBackend 只当 silero VAD 用（惰性建，不起 ASR 服务）


def _hybrid_mode() -> bool:
    return str((CFG.get("asr", {}) or {}).get("segmentation", "chunk")).lower() == "hybrid"


def _hybrid_vad_fn(pcm):
    """silero VAD（生产同款 CLI 与默认参数）→ [(start_sec, end_sec)]；失败回 None。

    失败不致命：对齐器退化为"保留 ASR 自报时间"，混合切句降级成 RMS 定句版。
    """
    global _HYBRID_VAD
    try:
        with _HYBRID_LOCK:
            if _HYBRID_VAD is None:
                from audiocpp_backend import AudioCppBackend
                _HYBRID_VAD = AudioCppBackend(
                    (CFG.get("asr", {}) or {}).get("audiocpp", {}) or {})
            vad = _HYBRID_VAD
        return vad.speech_spans(pcm)
    except Exception as e:
        print(f"[hybrid] VAD 不可用（{type(e).__name__}: {e}），退 ASR 自报时间",
              flush=True)
        return None


def _get_hybrid_buffer():
    """HybridBuffer 单例（双检锁懒建，见 _HYBRID_LOCK 注释）。"""
    global _HYBRID
    from hybrid_segmenter import HybridBuffer   # 本函数自用 import（R68 挪构造时漏带，探针实测 NameError）
    with _HYBRID_LOCK:
        if _HYBRID is None:
            h = ((CFG.get("asr", {}) or {}).get("hybrid", {}) or {})
            _HYBRID = HybridBuffer(
                silence_threshold=float(h.get("silence_threshold", 0.01)),
                silence_tail_sec=float(h.get("silence_tail_sec", 0.7)),
                min_phrase_sec=float(h.get("min_phrase_sec", 2.0)),
                max_phrase_sec=float(h.get("max_phrase_sec", 5.0)),
                long_silence_tail_sec=float(h.get("long_silence_tail_sec", 0.4)),
                long_phrase_sec=float(h.get("long_phrase_sec", 4.0)),
                vad_fn=_hybrid_vad_fn)
        return _HYBRID


def _hybrid_transcribe(pcm, lang, video_start_ms, seg_cfg,
                       want_partial: bool = False, asr_extra: str = "") -> dict:
    """混合切句的整句转写。返回结构与 state["asr"].transcribe 同约定。

    注意 skipped 语义：还在积累/静音丢弃时 = True——既如实告诉调用方"本轮
    无出句"，也让 whisper 二次兜底别拿半截缓冲去空跑（R62 教训：兜底只认
    "主引擎整段零输出"，混合模式下主引擎已经在完整句上跑过了）。

    `asr_extra`（评审 F20）：上一句原文截断后作 ASR 热词补充（跨块人名/专名承接）。
    此前调用方算了 `asr_extra` 却**没传进来**、这里两处又写死空串 ⇒ 混合模式
    （发运默认档位）下"上一句进 ASR 热词"这个模块头注释里列为核心设计的机制
    **静默失效**（翻译侧的剧情承接还生效，只有 ASR 侧断了）。
    """
    global _HYBRID, _LAST_PARTIAL_TS
    from hybrid_segmenter import (HybridBuffer, SR as _SR,
                                  align_segments_to_groups, merge_regions)
    hbuf = _get_hybrid_buffer()
    span, span_ms, reason = hbuf.feed(pcm, lang, video_start_ms)
    if span is None:
        # partial 渐进出字（R63.6，参考项目 sosv/realtime-subtitle 同思路）：
        # 句子还没切出来时，把当前缓冲的临时转写先发给**主动要了 partial 的
        # 客户端**（手机端；旧头显不带 partial=1 参数，行为零变化），客户端按
        # 「同文本覆盖」语义原位刷新，定稿到达后由跨块去重保留更完整的一条。
        # 节流（R68）：连续说话时切句周期能挤进 2~3 块，每块都全量转写一遍
        # 缓冲纯属烧 GPU——2s 一发已与块节拍持平，观感无差。
        if want_partial and time.monotonic() - _LAST_PARTIAL_TS >= 2.0:
            snap = hbuf.snapshot()
            if snap is not None:
                spcm, sms = snap
                res = state["asr"].transcribe(
                    spcm, lang, sms, 0, None, seg_cfg, asr_extra)
                psegs = res.get("segments") or []
                if psegs:
                    _LAST_PARTIAL_TS = time.monotonic()
                    for s in psegs:
                        s["partial"] = True
                    res["backend"] = "hybrid-partial"
                    print(f"[hybrid] partial {len(spcm) / _SR:.1f}s @{sms}ms → "
                          f"{len(psegs)} 段（临时稿）", flush=True)
                    return res
        return {"language": lang, "segments": [], "asr_ms": 0.0, "mt_ms": 0.0,
                "skipped": True, "backend": "hybrid"}
    result = state["asr"].transcribe(span, lang, span_ms, 0, None, seg_cfg, asr_extra)
    # 对齐用语音区：VAD 裁决的切句直接复切句时的语音区（同一缓冲同一起点，
    # 免第二次 CLI）；RMS/硬切切的才现场跑。⚠ 组是 span 内相对毫秒，必须加
    # span_ms 换成视频绝对时间轴（R63 实测：漏加偏移 → 中位 −700ms）。
    regions = hbuf.last_cut_regions
    hbuf.last_cut_regions = None
    if regions is None:
        regions = _hybrid_vad_fn(span)
    groups = ([(span_ms + ga, span_ms + gb) for ga, gb in merge_regions(regions)]
              if regions else [])
    segs = align_segments_to_groups(result.get("segments") or [], groups)
    result["segments"] = segs
    result["skipped"] = not segs
    result["backend"] = f"hybrid+{result.get('backend') or '?'}"
    print(f"[hybrid] 切句({reason}) {len(span) / _SR:.1f}s @{span_ms}ms → "
          f"组{len(groups)} 段{len(segs)}（ASR {result.get('asr_ms')}ms）", flush=True)
    return result


@app.post("/transcribe")
async def transcribe(request: Request, lang: str = "ja", video_start_ms: int = 0,
                     keep_from_ms: int = 0, translate: bool = True, partial: int = 0):
    global _LAST_REQ_MONO, _LAST_REQ_WALL, _INFLIGHT
    # 进 handler **第一件事**就是自增在飞计数（评审 F10）。旧顺序是"先读体再自增"：
    # 首个请求正在上传那几十~几百 KB 时 _INFLIGHT 仍是 0，空闲回收的临界点就会在
    # 这一刻把服务 os._exit 掉 —— 丢一块 + 服务重启数十秒（/transcribe/stream 本来就是
    # 先自增再读体，两个入口口径还不一致）。自增放这里，超大/超长提前返回也走 finally 归还。
    with _INFLIGHT_LOCK:
        _INFLIGHT += 1
    try:
        # 单客户端独占（评审 F11）：会话状态是进程级单份，多设备并发会互相串台
        deny = _claim_session(request)
        if deny:
            return {"language": None, "segments": [], "asr_ms": 0.0, "mt_ms": 0.0,
                    "skipped": True, "error": deny,
                    "recommended_chunk_sec": _recommended_chunk_sec()}
        # 先看 Content-Length 再读体：超大请求直接拒收，不先把几百 MB 读进内存。
        # 没有 Content-Length（分块传输）时由 read_capped_body 兜底——它边读边累加，
        # 超限立刻中断，与 /transcribe/stream 共用同一实现（两个入口必须同一口径）。
        cl = request.headers.get("content-length", "")
        oversize = {"language": None, "segments": [], "asr_ms": 0.0, "mt_ms": 0.0,
                    "skipped": True, "error": f"请求体超过上限 {MAX_BODY_BYTES // (1024*1024)}MB"}
        if cl.isdigit() and int(cl) > MAX_BODY_BYTES:
            oversize["recommended_chunk_sec"] = _recommended_chunk_sec()
            return oversize
        from stream_bridge import read_capped_body
        body = await read_capped_body(request, MAX_BODY_BYTES)
        if body is None:
            oversize["recommended_chunk_sec"] = _recommended_chunk_sec()
            return oversize
        result = await _transcribe_impl(body, lang, video_start_ms, keep_from_ms, translate,
                                        partial == 1)
        # 契约 B：每个应答都带档位建议（含"音频过短"等提前返回，都会流经这里）
        result["recommended_chunk_sec"] = _recommended_chunk_sec()
        return result
    finally:
        # **先刷时钟再减计数**（评审 F10）：反过来时，超长请求在"已减计数、时钟未刷"的
        # 间隙里会被判成空闲而遭 os._exit（两块请求正好卡在临界点上）。
        _touch_request_clock()
        with _INFLIGHT_LOCK:
            _INFLIGHT -= 1


async def _transcribe_impl(body: bytes, lang: str, video_start_ms: int,
                           keep_from_ms: int, translate: bool,
                           want_partial: bool = False) -> dict:
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
    if _hybrid_mode():
        # asr_extra 必须**传进去**（评审 F20）：此前算了不传，混合模式（发运默认档位）
        # 下"上一句原文进 ASR 热词"这条核心设计静默失效，只有翻译侧的承接还生效。
        result = await run_in_threadpool(
            _hybrid_transcribe, pcm, lang, video_start_ms, CFG.get("segment", {}),
            want_partial, asr_extra)
    else:
        result = await run_in_threadpool(
            state["asr"].transcribe, pcm, lang, video_start_ms, keep_from_ms,
            CFG.get("vad", {}), CFG.get("segment", {}), asr_extra,
        )

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
        # 更新滚动上下文：取时间上最后一条（下一块用它做翻译承接 + ASR 热词）。
        # partial 临时稿**不进**上下文（R68）：半句话当"上一句"会把定稿翻译的
        # 人称/场景衔接带偏；翻译照跑（客户端 partial 阶段显示的就是它）。
        if result["segments"] and not any(s.get("partial")
                                          for s in result["segments"]):
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
        if _hybrid_mode():
            # 混合切句（R63）：一个响应装的是按 VAD 组对齐后的**整句集**，
            # 短句（はい）与长句（はい、頑張ります）并存是常态，跨响应的重叠
            # 去重已由混合缓冲的时间轴完成——这里的子串去重会把正当短句当
            # 碎片误杀（实测 sivr002 掉到 92.1%），必须跳过。
            result["segments"] = sorted(segs, key=lambda x: x.get("start_ms") or 0)
        else:
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
    global _LAST_REQ_MONO, _LAST_REQ_WALL, _INFLIGHT
    with _INFLIGHT_LOCK:
        _INFLIGHT += 1
    try:
        # 单客户端独占（评审 F11）：与 /transcribe 同一口径
        deny = _claim_session(request)
        if deny:
            return JSONResponse({"error": deny,
                                 "recommended_chunk_sec": _recommended_chunk_sec()},
                                status_code=503)
        from stream_bridge import transcribe_stream as _impl   # 同目录，复用已验证实现
        return await _impl(request, lang, translate, video_start_ms)
    finally:
        # 与 /transcribe 同口径：先刷时钟再减计数（评审 F10）
        _touch_request_clock()
        with _INFLIGHT_LOCK:
            _INFLIGHT -= 1