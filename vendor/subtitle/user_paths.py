# -*- coding: utf-8 -*-
"""用户数据目录：**所有"会变的"数据都不放在安装目录里**。

## 为什么要有这个模块

把「会变的用户数据」放在 `{app}\\vendor\\subtitle\\` 里，一个设计问题同时撑起了三个症状：

  ① **删掉安装目录重装 = 半重置**：云端 key 与术语表没了，而 `%APPDATA%` 里的宿主设置
     （DLNA 根目录等）还在——用户看到的是"我明明删干净了，怎么还有数据"，而且**没有任何
     受支持的复位路径**。
  ② **升级安装丢数据**：installer 的 `[Files]` 是整树覆盖，把用户的 key 与术语表换成
     打包机副本（installer 侧已用 `onlyifdoesntexist` 兜住，但根因在这里）。
  ③ **工具链复杂度**：`sync_distapp` / `build_exe` / `build_installer` 都得给这两样写
     "摘出→回填"的特例。

## 怎么分

把两类东西分开：

  · **安装目录里只留模板/出厂基线**（`config.json`、`glossary_*.json`）——随包发、可被升级覆盖；
  · **用户数据一律落 `%APPDATA%\\FunScriptCast-Nexus\\`**，与 `integrated_settings.json`
    同处一个目录：**一处、一个寿命、一个清理入口**。

首次运行时把安装目录里的旧文件**迁移**过来（只在新位置不存在时复制），老用户不丢 key。

## 解析顺序（config.json）

  1. 新位置存在            → 用它
  2. 否则安装目录的旧文件存在 → 迁移过来再用它（**保留 key**）
  3. 否则（全新安装）       → 直接读安装目录的模板（只读，保存时写新位置）

**保存永远写新位置**，安装目录那份从此只是模板。

## 环境变量

  `NEXUS_USER_DIR` —— 覆盖整个用户数据目录。评测/测试必须设它，否则会写到真实用户目录。

## 一条给未来的约定

用户数据文件是**首次运行时的快照**，之后不随包更新。所以**新加的配置键必须在代码里带默认值**
（`cfg.get("新键", 默认)`），不能指望模板里那份能传到老用户手里——`asr.whisper`、
`drop_latin_hallucination` 都是这么处理的。
"""
from __future__ import annotations

import json
import os
import shutil
import threading
from pathlib import Path

# 术语表文件名（与 host_server.GLOSSARY_FILES 同源；config 的 glossary 段只写文件名）
GLOSSARY_NAMES = ("glossary_ja_zh.json", "glossary_en_zh.json")

_migrate_lock = threading.Lock()
_migrated: set = set()


def user_dir() -> Path:
    """用户数据目录（与 integrated_settings.json 同一个目录）。"""
    env = os.environ.get("NEXUS_USER_DIR")
    if env:
        return Path(env)
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "FunScriptCast-Nexus"


def _ensure_dir() -> Path:
    d = user_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def _migrate_once(src: Path, dst: Path) -> bool:
    """src → dst 的一次性迁移（dst 已存在则跳过）。失败只记不抛——启动不能因为迁移挂掉。

    写临时文件再 os.replace：两个进程同时迁（宿主与服务可能并发首启）时，
    最坏是各写一份同样内容，不会留下半截文件。
    """
    key = str(dst).lower()
    with _migrate_lock:
        if key in _migrated:
            return dst.exists()
        _migrated.add(key)
    try:
        if dst.exists() or not src.exists():
            return dst.exists()
        _ensure_dir()
        tmp = dst.with_suffix(dst.suffix + ".migrating")
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
        print(f"[paths] 已迁移用户数据：{src} → {dst}", flush=True)
        return True
    except Exception as e:
        print(f"[paths] 迁移 {src} 失败（忽略，回退读旧位置）：{type(e).__name__}: {e}",
              flush=True)
        return False


# ---- 一次性补救扫描（布局 v2 = 用户数据搬出安装目录的那一版）---------------------------
#
# 迁移是**先到先得**的：目标文件一旦存在，后来的源就不再被采纳。这在单一实例下是对的，
# 但本项目同时存在两个实例共用一个用户目录——① 装好的程序（dist-app 里的真身，
# 云端 key 在这儿）；② 仓库里直接跑（模板 key 为空）。谁先启动谁定音。
# 实测已经踩中：仓库侧先跑了一次，用**空 key 的模板**播了种；之后装好的程序再启动，
# 目标已存在 ⇒ 真实 key 永远迁不过去，用户看到的是"key 莫名其妙没了"。
#
# 所以补一次**只跑一次**的补救：在用户目录里落个标记，标记不存在时，用「有值者为准」
# 兜一遍 key、用「条数多者为准」兜一遍术语表（被覆盖的那份先备份）。
# 标记写完就不再回头——把这点"聪明"限制在升级过渡这一次，之后永远是简单的先到先得。
_LAYOUT_MARKER = ".layout-v2"
_layout_checked: set = set()


