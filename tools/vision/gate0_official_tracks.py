# -*- coding: utf-8 -*-
"""Gate 0c：通过官方 --serve 拉取轨道，与本地 ONNX 解码逐行对比。

用法：python tools/vision/gate0_official_tracks.py <video> --out OUT.json [--bundle DIR]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

EXE = Path(r"E:\Development\tmp\goblinscript\x\goblinscript.exe")


def api(base, token, path, payload=None, timeout=60):
    url = base + path
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"x-goblin-token": token,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--out", required=True)
    ap.add_argument("--bundle", default=str(Path(r"E:\Development\FunScriptCast-Nexus\vendor\vision\bundles\v0.6.0")))
    ap.add_argument("--timeout", type=float, default=1200)
    a = ap.parse_args()

    proc = subprocess.Popen(
        [str(EXE), "--serve", "--port", "0", "--quiet",
         "--bundle", a.bundle, "--no-autocrop", "--no-exposure", "--no-vr",
         "--no-crop-edit", "--mute"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1)
    line = proc.stdout.readline()
    print("[serve] handshake:", line.strip(), flush=True)
    hs = json.loads(line)
    base, token = f"http://127.0.0.1:{hs['port']}", hs["token"]

    try:
        st = api(base, token, "/sessions",
                 {"video": str(Path(a.video).resolve()), "vr": False,
                  "crop_edit": False, "browser": False})
        sid = st["id"]
        print("[serve] session", sid, st.get("state"), flush=True)
        t0 = time.time()
        while time.time() - t0 < a.timeout:
            time.sleep(3)
            st = api(base, token, f"/sessions/{sid}")
            print(f"[serve] {st.get('state')} {st.get('stage')}", flush=True)
            if st.get("state") == "ready":
                break
            if st.get("state") == "error":
                print("[serve] 错误:", st.get("error"), flush=True)
                return 1
        else:
            print("[serve] 超时", flush=True)
            return 1

        tr = api(base, token, f"/sessions/{sid}/tracks?from_ms=0&to_ms=100000000")
        out = {"sessions": sid, "tracks": tr, "health": api(base, token, "/health"),
               "manifest": api(base, token, "/manifest")}
        Path(a.out).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        print(f"[serve] tracks 键: {list(tr.keys()) if isinstance(tr, dict) else type(tr)}", flush=True)
        print("[serve] 已写出", a.out, flush=True)
    finally:
        try:
            api(base, token, "/quit", {}, timeout=5)
        except Exception:
            pass
        try:
            proc.stdin.close()
        except Exception:
            pass
        time.sleep(1)
        if proc.poll() is None:
            proc.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
