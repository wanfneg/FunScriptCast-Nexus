//! 预设播放引擎 —— **逐条对齐手机端 `sync/PresetPlayer.kt`**。
//!
//! 手机端语义：
//! - `select(id)`：选中预设并**退出随机**。
//! - `togglePlay()`：在播就停，否则开始；未选中时兜底 Normal。
//! - `start()`：先在跑就直接返回；取消上一个任务后起循环。
//! - `toggleRandom()`：已开 → 关（保持当前预设继续播）；未开 → 开，且在播时立即跳转。
//! - `effectiveSpeed()`：`v = boost ? 500 : 滑块值`；clamp 到 [1,500] 再 min(设备速度上限)。
//! - `playOneLoop(def)`：对每一段：`dist = |end-start|`、`naturalMs = durationMs ?? dist*1000/segSpeed`、
//!   `waitMs = naturalMs * segSpeed / speed`；`dist > 0` 先发指令（段末位置, speed）再等。
//!
//! 位置映射/限速/反转都由通道 `move_to` 内部处理（这里不 raw、不自算）。

use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicI32, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde::Serialize;

use crate::channel::DeviceChannel;
use crate::protocols::kotlin_round;

pub const BOOST_SPEED: i32 = 500;
pub const DEFAULT_ID: &str = "normal";
pub const TICK_MS: u64 = 20;

/// 波形式 keyframes → 段式（**逐条对齐手机端 `PresetDefs.toSegments`**，无损转换）。
///
/// 相邻关键帧 = 一段，`speed = 距离÷时长`（下限 1），`durationMs` 记录原始时间轴。
pub fn to_segments(kfs: &[[f64; 2]]) -> Vec<[f64; 4]> {
    let mut out = Vec::new();
    for w in kfs.windows(2) {
        let (a, b) = (w[0], w[1]);
        let dist = (b[0] - a[0]).abs();
        let dur = b[1] - a[1];
        let speed = if dist > 0.0 && dur > 0.0 {
            kotlin_round(dist * 1000.0 / dur).max(1)
        } else {
            1
        };
        out.push([a[0], b[0], speed as f64, dur.max(0.0)]);
    }
    out
}

#[derive(Debug, Clone, Serialize, Default)]
pub struct Preset {
    pub id: String,
    pub name: String,
    /// 段式：`[start, end, speed, durationMs]`
    pub segments: Vec<[f64; 4]>,
    /// 波形式：`[pos, ms]`
    pub keyframes: Vec<[f64; 2]>,
    /// 播放用段式列表（加载时一次性派生）
    #[serde(skip)]
    pub play: Vec<[f64; 4]>,
}

fn parse_pairs(v: &serde_json::Value, len: usize) -> Vec<[f64; 2]> {
    v.as_array()
        .map(|arr| {
            arr.iter()
                .filter_map(|x| {
                    let a = x.as_array()?;
                    if a.len() < len {
                        return None;
                    }
                    Some([a[0].as_f64()?, a[1].as_f64()?])
                })
                .collect()
        })
        .unwrap_or_default()
}

fn parse_quads(v: &serde_json::Value) -> Vec<[f64; 4]> {
    v.as_array()
        .map(|arr| {
            arr.iter()
                .filter_map(|x| {
                    let a = x.as_array()?;
                    if a.len() < 2 {
                        return None;
                    }
                    Some([
                        a[0].as_f64()?,
                        a[1].as_f64()?,
                        a.get(2).and_then(|x| x.as_f64()).unwrap_or(100.0),
                        a.get(3).and_then(|x| x.as_f64()).unwrap_or(0.0),
                    ])
                })
                .collect()
        })
        .unwrap_or_default()
}

/// 读预设文件；解析失败返回空表（与 Python 版一致）。
pub fn load_presets(path: &Path) -> Vec<Preset> {
    let Ok(text) = std::fs::read_to_string(path) else {
        return Vec::new();
    };
    let Ok(v) = serde_json::from_str::<serde_json::Value>(&text) else {
        return Vec::new();
    };
    let Some(arr) = v.as_array() else {
        return Vec::new();
    };
    arr.iter()
        .filter_map(|p| {
            let mut pr = Preset {
                id: p.get("id").and_then(|x| x.as_str()).unwrap_or("").to_string(),
                name: p.get("name").and_then(|x| x.as_str()).unwrap_or("").to_string(),
                segments: p.get("segments").map(parse_quads).unwrap_or_default(),
                keyframes: p.get("keyframes").map(|k| parse_pairs(k, 2)).unwrap_or_default(),
                play: Vec::new(),
            };
            // 22 个预设只有 keyframes —— 缺了这步派生就是空段列表，
            // 播放循环里一个 await 都没有，纯 CPU 自旋锁死整条事件循环。
            pr.play = if pr.segments.is_empty() && !pr.keyframes.is_empty() {
                to_segments(&pr.keyframes)
            } else {
                pr.segments.clone()
            };
            // 派生后仍为空（比如 keyframes 只有 1 个点）：不入播放表，选中即无解
            if pr.play.is_empty() {
                eprintln!("跳过空预设 {}（{}）：没有可播放的段", pr.id, pr.name);
                return None;
            }
            Some(pr)
        })
        .collect()
}

