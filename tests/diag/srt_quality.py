# -*- coding: utf-8 -*-
"""统计字幕质量指标，用于比较两次翻译层改动的效果。

指标：
  - 空白译文：兜底/LLM 都没出东西（最严重）
  - 漏译：中文译文里残留假名，或冒出原文没有的英文单词
  - 退化：同一字符连续刷屏
  - 平均译文长度：过低通常意味着整段被吞

用法：
    .venv\\Scripts\\python.exe tests\\diag\\srt_quality.py tests\\subtitle_out\\xxx.json [more.json ...]
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
from collections import Counter

KANA = re.compile(r"[\u3041-\u309f\u30a0-\u30ff]")
LATIN = re.compile(r"[A-Za-z]{2,}")
REPEAT = re.compile(r"(.)\1{7,}")


def load(path: pathlib.Path) -> list:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("segments") or []
    return data


def is_degenerate(src: str, tr: str) -> bool:
    """与 translate_engine._is_degenerate 同一口径：译文放大 3 倍以上且单字占比 >60%。

    不能只查 `(.)\\1{7,}`——原文本身就是 `ああああああ` 这种拟声词时，
    译文 `啊啊啊啊啊` 是完全正确的，会被误报。
    """
    if not tr or len(tr) <= max(6, len(src) * 3):
        return False
    top = Counter(tr).most_common(1)[0][1]
    return top / len(tr) > 0.6


def analyse(segs: list) -> dict:
    r = {"total": len(segs), "empty": 0, "kana": [], "latin": [], "degen": [],
         "src_chars": 0, "tr_chars": 0, "no_kana_src": 0}
    for s in segs:
        src = (s.get("text") or "").strip()
        tr = (s.get("translation") or "").strip()
        r["src_chars"] += len(src)
        r["tr_chars"] += len(tr)
        if not tr:
            r["empty"] += 1
            continue
        if KANA.search(tr):
            r["kana"].append((src, tr))
        elif LATIN.search(tr) and not LATIN.search(src):
            r["latin"].append((src, tr))
        if is_degenerate(src, tr):
            r["degen"].append((src, tr))
    # 完全没有假名的原文 = ASR 只出了汉字/符号，译文没假名是正常的
    r["no_kana_src"] = sum(1 for s in segs if not KANA.search(s.get("text") or ""))
    return r


def report(path: pathlib.Path) -> None:
    segs = load(path)
    r = analyse(segs)
    n = max(1, r["total"])
    print(f"\n===== {path.name} =====")
    print(f"  段数 {r['total']}   原文 {r['src_chars']} 字   译文 {r['tr_chars']} 字"
          f"   （原文无假名的段 {r['no_kana_src']}）")
    print(f"  平均译文 {r['tr_chars'] / n:.1f} 字/段")
    print(f"  空白译文      {r['empty']:3d}  ({r['empty'] / n * 100:.1f}%)")
    print(f"  漏译·残留假名 {len(r['kana']):3d}  ({len(r['kana']) / n * 100:.1f}%)")
    print(f"  漏译·夹英文   {len(r['latin']):3d}  ({len(r['latin']) / n * 100:.1f}%)")
    print(f"  退化重复      {len(r['degen']):3d}")
    for label in ("kana", "latin", "degen"):
        for src, tr in r[label][:6]:
            print(f"    [{label}] {src}  →  {tr}")


def main() -> int:
    if len(sys.argv) < 2:
        out = pathlib.Path(__file__).resolve().parent.parent / "subtitle_out"
        files = sorted(out.glob("*.json"))
    else:
        files = [pathlib.Path(a) for a in sys.argv[1:]]
    if not files:
        print("没有可分析的文件")
        return 2
    for f in files:
        if f.exists():
            report(f)
        else:
            print(f"跳过（不存在）：{f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
