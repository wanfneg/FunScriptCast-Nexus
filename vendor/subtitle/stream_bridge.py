"""AI 字幕 —— 流式转写桥（无自有状态，被 server_app.py 挂在 /transcribe/stream）

存在的意义：旧 `POST /transcribe` 是"整块 PCM 进、整块结果出"（25s 分块离线），
延迟 = 一块的时长。audio.cpp v0.7.4 起 Qwen3-ASR 支持 **streaming 会话**，
实测 TTFT 约 1s，所以这里把"分块离线"换成"流式增量"。

对外接口（与 /transcribe 同构，便于头显侧平滑切换）：
    POST /transcribe/stream?lang=ja&translate=true&video_start_ms=<ms>
        请求体 = 裸 PCM（s16le / 16k / 单声道），与 /transcribe 一致
        响应   = text/event-stream
            data: {"type":"delta","ja":"…","zh":"…","first_ms":…,"start_ms":…,"end_ms":…}
            data: {"type":"done","ja":"…","timing":{…},"total_ms":…}
            data: {"type":"error","error":"…"}
            data: [DONE]

上游：audio.cpp server 的 OpenAI 风格端点
    POST /v1/audio/transcriptions   multipart: file=<wav> model=<id> stream=true
    ← SSE: data:{"type":"transcript.text.delta","delta":"…"}
           data:{"type":"transcript.text.done","text":"…","timing":{"ttft_ms":…}}
           data:[DONE]
模型必须配置成 {"mode":"streaming"}（否则上游回 "currently supports offline sessions"）。

上游机制（读自 v0.7.4 / PR#553 源码，接手前必读）：
  · 每个 HTTP 请求 start_stream() → reset()：**跨请求没有上下文**，每个 PCM 块
    独立解码；请求内部按窗口（默认 30s）切窗，窗口文本累积后以 delta 增量吐出。
    3s 一块的现状下每请求恰好 1 个 delta（finalize 时统一出）。
  · 流式模式**不支持时间戳**（start_stream 对 return_timestamps 直接抛异常），
    所以逐句时间只能在这里按"块内字数比例分摊 + 单调游标"推算（_queue）。
  · 上游只收 WAV 容器（裸 PCM 直接 400），_wav_wrap 在内存补 44 字节头。

翻译策略（§8.1 方案B）：切出的句子**攒满 batch_size 或流结束时整批**交给
translate_engine——它的批量/多轮纠错/历史最好一轮/逐条兜底在批次≈10 时效果
最好；每条 delta 各翻一次会把批次打碎成 1~6 条小批，空译文概率显著升高
（实测如此）。桥**不做**任何漏译/空值判定，那全是 translate_engine 的职责。
"""
from __future__ import annotations

import argparse
import http.client
import json
import re
import sys
import threading
import time
import urllib.parse
import struct
import uuid
from collections import deque
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
import uvicorn

from text_filters import has_repetition_loop, count_kana, strip_wrap_quotes   # 与离线路径共用同一判据

# ---- 复用现有翻译器（与 server_app.py 同一套配置）--------------------------
_SUB_DIR = Path(__file__).resolve().parent
if str(_SUB_DIR) not in sys.path:
    sys.path.insert(0, str(_SUB_DIR))

CFG_PATH = _SUB_DIR / "config.json"
# 与 server_app 同源：用户数据目录优先（首次运行自动迁移），安装目录那份只是模板。
# 两条路径若各读各的，用户在界面上改的配置对这条路径就不生效。
import user_paths  # noqa: E402  （同目录，_SUB_DIR 已进 sys.path）

CFG: dict = user_paths.load_config(_SUB_DIR)

_translator = None
_translator_lock = threading.Lock()   # 并发首个请求同时懒加载时只建一份


def _get_translator():
    """懒加载翻译器（首次请求才建，避免启动即占资源）。失败则退化为不翻译。"""
    global _translator
    with _translator_lock:
        if _translator is not None:
            return _translator
        try:
            from translate_engine import Translator  # type: ignore

            _translator = Translator(CFG.get("translate", {}))
            print("[bridge] translator ready", flush=True)
        except Exception as exc:  # 翻译不可用时也不能让字幕整段空白
            print(f"[bridge] translator unavailable: {exc}", flush=True)
            _translator = False
        return _translator