#[derive(Debug, Clone, Serialize)]
pub struct PresetState {
    pub playing: bool,
    pub selected: Option<String>,
    pub random: bool,
    pub boost: bool,
    pub speed: i32,
    pub count: usize,
}

pub struct PresetPlayer {
    ch: Arc<DeviceChannel>,
    presets: Vec<Preset>,
    selected: Mutex<Option<String>>,
    playing: AtomicBool,
    random_mode: AtomicBool,
    boost: AtomicBool,
    speed: AtomicI32,
    gen: AtomicU64,
    boost_prev_speed: AtomicI32,
}

impl PresetPlayer {
    pub fn new(ch: Arc<DeviceChannel>, presets_path: &Path) -> Arc<Self> {
        Arc::new(Self {
            ch,
            presets: load_presets(presets_path),
            selected: Mutex::new(None),
            playing: AtomicBool::new(false),
            random_mode: AtomicBool::new(false),
            boost: AtomicBool::new(false),
            speed: AtomicI32::new(100),
            gen: AtomicU64::new(0),
            boost_prev_speed: AtomicI32::new(100),
        })
    }

    pub fn presets(&self) -> &[Preset] {
        &self.presets
    }

    pub fn by_id(&self, pid: Option<&str>) -> Option<&Preset> {
        let pid = pid?;
        self.presets.iter().find(|p| p.id == pid)
    }

    fn random_other(&self, pid: Option<&str>) -> String {
        let pool: Vec<&str> = self
            .presets
            .iter()
            .filter(|p| Some(p.id.as_str()) != pid)
            .map(|p| p.id.as_str())
            .collect();
        if pool.is_empty() {
            return DEFAULT_ID.to_string();
        }
        // 与 Python `random.choice` 同义：在池里均匀取一个（纳秒取模会偏）。
        pool[fastrand::usize(..pool.len())].to_string()
    }

    pub fn effective_speed(&self) -> i32 {
        let cap = self.ch.motion().max_speed.max(1);
        let v = if self.boost.load(Ordering::SeqCst) {
            BOOST_SPEED
        } else {
            self.speed.load(Ordering::SeqCst)
        };
        v.clamp(1, 500).min(cap)
    }

    pub fn is_playing(&self) -> bool {
        self.playing.load(Ordering::SeqCst)
    }

    pub fn selected(&self) -> Option<String> {
        self.selected.lock().unwrap().clone()
    }

    pub fn speed(&self) -> i32 {
        self.speed.load(Ordering::SeqCst)
    }

    // ---- 控件 ----
    pub fn select(&self, pid: Option<&str>) -> Result<PresetState, String> {
        if let Some(p) = pid {
            if self.by_id(Some(p)).is_none() {
                return Err("未知预设".to_string());
            }
        }
        *self.selected.lock().unwrap() = pid.map(|s| s.to_string());
        self.random_mode.store(false, Ordering::SeqCst); // 点网格卡片 = 退出随机
        Ok(self.state())
    }

    pub fn toggle_play(self: &Arc<Self>) -> Result<PresetState, String> {
        if self.is_playing() {
            self.stop();
            Ok(self.state())
        } else {
            self.start()
        }
    }

    pub fn start(self: &Arc<Self>) -> Result<PresetState, String> {
        if self.is_playing() {
            return Ok(self.state());
        }
        {
            let mut sel = self.selected.lock().unwrap();
            if sel.is_none() {
                *sel = Some(DEFAULT_ID.to_string());
            }
        }
        let cur = self.selected();
        if self.by_id(cur.as_deref()).is_none() {
            return Err("没有可用预设".to_string());
        }
        self.playing.store(true, Ordering::SeqCst);
        let gen = self.gen.fetch_add(1, Ordering::SeqCst) + 1;
        let me = Arc::clone(self);
        tokio::spawn(async move { me.run(gen).await });
        Ok(self.state())
    }

    pub fn stop(&self) -> PresetState {
        self.playing.store(false, Ordering::SeqCst);
        self.gen.fetch_add(1, Ordering::SeqCst);
        self.state()
    }

