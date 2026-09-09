# -*- coding: utf-8 -*-
"""验证无边框自绘标题栏的窗口行为。

注意：pywebview 的 evaluate_js() 必须由主线程调用，从后台线程调会阻塞，
所以这里只用窗口 API（state/minimize/restore/destroy）+ Win32 样式位验证。

  1) 窗口没有原生标题栏（WS_CAPTION 未置位），且保留可缩放边框
  2) minimize / restore 生效
  3) destroy 触发 closing 拦截 → 只隐藏、不销毁
  4) 托盘恢复后窗口重新可见
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
import threading
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))
import host_server as H  # noqa: E402

GWL_STYLE = -16
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000

R: dict = {}
LOG = APP_DIR / "tests" / "_fr_log.txt"


def mark(tag: str) -> None:
    line = f"STAGE {tag}\n"
    print(line, end="", flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
    except Exception:
        pass


def emit() -> None:
    line = "RESULT " + json.dumps(R, ensure_ascii=False) + "\n"
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
    except Exception:
        pass
    print(line, end="", flush=True)


def main() -> int:
    import shutil

    import webview

    settings_file = H.SETTINGS_FILE
    backup = settings_file.with_suffix(".json.bak-frameless")
    shutil.copy2(settings_file, backup)
    # 固定前置条件：close_to_tray 打开，才能验证「关闭 → 隐藏到托盘」
    H.save_settings({"close_to_tray": True})

    httpd = H.ThreadingHTTPServer(("127.0.0.1", H.UI_API_PORT), H.Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    win = webview.create_window(
        "frameless-check", f"http://127.0.0.1:{H.UI_API_PORT}/",
        width=1240, height=820, frameless=True, js_api=H.JSAPI,
    )
    H.JSAPI.bind(win)
    mark("window created")

    def on_loaded():
        mark("loaded event")
        R["tray_started"] = H.TRAY.start(win)
        R["tune"] = H.tune_frameless_window(win)
        threading.Thread(target=seq, daemon=True).start()

    win.events.loaded += on_loaded

    def seq():
        mark("seq enter")
        time.sleep(2.0)
        try:
            hwnd = int(win.native.Handle.ToInt64())
            st = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_STYLE)
            R["has_caption"] = bool(st & WS_CAPTION)
            R["has_thickframe"] = bool(st & WS_THICKFRAME)
            R["visible_initial"] = bool(ctypes.windll.user32.IsWindowVisible(hwnd))

            win.minimize()
            time.sleep(1.2)
            R["iconic_after_min"] = bool(ctypes.windll.user32.IsIconic(hwnd))
            win.restore()
            time.sleep(1.0)
            R["iconic_after_restore"] = bool(ctypes.windll.user32.IsIconic(hwnd))

            # win_close（JS 桥的关闭按钮）：close_to_tray=true 时应只隐藏
            R["close_to_tray_setting"] = H.load_settings().get("close_to_tray")
            R["win_close_resp"] = H.JSAPI.win_close()
            time.sleep(1.5)
            R["disposed_after_win_close"] = bool(win.native.IsDisposed)
            R["visible_after_win_close"] = bool(ctypes.windll.user32.IsWindowVisible(hwnd))

            H.TRAY.show_window()
            time.sleep(1.2)
            R["visible_after_tray_show"] = bool(ctypes.windll.user32.IsWindowVisible(hwnd))
            # close_to_tray=false 时应当直接退出（销毁窗体 + 结束事件循环）
            H.save_settings({"close_to_tray": False})
            R["win_close_resp2"] = H.JSAPI.win_close()
            time.sleep(1.5)
            R["disposed_after_quit"] = bool(win.native.IsDisposed)
        except Exception as e:
            R["error"] = repr(e)
        finally:
            time.sleep(0.3)
            try:
                shutil.move(str(backup), str(settings_file))
                R["settings_restored"] = True
            except Exception as e:
                R["settings_restore_error"] = repr(e)
            emit()
            os._exit(0)

    threading.Thread(
        target=lambda: (time.sleep(40), emit(), os._exit(2)),
        daemon=True,
    ).start()
    webview.start(debug=False)
    mark("webview.start returned")
    emit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
