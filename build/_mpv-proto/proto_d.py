# 原型 D：mpv 独立建窗 → SetParent 挂进 pywebview 窗口
import os, ctypes, threading, time
os.environ["PATH"] = os.path.dirname(os.path.abspath(__file__)) + os.pathsep + os.environ["PATH"]
import mpv
import webview

user32 = ctypes.windll.user32
TITLE = "MPV-PROTO-D"
g = {"mpv_hwnd": None}

HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
body{margin:0;background:#0b0e14;color:#e8e8e8;overflow:hidden;font-family:sans-serif}
#bar{position:fixed;bottom:0;left:0;right:0;height:56px;background:#12161f;display:flex;align-items:center;padding:0 16px}
</style></head><body><div id="bar">底部控制条（页面元素）</div></body></html>"""

def find_hwnd_by_class(cls):
    result = []
    buf = ctypes.create_unicode_buffer(256)
    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(h, _):
        user32.GetClassNameW(h, buf, 256)
        if buf.value == cls:
            result.append(h)
            return False
        return True
    user32.EnumWindows(cb, 0)
    return result[0] if result else None

def run_mpv():
    for _ in range(60):
        parent = None
        buf = ctypes.create_unicode_buffer(256)
        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def cb2(h, _):
            user32.GetWindowTextW(h, buf, 256)
            if TITLE == buf.value:
                globals()["_pw"] = h
                return False
            return True
        user32.EnumWindows(cb2, 0)
        parent = globals().get("_pw")
        if parent:
            break
        time.sleep(0.5)
    m = mpv.MPV(hwdec="auto", keep_open="always", osc=False,
                title="proto-d-video")
    g["mpv"] = m
    m.play(r"O:\01-H-JVR\01-with funscript\CRVR\CRVR-194\CRVR-194-1.mp4")
    # 等 mpv 自己的窗口出现（按类名 "mpv" 找——title 会被媒体名覆盖）
    h = None
    for _ in range(80):
        h = find_hwnd_by_class("mpv")
        if h:
            break
        time.sleep(0.25)
    g["mpv_hwnd"] = h
    print(f"diag parent={parent} mpv_hwnd={h}", flush=True)
    if not h:
        print("RESULT mpv window not found", flush=True)
        os._exit(1)
    ret = user32.SetParent(h, parent)
    user32.SetWindowPos(h, 0, 0, 0, 1100, 700 - 56, 0x0040)
    after = user32.GetParent(h)
    print(f"diag SetParent ret={ret} after GetParent={after}", flush=True)
    m.time_pos = 11.0
    for _ in range(60):
        try:
            if m.time_pos and m.time_pos > 11.5:
                break
        except Exception:
            pass
        time.sleep(0.3)
    time.sleep(10)
    print(f"RESULT pos={m.time_pos}", flush=True)
    os._exit(0)

webview.create_window(TITLE, html=HTML, width=1100, height=700)
t = threading.Thread(target=run_mpv, daemon=True)
t.start()
webview.start()