def _batch_size() -> int:
    """攒批阈值与 translate_engine 的批量大小保持同一个配置来源。"""
    try:
        return max(1, int((CFG.get("translate") or {}).get("batch_size", 10)))
    except Exception:
        return 10


app = FastAPI(title="VRFunScriptCast AI subtitle stream bridge")

# 上游 audiocpp 的地址与模型 id **必须与 asr.audiocpp 段同源**（F01 复审）：
# 此前这里硬编码 `:8081`，而 audiocpp_backend 的 port 缺省是 8083 —— 配置里 port 一旦
# 缺失（UI 切换识别模型时曾把 asr.audiocpp 整段替换掉，见 host_server.save_subtitle_config
# 的深合并修复），离线路径去 8083、流式路径去 8081，**流式字幕整条失效**且界面无从察觉。
# 缺省端口/模型 id 只在 audiocpp_backend 定义一份，这里只做"读配置 + 兜底"。
try:
    from audiocpp_backend import DEFAULT_PORT as _ASR_DEFAULT_PORT
    from audiocpp_backend import STREAM_MODEL_ID as _ASR_STREAM_MODEL
except Exception:            # 依赖缺失也不能把流式桥挡在启动之前
    _ASR_DEFAULT_PORT, _ASR_STREAM_MODEL = 8083, "qwen3-asr-stream"


def _asr_upstream_default() -> str:
    """按 asr.audiocpp 的 host/port 组装上游 base（与离线路径同一份配置）。"""
    ac = (CFG.get("asr") or {}).get("audiocpp") or {}
    host = str(ac.get("host") or "127.0.0.1")
    try:
        port = int(ac.get("port", _ASR_DEFAULT_PORT))
    except (TypeError, ValueError):
        port = _ASR_DEFAULT_PORT
    return f"http://{host}:{port}"


ASR_BASE = _asr_upstream_default()
ASR_MODEL = _ASR_STREAM_MODEL

# 块边界碎片去重：重叠区被相邻块重复识别时，常切出上一块句子的**前缀碎片**
# （实测："今日。"、"てるんだ。"）。与最近几条已出句比对，新句是其中某条的
# 子串 → 判为碎片丢弃（真重复的短句如「はい」连发会被误杀，量极少可接受）。
_recent_ja: deque = deque(maxlen=6)
_recent_lang = ""

# 上一批的字幕上下文（原文+译文）：翻译提示词做剧情承接（server_app 离线路径同款）。
_last_ctx = {"lang": "", "src": "", "zh": ""}

# 标签提前补偿：块内比例摊时假设语音铺满整块，实测相对人工字幕整体偏早
# （全片评测中位 −2.7s），统一后移让"出现时刻"更贴近说话时刻。
_LABEL_OFFSET_MS = 2500


def _display_zh(seg: dict) -> str:
    """该段最终下发显示的中文。

    引擎多轮纠错/逐条兜底后译文仍夹假名的，会标记 error=untranslated_leak。
    隐藏判据用**假名计数 ≥2**，不能用"有没有汉字"——整句日文被掺进两个汉字
    （人名等）就绕过了汉字判据（实测开头第一句
    'こんにちは、三上悠亚です。今日。'因此天天上屏，真机用户反复看到）。
    ≥2 个假名＝实质未翻，置空让头显跳过；恰好 1 个假名的夹字句保留
    （"你家って真的很香"尚可读）。判据在 text_filters.count_kana，离线路径同用。
    """
    zh = (seg.get("translation") or "").strip()
    if seg.get("error") == "untranslated_leak" and count_kana(zh) >= 2:
        return ""
    return zh

_SENT_END = "。！？!?…♪"
_PAUSE_END = "、，,；;"


def _split_sentences(text: str, max_chars: int = 42) -> list:
    """把引擎给的原始缓冲切成句子。

    为什么必须切：流式增量是 ASR 的原始缓冲（约 5s 一整段），直接当一条字幕会出现
    "半句话就换行"（实测："51.2s 那穿着服装在直播的时候"）。这里按日文句末标点切，
    并用长度上限兜底（长句没有标点时按 pause 类标点再切一层）。
    """
    out, cur = [], ""
    for ch in text:
        cur += ch
        if ch in _SENT_END:
            if cur.strip():
                out.append(cur.strip())
            cur = ""
        elif len(cur) >= max_chars and ch in _PAUSE_END:
            if cur.strip():
                out.append(cur.strip())
            cur = ""
        elif len(cur) >= max_chars * 2:
            if cur.strip():
                out.append(cur.strip())
            cur = ""
    if cur.strip():
        out.append(cur.strip())
    return out


