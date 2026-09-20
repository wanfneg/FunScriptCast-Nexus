# -*- coding: utf-8 -*-
"""出字延迟测量（R63.4）：量「一句话说完 → 它的文本首次随某个上传块返回」的间隔。

口径：emission_lag = 首次携带该段的块的音频末端(video_start_ms + 块长) − 段 end_ms。
实时推流时块的音频末端 ≈ 它到达的墙钟时刻，所以这个值 ≈ 句子说完到文本可用的等待。
不含 ASR/MT 处理耗时（两种切句模式各加一次同量级的处理，**差值**仍然成立）。

用法（服务起法与 run_eval.py 完全同源，:8759 隔离，随仓库 config 的切句模式跑）：
  .venv/Scripts/python.exe tests/measure_emission_lag.py --tag hybrid
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from run_eval import (EVAL_DIR, PCM2, PY, SUBTITLE_DIR, health_info,  # noqa: E402
                     local_code_sig, port_busy, wait_gone, wait_health)

SR = 16000
PORT = 8759
PCM2 = Path(r"E:/Development/_ref/eval/sivr002.pcm")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="lag")
    ap.add_argument("--sec", type=int, default=1254)
    args = ap.parse_args()

    import os
    import tempfile
    _iso = Path(tempfile.mkdtemp(prefix="nexus-lag-"))
    os.environ["NEXUS_USER_DIR"] = str(_iso)

    want_sig = local_code_sig()
    info = health_info()
    proc = None
    if info:
        if info.get("code_sig") == want_sig:
            print(f"[lag] 复用 :{PORT} 实例")
        else:
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(info.get("pid"))],
                               capture_output=True, timeout=15)
            except Exception:
                pass
            wait_gone()
    if not port_busy():
        proc = subprocess.Popen(
            [str(PY), "-m", "uvicorn", "server_app:app", "--host", "127.0.0.1",
             "--port", str(PORT)],
            cwd=str(SUBTITLE_DIR), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env=dict(os.environ))
    if not wait_health():
        print("服务未就绪", file=sys.stderr)
        return 2
    info = health_info()
    print(f"[lag] 服务就绪 segmentation={info.get('segmentation')} "
          f"asr={info.get('asr_backend')} mt_warm={info.get('mt_warm')}")

    raw = PCM2.read_bytes()
    total_n = min(len(raw) // 2, args.sec * SR)
    step, span = 32000, 48000            # 2s 步进、3s 块（生产行为）
    seen: dict = {}
    t0 = time.time()
    k = 0
    while k * step < total_n - span // 2:
        a = k * step
        chunk = raw[a * 2:(a + span) * 2]
        vstart = a * 1000 // SR
        url = (f"http://127.0.0.1:{PORT}/transcribe?lang=ja&video_start_ms={vstart}"
               f"&keep_from_ms={vstart + 500}&translate=true")
        req = urllib.request.Request(url, data=chunk, method="POST",
                                     headers={"Content-Type": "application/octet-stream"})
        with urllib.request.urlopen(req, timeout=120) as r:
            segs = json.loads(r.read().decode("utf-8")).get("segments") or []
        chunk_end_ms = vstart + span * 1000 // SR
        for s in segs:
            key = (s.get("start_ms"), s.get("end_ms"), s.get("text") or "")
            if key not in seen:
                seen[key] = chunk_end_ms
        k += 1
    wall = time.time() - t0

    lags = sorted(c - (s or 0) for (s, e, _), c in seen.items())
    n = len(lags)
    def pct(p: float) -> float:
        return lags[min(n - 1, int(n * p))] if n else 0
    stats = {
        "tag": args.tag, "segments": n, "chunks": k, "wall": round(wall, 1),
        "lag_median_ms": pct(0.5), "lag_mean_ms": round(sum(lags) / n) if n else 0,
        "lag_p90_ms": pct(0.9), "lag_max_ms": lags[-1] if n else 0,
        "lag_le_1000_pct": round(100 * sum(1 for x in lags if x <= 1000) / n, 1) if n else 0,
        "lag_le_2000_pct": round(100 * sum(1 for x in lags if x <= 2000) / n, 1) if n else 0,
    }
    print(json.dumps(stats, ensure_ascii=False, indent=1))
    out = EVAL_DIR / f"lag_{args.tag}.json"
    out.write_text(json.dumps({"stats": stats, "lags": lags}, ensure_ascii=False),
                   encoding="utf-8")
    print(f"[lag] 写出 {out}")
    if proc is not None:
        proc.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
