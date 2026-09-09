#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Funscript 同步模块（移植自 FunscriptSyncGUI）
=============================================
在 VR-DLNA 中提供 PC funscript 目录 -> Android 设备的增量同步能力。

功能与 FunscriptSyncGUI 对齐：
  * 扫描本地 *.funscript 递归目录
  * 增量（设备缺失）或强制全量同步
  * 本地打包 zip（Fastest 压缩）-> adb push -> 设备端 unzip 预检+解压
  * push 后大小校验（防传输损坏）
  * 可选删除设备上多余 funscript
  * 数量校验
  * 连接方式：记住的无线 IP 直连 / USB 开无线 / Android 11+ 配对后连接 / USB 直连

仅使用 Python 标准库；adb 通过命令行调用。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional

def _sync_data_dir() -> Path:
    """源码模式用脚本目录；exe 模式用 %APPDATA%\VR-DLNA，避免 onefile 临时目录丢配置。"""
    if getattr(sys, "frozen", False):
        base = Path(os.environ.get("APPDATA") or str(Path.home()))
        d = base / "VR-DLNA"
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return d
    return Path(__file__).parent


CONFIG_FILE = _sync_data_dir() / "vr_dlna_funscript_config.json"


class AdbException(Exception):
    """adb 执行失败。"""


def _shq(text: str) -> str:
    """Android mksh 兼容的单引号转义。"""
    return "'" + text.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
@dataclass
class FunscriptSyncConfig:
    local_folder: str = ""
    device_folder: str = "/sdcard/Funscript"
    adb_path: str = ""
    force_full: bool = False
    delete_extra: bool = False

    @staticmethod
    def load() -> "FunscriptSyncConfig":
        try:
            if CONFIG_FILE.exists():
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                cfg = FunscriptSyncConfig()
                for k, v in data.items():
                    if hasattr(cfg, k):
                        setattr(cfg, k, v)
                return cfg
        except (OSError, ValueError, TypeError):
            pass
        return FunscriptSyncConfig()

    def save(self) -> None:
        try:
            tmp = CONFIG_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, CONFIG_FILE)
        except OSError as e:
            raise e


# ---------------------------------------------------------------------------
# adb 进程封装
# ---------------------------------------------------------------------------
class AdbClient:
    def __init__(self, adb_path: str):
        self.adb_path = adb_path
        if not adb_path:
            raise AdbException("adb 路径为空")
        if not os.path.isfile(adb_path):
            raise AdbException(
                f"找不到 adb：{adb_path}\n请在「设置…」里指定 adb.exe 路径"
            )

    @staticmethod
    def find_adb() -> str:
        """探测 adb.exe：环境变量 → 常见 SDK 路径 → PATH。"""
        candidates: list[str] = []
        android_home = os.environ.get("ANDROID_HOME")
        local_app_data = os.environ.get("LOCALAPPDATA")
        if android_home:
            candidates.append(os.path.join(android_home, "platform-tools", "adb.exe"))
        if local_app_data:
            candidates.append(os.path.join(local_app_data, "Android", "Sdk", "platform-tools", "adb.exe"))
        candidates.extend([
            r"C:\Android\platform-tools\adb.exe",
        ])
        for c in candidates:
            if os.path.isfile(c):
                return c
        path_var = os.environ.get("PATH", "")
        for d in path_var.split(os.pathsep):
            try:
                p = os.path.join(d.strip(), "adb.exe")
                if os.path.isfile(p):
                    return p
            except OSError:
                pass
        return "adb"

    def run(
        self,
        args: list[str],
        stdin_text: Optional[str] = None,
        timeout_ms: int = 300000,
        throw_on_error: bool = True,
    ) -> str:
        """执行 adb 命令，返回 stdout 字符串。"""
        cmd = [self.adb_path] + args
        try:
            proc = subprocess.run(
                cmd,
                input=stdin_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_ms / 1000.0,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            raise AdbException(f"adb 命令超时（>{timeout_ms / 1000:.0f}s）：{' '.join(cmd)}")
        except OSError as e:
            raise AdbException(f"adb 启动失败：{self.adb_path}（{e}）")
        stderr = (proc.stderr or "").strip()
        if throw_on_error and proc.returncode != 0:
            raise AdbException(f"adb 返回 {proc.returncode}：{' '.join(cmd)}\n{stderr}")
        return proc.stdout or ""

    def devices(self) -> list[str]:
        """在线设备（state == device）的 serial 列表。"""
        out = self.run(["devices"], timeout_ms=60000)
        result: list[str] = []
        for line in out.splitlines():
            parts = line.strip().split("\t")
            if len(parts) == 2 and parts[1].strip() == "device":
                result.append(parts[0].strip())
        return result

    def verify_device(self, serial: str) -> None:
        state = self.run(["-s", serial, "get-state"], throw_on_error=False, timeout_ms=60000).strip()
        if state != "device":
            raise AdbException(f"设备 {serial} 不在线（state={state}）")

    def push(self, serial: str, local: str, remote: str) -> None:
        self.run(["-s", serial, "push", local, remote], timeout_ms=1800000)

    def shell(self, serial: str, command: str) -> str:
        return self.run(["-s", serial, "shell", command], timeout_ms=600000)


# ---------------------------------------------------------------------------
# 同步引擎
# ---------------------------------------------------------------------------
class InvalidOperationException(Exception):
    """业务校验失败（与 C# InvalidOperationException 对应）。"""


def _open_zip_fastest(path: str):
    """创建 Fastest（level=1）zip 文件；若 zlib 不可用则退化为 ZIP_STORED。"""
    import zipfile

    try:
        return zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=1)
    except (TypeError, RuntimeError):
        return zipfile.ZipFile(path, "w", zipfile.ZIP_STORED)


