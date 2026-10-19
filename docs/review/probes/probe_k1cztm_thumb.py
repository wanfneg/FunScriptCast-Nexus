# -*- coding: utf-8 -*-
"""复现 K1cztm.mp4 缩略图抽帧失败：逐档位试并打印每步状态。"""
import sys
import time
from pathlib import Path

sys.path.insert(0, "vendor/player")
sys.path.insert(0, ".")
from library import ensure_dll  # noqa: E402（外挂播放器已删，ensure_dll 随探测逻辑在 library.py）

ensure_dll()
import mpv  # noqa: E402

VP = r"E:\testvideo\K1cztm.mp4"
OUT = Path("build/_thumb-probe")
OUT.mkdir(parents=True, exist_ok=True)

# 1) 时长（同 _probe_local 的第一步）
m = mpv.MPV(vo="null", ao="null", hwdec="auto", osc=False, input_default_bindings=False)
m.play(VP)
for _ in range(80):
    if m.duration:
        break
    time.sleep(0.25)
dur = m.duration
try:
    print("duration:", dur, "| video-format:", m.video_format, "| hwdec-current:", m.hwdec_current,
          "| w/h:", m.width, m.height)
except Exception as e:
    print("props err:", e)
m.terminate()

# 2) 抽帧（同 _probe_local 的两档 + 再补两档边角）
for frac in (0.12, 0.45, 0.05, 0.30):
    cand = OUT / "00000001.jpg"
    if cand.exists():
        cand.unlink()
    t0 = time.monotonic()
    t = mpv.MPV(vo="image", vo_image_format="jpg", vo_image_outdir=str(OUT),
                start=round(dur * frac, 1), frames=1, ao="null", osc=False,
                input_default_bindings=False)
    ok = False
    try:
        t.play(VP)
        for i in range(80):
            if cand.is_file():
                print(f"frac={frac}: OK after {time.monotonic()-t0:.1f}s, "
                      f"size={cand.stat().st_size}")
                ok = True
                break
            time.sleep(0.25)
        if not ok:
            # 超时后打印 mpv 视角的状态
            try:
                print(f"frac={frac}: TIMEOUT 20s | time-pos={t.time_pos} paused={t.pause} "
                      f"eof={t.eof_reached} idle={t.idle_active} core-idle={t.core_idle} "
                      f"vformat={t.video_format}")
            except Exception as e:
                print(f"frac={frac}: TIMEOUT + props err {e}")
    except Exception as e:
        print(f"frac={frac}: EXC {type(e).__name__}: {e}")
    finally:
        t.terminate()

print("outdir:", sorted(p.name for p in OUT.iterdir()))
