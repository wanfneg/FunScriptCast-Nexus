# 对照实验：独立 mpv 窗口（无 wid 嵌入）播同一段，seek 到 11s
import os, time
os.environ["PATH"] = os.path.dirname(os.path.abspath(__file__)) + os.pathsep + os.environ["PATH"]
import mpv
p = mpv.MPV(hwdec="auto", keep_open="always", geometry="1100x700+900+300", osc=False)
p.play(r"O:\01-H-JVR\01-with funscript\CRVR\CRVR-194\CRVR-194-1.mp4")
p.wait_until_playing(timeout=30)
p.time_pos = 11.0
time.sleep(10)
print(f"CTRL pos={p.time_pos}", flush=True)
p.terminate()
