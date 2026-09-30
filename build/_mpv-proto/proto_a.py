# 原型 A：libmpv 冒烟——载入、开音频空跑、探测 HEVC 解码器存在
import os, sys
os.environ["PATH"] = os.path.dirname(os.path.abspath(__file__)) + os.pathsep + os.environ["PATH"]
import mpv

p = mpv.MPV(vo="null", ao="null", hwdec="auto")
import time
t0 = time.time()
p.play(r"O:\01-H-JVR\01-with funscript\CRVR\CRVR-194\CRVR-194-1.mp4")
p.wait_until_playing(timeout=30)
time.sleep(3)
vf = p.video_format
hw = p.hwdec_current
tp = p.time_pos
fps = p.container_fps
speed_ok = None
# 取解码速度：等 3 秒看 time-pos 前进
t1, t2 = p.time_pos, None
time.sleep(3)
t2 = p.time_pos
p.terminate()
print(f"video_format={vf} hwdec_current={hw} fps={fps} time {t1}->{t2} (advancing={t2>t1})")
print("PASS" if (vf and t2 and t2 > t1) else "FAIL")
