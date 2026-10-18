"""传输无关的路由注册表。

设计目的：新接口先在这里注册，由 app.bridge 适配到当前的 http.server；
将来整体换 FastAPI/ASGI 时，只需再写一个适配器，业务函数不用动。

约定：
  * 路径精确匹配（参数化路径以后再加，别提前抽象）；
  * handler 返回 Response；返回 None 视为"未处理"；
  * handler 内抛异常 -> 500，不吞。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, list[str]] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    body: Any = None

    def q(self, key: str, default: str = "") -> str:
        v = self.query.get(key)
        return v[0] if v else default


@dataclass
class Response:
    status: int = 200
    body: Any = None                      # dict/list/str -> JSON
    raw: bytes | None = None              # 非 None 时直接作为响应体
    content_type: str = "application/json; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)


Handler = Callable[[Request], Response | None]
_routes: list[tuple[str, str, Handler]] = []


def route(method: str, path: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        _routes.append((method.upper(), path, fn))
        return fn
    return deco


def routes() -> list[tuple[str, str, Handler]]:
    return list(_routes)


def dispatch(req: Request) -> Response | None:
    for method, path, fn in _routes:
        if method == req.method and path == req.path:
            return fn(req)
    return None


def load_builtin_routes() -> None:
    """显式导入路由模块（保持注册顺序稳定、可测）。"""
    from .routes import meta  # noqa: F401
