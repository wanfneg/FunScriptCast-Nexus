# -*- coding: utf-8 -*-
"""FunScriptCast-Nexus —— 宿主进程（后端）

职责：
  1. 拉起 / 停止 **DLNA 服务**（复用 vendor/dlna 的 DlnaApp + SSDPServer）
  2. 拉起 / 停止 **AI 字幕服务子进程**（vendor/subtitle，独立进程，
     停止即释放显存）
  3. 托管前端静态资源 + 提供 JSON API（同一端口，纯 stdlib http.server，无额外依赖）
  4. 持有设置与运行日志，供前端轮询

设计要点：
  - **单进程 + 一托盘**：DLNA 与 UI 同进程（import 复用，0.06s）；字幕服务是子进程，
    崩溃/显存可独立回收。
  - 所有阻塞操作（启动 DLNA、拉起子进程）都在后台线程里做，API 立刻返回，
    前端靠 /api/state 轮询拿状态。
  - 无第三方 Web 框架：只用 http.server，内存开销小、启动快。
"""

from __future__ import annotations

import csv
import ctypes
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 打包成 EXE（PyInstaller）后 __file__ 指向解包临时目录，必须用 exe 所在目录；
# ui/ 与 vendor/ 作为**外置数据**跟 EXE 放一起，方便查看与替换，也避免每次
# 启动解压几十 MB 到 %TEMP%。
FROZEN = bool(getattr(sys, "frozen", False))
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
UI_DIR = APP_DIR / "ui"
VENDOR_DIR = APP_DIR / "vendor"
MODELS_DIR = APP_DIR / "models"

# ---------------------------------------------------------------- 内置依赖路径
# 两个原本独立运行的项目已 vendor 进本仓库：vendor/dlna（DLNA 服务）、
# vendor/subtitle（ASR + 翻译服务）。模型与 venv 也随仓库自带，
# 因此本应用不再依赖 E:\Development 下的任何其他目录。
VRDLNA_DIR = Path(os.environ.get("VRDLNA_DIR", str(VENDOR_DIR / "dlna")))
SUBTITLE_DIR = Path(os.environ.get("SUBTITLE_DIR", str(VENDOR_DIR / "subtitle")))


def _subtitle_python() -> Path:
    """字幕服务用的解释器：自带 venv → 上级目录的 venv → PATH 上的 python。

    字幕服务依赖 torch/uvicorn（约 5 GB），不随 EXE 打包，仍走 .venv 子进程。
    dist-app 里的 EXE 单独拿出来跑时没有 .venv，会一路退到系统 Python 而缺依赖
    （实测报 "No module named 'uvicorn'"）。

    打包版额外看**上一级目录**的 .venv：dist-app 通常就放在源码仓库里，上一级
    那个 .venv 是现成的，没必要再复制 5 GB 一份。这不是"自包含"，所以挑中的
    来源会记进日志，免得以后误以为这个包拷到别的机器也能跑。
    """
    global SUBTITLE_PY_SOURCE
    env = os.environ.get("NEXUS_PY")
    if env:
        SUBTITLE_PY_SOURCE = "环境变量 NEXUS_PY"
        return Path(env)
    # 自包含安装（installer）自带轻量运行时：embeddable Python + fastapi/uvicorn/
    # numpy（audiocpp 主路径不需要 torch，见 asr_engine 的懒加载说明）。放第一位，
    # 命中即"自包含"，不再借用任何外部 .venv。
    candidates = [
        (APP_DIR / "runtime" / "python.exe", "自带运行时（自包含安装）"),
        (APP_DIR / ".venv" / "Scripts" / "python.exe", "应用目录自带的 .venv"),
        (APP_DIR / ".venv" / "bin" / "python", "应用目录自带的 .venv"),
    ]
    if FROZEN:
        candidates += [
            (APP_DIR.parent / ".venv" / "Scripts" / "python.exe", "上一级目录的 .venv（非自包含）"),
            (APP_DIR.parent / ".venv" / "bin" / "python", "上一级目录的 .venv（非自包含）"),
        ]
    for c, src in candidates:
        if c.exists():
            SUBTITLE_PY_SOURCE = src
            return c
    found = shutil.which("python") or shutil.which("python3")
    if found:
        SUBTITLE_PY_SOURCE = f"PATH 上的 python（多半缺依赖）：{found}"
        return Path(found)
    SUBTITLE_PY_SOURCE = "当前解释器（不可用）"
    return Path(sys.executable)


# 解释器是从哪儿来的（启动时记进日志，便于判断这个包是不是自包含）
SUBTITLE_PY_SOURCE = ""
SUBTITLE_VENV_PY = _subtitle_python()

# ---------------------------------------------------------------- 端口
UI_API_PORT = int(os.environ.get("FS_HOST_PORT", "8790"))   # 前端 + API（仅环回）
SUBTITLE_PORT = int(os.environ.get("FS_SUBTITLE_PORT", "8756"))
# 头显（Quest/PICO）专用接口：绑 0.0.0.0，但只放开三个路由。
# 为什么不把 8790 直接绑到局域网：那上面还有设置、术语表、设备同步、adb、退出……
# 单独一个端口 + 单独一个 Handler，按构造就不可能误暴露，而不是靠一处
# "记得判断 client_address"的检查（漏一处就全开）。
LAN_API_PORT = int(os.environ.get("FS_HOST_LAN_PORT", "8791"))
DLNA_PORT_DEFAULT = 8899

log = logging.getLogger("host")

# ================================================================ 运行状态
class Runtime:
    """宿主进程的可变状态（受 _lock 保护）。"""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.started_at = time.time()
        self.dlna_server = None
        self.dlna_ssdp = None
        self.dlna_port = DLNA_PORT_DEFAULT
        self.dlna_starting = False
        # start/stop 代数：stop 时 +1，start worker 登记结果前核对——不一致说明
        # 期间用户点了停止，worker 必须撤下刚起的服务，而不是"停了个寂寞"后
        # 照样把它留下来（DLNA 与字幕子进程同一套机制）。
        self.dlna_gen = 0
        self.dlna_error = ""
        self.dlna_requests = 0
        self.sub_proc: subprocess.Popen | None = None
        self.sub_starting = False
        self.sub_gen = 0
        self.sub_error = ""
        self.sub_ready = False
        self.sub_last_probe = 0.0
        # 我们 spawn 字幕服务的时刻。用来判断 8756 上应答的到底是不是自己人：
        # 孤儿一定是在我们启动**之前**就存在的（见 _service_ours）。
        self.sub_spawn_ts = 0.0
        self.logs: list[dict] = []          # {ts, level, msg}
        self.dlna_logs: list[str] = []

    # ---- 日志 ----
    def add_log(self, msg: str, level: str = "info") -> None:
        with self.lock:
            self.logs.append({"ts": time.time(), "level": level, "msg": msg})
            if len(self.logs) > 500:
                del self.logs[: len(self.logs) - 500]

    def add_dlna_log(self, msg: str) -> None:
        with self.lock:
            self.dlna_logs.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
            if len(self.dlna_logs) > 500:
                del self.dlna_logs[: len(self.dlna_logs) - 500]


RT = Runtime()


# ================================================================ 字幕缓存
# 目标：同一视频看第二遍时不再重跑 ASR，直接读已生成的字幕。
# 键 = 视频身份（路径/大小/mtime）+ 语言 + 配置指纹（ASR 模型 + 术语表内容），
# 任何一项变了就自动失效，避免「换了术语表还在用旧字幕」。
SUBTITLE_CACHE_DIR = Path(os.environ.get("NEXUS_CACHE_DIR", str(APP_DIR / "cache" / "subtitles")))

# 翻译层磁盘缓存（键 = 后端+端点+模型+目标语言+原文，见 vendor/subtitle/translate_engine.py）。
# 与上面的字幕缓存分开：字幕缓存按"整段视频"存，这个按"批次原文"存，
# 作用是同一句话在别的视频里出现时也不用重新请求 LLM。
TRANSLATE_CACHE_DIR = Path(os.environ.get("NEXUS_CACHE_DIR", str(APP_DIR / "cache"))) / "translate"


def _video_identity(path: str) -> dict:
    """视频身份：优先用绝对路径 + 大小 + mtime；文件不存在时退化为路径哈希。"""
    p = Path(path)
    try:
        st = p.stat()
        return {"path": str(p.resolve()), "size": st.st_size, "mtime": int(st.st_mtime)}
    except Exception:
        return {"path": str(p), "size": 0, "mtime": 0}


def _config_fingerprint() -> str:
    """ASR 模型 + 分段/VAD 配置 + 翻译配置 + 两张术语表 + **管线源码**的指纹。

    源码签名是 2026-09-16 补上的关键洞：此前只指纹配置，判据/策略/引擎的代码
    修复（如漏译隐藏、比例摊时）都不会改变缓存键——旧管线的差字幕会一直被
    "当基线加载"，且跨引擎换用的旧译文也不会失效。"""
    parts: list = []
    try:
        cfg = json.loads((SUBTITLE_DIR / "config.json").read_text(encoding="utf-8"))
        parts.append(json.dumps({"asr": cfg.get("asr"), "vad": cfg.get("vad"),
                                 "segment": cfg.get("segment"),
                                 "translate": cfg.get("translate")},
                                ensure_ascii=False, sort_keys=True))
    except Exception:
        parts.append("no-config")
    try:
        py_files = sorted(SUBTITLE_DIR.glob("*.py"), key=lambda f: f.name)
        code_sig = hashlib.sha256("|".join(
            f"{f.name}:{hashlib.sha256(f.read_bytes()).hexdigest()}"
            for f in py_files).encode("utf-8")).hexdigest()[:16]
        parts.append(f"code:{code_sig}")
    except Exception:
        parts.append("code:missing")
    for lang, fname in GLOSSARY_FILES.items():
        f = SUBTITLE_DIR / fname
        try:
            parts.append(f"{lang}:{hashlib.sha256(f.read_bytes()).hexdigest()[:16]}")
        except Exception:
            parts.append(f"{lang}:missing")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def subtitle_cache_key(video_path: str, lang: str) -> str:
    ident = _video_identity(video_path)
    raw = json.dumps({"v": ident, "lang": lang, "cfg": _config_fingerprint()},
                     ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def subtitle_cache_path(video_path: str, lang: str) -> Path:
    return SUBTITLE_CACHE_DIR / f"{subtitle_cache_key(video_path, lang)}.json"


def _resolve_video_path(video_path: str) -> str:
    """VR 端传来的是设备侧路径（如 /sdcard/Movies/a.mp4）或 DLNA 流 URL，
    PC 上并不存在。此时按**文件名**在已知媒体根里找同名文件，命中就用它的
    身份算缓存键——这样头显和 PC 能共享同一份缓存。"""
    p = Path(video_path)
    if p.exists():
        return str(p)
    # DLNA 流 URL 常带查询串（`.../a.mp4?sid=1`）。必须先剥掉再取文件名，
    # 否则 Path.name 会把 "a.mp4?sid=1" 整个当成文件名，永远找不到同名文件——
    # 表现是"头显看第二遍仍然重跑 ASR"，没有任何报错，极难发现。
    raw = video_path.split("?", 1)[0].split("#", 1)[0]
    name = Path(raw).name or raw.rstrip("/").split("/")[-1]
    if not name:
        return video_path
    roots = list(load_settings().get("dlna_roots") or [])
    for key in ("video_folder", "script_folder"):
        v = load_settings().get(key)
        if v:
            roots.append(v)
    for root in roots:
        try:
            cand = Path(root) / name
            if cand.exists():
                return str(cand)
        except Exception:
            continue
    # 找不到同名文件：至少返回剥掉查询串的形式。DLNA 每次播放可能带不同的
    # sid/token，带着它算身份会让同一部片子每次都得到一个新缓存键，
    # 命中率恒为 0——而且照样一声不响。
    return raw


def subtitle_cache_get(video_path: str, lang: str) -> dict:
    resolved = _resolve_video_path(video_path)
    f = subtitle_cache_path(resolved, lang)
    if not f.exists():
        return {"ok": True, "hit": False, "resolved": resolved}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "hit": False, "error": f"缓存损坏：{e}"}
    return {"ok": True, "hit": True, "path": str(f), "resolved": resolved,
            "count": len(data.get("segments") or []),
            "cover_ms": data.get("cover_ms") or 0,
            "created_at": data.get("created_at"),
            "video": data.get("video"),
            "lang": data.get("lang"),
            "segments": data.get("segments") or []}


