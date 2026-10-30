//! 位置合成（`style_positions_composed`）—— 逐条移植 `jepa_infer.py`。
//!
//! 生产配置（v0.6.0 manifest）：viterbi 反转分割 + 平台锁定 + 静止门 + 幅度上限 + 接缝速度钳制。
//!
//! 分工（官方注释的原话）：反转**时间**来自边际速度的过零，行程**水平**来自位置头，
//! 行程**幅度**来自生成包络。三者各出一个轴，合成位置曲线。

use std::collections::HashMap;

use crate::style::rows_at;

// ---- 常量（逐条对齐 goblintrain） ----
pub const DECODE_SMOOTH_S: f64 = 1.0;
pub const REV_SMOOTH_S: f64 = 0.1;
pub const EVENT_GAP_S: f64 = 0.066667;
pub const DWELL_GAP_S: f64 = 0.266667;
pub const DWELL_MIN_CALL_S: f64 = 0.266667;
pub const LOCK_SMOOTH_S: f64 = 0.466667;
pub const LOCK_EDGE_S: f64 = 0.2;
pub const VETO_FLANK_S: f64 = 2.0;
pub const MAX_POS_RATE: f64 = 600.0;

/// 连续 `true` 的区间 `[a, b)`。
pub fn runs(mask: &[bool]) -> Vec<(usize, usize)> {
    let mut out = Vec::new();
    let mut i = 0;
    while i < mask.len() {
        if mask[i] {
            let a = i;
            while i < mask.len() && mask[i] {
                i += 1;
            }
            out.push((a, i));
        } else {
            i += 1;
        }
    }
    out
}

/// 零填充的对称均值（`np.convolve(x, ones(k)/k, mode="same")`）。
pub fn box_same(x: &[f64], k: usize) -> Vec<f64> {
    let n = x.len();
    if n == 0 || k == 0 {
        return x.to_vec();
    }
    let half = (k / 2) as isize;
    let inv = 1.0 / k as f64;
    let mut out = vec![0.0; n];
    for i in 0..n {
        let mut acc = 0.0;
        for j in 0..k as isize {
            let src = i as isize - half + j;
            if src >= 0 && (src as usize) < n {
                acc += x[src as usize];
            }
        }
        out[i] = acc * inv;
    }
    out
}

/// 边沿填充的居中均值（`common.local_mean`）。
pub fn local_mean(x: &[f64], k: usize) -> Vec<f64> {
    let n = x.len();
    let k = (k | 1).max(1);
    if n == 0 {
        return Vec::new();
    }
    let half = (k / 2) as isize;
    let inv = 1.0 / k as f64;
    let mut out = vec![0.0; n];
    for i in 0..n {
        let mut acc = 0.0;
        for j in -half..=half {
            let idx = (i as isize + j).clamp(0, n as isize - 1) as usize;
            acc += x[idx];
        }
        out[i] = acc * inv;
    }
    out
}

fn nan_to(v: &[f64], fill: f64) -> Vec<f64> {
    // 对齐 numpy `nan_to_num` 口径（与 still_gate 同款）：NaN→fill、
    // ±Inf→±f64::MAX，不是把 Inf 一并归 fill
    v.iter()
        .map(|x| {
            if x.is_nan() {
                fill
            } else if *x == f64::INFINITY {
                f64::MAX
            } else if *x == f64::NEG_INFINITY {
                f64::MIN
            } else {
                *x
            }
        })
        .collect()
}

/// 驻留判定：平台概率轨道 → 逐行 `{0, +1(顶), -1(底)}`。
///
/// 带**迟滞**：以 `thr` 播种、沿 `thr_lo` 延伸；再补短洞、丢过短段、
/// 丢峰值不足的段。逐条对齐 `jepa_infer.dwell_kind`。
#[allow(clippy::too_many_arguments)]
pub fn dwell_kind(
    pt: &[f64],
    pb: &[f64],
    thr: f64,
    thr_lo: f64,
    gap: usize,
    min_rows: usize,
    peak: f64,
) -> Vec<i8> {
    let n = pt.len();
    let mut masks: Vec<Vec<bool>> = Vec::new();
    for src in [pt, pb] {
        let p = nan_to(src, 0.0);
        let mut m = vec![false; n];
        // 迟滞：先在 >= thr_lo 的连续段里播种，只要段内出现过 >= thr 就整段算
        let lo_mask: Vec<bool> = p.iter().map(|x| *x >= thr_lo).collect();
        for (a, b) in runs(&lo_mask) {
            if p[a..b].iter().any(|x| *x >= thr) {
                for x in m[a..b].iter_mut() {
                    *x = true;
                }
            }
        }
        // 补短洞
        let holes: Vec<bool> = m.iter().map(|x| !*x).collect();
        for (a, b) in runs(&holes) {
            if a > 0 && b < n && b - a <= gap {
                for x in m[a..b].iter_mut() {
                    *x = true;
                }
            }
        }
        // 丢过短段
        let on: Vec<bool> = m.clone();
        for (a, b) in runs(&on) {
            if b - a < min_rows {
                for x in m[a..b].iter_mut() {
                    *x = false;
                }
            }
        }
        masks.push(m);
    }
    let (top, bot) = (masks[0].clone(), masks[1].clone());
    // pt/pb/top/bot 理论等长（同一轨道的顶/底概率与掩码）；不等长（畸形输入）
    // 时按最短对齐，防止下面的 pb[i] 与 p[a..b] 越界 panic
    let n = n.min(pb.len()).min(top.len()).min(bot.len());
    let (pt, pb) = (&pt[..n], &pb[..n]);
    let (top, bot) = (&top[..n], &bot[..n]);
    let mut kind = vec![0i8; n];
    for i in 0..n {
        let only_top = top[i] && (!bot[i] || pt[i] >= pb[i]);
        let only_bot = bot[i] && (!top[i] || pb[i] > pt[i]);
        if only_top {
            kind[i] = 1;
        }
        if only_bot {
            kind[i] = -1;
        }
    }
    if peak > 0.0 {
        for (k, p) in [(1i8, pt), (-1i8, pb)] {
            let m: Vec<bool> = kind.iter().map(|x| *x == k).collect();
            for (a, b) in runs(&m) {
                let mx = p[a..b].iter().cloned().fold(f64::NEG_INFINITY, f64::max);
                if mx < peak {
                    for x in kind[a..b].iter_mut() {
                        *x = 0;
                    }
                }
            }
        }
    }
    kind
}

