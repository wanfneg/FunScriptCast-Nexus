# -*- coding: utf-8 -*-
"""视觉后端开关自检：用假引擎模块验证选择与参数传递，不加载 torch / onnxruntime。"""
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vendor" / "vision"))

# 顶替真引擎，避免为一次选择测试导入 torch / onnxruntime
_fake_stream = types.ModuleType("stream_engine")


class _FakeStream:
    def __init__(self, *a, **kw):
        self.args = a
        self.kw = kw


class _FakeOnnx(_FakeStream):
    pass


_fake_stream.StreamEngine = _FakeStream
_fake_onnx = types.ModuleType("onnx_stream")
_fake_onnx.OnnxStreamEngine = _FakeOnnx
sys.modules["stream_engine"] = _fake_stream
sys.modules["onnx_stream"] = _fake_onnx

import service  # noqa: E402

service._release_cuda = lambda: None      # close() 不碰 torch
service._gpu_used_gb = lambda: None       # health() 不碰 torch

ENVS = ("FSC_VISION_BACKEND", "FSC_VISION_BUNDLE")
ok = True
_tmpdirs = []


def check(name, cond, detail=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("  " + detail if detail else ""))
    ok = ok and cond


def make(cfg=None, env=None):
    """按指定 cfg/env 构造服务；构造完即恢复环境变量（选择在 __init__ 已完成）。"""
    saved = {k: os.environ.pop(k, None) for k in ENVS}
    for k, v in (env or {}).items():
        os.environ[k] = v
    try:
        cache = tempfile.mkdtemp(prefix="fsc_vision_backend_")
        _tmpdirs.append(cache)
        c = {"paths": {"cache_dir": cache}}
        c.update(cfg or {})
        return service.VisionService(c)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# 1) 默认走 torch，行为不变
svc = make()
check("默认后端 = torch", svc.backend == "torch", svc.backend)
eng = svc._build_engine("v.mp4", 1000.0)
check("torch 后端构造 StreamEngine", type(eng) is _FakeStream, type(eng).__name__)
check("torch 后端传入 ckpt/device", "ckpt" in eng.kw and eng.kw.get("device") == "cuda",
      str(sorted(eng.kw)))

# 2) config 选 onnx
svc = make({"engine": {"backend": "onnx"}})
check("config.engine.backend=onnx", svc.backend == "onnx", svc.backend)
eng = svc._build_engine("v.mp4", 1000.0)
check("onnx 后端构造 OnnxStreamEngine", type(eng) is _FakeOnnx, type(eng).__name__)
bundle = Path(str(eng.kw.get("bundle")))
check("onnx 默认 bundle = bundles/v0.6.0",
      bundle.name == "v0.6.0" and bundle.parent.name == "bundles", str(bundle))
check("onnx 后端不传 ckpt", "ckpt" not in eng.kw)

# 3) 环境变量优先于 config
svc = make({"engine": {"backend": "torch"}}, {"FSC_VISION_BACKEND": "onnx"})
check("FSC_VISION_BACKEND 覆盖 config", svc.backend == "onnx", svc.backend)

# 4) 非法值回退 torch
svc = make({"engine": {"backend": "bogus"}})
check("非法后端回退 torch", svc.backend == "torch", svc.backend)

# 5) bundle 环境变量覆盖
svc = make({"engine": {"backend": "onnx"}},
           {"FSC_VISION_BUNDLE": str(ROOT / "tmp-bundle")})
eng = svc._build_engine("v.mp4", 1000.0)
check("FSC_VISION_BUNDLE 覆盖默认路径",
      Path(str(eng.kw.get("bundle"))) == (ROOT / "tmp-bundle"), str(eng.kw.get("bundle")))

# 6) /health 暴露后端与 bundle
svc = make({"engine": {"backend": "onnx"}})
h = svc.health()
check("/health 含 backend=onnx", h.get("backend") == "onnx", str(h.get("backend")))
check("/health 含 bundle 路径", bool(h.get("bundle")), str(h.get("bundle")))
svc = make()
check("/health torch 时 bundle 为空", svc.health().get("bundle") is None,
      str(svc.health().get("bundle")))

for d in _tmpdirs:
    shutil.rmtree(d, ignore_errors=True)
print()
print("结果: " + ("ALL PASS" if ok else "FAIL"))
sys.exit(0 if ok else 1)
