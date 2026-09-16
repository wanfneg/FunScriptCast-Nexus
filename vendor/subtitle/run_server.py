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

# 文件日志：宿主拉起时 stdout 只在宿主内存里留 40 行，事后无法排查
# （2026-09-16 排查"字幕少"时发现）。这里把输出同步落到 logs/run_server.log
# （追加、>5MB 轮换一次），谁拉起服务都能事后看到完整请求/错误轨迹。
_log_dir = BASE / "logs"
_log_dir.mkdir(exist_ok=True)
_log_path = _log_dir / "run_server.log"
if _log_path.exists() and _log_path.stat().st_size > 5 * 1024 * 1024:
    _old = _log_dir / "run_server.old.log"
    _old.unlink(missing_ok=True)
    _log_path.rename(_old)


class _Tee:
    def __init__(self, *streams):
        self._streams = streams

    def write(self, data):
        for s in self._streams:
            try:
                s.write(data)
                s.flush()
            except Exception:
                pass

    def flush(self):
        for s in self._streams:
            try:
                s.flush()
            except Exception:
                pass

    def isatty(self):
        return False   # uvicorn 日志格式器探测终端用；非 TTY 输出纯文本


if not getattr(sys, "frozen", False) or True:   # 任何启动方式都落文件日志
    _log_file = open(_log_path, "a", buffering=1, encoding="utf-8", errors="replace")
    sys.stdout = _Tee(sys.stdout, _log_file)
    sys.stderr = _Tee(sys.stderr, _log_file)
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
