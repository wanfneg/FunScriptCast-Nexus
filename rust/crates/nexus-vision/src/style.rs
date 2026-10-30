//! 样式机的"转折点抽取"层：位置曲线 → funscript 动作点。
//!
//! 逐条移植 `goblintrain/jepa_infer.py:extrema_actions` 及其依赖
//! （`ease_cut_seams` / `slew_cut_seams` / `common.rdp_actions`）。
//!
//! 为什么先搬这一层：它把"连续的位置曲线"变成"离散的下发点"，
//! 是**输出口径**所在——写错一位，设备走出来的行程就变。
//!
//! 顺序不可调换（官方注释原话：ease → RDP → slew，"the same order style.rs owns"）：
//! 1. 每镜头内平滑 → 找换向行 → 加镜头端点（+ 强制行）
//! 2. ease：丢掉过快的**接缝**端点（镜头切换处那两个被强行插进去的点）
//! 3. RDP：抽稀近共线的点
//! 4. slew：RDP 之后再压一次接缝速度（此时写出的接缝才是设备真正播放的）

use std::collections::HashMap;

use crate::buffer::Action;

/// 平滑窗口（秒）：解码的换向平滑。
pub const REV_SMOOTH_S: f64 = 0.1;
/// 接缝邻域：切点前后各多少行（半行边界，避免浮点决定归属）。
pub const SEAM_BACK_ROWS: f64 = 2.5;
pub const SEAM_FWD_ROWS: f64 = 1.5;

/// 时长 → 行数（至少 1 行）。`odd` 用于对称卷积核。
pub fn rows_at(seconds: f64, row_hz: f64, odd: bool) -> usize {
    let n = ((seconds * row_hz).round() as i64).max(1) as usize;
    if odd {
        n | 1
    } else {
        n
    }
}

/// 带内部标记的中间动作点（`_seam`/`_open`/`_shot` 在写出前会剥掉）。
#[derive(Debug, Clone)]
struct Marked {
    at: f64,
    pos: f64,
    shot: usize,
    seam: bool,
    open: bool,
}

/// Douglas-Peucker（垂直偏差，pos 单位）。`eps <= 0` 时原样返回。
pub fn rdp_actions(actions: &[Action], eps: f64) -> Vec<Action> {
    if actions.len() < 3 || eps <= 0.0 {
        return actions.to_vec();
    }
    let n = actions.len();
    let at: Vec<f64> = actions.iter().map(|a| a.at).collect();
    let pos: Vec<f64> = actions.iter().map(|a| a.pos).collect();
    let mut keep = vec![false; n];
    keep[0] = true;
    keep[n - 1] = true;
    let mut stack = vec![(0usize, n - 1)];
    while let Some((a, b)) = stack.pop() {
        if b < a + 2 {
            continue;
        }
        let denom = (at[b] - at[a]).max(1e-9);
        let mut best_d = -1.0f64;
        let mut best_k = a;
        for i in (a + 1)..b {
            let line = pos[a] + (pos[b] - pos[a]) * (at[i] - at[a]) / denom;
            let d = (pos[i] - line).abs();
            if d > best_d {
                best_d = d;
                best_k = i;
            }
        }
        if best_d > eps {
            keep[best_k] = true;
            stack.push((a, best_k));
            stack.push((best_k, b));
        }
    }
    actions
        .iter()
        .zip(keep.iter())
        .filter(|(_, k)| **k)
        .map(|(a, _)| *a)
        .collect()
}

fn prev_kept(keep: &[bool], i: usize) -> Option<usize> {
    let mut j = i;
    while j > 0 {
        j -= 1;
        if keep[j] {
            return Some(j);
        }
    }
    None
}

fn next_kept(keep: &[bool], i: usize) -> Option<usize> {
    ((i + 1)..keep.len()).find(|&j| keep[j])
}

