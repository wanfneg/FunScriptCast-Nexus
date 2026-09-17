# -*- coding: utf-8 -*-
"""字幕质量速览：段数、文本长度分布、短碎片占比、术语命中情况。

用法： .venv\\Scripts\\python.exe tests\\quality_stats.py <stem>
"""
import json
import pathlib
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
stem = sys.argv[1] if len(sys.argv) > 1 else "FCVR-40-3_ja"
base = pathlib.Path(__file__).resolve().parent / "subtitle_out"
path = base / f"{stem}.json"
if not path.exists():
    print(f"找不到 {path}")
    raise SystemExit(2)
data = json.loads(path.read_text(encoding="utf-8"))
# 兼容两种产物形状：run_full_with_cache 写裸数组，offline_to_json/stream_to_json
# 写 {"segments": [...]}。旧代码只认裸数组，喂它新产物会直接
# TypeError: string indices must be integers，报错还看不出是形状问题。
segs = data.get("segments") if isinstance(data, dict) else data
if not isinstance(segs, list) or not segs:
    # 空集时下面 sum()/n 会 ZeroDivisionError、排序取 [0] 会 IndexError
    print(f"{path} 里没有字幕段（形状={type(data).__name__}）：无法统计")
    raise SystemExit(1)

n = len(segs)
lens = [len(s["text"]) for s in segs]
mt_lens = [len(s.get("translation", "")) for s in segs]
tiny = [s for s in segs if len(s["text"]) <= 3]
no_mt = [s for s in segs if not s.get("translation")]
# 假名口径必须与 compare_with_reference.py 一致（[\u3041-\u309f\u30a0-\u30ff] 含
# 长音符 ー 与中点 ・）：用 [ぁ-んァ-ヶ] 会把「コーヒー」判成已翻译，
# 两份报告对同一条字幕给出相反结论。
untranslated_kana = [s for s in segs
                     if s.get("translation") and re.search(r"[\u3041-\u309f\u30a0-\u30ff]",
                                                           s["translation"])]
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
