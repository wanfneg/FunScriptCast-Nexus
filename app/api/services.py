"""运行期由宿主注入的只读数据源。

P1 阶段的原则：路由搬家，逻辑不动。新路由调用宿主注册进来的函数，
返回结构与旧接口完全一致；等前端全部切到 v2 之后，再把实现搬进 app/services/。
"""
from __future__ import annotations

from typing import Any, Callable

_providers: dict[str, Callable[[], Any]] = {}


def bind(**providers: Callable[[], Any]) -> None:
    _providers.update(providers)


def bound() -> list[str]:
    return sorted(_providers)


def get(name: str) -> Any:
    fn = _providers.get(name)
    if fn is None:
        raise RuntimeError(f"服务未绑定: {name}（宿主启动时应调用 app.api.services.bind）")
    return fn()