/// 丢掉"过快的接缝端点"：镜头切换处两个被强行插入的边界点。
///
/// 每个镜头至少要留一个点；每侧每次最多丢一个（丢掉一个只会让接缝更长，
/// 不会连锁）。
fn ease_cut_seams(actions: Vec<Marked>, limit: f64) -> Vec<Marked> {
    let n = actions.len();
    let mut keep = vec![true; n];
    let too_fast = |j: Option<usize>, k: Option<usize>| -> bool {
        let (Some(j), Some(k)) = (j, k) else {
            return false;
        };
        if k >= n {
            return false;
        }
        let dt = actions[k].at - actions[j].at;
        dt > 0.0 && (actions[k].pos - actions[j].pos).abs() * 1000.0 / dt > limit
    };

    for i in 0..n {
        if !actions[i].seam {
            continue;
        }
        let k = next_kept(&keep, i);
        let j = prev_kept(&keep, i);
        let (Some(k), Some(j)) = (k, j) else { continue };
        if actions[j].shot != actions[i].shot || !too_fast(Some(i), Some(k)) {
            continue;
        }
        keep[i] = false;
        let m = next_kept(&keep, k);
        if actions[k].open
            && m.map(|m| actions[m].shot == actions[k].shot).unwrap_or(false)
            && too_fast(Some(j), Some(k))
        {
            keep[k] = false;
        }
    }
    actions
        .into_iter()
        .zip(keep.iter())
        .filter(|(_, k)| **k)
        .map(|(a, _)| a)
        .collect()
}

/// 接缝硬上限：切点附近任何写出的换向都不快于 `limit`（按**深度**截断，时间不动）。
fn slew_cut_seams(actions: &mut [Marked], cut_times_ms: &[f64], limit: f64, row_ms: f64) {
    if actions.len() < 2 {
        return;
    }
    let ats: Vec<f64> = actions.iter().map(|a| a.at).collect();
    for &c in cut_times_ms {
        let back = c - SEAM_BACK_ROWS * row_ms;
        // bisect_left(ats, back) - 1, 钳到 0
        let mut idx = ats.partition_point(|&x| x < back);
        let mut j = idx.saturating_sub(1);
        let _ = &mut idx;
        while j + 1 < actions.len() {
            let (a_at, a_pos) = (actions[j].at, actions[j].pos);
            let (b_at, b_pos) = (actions[j + 1].at, actions[j + 1].pos);
            let dt = b_at - a_at;
            if dt > 0.0 {
                let d = b_pos - a_pos;
                let lim = limit * dt / 1000.0;
                if d.abs() > lim {
                    let step = lim as i64; // 截断（四舍五入可能越过上限）
                    let np = a_pos + (if d > 0.0 { step as f64 } else { -(step as f64) });
                    actions[j + 1].pos = np.clamp(0.0, 100.0);
                } else if b_at > c + SEAM_FWD_ROWS * row_ms {
                    break;
                }
            }
            j += 1;
        }
    }
}

