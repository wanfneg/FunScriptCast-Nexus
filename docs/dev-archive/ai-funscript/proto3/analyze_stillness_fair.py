# -*- coding: utf-8 -*-
"""静止段"幻影动作"的公平口径复核 + 静止门可行性评估。

为什么重写（2026-10-19）：
  上一版 STILLNESS_ANALYSIS.md 用"流式旧产物(10-09) vs 单shot离线"得出
  "流式 5.7x 幻影"，三个口径问题导致结论失真：
    1) 流式参照是 10-09 的旧产物，比当前引擎落后 8 天的修复；
    2) 单 shot 离线参照（dump_tracks_full 用 edges=[0,total_rows]）密度只有
       官方 1/3，不是等价的"离线"；
    3) 人工脚本不覆盖片头（K1drsp 262s / K1cztm 350s 之前），把那段时间的
       动作算成"幻影"是错的。
  本脚本改用：
    * 共同窗口 [人工起点, min(输出末, 人工末)]
    * 三类对照：人工 / 官方 v0.6.0 / 当前流式 / 旧流式 / 单shot 离线
    * 静止定义：人工相邻关键帧 >=3s 且 |Δpos| <=2（作者显式"保持不动"）

用法:
    python analyze_stillness_fair.py
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

ROW = 30.0
BASE = Path(__file__).resolve().parent
HAND_DIR = Path(r"E:/testvideo/funscript test")

HANDS = {
    "K1drsp": HAND_DIR / "K1drsp.funscript",
    "K1cztm": HAND_DIR / "K1cztm.funscript",
}
ARTIFACTS = {
    "K1drsp": [
        ("当前流式会话 10-17", BASE / "sessions/20261017_191747/actions.json", True),
        ("官方 v0.6.0", BASE / "compare/K1drsp.funscript", False),
        ("新离线(修正) 10-19", BASE / "compare/fresh2/K1drsp_stream.funscript", False),
        ("旧离线(坏口径) 10-18", BASE / "compare/fresh/K1drsp_stream.funscript", False),
        ("流式旧 10-09", BASE / "compare/K1drsp_stream.funscript", False),
    ],
    "K1cztm": [
        ("官方 v0.6.0", BASE / "compare/K1cztm.funscript", False),
        ("新离线(修正) 10-19", BASE / "compare/fresh2/K1cztm_stream.funscript", False),
        ("旧离线(坏口径) 10-18", BASE / "compare/fresh/K1cztm_stream.funscript", False),
        ("流式旧 fullprefix 10-09", BASE / "compare/K1cztm_stream_fullprefix.funscript", False),
    ],
}


def load_actions(path: Path) -> tuple[np.ndarray, np.ndarray]:
    a = json.loads(Path(path).read_text(encoding="utf-8"))["actions"]
    return (np.array([x["at"] for x in a], float) / 1000.0,
            np.array([x["pos"] for x in a], float))


def platform_mask(t: np.ndarray, v: np.ndarray,
                  min_gap: float = 3.0, max_delta: float = 2.0) -> np.ndarray:
    n = int(t[-1] * ROW) + 1
    m = np.zeros(n, bool)
    for i in range(1, len(t)):
        if t[i] - t[i - 1] >= min_gap and abs(v[i] - v[i - 1]) <= max_delta:
            m[int(t[i - 1] * ROW):int(t[i] * ROW)] = True
    return m


def rates(t, plat, w0, w1):
    """在 [w0, w1] 内统计平台内/外每秒动作数。"""
    r0, r1 = int(w0 * ROW), int(w1 * ROW)
    pm = plat[r0:r1]
    inw = (t >= w0) & (t <= w1)
    if not inw.any():
        return None
    idx = np.clip((t[inw] * ROW).astype(int) - r0, 0, max(0, len(pm) - 1))
    pin = pm[idx]
    plat_s = pm.sum() / ROW
    out_s = (~pm).sum() / ROW
    return {
        "n": int(inw.sum()),
        "overall": inw.sum() / max(1e-9, (w1 - w0)),
        "plat_s": plat_s,
        "out_s": out_s,
        "in_plat": int(pin.sum()),
        "r_plat": pin.sum() / plat_s if plat_s else 0.0,
        "r_out": (~pin).sum() / out_s if out_s else 0.0,
    }


def main() -> int:
    print("=" * 96)
    for name, hand in HANDS.items():
        ht, hv = load_actions(hand)
        plat = platform_mask(ht, hv)
        h0, h1 = ht[0], ht[-1]
        print(f"\n### {name}  人工 {len(ht)} 点 / {h0:.0f}-{h1:.0f}s "
              f"(整体 {len(ht)/(h1-h0):.2f}/s) | 长平台占比 {100*plat.mean():.1f}%")
        print(f"共同窗口 [{h0:.0f}s, min(输出末,{h1:.0f}s)]")
        hdr = (f"{'输出':26} {'窗口':>15} {'总数':>6} {'整体/s':>7} "
               f"{'平台内/s':>9} {'平台外/s':>9} {'片头/s':>7}")
        print(hdr)
        print("-" * len(hdr))
        for label, path, _ in ARTIFACTS[name]:
            if not Path(path).is_file():
                print(f"{label:<26} 缺失")
                continue
            t, v = load_actions(path)
            w1 = min(t[-1], h1)
            r = rates(t, plat, h0, w1)
            if r is None:
                continue
            head = t < h0
            print(f"{label:<26} {h0:>6.0f}-{w1:>6.0f} {r['n']:>6} {r['overall']:>7.2f} "
                  f"{r['r_plat']:>9.2f} {r['r_out']:>9.2f} "
                  f"{head.sum()/h0:>7.2f}")
        print("  * 片头 = 人工脚本未覆盖区，不能算幻影，仅作参照")

    # ---- 静止门评估（只对带轨道的当前会话）----
    sess = BASE / "sessions/20261017_191747"
    if (sess / "tracks.npz").is_file():
        print("\n" + "=" * 96)
        ht, hv = load_actions(HANDS["K1drsp"])
        plat = platform_mask(ht, hv)
        h0, h1 = ht[0], ht[-1]
        at, av = load_actions(sess / "actions.json")
        trk = np.load(sess / "tracks.npz")
        # 速度必须换到 pos/s（乘 v_std），否则静止门永远为真
        vm = np.nan_to_num(np.abs(trk["vmarg"]), nan=0.0) * 193.57327270507812
        tms = np.asarray(trk["times_ms"], float) / 1000.0
        w1 = min(at[-1], h1)
        p50 = float(np.median(vm[(tms >= h0) & (tms <= w1) & (vm > 0)]))
        print(f"\n### 静止门可行性（会话 20261017_191747，|vmarg| p50={p50:.4f}）")
        print("连续静止段门：行级 |vmarg| < k*p50 且持续 >=1s 的 run 内抑制动作")
        print(f"{'k':>5} {'阈值':>7} {'平台召回%':>9} {'运动误报%':>9} {'平台内/s':>9} {'平台外/s':>9}")
        vwin = np.convolve(vm, np.ones(9) / 9, mode="same")
        r0, r1 = int(h0 * ROW), int(w1 * ROW)
        pm = plat[r0:r1]
        plat_s = pm.sum() / ROW
        out_s = (~pm).sum() / ROW
        inw = (at >= h0) & (at <= w1)
        pin = pm[np.clip((at[inw] * ROW).astype(int) - r0, 0, len(pm) - 1)]
        for k in (0.3, 0.5, 0.75, 1.0, 1.5):
            thr = k * p50
            still = vwin < thr
            d = np.diff(np.concatenate([[0], still.astype(np.int8), [0]]))
            keep = np.zeros_like(still)
            for s, e in zip(np.where(d == 1)[0], np.where(d == -1)[0]):
                if e - s >= int(1.0 * ROW):
                    keep[s:e] = True
            sw = keep[r0:r1]
            tw = keep[np.clip((at * ROW).astype(int), 0, len(keep) - 1)][inw]
            print(f"{k:>5.2f} {thr:>7.3f} {100*sw[pm].mean():>9.1f} {100*sw[~pm].mean():>9.1f} "
                  f"{int(((~tw) & pin).sum())/plat_s:>9.2f} {int(((~tw) & ~pin).sum())/out_s:>9.2f}")
        print(f"  当前基准: 平台内 {int(pin.sum())/plat_s:.2f}/s, 平台外 {int((~pin).sum())/out_s:.2f}/s")
        print(f"  人工基准: 平台内 0.30/s, 平台外 {len(ht)/(h1-h0):.2f}/s")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
