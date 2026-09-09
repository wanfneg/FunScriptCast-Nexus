# -*- coding: utf-8 -*-
"""只打印 GGUF 的关键元数据（跳过巨大的 tokenizer 词表）。

用法： .venv\\Scripts\\python.exe tests\\diag\\gguf_info.py <模型.gguf>
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

SKIP_PREFIX = ("tokenizer.ggml.tokens", "tokenizer.ggml.scores", "tokenizer.ggml.token_type",
               "tokenizer.ggml.merges")


def read_str(f):
    n = struct.unpack("<Q", f.read(8))[0]
    return f.read(n).decode("utf-8", "replace")


def read_val(f, t):
    simple = {0: ("<B", 1), 1: ("<b", 1), 2: ("<H", 2), 3: ("<h", 2),
              4: ("<I", 4), 5: ("<i", 4), 6: ("<f", 4), 7: ("<?", 1),
              10: ("<Q", 8), 11: ("<q", 8), 12: ("<d", 8)}
    if t in simple:
        fmt, _ = simple[t]
        return struct.unpack(fmt, f.read(struct.calcsize(fmt)))[0]
    if t == 8:
        return read_str(f)
    if t == 9:
        et = struct.unpack("<I", f.read(4))[0]
        n = struct.unpack("<Q", f.read(8))[0]
        return f"<array[{et}] len={n}>", n
    raise ValueError(t)


def main() -> int:
    p = Path(sys.argv[1])
    with p.open("rb") as f:
        magic = f.read(4)
        ver = struct.unpack("<I", f.read(4))[0]
        n_tensors = struct.unpack("<Q", f.read(8))[0]
        n_kv = struct.unpack("<Q", f.read(8))[0]
        print(f"文件 {p.name}  大小 {p.stat().st_size/1024**2:.1f} MB")
        print(f"GGUF version={ver}  tensors={n_tensors}  kv={n_kv}")
        for _ in range(n_kv):
            k = read_str(f)
            vt = struct.unpack("<I", f.read(4))[0]
            if k.startswith(SKIP_PREFIX):
                # 跳过巨大数组：读元素个数后按元素类型逐个吃掉
                if vt == 9:
                    et = struct.unpack("<I", f.read(4))[0]
                    n = struct.unpack("<Q", f.read(8))[0]
                    for _i in range(n):
                        read_val(f, et)
                    continue
                v = read_val(f, vt)
                continue
            v = read_val(f, vt)
            if isinstance(v, tuple):
                v = v[0]
            print(f"  {k} = {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
