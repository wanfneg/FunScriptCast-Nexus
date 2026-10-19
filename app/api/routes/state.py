"""只读状态接口（P1 第一批：state / logs / library.state）。"""
from __future__ import annotations

from ..registry import Request, Response, route
from ..services import get


@route("GET", "/api/v2/state")
def state(req: Request) -> Response:
    return Response(body=get("state"))


@route("GET", "/api/v2/logs")
def logs(req: Request) -> Response:
    return Response(body={"ok": True, "logs": get("logs")})


@route("GET", "/api/v2/library/state")
def library_state(req: Request) -> Response:
    return Response(body=get("library_state"))
