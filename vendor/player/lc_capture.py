# -*- coding: utf-8 -*-
"""Live Captions 词级字幕抓取（PC 直听模式 · R121/122 立项）。

用户拍板路线（2026-10-02）：
· PC 场景：LC 直听系统音频（零虚拟声卡）；本模块负责"把 LC 的词级输出变成可用字幕流"。
· VR/手机场景：另行接 PCM→虚拟声卡（见 dev-archive 调研报告），本模块复用同一抓取层。

职责：
1. LC 管家——确保 LiveCaptions.exe 在跑（复用已有实例）、识别语言=日语、窗口隐藏；
2. 抓取——轮询 UIA 的 CaptionsTextBlock（~120ms），维护"定稿句 + 进行中句"两级状态；
3. 翻译——定稿句交给注入的 translate_fn（宿主侧接现有 8082 llama-server），异步补译文。

对宿主暴露的接口：start() / stop() / state()（宿主 API 轮询用）。
"""
from __future__ import annotations

import re
import subprocess
import threading
import time

LC_EXE = r"C:\Windows\System32\LiveCaptions.exe"
LC_WINDOW_CLASS = "LiveCaptionsDesktopWindow"
CAPTION_AID = "CaptionsTextBlock"
LANG_AID = "SpeechModelDropDown"
CONTINUE_AID = "ContinueButton"
JA_NAME = "日语(日本)"

SETTLE_SEC = 0.7        # 句尾标点出现后，静默这么久即视为定稿
POLL_SEC = 0.12         # 抓取轮询间隔
MAX_LINES = 40          # 保留的定稿句上限（供前端回看）


def _uia():
    import uiautomation as auto  # 延迟导入：非 Windows/LX 环境导入失败不影响其它功能
    return auto