/// 无邻接行程的平台判定一律否决（低运动的片头/片尾最容易误报）。
pub fn stroke_veto(kind: &mut [i8], vmarg: &[f64], thr: f64, flank: usize) {
    let n = kind.len();
    // NaN→0、±Inf→f64::MAX（对齐 numpy 口径，见 still_gate 同款注释）
    let av: Vec<f64> = vmarg
        .iter()
        .map(|x| {
            if x.is_nan() {
                0.0
            } else if x.is_finite() {
                x.abs()
            } else {
                f64::MAX
            }
        })
        .collect();
    let nz: Vec<bool> = kind.iter().map(|x| *x != 0).collect();
    for (a, b) in runs(&nz) {
        let lo = &av[a.saturating_sub(flank)..a];
        let hi = &av[b..(b + flank).min(n)];
        let ml = if lo.is_empty() {
            0.0
        } else {
            lo.iter().sum::<f64>() / lo.len() as f64
        };
        let mh = if hi.is_empty() {
            0.0
        } else {
            hi.iter().sum::<f64>() / hi.len() as f64
        };
        if ml.max(mh) < thr {
            for x in kind[a..b].iter_mut() {
                *x = 0;
            }
        }
    }
}

/// 把已建好的行程**局部均值**钉到轨道上，保留残差（波纹）。
///
/// 减法而不是替换：均值里是"穿越"，残差里是"纹理"。整段覆盖会把纹理一起抹掉。
#[allow(clippy::too_many_arguments)]
pub fn level_lock(
    p: &[f64],
    kind: &[i8],
    rail_lo: &[f64],
    rail_hi: &[f64],
    smooth: usize,
    edge: usize,
    plat: Option<(&[f64], &[f64])>,
    soft: Option<(f64, f64)>,
    rail_track: bool,
    shift_cap: f64,
) -> Vec<f64> {
    let n = p.len();
    let m = local_mean(p, smooth);
    let mut out = p.to_vec();
    let free: Vec<bool> = kind.iter().map(|x| *x == 0).collect();
    let mut claimed = vec![false; n];
    let nz: Vec<bool> = kind.iter().map(|x| *x != 0).collect();
    for (a, b) in runs(&nz) {
        let rail: &[f64] = if kind[a] > 0 { rail_hi } else { rail_lo };
        let tgt_in: Vec<f64> = if rail_track {
            local_mean(&rail[a..b], smooth)
                .iter()
                .map(|x| x.clamp(0.0, 100.0))
                .collect()
        } else {
            let mean = rail[a..b].iter().sum::<f64>() / (b - a).max(1) as f64;
            vec![mean.clamp(0.0, 100.0); b - a]
        };
        let mut lo = a;
        while lo > 0 && a - lo < edge && free[lo - 1] && !claimed[lo - 1] {
            lo -= 1;
        }
        let mut hi = b;
        while hi < n && hi - b < edge && free[hi] && !claimed[hi] {
            hi += 1;
        }
        for x in claimed[lo..a].iter_mut() {
            *x = true;
        }
        for x in claimed[b..hi].iter_mut() {
            *x = true;
        }
        let len = hi - lo;
        let mut w = vec![1.0f64; len];
        if a > lo {
            // 借用行：第一行权重 0（无接缝）
            let cnt = a - lo;
            for i in 0..cnt {
                w[i] = i as f64 / cnt as f64;
            }
        }
        if hi > b {
            let cnt = hi - b;
            for i in 0..cnt {
                w[b - lo + i] = 1.0 - (i as f64 + 1.0) / (cnt as f64 + 1.0 - 1.0).max(1.0);
            }
        }
        let mut target = vec![0.0f64; len];
        target[a - lo..b - lo].copy_from_slice(&tgt_in);
        if a > lo {
            let v0 = tgt_in[0];
            for x in target[..a - lo].iter_mut() {
                *x = v0;
            }
        }
        if hi > b {
            let v1 = *tgt_in.last().unwrap();
            for x in target[b - lo..].iter_mut() {
                *x = v1;
            }
        }
        let mut corr: Vec<f64> = target
            .iter()
            .zip(m[lo..hi].iter())
            .map(|(t, mm)| t - mm)
            .collect();
        if shift_cap > 0.0 {
            for c in corr.iter_mut() {
                *c = c.clamp(-shift_cap, shift_cap);
            }
        }
        if let (Some((p0, p1)), Some((pt, pb))) = (soft, plat) {
            let src = if kind[a] > 0 { pt } else { pb };
            let pk = src[a..b].iter().cloned().fold(f64::NEG_INFINITY, f64::max);
            let scale = ((pk - p0) / (p1 - p0).max(1e-9)).clamp(0.0, 1.0);
            for c in corr.iter_mut() {
                *c *= scale;
            }
        }
        for i in lo..hi {
            out[i] = (p[i] + w[i - lo] * corr[i - lo]).clamp(0.0, 100.0);
        }
    }
    out
}

