//! VAD 混合分段：滚动缓冲 + 切句裁决（对齐 `vendor/subtitle/hybrid_segmenter.py`）。
//!
//! **纯状态，不碰 ASR/VAD**：`feed()` 只回答"这一轮该不该切、切在哪"，
//! 真正的识别与 VAD 由调用方提供。这样切句逻辑可以单测，不用起模型。
//!
//! 三优先级切点（顺序不能改）：
//!   ① RMS 静音扫描 —— 免费，纯安静内容秒判（两级端点：缓冲过 4s 后尾窗 0.7s → 0.4s）
//!   ② VAD 语音区间隙 —— BGM 内容的真正裁判（音乐不是语音，语音区的间隙才是句间停顿）
//!   ③ 硬切兜底 —— 连续说话时封顶延迟

pub const SR: usize = 16000;

pub const SILENCE_THRESHOLD: f32 = 0.01;
pub const SILENCE_TAIL_SEC: f64 = 1.0;
/// 无 VAD 时的真静音峰值下限：数字零/纯底噪的峰值趋近 0，
/// 轻声/耳语的波形峰值仍明显高于此——RMS 单独判会把轻声整段循环丢弃。
pub const SILENT_PEAK_FLOOR: f32 = 0.002;
pub const MIN_PHRASE_SEC: f64 = 2.0;
pub const MAX_PHRASE_SEC: f64 = 5.0;
pub const REGION_MERGE_GAP: f64 = 0.3;
pub const MAX_SNAP_SEC: f64 = 0.6;
pub const JUMP_RESET_MS: i64 = 1000;
pub const GAP_RESET_MS: i64 = 10000;
pub const LONG_SILENCE_TAIL_SEC: f64 = 0.4;
pub const LONG_PHRASE_SEC: f64 = 4.0;

/// 切句原因。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Reason {
    /// 还没攒够，本轮不出句
    None,
    Standard,
    Hard,
    Vad,
    /// 整段被判静音（丢弃）
    Silent,
    /// 整段 RMS 很低但 VAD 说有语音 → 救回来照常出句
    VadRescued,
}

impl Reason {
    pub fn as_str(&self) -> &'static str {
        match self {
            Reason::None => "",
            Reason::Standard => "standard",
            Reason::Hard => "hard",
            Reason::Vad => "vad",
            Reason::Silent => "silent",
            Reason::VadRescued => "vad-rescued",
        }
    }
}

pub fn rms(x: &[f32]) -> f64 {
    if x.is_empty() {
        return 0.0;
    }
    let s: f64 = x.iter().map(|v| (*v as f64) * (*v as f64)).sum();
    (s / x.len() as f64).sqrt()
}

/// 峰值（最大绝对样本值）。
pub fn peak(x: &[f32]) -> f32 {
    x.iter().fold(0.0f32, |m, v| m.max(v.abs()))
}

/// VAD 语音区（秒，相对缓冲起点）。
pub type Regions = Vec<(f64, f64)>;

pub struct HybridBuffer {
    thr: f64,
    tail_sec: f64,
    min_sec: f64,
    max_sec: f64,
    long_tail_sec: f64,
    long_sec: f64,
    pcm: Vec<f32>,
    start_ms: i64,
    next_ms: i64,
    lang: String,
    /// 上一次切句时算出的语音区（供对齐复用）
    pub last_cut_regions: Option<Regions>,
}

impl Default for HybridBuffer {
    fn default() -> Self {
        Self::new()
    }
}

impl HybridBuffer {
    /// 生产用：按 `config.asr.hybrid` 构造（尾窗 0.7s，而不是模块默认 1.0s）。
    pub fn from_config(c: &crate::config::HybridConfig) -> Self {
        let mut b = Self::new();
        b.thr = c.silence_threshold;
        b.tail_sec = c.silence_tail_sec;
        b.long_tail_sec = c.long_silence_tail_sec;
        b.long_sec = c.long_phrase_sec;
        b.min_sec = c.min_phrase_sec;
        b.max_sec = c.max_phrase_sec;
        b
    }

    pub fn new() -> Self {
        Self {
            thr: SILENCE_THRESHOLD as f64,
            tail_sec: SILENCE_TAIL_SEC,
            min_sec: MIN_PHRASE_SEC,
            max_sec: MAX_PHRASE_SEC,
            long_tail_sec: LONG_SILENCE_TAIL_SEC,
            long_sec: LONG_PHRASE_SEC,
            pcm: Vec::new(),
            start_ms: 0,
            next_ms: 0,
            lang: String::new(),
            last_cut_regions: None,
        }
    }

    pub fn reset(&mut self) {
        self.pcm.clear();
        self.start_ms = 0;
        self.next_ms = 0;
    }

    pub fn buffered_samples(&self) -> usize {
        self.pcm.len()
    }

