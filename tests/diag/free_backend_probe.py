# -*- coding: utf-8 -*-
"""免费翻译后端体检：逐个探测是否真的能翻出东西。

排查「兜底为什么没生效」时先跑这个——端点返回 200 不等于能翻译，
所以这里用一条真实的 "hello" 走完整链路，只有拿到非空译文才算可用。

用法：
    .venv\\Scripts\\python.exe tests\\diag\\free_backend_probe.py
"""
from __future__ import annotations

import pathlib
import sys
import time
import urllib.request

APP = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(APP / "vendor" / "subtitle"))

from free_translators import make_free  # noqa: E402

SAMPLE = ["こんにちは", "どっちのが好き？", "もう我慢できないんでしょう。"]


def probe_net() -> None:
    print(f"系统代理: {urllib.request.getproxies()}")
    for name, url in (
        ("edge auth (Bing token)", "https://edge.microsoft.com/translate/auth"),
        ("translate.google.com/m", "https://translate.google.com/m?tl=zh-CN&sl=auto&q=hello"),
        ("translate.googleapis.com", "https://translate.googleapis.com/translate_a/single?client=gtx&dt=t&q=hello"),
    ):
        t0 = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=12) as r:
                print(f"  {name:26s} HTTP {r.status}  {time.time() - t0:.2f}s")
        except Exception as e:
            print(f"  {name:26s} FAIL {type(e).__name__}: {e}")


def probe_backends() -> int:
    """返回可用后端数量。Bing 端点当前已知失效，只要还有别的能用就不算失败。"""
    ok_count = 0
    for kind in ("google", "bing", "auto"):
        t = make_free(kind)
        if t is None:
            print(f"  {kind:8s} 未实现")
            continue
        t0 = time.time()
        try:
            out = t.translate(list(SAMPLE), "zh")
        except Exception as e:
            print(f"  {kind:8s} FAIL {type(e).__name__}: {e}")
            continue
        dt = time.time() - t0
        ok = all((x or "").strip() for x in out)
        print(f"  {kind:8s} {'OK ' if ok else 'BAD'} {dt:.2f}s  "
              + " | ".join(f"{s}→{o}" for s, o in zip(SAMPLE, out)))
        if ok:
            ok_count += 1
        if kind == "auto":
            print(f"           探测记录 {getattr(t, 'probe_log', [])}  "
                  f"{getattr(t, 'last_error', '')}")
    return ok_count


def main() -> int:
    print("== 网络可达性 ==")
    probe_net()
    print("\n== 翻译后端 ==")
    ok = probe_backends()
    if ok:
        print(f"\n{ok} 个后端可用")
        return 0
    print("\n没有任何可用的免费后端——LLM 挂掉时字幕会整段空白")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
