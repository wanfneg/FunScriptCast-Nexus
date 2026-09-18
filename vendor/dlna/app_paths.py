# -*- coding: utf-8 -*-
"""DLNA 运行数据目录：**安装目录下的 `data\\`**，不写 C 盘。

与 Nexus 其余数据同源（`vendor\\subtitle\\user_paths.py`）：同一个安装目录、同一个
`data\\` 子目录 —— 用户装到 D 盘就全在 D 盘，删掉安装目录就一起走，不留"程序没了、
设置还在"的错觉。

为什么单独成一个模块而不是 import `user_paths`：VR-DLNA 本来就能独立运行（自带托盘、
自己的 README），不该依赖同级的另一个 vendor 包；而写在 `vr_dlna.py` 里再由两个 sync
模块 import 会绕进循环（`vr_dlna` → `funscript_sync_ui` → `funscript_sync`）。
本模块**不 import 同目录任何模块**，谁都安全地用它。

历史：exe 模式曾写 `%APPDATA%\\VR-DLNA\\`（源码模式写脚本目录，是两套行为）。
首次运行会把旧位置那几份 JSON 迁过来；日志（`*.log`）不迁 —— 那是运行产物。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def app_root() -> Path:
    """安装目录：exe 模式 = exe 所在目录；源码模式 = 仓库根。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # <安装目录>\vendor\dlna\app_paths.py → 上两级
    return Path(__file__).resolve().parents[2]


def legacy_appdata_dir() -> Path:
    """旧位置（exe 模式用过）：`%APPDATA%\\VR-DLNA`。"""
    return Path(os.environ.get("APPDATA") or str(Path.home())) / "VR-DLNA"


def data_dir() -> Path:
    """运行数据目录 `<安装目录>\\data`（顺带把旧位置的 JSON 迁过来，只迁一次）。"""
    d = app_root() / "data"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        return d
    _migrate_legacy(d)
    return d


def _migrate_legacy(dst_dir: Path) -> None:
    """旧位置 → `data\\`：只搬 `*.json`（设置与两个 sync 的配置），日志留在原地。

    逐个文件判断"目标不存在才搬"——用户可能已经在新位置改过设置，不能被旧文件盖回去。
    """
    src_dir = legacy_appdata_dir()
    try:
        if not src_dir.is_dir() or src_dir.resolve() == dst_dir.resolve():
            return
        for f in src_dir.glob("*.json"):
            dst = dst_dir / f.name
            if dst.exists():
                continue
            try:
                tmp = dst.with_suffix(dst.suffix + ".migrating")
                shutil.copy2(f, tmp)
                os.replace(tmp, dst)
                print(f"[dlna] 已迁移运行数据：{f} → {dst}", flush=True)
            except OSError:
                pass
    except OSError:
        pass