    /// partial 临时稿快照（与 Python `HybridBuffer.snapshot` 同义）：
    /// 返回当前缓冲拷贝与绝对起点；不足 `min_sec` 返回 None。
    pub fn snapshot(&self, min_sec: f64) -> Option<(Vec<f32>, i64)> {
        if (self.pcm.len() as f64 / SR as f64) < min_sec {
            return None;
        }
        Some((self.pcm.clone(), self.start_ms))
    }

    /// 拼块 + 切句裁决。返回 (切下的音频, 绝对起点 ms, 原因)。
    ///
    /// 时间轴规则：切句/丢静音只清**音频**，`next_ms` 必须续着切点——
    /// 头显的块带 1s 重叠，丢了切点会把重叠区重复入账（整段起点标早 1s）。
    pub fn feed(
        &mut self,
        pcm: &[f32],
        lang: &str,
        video_start_ms: i64,
        vad: Option<&dyn Fn(&[f32]) -> Option<Regions>>,
    ) -> (Option<Vec<f32>>, i64, Reason) {
        let lang = lang.split('-').next().unwrap_or("").trim().to_lowercase();
        if lang != self.lang {
            self.reset();
            self.lang = lang.clone();
        }
        // 会话级重置：缓冲非空时的跳变；缓冲空但时间轴离新块太远也一样全清
        let jump = |s: &Self, v: i64| -> bool {
            v < s.next_ms - JUMP_RESET_MS || v > s.next_ms + GAP_RESET_MS
        };
        if !self.pcm.is_empty() && jump(self, video_start_ms) {
            self.reset();
            self.lang = lang;
        } else if self.pcm.is_empty() && self.next_ms != 0 && jump(self, video_start_ms) {
            self.reset();
            self.lang = lang;
        }
        if self.pcm.is_empty() {
            let base = if self.next_ms != 0 { self.next_ms } else { video_start_ms };
            self.start_ms = base;
            self.next_ms = base;
        }
        // 重叠区丢弃：只追加缓冲末端之后的新音频；空洞补零保时间轴
        let skip = (self.next_ms - video_start_ms).max(0);
        if video_start_ms > self.next_ms {
            let gap_n = ((video_start_ms - self.next_ms) as f64 / 1000.0 * SR as f64).round() as i64;
            if gap_n > 0 {
                self.pcm.extend(std::iter::repeat_n(0.0f32, gap_n as usize));
                self.next_ms += (gap_n as f64 / SR as f64 * 1000.0).round() as i64;
            }
        }
        let skip_n = (skip as f64 / 1000.0 * SR as f64).round() as i64;
        let n = pcm.len() as i64 - skip_n;
        if n > 0 {
            let take = n as usize;
            self.pcm.extend_from_slice(&pcm[pcm.len() - take..]);
            self.next_ms += (n as f64 / SR as f64 * 1000.0).round() as i64;
        }

        let dur = self.pcm.len() as f64 / SR as f64;
        if dur < 0.5 {
            return (None, 0, Reason::None);
        }

        let step_n = (0.2 * SR as f64) as usize;
        let min_n = (self.min_sec * SR as f64) as usize;
        let max_n = (self.max_sec * SR as f64) as usize;
        let long_n = (self.long_sec * SR as f64) as usize;
        let tail_n = |pos: usize| -> usize {
            if pos >= long_n {
                (self.long_tail_sec * SR as f64) as usize
            } else {
                (self.tail_sec * SR as f64) as usize
            }
        };

        let mut cut_n = 0usize;
        let mut reason = Reason::None;
        let mut i = min_n;
        while i <= self.pcm.len() {
            if i > max_n {
                cut_n = i;
                reason = Reason::Hard;
                break;
            }
            let tn = tail_n(i);
            if i >= tn && rms(&self.pcm[i - tn..i]) < self.thr {
                cut_n = i;
                reason = Reason::Standard;
                break;
            }
            i += step_n.max(1);
        }

        let mut vad_regions: Option<Regions> = None;
        if cut_n == 0 {
            if let Some(f) = vad {
                if dur >= self.min_sec {
                    vad_regions = f(&self.pcm);
                    if let Some(regs) = vad_regions.as_ref() {
                        if let Some(last) = regs.last() {
                            let tail = if dur >= self.long_sec {
                                self.long_tail_sec
                            } else {
                                self.tail_sec
                            };
                            let pause = dur - last.1;
                            if pause >= tail && last.1 * SR as f64 >= (tail * SR as f64) {
                                cut_n = (((last.1 + 0.15) * SR as f64).round() as usize)
                                    .min(self.pcm.len());
                                reason = Reason::Vad;
                            }
                        }
                    }
                }
            }
        }
        if cut_n == 0 && self.pcm.len() > max_n + step_n {
            cut_n = max_n + step_n;
            reason = Reason::Hard;
        }
        if cut_n == 0 {
            return (None, 0, Reason::None);
        }

        // 丢弃只在**切点**评估：切下这段整段 RMS<阈值才算真静音
        if rms(&self.pcm[..cut_n]) < self.thr {
            // 丢弃前用 VAD 复核：轻声/耳语的切点本身就是被轻声 RMS 误判出来的，
            // VAD 明确报有语音就不能丢（否则那句每 2s 循环丢弃、一句不出）
            if let Some(f) = vad {
                if let Some(v) = f(&self.pcm[..cut_n]) {
                    if !v.is_empty() {
                        let span = self.pcm[..cut_n].to_vec();
                        let start_ms = self.start_ms;
                        self.pcm.drain(..cut_n);
                        self.start_ms += (cut_n as f64 / SR as f64 * 1000.0).round() as i64;
                        self.last_cut_regions = Some(clip_regions(&v, cut_n));
                        return (Some(span), start_ms, Reason::VadRescued);
                    }
                }
            }
            // 无 VAD 时用峰值复核：真静音（数字零/纯底噪）峰值趋近 0 才丢；
            // 峰值明显说明是轻声/耳语内容，照常出句（识别为空自然无字幕，无害）
            if peak(&self.pcm[..cut_n]) < SILENT_PEAK_FLOOR {
                self.pcm.drain(..cut_n);
                self.start_ms += (cut_n as f64 / SR as f64 * 1000.0).round() as i64;
                self.last_cut_regions = None;
                return (None, 0, Reason::Silent);
            }
        }

        let span = self.pcm[..cut_n].to_vec();
        let start_ms = self.start_ms;
        self.pcm.drain(..cut_n);
        self.start_ms += (cut_n as f64 / SR as f64 * 1000.0).round() as i64;
        self.last_cut_regions = clip_regions_opt(&vad_regions, cut_n);
        (Some(span), start_ms, reason)
    }
}