def _wav_wrap(pcm: bytes, sr: int = 16000, ch: int = 1, bits: int = 16) -> bytes:
    """上游目前只接受 WAV 容器（裸 PCM 会被 400 拒），这里在内存里加 44 字节头。"""
    n = len(pcm)
    hdr = (b"RIFF" + struct.pack("<I", 36 + n) + b"WAVE"
           + b"fmt " + struct.pack("<IHHIIHH", 16, 1, ch, sr, sr * ch * bits // 8, ch * bits // 8, bits)
           + b"data" + struct.pack("<I", n))
    return hdr + pcm


def _multipart(pcm: bytes, model: str) -> tuple[bytes, str]:
    boundary = "----vrfsc" + uuid.uuid4().hex
    out = bytearray()
    for name, value in (("model", model), ("stream", "true")):
        out += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
    out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.wav\"\r\n"
            f"Content-Type: audio/wav\r\n\r\n").encode()
    out += _wav_wrap(pcm)
    out += f"\r\n--{boundary}--\r\n".encode()
    return bytes(out), boundary


# 请求体上限：25s 块约 800KB，正常请求远远够用。与 server_app.MAX_BODY_BYTES 同值，
# 两个入口（/transcribe 与 /transcribe/stream）必须同一口径——此前只有 /transcribe
# 设了上限，而 /transcribe/stream 是**对局域网开放**的（头显流式模式直连它），
# 于是 `await request.body()` 成了无上限读入：一个不带 Content-Length 的分块请求
# 就能把服务进程读到 OOM。
MAX_BODY_BYTES = 100 * 1024 * 1024


async def read_capped_body(request: Request, limit: int = MAX_BODY_BYTES):
    """带上限读取请求体；超限返回 None（并且不把整段读进内存）。

    必须按块累加而不是先 `await request.body()` 再判长度：后者在判断之前
    已经把整个 body 读进内存了，上限形同虚设。分块传输（无 Content-Length）
    同样被这条挡住——`request.stream()` 是唯一的真相来源。
    """
    total = 0
    parts: list[bytes] = []
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        if total > limit:
            return None
        parts.append(chunk)
    return b"".join(parts)


@app.get("/health")
def health():
    return {"ok": True, "asr_base": ASR_BASE, "asr_model": ASR_MODEL,
            "batch_size": _batch_size()}


