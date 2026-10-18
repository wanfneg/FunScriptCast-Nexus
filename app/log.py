"""统一的日志出口。历史代码用标准 logging；新代码复用同一个 logger 名。"""
from __future__ import annotations

import logging

ROOT = "nexus"


def get_logger(name: str = ROOT) -> logging.Logger:
    return logging.getLogger(name if name.startswith(ROOT) else f"{ROOT}.{name}")
