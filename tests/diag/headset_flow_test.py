# -*- coding: utf-8 -*-
"""复现头显端的"等服务端就绪"时序，用来量出画面会被停多久。

头显侧的逻辑（AiSubtitleEngine.startWhenServerReady / holdAndWait）是：
  1. 先探一次 /api/headset/status —— 已经在跑就**一秒都不等**，直接开始
  2. 没跑就 POST /api/subtitle/start，同时按住画面
  3. 每 1.5s 轮询一次，直到 ready（或 90s 超时）
  4. 就绪后放开画面并开始抓音频

这里把同样的序列在 PC 上跑一遍，测出第 2~4 步的真实耗时——那就是用户看到的
"画面暂停了多久"。顺便验证探测分支确实能在服务已就绪时立刻返回。

用法： .venv\\Scripts\\python.exe tests\\diag\\headset_flow_test.py
      （需要宿主已在运行：.venv\\Scripts\\python.exe host_server.py --no-window）
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

LAN = "http://127.0.0.1:8791"
POLL_SEC = 1.5


def req(path: str, method: str = "GET", timeout: float = 8.0):
    r = urllib.request.Request(LAN + path, method=method,
                               data=b"" if method == "POST" else None)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def status():
    try:
        return req("/api/headset/status")
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "status": "unreachable",
                "ready": False}


def stop_service():
    import urllib.request as u
    r = u.Request("http://127.0.0.1:8790/api/subtitle/stop", method="POST", data=b"")
    with u.urlopen(r, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    s = status()
    if not s.get("ok"):
        print(f"头显接口不可达（{s.get('error')}）—— 先把宿主跑起来")
        return 2
    print(f"起始状态: status={s['status']} ready={s['ready']}")

    # 确保从"未启动"开始，才能测到最坏情况（冷启动）
    if s["ready"] or s["status"] == "loading":
        print("先停掉字幕服务，从冷启动开始测最坏情况…")
        stop_service()
        for _ in range(20):
            time.sleep(1)
            if status()["status"] == "stopped":
                break
        print(f"  现在: {status()['status']}")

    # ---- 步骤 1：探测。已就绪的话这里就该直接结束
    t0 = time.perf_counter()
    s = status()
    probe_ms = (time.perf_counter() - t0) * 1000
    if s["ready"]:
        print(f"探测到服务已就绪，直接开始：耗时 {probe_ms:.0f}ms（零停顿路径）")
    else:
        print(f"探测耗时 {probe_ms:.0f}ms，服务未就绪 → 进入按住等待")

        # ---- 步骤 2：请求拉起，同时"按住画面"
        t_hold = time.perf_counter()
        print(f"  POST /api/subtitle/start …")
        print(f"  [{0.0:5.1f}s] 画面在此刻被暂停")

        # ---- 步骤 3：轮询
        first = req("/api/subtitle/start", "POST")
        print(f"  start 返回: {json.dumps(first, ensure_ascii=False)}")
        last = None
        while True:
            el = time.perf_counter() - t_hold
            if el > 90:
                print(f"  [{el:5.1f}s] 超时（头显会放开画面并按无字幕继续）")
                break
            st = status()
            if st["status"] != last:
                print(f"  [{el:5.1f}s] status={st['status']}")
                last = st["status"]
            if st["ready"]:
                print(f"  [{el:5.1f}s] ready —— 放开画面，开始抓音频")
                print(f"\n画面暂停总时长 ≈ {el:.1f}s")
                break
            if st["status"] == "error":
                print(f"  [{el:5.1f}s] error: {st.get('error')}")
                break
            time.sleep(POLL_SEC)

    # ---- 验证热路径：服务已在跑时，探测必须立刻返回 ready
    t1 = time.perf_counter()
    hot = status()
    hot_ms = (time.perf_counter() - t1) * 1000
    print(f"\n热路径复测: ready={hot['ready']} 探测耗时 {hot_ms:.0f}ms")
    if hot["ready"] and hot_ms < 800:
        print("  → 第二次开字幕时不会停顿（符合预期）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
