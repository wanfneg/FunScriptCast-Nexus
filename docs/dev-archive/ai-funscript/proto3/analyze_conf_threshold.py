# -*- coding: utf-8 -*-
"""置信度（conf）能不能直接当"该不该出动作"的开关：分布 + 阈值 + 动作级验证。

真值：人工脚本。
  明确不动区 = 相邻关键帧 >=3s 且 |Δpos| <=2（作者显式保持）
  明确动作区 = 人工 0.5s 平滑速度 >= 20 pos/s（另给 >=50 的更严格口径）
只统计人工脚本覆盖区间。

用法: python analyze_conf_threshold.py
"""
from __future__ import annotations
import json
import numpy as np
from pathlib import Path

ROW = 30.0
V_STD = 193.57327270507812
BASE = Path(r"E:/Development/AI Funscript/proto3/compare/fresh2")
HAND = {"K1cztm": Path(r"E:/testvideo/funscript test/K1cztm.funscript"),
        "K1drsp": Path(r"E:/testvideo/funscript test/K1drsp.funscript")}


def load_actions(p):
    a = json.loads(Path(p).read_text(encoding="utf-8"))["actions"]
    return np.array([x["at"] for x in a], float) / 1000.0, np.array([x["pos"] for x in a], float)


def smooth(x, k):
    k = max(1, int(k)); return np.convolve(x, np.ones(k) / k, mode="same")


def platmask(t, v, g=3.0, d=2.0):
    n = int(t[-1] * ROW) + 1; m = np.zeros(n, bool)
    for i in range(1, len(t)):
        if t[i] - t[i - 1] >= g and abs(v[i] - v[i - 1]) <= d:
            m[int(t[i - 1] * ROW):int(t[i] * ROW)] = True
    return m


def q(a):
    return [round(float(np.percentile(a, p)), 3) for p in (1, 10, 25, 50, 75, 90, 99)]


