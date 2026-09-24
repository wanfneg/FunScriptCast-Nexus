# -*- coding: utf-8 -*-
"""设备端同步目录护栏：目标必须是**专用子目录**，不能是存储根或系统/应用树（评审 F28）。

## 为什么必须有

`delete_extra` 打开时同步收尾会执行

    find <目标目录> -mindepth 1 -depth -type d -empty -delete

把该目录下所有空目录清掉。若目标填成 `/storage/emulated` 或 `/sdcard/Android`，
这条命令就会在**整个共享存储 / 应用私有树**里枚举并删空目录——一次手滑配置即可越界
清目录（相册、其他应用的数据目录都在射程内）。

## 原来的问题

`funscript_sync` / `video_sync` 各写了一份 5 项清单，且只做**精确**比对：
漏了 `/storage/emulated`、`/sdcard/Android`、`/storage`、`/mnt`、`/system`、`/data`。
本模块把判据收成一份，两个 sync 模块共用（同名规则只写一遍）。

## 判定规则（任一命中即拒）

  1. 归一化后**等于**清单项（`/`、`/sdcard`、`/storage/emulated/0` …）；
  2. 归一化后是清单项的**祖先**——`/storage` 之于 `/storage/emulated`，删除范围一样大；
  3. 落在"绝不作为目标"的子树里——`/sdcard/Android`、`/system`、`/data`、`/vendor`；
  4. 归一化后**段数 < 2**（挂在设备根下的单段目录，如 `/sdcard` 已被 1 覆盖，
     但 `/foo` 这类同样不该当同步目标）。

正常目标（`/sdcard/Funscript`、`/sdcard/Movies`）全都不命中，行为不变。
"""
from __future__ import annotations

import posixpath

# 精确匹配的"存储根/系统根"
UNSAFE_EXACT = (
    "/", "/sdcard", "/mnt", "/mnt/sdcard", "/storage", "/storage/emulated",
    "/storage/emulated/0", "/storage/emulated/legacy", "/storage/self",
    "/system", "/data", "/vendor", "/proc", "/sys", "/dev",
)

# 绝不作为目标的**子树**（应用私有目录、系统树）
UNSAFE_PREFIX = (
    "/sdcard/android", "/storage/emulated/0/android", "/storage/emulated/legacy/android",
    "/storage/emulated/android",
    "/system", "/data", "/vendor", "/proc", "/sys", "/dev",
)


def normalize(device_folder: str) -> str:
    """设备端 POSIX 路径归一化（折叠 `..`、`//`、尾部斜杠）。"""
    p = posixpath.normpath(str(device_folder or "").replace("\\", "/"))
    return ("/" + p.lstrip("/")).rstrip("/") or "/"


def normalize_and_check(device_folder: str) -> "tuple[str, str]":
    """返回 `(归一化路径, 拒绝原因)`；安全时原因为空串。

    **不抛异常**：调用方各自抛自己的异常类型（两个 sync 模块的 `InvalidOperationException`
    是各自定义的），这里只给判据与理由，避免把异常类型耦合进来。
    """
    dev_path = normalize(device_folder)
    low = dev_path.casefold()
    roots = tuple(u.casefold() for u in UNSAFE_EXACT)

    if low in roots:
        return dev_path, ("设备目录过宽，禁止使用根目录/存储根目录进行同步，以免误删文件"
                          f"（{dev_path}）")
    # 祖先检查：low 是某个清单项的父级 ⇒ 删除范围一样大
    if any(u.startswith(low + "/") for u in roots if u != "/"):
        return dev_path, ("设备目录是存储根目录的上级，同步删除会越界，请选一个专用子目录"
                          f"（{dev_path}）")
    if any(low == p or low.startswith(p + "/") for p in UNSAFE_PREFIX):
        return dev_path, ("该目录属于应用私有/系统目录，禁止作为同步目标，"
                          f"以免误删其他应用的数据（{dev_path}）")
    if len([s for s in low.split("/") if s]) < 2:
        return dev_path, f"设备目录必须是一个专用子目录（如 /sdcard/Funscript），当前：{dev_path}"
    return dev_path, ""
