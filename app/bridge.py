"""http.server 与新底座之间的适配层。

host_server 在路由链最前面调用 handle()：
  * 返回 True  -> 新底座已经写出响应，历史路由不要再碰；
  * 返回 False -> 未命中，原样落回历史 if/elif 链。
"""
from __future__ import annotations

import json
import urllib.parse

from .api.registry import Request, dispatch, load_builtin_routes
from .log import get_logger
from .web.static import static_response

log = get_logger("bridge")
_loaded = False


def _ensure_routes() -> None:
    global _loaded
    if not _loaded:
        load_builtin_routes()
        _loaded = True


def _send(handler, resp) -> None:
    if resp.raw is not None:
        data = resp.raw
    else:
        data = json.dumps(resp.body if resp.body is not None else {},
                          ensure_ascii=False).encode("utf-8")
    handler.send_response(resp.status)
    handler.send_header("Content-Type", resp.content_type)
    handler.send_header("Content-Length", str(len(data)))
    for k, v in resp.headers.items():
        handler.send_header(k, v)
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(data)


def handle(handler, method: str, path: str, body=None) -> bool:
    try:
        _ensure_routes()
        query = urllib.parse.parse_qs(urllib.parse.urlparse(handler.path).query)
        req = Request(method=method.upper(), path=path, query=query,
                      headers={k: v for k, v in handler.headers.items()}, body=body)
        resp = dispatch(req)
        if resp is None:
            resp = static_response(path)
        if resp is None:
            return False
        _send(handler, resp)
        return True
    except Exception as e:  # noqa: BLE001
        log.exception("新底座处理失败: %s %s", method, path)
        try:
            _send(handler, __import__("app.api.registry", fromlist=["Response"]).Response(
                status=500, body={"ok": False, "error": f"{type(e).__name__}: {e}"}))
        except Exception:
            pass
        return True
