# -*- coding: utf-8 -*-
"""把整片字幕导成方便人工对照的文本（时间 / 原文 / 译文）。

用法： .venv\\Scripts\\python.exe tests\export_compare.py <stem> [输出文件名]
"""
import json
import pathlib
import sys

sys.stdout.reconfigure(encoding="utf-8")
stem = sys.argv[1] if len(sys.argv) > 1 else "FCVR-40-3_ja"
out_name = sys.argv[2] if len(sys.argv) > 2 else f"{stem}_对照.txt"

base = pathlib.Path(__file__).resolve().parent / "subtitle_out"
segs = json.loads((base / f"{stem}.json").read_text(encoding="utf-8"))


def ts(ms: int) -> str:
    m, s = divmod(int(ms) // 1000, 60)
    return f"{m:02d}:{s:02d}"


lines = [f"# {stem}  共 {len(segs)} 段", ""]
for i, s in enumerate(segs, 1):
    lines.append(f"[{i:03d}] {ts(s['start_ms'])}-{ts(s['end_ms'])}")
    lines.append(f"  ASR: {s['text']}")
    lines.append(f"  MT : {s.get('translation', '')}")
    lines.append("")

path = base / out_name
path.write_text("\n".join(lines), encoding="utf-8")
print(f"已写出 {path}（{len(segs)} 段）")
