# 原型 C（真分叉点）：libmpv wid 嵌入 pywebview 窗口
import os, sys, ctypes, threading, time
os.environ["PATH"] = os.path.dirname(os.path.abspath(__file__)) + os.pathsep + os.environ["PATH"]
import mpv
import webview

TITLE = "MPV-PROTO"
g = {"mpv": None, "hwnd": None, "error": None}

HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
body{margin:0;background:#0b0e14;color:#e8e8e8;font-family:sans-serif;overflow:hidden}
#bar{position:fixed;bottom:0;left:0;right:0;height:56px;background:#12161f;display:flex;align-items:center;padding:0 16px;gap:12px}
#video{position:fixed;top:0;left:0;right:0;bottom:56px;background:#000}
button{background:#1f2733;color:#fff;border:0;padding:8px 18px;border-radius:6px;font-size:14px}
</style></head><body>
<div id="video"></div>
<div id="bar"><button onclick="pywebview.api.toggle()">播放/暂停</button><span id="st">初始化…</span></div>
<script>function setst(s){document.getElementById('st').textContent=s}</script>
</body></html>"""

user32 = ctypes.windll.user32

def find_hwnd_by_title(title):
    hwnd = user32.FindWindowW(None, title)
    return hwnd or None

class Api:
    def toggle(self):
        m = g["mpv"]
        if m:
            m.pause = not m.pause
        return "ok"

def run_mpv():
    try:
        # 等 pywebview 窗口出现
        for _ in range(60):
            hwnd = find_hwnd_by_title(TITLE)
            if hwnd:
                break
            time.sleep(0.5)
        if not hwnd:
            g["error"] = "找不到 pywebview 窗口"
            return
        g["hwnd"] = hwnd
        m = mpv.MPV(wid=str(hex(hwnd)), hwdec="auto", keep_open="always", input_default_bindings=False, osc=False)
        g["mpv"] = m
        m.play(r"O:\01-H-JVR\01-with funscript\CRVR\CRVR-194\CRVR-194-1.mp4")
        for _ in range(60):
            try:
                if m.time_pos is not None and m.time_pos > 1:
                    break
            except Exception:
                pass
            time.sleep(0.5)
        # 用 JS API 把窗口 rect 告诉页面（模拟视频区矩形对齐）
        for _ in range(60):
            try:
                webview.windows[0].evaluate_js("setst('mpv 已嵌入，正在播放 HEVC')")
                break
            except Exception:
                time.sleep(0.5)
        time.sleep(10)   # 播放 10 秒（截图1 截图2 对比帧）
        g["done"] = True
        print(f"RESULT video={m.video_format} hwdec={m.hwdec_current} pos={m.time_pos}", flush=True)
        os._exit(0)      # 直接收尾：webview 阻塞主线程，不等了
    except Exception as e:
        g["error"] = f"{type(e).__name__}: {e}"
        print("error:", g["error"], flush=True)
        os._exit(1)

webview.create_window(TITLE, html=HTML, width=1100, height=700, js_api=Api())
t = threading.Thread(target=run_mpv, daemon=True)
t.start()
webview.start()
t.join(timeout=5)
print("error:", g["error"])
m = g["mpv"]
if m:
    try:
        print(f"video={m.video_format} hwdec={m.hwdec_current} pos={m.time_pos}")
        m.terminate()
    except Exception:
        pass
