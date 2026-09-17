# -*- coding: utf-8 -*-
"""真实 run() 路径验证（修复后）：

  1) close_to_tray=False 时托盘仍然启动（修复点，否则 start_minimized 无法恢复）
  2) 窗口隐藏后后端 API 仍可用，恢复后可见
  3) /api/quit 真正结束进程（修复点：旧实现只 shutdown HTTP 服务）

托盘存在性用 RT 日志里的「托盘图标已就绪」判定（进程内，避免枚举溢出区图标）。
"""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
PY = APP_DIR / ".venv" / "Scripts" / "python.exe"
SETTINGS = Path(os.environ.get("APPDATA")) / "FunScriptCast-Nexus" / "integrated_settings.json"
BACKUP = SETTINGS.with_suffix(".json.bak-test")


def get_state(timeout: float = 3.0) -> dict:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8790/api/state", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"_error": repr(e)}


def post(path: str, payload: dict, timeout: float = 5.0) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:8790{path}", method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"_error": repr(e)}


def wait_exit(proc: subprocess.Popen, secs: float) -> bool:
    end = time.time() + secs
    while time.time() < end:
        if proc.poll() is not None:
            return True
        time.sleep(0.25)
    return proc.poll() is not None


def window_pid(hwnd: int) -> int:
    """hwnd 属于哪个进程。

    必须比对：FindWindowW 按标题全屏找，找到的完全可能是**用户正在运行的**
    那个窗口（标题相同）。旧代码只要 ``if win:`` 就继续做隐藏/恢复断言，
    于是测试操作生产窗口、却把「隐藏后 API 仍可用」记成自己的成绩。
    """
    pid = ctypes.c_ulong(0)
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def main() -> int:
    R: dict = {}

    # 端口必须空着：host_server 撞上已占用的 8790 会主动退出，把请求留给已有实例，
    # 本脚本随后就会去隐藏/恢复**用户的窗口**，最后 /api/quit 杀掉用户的程序。
    import socket
    with socket.socket() as s:
        s.settimeout(1.0)
        if s.connect_ex(("127.0.0.1", 8790)) == 0:
            print("RESULT " + json.dumps(
                {"error": "8790 已被占用：先关掉正在运行的 FunScriptCast-Nexus / host_server"},
                ensure_ascii=False), flush=True)
            return 1

    # 备份前先判存在：文件不存在时 copy2 直接抛 FileNotFoundError，
    # 而那时 try/finally 还没进，进程带着 traceback 退出、设置也没人恢复
    if not SETTINGS.exists():
        print("RESULT " + json.dumps({"error": f"设置文件不存在：{SETTINGS}"},
                                     ensure_ascii=False), flush=True)
        return 1
    shutil.copy2(SETTINGS, BACKUP)
    s = json.loads(SETTINGS.read_text(encoding="utf-8"))
    s.update({"close_to_tray": False, "start_minimized": False,
              "subtitle_auto_start": False, "dlna_auto_start": False})
    SETTINGS.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")

    proc = subprocess.Popen([str(PY), str(APP_DIR / "host_server.py")], cwd=str(APP_DIR))
    try:
        st = {}
        for _ in range(60):
            time.sleep(0.5)
            st = get_state()
            if "_error" not in st:
                break
        R["api_up"] = "_error" not in st
        R["close_to_tray_loaded"] = st.get("settings", {}).get("close_to_tray")
        time.sleep(2.5)
        st = get_state()

        logs = [x.get("msg", "") for x in (st.get("events") or [])]
        R["events_tail"] = logs[-6:]
        R["log_has_tray_ready"] = any("托盘图标已就绪" in m for m in logs)
        R["log_has_tray_warn"] = any("托盘" in m and ("改为最小化" in m or "不可用" in m) for m in logs)
        R["tray_started"] = bool(R["log_has_tray_ready"])
        # 托盘启动是修复点 1 的核心：启动了就不该同时出现「托盘不可用」告警
        R["tray_ok"] = bool(R["log_has_tray_ready"] and not R["log_has_tray_warn"])

        win = ctypes.windll.user32.FindWindowW(None, "FunScriptCast-Nexus")
        R["window_found"] = bool(win)
        if win:
            R["window_pid"] = window_pid(win)
            R["my_pid"] = proc.pid
            R["window_is_mine"] = (R["window_pid"] == proc.pid)
            if R["window_is_mine"]:
                ctypes.windll.user32.ShowWindow(win, 0)          # SW_HIDE
                time.sleep(1.0)
                R["visible_after_hide"] = bool(ctypes.windll.user32.IsWindowVisible(win))
                R["api_while_hidden"] = "_error" not in get_state()
                ctypes.windll.user32.ShowWindow(win, 9)          # SW_RESTORE
                time.sleep(1.0)
                R["visible_after_restore"] = bool(ctypes.windll.user32.IsWindowVisible(win))
            else:
                # 找到的是别人的同名窗口：绝不能去隐藏它，窗口类断言整体按失败处理
                R["fail_reason"] = (f"找到的窗口属于 pid={R['window_pid']}，"
                                    f"不是本次拉起的 {proc.pid}——窗口断言作废")

        R["alive_before_quit"] = proc.poll() is None
        R["quit_resp"] = post("/api/quit", {})
        R["process_exited"] = wait_exit(proc, 12.0)

        # 门禁：以前所有结果只 print 不判定，恒以 0 退出（假通过）。
        # 关键项缺一即失败：托盘起来、窗口是自己的且隐藏/恢复都验证过、quit 真退出。
        checks = {
            "api_up": R["api_up"],
            "tray_ok": R["tray_ok"],
            "window_found": R["window_found"],
            "window_is_mine": R.get("window_is_mine", False),
            "visible_after_hide": R.get("visible_after_hide") is False,
            "api_while_hidden": R.get("api_while_hidden", False),
            "visible_after_restore": R.get("visible_after_restore") is True,
            "alive_before_quit": R["alive_before_quit"],
            "process_exited": R["process_exited"],
        }
        R["checks"] = checks
        R["pass"] = all(checks.values())
        R["failed_checks"] = [k for k, v in checks.items() if not v]
    finally:
        if proc.poll() is None:
            try:
                proc.kill()
                R["killed"] = True
            except Exception as e:
                R["kill_error"] = repr(e)
        # 恢复设置：无论成功失败都要发生，否则用户的真实配置被本测试改写
        try:
            if BACKUP.exists():
                shutil.move(str(BACKUP), str(SETTINGS))
                R["settings_restored"] = True
        except Exception as e:
            R["settings_restore_error"] = repr(e)
    print("RESULT " + json.dumps(R, ensure_ascii=False), flush=True)
    return 0 if R.get("pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())
