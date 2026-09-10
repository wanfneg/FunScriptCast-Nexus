# -*- coding: utf-8 -*-
"""完整跑一遍视频的字幕链路，并走「缓存优先」流程。

第一次：整片跑 ASR+翻译 → 保存缓存 + SRT/JSON
第二次：直接命中缓存，秒出，不重跑 ASR

用法：
    .venv\\Scripts\\python.exe tests\\run_full_with_cache.py "D:\\video.mp4" [--lang ja] [--force]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
PY = APP_DIR / ".venv" / "Scripts" / "python.exe"
OUT_DIR = APP_DIR / "tests" / "subtitle_out"
LOG = APP_DIR / "tests" / "_full_log.txt"


def emit(line: str) -> None:
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def api_get(path: str, timeout: float = 30) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:8790{path}", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def api_post(path: str, payload: dict, timeout: float = 120) -> dict:
    req = urllib.request.Request(f"http://127.0.0.1:8790{path}", method="POST",
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def srt_time(ms: int) -> str:
    h, ms = divmod(int(ms), 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(segments: list, path: Path) -> None:
    lines = []
    for i, s in enumerate(segments, 1):
        lines += [str(i), f"{srt_time(s['start_ms'])} --> {srt_time(s['end_ms'])}",
                  s.get("translation", ""), s["text"], ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def add_segments(all_segs: list, new_segs: list) -> int:
    """跨块去重（与 vendor/tools/test_client.py 同逻辑）：时间重叠 + 文本相似 → 保留更长的。"""
    import difflib

    def sim(a, b):
        if a in b or b in a:
            return 1.0
        return difflib.SequenceMatcher(None, a, b).ratio()

    added = 0
    for s in new_segs:
        drop = False
        for i, p in enumerate(all_segs):
            if not (s["start_ms"] < p["end_ms"] and p["start_ms"] < s["end_ms"]):
                continue
            a, b = p["text"], s["text"]
            if sim(a, b) >= 0.6 or (len(a) > 0 and len(b) > 0 and (a in b or b in a)):
                if len(b) > len(a):
                    s["start_ms"] = min(p["start_ms"], s["start_ms"])
                    all_segs[i] = s
                drop = True
                break
        if not drop:
            all_segs.append(s)
            added += 1
    return added


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--lang", default="ja")
    ap.add_argument("--chunk", type=float, default=25.0)
    ap.add_argument("--overlap", type=float, default=2.0)
    ap.add_argument("--force", action="store_true", help="忽略缓存，强制重跑")
    args = ap.parse_args()

    video = Path(args.video)
    if not video.exists():
        emit(f"找不到视频：{video}")
        return 2
    ffmpeg = shutil.which("ffmpeg") or r"C:\Users\admin\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0-full_build\bin\ffmpeg.exe"

    # 时长
    probe = subprocess.run([ffmpeg, "-hide_banner", "-i", str(video)], capture_output=True, text=True)
    dur_s = 0.0
    for line in probe.stderr.splitlines():
        if "Duration:" in line:
            hms = line.split("Duration:")[1].split(",")[0].strip()
            h, m, s = hms.split(":")
            dur_s = int(h) * 3600 + int(m) * 60 + float(s)
            break
    emit(f"视频 {video.name} 时长 {dur_s/60:.1f} 分钟 | lang={args.lang} | chunk={args.chunk}s")

    host = subprocess.Popen([str(PY), str(APP_DIR / "host_server.py")], cwd=str(APP_DIR))
    try:
        for _ in range(60):
            time.sleep(0.5)
            try:
                api_get("/api/state", 3)
                break
            except Exception:
                continue

        vq = urllib.parse.quote(str(video))
        cache = api_get(f"/api/subtitle/cache?video={vq}&lang={args.lang}")
        emit(f"缓存查询：hit={cache.get('hit')} count={cache.get('count')}")

        OUT_DIR.mkdir(parents=True, exist_ok=True)
        stem = f"{video.stem}_{args.lang}"

        if cache.get("hit") and not args.force:
            segs = cache["segments"]
            emit(f"命中缓存，直接使用（{len(segs)} 段，生成于 {time.strftime('%Y-%m-%d %H:%M', time.localtime(cache.get('created_at') or 0))}）")
            write_srt(segs, OUT_DIR / f"{stem}_cached.srt")
            (OUT_DIR / f"{stem}_cached.json").write_text(
                json.dumps(segs, ensure_ascii=False, indent=2), encoding="utf-8")
            emit(f"已写出 {OUT_DIR / (stem + '_cached.srt')}")
            return 0

        # 未命中：跑完整链路
        api_post("/api/subtitle/start", {})
        t0 = time.time()
        sub = {}
        while time.time() - t0 < 180:
            time.sleep(3)
            sub = api_get("/api/state").get("subtitle", {})
            if sub.get("status") in ("ready", "error"):
                break
        if sub.get("status") != "ready":
            emit(f"字幕服务未就绪：{sub.get('status')} {sub.get('error')}")
            return 1
        emit(f"字幕服务就绪 | {json.dumps(sub.get('health'), ensure_ascii=False)}")

        step = max(0.5, args.chunk - args.overlap)
        all_segs: list = []
        t = 0.0
        idx = 0
        wall_start = time.time()
        total_asr = total_mt = 0.0
        speech_chunks = skipped_chunks = 0
        while t < dur_s - 0.05:
            dur = min(args.chunk, dur_s - t)
            cmd = [ffmpeg, "-v", "error", "-nostdin"]
            if t > 0:
                cmd += ["-ss", f"{t:.3f}"]
            cmd += ["-i", str(video), "-vn", "-ac", "1", "-ar", "16000", "-t", f"{dur:.3f}",
                    "-f", "s16le", "-"]
            p = subprocess.run(cmd, capture_output=True)
            if p.returncode != 0:
                emit(f"  ! ffmpeg 失败 @{t:.0f}s")
                t += step
                idx += 1
                continue
            start_ms = int(t * 1000)
            q = (f"?lang={args.lang}&video_start_ms={start_ms}&keep_from_ms=0&translate=true")
            req = urllib.request.Request("http://127.0.0.1:8756/transcribe" + q, data=p.stdout,
                                         headers={"Content-Type": "application/octet-stream"})
            try:
                with urllib.request.urlopen(req, timeout=600) as r:
                    d = json.loads(r.read().decode("utf-8"))
            except Exception as e:
                emit(f"  ! transcribe 失败 @{t:.0f}s: {e}")
                t += step
                idx += 1
                continue
            segs = d.get("segments", [])
            total_asr += d.get("asr_ms", 0) or 0
            total_mt += d.get("mt_ms", 0) or 0
            if d.get("skipped"):
                skipped_chunks += 1
            else:
                speech_chunks += 1
            added = add_segments(all_segs, segs)
            if segs:
                emit(f"#{idx:03d} [{t:7.1f}s] asr={d.get('asr_ms'):7.0f}ms mt={d.get('mt_ms'):6.0f}ms "
                     f"段={len(segs)} 累计={len(all_segs)}")
                for s in segs:
                    emit(f"      {s['start_ms']/1000:7.1f}  {s['text']}  →  {s.get('translation','')}")
            elif idx % 10 == 0:
                emit(f"#{idx:03d} [{t:7.1f}s] 静音跳过（累计 {len(all_segs)} 段）")
            t += step
            idx += 1

        wall = time.time() - wall_start
        emit(f"===== 整片完成：{idx} 块 / 语音块 {speech_chunks} / 静音块 {skipped_chunks} / "
             f"字幕 {len(all_segs)} 段 / 墙钟 {wall/60:.1f} 分钟")
        emit(f"      ASR 累计 {total_asr/1000:.1f}s，翻译累计 {total_mt/1000:.1f}s")

        saved = api_post("/api/subtitle/cache/save",
                         {"video": str(video), "lang": args.lang, "segments": all_segs,
                          "meta": {"chunks": idx, "wall_sec": round(wall, 1),
                                   "speech_chunks": speech_chunks}},
                         timeout=180)
        emit(f"缓存保存：{json.dumps(saved, ensure_ascii=False)}")

        write_srt(all_segs, OUT_DIR / f"{stem}.srt")
        (OUT_DIR / f"{stem}.json").write_text(
            json.dumps(all_segs, ensure_ascii=False, indent=2), encoding="utf-8")
        emit(f"已写出 {OUT_DIR / (stem + '.srt')}")

        # 翻译层统计：批量/缓存/纠错/兜底——衡量这次改造效果的依据
        try:
            with urllib.request.urlopen("http://127.0.0.1:8756/translate/stats",
                                        timeout=5) as r:
                tr = json.loads(r.read().decode("utf-8"))
            emit(f"翻译层：{tr.get('describe')}")
            emit(f"         {json.dumps(tr.get('stats'), ensure_ascii=False)}")
        except Exception as e:
            emit(f"翻译层统计不可用：{e}")

        api_post("/api/subtitle/stop", {}, timeout=60)
        emit("字幕服务已停止（显存释放）")
    finally:
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