/// 交替事件 Viterbi（带不应期）：峰值/谷值概率 → (行号, 极性)。
///
/// 形态约束：峰值后面只能是谷值。阈值不存在——发射多少个由模型标定决定。
pub fn alternating_events(
    p_peak: &[f64],
    p_valley: &[f64],
    bias: &[f64], // 逐行发射先验（长度 1 表示整片同值）
    min_gap: usize,
) -> (Vec<usize>, Vec<i8>) {
    let t = p_peak.len();
    let g = min_gap;
    let n_a = g + 1;
    let ns = 1 + 2 * n_a;
    let free = g;
    let neg = f64::NEG_INFINITY;

    let pk: Vec<f64> = p_peak.iter().map(|x| x.clamp(1e-9, 1.0)).collect();
    let vl: Vec<f64> = p_valley.iter().map(|x| x.clamp(1e-9, 1.0)).collect();
    let nn: Vec<f64> = pk
        .iter()
        .zip(vl.iter())
        .map(|(a, b)| (1.0 - a - b).clamp(1e-9, 1.0))
        .collect();
    // ⚠ bias 是**逐行**的（fit_rev_bias 拟合出来就是一整条轨道）；
    // 取成单值会让发射率整体偏移，进而多出/少掉反转事件
    let bias_at = |i: usize| -> f64 {
        if bias.len() == 1 {
            bias[0]
        } else {
            bias.get(i).copied().unwrap_or(0.0)
        }
    };
    let lpk: Vec<f64> = pk.iter().enumerate().map(|(i, x)| x.ln() + bias_at(i)).collect();
    let lvl: Vec<f64> = vl.iter().enumerate().map(|(i, x)| x.ln() + bias_at(i)).collect();
    let lnn: Vec<f64> = nn.iter().map(|x| x.ln()).collect();

    let base = |k: usize| 1 + k * n_a;
    let mut score = vec![neg; ns];
    score[0] = 0.0;
    let mut back = vec![0i32; t * ns];
    let mut emit = vec![0i8; t * ns];

    for i in 0..t {
        let mut new = vec![neg; ns];
        let mut nb = vec![0i32; ns];
        let mut ne = vec![0i8; ns];

        new[0] = score[0] + lnn[i];
        nb[0] = 0;
        for k in 0..2usize {
            let b0 = base(k);
            for j in 0..(n_a - 1) {
                let dst = b0 + 1 + j;
                let src = b0 + j;
                if score[src] > neg {
                    new[dst] = score[src] + lnn[i];
                    nb[dst] = src as i32;
                }
            }
            let stay = score[b0 + free] + lnn[i];
            if stay > new[b0 + free] {
                new[b0 + free] = stay;
                nb[b0 + free] = (b0 + free) as i32;
            }
        }
        for (k, lp) in [(0usize, lpk[i]), (1usize, lvl[i])] {
            let src_free = base(1 - k) + free;
            let (src, c) = if score[0] >= score[src_free] {
                (0usize, score[0] + lp)
            } else {
                (src_free, score[src_free] + lp)
            };
            let dst = base(k);
            if c > new[dst] {
                new[dst] = c;
                nb[dst] = src as i32;
                ne[dst] = if k == 0 { 1 } else { 2 };
            }
        }
        score = new;
        back[i * ns..(i + 1) * ns].copy_from_slice(&nb);
        emit[i * ns..(i + 1) * ns].copy_from_slice(&ne);
    }

    let mut s = 0usize;
    let mut best = neg;
    for (i, v) in score.iter().enumerate() {
        if *v > best {
            best = *v;
            s = i;
        }
    }
    let mut rows = Vec::new();
    let mut kinds = Vec::new();
    for i in (0..t).rev() {
        let e = emit[i * ns + s];
        if e != 0 {
            rows.push(i);
            kinds.push(if e == 1 { 1i8 } else { -1i8 });
        }
        s = back[i * ns + s] as usize;
    }
    rows.reverse();
    kinds.reverse();
    (rows, kinds)
}

