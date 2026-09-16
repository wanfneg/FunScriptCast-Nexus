# -*- coding: utf-8 -*-
"""通过常驻 audiocpp_server 跑 VAD 分段 ASR，统计耗时与重复率。

用法： .venv\\Scripts\\python.exe tests\\diag\\audiocpp_server_test.py
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
URL = "http://127.0.0.1:8083/v1/audio/transcriptions"
SEGDIR = Path(r"E:\audiocpp-portable\tmp\vadcrop")
LOG = Path(__file__).resolve().parent.parent / "_srvtest_log.txt"


def emit(line: str) -> None:
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def transcribe(audio: Path, language: str = "Japanese") -> dict:
    # 只注册 streaming 模型（见 audiocpp_backend：双注册会双份驻留、挤爆 8GB 显存；
    # streaming 模型同样能吃不带 stream 的普通请求）
    body = json.dumps({"model": "qwen3-asr-stream", "audio": str(audio), "language": language}).encode()
    req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.loads(r.read().decode("utf-8"))
    d["wall_s"] = time.perf_counter() - t0
    return d


def main() -> int:
    segs = sorted(SEGDIR.glob("seg*.wav"))
    if not segs:
        emit(f"找不到分段音频：{SEGDIR}")
        return 1
    emit(f"共 {len(segs)} 段")

    texts, total_s, total_audio = [], 0.0, 0.0
    for s in segs:
        try:
            d = transcribe(s)
        except Exception as e:
            emit(f"  {s.name} 失败: {type(e).__name__}: {e}")
            continue
        t = (d.get("text") or "").strip()
        tm = d.get("timing") or {}
        total_s += d["wall_s"]
        total_audio += (tm.get("audio_duration_ms") or 0) / 1000.0
        if t:
            texts.append(t)
            emit(f"  {s.name}  {d['wall_s']:5.2f}s  rtf={tm.get('rtf', 0):.3f}  {t}")

    joined = "".join(texts)
    runs = [m for m in re.finditer(r"(.)\1{9,}", joined)]
    longest = max((len(m.group(0)) for m in runs), default=0)
    emit("")
    emit("===== 汇总 =====")
    emit(f"  有效段数        {len(texts)}")
    emit(f"  合并文本长度     {len(joined)}")
    emit(f"  ≥10 连重复串     {len(runs)}")
    emit(f"  最长连续重复     {longest}")
    emit(f"  总墙钟          {total_s:.1f}s（音频净时长 {total_audio:.1f}s，RTF {total_s/max(0.01,total_audio):.3f}）")
    emit(f"  平均每段        {total_s/max(1,len(texts)):.2f}s")
    emit("")
    emit("  文本：" + joined)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
