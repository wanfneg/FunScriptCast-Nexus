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

import queue
import re
import subprocess
import threading
import time

LC_EXE = r"C:\Windows\System32\LiveCaptions.exe"
LC_WINDOW_CLASS = "LiveCaptionsDesktopWindow"
CAPTION_AID = "CaptionsTextBlock"
LANG_AID = "SpeechModelDropDown"
CONTINUE_AID = "ContinueButton"
LANG_NAMES = {"ja": "日语(日本)", "en": "英语(美国)"}
JA_NAME = LANG_NAMES["ja"]

SETTLE_SEC = 0.7        # 句尾标点出现后，静默这么久即视为定稿
LINE_IDLE_SEC = 2.2     # 尾行无标点时，静默这么久也定稿（口语停顿不打标点；R127 延迟修复）
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
        self._want_lang = LANG_NAMES["ja"]
        self._cur_lang = ""
        # 翻译 worker（R125 适配）：抓取线程只入列，独立线程串行翻译回填
        self._tq: "queue.Queue[dict] | None" = None
        self._tw: threading.Thread | None = None
        self._lang_code = "ja"

    # ---------------------------------------------------------------- 对外接口
    def start(self, lang: str = "ja") -> dict:
        """启动/换语言重启。

        R125 适配修复（全项目审查高危③）：旧实现换语言时 join(3) 超时后仍
        clear **共享** _stop 并无条件起新线程——切换语言恰是耗时操作（UIA 导航
        含多次 sleep），超时几乎必然 → 旧线程被 clear"复活"与新线程双跑（字幕
        重复、翻译翻倍，已用最小脚本复现）。现在：每次启动用**独立 Event**
        （代次隔离），join 后检查存活，仍存活则**拒绝启动**并如实报错，
        绝不复活旧线程；成功重启时清空上一会话的定稿句（会话隔离）。
        """
        want = LANG_NAMES.get(lang, LANG_NAMES["ja"])
        old = None
        with self._lock:
            if self._running and self._cur_lang == want:
                return self.state()
            if self._running:
                self._stop.set()          # 停旧代次
                old = self._thread
            self._running = False
        if old is not None and old.is_alive():
            old.join(timeout=8)
            if old.is_alive():
                with self._lock:
                    self._err = "上一抓取线程未在 8 秒内退出，已拒绝重启（避免双线程重复字幕）"
                return self.state()
        stop_ev = threading.Event()
        with self._lock:
            self._stop = stop_ev
            self._want_lang = want
            self._cur_lang = want
            self._lang_code = lang if lang in ("ja", "en") else "ja"
            self._err = ""
            self._lc_ok = False
            self._cur = ""
            self._lines = []              # 会话隔离：重启不清上会话残留（前端 F5 串场根因）
            self._running = True
            self._thread = threading.Thread(target=self._run, args=(stop_ev,),
                                            name="lc-capture", daemon=True)
            self._thread.start()
            self._tq = queue.Queue()
            for _wi in range(2):   # R128：双 worker——单句翻译 1~3s，串行会在句密时积压
                self._tw = threading.Thread(target=self._translate_worker, args=(stop_ev,),
                                            name=f"lc-translate-{_wi}", daemon=True)
                self._tw.start()
        return self.state()

    def stop(self) -> dict:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=3)
        # 不恢复窗口（方案 A，R122 用户拍板）：LC 保持最小化待命，
        # 下次 start 直接复用——用户全程看不到 LC 的存在感。
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
                # R128 调试快照：切句状态（pending 卡住时一眼定位）
                "dbg_pending": getattr(self, "_dbg_pending", ""),
                "dbg_committed_tail": (getattr(self, "_dbg_committed", "") or "")[-80:],
                "dbg_read_err": getattr(self, "_dbg_read_err", ""),
            }

    def clear(self) -> dict:
        with self._lock:
            self._lines.clear()
            self._cur = ""
        return self.state()

    # ---------------------------------------------------------------- LC 管家
    def _spawn_minimized(self) -> None:
        """启动 LC 并**直接以最小化状态**出现——CreateProcessW + STARTF_USESHOWWINDOW +
        SW_SHOWMINNOACTIVE(7)，避免"先在前台弹一下再缩"的闪烁（R122 用户观察驱动；
        参考项目是"先弹后缩"，此写法更进一步，实测窗口出现即 IsMinimized）。
        之后 _ensure_lc 还会补 WS_EX_TOOLWINDOW 把它从任务栏隐藏。"""
        import ctypes

        class STARTUPINFO(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("lpReserved", ctypes.c_wchar_p),
                        ("lpDesktop", ctypes.c_wchar_p), ("lpTitle", ctypes.c_wchar_p),
                        ("dwX", ctypes.c_ulong), ("dwY", ctypes.c_ulong),
                        ("dwXSize", ctypes.c_ulong), ("dwYSize", ctypes.c_ulong),
                        ("dwXCountChars", ctypes.c_ulong), ("dwYCountChars", ctypes.c_ulong),
                        ("dwFillAttribute", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                        ("wShowWindow", ctypes.c_ushort), ("cbReserved2", ctypes.c_ushort),
                        ("lpReserved2", ctypes.c_void_p), ("hStdInput", ctypes.c_void_p),
                        ("hStdOutput", ctypes.c_void_p), ("hStdError", ctypes.c_void_p)]

        class PROCESS_INFORMATION(ctypes.Structure):
            _fields_ = [("hProcess", ctypes.c_void_p), ("hThread", ctypes.c_void_p),
                        ("dwProcessId", ctypes.c_ulong), ("dwThreadId", ctypes.c_ulong)]

        si = STARTUPINFO()
        si.cb = ctypes.sizeof(si)
        si.dwFlags = 0x00000001     # STARTF_USESHOWWINDOW
        si.wShowWindow = 7          # SW_SHOWMINNOACTIVE：显示但最小化、不激活
        pi = PROCESS_INFORMATION()
        ok = ctypes.windll.kernel32.CreateProcessW(
            None, ctypes.c_wchar_p(LC_EXE), None, None, False, 0,
            None, None, ctypes.byref(si), ctypes.byref(pi))
        if not ok:
            raise OSError("CreateProcess 启动 LiveCaptions 失败")

    def _ensure_lc(self, auto) -> "auto.WindowControl | None":
        win = auto.WindowControl(searchDepth=1, ClassName=LC_WINDOW_CLASS)
        if not win.Exists(1):
            try:
                self._spawn_minimized()
            except Exception as e:
                raise RuntimeError(f"启动 Live Captions 失败：{e}")
            win = auto.WindowControl(searchDepth=1, ClassName=LC_WINDOW_CLASS)
            if not win.Exists(20):
                raise RuntimeError("Live Captions 窗口未出现")
        # 语言：需要时切日语
        try:
            self._ensure_language(auto, win, self._want_lang)
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

    @staticmethod
    def _invoke(ctrl) -> bool:
        """**无鼠标**激活控件（R128 用户反馈：UIA Click = 真实移动光标+点击，
        会抢用户的鼠标干扰正常使用）。优先级：Invoke 模式 → legacy 默认动作。
        全部失败返回 False（调用方跳过该步，绝不落回真实鼠标点击）。"""
        try:
            ctrl.GetInvokePattern().Invoke()
            return True
        except Exception:
            pass
        try:
            ctrl.DoDefaultAction()
            return True
        except Exception:
            return False

    @staticmethod
    def _select_item(item) -> bool:
        """**无鼠标**选中列表项：SelectionItem 模式 → legacy 默认动作。"""
        try:
            item.GetSelectionItemPattern().Select()
            return True
        except Exception:
            pass
        try:
            item.DoDefaultAction()
            return True
        except Exception:
            return False

    def _ensure_language(self, auto, win, want: str) -> None:
        """语言不对就切：展开 SpeechModelDropDown → 选中目标语言项 → 继续
        （R128：全程 **UIA 语义操作（Invoke/Select/Expand），零鼠标点击**——
        旧实现的 .Click() 是真实移动光标+点击，会抢用户的鼠标干扰正常使用）。"""
        combo = win.ComboBoxControl(AutomationId=LANG_AID)
        if combo.Exists(1):
            try:
                val = combo.GetValuePattern().Value or ""
            except Exception:
                val = ""
            if want in val:
                return
        # 面板未展开 → 展开（找不到 combo 时从"设置"菜单进入"更改语言"）
        if not combo.Exists(0.5):
            btn = win.ButtonControl(AutomationId="SettingsButton")
            if btn.Exists(1):
                self._invoke(btn)
                time.sleep(0.8)
            item = auto.MenuItemControl(AutomationId="ChangeLanguageMenuFlyoutItem")
            if item.Exists(1.5):
                self._invoke(item)
                time.sleep(1.5)
            combo = win.ComboBoxControl(AutomationId=LANG_AID)
        if not combo.Exists(1):
            return
        try:
            combo.GetExpandCollapsePattern().Expand()
        except Exception:
            time.sleep(0.3)   # Expand 失败不再用鼠标点（保持零鼠标承诺）；靠下方全局找列表项
        time.sleep(1.8)
        # 在 LC 进程的可见树里找"日语(日本)"列表项并**语义选中**（无鼠标）
        target = None
        my_pid = win.ProcessId
        for w in auto.GetRootControl().GetChildren():
            # R125 适配修复（审查中危）：限定 LC 进程——其它应用里同名列表项不可触碰。
            try:
                if w.ProcessId != my_pid:
                    continue
            except Exception:
                continue
            if not (w.Name or ""):
                continue

            def walk(c, d=0):
                nonlocal target
                if d > 14 or target:
                    return
                for ch in c.GetChildren():
                    try:
                        if ch.ControlTypeName == "ListItemControl" and (ch.Name or "") == want:
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
            if not self._select_item(target):
                self._log(f"[lc] 语言列表项选中失败（保持现状：{want}）")
                return
            time.sleep(1.2)
            cont = win.ButtonControl(AutomationId=CONTINUE_AID)
            if cont.Exists(1):
                self._invoke(cont)
            self._log(f"[lc] 已切换识别语言 → {want}")
        else:
            self._log(f"[lc] 未找到语言列表项：{want}（保持现状）")

    # ---------------------------------------------------------------- 抓取循环
    def _run(self, stop_ev: threading.Event) -> None:
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

        _read_err = {"n": 0, "last": ""}

        def _read() -> str:
            # 每轮动态查元素：CaptionsTextBlock 随 LC 状态出现/消失（就绪态可能不在树里）
            el = win.TextControl(AutomationId=CAPTION_AID)
            try:
                return el.Name if el.Exists(0.1) else ""
            except Exception as e:
                # R128 诊断：宿主（冻结 EXE）进程内曾出现"读恒空且无异常可见"——
                # 首个异常透出到 _err（只记前 3 次，防刷屏），其余仍按空处理。
                _read_err["n"] += 1
                if _read_err["n"] <= 3:
                    msg = f"UIA 读取异常：{type(e).__name__}: {e}"
                    _read_err["last"] = msg
                    setattr(self, "_dbg_read_err", msg)
                    self._log(msg)
                return ""

        last = _read()
        committed = last          # 已定稿前缀（LC 累积全文里的已处理部分）
        pending = ""              # 尚未定稿的尾巴（= committed 之后的部分）
        settle_at = 0.0           # 看到句尾标点后的静默计时
        line_changed_at = 0.0     # 尾行最后一次变化的时刻（无标点停顿定稿用）
        # R125 适配修复（审查中危·僵尸化自愈）：LC 进程会"僵尸化"（进程在、窗口
        # 从 UIA 消失，实测出现过）——旧实现 _read 恒空串、线程空转、lc_ok 恒 True，
        # "开着但永无字幕"且无任何提示。每 ~6s 校验一次窗口存活，连续失联 → 重建；
        # 重建失败达上限则如实置错退出（前端能看到失败态）。
        alive_check = 0
        rebuilds = 0
        while not stop_ev.is_set():
            time.sleep(POLL_SEC)
            alive_check += 1
            if alive_check >= 50:            # ≈6s
                alive_check = 0
                try:
                    if not win.Exists(0.2):
                        raise RuntimeError("窗口失联")
                except Exception:
                    rebuilds += 1
                    self._log(f"[lc] LC 窗口失联，尝试重建（第 {rebuilds} 次）")
                    if rebuilds > 3:
                        with self._lock:
                            self._err = "Live Captions 反复失联，已停止（请检查 Windows 实时字幕可用性）"
                            self._running = False
                        return
                    try:
                        win = self._ensure_lc(auto)
                        rebuilds = 0
                        self._log("[lc] LC 窗口已重建")
                    except Exception as e:
                        self._log(f"[lc] 重建失败：{e}")
                        continue
            txt = _read()
            if txt != last:
                last = txt
                self._dbg_pending = pending[-60:]
                self._dbg_committed = committed[-60:]
                if not txt.startswith(committed):
                    # LC 偶发重写（滚动/修正）：以最新全文重新对齐
                    committed = ""
                pending = txt[len(committed):]
                settle_at = 0.0
                # R128 修正：**换行 ≠ 句边界**——LC 界面窄，长句在它内部"显示折行"
                # （实测：一句被拆成"因为距离很"+"近。"两条碎字幕）。区分：
                # 换行拆出的行**以句尾标点结尾 → 句完成，定稿送翻**；
                # **无标点 → 只是显示折行，拼回当前句继续攒**（配合下方 2.5s 停顿
                # 兜底：真正的句间停顿仍会及时定稿，不丢延迟）。
                if "\n" in pending:
                    rows = pending.split("\n")
                    done_rows, remaining = rows[:-1], rows[-1]
                    buf = ""
                    n_committed = 0
                    for idx, ln in enumerate(done_rows):
                        buf += ln
                        if re.search(r"[。！？]\s*$", ln.strip()):
                            s = buf.strip()
                            if len(s.strip("。！？ 　")) >= 2:
                                self._commit(s)
                            buf = ""
                            n_committed = idx + 1
                    # 已定稿的行并入 committed；未定稿的折行内容**保留在 pending**
                    # （不变式：pending ≡ 全文[len(committed):]，**必须保留字面 \n**——
                    # R128 首版用拼接丢了 \n，不变式破坏 → 折行内容永远无法定稿）
                    if n_committed:
                        committed = committed + "\n".join(done_rows[:n_committed]) + "\n"
                    pending = "\n".join(done_rows[n_committed:] + [remaining])
                if re.search(r"[。！？]\s*$", pending.strip()):
                    settle_at = time.time()
                line_changed_at = time.time()
            elif pending:
                now = time.time()
                # 尾行定稿兜底（两个条件任一）：
                # ① 尾行以句号结尾 + 静默 0.7s（原语义）；
                # ② 尾行**无标点**但已 2.5s 无新字（口语停顿/句间换气——LC 不打标点，
                #    旧实现要等下一句才定稿，造成"字幕总比说话晚一大截"）。
                tail_s = pending.strip()
                if settle_at and (now - settle_at) >= SETTLE_SEC and re.search(r"[。！？]\s*$", tail_s):
                    self._commit(tail_s)
                    committed = committed + pending
                    pending = ""
                    settle_at = 0.0
                    line_changed_at = 0.0
                elif not re.search(r"[。！？]\s*$", tail_s) and \
                        line_changed_at and (now - line_changed_at) >= LINE_IDLE_SEC:
                    if len(tail_s.strip("。！？ 　")) >= 2:
                        self._commit(tail_s)
                    committed = committed + pending
                    pending = ""
                    settle_at = 0.0
                    line_changed_at = 0.0
            # 暴露"进行中句"（最后一行）
            tail = pending.splitlines()[-1].strip() if pending.strip() else ""
            with self._lock:
                self._cur = tail

    def _translate_worker(self, stop_ev: threading.Event) -> None:
        """串行消费翻译队列：保序（字幕按句序回填）；失败置空不重试（下一句
        自然接上，避免错误文案上屏与请求堆积）。"""
        while not stop_ev.is_set():
            try:
                rec = self._tq.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                rec["zh"] = self._translate(rec["ja"], self._lang_code) or ""
            except Exception as e:
                self._log(f"[lc] 翻译失败（该句不上屏）：{type(e).__name__}: {e}")
                rec["zh"] = ""

    def _commit(self, ja: str) -> None:
        """定稿句入列，翻译由独立 worker 完成——**抓取循环永不因翻译阻塞**
        （R125 适配修复：旧实现在抓取线程内同步翻译（最长 20s），期间 UIA 不再
        轮询、LC 全文滚动会触发重对齐丢句，与 docstring"异步补译文"承诺相反）。
        翻译失败 zh 置空串：显示层对空译文静默跳过（对齐手机端），错误只进日志
        ——不再把「（翻译失败：…）」当字幕渲染到画面。"""
        rec = {"ja": ja, "zh": "", "ts": time.time()}
        with self._lock:
            self._lines.append(rec)
            self._cur = ""
            if len(self._lines) > MAX_LINES:
                self._lines = self._lines[-MAX_LINES:]
            q = self._tq
        if q is not None:
            q.put(rec)
