# -*- coding: utf-8 -*-
"""编码器吞吐基准：CUDA vs DirectML。

用法：
    python bench_enc.py [cuda|dml|cpu] [--enc vjepa_pca.onnx 路径]

默认使用仓库内 vendor/vision/bundles/v0.6.0/vjepa_pca.onnx。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
VISION = REPO / "vendor" / "vision"
DEFAULT_ENC = VISION / "bundles" / "v0.6.0" / "vjepa_pca.onnx"

ap = argparse.ArgumentParser(description="编码器吞吐基准")
ap.add_argument("ep", nargs="?", default="cpu", choices=["cuda", "dml", "cpu"])
ap.add_argument("--enc", default=str(DEFAULT_ENC), help="vjepa_pca.onnx 路径")
a = ap.parse_args()

ep = a.ep
enc = str(Path(a.enc))
if not Path(enc).is_file():
    raise SystemExit(f"编码器不存在：{enc}")
prov = {"cuda": ["CUDAExecutionProvider", "CPUExecutionProvider"],
        "dml": ["DmlExecutionProvider", "CPUExecutionProvider"],
        "cpu": ["CPUExecutionProvider"]}[ep]
print("ort", ort.__version__, "requested", ep, "available", ort.get_available_providers())
so = ort.SessionOptions()
so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
s = ort.InferenceSession(enc, so, providers=prov)
print("session providers:", s.get_providers())
x = np.random.randint(0, 255, (1, 32, 384, 384, 3), dtype=np.uint8)
for _ in range(3):
    s.run(None, {"frames": x})
n = 40
t0 = time.time()
for _ in range(n):
    s.run(None, {"frames": x})
el = time.time() - t0
print("%s: %.2f windows/s = %.1f fps(30) = %.2fx realtime  (%.3f s/window)" %
      (ep, n / el, n * 16 / el, (n * 16 / 30) / el, el / n))
