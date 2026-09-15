# -*- coding: utf-8 -*-
"""免费翻译后端：Google（JSON 优先）+ Bing（Edge 翻译），无需 API Key。

**为什么要有这一层**：LLM（本地 Ollama / 云端）挂掉时字幕不能整段空白。
免费后端不用 Key、不占显存，是最低成本的保底。

实测结论（2026-09 本机 + Clash 代理）：

  - Google `translate.google.com/translate_a/single?client=gtx` → **可用**，
    返回结构化 JSON `[[["是的。","うん。",null,null,10]],null,"ja",...]`。
    注意它**只吃一个 q**（多 q 只返回第一个），所以"批量"靠并发实现。
  - Google `translate.google.com/m`（HTML 抓取）→ 可用，作 JSON 失败时的兜底。
  - Google `translate.googleapis.com/.../single?client=gtx` → **超时**（被墙），别用。
  - Bing `edge.microsoft.com/translate/auth` → **404，端点已失效**
    （直连 / 系统代理 / 显式代理都一样，不是代理问题）。
    Bing 代码保留以便端点恢复，但默认顺序里排在 Google 之后。

参考思路来自 VideoCaptioner（WEIFENG2333/VideoCaptioner）。
"""
from __future__ import annotations

import html
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BING_LANG = {"zh": "zh-Hans", "zh-CN": "zh-Hans", "ja": "ja", "en": "en"}
GOOGLE_LANG = {"zh": "zh-CN", "zh-CN": "zh-CN", "ja": "ja", "en": "en"}

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0")


class _Retryable(Exception):
    pass


# 免费端点的全局并发上限。免费接口对突发高频访问会 429/封 IP：多批并行兜底时
# 每批各开 6 线程，4 批就是 24 并发，必须用模块级信号量把总并发压住。
_FREE_NET_SEM = threading.Semaphore(6)

# 命中 429/5xx 时的一次退避重试等待（秒）
_RETRY_BACKOFF = 1.5


def _is_rate_limited(e: Exception) -> bool:
    """HTTP 429 / 5xx 属于可退避重试的瞬时错误。"""
    return isinstance(e, urllib.error.HTTPError) and e.code in (429, 500, 502, 503, 504)


# ---------------------------------------------------------------------- Google
class GoogleTranslator:
    """Google 免费端点：JSON 优先，HTML 兜底；并发补齐多段。"""

    JSON_API = "https://translate.google.com/translate_a/single"
    HTML_API = "https://translate.google.com/m"
    _HTML_RE = re.compile(r'class="(?:t0|result-container)">(.*?)<', re.S)

    def __init__(self, timeout: int = 15, workers: int = 6):
        self.timeout = timeout
        self.workers = workers

    def _get(self, url: str) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        # 全局信号量限流 + 对 429/5xx 做一次指数退避重试（零重试时端点抖动
        # 会直接让这一整批掉进空译文）
        with _FREE_NET_SEM:
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return r.read().decode("utf-8", "ignore")
            except urllib.error.HTTPError as e:
                if _is_rate_limited(e):
                    time.sleep(_RETRY_BACKOFF)
                    with _FREE_NET_SEM:
                        with urllib.request.urlopen(req, timeout=self.timeout) as r:
                            return r.read().decode("utf-8", "ignore")
                raise

    def _one_json(self, text: str, tl: str) -> str:
        q = urllib.parse.urlencode({"client": "gtx", "sl": "auto", "tl": tl,
                                    "dt": "t", "q": text[:5000]})
        data = json.loads(self._get(f"{self.JSON_API}?{q}"))
        segs = (data or [None])[0] or []
        return "".join(s[0] for s in segs if s and s[0]).strip()

    def _one_html(self, text: str, tl: str) -> str:
        q = urllib.parse.urlencode({"tl": tl, "sl": "auto", "q": text[:5000]})
        page = self._get(f"{self.HTML_API}?{q}")
        m = self._HTML_RE.findall(page)
        return html.unescape(m[0]).strip() if m else ""

    def one(self, text: str, tl: str) -> str:
        text = (text or "").strip()
        if not text:
            return ""
        try:
            out = self._one_json(text, tl)
            if out:
                return out
        except Exception:
            pass
        return self._one_html(text, tl)      # JSON 挂了再抓页面

    def translate(self, texts: list, target: str = "zh") -> list:
        if not texts:
            return []
        tl = GOOGLE_LANG.get(target, "zh-CN")
        with ThreadPoolExecutor(max_workers=max(1, min(self.workers, len(texts)))) as ex:
            return list(ex.map(lambda t: self.one(t, tl), texts))


# ------------------------------------------------------------------------ Bing
class BingTranslator:
    """Edge 翻译（免费、真批量）。

    注意：token 端点 `edge.microsoft.com/translate/auth` 当前返回 404，
    需等其恢复；`AutoTranslator` 会自动跳过它。”
    """

    AUTH = "https://edge.microsoft.com/translate/auth"
    API = "https://api-edge.cognitive.microsofttranslator.com/translate"

    def __init__(self, timeout: int = 20):
        self.timeout = timeout
        self._token = ""
        self._retried = False

    def _auth(self) -> None:
        req = urllib.request.Request(self.AUTH, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            self._token = r.read().decode("utf-8").strip()
        if not self._token:
            raise _Retryable("Bing auth 返回空 token")

    def translate(self, texts: list, target: str = "zh") -> list:
        if not texts:
            return []
        if not self._token:
            self._auth()
        to = BING_LANG.get(target, "zh-Hans")
        params = urllib.parse.urlencode({"to": to, "api-version": "3.0"})
        body = json.dumps([{"Text": (t or "")[:5000]} for t in texts],
                          ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.API}?{params}", data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self._token}",
                     "User-Agent": _UA})
        try:
            with _FREE_NET_SEM:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403) and not self._retried:
                self._retried = True
                self._token = ""
                try:
                    return self.translate(texts, target)   # token 过期，刷新一次
                finally:
                    self._retried = False   # 复位：长会话中 token 还会再过期
            raise
        out = [""] * len(texts)
        for i, item in enumerate(data):
            if i >= len(out):
                break
            try:
                out[i] = (item["translations"][0]["text"] or "").strip()
            except Exception:
                pass
        return out


