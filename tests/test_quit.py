# -*- coding: utf-8 -*-
"""验证 /api/quit 能真正结束进程（含字幕子进程在跑的情况）。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
PY = APP_DIR / ".venv" / "Scripts" / "python.exe"
LOG = APP_DIR / "tests" / "_quit_log.txt"


def get(path: str, timeout: float = 5.0) -> dict:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:8790{path}", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"_error": repr(e)}


def post(path: str, payload: dict, timeout: float = 8.0) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:8790{path}", method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"_error": repr(e)}


def emit(obj: dict) -> None:
    line = "RESULT " + json.dumps(obj, ensure_ascii=False) + "\n"
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
    print(line, end="", flush=True)


def wait_exit(proc: subprocess.Popen, secs: float) -> bool:
    end = time.time() + secs
    while time.time() < end:
        if proc.poll() is not None:
            return True
        time.sleep(0.25)
    return proc.poll() is not None


def main() -> int:
    R: dict = {}
    case = sys.argv[1] if len(sys.argv) > 1 else "plain"
    R["case"] = case
    proc = subprocess.Popen([str(PY), str(APP_DIR / "host_server.py")], cwd=str(APP_DIR))
    try:
        st = {}
        for _ in range(60):
            time.sleep(0.5)
            st = get("/api/state")
            if "_error" not in st:
                break
        R["api_up"] = "_error" not in st
        if case == "with_subtitle":
            post("/api/subtitle/start", {})
            for _ in range(30):
                time.sleep(2)
                st = get("/api/state")
                if st.get("subtitle", {}).get("status") in ("ready", "error"):
                    break
            R["sub_status"] = st.get("subtitle", {}).get("status")
        time.sleep(1.0)
        R["quit_resp"] = post("/api/quit", {})
        R["process_exited"] = wait_exit(proc, 20.0)
    finally:
        if proc.poll() is None:
            proc.kill()
            R["killed"] = True
        emit(R)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