/// 幅度上限：写出行程不超过边际自身行程的 `amp_cap_x` 倍（只动深度，不动时间）。
pub fn amp_bound(end: f64, cur: f64, tr_raw: f64, n_rows: usize, amp_cap_x: f64) -> f64 {
    if amp_cap_x <= 0.0 || n_rows == 0 {
        return end;
    }
    let lim = amp_cap_x * tr_raw;
    if (end - cur).abs() > lim {
        (cur + (end - cur).signum() * lim).clamp(0.0, 100.0)
    } else {
        end
    }
}

/// 样式参数（生产配置的默认值来自 v0.6.0 manifest）。
#[derive(Debug, Clone)]
pub struct StyleParams {
    pub fps: f64,
    pub still_eps: f64,
    pub ext_snap: f64,
    pub amp_cap_x: f64,
    pub plat_thr: f64,
    pub plat_lo: f64,
    pub plat_peak: f64,
    pub plat_veto: f64,
    pub plat_soft: Option<(f64, f64)>,
    pub plat_rail_track: bool,
    pub plat_shift_cap: f64,
    /// true = viterbi 分割（生产），false = 过零分割
    pub viterbi: bool,
    /// 过零回退时的吸附半径（行）；对应 REV_SNAP_S
    pub rev_snap: usize,
    /// 亚帧时刻（对应 SUBFRAME="rev"）
    pub subframe_rev: bool,
}

impl Default for StyleParams {
    fn default() -> Self {
        Self {
            fps: 30.0,
            still_eps: 22.0,
            ext_snap: 0.0,
            amp_cap_x: 4.0,
            plat_thr: 0.25,
            plat_lo: 0.15,
            plat_peak: 0.45,
            plat_veto: 15.0,
            plat_soft: Some((0.5, 1.0)),
            plat_rail_track: true,
            plat_shift_cap: 25.0,
            viterbi: true,
            rev_snap: 2,
            subframe_rev: true,
        }
    }
}