def subtitle_cache_save(video_path: str, lang: str, segments: list,
                        meta: dict | None = None) -> dict:
    if not isinstance(segments, list):
        return {"ok": False, "error": "segments 必须是数组"}
    resolved = _resolve_video_path(video_path)
    SUBTITLE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    f = subtitle_cache_path(resolved, lang)
    # 覆盖到的时间点：调用方（头显）用它判断这份缓存是不是"看完整了"。
    # 只看了前 20 分钟的缓存如果被当成命中，后 10 分钟就永远没有字幕——
    # 所以这个字段是必需的，不是装饰。
    cover_ms = 0
    for s in segments:
        try:
            cover_ms = max(cover_ms, int(s.get("end_ms") or 0))
        except Exception:
            continue
    payload = {
        "video": _video_identity(resolved),
        "requested": video_path,
        "lang": lang,
        "config_fingerprint": _config_fingerprint(),
        "created_at": time.time(),
        "count": len(segments),
        "cover_ms": cover_ms,
        "segments": segments,
        "meta": meta or {},
    }
    tmp = None
    try:
        # tmp 名必须**唯一**（pid+线程）：同一 video+lang 的两次保存会并发——两台头显
        # 播完同一部片、或客户端超时重发。固定名 `<key>.json.tmp` 会让两方互相踩：
        # Windows 上 Python 的 open 不共享删除，先完成的一方 os.replace 会因另一方
        # 仍持有该 tmp 而 WinError 32，这次保存直接失败（缓存丢了，"看第二遍不再重跑
        # ASR"就成了空话）；交错发生在 replace 之前时，落盘还可能是两次写入的混合。
        tmp = f.with_suffix(f".{os.getpid()}-{threading.get_ident()}.json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, f)
    except Exception as e:
        try:
            if tmp is not None:
                tmp.unlink(missing_ok=True)   # 失败不留残骸（否则会被算进缓存体积）
        except Exception:
            pass
        return {"ok": False, "error": str(e)}
    RT.add_log(f"字幕已缓存（{lang} · {len(segments)} 段 → {f.name}）", "ok")
    return {"ok": True, "path": str(f), "count": len(segments),
            "cover_ms": cover_ms, "lang": lang}


def subtitle_cache_summary() -> dict:
    """缓存概览（给 /api/state 用，避免每次轮询都读全部字幕）。"""
    return _dir_summary(SUBTITLE_CACHE_DIR, "*.json")


def _dir_summary(root: Path, pattern: str) -> dict:
    n = 0
    total = 0
    newest = 0.0
    if root.exists():
        for f in root.glob(pattern):
            try:
                st = f.stat()
            except Exception:
                continue
            n += 1
            total += st.st_size
            newest = max(newest, st.st_mtime)
    return {"count": n, "size_kb": round(total / 1024, 1),
            "newest": newest, "dir": str(root)}


def translate_cache_summary() -> dict:
    """翻译缓存概览。翻译缓存按前两位哈希分桶，所以要递归统计。"""
    root = TRANSLATE_CACHE_DIR
    st = sub_translate_stats()
    if st.get("cache_dir"):
        root = Path(st["cache_dir"])   # 以服务端实际使用的目录为准
    return _dir_summary(root, "**/*.json")


def subtitle_cache_list() -> dict:
    items = []
    if SUBTITLE_CACHE_DIR.exists():
        for f in sorted(SUBTITLE_CACHE_DIR.glob("*.json"),
                        key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            v = d.get("video") or {}
            items.append({
                "key": f.stem,
                "lang": d.get("lang"),
                "count": d.get("count", 0),
                "created_at": d.get("created_at"),
                "size_kb": round(f.stat().st_size / 1024, 1),
                "video_name": Path(str(v.get("path", ""))).name,
                "video_path": v.get("path", ""),
            })
    return {"ok": True, "dir": str(SUBTITLE_CACHE_DIR), "items": items}


# ---------------------------------------------------------------- 版本
def app_version() -> dict:
    """读取 version.json（每次调用都读，便于开发时直接改文件生效）。"""
    try:
        data = json.loads((APP_DIR / "version.json").read_text(encoding="utf-8"))
        return {
            "name": str(data.get("versionName") or "0.0.0"),
            "code": int(data.get("versionCode") or 0),
            "channel": str(data.get("channel") or "dev"),
        }
    except Exception:
        return {"name": "0.0.0", "code": 0, "channel": "dev"}


# ================================================================ 设置
SETTINGS_FILE = Path(os.environ.get("APPDATA") or str(Path.home())) / "FunScriptCast-Nexus" / "integrated_settings.json"

# 设置/字幕配置是「读整个文件→改→写整个文件」，HTTP 服务又是多线程的（UI 连续
# 单字段 POST 很常见），不加锁时后写者会拿旧快照覆盖先写者的字段。
_SETTINGS_LOCK = threading.RLock()
# config.json / glossary_*.json 的写锁（同一原因；与设置文件分开，互不阻塞）
_SUBTITLE_FILE_LOCK = threading.RLock()
# 非空 = 设置文件存在但解析失败（由 load_settings 写、save_settings 读）：
# 此时内存里是默认值，绝不能拿它当基底整文件覆盖回去。
_SETTINGS_READ_ERROR = ""

DEFAULT_SETTINGS = {
    "dlna_port": DLNA_PORT_DEFAULT,
    "dlna_roots": [],
    "dlna_auto_start": True,
    "start_minimized": False,
    "close_to_tray": True,
    "theme": "dark",
    "motion": "full",
    "subtitle_url": f"http://127.0.0.1:{SUBTITLE_PORT}",
    "subtitle_auto_start": False,
    "script_folder": "",
    "video_folder": "",
    "device_folder": "/sdcard/Movies",
    "device_folder_script": "/sdcard/Funscript",
    "device_folder_video": "/sdcard/Movies",
    "adb_path": "",
    "sync_force_full": False,
    "sync_delete_extra": False,
}


def load_settings() -> dict:
    global _SETTINGS_READ_ERROR
    s = dict(DEFAULT_SETTINGS)
    _SETTINGS_READ_ERROR = ""
    try:
        if SETTINGS_FILE.exists():
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            for k in DEFAULT_SETTINGS:
                if k in data:
                    s[k] = data[k]
    except Exception as e:
        # "文件不存在"（给默认值是对的）和"文件存在却读不出来"（给默认值就是错的）
        # 必须分开：后者会让下一次 save_settings（它以本函数返回值作整文件写盘基底）
        # 把"默认值 + 本次改动"落盘，用户的媒体根/同步目录/设置被永久抹掉。
        # 打包版没有控制台，log.warning 谁也看不见——所以还要把状态传给 save_settings。
        _SETTINGS_READ_ERROR = f"{type(e).__name__}: {e}"
        log.warning("读取设置失败：%s", e)
    # 兼容历史数据：早期版本会把用户粘进来的引号一起存下来（`"D:\my folder"`），
    # 那个路径永远不存在，DLNA 只会安静地列出空目录——头显里就是"文件夹是空的"，
    # 界面上却显示"已启用"。读的时候顺手修掉，落盘由 _migrate_settings 负责。
    for k in PATH_KEYS:
        if isinstance(s.get(k), str):
            s[k] = norm_path(s[k])
    if isinstance(s.get("dlna_roots"), list):
        s["dlna_roots"] = [norm_path(x) for x in s["dlna_roots"] if norm_path(x)]
    return s


def _migrate_settings() -> None:
    """把规整后的设置写回磁盘（只在内容确实变了时才写）。

    不做的话，历史坏值每次读取都要靠内存里的临时修正，而别的地方（比如
    `/api/dlna/roots` 把 roots 拼给前端）拿到的仍是修正后的值——但文件里
    一直是错的，用户拿文件去核对会以为没问题。
    """
    try:
        if not SETTINGS_FILE.exists():
            return
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        fixed = load_settings()
        if json.dumps(raw, sort_keys=True, ensure_ascii=False) != \
           json.dumps(fixed, sort_keys=True, ensure_ascii=False):
            save_settings({})
            RT.add_log("已修正设置里带引号的路径（历史数据）", "warn")
    except Exception as e:
        log.warning("设置迁移失败：%s", e)


# 会被当成路径的键：写入前统一规整
PATH_KEYS = ("script_folder", "video_folder", "device_folder",
             "device_folder_script", "device_folder_video", "adb_path")
# 用户可能带上的引号：半角成对、以及中文输入法的全角引号
_QUOTES = ('"', "'", "“", "”", "‘", "’")


def norm_path(v) -> str:
    """规整用户输入的路径。

    **必须做**：用户习惯把带空格的路径连引号一起粘进来（`"D:\\my folder"`），
    这在任何 shell 里都合法，但不处理的话我们会把引号**当成文件名的一部分**存下来
    ——那个路径永远不存在，DLNA 于是列不出任何东西，头显里表现为"目录为空"，
    而界面上还显示"已启用"，完全给不出线索（实测踩过）。
    顺带处理中文输入法的全角引号，以及前后空白。
    """
    s = str(v or "").strip()
    changed = True
    while changed and len(s) >= 2:
        changed = False
        if s[0] in _QUOTES and s[-1] in _QUOTES:
            s = s[1:-1].strip()
            changed = True
    return s


def missing_roots(roots) -> list:
    """返回其中**不存在**的根目录（用于在添加时立刻提示，而不是等头显里看到空目录）。"""
    out = []
    for r in roots or []:
        p = norm_path(r)
        try:
            if not p or not Path(p).is_dir():
                out.append(str(r))
        except OSError:
            out.append(str(r))
    return out


def save_settings(patch: dict) -> dict:
    with _SETTINGS_LOCK:
        s = load_settings()
        if _SETTINGS_READ_ERROR:
            # 读失败时 s 就是默认值：写下去等于把用户配置换成默认值（只保留本次改动）。
            # 宁可拒绝这次保存并保留原文件，也不能静默抹掉用户配置。
            try:
                shutil.copy2(SETTINGS_FILE, str(SETTINGS_FILE) + ".bak")
                kept = f"原文件已备份为 {SETTINGS_FILE.name}.bak"
            except Exception:
                kept = "原文件未改动"
            msg = f"设置文件无法解析（{_SETTINGS_READ_ERROR}），已拒绝覆盖以免清空配置（{kept}）"
            log.warning("%s", msg)
            RT.add_log(msg, "err")
            return {"ok": False, "error": msg}
        for k, v in patch.items():
            if k not in DEFAULT_SETTINGS:
                continue
            # 路径类字段统一规整（去引号/去空白）。放在这一层是因为所有入口都会
            # 经过它：界面输入、托盘、REST 接口——只在前端做的话，别的调用方照样能
            # 把带引号的路径写进来。
            if k in PATH_KEYS and isinstance(v, str):
                v = norm_path(v)
            elif k == "dlna_roots" and isinstance(v, list):
                v = [norm_path(x) for x in v if norm_path(x)]
            s[k] = v
        try:
            SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = SETTINGS_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, SETTINGS_FILE)
        except Exception as e:
            log.warning("保存设置失败：%s", e)
        return s


# ================================================================ DLNA 服务
_vrdlna = None


def _vrdlna_mod():
    """懒加载 VR-DLNA 模块（复用其 DlnaApp / DlnaHTTPServer / SSDPServer）。"""
    global _vrdlna
    if _vrdlna is None:
        sys.path.insert(0, str(VRDLNA_DIR))
        import vr_dlna as m  # noqa: E402

        _vrdlna = m
    return _vrdlna


def lan_ip() -> str:
    try:
        m = _vrdlna_mod()
        ips = m.lan_ips()
        if ips:
            return ips[0]
    except Exception:
        pass
    # 兜底：连一次外部地址拿到本机出口 IP（不实际发包）
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def dlna_start(port: int | None = None, roots: list[str] | None = None) -> dict:
    """启动 DLNA（后台线程执行，立即返回）。"""
    with RT.lock:
        if RT.dlna_server is not None:
            return {"ok": True, "already": True}
        if RT.dlna_starting:
            return {"ok": True, "starting": True}
        RT.dlna_starting = True
        RT.dlna_error = ""
        RT.dlna_gen += 1
        gen = RT.dlna_gen
    s = load_settings()
    port = int(port or s.get("dlna_port") or DLNA_PORT_DEFAULT)
    roots = roots if roots is not None else s.get("dlna_roots") or []

    def worker() -> None:
        server = None
        ssdp = None
        try:
            m = _vrdlna_mod()
            from pathlib import Path as _P

            if not roots:
                raise RuntimeError("请先添加至少一个媒体根目录")
            media_roots = [m.MediaRoot(label=_P(p).name or "Videos", path=_P(p)) for p in roots]
            app = m.DlnaApp(media_roots, port)
            server = m.DlnaHTTPServer(("0.0.0.0", port), app)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                ssdp = m.SSDPServer(port, lan_ip())
                ssdp.start()
            except Exception:
                # SSDP 挂了必须把已监听的 HTTP 服务一起撤下：否则 dlna_running()
                # 显示停止、8899 却仍在服务，重试启动还会端口冲突。
                try:
                    server.shutdown()
                    server.server_close()
                except Exception:
                    pass
                server = None
                raise
            with RT.lock:
                # 无论成功还是被取消，都必须复位 starting：dlna_start() 用这个标志
                # 判断"已在启动中"，取消分支此前漏了复位 —— 于是"启动中点停止"之后
                # 本会话内 DLNA 再也起不来（点启动只回 starting，界面永久卡在"启动中"，
                # 只能重启宿主）。R41 加了代数校验撤下服务，但漏了这一个标志。
                RT.dlna_starting = False
                if gen != RT.dlna_gen:      # 期间用户点了停止 → 撤下，不留"僵尸服务"
                    cancelled = True
                else:
                    cancelled = False
                    RT.dlna_server, RT.dlna_ssdp, RT.dlna_port = server, ssdp, port
            if cancelled:
                try:
                    ssdp.stop()
                    server.shutdown()
                    server.server_close()
                except Exception:
                    pass
                RT.add_log("DLNA 启动完成前收到停止请求，已撤下", "info")
                return
            RT.add_log(f"DLNA 已启动 · {lan_ip()}:{port} · {len(roots)} 个媒体根", "ok")
            RT.add_dlna_log(f"服务已启动 http://{lan_ip()}:{port}")
        except Exception as e:
            with RT.lock:
                RT.dlna_starting = False
                RT.dlna_error = f"{type(e).__name__}: {e}"
            RT.add_log(f"DLNA 启动失败：{RT.dlna_error}", "err")
            RT.add_dlna_log(f"启动失败：{RT.dlna_error}")

    threading.Thread(target=worker, daemon=True, name="dlna-start").start()
    return {"ok": True, "starting": True}


def dlna_stop() -> dict:
    with RT.lock:
        ssdp, server = RT.dlna_ssdp, RT.dlna_server
        RT.dlna_ssdp, RT.dlna_server = None, None
        RT.dlna_gen += 1      # 正在启动中的 worker 看到代数变了会自行撤下
    if ssdp:
        try:
            ssdp.stop()
        except Exception:
            pass
    if server:
        def _shutdown(srv):
            try:
                srv.shutdown()
            finally:
                srv.server_close()
        threading.Thread(target=_shutdown, args=(server,), daemon=True, name="dlna-stop").start()
        RT.add_log("DLNA 已停止", "warn")
        RT.add_dlna_log("服务已停止")
    return {"ok": True}


def dlna_running() -> bool:
    with RT.lock:
        return RT.dlna_server is not None


# ================================================================ 字幕服务子进程
def sub_port_open(timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", SUBTITLE_PORT), timeout=timeout):
            return True
    except Exception:
        return False


def sub_health() -> dict | None:
    """直接问服务端 /health（1s 超时）。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{SUBTITLE_PORT}/health", timeout=1.0) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


_tr_stats_cache = {"ts": 0.0, "data": {}}


def sub_translate_stats() -> dict:
    """翻译层累计统计（批量/缓存命中/纠错/兜底）。2s 缓存，避免轮询压力。"""
    now = time.time()
    if now - _tr_stats_cache["ts"] < 2.0:
        return _tr_stats_cache["data"]
    data = {}
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{SUBTITLE_PORT}/translate/stats", timeout=1.0) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        data = {}
    _tr_stats_cache.update(ts=now, data=data)
    return data


def sub_translate_selftest(text: str = "") -> dict:
    """让字幕服务用当前配置真翻一句（UI「测试」按钮）。不缓存：每次都要真结果。

    超时给足（云端首字可能十几秒），但仍是有上限的探测，不会挂死。
    """
    q = urllib.parse.urlencode({"text": text}) if text else ""
    url = f"http://127.0.0.1:{SUBTITLE_PORT}/translate/selftest" + (f"?{q}" if q else "")
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return {"ok": False, "error": f"字幕服务 HTTP {e.code}：{body or e.reason}"}
    except Exception as e:
        return {"ok": False, "error": f"字幕服务不可达（{type(e).__name__}: {e}）——先点「启动字幕服务」"}


def _mask_translate_secrets(cfg: dict) -> dict:
    """把 translate.*.api_key 打码后再回给前端（key 仍以明文存在本地 config.json）。

    只标记"是否已设置"+末 4 位，前端据此显示占位符；保存时若前端回传的是打码值则忽略，
    避免把掩码写回配置（见 /api/subtitle/config 的写回逻辑）。
    """
    out = json.loads(json.dumps(cfg or {}, ensure_ascii=False))
    tr = out.get("translate") or {}
    for sect in ("openai", "ollama", "local"):
        s = tr.get(sect)
        if isinstance(s, dict) and s.get("api_key"):
            k = str(s["api_key"])
            s["api_key"] = ""
            s["api_key_set"] = True
            s["api_key_tail"] = k[-4:] if len(k) >= 4 else "****"
    return out


def _strip_masked_keys(patch: dict) -> dict:
    """写回配置前，把"打码过的"api_key 字段删掉（前端不清空输入框就不该覆盖真 key）。

    判据：值里带 "*" 或等于回给前端的空串 + api_key_set 标记。
    """
    tr = (patch or {}).get("translate")
    if not isinstance(tr, dict):
        return patch
    for sect in ("openai", "ollama", "local"):
        s = tr.get(sect)
        if isinstance(s, dict) and "api_key" in s:
            v = str(s.get("api_key") or "")
            if not v.strip() or "*" in v:
                s.pop("api_key", None)
    return patch


def _service_ours(health: dict | None) -> bool:
    """8756 上应答的服务，是不是本程序拉起来的那个。

    **不能用 PID 比**：Windows 上 venv 里的 python.exe 只是个启动器，它会再起一个
    基础解释器来跑脚本，真正跑 uvicorn、应答 /health 的是那个**子进程**。
    拿我们 spawn 到的 PID 去比永远对不上，会把自家服务误判成外来的
    （本机就踩了这个坑：干净启动也报 foreignPid）。

    判据是"服务自报的启动时刻 vs 我们 spawn 的时刻"——孤儿必然早于我们启动：
      · sub_spawn_ts == 0 → 本次宿主从没启动过服务，那 8756 上的一定是别人的；
      · started_at 缺失（老版本服务）→ 无从判断，当作自己人，宁可少报警。
    """
    if not health or not isinstance(health, dict):
        # 非 dict 的 /health 返回体（别的程序占着 8756、或返回一个 JSON 数组/标量）
        # 走 .get 会直接抛 AttributeError。这里判"不是自己人"而不是抛出去：
        # 调用方 sub_start() 在异常下会把 sub_starting 永久卡住（见那里的注释）。
        return False
    with RT.lock:
        spawn = RT.sub_spawn_ts
    started = health.get("started_at")
    if not spawn:
        return False                # 我们没起过 → 不可能是自己人
    if not started:
        return True                 # 判不了，别误报
    try:
        return float(started) >= float(spawn) - 1.0
    except (TypeError, ValueError):
        # started_at 不是数字（例如对方回 ISO 字符串）→ 判不了。
        # 与 762-763 同一取舍：判不出来时当作自己人，宁可少报警也不要误杀。
        return True


def foreign_service() -> dict | None:
    """8756 上的服务是不是**别人**在跑（不是本程序拉起来的那个）。

    为什么必须查：只看"端口开着"会被上次异常退出残留的进程骗过去——
    那时 `sub_state()` 会报 ready，头显就会跳过等待、把音频发给一个陈旧进程，
    而那个进程用的是它启动时的旧 config，改过的设置全都不生效。
    本机反复 Stop-Process 杀宿主留下的就是这种残留（子进程会活下来）。
    """
    if not sub_port_open():
        return None
    health = sub_health()
    if not health or _service_ours(health):
        return None
    return {"pid": health.get("pid"), "started_at": health.get("started_at")}


def sub_start() -> dict:
    with RT.lock:
        if RT.sub_proc and RT.sub_proc.poll() is None:
            return {"ok": True, "already": True}
        if RT.sub_starting:
            return {"ok": True, "starting": True}
        RT.sub_starting = True
        RT.sub_error = ""
        gen = RT.sub_gen

    # 端口已被别的进程占着：再拉一个也是徒劳（uvicorn 绑不上端口会立刻退出），
    # 直接复用它并如实标注，别制造一个"刚起来就死"的子进程。
    # ⚠️ 这一步会去读 8756 的 /health，必须在 sub_starting=True 之后**兜住异常**：
    # 此前它裸奔，一抛就冒到 HTTP 层返回 500，而 sub_starting 永不复位 ——
    # 之后每次启动都在 787-788 被短路成"启动中"，字幕服务直到重启宿主都起不来。
    try:
        f = foreign_service()
    except Exception as e:
        with RT.lock:
            RT.sub_starting = False
        RT.add_log(f"探测 8756 上已有服务失败：{type(e).__name__}: {e}", "err")
        return {"ok": False, "error": f"探测已有服务失败：{type(e).__name__}: {e}"}
    if f:
        with RT.lock:
            RT.sub_starting = False
        RT.add_log(f"8756 上已有其他字幕服务在跑（PID {f['pid']}），直接复用；"
                   f"它不是本程序启动的，改过设置可能需要先结束它", "warn")
        return {"ok": True, "foreign": True, "reused": True, "pid": f["pid"]}

    def worker() -> None:
        try:
            py = SUBTITLE_VENV_PY
            if FROZEN and py.resolve() == Path(sys.executable).resolve():
                raise RuntimeError(
                    "找不到 Python 解释器：字幕服务需要 torch，不随 EXE 打包。"
                    f"请把 .venv 目录复制到 {APP_DIR}，或设置环境变量 NEXUS_PY 指向 python.exe"
                )
            if not SUBTITLE_DIR.exists():
                raise RuntimeError(f"字幕服务目录不存在：{SUBTITLE_DIR}")
            # 先确认这个解释器真的跑得起来字幕服务。
            # dist-app 里没有 .venv 时 _subtitle_python() 会退回系统 Python，
            # 而它多半没装 uvicorn——不先探一下的话，用户看到的是一句
            # "No module named 'uvicorn'"，完全指不出该做什么（本机实测过）。
            probe = subprocess.run([str(py), "-c", "import uvicorn, fastapi"],
                                   capture_output=True, text=True, timeout=40,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if probe.returncode != 0:
                tail = ""
                for ln in reversed((probe.stderr or "").strip().splitlines()):
                    if ln.strip():
                        tail = ln.strip()
                        break
                raise RuntimeError(
                    f"解释器 {py} 缺依赖（uvicorn/fastapi）{('：' + tail) if tail else ''}。"
                    f"把本仓库的 .venv 复制到 {APP_DIR}，"
                    f"或设环境变量 NEXUS_PY 指向装好依赖的 python.exe"
                )
            flags = 0
            if os.name == "nt":
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            env = dict(os.environ)
            # 模型由 config.json 决定（服务端会把相对路径按自身目录解析），
            # 所以这里只在 config 完全没写模型时才兜底注入绝对路径。
            try:
                _cfg = json.loads((SUBTITLE_DIR / "config.json").read_text(encoding="utf-8"))
                _m = str((_cfg.get("asr") or {}).get("model") or "").strip()
            except Exception:
                _m = ""
            if not _m and MODELS_DIR.exists():
                env.setdefault("ASR_MODEL", str(MODELS_DIR / "Qwen3-ASR-0.6B"))
            # 记下 spawn 时刻：判断 8756 上应答的是不是自己人就靠它
            # （服务自报的 started_at 若早于这个时刻，说明是上次残留的）。
            with RT.lock:
                RT.sub_spawn_ts = time.time()
                if gen != RT.sub_gen:      # 探测依赖期间用户点了停止
                    cancelled = True
                else:
                    cancelled = False
            if cancelled:
                with RT.lock:
                    RT.sub_starting = False
                RT.add_log("字幕服务启动完成前收到停止请求，已取消", "info")
                return
            # 用管道接住子进程输出：起来就挂（缺依赖等）时能给出可读原因
            proc = subprocess.Popen(
                [str(py), "run_server.py", "--port", str(SUBTITLE_PORT)],
                cwd=str(SUBTITLE_DIR),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=flags,
            )
            with RT.lock:
                if gen != RT.sub_gen:      # Popen 期间收到停止 → 立刻带走刚拉起的进程
                    RT.sub_starting = False
                    stale = True
                else:
                    RT.sub_proc = proc
                    RT.sub_starting = False
                    stale = False
            if stale:
                _kill_tree(proc.pid)
                RT.add_log("字幕服务启动完成前收到停止请求，已撤下刚拉起的进程", "info")
                return
            RT.add_log(f"字幕服务已启动", "ok")
            _watch_subtitle(proc)
        except Exception as e:
            with RT.lock:
                RT.sub_starting = False
                RT.sub_error = f"{type(e).__name__}: {e}"
            RT.add_log(f"字幕服务启动失败：{RT.sub_error}", "err")

    threading.Thread(target=worker, daemon=True, name="sub-start").start()
    return {"ok": True, "starting": True}


def _watch_subtitle(proc: "subprocess.Popen") -> None:
    """读子进程输出；若 3 秒内就退出，把最后几行输出作为错误上报。

    字幕服务依赖 torch/uvicorn 等，环境不全时往往秒退，静默 DEVNULL 会让
    界面一直显示「加载中」，所以这里保留最近输出用于诊断。
    """
    tail: list[str] = []

    def reader() -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    tail.append(line)
                    if len(tail) > 40:
                        del tail[: len(tail) - 40]
        except Exception:
            pass

    t = threading.Thread(target=reader, daemon=True, name="sub-out")
    t.start()
    # 3 秒后检查是否已经退出
    for _ in range(12):
        time.sleep(0.25)
        if proc.poll() is not None:
            break
    if proc.poll() is not None:
        detail = " / ".join(tail[-3:]) or "无输出"
        with RT.lock:
            RT.sub_error = f"子进程退出（code {proc.returncode}）：{detail}"
            RT.sub_proc = None
        RT.add_log(f"字幕服务启动失败：{RT.sub_error}", "err")
        return
    # 撑过启动窗口：继续盯到进程退出为止。正常停止（sub_stop）会使代数 +1 或
    # 清掉 sub_proc，此时静默收场；否则就是**运行中崩溃**——此前这一段完全无
    # 监控：无日志、无事件，状态回落成"已停止"，用户无法区分"没启动过"和
    # "跑到一半崩了"（崩溃输出也拿不到）。
    with RT.lock:
        gen = RT.sub_gen
    rc = proc.wait()
    with RT.lock:
        ours = RT.sub_proc is proc
        if ours:
            RT.sub_proc = None
            RT.sub_ready = False
    if ours and gen == RT.sub_gen and not TRAY.quitting:
        detail = " / ".join(tail[-3:]) or "无输出"
        with RT.lock:
            RT.sub_error = f"字幕服务运行中退出（code {rc}）：{detail}"
        RT.add_log(f"字幕服务异常退出：{RT.sub_error}", "err")


def _port_owner_pids(port: int) -> list:
    """占用某端口的 PID 列表（解析 netstat，不引第三方依赖）。"""
    if os.name != "nt":
        return []
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                             capture_output=True, text=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except Exception:
        return []
    pids: list = []
    for line in out.splitlines():
        p = line.split()
        if len(p) >= 5 and p[1].endswith(f":{port}") and p[3] == "LISTENING":
            try:
                pid = int(p[4])
            except ValueError:
                continue
            if pid not in pids:
                pids.append(pid)
    return pids


def _proc_name(pid: int) -> str:
    """进程映像名（小写）。用来确认要杀的是不是 audiocpp 后端，别误伤。"""
    if os.name != "nt":
        return ""
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                             capture_output=True, text=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        line = out.strip().splitlines()[0] if out.strip() else ""
        return line.split(",")[0].strip().strip('"').lower()
    except Exception:
        return ""


def _kill_tree(pid: int) -> None:
    """结束进程**及整棵子树**。

    必须带 /T：字幕服务的 audiocpp 后端是孙子进程，而 Windows 上
    `terminate()` 走的是 TerminateProcess——它不给 Python 执行 lifespan 收尾的
    机会，于是 `stop_server()` 永远不会被调用。实测停止字幕服务后
    audiocpp_server 仍活着，带着 3 GB 内存并继续占着 8083 端口。
    """
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return
        except Exception as e:
            log.warning("taskkill 失败（PID %s）：%s", pid, e)
    try:
        os.kill(pid, signal.SIGTERM)
    except Exception:
        pass


def _audiocpp_port() -> int:
    """audiocpp 后端端口，从字幕服务配置里读（读不到用 8083）。"""
    try:
        cfg = json.loads((SUBTITLE_DIR / "config.json").read_text(encoding="utf-8"))
        return int((cfg.get("asr") or {}).get("audiocpp", {}).get("port") or 8083)
    except Exception:
        return 8083


def reap_orphan_audiocpp() -> int:
    """收拾"在跑但没有字幕服务在用"的 audiocpp 后端，返回清掉的个数。

    宿主被强杀时它会变成孤儿（见 _kill_tree 的说明），带着约 3 GB 内存常驻。
    """
    if sub_port_open():
        return 0                     # 字幕服务在，audiocpp 是它在用的，别动
    n = 0
    for pid in _port_owner_pids(_audiocpp_port()):
        if _proc_name(pid) == "audiocpp_server.exe":
            _kill_tree(pid)
            n += 1
    if n:
        RT.add_log(f"已清理残留的 audiocpp 后端（{n} 个进程，约 3 GB 内存）", "warn")
    return n


def sub_stop() -> dict:
    with RT.lock:
        proc = RT.sub_proc
        RT.sub_proc = None
        RT.sub_ready = False
        RT.sub_gen += 1          # 启动中的 worker 看到代数变了会自行撤下
        starting = RT.sub_starting
    if proc is None and starting:
        RT.add_log("字幕服务正在启动，已请求取消本次启动", "info")
    stopped = False
    if proc and proc.poll() is None:
        _kill_tree(proc.pid)          # /T：连它拉起的 audiocpp 子进程一起带走
        stopped = True
        RT.add_log("字幕服务已停止 · 模型内存已释放", "warn")
    # 兜底：子树没被带走时（父子关系断裂、进程被重新挂到别处），按端口收拾。
    # audiocpp 带着约 3 GB 内存，不还回来就等于"常驻"，只是晚一点发生。
    reap_orphan_audiocpp()
    return {"ok": True, "stopped": stopped}


def sub_state() -> dict:
    with RT.lock:
        proc = RT.sub_proc
        starting = RT.sub_starting
        err = RT.sub_error
    alive = bool(proc and proc.poll() is None)
    # 端口探测缓存 2s，避免每次轮询都建连接
    now = time.time()
    if now - RT.sub_last_probe > 2.0:
        RT.sub_last_probe = now
        RT.sub_ready = sub_port_open()
    health = sub_health() if RT.sub_ready else None
    # 端口开着 ≠ 我们的服务在跑。区分三种情况，否则会给出一个会骗人的 ready：
    #   · 服务是别人在跑，但能正常应答 /health → 服务确实可用，算 ready，
    #     只把"不是本程序管的"标出来（改过设置可能需要先结束它）；
    #   · 端口开着却连 /health 都不应答 → 是个半死进程，必须报错，
    #     否则 sub_start 会再拉一个绑不上端口的子进程、立刻退出，白折腾一轮。
    foreign_pid = None
    if health and not _service_ours(health):
        foreign_pid = health.get("pid")
    if health and foreign_pid:
        status = "ready"
        if not alive:
            err = err or f"字幕服务被残留的旧进程占用（PID {foreign_pid}），可点「结束并重启」恢复"
    elif alive and RT.sub_ready:
        status = "ready"
    elif not alive and RT.sub_ready and not starting:
        status = "error"
        err = err or "字幕服务端口被占用且无响应，请尝试「结束并重启」"
    elif starting or (alive and not RT.sub_ready):
        status = "loading"
    elif err:
        status = "error"
    else:
        status = "stopped"
    return {
        "status": status,
        "alive": alive,
        "pid": proc.pid if alive else None,
        "port": SUBTITLE_PORT,
        "error": err,
        # 非空表示 8756 上的服务不是本程序管的（残留进程），PC 端界面据此提示用户
        "foreignPid": foreign_pid,
        "health": health,
    }


def sub_reclaim() -> dict:
    """结束 8756 上不属于本程序的服务，然后重新拉起自己那份。

    为什么需要：宿主被强杀（任务管理器 / 崩溃）时字幕服务子进程会活下来，
    下次启动端口就被它占着——而它加载的是**当时**的 config，之后改过的设置
    （换模型、换翻译后端、改术语表）一律不生效，界面上却显示"就绪"。
    唯一干净的做法是把它结束掉，再起一份按当前配置加载的。
    """
    f = foreign_service()
    if not f:
        return {"ok": True, "nothing": True}
    pid = int(f["pid"])
    try:
        # 必须走 _kill_tree（带 /T）：残留服务下面还挂着 audiocpp 孙进程，
        # 裸 taskkill /F 杀完 python 后 audiocpp 带着 ~3 GB 内存继续常驻，
        # 而 reap_orphan_audiocpp 只在宿主启动时跑一次，之后无人清理。
        _kill_tree(pid)
    except Exception as e:
        return {"ok": False, "error": f"结束 PID {pid} 失败：{type(e).__name__}: {e}"}
    RT.add_log(f"已结束残留的字幕服务（PID {pid}）", "warn")
    time.sleep(1.2)
    with RT.lock:
        RT.sub_ready = False
        RT.sub_last_probe = 0.0
    return {"ok": True, "killed": pid, "restart": sub_start()}


def _host_code_sig() -> str:
    """宿主自身身份签名（与字幕服务 /health 里的 code_sig 同一用途）。

    为什么需要这个：头显其实同时依赖**两个面**——8756 的识别/翻译管线，以及 8791
    的宿主面（字幕缓存读写、请求拉起服务）。而字幕服务的 code_sig 只覆盖
    `vendor/subtitle/*.py`，宿主侧改了（缓存格式、白名单路由、缓存键规则…）
    它完全看不出来。头显仓库的审查明确提出"无法回溯哪个 APK 配哪个服务端版本"，
    这里把两半都做成头显能读到、能记录的标识。

    ⚠️ 打包版（PyInstaller onefile）里 `__file__` 指向包内路径、磁盘上并不存在，
    读源码必然失败——实测第一次重编后本字段恒为空（"字段在但永远空"比没有更糟，
    看起来像能用）。所以读不到源码时退化为对 **exe 本身** 取哈希：宿主是编译进
    exe 的，exe 哈希才是它真正的构建标识。
    """
    for cand in (Path(__file__), Path(sys.executable)):
        try:
            data = cand.read_bytes()
            if data:
                return hashlib.sha256(data).hexdigest()[:12]
        except Exception:
            continue
    return ""


def _configured_translate_backend() -> str:
    """config.json 里**配置**的翻译后端（服务没跑、/health 拿不到时用它）。

    头显正是在"等 PC 就绪"阶段轮询 headset_status，此时服务通常还没起来、
    `/health` 是空的。若此时一律按"本地"给建议，配置成云端的用户会拿到
    3 秒档的建议——那正是要防的那个结构性追不上的坑。
    """
    try:
        cfg = json.loads((SUBTITLE_DIR / "config.json").read_text(encoding="utf-8"))
        return str((cfg.get("translate") or {}).get("backend") or "")
    except Exception:
        return ""


_HOST_CODE_SIG = _host_code_sig()


def headset_status() -> dict:
    """头显轮询用：模型起来没有。

    刻意只回最小字段——这个接口是暴露在局域网上的，`/api/state` 里有本机路径、
    日志、设备序列号之类的东西，不适合给头显（也就等于给整个局域网）。
    这里新增的都是**标识类**字段（哈希/后端名/档位建议），不含路径与密钥。
    """
    st = sub_state()
    h = st.get("health") or {}
    status = st.get("status")
    backend = str((h or {}).get("translate_backend") or "")
    if not backend:
        # 服务没在跑时 /health 拿不到后端 —— 头显正是在"等就绪"阶段轮询这里，
        # 此时必须回落到**配置**里的后端，否则配云端的用户会拿到 3 秒档的建议
        # （正是要防的那个"结构性追不上"的坑）。
        backend = _configured_translate_backend()
    # 档位建议：云端单块 6.6–15s，而头显 3s 档的过期阈值只有 6s（STREAM_MAX_LAG_MS）
    # ⇒ 3 秒块在云端**结构性**追不上，每块出队即被判过期丢弃（R40 实测）。本地
    # 7B 单块 1.0–1.9s，3 秒档没问题。把建议由 PC 明确给出，头显据此切「分块 25s」，
    # 不必靠人去记"切云端要手动改档位"这条隐规则。
    cloud = backend in ("openai", "cloud")
    return {
        "ok": True,
        # 模型没下载时服务虽在但识别不可用：不能给头显报 ready（否则它白推音频）
        "ready": status == "ready" and h.get("asr_ready") is not False,
        "status": status,
        "error": st.get("error"),
        "asr": h.get("asr_model"),
        "translate": h.get("translate"),
        "version": app_version()["name"],
        # ↓ 版本可追溯 + 档位联动（头显侧据此记录"哪个 APK 配哪个服务端版本"）
        "translate_backend": backend or None,
        "recommended_chunk_sec": 25 if cloud else 3,
        "code_sig": (h or {}).get("code_sig"),
        "host_sig": _HOST_CODE_SIG,
    }


# ================================================================ 模型下载
# 应用自带：adb（tools/adb）与 llama-server 运行时（vendor/llama，构建机 fetch_llama）。
# 模型不随包（单个 1.2~4GB）：全新安装后在界面一键下载（走 hf-mirror 镜像），
# 或手动放置——翻译模型放进 models/<目录>/ 后刷新界面即出现在下拉里。
#
# whisper 的落盘位置是 HF 缓存标准结构（refs/main -> snapshots/<commit>/），
# faster-whisper 的 local_files_only 能直接认领，与"用户用别的方式下载"等价。
_HF_MIRROR = "https://hf-mirror.com"
def _hf_hub_dir() -> Path:
    """HF 缓存根：尊重 HF_HUB_CACHE / HF_HOME，再退 huggingface_hub 常量，
    最后退默认值——与服务端 faster-whisper 的解析保持同源，避免下载/读取错位。"""
    env = os.environ.get("HF_HUB_CACHE") or (
        os.environ.get("HF_HOME") and os.path.join(os.environ["HF_HOME"], "hub"))
    if env:
        return Path(env)
    try:
        from huggingface_hub.constants import HF_HUB_CACHE
        return Path(HF_HUB_CACHE)
    except Exception:
        return Path.home() / ".cache" / "huggingface" / "hub"

MODELS_CATALOG = [
    {
        "id": "whisper",
        "role": "asr",
        "label": "识别模型（Whisper · 日文特化）",
        "repo_dirname": "models--kotoba-tech--kotoba-whisper-v2.0-faster",
        "commit": "local-download",
        "size_gb": 1.4,
        "files": [
            {"rel": fn,
             "url": _HF_MIRROR + "/kotoba-tech/kotoba-whisper-v2.0-faster/resolve/main/" + fn}
            for fn in ("config.json", "preprocessor_config.json",
                       "tokenizer.json", "vocabulary.json", "model.bin")
        ],
    },
    {
        "id": "llama-runtime",
        "role": "translate-runtime",
        "label": "本地翻译运行时（llama.cpp · CUDA）",
        "kind": "zip",
        "zip_url": "https://github.com/wanfneg/FunScriptCast-Nexus/releases/latest/download/llama-runtime-windows.zip",
        "dest_dir": APP_DIR / "vendor" / "llama",
        "size_gb": 1.1,
        "files": [],
    },
    {
        "id": "sakura-7b",
        "role": "translate",
        "label": "翻译模型 · Sakura-7B（推荐）",
        "dest_dir": MODELS_DIR / "Sakura-7B-Qwen2.5-v1.0",
        "size_gb": 4.0,
        "files": [
            {"rel": "sakura-7b-qwen2.5-v1.0-iq4xs.gguf",
             "url": _HF_MIRROR + "/SakuraLLM/Sakura-7B-Qwen2.5-v1.0-GGUF/resolve/main/sakura-7b-qwen2.5-v1.0-iq4xs.gguf"},
        ],
    },
    {
        "id": "sakura-1.5b",
        "role": "translate",
        "label": "翻译模型 · Sakura-1.5B（轻量）",
        "dest_dir": MODELS_DIR / "Sakura-1.5B-Qwen2.5-v1.0",
        "size_gb": 1.2,
        "files": [
            {"rel": "sakura-1.5b-qwen2.5-v1.0-q5ks.gguf",
             "url": _HF_MIRROR + "/SakuraLLM/Sakura-1.5B-Qwen2.5-v1.0-GGUF/resolve/main/sakura-1.5b-qwen2.5-v1.0-q5ks.gguf"},
        ],
    },
]

_MODEL_DL: dict = {}
_DL_LOCK = threading.Lock()
# 直连 hf-mirror（绕过系统代理：代理软件没开时 urllib 读注册表代理会 TLS 失败，
# 与 fetch_llama / fetch_adb 的 NO_PROXY 教训同源）
_DL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _download_to_file(url: str, dest: Path, prog=None) -> None:
    """流式下载到 .part（断点续传 + 原子替换）。prog(done, total)。

    两种必须防住的坏例（审查 P0-2 实测复现过）：
      · 发了 Range 服务器却回 200 全量 → 若继续追加会把文件写成"两份拼接"的
        静默损坏。判据：带 Range 请求时响应码必须是 206，否则推倒重下。
      · 416（.part 比服务器内容还长）→ 删 .part 全新重下一次。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    for restart in (0, 1):
        done = part.stat().st_size if part.exists() else 0
        headers = {"User-Agent": "FunScriptCast-Nexus"}
        if done:
            headers["Range"] = "bytes=%d-" % done
        req = urllib.request.Request(url, headers=headers)
        try:
            r = _DL_OPENER.open(req, timeout=60)
        except urllib.error.HTTPError as e:
            if e.code == 416 and part.exists() and restart == 0:
                part.unlink()                # 坏 .part：推倒重来
                continue
            raise
        with r:
            ranged = bool(done) and r.status == 206
            if done and not ranged:
                done = 0                     # 服务器不支持续传：全量重下
            total = int(r.headers.get("Content-Length") or 0) + done
            if total and done >= total:
                os.replace(part, dest)       # 上次恰好在结尾中断，已完整
                if prog:
                    prog(done, total)
                return
            with open(part, "ab" if done else "wb") as f:
                while True:
                    chunk = r.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if prog:
                        prog(done, total)
        if done == 0:
            raise RuntimeError("服务器没有返回数据：" + url)
        os.replace(part, dest)
        return
    raise RuntimeError("下载重试仍失败：" + url)


def _model_installed(e: dict) -> bool:
    if e.get("kind") == "zip":               # 运行时压缩包：看关键可执行文件
        return (e["dest_dir"] / "llama-server.exe").is_file()
    if e.get("repo_dirname"):                # whisper：HF 缓存结构
        repo = _hf_hub_dir() / e["repo_dirname"]
        ref = repo / "refs" / "main"
        try:
            commit = ref.read_text(encoding="utf-8").strip()
        except Exception:
            commit = e["commit"]
        snap = repo / "snapshots" / commit
        return all((snap / f["rel"]).is_file() for f in e["files"])
    return all((e["dest_dir"] / f["rel"]).is_file() for f in e["files"])


def _model_dl_worker(e: dict) -> None:
    import tempfile
    id_ = e["id"]
    try:
        if e.get("kind") == "zip":
            import zipfile
            tmp = Path(tempfile.gettempdir()) / ("nexus-" + id_ + "-"
                                                 + str(int(time.time())) + ".zip")
            _download_to_file(e["zip_url"], tmp,
                              prog=lambda done, total: (
                                  None if not total else
                                  (_MODEL_DL[id_].__setitem__(
                                      "pct", round(done / total * 99)))))
            with _DL_LOCK:
                _MODEL_DL[id_]["pct"] = 100
            e["dest_dir"].mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(tmp) as z:
                z.extractall(e["dest_dir"])
            tmp.unlink(missing_ok=True)
            if not (e["dest_dir"] / "llama-server.exe").is_file():
                raise RuntimeError("解压后缺少 llama-server.exe（安装包内容不符）")
            with _DL_LOCK:
                _MODEL_DL[id_].update(state="done", pct=100)
            RT.add_log("模型下载完成：" + e["label"], "ok")
            return
        if e.get("repo_dirname"):
            # 修复历史损坏：此前版本给 refs/main 写过带换行的值，faster-whisper
            # 读 refs 不 strip → 解析出带换行的 snapshot 目录名 → 永远找不到模型
            ref = _hf_hub_dir() / e["repo_dirname"] / "refs" / "main"
            if ref.exists():
                txt = ref.read_text(encoding="utf-8").strip()
                if txt and txt != ref.read_text(encoding="utf-8"):
                    ref.write_text(txt, encoding="utf-8")
            base = _hf_hub_dir() / e["repo_dirname"] / "snapshots" / e["commit"]
        else:
            base = Path(e["dest_dir"])
        n = max(1, len(e["files"]))
        for i, f in enumerate(e["files"]):
            dest = base / f["rel"]
            if dest.is_file() and dest.stat().st_size > 0:
                continue                     # 重试/断点：已完成的文件跳过

            def prog(done, total, _i=i):
                frac = (_i + (done / total if total else 0.0)) / n
                with _DL_LOCK:
                    st = _MODEL_DL[id_]
                    st["pct"] = round(frac * 100)
                    st["bytes"] = done
                    st["total"] = total

            _download_to_file(f["url"], dest, prog)
        if e.get("repo_dirname"):
            ref = _hf_hub_dir() / e["repo_dirname"] / "refs" / "main"
            ref.parent.mkdir(parents=True, exist_ok=True)
            ref.write_text(e["commit"], encoding="utf-8")
        with _DL_LOCK:
            _MODEL_DL[id_].update(state="done", pct=100)
        RT.add_log("模型下载完成：" + e["label"], "ok")
    except Exception as exc:
        with _DL_LOCK:
            _MODEL_DL[id_].update(state="error", error=type(exc).__name__ + ": " + str(exc))
        RT.add_log("模型下载失败：" + e["label"] + "（" + str(exc) + "）", "err")


def models_catalog_payload() -> dict:
    items = []
    for e in MODELS_CATALOG:
        installed = _model_installed(e)
        with _DL_LOCK:
            st = dict(_MODEL_DL.get(e["id"]) or {})
        if st.get("state") == "downloading":
            state = "downloading"
        elif st.get("state") == "done":
            state = "done"
        elif installed:
            state = "installed"
        elif st.get("state") == "error":
            state = "error"
        else:
            state = "absent"
        items.append({
            "id": e["id"], "role": e["role"], "label": e["label"],
            "size_gb": e["size_gb"], "installed": installed,
            "state": state, "pct": st.get("pct", 0), "error": st.get("error", ""),
        })
    return {"ok": True, "items": items}


def model_download_start(body: dict) -> dict:
    id_ = str(body.get("id") or "")
    e = next((x for x in MODELS_CATALOG if x["id"] == id_), None)
    if e is None:
        return {"ok": False, "error": "未知的模型"}
    with _DL_LOCK:
        st = _MODEL_DL.get(id_)
        if st and st.get("state") == "downloading":
            return {"ok": True, "state": "downloading"}
        _MODEL_DL[id_] = {"state": "downloading", "pct": 0, "error": ""}
    threading.Thread(target=_model_dl_worker, args=(e,), daemon=True,
                     name="model-dl-" + id_).start()
    RT.add_log("开始下载模型：" + e["label"], "info")
    return {"ok": True, "state": "downloading"}


# ================================================================ 指标
_gpu_cache = {"ts": 0.0, "data": {}}


def gpu_info() -> dict:
    now = time.time()
    if now - _gpu_cache["ts"] < 3.0:
        return _gpu_cache["data"]
    data = {"name": "", "used_mb": 0, "total_mb": 0, "util": 0}
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4, creationflags=flags,
        ).stdout.strip()
        if out:
            parts = [p.strip() for p in out.splitlines()[0].split(",")]
            data = {"name": parts[0], "used_mb": int(parts[1]), "total_mb": int(parts[2]), "util": int(parts[3])}
    except Exception:
        pass
    _gpu_cache.update(ts=now, data=data)
    return data


# CPU / 内存指标（纯 ctypes，零新依赖；给仪表盘的圆环用）
_sys_cache = {"ts": 0.0, "data": {}, "cpu_raw": None}


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]


