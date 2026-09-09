# -*- coding: utf-8 -*-
"""字幕质量速览：段数、文本长度分布、短碎片占比、术语命中情况。

用法： .venv\\Scripts\\python.exe tests\quality_stats.py <stem>
"""
import json
import pathlib
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
stem = sys.argv[1] if len(sys.argv) > 1 else "FCVR-40-3_ja"
base = pathlib.Path(__file__).resolve().parent / "subtitle_out"
segs = json.loads((base / f"{stem}.json").read_text(encoding="utf-8"))

n = len(segs)
lens = [len(s["text"]) for s in segs]
mt_lens = [len(s.get("translation", "")) for s in segs]
tiny = [s for s in segs if len(s["text"]) <= 3]
no_mt = [s for s in segs if not s.get("translation")]
untranslated_kana = [s for s in segs
                     if s.get("translation") and re.search(r"[ぁ-んァ-ヶ]", s["translation"])]
same = [s for s in segs if s.get("translation") and s["translation"] == s["text"]]

print(f"段数            = {n}")
print(f"原文平均长度     = {sum(lens)/n:.1f} 字（最短 {min(lens)} / 最长 {max(lens)}）")
print(f"译文平均长度     = {sum(mt_lens)/n:.1f} 字")
print(f"≤3 字的碎片段    = {len(tiny)} ({len(tiny)*100//n}%)  ← 越少越好")
print(f"无译文的段       = {len(no_mt)}")
print(f"译文里仍含假名    = {len(untranslated_kana)}")
for s in untranslated_kana[:5]:
    print(f"    {s['text']}  →  {s['translation']}")
print(f"译文与原文完全相同 = {len(same)}")
print()
print("最长 5 段：")
for s in sorted(segs, key=lambda x: -len(x["text"]))[:5]:
    print(f"  [{s['start_ms']/1000:7.1f}] {s['text']}")
    print(f"            → {s.get('translation','')}")
