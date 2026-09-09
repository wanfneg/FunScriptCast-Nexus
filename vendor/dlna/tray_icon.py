# -*- coding: utf-8 -*-
"""
Windows 系统托盘图标（仅标准库 / ctypes）
=======================================
供“抚物器”在关闭窗口时隐藏到托盘使用。

- 左键双击：恢复主窗口
- 右键菜单：显示主界面 / 退出

如果当前不是 Windows 或 Shell_NotifyIcon 不可用，则 start() 返回 False，
调用方应降级为最小化到任务栏。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import queue
import threading
from pathlib import Path

WM_APP = 0x8000
WM_TRAY_CALLBACK = WM_APP + 1

# Shell_NotifyIcon 消息
NIM_ADD = 0
NIM_MODIFY = 1
NIM_DELETE = 2

# NOTIFYICONDATA 标志
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004

# 托盘鼠标消息
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002

# 预定义图标
IDI_APPLICATION = 32512

# LoadImageW 参数
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x00000010

# 消息窗口父窗口句柄常量：HWND_MESSAGE
HWND_MESSAGE = -3


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("hWnd", wt.HWND),
        ("uID", wt.UINT),
        ("uFlags", wt.UINT),
        ("uCallbackMessage", wt.UINT),
        ("hIcon", wt.HICON),
        ("szTip", wt.WCHAR * 128),
        ("dwState", wt.DWORD),
        ("dwStateMask", wt.DWORD),
        ("szInfo", wt.WCHAR * 256),
        ("uTimeoutOrVersion", wt.UINT),
        ("szInfoTitle", wt.WCHAR * 64),
        ("dwInfoFlags", wt.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wt.HICON),
    ]


LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wt.HWND),
        ("message", wt.UINT),
        ("wParam", wt.WPARAM),
        ("lParam", wt.LPARAM),
        ("time", wt.DWORD),
        ("pt", wt.POINT),
    ]


_user32 = ctypes.windll.user32
_shell32 = ctypes.windll.shell32
_kernel32 = ctypes.windll.kernel32

# 常用 WinAPI 原型（避免 64 位指针截断 / 返回值类型错误）
_user32.DefWindowProcW.restype = LRESULT
_user32.LoadIconW.restype = wt.HICON
_user32.CreateWindowExW.argtypes = [
    wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wt.HWND, wt.HANDLE, wt.HINSTANCE, ctypes.c_void_p,
]
_user32.CreateWindowExW.restype = wt.HWND
_user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
_user32.RegisterClassW.restype = wt.ATOM
_user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
_user32.GetCursorPos.restype = wt.BOOL
_kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
_kernel32.GetModuleHandleW.restype = wt.HMODULE
_user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
_user32.PostMessageW.restype = wt.BOOL
_user32.DestroyWindow.argtypes = [wt.HWND]
_user32.DestroyWindow.restype = wt.BOOL
_user32.LoadImageW.restype = wt.HANDLE
_user32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int, ctypes.c_int, wt.UINT]
_user32.DestroyIcon.argtypes = [wt.HICON]
_user32.DestroyIcon.restype = wt.BOOL
_user32.UnregisterClassW.argtypes = [wt.LPCWSTR, wt.HINSTANCE]
_user32.UnregisterClassW.restype = wt.BOOL
_shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
_shell32.Shell_NotifyIconW.restype = wt.BOOL


class TrayIcon:
    """最小化的 Windows 托盘图标类。

    通过 message_queue 向 Tk 主线程传递事件：
      ("show",)
      ("menu", x, y)
    """

    def __init__(self, message_queue: queue.Queue, icon_path: str | None = None):
        self._q = message_queue
        self._icon_path = icon_path
        self._custom_icon = None
        self._thread: threading.Thread | None = None
        self._hwnd: int | None = None
        self._nid = None
        self._running = False
        self._tip = "抚物器"
        self._wndproc = None
        self._error = ""
        self._class_name = ""
        self._class_atom = 0
        self._hinst = None

    @property
    def running(self) -> bool:
        return self._running

    def start(self, tip: str = "抚物器") -> bool:
        if self._running:
            return True
        try:
            self._tip = tip
            if not self._setup():
                self._cleanup()
                self._running = False
                return False
            self._running = True
            self._thread = threading.Thread(target=self._message_loop, daemon=True)
            self._thread.start()
            return True
        except Exception:
            self._cleanup()
            self._running = False
            return False

    def stop(self) -> None:
        if not self._running and self._hwnd is None:
            return
        self._running = False
        if self._hwnd:
            try:
                _user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
            except Exception:
                pass
        if self._thread is not None:
            try:
                self._thread.join(timeout=2.0)
            except Exception:
                pass
            self._thread = None
        self._cleanup()

    def _setup(self) -> bool:
        try:
            hinst = _kernel32.GetModuleHandleW(None)
            self._hinst = hinst
            # 每个实例用唯一类名，避免反复开关托盘时类冲突
            self._class_name = f"FuwuqiTrayWindow_{id(self)}"
            self._wndproc = WNDPROC(self._wnd_proc)

            wc = WNDCLASSW()
            wc.style = 0
            wc.lpfnWndProc = self._wndproc
            wc.cbClsExtra = 0
            wc.cbWndExtra = 0
            wc.hInstance = hinst
            wc.hIcon = None
            wc.hCursor = None
            wc.hbrBackground = None
            wc.lpszMenuName = None
            wc.lpszClassName = self._class_name
            atom = _user32.RegisterClassW(ctypes.byref(wc))
            if not atom:
                self._error = f"RegisterClass失败, GetLastError={_kernel32.GetLastError()}"
                return False
            self._class_atom = atom

            hwnd = _user32.CreateWindowExW(
                0,
                self._class_name,
                "FuwuqiTray",
                0,
                0, 0, 0, 0,
                wt.HWND(HWND_MESSAGE),
                None,
                hinst,
                None,
            )
            if not hwnd:
                self._error = f"CreateWindow失败, GetLastError={_kernel32.GetLastError()}"
                return False
            self._hwnd = hwnd
            return self._add_icon()
        except Exception as e:
            self._error = repr(e)
            return False

    def _message_loop(self) -> None:
        try:
            msg = MSG()
            while self._running:
                ret = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret <= 0:
                    break
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            pass
        finally:
            self._running = False
            self._cleanup()

    def _add_icon(self) -> bool:
        if not self._hwnd:
            return False
        try:
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = self._hwnd
            nid.uID = 1
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.uCallbackMessage = WM_TRAY_CALLBACK
            nid.hIcon = None
            if self._icon_path:
                icon_file = Path(self._icon_path)
                if icon_file.exists():
                    try:
                        nid.hIcon = _user32.LoadImageW(
                            None,
                            str(icon_file),
                            IMAGE_ICON,
                            32,
                            32,
                            LR_LOADFROMFILE,
                        )
                        if nid.hIcon:
                            self._custom_icon = nid.hIcon
                    except Exception:
                        nid.hIcon = None
            if not nid.hIcon:
                nid.hIcon = _user32.LoadIconW(None, IDI_APPLICATION)
                self._custom_icon = None
            tip = self._tip[:127]
            nid.szTip = tip
            self._nid = nid
            ok = bool(_shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)))
            if not ok:
                self._error = f"Shell_NotifyIcon失败, GetLastError={ctypes.GetLastError()}"
            return ok
        except Exception as e:
            self._error = repr(e)
            return False

    def _cleanup(self) -> None:
        if self._custom_icon:
            try:
                _user32.DestroyIcon(self._custom_icon)
            except Exception:
                pass
            self._custom_icon = None
        if self._nid is not None:
            try:
                _shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            except Exception:
                pass
            self._nid = None
        if self._hwnd:
            try:
                _user32.DestroyWindow(self._hwnd)
            except Exception:
                pass
            self._hwnd = None
        if self._class_atom and self._hinst:
            try:
                _user32.UnregisterClassW(self._class_name, self._hinst)
            except Exception:
                pass
            self._class_atom = 0
            self._class_name = ""

    def _wnd_proc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        if msg == WM_TRAY_CALLBACK:
            low = lparam & 0xFFFF
            if low == WM_LBUTTONDBLCLK:
                try:
                    self._q.put(("show",))
                except Exception:
                    pass
                return 0
            if low == WM_RBUTTONUP:
                try:
                    pt = wt.POINT()
                    _user32.GetCursorPos(ctypes.byref(pt))
                    self._q.put(("menu", int(pt.x), int(pt.y)))
                except Exception:
                    pass
                return 0
            return 0
        if msg == WM_CLOSE:
            _user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            _user32.PostQuitMessage(0)
            return 0
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)