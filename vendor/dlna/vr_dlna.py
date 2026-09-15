#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VR DLNA 服务器（网盘挂载优化版）
================================
专为 DeoVR + 云盘/网盘挂载（cd2/raidrive 等）场景设计的轻量 DLNA/UPnP
MediaServer，核心差异：

  * 目录浏览零探测：只列文件名，不调用 ffprobe 读取视频参数
    —— 网盘挂载下目录内视频再多也秒开（原 VR-Video-Toolbox-CE 的
       BrowseDirectChildren 对每个视频同步 ffprobe，网盘上必然超时）
  * UTF-8 文件名输出：DeoVR 中文文件名不乱码，App 同名脚本自动匹配正常
  * DeoVR 兼容：SSDP 自动发现 + description.xml + ContentDirectory SOAP
    Browse + ConnectionManager + HTTP Range 流 + 外挂字幕
    （sec:CaptionInfoEx / res 字幕条目）
  * 外挂字幕：视频同目录的 .srt/.ass/.vtt，DeoVR 播放时可切换

运行：python vr_dlna.py        （或 pythonw vr_dlna.py 无控制台窗口）
依赖：仅 Python 3.8+ 标准库（tkinter / http.server / socket）。
"""

from __future__ import annotations

import html
import http.client
import ipaddress
import json
import logging
import mimetypes
import os
import queue
import random
import re
import socket
import struct
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".ts", ".m2ts", ".mts", ".wmv", ".flv", ".mpg", ".mpeg", ".3gp", ".ogv", ".vob"}
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma", ".amr", ".m4b", ".aiff", ".ape"}
SUBTITLE_EXTS = {".srt", ".ass", ".vtt"}
SUBTITLE_MIME = {".srt": "application/x-subrip", ".ass": "application/x-ass", ".vtt": "text/vtt"}
HIDDEN_PREFIXES = (".", "$")

# 注意：UDN 变更会让已缓存该服务器的客户端（DeoVR 等）重新发现并重建目录树。
# 若客户端目录显示过期/空文件夹，可更换此 UUID 强制其重建缓存。
DEVICE_UUID = "a3c9e1f7-4b2d-4e5a-9c8b-1d2e3f4a5b6c"
SERVER_NAME = "抚物器"
SERVER_NAME_HTTP = "VR-DLNA"  # 协议头必须 ASCII
DLNA_FLAGS = "01700000000000000000000000000000"


def _app_data_dir() -> Path:
    """运行数据目录：源码模式用脚本目录；exe 模式用 %APPDATA%\VR-DLNA。"""
    if getattr(sys, "frozen", False):
        base = Path(os.environ.get("APPDATA") or str(Path.home()))
        d = base / "VR-DLNA"
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return d
    return Path(__file__).parent


def _app_icon_path() -> Path:
    """应用图标路径：源码模式用脚本目录；exe 打包时从 _MEIPASS 取。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "app_icon.ico"
    return Path(__file__).parent / "app_icon.ico"

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
SSDP_TARGETS = [
    "upnp:rootdevice",
    f"uuid:{DEVICE_UUID}",
    "urn:schemas-upnp-org:device:MediaServer:1",
    "urn:schemas-upnp-org:service:ContentDirectory:1",
    "urn:schemas-upnp-org:service:ConnectionManager:1",
]

ACCESS_LOG = str(_app_data_dir() / "vr_dlna_access.log")
ACCESS_LOG_MAX = 5 * 1024 * 1024  # 访问日志超过 5MB 轮转为 .old
BROKEN_DIRS_FILE = _app_data_dir() / "vr_dlna_broken_dirs.json"
BROKEN_DIRS_TTL = 24 * 3600  # 黑名单有效期：24h 后重新尝试（云盘可能恢复）

# 云盘虚拟卷"瞬时"错误码（值得重试）；其余（如 WinError 2 文件不存在）视为永久错误直接失败
TRANSIENT_WINERRORS = {1005, 1006, 64, 55, 121, 232, 59, 33, 87}

log = logging.getLogger("vr-dlna")
_log_lock = threading.Lock()  # 保护 ACCESS_LOG 写入

# ---- 应用设置（开机自启 / 启动最小化 / 关闭到托盘） ----
APP_SETTINGS_FILE = _app_data_dir() / "vr_dlna_settings.json"
APP_SETTINGS_DEFAULT = {
    "start_at_boot": False,
    "start_minimized": False,
    "close_to_tray": False,
    "auto_start_dlna": False,
}


def load_app_settings() -> dict:
    settings = dict(APP_SETTINGS_DEFAULT)
    try:
        if APP_SETTINGS_FILE.exists():
            data = json.loads(APP_SETTINGS_FILE.read_text(encoding="utf-8"))
            for k in APP_SETTINGS_DEFAULT:
                if k in data and isinstance(data[k], bool):
                    settings[k] = data[k]
    except Exception:
        pass
    return settings


def save_app_settings(settings: dict) -> None:
    try:
        tmp = APP_SETTINGS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, APP_SETTINGS_FILE)
    except OSError as e:
        log.warning("设置保存失败: %s", e)


