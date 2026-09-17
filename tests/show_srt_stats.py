# -*- coding: utf-8 -*-
"""打印字幕产物统计。用法： .venv\\Scripts\\python.exe tests\\show_srt_stats.py <stem>"""
import json
import pathlib
import sys

sys.stdout.reconfigure(encoding="utf-8")
stem = sys.argv[1] if len(sys.argv) > 1 else "FCVR-40-3_ja"
base = pathlib.Path(__file__).resolve().parent / "subtitle_out"
path = base / f"{stem}.json"
if not path.exists():
    print(f"找不到 {path}")
    raise SystemExit(2)
data = json.loads(path.read_text(encoding="utf-8"))
# 兼容 {"segments":[...]} 与裸数组两种形状：offline_to_json/stream_to_json 产出前者，
# run_full_with_cache 产出后者，旧代码只认裸数组会崩在 string indices 上。
segs = data.get("segments") if isinstance(data, dict) else data
if not isinstance(segs, list) or not segs:
    # 空集时 segs[0]/segs[-1] 直接 IndexError
    print(f"{path} 里没有字幕段（形状={type(data).__name__}）：无法统计")
    raise SystemExit(1)
print(f"字幕段数 = {len(segs)}")
print(f"时间跨度 = {segs[0]['start_ms']/1000:.0f}s ~ {segs[-1]['end_ms']/1000:.0f}s")
tr = [s for s in segs if s.get("translation")]
print(f"有译文 = {len(tr)} / {len(segs)}")
degen = [s for s in segs if s.get("error") == "translation_degenerate"]
print(f"被丢弃的退化译文 = {len(degen)}")
print("样例：")
for s in segs[:4] + segs[-4:]:
    print(f"  {s['start_ms']/1000:7.1f}  {s['text']}  →  {s.get('translation','')}")
