# -*- coding: utf-8 -*-
"""PyInstaller 构建补丁：修 Python 3.10.0 的 dis._unpack_opargs。

背景
----
CPython 3.10.0 的 `dis._unpack_opargs()` 在遇到不带参数的指令时**没有重置**
extended_arg 状态：

    # 3.10.0（有 bug）
    if op >= HAVE_ARGUMENT:
        arg = code[i+1] | extended_arg
        extended_arg = (arg << 8) if op == EXTENDED_ARG else 0
    else:
        arg = None            # ← 这里漏了 extended_arg = 0

结果：某些大模块（如 bottle.py，pywebview 的依赖）会算出越界常量索引，
`dis.get_instructions()` 抛 `IndexError: tuple index out of range`，
PyInstaller 的 modulegraph 在分析阶段直接崩掉。

3.10.2+ 的写法（等价于下面 patched 的实现）：

    if op >= HAVE_ARGUMENT:
        arg = code[i+1] | extended_arg
        extended_arg = (arg << 8) if op == EXTENDED_ARG else 0
    else:
        arg = None
        extended_arg = 0

这里在构建前把 dis._unpack_opargs 换掉，不改动系统 Python。
"""
from __future__ import annotations

import dis
import sys


def _patched_unpack_opargs(code):
    extended_arg = 0
    for i in range(0, len(code), 2):
        op = code[i]
        if op >= dis.HAVE_ARGUMENT:
            arg = code[i + 1] | extended_arg
            extended_arg = (arg << 8) if op == dis.EXTENDED_ARG else 0
        else:
            arg = None
            extended_arg = 0        # ← 关键修复
        yield (i, op, arg)


def apply() -> bool:
    """打补丁；返回是否真的改了东西（已经是修复版则返回 False）。"""
    if getattr(dis._unpack_opargs, "_nexus_patched", False):
        return False
    _patched_unpack_opargs._nexus_patched = True  # type: ignore[attr-defined]
    dis._unpack_opargs = _patched_unpack_opargs
    return True


def selftest(path: str = "") -> None:
    """自检：对指定文件（默认 bottle.py）跑一遍 dis，确认不再崩。"""
    import importlib.util

    target = path
    if not target:
        spec = importlib.util.find_spec("bottle")
        target = spec.origin if spec and spec.origin else ""
    if not target:
        print("selftest: 找不到 bottle.py，跳过")
        return
    with open(target, encoding="utf-8") as f:
        src = f.read()
    co = compile(src, target, "exec")
    n = sum(1 for _ in dis.get_instructions(co))
    print(f"selftest OK: {target} 指令数={n}  python={sys.version.split()[0]}")


if __name__ == "__main__":
    changed = apply()
    print("patched:", changed)
    selftest()
