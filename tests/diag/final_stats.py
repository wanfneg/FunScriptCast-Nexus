# -*- coding: utf-8 -*-
"""打印字幕产物的最终统计。用法： python final_stats.py <字幕.json>"""
import json
import pathlib
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
segs = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
text = "".join(s.get("text", "") for s in segs)
tiny = [s for s in segs if len(s.get("text", "")) <= 3]
with_mt = [s for s in segs if s.get("translation")]
runs = {}
for n in (4, 6, 8, 10):
    ms = [m for m in re.finditer(r"(.)\1{%d,}" % (n - 1), text)]
    runs[n] = (len(ms), max((len(m.group(0)) for m in ms), default=0))

print(f"段数          {len(segs)}")
print(f"总字符        {len(text)}")
print(f"平均段长      {len(text)/max(1,len(segs)):.1f}")
print(f"≤3字碎片      {len(tiny)} ({len(tiny)*100//max(1,len(segs))}%)")
print(f"有译文        {len(with_mt)}")
for n, (c, l) in runs.items():
    print(f">={n} 连重复串  {c} (最长 {l})")