def _openai_key(p: Path) -> "tuple[str, str]":
    """(api_key, api_key_env)；读不动就是两个空串。"""
    try:
        t = (json.loads(p.read_text(encoding="utf-8")).get("translate") or {}).get("openai") or {}
        return str(t.get("api_key") or "").strip(), str(t.get("api_key_env") or "").strip()
    except Exception:
        return "", ""


def _entry_count(p: Path) -> int:
    try:
        j = json.loads(p.read_text(encoding="utf-8"))
        return len(j) if isinstance(j, dict) else 0
    except Exception:
        return 0


def _backup(p: Path) -> None:
    """被补救覆盖前先留一份后悔药（已有备份就不再叠）。"""
    bak = p.with_suffix(p.suffix + ".bak-layout-v2")
    try:
        if p.exists() and not bak.exists():
            shutil.copy2(p, bak)
    except OSError:
        pass


def _recover_layout_v2(base: Path) -> None:
    """升级过渡的一次性补救：安装目录里那份**更值钱**就采纳它。"""
    d = _ensure_dir()
    stamp = d / _LAYOUT_MARKER
    try:
        if stamp.exists():
            return
    except OSError:
        return
    try:
        dst = d / "subtitle_config.json"
        legacy = base / "config.json"
        if dst.exists() and legacy.exists():
            k_new, env_new = _openai_key(dst)
            k_old, _ = _openai_key(legacy)
            # 只在"用户这份没有任何 key 来源"时才让安装目录那份说话：
            # 用户若显式用 api_key_env（把 key 放在环境变量里），空 api_key 是他有意为之。
            if k_old and not k_new and not env_new:
                _backup(dst)
                shutil.copy2(legacy, dst)
                print(f"[paths] 补救：用户配置的 api_key 为空而安装目录里有值 → 采纳安装目录"
                      f"（原文件已备份 {dst.name}.bak-layout-v2）", flush=True)
        for name in GLOSSARY_NAMES:
            g_new, g_old = d / name, base / name
            if not (g_new.exists() and g_old.exists()):
                continue
            n_new, n_old = _entry_count(g_new), _entry_count(g_old)
            # 术语表只增不减地攒；条数明显多的那份是更晚的状态。用 >= +1 免得等于时白折腾。
            if n_old > n_new:
                _backup(g_new)
                shutil.copy2(g_old, g_new)
                print(f"[paths] 补救：术语表 {name} 安装目录 {n_old} 条 > 用户目录 {n_new} 条 → "
                      f"采纳安装目录（原文件已备份）", flush=True)
        stamp.write_text("用户数据布局 v2：一次性补救扫描已完成\n", encoding="utf-8")
    except Exception as e:
        print(f"[paths] 布局补救扫描失败（忽略，继续用现有文件）：{type(e).__name__}: {e}",
              flush=True)


def _check_layout(base: Path) -> None:
    key = str(base).lower()
    if key in _layout_checked:
        return
    _layout_checked.add(key)
    _recover_layout_v2(base)


def config_path(base_dir: Path) -> Path:
    """字幕服务配置文件路径。

    全新安装时新位置并不存在——此时返回**安装目录里的模板**（调用方只读它取默认值）；
    一旦宿主保存过一次配置，新位置就有了，之后永远走新位置。
    """
    base = Path(base_dir)
    _check_layout(base)
    legacy = base / "config.json"
    dst = _ensure_dir() / "subtitle_config.json"
    if dst.exists():
        return dst
    if _migrate_once(legacy, dst):
        return dst
    return legacy            # 模板：只读兜底（保存会写新位置）


def glossary_path(base_dir: Path, name: str) -> Path:
    """术语表路径。首次运行时从安装目录的出厂基线迁移一份过来。"""
    base = Path(base_dir)
    _check_layout(base)
    legacy = base / name
    dst = _ensure_dir() / name
    if dst.exists():
        return dst
    if _migrate_once(legacy, dst):
        return dst
    return legacy            # 出厂基线：只读兜底


def ensure_user_data(base_dir: Path) -> dict:
    """把该迁的都迁一遍，返回解析结果（宿主启动时调一次即可）。

    返回 {"dir", "config", "glossary": {name: path}, "migrated": [...]}。
    """
    base = Path(base_dir)
    out = {"dir": _ensure_dir(), "config": config_path(base), "glossary": {}, "migrated": []}
    for n in GLOSSARY_NAMES:
        p = glossary_path(base, n)
        out["glossary"][n] = p
        if p.parent == out["dir"]:
            out["migrated"].append(n)
    return out


def load_config(base_dir: Path) -> dict:
    """读配置（新位置优先，缺失时读安装目录模板）。解析失败抛异常，由调用方决定怎么办。

    **顺带把该迁的都迁一遍**：迁移如果只挂在 config 上，调用方就得自己记得"迁了
    config 还要迁词表"，漏一句就是一种静默的半迁移——config 到了新位置、词表还在
    旧位置，而服务按 `USER_DIR` 找词表，于是**用户的词表凭空变成空表**（不报错）。
    把迁移收进这一个入口，任何读配置的路径都拿到完整状态。
    """
    base = Path(base_dir)
    ensure_user_data(base)
    return json.loads(config_path(base).read_text(encoding="utf-8"))
