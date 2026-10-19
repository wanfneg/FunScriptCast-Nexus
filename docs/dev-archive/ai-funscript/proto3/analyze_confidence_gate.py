# -*- coding: utf-8 -*-
"""用"人工脚本"当真值，检验各置信度信号能否判定"这里该不该有动作"，并定阈值。

回答三个问题：
  1) 模型给出的 conf 头，逐行看能不能区分"该动/不该动"？（AUC）
  2) 如果不能，哪个现成轨道能？(|vmarg| / plat / rev / env 比值)
  3) 阈值怎么定：绝对阈值还是相对 p50 的倍数？两片交叉验证。

用法：
    python analyze_confidence_gate.py compare/fresh2/K1cztm compare/fresh2/K1drsp
输出：
    终端表格 + <公共父目录>/gate_report.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROW = 30.0
HAND = {
    "K1cztm": Path(r"E:/testvideo/funscript test/K1cztm.funscript"),
    "K1drsp": Path(r"E:/testvideo/funscript test/K1drsp.funscript"),
}
GATE_KS = (0.3, 0.5, 0.75, 1.0)


def load_actions(p):
    a = json.loads(Path(p).read_text(encoding="utf-8"))["actions"]
    return (np.array([x["at"] for x in a], float) / 1000.0,
            np.array([x["pos"] for x in a], float))


def smooth(x, k):
    k = max(1, int(k))
    return np.convolve(x, np.ones(k) / k, mode="same")


def platform_mask(t, v, min_gap=3.0, max_delta=2.0):
    n = int(t[-1] * ROW) + 1
    m = np.zeros(n, bool)
    for i in range(1, len(t)):
        if t[i] - t[i - 1] >= min_gap and abs(v[i] - v[i - 1]) <= max_delta:
            m[int(t[i - 1] * ROW):int(t[i] * ROW)] = True
    return m


def auc(pos_scores, neg_scores):
    """P(pos > neg) + 0.5 P(equal)，秩和法。"""
    a = np.concatenate([pos_scores, neg_scores])
    order = np.argsort(a, kind="mergesort")
    n = len(a)
    r_sorted = np.arange(1, n + 1, dtype=float)
    rank_orig = np.empty(n, float)
    rank_orig[order] = r_sorted          # 每个元素（原始顺序）的名次
    uniq, inv, cnt = np.unique(a, return_inverse=True, return_counts=True)
    if len(uniq) < n:                     # 并列 -> 取平均名次
        sums = np.zeros(len(uniq))
        np.add.at(sums, inv, rank_orig)
        ranks = (sums / cnt)[inv]
    else:
        ranks = rank_orig
    n1 = len(pos_scores)
    return (ranks[:n1].sum() - n1 * (n1 + 1) / 2.0) / (n1 * len(neg_scores))


def runs_below(sig, thr, min_rows):
    still = smooth(sig, 9) < thr
    d = np.diff(np.concatenate([[0], still.astype(np.int8), [0]]))
    keep = np.zeros_like(still)
    for s, e in zip(np.where(d == 1)[0], np.where(d == -1)[0]):
        if e - s >= min_rows:
            keep[s:e] = True
    return keep


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    out = []
    for a in sys.argv[1:]:
        out.append(analyze(Path(a)))
    for r in out:
        print("=" * 80)
        print("### %s: %d 行, 静止行 %d, 运动行 %d, p50|vmarg|=%.2f pos/s, v_std=%.1f"
              % (r["name"], r["rows"], r["still_rows"], r["motion_rows"],
                 r["p50"]["vmarg"], r["v_std"]))
        print("%-20s %7s %10s %10s %7s" % ("信号", "AUC", "运动中位", "静止中位", "分离倍"))
        for k, v in r["signals"].items():
            print("%-20s %7.3f %10.3f %10.3f %7s"
                  % (k, v["auc"], v["median_motion"], v["median_still"], str(v["sep"])))
        b = r["vmarg_best"]
        print("|vmarg| 最优阈值: %.2f pos/s = %.2f x p50 (F1 %.3f, P %.3f, R %.3f)"
              % (b["thr"], b["k_p50"], b["f1"], b["prec"], b["recall"]))
        e = r["still_eps_ref"]
        print("  官方 STILL_EPS=22 pos/s: 静止召回 %.1f%%, 运动误报 %.1f%%"
              % (100 * e["recall"], 100 * e["fp"]))
        print("动作: 无门 平台内 %.2f/s 平台外 %.2f/s | 人工 %.2f/%.2f"
              % (r["baseline"]["plat_per_s"], r["baseline"]["out_per_s"],
                 r["manual"]["plat_per_s"], r["manual"]["out_per_s"]))
        for sig in ("vmarg", "conf", "combo"):
            for k, v in r["gate"][sig].items():
                print("      门 %-5s %-6s thr=%.3f -> 平台内 %.2f/s 平台外 %.2f/s"
                      % (sig, k, v["thr"], v["plat_per_s"], v["out_per_s"]))
    p = Path(sys.argv[1]).parent / "gate_report.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n报告: %s" % p)
    return 0


def analyze(prefix: Path):
    name = prefix.name
    trk = np.load(str(prefix) + "_tracks.npz")
    meta = json.loads(Path(str(prefix) + "_meta.json").read_text(encoding="utf-8"))
    vstd = float(meta["v_std"])
    tms = np.asarray(trk["times_ms"], float) / 1000.0
    n = len(tms)

    ht, hv = load_actions(HAND[name])
    pm_full = platform_mask(ht, hv)
    plat = np.zeros(n, bool)
    m = min(n, len(pm_full))
    plat[:m] = pm_full[:m]
    pos = np.interp(np.arange(n) / ROW, ht, hv)
    spd = smooth(np.abs(np.gradient(pos, 1.0 / ROW)), 0.5 * ROW)
    covered = (tms >= ht[0]) & (tms <= ht[-1])
    still = covered & plat
    motion = covered & (spd >= 20.0) & ~still

    vm = np.nan_to_num(np.abs(trk["vmarg"]), nan=0.0) * vstd
    conf = np.nan_to_num(trk["conf"], nan=0.0)
    platp = np.nan_to_num(trk["plat_top"], nan=0.0) + np.nan_to_num(trk["plat_bot"], nan=0.0)
    revp = np.nan_to_num(trk["rev_top"], nan=0.0) + np.nan_to_num(trk["rev_bot"], nan=0.0)
    env = np.nan_to_num(trk["env"], nan=0.0) * vstd
    ratio = env / (vm + 1.0)

    p50 = {"vmarg": float(np.median(vm[covered])),
           "conf": float(np.median(conf[covered])),
           "plat": float(np.median(platp[covered]))}
    # 组合信号：速度相对量 与 (1-conf) 的几何平均（都是"这里不该动"的证据）
    combo = (vm / max(p50["vmarg"], 1e-9)) * np.clip(conf, 0.0, 1.0)
    p50["combo"] = float(np.median(combo[covered]))

    sigs = {
        "vmarg pos/s": vm,
        "conf(head)": conf,
        "plat_prob(反向：越高越静)": -platp,
        "rev_prob": revp,
        "env/(vmarg+1)": ratio,
        "combo=(vm/p50)*conf": combo,
    }
    rec = {"name": name, "rows": n, "v_std": vstd,
           "still_rows": int(still.sum()), "motion_rows": int(motion.sum()),
           "p50": {k: round(v, 4) for k, v in p50.items()},
           "signals": {}}
    for k, s in sigs.items():
        a = float(auc(s[motion], s[still]))
        ms = float(np.median(s[motion])); st = float(np.median(s[still]))
        rec["signals"][k] = {"auc": round(a, 4),
                             "median_motion": round(ms, 4),
                             "median_still": round(st, 4),
                             "sep": (round(ms / st, 2) if abs(st) > 1e-9 else None)}

    # |vmarg| 最优绝对阈值
    hi = max(4.0 * p50["vmarg"], 1.5 * 22.0)
    grid = np.unique(np.concatenate([np.linspace(0.0, hi, 240), [p50["vmarg"], 22.0]]))
    best = None
    for thr in grid:
        pred = vm < thr
        tp = int((pred & still).sum()); fp = int((pred & motion).sum())
        prec = tp / max(1, tp + fp); rc = tp / max(1, int(still.sum()))
        f1 = 2 * prec * rc / max(1e-9, prec + rc)
        if best is None or f1 > best["f1"]:
            best = {"thr": float(thr), "f1": float(f1), "prec": float(prec),
                    "recall": float(rc), "k_p50": float(thr / p50["vmarg"])}
    rec["vmarg_best"] = best
    pred22 = vm < 22.0
    rec["still_eps_ref"] = {
        "recall": float((pred22 & still).sum() / max(1, int(still.sum()))),
        "fp": float((pred22 & motion).sum() / max(1, int(motion.sum()))),
    }

    at, av = load_actions(str(prefix) + "_stream.funscript")
    w0, w1 = ht[0], min(at[-1], ht[-1])
    r0, r1 = int(w0 * ROW), int(w1 * ROW)
    pm = plat[r0:r1]
    plat_s = pm.sum() / ROW
    out_s = (~pm).sum() / ROW
    idxa = np.clip((at * ROW).astype(int), 0, n - 1)
    pin = plat[idxa]
    inw = (at >= w0) & (at <= w1)
    rec["baseline"] = {"n": int(inw.sum()),
                       "plat_per_s": round(float((inw & pin).sum() / plat_s), 2),
                       "out_per_s": round(float((inw & ~pin).sum() / out_s), 2)}
    rec["gate"] = {"vmarg": {}, "conf": {}, "combo": {}}
    for key, sig in (("vmarg", vm), ("conf", conf), ("combo", combo)):
        for k in GATE_KS:
            thr = k * p50[key]
            keep = runs_below(sig, thr, int(1.0 * ROW))
            rk = keep[np.clip((at * ROW).astype(int), 0, n - 1)]
            survive = inw & ~rk
            rec["gate"][key]["k=%.2f" % k] = {
                "thr": round(thr, 4),
                "plat_per_s": round(float((survive & pin).sum() / plat_s), 2),
                "out_per_s": round(float((survive & ~pin).sum() / out_s), 2),
            }
    rec["manual"] = {"n": int(len(ht)), "plat_per_s": 0.30,
                     "out_per_s": round(float(len(ht) / (ht[-1] - ht[0])), 2)}
    return rec


if __name__ == "__main__":
    raise SystemExit(main())