def set_autostart(enabled: bool) -> bool:
    """写入/删除 HKCU 开机启动项。"""
    try:
        import winreg
        value_name = "抚物器"
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as k:
            if enabled:
                if getattr(sys, "frozen", False):
                    cmd = f'"{sys.executable}"'
                else:
                    cmd = f'"{sys.executable}" "{Path(__file__)}"'
                winreg.SetValueEx(k, value_name, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(k, value_name)
                except FileNotFoundError:
                    pass
        return True
    except Exception:
        return False



def _is_transient_oserror(e: OSError) -> bool:
    """判断 OSError 是否值得重试（云盘打盹类），FileNotFound 等永久错误不重试。"""
    if getattr(e, "winerror", None) in TRANSIENT_WINERRORS:
        return True
    return e.errno in (11, 35, 100, 101, 104, 110)  # EAGAIN/EWOULDBLOCK/网络类


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")


def _os_retry(fn, *, tries: int = 3, delay: float = 0.5, what: str = "fs op"):
    """云盘挂载（cd2/115/raidrive 虚拟卷）偶发瞬时错误（WinError 1005 等）。

    只对瞬时错误重试（永久错误如 FileNotFoundError 直接抛出，不浪费退避）；
    退避带随机抖动打散并发惊群。全部失败后抛出最后一次 OSError。
    """
    last: OSError | None = None
    for i in range(tries):
        try:
            return fn()
        except OSError as e:
            if not _is_transient_oserror(e):
                raise
            last = e
            if i < tries - 1:
                time.sleep(delay * (i + 1) * (0.7 + 0.6 * random.random()))
    assert last is not None
    log.warning("%s failed after %d tries: %s", what, tries, last)
    raise last


def _norm(p: Path) -> Path:
    """纯字符串规范化（Windows abspath + normpath），不访问文件系统。

    cd2 等云盘虚拟卷不支持 Path.resolve()（内部走 GetFinalPathNameByHandle，
    必然抛 WinError 1005「此卷不包含可识别的文件系统」），列目录/浏览
    必须避免任何句柄类操作。abspath 会合并 '..' 段并补全为绝对路径，
    对防穿越检查已足够。
    """
    return Path(os.path.abspath(os.path.normpath(str(p))))


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def lan_ips() -> list[str]:
    """枚举本机 IPv4 局域网地址，优先返回真正的局域网网段。"""
    ips: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and not ip.startswith("169.254.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    # 备用：UDP 探测
    if not ips:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            if not ip.startswith("127."):
                ips.append(ip)
        except Exception:
            pass

    def _lan_score(ip: str) -> int:
        """越小越优先：192.168 / 10 / 172.16-31 是真正局域网；100.64/10 多为 VPN/CGNAT。"""
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return 9
        first = int(a)
        if (first & 0xFFFF0000) == 0xC0A80000:      # 192.168.0.0/16
            return 0
        if (first & 0xFF000000) == 0x0A000000:      # 10.0.0.0/8
            return 1
        if (first & 0xFFF00000) == 0xAC100000:      # 172.16.0.0/12
            return 1
        if (first & 0xFFC00000) == 0x64400000:      # 100.64.0.0/10 (CGNAT/VPN)
            return 5
        return 3

    ips.sort(key=_lan_score)
    return ips


def _is_public_http_target(host: str) -> bool:
    """SSRF 防护：.strm 代理目标必须是公网地址（拒绝私网/回环/链路本地/组播）。"""
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET)
    except socket.gaierror:
        return False
    if not infos:
        return False
    for res in infos:
        try:
            ip = ipaddress.ip_address(res[4][0])
        except ValueError:
            return False
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False
    return True


def is_video(name: str) -> bool:
    return Path(name).suffix.lower() in VIDEO_EXTS


def is_audio(name: str) -> bool:
    return Path(name).suffix.lower() in AUDIO_EXTS


def is_subtitle(name: str) -> bool:
    return Path(name).suffix.lower() in SUBTITLE_EXTS


def is_strm(name: str) -> bool:
    """.strm 指针文件：内容是一行 http(s) URL，播放时服务器代理转发。"""
    return Path(name).suffix.lower() == ".strm"


def subtitle_mime(path: Path) -> str:
    return SUBTITLE_MIME.get(path.suffix.lower(), "text/plain")


def parse_range_header(value: str | None, total: int) -> tuple[int, int] | None | str:
    """解析 HTTP Range（仅单段）。

    返回 (start, end)（闭区间）；无 Range 或无法解析返回 None（按 200 全量处理）；
    多段 Range 或不可满足（start>=total 等）返回 "invalid"（应回 416）。
    """
    if not value:
        return None
    if "," in value:
        return "invalid"
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if not m:
        return None
    start_s, end_s = m.group(1), m.group(2)
    try:
        if start_s == "":
            # 后缀范围 bytes=-N：最后 N 字节
            n = int(end_s)
            if n <= 0:
                return "invalid"
            start = max(0, total - n)
            end = total - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else total - 1
        if start >= total:
            return "invalid"
        end = min(end, total - 1)
        if end < start:
            return "invalid"
        return start, end
    except ValueError:
        return "invalid"


def mime_for(path: Path) -> str:
    """按扩展名给 MIME（云盘卷上不探测内容）。"""
    ext = path.suffix.lower()
    table = {
        ".mp4": "video/mp4",
        ".mkv": "video/x-matroska",
        ".ts": "video/mp2t",
        ".m2ts": "video/mp2t",
        ".mts": "video/mp2t",
        ".mov": "video/quicktime",
        ".avi": "video/x-msvideo",
        ".wmv": "video/x-ms-wmv",
        ".webm": "video/webm",
        ".flv": "video/x-flv",
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".m4b": "audio/mp4",
        ".aac": "audio/aac",
        ".wav": "audio/wav",
        ".flac": "audio/flac",
        ".ogg": "audio/ogg",
        ".opus": "audio/opus",
        ".wma": "audio/x-ms-wma",
        ".amr": "audio/amr",
        ".aiff": "audio/aiff",
        ".ape": "audio/x-ape",
    }
    if ext in table:
        return table[ext]
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def dlna_proto(path: Path) -> str:
    """按容器给出 DLNA protocolInfo（PN 必须与容器匹配，否则 DeoVR 解复用失败）。"""
    ext = path.suffix.lower()
    base = f"DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS={DLNA_FLAGS}"
    if ext == ".mp4":
        return f"http-get:*:video/mp4:DLNA.ORG_PN=AVC_MP4_HP_HD_AAC;{base}"
    if ext == ".mkv":
        return f"http-get:*:video/x-matroska:DLNA.ORG_PN=MATROSKA;{base}"
    if ext in (".ts", ".m2ts", ".mts"):
        return f"http-get:*:video/mp2t:DLNA.ORG_PN=MPEG_TS_HD_NA_ISO;{base}"
    return f"http-get:*:{mime_for(path)}:{base}"


def dlna_pn_for(mime: str) -> str:
    """MIME → DLNA profile 名（用于 contentFeatures.dlna.org 响应头）。"""
    return {
        "video/mp4": "AVC_MP4_HP_HD_AAC",
        "video/x-matroska": "MATROSKA",
        "video/mp2t": "MPEG_TS_HD_NA_ISO",
    }.get(mime, "")


def parent_object_id(key: str) -> str:
    """DLNA key（不含 "F:"/"V:" 前缀）→ 其上级容器的 ObjectID。

    顶层目录与单根模式的根目录都归 CDS 根 "0"。
    （本服务器不产生空 key，空 key 按根处理。）
    """
    parent_key = key.rsplit("/", 1)[0] if "/" in key else ""
    return ("F:" + parent_key) if parent_key else "0"


def display_title(path: Path) -> str:
    """显示标题：直接用完整文件名（含扩展名），零探测。"""
    return path.name


# ---------------------------------------------------------------------------
# 媒体库（纯目录浏览，零探测）
# ---------------------------------------------------------------------------
@dataclass
class MediaRoot:
    label: str
    path: Path


class MediaLibrary:
    def __init__(self, roots: list[MediaRoot]):
        self.roots = roots
        # DlnaApp 构造时注入（http://IP:port）；为空则输出相对 URL（仅测试用）
        self.base_url = ""
        # 云盘上"列得出但打不开"的坏目录黑名单（cd2 虚拟卷坏条目），key=规范化路径, value=标记时间戳
        self._broken: dict[str, float] = {}
        self._broken_lock = threading.Lock()
        self._load_broken_dirs()

    # ---- 坏目录黑名单 ----
    def _load_broken_dirs(self) -> None:
        try:
            data = json.loads(BROKEN_DIRS_FILE.read_text(encoding="utf-8"))
            now = time.time()
            with self._broken_lock:
                self._broken = {
                    str(k): float(ts)
                    for k, ts in data.items()
                    if now - float(ts) < BROKEN_DIRS_TTL
                }
        except (OSError, ValueError, TypeError):
            self._broken = {}

    def _mark_broken(self, dir_path: Path) -> None:
        """标记坏目录并持久化（加锁 + 仅新增时写盘 + 原子替换）。"""
        key = str(_norm(dir_path))
        with self._broken_lock:
            if key in self._broken:
                return  # 已标记，不重复写盘、不续期
            self._broken[key] = time.time()
            try:
                self._broken = {k: v for k, v in self._broken.items() if time.time() - v < BROKEN_DIRS_TTL}
                tmp = BROKEN_DIRS_FILE.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(self._broken, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, BROKEN_DIRS_FILE)
            except OSError as e:
                log.warning("坏目录黑名单写盘失败: %s", e)

    def _is_broken(self, dir_path: Path) -> bool:
        """惰性 TTL 校验：过期条目自动剔除并返回 False（云盘恢复后重新可见）。"""
        key = str(_norm(dir_path))
        now = time.time()
        with self._broken_lock:
            ts = self._broken.get(key)
            if ts is None:
                return False
            if now - ts >= BROKEN_DIRS_TTL:
                del self._broken[key]
                return False
            return True

    def path_to_key(self, path: Path) -> str:
        """绝对路径 → 相对 key（URL 用，正斜杠）。云盘卷异常视为不匹配。"""
        for root in self.roots:
            try:
                rel = _norm(path).relative_to(_norm(root.path))
                if len(self.roots) == 1:
                    return rel.as_posix()
                return f"{root.label}/{rel.as_posix()}"
            except (OSError, ValueError):
                continue
        raise ValueError(f"path outside media roots: {path}")

    def key_to_path(self, key: str) -> Path | None:
        """URL key → 绝对路径（防穿越）。云盘卷异常时返回 None。"""
        rel = urllib.parse.unquote(key).replace("\\", "/").strip("/")
        if not rel or Path(rel).is_absolute():
            return None
        if ":" in rel:
            # 拒绝 Windows 备用数据流（ADS）后缀与盘符注入
            return None
        try:
            if len(self.roots) == 1:
                # 兼容陈旧的多根风格 key（label/rel）：剥离 label 前缀
                lbl = self.roots[0].label
                if lbl and rel.casefold().startswith(lbl.casefold() + "/"):
                    rel = rel[len(lbl) + 1:]
                    if not rel:
                        return None
                p = _norm(Path(self.roots[0].path) / rel)
                if not self._inside_root(self.roots[0], p):
                    return None
            else:
                label, _, rest = rel.partition("/")
                root = next((r for r in self.roots if r.label.casefold() == label.casefold()), None)
                if root is None or not rest:
                    return None
                p = _norm(Path(root.path) / rest)
                if not self._inside_root(root, p):
                    return None
        except OSError as e:
            log.warning("key_to_path resolve failed for %r: %s", key, e)
            return None
        # 词法 containment 防不了符号链接/联接逃逸：最终组件若是 reparse point 直接拒绝
        try:
            if os.lstat(p).st_file_attributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
                log.warning("拒绝 reparse point（符号链接/联接）: %s", p)
                return None
        except OSError:
            pass  # 不存在或云盘异常：由后续 open/stat 处理
        return p

    @staticmethod
    def _inside_root(root: MediaRoot, path: Path) -> bool:
        rp = _norm(root.path)
        p = _norm(path)
        if os.name == "nt":
            rp_s, p_s = str(rp).casefold(), str(p).casefold()
            if p_s == rp_s:
                return True
            return any(str(x).casefold() == rp_s for x in p.parents)
        return p == rp or rp in p.parents

    @staticmethod
    def _is_reparse(path: Path) -> bool:
        """是否符号链接/联接（REPARSE_POINT）：枚举时拒绝，防止脚本逃逸到根外。"""
        try:
            return bool(os.lstat(path).st_file_attributes & 0x400)
        except (OSError, AttributeError):
            return False

    def collect_scripts(self, base_key: str = "") -> list[dict]:
        """枚举 .funscript 文件，返回 [{key,url,name,basename,rel}]。

        base_key 为空时枚举全部根；否则以该路径键为枚举起点（须落在某根内部）。
        普通 DLNA Browse 不暴露 .funscript（媒体索引之外的文件），此法专供
        "远程脚本源"建立 basename→URL 索引使用。复用 key_to_path 的防穿越与
        reparse 拒绝规则；坏目录走黑名单；迭代式遍历避免深层目录递归溢出。
        """
        starts: list[Path] = []
        if base_key:
            bk = base_key.rstrip("/")
            if bk.startswith("F:"):
                bk = bk[2:]
            abs_dir = self.key_to_path(bk)
            if abs_dir is None:
                # 多根模式下可能选中的是根容器本身（base 恰为根 label），
                # key_to_path 对 rest 为空会拒绝，这里直接映射到该根路径。
                root = next((r for r in self.roots if r.label.casefold() == bk.casefold()), None)
                if root is not None:
                    abs_dir = root.path
            if abs_dir is not None:
                starts.append(_norm(abs_dir))
        else:
            starts = [_norm(r.path) for r in self.roots]

        scripts: list[dict] = []
        seen: set[str] = set()
        for start in starts:
            stack: list[tuple[Path, str | None]] = [(start, None)]  # (目录, 相对输出基准路径 or None)
            while stack:
                cur, rel = stack.pop()
                try:
                    it = _os_retry(lambda: os.scandir(cur), what=f"scandir {cur}")
                except OSError:
                    self._mark_broken(cur)
                    continue
                with it:
                    for entry in it:
                        name = entry.name
                        if name.startswith(HIDDEN_PREFIXES):
                            continue
                        try:
                            is_dir = entry.is_dir()
                        except OSError:
                            continue
                        ep = _norm(Path(entry.path))
                        if is_dir:
                            if self._is_broken(ep) or self._is_reparse(ep):
                                continue
                            nrel = (rel + "/" + name) if rel is not None else name
                            stack.append((ep, nrel))
                            continue
                        if entry.name.lower().endswith(".funscript"):
                            try:
                                key = self.path_to_key(ep)
                            except (OSError, ValueError):
                                continue
                            if key in seen:
                                continue
                            seen.add(key)
                            rel_s = (rel + "/" + name) if rel is not None else name
                            url = f"{self.base_url}/media/{urllib.parse.quote(key, safe='/')}" if self.base_url else f"/media/{urllib.parse.quote(key, safe='/')}"
                            scripts.append({
                                "key": key,
                                "url": url,
                                "name": name,
                                "basename": _norm(ep).stem,
                                "rel": rel_s,
                            })
        return scripts

    def root_container_xml(self, parent_id: str) -> str:
        """根级容器（多根时各根一个容器；单根时直接列根内容）。"""
        if len(self.roots) == 1:
            return self._dir_items(self.roots[0].path, "0", "")
        items = []
        for root in self.roots:
            items.append(
                f'<container id="{html.escape(root.label)}" parentID="0" restricted="1">'
                f"<dc:title>{html.escape(root.label)}</dc:title>"
                f"<upnp:class>object.container.storageFolder</upnp:class>"
                f"</container>"
            )
        return "".join(items)

    def _dir_items(self, dir_path: Path, parent_id: str, prefix_key: str) -> str:
        """列目录：只 scandir，不探测任何视频参数 → 秒开。云盘卷异常自动重试。"""
        items: list[str] = []
        try:
            # 排序只用文件名（is_dir 在受保护循环里探测；sort key 里探测一个坏条目会毁掉整个目录）
            entries = _os_retry(
                lambda: sorted(os.scandir(dir_path), key=lambda e: e.name.casefold()),
                what=f"scandir {dir_path}",
            )
        except (FileNotFoundError, NotADirectoryError) as e:
            # 路径根本不存在 = 配置写错了，不是"云盘卷抖动"。
            # **绝不能塞进坏目录黑名单**：那是给临时故障用的 24h 退避，把配置错误
            # 也记进去的话，用户即使补建了目录也会被继续隐藏一整天。
            # 大声报出来即可——这里只影响脚本枚举，目录浏览见 _dir_items。
            log.error("目录不存在，已跳过：%s（%s）", dir_path, e)
            return ""
        except OSError:
            # 云盘卷暂时不可访问或坏条目：记录黑名单，返回空目录（DeoVR 显示空文件夹而非报错）
            self._mark_broken(dir_path)
            log.warning("目录 %s 无法枚举，已加入黑名单（24h 后重试）", dir_path)
            return ""
        sibling_names = {e.name for e in entries}
        for entry in entries:
            name = entry.name
            if name.startswith(HIDDEN_PREFIXES):
                continue
            try:
                is_dir = entry.is_dir()
            except OSError:
                continue
            entry_path = Path(entry.path)
            if is_dir and self._is_broken(entry_path):
                log.info("跳过坏目录（黑名单）: %s", entry_path)
                continue
            try:
                key = self.path_to_key(entry_path) if prefix_key == "" else f"{prefix_key}/{name}"
            except (OSError, ValueError):
                continue
            if is_dir:
                items.append(
                    f'<container id="{html.escape("F:" + key)}" parentID="{html.escape(parent_id)}" restricted="1">'
                    f"<dc:title>{html.escape(name)}</dc:title>"
                    f"<upnp:class>object.container.storageFolder</upnp:class>"
                    f"</container>"
                )
            elif is_video(name) or is_strm(name):
                items.append(self._video_item(entry_path, key, parent_id, sibling_names))
            elif is_audio(name):
                items.append(self._audio_item(entry_path, key, parent_id))
        return "".join(items)

    def _video_item(self, path: Path, key: str, parent_id: str, sibling_names: set[str] | None = None) -> str:
        title = html.escape(display_title(path))
        url = f"{self.base_url}/media/{html.escape(urllib.parse.quote(key, safe='/'))}"
        if is_strm(path.name):
            # .strm 指针：无 size（对端未知），用通用 MP4 协议信息（DeoVR 靠流嗅探）
            proto = f"http-get:*:video/mp4:DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS={DLNA_FLAGS}"
            return (
                f'<item id="{html.escape("V:" + key)}" parentID="{html.escape(parent_id)}" restricted="1">'
                f"<dc:title>{title}</dc:title>"
                f"<upnp:class>object.item.videoItem</upnp:class>"
                f'<res protocolInfo="{html.escape(proto)}">{url}</res>'
                f"</item>"
            )
        try:
            size = _os_retry(path.stat, what=f"stat {path}").st_size
        except OSError:
            size = 0
        proto = dlna_proto(path)
        size_attr = f' size="{size}"' if size > 0 else ""
        out = (
            f'<item id="{html.escape("V:" + key)}" parentID="{html.escape(parent_id)}" restricted="1">'
            f"<dc:title>{title}</dc:title>"
            f"<upnp:class>object.item.videoItem</upnp:class>"
            f'<res protocolInfo="{html.escape(proto)}"{size_attr}>{url}</res>'
        )
        # 外挂字幕：同目录同名（或 视频名.语言.字幕）；sibling_names 复用本次枚举结果，避免每视频一次 iterdir
        subs = self._find_subtitles(path, sibling_names)
        for sub in subs:
            sub_url = f"{self.base_url}/subs/{html.escape(urllib.parse.quote(sub['key'], safe='/'))}"
            sub_mime = html.escape(sub["mime"])
            lang = html.escape(sub.get("lang") or "")
            lang_attr = f' xml:lang="{lang}"' if lang else ""
            out += f'<res protocolInfo="http-get:*:{sub_mime}:*"{lang_attr}>{sub_url}</res>'
            out += f'<sec:CaptionInfoEx sec:type="{sub["kind"]}">{sub_url}</sec:CaptionInfoEx>'
            out += f'<sec:CaptionInfo sec:type="{sub["kind"]}">{sub_url}</sec:CaptionInfo>'
        out += "</item>"
        return out

    def _audio_item(self, path: Path, key: str, parent_id: str) -> str:
        title = html.escape(display_title(path))
        url = f"{self.base_url}/media/{html.escape(urllib.parse.quote(key, safe='/'))}"
        try:
            size = _os_retry(path.stat, what=f"stat {path}").st_size
        except OSError:
            size = 0
        proto = dlna_proto(path)
        size_attr = f' size="{size}"' if size > 0 else ""
        return (
            f'<item id="{html.escape("V:" + key)}" parentID="{html.escape(parent_id)}" restricted="1">'
            f"<dc:title>{title}</dc:title>"
            f"<upnp:class>object.item.audioItem</upnp:class>"
            f'<res protocolInfo="{html.escape(proto)}"{size_attr}>{url}</res>'
            f"</item>"
        )

    def _find_subtitles(self, video: Path, sibling_names: set[str] | None = None) -> list[dict]:
        """找视频同目录的外挂字幕：同名、或 同名.语言（zh/en…），中文优先。

        传入 sibling_names（父目录已枚举结果）时零额外目录扫描；否则回退为一次 iterdir。
        """
        results: list[dict] = []
        stem = video.stem
        if sibling_names is None:
            try:
                sibling_names = {s.name for s in _os_retry(lambda: list(video.parent.iterdir()), what=f"iterdir {video.parent}")}
            except OSError:
                return results
        for sname in sibling_names:
            if not is_subtitle(sname) or sname.startswith(HIDDEN_PREFIXES):
                continue
            s_stem = Path(sname).stem
            # 以完整 stem 匹配（不再 split('.') 过度截断导致跨视频误匹配）
            if s_stem == stem or s_stem.startswith(stem + "."):
                lang = s_stem[len(stem) + 1:].replace("_", "-") if s_stem.startswith(stem + ".") else ""
                kind = "srt" if Path(sname).suffix.lower() == ".srt" else "ass" if Path(sname).suffix.lower() == ".ass" else "vtt"
                results.append({
                    "key": self.path_to_key(video.parent / sname),
                    "mime": subtitle_mime(Path(sname)),
                    "lang": lang,
                    "kind": kind,
                })
        # 中文优先排序
        def rank(t: dict) -> tuple[int, str]:
            lang = (t.get("lang") or "").lower()
            return (0 if lang in ("zh", "zh-cn", "zh-hans", "chs", "zh-hant", "cht") else 1, lang)
        results.sort(key=rank)
        return results


_MUTEX_HANDLE = None  # 持有单实例互斥体句柄（进程生命周期内保持）


def _acquire_single_instance() -> bool:
    """Windows 命名互斥体：防止多实例并存（否则配置互相覆盖 + 同端口/UUID 双服务）。"""
    global _MUTEX_HANDLE
    try:
        import ctypes
        create_mutex = ctypes.windll.kernel32.CreateMutexW
        create_mutex.restype = ctypes.c_void_p
        create_mutex.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        close_handle = ctypes.windll.kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        _MUTEX_HANDLE = create_mutex(None, False, "Local\\抚物器-Server")
        if not _MUTEX_HANDLE:
            return False
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            close_handle(_MUTEX_HANDLE)
            _MUTEX_HANDLE = None
            return False
        return True
    except Exception:
        return True  # 非 Windows/异常时放行


def ssdp_conflict_warning() -> str:
    """检测 Windows SSDPSRV 是否占用 1900。

    本服务器使用 SO_REUSEPORT 与 SSDPSRV 共存，通常不影响发现。
    仅当 DeoVR 仍扫描不到时才需要停用 SSDPSRV 排查。
    """
    try:
        import subprocess
        r = subprocess.run(
            ["sc", "query", "SSDPSRV"],
            capture_output=True, text=True, timeout=5,
        )
        if "RUNNING" in r.stdout.upper():
            return (
                "提示：Windows 的 SSDP Discovery 服务正在运行（占用 UDP 1900）。\n"
                "本服务器已与其共存，一般不影响 DeoVR 发现。\n"
                "若 DeoVR 仍扫描不到，可尝试以管理员身份运行「修复SSDP冲突.bat」"
                "（services.msc → SSDP Discovery → 停止并禁用）后重启本服务器。"
            )
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# SSDP 发现
# ---------------------------------------------------------------------------
class SSDPServer(threading.Thread):
    def __init__(self, http_port: int, lan_ip: str):
        super().__init__(daemon=True)
        self.http_port = http_port
        self.lan_ip = lan_ip
        self.stop_event = threading.Event()
        self.sock = None
        self.sender = None

    def _location(self) -> str:
        return f"http://{self.lan_ip}:{self.http_port}/description.xml"

    def _server_header(self) -> str:
        return f"Windows/10 UPnP/1.0 {SERVER_NAME_HTTP}/1.0"

    def _response(self, st: str) -> bytes:
        usn = f"uuid:{DEVICE_UUID}" if st == f"uuid:{DEVICE_UUID}" else f"uuid:{DEVICE_UUID}::{st}"
        return (
            "HTTP/1.1 200 OK\r\n"
            f"CACHE-CONTROL: max-age=1800\r\n"
            f"DATE: {time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime())}\r\n"
            f"EXT:\r\n"
            f"LOCATION: {self._location()}\r\n"
            f"SERVER: {self._server_header()}\r\n"
            f"ST: {st}\r\n"
            f"USN: {usn}\r\n"
            "\r\n"
        ).encode("utf-8")

    def _notify(self, nt: str, alive: bool) -> bytes:
        usn = f"uuid:{DEVICE_UUID}" if nt == f"uuid:{DEVICE_UUID}" else f"uuid:{DEVICE_UUID}::{nt}"
        nts = "ssdp:alive" if alive else "ssdp:byebye"
        return (
            f"NOTIFY * HTTP/1.1\r\n"
            f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
            f"CACHE-CONTROL: max-age=1800\r\n"
            f"LOCATION: {self._location()}\r\n"
            f"NT: {nt}\r\n"
            f"NTS: {nts}\r\n"
            f"SERVER: {self._server_header()}\r\n"
            f"USN: {usn}\r\n"
            "\r\n"
        ).encode("utf-8")

    def run(self) -> None:
        # 完全对齐 VR-Video-Toolbox-CE 的 SSDP 实现：
        #  1. 接收 socket：SO_REUSEADDR+SO_REUSEPORT 绑定 0.0.0.0:1900，与 Windows
        #     SSDPSRV 共存；显式 IP_MULTICAST_IF 指定出接口（多网卡时默认会选错）
        #  2. 发送 socket：独立 UDP socket，同样显式 IP_MULTICAST_IF
        #  3. 启动时爆发 3 次 NOTIFY（0.3s 间隔），此后每 60s
        recv = None
        try:
            recv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            recv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                recv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except (AttributeError, OSError):
                pass
            recv.bind(("", SSDP_PORT))
            mreq = struct.pack("=4s4s", socket.inet_aton(SSDP_ADDR), socket.inet_aton(self.lan_ip))
            recv.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            recv.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
            recv.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.lan_ip))
            self.sock = recv
        except OSError as e:
            log.warning("SSDP 接收 socket 初始化失败: %s", e)
            self.sock = None

        # 独立发送 socket（对齐原项目）
        sender = None
        try:
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
            sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.lan_ip))
        except OSError as e:
            log.warning("SSDP 发送 socket 初始化失败: %s", e)
        self.sender = sender

        # stop() 可能在 run() 完成初始化前被调用：立即清理并退出
        if self.stop_event.is_set():
            try:
                if self.sock:
                    self.sock.close()
                    self.sock = None
                if self.sender:
                    self.sender.close()
                    self.sender = None
            except OSError:
                pass
            return

        def broadcast(alive: bool):
            if sender is None:
                return
            for nt in SSDP_TARGETS:
                try:
                    sender.sendto(self._notify(nt, alive), (SSDP_ADDR, SSDP_PORT))
                except OSError:
                    pass

        def reply(st: str, addr):
            try:
                if recv:
                    recv.sendto(self._response(st), addr)
            except OSError:
                pass

        # 启动爆发 3 次 NOTIFY（提高 DeoVR 发现率）
        for _ in range(3):
            broadcast(True)
            time.sleep(0.3)

        last_notify = time.time()
        while not self.stop_event.is_set():
            if recv is None:
                time.sleep(2)
                continue
            # 每 60s 广播 NOTIFY alive
            now = time.time()
            if now - last_notify >= 60:
                last_notify = now
                broadcast(True)
            # 响应 M-SEARCH（recv 带超时，保证 stop() 可确定性退出）
            try:
                recv.settimeout(0.5)
                data, addr = recv.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                continue
            try:
                text = data.decode("utf-8", errors="ignore")
                if text.startswith("M-SEARCH"):
                    st_match = re.search(r"(?im)^ST:\s*(.+?)\r?$", text)
                    mx_match = re.search(r"(?im)^MX:\s*(\d+)", text)
                    st = st_match.group(1).strip() if st_match else ""
                    mx = float(mx_match.group(1)) if mx_match and mx_match.group(1).isdigit() else 1.0
                    replies = []
                    if st == "ssdp:all":
                        replies = SSDP_TARGETS
                    elif st in SSDP_TARGETS:
                        replies = [st]
                    # 其他 uuid: 前缀（搜索别的设备）一律不响应（SSDP 1.0）
                    if replies:
                        # 随机延迟（对齐原项目），避免响应风暴
                        delay = random.uniform(0.0, max(0.0, min(mx, 2.0)))
                        if delay > 0 and self.stop_event.wait(delay):
                            continue
                        for r in replies:
                            reply(r, addr)
            except Exception:
                continue

    def stop(self) -> None:
        self.stop_event.set()
        try:
            if self.sock:
                # 先广播 byebye，再关闭 socket（客户端据此立即移除设备，不必等 30 分钟缓存过期）
                self.sock.settimeout(0.3)
                sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
                try:
                    sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.lan_ip))
                    for nt in SSDP_TARGETS:
                        try:
                            sender.sendto(self._notify(nt, alive=False), (SSDP_ADDR, SSDP_PORT))
                        except OSError:
                            pass
                finally:
                    sender.close()
                self.sock.close()
        except OSError:
            pass
        try:
            if self.sender:
                self.sender.close()
                self.sender = None
        except OSError:
            pass


