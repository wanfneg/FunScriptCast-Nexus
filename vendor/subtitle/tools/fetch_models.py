#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""下载 Qwen3-ASR 系列模型权重（ModelScope 国内源，可续传）

绕过 modelscope 1.39.1 的 bug：@dataclass(slots=True) 下 `_logged_out`
在 __post_init__ 里被读取时尚未初始化 → AttributeError。
把 load_token 换成空实现即可（只跳过本地登录态读取，不影响下载）。

用法：
  .venv/Scripts/python.exe fetch_models.py              # 全部
  .venv/Scripts/python.exe fetch_models.py 0.6B         # 只下含 "0.6B" 的
"""
from modelscope_hub.config import HubConfig

HubConfig.load_token = lambda self: None  # noqa: E731

from modelscope import snapshot_download  # noqa: E402

JOBS = [
    ("Qwen/Qwen3-ASR-0.6B", "models/Qwen3-ASR-0.6B"),
    ("Qwen/Qwen3-ForcedAligner-0.6B", "models/Qwen3-ForcedAligner-0.6B"),
    ("Qwen/Qwen3-ASR-1.7B", "models/Qwen3-ASR-1.7B"),
]

if __name__ == "__main__":
    import sys

    only = sys.argv[1:]
    for mid, dst in JOBS:
        if only and not any(o in mid for o in only):
            continue
        print("=== downloading", mid, "->", dst, flush=True)
        print(snapshot_download(mid, local_dir=dst), flush=True)
    print("ALL_DONE")
