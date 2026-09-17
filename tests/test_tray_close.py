# -*- coding: utf-8 -*-
"""端到端验证「点关闭按钮 → 隐藏到托盘，进程不退出」以及托盘「退出」能真正结束进程。

用真实路径：给窗体发 WM_CLOSE（等价于用户点 X），走 host_server.run() 里注册的
on_closing。若 on_closing 返回 False，窗体应当仍然存在（IsDisposed == False）
但 Visible == False。
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

WM_CLOSE = 0x0010
R: dict = {}
STAGE: list = []
EXIT_CODE = 0
_restored = False


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


def _restore_settings(settings_file: Path, backup: Path) -> None:
    """恢复真实 %APPDATA% 设置。

    为什么必须在主线程也有份：本测试用**真实设置文件**做前置条件
    （save_settings 改的是用户的 integrated_settings.json）。旧实现只把恢复写在
    daemon 线程的 finally 里，超时看门狗那条分支直接 os._exit(2) —— 恢复根本没跑，
    用户配置就被永久改成测试值了。所以这里做成可重复调用，主线程 finally 与
    看门狗分支都会走一遍。
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

    mark("imported")
    settings_file = H.SETTINGS_FILE
    backup = settings_file.with_suffix(".json.bak-trayclose")
    # copy2 前先判存在：不存在时它抛 FileNotFoundError，而那时 try/finally 还没进
    if not settings_file.exists():
        R["error"] = f"设置文件不存在：{settings_file}"
        print("RESULT " + json.dumps(R, ensure_ascii=False), flush=True)
        os._exit(1)
        return 1
    shutil.copy2(settings_file, backup)
    # 固定前置条件：close_to_tray=true，才能验证「关闭 → 隐藏到托盘」
    H.save_settings({"close_to_tray": True})

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
        global EXIT_CODE
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
            # 门禁：以前这里只 print RESULT 就 os._exit(0)，跑挂了也是 PASS。
            # 关键项：托盘真的起来过；点关闭后「窗口未销毁 + 不可见」（隐藏到托盘）；
            # 托盘的显示/退出两条路径都生效。
            checks = {
                "tray_started": R.get("tray_started") is True,
                "no_error": "error" not in R,
                "visible_before_close": R.get("visible_before_close") is True,
                "hidden_not_disposed": (R.get("visible_after_close") is False
                                        and R.get("disposed_after_close") is False),
                "tray_show_restores": R.get("visible_after_tray_show") is True,
                "tray_quit_disposes": R.get("disposed_after_quit") is True,
            }
            R["checks"] = checks
            R["pass"] = all(checks.values())
            R["failed_checks"] = [k for k, v in checks.items() if not v]
            EXIT_CODE = 0 if R["pass"] else 1
            _restore_settings(settings_file, backup)
            print("RESULT " + repr(R), flush=True)
            print("STAGES " + " | ".join(STAGE), flush=True)
            os._exit(EXIT_CODE)

    def _watchdog() -> None:
        # 兜底分支同样要恢复设置：os._exit 不会触发任何 finally/atexit
        global EXIT_CODE
        time.sleep(30)
        R["pass"] = False
        R["fail_reason"] = "30s 看门狗超时（窗口/托盘没走到预期状态）"
        EXIT_CODE = 1
        _restore_settings(settings_file, backup)
        print("TIMEOUT " + repr(R), flush=True)
        os._exit(1)

    threading.Thread(target=_watchdog, daemon=True).start()

    mark("webview-start-entering")
    try:
        webview.start(debug=False)
    finally:
        # 主线程兜底：无论 webview.start 正常返回还是抛异常，用户设置都必须还原
        _restore_settings(settings_file, backup)
    print("webview-start-returned " + repr(R), flush=True)
    print("STAGES " + " | ".join(STAGE), flush=True)
    return EXIT_CODE


if __name__ == "__main__":
    raise SystemExit(main())
