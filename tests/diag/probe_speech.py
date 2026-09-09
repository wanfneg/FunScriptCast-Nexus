# -*- coding: utf-8 -*-
"""探测视频中语音密集的片段：抽若干 25s 音频送 /transcribe，比较文本量。

用法： .venv\\Scripts\\python.exe tests\\probe_speech.py <视频> [偏移秒,偏移秒,...]
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
PY = APP_DIR / ".venv" / "Scripts" / "python.exe"
SR = 16000
LOG = APP_DIR / "tests" / "_probe_log.txt"


def emit(line: str) -> None:
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def extract_pcm(ffmpeg: str, path: Path, offset: float, dur: float) -> bytes:
    cmd = [ffmpeg, "-v", "error", "-nostdin"]
    if offset > 0:
        cmd += ["-ss", f"{offset:.3f}"]
    cmd += ["-i", str(path), "-vn", "-ac", "1", "-ar", str(SR), "-t", f"{dur:.3f}",
            "-f", "s16le", "-"]
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "ignore")[:300])
    return p.stdout


def post(pcm: bytes, start_ms: int) -> dict:
    q = f"?lang=ja&video_start_ms={start_ms}&keep_from_ms=0&translate=true"
    req = urllib.request.Request(
        "http://127.0.0.1:8756/transcribe" + q, data=pcm,
        headers={"Content-Type": "application/octet-stream"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.loads(r.read().decode("utf-8"))
    d["wall_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return d


def main() -> int:
    video = Path(sys.argv[1])
    offsets = [float(x) for x in (sys.argv[2].split(",") if len(sys.argv) > 2
                                  else ["60", "300", "600", "900", "1200", "1500"])]
    ffmpeg = shutil.which("ffmpeg") or r"C:\Users\admin\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0-full_build\bin\ffmpeg.exe"

    host = subprocess.Popen([str(PY), str(APP_DIR / "host_server.py")], cwd=str(APP_DIR))
    try:
        for _ in range(60):
            time.sleep(0.5)
            try:
                with urllib.request.urlopen("http://127.0.0.1:8790/api/state", timeout=3) as r:
                    r.read()
                break
            except Exception:
                continue
        req = urllib.request.Request("http://127.0.0.1:8790/api/subtitle/start", method="POST",
                                     data=b"{}", headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=20).read()
        t0 = time.time()
        sub = {}
        while time.time() - t0 < 180:
            time.sleep(3)
            with urllib.request.urlopen("http://127.0.0.1:8790/api/state", timeout=10) as r:
                sub = json.loads(r.read().decode("utf-8")).get("subtitle", {})
            if sub.get("status") in ("ready", "error"):
                break
        emit(f"字幕服务: {sub.get('status')} err={sub.get('error')}")

        for off in offsets:
            pcm = extract_pcm(ffmpeg, video, off, 25.0)
            d = post(pcm, int(off * 1000))
            segs = d.get("segments", [])
            chars = sum(len(s.get("text", "")) for s in segs)
            emit(f"--- 偏移 {off:6.0f}s  asr={d.get('asr_ms')}ms mt={d.get('mt_ms')}ms "
                 f"段数={len(segs)} 字符={chars}{' [静音]' if d.get('skipped') else ''}")
            for s in segs:
                emit(f"      {s['start_ms']/1000:7.1f}  {s['text']}")
                if s.get("translation"):
                    emit(f"                 → {s['translation']}")
        req = urllib.request.Request("http://127.0.0.1:8790/api/subtitle/stop", method="POST",
                                     data=b"{}", headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=30).read()
        emit("字幕服务已停止")
    finally:
        try:
            req = urllib.request.Request("http://127.0.0.1:8790/api/quit", method="POST",
                                         data=b"{}", headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5).read()
        except Exception:
            pass
        time.sleep(3)
        if host.poll() is None:
            host.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
