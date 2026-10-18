# -*- coding: utf-8 -*-
"""底座自检：不启动服务器，用假 handler 验证桥接行为。"""
import io, json, sys
from pathlib import Path
ROOT = Path(r"E:/Development/FunScriptCast-Nexus")
sys.path.insert(0, str(ROOT))
from app.bridge import handle

class FakeHandler:
    def __init__(self, path, command="GET"):
        self.path = path
        self.command = command
        self.headers = {"Host": "127.0.0.1:8790"}
        self.wfile = io.BytesIO()
        self.status = None
        self.hdrs = {}
    def send_response(self, code):
        self.status = code
    def send_header(self, k, v):
        self.hdrs[k] = v
    def end_headers(self):
        pass

def call(path, method="GET", body=None):
    h = FakeHandler(path, method)
    hit = handle(h, method, path.split("?")[0], body)
    return hit, h.status, h.hdrs, h.wfile.getvalue()

ok = True
def check(name, cond, detail=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("  " + detail if detail else ""))
    ok = ok and cond

hit, st, hd, body = call("/api/v2/ping")
check("GET /api/v2/ping 命中", hit and st == 200, f"status={st}")
check("ping 返回 JSON", json.loads(body).get("pong") is True, body[:80].decode("utf-8", "replace"))

hit, st, hd, body = call("/api/v2/system")
sysinfo = json.loads(body)
check("GET /api/v2/system", hit and sysinfo.get("ok") is True, f"version={sysinfo.get('version')} py={sysinfo.get('python')}")
check("web_dist 已构建", sysinfo.get("web_dist", {}).get("built") is True, str(sysinfo.get("web_dist")))

hit, st, hd, body = call("/app/")
check("GET /app/ 命中", hit and st == 200, f"ctype={hd.get('Content-Type')}")
check("返回 index.html", b'<div id="root">' in body, f"{len(body)} bytes")

hit, st, hd, body = call("/app/assets/not-exist.js")
check("不存在的静态文件不吞（回落历史）", hit is False, f"hit={hit}")

hit, st, hd, body = call("/api/state")
check("历史路由回落", hit is False, f"hit={hit}")

hit, st, hd, body = call("/app/library")
check("SPA 回退到 index.html", hit and b'<div id="root">' in body, f"status={st}")

hit, st, hd, body = call("/app/../host_server.py")
check("路径穿越被拒", hit is False, f"hit={hit}")

hit, st, hd, body = call("/api/v2/nope")
check("未知 v2 接口回落", hit is False, f"hit={hit}")

print("\n结果:", "ALL PASS" if ok else "有失败项")
sys.exit(0 if ok else 1)
