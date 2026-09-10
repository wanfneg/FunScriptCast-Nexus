# -*- coding: utf-8 -*-
"""对比两份字幕产物的重复退化与碎片率。用法： python cmp_quality.py <A.json> <B.json>"""
import json
import pathlib
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")


def stats(path: pathlib.Path) -> dict:
    segs = json.loads(path.read_text(encoding="utf-8"))
    joined = "".join(s.get("text", "") for s in segs)
    r10 = [m for m in re.finditer(r"(.)\1{9,}", joined)]
    r4 = [m for m in re.finditer(r"(.)\1{3,}", joined)]
    tiny = [s for s in segs if len(s.get("text", "")) <= 3]
    has_mt = [s for s in segs if s.get("translation")]
    return {
        "file": path.name,
        "segs": len(segs),
        "chars": len(joined),
        "avg": len(joined) / max(1, len(segs)),
        "runs_ge10": len(r10),
        "longest10": max((len(m.group(0)) for m in r10), default=0),
        "runs_ge4": len(r4),
        "longest4": max((len(m.group(0)) for m in r4), default=0),
        "tiny": len(tiny),
        "tiny_pct": round(len(tiny) * 100 / max(1, len(segs))),
        "with_mt": len(has_mt),
    }


rows = [stats(pathlib.Path(p)) for p in sys.argv[1:]]
keys = ["segs", "chars", "avg", "runs_ge10", "longest10", "runs_ge4", "longest4",
        "tiny", "tiny_pct", "with_mt"]
labels = {
    "segs": "段数", "chars": "总字符", "avg": "平均段长", "runs_ge10": "≥10连重复串",
    "longest10": "最长≥10连", "runs_ge4": "≥4连重复串", "longest4": "最长≥4连",
    "tiny": "≤3字碎片", "tiny_pct": "碎片占比%", "with_mt": "有译文段",
}
print(f"{'指标':<14}" + "".join(f"{r['file']:<28}" for r in rows))
for k in keys:
    line = f"{labels[k]:<14}"
    for r in rows:
        v = r[k]
        line += f"{v if not isinstance(v, float) else round(v,1):<28}"
    print(line)
