# -*- coding: utf-8 -*-
"""定位字幕里出现长重复的段。用法： python find_repeat.py <字幕.json> [阈值]"""
import json
import pathlib
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
path = pathlib.Path(sys.argv[1])
threshold = int(sys.argv[2]) if len(sys.argv) > 2 else 9
segs = json.loads(path.read_text(encoding="utf-8"))

found = 0
for i, s in enumerate(segs):
    t = s.get("text", "")
    m = re.search(r"(.)\1{%d,}" % threshold, t)
    if not m:
        continue
    found += 1
    print(f"段 #{i}  [{s.get('start_ms',0)/1000:.1f}s - {s.get('end_ms',0)/1000:.1f}s]  "
          f"重复字符 {m.group(1)!r} x {len(m.group(0))}")
    print(f"  原文: {t[:250]}")
    print(f"  译文: {(s.get('translation') or '')[:120]}")
    print()

print(f"共 {found} 段含 >={threshold} 连重复（总段数 {len(segs)}）")
