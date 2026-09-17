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

# 窗口类样式：**必须有 CS_DBLCLKS，否则 Windows 根本不产生 WM_LBUTTONDBLCLK**
# （只发两次 WM_LBUTTONUP）—— "双击图标没反应"就是缺了它。
CS_DBLCLKS = 0x0008

# 右键菜单（纯 Win32，不用 tkinter：托盘回调在非主线程，tk 菜单不可靠）
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
MF_STRING = 0x0000
MF_SEPARATOR = 0x0800
IDM_SHOW = 1
IDM_QUIT = 2


# LoadImageW 参数
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x00000010

# 消息窗口父窗口句柄常量：HWND_MESSAGE
HWND_MESSAGE = -3


def _tray_log(msg: str) -> None:
    """托盘诊断日志（临时）：统一写到 %TEMP%，排查「托盘不出现」时可查。"""
    try:
        import datetime
        import os

        p = os.path.join(os.environ.get("TEMP", "."), "nexus_tray_debug.log")
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now():%H:%M:%S} [tray_icon] {msg}\n")
    except Exception:
        pass


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
# argtypes 不能省：缺省时 ctypes 对未声明的参数按 32 位 c_int 转换，lParam/wParam
# 一旦 ≥0x100000000（64 位下完全可能）就抛 ctypes.ArgumentError —— 而这里是在
# **窗口回调内部**调用，回调抛异常等于把该消息吞掉（消息对应的行为静默丢失）。
# 当前必经消息恰好都在低地址所以没爆，属明确遗漏。
_user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
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
# 菜单句柄：ctypes.wintypes 没有 HMENU，用 HANDLE 等价替代
_HMENU = wt.HANDLE
_user32.CreatePopupMenu.restype = _HMENU
_user32.AppendMenuW.argtypes = [_HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR]
_user32.AppendMenuW.restype = wt.BOOL
_user32.TrackPopupMenu.argtypes = [_HMENU, wt.UINT, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, wt.HWND, ctypes.c_void_p]
_user32.TrackPopupMenu.restype = wt.BOOL
_user32.DestroyMenu.argtypes = [_HMENU]
_user32.DestroyMenu.restype = wt.BOOL
_user32.SetForegroundWindow.argtypes = [wt.HWND]
_user32.SetForegroundWindow.restype = wt.BOOL
_shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
_shell32.Shell_NotifyIconW.restype = wt.BOOL


