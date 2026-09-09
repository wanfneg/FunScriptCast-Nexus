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


def main() -> int:
    R: dict = {}
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

        win = ctypes.windll.user32.FindWindowW(None, "FunScriptCast-Nexus")
        R["window_found"] = bool(win)
        if win:
            ctypes.windll.user32.ShowWindow(win, 0)          # SW_HIDE
            time.sleep(1.0)
            R["visible_after_hide"] = bool(ctypes.windll.user32.IsWindowVisible(win))
            R["api_while_hidden"] = "_error" not in get_state()
            ctypes.windll.user32.ShowWindow(win, 9)          # SW_RESTORE
            time.sleep(1.0)
            R["visible_after_restore"] = bool(ctypes.windll.user32.IsWindowVisible(win))

        R["quit_resp"] = post("/api/quit", {})
        R["process_exited"] = wait_exit(proc, 12.0)
    finally:
        if proc.poll() is None:
            proc.kill()
            R["killed"] = True
        shutil.move(str(BACKUP), str(SETTINGS))
    print("RESULT " + json.dumps(R, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