class LiveCaptionsCapture:
    def __init__(self, translate_fn=None, log=print):
        self._translate = translate_fn or (lambda s: "")
        self._log = log
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # 状态（state() 返回；前端轮询）
        self._running = False
        self._lc_ok = False
        self._err = ""
        self._cur = ""            # 进行中句（原文）
        self._lines: list[dict] = []   # 定稿句 [{"ja","zh","ts"}]
        self._hide_window = True

    # ---------------------------------------------------------------- 对外接口
    def start(self) -> dict:
        with self._lock:
            if self._running:
                return self.state()
            self._stop.clear()
            self._err = ""
            self._running = True
            self._thread = threading.Thread(target=self._run, name="lc-capture", daemon=True)
            self._thread.start()
        return self.state()

    def stop(self) -> dict:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=3)
        self._restore_window()
        with self._lock:
            self._running = False
        return self.state()

    def state(self) -> dict:
        with self._lock:
            return {
                "running": self._running,
                "lc_ok": self._lc_ok,
                "error": self._err,
                "cur": self._cur,
                "lines": list(self._lines[-MAX_LINES:]),
            }

    def clear(self) -> dict:
        with self._lock:
            self._lines.clear()
            self._cur = ""
        return self.state()

    # ---------------------------------------------------------------- LC 管家
    def _ensure_lc(self, auto) -> "auto.WindowControl | None":
        win = auto.WindowControl(searchDepth=1, ClassName=LC_WINDOW_CLASS)
        if not win.Exists(1):
            try:
                subprocess.Popen([LC_EXE])
            except Exception as e:
                raise RuntimeError(f"启动 Live Captions 失败：{e}")
            win = auto.WindowControl(searchDepth=1, ClassName=LC_WINDOW_CLASS)
            if not win.Exists(20):
                raise RuntimeError("Live Captions 窗口未出现")
        # 语言：需要时切日语
        try:
            self._ensure_japanese(auto, win)
        except Exception as e:
            self._log(f"[lc] 语言检查异常（继续尝试）：{e}")
        # "后台静默"：参考 LiveCaptions-Translator 的方案 SW_MINIMIZE + WS_EX_TOOLWINDOW
        # （最小化且从任务栏/Alt-Tab 消失）。实测最小化后 UIA 照读不误、字幕持续更新。
        # ⚠️ 不要用 SW_HIDE：完全隐藏会让 UIA 对该窗口失明，元素全部读不到（R122 实测教训）。
        if self._hide_window:
            try:
                import ctypes
                u = ctypes.windll.user32
                hwnd = win.NativeWindowHandle
                self._hwnd = hwnd
                self._ex = u.GetWindowLongW(hwnd, -20)          # GWL_EXSTYLE
                u.ShowWindow(hwnd, 6)                            # SW_MINIMIZE
                u.SetWindowLongW(hwnd, -20, self._ex | 0x80)     # WS_EX_TOOLWINDOW
            except Exception as e:
                self._log(f"[lc] 最小化失败：{e}")
        return win

    def _restore_window(self) -> None:
        """停止时把 LC 窗口恢复回可见状态（用户自己开的 LC 不该被我们弄没）。"""
        try:
            import ctypes
            u = ctypes.windll.user32
            hwnd = getattr(self, "_hwnd", None)
            if hwnd:
                u.SetWindowLongW(hwnd, -20, getattr(self, "_ex", u.GetWindowLongW(hwnd, -20)))
                u.ShowWindow(hwnd, 9)   # SW_RESTORE
        except Exception:
            pass

    def _ensure_japanese(self, auto, win) -> None:
        """语言不对就切：展开 SpeechModelDropDown → 点 日语(日本) → 继续。"""
        combo = win.ComboBoxControl(AutomationId=LANG_AID)
        if combo.Exists(1):
            try:
                val = combo.GetValuePattern().Value or ""
            except Exception:
                val = ""
            if JA_NAME in val:
                return
        # 面板未展开 → 展开（找不到 combo 时从"设置"菜单进入"更改语言"）
        if not combo.Exists(0.5):
            btn = win.ButtonControl(AutomationId="SettingsButton")
            if btn.Exists(1):
                btn.Click()
                time.sleep(0.8)
            item = auto.MenuItemControl(AutomationId="ChangeLanguageMenuFlyoutItem")
            if item.Exists(1.5):
                item.Click()
                time.sleep(1.5)
            combo = win.ComboBoxControl(AutomationId=LANG_AID)
        if not combo.Exists(1):
            return
        try:
            combo.GetExpandCollapsePattern().Expand()
        except Exception:
            combo.Click()
        time.sleep(1.8)
        # 在整棵可见树里找"日语(日本)"列表项并点击
        target = None
        for w in auto.GetRootControl().GetChildren():
            if not (w.Name or ""):
                continue

            def walk(c, d=0):
                nonlocal target
                if d > 14 or target:
                    return
                for ch in c.GetChildren():
                    try:
                        if ch.ControlTypeName == "ListItemControl" and (ch.Name or "") == JA_NAME:
                            r = ch.BoundingRectangle
                            if r and r.width() > 0:
                                target = ch
                                return
                        walk(ch, d + 1)
                    except Exception:
                        pass

            walk(w)
            if target:
                break
        if target:
            target.Click()
            time.sleep(1.2)
            cont = win.ButtonControl(AutomationId=CONTINUE_AID)
            if cont.Exists(1):
                cont.Click()
            self._log("[lc] 已切换识别语言 → 日语")
        else:
            self._log("[lc] 未找到日语列表项（保持现状）")

    # ---------------------------------------------------------------- 抓取循环
    def _run(self) -> None:
        try:
            auto = _uia()
        except Exception as e:
            with self._lock:
                self._err = f"uiautomation 不可用：{e}"
                self._running = False
            return
        try:
            win = self._ensure_lc(auto)
        except Exception as e:
            with self._lock:
                self._err = str(e)
                self._running = False
            return
        with self._lock:
            self._lc_ok = True

        def _read() -> str:
            # 每轮动态查元素：CaptionsTextBlock 随 LC 状态出现/消失（就绪态可能不在树里）
            el = win.TextControl(AutomationId=CAPTION_AID)
            try:
                return el.Name if el.Exists(0.1) else ""
            except Exception:
                return ""

        last = _read()
        committed = last          # 已定稿前缀（LC 累积全文里的已处理部分）
        pending = ""              # 尚未定稿的尾巴（= committed 之后的部分）
        settle_at = 0.0           # 看到句尾标点后的静默计时
        while not self._stop.is_set():
            time.sleep(POLL_SEC)
            txt = _read()
            if txt != last:
                last = txt
                if not txt.startswith(committed):
                    # LC 偶发重写（滚动/修正）：以最新全文重新对齐
                    committed = ""
                pending = txt[len(committed):]
                settle_at = 0.0
                if re.search(r"[。！？]\s*$", pending.strip()):
                    settle_at = time.time()
                elif re.search(r"[。！？]\s*\n", pending):
                    settle_at = time.time()
            elif pending and settle_at and (time.time() - settle_at) >= SETTLE_SEC:
                # 静默定稿：pending 里"以标点结尾的完整行"逐行送翻，未完成的尾行留在 pending
                rows = pending.split("\n")
                if rows and not re.search(r"[。！？]\s*$", rows[-1].strip()) and len(rows) > 1:
                    done_rows, remaining = rows[:-1], rows[-1]
                elif rows and re.search(r"[。！？]\s*$", rows[-1].strip()):
                    done_rows, remaining = rows, ""
                else:
                    done_rows, remaining = [], pending
                for ln in done_rows:
                    ja = ln.strip()
                    if len(ja.strip("。！？ 　")) >= 2:
                        self._commit(ja)
                committed = committed + "\n".join(done_rows) + ("\n" if done_rows else "")
                pending = remaining
                settle_at = time.time() if remaining else 0.0
            # 暴露"进行中句"（最后一行）
            tail = pending.splitlines()[-1].strip() if pending.strip() else ""
            with self._lock:
                self._cur = tail

    def _commit(self, ja: str) -> None:
        rec = {"ja": ja, "zh": "", "ts": time.time()}
        with self._lock:
            self._lines.append(rec)
            self._cur = ""
        try:
            zh = self._translate(ja)
        except Exception as e:
            zh = f"（翻译失败：{e}）"
        with self._lock:
            rec["zh"] = zh
            if len(self._lines) > MAX_LINES:
                self._lines = self._lines[-MAX_LINES:]