/// 位置曲线 → 动作点（funscript）。
///
/// `p` 是逐行位置（0..100），`times_ms` 是逐行时间戳，
/// `shot_edges` 是镜头边界行号（含 0 与 n）。
#[allow(clippy::too_many_arguments)]
pub fn extrema_actions(
    p: &[f64],
    times_ms: &[f64],
    shot_edges: &[usize],
    sub: Option<&HashMap<usize, f64>>,
    force: Option<&[usize]>,
    cut_ease: f64,
    rdp_eps: f64,
    fps: f64,
) -> Vec<Action> {
    let kr = rows_at(REV_SMOOTH_S, fps, true);
    let half = (kr / 2) as isize;
    let inv = 1.0 / kr as f64;

    let mut acts: Vec<Marked> = Vec::new();
    let mut edges: Vec<usize> = shot_edges.to_vec();
    if edges.last() != Some(&p.len()) {
        edges.push(p.len());
    }

    for si in 0..edges.len().saturating_sub(1) {
        let (lo, hi) = (edges[si], edges[si + 1]);
        if hi <= lo + 1 {
            continue; // 段太短
        }
        let seg = &p[lo..hi];
        let n = seg.len();

        // 平滑：零填充的对称均值（numpy convolve mode="same"，奇数核）
        let mut s = vec![0.0f64; n];
        for i in 0..n {
            let mut acc = 0.0;
            for k in 0..kr {
                let src = i as isize - half + k as isize;
                if src >= 0 && (src as usize) < n {
                    acc += seg[src as usize];
                }
            }
            s[i] = acc * inv;
        }

        // 一阶差分取符号，0 当 +1；符号翻转处即换向
        let mut d: Vec<i32> = Vec::with_capacity(n.saturating_sub(1));
        for i in 1..n {
            let v = s[i] - s[i - 1];
            d.push(if v > 0.0 {
                1
            } else if v < 0.0 {
                -1
            } else {
                1
            });
        }
        let mut idx: Vec<usize> = vec![0];
        for i in 1..d.len() {
            if d[i] * d[i - 1] < 0 {
                idx.push(i);
            }
        }
        idx.push(n - 1);
        if let Some(fr) = force {
            for &r in fr {
                if r >= lo && r < hi {
                    idx.push(r - lo);
                }
            }
        }
        idx.sort_unstable();
        idx.dedup();

        for &i in &idx {
            let mut t = times_ms[lo + i];
            if let Some(sm) = sub {
                let base_r = lo + i;
                for r in [base_r, base_r.saturating_sub(1), base_r + 1] {
                    if let Some(&v) = sm.get(&r) {
                        t = v;
                        break;
                    }
                }
            }
            acts.push(Marked {
                at: t.round(),
                pos: seg[i].clamp(0.0, 100.0).round(),
                shot: si,
                seam: i == n - 1 && hi < *edges.last().unwrap(),
                open: i == 0 && lo > edges[0],
            });
        }
    }

    acts.sort_by(|a, b| a.at.partial_cmp(&b.at).unwrap_or(std::cmp::Ordering::Equal));
    // 同一毫秒只留第一个
    let mut out: Vec<Marked> = Vec::with_capacity(acts.len());
    for a in acts {
        if out.last().map(|p: &Marked| a.at > p.at).unwrap_or(true) {
            out.push(a);
        }
    }

    if cut_ease > 0.0 {
        out = ease_cut_seams(out, cut_ease);
    }
    for a in out.iter_mut() {
        a.shot = 0;
        a.seam = false;
        a.open = false;
    }

    let mut plain: Vec<Action> = out.into_iter().map(|m| Action { at: m.at, pos: m.pos }).collect();
    if rdp_eps > 0.0 {
        plain = rdp_actions(&plain, rdp_eps);
    }
    if cut_ease > 0.0 && edges.len() > 1 {
        // 空 shot_edges 时 edges 只有 [p.len()] 一个元素，[1..0] 会 panic——
        // 没有镜头切点就不做切点缓动
        let cuts: Vec<f64> = edges[1..edges.len().saturating_sub(1)]
            .iter()
            .filter(|&&e| e < times_ms.len())
            .map(|&e| times_ms[e])
            .collect();
        // 这里需要 Marked 才能就地改 pos；转换一次再转回
        let mut marked: Vec<Marked> = plain
            .iter()
            .map(|a| Marked {
                at: a.at,
                pos: a.pos,
                shot: 0,
                seam: false,
                open: false,
            })
            .collect();
        slew_cut_seams(&mut marked, &cuts, cut_ease, 1000.0 / fps);
        plain = marked.into_iter().map(|m| Action { at: m.at, pos: m.pos }).collect();
    }
    plain
}