/// 把整缓冲的语音区截到切点之前（相对 span 秒）。
fn clip_regions(regions: &Regions, cut_n: usize) -> Regions {
    let cut_sec = cut_n as f64 / SR as f64;
    regions
        .iter()
        .filter(|(a, _)| *a < cut_sec)
        .map(|(a, b)| (*a, (*b).min(cut_sec)))
        .filter(|(a, b)| b > a)
        .collect()
}

fn clip_regions_opt(regions: &Option<Regions>, cut_n: usize) -> Option<Regions> {
    regions.as_ref().map(|r| clip_regions(r, cut_n))
}

/// 合并相邻语音区（间隙小于 `gap` 的并成一段）。
pub fn merge_regions(mut regions: Regions, gap: f64) -> Regions {
    if regions.is_empty() {
        return regions;
    }
    regions.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal));
    let mut out: Regions = Vec::with_capacity(regions.len());
    // 与 Python 同口径：转成毫秒整数后 `<=`，避免 0.3s 浮点边界
    // （1.3-1.0 = 0.30000000000000004）把应合并的区间拆开。
    let gap_ms = (gap * 1000.0).round() as i64;
    let to_ms = |x: f64| (x * 1000.0).round() as i64;
    for (a, b) in regions {
        match out.last_mut() {
            Some(last) if to_ms(a) - to_ms(last.1) <= gap_ms => {
                if b > last.1 {
                    last.1 = b;
                }
            }
            _ => out.push((a, b)),
        }
    }
    out
}

