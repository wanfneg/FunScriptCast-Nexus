"""flush 内部微基准 —— 把 flush_cpu(实测 58.9s/窗口)拆成 纯CPU vs GPU同步等待。

在空闲 GPU 上单独测: ① Encoder.tokens 固有速率 ② basis 投影+下载 ③
argsort/int8 量化 ④ memmap 写盘 ⑤ stack+H2D。与引擎内实测相减即争用等待。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

GTG = Path(__file__).resolve().parent.parent / "_third_party" / "GoblinTrainingGrounds"
sys.path.insert(0, str(GTG))

import torch  # noqa: E402
from goblintrain import project as gproject  # noqa: E402
from goblintrain.encoders import Encoder  # noqa: E402

DEV = "cuda"


def bench(name, iters, fn):
    fn()  # warmup
    torch.cuda.synchronize()
    t0 = time.perf_counter()
    for _ in range(iters):
        fn()
    torch.cuda.synchronize()
    dt = (time.perf_counter() - t0) / iters
    print(f"{name:<42} {dt*1000:9.2f} ms/次")
    return dt


def main() -> None:
    print(f"device={torch.cuda.get_device_name(0)}")
    enc = Encoder(gproject.PERCEPTION["encoder"], DEV)
    print(f"encoder={enc}")

    # ① tokens 固有速率(批 = 2 窗口 × 32 帧, 与引擎 WINS_PER_BATCH=2 相同;
    #    引擎在 torch.no_grad() 下调用, 这里必须一致)
    x = torch.randint(0, 256, (64, 384, 384, 3), dtype=torch.uint8, device=DEV)
    with torch.no_grad():
        dt_tok = bench("① enc.tokens (64帧批, 固有)", 20, lambda: enc.tokens(x))
    fps_enc = 64.0 / dt_tok
    print(f"   → 固有 {fps_enc:.0f} 帧/s = {fps_enc/30.0:.1f}x 实时(纯编码)\n")

    # ② basis 投影 + 下载 + fp16 转换(每窗口)
    y = torch.randn(2, 16, 24, 24, 64, device=DEV)
    mu = torch.randn(24, 24, 64, device=DEV)
    W = torch.randn(64, 64, device=DEV)
    def proj():
        for i in range(2):
            ya = (y[i] - mu) @ W
            ya.permute(0, 3, 1, 2).cpu().numpy().astype(np.float16)
    bench("② basis投影+cpu下载 (每批2窗口)", 50, proj)

    # ③ argsort + fancy index + int8 量化(每组)
    feats = np.random.randn(64, 16, 24, 24).astype(np.float16)
    order = np.argsort(np.arange(64), kind="stable")
    def quant():
        f = feats[order].astype(np.float32)
        np.clip(np.round(f / (8.0 / 127.0)), -127, 127).astype(np.int8)
    bench("③ argsort+int8量化 (每组64行)", 200, quant)

    # ④ memmap 写盘(E:, 与引擎同盘同形状)
    lat_path = Path(__file__).with_name("_bench_lat.bin")
    lat = np.memmap(lat_path, dtype=np.int8, mode="w+",
                    shape=(40967, 64, 24, 24))
    q = np.zeros((64, 64, 24, 24), np.int8)
    def wr():
        lat[0:64] = q[:64]
    bench("④ memmap写 64行=2.36MB (同盘E:)", 73, wr)
    del lat
    lat_path.unlink(missing_ok=True)

    # ⑤ np.stack 4窗口 + concat + H2D(每批)
    frames = [np.zeros((384, 384, 3), np.uint8) for _ in range(128)]
    def stack_h2d():
        wins = [np.stack(frames[i*32:(i+1)*32]) for i in range(2)]
        x2 = torch.from_numpy(np.concatenate(wins)).to(DEV)
    bench("⑤ stack2×32帧+concat+H2D (每批)", 100, stack_h2d)

    # 引擎每组 = 2批tokens + 2×② + 1×③ + 1×④ + 2×⑤
    per_group = 2 * dt_tok + 2 * 0 + 0 + 0 + 0
    print(f"\n每组(64行)合成: 2×tokens={2*dt_tok:.3f}s → 引擎 73 组固有 "
          f"{73*2*dt_tok:.1f}s vs 引擎内实测 flush总成 121.8s")


if __name__ == "__main__":
    main()