def main():
    print("%-8s %-16s %6s %8s %8s %8s %8s %8s %8s %8s" %
          ("片子", "区域", "行数", "p1", "p10", "p25", "p50", "p75", "p90", "p99"))
    store = {}
    for name, hp in HAND.items():
        trk = np.load(BASE / (name + "_tracks.npz"))
        conf = np.asarray(trk["conf"], float)
        tms = np.asarray(trk["times_ms"], float) / 1000.0
        n = len(tms)
        ht, hv = load_actions(hp)
        pm_full = platmask(ht, hv); plat = np.zeros(n, bool)
        m = min(n, len(pm_full)); plat[:m] = pm_full[:m]
        pos = np.interp(np.arange(n) / ROW, ht, hv)
        spd = smooth(np.abs(np.gradient(pos, 1.0 / ROW)), 0.5 * ROW)
        cov = (tms >= ht[0]) & (tms <= ht[-1])
        still = cov & plat
        mot20 = cov & (spd >= 20) & ~still
        mot50 = cov & (spd >= 50) & ~still
        store[name] = dict(conf=conf, tms=tms, still=still, mot20=mot20, mot50=mot50,
                           cov=cov, ht=ht, hv=hv, plat=plat)
        for label, mask in (("明确不动", still), ("动作>=20", mot20), ("动作>=50", mot50)):
            c = conf[mask]
            print("%-8s %-16s %6d %8.3f %8.3f %8.3f %8.3f %8.3f %8.3f %8.3f" %
                  (name, label, len(c), *q(c)))
        print()

    # 直方图（看懂重叠）
    for name, d in store.items():
        conf = d["conf"]
        bins = np.linspace(0, 1, 11)
        hs, _ = np.histogram(conf[d["still"]], bins)
        hm, _ = np.histogram(conf[d["mot20"]], bins)
        print(f"{name} 直方图（行数占比 %，左=不动 右=动作）")
        print("  区间    " + " ".join(f"{bins[i]:.1f}-{bins[i+1]:.1f}" for i in range(10)))
        print("  不动  % " + " ".join(f"{100.0*x/max(1,hs.sum()):5.1f}" for x in hs))
        print("  动作  % " + " ".join(f"{100.0*x/max(1,hm.sum()):5.1f}" for x in hm))
        print()

    # 阈值扫描
    for name, d in store.items():
        conf = d["conf"]; still = d["still"]; mot = d["mot20"]
        print(f"=== {name}: 阈值扫描（conf < T 判为'该静音'）")
        print("     T    不动召回%  动作误杀%   加权F1")
        best = None
        for T in np.arange(0.05, 0.98, 0.01):
            pred = conf < T
            tp = (pred & still).sum(); fn = (~pred & still).sum()
            fp = (pred & mot).sum()
            rec_s = tp / max(1, tp + fn)
            kill_m = fp / max(1, mot.sum())
            # 以"不动"为正类的 F1
            prec = tp / max(1, tp + fp)
            f1 = 2 * prec * rec_s / max(1e-9, prec + rec_s)
            if best is None or f1 > best[1]:
                best = (T, f1, rec_s, kill_m)
            if abs(T * 100 - round(T * 100)) < 1e-9 and int(round(T * 100)) % 5 == 0:
                print(f"  {T:5.2f}   {100*rec_s:7.1f}   {100*kill_m:8.1f}   {f1:6.3f}")
        print(f"  -> 最佳 F1: T={best[0]:.2f}, F1={best[1]:.3f}, 不动召回 {100*best[2]:.1f}%, 动作误杀 {100*best[3]:.1f}%")
        # 动作误杀 <= 1% / 2% / 5% 时的最大召回
        for cap in (0.01, 0.02, 0.05):
            ok = [(T, (conf < T)[still].mean()) for T in np.arange(0.05, 0.98, 0.01)
                  if (conf < T)[mot].mean() <= cap]
            if ok:
                T, r = max(ok, key=lambda x: x[1])
                print(f"     动作误杀<={100*cap:.0f}% 时: 最高 T={T:.2f}, 不动召回 {100*r:.1f}%")

    # 动作级：把 conf 低的动作点丢掉
    print()
    print("=" * 70)
    print("动作级：conf 低于 T 的动作点直接不生成")
    print("%-8s %-10s %7s %10s %10s %8s" % ("片子", "方案", "动作数", "不动区/s", "动作区/s", "保留%"))
    for name, d in store.items():
        at, av = load_actions(BASE / (name + "_stream.funscript"))
        conf = d["conf"]; n = len(conf)
        plat = d["plat"]; ht = d["ht"]
        w0, w1 = ht[0], min(at[-1], ht[-1])
        r0, r1 = int(w0 * ROW), int(w1 * ROW)
        pm = plat[r0:r1]; ps = pm.sum() / ROW; os_ = (~pm).sum() / ROW
        idx = np.clip((at * ROW).astype(int), 0, n - 1)
        pin = plat[idx]; inw = (at >= w0) & (at <= w1)
        # 动作的 conf 取 ±0.2s 中位数
        ac = []
        for t in at:
            lo = np.searchsorted(d["tms"], t - 0.2); hi = np.searchsorted(d["tms"], t + 0.2)
            w = conf[lo:hi]; ac.append(float(np.median(w)) if len(w) else 0.0)
        ac = np.array(ac)
        print("%-8s %-10s %7d %10.2f %10.2f %8.1f" % (name, "无门", inw.sum(),
              (inw & pin).sum() / ps, (inw & ~pin).sum() / os_, 100.0))
        for T in (0.5, 0.6, 0.7, 0.8, 0.9):
            keep = (ac >= T) | ~inw
            print("%-8s %-10s %7d %10.2f %10.2f %8.1f" % (name, f"conf>={T:.2f}",
                  (inw & keep).sum(), (inw & keep & pin).sum() / ps,
                  (inw & keep & ~pin).sum() / os_, 100.0 * (inw & keep).sum() / max(1, inw.sum())))
        print("        人工基准   平台 0.30/s  动作区 %.2f/s" % (len(d["ht"]) / (d["ht"][-1] - d["ht"][0])))


if __name__ == "__main__":
    main()
