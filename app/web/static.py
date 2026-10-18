"""把 web/dist（Vite 构建产物）挂到 /app/ 下。

规则：
  * /app 或 /app/        -> index.html
  * /app/<存在文件>       -> 原样返回
  * /app/<无扩展名路径>   -> index.html（SPA 回退，交给前端路由）
  * 其余（不存在且有扩展名、或越界）-> None（交还给历史路由）
"""
from __future__ import annotations

import mimetypes
from pathlib import Path

from ..api.registry import Response
from ..log import get_logger

log = get_logger("web")
PREFIX = "/app"

_CTYPE = {
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".webp": "image/webp",
    ".woff2": "font/woff2",
    ".map": "application/json; charset=utf-8",
}


def _ctype(p: Path) -> str:
    if p.suffix.lower() in _CTYPE:
        return _CTYPE[p.suffix.lower()]
    guess, _ = mimetypes.guess_type(p.name)
    return guess or "application/octet-stream"


def _read(p: Path, index: bool) -> Response:
    data = p.read_bytes()
    cache = "no-cache" if index or p.suffix == ".html" else "public, max-age=31536000, immutable"
    return Response(raw=data, content_type=_ctype(p),
                    headers={"Cache-Control": cache})


def static_response(path: str) -> Response | None:
    from ..config import WEB_DIST

    if path == PREFIX:
        path = PREFIX + "/"
    if not path.startswith(PREFIX + "/"):
        return None
    rel = path[len(PREFIX) + 1:].split("?")[0].strip("/") or "index.html"
    if not WEB_DIST.is_dir():
        return None

    root = WEB_DIST.resolve()
    try:
        target = (root / rel).resolve()
    except OSError:
        return None
    if not target.is_relative_to(root):
        log.warning("越界路径被拒绝：%s", rel)
        return None

    if target.is_file():
        return _read(target, index=(rel == "index.html"))
    if not Path(rel).suffix:
        index = root / "index.html"
        if index.is_file():
            return _read(index, index=True)
    return None
