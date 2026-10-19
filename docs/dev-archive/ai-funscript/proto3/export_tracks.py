# -*- coding: utf-8 -*-
"""离线导出 StreamEngine 的全部轨道 + 生成的 funscript（用于静止段分析）。

用法：
    python export_tracks.py "E:/testvideo/funscript test/K1drsp.mp4" out/K1drsp

产物：
    <out>_tracks.npz    vmarg/level/env/blo/bhi/plat_top/plat_bot/rev_top/rev_bot
    <out>_stream.funscript   引擎最终发布的动作
    <out>_meta.json     行率、总行数、转码路径

为什么需要它：proto3/compare 里的旧缓存有多份是坏的
（K1drsp 只有前 171 秒有效、vmarg 几乎恒为 0），
静止段分析必须用**同一版引擎**重新跑一遍。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from stream_engine import StreamEngine  # noqa: E402


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    video = sys.argv[1]
    out = Path(sys.argv[2])
    out.parent.mkdir(parents=True, exist_ok=True)

    # 时长：引擎会用 ffprobe，这里传 None 让它自己探
    notes: list[str] = []

    def on_note(s: str) -> None:
        notes.append(s)
        print("[engine]", s, flush=True)

    # 时长必须显式给出（StreamEngine 用它算 total_rows）
    import subprocess
    pr = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", video],
        capture_output=True, text=True)
    dur_ms = float((pr.stdout or "0").strip() or 0) * 1000.0
    if dur_ms <= 0:
        raise SystemExit(f"ffprobe 读不到时长：{pr.stderr[:300]}")
    print(f"[export] 打开 {video} ({dur_ms/1000:.1f}s)", flush=True)
    eng = StreamEngine(video, duration_ms=dur_ms, device="cuda",
                       ckpt=None, on_note=on_note,
                       cache_dir=str(out.parent))
    eng.start()

    # 等到全片完成（或出错）
    t0 = time.time()
    last = -1
    while True:
        st = eng.buffer.state
        cov = eng.buffer.gen_until_ms
        if st in ("done", "error"):
            break
        if time.time() - t0 > 3600:
            print("[export] 超时（1h），按当前进度导出", flush=True)
            break
        if int(cov // 5000) != last:
            last = int(cov // 5000)
            print(f"[export] state={st} coverage={cov/1000:.0f}s", flush=True)
        time.sleep(2)

    print(f"[export] 结束 state={eng.buffer.state}", flush=True)

    # 轨道：只取已生成的稳定前缀（以 vmarg 最后一个有效行为准）
    vm = eng._trk.get("vmarg")
    rows = 0
    if vm is not None:
        ok = np.where(~np.isnan(vm))[0]
        rows = int(ok[-1]) + 1 if len(ok) else 0
    print(f"[export] 有效行 {rows} (n_rows={eng.n_rows})", flush=True)

    trk = {}
    for k, arr in eng._trk.items():
        trk[k] = np.asarray(arr[:rows], dtype=np.float32)
    trk["row_hz"] = np.float32(30.0)
    np.savez(str(out) + "_tracks.npz", **trk)

    # 动作
    with eng.buffer.lock:
        actions = list(eng.buffer.actions)
        gen_until = eng.buffer.gen_until_ms
    fs = {"actions": [{"at": int(round(a["at"])), "pos": int(round(a["pos"]))}
                      for a in actions]}
    (Path(str(out) + "_stream.funscript")).write_text(
        json.dumps(fs, ensure_ascii=False), encoding="utf-8")

    meta = {
        "video": video,
        "rows": rows,
        "row_hz": 30.0,
        "coverage_ms": gen_until,
        "n_actions": len(actions),
        "state": eng.buffer.state,
        "extract_video": getattr(eng, "extract_video", None),
        "notes": notes[-40:],
    }
    (Path(str(out) + "_meta.json")).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[export] 完成: {rows} 行, {len(actions)} 动作", flush=True)

    try:
        eng.stop(cleanup=False)
    except Exception as e:  # noqa: BLE001
        print("[export] stop 异常（忽略）:", e, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