# ---------------------------------------------------------------------------
# HTTP 服务（UPnP + 媒体流）
# ---------------------------------------------------------------------------
class DlnaHandler(BaseHTTPRequestHandler):
    server_version = f"{SERVER_NAME_HTTP}/1.0"
    protocol_version = "HTTP/1.1"
    timeout = 60  # 空闲 keep-alive 连接超时回收，防止线程缓慢泄漏

    @property
    def app(self) -> "DlnaApp":
        return self.server.app  # type: ignore[attr-defined]

    def _write_access_log(self, line: str) -> None:
        """访问日志：加锁防交错；超 5MB 轮转。"""
        try:
            with _log_lock:
                if os.path.exists(ACCESS_LOG) and os.path.getsize(ACCESS_LOG) > ACCESS_LOG_MAX:
                    try:
                        os.replace(ACCESS_LOG, ACCESS_LOG + ".old")
                    except OSError:
                        pass
                with open(ACCESS_LOG, "a", encoding="utf-8") as f:
                    f.write(line)
        except OSError:
            pass

    def log_message(self, fmt, *args):  # 访问日志：写入文件便于排查 DeoVR 请求
        self._write_access_log(f"[{time.strftime('%m-%d %H:%M:%S')}] {fmt % args}\n")

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None, want_body: bool = True) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Language", "zh-CN")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self._send_connection_headers()
        self.end_headers()
        if want_body:
            self.wfile.write(body)

    def _send_connection_headers(self) -> None:
        """HTTP/1.0 keep-alive 需显式 Connection 头；决定关闭时回 Connection: close。"""
        if self.close_connection:
            self.send_header("Connection", "close")
        elif self.request_version == "HTTP/1.0":
            self.send_header("Connection", "keep-alive")

    def _send_error_text(self, code: int, msg: str, want_body: bool = True) -> None:
        self._send(code, msg.encode("utf-8"), "text/plain; charset=utf-8", want_body=want_body)

    # ---- GET ----
    def do_GET(self) -> None:
        self._route(True)

    def do_HEAD(self) -> None:
        # DeoVR 播放前会用 HEAD 探测媒体（Content-Length/Type），必须支持
        self._route(False)

    def do_OPTIONS(self) -> None:
        # 部分 DLNA 控制点会先 OPTIONS 探测；回 200 + Allow 避免被误判不可用
        self.send_response(200)
        self.send_header("Allow", "GET, HEAD, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self._send_connection_headers()
        self.end_headers()

    def _route(self, want_body: bool) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path == "/description.xml":
                self._send(200, self.app.device_description().encode("utf-8"), 'text/xml; charset="utf-8"', want_body=want_body)
            elif path == "/cds.xml":
                self._send(200, self.app.cds_scpd().encode("utf-8"), 'text/xml; charset="utf-8"', want_body=want_body)
            elif path == "/cm.xml":
                self._send(200, self.app.cm_scpd().encode("utf-8"), 'text/xml; charset="utf-8"', want_body=want_body)
            elif path.startswith("/media/"):
                self._stream_file(parsed.path, parsed.query, want_body)
            elif path.startswith("/subs/"):
                self._stream_subtitle(parsed.path, want_body)
            elif path == "/scripts":
                self._handle_scripts(parsed.query, want_body)
            else:
                self._send_error_text(404, "not found")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log.warning("%s %s error: %s", "GET" if want_body else "HEAD", path, e)
            try:
                self._send_error_text(500, f"server error: {e}", want_body=want_body)
            except Exception:
                pass

    def _stream_file(self, path: str, query: str, want_body: bool) -> None:
        key = path[len("/media/"):]
        file_path = self.app.library.key_to_path(key)
        try:
            is_file = _os_retry(file_path.is_file, what=f"is_file {file_path}")
        except OSError:
            self._send_error_text(404, "not found")
            return
        if not is_file:
            self._send_error_text(404, "not found")
            return
        if is_strm(file_path.name):
            self._proxy_strm(file_path, self.headers.get("Range"), want_body)
            return
        try:
            total = _os_retry(file_path.stat, what=f"stat {file_path}").st_size
        except OSError as e:
            self._send_error_text(500, f"stat failed: {e}")
            return
        # Range 仅对 GET 生效（RFC 7233）；HEAD 一律 200 头部
        rng = parse_range_header(self.headers.get("Range"), total) if want_body else None
        if rng == "invalid":
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{total}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        start, end = rng if rng else (0, total - 1)
        length = end - start + 1
        ctype = mime_for(file_path)
        status = 206 if rng else 200
        log.info("media GET %s Range=%s -> %s (%d B, %d/%d)", path, self.headers.get("Range") or "-", status, length, start, total)
        # 先打开并 seek 成功，再发响应头（避免"声明 Content-Length 却 0 字节"的悬空响应）
        f = None
        try:
            if want_body:
                try:
                    f = _os_retry(lambda: open(file_path, "rb"), what=f"open {file_path}")
                    if start:
                        f.seek(start)
                except OSError as e:
                    self._send_error_text(404, f"open failed: {e}")
                    return
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("transferMode.dlna.org", "Streaming")
            self.send_header("contentFeatures.dlna.org", f"DLNA.ORG_PN={dlna_pn_for(ctype)};DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS={DLNA_FLAGS}")
            if rng:
                self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
            self.send_header("Content-Disposition", "inline")
            self.end_headers()
            if not want_body:
                return
            try:
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
                if remaining > 0:
                    # 提前 EOF（云盘短读/文件缩小）：发送字节少于 Content-Length，
                    # 必须关闭连接让客户端感知截断，而不是带着残缺 body 保持 keep-alive
                    self.close_connection = True
                    log.warning("stream %s 提前 EOF: 声明 %d B 实发 %d B", file_path, length, length - remaining)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
            except OSError as e:
                self.close_connection = True
                log.warning("stream %s failed: %s", file_path, e)
        finally:
            try:
                if f:
                    f.close()
            except OSError:
                pass

    def _handle_scripts(self, query: str, want_body: bool) -> None:
        """GET /scripts?base=<key>：枚举 .funscript 索引（"远程脚本源"用）。

        普通 DLNA Browse 不暴露脚本文件，客户端据此一次性建立 basename→URL
        映射并本地缓存，播放时 O(1) 命中 + 单文件下载，不做逐播放目录漫游。
        """
        qs = urllib.parse.parse_qs(query)
        base = (qs.get("base") or [""])[0]
        try:
            scripts = self.app.library.collect_scripts(base)
        except Exception as e:
            log.warning("scripts enumerate error: %s", e)
            self._send(500, b'{"error":"enumerate failed"}\n', "application/json", want_body=want_body)
            return
        payload = {
            "base": base,
            "count": len(scripts),
            "scripts": scripts,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(200, body, "application/json", want_body=want_body)

    def _proxy_strm(self, file_path: Path, range_header: str | None, want_body: bool) -> None:
        """.strm 指针文件：读取内部 URL，原样代理转发（Range 透传，DeoVR 可拖动）。

        SSRF 防护：只允许公网目标（拒绝 loopback/私网/link-local），避免服务器变成
        内网跳板；.strm 来自云盘共享目录，可能被第三方内容触发。
        """
        try:
            raw = _os_retry(
                lambda: file_path.read_text(encoding="utf-8", errors="ignore"),
                what=f"read strm {file_path}",
            )
        except OSError as e:
            self._send_error_text(500, f"strm read failed: {e}")
            return
        target = next(
            (ln.strip() for ln in raw.splitlines()
             if ln.strip().startswith(("http://", "https://"))),
            "",
        )
        if not target:
            self._send_error_text(500, "strm 内无有效 http(s) URL")
            return
        u = urllib.parse.urlsplit(target)
        host = u.hostname
        if not host:
            self._send_error_text(500, "strm URL 无效")
            return
        if not _is_public_http_target(host):
            log.warning("strm 目标被拒绝（非公网地址）: %s", host)
            self._send_error_text(403, "strm 目标仅允许公网地址")
            return
        conn = None
        headers_sent = False
        try:
            if u.scheme == "https":
                conn = http.client.HTTPSConnection(host, u.port or 443, timeout=30)
            else:
                conn = http.client.HTTPConnection(host, u.port or 80, timeout=30)
            req_path = u.path or "/"
            if u.query:
                req_path += "?" + u.query
            headers = {"User-Agent": "VR-DLNA/1.0", "Accept": "*/*"}
            if range_header:
                headers["Range"] = range_header
            method = "GET" if want_body else "HEAD"
            conn.request(method, req_path, headers=headers)
            resp = conn.getresponse()
            # HEAD 被上游拒绝时回退 GET（只消费头部），保证 DeoVR 探测可用
            if not want_body and resp.status in (405, 501):
                resp.read()
                conn.close()
                conn = http.client.HTTPSConnection(host, u.port or 443, timeout=30) if u.scheme == "https" else http.client.HTTPConnection(host, u.port or 80, timeout=30)
                conn.request("GET", req_path, headers=headers)
                resp = conn.getresponse()
                resp.read(65536)  # 只探测可用性，绝不能把整个视频读进内存
                self._send(200, b"", "video/mp4", want_body=False)
                return
            self.send_response(resp.status)
            for h in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "Content-Disposition",
                      "Location", "Content-Encoding"):
                v = resp.getheader(h)
                if v:
                    self.send_header(h, v)
            if resp.length is None and want_body:
                # 上游无 Content-Length（chunked/close-delimited）：close-delimited 转发，
                # 否则 HTTP/1.1 keep-alive 下客户端读不到 body 结束信号
                self.send_header("Connection", "close")
                self.close_connection = True
            if resp.getheader("Content-Type") is None:
                self.send_header("Content-Type", "video/mp4")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            headers_sent = True
            if want_body and resp.status not in (204, 304):
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except OSError as e:
            log.warning("strm proxy %s failed: %s", target[:80], e)
            if not headers_sent:
                try:
                    self._send_error_text(502, f"strm 代理失败: {e}")
                except Exception:
                    pass
            else:
                # 头部已发出后失败：绝不能二次发响应污染媒体流
                self.close_connection = True
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

    def _stream_subtitle(self, path: str, want_body: bool) -> None:
        key = path[len("/subs/"):]
        sub_path = self.app.library.key_to_path(key)
        try:
            is_file = sub_path is not None and _os_retry(sub_path.is_file, what=f"is_file {sub_path}")
        except OSError:
            is_file = False
        if not is_file:
            self._send_error_text(404, "not found")
            return
        if not want_body:
            # HEAD：只 stat 返回大小/类型，不读取内容（云盘上读整个字幕文件代价高）
            try:
                size = _os_retry(sub_path.stat, what=f"stat {sub_path}").st_size
            except OSError as e:
                self._send_error_text(500, f"stat failed: {e}")
                return
            self.send_response(200)
            self.send_header("Content-Type", subtitle_mime(sub_path))
            self.send_header("Content-Length", str(size))
            self.end_headers()
            return
        try:
            raw = _os_retry(sub_path.read_bytes, what=f"read {sub_path}")
        except OSError as e:
            self._send_error_text(500, f"read failed: {e}")
            return
        # 尝试按 UTF-8 输出；若含非 UTF-8 字节则转码（GBK 常见）
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = raw.decode("gbk")
            except UnicodeDecodeError:
                text = raw.decode("latin-1")
        body = text.encode("utf-8")
        self._send(200, body, subtitle_mime(sub_path))

    # ---- SOAP ----
    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length > 0 else b""
            if path in ("/control/cds", "/control/cm"):
                # 调试：记录 SOAP 请求体（截断），便于对照 DeoVR 实际请求
                try:
                    txt = body.decode("utf-8", errors="replace")
                    self._write_access_log(
                        f"[{time.strftime('%m-%d %H:%M:%S')}] SOAP {path} SOAPACTION={self.headers.get('SOAPACTION') or '-'}\n"
                        f"  BODY: {txt[:400]}\n"
                    )
                except OSError:
                    pass
            if path == "/control/cds":
                self._handle_cds(body)
            elif path == "/control/cm":
                self._handle_cm(body)
            else:
                self._send_error_text(404, "not found")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log.warning("POST %s error: %s", path, e)
            try:
                self._send_error_text(500, f"server error: {e}")
            except Exception:
                pass

    def _soap_action(self, body: bytes) -> str:
        m = re.search(rb'<u:(\w+)\s+xmlns:u="[^"]*ContentDirectory[^"]*"', body)
        if m:
            return m.group(1).decode()
        m = re.search(rb'<u:(\w+)\s+xmlns:u="[^"]*ConnectionManager[^"]*"', body)
        if m:
            return m.group(1).decode()
        m = re.search(rb'<(\w+)\s+xmlns:u=', body)
        return m.group(1).decode() if m else ""

    def _arg(self, body: bytes, name: str) -> str:
        m = re.search(rb"<%s>([^<]*)</%s>" % (name.encode(), name.encode()), body)
        return m.group(1).decode("utf-8", errors="ignore") if m else ""

    def _soap_response(self, action: str, service: str, inner: str) -> None:
        body = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            f's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            f'<s:Body><u:{action}Response xmlns:u="urn:schemas-upnp-org:service:{service}:1">'
            f"{inner}"
            f"</u:{action}Response></s:Body></s:Envelope>"
        ).encode("utf-8")
        self._send(200, body, 'text/xml; charset="utf-8"',
                   {"EXT": ""})

    def _soap_fault(self, error_code: int, description: str) -> None:
        """UPnP 标准 SOAP Fault（未实现/无效 action 等）。"""
        body = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            '<s:Body><s:Fault><faultcode>s:Client</faultcode>'
            '<faultstring>UPnPError</faultstring>'
            '<detail><UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
            f"<errorCode>{error_code}</errorCode>"
            f"<errorDescription>{html.escape(description)}</errorDescription>"
            "</UPnPError></detail></s:Fault></s:Body></s:Envelope>"
        ).encode("utf-8")
        self._send(500, body, 'text/xml; charset="utf-8"')

    def _handle_cds(self, body: bytes) -> None:
        action = self._soap_action(body)
        if action == "Browse":
            obj_id = self._arg(body, "ObjectID")
            flag = self._arg(body, "BrowseFlag")
            try:
                req_count = int(self._arg(body, "RequestedCount") or "0")
                start = int(self._arg(body, "StartingIndex") or "0")
            except ValueError:
                req_count, start = 0, 0
            if start < 0:
                start = 0
            if req_count < 0:
                req_count = 0
            result, returned, total = self.app.browse(obj_id, flag, start, req_count)
            if flag == "BrowseMetadata":
                returned = 1 if result else 0
                total = returned
            result_esc = html.escape(result)
            inner = (
                f"<Result>{result_esc}</Result>"
                f"<NumberReturned>{returned}</NumberReturned>"
                f"<TotalMatches>{total}</TotalMatches>"
                f"<UpdateID>0</UpdateID>"
            )
            self._soap_response("Browse", "ContentDirectory", inner)
        elif action in ("GetSortCapabilities", "GetSearchCapabilities"):
            self._soap_response(action, "ContentDirectory", "<SortCaps></SortCaps>" if action == "GetSortCapabilities" else "<SearchCaps></SearchCaps>")
        elif action == "GetSystemUpdateID":
            self._soap_response(action, "ContentDirectory", "<Id>0</Id>")
        else:
            # UPnP 规范：未知 action 应回 SOAP Fault（401 Invalid Action），而非纯文本 501
            self._soap_fault(401, "Invalid Action")

    def _handle_cm(self, body: bytes) -> None:
        action = self._soap_action(body)
        if action == "GetProtocolInfo":
            # 与实际可提供的 MIME 集合保持一致（video/mp2t 等已由 dlna_proto 宣传）
            inner = (
                "<Source>http-get:*:video/mp4:DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000,"
                "http-get:*:video/x-matroska:DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000,"
                "http-get:*:video/mp2t:DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000,"
                "http-get:*:video/mpeg:DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000,"
                "http-get:*:video/quicktime:DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000,"
                "http-get:*:video/webm:DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000,"
                "http-get:*:video/x-msvideo:DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000,"
                "http-get:*:video/x-ms-wmv:DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000,"
                "http-get:*:video/x-flv:DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000,"
                "http-get:*:audio/mpeg:*,http-get:*:audio/mp4:*,http-get:*:audio/aac:*,http-get:*:audio/wav:*,"
                "http-get:*:audio/flac:*,http-get:*:audio/ogg:*,http-get:*:audio/opus:*,http-get:*:audio/x-ms-wma:*,"
                "http-get:*:audio/amr:*,http-get:*:audio/aiff:*,http-get:*:audio/x-ape:*,"
                "http-get:*:application/x-subrip:*,http-get:*:application/x-ass:*,http-get:*:text/vtt:*</Source>"
                "<Sink></Sink>"
            )
            self._soap_response("GetProtocolInfo", "ConnectionManager", inner)
        else:
            self._soap_fault(401, "Invalid Action")


