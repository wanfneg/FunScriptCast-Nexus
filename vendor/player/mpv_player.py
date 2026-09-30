# -*- coding: utf-8 -*-
"""桌面播放器控制层（M1）：libmpv 外挂窗口 + 属性轮询。

R108 原型定案：wid 直嵌 pywebview 黑屏、SetParent 被 mpv 反抗——外挂独立窗口是
唯一验证过的渲染路径。本模块只管「一个 mpv 实例」的生命周期与控制；媒体库/进度
持久化在 host_server 侧。

线程约定：libmpv 属性访问线程安全；宿主用 1s 轮询读 state（不订阅事件，避免
python-mpv 回调线程把异常吞进 libmpv）。

DLL：vendor\mpv\libmpv-2.dll 必须存在（tools\fetch_mpv.ps1 拉取）；import mpv 前
须把它加进 DLL 搜索路径（add_dll_directory + PATH），由 ensure_dll() 统一处理。
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent.parent   # vendor/player → 安装目录
DLL_DIR = APP_DIR / "vendor" / "mpv"

_imported = False


def ensure_dll() -> None:
    """把 vendor\\mpv 加进 DLL 搜索路径（必须在 import mpv 之前调用一次）。"""
    global _imported
    if _imported:
        return
    d = str(DLL_DIR)
    if (DLL_DIR / "libmpv-2.dll").is_file():
        try:
            os.add_dll_directory(d)
        except Exception:
            pass
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
    _imported = True


class MpvPlayer:
    """单实例 mpv 播放器。非线程安全——所有公开方法自带锁。"""

    def __init__(self, on_end=None, on_progress=None):
        self._lock = threading.RLock()
        self._mpv = None
        self._path = ""
        self._title = ""
        self._started_mono = 0.0
        self._on_end = on_end               # callback(path) 播放结束/窗口被关
        self._on_progress = on_progress     # callback(path, pos, dur) 约 5s 一次
        self._last_prog_save = 0.0
        self._poll_stop = threading.Event()
        threading.Thread(target=self._poll_loop, daemon=True, name="mpv-poll").start()

    # ------------------------------------------------------------ 控制
    def open(self, path: str, title: str = "", start_pos: float = 0.0) -> dict:
        with self._lock:
            ensure_dll()
            import mpv   # noqa: PLC0415（延迟导入：DLL 就绪后）
            if self._mpv is not None:
                self._terminate_locked()
            m = mpv.MPV(hwdec="auto", keep_open="no", osc=False,
                        input_default_bindings=False, input_cursor=False,
                        fullscreen="no")
            self._mpv = m
            self._path = path
            self._title = title or Path(path).stem
            self._started_mono = time.monotonic()
            self._last_prog_save = 0.0
            m.play(path)
            if start_pos and start_pos > 1:
                # 等待加载完成再 seek（播放未起时 seek 会被丢弃）
                for _ in range(60):
                    try:
                        if m.time_pos is not None:
                            break
                    except Exception:
                        pass
                    time.sleep(0.25)
                try:
                    m.time_pos = float(start_pos)
                except Exception:
                    pass
            return self.state()

    def toggle_pause(self) -> bool:
        with self._lock:
            if self._mpv is None:
                return False
            self._mpv.pause = not bool(self._mpv.pause)
            return bool(self._mpv.pause)

    def seek(self, pos: float) -> None:
        with self._lock:
            if self._mpv is not None:
                try:
                    self._mpv.time_pos = max(0.0, float(pos))
                except Exception:
                    pass

    def seek_rel(self, delta: float) -> None:
        with self._lock:
            if self._mpv is not None:
                try:
                    self._mpv.command("seek", delta)
                except Exception:
                    pass

    def set_volume(self, v: int) -> None:
        with self._lock:
            if self._mpv is not None:
                try:
                    self._mpv.volume = max(0, min(130, int(v)))
                except Exception:
                    pass

    def toggle_fullscreen(self) -> bool:
        with self._lock:
            if self._mpv is None:
                return False
            fs = not bool(self._mpv.fullscreen)
            self._mpv.fullscreen = fs
            return fs

    def stop(self) -> None:
        with self._lock:
            self._terminate_locked()

    def _terminate_locked(self) -> None:
        self._save_progress_now()
        m, self._mpv, path = self._mpv, None, self._path
        self._path = ""
        if m is not None:
            try:
                m.terminate()
            except Exception:
                pass
        if path and self._on_end:
            try:
                self._on_end(path)
            except Exception:
                pass

    # ------------------------------------------------------------ 状态
    def state(self) -> dict:
        with self._lock:
            m = self._mpv
            if m is None:
                return {"open": False, "playing": False, "path": "", "title": "",
                        "pos": 0.0, "dur": 0.0, "paused": False}
            try:
                alive = not m.core_idle
            except Exception:
                alive = False
            if not alive and time.monotonic() - self._started_mono < 8.0:
                # 起播宽限窗（R109）：play() 是异步的，加载完成前 core_idle 短暂
                # 为 True——不能当"已退出"杀掉（首版把刚启动的播放器自己杀了）。
                return {"open": True, "playing": False, "path": self._path,
                        "title": self._title, "pos": 0.0, "dur": 0.0,
                        "paused": False, "starting": True}
            if not alive:
                # mpv 窗口被用户关掉/播完退出：等同 stop（含 on_end 回调）
                self._terminate_locked()
                return {"open": False, "playing": False, "path": "", "title": "",
                        "pos": 0.0, "dur": 0.0, "paused": False}
            pos = m.time_pos or 0.0
            dur = m.duration or 0.0
            paused = bool(m.pause)
            return {"open": True, "playing": not paused and dur > 0,
                    "path": self._path, "title": self._title,
                    "pos": round(float(pos), 2), "dur": round(float(dur), 2),
                    "paused": paused}

    def _save_progress_now(self) -> None:
        m = self._mpv
        if m is None or not self._path or self._on_progress is None:
            return
        try:
            pos, dur = m.time_pos or 0.0, m.duration or 0.0
        except Exception:
            return
        if dur > 0:
            try:
                self._on_progress(self._path, float(pos), float(dur))
            except Exception:
                pass

    def _poll_loop(self) -> None:
        """1s 轮询：检测窗口被手关（core_idle）+ 周期性持久化进度。"""
        while not self._poll_stop.wait(1.0):
            try:
                with self._lock:
                    if self._mpv is None:
                        continue
                    alive = not self._mpv.core_idle
                    if not alive and time.monotonic() - self._started_mono < 8.0:
                        continue   # 起播宽限窗内不判死
                    if not alive:
                        self._terminate_locked()
                        continue
                    now = time.monotonic()
                    if now - self._last_prog_save >= 5.0:
                        self._last_prog_save = now
                        self._save_progress_now()
            except Exception:
                pass
