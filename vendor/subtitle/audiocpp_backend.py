# -*- coding: utf-8 -*-
"""audiocpp 常驻服务 ASR 后端。

为什么用它：
  - 显存占用 0（CPU 后端），把 8GB GPU 全留给翻译/更大模型
  - 无 Python/torch 依赖，常驻进程 ~2.8GB 内存
  - 自带标点

为什么必须配 VAD 裁剪（实测结论）：
  整块 600s 音频直接送 ASR → 出现 6 个 ≥10 连重复串（「啊啊啊…」×509），
  有效日文仅 56 字符；先 VAD 出语音段再逐段送 → 重复串 0、文本干净。

用法（作为库）：
    from audiocpp_backend import AudioCppBackend
    be = AudioCppBackend(cfg)          # cfg 见 server 配置
    be.ensure_server()                 # 拉起常驻服务（幂等）
    spk = be.speech_spans(pcm)         # VAD → [(start_sec, end_sec)]
    segs = be.transcribe_spans(pcm, spans, video_start_ms, lang)   # → 分句
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.request
import wave
from pathlib import Path

import numpy as np

SR = 16000
AUDIOCPP_DIR = Path(os.environ.get("AUDIOCPP_DIR", r"E:\audiocpp-portable"))


class AudioCppError(RuntimeError):
    pass


class AudioCppBackend:
    """封装 audiocpp_server 的 ASR + silero_vad。"""

    def __init__(self, cfg: dict):
        self.dir = Path(cfg.get("dir") or AUDIOCPP_DIR)
        self.backend = str(cfg.get("backend", "cpu"))          # cpu | cuda
        self.threads = int(cfg.get("threads", max(1, (os.cpu_count() or 4) - 1)))
        self.port = int(cfg.get("port", 8083))
        self.host = str(cfg.get("host", "127.0.0.1"))
        self.model = str(cfg.get("model") or (self.dir / "models" / "Qwen3-ASR-0.6B"))
        self.vad_model = str(cfg.get("vad_model") or
                             (self.dir / "assets" / "framework" / "models" / "silero_vad"))
        self.language = str(cfg.get("language", "Japanese"))
        self.pad_sec = float(cfg.get("pad_sec", 0.25))
        self.merge_gap = float(cfg.get("merge_gap_sec", 0.5))
        self.min_speech_ms = int(cfg.get("min_speech_ms", 250))
        # 单段最长秒数：超过此长度的语音段先切开再送 ASR（防退化）
        self.max_span_sec = float(cfg.get("max_span_sec", 8.0))
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    # ---- 与 PyTorch 引擎对齐的属性（server_app 的 /health 会读）----
    @property
    def vad(self):
        """VAD 由 audiocpp 的 silero_vad 承担；返回非 None 表示可用。"""
        return True

    @property
    def use_aligner(self) -> bool:
        """本后端暂不做逐字对齐（时间戳来自 VAD 语音段边界）。"""
        return False

    @property
    def load_s(self) -> float:
        return 0.0

    @property
    def model(self):
        return self._model_path

    @model.setter
    def model(self, v):
        self._model_path = str(v)

    # ---------------------------------------------------------------- 服务
    @property
    def exe_dir(self) -> Path:
        return self.dir / ("cpu" if self.backend == "cpu" else "gpu")

    def _url(self, path: str) -> str:
        return f"http://{self.host}:{self.port}{path}"

    def probe(self, timeout: float = 2.0) -> bool:
        try:
            with urllib.request.urlopen(self._url("/health"), timeout=timeout) as r:
                d = json.loads(r.read().decode("utf-8"))
            return d.get("status") == "ok"
        except Exception:
            return False

    def ensure_server(self, wait_s: float = 60.0) -> bool:
        """确保常驻服务在跑；已在跑则直接返回。"""
        with self._lock:
            if self.probe():
                return True
            exe = self.exe_dir / "audiocpp_server.exe"
            if not exe.exists():
                raise AudioCppError(f"找不到 audiocpp_server.exe：{exe}")
            cfg_path = Path(tempfile.gettempdir()) / f"audiocpp_asr_{self.port}.json"
            cfg_path.write_text(json.dumps({
                "host": self.host, "port": self.port,
                "backend": "cuda" if self.backend != "cpu" else "cpu",
                "device": 0, "threads": self.threads,
                "models": [{"id": "qwen3-asr", "family": "qwen3_asr",
                            "path": self.model, "task": "asr", "mode": "offline"}],
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            self._proc = subprocess.Popen(
                [str(exe), "--config", str(cfg_path), "--host", self.host,
                 "--port", str(self.port), "--backend",
                 "cuda" if self.backend != "cpu" else "cpu", "--threads", str(self.threads)],
                cwd=str(self.dir),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            t0 = time.time()
            while time.time() - t0 < wait_s:
                if self.probe():
                    return True
                if self._proc.poll() is not None:
                    raise AudioCppError(f"audiocpp_server 启动即退出（code {self._proc.returncode}）")
                time.sleep(0.5)
            raise AudioCppError(f"audiocpp_server 未在 {wait_s}s 内就绪")

    def stop_server(self) -> None:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=5)
                except Exception:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
            self._proc = None

    # ------------------------------------------------------------------ VAD
    def speech_spans(self, pcm: np.ndarray, tmpdir: Path | None = None) -> list:
        """写临时 wav → audiocpp VAD → [(start_sec, end_sec)]（相对本块）。"""
        exe = self.exe_dir / "audiocpp_cli.exe"
        d = Path(tmpdir or tempfile.gettempdir())
        wav_in = d / f"vad_in_{os.getpid()}_{int(time.time()*1000)}.wav"
        out_json = wav_in.with_suffix(".chunks.json")
        try:
            self._write_wav(wav_in, pcm)
            r = subprocess.run(
                [str(exe), "--task", "vad", "--family", "silero_vad",
                 "--model", self.vad_model, "--backend", "cpu",
                 "--audio", str(wav_in), "--vad-chunks-out", str(out_json)],
                cwd=str(self.dir), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=300,
            )
            if not out_json.exists():
                return []
            data = json.loads(out_json.read_text(encoding="utf-8"))
            spans = []
            for c in data:
                a = float(c["start_sample"]) / SR
                b = float(c["end_sample"]) / SR
                if b > a:
                    spans.append((a, b))
            return spans
        except Exception:
            return []
        finally:
            for f in (wav_in, out_json):
                try:
                    f.unlink()
                except Exception:
                    pass

    # ------------------------------------------------------------- 转写
    def transcribe_wav(self, wav: Path) -> dict:
        """调常驻服务转写单个 wav，返回 {text, rtf, wall_s}。"""
        body = json.dumps({"model": "qwen3-asr", "audio": str(wav),
                           "language": self.language}).encode("utf-8")
        req = urllib.request.Request(self._url("/v1/audio/transcriptions"), data=body,
                                     headers={"Content-Type": "application/json"})
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=600) as r:
            d = json.loads(r.read().decode("utf-8"))
        d["wall_s"] = time.perf_counter() - t0
        return d

    @staticmethod
    def _write_wav(path: Path, pcm: np.ndarray) -> None:
        data = np.clip(pcm, -1.0, 1.0)
        ints = (data * 32767.0).astype("<i2")
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(ints.tobytes())

    def transcribe(
        self,
        pcm: np.ndarray,
        lang_key: str = "ja",
        video_start_ms: int = 0,
        keep_from_ms: int = 0,
        vad_cfg: dict | None = None,
        seg_cfg: dict | None = None,
        tmpdir: Path | None = None,
    ) -> dict:
        """接口对齐 PyTorch 引擎：pcm(float32 @16k) → {segments, asr_ms, skipped}。"""
        t0 = time.perf_counter()
        seg_cfg = seg_cfg or {}
        min_ms = int((vad_cfg or {}).get("min_speech_ms", self.min_speech_ms))
        d = Path(tmpdir or tempfile.gettempdir())

        spans = self.speech_spans(pcm, tmpdir=d)
        if not spans:
            return {"language": lang_key, "segments": [], "asr_ms": 0.0,
                    "skipped": True, "backend": "audiocpp"}
        merged_raw = spans

        # 合并过近的语音段（避免切出大量 0.x 秒碎片）
        merged: list = []
        for s, e in merged_raw:
            if merged and s - merged[-1][1] <= self.merge_gap:
                merged[-1] = (merged[-1][0], e)
            else:
                merged.append((s, e))
        # 过滤低于阈值的碎段
        merged = [(s, e) for s, e in merged if (e - s) * 1000 >= min_ms]
        # 长段再切：VAD 合并出的长段（>max_span_sec）会让模型退化出成百连重复，
        # 实测有一段 11.5s 吐出 342 连「あ」。切成 <=max_span_sec 后消失。
        capped: list = []
        for s, e in merged:
            cur = s
            while e - cur > self.max_span_sec:
                capped.append((cur, cur + self.max_span_sec))
                cur += self.max_span_sec
            if e - cur > 0.05:
                capped.append((cur, e))
        merged = capped

        pad = self.pad_sec
        segs = []
        for i, (s, e) in enumerate(merged):
            a = max(0, int((s - pad) * SR))
            b = min(len(pcm), int((e + pad) * SR))
            if b - a < int(0.2 * SR):
                continue
            wav = d / f"asr_{os.getpid()}_{int(time.time()*1000)}_{i}.wav"
            try:
                self._write_wav(wav, pcm[a:b])
                r = self.transcribe_wav(wav)
            except Exception as ex:
                segs.append({"start_ms": video_start_ms + int(a / SR * 1000),
                             "end_ms": video_start_ms + int(b / SR * 1000),
                             "text": "", "error": f"{type(ex).__name__}: {ex}"})
                continue
            finally:
                try:
                    wav.unlink()
                except Exception:
                    pass
            text = (r.get("text") or "").strip()
            if not text:
                continue
            # 重复退化过滤（与 PyTorch 后端同思路）：同一字符连续 >=8 次判退化
            if re.search(r"(.)\1{7,}", text):
                continue
            segs.append({
                "start_ms": video_start_ms + int(a / SR * 1000),
                "end_ms": video_start_ms + int(b / SR * 1000),
                "text": text,
            })

        if keep_from_ms:
            segs = [x for x in segs if x["start_ms"] >= keep_from_ms]
        return {"language": lang_key, "segments": segs,
                "asr_ms": round((time.perf_counter() - t0) * 1000, 1),
                "skipped": False, "backend": "audiocpp"}


if __name__ == "__main__":
    # 自检：对一段音频跑 VAD + 转写
    import sys
    be = AudioCppBackend({"backend": "cpu"})
    print("服务就绪:", be.ensure_server())
    wav = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if wav:
        import wave as _w
        with _w.open(str(wav), "rb") as w:
            raw = w.readframes(w.getnframes())
        pcm = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        t0 = time.time()
        out = be.transcribe(pcm, "ja", 0, 0, {"min_speech_ms": 250},
                            {"max_sec": 8.0, "max_chars": 50, "pause_sec": 0.8})
        print(f"耗时 {time.time()-t0:.1f}s  段数 {len(out['segments'])}")
        for s in out["segments"]:
            print(f"  {s['start_ms']/1000:7.1f}  {s['text']}")
