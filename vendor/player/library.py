# -*- coding: utf-8 -*-
"""媒体库扫描与索引（M1）。

设计要点（R108 挑刺修正后）：
· 增量扫描：以 (路径, mtime, size) 为键的持久化索引（data\\library_index.json），
  未变化的文件不重新探测。
· 本地盘/网盘分级：固定磁盘（GetDriveTypeW=DRIVE_FIXED）才做时长探测与缩略图
  （都走 libmpv 秒级开销）；网络盘只列文件名——DLNA 当年「网盘逐视频 ffprobe 必超时」
  的教训不重蹈。
· 无 ffmpeg 依赖：时长/缩略图全走 libmpv（vo=image 抽帧）。
· 多文件文件夹聚合：同目录同基名（去尾部分件号）的视频合并为一张卡片，缩略图取
  最近播放的分件（用户观察：显示上一次播放的那个）。
· funscript 配对：同目录同名 .funscript（精确 stem 匹配，含分件各自配对）。
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mkv", ".wmv", ".avi", ".mov", ".webm", ".m2ts", ".ts"}
PART_TAIL = re.compile(r"^(?P<base>.+?)[\s._-]*(?:cd|part)?[\s._-]?\d{1,3}$", re.I)
CODE_RE = re.compile(r"\b([A-Z]{2,6})-?(\d{2,5})\b")

def drive_is_local(path: Path) -> bool:
    """固定磁盘 True；网络盘/可移动盘 False（只列文件名，不做逐文件探测）。"""
    try:
        drive = str(path.resolve().drive) + "\\"
        return ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(drive)) == 3
    except Exception:
        return False


def code_of(name: str) -> str:
    m = CODE_RE.search(Path(name).stem.upper())
    return f"{m.group(1)}-{m.group(2)}" if m else ""


def base_of(stem: str) -> str:
    """分件基名：CRVR-194-1 / CRVR-194-2 → crvr-194（无尾部数字则原样）。"""
    m = PART_TAIL.match(stem.strip())
    return (m.group("base") if m else stem).strip().lower()


class Library:
    def __init__(self, index_file: Path, thumb_dir: Path):
        self.index_file = index_file
        self.thumb_dir = thumb_dir
        self.lock = threading.Lock()
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
        try:
            return json.loads(self.index_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_index(self, idx: dict) -> None:
        try:
            tmp = self.index_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
            import os
            os.replace(tmp, self.index_file)
        except Exception:
            pass

    def cards(self) -> list[dict]:
        with self.lock:
            return list(self._cards)

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
                    if need_probe or (local and not prev.get("dur")):
                        dur, thumb = self._probe_local(vp)
                        e["dur"], e["thumb"] = dur, thumb
                    found[key] = e
        # 清掉本轮没见到的旧条目（文件被删/挪走）
        for k in list(files_idx):
            if k not in found:
                del files_idx[k]
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
        from mpv_player import ensure_dll   # noqa: PLC0415（同目录，宿主已把本目录加进 sys.path）
        ensure_dll()
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
                    try:
                        t = mpv.MPV(vo=f"image:format=jpg:outdir={self.thumb_dir}",
                                    start=round(dur * frac, 1), frames=1,
                                    ao="null", osc=False,
                                    input_default_bindings=False)
                        t.play(str(vp))
                        for _ in range(80):
                            if (self.thumb_dir / "00000001.jpg").is_file():
                                break
                            time.sleep(0.25)
                        t.terminate()
                        cand = self.thumb_dir / "00000001.jpg"
                        if cand.is_file():
                            cand.replace(out)
                            thumb = out.name
                            break
                    except Exception:
                        continue
        except Exception:
            pass
        return dur, thumb

    # ------------------------------------------------------------ 聚合
    def _aggregate(self, files: dict, idx: dict) -> list[dict]:
        groups: dict[tuple, list] = {}
        for path, e in files.items():
            stem = Path(e["name"]).stem
            gk = (e["dir"], base_of(stem))
            groups.setdefault(gk, []).append({**e, "path": path})
        cards = []
        for (d, base), parts in groups.items():
            parts.sort(key=lambda p: p["name"])
            # 缩略图取最近播放的分件；没播过取第一件
            parts_sorted = sorted(parts, key=lambda p: p.get("played_at") or 0, reverse=True)
            pick = parts_sorted[0]
            code = code_of(pick["name"]) or code_of(base)
            last = max((p.get("played_at") or 0) for p in parts)
            card = {
                "id": hashlib.md5(f"{d}|{base}".encode("utf-8")).hexdigest()[:16],
                "title": code or base,
                "code": code,
                "folder": Path(d).name,
                "parts": [{"path": p["path"], "name": p["name"],
                           "dur": p.get("dur", 0.0), "pos": p.get("pos", 0.0),
                           "local": p["local"]} for p in parts],
                "has_funscript": any(p["funscript"] for p in parts),
                "duration": max(p.get("dur") or 0 for p in parts),
                "thumb": pick.get("thumb", ""),
                "local": pick["local"],
                "last_played": last,
                "progress": ({"path": pick["path"], "pos": pick.get("pos", 0.0),
                              "dur": pick.get("dur", 0.0)}
                             if pick.get("pos") else None),
            }
            cards.append(card)
        cards.sort(key=lambda c: c["last_played"], reverse=True)
        return cards
