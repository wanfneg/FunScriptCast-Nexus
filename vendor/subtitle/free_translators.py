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
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return r.read().decode("utf-8", "ignore")

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
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403) and not getattr(self, "_retried", False):
                self._retried = True
                self._token = ""
                return self.translate(texts, target)   # token 过期，刷新一次
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
    """

    def __init__(self, order=("google", "bing"), timeout: int = 15):
        self.order = tuple(order)
        self.timeout = timeout
        self.picked = None
        self.last_error = ""
        self.probe_log: list = []

    def _candidates(self):
        for kind in self.order:
            t = make_free(kind)
            if t is not None:
                yield kind, t

    def _pick(self):
        if self.picked is not None:
            return self.picked
        errs = []
        for kind, t in self._candidates():
            try:
                r = t.translate(["hello"], "zh")
                if r and (r[0] or "").strip():
                    self.picked = t
                    self.probe_log.append(f"{kind}=OK")
                    return t
                errs.append(f"{kind}: 探测返回空")
            except Exception as e:
                errs.append(f"{kind}: {type(e).__name__}: {e}")
            self.probe_log.append(f"{kind}=FAIL")
        self.last_error = "; ".join(errs)
        return None

    def translate(self, texts: list, target: str = "zh") -> list:
        t = self._pick()
        if t is None:
            raise RuntimeError("无可用免费后端 —— " + (self.last_error or "未配置"))
        return t.translate(texts, target)


def make_free(kind: str):
    """按名字创建免费后端。kind: auto | google | bing"""
    k = (kind or "").lower()
    if k in ("auto", ""):
        return AutoTranslator()
    if k in ("bing", "edge"):
        return BingTranslator()
    if k in ("google",):
        return GoogleTranslator()
    return None