def sys_info() -> dict:
    """CPU 占比（GetSystemTimes 差分）+ 内存占用（GlobalMemoryStatusEx）。

    CPU 占比必须两次采样做差，单次调用只能拿到累计值——所以首次调用返回 0，
    下个轮询周期起有效（轮询 1s 一次，感知不到这个空窗）。缓存 2s。
    """
    now = time.time()
    if now - _sys_cache["ts"] < 2.0:
        return _sys_cache["data"]
    data = {"cpu_pct": 0, "ram_used_mb": 0, "ram_total_mb": 0}
    try:
        k32 = ctypes.windll.kernel32
        st = _MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if k32.GlobalMemoryStatusEx(ctypes.byref(st)):
            data["ram_total_mb"] = int(st.ullTotalPhys // (1024 * 1024))
            data["ram_used_mb"] = int((st.ullTotalPhys - st.ullAvailPhys) // (1024 * 1024))
        idle, kernel, user = _FILETIME(), _FILETIME(), _FILETIME()
        if k32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            def _u64(ft):
                return (ft.dwHighDateTime << 32) | ft.dwLowDateTime
            cur = (_u64(idle), _u64(kernel), _u64(user))
            prev = _sys_cache["cpu_raw"]
            _sys_cache["cpu_raw"] = cur
            if prev:
                d_idle = cur[0] - prev[0]
                d_total = (cur[1] - prev[1]) + (cur[2] - prev[2])
                if d_total > 0:
                    data["cpu_pct"] = int(max(0.0, min(100.0, (1 - d_idle / d_total) * 100)))
    except Exception:
        pass
    _sys_cache.update(ts=now, data=data)
    return data


# ================================================================ API
def state_payload() -> dict:
    s = load_settings()
    with RT.lock:
        dlna_starting = RT.dlna_starting
        dlna_error = RT.dlna_error
        uptime = time.time() - RT.started_at
        logs = RT.logs[-40:]
        dlna_logs = RT.dlna_logs[-200:]
    dlna_on = dlna_running()
    return {
        "ok": True,
        "version": app_version()["name"],
        "versionInfo": app_version(),
        "uptime": round(uptime, 1),
        "host": {"port": UI_API_PORT, "lan_ip": lan_ip(), "started_at": RT.started_at,
                 "lan_api_port": LAN_API_PORT,
                 "lan_api_url": f"http://{lan_ip()}:{LAN_API_PORT}"},
        "dlna": {
            "running": dlna_on,
            "starting": dlna_starting,
            "error": dlna_error,
            "port": RT.dlna_port,
            "url": f"http://{lan_ip()}:{RT.dlna_port}" if dlna_on else None,
            "roots": s.get("dlna_roots") or [],
            "logs": dlna_logs,
        },
        "subtitle": sub_state(),
        "subtitleCache": subtitle_cache_summary(),
        "translate": sub_translate_stats(),
        "translateCache": translate_cache_summary(),
        "sync": SYNC.public(),
        "gpu": gpu_info(),
        "sys": sys_info(),
        "settings": s,
        "events": logs,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "FSHost/1.0"

    # ---- 工具 ----
    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, rel: str) -> None:
        p = (UI_DIR / rel).resolve()
        # is_relative_to 而不是 startswith：后者不带分隔符，`/ui-backup/x` 这类
        # 兄弟目录前缀会被放行（目录遍历防护必须按路径组件比）。
        if not p.is_relative_to(UI_DIR.resolve()) or not p.is_file():
            self.send_error(404)
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".woff2": "font/woff2",
            ".json": "application/json; charset=utf-8",
        }.get(p.suffix.lower(), "application/octet-stream")
        data = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # 请求体上限：本机 API 的合法请求（设置/配置/字幕缓存回存）都远小于 32MB；
    # 无上限整读会被人一个请求打爆内存。
    MAX_BODY_BYTES = 32 * 1024 * 1024

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        if n > self.MAX_BODY_BYTES:
            # 不读体直接返回错误（读掉才是标准做法，但此处直接断开更省事——
            # 合法客户端永远不会触发这条路）
            raise ValueError("请求体过大")
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # ---- 路由 ----
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        try:
            if path == "/api/state":
                self._json(state_payload())
            elif path == "/api/settings":
                self._json({"ok": True, "settings": load_settings()})
            elif path == "/api/logs":
                with RT.lock:
                    self._json({"ok": True, "logs": RT.logs[-300:]})
            elif path == "/api/glossary":
                self._json(glossary_payload())
            elif path == "/api/sync":
                self._json({"ok": True, "sync": SYNC.public()})
            elif path == "/api/subtitle/cache":
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                vp = (q.get("video") or [""])[0]
                lg = (q.get("lang") or ["ja"])[0]
                if not vp:
                    self._json({"ok": False, "error": "缺少 video 参数"}, 400)
                else:
                    self._json(subtitle_cache_get(vp, lg))
            elif path == "/api/subtitle/cache/list":
                self._json(subtitle_cache_list())
            elif path == "/api/subtitle/config":
                self._json(subtitle_config())
            elif path == "/api/subtitle/models":
                self._json(subtitle_models())
            elif path == "/api/models/catalog":
                self._json(models_catalog_payload())
            elif path in ("/", "/index.html"):
                self._file("index.html")
            else:
                self._file(path.lstrip("/"))
        except Exception as e:
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        try:
            # CSRF 栅栏：浏览器发起的**跨站** POST 一定带 Origin 头；curl / 头显
            # (OkHttp) / 本机脚本不带。8790 能退出应用、改设置、删缓存文件，
            # 不能放任用户浏览器里的任意网页对它发请求（PNA 只救得了新 Chrome）。
            origin = (self.headers.get("Origin") or "").strip()
            if origin:
                host = urllib.parse.urlparse(origin).netloc.lower()
                if host not in (f"127.0.0.1:{UI_API_PORT}", f"localhost:{UI_API_PORT}"):
                    self._json({"ok": False, "error": "跨站请求被拒绝"}, 403)
                    return
            body = self._body()
        except ValueError as e:
            self._json({"ok": False, "error": str(e)}, 413)
            return
        except Exception as e:
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
            return
        try:
            if path == "/api/settings":
                self._json({"ok": True, "settings": save_settings(body)})
            elif path == "/api/win/show":
                # 供「重复启动」唤起已有实例的窗口（见 run() 开头的单实例处理）。
                # 托盘图标可能被系统收进溢出面板、用户找不到入口，这条路始终可用。
                TRAY.show_window()
                self._json({"ok": True})
            elif path == "/api/dlna/start":
                self._json(dlna_start(body.get("port"), body.get("roots")))
            elif path == "/api/dlna/stop":
                self._json(dlna_stop())
            elif path == "/api/dlna/roots":
                s = load_settings()
                roots = body.get("roots")
                if isinstance(roots, list):
                    roots = [norm_path(x) for x in roots if norm_path(x)]
                    save_settings({"dlna_roots": roots})
                    # 立刻校验：路径不存在的话 DLNA 会安静地列出空目录，
                    # 那头显里就只是"文件夹是空的"，完全猜不到是路径写错了。
                    bad = missing_roots(roots)
                    if bad:
                        RT.add_log(f"这些媒体根目录不存在：{'；'.join(bad)}", "err")
                    self._json({"ok": True, "roots": roots, "missing": bad})
                else:
                    self._json({"ok": True, "roots": s.get("dlna_roots")})
            elif path == "/api/subtitle/start":
                self._json(sub_start())
            elif path == "/api/subtitle/stop":
                self._json(sub_stop())
            elif path == "/api/subtitle/reclaim":
                self._json(sub_reclaim())
            elif path == "/api/subtitle/config":
                self._json(save_subtitle_config(body))
            elif path == "/api/models/download":
                self._json(model_download_start(body))
            elif path == "/api/subtitle/translate-test":
                self._json(sub_translate_selftest((body.get("text") or "").strip()))
            elif path == "/api/glossary/save":
                self._json(save_glossary(body))
            elif path == "/api/glossary/export":
                self._json(glossary_export_csv(body))
            elif path == "/api/glossary/import":
                self._json(glossary_import_csv(body))
            elif path == "/api/sync/devices":
                self._json(SYNC.list_devices())
            elif path == "/api/sync/connect":
                self._json(SYNC.connect((body.get("serial") or "").strip()))
            elif path == "/api/sync/disconnect":
                self._json(SYNC.disconnect())
            elif path == "/api/sync/run":
                self._json(SYNC.sync((body.get("kind") or "").strip()))
            elif path == "/api/sync/settings":
                self._json({"ok": True, "settings": save_settings(body)})
            elif path == "/api/subtitle/cache/save":
                self._json(subtitle_cache_save((body.get("video") or "").strip(),
                                               (body.get("lang") or "ja").strip(),
                                               body.get("segments") or [],
                                               body.get("meta")))
            elif path == "/api/subtitle/cache/clear":
                key = (body.get("key") or "").strip()
                try:
                    if key:
                        # 缓存键是 24 位十六进制（subtitle_cache_key）。不校验的话
                        # `..\..\xxx` 或绝对路径可以命中缓存目录之外的任意 .json
                        # （设置、词库、config）并删除——路径遍历。
                        if not re.fullmatch(r"[0-9a-f]{24}", key):
                            self._json({"ok": False, "error": "非法的缓存键"}, 400)
                            return
                        f = SUBTITLE_CACHE_DIR / f"{key}.json"
                        if f.exists():
                            f.unlink()
                    else:
                        for f in SUBTITLE_CACHE_DIR.glob("*.json"):
                            f.unlink()
                except Exception as e:
                    self._json({"ok": False, "error": str(e)})
                    return
                self._json({"ok": True})
            elif path == "/api/quit":
                self._json({"ok": True})
                request_quit()
            else:
                self._json({"ok": False, "error": "not found"}, 404)
        except Exception as e:
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def log_message(self, fmt: str, *args) -> None:  # 静默访问日志
        return


# ================================================================ 头显接口
class ExclusiveHTTPServer(ThreadingHTTPServer):
    """禁用 SO_REUSEADDR 的 HTTP 服务。

    Python 的 HTTPServer 默认 `allow_reuse_address = 1`。这在 Linux 上只是允许
    快速重启，但在 **Windows 上允许两个进程绑同一个端口**，之后连接会随机落到
    其中一个进程上——表现是"接口时好时坏、日志对不上、改了代码却不生效"，
    极难排查（本机调试就踩过：新进程明明有新路由，请求却被打到旧进程上返回 403）。

    宁可让第二个实例启动就明确报错，也不要留两个半残的服务在同一个端口上抢请求。
    """

    allow_reuse_address = False


class HeadsetHandler(BaseHTTPRequestHandler):
    """头显（Quest/PICO）专用接口，绑 0.0.0.0。

    **只放开这四个路由**，其余一律 403：

      GET  /api/headset/status        模型起来没有（头显轮询用）
      GET  /api/subtitle/cache        查该视频的字幕缓存（命中就完全不用跑 ASR）
      POST /api/subtitle/start        请求 PC 拉起 ASR + 翻译服务
      POST /api/subtitle/cache/save   播完把字幕存回来（否则缓存永远是空的）

    设计取舍：8790 上挂着设置、术语表 CSV 导入导出、设备同步（会调 adb）、退出……
    把它绑到局域网就等于把这些全开了。所以这里另起一个端口、另写一个 Handler，
    白名单是"正向枚举"的——新增路由必须显式加进来，不可能因为漏了一处判断而
    意外暴露。头显是可信设备，但局域网不一定只有头显。
    """

    server_version = "FSHost-Headset/1.0"
    # 头显端 OkHttp 不需要 CORS（不是浏览器）；去掉 ACAO * 是安全收紧——
    # 否则任意网页都能跨域读这四个接口的响应（里面含视频路径、服务状态）。
    _ALLOW_HEADERS = ("Content-Type", "Authorization")

    # ---- 工具 ----
    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _deny(self, path: str) -> None:
        log.warning("[头显] 拒绝未开放路径 %s（来自 %s）", path, self.client_address[0])
        self._json({"ok": False, "error": "该接口不对局域网开放"}, 403)

    def _query(self) -> dict:
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    def _drain(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        if n > Handler.MAX_BODY_BYTES:
            raise ValueError("请求体过大")
        try:
            raw = self.rfile.read(n)
            return json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return {}

    # ---- 路由 ----
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        try:
            if path == "/api/headset/status":
                self._json(headset_status())
            elif path == "/api/subtitle/cache":
                q = self._query()
                vp = (q.get("video") or [""])[0]
                lg = (q.get("lang") or ["ja"])[0]
                if not vp:
                    self._json({"ok": False, "error": "缺少 video 参数"}, 400)
                else:
                    self._json(subtitle_cache_get(vp, lg))
            else:
                self._deny(path)
        except Exception:
            # 局域网接口不回内部细节（路径/配置文件名都在异常文本里），只给通用文案
            self._json({"ok": False, "error": "服务器内部错误"}, 500)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        try:
            body = self._drain()
        except ValueError as e:
            self._json({"ok": False, "error": str(e)}, 413)
            return
        except Exception:
            self._json({"ok": False, "error": "请求体解析失败"}, 400)
            return
        try:
            if path == "/api/subtitle/start":
                RT.add_log(f"头显（{self.client_address[0]}）请求启动字幕服务", "info")
                self._json(sub_start())
            elif path == "/api/subtitle/cache/save":
                # 头显播完/看完整后把字幕存回来。没有这一条，缓存永远是空的——
                # "同一视频看第二遍不再重跑 ASR"就只是个写在界面上的说法。
                r = subtitle_cache_save((body.get("video") or "").strip(),
                                        (body.get("lang") or "ja").strip(),
                                        body.get("segments") or [],
                                        body.get("meta") or {})
                if r.get("ok"):
                    RT.add_log(f"头显保存字幕缓存：{r.get('count')} 段", "ok")
                self._json(r)
            else:
                self._deny(path)
        except Exception:
            self._json({"ok": False, "error": "服务器内部错误"}, 500)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._json({"ok": True})

    def log_message(self, fmt: str, *args) -> None:
        log.info("[头显] %s %s", self.client_address[0], fmt % args)


# ---------------------------------------------------------------- 字幕服务配置 / 术语表
def subtitle_models() -> dict:
    """枚举安装目录 models\ 下的 GGUF（UI 本地翻译模型下拉的数据源）。

    展示名优先用模型目录名（如 Sakura-7B-Qwen2.5-v1.0）；同一目录有多个量化
    或模型裸放在根目录时退回文件名（自带量化后缀，可区分）。path 是相对
    vendor\subtitle 的路径——与 config 的既有格式一致，translate_engine 按
    自身目录解析，UI 选什么就存什么，用户不再接触路径。
    """
    out = []
    if MODELS_DIR.exists():
        seen = set()
        for p in sorted(MODELS_DIR.rglob("*.gguf")):
            try:
                rel = os.path.relpath(p, SUBTITLE_DIR).replace("\\", "/")
                size_gb = p.stat().st_size / (1024 ** 3)
            except Exception:
                continue
            label = p.parent.name if p.parent != MODELS_DIR else p.stem
            if label in seen:          # 同目录多个量化版本：用文件名区分
                label = p.stem
            seen.add(label)
            out.append({"name": label, "path": rel, "size_gb": round(size_gb, 1)})
    return {"ok": True, "dir": str(MODELS_DIR), "models": out}


def subtitle_config() -> dict:
    cfg_file = SUBTITLE_DIR / "config.json"
    try:
        cfg = json.loads(cfg_file.read_text(encoding="utf-8")) if cfg_file.exists() else {}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    # API Key 不回明文给前端（只回 api_key_set / api_key_tail，前端显示占位符）
    return {"ok": True, "path": str(cfg_file), "config": _mask_translate_secrets(cfg)}


def save_subtitle_config(patch: dict) -> dict:
    """写回 config.json（asr/vad/segment/translate/glossary 分组）。

    · translate 组做**一层深合并**：否则前端只回传 openai.{base_url,model,api_key} 时，
      会把同组的 api_key_env/temperature/max_tokens 一起覆盖掉。
    · 打码过的 api_key（空串或含 *）不会写回，避免把掩码存进配置。
    """
    cfg_file = SUBTITLE_DIR / "config.json"
    try:
        patch = _strip_masked_keys(patch or {})
        with _SUBTITLE_FILE_LOCK:
            cfg = json.loads(cfg_file.read_text(encoding="utf-8")) if cfg_file.exists() else {}
            for group, values in patch.items():
                if group == "translate" and isinstance(values, dict) and isinstance(cfg.get(group), dict):
                    for k, v in values.items():
                        if isinstance(v, dict) and isinstance(cfg[group].get(k), dict):
                            cfg[group][k].update(v)          # 深一层：openai/local/ollama 段
                        else:
                            cfg[group][k] = v
                elif isinstance(values, dict) and isinstance(cfg.get(group), dict):
                    cfg[group].update(values)
                else:
                    cfg[group] = values
            tmp = cfg_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, cfg_file)
        # 术语表相关字段改动了 → 顺手让运行中的服务热同步（enabled 开关即时生效，
        # 不用重启；服务没在跑就静默跳过，下次启动自然按新配置初始化）
        if "glossary" in (patch or {}):
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{SUBTITLE_PORT}/glossary/reload",
                    method="POST", data=b"")
                urllib.request.urlopen(req, timeout=2).read()
            except Exception:
                pass
        RT.add_log("字幕服务配置已保存（重启服务后生效）", "ok")
        return {"ok": True, "config": _mask_translate_secrets(cfg)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


GLOSSARY_FILES = {"ja": "glossary_ja_zh.json", "en": "glossary_en_zh.json"}


def glossary_file(lang: str) -> "Path | None":
    fname = GLOSSARY_FILES.get(lang)
    return (SUBTITLE_DIR / fname) if fname else None


def glossary_payload() -> dict:
    out = {"ok": True, "langs": {}}
    for lang in GLOSSARY_FILES:
        f = glossary_file(lang)
        try:
            out["langs"][lang] = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        except Exception as e:
            # 读失败**绝不能**伪装成"空表"：前端拿到 ok:true + 空表就认为"词库是空的"，
            # 于是置 glossaryLoaded=true 放行保存；用户随后的常规操作（导入十几条新词
            # 再保存）会用这十几条把几千条的词库整体覆盖。save_glossary 的空表防护
            # 此时也判不出来——它读的是同一个坏文件。这里如实回 ok:false，
            # 前端 loadGlossary 的 `if (!r.ok) return` 就会拒绝置位、保存被拦下。
            out["langs"][lang] = {}
            out["ok"] = False
            out.setdefault("errors", {})[lang] = f"{type(e).__name__}: {e}"
    return out


def save_glossary(body: dict) -> dict:
    lang = body.get("lang")
    terms = body.get("terms")
    f = glossary_file(lang)
    if f is None or not isinstance(terms, dict):
        return {"ok": False, "error": "参数错误"}
    with _SUBTITLE_FILE_LOCK:
        # 现有文件存在却**解析不了**时一律拒绝写入并先备份：此时 terms 很可能是
        # 前端基于"读失败=空表"拼出来的残缺表（见 glossary_payload），
        # 直接 os.replace 就等于把用户的词库替换成残缺版；而且下面那条
        # "空表覆盖防护"的 existing 读的也是同一个坏文件，判不出来。
        if f.exists():
            try:
                json.loads(f.read_text(encoding="utf-8"))
            except Exception as e:
                try:
                    shutil.copy2(f, str(f) + ".bak")
                    kept = f"（原文件已备份为 {f.name}.bak）"
                except Exception:
                    kept = "（备份失败，原文件未改动）"
                return {"ok": False,
                        "error": f"现有词表无法解析，已拒绝覆盖{kept}：{type(e).__name__}: {e}"}
        # 空表覆盖防护：前端在术语表**加载失败/未完成**时内存里就是空 dict，
        # 此时保存会把几千条词库整表清空且还报成功。真想清空的合法路径必须
        # 显式带 allow_empty（前端在用户确认后补发）。
        if not terms and not body.get("allow_empty"):
            try:
                existing = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
            except Exception:
                existing = {}
            if existing:
                return {"ok": False, "needs_confirm": "empty",
                        "error": "新表是空的而现有词表不为空——若确要清空，请二次确认"}
        try:
            tmp = f.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(terms, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, f)
        except Exception as e:
            return {"ok": False, "error": str(e)}
    # 通知服务端热重载（若在跑）。结果必须如实回显：此前异常被 pass 吞掉，
    # 而成功文案无条件打印——字幕服务没在跑时用户会看到"已热重载"，
    # 实际服务下次启动读的还是启动时的旧词表，改动无声失效。
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{SUBTITLE_PORT}/glossary/reload", method="POST", data=b"")
        urllib.request.urlopen(req, timeout=2).read()
        reloaded = True
    except Exception:
        reloaded = False
    if reloaded:
        RT.add_log(f"术语表已保存并热重载（{lang} · {len(terms)} 条）", "ok")
    else:
        RT.add_log(f"术语表已保存（字幕服务当前未运行，重启服务后生效；{lang} · {len(terms)} 条）", "warn")
    return {"ok": True, "count": len(terms), "reloaded": reloaded}


# ---------------------------------------------------------------- 术语表 CSV
def glossary_export_csv(body: dict) -> dict:
    """导出术语表为 CSV（UTF-8 BOM，Excel 直接打开不乱码）。"""
    lang = body.get("lang")
    f = glossary_file(lang)
    path = (body.get("path") or "").strip()
    if f is None:
        return {"ok": False, "error": "参数错误：lang"}
    if not path:
        return {"ok": False, "error": "未指定导出路径"}
    try:
        data = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    except Exception as e:
        return {"ok": False, "error": f"读取术语表失败：{e}"}
    try:
        with open(path, "w", encoding="utf-8-sig", newline="") as fp:
            w = csv.writer(fp)
            w.writerow(["term", "translation"])
            for k, v in data.items():
                w.writerow([k, v])
    except Exception as e:
        return {"ok": False, "error": f"写入失败：{e}"}
    RT.add_log(f"术语表已导出（{lang} · {len(data)} 条 → {path}）", "ok")
    return {"ok": True, "count": len(data), "path": path}


def glossary_import_csv(body: dict) -> dict:
    """解析 CSV 并返回词条；写盘与热重载交给 /api/glossary/save，避免两套逻辑。

    mode="replace" 时直接覆盖写盘（原有条目全部丢弃），用于整表替换。
    """
    path = (body.get("path") or "").strip()
    lang = (body.get("lang") or "").strip()
    replace = (body.get("mode") or "").strip().lower() == "replace"
    if not path:
        return {"ok": False, "error": "未指定导入路径"}
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as fp:
            rows = list(csv.reader(fp))
    except UnicodeDecodeError:
        try:
            with open(path, "r", encoding="gbk", newline="") as fp:
                rows = list(csv.reader(fp))
        except Exception as e:
            return {"ok": False, "error": f"编码识别失败（试过 UTF-8 / GBK）：{e}"}
    except Exception as e:
        return {"ok": False, "error": f"读取失败：{e}"}

    rows = [r for r in rows if any((c or "").strip() for c in r)]
    if not rows:
        return {"ok": False, "error": "文件是空的"}

    # 首行是表头（term/translation 或 原文/译文）就跳过
    head = [c.strip().lower() for c in rows[0][:2]]
    header_words = {"term", "translation", "source", "target", "原文", "译文", "术语", "翻译"}
    if head and (set(head) & header_words):
        rows = rows[1:]

    terms: dict = {}
    skipped = 0
    for r in rows:
        if len(r) < 2:
            skipped += 1
            continue
        k = (r[0] or "").strip()
        v = (r[1] or "").strip()
        if not k:
            skipped += 1
            continue
        terms[k] = v
    if not terms:
        return {"ok": False, "error": "没有解析到有效词条（需要两列：原文, 译文）"}

    # 整表替换：直接写盘 + 热重载
    if replace:
        if lang not in GLOSSARY_FILES:
            return {"ok": False, "error": "参数错误：替换模式需要 lang"}
        saved = save_glossary({"lang": lang, "terms": terms})
        if not saved.get("ok"):
            return {"ok": False, "error": saved.get("error") or "写入失败"}
        RT.add_log(f"术语表已整体替换（{lang} · {len(terms)} 条）", "ok")
        return {"ok": True, "terms": terms, "count": len(terms),
                "skipped": skipped, "replaced": True}

    return {"ok": True, "terms": terms, "count": len(terms), "skipped": skipped}


# ================================================================ 设备同步
# 复用 vendor/dlna 的 funscript_sync / video_sync（两者接口一致：
# Config(local_folder/device_folder/adb_path/force_full/delete_extra) +
# Controller(get_adb/connect_usb/run_sync)）。本应用只做统一编排与状态上报。
SYNC_KINDS = {
    "script": {
        "label": "脚本（.funscript）",
        "module": "funscript_sync",
        "config": "FunscriptSyncConfig",
        "controller": "FunscriptSyncController",
        "local_key": "script_folder",
        "device_key": "device_folder_script",
    },
    "video": {
        "label": "视频",
        "module": "video_sync",
        "config": "VideoSyncConfig",
        "controller": "VideoSyncController",
        "local_key": "video_folder",
        "device_key": "device_folder_video",
    },
}


class SyncSlot:
    """一种同步类型（脚本 / 视频）的运行时状态。"""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.controller = None
        self.serial = ""
        self.device = ""
        self.busy = False
        self.logs: list[str] = []
        self.result: dict | None = None
        self.error = ""

    def add_log(self, msg: str) -> None:
        self.logs.append(msg)
        if len(self.logs) > 400:
            del self.logs[: len(self.logs) - 400]

    def public(self) -> dict:
        meta = SYNC_KINDS[self.kind]
        s = load_settings()
        return {
            "kind": self.kind,
            "label": meta["label"],
            "busy": self.busy,
            "connected": bool(self.serial),
            "serial": self.serial,
            "device": self.device,
            "local_folder": s.get(meta["local_key"]) or "",
            "device_folder": s.get(meta["device_key"]) or "",
            "error": self.error,
            "result": self.result,
            "logs": self.logs[-120:],
        }


class SyncService:
    """脚本 / 视频 → Quest 的 adb 增量同步编排。"""

    def __init__(self) -> None:
        self.slots = {k: SyncSlot(k) for k in SYNC_KINDS}
        self.lock = threading.RLock()
        self.adb_path = ""

    # ---- 懒加载 vendor 模块 ----
    def _resolve_adb(self) -> str:
        """adb 只有两个可能：界面填的路径，否则永远是自带的 tools/adb/adb.exe。

        不做任何系统探测：自带版本经过验证，避免悄悄用上用户机器上
        版本不明/位置不明的 adb。tools/fetch_adb.ps1 负责把 adb 放进来。
        """
        s = load_settings()
        p = str(s.get("adb_path") or "").strip()
        if p:
            return p
        return str(APP_DIR / "tools" / "adb" / "adb.exe")

    def _controller(self, kind: str):
        slot = self.slots[kind]
        if slot.controller is not None:
            return slot.controller
        meta = SYNC_KINDS[kind]
        sys.path.insert(0, str(VRDLNA_DIR))
        mod = __import__(meta["module"])
        s = load_settings()
        cfg = getattr(mod, meta["config"])()
        cfg.local_folder = s.get(meta["local_key"]) or ""
        cfg.device_folder = s.get(meta["device_key"]) or cfg.device_folder
        cfg.adb_path = self._resolve_adb()
        cfg.force_full = bool(s.get("sync_force_full"))
        cfg.delete_extra = bool(s.get("sync_delete_extra"))
        ctrl = getattr(mod, meta["controller"])(cfg)
        ctrl.on_log = slot.add_log
        slot.controller = ctrl
        return ctrl

    def _apply_settings(self, kind: str) -> None:
        """把最新设置同步进 vendor 配置对象。"""
        ctrl = self._controller(kind)
        meta = SYNC_KINDS[kind]
        s = load_settings()
        ctrl.config.local_folder = s.get(meta["local_key"]) or ""
        ctrl.config.device_folder = s.get(meta["device_key"]) or ctrl.config.device_folder
        ctrl.config.adb_path = self._resolve_adb()
        ctrl.config.force_full = bool(s.get("sync_force_full"))
        ctrl.config.delete_extra = bool(s.get("sync_delete_extra"))

    # ---- adb ----
    def adb(self):
        return self._controller("script").get_adb()

    def adb_path_resolved(self) -> str:
        try:
            return self.adb().adb_path
        except Exception:
            # 不把异常写进 script 槽的 error：这个方法在**每次状态轮询**都会被
            # public() 调到，没装 adb 的机器上同步徽章会永远显示失败+报错，
            # 而用户可能根本没打算同步。连接/同步路径各自会把真实错误记上。
            return ""

    def list_devices(self) -> dict:
        """列出 adb 设备；同时带上每个设备的型号（best-effort）。"""
        try:
            adb = self.adb()
        except Exception as e:
            return {"ok": False, "error": str(e), "adb": "", "devices": []}
        try:
            serials = adb.devices()
        except Exception as e:
            return {"ok": False, "error": str(e), "adb": adb.adb_path, "devices": []}
        out = []
        for s in serials:
            model = ""
            try:
                model = adb.shell(s, "getprop ro.product.model").strip()
            except Exception:
                pass
            out.append({"serial": s, "model": model, "usb": ":" not in s})
        return {"ok": True, "adb": adb.adb_path, "devices": out}

    def connect(self, serial: str) -> dict:
        with self.lock:
            if not serial:
                return {"ok": False, "error": "未选择设备"}
            res = {"ok": False, "serial": serial}
            for kind in SYNC_KINDS:
                self._apply_settings(kind)
                ctrl = self._controller(kind)
                try:
                    ctrl.get_adb().verify_device(serial)
                    ctrl.serial = serial
                    self.slots[kind].serial = serial
                    self.slots[kind].device = serial
                    self.slots[kind].error = ""
                except Exception as e:
                    self.slots[kind].error = str(e)
                    res["error"] = str(e)
            res["ok"] = all(s.serial for s in self.slots.values())
            RT.add_log(
                f"设备已连接：{serial}" if res["ok"] else f"设备连接失败：{res.get('error')}",
                "ok" if res["ok"] else "err",
            )
            return res

    def disconnect(self) -> dict:
        with self.lock:
            for slot in self.slots.values():
                slot.serial = ""
                slot.device = ""
                if slot.controller is not None:
                    slot.controller.serial = ""
            RT.add_log("已断开设备", "info")
            return {"ok": True}

    # ---- 同步 ----
    def sync(self, kind: str) -> dict:
        if kind not in SYNC_KINDS:
            return {"ok": False, "error": "未知的同步类型"}
        slot = self.slots[kind]
        # busy 检查与置位必须同锁：两个并发 /api/sync/run 曾能同时通过检查
        # （间隔里还有模块加载），各自起一条同步线程互相踩。
        with self.lock:
            if slot.busy:
                return {"ok": False, "error": "该类型正在同步中"}
            s = load_settings()
            meta = SYNC_KINDS[kind]
            folder = s.get(meta["local_key"]) or ""
            if not folder or not os.path.isdir(folder):
                return {"ok": False, "error": "请先选择有效的本地目录"}
            if not slot.serial:
                return {"ok": False, "error": "请先连接设备"}
            self._apply_settings(kind)
            slot.busy = True
            slot.error = ""
            slot.result = None
            slot.logs = []
        RT.add_log(f"开始同步{meta['label']} → {slot.serial}", "info")

        def _run() -> None:
            try:
                res = self._controller(kind).run_sync()
                if res is None:
                    slot.error = "同步未执行"
                else:
                    local_n, device_n, pushed = res
                    slot.result = {"local": local_n, "device": device_n, "pushed": pushed}
                    RT.add_log(
                        f"{meta['label']}同步完成：本地 {local_n} / 设备 {device_n} / 推送 {pushed}",
                        "ok",
                    )
            except Exception as e:
                slot.error = f"{type(e).__name__}: {e}"
                RT.add_log(f"{meta['label']}同步失败：{slot.error}", "err")
            finally:
                slot.busy = False

        threading.Thread(target=_run, daemon=True, name=f"sync-{kind}").start()
        return {"ok": True, "busy": True}

    def public(self) -> dict:
        return {
            "adb_path": self.adb_path_resolved(),
            "connected": any(s.serial for s in self.slots.values()),
            "serial": next((s.serial for s in self.slots.values() if s.serial), ""),
            "force_full": bool(load_settings().get("sync_force_full")),
            "delete_extra": bool(load_settings().get("sync_delete_extra")),
            "slots": {k: s.public() for k, s in self.slots.items()},
        }


SYNC = SyncService()


# ================================================================ 托盘
class TrayController:
    """系统托盘：左键双击显示主窗口，右键菜单「显示 / 退出」。

    复用 vendor/dlna/tray_icon.py（纯 ctypes，零依赖）。托盘消息循环在自己的
    守护线程里跑，事件通过 queue 传回主线程处理——避免跨线程直接操作窗口。
    """

    def __init__(self) -> None:
        self.q: "queue.Queue" = queue.Queue()
        self.icon = None
        self.window = None
        self.started = False
        self._stop = False
        self._quitting = False

    @property
    def quitting(self) -> bool:
        """真正退出中：closing 处理器据此放行，不再拦截为「最小化到托盘」。"""
        return self._quitting

    def begin_quit(self) -> None:
        """标记退出并停止托盘消息泵。"""
        self._stop = True
        self._quitting = True

    def start(self, window, tip: str = "FunScriptCast-Nexus") -> bool:
        self.window = window
        try:
            sys.path.insert(0, str(VRDLNA_DIR))
            from tray_icon import TrayIcon  # noqa: E402
        except Exception as e:
            RT.add_log(f"托盘不可用（{type(e).__name__}），改为最小化到任务栏", "warn")
            return False
        ico = APP_DIR / "tools" / "icon.ico"
        self.icon = TrayIcon(self.q, str(ico) if ico.exists() else None,
                             on_quit=self.quit_app)
        ok = self.icon.start(tip)
        self.started = ok
        if ok:
            threading.Thread(target=self._pump, daemon=True, name="tray-pump").start()
            RT.add_log("托盘图标已就绪", "ok")
        else:
            RT.add_log("托盘图标启动失败，改为最小化到任务栏", "warn")
        return ok

    def _pump(self) -> None:
        """消费托盘事件（守护线程）。"""
        while not self._stop:
            try:
                evt = self.q.get(timeout=0.3)
            except queue.Empty:
                continue
            except Exception:
                break
            try:
                if evt[0] == "show":
                    self.show_window()
                elif evt[0] == "menu":
                    self._popup_menu(int(evt[1]), int(evt[2]))
            except Exception as e:
                log.warning("托盘事件处理失败：%s", e)

    def _popup_menu(self, x: int, y: int) -> None:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        m = tk.Menu(root, tearoff=0)
        m.add_command(label="显示 FunScriptCast-Nexus", command=self.show_window)
        m.add_separator()
        m.add_command(label="退出", command=self.quit_app)
        try:
            m.tk_popup(x, y)
        finally:
            try:
                m.grab_release()
            except Exception:
                pass
            root.destroy()

    def show_window(self) -> None:
        if self.window is None:
            return
        try:
            self.window.restore()
            self.window.show()
        except Exception as e:
            log.warning("恢复窗口失败：%s", e)

    def hide_window(self) -> None:
        if self.window is None:
            return
        try:
            self.window.hide()
            RT.add_log("已最小化到托盘", "info")
        except Exception as e:
            log.warning("隐藏窗口失败：%s", e)

    def quit_app(self) -> None:
        request_quit()

    def stop(self) -> None:
        self._stop = True
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass
            self.icon = None


TRAY = TrayController()


def request_quit() -> None:
    """统一退出入口（UI 按钮 / 托盘菜单 / 无窗口模式）。

    窗口模式下必须销毁 pywebview 窗口——只 shutdown HTTP 服务的话事件循环
    仍在跑，进程不会退出。销毁动作放到独立线程，避免在窗口事件回调里重入。
    """
    RT.add_log("正在退出…", "warn")
    TRAY.begin_quit()

    def _do() -> None:
        win = TRAY.window
        if win is not None:
            try:
                win.destroy()
                return             # 窗口模式：主线程 run() 的 finally 会做收尾
            except Exception as e:
                log.warning("销毁窗口失败：%s", e)
        # 无窗口模式（或销毁失败）走到这里。os._exit 会**跳过** run() 的 finally，
        # 所以必须自己把字幕服务（连同 audiocpp 孙进程，约 3GB）和 DLNA 带走：
        # 否则它们变成孤儿，而下次启动的 reap_orphan_audiocpp 看到 8756 还开着
        # 会把它当成"在用"，于是永不回收，只能靠手动点「回收残留服务」。
        for _step in (TRAY.stop, sub_stop, dlna_stop):
            try:
                _step()
            except Exception:
                pass
        os._exit(0)

    threading.Thread(target=_do, daemon=True, name="quit").start()


# ================================================================ 无边框窗口微调
def tune_frameless_window(window, rounded: bool = True) -> dict:
    """无边框窗口的边角处理：找回原生缩放边框 + 圆角 + 阴影。

    pywebview 的 frameless 会把 FormBorderStyle 设成 None，缩放边框一并丢失。
    这里在窗体句柄就绪后重新加上 WS_THICKFRAME（只给缩放热区，不画标题栏），
    并用 DWM 打开圆角与投影，保持原生观感。
    """
    import ctypes

    out: dict = {}
    try:
        hwnd = int(window.native.Handle.ToInt64())
    except Exception as e:
        return {"ok": False, "error": repr(e)}
    GWL_STYLE = -16
    WS_THICKFRAME = 0x00040000
    WS_MAXIMIZEBOX = 0x00010000
    try:
        st = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_STYLE)
        new = (st | WS_THICKFRAME | WS_MAXIMIZEBOX) & ~0x00C00000  # 清 WS_CAPTION
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_STYLE, new)
        # 改样式位后需要重设一次尺寸，让非客户区重新计算
        SWP_NOMOVE, SWP_NOSIZE, SWP_NOZORDER, SWP_FRAMECHANGED = 0x0002, 0x0001, 0x0004, 0x0020
        ctypes.windll.user32.SetWindowPos(
            hwnd, 0, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED
        )
        out["style"] = hex(new)
        out["thickframe"] = bool(new & WS_THICKFRAME)
    except Exception as e:
        out["style_error"] = repr(e)
    if rounded:
        try:
            from webview.platforms.winforms import DwmSetWindowAttribute

            # 33 = DWMWA_WINDOW_CORNER_PREFERENCE, 2 = DWMWCP_ROUND
            DwmSetWindowAttribute(hwnd, 33, 2)
            out["rounded"] = True
        except Exception as e:
            out["rounded_error"] = repr(e)
    try:
        # 34 = DWMWA_BORDER_COLOR，0xFFFFFFFE = DWMWA_COLOR_NONE：去掉 Windows 11
        # 给顶层窗口画的系统描边（跟随主题/强调色，实测显示为蓝边）。Win10 不支持
        # 该属性会失败，静默忽略即可。
        bc = ctypes.c_uint(0xFFFFFFFE)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 34, ctypes.byref(bc), ctypes.sizeof(bc))
        out["border_none"] = True
    except Exception as e:
        out["border_error"] = repr(e)
    out["ok"] = bool(out.get("thickframe"))
    return out


