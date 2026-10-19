"""回归测试: _fit_rev_bias_prefix 必须返回长度 == gen_end 的先验数组。

这个不变量是硬要求: 样式机按镜头段切 `rev_bias[lo:hi]` 再交给
`common.alternating_events`, 长度短了会广播失败
(ValueError: operands could not be broadcast together with shapes (744,) (488,)),
而引擎的异常处理会把整个引擎置为 state=error、停止出动作。

用真实会话轨道做 CPU 驱动的单元测试(不需要 GPU):
  第一次调用触发标定 -> 返回长度 900 的数组
  之后前缀增长但未到重标定间隔 -> 必须**补齐**到当前 gen_end

运行: <GTG venv python> test_rev_bias_pad.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "_third_party" / "GoblinTrainingGrounds"))

from stream_engine import ROW_HZ, StreamEngine      # noqa: E402

TRACKS = ROOT / "sessions" / "20261009_205246" / "tracks.npz"


def main() -> int:
    z = np.load(TRACKS)
    n = int(np.sum(z["times_ms"] > 0))
    trk = {k: z[k][:n].astype(np.float64)
           for k in ("vmarg", "rev_top", "rev_bot")}
    vel = np.nan_to_num(trk["vmarg"]) * 193.57327270507812

    eng = StreamEngine.__new__(StreamEngine)     # 只需状态, 不建 memmap/线程
    eng._trk = trk
    eng._rev_bias = 0.0
    eng._rev_bias_rows = 0
    eng.note = lambda s: print(f"    [note] {s}")

    print("前缀增长序列 -> 先验长度(必须等于 gen_end):")
    ok = True
    for rows in (450, 900, 1024, 1280, 2000, 4096, 9000, 20000, n):
        bias = eng._fit_rev_bias_prefix(rows, vel)
        got = np.shape(bias)[0] if np.ndim(bias) else "scalar(0)"
        good = (np.ndim(bias) == 0) or (int(np.shape(bias)[0]) == rows)
        ok &= bool(good)
        print(f"    gen_end={rows:<6} -> {str(got):<14} {'OK' if good else 'MISMATCH'}"
              f"  rev_bias_rows={eng._rev_bias_rows}")
        # 复现最初的崩溃场景: 按镜头段切片, 段长度必须能对上
        if np.ndim(bias):
            for lo, hi in ((0, rows // 3), (rows // 3, rows)):
                seg = np.asarray(bias)[lo:hi]
                assert len(seg) == hi - lo, f"切片长度不符: {len(seg)} != {hi - lo}"
    print("\n结果:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
