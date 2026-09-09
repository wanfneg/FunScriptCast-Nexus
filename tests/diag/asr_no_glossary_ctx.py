# -*- coding: utf-8 -*-
"""定位 ASR 25s 尖峰：关掉 ASR 热词上下文（780 字符 / 200 条）后重测同一段音频。

对照组：tests/asr_ab.py 默认配置下该段 ASR ≈ 25s。
用法： .venv\\Scripts\\python.exe tests\\asr_no_glossary_ctx.py <视频> <偏移秒>
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
PY = APP_DIR / ".venv" / "Scripts" / "python.exe"
CFG = APP_DIR / "vendor" / "subtitle" / "config.json"
LOG = APP_DIR / "tests" / "_nctx_log.txt"


def emit(line: str) -> None:
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def api_post(path: str, payload: dict, timeout: float = 30) -> dict:
    req = urllib.request.Request(f"http://127.0.0.1:8790{path}", method="POST",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def api_get(path: str, timeout: float = 10) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:8790{path}", timeout=timeout) as r:
        return json.loads(r.read().decode())


def extract_pcm(ffmpeg: str, path: Path, offset: float, dur: float) -> bytes:
    cmd = [ffmpeg, "-v", "error", "-nostdin"]
    if offset > 0:
        cmd += ["-ss", f"{offset:.3f}"]
    cmd += ["-i", str(path), "-vn", "-ac", "1", "-ar", "16000", "-t", f"{dur:.3f}",
            "-f", "s16le", "-"]
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "ignore")[:300])
    return p.stdout


def transcribe(pcm: bytes, start_ms: int, translate: bool = False) -> dict:
    q = f"?lang=ja&video_start_ms={start_ms}&keep_from_ms=0&translate={'true' if translate else 'false'}"
    req = urllib.request.Request("http://127.0.0.1:8756/transcribe" + q, data=pcm,
                                 headers={"Content-Type": "application/octet-stream"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.loads(r.read().decode())
    d["wall_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return d


def main() -> int:
    video = Path(sys.argv[1])
    off = float(sys.argv[2]) if len(sys.argv) > 2 else 600.0
    ffmpeg = shutil.which("ffmpeg") or r"C:\Users\admin\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0-full_build\bin\ffmpeg.exe"

    # 关掉 ASR 热词上下文（改配置要在服务启动前生效）
    backup = CFG.with_suffix(".json.bak-nctx")
    shutil.copy2(CFG, backup)
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    cfg["asr"]["use_glossary_context"] = False
    CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    emit("已临时关闭 asr.use_glossary_context")

    host = subprocess.Popen([str(PY), str(APP_DIR / "host_server.py")], cwd=str(APP_DIR))
    try:
        for _ in range(60):
            time.sleep(0.5)
            try:
                api_get("/api/state", 3)
                break
            except Exception:
                continue
        api_post("/api/subtitle/start", {})
        t0 = time.time()
        sub = {}
        while time.time() - t0 < 180:
            time.sleep(3)
            sub = api_get("/api/state").get("subtitle", {})
            if sub.get("status") in ("ready", "error"):
                break
        emit(f"字幕服务: {sub.get('status')}")

        pcm = extract_pcm(ffmpeg, video, off, 25.0)
        for label, tr in (("无热词上下文 只ASR", False), ("无热词上下文 ASR+翻译", True)):
            d = transcribe(pcm, int(off * 1000), translate=tr)
            emit(f"{label}: asr={d.get('asr_ms')}ms mt={d.get('mt_ms')}ms wall={d['wall_ms']}ms 段数={len(d.get('segments', []))}")
            for s in d.get("segments", []):
                emit(f"      {s['text']}  →  {s.get('translation', '')}")
        api_post("/api/subtitle/stop", {}, timeout=30)
    finally:
        shutil.move(str(backup), str(CFG))
        emit("已恢复 config.json")
        try:
            api_post("/api/quit", {}, timeout=5)
        except Exception:
            pass
        time.sleep(3)
        if host.poll() is None:
            host.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
