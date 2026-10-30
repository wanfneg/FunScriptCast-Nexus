//! 静止门：动作点的置信度/速度低于阈值就不生成。
//!
//! 逐条移植 `vendor/vision/still_gate.py`（**必须逐值一致**：它砍掉约 22% 的动作点，
//! 是用户今天看到的成品的一部分）。
//!
//! 三种规则（mode）：
//! - `and`   —— conf >= conf_thr 且 |vmarg| >= vm_thr 才保留（最严）
//! - `or`    —— 任一达标就保留（最温和）
//! - `combo` —— (|vmarg|/vm_thr) * (conf/conf_thr) >= score_thr 才保留（推荐）
//!
//! 默认 conf_thr=0.70、vm_thr=50 pos/s、score_thr=0.70、窗口 ±0.2s。

use serde::{Deserialize, Serialize};

pub const CONF_THR: f64 = 0.70;
pub const VM_THR: f64 = 50.0;
pub const SCORE_THR: f64 = 0.70;
pub const WINDOW_S: f64 = 0.2;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum GateMode {
    /// 关（不门控）
    Off,
    And,
    Or,
    Combo,
}

impl GateMode {
    pub fn parse(s: &str) -> Self {
        match s.trim().to_lowercase().as_str() {
            "and" => GateMode::And,
            "or" => GateMode::Or,
            "off" | "none" | "0" | "false" | "" => GateMode::Off,
            _ => GateMode::Combo,
        }
    }
    pub fn is_off(self) -> bool {
        matches!(self, GateMode::Off)
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GateConfig {
    pub mode: GateMode,
    pub conf_thr: f64,
    pub vm_thr: f64,
    pub score_thr: f64,
    pub window_s: f64,
}

impl Default for GateConfig {
    fn default() -> Self {
        Self {
            mode: GateMode::Combo,
            conf_thr: CONF_THR,
            vm_thr: VM_THR,
            score_thr: SCORE_THR,
            window_s: WINDOW_S,
        }
    }
}

/// 从轨道算出逐行的 (速度 pos/s, 置信度)。
///
/// Python: `np.nan_to_num(|vmarg|) * v_std`；`np.nan_to_num(conf)`。
/// NaN/Inf 一律当 0（`np.nan_to_num` 的默认行为）。
pub fn signals(vmarg: &[f64], conf: &[f64], v_std: f64) -> (Vec<f64>, Vec<f64>) {
    // numpy `nan_to_num` 只替换 NaN→0；±Inf 变成 ±f64::MAX。
    // vmarg 先取绝对值，所以负 Inf 也变成 +MAX。
    let vm = vmarg
        .iter()
        .map(|v| {
            if v.is_nan() {
                0.0
            } else if v.is_infinite() {
                f64::MAX
            } else {
                v.abs() * v_std
            }
        })
        .collect();
    let c = conf
        .iter()
        .map(|v| {
            if v.is_nan() {
                0.0
            } else if *v == f64::INFINITY {
                f64::MAX
            } else if *v == f64::NEG_INFINITY {
                f64::MIN
            } else {
                *v
            }
        })
        .collect();
    (vm, c)
}

/// 窗口中位数：在 `times_sec` 上取 `[t-half, t+half)` 的中位数。
///
/// 对齐 `np.searchsorted(times, x)`（side='left'）与 `np.median`。
fn window_median(sig: &[f64], times_sec: &[f64], t: f64, half_s: f64) -> f64 {
    let lo = times_sec.partition_point(|&x| x < t - half_s);
    let hi = times_sec.partition_point(|&x| x < t + half_s);
    if hi <= lo || lo >= sig.len() {
        return 0.0;
    }
    let hi = hi.min(sig.len());
    let mut w: Vec<f64> = sig[lo..hi].to_vec();
    if w.is_empty() {
        return 0.0;
    }
    w.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let n = w.len();
    if n % 2 == 1 {
        w[n / 2]
    } else {
        (w[n / 2 - 1] + w[n / 2]) / 2.0
    }
}

/// 每个动作点是否保留，以及其对应的 conf/vm 值。
///
/// `times_sec` 是**秒**（Python 调用方传 `times/1000.0`）；
/// `actions_at_ms` 是动作点的毫秒时间。
pub fn keep_flags(
    actions_at_ms: &[f64],
    times_sec: &[f64],
    vm: &[f64],
    conf: &[f64],
    cfg: &GateConfig,
) -> (Vec<bool>, Vec<f64>, Vec<f64>) {
    let mut keep = Vec::with_capacity(actions_at_ms.len());
    let mut cv = Vec::with_capacity(actions_at_ms.len());
    let mut vv = Vec::with_capacity(actions_at_ms.len());
    for &at in actions_at_ms {
        let t = at / 1000.0;
        let c = window_median(conf, times_sec, t, cfg.window_s);
        let v = window_median(vm, times_sec, t, cfg.window_s);
        let k = match cfg.mode {
            GateMode::Off => true,
            GateMode::Or => c >= cfg.conf_thr || v >= cfg.vm_thr,
            GateMode::And => c >= cfg.conf_thr && v >= cfg.vm_thr,
            GateMode::Combo => {
                (v / cfg.vm_thr.max(1e-9)) * (c / cfg.conf_thr.max(1e-9)) >= cfg.score_thr
            }
        };
        keep.push(k);
        cv.push(c);
        vv.push(v);
    }
    (keep, cv, vv)
}

#[derive(Debug, Clone, Serialize, Default)]
pub struct GateStats {
    pub n_in: usize,
    pub n_out: usize,
    pub dropped: usize,
    pub conf_median_dropped: Option<f64>,
    pub vm_median_dropped: Option<f64>,
}

fn median(mut v: Vec<f64>) -> Option<f64> {
    if v.is_empty() {
        return None;
    }
    v.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let n = v.len();
    Some(if n % 2 == 1 {
        v[n / 2]
    } else {
        (v[n / 2 - 1] + v[n / 2]) / 2.0
    })
}

/// 过滤动作列表（`actions` 为 (at_ms, pos)），返回保留项与统计。
pub fn gate_actions(
    actions: &[(f64, f64)],
    times_sec: &[f64],
    vm: &[f64],
    conf: &[f64],
    cfg: &GateConfig,
) -> (Vec<(f64, f64)>, GateStats) {
    let ats: Vec<f64> = actions.iter().map(|a| a.0).collect();
    let (keep, cv, vv) = keep_flags(&ats, times_sec, vm, conf, cfg);
    let kept: Vec<(f64, f64)> = actions
        .iter()
        .zip(keep.iter())
        .filter(|(_, k)| **k)
        .map(|(a, _)| *a)
        .collect();
    let dropped_cv: Vec<f64> = cv
        .iter()
        .zip(keep.iter())
        .filter(|(_, k)| !**k)
        .map(|(v, _)| *v)
        .collect();
    let dropped_vv: Vec<f64> = vv
        .iter()
        .zip(keep.iter())
        .filter(|(_, k)| !**k)
        .map(|(v, _)| *v)
        .collect();
    let stats = GateStats {
        n_in: actions.len(),
        n_out: kept.len(),
        dropped: actions.len() - kept.len(),
        conf_median_dropped: median(dropped_cv),
        vm_median_dropped: median(dropped_vv),
    };
    (kept, stats)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg(mode: GateMode) -> GateConfig {
        GateConfig {
            mode,
            ..Default::default()
        }
    }

    #[test]
    fn signals_matches_nan_to_num() {
        let vm = vec![0.1, f64::NAN, -0.2, f64::INFINITY];
        let conf = vec![0.5, f64::NAN, 0.9, 1.0];
        let (v, c) = signals(&vm, &conf, 100.0);
        assert_eq!(v[0], 10.0);
        assert_eq!(v[1], 0.0, "NaN → 0");
        assert_eq!(v[2], 20.0, "取绝对值");
        assert!(v[3] > 1e300, "Inf → 极值（numpy nan_to_num 语义）");
        assert_eq!(c[0], 0.5);
        assert_eq!(c[1], 0.0, "NaN → 0");
    }

    /// numpy `nan_to_num` 只把 NaN 变 0：Inf 保留为极值（不能当 0 丢掉强动作）。
    #[test]
    fn signals_keep_infinity_like_numpy() {
        let (v, c) = signals(
            &[f64::INFINITY, f64::NEG_INFINITY],
            &[f64::INFINITY, f64::NAN],
            1.0,
        );
        assert!(v[0] > 1e300, "posinf 应保留为极大值，实际 {}", v[0]);
        assert!(v[1] > 1e300, "neginf 取绝对值后应为极大值，实际 {}", v[1]);
        assert!(c[0] > 1e300, "conf posinf 应保留");
        assert_eq!(c[1], 0.0, "NaN 才归零");
    }

    #[test]
    fn window_median_basic() {
        // 时间轴 0,0.1,...,1.0（秒），信号恒为 5
        let times: Vec<f64> = (0..=10).map(|i| i as f64 * 0.1).collect();
        let sig: Vec<f64> = vec![5.0; 11];
        assert_eq!(window_median(&sig, &times, 0.5, 0.2), 5.0);
        // 窗口落在时间轴之外 → 0
        assert_eq!(window_median(&sig, &times, 99.0, 0.2), 0.0);
    }

    #[test]
    fn window_median_even_count_averages_middle() {
        let times = vec![0.0, 0.1, 0.2, 0.3];
        let sig = vec![1.0, 2.0, 3.0, 4.0];
        // t=0.15, 窗口 [−0.05, 0.35) → 全部 4 个点 → 中位 = (2+3)/2
        assert_eq!(window_median(&sig, &times, 0.15, 0.2), 2.5);
    }

    #[test]
    fn combo_gate_matches_formula() {
        // v/vm_thr * c/conf_thr >= score_thr
        let times: Vec<f64> = (0..=100).map(|i| i as f64 * 0.1).collect();
        let vm: Vec<f64> = vec![50.0 / 100.0; 101]; // vm = 50 pos/s（乘 v_std=100）
        let conf: Vec<f64> = vec![0.70; 101];
        let (v, c) = signals(&vm, &conf, 100.0);
        assert!((v[0] - 50.0).abs() < 1e-9);

        let acts = vec![1000.0, 2000.0];
        let (keep, _, _) = keep_flags(&acts, &times, &v, &c, &cfg(GateMode::Combo));
        // score = 1 * 1 = 1 >= 0.7 → 保留
        assert_eq!(keep, vec![true, true]);
    }

    #[test]
    fn combo_gate_drops_low_signal() {
        let times: Vec<f64> = (0..=100).map(|i| i as f64 * 0.1).collect();
        // vm = 10 pos/s, conf = 0.3 → 0.2 * 0.43 = 0.086 < 0.7 → 丢
        let vm: Vec<f64> = vec![0.1; 101];
        let conf: Vec<f64> = vec![0.3; 101];
        let (v, c) = signals(&vm, &conf, 100.0);
        let acts = vec![1000.0];
        let (keep, _, _) = keep_flags(&acts, &times, &v, &c, &cfg(GateMode::Combo));
        assert_eq!(keep, vec![false]);
    }

    #[test]
    fn and_requires_both_or_requires_either() {
        let times: Vec<f64> = (0..=100).map(|i| i as f64 * 0.1).collect();
        // 高速度、低置信
        let vm = vec![0.8; 101]; // 80 pos/s
        let conf = vec![0.2; 101];
        let (v, c) = signals(&vm, &conf, 100.0);
        let acts = vec![1000.0];
        assert_eq!(keep_flags(&acts, &times, &v, &c, &cfg(GateMode::And)).0, vec![false]);
        assert_eq!(keep_flags(&acts, &times, &v, &c, &cfg(GateMode::Or)).0, vec![true]);
    }

    #[test]
    fn off_mode_keeps_everything() {
        let times = vec![0.0];
        let vm = vec![0.0];
        let conf = vec![0.0];
        let acts = vec![0.0, 1000.0];
        let (keep, _, _) = keep_flags(&acts, &times, &vm, &conf, &cfg(GateMode::Off));
        assert_eq!(keep, vec![true, true]);
    }

    #[test]
    fn gate_actions_reports_stats() {
        let times: Vec<f64> = (0..=100).map(|i| i as f64 * 0.1).collect();
        let vm: Vec<f64> = (0..=100)
            .map(|i| if i < 50 { 0.1 } else { 0.9 })
            .collect();
        let conf: Vec<f64> = (0..=100)
            .map(|i| if i < 50 { 0.2 } else { 0.95 })
            .collect();
        let (v, c) = signals(&vm, &conf, 100.0);
        let acts = vec![(1000.0, 10.0), (8000.0, 90.0)];
        let (kept, stats) = gate_actions(&acts, &times, &v, &c, &cfg(GateMode::Combo));
        assert_eq!(stats.n_in, 2);
        assert_eq!(stats.n_out, kept.len());
        assert_eq!(stats.dropped, 2 - kept.len());
        assert_eq!(kept.len(), 1, "低信号段应被丢掉");
        assert_eq!(kept[0].1, 90.0, "保留的是有动作的那一点");
    }

    #[test]
    fn mode_parsing() {
        assert_eq!(GateMode::parse("off"), GateMode::Off);
        assert_eq!(GateMode::parse(""), GateMode::Off);
        assert_eq!(GateMode::parse("and"), GateMode::And);
        assert_eq!(GateMode::parse("OR"), GateMode::Or);
        assert_eq!(GateMode::parse("combo"), GateMode::Combo);
        assert_eq!(GateMode::parse("whatever"), GateMode::Combo, "未知值回落 combo");
    }
}