/// 合成位置曲线（0..100，逐行）。
#[allow(clippy::too_many_arguments)]
pub fn style_positions_composed(
    vmarg: &[f64],
    level: &[f64],
    env_script: &[f64],
    shot_edges: &[usize],
    band: Option<(&[f64], &[f64])>,
    plat: Option<(&[f64], &[f64])>,
    rev: Option<(&[f64], &[f64])>,
    rev_bias: &[f64], // 逐行发射先验（长度 1 表示整片同值）
    prm: &StyleParams,
) -> Vec<f64> {
    let n_all = vmarg.len();
    let mut p = vec![50.0f64; n_all];
    let fps = prm.fps;
    let k_lp = rows_at(DECODE_SMOOTH_S, fps, true);
    let k_smooth = rows_at(REV_SMOOTH_S, fps, true);
    let dt = 1.0 / fps;

    let lv = box_same(&nan_to(level, 50.0), k_lp);
    let ev: Vec<f64> = nan_to(env_script, 0.0);
    let (blo, bhi) = match band {
        Some((lo, hi)) => (
            Some(box_same(&nan_to(lo, 0.0), k_lp)),
            Some(box_same(&nan_to(hi, 100.0), k_lp)),
        ),
        None => (None, None),
    };

    let mut dk = plat.map(|(pt, pb)| {
        dwell_kind(
            pt,
            pb,
            prm.plat_thr,
            prm.plat_lo,
            rows_at(DWELL_GAP_S, fps, false),
            rows_at(DWELL_MIN_CALL_S, fps, false),
            prm.plat_peak,
        )
    });
    if let Some(k) = dk.as_mut() {
        if prm.plat_veto > 0.0 {
            stroke_veto(k, vmarg, prm.plat_veto, rows_at(VETO_FLANK_S, fps, false));
        }
    }

    let rev_gap = rows_at(EVENT_GAP_S, fps, false);

    for w in shot_edges.windows(2) {
        let (lo, hi) = (w[0], w[1]);
        if hi <= lo + 1 || hi > n_all {
            continue;
        }
        let v = &vmarg[lo..hi];
        let n = hi - lo;
        if n < 2 {
            continue;
        }
        let s = box_same(v, k_smooth);
        let sg: Vec<f64> = s
            .iter()
            .map(|x| if *x > 0.0 { 1.0 } else if *x < 0.0 { -1.0 } else { 1.0 })
            .collect();
        let mut cross: Vec<usize> = Vec::new();
        for i in 1..n {
            if sg[i] * sg[i - 1] < 0.0 {
                cross.push(i);
            }
        }
        let mut seg_dirs: Option<Vec<f64>> = None;

        if prm.viterbi {
            if let Some((rt, rb)) = rev {
                let rtv = nan_to(&rt[lo..hi], 0.0);
                let rbv = nan_to(&rb[lo..hi], 0.0);
                let bslice: &[f64] = if rev_bias.len() == 1 {
                    rev_bias
                } else if rev_bias.len() > lo {
                    &rev_bias[lo..hi.min(rev_bias.len())]
                } else {
                    // 短 bias（1 < len <= lo）：没有 per-镜头信息可对齐，
                    // 退化为整片同值——切片 start>end 会直接 panic（pub API 地雷）
                    &rev_bias[rev_bias.len() - 1..]
                };
                let (vrows, vkinds) = alternating_events(&rtv, &rbv, bslice, rev_gap);
                let keep: Vec<usize> = vrows
                    .iter()
                    .copied()
                    .filter(|&r| r + 1 <= n - 1)
                    .collect();
                if !keep.is_empty() {
                    let kinds: Vec<i8> = keep
                        .iter()
                        .map(|r| {
                            let pos = vrows.iter().position(|x| x == r).unwrap();
                            vkinds[pos]
                        })
                        .collect();
                    cross = keep.iter().map(|r| r + 1).collect();
                    let mut dirs: Vec<f64> = kinds.iter().map(|k| *k as f64).collect();
                    if let Some(last) = kinds.last() {
                        dirs.push(-(*last) as f64);
                    }
                    seg_dirs = Some(dirs);
                }
            }
        }

        // 无 Viterbi 事件的镜头回退到过零分割：原型仍会把 crossing 吸附到
        // rev head 的局部 argmax（±rev_snap 行，方向感知、保序）。
        if seg_dirs.is_none() && prm.rev_snap > 0 {
            if let Some((rt, rb)) = rev {
                if !cross.is_empty() {
                    let rtv = nan_to(&rt[lo..hi], 0.0);
                    let rbv = nan_to(&rb[lo..hi], 0.0);
                    let mut snapped: Vec<usize> = Vec::with_capacity(cross.len());
                    let mut prev = 0usize;
                    let mut b0: Vec<usize> = vec![0];
                    b0.extend(cross.iter().copied());
                    for (&a, &c) in b0[..b0.len() - 1].iter().zip(cross.iter()) {
                        let d = if c > a {
                            let m = s[a..c].iter().sum::<f64>() / (c - a) as f64;
                            if m > 0.0 {
                                1.0
                            } else if m < 0.0 {
                                -1.0
                            } else {
                                0.0
                            }
                        } else {
                            0.0
                        };
                        let track: &[f64] = if d > 0.0 { &rtv } else { &rbv };
                        let w0 = prev.max((c - 1).saturating_sub(prm.rev_snap));
                        let w1 = (n - 2).min(c - 1 + prm.rev_snap);
                        let c2 = if w1 < w0 {
                            c
                        } else {
                            let mut best = 0usize;
                            for (i, &v) in track[w0..=w1].iter().enumerate() {
                                if v > track[w0 + best] {
                                    best = i;
                                }
                            }
                            w0 + best + 1
                        };
                        if prev < c2 && c2 < n {
                            snapped.push(c2);
                            prev = c2;
                        }
                    }
                    cross = snapped;
                }
            }
        }

        let mut bounds: Vec<usize> = vec![0];
        bounds.extend(cross.iter().copied());
        bounds.push(n);

        let mut cur = lv[lo];
        for (si, w2) in bounds.windows(2).enumerate() {
            let (a, b) = (w2[0], w2[1]);
            if b <= a {
                continue;
            }
            let d = match &seg_dirs {
                Some(dirs) => dirs.get(si).copied().unwrap_or(0.0),
                None => {
                    let m = s[a..b].iter().sum::<f64>() / (b - a) as f64;
                    if m > 0.0 {
                        1.0
                    } else if m < 0.0 {
                        -1.0
                    } else {
                        0.0
                    }
                }
            };
            let seg_env = ev[lo + a..lo + b].iter().sum::<f64>() / (b - a) as f64;
            let tr_env = seg_env * (b - a) as f64 * dt;
            let tr_raw = v[a..b].iter().map(|x| x.abs()).sum::<f64>() * dt;
            // ⚠ 静止门看的是 **均值(|v|)**，不是 |均值(v)| —— 段内速度会变号，
            // 用 |均值| 会把大量正常行程误判成静止（实测造成 10 个位置单位的偏差）
            let vabs_mean =
                v[a..b].iter().map(|x| x.abs()).sum::<f64>() / (b - a).max(1) as f64;
            let is_still = vabs_mean < prm.still_eps;
            if tr_raw <= 1e-9 || d == 0.0 || tr_env <= 1e-9 || is_still {
                if d != 0.0 && tr_raw > 1e-9 && tr_env > 1e-9 {
                    let end = (lv[lo + b - 1] + d * tr_raw.min(tr_env).min(100.0) / 2.0)
                        .clamp(0.0, 100.0);
                    let mut acc = 0.0;
                    for i in a..b {
                        acc += v[i].abs() * dt;
                        let frac = (acc / tr_raw).clamp(0.0, 1.0);
                        p[lo + i] = cur + (end - cur) * frac;
                    }
                    cur = end;
                } else {
                    for i in a..b {
                        p[lo + i] = lv[lo + i];
                    }
                    cur = lv[lo + b - 1];
                }
                continue;
            }

            let mut end = (lv[lo + b - 1] + d * tr_env.min(100.0) / 2.0).clamp(0.0, 100.0);
            if let (Some(bl), Some(bh)) = (blo.as_ref(), bhi.as_ref()) {
                if d < 0.0 && end < bl[lo + b - 1] + prm.ext_snap {
                    end = bl[lo + b - 1].clamp(0.0, 100.0);
                } else if d > 0.0 && end > bh[lo + b - 1] - prm.ext_snap {
                    end = bh[lo + b - 1].clamp(0.0, 100.0);
                }
            }
            if seg_dirs.is_some() && (end - cur).signum() != d {
                end = (cur + d * tr_env.min(100.0) / 2.0).clamp(0.0, 100.0);
            }
            end = amp_bound(end, cur, tr_raw, b - a, prm.amp_cap_x);
            let lim = MAX_POS_RATE * (b - a) as f64 * dt;
            if (end - cur).abs() > lim {
                end = (cur + (end - cur).signum() * lim).clamp(0.0, 100.0);
            }
            let mut acc = 0.0;
            for i in a..b {
                acc += v[i].abs() * dt;
                let frac = (acc / tr_raw).clamp(0.0, 1.0);
                p[lo + i] = cur + (end - cur) * frac;
            }
            cur = end;
        }
    }

    if let Some(k) = dk {
        let (rlo, rhi) = match (blo.as_ref(), bhi.as_ref()) {
            (Some(a), Some(b)) => (a.clone(), b.clone()),
            _ => (lv.clone(), lv.clone()),
        };
        p = level_lock(
            &p,
            &k,
            &rlo,
            &rhi,
            rows_at(LOCK_SMOOTH_S, fps, true),
            rows_at(LOCK_EDGE_S, fps, false),
            plat,
            prm.plat_soft,
            prm.plat_rail_track,
            prm.plat_shift_cap,
        );
    }
    p
}

