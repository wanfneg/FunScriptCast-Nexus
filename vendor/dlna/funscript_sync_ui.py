#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Funscript 同步页面（嵌入 VR-DLNA 主窗口的第二个标签页）
======================================================
主界面使用切换页面设计：
  - 页 1：DLNA服务器部署
  - 页 2：Funscript脚本同步
本模块负责构建页 2 的全部控件与逻辑。

连接方式：仅 USB（adb 直连 USB 设备），不含无线传输功能；日志隐藏时窗口会向上收起变小。
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from funscript_sync import (
    AdbClient,
    AdbException,
    FunscriptSyncConfig,
    FunscriptSyncController,
    InvalidOperationException,
)

# 保存当前同步页引用，供主窗口关闭时统一保存配置
_sync_page_ref: dict = {"page": None, "config": None, "save_ui": None}


def save_all_configs() -> None:
    """供主界面关闭时调用：保存同步页中的配置。"""
    page = _sync_page_ref.get("page")
    if page is not None and page.winfo_exists():
        try:
            save_ui = _sync_page_ref.get("save_ui")
            if save_ui is not None:
                save_ui()
            config = _sync_page_ref.get("config")
            if config is not None:
                config.save()
        except Exception as e:
            logging.getLogger("vr-dlna").exception("同步配置保存失败: %s", e)


