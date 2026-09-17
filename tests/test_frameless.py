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
import shutil
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
EXIT_CODE = 0
_restored = False


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


def _restore_settings(settings_file: Path, backup: Path) -> None:
    """恢复真实 %APPDATA% 设置（可重复调用）。

    为什么主线程也要有份：本测试的前置条件是直接改**用户的**
    integrated_settings.json。旧实现把恢复只写在 daemon 线程的 finally 里，
    而超时看门狗那条分支 os._exit(2) 根本不执行 finally —— 用户配置就被永久
    改成测试值了。主线程 finally + 看门狗分支都调一遍才算兜住。
    """
    global _restored
    if _restored:
        return
    try:
        if backup.exists():
            shutil.move(str(backup), str(settings_file))
            R["settings_restored"] = True
    except Exception as e:
        R["settings_restore_error"] = repr(e)
    _restored = True


def main() -> int:
    global EXIT_CODE

    import webview

    settings_file = H.SETTINGS_FILE
    backup = settings_file.with_suffix(".json.bak-frameless")
    # copy2 前先判存在：不存在时它抛 FileNotFoundError，那时 try/finally 还没进
    if not settings_file.exists():
        R["error"] = f"设置文件不存在：{settings_file}"
        emit()
        return 1
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
        global EXIT_CODE
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
            # 门禁：以前这里只 emit() 就 os._exit(0)，任何一项不成立也报「通过」。
            # 关键项：托盘起来过；无边框（无 WS_CAPTION 且保留缩放边框）；
            # minimize/restore 生效；托盘恢复后可见；close_to_tray=false 时真销毁。
            checks = {
                "no_error": "error" not in R,
                "tray_started": R.get("tray_started") is True,
                "tune_ok": bool((R.get("tune") or {}).get("ok")),
                "frameless": R.get("has_caption") is False,
                "resizable_border": R.get("has_thickframe") is True,
                "visible_initial": R.get("visible_initial") is True,
                "minimize_works": R.get("iconic_after_min") is True,
                "restore_works": R.get("iconic_after_restore") is False,
                "close_hides_not_disposes": (R.get("visible_after_win_close") is False
                                             and R.get("disposed_after_win_close") is False),
                "tray_show_restores": R.get("visible_after_tray_show") is True,
                "quit_disposes": R.get("disposed_after_quit") is True,
            }
            R["checks"] = checks
            R["pass"] = all(checks.values())
            R["failed_checks"] = [k for k, v in checks.items() if not v]
            EXIT_CODE = 0 if R["pass"] else 1
            _restore_settings(settings_file, backup)
            emit()
            os._exit(EXIT_CODE)

    def _watchdog() -> None:
        # 兜底分支同样恢复设置：os._exit 不触发 finally
        global EXIT_CODE
        time.sleep(40)
        R["pass"] = False
        R["fail_reason"] = "40s 看门狗超时（窗口没走到预期状态）"
        EXIT_CODE = 1
        _restore_settings(settings_file, backup)
        emit()
        os._exit(1)

    threading.Thread(target=_watchdog, daemon=True).start()

    mark("webview.start entering")
    try:
        webview.start(debug=False)
    finally:
        # 主线程兜底：webview.start 抛异常也要还原用户的设置文件
        _restore_settings(settings_file, backup)
    mark("webview.start returned")
    emit()
    return EXIT_CODE


if __name__ == "__main__":
    raise SystemExit(main())
