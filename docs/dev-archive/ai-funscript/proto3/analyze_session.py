# 会话分析: 幻影动作占比 + 密度/幅度/行程 + 低能量段定位
# 用法: analyze_session.py sessions/<时间戳目录>
import json
import statistics
import sys
from pathlib import Path

import numpy as np

sess = Path(sys.argv[1])
acts = json.load(open(sess / "actions.json", encoding="utf-8"))
meta = json.load(open(sess / "meta.json", encoding="utf-8"))
trk_f = sess / "tracks.npz"
if not trk_f.exists() or not acts["actions"]:
    print("会话数据不足(无轨道或无动作)")
    sys.exit(1)
trk = np.load(trk_f)
a = acts["actions"]
cover = a[-1]["at"] / 1000.0
rows = min(len(trk["vmarg"]), int(cover * 30))

# 每秒运动能量 + 自适应门(运动门已删, 此处仅作离线幻影归类)
in_low, in_high = [], []
if "motion" in trk:
    mot = trk["motion"][:rows]
    sec = {int(i / 30): float(np.mean(mot[max(0, i - 15):i + 15])) for i in range(0, rows, 15)}
    med = float(np.median(list(sec.values()))) or 1.0
    gate = 0.35 * med
    low = {s for s, e in sec.items() if e < gate}
    in_low = [x for x in a if int(x["at"] / 1000) in low]
    in_high = [x for x in a if int(x["at"] / 1000) not in low]
else:
    low = set()
amps = [abs(b["pos"] - x["pos"]) for x, b in zip(a, a[1:]) if b["at"] > x["at"]]
gaps = sorted((b["at"] - x["at"]) / 1000.0 for x, b in zip(a, a[1:]) if b["at"] > x["at"])

print(f"会话 {sess.name}: 覆盖 {cover:.0f}s, 引擎状态 {acts.get('engine_state', meta.get('engine_state'))}")
print(f"动作 {len(a)} 个 (密度 {len(a)/max(cover,1):.2f}/s), 发送帧 {meta.get('frames_sent')}")
if low:
    print(f"运动能量: 中位 {med:.2f}, 门限 {gate:.2f}, 低能量秒 {len(low)}s ({len(low)/max(cover,1):.0%})")
    print(f"低能量区动作: {len(in_low)} ({len(in_low)/max(len(a),1):.1%})  ← 幻影嫌疑")
    if len(in_high) > 1:
        print(f"高能量区动作: {len(in_high)}, 平均幅度 {statistics.mean(abs(b['pos']-x['pos']) for x,b in zip(in_high, in_high[1:]) if b['at']>x['at']):.1f}")
if gaps:
    print(f"间隔: 最大 {gaps[-1]:.1f}s, P95 {gaps[int(len(gaps)*0.95)]:.2f}s")

# 幻影最集中的连续段(前 5 个)
runs, cur = [], []
for x in a:
    if int(x["at"] / 1000) in low:
        cur.append(x)
    elif cur:
        runs.append(cur); cur = []
if cur:
    runs.append(cur)
runs.sort(key=len, reverse=True)
for r in runs[:5]:
    if len(r) >= 3:
        print(f"  幻影段: {r[0]['at']/1000:.0f}-{r[-1]['at']/1000:.0f}s, {len(r)} 动作")
