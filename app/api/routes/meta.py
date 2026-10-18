"""/api/v2 元信息：底座自检用，不碰历史逻辑。"""
from __future__ import annotations

from ..registry import Request, Response, route


@route("GET", "/api/v2/ping")
def ping(req: Request) -> Response:
    return Response(body={"ok": True, "pong": True})


@route("GET", "/api/v2/system")
def system(req: Request) -> Response:
    from app.config import info
    return Response(body={"ok": True, **info()})


@route("GET", "/api/v2/routes")
def list_routes(req: Request) -> Response:
    from ..registry import routes
    return Response(body={"ok": True, "routes": [
        {"method": m, "path": p} for m, p, _ in routes()
    ]})
