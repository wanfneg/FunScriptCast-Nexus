#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AI 实时识别服务端（FunScriptCast Nexus · PC 端）

边播边推理：视频解码 → V-JEPA 视觉编码 → trunk+heads → funscript 动作流。
对外是一个 HTTP/JSON 服务，宿主（Nexus）只负责生命周期与转发，不含模型代码。

    python run_server.py                    # 读 config.json
    python run_server.py --port 8757

与 AI 字幕模块同构：宿主 sub_vision_start() 拉起 → 健康检查 → 空闲回收。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from service import VisionService  # noqa: E402


def load_config() -> dict:
    p = BASE / "config.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main() -> int:
    cfg = load_config()
    srv = cfg.get("server") or {}
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("FSC_VISION_PORT",
                                               srv.get("port", 8757))))
    ap.add_argument("--host", default=srv.get("host", "127.0.0.1"))
    args = ap.parse_args()

    svc = VisionService(cfg)
    from http.server import ThreadingHTTPServer
    httpd = ThreadingHTTPServer((args.host, args.port), svc.handler())
    print(f"[vision] listening on http://{args.host}:{args.port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        svc.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