class DlnaApp:
    def __init__(self, roots: list[MediaRoot], http_port: int):
        # 根 label 强制唯一（盘符根等 basename 可能为空或重名），大小写不敏感
        seen: dict[str, int] = {}
        for r in roots:
            base = r.label or "Videos"
            lbl, i = base, 2
            while lbl.casefold() in seen:
                lbl = f"{base}-{i}"
                i += 1
            seen[lbl.casefold()] = 1
            r.label = lbl
        self.library = MediaLibrary(roots)
        self.http_port = http_port
        self.library.base_url = self._base_url()

    def _base_url(self) -> str:
        """绝对 res URL 前缀（DLNA 规范要求 res 为绝对地址，DeoVR 不认相对路径）。"""
        ip = lan_ips()[0] if lan_ips() else "127.0.0.1"
        return f"http://{ip}:{self.http_port}"

    # ---- UPnP 描述 ----
    def device_description(self) -> str:
        ip = lan_ips()[0] if lan_ips() else "127.0.0.1"
        base = f"http://{ip}:{self.http_port}"
        return f"""<?xml version="1.0" encoding="utf-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0" xmlns:dlna="urn:schemas-dlna-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <dlna:X_DLNADOC>DMS-1.50</dlna:X_DLNADOC>
    <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
    <friendlyName>抚物器</friendlyName>
    <manufacturer>抚物器</manufacturer>
    <manufacturerURL>http://localhost</manufacturerURL>
    <modelDescription>Lightweight DLNA MediaServer for cloud-mounted VR video folders</modelDescription>
    <modelName>抚物器</modelName>
    <modelNumber>1.0</modelNumber>
    <UDN>uuid:{DEVICE_UUID}</UDN>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:ContentDirectory:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:ContentDirectory</serviceId>
        <SCPDURL>/cds.xml</SCPDURL>
        <controlURL>/control/cds</controlURL>
      </service>
      <service>
        <serviceType>urn:schemas-upnp-org:service:ConnectionManager:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:ConnectionManager</serviceId>
        <SCPDURL>/cm.xml</SCPDURL>
        <controlURL>/control/cm</controlURL>
      </service>
    </serviceList>
  </device>
</root>
"""

    def cds_scpd(self) -> str:
        return """<?xml version="1.0" encoding="utf-8"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <actionList>
    <action><name>GetSearchCapabilities</name><argumentList><argument><name>SearchCaps</name><direction>out</direction><relatedStateVariable>SearchCapabilities</relatedStateVariable></argument></argumentList></action>
    <action><name>GetSortCapabilities</name><argumentList><argument><name>SortCaps</name><direction>out</direction><relatedStateVariable>SortCapabilities</relatedStateVariable></argument></argumentList></action>
    <action><name>GetSystemUpdateID</name><argumentList><argument><name>Id</name><direction>out</direction><relatedStateVariable>SystemUpdateID</relatedStateVariable></argument></argumentList></action>
    <action><name>Browse</name><argumentList>
      <argument><name>ObjectID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_ObjectID</relatedStateVariable></argument>
      <argument><name>BrowseFlag</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_BrowseFlag</relatedStateVariable></argument>
      <argument><name>Filter</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Filter</relatedStateVariable></argument>
      <argument><name>StartingIndex</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Index</relatedStateVariable></argument>
      <argument><name>RequestedCount</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
      <argument><name>SortCriteria</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_SortCriteria</relatedStateVariable></argument>
      <argument><name>Result</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Result</relatedStateVariable></argument>
      <argument><name>NumberReturned</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
      <argument><name>TotalMatches</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
      <argument><name>UpdateID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_UpdateID</relatedStateVariable></argument>
    </argumentList></action>
  </actionList>
  <serviceStateTable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_ObjectID</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_BrowseFlag</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Filter</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Index</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Count</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_SortCriteria</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Result</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_UpdateID</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>SearchCapabilities</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>SortCapabilities</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="yes"><name>SystemUpdateID</name><dataType>ui4</dataType></stateVariable>
  </serviceStateTable>
</scpd>
"""

    def cm_scpd(self) -> str:
        return """<?xml version="1.0" encoding="utf-8"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <actionList>
    <action><name>GetProtocolInfo</name><argumentList>
      <argument><name>Source</name><direction>out</direction><relatedStateVariable>SourceProtocolInfo</relatedStateVariable></argument>
      <argument><name>Sink</name><direction>out</direction><relatedStateVariable>SinkProtocolInfo</relatedStateVariable></argument>
    </argumentList></action>
  </actionList>
  <serviceStateTable>
    <stateVariable sendEvents="no"><name>SourceProtocolInfo</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>SinkProtocolInfo</name><dataType>string</dataType></stateVariable>
  </serviceStateTable>
</scpd>
"""

    # ---- 浏览 ----
    DIDL_HEAD = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        'xmlns:sec="http://www.sec.co.kr/" '
        'xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">'
    )

    def browse(self, obj_id: str, flag: str, start: int = 0, count: int = 0) -> tuple[str, int, int]:
        """返回 (DIDL-Lite 内容, NumberReturned, TotalMatches)。

        start/count 实现 CDS:1 分页（count=0 表示全部，DeoVR 即用 0）。
        任何未知 ObjectID / 云盘卷异常都返回"格式良好的空 DIDL"（绝不为空串），
        DeoVR 的 contentProc 对空 Result 会 LoadXml(null) 直接崩溃。
        """
        key = obj_id[2:] if obj_id.startswith(("F:", "V:")) else obj_id
        parts: list[str] = []
        if flag == "BrowseMetadata" and (obj_id.startswith("F:") or (obj_id != "0" and not obj_id.startswith("V:"))):
            # 容器自身的元数据：必须返回**该容器自己**，而不是它的子项列表。
            #
            # 这是 UPnP CDS 的规范语义（BrowseMetadata = 请求对象自身的属性），
            # 也是客户端"返回上一级"的唯一信息来源。旧实现把容器的 BrowseMetadata
            # 落进下面的浏览分支，返回的是**子项列表**；而 `_dir_item_list` 给每个
            # 子项写的 parentID 正是**当前容器自己**（它描述的是"子项的父亲"）。
            # 客户端从列表里取 parentID，拿到的就是"容器自己 =
            # 容器自己的父亲"，过滤掉等于自身的值之后只剩"没有上级"，
            # 于是 DeoVR/Unity 侧按"返回上一级"会直接跳到根目录。
            meta_path = self.library.key_to_path(key)
            if meta_path is None:
                root = next((r for r in self.library.roots if r.label.casefold() == key.casefold()), None)
                if root is not None:
                    # 多根模式下根容器的 ObjectID 就是 root.label，其上级即 CDS 根
                    parts = [
                        f'<container id="{html.escape(root.label)}" parentID="0" restricted="1">'
                        f"<dc:title>{html.escape(root.label)}</dc:title>"
                        f"<upnp:class>object.container.storageFolder</upnp:class>"
                        f"</container>"
                    ]
            else:
                # 父 key = 去掉最后一段；顶层目录与单根模式的根目录都归 CDS 根 "0"。
                parent_id = parent_object_id(key)
                title = meta_path.name
                parts = [
                    f'<container id="{html.escape(obj_id)}" parentID="{html.escape(parent_id)}" restricted="1">'
                    f"<dc:title>{html.escape(title)}</dc:title>"
                    f"<upnp:class>object.container.storageFolder</upnp:class>"
                    f"</container>"
                ]
        elif key == "" or obj_id == "0":
            parts = self._dir_item_list(self.library.roots[0].path, "0", "") if len(self.library.roots) == 1 else [
                f'<container id="{html.escape(r.label)}" parentID="0" restricted="1">'
                f"<dc:title>{html.escape(r.label)}</dc:title>"
                f"<upnp:class>object.container.storageFolder</upnp:class>"
                f"</container>" for r in self.library.roots
            ]
        elif obj_id.startswith("F:"):
            p = self.library.key_to_path(key)
            if p is not None:
                parts = self._dir_item_list(p, obj_id, key)
        elif obj_id.startswith("V:"):
            # 视频元数据（无参数探测，仅基本字段）
            p = self.library.key_to_path(key)
            try:
                is_file = p is not None and _os_retry(p.is_file, what=f"is_file {p}")
            except OSError:
                is_file = False
            if is_file and flag == "BrowseMetadata":
                # 上级是**所在目录**，不是文件自己。旧实现传 obj_id，产出的
                # parentID 等于自身（"自己是自己的父亲"）——客户端读 parentID 想
                # 定位文件所在目录时会拿到一个无效值，进而退化成"没有上级"。
                parent_id = parent_object_id(key)
                item = (self.library._audio_item(p, key, parent_id) if is_audio(p.name)
                        else self.library._video_item(p, key, parent_id))
                parts = [item] if item else []
        else:
            # 多根模式下，根容器的 ObjectID 就是 root.label（DeoVR 会 Browse 根容器）
            root = next((r for r in self.library.roots if r.label.casefold() == key.casefold()), None)
            if root is not None:
                parts = self._dir_item_list(root.path, obj_id, key)
        total = len(parts)
        if start >= total:
            parts = []
        elif start > 0:
            parts = parts[start:]
        if count > 0:
            parts = parts[:count]
        inner = "".join(parts)
        return f"{self.DIDL_HEAD}{inner}</DIDL-Lite>", len(parts), total

    def _dir_item_list(self, dir_path: Path, parent_id: str, prefix_key: str) -> list[str]:
        """列目录（返回条目 XML 列表，供 browse 分页与 TotalMatches 复用，避免二次扫描）。"""
        try:
            entries = _os_retry(
                lambda: sorted(os.scandir(dir_path), key=lambda e: e.name.casefold()),
                what=f"scandir {dir_path}",
            )
        except OSError:
            self.library._mark_broken(dir_path)
            log.warning("目录 %s 无法枚举，已加入黑名单（24h 后重试）", dir_path)
            return []
        sibling_names = {e.name for e in entries}
        # 本层条目的上级就是本层容器；`prefix_key` 为空时是单根模式的根目录，归 "0"。
        # 旧实现把传进来的 `parent_id`（= 正在浏览的容器自己的 ObjectID）直接转发给
        # `_video_item/_audio_item`，产出的文件条目 parentID 等于**文件自身**，
        # 与"容器的 BrowseMetadata 返回子项列表"是同一个根因的两处表现。
        entry_parent_id = ("F:" + prefix_key) if prefix_key else "0"
        parts: list[str] = []
        for entry in entries:
            name = entry.name
            if name.startswith(HIDDEN_PREFIXES):
                continue
            try:
                is_dir = entry.is_dir()
            except OSError:
                continue
            entry_path = Path(entry.path)
            if is_dir and self.library._is_broken(entry_path):
                log.info("跳过坏目录（黑名单）: %s", entry_path)
                continue
            try:
                key = self.library.path_to_key(entry_path) if prefix_key == "" else f"{prefix_key}/{name}"
            except (OSError, ValueError):
                continue
            if is_dir:
                parts.append(
                    f'<container id="{html.escape("F:" + key)}" parentID="{html.escape(parent_id)}" restricted="1">'
                    f"<dc:title>{html.escape(name)}</dc:title>"
                    f"<upnp:class>object.container.storageFolder</upnp:class>"
                    f"</container>"
                )
            elif is_video(name) or is_strm(name):
                parts.append(self.library._video_item(entry_path, key, entry_parent_id, sibling_names))
            elif is_audio(name):
                parts.append(self.library._audio_item(entry_path, key, entry_parent_id))
        return parts

    def child_count(self, obj_id: str) -> int:
        key = obj_id[2:] if obj_id.startswith(("F:", "V:")) else obj_id
        if key == "" or obj_id == "0":
            if len(self.library.roots) > 1:
                return len(self.library.roots)
            return self._count_dir(self.library.roots[0].path)
        p = self.library.key_to_path(key)
        if p is None:
            # 多根: ObjectID 可能是根容器 label
            root = next((r for r in self.library.roots if r.label.casefold() == key.casefold()), None)
            return self._count_dir(root.path) if root is not None else 0
        try:
            is_dir = _os_retry(p.is_dir, what=f"is_dir {p}")
        except OSError:
            return 0
        return self._count_dir(p) if is_dir else 1

    @staticmethod
    def _count_dir(p: Path) -> int:
        n = 0
        try:
            entries = _os_retry(lambda: list(os.scandir(p)), what=f"scandir {p}")
        except OSError:
            return n
        for entry in entries:
            if entry.name.startswith(HIDDEN_PREFIXES):
                continue
            try:
                if entry.is_dir() or is_video(entry.name) or is_audio(entry.name) or is_strm(entry.name):
                    n += 1
            except OSError:
                pass
        return n


class DlnaHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, app: DlnaApp):
        self.app = app
        super().__init__(addr, DlnaHandler)


# ---------------------------------------------------------------------------
# 简易 GUI（tkinter）
# ---------------------------------------------------------------------------
def run_gui() -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title("抚物器")
    # 默认打开即为手动拖拽可调到的最小尺寸
    root.geometry("680x320")
    root.minsize(680, 320)
    _window_icons: list = []

    def apply_window_icon() -> None:
        try:
            icon_file = _app_icon_path()
            if icon_file.exists():
                root.iconbitmap(default=str(icon_file))
        except Exception:
            pass
        # 额外通过 WM_SETICON 强制设置任务栏/标题栏图标（Tk 的 iconbitmap 偶尔在打包环境失效）
        try:
            import ctypes
            from ctypes import wintypes
            hwnd = root.winfo_id()
            if not hwnd:
                return
            user32 = ctypes.windll.user32
            user32.LoadImageW.restype = ctypes.c_void_p
            user32.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
            user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            user32.SendMessageW.restype = wintypes.LPARAM
            icon_file = _app_icon_path()
            if not icon_file.exists():
                return
            hicon_big = user32.LoadImageW(None, str(icon_file), 1, 48, 48, 0x10)   # IMAGE_ICON, LR_LOADFROMFILE
            hicon_small = user32.LoadImageW(None, str(icon_file), 1, 16, 16, 0x10)
            if hicon_big:
                user32.SendMessageW(hwnd, 0x0080, 1, hicon_big)  # WM_SETICON, ICON_BIG
            if hicon_small:
                user32.SendMessageW(hwnd, 0x0080, 0, hicon_small)  # WM_SETICON, ICON_SMALL
            _window_icons.append((hicon_big, hicon_small))
        except Exception:
            pass

    apply_window_icon()
    root.after(100, apply_window_icon)

    state = {"roots": [], "server": None, "ssdp": None, "port": 1919, "starting": False}
    server_events: queue.Queue = queue.Queue()

    # ---- 切换页面：DLNA 部署 / Funscript 同步 ----
    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True, padx=4, pady=4)
    tab_dlna = ttk.Frame(nb, padding=4)
    tab_sync = ttk.Frame(nb, padding=4)
    nb.add(tab_dlna, text="DLNA服务器部署")
    nb.add(tab_sync, text="脚本文件夹同步")
    tab_video = ttk.Frame(nb, padding=4)
    nb.add(tab_video, text="视频文件夹同步")
    tab_settings = ttk.Frame(nb, padding=4)
    nb.add(tab_settings, text="设置")

    # ---- DLNA 页：顶部根目录 ----
    frame_roots = ttk.LabelFrame(tab_dlna, text=" 媒体根目录（视频/音频，支持多个，浏览零探测·秒开） ", padding=6)
    frame_roots.pack(fill="x", padx=8, pady=(8, 4))

    list_roots = tk.Listbox(frame_roots, height=5)
    list_roots.pack(side="left", fill="both", expand=True)

    def refresh_roots_list() -> None:
        list_roots.delete(0, tk.END)
        for r in state["roots"]:
            list_roots.insert(tk.END, r)

    def add_root() -> None:
        d = filedialog.askdirectory(title="选择视频文件夹（网盘挂载盘符/路径）")
        if d and d not in state["roots"]:
            state["roots"].append(d)
            refresh_roots_list()

    def remove_root() -> None:
        sel = list_roots.curselection()
        if sel:
            state["roots"].pop(sel[0])
            refresh_roots_list()

    btn_col = ttk.Frame(frame_roots)
    btn_col.pack(side="right", fill="y", padx=(6, 0))
    ttk.Button(btn_col, text="添加…", command=add_root).pack(fill="x", pady=1)
    ttk.Button(btn_col, text="移除", command=remove_root).pack(fill="x", pady=1)

    # ---- 端口 ----
    frame_port = ttk.Frame(tab_dlna)
    frame_port.pack(fill="x", padx=8, pady=2)
    ttk.Label(frame_port, text="HTTP 端口：").pack(side="left")
    var_port = tk.StringVar(value=str(state["port"]))
    ent_port = ttk.Entry(frame_port, textvariable=var_port, width=8)
    ent_port.pack(side="left", padx=4)
    ttk.Label(frame_port, text="（DeoVR 用 SSDP 自动发现，或手动填 http://IP:端口）").pack(side="left", padx=8)

    # ---- 控制 ----
    frame_ctl = ttk.Frame(tab_dlna)
    frame_ctl.pack(fill="x", padx=8, pady=4)
    lbl_state = ttk.Label(frame_ctl, text="未启动", foreground="#888")
    lbl_state.pack(side="left")

    def start_server() -> None:
        if state["server"]:
            messagebox.showinfo("提示", "已在运行")
            return
        if state["starting"]:
            return
        if not state["roots"]:
            messagebox.showwarning("提示", "请先添加至少一个视频根目录")
            return
        try:
            port = int(var_port.get())
        except ValueError:
            messagebox.showerror("错误", "端口必须是数字")
            return
        if not 0 < port <= 65535:
            messagebox.showerror("错误", "端口必须在 1~65535 之间（0 会导致 SSDP 广告无效端口）")
            return

        # 进入“启动中”状态：给用户明确反馈
        state["starting"] = True
        lbl_state.config(text="启动中…", foreground="#E67E22")
        btn_start.config(state="disabled")
        prg_start.pack(side="left", padx=8)
        prg_start.start(80)

        def worker() -> None:
            # SSDPSRV 共存说明：实测与 Windows SSDP 服务共存（SO_REUSEPORT）发现完全正常。
            # CREATE_NO_WINDOW 防止 GUI 程序启动 sc.exe 时闪黑窗。
            try:
                import subprocess
                r = subprocess.run(
                    ["sc", "query", "SSDPSRV"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if "RUNNING" in r.stdout.upper():
                    log.info("Windows SSDPSRV 正在运行（与 VR-DLNA 共存，不影响发现）")
            except Exception:
                pass
            try:
                ip = lan_ips()[0] if lan_ips() else "127.0.0.1"
                roots = [MediaRoot(label=Path(p).name or "Videos", path=Path(p)) for p in state["roots"]]
                app = DlnaApp(roots, port)
                server = DlnaHTTPServer(("0.0.0.0", port), app)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                ssdp = SSDPServer(port, ip)
                ssdp.start()
                state["server"], state["ssdp"] = server, ssdp
                server_events.put(("ok", ip, port))
            except Exception as e:
                server_events.put(("error", f"{type(e).__name__}: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def stop_server() -> None:
        if state["ssdp"]:
            state["ssdp"].stop()
            state["ssdp"] = None
        if state["server"]:
            server = state["server"]
            state["server"] = None

            def _shutdown(server) -> None:
                try:
                    server.shutdown()
                finally:
                    server.server_close()

            threading.Thread(target=_shutdown, args=(server,), daemon=True).start()
        lbl_state.config(text="已停止", foreground="#C0392B")
        btn_start.config(state="normal")
        btn_stop.config(state="disabled")
        log_box.insert(tk.END, f"[{time.strftime('%H:%M:%S')}] 已停止\n")
        log_box.see(tk.END)

    btn_start = ttk.Button(frame_ctl, text="启动", command=start_server)
    btn_start.pack(side="left", padx=4)
    btn_stop = ttk.Button(frame_ctl, text="停止", command=stop_server, state="disabled")
    btn_stop.pack(side="left", padx=4)
    btn_toggle_log = ttk.Button(frame_ctl, text="显示日志")
    btn_toggle_log.pack(side="left", padx=4)
    prg_start = ttk.Progressbar(frame_ctl, mode="indeterminate", length=120)
    prg_start.pack(side="left", padx=8)
    prg_start.pack_forget()

    def drain_server_events() -> None:
        try:
            while True:
                evt = server_events.get_nowait()
                if evt[0] == "ok":
                    _, ip, port = evt
                    state["starting"] = False
                    lbl_state.config(text=f"运行中  http://{ip}:{port}", foreground="#2E7D32")
                    btn_start.config(state="disabled")
                    btn_stop.config(state="normal")
                    prg_start.stop()
                    prg_start.pack_forget()
                    log_box.insert(tk.END, f"[{time.strftime('%H:%M:%S')}] 已启动: http://{ip}:{port}  SSDP 发现已开启\n")
                    log_box.see(tk.END)
                elif evt[0] == "error":
                    _, msg = evt
                    state["starting"] = False
                    lbl_state.config(text="启动失败", foreground="#C0392B")
                    btn_start.config(state="normal")
                    prg_start.stop()
                    prg_start.pack_forget()
                    messagebox.showerror("启动失败", msg, parent=root)
        except queue.Empty:
            pass
        except Exception:
            pass
        root.after(100, drain_server_events)

    root.after(100, drain_server_events)

    # ---- 设置页 ----
    settings = load_app_settings()
    var_start_boot = tk.BooleanVar(value=settings.get("start_at_boot", False))
    var_start_minimized = tk.BooleanVar(value=settings.get("start_minimized", False))
    var_close_tray = tk.BooleanVar(value=settings.get("close_to_tray", False))
    var_auto_dlna = tk.BooleanVar(value=settings.get("auto_start_dlna", False))
    tray_queue: queue.Queue = queue.Queue()
    tray_icon = None

    def ensure_tray(enabled: bool) -> None:
        nonlocal tray_icon
        if enabled:
            if tray_icon is None:
                try:
                    from tray_icon import TrayIcon
                    icon = TrayIcon(tray_queue, icon_path=str(_app_icon_path()))
                    if icon.start():
                        tray_icon = icon
                    else:
                        log.warning("系统托盘不可用，关闭时将降级为最小化到任务栏")
                except Exception as e:
                    tray_icon = None
                    log.warning("系统托盘初始化失败: %s", e)
            else:
                if not tray_icon.start() or not tray_icon.running:
                    try:
                        tray_icon.stop()
                    except Exception:
                        pass
                    tray_icon = None
                    log.warning("系统托盘不可用，关闭时将降级为最小化到任务栏")
        else:
            if tray_icon is not None:
                try:
                    tray_icon.stop()
                except Exception:
                    pass
                tray_icon = None

    def restore_from_tray() -> None:
        root.deiconify()
        root.lift()
        root.focus_force()

    def save_settings_ui() -> None:
        settings["start_at_boot"] = bool(var_start_boot.get())
        settings["start_minimized"] = bool(var_start_minimized.get())
        settings["close_to_tray"] = bool(var_close_tray.get())
        settings["auto_start_dlna"] = bool(var_auto_dlna.get())
        save_app_settings(settings)
        if not set_autostart(settings["start_at_boot"]):
            log.warning("开机自启动设置失败")
        ensure_tray(settings["close_to_tray"])

    ttk.Label(tab_settings, text="常规设置", font=("", 12, "bold")).pack(anchor="w", pady=(4, 8))
    chk_boot = ttk.Checkbutton(tab_settings, text="开机自启动（登录 Windows 后自动运行）", variable=var_start_boot)
    chk_boot.pack(anchor="w", pady=3)
    chk_min = ttk.Checkbutton(tab_settings, text="启动时最小化窗口", variable=var_start_minimized)
    chk_min.pack(anchor="w", pady=3)
    chk_tray = ttk.Checkbutton(tab_settings, text="点击关闭时最小化到托盘（而不是退出）", variable=var_close_tray)
    chk_tray.pack(anchor="w", pady=3)
    chk_auto_dlna = ttk.Checkbutton(tab_settings, text="启动应用时自动开启DLNA服务器", variable=var_auto_dlna)
    chk_auto_dlna.pack(anchor="w", pady=3)
    btn_save_settings = ttk.Button(tab_settings, text="保存设置", command=save_settings_ui)
    btn_save_settings.pack(anchor="w", pady=(12, 4))
    ttk.Label(tab_settings, text="提示：关闭到托盘后，可在托盘图标右键选择“退出”。", foreground="#888").pack(anchor="w")

    def drain_tray_queue() -> None:
        try:
            while True:
                evt = tray_queue.get_nowait()
                if evt[0] == "show":
                    restore_from_tray()
                elif evt[0] == "menu":
                    _, x, y = evt
                    m = tk.Menu(root, tearoff=0)
                    m.add_command(label="显示抚物器", command=restore_from_tray)
                    m.add_command(label="退出", command=real_exit)
                    try:
                        m.tk_popup(x, y)
                    finally:
                        try:
                            m.grab_release()
                        except Exception:
                            pass
        except queue.Empty:
            pass
        except Exception:
            pass
        try:
            root.after(120, drain_tray_queue)
        except Exception:
            pass

    root.after(120, drain_tray_queue)
    ensure_tray(settings["close_to_tray"])
    if settings["start_minimized"]:
        if settings["close_to_tray"] and tray_icon is not None:
            root.after(150, root.withdraw)
        else:
            root.after(150, lambda: root.state("iconic"))

    # ---- 窗口大小跟随日志显隐（相当于把日志窗口切掉） ----
    def get_notebook_chrome() -> int:
        try:
            max_req = max(tab_dlna.winfo_reqheight(), tab_sync.winfo_reqheight(), tab_video.winfo_reqheight(), tab_settings.winfo_reqheight())
            return max(0, nb.winfo_reqheight() - max_req)
        except Exception:
            return 28

    def adjust_window_size() -> None:
        try:
            root.update_idletasks()
            sel = nb.select()
            if not sel:
                return
            cur = root.nametowidget(sel)
            chrome = get_notebook_chrome()
            h = cur.winfo_reqheight() + chrome + 8
            w = root.winfo_width() or 760
            root.geometry(f"{w}x{h}")
        except Exception:
            pass

    def on_sync_log_resize(visible: bool) -> None:
        adjust_window_size()

    # ---- Funscript 脚本同步页 ----
    from funscript_sync_ui import create_sync_page, save_all_configs as _save_funscript_configs
    create_sync_page(tab_sync, root, on_resize=on_sync_log_resize)

    # ---- 视频文件夹同步页（逻辑与 Funscript 脚本同步一致） ----
    from video_sync_ui import create_video_sync_page, save_all_video_configs as _save_video_configs

    def on_video_log_resize(visible: bool) -> None:
        adjust_window_size()

    create_video_sync_page(tab_video, root, on_resize=on_video_log_resize)



    # ---- 日志 ----
    frame_log = ttk.LabelFrame(tab_dlna, text=" 日志 ", padding=4)
    # 默认隐藏日志，点击“显示日志”后才显示
    log_box = tk.Text(frame_log, height=8, state="normal")
    scroll = ttk.Scrollbar(frame_log, command=log_box.yview)
    log_box.config(yscrollcommand=scroll.set)
    log_box.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")

    log_visible = {"show": False}
    def toggle_log() -> None:
        log_visible["show"] = not log_visible["show"]
        if log_visible["show"]:
            frame_log.pack(fill="both", expand=True, padx=8, pady=(4, 8))
            btn_toggle_log.config(text="隐藏日志")
        else:
            frame_log.pack_forget()
            btn_toggle_log.config(text="显示日志")
        adjust_window_size()

    btn_toggle_log.config(command=toggle_log)
    nb.bind("<<NotebookTabChanged>>", lambda _e: adjust_window_size())

    # 日志重定向（队列 + 主线程轮询：tkinter 控件只能由主线程操作）
    gui_log_handler = None

    class GuiHandler(logging.Handler):
        def __init__(self, box: "tk.Text"):
            super().__init__()
            self._box = box
            self._q: queue.Queue = queue.Queue()
            root.after(120, self._drain_loop)

        def emit(self, record):
            self._q.put(f"[{time.strftime('%H:%M:%S')}] {record.getMessage()}\n")

        def _drain_loop(self):
            try:
                while True:
                    line = self._q.get_nowait()
                    self._box.insert(tk.END, line)
                self._box.see(tk.END)
            except queue.Empty:
                pass
            except Exception:
                pass
            finally:
                try:
                    root.after(120, self._drain_loop)
                except Exception:
                    pass

    gui_log_handler = GuiHandler(log_box)
    logging.getLogger("vr-dlna").addHandler(gui_log_handler)

    # 记住上次配置
    cfg_path = _app_data_dir() / "vr_dlna_config.json"
    try:
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            raw_roots = cfg.get("roots", [])
            loaded: list[str] = []
            missing: list[str] = []
            for r in raw_roots:
                try:
                    if _os_retry(lambda: os.path.isdir(r), what=f"isdir {r}"):
                        loaded.append(r)
                    else:
                        missing.append(r)
                except OSError:
                    missing.append(r)
            if missing:
                # 只要有任何根目录暂时不可访问就保留原配置，避免云盘“打盹”导致配置丢失
                state["roots"] = raw_roots
                log.warning("部分根目录当前不可访问，已保留原配置: %s", missing)
            else:
                state["roots"] = loaded
            refresh_roots_list()
            if cfg.get("port"):
                var_port.set(str(cfg["port"]))
    except Exception as e:
        log.warning("配置加载失败: %s", e)

    if settings.get("auto_start_dlna"):
        if state["roots"]:
            root.after(300, start_server)
        else:
            log.warning("已开启“启动时自动开启DLNA”，但当前没有视频根目录，已跳过自动启动")

    def save_gui_configs() -> None:
        try:
            port = int(var_port.get())
        except ValueError:
            port = 1919
            log.warning("端口输入无效，保存默认 1919")
        if not 0 < port <= 65535:
            port = 1919
        try:
            tmp = cfg_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"roots": state["roots"], "port": port}, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, cfg_path)
        except OSError as e:
            log.error("配置保存失败: %s", e)
        _save_funscript_configs()
        _save_video_configs()

    def real_exit() -> None:
        try:
            try:
                save_settings_ui()
            except Exception:
                pass
            try:
                save_gui_configs()
            except Exception as e:
                log.error("关闭时保存配置失败: %s", e)
        finally:
            if gui_log_handler is not None:
                logging.getLogger("vr-dlna").removeHandler(gui_log_handler)
            stop_server()
            ensure_tray(False)
            root.destroy()

    def on_close() -> None:
        if settings["close_to_tray"]:
            try:
                save_settings_ui()
            except Exception:
                pass
            save_gui_configs()
            ensure_tray(True)
            if tray_icon is not None:
                root.withdraw()
            else:
                # 托盘不可用时降级为最小化到任务栏，避免窗口彻底消失
                root.iconify()
            return
        real_exit()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


def _main_impl() -> None:
    setup_logging()
    # 单实例检查放在任何服务/命令行启动之前，避免双开
    if not _acquire_single_instance():
        try:
            import tkinter.messagebox as mb
            mb.showerror("抚物器", "已有另一个 抚物器 实例在运行。\n请先关闭它再启动，避免配置互相覆盖。")
        except Exception:
            print("已有另一个 抚物器 实例在运行")
        return
    # 支持命令行快速启动：python vr_dlna.py <dir> [port]
    if len(sys.argv) > 1 and os.path.isdir(sys.argv[1]):
        roots = [MediaRoot(label=Path(sys.argv[1]).name or "Videos", path=Path(sys.argv[1]))]
        port = 1919
        if len(sys.argv) > 2 and sys.argv[2].isdigit():
            port = int(sys.argv[2])
        if not 0 < port <= 65535:
            print("端口必须在 1~65535 之间")
            return
        app = DlnaApp(roots, port)
        server = DlnaHTTPServer(("0.0.0.0", port), app)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        ip = lan_ips()[0] if lan_ips() else "127.0.0.1"
        SSDPServer(port, ip).start()
        print(f"抚物器 运行中: http://{ip}:{port}   (Ctrl+C 停止)")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            server.shutdown()
        return
    run_gui()


def main() -> None:
    try:
        _main_impl()
    except Exception:
        import traceback
        try:
            import tkinter.messagebox as mb
            log_path = _app_data_dir() / "vr_dlna_error.log"
            try:
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}]\n")
                    traceback.print_exc(file=f)
            except OSError:
                pass
            mb.showerror("抚物器", f"程序启动失败：\n{traceback.format_exc()}\n\n详情已写入：{log_path}")
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