const BIAS_FIT_S: f64 = 8000.0;
const SPEED_REF_S: f64 = 0.6;

/// 零填充居中平滑（`np.convolve(..., mode="same")` 的同义物）。
fn box_zero_padded(x: &[f64], k: usize) -> Vec<f64> {
    let k = k | 1;
    let half = (k / 2) as isize;
    let inv = 1.0 / k as f64;
    (0..x.len())
        .map(|i| {
            let mut s = 0.0;
            for j in -half..=half {
                let idx = i as isize + j;
                if idx >= 0 && (idx as usize) < x.len() {
                    s += x[idx as usize];
                }
            }
            s * inv
        })
        .collect()
}

/// 逐 band 二分拟合发射先验（对齐 Python `fit_emission_bias_bands`）。
fn fit_emission_bias_bands(
    p_peak: &[f64],
    p_valley: &[f64],
    band_of: &[usize],
    targets: [usize; 2],
    min_gap: usize,
) -> [f64; 2] {
    let mut b = [0.0f64; 2];
    for _ in 0..2 {
        for k in 0..2 {
            let (mut lo, mut hi) = (-6.0f64, 6.0f64);
            for _ in 0..16 {
                let mid = 0.5 * (lo + hi);
                b[k] = mid;
                let bias: Vec<f64> = band_of.iter().map(|&x| b[x]).collect();
                let (rows, _) = alternating_events(p_peak, p_valley, &bias, min_gap);
                let n = rows
                    .iter()
                    .filter(|&&r| band_of.get(r).copied() == Some(k))
                    .count();
                if n == targets[k] {
                    lo = mid;
                    hi = mid;
                    break;
                }
                if n < targets[k] {
                    lo = mid;
                } else {
                    hi = mid;
                }
                if hi - lo < 1e-3 {
                    break;
                }
            }
            b[k] = 0.5 * (lo + hi);
        }
    }
    b
}

/// 反转发射先验：按“静止/移动”两个 band 拟合，返回逐行 bias。
///
/// 原型 `jepa_infer.fit_rev_bias`：bias=0 时 Viterbi 会欠发射，慢行程被合并
/// 成驻留段——动作点因此大幅减少。必须在样式解码前做。
pub fn fit_rev_bias(
    rev_top: &[f64],
    rev_bot: &[f64],
    vel: &[f64],
    fps: f64,
    still_eps: f64,
) -> Vec<f64> {
    let n = rev_top.len().min(rev_bot.len()).min(vel.len());
    if n == 0 {
        return Vec::new();
    }
    let rt: Vec<f64> = rev_top[..n]
        .iter()
        .map(|x| if x.is_finite() { *x } else { 0.0 })
        .collect();
    let rb: Vec<f64> = rev_bot[..n]
        .iter()
        .map(|x| if x.is_finite() { *x } else { 0.0 })
        .collect();
    let cap = ((BIAS_FIT_S * fps).round() as usize).min(n).max(1);
    let k = rows_at(SPEED_REF_S, fps, true);
    let abs_vel: Vec<f64> = vel[..n].iter().map(|x| x.abs()).collect();
    let smooth = box_zero_padded(&abs_vel, k);
    let band_of: Vec<usize> = smooth
        .iter()
        .map(|v| if *v >= still_eps { 1 } else { 0 })
        .collect();
    let mut want_f = [0.0f64; 2];
    for i in 0..cap {
        want_f[band_of[i]] += rt[i] + rb[i];
    }
    let targets = [
        want_f[0].round().max(0.0) as usize,
        want_f[1].round().max(0.0) as usize,
    ];
    let bb = fit_emission_bias_bands(
        &rt[..cap],
        &rb[..cap],
        &band_of[..cap],
        targets,
        rows_at(EVENT_GAP_S, fps, false),
    );
    band_of.iter().map(|&k| bb[k]).collect()
}

