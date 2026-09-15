#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""启动 AI 字幕服务端

  .venv/Scripts/python.exe run_server.py            # 读 config.json
  .venv/Scripts/python.exe run_server.py --port 8756 --host 0.0.0.0
"""

import argparse
import json
import sys
from pathlib import Path

import uvicorn

BASE = Path(__file__).resolve().parent
# 自包含安装的 embeddable Python 用 ._pth 封闭 sys.path，不含脚本所在目录，
# 这里显式补上，否则 uvicorn 找不到同目录的 server_app（实测 Not Found）。
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

if __name__ == "__main__":
    cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
    srv = cfg.get("server", {})
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=srv.get("host", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=srv.get("port", 8756))
    ap.add_argument("--reload", action="store_true")
    args = ap.parse_args()
    print(f"[run] http://{args.host}:{args.port}  （局域网内 Quest 用 PC 的 IP 访问）")
    uvicorn.run("server_app:app", host=args.host, port=args.port,
                reload=args.reload, log_level="info")
