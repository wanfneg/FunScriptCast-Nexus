"""流式引擎冒烟测试: 独立跑 N 秒, 打印提取/生成进度, 最后导出已生成动作。

产出两处:
  stream_partial.funscript  —— benchmark.py 读的中间产物(保持原样)
  sessions/<ts>/            —— 与 main_stream.py 同构的完整会话存档
                               (tracks.npz + actions.json + meta.json),
                               便于用 ab_*.py 离线复现/核对样式解码。
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from stream_engine import ROW_HZ, StreamEngine

VIDEO = r"E:\testvideo\funscript test\K1drsp.mp4"
DURATION_MS = 1365320.0
RUN_SEC = float(sys.argv[1]) if len(sys.argv) > 1 else 150.0


def note(s):
    print(f"[{time.strftime('%H:%M:%S')}] {s}", flush=True)


eng = StreamEngine(VIDEO, DURATION_MS, device="cuda", on_note=note)
eng.start()
t0 = time.time()
# 模拟 1x 播放头(用户立即按播放): 节流按真实消费速度放行提取
last_state = ""
last_ph = -1.0
while time.time() - t0 < RUN_SEC:
    time.sleep(5)
    ph = max(0.0, (time.time() - t0) - 12.0) * 1000.0   # 扣除加载期, 模拟开播
    eng.set_playhead(ph)
    actions, gen_until = eng.buffer.snapshot()
    line = (f"rows={eng.n_rows} ({eng.n_rows/ROW_HZ:.0f}s 视频) "
            f"state={eng.buffer.state} actions={len(actions)} "
            f"gen_until={gen_until/1000:.0f}s playhead={ph/1000:.0f}s")
    if line != last_state:
        print(f"[{time.time()-t0:6.0f}s] {line}", flush=True)
        last_state = line
    if eng.buffer.state == "done":
        break

actions, gen_until = eng.buffer.snapshot()
out = {"version": "1.0", "inverted": False, "range": 100,
       "actions": [{"at": int(round(a["at"])), "pos": int(a["pos"])} for a in actions]}
with open("stream_partial.funscript", "w") as f:
    json.dump(out, f)

# 完整会话存档(与 main_stream.py 的导出同构): 离线复现样式解码要 tracks
sess = Path(__file__).resolve().parent / "sessions" / \
    ("smoke_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
sess.mkdir(parents=True, exist_ok=True)
trk = getattr(eng, "_trk", None)
if trk:
    np.savez(sess / "tracks.npz", **trk, times_ms=np.asarray(eng.times_ms))
json.dump({"video": VIDEO, "device": "smoke", "n_actions": len(actions),
           "gen_until_ms": gen_until,
           "coverage_s": actions[-1]["at"] / 1000.0 if actions else 0.0,
           "actions": actions},
          open(sess / "actions.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
json.dump({"video": VIDEO, "run_sec": RUN_SEC, "rows": int(eng.n_rows),
           "rev_bias_rows": getattr(eng, "_rev_bias_rows", 0),
           "engine_state": eng.buffer.state, "note": eng.buffer.note},
          open(sess / "meta.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
print(f"会话已存: {sess}")
from stream_engine import PERF
print(f"\n导出 stream_partial.funscript: {len(actions)} 动作, "
      f"覆盖至 {gen_until/1000:.0f}s / 全片 {DURATION_MS/1000:.0f}s")
print(f"耗时打点: flush(含h0)={PERF['flush']:.0f}s decode={PERF['decode']:.0f}s")
eng.stop(cleanup=True)
