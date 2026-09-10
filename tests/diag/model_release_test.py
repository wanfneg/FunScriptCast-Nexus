# -*- coding: utf-8 -*-
"""模型"按需加载 / 用完释放"的回归测试。

要守住的两件事：
  A. 停止字幕服务后，audiocpp 后端必须一起消失、内存真的还回来。
     之前 sub_stop() 用的是 proc.terminate()，而 Windows 上那是
     TerminateProcess——不给 Python 跑 lifespan 收尾的机会，于是
     stop_server() 永远不执行，audiocpp_server 变成孤儿：实测仍占 3,089 MB
     并继续占着 8083 端口。"按需加载"只做了一半，等于换个时间点常驻。
  B. 空闲足够久要自动释放（idle_release_min），否则看过一次片之后
     那 3 GB 会一直挂着。

测试会临时把 idle_release_min 改成 1 分钟并在结束时还原；需要 8790/8791/8756/8083
空闲，跑之前先关掉正在用的 Nexus。

用法： .venv\\Scripts\\python.exe tests\\diag\\model_release_test.py
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import time
import urllib.request

APP = pathlib.Path(__file__).resolve().parent.parent.parent
PY = APP / ".venv" / "Scripts" / "python.exe"
CFG = APP / "vendor" / "subtitle" / "config.json"
API = "http://127.0.0.1:8790"
AUDIOCPP_PORT = 8083

_fails: list = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  — {detail}" if detail else ""))
    if not ok:
        _fails.append(label)


def get(url: str, method: str = "GET", timeout: float = 15.0):
    r = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def port_owner(port: int):
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                             text=True, timeout=10).stdout
    except Exception:
        return None
    for line in out.splitlines():
        p = line.split()
        if len(p) >= 5 and p[1].endswith(f":{port}") and p[3] == "LISTENING":
            try:
                return int(p[4])
            except ValueError:
                continue
    return None


def proc_mb(pid) -> float:
    if not pid:
        return 0.0
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                             capture_output=True, text=True, timeout=10).stdout
        parts = (out.strip().splitlines() or [""])[0].split(",")
        if len(parts) >= 5:
            return float(parts[4].strip().strip('"').replace(",", "").replace(" K", "")) / 1024
    except Exception:
        pass
    return 0.0


def _kb_from_tasklist(line: str) -> float:
    """从 tasklist 的 CSV 行里取出内存（KB → MB）。

    不能按逗号 split：内存字段是 `"1,849,000 K"`，里面的逗号会把字段切碎，
    于是 parts[4] 只剩 "1"，算出来永远是 0（这个坑我在测试里踩过一次）。
    """
    m = re.search(r'"([\d,]+) K"', line)
    if not m:
        return 0.0
    try:
        return float(m.group(1).replace(",", "")) / 1024
    except ValueError:
        return 0.0


def audiocpp_mb() -> float:
    """所有 audiocpp_server 进程的内存合计（MB）。"""
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq audiocpp_server.exe",
                              "/NH", "/FO", "CSV"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return 0.0
    total = 0.0
    for line in out.strip().splitlines():
        if "audiocpp_server.exe" in line:
            total += _kb_from_tasklist(line)
    return total


def start_host() -> subprocess.Popen:
    return subprocess.Popen([str(PY), str(APP / "host_server.py"), "--no-window"],
                            cwd=str(APP), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_up(limit: float = 40.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < limit:
        time.sleep(1)
        try:
            get(f"{API}/api/state", timeout=3)
            return True
        except Exception:
            continue
    return False


def wait_service(limit: float = 90.0) -> str:
    t0 = time.time()
    while time.time() - t0 < limit:
        time.sleep(1)
        st = get(f"{API}/api/state")["subtitle"]["status"]
        if st in ("ready", "error"):
            return st
    return "timeout"


def kill_strays() -> None:
    for port in (8790, 8791, 8756, AUDIOCPP_PORT):
        p = port_owner(port)
        if p:
            subprocess.run(["taskkill", "/PID", str(p), "/T", "/F"],
                           capture_output=True, timeout=15)
    subprocess.run(["taskkill", "/IM", "audiocpp_server.exe", "/F"],
                   capture_output=True, timeout=15)
    time.sleep(3)


def main() -> int:
    kill_strays()
    orig = CFG.read_text(encoding="utf-8")
    host = None
    try:
        print("启动宿主…")
        host = start_host()
        if not wait_up():
            print("宿主没起来")
            return 2

        print("\n=== A) 停止服务必须把 audiocpp 一起带走 ===")
        get(f"{API}/api/subtitle/start", "POST")
        st = wait_service()
        check(st == "ready", "字幕服务就绪", st)
        before = audiocpp_mb()
        pid_before = port_owner(AUDIOCPP_PORT)
        print(f"  就绪后 audiocpp_server 合计 {before:,.0f} MB  (PID {pid_before})")
        check(before > 100, "audiocpp 后端确实在跑", f"{before:,.0f} MB")

        get(f"{API}/api/subtitle/stop", "POST")
        time.sleep(6)
        after = audiocpp_mb()
        pid_after = port_owner(AUDIOCPP_PORT)
        print(f"  停止后 audiocpp_server 合计 {after:,.0f} MB  8083 占用者={pid_after}")
        check(after == 0, "audiocpp 进程已消失", f"仍有 {after:,.0f} MB")
        check(pid_after is None, "8083 端口已释放", str(pid_after))

        print("\n=== B) 空闲足够久自动释放（临时把门槛改成 1 分钟）===")
        cfg = json.loads(orig)
        cfg.setdefault("server", {})["idle_release_min"] = 1
        CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        get(f"{API}/api/subtitle/start", "POST")
        st = wait_service()
        check(st == "ready", "改了配置后仍能起来", st)
        print(f"  audiocpp {audiocpp_mb():,.0f} MB；等待空闲回收（门槛 1 分钟，检查周期 30s）…")
        t0 = time.time()
        while time.time() - t0 < 150:
            time.sleep(5)
            if port_owner(8756) is None:
                break
        el = time.time() - t0
        gone = port_owner(8756) is None
        print(f"  {el:.0f}s 后字幕服务{'已退出' if gone else '仍在'}；"
              f"audiocpp {audiocpp_mb():,.0f} MB")
        check(gone, "空闲后字幕服务自动退出")
        check(audiocpp_mb() == 0, "空闲回收把 audiocpp 也带走了",
              f"仍有 {audiocpp_mb():,.0f} MB")
    finally:
        CFG.write_text(orig, encoding="utf-8")
        print(f"\n已还原 {CFG.name}")
        try:
            get(f"{API}/api/quit", "POST", timeout=5)
        except Exception:
            pass
        time.sleep(2)
        if host and host.poll() is None:
            host.kill()
        kill_strays()

    print()
    if _fails:
        print(f"FAILED {len(_fails)} 项：" + "; ".join(_fails))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
