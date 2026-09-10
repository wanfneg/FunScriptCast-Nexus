# -*- coding: utf-8 -*-
"""字幕服务"孤儿进程"识别与回收的回归测试。

背景：宿主被强杀（任务管理器 / 崩溃）时，字幕服务子进程会**活下来**并继续占着
8756。下一次启动宿主时，如果只按"端口开着"判断，就会报一个**假的 ready**——
头显因此跳过等待、把音频发给那个陈旧进程，而它加载的是它启动时的旧 config，
之后改过的设置（换模型 / 换翻译后端 / 改术语表）一律不生效，界面上却一切正常。

这个测试自己管理宿主生命周期：
  1. 干净启动 → 启动字幕服务 → 应无 foreignPid
  2. 强杀宿主（只杀宿主，子进程留下）→ 制造孤儿
  3. 重启宿主 → 必须把 8756 上的服务识别为外来（foreignPid 非空）
  4. POST /api/subtitle/reclaim → 结束孤儿并重新拉起自己的服务

用法： .venv\\Scripts\\python.exe tests\\diag\\subtitle_orphan_test.py
       （测试会占用 8790/8791/8756，跑之前先关掉正在用的 Nexus）
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import time
import urllib.request

APP = pathlib.Path(__file__).resolve().parent.parent.parent
PY = APP / ".venv" / "Scripts" / "python.exe"
API = "http://127.0.0.1:8790"
LAN = "http://127.0.0.1:8791"

_fails: list = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  — {detail}" if detail else ""))
    if not ok:
        _fails.append(label)


def get(url: str, method: str = "GET", timeout: float = 8.0):
    r = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def state() -> dict:
    return get(f"{API}/api/state").get("subtitle", {})


def start_host() -> subprocess.Popen:
    return subprocess.Popen([str(PY), str(APP / "host_server.py"), "--no-window"],
                            cwd=str(APP), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_ready(limit: float = 60.0) -> str:
    t0 = time.time()
    while time.time() - t0 < limit:
        time.sleep(1)
        st = state().get("status")
        if st in ("ready", "error"):
            return st
    return "timeout"


def port_owner(port: int):
    """占用某端口的 PID（netstat 解析，不引第三方依赖）。"""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3] == "LISTENING":
            try:
                return int(parts[4])
            except ValueError:
                continue
    return None


def kill_pid(pid) -> None:
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       capture_output=True, timeout=10)
    except Exception:
        pass


def main() -> int:
    print("启动宿主（干净）…")
    host = start_host()
    try:
        for _ in range(30):
            time.sleep(1)
            try:
                get(f"{API}/api/state", timeout=3)
                break
            except Exception:
                continue

        print("\n=== 1) 干净启动 + 启动字幕服务 ===")
        get(f"{API}/api/subtitle/start", "POST")
        st = wait_ready()
        s = state()
        print(f"  status={st} alive={s.get('alive')} pid={s.get('pid')} "
              f"foreignPid={s.get('foreignPid')!r}")
        check(st == "ready", "服务就绪", st)
        check(not s.get("foreignPid"), "干净启动不应被判为外来服务",
              f"foreignPid={s.get('foreignPid')!r}")

        print("\n=== 2) 强杀宿主，制造孤儿 ===")
        host.kill()
        host.wait(timeout=10)
        time.sleep(4)
        orphan = port_owner(8756)
        print(f"  8756 占用者 PID={orphan}")
        check(orphan is not None, "字幕服务确实成了孤儿（端口仍被占）", str(orphan))
        if orphan is None:
            return 1

        print("\n=== 3) 重启宿主：必须识别为外来 ===")
        host = start_host()
        for _ in range(30):
            time.sleep(1)
            try:
                get(f"{API}/api/state", timeout=3)
                break
            except Exception:
                continue
        time.sleep(2)
        s2 = state()
        print(f"  status={s2.get('status')} alive={s2.get('alive')} "
              f"foreignPid={s2.get('foreignPid')} error={s2.get('error')!r}")
        check(s2.get("foreignPid") == orphan, "识别出孤儿 PID",
              f"期望 {orphan}，实际 {s2.get('foreignPid')}")
        hs = get(f"{LAN}/api/headset/status")
        check(hs.get("ready") is True, "服务本身可用（仍算 ready，头显照常干活）", str(hs.get("ready")))

        print("\n=== 4) reclaim：结束孤儿并重新拉起自己的服务 ===")
        r = get(f"{API}/api/subtitle/reclaim", "POST", timeout=60)
        print(f"  {json.dumps(r, ensure_ascii=False)[:160]}")
        check(r.get("killed") == orphan, "回收接口结束了孤儿", str(r.get("killed")))
        time.sleep(2)
        check(port_owner(8756) != orphan, "孤儿已不再监听 8756", str(port_owner(8756)))
        st3 = wait_ready()
        s3 = state()
        check(st3 == "ready", "回收后自己的服务重新就绪", st3)
        check(not s3.get("foreignPid"), "回收后不再有外来标记",
              f"foreignPid={s3.get('foreignPid')!r}")
    finally:
        try:
            get(f"{API}/api/quit", "POST", timeout=5)
        except Exception:
            pass
        time.sleep(2)
        if host.poll() is None:
            host.kill()
        # 收尾：把可能残留的字幕服务也清掉，避免污染下一次测试
        for _ in range(20):
            p = port_owner(8756)
            if not p:
                break
            kill_pid(p)
            time.sleep(0.5)

    print()
    if _fails:
        print(f"FAILED {len(_fails)} 项：" + "; ".join(_fails))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