/// 带亚帧时刻的位置合成：返回 (位置曲线, 行号->精确毫秒)。
///
/// `subframe_rev` 打开时，每条内部边界（镜头内反转的顶点行）用事件头概率做三点
/// 抛物线拟合，把时刻挪到行与行之间——这是 Python `subframe="rev"` 的等价物，
/// 也是动作点时间精度的最后一层。
#[allow(clippy::too_many_arguments)]
pub fn style_positions_with_sub(
    vmarg: &[f64],
    level: &[f64],
    env_script: &[f64],
    shot_edges: &[usize],
    band: Option<(&[f64], &[f64])>,
    plat: Option<(&[f64], &[f64])>,
    rev: Option<(&[f64], &[f64])>,
    rev_bias: &[f64],
    prm: &StyleParams,
    times_ms: &[f64],
    subframe_rev: bool,
) -> (Vec<f64>, HashMap<usize, f64>, Vec<usize>) {
    let p = style_positions_composed(
        vmarg, level, env_script, shot_edges, band, plat, rev, rev_bias, prm,
    );
    let mut sub: HashMap<usize, f64> = HashMap::new();
    // Viterbi 反转顶点行：必须交给 extrema_actions 强制出点，否则平滑会把
    // 快速反转抹平（原型 force_out 的同义物）。
    let mut force: Vec<usize> = Vec::new();
    if !subframe_rev {
        return (p, sub, force);
    }
    let Some((rt, rb)) = rev else {
        return (p, sub, force);
    };
    let n_all = vmarg.len();
    let rev_gap = rows_at(EVENT_GAP_S, prm.fps, false);
    let mut edges: Vec<usize> = shot_edges.to_vec();
    if edges.last() != Some(&n_all) {
        edges.push(n_all);
    }
    for w in edges.windows(2) {
        let (lo, hi) = (w[0], w[1]);
        if hi <= lo + 1 || hi > n_all {
            continue;
        }
        let n = hi - lo;
        let mut cross: Vec<usize> = Vec::new();
        let mut seg_dirs: Option<Vec<f64>> = None;
        if prm.viterbi {
            if let Some((rtt, rbb)) = rev {
                let rtv = nan_to(&rtt[lo..hi], 0.0);
                let rbv = nan_to(&rbb[lo..hi], 0.0);
                let bslice: &[f64] = if rev_bias.len() == 1 {
                    rev_bias
                } else if rev_bias.len() > lo {
                    &rev_bias[lo..hi.min(rev_bias.len())]
                } else {
                    // 短 bias（1 < len <= lo）：没有 per-镜头信息可对齐，
                    // 退化为整片同值——切片 start>end 会直接 panic（pub API 地雷）
                    &rev_bias[rev_bias.len() - 1..]
                };
                let (vrows, vkinds) = alternating_events(&rtv, &rbv, bslice, rev_gap);
                let kept: Vec<(usize, i8)> = vrows
                    .iter()
                    .copied()
                    .zip(vkinds.iter().copied())
                    .filter(|(r, _)| r + 1 <= n - 1)
                    .collect();
                if !kept.is_empty() {
                    cross = kept.iter().map(|(r, _)| r + 1).collect();
                    // 原型：force_out.extend(lo + c - 1)，c = vrow + 1
                    force.extend(kept.iter().map(|(r, _)| lo + r));
                    let mut dirs: Vec<f64> = kept.iter().map(|(_, k)| *k as f64).collect();
                    if let Some((_, last)) = kept.last() {
                        dirs.push(-(*last) as f64);
                    }
                    seg_dirs = Some(dirs);
                }
            }
        }
        if seg_dirs.is_none() {
            continue; // 没有 viterbi 事件就没有"被调用的顶点"，不做亚帧
        }
        let mut bounds: Vec<usize> = vec![0];
        bounds.extend(cross.iter().copied());
        bounds.push(n);
        let dirs = seg_dirs.unwrap();
        // 只处理**内部**边界：最后一个 bounds 是段尾，没有下一个段可对齐
        for si in 0..bounds.len().saturating_sub(2) {
            let b = bounds[si + 1];
            if b == 0 || b > n {
                continue;
            }
            let f = b - 1;
            let gl = lo + f;
            let d = dirs.get(si).copied().unwrap_or(0.0);
            if d == 0.0 {
                continue;
            }
            let dt_ms = if gl + 1 < times_ms.len() {
                times_ms[gl + 1] - times_ms[gl]
            } else {
                1000.0 / prm.fps
            };
            let y: &[f64] = if d > 0.0 { rt } else { rb };
            if gl >= 1 && gl < y.len() - 1 {
                let dlt = crate::style::subrow_offset(y, gl, 1, 0.5);
                sub.insert(gl, times_ms[gl] + dlt * dt_ms);
            }
        }
    }
    (p, sub, force)
}
// 说明：`style_to_actions`/`slew` 曾是无调用方的死代码（生产出点走
// `extrema_actions` 直调），且 slew 在 shot_edges.len()==1 时会构造 [1..0]
// 切片 panic，2026-10 审查后删除——生产口径的入口只有 extrema_actions。

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn runs_finds_spans() {
        let m = vec![false, true, true, false, true];
        assert_eq!(runs(&m), vec![(1, 3), (4, 5)]);
    }

    #[test]
    fn box_same_is_zero_padded() {
        let x = vec![10.0; 5];
        let k = 3;
        let out = box_same(&x, k);
        assert!((out[2] - 10.0).abs() < 1e-9, "中间应保持");
        assert!(out[0] < 10.0, "零填充使边缘下沉：{}", out[0]);
        assert!((out[0] - 20.0 / 3.0).abs() < 1e-9);
    }

    #[test]
    fn local_mean_is_edge_padded() {
        let x = vec![10.0; 5];
        let out = local_mean(&x, 3);
        assert!((out[0] - 10.0).abs() < 1e-9, "边沿填充保持不变，实际 {}", out[0]);
    }

    #[test]
    fn dwell_kind_hysteresis_and_peak() {
        // 一段高概率 → 判为顶
        let pt = vec![0.0, 0.1, 0.6, 0.6, 0.1, 0.0, 0.0, 0.0];
        let pb = vec![0.0; 8];
        let k = dwell_kind(&pt, &pb, 0.25, 0.15, 2, 2, 0.45);
        assert!(k.iter().any(|x| *x == 1), "应识别出顶部驻留");

        // 峰值不足 0.45 → 整段丢掉
        let pt2 = vec![0.0, 0.1, 0.3, 0.3, 0.1, 0.0, 0.0, 0.0];
        let k2 = dwell_kind(&pt2, &pb, 0.25, 0.15, 2, 2, 0.45);
        assert!(k2.iter().all(|x| *x == 0), "峰值不足应被丢");
    }

    #[test]
    fn stroke_veto_drops_isolated_calls() {
        let mut kind = vec![0i8; 40];
        for x in kind[20..24].iter_mut() {
            *x = 1;
        }
        // 两侧都没有速度 → 否决
        let flat = vec![0.0f64; 40];
        stroke_veto(&mut kind, &flat, 15.0, 10);
        assert!(kind.iter().all(|x| *x == 0));

        // 两侧有速度 → 保留
        let mut kind2 = vec![0i8; 40];
        for x in kind2[20..24].iter_mut() {
            *x = 1;
        }
        let mut burst = vec![0.0f64; 40];
        for x in burst[10..20].iter_mut() {
            *x = 100.0;
        }
        stroke_veto(&mut kind2, &burst, 15.0, 10);
        assert_eq!(kind2[21], 1, "有邻接行程时不该否决");
    }

    #[test]
    fn alternating_events_respects_alternation() {
        // 交替的强概率：峰在 5、谷在 15、峰在 25
        let mut pp = vec![0.01f64; 40];
        let mut pv = vec![0.01f64; 40];
        pp[5] = 0.95;
        pv[15] = 0.95;
        pp[25] = 0.95;
        let (rows, kinds) = alternating_events(&pp, &pv, &[0.0], 2);
        assert!(!rows.is_empty());
        // 相邻事件类型必须相反
        for w in kinds.windows(2) {
            assert_ne!(w[0], w[1], "同类型相邻不合法");
        }
        // 事件间隔 >= min_gap + 1
        for w in rows.windows(2) {
            assert!(w[1] - w[0] >= 3, "不应期未生效：{:?}", rows);
        }
    }

    #[test]
    fn amp_bound_limits_depth_only() {
        // 上限 = 4 * 10 = 40；这一步想走 80 → 截到 40
        let e = amp_bound(90.0, 10.0, 10.0, 30, 4.0);
        assert!((e - 50.0).abs() < 1e-9, "实际 {e}");
        // 关掉就不动
        assert_eq!(amp_bound(90.0, 10.0, 10.0, 30, 0.0), 90.0);
    }

    #[test]
    fn compose_produces_bounded_curve() {
        let n = 240;
        let vmarg: Vec<f64> = (0..n)
            .map(|i| 0.4 * ((i as f64) / 10.0).sin())
            .collect();
        let level = vec![50.0f64; n];
        let env = vec![0.3f64; n];
        let p = style_positions_composed(
            &vmarg,
            &level,
            &env,
            &[0, n],
            None,
            None,
            None,
            &[0.0],
            &StyleParams {
                viterbi: false,
                ..Default::default()
            },
        );
        assert_eq!(p.len(), n);
        for x in &p {
            assert!((0.0..=100.0).contains(x), "越界：{x}");
        }
        // 有行程（不是一条直线）
        let lo = p.iter().cloned().fold(f64::INFINITY, f64::min);
        let hi = p.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
        assert!(hi - lo > 1.0, "应当产生行程，实际跨度 {}", hi - lo);
    }
}
