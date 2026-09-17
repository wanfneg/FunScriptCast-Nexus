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
path_in = base / f"{stem}.json"
if not path_in.exists():
    print(f"找不到 {path_in}")
    raise SystemExit(2)
data = json.loads(path_in.read_text(encoding="utf-8"))
# 兼容 {"segments":[...]} 与裸数组两种形状（两种产物脚本各写一种）
segs = data.get("segments") if isinstance(data, dict) else data
if not isinstance(segs, list) or not segs:
    # 空集时至少还能写个空文件，但必须让人知道不是"导出成功"
    print(f"{path_in} 里没有字幕段（形状={type(data).__name__}）")
    raise SystemExit(1)


def ts(ms: int) -> str:
    """毫秒 → 时间戳。

    以前只有 mm:ss，超过 60 分钟就把 4530s 打成 75:30，人工对照时无法定位
    （与 SRT/播放器的小时口径也不一致）。现在补上小时位。
    """
    h, rem = divmod(int(ms) // 1000, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}"


lines = [f"# {stem}  共 {len(segs)} 段", ""]
for i, s in enumerate(segs, 1):
    lines.append(f"[{i:03d}] {ts(s['start_ms'])}-{ts(s['end_ms'])}")
    lines.append(f"  ASR: {s['text']}")
    lines.append(f"  MT : {s.get('translation', '')}")
    lines.append("")

path = base / out_name
path.write_text("\n".join(lines), encoding="utf-8")
print(f"已写出 {path}（{len(segs)} 段）")