def create_sync_page(parent: tk.Widget, root: tk.Misc, on_resize=None) -> None:
    """在 parent 中构建 Funscript 同步页面。root 用于定时器/对话框父窗口。"""
    config = FunscriptSyncConfig.load()
    ctrl = FunscriptSyncController(config)
    state = {"busy": False}
    events: queue.Queue = queue.Queue()

    def sync_log(msg: str) -> None:
        events.put(("msg", f"[{time.strftime('%H:%M:%S')}] {msg}"))

    # ---- 页面主体 ----
    frame_sync = ttk.LabelFrame(parent, text=" Funscript 同步（仅 USB 连接） ", padding=6)
    frame_sync.pack(fill="both", expand=True, padx=4, pady=4)

    grid_sync = ttk.Frame(frame_sync)
    grid_sync.pack(fill="x")
    grid_sync.columnconfigure(1, weight=1)

    ent_sync_local = ttk.Entry(grid_sync)
    ent_sync_device = ttk.Entry(grid_sync)
    var_sync_force = tk.BooleanVar()
    var_sync_delete = tk.BooleanVar()
    var_sync_status = tk.StringVar(value="未连接")

    # 行0：本地目录
    ttk.Label(grid_sync, text="本地目录：").grid(row=0, column=0, sticky="e", padx=(0, 4), pady=2)
    ent_sync_local.grid(row=0, column=1, columnspan=5, sticky="ew", pady=2)
    btn_sync_browse = ttk.Button(grid_sync, text="浏览…")
    btn_sync_browse.grid(row=0, column=6, sticky="ew", padx=(4, 0), pady=2)

    # 行1：设备目录 + USB 连接 + 设置
    ttk.Label(grid_sync, text="设备目录：").grid(row=1, column=0, sticky="e", padx=(0, 4), pady=2)
    ent_sync_device.grid(row=1, column=1, columnspan=4, sticky="ew", pady=2)
    btn_sync_usb = ttk.Button(grid_sync, text="连接USB", width=9)
    btn_sync_usb.grid(row=1, column=5, sticky="ew", padx=(2, 0), pady=2)
    btn_sync_settings = ttk.Button(grid_sync, text="设置…", width=7)
    btn_sync_settings.grid(row=1, column=6, sticky="ew", padx=(2, 0), pady=2)

    # 行2：选项 + 同步
    chk_sync_force = ttk.Checkbutton(grid_sync, text="强制全量同步", variable=var_sync_force)
    chk_sync_force.grid(row=2, column=0, columnspan=2, sticky="w", padx=(4, 8), pady=2)
    chk_sync_delete = ttk.Checkbutton(grid_sync, text="删除设备上多余文件", variable=var_sync_delete)
    chk_sync_delete.grid(row=2, column=2, columnspan=4, sticky="w", pady=2)
    btn_sync_run = ttk.Button(grid_sync, text="开始同步", width=10)
    btn_sync_run.grid(row=2, column=6, sticky="ew", pady=2)

    ttk.Label(frame_sync, textvariable=var_sync_status, foreground="#555").pack(anchor="w", pady=(4, 0))
    # 空闲时 determinate 空进度条，同步时才切换为 indeterminate
    sync_progress = ttk.Progressbar(frame_sync, mode="determinate", maximum=100, value=0)
    sync_progress.pack(fill="x", pady=2)

    btn_sync_toggle_log = ttk.Button(frame_sync, text="显示日志")
    btn_sync_toggle_log.pack(anchor="w", pady=(2, 0))

    frame_sync_log = ttk.Frame(frame_sync)
    # 默认隐藏日志，点击“显示日志”后才显示
    sync_log_text = tk.Text(frame_sync_log, height=12, state="normal")
    sync_scroll = ttk.Scrollbar(frame_sync_log, command=sync_log_text.yview)
    sync_log_text.config(yscrollcommand=sync_scroll.set)
    sync_log_text.pack(side="left", fill="both", expand=True)
    sync_scroll.pack(side="right", fill="y")

    sync_log_visible = {"show": False}

    def toggle_sync_log() -> None:
        sync_log_visible["show"] = not sync_log_visible["show"]
        if sync_log_visible["show"]:
            frame_sync_log.pack(fill="both", expand=True, pady=(2, 0))
            btn_sync_toggle_log.config(text="隐藏日志")
        else:
            frame_sync_log.pack_forget()
            btn_sync_toggle_log.config(text="显示日志")
        if on_resize is not None:
            on_resize(sync_log_visible["show"])

    def sync_load_config_ui() -> None:
        ent_sync_local.delete(0, tk.END)
        ent_sync_local.insert(0, config.local_folder)
        ent_sync_device.delete(0, tk.END)
        ent_sync_device.insert(0, config.device_folder)
        var_sync_force.set(config.force_full)
        var_sync_delete.set(config.delete_extra)

    def sync_save_ui() -> None:
        config.local_folder = ent_sync_local.get().strip()
        config.device_folder = ent_sync_device.get().strip() or "/sdcard/Funscript"
        config.force_full = bool(var_sync_force.get())
        config.delete_extra = bool(var_sync_delete.get())

    def save_ui_and_config() -> None:
        sync_save_ui()
        try:
            config.save()
        except OSError as e:
            sync_log(f"Funscript 配置保存失败：{e}")

    def sync_browse_local() -> None:
        d = filedialog.askdirectory(
            title="选择 PC 上的 funscript 文件夹",
            parent=root,
            initialdir=ent_sync_local.get() or None,
        )
        if d:
            ent_sync_local.delete(0, tk.END)
            ent_sync_local.insert(0, d)

    def sync_show_settings() -> None:
        dlg = tk.Toplevel(root)
        dlg.title("Funscript 连接设置")
        dlg.geometry("460x160")
        dlg.transient(root)
        dlg.grab_set()

        grid = ttk.Frame(dlg, padding=10)
        grid.pack(fill="both", expand=True)
        grid.columnconfigure(1, weight=1)

        ttk.Label(grid, text="adb 路径：").grid(row=0, column=0, sticky="e", padx=(0, 4), pady=6)
        ent_dlg_adb = ttk.Entry(grid)
        ent_dlg_adb.grid(row=0, column=1, sticky="ew", pady=6)
        if config.adb_path:
            ent_dlg_adb.insert(0, config.adb_path)
        else:
            found_adb = AdbClient.find_adb()
            if found_adb and os.path.isfile(found_adb):
                ent_dlg_adb.insert(0, found_adb)

        def browse_dlg_adb() -> None:
            initial = os.path.dirname(ent_dlg_adb.get()) if ent_dlg_adb.get() else ""
            f = filedialog.askopenfilename(
                title="选择 adb.exe",
                parent=dlg,
                filetypes=[("adb 可执行文件", "adb.exe")],
                initialdir=initial,
            )
            if f:
                ent_dlg_adb.delete(0, tk.END)
                ent_dlg_adb.insert(0, f)

        ttk.Button(grid, text="浏览…", command=browse_dlg_adb).grid(row=0, column=2, sticky="ew", padx=(4, 0), pady=6)

        def settings_ok() -> None:
            sync_save_ui()
            config.adb_path = ent_dlg_adb.get().strip()
            try:
                config.save()
            except OSError as e:
                sync_log(f"配置保存失败：{e}")
            sync_load_config_ui()
            if dlg.winfo_exists():
                dlg.destroy()

        btn_ok = ttk.Button(grid, text="确定", command=settings_ok)
        btn_cancel = ttk.Button(grid, text="取消", command=dlg.destroy)
        btn_ok.grid(row=1, column=1, sticky="e", padx=(0, 4), pady=8)
        btn_cancel.grid(row=1, column=2, sticky="w", padx=(4, 0), pady=8)
        dlg.bind("<Return>", lambda _e: settings_ok())
        dlg.bind("<Escape>", lambda _e: dlg.destroy())

    def sync_set_busy(busy: bool) -> None:
        state["busy"] = busy
        widget_state = "disabled" if busy else "normal"
        for w in (btn_sync_run, btn_sync_usb, btn_sync_settings, btn_sync_browse,
                  chk_sync_force, chk_sync_delete):
            w.config(state=widget_state)
        ent_sync_local.config(state=widget_state)
        ent_sync_device.config(state=widget_state)
        if busy:
            sync_progress.configure(mode="indeterminate")
            sync_progress.start(100)
        else:
            sync_progress.stop()
            sync_progress.configure(mode="determinate", value=0)

    def sync_drain_events() -> None:
        try:
            if not parent.winfo_exists():
                return
            while True:
                kind, payload = events.get_nowait()
                if kind == "msg":
                    sync_log_text.insert(tk.END, payload + "\n")
                    sync_log_text.see(tk.END)
                elif kind == "status":
                    var_sync_status.set(payload)
                elif kind == "busy":
                    sync_set_busy(payload)
                elif kind == "error":
                    messagebox.showerror("同步失败", payload, parent=root)
                    var_sync_status.set("失败")
                elif kind == "warning":
                    messagebox.showwarning("提示", payload, parent=root)
        except queue.Empty:
            pass
        except Exception:
            pass
        finally:
            try:
                if parent.winfo_exists():
                    root.after(120, sync_drain_events)
            except Exception:
                pass

    def sync_connect_usb() -> None:
        if state["busy"]:
            return
        save_ui_and_config()
        state["busy"] = True
        sync_set_busy(True)
        var_sync_status.set("连接 USB…")

        def worker() -> None:
            ctrl.on_log = lambda m: events.put(("msg", m))
            ok = ctrl.connect_usb()
            events.put(("status", "已连接（USB）" if ok else "未连接"))
            events.put(("busy", False))
            if not ok:
                events.put(("warning", "USB 连接失败：未发现设备或设备不在线"))

        threading.Thread(target=worker, daemon=True).start()

    def sync_run() -> None:
        if state["busy"]:
            return
        save_ui_and_config()
        if not config.local_folder or not os.path.isdir(config.local_folder):
            messagebox.showwarning("提示", "请先选择有效的本地 funscript 目录", parent=root)
            return
        ctrl.busy = False
        state["busy"] = True
        sync_set_busy(True)
        var_sync_status.set("同步中…")

        def worker() -> None:
            ctrl.on_log = lambda m: events.put(("msg", m))
            try:
                result = ctrl.run_sync()
                if result is not None:
                    events.put(("status", f"完成（设备 {result[1]} 个）"))
                else:
                    events.put(("status", "完成"))
            except InvalidOperationException as e:
                events.put(("error", str(e)))
            except AdbException as e:
                events.put(("error", f"同步失败：{e}"))
            except Exception as e:
                events.put(("error", f"同步失败：{e}"))
            finally:
                events.put(("busy", False))

        threading.Thread(target=worker, daemon=True).start()

    btn_sync_browse.config(command=sync_browse_local)
    btn_sync_usb.config(command=sync_connect_usb)
    btn_sync_settings.config(command=sync_show_settings)
    btn_sync_run.config(command=sync_run)
    btn_sync_toggle_log.config(command=toggle_sync_log)
    sync_load_config_ui()
    root.after(120, sync_drain_events)

    _sync_page_ref["page"] = parent
    _sync_page_ref["config"] = config
    _sync_page_ref["save_ui"] = sync_save_ui