# -*- coding: utf-8 -*-
"""流程保真度分析：ASR 日文原文（ja）vs 流程最终输出字幕（zh）。

回答"识别到的原文有没有被流程丢掉/扭曲"：
  · 空译文     —— 观众什么都看不到（v1.6.9 起空译文直接不上屏，等于这一句没了）
  · 假名残留   —— 漏译，观众看到日文
  · 长度比     —— zh 字数 / ja 字数。中日互译正常约 0.4~0.9；
                  <0.25 疑似丢内容，>1.5 疑似复读/幻觉
  · 抄原文     —— zh 与 ja 完全相同（100% 漏译）
另外给多配置对比：各配置的行数、发声时长、请求耗时中位数。

用法：
  .venv/Scripts/python.exe tests/analyze_fidelity.py <a.json> [b.json ...] [--dump 15]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re

KANA = re.compile(r"[\u3041-\u309f\u30a0-\u30ff]")


def clean(s: str) -> str:
    return re.sub(r"[\s。，、！？!?.,；;：:「」『』…—－\-\"“”‘’()（）]", "", s or "")


def analyze(path: pathlib.Path, dump: int) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    segs = data.get("segments") or []
    meta = data.get("meta") or {}
    n = len(segs)
    print(f"\n===== {path.name}  （chunks={meta.get('chunks')} "
          f"chunk={meta.get('chunk_sec')}s/{meta.get('overlap_sec')}s） =====")
    if not n:
        print("  （无段）")
        return

    empty = [s for s in segs if not s["translation"].strip()]
    kana = [s for s in segs if s["translation"].strip() and KANA.search(s["translation"])]
    same = [s for s in segs if clean(s["translation"]) and clean(s["translation"]) == clean(s["text"])]
    ratios = []
    weird = []
    for s in segs:
        zt, jt = clean(s["translation"]), clean(s["text"])
        if not zt:
            continue
        r = len(zt) / max(1, len(jt))
        ratios.append(r)
        if (len(jt) >= 6 and r < 0.25) or r > 1.5:
            weird.append((r, s))
    ratios.sort()
    cov_s = sum(max(0, s["end_ms"] - s["start_ms"]) for s in segs) / 1000

    def pct(x: int) -> str:
        return f"{x:4d} ({x / n * 100:5.1f}%)"

    print(f"  段数={n}  发声时长={cov_s:.0f}s")
    print(f"  空译文（不上屏） {pct(len(empty))}")
    print(f"  假名残留        {pct(len(kana))}")
    print(f"  抄原文(zh==ja)  {pct(len(same))}")
    if ratios:
        med = ratios[len(ratios) // 2]
        low = sum(1 for r in ratios if r < 0.25)
        high = sum(1 for r in ratios if r > 1.5)
        print(f"  长度比 中位={med:.2f}  <0.25: {low}  >1.5: {high}")

    def show(tag: str, items: list, limit: int) -> None:
        for s in items[:limit]:
            print(f"    [{tag} @{s['start_ms']/1000:.0f}s] ja={s['text']}")
            print(f"        zh={s['translation']}")

    if empty:
        print(f"  —— 空译文样例（前 {min(dump, len(empty))}）——")
        show("空", empty, dump)
    if kana:
        print(f"  —— 假名残留样例（前 {min(dump, len(kana))}）——")
        show("假名", kana, dump)
    if weird:
        weird.sort(key=lambda x: abs(x[0] - 0.65), reverse=True)
        print(f"  —— 长度比异常样例（前 {min(dump, len(weird))}）——")
        for r, s in weird[:dump]:
            print(f"    [×{r:.2f} @{s['start_ms']/1000:.0f}s] ja={s['text']}")
            print(f"        zh={s['translation']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--dump", type=int, default=15)
    args = ap.parse_args()
    for p in args.inputs:
        analyze(pathlib.Path(p), args.dump)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
