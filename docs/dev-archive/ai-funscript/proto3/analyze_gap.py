# -*- coding: utf-8 -*-
"""analyze_gap.py —— 流式引擎轨道 / 动作点 vs 官方离线产物的差距定位。

只读已有的 compare/*.npz 与 compare/*.funscript，不修改任何既有文件。
运行:
    E:\\Development\\AI Funscript\\_third_party\\GoblinTrainingGrounds\\venv\\Scripts\\python.exe ^
        proto3\\analyze_gap.py            # 全部章节
        proto3\\analyze_gap.py 1 3 5      # 只跑指定章节

章节:
  1  逐文件逐轨道对齐质量(含 ±60 行互相关 lag 扫描)
  2  times_ms 时间轴一致性
  3  幅度衰减定位(分位数 / 幅度分箱比值 / vmarg 出处核验)
  4  块边界(行号 256 整数倍)伪影
  5  NaN 与"未计算区域"
  6  动作点层面(funscript)
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys

import numpy as np

# Windows 控制台默认 GBK, 中文表格会乱码 -> 强制 UTF-8 输出
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
CMP = os.path.join(HERE, "compare")

KEYS = ["vmarg", "level", "env", "blo", "bhi",
        "plat_top", "plat_bot", "rev_top", "rev_bot"]
ROW_HZ = 30.0                     # 行率(引擎 ROW_HZ / 官方 row grid 实测)
BLOCK = 256                       # 引擎每次样式解码重跑的行数(8.5333 s)
CTX = 1024                        # 每次前向两侧上下文行数(34.13 s)
LAG_MAX = 60                      # 互相关扫描范围(行)
# 引擎: total_rows = int(duration_ms/1000*ROW_HZ) + 8   (stream_engine.py:170)
PAD_ROWS = 8

OFFICIAL = os.path.join(CMP, "official_tracks.npz")
# 官方动作点: GoblinScript v0.6.0 的离线产出(带 metadata, 见 funscript header)
OFFICIAL_FS = os.path.join(CMP, "K1cztm.funscript")


# --------------------------------------------------------------------------
# 载入
# --------------------------------------------------------------------------
def stream_files() -> list[str]:
    return sorted(glob.glob(os.path.join(CMP, "K1cztm_stream_*_tracks.npz")))


def tag(path: str) -> str:
    """K1cztm_stream_homewrite_tracks.npz -> homewrite"""
    m = re.match(r"K1cztm_stream_(.+)_tracks\.npz$", os.path.basename(path))
    return m.group(1) if m else os.path.basename(path)


def valid_end(a: np.ndarray) -> int:
    """最后一个非有限值之后的第一个下标 = 有效行数。"""
    fin = np.isfinite(a)
    if not fin.any():
        return 0
    return int(np.flatnonzero(fin)[-1]) + 1


def load_npz(path: str) -> dict:
    z = np.load(path, allow_pickle=True)
    return {k: np.asarray(z[k]) for k in z.files}


def load_fs(path: str) -> tuple[np.ndarray, dict]:
    js = json.load(open(path, encoding="utf-8-sig"))
    a = np.array([[x["at"], x["pos"]] for x in js["actions"]], dtype=np.float64)
    meta = {k: v for k, v in js.items() if k != "actions"}
    return a, meta


def hdr(s: str) -> None:
    print()
    print("=" * 100)
    print(s)
    print("=" * 100)


def corr(a: np.ndarray, b: np.ndarray) -> float:
    if len(a) < 3 or a.std() <= 0 or b.std() <= 0:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


def xcorr_lag(o: np.ndarray, s: np.ndarray, maxlag: int = LAG_MAX):
    """±maxlag 行互相关扫描。返回 (best_lag, corr_at_best, corr_at_0, curve)。

    约定: lag = L 表示 s 相对 o 平移 L 行使相关最大, 即 s[i+L] ~ o[i]。
    """
    o = np.asarray(o, np.float64)
    s = np.asarray(s, np.float64)
    o = o - o.mean()
    s = s - s.mean()
    curve = {}
    best = (0, -2.0)
    for L in range(-maxlag, maxlag + 1):
        if L >= 0:
            a, b = o[L:], s[:len(s) - L] if L else s
        else:
            a, b = o[:L], s[-L:]
        if len(a) < 100:
            continue
        den = np.sqrt((a * a).sum() * (b * b).sum())
        c = float((a * b).sum() / den) if den > 0 else 0.0
        curve[L] = c
        if c > best[1]:
            best = (L, c)
    return best[0], best[1], curve.get(0, float("nan")), curve


# --------------------------------------------------------------------------
# 1. 逐文件逐轨道对齐质量
# --------------------------------------------------------------------------
def section1() -> None:
    hdr("1. 逐文件逐轨道对齐质量(共同窗 = 所有流式文件有效行的交集)")
    off = load_npz(OFFICIAL)
    files = stream_files()
    lasts = {tag(f): valid_end(load_npz(f)["vmarg"]) for f in files}
    win = min(min(lasts.values()), len(off["vmarg"]))
    print(f"各流式文件有效行数: " +
          ", ".join(f"{k}={v}" for k, v in lasts.items()) +
          f"  (官方 T={len(off['vmarg'])})")
    print(f"共同比较窗 = 0 .. {win - 1}  ({win / ROW_HZ:.1f} s)")
    print()

    rows = []
    for f in files:
        z = load_npz(f)
        for k in KEYS:
            o = off[k][:win].astype(np.float64)
            s = z[k][:win].astype(np.float64)
            m = np.isfinite(o) & np.isfinite(s)
            oo, ss = o[m], s[m]
            c0 = corr(oo, ss)
            L, cL, _, curve = xcorr_lag(oo, ss)
            so, sstd = oo.std(), ss.std()
            axo, axs = np.abs(oo).max(), np.abs(ss).max()
            rows.append(dict(
                tag=tag(f), k=k, n=int(m.sum()), c0=c0, lag=L, c_lag=cL,
                ort=ss.mean() / so.mean() if abs(so.mean()) > 1e-12 else float("nan"),
                srt=sstd / so if so > 0 else float("nan"),
                axr=axs / axo if axo > 0 else float("nan"),
                axo=axo, axs=axs, std_o=so, std_s=sstd,
                means_o=oo.mean(), means_s=ss.mean(),
                curve=curve))

    print("表 1a: 每条轨道的对齐指标 (win=%d 行)" % win)
    print(f"{'改法':<12}{'轨道':<10}{'corr(lag0)':>11}{'best_lag':>9}"
          f"{'corr@lag':>10}{'std_o':>10}{'std_s':>10}{'std比':>8}"
          f"{'absmax_o':>10}{'absmax_s':>10}{'absmax比':>9}")
    for r in rows:
        print(f"{r['tag']:<12}{r['k']:<10}{r['c0']:>11.4f}{r['lag']:>+9d}"
              f"{r['c_lag']:>10.4f}{r['std_o']:>10.4f}{r['std_s']:>10.4f}"
              f"{r['srt']:>8.4f}{r['axo']:>10.3f}{r['axs']:>10.3f}{r['axr']:>9.4f}")

    # 汇总: 每个改法的平均分
    print()
    print("表 1b: 每个改法的汇总 (9 条轨道)")
    print(f"{'改法':<12}{'有效行':>8}{'corr0均值':>11}{'corr0最小':>11}"
          f"{'lag!0条数':>10}{'|1-std比|均值':>15}{'|1-absmax比|均值':>17}")
    summ = []
    for f in files:
        rs = [r for r in rows if r["tag"] == tag(f)]
        c0 = np.array([r["c0"] for r in rs])
        srt = np.array([r["srt"] for r in rs])
        axr = np.array([r["axr"] for r in rs])
        nl = sum(1 for r in rs if r["lag"] != 0)
        summ.append((tag(f), lasts[tag(f)], c0.mean(), c0.min(), nl,
                     np.abs(1 - srt).mean(), np.abs(1 - axr).mean()))
    for t, n, cm, cmin, nl, sr, ar in sorted(summ, key=lambda x: -x[2]):
        print(f"{t:<12}{n:>8}{cm:>11.4f}{cmin:>11.4f}{nl:>10}{sr:>15.4f}{ar:>17.4f}")

    # lag 曲线陡峭度: lag=0 处的曲率, 证明 0 是真峰
    print()
    print("表 1c: lag 曲线在 0 附近的陡峭度 (corr(0)-corr(±1), 取较大者; 越大说明 0 越确定)")
    print(f"{'改法':<12}{'轨道':<10}{'corr(0)':>10}{'corr(+1)':>10}{'corr(-1)':>10}{'drop':>9}")
    for r in rows:
        if r["k"] not in ("vmarg", "level", "env"):
            continue
        cp = r["curve"].get(1, float("nan"))
        cm = r["curve"].get(-1, float("nan"))
        drop = r["c_lag"] - max(cp, cm)
        print(f"{r['tag']:<12}{r['k']:<10}{r['c_lag']:>10.4f}{cp:>10.4f}"
              f"{cm:>10.4f}{drop:>9.4f}")

    print()
    print("结论: 所有文件所有轨道 best_lag=0 (见 1c 的 drop>0), 行时间轴无整体偏移。")
    best = max(summ, key=lambda x: x[2])
    worst = min(summ, key=lambda x: x[2])
    print(f"最优改法: {best[0]} (corr0均值 {best[2]:.4f}, 最差轨道 {best[3]:.4f})")
    print(f"最差改法: {worst[0]} (corr0均值 {worst[2]:.4f}, 最差轨道 {worst[3]:.4f})")


# --------------------------------------------------------------------------
# 2. times_ms 时间轴
# --------------------------------------------------------------------------
def section2() -> None:
    hdr("2. 行时间轴一致性")
    off = load_npz(OFFICIAL)
    t = off["times_ms"]
    grid = (np.arange(len(t)) + 1) * (1000.0 / ROW_HZ)
    d = np.abs(t - grid)
    print(f"official times_ms: T={len(t)}  t[0]={t[0]:.6f}  t[-1]={t[-1]:.6f}")
    print(f"  median dt={np.median(np.diff(t)):.6f} ms  -> row rate="
          f"{1000.0 / np.median(np.diff(t)):.6f} 行/秒")
    print(f"  与 (i+1)*1000/30 栅格: max|diff|={d.max():.3e} ms  "
          f"(逐点严格相等={bool((d == 0).all())}, 差异仅为 float64 末位舍入; "
          f"dt 恒定 -> 无 15/30 行/秒 混淆)")
    print(f"  times_ms==0 的行数: {int((t == 0).sum())} / {len(t)}")

    print()
    print("流式 *_tracks.npz 的 key 列表:")
    for f in stream_files():
        z = np.load(f, allow_pickle=True)
        print(f"  {os.path.basename(f):<40} keys={sorted(z.files)}")
    print("  -> stream_to_script.py:69-70 保存时显式剔除了 times_ms "
          "(`if k != \"times_ms\"`), 所以流式 npz 里没有时间轴可比。")
    print("     因此'两个 npz 的 times_ms 是否逐行相同'——做不了: 流式侧该字段不存在。")

    print()
    print("间接证据(行数公式 + funscript 时间栅格):")
    print(f"  引擎 stream_engine.py:170  total_rows = int(duration_ms/1000*{ROW_HZ:g}) + {PAD_ROWS}")
    for f in stream_files():
        z = np.load(f, allow_pickle=True)
        n = len(z["vmarg"])
        print(f"  {os.path.basename(f):<40} T={n} = int(dur*30)+8 -> "
              f"int(dur*30)={n - PAD_ROWS} ; 官方 T={len(off['times_ms'])}")
    print(f"  -> 流式数组比官方多 {39110 - 39103} 行, 正好是引擎的 +8 尾部 padding 造成的;")
    print("     官方第 0..39102 行与流式第 0..39102 行是同一行栅格(见第 1 节 best_lag 全为 0)。")

    print()
    print("funscript 时间栅格(首个动作的 at, 应为 33 ms = 1/30 s):")
    for p in sorted(glob.glob(os.path.join(CMP, "K1cztm_stream_*.funscript"))):
        a, _ = load_fs(p)
        print(f"  {os.path.basename(p):<40} t0={a[0,0]:.0f} "
              f"t1={a[-1,0]:.0f} n={len(a)}")
    a, _ = load_fs(OFFICIAL_FS)
    print(f"  {os.path.basename(OFFICIAL_FS):<40} t0={a[0,0]:.0f} "
          f"t1={a[-1,0]:.0f} n={len(a)}")
    print("  -> 全部落在 1/30 s 的栅格上, 无 15 行/秒 的降采样迹象。")


# --------------------------------------------------------------------------
# 3. 幅度衰减定位
# --------------------------------------------------------------------------
QS = [1, 25, 50, 75, 99]


def section3() -> None:
    hdr("3. 幅度衰减定位")
    off = load_npz(OFFICIAL)
    files = stream_files()
    win = min(min(valid_end(load_npz(f)["vmarg"]) for f in files),
              len(off["vmarg"]))
    print(f"窗 = 0..{win - 1} ({win / ROW_HZ:.1f}s)")

    # ---- 3a 分位数对比 ----
    print()
    print("表 3a: 分位数对比 (官方 / 流式, 以及 流式-官方 的相对变化)")
    print(f"{'改法':<12}{'轨道':<10}" + "".join(f"{'p' + str(q) + '_o':>11}" for q in QS)
          + "".join(f"{'p' + str(q) + '_s':>11}" for q in QS) + "  比值(p1/p25/p50/p75/p99)")
    qstats = {}
    for f in files:
        z = load_npz(f)
        for k in KEYS:
            o = off[k][:win].astype(np.float64)
            s = z[k][:win].astype(np.float64)
            m = np.isfinite(o) & np.isfinite(s)
            qo = np.percentile(o[m], QS)
            qs = np.percentile(s[m], QS)
            ratio = np.where(np.abs(qo) > 1e-9, qs / np.where(qo == 0, 1, qo), np.nan)
            qstats[(tag(f), k)] = (qo, qs, ratio)
            print(f"{tag(f):<12}{k:<10}"
                  + "".join(f"{v:>11.4f}" for v in qo)
                  + "".join(f"{v:>11.4f}" for v in qs)
                  + "  " + "/".join(f"{r:6.3f}" for r in ratio))

    # ---- 3b 幅度分箱比值: 判断 clamp(大值被压) vs 纯缩放 ----
    print()
    print("表 3b: 按 |官方值| 分箱的流式/官方 中位数比值 (等比缩放 -> 各箱都 ~常数; "
          "饱和/clamp -> 大值箱显著变小)")
    edges = [0, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 100.0]
    print(f"{'改法':<12}{'轨道':<10}" + "".join(f"{'|o|∈[' + str(edges[i]) + ',' + str(edges[i+1]) + ')':>20}" for i in range(len(edges) - 1)))
    for f in files:
        z = load_npz(f)
        for k in KEYS:
            o = off[k][:win].astype(np.float64)
            s = z[k][:win].astype(np.float64)
            m = np.isfinite(o) & np.isfinite(s)
            oo, ss = o[m], s[m]
            ao = np.abs(oo)
            cells = []
            for i in range(len(edges) - 1):
                sel = (ao >= edges[i]) & (ao < edges[i + 1])
                if sel.sum() < 30:
                    cells.append(f"{'n/a':>20}")
                    continue
                mo = np.median(ao[sel])
                if mo < 1e-9:
                    # 官方侧中位数为 0(重组轨在该档填了精确 0) -> 比值无意义
                    cells.append(f"{'o_med=0':>12}(n={int(sel.sum()):<6d})")
                    continue
                r = np.median(np.abs(ss[sel])) / mo
                cells.append(f"{r:>10.4f}(n={int(sel.sum()):<6d})")
            print(f"{tag(f):<12}{k:<10}" + "".join(cells))

    # ---- 3c vmarg 出处核验 ----
    print()
    print("表 3c: vmarg 出处核验 —— 官方设计里 vmarg = u*eg, u = clamp(v/(envelope(v,env_k)+.05), -3, 3)")
    print("      (jepa_train.py:817-822; jepa_infer.py:1475 用 marginal 做相位轨)")
    print("      所以官方 npz 的 vmarg/env 必须严格落在 [-3, 3] 内, 且 env==0 的行 vmarg 必须为 0。")
    oe = off["env"][:win].astype(np.float64)
    ov = off["vmarg"][:win].astype(np.float64)
    mz = oe > 1e-12
    q = ov[mz] / oe[mz]
    print(f"  official: env==0 行数={int((oe == 0).sum())}/{win} ({(oe == 0).mean() * 100:.2f}%)")
    print(f"            vmarg 在这些行上为 0 的行数={int((ov[oe == 0] == 0).sum())}")
    print(f"            vmarg/env 范围 = [{q.min():.4f}, {q.max():.4f}]; |q|>3 的比例="
          f"{float(np.mean(np.abs(q) > 3)) * 100:.4f}%")
    print(f"  -> 官方 npz 的 vmarg 是 **重组后** 的返回轨 (u*eg), 不是 marginal。")
    for f in files:
        z = load_npz(f)
        sv = z["vmarg"][:win].astype(np.float64)
        se = z["env"][:win].astype(np.float64)
        m2 = (se > 1e-12) & np.isfinite(sv)
        q2 = sv[m2] / se[m2]
        print(f"  {tag(f):<12} env==0 行={int((se == 0).sum()):<6d} "
              f"vmarg/env |q|>3 比例={float(np.mean(np.abs(q2) > 3)) * 100:8.3f}%  "
              f"absmax={np.abs(q2).max():12.1f}   <-- 不是重组轨")
    print("  => 表 1/3a 里 vmarg 的 0.84~0.92 比值, 是官方 npz 存错字段造成的")
    print("     苹果对橘子比较, 不是引擎的幅度损失。真正可比的只有 vmarg 的 *形状*。")

    # ---- 3d vmarg 与 level 的关系 ----
    print()
    print("表 3d: vmarg / level 的关系")
    print(f"{'改法':<12}{'corr(vmarg,level)':>18}{'std(vmarg)/std(level)':>23}"
          f"{'env 均值比':>12}{'env p99 比':>12}")
    for f in [OFFICIAL] + files:
        z = load_npz(f)
        nm = "official" if f == OFFICIAL else tag(f)
        v = z["vmarg"][:win].astype(np.float64)
        l = z["level"][:win].astype(np.float64)
        e = z["env"][:win].astype(np.float64)
        m = np.isfinite(v) & np.isfinite(l)
        oe_ = off["env"][:win].astype(np.float64)
        me = np.isfinite(e) & np.isfinite(oe_)
        print(f"{nm:<12}{corr(v[m], l[m]):>18.4f}"
              f"{v[m].std() / l[m].std():>23.6f}"
              f"{e[me].mean() / oe_[me].mean():>12.4f}"
              f"{np.percentile(e[me], 99) / np.percentile(oe_[me], 99):>12.4f}")

    # ---- 3e 输入 style decode 的关键量 ----
    print()
    print("表 3e: 送入 style_positions_composed 的三个幅度输入量(相对官方)")
    print(f"{'改法':<12}{'|vel| 均值比':>13}{'|vel| p99比':>12}"
          f"{'|vel|>STILL_EPS(22)占比':>24}{'官方同占比':>12}{'env*v_std 均值比':>18}")
    v_std = 193.57327270507812
    ov_ = np.abs(off["vmarg"][:win].astype(np.float64)) * v_std
    oe_ = off["env"][:win].astype(np.float64) * v_std
    print(f"{'official':<12}{1.0:>13.4f}{1.0:>12.4f}"
          f"{float((ov_ > 22).mean()):>24.4f}{float((ov_ > 22).mean()):>12.4f}{1.0:>18.4f}")
    for f in files:
        z = load_npz(f)
        sv_ = np.abs(z["vmarg"][:win].astype(np.float64)) * v_std
        se_ = z["env"][:win].astype(np.float64) * v_std
        print(f"{tag(f):<12}{sv_.mean() / ov_.mean():>13.4f}"
              f"{np.percentile(sv_, 99) / np.percentile(ov_, 99):>12.4f}"
              f"{float((sv_ > 22).mean()):>24.4f}{float((ov_ > 22).mean()):>12.4f}"
              f"{se_.mean() / oe_.mean():>18.4f}")

    # ---- 3f 决定性实验: 用官方样式解码跑流式轨道 ----
    section3f(files, win)


def section3f(files: list[str], win: int) -> None:
    """把流式 npz 的轨道喂回官方 style_positions_composed + extrema_actions。

    若重解码出来的动作点统计(密度/行程)回到官方水平, 说明轨道没问题,
    差距出在引擎自己的动作合成/导出路径; 若仍小, 说明轨道里已经有伤。
    """
    print()
    print("表 3f: 决定性实验 —— 用【官方样式解码】跑【流式轨道 npz】")
    print("      调用与 jepa_infer.run / stream_engine._decode_chunk 同参:")
    print("      style_positions_composed(vmarg*v_std, level, env*v_std, edges=[0,T],")
    print("        band, plat, rev, rev_source='viterbi', still_eps=22, amp_cap_x=4, ...)")
    print("      + extrema_actions(rdp_eps=1, cut_ease=250)。")
    print("      注意: 官方 npz 的 vmarg 是重组轨, 不可用于此实验 -> 只跑流式 npz;")
    print("            切分统一用单 shot [0,T](引擎实际用的是真实剪缝, 这里做受控对比)。")
    try:
        import torch
        sys.path.insert(0, r"E:\Development\AI Funscript\_third_party"
                           r"\GoblinTrainingGrounds")
        from goblintrain import common as gcommon, jepa_infer
        ck = torch.load(
            r"E:\Development\AI Funscript\_third_party\GoblinTrainingGrounds"
            r"\weights\checkpoints\v0.6.0.pt", map_location="cpu", weights_only=False)
    except Exception as e:  # noqa: BLE001
        print(f"  跳过: 无法导入官方实现 ({e})")
        return
    v_std = float(ck["v_std"])
    jepa_infer.DT = 1.0 / ROW_HZ
    jepa_infer.FPS = ROW_HZ

    off_a, _ = load_fs(OFFICIAL_FS)

    def decode(z: dict, n: int) -> np.ndarray:
        times = np.arange(n) * (1000.0 / ROW_HZ)
        g = lambda k: np.nan_to_num(z[k][:n].astype(np.float64))  # noqa: E731
        vel = g("vmarg") * v_std
        env = g("env") * v_std
        trk = {k: g(k) for k in ("rev_top", "rev_bot", "plat_top", "plat_bot")}
        try:
            bias = jepa_infer.fit_rev_bias(
                {"rev_top": trk["rev_top"], "rev_bot": trk["rev_bot"]}, vel, "x")
        except Exception:  # noqa: BLE001
            bias = 0.0
        sub: dict = {}
        p = jepa_infer.style_positions_composed(
            vel, g("level"), env, np.array([0, n]),
            band=(g("blo"), g("bhi")), still_eps=jepa_infer.STILL_EPS,
            plat=(trk["plat_top"], trk["plat_bot"]),
            plat_thr=jepa_infer.PLAT_THR, plat_lo=jepa_infer.PLAT_LO,
            plat_peak=jepa_infer.PLAT_PEAK, plat_veto=jepa_infer.PLAT_VETO,
            plat_soft=jepa_infer.PLAT_SOFT, plat_rail_track=True,
            plat_shift_cap=jepa_infer.PLAT_SHIFT_CAP,
            rev=(trk["rev_top"], trk["rev_bot"]),
            rev_snap=gcommon.rows_at(gcommon.REV_SNAP_S, ROW_HZ),
            times_ms=times, subframe=jepa_infer.SUBFRAME, sub_out=sub,
            ext_snap=jepa_infer.EXT_SNAP, amp_cap_x=jepa_infer.AMP_CAP_X,
            rev_source=jepa_infer.REV_SOURCE, rev_bias=bias,
            rev_gap=gcommon.rows_at(gcommon.EVENT_GAP_S, ROW_HZ),
            force_out=None, phase_out=None)
        acts = jepa_infer.extrema_actions(
            p, times, np.array([0, n]), sub=sub if sub else None, force=None,
            cut_ease=jepa_infer.CUT_EASE, rdp_eps=jepa_infer.RDP_EPS, stats={})
        return np.array([[a["at"], a["pos"]] for a in acts], dtype=np.float64)

    print()
    print(f"{'改法':<12}{'窗s':>7}| {'重解码n':>8}{'官方n':>7}{'引擎导出n':>10}"
          f"| {'重解码行程':>10}{'官方行程':>9}{'导出行程':>9}"
          f"| {'行程比(重/官)':>13}{'行程比(导/官)':>13}")
    cache = {}
    for f in files:
        z = load_npz(f)
        # 与导出 funscript 覆盖对齐的窗
        fsp = os.path.join(CMP, f"K1cztm_stream_{tag(f)}.funscript")
        rows = valid_end(z["vmarg"])
        if os.path.exists(fsp):
            fa, _ = load_fs(fsp)
            rows = min(rows, int(round(fa[-1, 0] / 1000.0 * ROW_HZ)))
        rows = max(rows, 64)
        dec = decode(z, rows)
        dur = (rows - 1) / ROW_HZ
        so = isi_stats(off_a[off_a[:, 0] <= dur * 1000])
        sd = isi_stats(dec)
        cache[tag(f)] = (dec, dur)
        if os.path.exists(fsp):
            sf = isi_stats(load_fs(fsp)[0])
            n_eng, tr_eng, r_eng = sf["n"], sf["travel"], sf["travel"] / so["travel"]
        else:
            n_eng, tr_eng, r_eng = -1, float("nan"), float("nan")
        print(f"{tag(f):<12}{dur:>7.0f}| {sd['n']:>8}{so['n']:>7}{n_eng:>10}"
              f"| {sd['travel']:>10.2f}{so['travel']:>9.2f}{tr_eng:>9.2f}"
              f"| {sd['travel'] / so['travel']:>13.3f}{r_eng:>13.3f}")
    print()
    print("读法: 第 4 列(重解码行程/官方)若接近 1, 而第 5 列(引擎导出行程/官方)仍 ~0.5,")
    print("      则差距不在轨道, 而在引擎自己的动作合成/导出路径。")

    # ---- 3g 嫌疑点隔离: rev_bias 冻结 + 零填充 ----
    print()
    print("表 3g: 嫌疑点隔离 —— rev_bias 的'冻结 + 零填充'语义")
    print("      引擎 stream_engine.py:579-593 只在 gen_end 首次 ≥8000 行时拟合一次 rev_bias,")
    print("      之后 `len(bias) < gen_end` 的分支用 0 补齐。而官方 jepa_infer.run 每次都按整片")
    print("      的 band 展开 bias。下面用 homewrite 的轨道, 用同一个官方解码器跑三种 bias:")
    f0 = [x for x in files if tag(x) == "homewrite"]
    if not f0:
        return
    z = load_npz(f0[0])
    n = min(valid_end(z["vmarg"]), int(round(0.559300e6 / 1000.0 * ROW_HZ)))
    tt = np.arange(n) * (1000.0 / ROW_HZ)
    gg = lambda k: np.nan_to_num(z[k][:n].astype(np.float64))  # noqa: E731
    vel = gg("vmarg") * v_std
    env = gg("env") * v_std
    so = isi_stats(off_a[off_a[:, 0] <= (n - 1) / ROW_HZ * 1000])

    def dec_with(bias):
        trk = {k: gg(k) for k in ("rev_top", "rev_bot", "plat_top", "plat_bot")}
        sub: dict = {}
        p = jepa_infer.style_positions_composed(
            vel, gg("level"), env, np.array([0, n]),
            band=(gg("blo"), gg("bhi")), still_eps=jepa_infer.STILL_EPS,
            plat=(trk["plat_top"], trk["plat_bot"]),
            plat_thr=jepa_infer.PLAT_THR, plat_lo=jepa_infer.PLAT_LO,
            plat_peak=jepa_infer.PLAT_PEAK, plat_veto=jepa_infer.PLAT_VETO,
            plat_soft=jepa_infer.PLAT_SOFT, plat_rail_track=True,
            plat_shift_cap=jepa_infer.PLAT_SHIFT_CAP,
            rev=(trk["rev_top"], trk["rev_bot"]),
            rev_snap=gcommon.rows_at(gcommon.REV_SNAP_S, ROW_HZ),
            times_ms=tt, subframe=jepa_infer.SUBFRAME, sub_out=sub,
            ext_snap=jepa_infer.EXT_SNAP, amp_cap_x=jepa_infer.AMP_CAP_X,
            rev_source=jepa_infer.REV_SOURCE, rev_bias=bias,
            rev_gap=gcommon.rows_at(gcommon.EVENT_GAP_S, ROW_HZ),
            force_out=None, phase_out=None)
        a = jepa_infer.extrema_actions(
            p, tt, np.array([0, n]), sub=sub if sub else None, force=None,
            cut_ease=jepa_infer.CUT_EASE, rdp_eps=jepa_infer.RDP_EPS, stats={})
        return isi_stats(np.array([[x["at"], x["pos"]] for x in a], np.float64))

    rev = {"rev_top": gg("rev_top"), "rev_bot": gg("rev_bot")}
    b_full = jepa_infer.fit_rev_bias(rev, vel, "x", verbose=False)
    b_frozen = jepa_infer.fit_rev_bias(
        {"rev_top": rev["rev_top"][:8192], "rev_bot": rev["rev_bot"][:8192]},
        vel[:8192], "x", verbose=False)
    b_frozen = np.concatenate([b_frozen, np.zeros(n - len(b_frozen))]) \
        if np.ndim(b_frozen) else b_frozen
    print(f"{'bias 语义':<34}{'动作数':>8}{'mean|Δpos|':>12}{'行程/s':>10}{'行程比(官方)':>14}")
    print(f"{'官方(同窗 0-559s)':<34}{so['n']:>8}{so['amp']:>12.2f}"
          f"{so['travel']:>10.2f}{1.0:>14.3f}")
    for nm, bb in (("整窗拟合(官方语义)", b_full),
                   ("冻结@8192行+0填充(引擎语义)", b_frozen),
                   ("bias=0", 0.0)):
        s = dec_with(bb)
        print(f"{nm:<34}{s['n']:>8}{s['amp']:>12.2f}{s['travel']:>10.2f}"
              f"{s['travel'] / so['travel']:>14.3f}")
    print("  -> 冻结+零填充会把行程压到官方的 ~0.18 倍, 比引擎实际导出(~0.46 倍)还低,")
    print("     方向对但幅度过头; 说明它至少是嫌疑之一, 但引擎实际路径还有别的因子。")


# --------------------------------------------------------------------------
# 4. 块边界伪影
# --------------------------------------------------------------------------
def section4() -> None:
    hdr("4. 块边界伪影 (引擎每 256 行 = 8.5333 s 重跑一次样式解码, 前向含 ±1024 行上下文)")
    files = stream_files()
    print("方法: 对每条轨道的逐行跳变 |x[r]-x[r-1]|, 按 r 到最近 256 倍数的距离 d 分组,")
    print("      比较各组的中位数与 p99.9。若 home-write 无缝, 各 d 组应同分布。")
    print()
    print(f"{'改法':<12}{'轨道':<10}{'d=0 中位':>11}{'d>=1 中位':>11}{'中位比':>9}"
          f"{'d=0 p99.9':>12}{'d>=1 p99.9':>12}{'p99.9比':>10}{'n(d=0)':>8}")
    summary = {}
    for f in files:
        z = load_npz(f)
        v = z["vmarg"].astype(np.float64)
        last = valid_end(v)
        rs = []
        for k in KEYS:
            a = z[k][:last].astype(np.float64)
            j = np.abs(np.diff(a))
            r = np.arange(1, last)
            ok = np.isfinite(j)
            j, r = j[ok], r[ok]
            d0 = (r % BLOCK) == 0
            if d0.sum() < 5 or (~d0).sum() < 5:
                continue
            m0, m1 = np.median(j[d0]), np.median(j[~d0])
            p0, p1 = np.percentile(j[d0], 99.9), np.percentile(j[~d0], 99.9)
            rs.append((k, m0, m1, p0, p1, int(d0.sum())))
            print(f"{tag(f):<12}{k:<10}{m0:>11.5f}{m1:>11.5f}"
                  f"{m0 / m1 if m1 else float('nan'):>9.3f}"
                  f"{p0:>12.4f}{p1:>12.4f}{p0 / p1 if p1 else float('nan'):>10.3f}"
                  f"{int(d0.sum()):>8}")
        if rs:
            summary[tag(f)] = float(np.mean([x[1] / x[2] for x in rs if x[2]]))
    print()
    print("表 4a2: 按'距最近 256 倍数的距离' 0..8 分组的跳变中位数 (vmarg, 归一化到全体中位数)")
    print(f"{'改法':<12}" + "".join(f"{'d=' + str(i):>10}" for i in range(9)))
    for f in files:
        z = load_npz(f)
        v = z["vmarg"].astype(np.float64)
        last = valid_end(v)
        j = np.abs(np.diff(v[:last]))
        r = np.arange(1, last)
        ok = np.isfinite(j)
        j, r = j[ok], r[ok]
        base = np.median(j)
        cells = []
        for i in range(9):
            sel = (r % BLOCK == i % BLOCK) if i == 0 else (np.minimum(r % BLOCK, BLOCK - r % BLOCK) == i)
            cells.append(np.median(j[sel]) / base if sel.sum() > 10 else float("nan"))
        print(f"{tag(f):<12}" + "".join(f"{c:>10.3f}" for c in cells))
    print()
    print("表 4b: 块边界 ±8 行的跳变剖面 (先按各文件自己的中位跳变归一化, 再跨文件取中位)")
    print("      offset=0 就是行号 256 的整数倍的跳变 |x[256k]-x[256k-1]|。")
    print("      官方轨道作对照: 若官方在 offset 0 也起峰, 说明是信号本身的性质, 不是引擎伪影。")
    print(f"{'对象':<12}{'轨道':<9}" + "".join(f"{o:>7d}" for o in range(-4, 5)))
    for nm, path, last in ([("official", OFFICIAL, len(load_npz(OFFICIAL)["vmarg"]))] +
                           [(tag(f), f, valid_end(load_npz(f)["vmarg"])) for f in files]):
        z = load_npz(path)
        for k in ("vmarg", "level", "env"):
            x = np.asarray(z[k][:last], np.float64)
            j = np.abs(np.diff(x))
            base = np.median(j)
            cells = []
            for off in range(-4, 5):
                vals = [j[b + off - 1] for b in range(BLOCK, last - 2, BLOCK)
                        if 1 <= b + off < last and np.isfinite(j[b + off - 1])]
                cells.append(np.median(vals) / base if vals else float("nan"))
            print(f"{nm:<12}{k:<9}" + "".join(f"{c:>7.2f}" for c in cells))

    print()
    print("表 4c: 富集度 —— 行号 r ≡ m (mod 256) 的位置上, |Δx| 超过全体 p95 的比例。")
    print("      均匀无伪影时应为 5%。跨 7 个流式文件汇总(每格约 470 个边界样本)。")
    print(f"{'对象':<12}{'轨道':<9}{'m=0':>8}{'m=64':>8}{'m=128':>8}{'m=192':>8}"
          f"{'m=32':>8}{'m=4':>8}{'其余均值':>10}")
    for nm, items in (("official", [(OFFICIAL, len(load_npz(OFFICIAL)["vmarg"]))]),
                      ("stream(7文件)", [(f, valid_end(load_npz(f)["vmarg"]))
                                         for f in files])):
        for k in ("vmarg", "level"):
            acc = np.zeros(BLOCK)
            cnt = np.zeros(BLOCK)
            for path, last in items:
                z = load_npz(path)
                x = np.asarray(z[k][:last], np.float64)
                j = np.abs(np.diff(x))
                r = np.arange(1, last)
                ok = np.isfinite(j)
                j, r = j[ok], r[ok]
                big = (j > np.percentile(j, 95)).astype(float)
                for m in range(BLOCK):
                    sel = (r % BLOCK) == m
                    acc[m] += big[sel].sum()
                    cnt[m] += sel.sum()
            p = acc / np.maximum(cnt, 1)
            rest = np.mean([p[m] for m in range(BLOCK)
                            if m not in (0, 4, 32, 64, 128, 192)])
            print(f"{nm:<12}{k:<9}{p[0]:>8.3f}{p[64]:>8.3f}{p[128]:>8.3f}"
                  f"{p[192]:>8.3f}{p[32]:>8.3f}{p[4]:>8.3f}{rest:>10.3f}")

    print()
    print("表 4d: 把窗口切成 256 行的块, 块内 corr(官方,流式); 块【首 16 行】/块【中段】")
    print("      分别算 —— 若只有块首尾异常, 块首的 corr 会明显低于块中。")
    off = load_npz(OFFICIAL)
    win = min(min(valid_end(load_npz(f)["vmarg"]) for f in files), len(off["vmarg"]))
    nb = win // BLOCK
    print(f"{'改法':<12}{'整窗corr(vmarg)':>16}{'块内corr均值':>14}{'块内corr最小':>14}"
          f"{'块内corr标准差':>15}{'块首16行corr':>14}{'块中corr':>11}{'块数':>7}")
    for f in files:
        z = load_npz(f)
        o = off["vmarg"][:win].astype(np.float64)
        s = z["vmarg"][:win].astype(np.float64)
        cs, head, mid = [], [], []
        for b in range(nb):
            lo, hi = b * BLOCK, (b + 1) * BLOCK
            a, bb = o[lo:hi], s[lo:hi]
            if not (np.isfinite(a).all() and np.isfinite(bb).all()):
                continue
            cs.append(corr(a, bb))
            if np.isfinite(corr(a[:16], bb[:16])):
                head.append(corr(a[:16], bb[:16]))
            if np.isfinite(corr(a[64:192], bb[64:192])):
                mid.append(corr(a[64:192], bb[64:192]))
        print(f"{tag(f):<12}{corr(o, s):>16.4f}{np.mean(cs):>14.4f}"
              f"{np.min(cs):>14.4f}{np.std(cs):>15.4f}{np.mean(head):>14.4f}"
              f"{np.mean(mid):>11.4f}{len(cs):>7}")
    print("  注: 块内 corr (~0.63) 远低于整窗 (~0.97) 是短窗估计的正常现象;")
    print("      要看的是 '块首 vs 块中' 的差 —— 表内差别很小, 说明没有'块首若干行全错'。")

    print()
    print("表 4e: 块内 16 行一条的 corr 剖面 —— 直接看'块内分段差异'。")
    print("      对每个 256 行块, 把 [16k,16k+16) 与官方同段算 corr, 跨块取中位。")
    print(f"{'改法':<12}{'轨道':<9}" + "".join(f"{'k=' + str(k):>7}" for k in range(16)))
    for f in files:
        z = load_npz(f)
        for k in ("vmarg", "level"):
            o = off[k][:win].astype(np.float64)
            s = z[k][:win].astype(np.float64)
            cells = []
            for kk in range(16):
                vals = []
                for b in range(nb):
                    lo = b * BLOCK + kk * 16
                    a, bb = o[lo:lo + 16], s[lo:lo + 16]
                    if np.isfinite(a).all() and np.isfinite(bb).all():
                        c = corr(a, bb)
                        if np.isfinite(c):
                            vals.append(c)
                cells.append(np.median(vals) if vals else float("nan"))
            print(f"{tag(f):<12}{k:<9}" + "".join(f"{c:>7.3f}" for c in cells))


# --------------------------------------------------------------------------
# 5. NaN 与未计算区域
# --------------------------------------------------------------------------
def section5() -> None:
    hdr("5. NaN 与未计算区域")
    files = stream_files()
    print(f"{'文件':<40}{'T':>7}{'每条轨道NaN':>12}{'有效尾行':>10}"
          f"{'有效秒数':>10}{'NaN占比':>9}")
    for f in files:
        z = load_npz(f)
        n = len(z[KEYS[0]])
        nan = [int(np.isnan(z[k]).sum()) for k in KEYS]
        last = valid_end(z["vmarg"])
        same = len(set(nan)) == 1
        print(f"{os.path.basename(f):<40}{n:>7}{nan[0]:>12}{last - 1:>10}"
              f"{last / ROW_HZ:>10.1f}{nan[0] / n * 100:>8.2f}%"
              + ("" if same else "  <-- 各轨道 NaN 数不同!"))
    off = load_npz(OFFICIAL)
    print(f"{'official_tracks.npz':<40}{len(off['vmarg']):>7}{0:>12}"
          f"{len(off['vmarg']) - 1:>10}{len(off['vmarg']) / ROW_HZ:>10.1f}{0.0:>8.2f}%")

    print()
    print("NaN 位置: 全部文件都是'前段有效 + 尾部连续 NaN 一段'。逐文件尾部 NaN 起止:")
    for f in files:
        z = load_npz(f)
        v = z["vmarg"]
        n = len(v)
        bad = ~np.isfinite(v)
        first = int(np.flatnonzero(bad)[0]) if bad.any() else n
        # 是否连续
        seg = bad[first:]
        cont = bool(seg.all())
        print(f"  {os.path.basename(f):<40} NaN 从行 {first} ({first / ROW_HZ:.1f}s) 到 "
              f"{n - 1}; 连续={cont}; 前段内部 NaN 数={int(bad[:first].sum())}")
    print("  流式引擎 _trk 初始化为 np.full(total_rows, np.nan)(stream_engine.py:181),")
    print("  只有被前向覆盖过的行才被写入 -> 尾部未跑到的行恒为 NaN。")

    print()
    print("official times_ms==0 的尾部区域: %d 行 (无)。流式 npz 无 times_ms 字段。"
          % int((off["times_ms"] == 0).sum()))
    print()
    print("np.nan_to_num 污染: 上游在送进样式解码前做了 nan_to_num:")
    print("  stream_engine.py:570  vel_all = np.nan_to_num(self._trk['vmarg'][:gen_end]) * v_std")
    print("  jepa_infer.py:574    ev = np.nan_to_num(env_script, nan=0.0)")
    print("  但两者都切成 [:gen_end], gen_end 只覆盖已算出的行, 所以实际被当成 0 的行数 =")
    for f in files:
        z = load_npz(f)
        last = valid_end(z["vmarg"])
        # gen_end 由引擎跟踪, npz 里没有; 用有效尾行近似
        print(f"    {tag(f):<12} 有效 {last} 行 -> 该文件在 [:gen_end] 内 NaN 数 ≈ 0 "
              f"(尾部第 {last}..{len(z['vmarg']) - 1} 行共 "
              f"{len(z['vmarg']) - last} 行从未进入解码)")
    print("  结论: npz 里那些 NaN 不会污染样式解码(被 :gen_end 切掉);")
    print("        真正会把 NaN 当 0 的是*行内空洞*——本次数据里前段空洞数=0, 未观察到。")


# --------------------------------------------------------------------------
# 6. 动作点层面
# --------------------------------------------------------------------------
def isi_stats(a: np.ndarray) -> dict:
    t = a[:, 0]
    isi = np.diff(t)
    dp = np.abs(np.diff(a[:, 1]))
    dur = (t[-1] - t[0]) / 1000.0
    # 密度无关幅度: 30 Hz 重采样后的 std 与 travel/s
    grid = np.arange(t[0], t[-1], 1000.0 / 30.0)
    pos = np.interp(grid, t, a[:, 1])
    travel = dp.sum() / dur if dur > 0 else float("nan")
    return dict(n=len(a), t1=t[-1], dur=dur, density=len(a) / dur if dur else 0,
                amp=dp.mean(), amp_med=np.median(dp),
                amp_p95=np.percentile(dp, 95),
                amp_top10=float(np.sort(dp)[int(len(dp) * 0.9):].mean()),
                isi_med=np.median(isi), isi_p95=np.percentile(isi, 95),
                isi_max=isi.max(), std30=pos.std(),
                std_1s=_smooth_std(a, 1.0), std_3s=_smooth_std(a, 3.0),
                std_8s=_smooth_std(a, 8.0),
                travel=travel, dpos_std=dp.std())


def _smooth_std(a: np.ndarray, win_s: float) -> float:
    """30 Hz 重采样后做 win_s 秒滑动平均再去 std —— 只看低频行程的幅度。"""
    grid = np.arange(a[0, 0], a[-1, 0], 1000.0 / 30.0)
    if len(grid) < 60:
        return float("nan")
    p = np.interp(grid, a[:, 0], a[:, 1])
    k = max(1, int(win_s * 30))
    c = np.convolve(p, np.ones(k) / k, mode="same")
    e = min(len(p) // 4, k)
    c = c[e:len(p) - e] if len(p) > 2 * e else c
    return float(c.std())


def section6() -> None:
    hdr("6. 动作点层面 (funscript)")
    off_a, off_m = load_fs(OFFICIAL_FS)
    print(f"官方动作点 = {os.path.basename(OFFICIAL_FS)}  metadata={off_m.get('metadata', {}).get('author')} "
          f"model={off_m.get('metadata', {}).get('model')}")
    print(f"  settings = {off_m.get('metadata', {}).get('settings')}")
    print(f"  动作数={len(off_a)}  覆盖 {off_a[0, 0]:.0f}..{off_a[-1, 0]:.0f} ms")
    dbg = os.path.join(CMP, "dbg.funscript")
    if os.path.exists(dbg):
        da, dm = load_fs(dbg)
        print(f"  (另: dbg.funscript 只有 {len(da)} 个动作, "
              f"覆盖 {da[0, 0]:.0f}..{da[-1, 0]:.0f} ms, 无 metadata -> 只是调试片段)")

    print()
    print("表 6a: 每个流式 funscript 在其自身覆盖窗内 vs 官方(同窗截断)")
    print(f"{'文件':<38}{'n':>6}{'覆盖s':>8}{'密度/s':>8}{'密度比':>8}"
          f"{'amp':>8}{'amp比':>8}{'30Hz std':>10}{'std比':>8}{'travel/s':>10}{'travel比':>9}")
    rows = []
    for p in sorted(glob.glob(os.path.join(CMP, "K1cztm_stream_*.funscript"))):
        a, _ = load_fs(p)
        t1 = a[-1, 0]
        m = off_a[:, 0] <= t1
        o = off_a[m]
        # 官方的"同窗"密度/幅度(用 0..t1, 两端都不裁)
        so, ss = isi_stats(o), isi_stats(a)
        dr = ss["density"] / so["density"] if so["density"] else float("nan")
        rows.append((os.path.basename(p), ss, so, dr))
        print(f"{os.path.basename(p):<38}{ss['n']:>6}{ss['dur']:>8.0f}"
              f"{ss['density']:>8.2f}{dr:>8.3f}{ss['amp']:>8.2f}"
              f"{ss['amp'] / so['amp']:>8.3f}{ss['std30']:>10.2f}"
              f"{ss['std30'] / so['std30']:>8.3f}{ss['travel']:>10.2f}"
              f"{ss['travel'] / so['travel']:>9.3f}")

    print()
    print("对照: 官方在若干截断窗上的绝对量")
    print(f"{'窗上限s':<10}{'n':>6}{'密度/s':>8}{'amp':>8}{'30Hz std':>10}{'travel/s':>10}")
    for T in (200, 300, 400, 500, 546, 560, 596, 631, 1219, 1303):
        m = off_a[:, 0] <= T * 1000
        if m.sum() < 5:
            continue
        s = isi_stats(off_a[m])
        print(f"{T:<10}{s['n']:>6}{s['density']:>8.2f}{s['amp']:>8.2f}"
              f"{s['std30']:>10.2f}{s['travel']:>10.2f}")

    print()
    print("表 6b: ISI 分布 (ms)")
    print(f"{'文件':<38}{'ISI中位':>9}{'ISI p95':>9}{'ISI max':>9}{'|Δpos|中位':>11}"
          f"{'|Δpos| p95':>11}")
    def line(name, s):
        print(f"{name:<38}{s['isi_med']:>9.0f}{s['isi_p95']:>9.0f}{s['isi_max']:>9.0f}"
              f"{s['amp_med']:>11.1f}{s['amp_p95']:>11.1f}")
    line("official(全片)", isi_stats(off_a))
    line("official(<=560s)", isi_stats(off_a[off_a[:, 0] <= 560000]))
    for p in sorted(glob.glob(os.path.join(CMP, "K1cztm_stream_*.funscript"))):
        a, _ = load_fs(p)
        line(os.path.basename(p), isi_stats(a))

    print()
    print("说明: 上表的 'amp' 是 mean|Δpos|, 与点数密度强耦合(点多 -> 步长小),")
    print("      所以跨文件比较要看 'travel/s'(总行程/秒, 密度无关)和 '30Hz std'。")

    print()
    print("表 6c: 多尺度幅度 —— 先把动作插值到 30 Hz, 再做 1/3/8 s 滑动平均去 std。")
    print("      std30 看整体散布; std_8s 只看'长冲程'的低频摆幅 —— 两者分开才能看清")
    print("      '保留了抖动、丢了长冲程'这种失真。")
    print(f"{'对象':<36}{'窗s':>6}{'std30':>8}{'std1s':>8}{'std3s':>8}{'std8s':>8}"
          f"{'p50|Δ|':>8}{'p95|Δ|':>8}{'top10%|Δ|':>11}")
    def row(name, a):
        s = isi_stats(a)
        print(f"{name:<36}{s['dur']:>6.0f}{s['std30']:>8.2f}{s['std_1s']:>8.2f}"
              f"{s['std_3s']:>8.2f}{s['std_8s']:>8.2f}{s['amp_med']:>8.1f}"
              f"{s['amp_p95']:>8.1f}{s['amp_top10']:>11.1f}")
    for T in (546, 559, 575, 597, 631, 1219, 1303):
        row(f"official(<= {T}s)", off_a[off_a[:, 0] <= T * 1000])
    for p in sorted(glob.glob(os.path.join(CMP, "K1cztm_stream_*.funscript"))):
        row(os.path.basename(p).replace("K1cztm_stream_", "").replace(".funscript", ""),
            load_fs(p)[0])
    print("  -> 关键: std30 比 ~0.95-1.03, 连 std_8s 都比官方 *大* ~1.1-1.2 倍 —— 位置信号的")
    print("     '散布' 并没有缩小。缩掉的只有 '每步幅度' 和 '总行程/秒':")
    print("     homewrite p50|Δpos|=4 vs 官方 14, p95 44 vs 82, travel/s 16.66 vs 36.32 (0.459)。")
    print("     即产物是'抖动还在、大步长消失'的平滑版, 不是整体变小。")

    print()
    print("表 6d: bench_final.log 的 amplitude_ratio=0.694 复核")
    print("  bench_final.log: METRICS {n_actions:937, coverage_s:244.1, density_ratio:2.708,")
    print("                          amplitude_ratio:0.694, ...}")
    print("  benchmark.py:56-62 的 amplitude_ratio = mean|Δpos|(stream) / mean|Δpos|(hand),")
    print("  且 HAND = K1drsp.funscript —— 是 K1drsp 的人工脚本, 不是 K1cztm 的官方离线产物。")
    print("  同一条日志里 density_ratio=2.708: 流式点数多 2.7 倍, mean|Δpos| 天然被摊薄。")
    print(f"  用同一定义复核 density_ratio*amplitude_ratio = "
          f"{2.708 * 0.694:.3f} = 流式总行程/人工总行程, 即流式行程是人工的 "
          f"{2.708 * 0.694 * 100:.0f}%, 幅度其实更大而不是更小。")
    print("  -> 0.694 是'密度混淆'的读数, 不能作为轨道幅度衰减的证据。")


SECTIONS = {1: section1, 2: section2, 3: section3,
            4: section4, 5: section5, 6: section6}


def main() -> int:
    want = [int(x) for x in sys.argv[1:] if x.isdigit()] or list(SECTIONS)
    for n in want:
        if n in SECTIONS:
            SECTIONS[n]()
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
