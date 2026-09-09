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

import json
import logging
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
UI_DIR = APP_DIR / "ui"
VENDOR_DIR = APP_DIR / "vendor"
MODELS_DIR = APP_DIR / "models"

# ---------------------------------------------------------------- 内置依赖路径
# 两个原本独立运行的项目已 vendor 进本仓库：vendor/dlna（DLNA 服务）、
# vendor/subtitle（ASR + 翻译服务）。模型与 venv 也随仓库自带，
# 因此本应用不再依赖 E:\Development 下的任何其他目录。
VRDLNA_DIR = Path(os.environ.get("VRDLNA_DIR", str(VENDOR_DIR / "dlna")))
SUBTITLE_DIR = Path(os.environ.get("SUBTITLE_DIR", str(VENDOR_DIR / "subtitle")))
SUBTITLE_VENV_PY = Path(os.environ.get("NEXUS_PY", str(APP_DIR / ".venv" / "Scripts" / "python.exe")))

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
            py = SUBTITLE_VENV_PY if SUBTITLE_VENV_PY.exists() else Path(sys.executable)
            if not SUBTITLE_DIR.exists():
                raise RuntimeError(f"字幕服务目录不存在：{SUBTITLE_DIR}")
            flags = 0
            if os.name == "nt":
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            env = dict(os.environ)
            # 模型随仓库自带，用绝对路径注入，避免 cwd 变化导致相对路径失效
            if MODELS_DIR.exists():
                env.setdefault("ASR_MODEL", str(MODELS_DIR / "Qwen3-ASR-0.6B"))
            proc = subprocess.Popen(
                [str(py), "run_server.py", "--port", str(SUBTITLE_PORT)],
                cwd=str(SUBTITLE_DIR),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
            with RT.lock:
                RT.sub_proc = proc
                RT.sub_starting = False
            RT.add_log(f"字幕服务子进程已拉起 · PID {proc.pid}", "ok")
        except Exception as e:
            with RT.lock:
                RT.sub_starting = False
                RT.sub_error = f"{type(e).__name__}: {e}"
            RT.add_log(f"字幕服务启动失败：{RT.sub_error}", "err")

    threading.Thread(target=worker, daemon=True, name="sub-start").start()
    return {"ok": True, "starting": True}


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
        "version": "1.0.0",
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


def glossary_payload() -> dict:
    out = {"ok": True, "langs": {}}
    for lang, fname in (("ja", "glossary_ja_zh.json"), ("en", "glossary_en_zh.json")):
        f = SUBTITLE_DIR / fname
        try:
            out["langs"][lang] = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        except Exception:
            out["langs"][lang] = {}
    return out


def save_glossary(body: dict) -> dict:
    lang = body.get("lang")
    terms = body.get("terms")
    fname = {"ja": "glossary_ja_zh.json", "en": "glossary_en_zh.json"}.get(lang)
    if not fname or not isinstance(terms, dict):
        return {"ok": False, "error": "参数错误"}
    f = SUBTITLE_DIR / fname
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
        easy_drag=False,
        hidden=bool(s.get("start_minimized", False)),
    )

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
