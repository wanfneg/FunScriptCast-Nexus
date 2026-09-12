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
import hashlib
import json
import logging
import os
import queue
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
    candidates = [
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
        self.dlna_error = ""
        self.dlna_requests = 0
        self.sub_proc: subprocess.Popen | None = None
        self.sub_starting = False
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
    """ASR 模型 + 分段/VAD 配置 + 两张术语表的指纹。"""
    parts: list = []
    try:
        cfg = json.loads((SUBTITLE_DIR / "config.json").read_text(encoding="utf-8"))
        parts.append(json.dumps({"asr": cfg.get("asr"), "vad": cfg.get("vad"),
                                 "segment": cfg.get("segment")},
                                ensure_ascii=False, sort_keys=True))
    except Exception:
        parts.append("no-config")
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
    try:
        tmp = f.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, f)
    except Exception as e:
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
    s = dict(DEFAULT_SETTINGS)
    try:
        if SETTINGS_FILE.exists():
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            for k in DEFAULT_SETTINGS:
                if k in data:
                    s[k] = data[k]
    except Exception as e:
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
    s = load_settings()
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
    s = load_settings()
    port = int(port or s.get("dlna_port") or DLNA_PORT_DEFAULT)
    roots = roots if roots is not None else s.get("dlna_roots") or []

    def worker() -> None:
        try:
            m = _vrdlna_mod()
            from pathlib import Path as _P

            if not roots:
                raise RuntimeError("请先添加至少一个媒体根目录")
            media_roots = [m.MediaRoot(label=_P(p).name or "Videos", path=_P(p)) for p in roots]
            app = m.DlnaApp(media_roots, port)
            server = m.DlnaHTTPServer(("0.0.0.0", port), app)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            ssdp = m.SSDPServer(port, lan_ip())
            ssdp.start()
            with RT.lock:
                RT.dlna_server, RT.dlna_ssdp, RT.dlna_port = server, ssdp, port
                RT.dlna_starting = False
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
    if not health:
        return False
    with RT.lock:
        spawn = RT.sub_spawn_ts
    started = health.get("started_at")
    if not spawn:
        return False                # 我们没起过 → 不可能是自己人
    if not started:
        return True                 # 判不了，别误报
    return float(started) >= float(spawn) - 1.0


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

    # 端口已被别的进程占着：再拉一个也是徒劳（uvicorn 绑不上端口会立刻退出），
    # 直接复用它并如实标注，别制造一个"刚起来就死"的子进程。
    f = foreign_service()
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
                                   capture_output=True, text=True, timeout=40)
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
                RT.sub_proc = proc
                RT.sub_starting = False
            RT.add_log(f"字幕服务子进程已拉起 · PID {proc.pid}", "ok")
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


def _port_owner_pids(port: int) -> list:
    """占用某端口的 PID 列表（解析 netstat，不引第三方依赖）。"""
    if os.name != "nt":
        return []
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                             capture_output=True, text=True, timeout=10).stdout
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
                             capture_output=True, text=True, timeout=10).stdout
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
                           capture_output=True, timeout=20)
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
            err = err or f"8756 上的字幕服务不是本程序启动的（PID {foreign_pid}）"
    elif alive and RT.sub_ready:
        status = "ready"
    elif not alive and RT.sub_ready and not starting:
        status = "error"
        err = err or "8756 被某个进程占用，但它不应答 /health，不像是正常的字幕服务"
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
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=12)
        else:
            os.kill(pid, 15)
    except Exception as e:
        return {"ok": False, "error": f"结束 PID {pid} 失败：{type(e).__name__}: {e}"}
    RT.add_log(f"已结束残留的字幕服务（PID {pid}）", "warn")
    time.sleep(1.2)
    with RT.lock:
        RT.sub_ready = False
        RT.sub_last_probe = 0.0
    return {"ok": True, "killed": pid, "restart": sub_start()}


