"""应用级配置与路径（唯一真相来源）。

历史代码里的 data_dir()/APP_DIR 仍在 host_server.py；新代码一律从这里取。
两者需要保持一致时，由 host_server 通过 bind_runtime() 注入。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

APP_ROOT: Path = Path(os.environ.get("FSC_APP_ROOT") or Path(__file__).resolve().parent.parent)
WEB_DIST: Path = APP_ROOT / "web" / "dist"
LEGACY_UI: Path = APP_ROOT / "ui"

_runtime: dict = {}


def bind_runtime(**kw) -> None:
    """由 host_server 注入的运行时事实（端口、数据目录等）。"""
    _runtime.update(kw)


def runtime() -> dict:
    return dict(_runtime)


def version_info() -> dict:
    """读 version.json；读不到就返回空壳，绝不抛。"""
    for p in (APP_ROOT / "version.json", Path(sys.executable).parent / "version.json"):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
    return {}


def info() -> dict:
    """给 /api/v2/system 的机器可读快照。"""
    v = version_info()
    rt = runtime()
    return {
        "app_root": str(APP_ROOT),
        "version": str(v.get("versionName") or "0.0.0"),
        "version_code": int(v.get("versionCode") or 0),
        "channel": str(v.get("channel") or "dev"),
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "web_dist": {"path": str(WEB_DIST), "built": (WEB_DIST / "index.html").is_file()},
        "legacy_ui": (LEGACY_UI / "index.html").is_file(),
        "runtime": rt,
    }
