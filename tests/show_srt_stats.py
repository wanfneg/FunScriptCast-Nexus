# -*- coding: utf-8 -*-
"""打印字幕产物统计。用法： .venv\\Scripts\\python.exe tests\\show_srt_stats.py <stem>"""
import json
import pathlib
import sys

sys.stdout.reconfigure(encoding="utf-8")
stem = sys.argv[1] if len(sys.argv) > 1 else "FCVR-40-3_ja"
base = pathlib.Path(__file__).resolve().parent / "subtitle_out"
segs = json.loads((base / f"{stem}.json").read_text(encoding="utf-8"))
print(f"字幕段数 = {len(segs)}")
print(f"时间跨度 = {segs[0]['start_ms']/1000:.0f}s ~ {segs[-1]['end_ms']/1000:.0f}s")
tr = [s for s in segs if s.get("translation")]
print(f"有译文 = {len(tr)} / {len(segs)}")
degen = [s for s in segs if s.get("error") == "translation_degenerate"]
print(f"被丢弃的退化译文 = {len(degen)}")
print("样例：")
for s in segs[:4] + segs[-4:]:
    print(f"  {s['start_ms']/1000:7.1f}  {s['text']}  →  {s.get('translation','')}")
