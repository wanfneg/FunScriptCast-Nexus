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
    """字幕服务用的解释器：优先自带 venv，其次 PATH 上的 python。

    字幕服务依赖 torch（约 4 GB），不随 EXE 打包，仍走 .venv 子进程。
    打包后如果没带 .venv，就退回系统 python，让报错信息更直白。
    """
    env = os.environ.get("NEXUS_PY")
    if env:
        return Path(env)
    candidates = [
        APP_DIR / ".venv" / "Scripts" / "python.exe",
        APP_DIR / ".venv" / "bin" / "python",
    ]
    for c in candidates:
        if c.exists():
            return c
    found = shutil.which("python") or shutil.which("python3")
    if found:
        return Path(found)
    return Path(sys.executable)


SUBTITLE_VENV_PY = _subtitle_python()

# ---------------------------------------------------------------- 端口
UI_API_PORT = int(os.environ.get("FS_HOST_PORT", "8790"))   # 前端 + API
SUBTITLE_PORT = int(os.environ.get("FS_SUBTITLE_PORT", "8756"))
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
    name = p.name or video_path.rstrip("/").split("/")[-1]
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
    return video_path


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
    payload = {
        "video": _video_identity(resolved),
        "requested": video_path,
        "lang": lang,
        "config_fingerprint": _config_fingerprint(),
        "created_at": time.time(),
        "count": len(segments),
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
    return {"ok": True, "path": str(f), "count": len(segments)}


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
    return s


def save_settings(patch: dict) -> dict:
    s = load_settings()
    for k, v in patch.items():
        if k in DEFAULT_SETTINGS:
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


def sub_start() -> dict:
    with RT.lock:
        if RT.sub_proc and RT.sub_proc.poll() is None:
            return {"ok": True, "already": True}
        if RT.sub_starting:
            return {"ok": True, "starting": True}
        RT.sub_starting = True
        RT.sub_error = ""

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


def sub_stop() -> dict:
    with RT.lock:
        proc = RT.sub_proc
        RT.sub_proc = None
        RT.sub_ready = False
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=6)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception as e:
            log.warning("终止字幕服务失败：%s", e)
        RT.add_log("字幕服务已停止 · 显存已释放", "warn")
    return {"ok": True}


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
    if alive and RT.sub_ready:
        status = "ready"
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
        "health": health,
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
        "host": {"port": UI_API_PORT, "lan_ip": lan_ip(), "started_at": RT.started_at},
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
            elif path == "/api/dlna/start":
                self._json(dlna_start(body.get("port"), body.get("roots")))
            elif path == "/api/dlna/stop":
                self._json(dlna_stop())
            elif path == "/api/dlna/roots":
                s = load_settings()
                roots = body.get("roots")
                if isinstance(roots, list):
                    save_settings({"dlna_roots": roots})
                self._json({"ok": True, "roots": s.get("dlna_roots")})
            elif path == "/api/subtitle/start":
                self._json(sub_start())
            elif path == "/api/subtitle/stop":
                self._json(sub_stop())
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
        self.icon = TrayIcon(self.q, str(ico) if ico.exists() else None)
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

    def pick_folder(self, current: str = "") -> dict:
        """系统「选择文件夹」对话框；取消返回 ok=False。"""
        try:
            import webview

            res = self._win.create_file_dialog(
                webview.FOLDER_DIALOG,
                directory=current or "",
            )
            if not res:
                return {"ok": False, "cancelled": True}
            return {"ok": True, "path": res[0]}
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
    s = load_settings()
    RT.add_log(f"FunScriptCast-Nexus 启动（{lan_ip()}）", "ok")

    httpd = ThreadingHTTPServer(("127.0.0.1", UI_API_PORT), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True, name="ui-api").start()
    log.info("UI/API: http://127.0.0.1:%d", UI_API_PORT)

    # 按设置自动启动
    if s.get("dlna_auto_start") and s.get("dlna_roots"):
        dlna_start()
    if s.get("subtitle_auto_start"):
        sub_start()

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
        # 托盘常驻：即便关闭按钮不拦截，也需要托盘作为「启动即最小化」的恢复入口
        TRAY.start(window)
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

    try:
        webview.start(debug=False)
    finally:
        TRAY.stop()
        sub_stop()
        dlna_stop()


if __name__ == "__main__":
    run(open_window="--no-window" not in sys.argv)