class FunscriptSyncEngine:
    """本地 funscript -> 设备增量/全量同步。"""

    def __init__(self, local_folder: str, device_folder: str, serial: str, adb: AdbClient):
        self.local_folder = local_folder
        self.device_folder = (device_folder or "/sdcard/Funscript").rstrip("/") or "/"
        self.serial = serial
        self.adb = adb
        self.force_full = False
        self.delete_extra = False
        self.on_log: Optional[Callable[[str], None]] = None

    def _emit(self, msg: str) -> None:
        if self.on_log:
            self.on_log(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def scan_local(self) -> dict[str, str]:
        """返回 {相对路径(实际): 相对路径(实际)}，键即值，方便按 casefold 比较。"""
        root = Path(self.local_folder)
        result: dict[str, str] = {}
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix.lower() != ".funscript":
                continue
            try:
                rel = p.relative_to(root).as_posix()
            except ValueError:
                continue
            result[rel] = rel
        return result

    def scan_device(self) -> set[str]:
        """设备上的 funscript 相对路径集合（实际路径字符串）。"""
        result: set[str] = set()
        cmd = f"find {_shq(self.device_folder)} -name '*.funscript' -type f 2>/dev/null"
        out = self.adb.shell(self.serial, cmd)
        for line in out.splitlines():
            p = line.rstrip("\r\n")
            if not p:
                continue
            if p.casefold().startswith(self.device_folder.casefold()):
                p = p[len(self.device_folder):].lstrip("/")
            else:
                p = p.lstrip("/")
            if p:
                result.add(p)
        return result

    def remote_size(self, remote_path: str) -> int:
        """设备端文件大小（字节）；失败返回 -1。"""
        cmd = f"stat -c %s {_shq(remote_path)} 2>/dev/null || ls -l {_shq(remote_path)} 2>/dev/null"
        out = self.adb.shell(self.serial, cmd)
        for line in out.splitlines():
            t = line.strip()
            if t.isdigit():
                return int(t)
            parts = t.split()
            if len(parts) >= 5 and parts[4].isdigit():
                return int(parts[4])
        return -1

    def run(self) -> tuple[int, int, int]:
        """执行同步，返回 (本地数, 设备数, 本次推送数)。"""
        if not self.local_folder or not os.path.isdir(self.local_folder):
            raise InvalidOperationException("本地目录不存在：" + str(self.local_folder))

        unsafe = {"/", "/sdcard", "/storage/emulated/0", "/storage/emulated/legacy", "/mnt/sdcard"}
        dev_norm = self.device_folder.rstrip("/").casefold()
        if dev_norm in {u.casefold() for u in unsafe}:
            raise InvalidOperationException("设备目录过宽，禁止使用根目录/存储根目录进行同步，以免误删文件")

        self.adb.verify_device(self.serial)
        self._emit(f"设备校验通过：{self.serial}")

        local = self.scan_local()
        self._emit(f"本地 funscript 共 {len(local)} 个")

        self.adb.shell(self.serial, f"mkdir -p {_shq(self.device_folder)}")

        # 计算待推送集合
        if self.force_full:
            to_send = list(local.values())
            self._emit("强制全量模式：全部重推")
        else:
            device_set = self.scan_device()
            device_cf = {p.casefold() for p in device_set}
            self._emit(f"设备现有 {len(device_set)} 个")
            to_send = [rel for rel in local.values() if rel.casefold() not in device_cf]

        # 去重（大小写不敏感）
        seen: set[str] = set()
        dedup: list[str] = []
        for rel in to_send:
            cf = rel.casefold()
            if cf not in seen:
                seen.add(cf)
                dedup.append(rel)
        to_send = dedup
        self._emit(f"待推送 {len(to_send)} 个")

        pushed = 0
        if to_send:
            zip_local = os.path.join(tempfile.gettempdir(), f"funscript_sync_{uuid.uuid4().hex}.zip")
            try:
                self._emit("打包中（Fastest 压缩）...")
                with _open_zip_fastest(zip_local) as zf:
                    for rel in to_send:
                        src = os.path.join(self.local_folder, rel.replace("/", os.sep))
                        if not os.path.isfile(src):
                            continue
                        zf.write(src, rel)
                        pushed += 1
                zip_size = os.path.getsize(zip_local)
                self._emit(f"压缩包 {zip_size / 1024.0 / 1024.0:.1f} MB")

                zip_remote = f"{self.device_folder}/_funscript_sync_tmp.zip"
                self._emit("推送压缩包到设备...")
                try:
                    self.adb.push(self.serial, zip_local, zip_remote)

                    self._emit("校验推送完整性（大小比对）...")
                    remote_size = self.remote_size(zip_remote)
                    if remote_size < 0 or remote_size != zip_size:
                        raise AdbException(f"推送大小不一致：本地 {zip_size} vs 设备 {remote_size}")

                    self._emit("设备端预检（unzip -t）...")
                    test = self.adb.shell(
                        self.serial,
                        f"unzip -t {_shq(zip_remote)} >/dev/null 2>&1; echo $?",
                    ).strip()
                    if test.strip() != "0":
                        raise AdbException("设备端 unzip 校验失败")

                    self._emit("设备端解压...")
                    unzip_out = self.adb.shell(
                        self.serial,
                        f"unzip -o {_shq(zip_remote)} -d {_shq(self.device_folder)} >/dev/null 2>&1; echo $?",
                    ).strip()
                    if unzip_out != "0":
                        raise AdbException("设备端解压失败")
                finally:
                    # 无论成功/失败都清理设备端临时压缩包
                    try:
                        self.adb.shell(self.serial, f"rm -f {_shq(zip_remote)}")
                    except Exception:
                        pass
            finally:
                try:
                    os.remove(zip_local)
                except OSError:
                    pass

        # 删除检测：设备上存在但本地已不存在的文件
        if self.delete_extra:
            after = self.scan_device()
            local_cf = {rel.casefold() for rel in local}
            stale = [p for p in after if p.casefold() not in local_cf]
            if stale:
                total_stale = len(stale)
                self._emit(f"删除设备上多余文件 {total_stale} 个...")
                # 批量删除：一条 adb shell 命令删多个文件，避免 1 万多个文件启动 1 万多次 adb
                # 同时限制单条命令长度，避免超过 Windows/Android 命令行上限。
                MAX_RM_CMD_LEN = 28000
                batch: list[str] = []
                batch_len = len("rm -f ")
                done = 0
                for rel in stale:
                    quoted = _shq(f"{self.device_folder}/{rel}")
                    add_len = len(quoted) + 1
                    if batch and batch_len + add_len > MAX_RM_CMD_LEN:
                        self.adb.shell(self.serial, "rm -f " + " ".join(batch))
                        done += len(batch)
                        self._emit(f"已删除 {done}/{total_stale}")
                        batch = []
                        batch_len = len("rm -f ")
                    batch.append(quoted)
                    batch_len += add_len
                if batch:
                    self.adb.shell(self.serial, "rm -f " + " ".join(batch))
                    done += len(batch)
                    self._emit(f"已删除 {done}/{total_stale}")
                self._emit("多余文件删除完成")

            # 清理多余文件残留的空目录（保留设备根目录本身）
            self._emit("清理空目录...")
            self.adb.shell(
                self.serial,
                f"find {_shq(self.device_folder)} -mindepth 1 -depth -type d -empty -delete 2>/dev/null || true",
            )
            self._emit("空目录清理完成")

        # 数量校验
        final_count = len(self.scan_device())
        self._emit(f"同步完成：本地 {len(local)} 个，设备 {final_count} 个，本次推送 {pushed} 个")
        if final_count < len(local):
            self._emit(f"⚠ 注意：设备数量少于本地（差 {len(local) - final_count}），请检查设备目录配置")
        return len(local), final_count, pushed


# ---------------------------------------------------------------------------
# 连接辅助（对应 MainForm 的连接逻辑）
# ---------------------------------------------------------------------------
class FunscriptSyncController:
    """封装 GUI 与引擎之间的连接/同步流程（不依赖 tkinter）。"""

    def __init__(self, config: FunscriptSyncConfig):
        self.config = config
        self.serial = ""
        self.busy = False
        self.on_log: Optional[Callable[[str], None]] = None

    def _emit(self, msg: str) -> None:
        if self.on_log:
            self.on_log(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def get_adb(self) -> AdbClient:
        path = self.config.adb_path.strip()
        if not path:
            path = AdbClient.find_adb()
        return AdbClient(path)

    def connect_usb(self) -> bool:
        if self.busy:
            return False
        try:
            adb = self.get_adb()
            usb = next((d for d in adb.devices() if ":" not in d), None)
            if usb is None:
                self._emit("未发现 USB 设备")
                return False
            adb.verify_device(usb)
            self.serial = usb
            self._emit(f"✓ USB 连接成功：{usb}")
            return True
        except Exception as ex:
            self._emit(f"USB 连接失败：{ex}")
            return False

    def run_sync(self) -> Optional[tuple[int, int, int]]:
        """执行同步；返回 (本地数, 设备数, 推送数)。需要已连接（USB 或已设置 serial）。"""
        if self.busy:
            return None
        if not self.config.local_folder or not os.path.isdir(self.config.local_folder):
            self._emit("请先选择有效的本地 funscript 目录")
            raise InvalidOperationException("请先选择有效的本地 funscript 目录")
        if not self.serial:
            self._emit("未连接设备，先自动连接…")
            if not self.connect_usb():
                raise InvalidOperationException("无法连接 USB 设备，同步未执行")
        self.busy = True
        try:
            engine = FunscriptSyncEngine(
                local_folder=self.config.local_folder,
                device_folder=self.config.device_folder,
                serial=self.serial,
                adb=self.get_adb(),
            )
            engine.force_full = self.config.force_full
            engine.delete_extra = self.config.delete_extra
            engine.on_log = self.on_log
            self._emit("==== 开始同步 ====")
            local_n, device_n, pushed = engine.run()
            self._emit(f"==== 同步结束：本地 {local_n} / 设备 {device_n} / 本次推送 {pushed} ====")
            return local_n, device_n, pushed
        finally:
            self.busy = False
