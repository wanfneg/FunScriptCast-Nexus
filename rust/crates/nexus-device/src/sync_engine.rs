//! 脚本同步（对应手机端 `sync/SyncEngine.kt` 的核心职责）。
//!
//! **帧语义与手机端逐条对齐**（"动作一顿一顿"的根因修复）：
//! - 手机端主循环 20ms 跑一圈，用本地时钟外推播放进度、只在**段边界**发帧；
//! - 发帧条件：t 落入新动作段（`n != lastIndex`）—— **每段只发一帧**；
//! - 帧内容 = **段末位置 + 段速度**（`computeMove`：`target = keyframes[n]`，
//!   `speed = 段斜率 × 播放倍速`），之后的整段行程由设备固件按速度插值走完；
//! - 首/末动作点之外一帧不发；急停（allow_move）不发；发送失败不推进段索引（下拍重试）。

use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use serde::{Deserialize, Serialize};

use crate::channel::DeviceChannel;
use crate::protocols::kotlin_round;

/// 一个动作点：`at`（毫秒）+ `pos`（0..100）。
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct Action {
    pub at: f64,
    pub pos: f64,
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct SyncState {
    pub active: bool,
    pub live: bool,
    pub live_coverage_ms: f64,
    pub script: String,
    pub actions: usize,
    pub sent: u64,
    pub skipped: u64,
    pub delay_ms: f64,
    pub skip_idle: bool,
    pub idle_threshold: f64,
}

struct Inner {
    active: bool,
    path: String,
    actions: Vec<Action>,
    times: Vec<f64>,
    delay_ms: f64,
    skip_idle: bool,
    idle_threshold: f64,
    sent: u64,
    skipped: u64,
    last_n: Option<usize>,
    skip_n: Option<usize>,
    seek_to: Option<f64>,
    live: bool,
    live_coverage_ms: f64,
    live_source: String,
}

impl Default for Inner {
    fn default() -> Self {
        Self {
            active: false,
            path: String::new(),
            actions: Vec::new(),
            times: Vec::new(),
            delay_ms: 0.0,
            // 对齐手机端默认：skipIdleEnabled=false / 阈值 60s
            skip_idle: false,
            idle_threshold: 60.0,
            sent: 0,
            skipped: 0,
            last_n: None,
            skip_n: None,
            seek_to: None,
            live: false,
            live_coverage_ms: 0.0,
            live_source: String::new(),
        }
    }
}

pub struct SyncEngine {
    ch: Arc<DeviceChannel>,
    inner: Mutex<Inner>,
}

impl SyncEngine {
    pub fn new(ch: Arc<DeviceChannel>) -> Self {
        Self {
            ch,
            inner: Mutex::new(Inner::default()),
        }
    }

    /// 视频同目录同名的 `.funscript`（大小写不敏感）。
    pub fn resolve_script(video_path: &str) -> Option<PathBuf> {
        if video_path.is_empty() {
            return None;
        }
        let p = Path::new(video_path);
        for ext in ["funscript", "FunScript"] {
            let cand = p.with_extension(ext);
            if cand.is_file() {
                return Some(cand);
            }
        }
        let stem = p.file_stem()?.to_string_lossy().to_lowercase();
        let dir = p.parent()?;
        let rd = std::fs::read_dir(dir).ok()?;
        for e in rd.flatten() {
            let path = e.path();
            if !path.is_file() {
                continue;
            }
            let is_fs = path
                .extension()
                .map(|x| x.to_string_lossy().eq_ignore_ascii_case("funscript"))
                .unwrap_or(false);
            let stem_match = path
                .file_stem()
                .map(|x| x.to_string_lossy().to_lowercase() == stem)
                .unwrap_or(false);
            if is_fs && stem_match {
                return Some(path);
            }
        }
        None
    }

    /// 读脚本（返回错误文案，与 Python 版逐字一致）。
    pub fn load(&self, video_path: &str) -> Result<PathBuf, String> {
        let sp = Self::resolve_script(video_path).ok_or_else(|| "这个视频没有配套脚本".to_string())?;
        self.load_file(&sp)?;
        Ok(sp)
    }

    /// 读**任意路径**的脚本（手动选择 .funscript 用）。
    pub fn load_file(&self, sp: &Path) -> Result<(), String> {
        let text =
            std::fs::read_to_string(sp).map_err(|e| format!("脚本解析失败：{e}"))?;
        let parsed: serde_json::Value =
            serde_json::from_str(&text).map_err(|e| format!("脚本解析失败：{e}"))?;
        // 与 Python 一致：任何动作点缺字段/类型错误都算"脚本解析失败"，
        // 不能静默丢点后以"动作点不足"掩盖坏脚本。
        let mut acts: Vec<Action> = Vec::new();
        if let Some(arr) = parsed.get("actions").and_then(|v| v.as_array()) {
            for a in arr {
                let at = a
                    .get("at")
                    .and_then(|v| v.as_f64())
                    .ok_or_else(|| "脚本解析失败：动作点缺少 at 或类型错误".to_string())?;
                let pos = a
                    .get("pos")
                    .and_then(|v| v.as_f64())
                    .ok_or_else(|| "脚本解析失败：动作点缺少 pos 或类型错误".to_string())?;
                acts.push(Action { at, pos });
            }
        }
        acts.sort_by(|a, b| a.at.partial_cmp(&b.at).unwrap_or(std::cmp::Ordering::Equal));
        if acts.len() < 2 {
            return Err("脚本动作点不足".to_string());
        }
        let mut g = self.inner.lock().unwrap();
        g.times = acts.iter().map(|a| a.at).collect();
        g.actions = acts;
        g.path = sp.to_string_lossy().to_string();
        g.last_n = None;
        g.skip_n = None;
        Ok(())
    }

    pub fn start(&self, video_path: &str) -> Result<usize, String> {
        self.load(video_path)?;
        let mut g = self.inner.lock().unwrap();
        g.active = true;
        g.sent = 0;
        g.skipped = 0;
        Ok(g.actions.len())
    }

    /// 用任意脚本路径启动（手动选择 .funscript）。
    pub fn start_file(&self, sp: &Path) -> Result<usize, String> {
        self.load_file(sp)?;
        let mut g = self.inner.lock().unwrap();
        g.active = true;
        g.sent = 0;
        g.skipped = 0;
        Ok(g.actions.len())
    }

    /// 手机端 `resetLastIndex()`：任何"急停 → 继续"都强制重发当前段目标帧。
    pub fn reset_last_index(&self) {
        self.inner.lock().unwrap().last_n = None;
    }

    /// 进入 AI 实时模式：动作由 `feed_live` 灌入，不读磁盘脚本。
    pub fn start_live(&self, source: &str) {
        let mut g = self.inner.lock().unwrap();
        g.actions.clear();
        g.times.clear();
        g.path.clear();
        g.live = true;
        g.live_source = source.to_string();
        g.live_coverage_ms = 0.0;
        g.active = true;
        g.sent = 0;
        g.skipped = 0;
        g.last_n = None;
        g.skip_n = None;
    }

    /// 用引擎返回的完整动作表替换。
    ///
    /// last_n 复位以**自身注释**为准（动作表整体替换后同一段索引指向不同动作，
    /// 必须复位）而非沿用 Python 的 `if grew` 写法：覆盖变短/重置时残留的
    /// last_n 会指向表里不同的动作，漏发或多发一段。这是对 Python 基准的
    /// **有意分叉**（Python 同款矛盾已在 2026-10 审查标记），parity 测试不覆盖
    /// 覆盖变短场景，故此修改不影响对拍。
    pub fn feed_live(&self, mut actions: Vec<Action>, coverage_ms: f64) -> usize {
        actions.sort_by(|a, b| a.at.partial_cmp(&b.at).unwrap_or(std::cmp::Ordering::Equal));
        let mut g = self.inner.lock().unwrap();
        g.live_coverage_ms = coverage_ms;
        g.times = actions.iter().map(|a| a.at).collect();
        g.actions = actions;
        g.last_n = None;
        g.actions.len()
    }

    pub fn stop(&self) {
        let mut g = self.inner.lock().unwrap();
        g.active = false;
        g.live = false;
        g.live_source.clear();
        g.seek_to = None;
    }

    pub fn set_delay_ms(&self, v: f64) {
        self.inner.lock().unwrap().delay_ms = v;
    }

    pub fn set_skip_idle(&self, on: bool) {
        self.inner.lock().unwrap().skip_idle = on;
    }

    pub fn set_idle_threshold(&self, v: f64) {
        self.inner.lock().unwrap().idle_threshold = v;
    }

    pub fn take_seek(&self) -> Option<f64> {
        self.inner.lock().unwrap().seek_to.take()
    }

    pub fn state(&self) -> SyncState {
        let g = self.inner.lock().unwrap();
        SyncState {
            active: g.active,
            live: g.live,
            live_coverage_ms: g.live_coverage_ms,
            script: Path::new(&g.path)
                .file_name()
                .map(|x| x.to_string_lossy().to_string())
                .unwrap_or_default(),
            actions: g.actions.len(),
            sent: g.sent,
            skipped: g.skipped,
            delay_ms: g.delay_ms,
            skip_idle: g.skip_idle,
            idle_threshold: g.idle_threshold,
        }
    }

    pub fn is_active(&self) -> bool {
        self.inner.lock().unwrap().active
    }

    pub fn is_live(&self) -> bool {
        self.inner.lock().unwrap().live
    }

    pub fn sent(&self) -> u64 {
        self.inner.lock().unwrap().sent
    }

    pub fn skipped(&self) -> u64 {
        self.inner.lock().unwrap().skipped
    }

    /// 一拍：`rate` = 播放倍速（手机端 `computeMove` 的 playbackRate）。
    #[cfg(test)]
    pub fn actions_count(&self) -> usize {
        self.inner.lock().unwrap().actions.len()
    }

    pub async fn apply(&self, t_sec: f64, rate: f64) {
        // 取出本拍需要的快照，避免持锁跨 await
        let (ms, delay_ms, skip_idle, idle_threshold, n, a_at, a_pos, b_at, b_pos) = {
            let g = self.inner.lock().unwrap();
            if !g.active || g.actions.is_empty() {
                return;
            }
            if !self.ch.snapshot().connected || !self.ch.allow_move() {
                return;
            }
            // 延迟补偿方向对齐手机端：t = clock + offset（正值 = 设备滞后）
            let ms = t_sec * 1000.0 + g.delay_ms;
            // indexAfter：第一个 at > ms 的索引 = 当前段上界 n
            let n = g.times.partition_point(|&x| x <= ms);
            if n < 1 || n >= g.actions.len() {
                return; // 首/末动作点之外：一帧不发
            }
            let a = g.actions[n - 1];
            let b = g.actions[n];
            (ms, g.delay_ms, g.skip_idle, g.idle_threshold, n, a.at, a.pos, b.at, b.pos)
        };
        let _ = ms;

        let dt_s = ((b_at - a_at) / 1000.0).max(0.001);
        let dist = (b_pos - a_pos).abs();
        let slope = dist / dt_s; // %/秒

        if skip_idle && slope < 1.0 && dt_s >= idle_threshold {
            // 手机端 rebuildIdleGaps 口径：|Δval|/dt < 0.01（value 0..1 域）= 1 %/秒
            let mut g = self.inner.lock().unwrap();
            if g.skip_n != Some(n) {
                g.skip_n = Some(n);
                g.skipped += 1;
                // 脚本坐标 → 媒体时间：t = (script_ms - delay)/1000
                g.seek_to = Some((b_at - delay_ms) / 1000.0);
            }
            return;
        }

        {
            let g = self.inner.lock().unwrap();
            if g.last_n == Some(n) {
                return; // 段没变：只在段边界发帧
            }
        }

        // computeMove：target = 段末位置；speed = 段斜率 × 播放倍速
        let speed = kotlin_round(slope * rate);
        let ok = self.ch.move_to(b_pos, Some(speed), false).await;
        if ok {
            let mut g = self.inner.lock().unwrap();
            g.sent += 1;
            g.last_n = Some(n); // 仅发送成功才推进段索引（失败下拍重试）
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 动作点缺字段时必须报"脚本解析失败"，不能静默丢点后报"动作点不足"。
    #[test]
    fn malformed_action_point_reports_parse_error() {
        let dir = std::env::temp_dir().join(format!("nexus-sync-bad-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let video = dir.join("v.mp4");
        std::fs::write(&video, b"x").unwrap();
        std::fs::write(
            dir.join("v.funscript"),
            r#"{"actions":[{"at":0,"pos":0},{"at":100}]}"#,
        )
        .unwrap();
        let eng = SyncEngine::new(Arc::new(DeviceChannel::new()));
        let err = eng.load(video.to_str().unwrap()).unwrap_err();
        assert!(err.contains("脚本解析失败"), "实际错误：{err}");
        assert_eq!(eng.actions_count(), 0, "解析失败不得留下半份动作表");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
