# -*- coding: utf-8 -*-
"""媒体库扫描与索引（M1）。

设计要点（R108 挑刺修正后）：
· 增量扫描：以 (路径, mtime, size) 为键的持久化索引（data\\library_index.json），
  未变化的文件不重新探测。
· 本地盘/网盘分级：固定磁盘（GetDriveTypeW=DRIVE_FIXED）才做时长探测与缩略图
  （都走 libmpv 秒级开销）；网络盘只列文件名——DLNA 当年「网盘逐视频 ffprobe 必超时」
  的教训不重蹈。
· 无 ffmpeg 依赖：时长/缩略图全走 libmpv（vo=image 抽帧）。
· 一文件一卡片：不做多文件自动归组（用户裁定 R119 删除——曾按"同基名+尾号"聚合，
  实测会把 SIVR-001/SIVR-002 这类不同编号误并成一张卡）。
· funscript 配对：同目录同名 .funscript（精确 stem 匹配）。
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import threading
import time
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mkv", ".wmv", ".avi", ".mov", ".webm", ".m2ts", ".ts"}

APP_DIR = Path(__file__).resolve().parent.parent.parent   # vendor/player → 安装目录
DLL_DIR = APP_DIR / "vendor" / "mpv"
_dll_ready = False


def ensure_dll() -> None:
    """把 vendor\\mpv 加进 DLL 搜索路径（libmpv 抽帧/探时长前必须调用一次）。
    DLL 不在时不置就绪位：装好/补回 DLL 后下次调用还能生效。
    （原在 mpv_player.py〔外挂 mpv 播放器封装，已随其弃用删除〕，搬到此处——
    缩略图/时长探测仍依赖 libmpv。）"""
    global _dll_ready
    if _dll_ready:
        return
    if (DLL_DIR / "libmpv-2.dll").is_file():
        d = str(DLL_DIR)
        try:
            os.add_dll_directory(d)
        except Exception:
            pass
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
        _dll_ready = True


def drive_is_local(path: Path) -> bool:
    """固定磁盘 True；网络盘/可移动盘 False（只列文件名，不做逐文件探测）。"""
    try:
        drive = str(path.resolve().drive) + "\\"
        return ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(drive)) == 3
    except Exception:
        return False


class Library:
    def __init__(self, index_file: Path, thumb_dir: Path):
        self.index_file = index_file
        self.thumb_dir = thumb_dir
        # RLock：host 的 /api/library/* 处理器会持锁调 cards()（内部再加锁）——
        # Lock 不可重入，曾经因此在 browse/items 两个端点上永久死锁（R110）。
        self.lock = threading.RLock()
        self.scanning = False
        self.scan_progress = ""      # 人读的进度行
        self.last_scan = 0.0
        self._cards: list[dict] = []
        try:
            self.thumb_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    # ------------------------------------------------------------ 索引
    def _load_index(self) -> dict:
        # 内存缓存：browse 每个视频卡都查一次缩略图，旧版每次都整份读盘解析 JSON
        # （100 个视频 = 100 次全量解析）；写路径都经 _save_index 回填缓存，
        # _scan 与 set_progress 因此共享同一份 dict，扫描中途的进度更新不再被覆盖。
        if getattr(self, "_idx_cache", None) is not None:
            return self._idx_cache
        try:
            self._idx_cache = json.loads(self.index_file.read_text(encoding="utf-8"))
        except Exception:
            self._idx_cache = {}
        return self._idx_cache

    def _save_index(self, idx: dict) -> None:
        try:
            tmp = self.index_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
            import os
            os.replace(tmp, self.index_file)
            self._idx_cache = idx
        except Exception:
            pass

    def cards(self) -> list[dict]:
        with self.lock:
            return list(self._cards)

    def thumb_name_for(self, path: str) -> str:
        """索引里该视频的缩略图文件名（无则空串）。浏览列表轻量查询用。"""
        e = self._load_index().get("files", {}).get(path) or {}
        return e.get("thumb", "")

    def set_progress(self, path: str, pos: float, dur: float, played_at: float) -> None:
        """播放进度落索引（host 的 player 回调调用），并同步到内存卡片。"""
        with self.lock:
            idx = self._load_index()
            files = idx.setdefault("files", {})
            e = files.setdefault(path, {})
            e["pos"], e["dur"], e["played_at"] = round(pos, 1), round(dur, 1), played_at
            self._save_index(idx)
            for c in self._cards:
                if path in c.get("paths", []):
                    c["last_played"] = played_at
                    c["progress"] = {"path": path, "pos": round(pos, 1), "dur": round(dur, 1)}
                    for p in c["parts"]:
                        if p["path"] == path:
                            p["pos"], p["dur"] = round(pos, 1), round(dur, 1)

    # ------------------------------------------------------------ 扫描
    def rescan(self, roots: list[str]) -> dict:
        """阻塞式全量扫描（增量利用索引）。宿主放后台线程调。"""
        with self.lock:
            if self.scanning:
                return {"ok": False, "error": "扫描已在进行"}
            self.scanning = True
        try:
            return self._scan(roots)
        finally:
            with self.lock:
                self.scanning = False
                self.scan_progress = ""
                self.last_scan = time.time()

    def _scan(self, roots: list[str]) -> dict:
        t0 = time.time()
        idx = self._load_index()
        files_idx = idx.setdefault("files", {})
        found: dict[str, dict] = {}   # path → entry（本轮见到的全部视频）
        seen_dirs = 0
        for root in roots:
            rp = Path(root)
            if not rp.is_dir():
                continue
            local = drive_is_local(rp)
            for dirpath, dirnames, filenames in os.walk(rp):
                seen_dirs += 1
                self.scan_progress = f"扫描 {dirpath}"
                dirnames[:] = [d for d in dirnames if not d.startswith((".", "$"))]
                vids = [f for f in filenames if Path(f).suffix.lower() in VIDEO_EXTS]
                if not vids:
                    continue
                d = Path(dirpath)
                for v in vids:
                    vp = d / v
                    try:
                        st = vp.stat()
                    except OSError:
                        continue
                    key = str(vp)
                    prev = files_idx.get(key) or {}
                    need_probe = (local and
                                  (prev.get("mtime") != st.st_mtime_ns or
                                   prev.get("size") != st.st_size))
                    fun = key.rsplit(".", 1)[0] + ".funscript"
                    e = {
                        "dir": str(d), "name": v, "size": st.st_size,
                        "mtime": st.st_mtime_ns,
                        "dur": prev.get("dur", 0.0),
                        "thumb": prev.get("thumb", ""),
                        "local": local,
                        "funscript": fun if (d / (v.rsplit(".", 1)[0] + ".funscript")).is_file() else "",
                        "pos": prev.get("pos", 0.0),
                        "played_at": prev.get("played_at", 0.0),
                    }
                    if need_probe or (local and (not prev.get("dur") or not prev.get("thumb"))):
                        # 时长或缩略图缺一就补探——旧条件只看 dur：某次扫描抽帧失败（瞬时）
                        # 但时长成功，此后永不重试，缩略图永久缺失（K1cztm.mp4 实测案例）
                        dur, thumb = self._probe_local(vp)
                        e["dur"], e["thumb"] = dur, thumb
                    found[key] = e
        # 清掉本轮没见到的旧条目（文件被删/挪走）
        for k in list(files_idx):
            if k not in found:
                del files_idx[k]
        # R125 修复（全项目审查中危）：合并时逐条保留**较新的播放进度**——
        # _scan 全程不持锁，扫描（单文件探测可达 60s）期间 set_progress 并发写入
        # 的新进度会被 found 里的扫描期快照覆盖（实测复现：进度 999 → 扫后回退 50，
        # 内存卡片同步回退）。对每个本轮见过的文件，若 files_idx 同键的 played_at
        # 更新（用户在扫描期间看过），则保留其 pos/played_at。
        for k, e in found.items():
            prev_e = files_idx.get(k)
            if prev_e and float(prev_e.get("played_at") or 0) > float(e.get("played_at") or 0):
                e["pos"] = prev_e.get("pos", 0.0)
                e["played_at"] = prev_e.get("played_at", 0.0)
        # R125 修复（全项目审查中危·补充窗口）：收尾合并前重读磁盘索引——扫描期间
        # set_progress（持锁写盘）对"**尚未被遍历到**的文件"的更新落在磁盘上，
        # 若直接用扫描开始时的快照落盘会把这些更新整体打回（用户正在看的视频
        # 恰在扫描后半段时必中）。本轮没见到的文件 _scan 未触碰，以磁盘最新为准。
        latest_idx = self._load_index().get("files", {})
        for k in files_idx:
            if k not in found and k in latest_idx:
                files_idx[k] = latest_idx[k]
        idx["files"] = {**files_idx, **found}
        idx["last_scan"] = time.time()
        self._save_index(idx)
        cards = self._aggregate(found, idx)
        with self.lock:
            self._cards = cards
        return {"ok": True, "files": len(found), "cards": len(cards),
                "dirs": seen_dirs, "sec": round(time.time() - t0, 1)}

    def _probe_local(self, vp: Path) -> tuple[float, str]:
        """本地盘：libmpv 探时长 + 抽缩略图（12%，全黑则 45% 重试）。"""
        ensure_dll()   # vendor/mpv 加入 DLL 搜索路径（libmpv）
        dur, thumb = 0.0, ""
        try:
            import mpv   # noqa: PLC0415
            h = hashlib.md5(str(vp).encode("utf-8")).hexdigest()[:16]
            out = self.thumb_dir / f"{h}.jpg"
            if out.is_file() and out.stat().st_size > 0:
                thumb = out.name
            m = mpv.MPV(vo="null", ao="null", hwdec="auto", osc=False,
                        input_default_bindings=False)
            try:
                m.play(str(vp))
                for _ in range(80):
                    if m.duration:
                        break
                    time.sleep(0.25)
                dur = round(m.duration or 0.0, 1)
            finally:
                m.terminate()
            if not thumb and dur > 0:
                for frac in (0.12, 0.45):
                    cand = self.thumb_dir / "00000001.jpg"
                    try:
                        if cand.exists():
                            cand.unlink()
                        # outdir 必须用独立属性 vo_image_outdir——塞进 vo= 子选项
                        # 会被盘符冒号切坏（R109 实测）。
                        t = mpv.MPV(vo="image", vo_image_format="jpg",
                                    vo_image_outdir=str(self.thumb_dir),
                                    start=round(dur * frac, 1), frames=1,
                                    ao="null", osc=False,
                                    input_default_bindings=False)
                        t.play(str(vp))
                        for _ in range(80):
                            if cand.is_file():
                                break
                            time.sleep(0.25)
                        t.terminate()
                        if cand.is_file():
                            cand.replace(out)
                            thumb = out.name
                            break
                    except Exception:
                        try:
                            t.terminate()
                        except Exception:
                            pass
                        continue
        except Exception:
            pass
        return dur, thumb

    # ------------------------------------------------------------ 卡片组装
    def _aggregate(self, files: dict, idx: dict) -> list[dict]:
        """一个文件一张卡（R119：不再做多文件自动归组——见文件头说明）。
        卡片结构保留为单元素 parts 列表，接口/日志结构与旧版兼容。"""
        cards = []
        for path, e in files.items():
            stem = Path(e["name"]).stem
            parts = [{**e, "path": path}]
            d = e["dir"]
            pick = parts[0]
            card = {
                "id": hashlib.md5(f"{d}|{stem}".encode("utf-8")).hexdigest()[:16],
                "title": stem,
                "folder": Path(d).name,
                "parts": [{"path": p["path"], "name": p["name"],
                           "dur": p.get("dur", 0.0), "pos": p.get("pos", 0.0),
                           "local": p["local"]} for p in parts],
                # 浏览页按路径反查卡片用（/api/library/browse 的 dur/pos/thumb 都靠它）
                "paths": [p["path"] for p in parts],
                "has_funscript": any(p["funscript"] for p in parts),
                "duration": max(p.get("dur") or 0 for p in parts),
                "thumb": pick.get("thumb", ""),
                "local": pick["local"],
                "last_played": pick.get("played_at") or 0,
                "progress": ({"path": pick["path"], "pos": pick.get("pos", 0.0),
                              "dur": pick.get("dur", 0.0)}
                             if pick.get("pos") else None),
            }
            cards.append(card)
        cards.sort(key=lambda c: c["last_played"], reverse=True)
        return cards