# ================================================================ 前端 JS 桥
class NexusApi:
    """暴露给前端 JS 的窗口控制（无边框窗口需要自绘标题栏按钮）。

    注意：窗口引用必须放在**下划线开头**的属性里。pywebview 的 get_functions()
    会递归展开 js_api 对象的所有公开属性，直接挂 `self.window = window` 会导致
    `window.native.AccessibilityObject.…` 无限递归（RecursionError）。
    """

    def __init__(self) -> None:
        self._win = None

    def bind(self, window) -> None:
        self._win = window

    def win_minimize(self) -> dict:
        try:
            self._win.minimize()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def win_hide(self) -> dict:
        """隐藏到托盘。"""
        try:
            self._win.hide()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def win_close(self) -> dict:
        """自绘标题栏的关闭按钮。

        无边框窗口下不能让 FormClosing 拦截来「关闭到托盘」——pywebview 在
        窗体关闭流程里会走 Application.Exit()，即使 args.Cancel 也会结束事件
        循环。所以这里按设置显式二选一：隐藏到托盘，或直接销毁退出。
        """
        try:
            if bool(load_settings().get("close_to_tray", True)):
                self._win.hide()
            else:
                self._win.destroy()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def pick_folder(self, current: str = "", multiple: bool = False) -> dict:
        """系统「选择文件夹」对话框；取消返回 ok=False。

        multiple=True 时返回 paths（媒体根通常分散在好几个盘/目录，一次选完更省事）。
        """
        try:
            import webview

            res = self._win.create_file_dialog(
                webview.FOLDER_DIALOG,
                directory=current or "",
                allow_multiple=bool(multiple),
            )
            if not res:
                return {"ok": False, "cancelled": True}
            paths = [str(p) for p in res]
            return {"ok": True, "path": paths[0], "paths": paths}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def pick_file(self, mode: str = "open", filename: str = "", file_types: tuple = ()) -> dict:
        """系统文件对话框：mode=open 选文件，mode=save 选保存位置。"""
        try:
            import webview

            if mode == "save":
                res = self._win.create_file_dialog(
                    webview.SAVE_DIALOG,
                    save_filename=filename or "",
                    file_types=file_types or (),
                )
            else:
                res = self._win.create_file_dialog(
                    webview.OPEN_DIALOG,
                    allow_multiple=False,
                    file_types=file_types or (),
                )
            if not res:
                return {"ok": False, "cancelled": True}
            return {"ok": True, "path": res[0] if isinstance(res, (list, tuple)) else res}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}