def headset_status() -> dict:
    """头显轮询用：模型起来没有。

    刻意只回最小字段——这个接口是暴露在局域网上的，`/api/state` 里有本机路径、
    日志、设备序列号之类的东西，不适合给头显（也就等于给整个局域网）。
    """
    st = sub_state()
    h = st.get("health") or {}
    status = st.get("status")
    return {
        "ok": True,
        "ready": status == "ready",
        "status": status,
        "error": st.get("error"),
        "asr": h.get("asr_model"),
        "translate": h.get("translate"),
        "version": app_version()["name"],
    }


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
        if not str(p).startswith(str(UI_DIR.resolve())) or not p.is_file():
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

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
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
            elif path in ("/", "/index.html"):
                self._file("index.html")
            else:
                self._file(path.lstrip("/"))
        except Exception as e:
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        body = self._body()
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
    # 头显端 OkHttp 的 Origin 是 null/自定义，DLNA 播放器也可能带跨源请求
    _ALLOW_HEADERS = ("Content-Type", "Authorization")

    # ---- 工具 ----
    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", ", ".join(self._ALLOW_HEADERS))
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
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
        except Exception as e:
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        body = self._drain()
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
        except Exception as e:
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._json({"ok": True})

    def log_message(self, fmt: str, *args) -> None:
        log.info("[头显] %s %s", self.client_address[0], fmt % args)


# ---------------------------------------------------------------- 字幕服务配置 / 术语表
def subtitle_config() -> dict:
    cfg_file = SUBTITLE_DIR / "config.json"
    try:
        cfg = json.loads(cfg_file.read_text(encoding="utf-8")) if cfg_file.exists() else {}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "path": str(cfg_file), "config": cfg}


def save_subtitle_config(patch: dict) -> dict:
    """浅合并写回 config.json（asr/vad/segment/translate/glossary 分组）。"""
    cfg_file = SUBTITLE_DIR / "config.json"
    try:
        cfg = json.loads(cfg_file.read_text(encoding="utf-8")) if cfg_file.exists() else {}
        for group, values in patch.items():
            if isinstance(values, dict) and isinstance(cfg.get(group), dict):
                cfg[group].update(values)
            else:
                cfg[group] = values
        tmp = cfg_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, cfg_file)
        RT.add_log("字幕服务配置已保存（重启服务后生效）", "ok")
        return {"ok": True, "config": cfg}
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
        except Exception:
            out["langs"][lang] = {}
    return out


def save_glossary(body: dict) -> dict:
    lang = body.get("lang")
    terms = body.get("terms")
    f = glossary_file(lang)
    if f is None or not isinstance(terms, dict):
        return {"ok": False, "error": "参数错误"}
    try:
        tmp = f.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(terms, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, f)
        # 通知服务端热重载（若在跑）
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{SUBTITLE_PORT}/glossary/reload", method="POST", data=b"")
            urllib.request.urlopen(req, timeout=2).read()
        except Exception:
            pass
        RT.add_log(f"术语表已保存并热重载（{lang} · {len(terms)} 条）", "ok")
        return {"ok": True, "count": len(terms)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


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
        cfg.adb_path = s.get("adb_path") or ""
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
        ctrl.config.adb_path = s.get("adb_path") or ""
        ctrl.config.force_full = bool(s.get("sync_force_full"))
        ctrl.config.delete_extra = bool(s.get("sync_delete_extra"))

    # ---- adb ----
    def adb(self):
        return self._controller("script").get_adb()

    def adb_path_resolved(self) -> str:
        try:
            return self.adb().adb_path
        except Exception as e:
            self.slots["script"].error = str(e)
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
                return
            except Exception as e:
                log.warning("销毁窗口失败：%s", e)
        os._exit(0)                # 无窗口模式兜底

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
def run(open_window: bool = True) -> None:
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")
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

    close_to_tray = bool(s.get("close_to_tray", True))
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
        if close_to_tray and TRAY.started:
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