/// 亚帧时刻：事件头峰值在 `gl` 附近的**行内偏移**（单位：行）。
///
/// 三点抛物线拟合，最多移动 `clamp` 行。模板越界或没有内部极大值就落在本行。
/// 与 `jepa_infer.subrow_offset` 逐条对齐。
pub fn subrow_offset(y: &[f64], gl: usize, half: usize, clamp: f64) -> f64 {
    let lo = gl as isize - half as isize;
    let hi = gl as isize + half as isize;
    if lo < 0 || hi >= y.len() as isize {
        return 0.0;
    }
    let get = |i: isize| -> f64 {
        let v = y[i as usize];
        if v.is_finite() {
            v
        } else {
            0.0
        }
    };
    let (y0, y1, y2) = (
        get(lo + half as isize - 1),
        get(lo + half as isize),
        get(lo + half as isize + 1),
    );
    let den = y0 - 2.0 * y1 + y2;
    let dlt = if den.abs() < 1e-9 {
        0.0
    } else {
        0.5 * (y0 - y2) / den
    };
    dlt.clamp(-clamp, clamp)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn subrow_offset_fits_parabola() {
        // 峰值精确落在中间行 → 偏移 0
        let y = vec![0.0, 1.0, 0.0];
        assert!((subrow_offset(&y, 1, 1, 0.5)).abs() < 1e-9);
        // 峰值偏左 → 负偏移，且被 clamp 住
        let y = vec![1.0, 0.9, 0.0];
        let d = subrow_offset(&y, 1, 1, 0.5);
        assert!((-0.5..0.0).contains(&d), "应向左偏：{d}");
        // 越界模板 → 0
        let y = vec![0.0, 1.0];
        assert_eq!(subrow_offset(&y, 1, 1, 0.5), 0.0);
    }

    #[test]
    fn rows_at_matches_python() {
        assert_eq!(rows_at(0.1, 15.0, true), 3); // round(1.5)=2? -> Python int(round(1.5))=2 -> |1 = 3
        assert_eq!(rows_at(0.1, 30.0, true), 3); // round(3.0)=3 -> 3
        assert_eq!(rows_at(0.0, 30.0, false), 1, "至少一行");
        assert_eq!(rows_at(0.1, 30.0, false), 3);
    }

    #[test]
    fn rdp_drops_collinear_points() {
        let a = vec![
            Action { at: 0.0, pos: 0.0 },
            Action { at: 100.0, pos: 5.0 },
            Action { at: 200.0, pos: 10.0 },
            Action { at: 300.0, pos: 60.0 },
        ];
        let out = rdp_actions(&a, 1.0);
        assert_eq!(out.len(), 3, "中间共线的 5.0 应被丢掉");
        assert_eq!(out[0].pos, 0.0);
        assert_eq!(out.last().unwrap().pos, 60.0);
    }

    #[test]
    fn rdp_keeps_real_reversals() {
        let a = vec![
            Action { at: 0.0, pos: 10.0 },
            Action { at: 100.0, pos: 90.0 },
            Action { at: 200.0, pos: 10.0 },
        ];
        assert_eq!(rdp_actions(&a, 1.0).len(), 3, "真换向不能被抽掉");
    }

    #[test]
    fn extrema_finds_reversals_on_sine() {
        // 一个完整正弦：应当抽出峰/谷/端点
        let n = 300;
        let p: Vec<f64> = (0..n)
            .map(|i| 50.0 + 40.0 * ((i as f64) / 30.0 * std::f64::consts::TAU / 2.0).sin())
            .collect();
        let times: Vec<f64> = (0..n).map(|i| i as f64 * (1000.0 / 30.0)).collect();
        let edges = vec![0usize, n];
        let acts = extrema_actions(&p, &times, &edges, None, None, 0.0, 0.0, 30.0);
        assert!(acts.len() >= 3, "至少要有起点/峰/终点，实际 {}", acts.len());
        // 时间单调
        for w in acts.windows(2) {
            assert!(w[1].at > w[0].at, "动作点时间必须严格递增");
        }
        // 位置在 0..100
        for a in &acts {
            assert!((0.0..=100.0).contains(&a.pos));
        }
    }

    #[test]
    fn flat_line_matches_python_three_points() {
        // 直观上直线只有两端，但**平滑是零填充的**：尾部窗口缺数据 → 平滑值下沉
        // → 尾部出现一次符号翻转。Python 实测同样给 3 个点（0/3929/3963），
        // 这里照抄它的行为，不"修正"成 2 —— 与 Python 一致比"看起来合理"重要。
        let n = 120;
        let p = vec![50.0; n];
        let times: Vec<f64> = (0..n).map(|i| i as f64 * 33.3).collect();
        let acts = extrema_actions(&p, &times, &[0, n], None, None, 0.0, 0.0, 30.0);
        assert_eq!(acts.len(), 3, "实际 {}", acts.len());
        assert!(acts.iter().all(|a| (a.pos - 50.0).abs() < 1e-9), "位置应恒为 50");
    }

    #[test]
    fn slew_clamps_seam_depth_but_keeps_time() {
        // 切点两侧一行之内位置从 0 跳到 90：速度远超 250 pos/s
        let mut acts = vec![
            Marked { at: 990.0, pos: 0.0, shot: 0, seam: true, open: false },
            Marked { at: 1023.0, pos: 90.0, shot: 1, seam: false, open: true },
        ];
        let before_at = acts[1].at;
        slew_cut_seams(&mut acts, &[1000.0], 250.0, 1000.0 / 30.0);
        assert_eq!(acts[1].at, before_at, "时间不许动");
        assert!(acts[1].pos < 90.0, "深度应被截断，实际 {}", acts[1].pos);
        // lim = 250 * 33 / 1000 = 8.25 → step=8 → pos = 0 + 8
        assert!((acts[1].pos - 8.0).abs() < 1e-9, "实际 {}", acts[1].pos);
    }
}