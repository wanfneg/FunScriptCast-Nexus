# -*- coding: utf-8 -*-
"""把静止门应用到生成脚本，并与人工脚本逐项对比。

用法：
    python gate_and_compare.py <前缀> [<前缀> ...]
    例：python gate_and_compare.py ../docs/dev-archive/ai-funscript/results/K1cztm

两种门控：
  严格  conf>=0.70 且 |vmarg|>=50（任一项不达标就丢弃）
  温和  conf>=0.70 或 |vmarg|>=50（两项都低才丢弃）

产物：<prefix>_stream_gated.funscript / _stream_gated_gentle.funscript / _compare.png
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from still_gate import signals, gate_actions  # noqa: E402

ROW = 30.0
# 人工脚本（真值）目录：外部测试素材，默认 E:/testvideo/funscript test，
# 可用环境变量 FSC_TESTVIDEO 覆盖。
HAND_DIR = Path(os.environ.get("FSC_TESTVIDEO", r"E:/testvideo/funscript test"))
HAND = {"K1cztm": HAND_DIR / "K1cztm.funscript",
        "K1drsp": HAND_DIR / "K1drsp.funscript"}


def load_actions(p):
    a = json.loads(Path(p).read_text(encoding="utf-8"))["actions"]
    return sorted(a, key=lambda x: x["at"])


def ta(actions):
    return (np.array([x["at"] for x in actions], float) / 1000.0,
            np.array([x["pos"] for x in actions], float))


def smooth(x, k):
    k = max(1, int(k)); return np.convolve(x, np.ones(k) / k, mode="same")


def platmask(t, v, g=3.0, d=2.0):
    n = int(t[-1] * ROW) + 1; m = np.zeros(n, bool)
    for i in range(1, len(t)):
        if t[i] - t[i - 1] >= g and abs(v[i] - v[i - 1]) <= d:
            m[int(t[i - 1] * ROW):int(t[i] * ROW)] = True
    return m


def recall(gen_t, ref_t, tol):
    if len(gen_t) == 0:
        return 0.0
    hits = 0
    for t in ref_t:
        i = int(np.searchsorted(gen_t, t))
        cand = [j for j in (i - 1, i) if 0 <= j < len(gen_t)]
        if cand and min(abs(gen_t[j] - t) for j in cand) <= tol:
            hits += 1
    return hits / len(ref_t)


def precision(gen_t, ref_t, tol):
    if len(gen_t) == 0:
        return 0.0
    hits = 0
    for t in gen_t:
        i = int(np.searchsorted(ref_t, t))
        cand = [j for j in (i - 1, i) if 0 <= j < len(ref_t)]
        if cand and min(abs(ref_t[j] - t) for j in cand) <= tol:
            hits += 1
    return hits / len(gen_t)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    report = {}
    for arg in sys.argv[1:]:
        prefix = Path(arg); name = prefix.name
        trk = np.load(str(prefix) + "_tracks.npz")
        meta = json.loads(Path(str(prefix) + "_meta.json").read_text(encoding="utf-8"))
        vm, conf = signals(trk, meta["v_std"])
        times = np.asarray(trk["times_ms"], float) / 1000.0
        raw = load_actions(str(prefix) + "_stream.funscript")
        strict, st_s = gate_actions(raw, times, vm, conf, conf_thr=0.70, vm_thr=50.0, mode="and")
        balanced, st_b = gate_actions(raw, times, vm, conf, conf_thr=0.70, vm_thr=50.0,
                                      mode="combo", score_thr=0.70)
        gentle, st_g = gate_actions(raw, times, vm, conf, conf_thr=0.70, vm_thr=50.0, mode="or")
        Path(str(prefix) + "_stream_gated.funscript").write_text(
            json.dumps({"actions": balanced}, ensure_ascii=False), encoding="utf-8")
        Path(str(prefix) + "_stream_gated_strict.funscript").write_text(
            json.dumps({"actions": strict}, ensure_ascii=False), encoding="utf-8")
        Path(str(prefix) + "_stream_gated_gentle.funscript").write_text(
            json.dumps({"actions": gentle}, ensure_ascii=False), encoding="utf-8")

        ht, hv = ta(load_actions(HAND[name]))
        pm_full = platmask(ht, hv)
        n = len(times); plat = np.zeros(n, bool)
        mm = min(n, len(pm_full)); plat[:mm] = pm_full[:mm]
        pos = np.interp(np.arange(n) / ROW, ht, hv)
        spd = smooth(np.abs(np.gradient(pos, 1.0 / ROW)), 0.5 * ROW)
        cov = (times >= ht[0]) & (times <= ht[-1])
        still = cov & plat
        r0, r1 = int(ht[0] * ROW), int(ht[-1] * ROW)
        pm = plat[r0:r1]; ps = pm.sum() / ROW; os_ = (~pm).sum() / ROW

        def row_of(acts, label, still_per_s=None, motion_per_s=None):
            t, v = ta(acts)
            inw = (t >= ht[0]) & (t <= ht[-1])
            idx = np.clip((t * ROW).astype(int), 0, n - 1)
            pin = plat[idx]
            span = max(1e-9, min(t[-1], ht[-1]) - ht[0]) if len(t) else 1.0
            return {
                "label": label, "n": int(len(t)),
                "overall": round(float(len(t) / span), 2) if len(t) else 0.0,
                "still_per_s": round(float((inw & pin).sum() / ps), 2) if still_per_s is None else still_per_s,
                "motion_per_s": round(float((inw & ~pin).sum() / os_), 2) if motion_per_s is None else motion_per_s,
                "recall_100ms": round(recall(t, ht, 0.10), 3),
                "recall_200ms": round(recall(t, ht, 0.20), 3),
                "precision_100ms": round(precision(t, ht, 0.10), 3),
            }

        manual = {"label": "人工", "n": len(ht),
                  "overall": round(float(len(ht) / (ht[-1] - ht[0])), 2),
                  "still_per_s": 0.30,
                  "motion_per_s": round(float(len(ht) / (ht[-1] - ht[0])), 2),
                  "recall_100ms": 1.0, "recall_200ms": 1.0, "precision_100ms": 1.0}
        r_raw = row_of(raw, "生成(未门控)")
        r_s = row_of(strict, "生成(严格门)")
        r_b = row_of(balanced, "生成(平衡门)")
        r_g = row_of(gentle, "生成(温和门)")
        report[name] = {"manual": manual, "raw": r_raw, "strict": r_s,
                        "balanced": r_b, "gentle": r_g,
                        "stats_balanced": st_b, "stats_strict": st_s, "stats_gentle": st_g}

        print("=" * 100)
        print(f"### {name}: 人工 {len(ht)} 点 | 生成 {len(raw)} 点"
              f"（严格 {len(strict)} / 平衡 {len(balanced)} / 温和 {len(gentle)}）")
        print("%-16s %7s %8s %10s %11s %10s %10s %10s" %
              ("脚本", "点数", "整体/s", "不动区/s", "动作区/s", "覆盖100ms", "覆盖200ms", "精确100ms"))
        for r in (manual, r_raw, r_b, r_s, r_g):
            print("%-16s %7d %8.2f %10.2f %11.2f %10.3f %10.3f %10.3f" %
                  (r["label"], r["n"], r["overall"], r["still_per_s"], r["motion_per_s"],
                   r["recall_100ms"], r["recall_200ms"], r["precision_100ms"]))
        print("  * 覆盖 = 人工点被生成点落在容差内的比例；精确 = 生成点附近有人工点的比例")

        # ---- 图：最长平台窗口 + 最活跃窗口
        try:
            import matplotlib
            matplotlib.use("Agg")
            matplotlib.rcParams["font.sans-serif"] = [
                "Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "DejaVu Sans"]
            matplotlib.rcParams["axes.unicode_minus"] = False
            import matplotlib.pyplot as plt
            cands = [(ht[i - 1], ht[i]) for i in range(1, len(ht))
                     if ht[i] - ht[i - 1] >= 8.0 and abs(hv[i] - hv[i - 1]) <= 2.0]
            if cands:
                a, b = max(cands, key=lambda x: x[1] - x[0])
                mid = (a + b) / 2
            else:
                mid = ht[len(ht) // 2]
            sw = (max(0.0, mid - 15), mid + 15)
            best = (0, float(ht[0]))
            for t0 in np.arange(float(ht[0]), max(float(ht[0]) + 1, float(ht[-1]) - 60), 30):
                c = int(((ht >= t0) & (ht < t0 + 60)).sum())
                if c > best[0]:
                    best = (c, float(t0))
            mw = (best[1], best[1] + 60)
            fig, axes = plt.subplots(2, 1, figsize=(12, 6), dpi=110)
            raw_t, raw_v = ta(raw)
            st_t, st_v = ta(balanced)
            for ax, (w0, w1), title in ((axes[0], sw, "不动窗口"), (axes[1], mw, "动作窗口")):
                for t, v, label, style, w in ((ht, hv, "人工", "-", 1.8),
                                              (raw_t, raw_v, "生成(未门控)", "--", 1.0),
                                              (st_t, st_v, "生成(平衡门)", "-", 1.2)):
                    # 取窗口两侧最近的一个点：不动区间里本来就没有点，
                    # 只取窗口内会让"人工/门控"整段看不见（而那正是保持段）
                    i0 = int(np.searchsorted(t, w0))
                    i1 = int(np.searchsorted(t, w1))
                    lo = max(0, i0 - 1); hi = min(len(t), i1 + 1)
                    ax.step(t[lo:hi], v[lo:hi], where="post", linestyle=style,
                            label=label, linewidth=w)
                ax.set_xlim(w0, w1); ax.set_ylim(-4, 104)   # 留余量：保持段常贴在 0/100
                ax.set_title(f"{name} {title} {w0:.0f}-{w1:.0f}s")
                ax.set_ylabel("位置"); ax.grid(alpha=0.3)
                ax.legend(fontsize=8, loc="upper right")
            axes[1].set_xlabel("时间 秒")
            fig.tight_layout()
            png = Path(str(prefix) + "_compare.png")
            fig.savefig(png); plt.close(fig)
            print(f"  图: {png}")
        except Exception as e:  # noqa: BLE001
            import traceback
            print("  绘图失败:", repr(e))
            traceback.print_exc()

    p = Path(sys.argv[1]).parent / "gate_compare.json"
    p.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n报告: {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