/// ASR 段按中点对齐到 VAD 语音组；落不进任何组的段保留自报时间。
pub fn align_segments_to_groups(
    segs: &mut [(i64, i64)],
    groups_ms: &[(i64, i64)],
    max_snap_ms: i64,
) {
    if segs.is_empty() || groups_ms.is_empty() {
        return;
    }
    for s in segs.iter_mut() {
        let mid = (s.0 + s.1) as f64 / 2.0;
        let hit = groups_ms
            .iter()
            .find(|(a, b)| (*a as f64) <= mid && mid <= (*b as f64));
        if let Some((a, b)) = hit {
            *s = (*a, *b);
            continue;
        }
        // 中点落在语音组外：吸附到最近的一组，但太远就保留自报时间
        let mut best = None;
        let mut bestd = f64::MAX;
        for (a, b) in groups_ms {
            let d = (mid - *a as f64).abs().min((mid - *b as f64).abs());
            if d < bestd {
                bestd = d;
                best = Some((*a, *b));
            }
        }
        if let Some((a, b)) = best {
            if bestd <= max_snap_ms as f64 {
                *s = (a, b);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tone(n: usize, amp: f32) -> Vec<f32> {
        (0..n).map(|i| amp * ((i as f32) * 0.05).sin()).collect()
    }

    #[test]
    fn rms_of_silence_is_zero() {
        assert_eq!(rms(&[0.0; 100]), 0.0);
        assert!(rms(&tone(1000, 0.5)) > 0.1);
    }

    #[test]
    fn accumulates_until_min_phrase() {
        let mut b = HybridBuffer::new();
        // 0.6 秒：连 0.5s 的门槛都不到 → 不评估
        let (span, _, reason) = b.feed(&tone(9600, 0.3), "ja", 0, None);
        assert!(span.is_none());
        assert_eq!(reason, Reason::None);
        assert!(b.buffered_samples() >= 9600);
    }

    #[test]
    fn cuts_on_silence_tail() {
        let mut b = HybridBuffer::new();
        // 先喂 2 秒语音
        b.feed(&tone(32000, 0.4), "ja", 0, None);
        // 再喂一段静音：累计到 2s 之后、尾部 1s 全是静音 → standard 切
        let (span, start_ms, reason) = b.feed(&vec![0.0f32; 32000], "ja", 2000, None);
        assert!(span.is_some(), "应切出一段：{reason:?}");
        assert_eq!(reason, Reason::Standard);
        assert_eq!(start_ms, 0);
    }

    #[test]
    fn hard_cut_caps_latency() {
        let mut b = HybridBuffer::new();
        // 一直有声音 → 触发硬切（上界 5s + 一步）
        let mut got = None;
        for i in 0..6 {
            let (span, _, reason) = b.feed(&tone(16000, 0.5), "ja", i * 1000, None);
            if span.is_some() {
                got = Some(reason);
                break;
            }
        }
        assert_eq!(got, Some(Reason::Hard), "持续有声应走硬切");
    }

    #[test]
    fn silent_span_is_dropped_without_vad() {
        let mut b = HybridBuffer::new();
        let (span, _, reason) = b.feed(&vec![0.0f32; 48000], "ja", 0, None);
        // 3 秒全静音 → 切出来但整段 RMS 低于阈值 → 丢弃
        assert!(span.is_none());
        assert_eq!(reason, Reason::Silent);
    }

    /// 无 VAD 时轻声内容（RMS 低于阈值但峰值明显）必须照常出句：
    /// 否则轻声/耳语对白会每 2s 循环丢弃（Python sivr002 丢 1/3 语音的教训）。
    #[test]
    fn quiet_speech_survives_without_vad() {
        let mut b = HybridBuffer::new();
        // 幅值 0.012 的正弦：RMS≈0.0085 < 阈值 0.01，峰值 0.012 > 底线 0.002
        let (span, _, reason) = b.feed(&tone(48000, 0.012), "ja", 0, None);
        assert!(span.is_some(), "轻声内容不能整段丢弃：{reason:?}");
    }

    #[test]
    fn vad_rescues_quiet_speech() {
        let mut b = HybridBuffer::new();
        let vad = |_: &[f32]| -> Option<Regions> { Some(vec![(0.1, 0.9)]) };
        let (span, _, reason) = b.feed(&vec![0.0f32; 48000], "ja", 0, Some(&vad));
        assert!(span.is_some(), "VAD 说有语音就不能丢：{reason:?}");
        assert_eq!(reason, Reason::VadRescued);
    }

    #[test]
    fn lang_change_resets_buffer() {
        let mut b = HybridBuffer::new();
        b.feed(&tone(16000, 0.3), "ja", 0, None);
        assert!(b.buffered_samples() > 0);
        b.feed(&tone(1600, 0.3), "en", 0, None);
        // 换语言后缓冲被重置，只剩新块
        assert!(b.buffered_samples() <= 1600);
    }

    #[test]
    fn merge_regions_joins_close_pauses() {
        let r = merge_regions(vec![(0.0, 1.0), (1.2, 2.0), (3.0, 4.0)], 0.3);
        assert_eq!(r, vec![(0.0, 2.0), (3.0, 4.0)]);
    }

    /// Python `a - last_end <= int(gap*1000)`：间隙恰好 0.3s 也要合并。
    #[test]
    fn merge_regions_includes_exact_gap() {
        let r = merge_regions(vec![(0.0, 1.0), (1.3, 2.0)], 0.3);
        assert_eq!(r, vec![(0.0, 2.0)]);
    }

    #[test]
    fn align_snaps_to_group_or_keeps_reported_time() {
        let groups = vec![(1000, 3000), (5000, 9000)];
        // 中点落在组内 → 用组时间
        let mut segs = vec![(1500i64, 2500i64)];
        align_segments_to_groups(&mut segs, &groups, 600);
        assert_eq!(segs[0], (1000, 3000));
        // 中点远离任何组 → 保留自报
        let mut segs = vec![(20000i64, 21000i64)];
        align_segments_to_groups(&mut segs, &groups, 600);
        assert_eq!(segs[0], (20000, 21000));
    }
}
