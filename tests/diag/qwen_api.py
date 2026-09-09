# -*- coding: utf-8 -*-
"""检查 qwen_asr 的 from_pretrained 是否支持量化配置传入。"""
import inspect
import sys

sys.stdout.reconfigure(encoding="utf-8")
from qwen_asr import Qwen3ASRModel  # noqa: E402

sig = inspect.signature(Qwen3ASRModel.from_pretrained)
print("from_pretrained 参数：")
for name, p in sig.parameters.items():
    default = "<required>" if p.default is inspect.Parameter.empty else repr(p.default)
    print(f"  {name} = {default}")

print("\n--- 源码片段 ---")
src = inspect.getsource(Qwen3ASRModel.from_pretrained)
print(src[:2000])