@app.post("/transcribe/stream")
async def transcribe_stream(request: Request, lang: str = "ja", translate: bool = True, video_start_ms: int = 0):
    pcm = await read_capped_body(request)
    if pcm is None:
        return JSONResponse(
            {"error": f"请求体超过上限 {MAX_BODY_BYTES // (1024 * 1024)}MB"},
            status_code=413)
    if len(pcm) % 2:
        pcm = pcm[:-1]
    if len(pcm) < 3200:
        async def _empty():
            yield "data: [DONE]\n\n"
        return StreamingResponse(_empty(), media_type="text/event-stream")

    url = urllib.parse.urlparse(ASR_BASE)
    batch_size = _batch_size()

    print(f"[stream] request {len(pcm)} bytes lang={lang} translate={translate}", flush=True)

    async def gen():
        t0 = time.perf_counter()
        # 块时长由 PCM 长度反推（16k / 单声道 / s16le），用于把整块时间按字数分摊到各句
        block_ms = max(0, len(pcm) // 2 * 1000 // 16000)
        # 单调游标：整个请求共用一条，只前进不回头。若各 delta 从块首重新分摊，
        # 同一块里多条句子的时间区间会互相重叠（实测第二个 delta 又从 video_start_ms 起算）。
        cursor_ms = video_start_ms
        # 攒批缓冲：切好的句子先入队，攒满 batch_size（或流结束）才整批翻译。
        pending: list[dict] = []
        first_ms = None
        dropped_rep = 0
        dropped_frag = 0
        logged = {"done": False}

        def _log_done():
            # done 日志只打一次：此前"错误/上游DONE/连接结束"三条路径各打一份，
            # 一次请求在日志里出现 3 份同样的 done，纯噪音。
            if not logged["done"]:
                logged["done"] = True
                print(f"[stream] done in {round((time.perf_counter() - t0) * 1000, 1)}ms "
                      f"dropped_rep={dropped_rep} dropped_frag={dropped_frag}", flush=True)

        def _queue(delta: str) -> None:
            """切句 + 清洗 + 过滤 + 分配逐句时间，入攒批缓冲。

            时间分摊：块内按**字数比例**摊块时长（旧版 130ms/字 会把整块句子压在
            块首——块越长错得越远，30s 块能把句子标早 15s+，与画面完全对不上，
            全片评测里 10s/30s 块的"识别召回"暴跌到 42%/27% 主要是这个伪影）。
            比例摊法假设语音均匀铺满块区间，静音段会让句子偏晚，但偏差被限制在
            块长以内，且"句子落在自己块里"永远成立；再整体后移 _LABEL_OFFSET_MS
            补偿实测的 2.7s 提前量。"""
            nonlocal cursor_ms, dropped_rep, dropped_frag
            global _recent_ja, _recent_lang
            if _recent_lang != lang:      # 换语言：碎片比对的参照清空
                _recent_ja.clear()
                _recent_lang = lang
            kept: list[str] = []
            for raw in _split_sentences(delta):
                s = strip_wrap_quotes(raw)
                if not s:
                    continue
                if has_repetition_loop(s):
                    dropped_rep += 1      # 喘息/拟声退化（"あ"x100），判据同离线路径
                    continue
                if any(len(r) > len(s) and s in r for r in _recent_ja) or \
                   any(len(k) > len(s) and s in k for k in kept):
                    dropped_frag += 1     # 上一块/本批句子的前缀碎片（重叠区重复识别）
                    continue
                kept.append(s)
            if not kept:
                return
            total_chars = sum(len(s) for s in kept) or 1
            block_end = video_start_ms + block_ms
            for s in kept:
                dur = block_ms * len(s) // total_chars
                cursor_ms = min(cursor_ms + dur, block_end)
                s0 = cursor_ms - dur + _LABEL_OFFSET_MS
                s1 = cursor_ms + _LABEL_OFFSET_MS
                if s1 <= s0:      # 比例时长被块尾截没（长句挤压）：至少给 400ms
                    s1 = s0 + 400
                if dur < 1500 and s1 - s0 < 1500 and s1 < block_end + _LABEL_OFFSET_MS:
                    # 最短显示 1.5s（旧 400ms 是"闪一下"的直接来源）；向后借时间，
                    # 但不越过块尾+偏移——挤压后面句子的空间有下限保护
                    s1 = min(block_end + _LABEL_OFFSET_MS, s0 + 1500)
                pending.append({"ja": s, "start_ms": s0, "end_ms": s1})
                _recent_ja.append(s)

        async def _flush_lines() -> list:
            """把攒到的句子整批交给原翻译引擎，返回要下发的 SSE 行（并清空缓冲）。

            翻译走线程池：translator 内部是阻塞 HTTP，直接在事件循环里调会把
            /health 等同循环端点一起卡住（上一版就是直接调的）。"""
            nonlocal first_ms
            if not pending:
                return []
            segs = [{"text": p["ja"], "start_ms": p["start_ms"], "end_ms": p["end_ms"]}
                    for p in pending]
            zhs = [""] * len(segs)
            tr = _get_translator() if translate else None
            if tr:
                tr_ctx = ""
                if _last_ctx["lang"] == lang and (_last_ctx["src"] or _last_ctx["zh"]):
                    tr_ctx = ("以下是上一句的原文与译文，仅供理解剧情衔接；不要翻译或输出它们：\n"
                              f"上一句原文：{_last_ctx['src']}\n上一句译文：{_last_ctx['zh']}")
                try:
                    await run_in_threadpool(tr.translate_segments, segs, lang, tr_ctx)
                    zhs = [_display_zh(x) for x in segs]
                except Exception as exc:
                    print(f"[stream] translate failed: {exc}", flush=True)
                # 更新滚动上下文：取时间上最后一条译文非空的段
                for s, z in zip(reversed(segs), reversed(zhs)):
                    if (z or "").strip():
                        _last_ctx.update({"lang": lang, "src": s.get("text", "")[:80],
                                          "zh": z[:80]})
                        break
            lines = []
            for p, zh in zip(pending, zhs):
                lines.append("data: " + json.dumps(
                    {"type": "delta", "ja": p["ja"], "zh": zh, "first_ms": first_ms,
                     "start_ms": p["start_ms"], "end_ms": p["end_ms"]},
                    ensure_ascii=False) + "\n\n")
            pending.clear()
            return lines

        conn = None
        try:
            payload, boundary = _multipart(pcm, ASR_MODEL)

            def _open_upstream():
                c = http.client.HTTPConnection(url.hostname, url.port or 80, timeout=600)
                c.request("POST", "/v1/audio/transcriptions", payload, {
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "Accept": "text/event-stream",
                })
                return c, c.getresponse()

            # 上游 HTTP 的连接/响应/逐块读取全是阻塞调用，**必须进线程池**：
            # 直接跑在事件循环里会把 /health 与并发的 /transcribe 一起卡住
            # （ASR 一跑就是数秒，本桥以 server_app 路由形式运行，共用一个循环）。
            conn, resp = await run_in_threadpool(_open_upstream)
            if resp.status != 200:
                yield f"data: {json.dumps({'type': 'error', 'error': f'upstream {resp.status}'})}\n\n"
                _log_done()
                yield "data: [DONE]\n\n"
                return
            buf = b""
            while True:
                chunk = await run_in_threadpool(
                    lambda: resp.read1(4096) if hasattr(resp, "read1") else resp.read(4096))
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line.startswith(b"data:"):
                        continue
                    data = line[5:].strip()
                    if data == b"[DONE]":
                        for out in await _flush_lines():
                            yield out
                        _log_done()
                        yield "data: [DONE]\n\n"
                        return
                    try:
                        ev = json.loads(data)
                    except Exception:
                        continue
                    etype = ev.get("type")
                    if not etype and ev.get("error"):
                        # 上游（audio.cpp）的错误事件没有 type 字段，不识别就会被静默忽略，
                        # 表现成"桥返回 [DONE] 但一句都没有"的哑失败（实测坏 WAV 时如此）。
                        _em = ev.get("error")
                        _emsg = _em.get("message") if isinstance(_em, dict) else str(_em)
                        print(f"[stream] upstream error: {_emsg}", flush=True)
                        for out in await _flush_lines():
                            yield out
                        yield "data: " + json.dumps(
                            {"type": "error", "error": _emsg or "upstream error"},
                            ensure_ascii=False) + "\n\n"
                        continue
                    if etype == "transcript.text.delta":
                        delta = ev.get("delta") or ""
                        if first_ms is None and delta.strip():
                            first_ms = round((time.perf_counter() - t0) * 1000, 1)
                        _queue(delta)
                        if len(pending) >= batch_size:
                            for out in await _flush_lines():
                                yield out
                    elif etype == "transcript.text.done":
                        for out in await _flush_lines():
                            yield out
                        yield "data: " + json.dumps(
                            {"type": "done", "ja": ev.get("text", ""), "timing": ev.get("timing"),
                             "total_ms": round((time.perf_counter() - t0) * 1000, 1)},
                            ensure_ascii=False) + "\n\n"
                    elif etype == "error":
                        for out in await _flush_lines():
                            yield out
                        yield "data: " + json.dumps(
                            {"type": "error", "error": (ev.get("error") or {}).get("message", "unknown")},
                            ensure_ascii=False) + "\n\n"
            conn.close()
            # 上游没发 [DONE] 就直接断开：把攒着的尾巴翻完再收尾
            for out in await _flush_lines():
                yield out
        except Exception as exc:
            try:
                if conn is not None:
                    conn.close()      # 异常路径此前不关连接，句柄靠 GC 兜底
            except Exception:
                pass
            try:
                for out in await _flush_lines():   # 断流前攒下的句子尽量翻完下发
                    yield out
            except Exception:
                pass
            yield "data: " + json.dumps({"type": "error", "error": str(exc)}, ensure_ascii=False) + "\n\n"
        _log_done()
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8757)
    ap.add_argument("--asr", default=ASR_BASE)
    ap.add_argument("--model", default=ASR_MODEL)
    a = ap.parse_args()
    ASR_BASE, ASR_MODEL = a.asr, a.model
    print(f"[bridge] listening :{a.port}  asr={ASR_BASE}  model={ASR_MODEL}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")