# ------------------------------------------------------------------------ auto
class AutoTranslator:
    """按顺序探测可用后端，命中后固定使用（不再每次重试已失效的那个）。

    用一条 "hello" 做真实探测——只有真能翻出来才算可用，避免"端点通但不出译文"
    这种比直接失败更难查的情况。

    **负缓存是必须的**：全挂时必须记住"全挂"，否则每次调用都重新探测一遍。
    实测（代理关闭、Google 不可达）每次探测 30.4s，一批 3 轮 × 4 批 = 406s
    全耗在空探测上，而同一批文本 Ollama 只要 0.6s。冷却期内直接失败，
    等 LLM 那条路自己恢复。

    **探测必须并发且整体限时**：串行探测最坏是 timeout 之和——实测
    google 15s + bing 20s = 31s，字幕流程会因此整整卡半分钟。改成每个候选
    各起一个守护线程、整体只等 probe_timeout 秒，最坏 8s。
    """

    def __init__(self, order=("google", "bing"), timeout: int = 15,
                 probe_cooldown: float = 180.0, probe_timeout: float = 8.0):
        self.order = tuple(order)
        self.timeout = timeout
        self.probe_cooldown = float(probe_cooldown)
        self.probe_timeout = float(probe_timeout)
        self.picked = None
        self.last_error = ""
        self.probe_log: list = []
        self.probes = 0
        self.probe_seconds = 0.0
        self._dead_until = 0.0
        self._lock = threading.Lock()

    def _candidates(self):
        for kind in self.order:
            t = make_free(kind)
            if t is not None:
                yield kind, t

    def _pick(self):
        if self.picked is not None:
            return self.picked
        with self._lock:                       # 多线程同时要兜底时只探测一轮
            if self.picked is not None:
                return self.picked
            if time.monotonic() < self._dead_until:
                return None
            t = self._probe_all()
            if t is None:
                self._dead_until = time.monotonic() + self.probe_cooldown
                self._log(f"全部不可用，{self.probe_cooldown:.0f}s 内不再探测")
            else:
                self.picked = t
            return t

    def _probe_all(self):
        """并发探测所有候选后端，整体只等 probe_timeout 秒。"""
        cands = list(self._candidates())
        if not cands:
            self.last_error = "未配置任何免费后端"
            return None
        self.probes += 1
        t0 = time.monotonic()
        res: dict = {}
        errs: list = []
        lk = threading.Lock()

        def run(kind, t):
            try:
                r = t.translate(["hello"], "zh")
                ok = bool(r and (r[0] or "").strip())
            except Exception as e:
                ok = False
                with lk:
                    errs.append(f"{kind}: {type(e).__name__}: {e}")
            with lk:
                res[kind] = t if ok else None

        for k, t in cands:
            threading.Thread(target=run, args=(k, t), daemon=True,
                             name=f"probe-{k}").start()
        deadline = time.monotonic() + self.probe_timeout
        while time.monotonic() < deadline:
            with lk:
                if any(v is not None for v in res.values()):
                    break
                if len(res) >= len(cands):
                    break
            time.sleep(0.05)
        self.probe_seconds += time.monotonic() - t0

        for kind, _ in cands:                  # 按配置顺序取第一个成功的
            if res.get(kind) is not None:
                self._log(f"{kind}=OK")
                return res[kind]
            self._log(f"{kind}=FAIL")
        self.last_error = "; ".join(errs) or "全部探测超时或无译文"
        return None

    def _log(self, msg: str) -> None:
        self.probe_log.append(msg)
        if len(self.probe_log) > 40:          # 长视频会跑几百批，别让日志无限涨
            del self.probe_log[:-20]

    def translate(self, texts: list, target: str = "zh") -> list:
        t = self._pick()
        if t is None:
            raise RuntimeError("无可用免费后端 —— " + (self.last_error or "探测冷却中/未配置"))
        try:
            return t.translate(texts, target)
        except Exception:
            # 选中的后端这次挂了：清掉选择，下次重新探测（token 过期等）
            self.picked = None
            raise


_INSTANCES: dict = {}
_INSTANCES_LOCK = threading.Lock()


def make_free(kind: str):
    """按名字创建免费后端。kind: auto | google | bing

    实例按 kind 复用：探测/兜底会反复调用本函数，Google 每次新建实例会
    丢失退避状态，Bing 每次新建会白白重取 token。"""
    k = (kind or "").lower()
    if k in ("auto", ""):
        return AutoTranslator()
    if k in ("bing", "edge", "google"):
        with _INSTANCES_LOCK:
            inst = _INSTANCES.get(k)
            if inst is None:
                inst = BingTranslator() if k in ("bing", "edge") else GoogleTranslator()
                _INSTANCES[k] = inst
            return inst
    return None