class TrayIcon:
    """最小化的 Windows 托盘图标类。

    通过 message_queue 向 Tk 主线程传递事件：
      ("show",)
      ("menu", x, y)
    """

    def __init__(self, message_queue: queue.Queue, icon_path: str | None = None,
                 on_quit=None):
        self._q = message_queue
        self._icon_path = icon_path
        self._on_quit = on_quit
        self._custom_icon = None
        self._thread: threading.Thread | None = None
        self._ready: threading.Event | None = None
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
        """启动托盘。

        **窗口创建与消息循环必须在同一个线程**：Win32 的窗口消息只投递到创建该窗口的
        线程队列，而 GetMessageW 只取当前线程的消息。旧实现"窗口在调用者线程创建、
        消息循环在另一个新线程跑" → 消息永远收不到：
        图标能显示（Shell_NotifyIcon 立即生效），但双击/右键菜单全是死的。
        现在两者都放在这个新线程里，start() 等到窗口就绪后返回。
        """
        if self._running:
            return True
        self._tip = tip
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()
        # 等窗口建好（最多 5 秒），让调用方能立刻知道成败
        self._ready.wait(timeout=5.0)
        return self._running

    def _thread_main(self) -> None:
        """托盘线程：先建窗口，再在同一线程跑消息循环。"""
        try:
            if not self._setup():
                _tray_log(f"_setup 失败: {self._error}")
                self._cleanup()
                self._running = False
                self._ready.set()
                return
            self._running = True
            self._ready.set()
            _tray_log(f"托盘窗口已建立 hwnd={self._hwnd} thread={threading.get_ident()} main={threading.main_thread().ident}")
            self._message_loop()
        except Exception as e:
            _tray_log(f"托盘线程异常: {type(e).__name__}: {e}")
            self._cleanup()
            self._running = False
            self._ready.set()

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
            wc.style = CS_DBLCLKS   # 必须：否则收不到 WM_LBUTTONDBLCLK（双击无效）
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

            # 宿主窗口用 message-only 窗口（与旧项目 VR-DLNA 的可用实现一致）。
            #
            # 中途我曾把它改成"普通顶层窗口 + WS_EX_TOOLWINDOW"，理由是
            # "message-only 窗口在 Win11 新托盘上不显示" —— 那是没有依据的猜测，
            # 实测反而导致图标完全不显示；同机对照实验证明这份原始写法可用，已还原。
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
            _tray_log(f"托盘窗口已建立 hwnd={hwnd} thread={threading.get_ident()} main={threading.main_thread().ident}")
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
        except Exception as e:
            # 原来是静默 pass：托盘线程一旦异常退出，图标就无声消失、无处可查。
            # 排查「托盘不出现」时应能看到这里的原因。
            try:
                import datetime
                import os
                p = os.path.join(os.environ.get("TEMP", "."), "nexus_tray_debug.log")
                with open(p, "a", encoding="utf-8") as f:
                    f.write(f"{datetime.datetime.now():%H:%M:%S} _message_loop 异常: {type(e).__name__}: {e}\n")
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
                        _tray_log(f"LoadImageW(32x32) -> {hex(nid.hIcon) if nid.hIcon else 'NULL'}  file={icon_file}")
                    except Exception as e:
                        nid.hIcon = None
                        _tray_log(f"LoadImageW 异常: {type(e).__name__}: {e}")
                else:
                    _tray_log(f"图标文件不存在: {icon_file}")
            if not nid.hIcon:
                nid.hIcon = _user32.LoadIconW(None, IDI_APPLICATION)
                self._custom_icon = None
                _tray_log("回退到系统默认图标 IDI_APPLICATION（这就是托盘显示默认样式的原因）")
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

    def _show_menu(self, hwnd: int) -> None:
        """右键菜单：纯 Win32 弹出菜单。

        不用 tkinter：托盘回调跑在 TrayIcon 自己的线程上，那里没有 Tk 的主循环，
        而且 tk 的对象只在创建它的线程里安全 —— 实测右键毫无反应。
        纯 Win32 菜单只要在**有消息泵的线程**里 TrackPopupMenu 即可，本线程正好有。

        用 TPM_RETURNCMD 拿返回值而不是走 WM_COMMAND，省掉一整套菜单消息分发。
        """
        try:
            pt = wt.POINT()
            _user32.GetCursorPos(ctypes.byref(pt))
            hmenu = _user32.CreatePopupMenu()
            if not hmenu:
                return
            try:
                _user32.AppendMenuW(hmenu, MF_STRING, IDM_SHOW, "显示 FunScriptCast-Nexus")
                _user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
                _user32.AppendMenuW(hmenu, MF_STRING, IDM_QUIT, "退出")
                # 必须先置前台：否则点击菜单外部时菜单不会消失（Win32 托盘菜单的经典要求）
                _user32.SetForegroundWindow(hwnd)
                cmd = _user32.TrackPopupMenu(
                    hmenu, TPM_RIGHTBUTTON | TPM_RETURNCMD,
                    int(pt.x), int(pt.y), 0, hwnd, None,
                )
                _tray_log(f"托盘菜单选择 cmd={cmd}")
            finally:
                _user32.DestroyMenu(hmenu)

            if cmd == IDM_SHOW:
                self._q.put(("show",))
            elif cmd == IDM_QUIT:
                if self._on_quit is not None:
                    try:
                        self._on_quit()
                    except Exception as e:
                        _tray_log(f"退出回调异常: {type(e).__name__}: {e}")
                else:
                    self._q.put(("menu", int(pt.x), int(pt.y)))
        except Exception as e:
            _tray_log(f"_show_menu 异常: {type(e).__name__}: {e}")

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
                self._show_menu(hwnd)
                return 0
            return 0
        if msg == WM_CLOSE:
            _user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            _user32.PostQuitMessage(0)
            return 0
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)