    pub fn toggle_random(self: &Arc<Self>) -> PresetState {
        if self.random_mode.load(Ordering::SeqCst) {
            self.random_mode.store(false, Ordering::SeqCst); // 关：保持当前预设继续播
            return self.state();
        }
        self.random_mode.store(true, Ordering::SeqCst);
        if self.is_playing() {
            // 开：在播就立即跳转
            let cur = self.selected();
            let next = self.random_other(cur.as_deref());
            *self.selected.lock().unwrap() = Some(next);
            let gen = self.gen.fetch_add(1, Ordering::SeqCst) + 1;
            let me = Arc::clone(self);
            tokio::spawn(async move { me.run(gen).await });
        }
        self.state()
    }

    /// 手机端 `togglePresetBoost`：激活时记住当前速度并把滑块跳到 500；取消时恢复原速。
    pub fn toggle_boost(&self) -> PresetState {
        if self.boost.load(Ordering::SeqCst) {
            self.boost.store(false, Ordering::SeqCst);
            let prev = self.boost_prev_speed.load(Ordering::SeqCst);
            self.speed.store(prev, Ordering::SeqCst);
        } else {
            self.boost_prev_speed
                .store(self.speed.load(Ordering::SeqCst), Ordering::SeqCst);
            self.boost.store(true, Ordering::SeqCst);
            self.speed.store(BOOST_SPEED, Ordering::SeqCst);
        }
        self.state()
    }

    pub fn set_speed(&self, v: i32) -> PresetState {
        let v = v.clamp(1, 500);
        self.speed.store(v, Ordering::SeqCst);
        if self.boost.load(Ordering::SeqCst) {
            // BOOST 期间拖滑块 → 取消后恢复该值
            self.boost_prev_speed.store(v, Ordering::SeqCst);
        }
        self.state()
    }

    pub fn state(&self) -> PresetState {
        PresetState {
            playing: self.is_playing(),
            selected: self.selected(),
            random: self.random_mode.load(Ordering::SeqCst),
            boost: self.boost.load(Ordering::SeqCst),
            speed: self.speed.load(Ordering::SeqCst),
            count: self.presets.len(),
        }
    }

    // ---- 循环 ----
    fn can(&self, gen: u64, pid: Option<&str>) -> bool {
        if !self.is_playing() || gen != self.gen.load(Ordering::SeqCst) {
            return false;
        }
        match pid {
            None => true,
            Some(p) => self.selected().as_deref() == Some(p),
        }
    }

    async fn run(self: Arc<Self>, gen: u64) {
        while self.is_playing() && gen == self.gen.load(Ordering::SeqCst) {
            let pid = self.selected().unwrap_or_else(|| DEFAULT_ID.to_string());
            let pr = self
                .by_id(Some(&pid))
                .or_else(|| self.by_id(Some(DEFAULT_ID)))
                .cloned();
            let Some(pr) = pr else { break };
            self.play_one_loop(&pr, gen).await;
            // 兜底让出：play_one_loop 对空表/急停态可能一个 await 都不碰，
            // 没有这一拍就是纯 CPU 自旋锁死事件循环。
            tokio::time::sleep(Duration::from_millis(TICK_MS)).await;
            if !(self.is_playing() && gen == self.gen.load(Ordering::SeqCst)) {
                break;
            }
            if self.random_mode.load(Ordering::SeqCst) {
                let next = self.random_other(Some(&pid)); // 每个循环结束换下一个
                *self.selected.lock().unwrap() = Some(next);
            }
        }
    }

    async fn play_one_loop(self: &Arc<Self>, pr: &Preset, gen: u64) {
        for sg in &pr.play {
            if !self.can(gen, Some(&pr.id)) {
                return;
            }
            let (s, e) = (sg[0], sg[1]);
            let seg_speed = if sg[2] > 0.0 { sg[2] } else { 100.0 }.max(1.0);
            let dur = sg[3];
            let dist = (e - s).abs();
            let speed = self.effective_speed();
            let natural_ms = if dur > 0.0 {
                dur
            } else {
                dist * 1000.0 / seg_speed
            };
            let wait_ms = natural_ms * seg_speed / speed.max(1) as f64;
            if dist > 0.0 && self.ch.allow_move() {
                // allow_move 检查是纵深防御：急停正常路径已把会话停掉（arbiter）
                self.ch.move_to(e, Some(speed), false).await;
            }
            let t0 = Instant::now();
            while (t0.elapsed().as_secs_f64() * 1000.0) < wait_ms
                && self.can(gen, Some(&pr.id))
            {
                tokio::time::sleep(Duration::from_millis(TICK_MS)).await;
            }
        }
    }
}
