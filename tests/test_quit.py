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

    # 端口必须空着：host_server 发现 8790 已被占用时会主动退出、把请求留给已有实例，
    # 于是下面的 /api/quit 打到的是**用户正在跑的宿主**——测试杀掉了生产程序，
    # 还因为「进程退出成功」报 PASS。这条预检必须先于 Popen。
    import socket
    with socket.socket() as s:
        s.settimeout(1.0)
        if s.connect_ex(("127.0.0.1", 8790)) == 0:
            print("RESULT " + json.dumps({"error": "8790 已被占用：先关掉正在运行的 "
                                                   "FunScriptCast-Nexus / host_server"},
                                         ensure_ascii=False), flush=True)
            return 1

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
        # quit 之前必须先确认「自己那个进程还活着」：否则 proc 早就崩了，
        # wait_exit 会立刻返回 True，测试把一个从没被 quit 到的死进程当成退出成功。
        R["alive_before_quit"] = proc.poll() is None
        R["quit_resp"] = post("/api/quit", {})
        R["process_exited"] = wait_exit(proc, 20.0)
        R["exit_code"] = proc.poll()
        # 判定：API 通 + 进程本来活着 + 确实退出了，三者缺一不可
        R["pass"] = bool(R.get("api_up") and R["alive_before_quit"] and R["process_exited"])
        if not R.get("api_up"):
            R["fail_reason"] = "宿主 API 没起来（api_up=False），结果不可信"
        elif not R["alive_before_quit"]:
            R["fail_reason"] = "quit 之前自己的宿主进程就已退出（多半是启动失败，不是退出成功）"
    finally:
        if proc.poll() is None:
            try:
                proc.kill()
                R["killed"] = True
            except Exception as e:
                # 记下而不是让 finally 抛出去：否则 emit(R) 不执行，
                # RESULT 行消失，日志里就看不出到底哪一步失败
                R["kill_error"] = repr(e)
        emit(R)
    # 以前不管结果如何都 return 0（只记录不判定），失败在 CI/脚本调用方看来是 PASS。
    # 这里用 os._exit 会跳过缓冲刷新，所以先 emit（内部已 flush）再返回退出码。
    return 0 if R.get("pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())