JSAPI = NexusApi()


# ================================================================ 启动
def _create_app_mutex() -> None:
    """命名互斥量：给 Inno 安装器的 [Setup] AppMutex 用。

    托盘常驻进程在"运行中升级"时，没有这个标记用户只会看到"文件被占用"的
    裸错误；有了它安装器能识别程序在跑并提示先关闭。句柄有意不关闭——
    进程存活期间互斥量必须存在，进程退出由 OS 回收。
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.CreateMutexW(None, False, "FunScriptCastNexusMutex")
    except Exception:
        pass


def _setup_file_logging() -> None:
    """把宿主日志同时落到 `%APPDATA%\\FunScriptCast-Nexus\\logs\\host.log`。

    打包版是 GUI 子系统程序（`build/nexus.spec`: `console=False`）——没有控制台，
    而 `logging` 此前只装了 stderr handler ⇒ `log.warning/error` **全部被丢弃**。
    这正是"术语表读失败""设置文件解析失败""taskkill 失败"这类问题长期无声的直接原因：
    它们只写 log，用户看不见，事后也无从排查。字幕服务那边早有同类做法
    （`vendor/subtitle/run_server.py` tee 到 `logs/run_server.log`），宿主一直缺这一半。
    """
    try:
        d = SETTINGS_FILE.parent / "logs"
        d.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(d / "host.log", maxBytes=2 * 1024 * 1024,
                                 backupCount=3, encoding="utf-8")
        fh.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s"))
        logging.getLogger().addHandler(fh)
    except Exception as e:
        log.warning("宿主文件日志不可用（忽略）：%s", e)


def run(open_window: bool = True) -> None:
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")
    _setup_file_logging()
    _create_app_mutex()
    _migrate_settings()          # 先把历史设置里带引号的路径修掉，再读
    s = load_settings()

    # 单实例：已经在跑就唤起它的窗口并退出，不再起第二个进程。
    #
    # 为什么需要：托盘图标有可能被 Windows 收进溢出面板（用户反馈"最小化后托盘没有、
    # 不知道去哪重新打开"），此时双击启动是唯一直觉入口。若这里不做处理，
    # 第二次启动只会静默失败在端口占用上，用户更无路可走。
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(0.6)
        running = probe.connect_ex(("127.0.0.1", UI_API_PORT)) == 0
        probe.close()
    except Exception:
        running = False
    if running:
        log.info("检测到已有实例在运行（端口 %d），改为唤起它的窗口", UI_API_PORT)
        try:
            # **必须绕过系统代理**：urllib 默认继承 http_proxy/系统代理设置，
            # 本机请求被送去代理就会失败 —— 那样这里会误判成"没有实例在跑"，
            # 于是继续启动并撞上端口占用而报错（"打开软件打开不了还报错"）。
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            opener.open(
                f"http://127.0.0.1:{UI_API_PORT}/api/win/show",
                data=b"{}",
                timeout=3,
            ).read()
            log.info("已请求已有实例显示窗口，本进程退出")
            return
        except Exception as e:
            # 走到这里说明"端口有监听但唤不起窗口"，属于异常状态：
            # 与其继续启动（必然在端口占用上失败、还留下更难懂的报错），
            # 不如明确告知用户。
            log.error("已有实例在运行但无法唤起其窗口：%s", e)
            log.error("请先在任务管理器结束旧的 FunScriptCast-Nexus 进程，再重新启动。")
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(
                    None,
                    "检测到 FunScriptCast-Nexus 已在运行，但无法把它的窗口叫出来。\n\n"
                    "请在任务管理器里结束所有 FunScriptCast-Nexus / python 进程后再启动。",
                    "FunScriptCast-Nexus",
                    0x30,  # MB_ICONWARNING
                )
            except Exception:
                pass
            return

    RT.add_log(f"FunScriptCast-Nexus 启动（{lan_ip()}）", "ok")
    # 字幕服务的解释器来源：自包含的包和"借用上级 .venv"的包在日志里要能一眼区分，
    # 否则把 dist-app 拷到别的机器上才发现少依赖，会很莫名其妙。
    if SUBTITLE_PY_SOURCE:
        log.info("字幕服务解释器来源：%s → %s", SUBTITLE_PY_SOURCE, SUBTITLE_VENV_PY)
        if "非自包含" in SUBTITLE_PY_SOURCE:
            RT.add_log(f"字幕服务借用上级目录的 .venv（{SUBTITLE_VENV_PY.parent.parent}）；"
                       f"要把这个目录拷到别的机器，需先复制 .venv 进来", "warn")

    httpd = ExclusiveHTTPServer(("127.0.0.1", UI_API_PORT), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True, name="ui-api").start()
    log.info("UI/API: http://127.0.0.1:%d", UI_API_PORT)

    # 头显接口：绑局域网，但只放开四个路由（见 HeadsetHandler）。
    # 起不来（端口被占）不该拖垮整个程序——PC 端自己的功能不受影响。
    try:
        lan = ExclusiveHTTPServer(("0.0.0.0", LAN_API_PORT), HeadsetHandler)
        lan.daemon_threads = True
        threading.Thread(target=lan.serve_forever, daemon=True, name="headset-api").start()
        log.info("头显接口: http://%s:%d", lan_ip(), LAN_API_PORT)
        RT.add_log(f"头显接口已就绪：http://{lan_ip()}:{LAN_API_PORT}", "ok")
    except Exception as e:
        log.warning("头显接口启动失败（%s: %s），头显将无法查询缓存/拉起模型", type(e).__name__, e)
        RT.add_log(f"头显接口启动失败：{e}", "warn")

    # 按设置自动启动
    if s.get("dlna_auto_start") and s.get("dlna_roots"):
        dlna_start()
    if s.get("subtitle_auto_start"):
        sub_start()

    # 启动体检：上次被强杀可能留下 audiocpp 常驻进程（约 3 GB 内存）。
    # 判据是"它在跑，但字幕服务并不在"——那它就没有主人，是残留。
    threading.Thread(target=reap_orphan_audiocpp, daemon=True, name="reap-audiocpp").start()

    if not open_window:
        # 无窗口模式（调试/被外部托管）：保持进程存活
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return

    import webview  # 延迟导入，便于无窗口调试

    window = webview.create_window(
        "FunScriptCast-Nexus",
        f"http://127.0.0.1:{UI_API_PORT}/",
        width=1240,
        height=820,
        min_size=(960, 640),
        background_color="#07090f",
        text_select=False,
        frameless=True,          # 自绘标题栏（HTML），配合 .pywebview-drag-region 拖动
        easy_drag=False,
        js_api=JSAPI,
        hidden=bool(s.get("start_minimized", False)),
    )
    JSAPI.bind(window)

    # 关闭按钮 → 最小化到托盘（可在设置里关掉）
    def on_closing():
        if TRAY.quitting:
            return True            # 托盘「退出」放行
        # 必须现读设置：闭包捕获的启动时值是旧的——用户在设置页切换"关闭到托盘"
        # 之后，Alt+F4 走的仍是老行为（自绘关闭按钮 win_close 是现读的，两者曾不一致）
        if bool(load_settings().get("close_to_tray", True)) and TRAY.started:
            TRAY.hide_window()
            return False           # 拦截关闭
        return True

    try:
        window.events.closing += on_closing
    except Exception as e:
        log.warning("注册关闭事件失败：%s", e)

    def on_loaded():
        # 托盘已在 webview.start() 之前于**主线程**创建（见下方注释）。
        # 这里只做窗口相关的收尾。
        # 无边框窗口补回原生缩放边框 + 圆角
        try:
            r = tune_frameless_window(window)
            if not r.get("ok"):
                log.warning("无边框窗口微调失败：%s", r)
        except Exception as e:
            log.warning("无边框窗口微调异常：%s", e)
        if s.get("start_minimized"):
            try:
                window.hide()
            except Exception:
                pass

    try:
        window.events.loaded += on_loaded
    except Exception as e:
        log.warning("注册加载事件失败：%s", e)

    # 托盘必须在**主线程**创建（这里），不能放在 on_loaded 里 —— 实测根因：
    # on_loaded 由 pywebview 在它的 GUI 线程上回调，托盘宿主窗口就建在了那个线程上，
    # 而 TrayIcon 的消息循环又跑在第三个线程里，三者不一致 → Shell_NotifyIcon 注册
    # 返回成功、但图标始终不显示（日志可比对：独立脚本里建窗口的线程 == 主线程，图标正常）。
    # 旧项目 VR-DLNA 能用，也是因为它在 tkinter mainloop() 之前就把托盘建好了。
    if TRAY.start(window):
        log.info("托盘已就绪")
    else:
        log.warning("托盘启动失败，关闭窗口时将降级为最小化到任务栏")

    try:
        webview.start(debug=False)
    except Exception as e:
        log.error("webview.start 失败：%s", e)
        raise
    finally:
        TRAY.stop()
        sub_stop()
        dlna_stop()


if __name__ == "__main__":
    run(open_window="--no-window" not in sys.argv)
