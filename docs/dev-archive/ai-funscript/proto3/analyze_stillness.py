# -*- coding: utf-8 -*-
"""静止段（不该动的地方）分析：两部片子交叉验证。

核心问题：人工脚本在哪些区间**完全没有动作**，模型在这些区间输出了多少动作。

判定"人工脚本静止"的方法（三种口径，避免单一阈值偏见）：
  A. 长平台：相邻两个关键帧间隔 >= 3s 且位置变化 <= 2 单位
     （作者明确写了"保持不动"）
  B. 低速平滑：0.5s 平滑速度 < 阈值（覆盖慢速微动之外的区间）
  C. 头尾空档：脚本开始前 / 结束后

用法：
    python analyze_stillness.py
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

ROW = 30.0
CMP = Path(r"E:/Development/AI Funscript/proto3/compare")
FRESH = CMP / "fresh"

CASES = [
    ("K1cztm", Path(r"E:/testvideo/funscript test/K1cztm.funscript"),
     FRESH / "K1cztm_tracks.npz"),
    ("K1drsp", Path(r"E:/testvideo/funscript test/K1drsp.funscript"),
     FRESH / "K1drsp_tracks.npz"),
]


def load_actions(p: Path):
    a = json.loads(p.read_text(encoding="utf-8"))["actions"]
    t = np.array([x["at"] for x in a], float) / 1000.0
    v = np.array([x["pos"] for x in a], float)
    return t, v


def platform_mask(t, v, min_gap=3.0, max_delta=2.0):
    """A 口径：长平台（作者明确保持不动）。"""
    n = int(t[-1] * ROW) + 1
    m = np.zeros(n, bool)
    for i in range(1, len(t)):
        dt = t[i] - t[i - 1]
        if dt >= min_gap and abs(v[i] - v[i - 1]) <= max_delta:
            m[int(t[i - 1] * ROW):int(t[i] * ROW)] = True
    return m


def smooth_speed(t, v, win_s=0.5):
    """B 口径：逐行速度（0.5s 平滑）。"""
    n = int(t[-1] * ROW) + 1
    rows = np.arange(n) / ROW
    pos = np.interp(rows, t, v)
    sp = np.abs(np.gradient(pos, 1.0 / ROW))
    k = max(1, int(win_s * ROW))
    return np.convolve(sp, np.ones(k) / k, mode="same"), rows, pos


def main() -> None:
    for name, fs, trk_path in CASES:
        if not trk_path.is_file():
            print(f"[{name}] 缺轨道：{trk_path}")
            continue
        t, v = load_actions(fs)
        trk = np.load(trk_path)
        vm = np.abs(trk["vmarg"])
        n = min(len(vm), int(t[-1] * ROW) + 1)
        vm = vm[:n]

        plat = platform_mask(t, v)[:n]
        spd, rows, pos = smooth_speed(t, v)
        spd = spd[:n]

        # 模型输出动作
        mout = trk_path.parent / (trk_path.name.replace("_tracks.npz", "_stream.funscript"))
        if not mout.is_file():
            mout = CMP / f"{name}_stream.funscript"
        mt, mv = load_actions(mout)
        mrow = np.clip((mt * ROW).astype(int), 0, n - 1)

        print("=" * 72)
        print(f"{name}: 人工 {len(t)} 点 / {t[-1]:.0f}s；模型 {len(mt)} 点")
        print(f"  A 长平台（>=3s 且 Δpos<=2）: {100*plat.mean():.1f}% 时长")
        for thr in (2.0, 5.0, 10.0, 20.0):
            print(f"  B 平滑速度 < {thr:>4.0f} pos/s      : {100*(spd<thr).mean():.1f}% 时长")

        print()
        print("  模型动作落在各类静止区的比例：")
        for label, mask in [
            ("A 长平台", plat),
            ("B <2", spd < 2),
            ("B <5", spd < 5),
            ("B <10", spd < 10),
            ("B <20", spd < 20),
        ]:
            hit = mask[mrow]
            print(f"    {label:<10} 静止占比 {100*mask.mean():5.1f}%  "
                  f"→ 模型命中 {100*hit.mean():5.1f}%  ({hit.sum()}/{len(mrow)})")

        # 关键指标：模型在"长平台"里发了多少动作
        # 人工脚本在平台里本来有 2 个端点，模型理应也少发
        plat_rows = np.where(plat)[0]
        if len(plat_rows):
            # 每 100 行（3.3s）的平均动作数
            seg = np.zeros(n, int)
            np.add.at(seg, mrow, 1)
            inplat = seg[plat]
            outplat = seg[~plat]
            print()
            print(f"  每秒动作数：平台内 {inplat.mean()*ROW:.2f}  "
                  f"非平台 {outplat.mean()*ROW:.2f}  "
                  f"（人工脚本整体 {len(t)/t[-1]:.2f}）")

        print()
        print("  |vmarg| 在各静止区的分布（越接近 0 说明模型确实认为静止）：")
        for label, mask in [("A 长平台", plat), ("B <5", spd < 5), ("运动区", spd >= 20)]:
            if mask.sum() == 0:
                continue
            q = np.percentile(vm[mask], [50, 90])
            print(f"    {label:<10} p50={q[0]:.4f}  p90={q[1]:.4f}  n={mask.sum()}")
        print()


if __name__ == "__main__":
    main()
