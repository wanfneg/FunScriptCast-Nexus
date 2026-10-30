//! 快捷动作（逐条对应手机端 `sync/QuickMoves.kt`）。
//!
//! - 一键急停：`allow_move = false` —— 脚本/爆发/缓动全部"待命不动作"。
//! - 一键爆发：在 min/max 之间以 maxSpeed 交替，每 `(max-min)/maxSpeed` 秒一次；
//!   用 raw 移动（尊重反转，跳过重映射/限幅）。
//! - 待机缓动：设备 `idle_detect_seconds` 没有动作后，才以 slowSpeed 在 min/max 间轻缓交替；
//!   外部动作（脚本/手动/爆发）会重置空闲计时并**立刻取消**正在跑的缓动循环。

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde::Serialize;

use crate::channel::DeviceChannel;

#[derive(Debug, Clone, Serialize)]
pub struct OrgasmSettings {
    pub min_percent: i32,
    pub max_percent: i32,
    pub max_speed: i32,
    pub link_percent: bool,
    pub link_speed: bool,
}

impl Default for OrgasmSettings {
    fn default() -> Self {
        Self {
            min_percent: 0,
            max_percent: 100,
            max_speed: 500,
            link_percent: false,
            link_speed: false,
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct SlowSettings {
    pub min_percent: i32,
    pub max_percent: i32,
    pub max_speed: i32,
    pub idle_detect_seconds: i32,
    pub link_percent: bool,
}

impl Default for SlowSettings {
    fn default() -> Self {
        Self {
            min_percent: 0,
            max_percent: 100,
            max_speed: 100,
            idle_detect_seconds: 5,
            link_percent: false,
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct QuickState {
    pub slow: bool,
    pub orgasm: bool,
    pub stop: bool,
    pub orgasm_settings: OrgasmSettings,
    pub slow_settings: SlowSettings,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn invalid_orgasm_range_stops_and_resets_state() {
        let ch = Arc::new(DeviceChannel::new());
        let qm = QuickMoves::new(ch);
        qm.set_orgasm_settings(|s| {
            s.min_percent = 80;
            s.max_percent = 20;
        });
        qm.start_orgasm();
        tokio::time::sleep(Duration::from_millis(80)).await;
        assert!(!qm.is_orgasm(), "范围无效退出后必须复位，不能假死");
        assert!(!qm.state().orgasm);
    }

    #[tokio::test]
    async fn invalid_slow_range_stops_and_resets_state() {
        let ch = Arc::new(DeviceChannel::new());
        let qm = QuickMoves::new(ch);
        let _ = qm.start_slow();
        qm.set_slow_settings(|s| {
            s.min_percent = 90;
            s.max_percent = 10;
            s.idle_detect_seconds = 1;
        });
        // 让空闲看护进入缓动循环；范围无效应立即退出并复位
        qm.note_external();
        qm.start_slow();
        qm.set_slow_settings(|s| {
            s.min_percent = 90;
            s.max_percent = 10;
        });
        tokio::time::sleep(Duration::from_millis(1400)).await;
        assert!(!qm.is_slow(), "范围无效退出后必须复位，不能假死");
        assert!(!qm.state().slow);
    }
}

pub struct QuickMoves {
    ch: Arc<DeviceChannel>,
    orgasm: Mutex<OrgasmSettings>,
    slow: Mutex<SlowSettings>,
    is_orgasm: AtomicBool,
    is_slow: AtomicBool,
    gen_o: AtomicU64,
    gen_s: AtomicU64,
    slow_running: AtomicBool,
    self_moving: AtomicBool,
    last_external: Mutex<Instant>,
    orgasm_resume: AtomicBool,
    slow_resume: AtomicBool,
}

impl QuickMoves {
    pub fn new(ch: Arc<DeviceChannel>) -> Arc<Self> {
        let qm = Arc::new(Self {
            ch: ch.clone(),
            orgasm: Mutex::new(OrgasmSettings::default()),
            slow: Mutex::new(SlowSettings::default()),
            is_orgasm: AtomicBool::new(false),
            is_slow: AtomicBool::new(false),
            gen_o: AtomicU64::new(0),
            gen_s: AtomicU64::new(0),
            slow_running: AtomicBool::new(false),
            self_moving: AtomicBool::new(false),
            last_external: Mutex::new(Instant::now()),
            orgasm_resume: AtomicBool::new(false),
            slow_resume: AtomicBool::new(false),
        });
        // 设备每写一帧都会回调这里（含脚本帧）；缓动循环自己的帧不算"外部动作"
        let weak = Arc::downgrade(&qm);
        ch.set_on_move(Arc::new(move |percent, speed| {
            if let Some(q) = weak.upgrade() {
                q.on_move(percent, speed);
            }
        }));
        qm
    }

    // ---- 外部动作记账（缓动让路） ----
    fn on_move(self: &Arc<Self>, _percent: f64, _speed: i32) {
        if self.self_moving.load(Ordering::SeqCst) {
            return;
        }
        if self.is_slow.load(Ordering::SeqCst) {
            // 手机端 onAnyMove → restartSlowIdle：外部动作随时重置空闲倒计时，
            // 并掐掉正在跑的缓动循环 —— "缓动保持启用，等下一轮空闲"。
            self.restart_slow_idle();
        } else {
            self.note_external();
        }
    }

    pub fn note_external(&self) {
        *self.last_external.lock().unwrap() = Instant::now();
    }

    pub fn mark_idle_ok(&self) {
        self.note_external();
    }

    // ---- 急停 ----
    pub fn set_stop(&self, stopped: bool) -> QuickState {
        self.ch.set_allow_move(!stopped); // 只切 allowMove，循环继续空转
        self.state()
    }

    // ---- 爆发 ----
    pub fn start_orgasm(self: &Arc<Self>) -> QuickState {
        if self.is_orgasm.load(Ordering::SeqCst) {
            return self.state();
        }
        if self.is_slow.load(Ordering::SeqCst) {
            self.stop_slow(); // 爆发/缓动互斥
        }
        self.is_orgasm.store(true, Ordering::SeqCst);
        let gen = self.gen_o.fetch_add(1, Ordering::SeqCst) + 1;
        let me = Arc::clone(self);
        tokio::spawn(async move { me.orgasm_loop(gen).await });
        self.state()
    }

    pub fn stop_orgasm(&self) -> QuickState {
        self.is_orgasm.store(false, Ordering::SeqCst);
        self.gen_o.fetch_add(1, Ordering::SeqCst);
        self.state()
    }

    /// 手机端 `linkedOrgasm()`：**每拍实时读**（含关联解析）——
    /// 播放中改范围/速度立即生效；取消关联后用户自定义值不会被改写。
    pub fn orgasm_params(&self) -> (i32, i32, i32) {
        let s = self.orgasm.lock().unwrap().clone();
        let m = self.ch.motion();
        let lo = if s.link_percent { m.range_lo as i32 } else { s.min_percent };
        let hi = if s.link_percent { m.range_hi as i32 } else { s.max_percent };
        let speed = if s.link_speed { m.max_speed } else { s.max_speed };
        (lo, hi, speed)
    }

    async fn orgasm_loop(self: Arc<Self>, gen: u64) {
        let mut idx: u64 = 0;
        while self.is_orgasm.load(Ordering::SeqCst) && gen == self.gen_o.load(Ordering::SeqCst) {
            let (lo, hi, speed) = self.orgasm_params();
            // 范围无效（min>=max 或配反）：退出并**复位状态位**，否则 UI 显示
            // "爆发中"而设备不动（Python 没有这条保护，但也不会假死）。
            if lo >= hi {
                eprintln!("爆发范围无效（min={lo} >= max={hi}），已停止爆发");
                self.is_orgasm.store(false, Ordering::SeqCst);
                break;
            }
            if speed <= 0 || !self.ch.allow_move() {
                tokio::time::sleep(Duration::from_secs(1)).await;
                continue;
            }
            let target = if idx % 2 == 0 { lo } else { hi };
            idx += 1;
            self.self_moving.store(true, Ordering::SeqCst);
            self.ch.move_to(target as f64, Some(speed), true).await;
            self.self_moving.store(false, Ordering::SeqCst);
            let secs = ((hi - lo) as f64 / speed as f64).max(0.05);
            tokio::time::sleep(Duration::from_secs_f64(secs)).await;
        }
        self.self_moving.store(false, Ordering::SeqCst);
    }

    // ---- 缓动 ----
    pub fn start_slow(self: &Arc<Self>) -> QuickState {
        if self.is_slow.load(Ordering::SeqCst) {
            return self.state();
        }
        if self.is_orgasm.load(Ordering::SeqCst) {
            self.stop_orgasm(); // 互斥
        }
        self.is_slow.store(true, Ordering::SeqCst);
        // 手机端 startSlow → restartSlowIdle()：从点击时刻重新计满空闲秒数，
        // 绝不因为时间戳陈旧而立刻开跑。
        self.restart_slow_idle();
        self.state()
    }

    pub fn stop_slow(&self) -> QuickState {
        self.is_slow.store(false, Ordering::SeqCst);
        self.gen_s.fetch_add(1, Ordering::SeqCst); // 空闲计时与缓动循环同时失效
        self.slow_running.store(false, Ordering::SeqCst);
        self.state()
    }

    /// 手机端 `restartSlowIdle`：代际 +1 掐掉旧循环/旧计时，倒计时从现在重新计满。
    fn restart_slow_idle(self: &Arc<Self>) {
        let gen = self.gen_s.fetch_add(1, Ordering::SeqCst) + 1;
        self.slow_running.store(false, Ordering::SeqCst);
        self.note_external();
        let me = Arc::clone(self);
        tokio::spawn(async move { me.idle_watch(gen).await });
    }

    async fn idle_watch(self: Arc<Self>, gen: u64) {
        while self.is_slow.load(Ordering::SeqCst) && gen == self.gen_s.load(Ordering::SeqCst) {
            tokio::time::sleep(Duration::from_millis(250)).await;
            if !self.ch.allow_move() {
                continue;
            }
            // 每轮都重读设置：等待期间改"空闲判定秒数"立即生效
            let secs = {
                let s = self.slow.lock().unwrap();
                s.idle_detect_seconds.max(1) as u64
            };
            let idle = self.last_external.lock().unwrap().elapsed();
            if idle >= Duration::from_secs(secs) && !self.slow_running.load(Ordering::SeqCst) {
                self.slow_running.store(true, Ordering::SeqCst);
                let me = Arc::clone(&self);
                tokio::spawn(async move { me.slow_loop(gen).await });
            }
        }
    }

    /// 手机端 `linkedSlow()`：每拍实时读（缓动只有范围关联，没有速度关联）。
    pub fn slow_params(&self) -> (i32, i32, i32) {
        let s = self.slow.lock().unwrap().clone();
        let m = self.ch.motion();
        let lo = if s.link_percent { m.range_lo as i32 } else { s.min_percent };
        let hi = if s.link_percent { m.range_hi as i32 } else { s.max_percent };
        (lo, hi, s.max_speed)
    }

    async fn slow_loop(self: Arc<Self>, gen: u64) {
        let mut idx: u64 = 0;
        while self.is_slow.load(Ordering::SeqCst) && gen == self.gen_s.load(Ordering::SeqCst) {
            let (lo, hi, speed) = self.slow_params();
            // 范围无效（min>=max 或配反）：退出并复位，避免 UI 假死。
            if lo >= hi {
                eprintln!("缓动范围无效（min={lo} >= max={hi}），已停止缓动");
                self.is_slow.store(false, Ordering::SeqCst);
                self.slow_running.store(false, Ordering::SeqCst);
                break;
            }
            if speed <= 0 || !self.ch.allow_move() {
                tokio::time::sleep(Duration::from_secs(1)).await;
                continue;
            }
            let target = if idx % 2 == 0 { lo } else { hi };
            idx += 1;
            self.self_moving.store(true, Ordering::SeqCst);
            self.ch.move_to(target as f64, Some(speed), true).await;
            self.self_moving.store(false, Ordering::SeqCst);
            let secs = ((hi - lo) as f64 / speed as f64).max(0.1);
            tokio::time::sleep(Duration::from_secs_f64(secs)).await;
        }
        self.slow_running.store(false, Ordering::SeqCst);
        self.self_moving.store(false, Ordering::SeqCst);
    }

    // ---- 播放器联动 ----
    pub fn pause_for_player(&self) -> QuickState {
        if self.is_orgasm.load(Ordering::SeqCst) {
            self.orgasm_resume.store(true, Ordering::SeqCst);
            self.stop_orgasm();
        }
        if self.is_slow.load(Ordering::SeqCst) {
            self.slow_resume.store(true, Ordering::SeqCst);
            self.stop_slow();
        }
        self.state()
    }

    /// 取走"暂停前在跑"的旗标并清空 —— 供宿主经仲裁恢复。
    pub fn take_resume_flags(&self) -> (bool, bool) {
        (
            self.orgasm_resume.swap(false, Ordering::SeqCst),
            self.slow_resume.swap(false, Ordering::SeqCst),
        )
    }

    /// 丢弃暂停恢复旗标：预设接管设备时调用。
    pub fn discard_resume(&self) -> QuickState {
        self.orgasm_resume.store(false, Ordering::SeqCst);
        self.slow_resume.store(false, Ordering::SeqCst);
        self.state()
    }

    /// 设置合并（只覆盖认识且非 None 的键）。
    pub fn apply(&self, orgasm: Option<&serde_json::Value>, slow: Option<&serde_json::Value>) {
        if let Some(o) = orgasm {
            let mut s = self.orgasm.lock().unwrap();
            apply_orgasm(&mut s, o);
        }
        if let Some(s) = slow {
            let mut t = self.slow.lock().unwrap();
            apply_slow(&mut t, s);
        }
        // 关联由 orgasm_params/slow_params **每拍实时解析**：
        // 不在这里把主范围/上限写回设置对象 —— 会毁掉用户自定义值。
    }

    pub fn settings(&self) -> (OrgasmSettings, SlowSettings) {
        (
            self.orgasm.lock().unwrap().clone(),
            self.slow.lock().unwrap().clone(),
        )
    }

    pub fn is_orgasm(&self) -> bool {
        self.is_orgasm.load(Ordering::SeqCst)
    }

    pub fn is_slow(&self) -> bool {
        self.is_slow.load(Ordering::SeqCst)
    }

    pub fn gen_s(&self) -> u64 {
        self.gen_s.load(Ordering::SeqCst)
    }

    pub fn state(&self) -> QuickState {
        QuickState {
            slow: self.is_slow(),
            orgasm: self.is_orgasm(),
            stop: !self.ch.allow_move(),
            orgasm_settings: self.orgasm.lock().unwrap().clone(),
            slow_settings: self.slow.lock().unwrap().clone(),
        }
    }
}

/// JSON 数值 → i32（联动页夹紧后可能是 `20.0` 这种浮点形态；
/// Python 侧直接拿浮点参与运算，这里统一收成整数，别让 `as_i64()` 把它丢掉）。
fn json_i32(v: &serde_json::Value) -> Option<i32> {
    // 饱和转换：`as i32` 是截低位（wrapping），4294967296 会静默变 0，
    // 快捷动作的超大设置值必须夹到 i32 边界
    let sat_i32 = |x: i64| x.clamp(i32::MIN as i64, i32::MAX as i64) as i32;
    v.as_i64()
        .map(sat_i32)
        .or_else(|| v.as_f64().map(|x| x.clamp(i32::MIN as f64, i32::MAX as f64) as i32))
}

fn apply_orgasm(s: &mut OrgasmSettings, v: &serde_json::Value) {
    if let Some(x) = v.get("min_percent").and_then(json_i32) {
        s.min_percent = x;
    }
    if let Some(x) = v.get("max_percent").and_then(json_i32) {
        s.max_percent = x;
    }
    if let Some(x) = v.get("max_speed").and_then(json_i32) {
        s.max_speed = x;
    }
    if let Some(x) = v.get("link_percent").and_then(|x| x.as_bool()) {
        s.link_percent = x;
    }
    if let Some(x) = v.get("link_speed").and_then(|x| x.as_bool()) {
        s.link_speed = x;
    }
}

fn apply_slow(s: &mut SlowSettings, v: &serde_json::Value) {
    if let Some(x) = v.get("min_percent").and_then(json_i32) {
        s.min_percent = x;
    }
    if let Some(x) = v.get("max_percent").and_then(json_i32) {
        s.max_percent = x;
    }
    if let Some(x) = v.get("max_speed").and_then(json_i32) {
        s.max_speed = x;
    }
    if let Some(x) = v.get("idle_detect_seconds").and_then(json_i32) {
        s.idle_detect_seconds = x;
    }
    if let Some(x) = v.get("link_percent").and_then(|x| x.as_bool()) {
        s.link_percent = x;
    }
}

impl QuickMoves {
    /// 测试/设置页用：直接改爆发参数。
    pub fn set_orgasm_settings(&self, f: impl FnOnce(&mut OrgasmSettings)) {
        f(&mut self.orgasm.lock().unwrap());
    }

    pub fn set_slow_settings(&self, f: impl FnOnce(&mut SlowSettings)) {
        f(&mut self.slow.lock().unwrap());
    }
}
