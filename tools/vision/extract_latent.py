# -*- coding: utf-8 -*-
"""只做 latent 提取（不跑 TCN 解码），把 .lat.bin 完整填满。

为什么单独写：
  StreamEngine 把"提取"和"流式解码"放在同一个循环里，而流式解码每次发布都
  重跑全前缀样式（O(n²)）——1300s 的片子在 247s 处几乎停住，留下一个
  **大小正确但后半全是零** 的 .lat.bin（2026-10-18 实测踩到）。
  本脚本只跑编码器，一次填满 latent；之后的轨道导出用 dump_tracks_full.py
  （一次前向 + 一次样式，十几秒）。

实现上**直接复用 StreamEngine._flush_group**（而不是复制一份编码逻辑）：
那份代码里有 batch 维度、PCA 白化、int8 量化、时间轴回填、h0 增量缓存，
复制必然走样。这里只把引擎的 _stream 循环换成"只提取、不解码"。

用法：
    python extract_latent.py <video> <out前缀>
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
REPO = HERE.parents[1]
VISION = REPO / "vendor" / "vision"
if str(VISION) not in sys.path:
    sys.path.insert(0, str(VISION))

from stream_engine import (StreamEngine, FPS_GRID, K, GROUP,  # noqa: E402
                           ROW_HZ)
from stream_engine import _ffmpeg_decode  # noqa: E402


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    video = sys.argv[1]
    out = Path(sys.argv[2])
    out.parent.mkdir(parents=True, exist_ok=True)

    import subprocess
    pr = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                         "format=duration", "-of", "default=nw=1:nk=1", video],
                        capture_output=True, text=True)
    dur_s = float((pr.stdout or "0").strip() or 0)
    if dur_s <= 0:
        raise SystemExit(f"ffprobe 读不到时长：{pr.stderr[:300]}")

    eng = StreamEngine(video, duration_ms=dur_s * 1000.0, device="cuda",
                       ckpt=None, on_note=lambda s: print("[engine]", s, flush=True),
                       cache_dir=str(out.parent))

    # 复用引擎的初始化（转码 + 编码器 + PCA），但不启动它的 _stream
    import torch
    from goblintrain import jepa_infer, project as gproject
    from goblintrain.encoders import Encoder
    from goblintrain.extract import Basis

    print("[extract] 加载编码器…", flush=True)
    enc = Encoder(gproject.PERCEPTION["encoder"], eng.device)
    basis = Basis(gproject.PERCEPTION, eng.device)

    print("[extract] 转码/复用 480p30…", flush=True)
    trans = eng._transcode_official()

    print(f"[extract] 开始提取，目标 {eng.total_rows} 行", flush=True)
    t0 = time.time()
    buf, tbuf = [], []
    n_rows = 0
    # 与引擎完全同一条解码链（384×384、pre_scaled=True）
    gen_iter, proc = _ffmpeg_decode(str(trans), FPS_GRID, 0.0, 384, 384,
                                    pre_scaled=True)
    for t_ms, frame in gen_iter:
        buf.append(frame)
        tbuf.append(t_ms)
        if len(buf) - K < GROUP:
            continue
        n = eng._flush_group(buf, tbuf, enc, basis, torch)
        if n:
            n_rows += n
            del buf[:GROUP], tbuf[:GROUP]
            if n_rows % (GROUP * 20) == 0 or n_rows >= eng.total_rows:
                pct = 100.0 * n_rows / eng.total_rows
                print(f"[extract] {n_rows}/{eng.total_rows} ({pct:.1f}%) "
                      f"{time.time()-t0:.0f}s", flush=True)
        if n_rows >= eng.total_rows:
            break

    # 尾帧补齐（官方 extract.end 的等价）。
    # _flush_group 内部按 N_ALIGN 组窗口，每个窗口要 CLIP_LEN=32 帧；
    # 帧数不足时 v.view(m,32,3,384,384) 会因元素数不匹配而抛错。
    # 所以先把缓冲补到 GROUP+K 帧（重复末帧），再调。
    if buf and n_rows < eng.total_rows:
        need = GROUP + K
        while len(buf) < need:
            buf.append(buf[-1])
            tbuf.append(tbuf[-1] + 1000.0 / FPS_GRID)
        n = eng._flush_group(buf, tbuf, enc, basis, torch)
        if n:
            n_rows += n

    try:
        proc.terminate()
    except OSError:
        pass
    eng.lat.flush()
    pct = 100.0 * n_rows / eng.total_rows
    print(f"[extract] 完成 {n_rows}/{eng.total_rows} ({pct:.1f}%) "
          f"用时 {time.time()-t0:.0f}s", flush=True)
    if pct < 98:
        print("[extract] ⚠ 未填满，导出会读到零区", flush=True)

    # 验证：抽样检查尾部是否非零
    tail = np.array(eng.lat[max(0, n_rows - 200):n_rows])
    print(f"[extract] 尾部 200 行非零率 {100*(tail != 0).mean():.0f}%", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
