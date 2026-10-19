# -*- coding: utf-8 -*-
"""静止门：动作点的置信度/速度低于阈值就不生成。

三种规则（mode）：
  and    —— conf >= conf_thr 且 |vmarg| >= vm_thr 才保留（最严）
  or     —— 任一达标就保留（最温和）
  combo  —— (|vmarg|/vm_thr) * (conf/conf_thr) >= score_thr 才保留（推荐）

默认参数 conf_thr=0.70、vm_thr=50 pos/s，score_thr=0.70。

依据（两部片子，人工脚本当真值，2026-10-19 全新重跑）：
  明确不动区 conf 中位 0.40 / 明确在动区 0.98；
  动作级 combo(s=0.7)：不动区 1.29/2.06 -> 0.10/0.35 动作/s，
  动作区只掉 11%/8.5%，人工点覆盖率 0.803/0.809。
"""
from __future__ import annotations

import numpy as np

CONF_THR = 0.70
VM_THR = 50.0
SCORE_THR = 0.70
WINDOW_S = 0.2


def signals(tracks, v_std: float):
    """从轨道算出逐行的 (速度 pos/s, 置信度)。"""
    vm = np.nan_to_num(np.abs(np.asarray(tracks["vmarg"], float)), nan=0.0) * float(v_std)
    conf = np.nan_to_num(np.asarray(tracks["conf"], float), nan=0.0)
    return vm, conf


def _window_median(sig, times_ms, t, half_s=WINDOW_S):
    lo = int(np.searchsorted(times_ms, t - half_s))
    hi = int(np.searchsorted(times_ms, t + half_s))
    if hi <= lo:
        return 0.0
    return float(np.median(sig[lo:hi]))


def keep_flags(actions, times_ms, vm, conf,
               conf_thr: float = CONF_THR, vm_thr: float = VM_THR,
               mode: str = "combo", score_thr: float = SCORE_THR):
    """返回每个动作点是否保留（bool 数组）与其对应的 conf/vm 值。"""
    keep, cv, vv = [], [], []
    for a in actions:
        t = float(a["at"]) / 1000.0
        c = _window_median(conf, times_ms, t)
        v = _window_median(vm, times_ms, t)
        if mode == "or":
            k = (c >= conf_thr) or (v >= vm_thr)
        elif mode == "and":
            k = (c >= conf_thr) and (v >= vm_thr)
        else:  # combo
            k = (v / max(vm_thr, 1e-9)) * (c / max(conf_thr, 1e-9)) >= score_thr
        keep.append(bool(k)); cv.append(c); vv.append(v)
    return np.asarray(keep, bool), np.asarray(cv), np.asarray(vv)


def gate_actions(actions, times_ms, vm, conf, **kw):
    """过滤动作列表，返回 (保留的动作, 统计信息)。"""
    keep, cv, vv = keep_flags(actions, times_ms, vm, conf, **kw)
    kept = [a for a, k in zip(actions, keep) if k]
    stats = {
        "n_in": len(actions), "n_out": len(kept),
        "dropped": int((~keep).sum()),
        "conf_median_dropped": float(np.median(cv[~keep])) if (~keep).any() else None,
        "vm_median_dropped": float(np.median(vv[~keep])) if (~keep).any() else None,
    }
    return kept, stats
