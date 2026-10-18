# -*- coding: utf-8 -*-
"""AI 实时识别服务：HTTP/JSON 接口 + 引擎生命周期。

接口（全部回 JSON；`/health` 始终开放，其余受 auth_token 保护）：

  GET  /health
      {ok, pid, engine_state, warmup, model, device, gpu_used_gb, job, ...}
  POST /open      {video, duration_ms?, start_ms?}
      打开一个识别任务（异步）：转码 → 加载模型 → 边播边推理。
  GET  /state
      {ok, state, coverage_ms, n_actions, warmup_pct, note, ...}
  GET  /actions?from_ms&to_ms
      {ok, actions:[{at,pos}], coverage_ms}
  POST /close
      结束任务、释放显存。
  POST /shutdown
      进程退出（宿主用）。

设计要点：宿主每 100~200ms 拉一次 /actions 增量，用和现有脚本联动完全相同的
下发路径（/api/sync/tick）驱动设备 —— 引擎与设备解耦，BLE 只由宿主碰。
"""
from __future__ import annotations

import json
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE = Path(__file__).resolve().parent


class VisionService:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg or {}
        srv = self.cfg.get("server") or {}
        self.auth_token = str(srv.get("auth_token") or "")
        self.model_cfg = self.cfg.get("model") or {}
        self.engine_cfg = self.cfg.get("engine") or {}
        self.cache_dir = Path(
            (self.cfg.get("paths") or {}).get("cache_dir")
            or (BASE / "cache"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.engine = None
        self.job = None                 # {"video", "started_at", "duration_ms"}
        self.last_error = ""
        self.last_note = ""
        self.last_note_at = 0.0
        self.idle_since = time.time()

    # ---------------- 引擎 ----------------
    def open(self, video: str, duration_ms: float | None = None,
             start_ms: float = 0.0) -> dict:
        with self.lock:
            if self.engine is not None:
                return {"ok": False, "error": "已有识别任务在跑，先 /close"}
            if not Path(video).is_file():
                return {"ok": False, "error": f"视频不存在: {video}"}
            try:
                from stream_engine import StreamEngine  # 延迟导入(torch 很重)
                if duration_ms is None:
                    duration_ms = _probe_duration_ms(video)
                eng = StreamEngine(
                    video, duration_ms,
                    device=str(self.engine_cfg.get("device", "cuda")),
                    ckpt=self.model_cfg.get("checkpoint"),
                    on_note=self._note,
                    cache_dir=str(self.cache_dir),
                )
                eng.start()
                self.engine = eng
                self.job = {"video": video, "started_at": time.time(),
                            "duration_ms": duration_ms,
                            "start_ms": float(start_ms or 0.0)}
                self.last_error = ""
                return {"ok": True, "video": video,
                        "duration_ms": duration_ms}
            except Exception as e:  # noqa: BLE001
                self.last_error = f"{type(e).__name__}: {e}"
                traceback.print_exc()
                return {"ok": False, "error": self.last_error}

    def close(self) -> dict:
        with self.lock:
            eng, self.engine = self.engine, None
            self.job = None
            self.idle_since = time.time()
        if eng is not None:
            try:
                eng.stop(cleanup=True)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
        _release_cuda()
        return {"ok": True}

    def _note(self, s: str) -> None:
        """引擎阶段提示：既进日志，也存一份给 /state —— 预热遮罩要显示
        "正在转码 / 正在加载模型 / 正在识别"，只靠 coverage 百分比用户
        分不清是在干活还是卡死。"""
        print(f"[engine] {s}", flush=True)
        self.last_note = s
        self.last_note_at = time.time()

    # ---------------- 查询 ----------------
    def state(self) -> dict:
        eng = self.engine
        if eng is None:
            return {"ok": True, "state": "idle", "n_actions": 0,
                    "coverage_ms": 0, "warmup_pct": 0,
                    "note": self.last_note, "job": None,
                    "error": self.last_error}
        actions, gen_until = eng.buffer.snapshot()
        job = dict(self.job or {})
        elapsed = max(0.0, time.time() - job.get("started_at", time.time()))
        dur = job.get("duration_ms") or 1.0
        return {
            "ok": True,
            "state": eng.buffer.state,          # warming/running/done/error
            "note": eng.buffer.note or self.last_note,
            "n_actions": len(actions),
            "coverage_ms": float(gen_until),
            "warmup_pct": round(min(100.0, gen_until / dur * 100.0), 1),
            "elapsed_sec": round(elapsed, 1),
            "job": job,
            "error": self.last_error,
        }

    def actions(self, from_ms: float, to_ms: float) -> dict:
        eng = self.engine
        if eng is None:
            return {"ok": True, "actions": [], "coverage_ms": 0}
        actions, gen_until = eng.buffer.snapshot()
        out = [a for a in actions if from_ms <= a["at"] < to_ms]
        return {"ok": True, "actions": out, "coverage_ms": float(gen_until)}

    def health(self) -> dict:
        st = self.state()
        return {
            "ok": True, "pid": __import__("os").getpid(),
            "engine_state": st.get("state"),
            "warmup_pct": st.get("warmup_pct", 0),
            "n_actions": st.get("n_actions", 0),
            "coverage_ms": st.get("coverage_ms", 0),
            "model": self.model_cfg.get("checkpoint") or "v0.6.0",
            "device": self.engine_cfg.get("device", "cuda"),
            "gpu_used_gb": _gpu_used_gb(),
            "idle_sec": round(time.time() - self.idle_since, 1),
            "error": self.last_error,
        }

    def shutdown(self) -> None:
        self.close()

    # ---------------- HTTP ----------------
    def handler(self):
        svc = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, fmt, *a):
                pass

            def _json(self, obj, code=200):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

            def _auth_ok(self) -> bool:
                if not svc.auth_token:
                    return True
                return self.headers.get("X-FSC-Vision-Token", "") == svc.auth_token

            def _body(self) -> dict:
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    if n <= 0 or n > 1 << 20:
                        return {}
                    return json.loads(self.rfile.read(n).decode("utf-8"))
                except Exception:  # noqa: BLE001
                    return {}

            def do_GET(self):  # noqa: N802
                u = urlparse(self.path)
                if u.path == "/health":
                    self._json(svc.health())
                    return
                if not self._auth_ok():
                    self._json({"ok": False, "error": "unauthorized"}, 401)
                    return
                q = parse_qs(u.query)
                if u.path == "/state":
                    self._json(svc.state())
                elif u.path == "/actions":
                    a = float((q.get("from_ms") or ["0"])[0] or 0)
                    b = float((q.get("to_ms") or ["1e18"])[0] or 1e18)
                    self._json(svc.actions(a, b))
                else:
                    self._json({"ok": False, "error": "not found"}, 404)

            def do_POST(self):  # noqa: N802
                if not self._auth_ok():
                    self._json({"ok": False, "error": "unauthorized"}, 401)
                    return
                u = urlparse(self.path)
                b = self._body()
                if u.path == "/open":
                    self._json(svc.open(str(b.get("video") or ""),
                                        b.get("duration_ms"),
                                        b.get("start_ms") or 0.0))
                elif u.path == "/close":
                    self._json(svc.close())
                elif u.path == "/shutdown":
                    self._json({"ok": True})
                    threading.Thread(target=lambda: (
                        time.sleep(0.2), svc.shutdown(),
                        __import__("os")._exit(0)), daemon=True).start()
                else:
                    self._json({"ok": False, "error": "not found"}, 404)

        return H


def _probe_duration_ms(video: str) -> float:
    import subprocess
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration", "-of", "csv=p=0", video],
                       capture_output=True, text=True)
    return float(r.stdout.strip()) * 1000.0


def _gpu_used_gb() -> float | None:
    try:
        import torch
        if not torch.cuda.is_available():
            return None
        return round(torch.cuda.memory_allocated() / 1e9, 2)
    except Exception:  # noqa: BLE001
        return None


def _release_cuda() -> None:
    try:
        import torch
        torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001
        pass
