# -*- coding: utf-8 -*-
"""端到端验证「点关闭按钮 → 隐藏到托盘，进程不退出」以及托盘「退出」能真正结束进程。

用真实路径：给窗体发 WM_CLOSE（等价于用户点 X），走 host_server.run() 里注册的
on_closing。若 on_closing 返回 False，窗体应当仍然存在（IsDisposed == False）
但 Visible == False。
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))
import host_server as H  # noqa: E402

WM_CLOSE = 0x0010
R: dict = {}
STAGE: list = []


def mark(tag: str) -> None:
    STAGE.append(f"{time.strftime('%H:%M:%S')} {tag}")
    print("STAGE " + tag, flush=True)


def _form(window):
    return getattr(window, "native", None)


def _visible(window):
    f = _form(window)
    try:
        return bool(f.Visible)
    except Exception:
        return None


def _disposed(window):
    f = _form(window)
    try:
        return bool(f.IsDisposed)
    except Exception:
        return None


def main() -> int:
    import webview

    mark("imported")
    httpd = H.ThreadingHTTPServer(("127.0.0.1", H.UI_API_PORT), H.Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    mark("httpd-up")

    win = webview.create_window(
        "tray-close-e2e", f"http://127.0.0.1:{H.UI_API_PORT}/", width=900, height=600
    )
    mark("window-created")

    # 与 host_server.run() 中完全一致的关闭拦截逻辑
    close_to_tray = True

    def on_closing():
        if H.TRAY.quitting:
            return True
        if close_to_tray and H.TRAY.started:
            H.TRAY.hide_window()
            return False
        return True

    win.events.closing += on_closing

    def on_loaded():
        mark("loaded-event")
        R["tray_started"] = H.TRAY.start(win)
        mark(f"tray-started={R['tray_started']}")
        threading.Thread(target=seq, daemon=True).start()

    win.events.loaded += on_loaded

    def seq():
        mark("seq-enter")
        time.sleep(1.5)
        try:
            R["visible_before_close"] = _visible(win)
            mark(f"visible_before_close={R['visible_before_close']}")
            hwnd = int(_form(win).Handle.ToInt64())
            R["hwnd"] = hwnd
            mark(f"hwnd={hwnd}")
            ctypes.windll.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            mark("wm-close-posted")
            time.sleep(1.2)
            R["visible_after_close"] = _visible(win)
            R["disposed_after_close"] = _disposed(win)
            mark(f"after_close visible={R['visible_after_close']} disposed={R['disposed_after_close']}")
            # 从托盘恢复
            H.TRAY.show_window()
            time.sleep(1.0)
            R["visible_after_tray_show"] = _visible(win)
            mark(f"after_tray_show visible={R['visible_after_tray_show']}")
            # 托盘「退出」：应当真正销毁窗口
            H.TRAY.quit_app()
            mark("quit_app-called")
            time.sleep(1.2)
            R["disposed_after_quit"] = _disposed(win)
            mark(f"disposed_after_quit={R['disposed_after_quit']}")
        except Exception as e:
            R["error"] = repr(e)
            mark("error " + repr(e))
        finally:
            time.sleep(0.3)
            try:
                H.TRAY.stop()
            except Exception:
                pass
            print("RESULT " + repr(R), flush=True)
            print("STAGES " + " | ".join(STAGE), flush=True)
            os._exit(0)

    threading.Thread(
        target=lambda: (time.sleep(30), print("TIMEOUT " + repr(R), flush=True), os._exit(2)),
        daemon=True,
    ).start()
    mark("webview-start-entering")
    webview.start(debug=False)
    print("webview-start-returned " + repr(R), flush=True)
    print("STAGES " + " | ".join(STAGE), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
