# -*- coding: utf-8 -*-
"""llama.cpp 常驻推理服务 —— 本地翻译模型（GGUF）后端。

为什么用它而不是 Ollama（用户 2026-09-16 明确要求：模型要放**安装目录的 models 文件夹**）：
  1. **模型随安装目录走**：Ollama 的模型库是全局的（`OLLAMA_MODELS`），`ollama create`
     会把 GGUF 复制进它自己的 blob 库——"模型放在 <安装目录>\\models 下面"这条它做不到。
     llama-server 直接吃文件路径：`-m <安装目录>\\models\\...gguf` 即可。
  2. **不依赖用户机器上有没有 Ollama**，也不需要额外的环境变量或开机自启的常驻服务。
  3. 生命周期与 audiocpp（ASR）一致：由字幕服务自己拉起、随字幕服务一起回收
     （宿主 stop 时用 taskkill /T 连孙子进程一起带走，见 host_server._kill_tree）。

为什么常驻而不是每次请求起一次：
  7B-Q4 权重约 4 GB，每次重新加载要数秒（还要重新申请显存），逐批翻译会反复付这个代价。

配置（vendor/subtitle/config.json → translate.local，相对路径按本文件所在目录解析，
与既有 asr.model 的 "./models" 约定一致）：
    {
      "dir":   "../../vendor/llama",
      "exe":   "llama-server.exe",
      "model": "../../models/Sakura-7B-Qwen2.5-v1.0/sakura-7b-qwen2.5-v1.0-iq4xs.gguf",
      "alias": "sakura-7b",
      "host":  "127.0.0.1",
      "port":  8082,
      "ctx":   2048,
      "ngl":   99
    }

用法（作为库）：
    from llama_backend import LlamaBackend
    be = LlamaBackend(cfg)
    be.ensure_server()        # 幂等：已在跑直接复用
    be.base_url               # http://127.0.0.1:8082 → /v1/chat/completions
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent


class LlamaError(RuntimeError):
    pass


def _resolve(p) -> Path:
    """相对路径按本文件所在目录解析（vendor/subtitle → ../../ = 应用根目录）。"""
    q = Path(str(p))
    return q if q.is_absolute() else (BASE / q).resolve()


class LlamaBackend:
    """封装 llama-server（OpenAI 兼容 /v1/chat/completions）。"""

    def __init__(self, cfg: dict):
        cfg = cfg or {}
        self.dir = _resolve(cfg.get("dir") or "../../vendor/llama")
        self.exe = self.dir / str(cfg.get("exe", "llama-server.exe"))
        self.model = _resolve(cfg.get("model") or "")
        self.alias = str(cfg.get("alias", "sakura"))
        self.host = str(cfg.get("host", "127.0.0.1"))
        self.port = int(cfg.get("port", 8082))
        # 上下文窗口：翻译请求是「系统提示 + 一批 ≤10 句」，2048 足够；
        # 显存与 ASR 共享 8 GB，开大只会挤掉 ASR（KV 每 1k token 约 57 MB）
        self.ctx = int(cfg.get("ctx", 2048))
        self.ngl = int(cfg.get("ngl", 99))
        self.threads = int(cfg.get("threads", max(1, (os.cpu_count() or 4) // 2)))
        self.wait_s = float(cfg.get("start_timeout_sec", 180))
        # 日志放**安装目录** logs\（与宿主 host.log / 服务 run_server.log 同处）。
        # 原先落 %TEMP%：排查翻译问题时没人想得到去那儿翻，而且那在系统盘上。
        try:
            import user_paths
            _log_dir = Path(user_paths.logs_dir())
        except Exception:
            _log_dir = Path(tempfile.gettempdir())
        self._log = _log_dir / f"llama_server_{self.port}.log"
        self._proc: subprocess.Popen | None = None
        self._job_handle = None      # Windows Job Object 句柄（父进程崩溃时带走子进程）
        # RLock 而不是 Lock：ensure_server（持锁）超时收尾会调 stop_server（也拿锁），
        # 非重入锁在这里必然自锁死——线程挂在锁上，字幕请求全部跟着挂死。
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- 服务
    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def use_model(self, model_path, alias: str | None = None) -> None:
        """按语言路由（R65.1）：目标模型与当前加载的不同 → 停掉常驻实例，
        下次 ensure_server 用新模型拉起（热切换：首次切换付一次加载时间）。
        路径相同则什么都不做（幂等）。"""
        with self._lock:
            m = _resolve(model_path or "")
            if not m or m == self.model:
                return
            self.stop_server()          # 换模型必须重启 llama-server（权重随进程走）
            self.model = m
            if alias:
                self.alias = str(alias)
            print(f"[llama] 切换翻译模型 → {m.name}", flush=True)

    def probe(self, timeout: float = 2.0) -> bool:
        """llama-server 的 /health：加载中返回 503，就绪后 {"status":"ok"}。"""
        try:
            with urllib.request.urlopen(self.base_url + "/health", timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8")).get("status") == "ok"
        except Exception:
            return False

    def _tail_log(self, lines: int = 12) -> str:
        try:
            txt = self._log.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            return "(无日志)"
        return "\n".join("      " + s for s in txt[-lines:])

    def ensure_server(self) -> bool:
        """确保常驻服务在跑；已在跑则直接复用（幂等，多线程安全）。"""
        with self._lock:
            if self.probe():
                return True
            if not self.exe.exists():
                raise LlamaError(
                    f"找不到 llama-server.exe：{self.exe}"
                    f"（本地翻译运行时未安装：请在 PC 端「识别与翻译」卡的模型列表下载，"
                    f"或运行 tools\\fetch_llama.ps1）")
            if not self.model.exists():
                raise LlamaError(
                    f"找不到翻译模型：{self.model}"
                    f"（模型应放在安装目录的 models 文件夹下）")
            args = [str(self.exe), "-m", str(self.model), "--alias", self.alias,
                    "-c", str(self.ctx), "-ngl", str(self.ngl), "-t", str(self.threads),
                    "--host", self.host, "--port", str(self.port), "--jinja"]
            print(f"[llama] 启动 {self.exe.name}：{self.model.name} "
                  f"(ctx={self.ctx}, ngl={self.ngl}, port={self.port})", flush=True)
            self._log.parent.mkdir(parents=True, exist_ok=True)
            logf = open(self._log, "wb")
            try:
                self._proc = subprocess.Popen(
                    args, cwd=str(self.dir), stdin=subprocess.DEVNULL,
                    stdout=logf, stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            finally:
                # 子进程已继承写句柄，父进程这份用完即关（此前每次启动泄一个句柄）
                logf.close()
            self._attach_kill_on_close(self._proc)
            # 有上限的轮询就绪检测（不固定硬等；进程提前退出立刻报错并带上日志尾巴）
            t0 = time.time()
            while time.time() - t0 < self.wait_s:
                if self.probe():
                    print(f"[llama] 就绪：{self.base_url}（{self.model.name}，"
                          f"{time.time() - t0:.1f}s）", flush=True)
                    return True
                if self._proc.poll() is not None:
                    tail = self._tail_log()
                    raise LlamaError(
                        f"llama-server 启动即退出（code {self._proc.returncode}）：\n{tail}")
                time.sleep(0.5)
            self.stop_server()
            raise LlamaError(
                f"llama-server 未在 {self.wait_s:.0f}s 内就绪（日志 {self._log}）")

    def _attach_kill_on_close(self, proc: subprocess.Popen) -> None:
        """把子进程加入 Job Object（KILL_ON_JOB_CLOSE）：本进程崩溃/被强杀时，
        句柄随进程关闭，llama-server（约 4 GB 显存）跟着被带走，不再变孤儿。

        与 audiocpp_backend 同一实现（纯 ctypes，失败时静默降级为旧行为）。
        """
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes

            class IO_COUNTERS(ctypes.Structure):
                # 注意：ctypes.wintypes **没有** ULONGLONG（实测 AttributeError，
                # 会让整段 Job Object 保护静默失效 → 父进程退出后 llama-server
                # 变孤儿、4 GB 显存不释放），必须用 ctypes.c_ulonglong。
                _fields_ = [(n, ctypes.c_ulonglong) for n in (
                    "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                    "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

            class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                    ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            k32 = ctypes.windll.kernel32
            job = k32.CreateJobObjectW(None, None)
            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = 0x2000   # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(
                    job, 9, ctypes.byref(info), ctypes.sizeof(info)):  # 9 = ExtendedLimitInformation
                raise OSError("SetInformationJobObject 失败")
            if not k32.AssignProcessToJobObject(job, proc._handle):
                raise OSError("AssignProcessToJobObject 失败")
            self._job_handle = job    # 句柄保持打开；进程结束由 OS 回收
        except Exception as e:
            print(f"[llama] Job Object 保护不可用（{e}），跳过", flush=True)

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
            self._job_handle = None   # 进程已死，关闭 Job 句柄（幂等，OS 兜底回收）
