//! BLE 设备通道（对应手机端 `ble/BleDeviceService.kt` 的设备侧职责）。
//!
//! 传输层走 `Transport` 抽象：生产实现是 `btleplug`，测试实现是记录器。
//! 这样闸门、行程重映射、速度缩放、分片写这些**语义**可以在没有硬件的机器上焊死。

use std::sync::{Arc, Mutex};
use std::time::Duration;

use async_trait::async_trait;

use crate::protocols::{
    cmd_info, cmd_limit, cmd_move, cmd_mode, cmd_oc_mode, kotlin_round, parse_notify, DeviceInfo,
    Notify, ToyDevice, CHUNK,
};

/// BLE 传输抽象（写一帧、查连接态）。
#[async_trait]
pub trait Transport: Send + Sync {
    async fn write(&self, payload: &[u8]) -> bool;
    fn is_connected(&self) -> bool;
    /// 显式断开真实链路（BLE 实现释放系统侧连接；测试实现为空操作）。
    async fn disconnect(&self) {}
}

/// 写帧回调（脚本同步/快捷动作用来判空闲）。全局只有一个槽位。
pub type MoveCallback = Arc<dyn Fn(f64, i32) + Send + Sync>;

/// 观测用探针：独立于 `on_move`，每个成功落下的帧都会回调一次。
/// 生产不用；测试用它记录 `(原始位置, 压缩前速度)`，与 Python 侧 FakeChannel 的
/// `calls` 语义逐值一致。
pub type MoveTap = Arc<dyn Fn(f64, i32) + Send + Sync>;

#[derive(Debug, Clone, Default, serde::Serialize)]
pub struct ChannelState {
    pub connected: bool,
    pub connecting: bool,
    pub address: String,
    pub name: String,
    pub toy: String,
    pub error: String,
    pub info: Option<DeviceInfo>,
    pub limit_min: i32,
    pub limit_speed: i32,
    pub allow_move: bool,
    pub last_move: (f64, i32),
    pub moves: u32,
    /// 最近下发的位置（排查"乱跑"用，保留最后 12 个）
    pub recent: Vec<i32>,
}

impl ChannelState {
    pub fn new() -> Self {
        Self {
            allow_move: true,
            ..Default::default()
        }
    }
}

#[derive(Debug, Clone)]
pub struct MotionParams {
    pub range_lo: f64,
    pub range_hi: f64,
    pub max_speed: i32,
    pub reversed: bool,
}

impl Default for MotionParams {
    fn default() -> Self {
        Self {
            range_lo: 0.0,
            range_hi: 100.0,
            max_speed: 500,
            reversed: false,
        }
    }
}

struct Inner {
    state: ChannelState,
    motion: MotionParams,
    toy: Option<ToyDevice>,
    mode_override: Option<i32>,
    oc_mode: bool,
    /// 手机端同款：握手完成（含限位下发）前不写运动帧。
    ready: bool,
    forced_toy: Option<ToyDevice>,
}

pub struct DeviceChannel {
    inner: Mutex<Inner>,
    transport: Mutex<Option<Arc<dyn Transport>>>,
    on_move: Mutex<Option<MoveCallback>>,
    move_tap: Mutex<Option<MoveTap>>,
}

impl std::fmt::Debug for DeviceChannel {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("DeviceChannel").finish_non_exhaustive()
    }
}

impl Default for DeviceChannel {
    fn default() -> Self {
        Self::new()
    }
}

impl DeviceChannel {
    pub fn new() -> Self {
        Self {
            inner: Mutex::new(Inner {
                state: ChannelState::new(),
                motion: MotionParams::default(),
                toy: None,
                mode_override: None,
                oc_mode: false,
                ready: false,
                forced_toy: None,
            }),
            transport: Mutex::new(None),
            on_move: Mutex::new(None),
            move_tap: Mutex::new(None),
        }
    }

    /// 测试/生产注入口：接上传输实现。
    pub fn attach_transport(&self, t: Arc<dyn Transport>) {
        *self.transport.lock().unwrap() = Some(t);
    }

    pub fn set_on_move(&self, cb: MoveCallback) {
        *self.on_move.lock().unwrap() = Some(cb);
    }

    pub fn clear_on_move(&self) {
        *self.on_move.lock().unwrap() = None;
    }

    pub fn set_move_tap(&self, cb: MoveTap) {
        *self.move_tap.lock().unwrap() = Some(cb);
    }

    pub fn snapshot(&self) -> ChannelState {
        self.inner.lock().unwrap().state.clone()
    }

    pub fn motion(&self) -> MotionParams {
        self.inner.lock().unwrap().motion.clone()
    }

    pub fn apply_motion(&self, lo: Option<f64>, hi: Option<f64>, max_speed: Option<i32>, reversed: Option<bool>) {
        let mut g = self.inner.lock().unwrap();
        if let Some(v) = lo {
            g.motion.range_lo = v;
        }
        if let Some(v) = hi {
            g.motion.range_hi = v;
        }
        if let Some(v) = max_speed {
            g.motion.max_speed = v;
        }
        if let Some(v) = reversed {
            g.motion.reversed = v;
        }
    }

    pub fn set_allow_move(&self, allow: bool) {
        self.inner.lock().unwrap().state.allow_move = allow;
    }

    pub fn allow_move(&self) -> bool {
        self.inner.lock().unwrap().state.allow_move
    }

    pub fn set_ready(&self, ready: bool) {
        self.inner.lock().unwrap().ready = ready;
    }

    /// 连接流程进行中标志（Python `channel.connecting` 同款：连接前 true、
    /// 落定后 false）——不维护的话状态上报恒为 false，前端只能靠本地乐观标记。
    pub fn set_connecting(&self, connecting: bool) {
        self.inner.lock().unwrap().state.connecting = connecting;
    }

    pub fn is_ready(&self) -> bool {
        self.inner.lock().unwrap().ready
    }

    pub fn set_mode_override(&self, m: Option<i32>) {
        self.inner.lock().unwrap().mode_override = m;
    }

    pub fn mode_override(&self) -> Option<i32> {
        self.inner.lock().unwrap().mode_override
    }

    pub fn set_oc_mode_flag(&self, on: bool) {
        self.inner.lock().unwrap().oc_mode = on;
    }

    pub fn oc_mode(&self) -> bool {
        self.inner.lock().unwrap().oc_mode
    }

    /// 会话边界复位（手机端断开时显式复位同款）。
    ///
    /// 重连后算"新设备"：急停态/限位记忆不能跨连接残留 ——
    /// 否则"连上了但设备不动"还没处查。
    pub fn cleanup(&self) {
        // 拿到传输就显式断开（真实 BLE 释放系统侧连接，测试传输为空操作）
        if let Some(t) = self.transport.lock().unwrap().take() {
            tokio::spawn(async move {
                // WinRT 栈可能挂起：包超时，别让断开任务永远挂着泄漏链路
                let _ = tokio::time::timeout(Duration::from_secs(5), t.disconnect()).await;
            });
        }
        self.reset_session();
    }

    /// 退出流程用的复位：**等待**链路显式断开（最多 5s，别赖着不退）。
    pub async fn cleanup_async(&self) {
        let transport = self.transport.lock().unwrap().take();
        if let Some(t) = transport {
            let _ = tokio::time::timeout(Duration::from_secs(5), t.disconnect()).await;
        }
        self.reset_session();
    }

    fn reset_session(&self) {
        let mut g = self.inner.lock().unwrap();
        g.toy = None;
        g.ready = false;
        let s = &mut g.state;
        s.allow_move = true;
        s.limit_min = 0;
        s.limit_speed = 0;
        s.recent.clear();
        s.connected = false;
        s.address.clear();
        s.name.clear();
        s.toy.clear();
        s.info = None;
    }

    /// 通知回调入口（生产由 BLE 栈调用；测试可直接喂）。
    pub fn on_notify(&self, data: &[u8]) {
        let mut g = self.inner.lock().unwrap();
        match parse_notify(data) {
            Notify::Info(i) => g.state.info = Some(i),
            Notify::LimitMin(v) => g.state.limit_min = v as i32,
            Notify::LimitSpeed(v) => g.state.limit_speed = v as i32,
            _ => {}
        }
    }

    // ---------- 运动换算 ----------

    /// 手机端 `forceMoveTo`：`scaledSpeed = round(speed * span/100)`。
    pub fn scale_speed(&self, speed: f64) -> i32 {
        let m = self.inner.lock().unwrap().motion.clone();
        let span = m.range_hi - m.range_lo;
        kotlin_round(speed * span / 100.0)
    }

    /// 行程范围重映射（对应手机端 `moveTo`：先按 range 映射，再按需反转）。
    pub fn remap(&self, percent: f64) -> i32 {
        let m = self.inner.lock().unwrap().motion.clone();
        let span = m.range_hi - m.range_lo;
        let remapped = kotlin_round(percent * span / 100.0) + m.range_lo as i32;
        let target = if m.reversed { 100 - remapped } else { remapped };
        target.clamp(0, 100)
    }

    /// `forceMoveToInverted`：只加反转，不重映射。
    pub fn invert(&self, percent: f64) -> i32 {
        let rev = self.inner.lock().unwrap().motion.reversed;
        let v = if rev { 100.0 - percent } else { percent };
        kotlin_round(v).clamp(0, 100)
    }

    // ---------- 写 ----------

    pub async fn write(&self, payload: &[u8]) -> bool {
        let t = self.transport.lock().unwrap().clone();
        let Some(t) = t else { return false };
        if !t.is_connected() {
            return false;
        }
        for chunk in payload.chunks(CHUNK) {
            if !t.write(chunk).await {
                // BLE 常态断开窗口：返回 false，调用方按"本帧没发出去"处理。
                return false;
            }
        }
        true
    }

    /// 移动到位置。
    ///
    /// `raw = true`（爆发/缓动，手机端 `forceMoveToInverted`）：只加反转，
    /// **不重映射、不按设备速度上限夹紧** —— 这两条循环的间隔按发出去的速度计算
    /// （行程÷速度），帧速度一旦被夹到上限以下，设备走不完就被下一帧叫回，
    /// 行程被截短且随抖动漂移（用户报"飘忽"）。
    ///
    /// `raw = false`（脚本/预设/手动，手机端 `forceMoveTo`）：重映射进行程范围、
    /// 速度按跨度缩放，并夹紧到设备速度上限。
    ///
    /// 急停（`allow_move = false`）时一律拒写。
    pub async fn move_to(&self, percent: f64, speed: Option<i32>, raw: bool) -> bool {
        let (allow, ready, motion) = {
            let g = self.inner.lock().unwrap();
            (g.state.allow_move, g.ready, g.motion.clone())
        };
        if !allow {
            return false;
        }
        // speed=None 才表示"用速度上限"；显式给 0 就是 0（慢段爬行靠它）。
        let base = speed.unwrap_or(motion.max_speed).max(0);
        let (target, sp) = if raw {
            let t = {
                let rev = motion.reversed;
                let v = if rev { 100.0 - percent } else { percent };
                kotlin_round(v).clamp(0, 100)
            };
            (t, base)
        } else {
            let span = motion.range_hi - motion.range_lo;
            let remapped = kotlin_round(percent * span / 100.0) + motion.range_lo as i32;
            let t = (if motion.reversed { 100 - remapped } else { remapped }).clamp(0, 100);
            let scaled = kotlin_round(base as f64 * span / 100.0);
            (t, scaled.clamp(0, motion.max_speed))
        };
        if !ready {
            return false;
        }
        // 急停 TOCTOU 收口：换算与首查之间闸门可能被落下，发帧前在锁内复查一次
        if !self.inner.lock().unwrap().state.allow_move {
            return false;
        }
        let ok = self.write(&cmd_move(target as f64, sp)).await;
        if ok {
            let cb = {
                let mut g = self.inner.lock().unwrap();
                let s = &mut g.state;
                s.last_move = (percent, sp);
                s.moves += 1;
                s.recent.push(target);
                if s.recent.len() > 12 {
                    let cut = s.recent.len() - 12;
                    s.recent.drain(0..cut);
                }
                drop(g);
                self.on_move.lock().unwrap().clone()
            };
            // 回调 panic 不能带走调用方线程（Python R125 同位有 try/except 兜底；
            // 带走线程会让爆发/预设循环静默死亡、脚本被永久 defer）
            if let Some(cb) = cb {
                let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    cb(percent, sp);
                }));
            }
            let tap = self.move_tap.lock().unwrap().clone();
            if let Some(tap) = tap {
                let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    tap(percent, sp);
                }));
            }
        }
        ok
    }

    pub async fn set_limit(&self, lo: i32, hi: i32, speed: i32) -> bool {
        let supported = {
            let g = self.inner.lock().unwrap();
            g.state
                .info
                .map(|i| i.supports_temporary_limit())
                .unwrap_or(true)
        };
        if !supported {
            return false;
        }
        self.write(&cmd_limit(lo, hi, speed)).await
    }

    pub async fn apply_limits(&self) -> bool {
        let (supported, lo, hi, speed) = {
            let g = self.inner.lock().unwrap();
            (
                g.state
                    .info
                    .map(|i| i.supports_temporary_limit())
                    .unwrap_or(true),
                g.motion.range_lo,
                g.motion.range_hi,
                g.motion.max_speed,
            )
        };
        if !supported {
            return false;
        }
        self.write(&cmd_limit(lo as i32, hi as i32, speed)).await
    }

    pub async fn set_oc_mode(&self, enabled: bool) -> bool {
        self.set_oc_mode_flag(enabled);
        self.write(&cmd_oc_mode(enabled)).await
    }

    pub async fn refresh_info(&self) -> bool {
        self.write(&cmd_info()).await
    }

    pub async fn set_mode(&self, a10_mode: i32) -> bool {
        let v = if a10_mode != 0 { 1 } else { 0 };
        self.set_mode_override(Some(v));
        self.write(&cmd_mode(v)).await
    }

    /// 伪装设备：切换整套 GATT 档案。未连接时记下来，下次连接直接用该档案。
    pub fn set_forced_toy(&self, toy: ToyDevice) {
        let mut g = self.inner.lock().unwrap();
        g.mode_override = Some(toy.a10_mode);
        g.forced_toy = Some(toy);
    }

    /// 伪装档案的可清空版本（对应 Python `_forced_toy = None` 的分支）：
    /// 设置页把 A10 模式改回未知值时，必须解除强制档案，否则下次连接还会用旧档案。
    pub fn set_forced_toy_opt(&self, toy: Option<ToyDevice>) {
        let mut g = self.inner.lock().unwrap();
        g.forced_toy = toy;
        if let Some(t) = toy {
            g.mode_override = Some(t.a10_mode);
        }
    }

    pub fn forced_toy(&self) -> Option<ToyDevice> {
        self.inner.lock().unwrap().forced_toy
    }

    /// 连接态以传输实现为准（设备可能掉线或被别处抢走）。
    pub fn sync_connected(&self) {
        let alive = self
            .transport
            .lock()
            .unwrap()
            .as_ref()
            .map(|t| t.is_connected())
            .unwrap_or(false);
        let mut g = self.inner.lock().unwrap();
        if g.state.connected && !alive {
            let s = &mut g.state;
            s.connected = false;
            s.address.clear();
            s.name.clear();
            s.toy.clear();
            s.info = None;
        }
    }

    pub fn mark_connected(&self, address: &str, toy: Option<ToyDevice>) {
        let mut g = self.inner.lock().unwrap();
        g.state.connected = true;
        g.state.address = address.to_string();
        g.state.name = toy.map(|t| t.name.to_string()).unwrap_or_else(|| address.to_string());
        g.state.toy = toy.map(|t| t.id.to_string()).unwrap_or_default();
        g.toy = toy;
    }

    pub fn current_toy(&self) -> Option<ToyDevice> {
        self.inner.lock().unwrap().toy
    }

    pub fn set_current_toy(&self, t: ToyDevice) {
        let mut g = self.inner.lock().unwrap();
        g.toy = Some(t);
        g.state.name = t.name.to_string();
        g.state.toy = t.id.to_string();
        g.state.info = None;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex as StdMutex;

    #[derive(Default)]
    pub struct RecordingTransport {
        pub frames: StdMutex<Vec<Vec<u8>>>,
        pub connected: StdMutex<bool>,
    }

    impl RecordingTransport {
        pub fn new(connected: bool) -> Arc<Self> {
            let t = Arc::new(Self::default());
            *t.connected.lock().unwrap() = connected;
            t
        }
        pub fn frames(&self) -> Vec<Vec<u8>> {
            self.frames.lock().unwrap().clone()
        }
    }

    #[async_trait]
    impl Transport for RecordingTransport {
        async fn write(&self, payload: &[u8]) -> bool {
            self.frames.lock().unwrap().push(payload.to_vec());
            true
        }
        fn is_connected(&self) -> bool {
            *self.connected.lock().unwrap()
        }
    }

    fn ready_channel(connected: bool) -> (DeviceChannel, Arc<RecordingTransport>) {
        let ch = DeviceChannel::new();
        let t = RecordingTransport::new(connected);
        ch.attach_transport(t.clone());
        ch.set_ready(true);
        (ch, t)
    }

    #[tokio::test]
    async fn estop_gate_rejects_every_write_including_raw() {
        let (ch, t) = ready_channel(true);
        ch.set_allow_move(false);
        assert!(!ch.move_to(50.0, Some(100), false).await);
        assert!(!ch.move_to(50.0, Some(100), true).await);
        assert!(t.frames().is_empty(), "急停期间不得写出任何字节");

        ch.set_allow_move(true);
        assert!(ch.move_to(50.0, Some(100), false).await);
        assert_eq!(t.frames().len(), 1);
    }

    #[tokio::test]
    async fn not_ready_rejects_motion() {
        let ch = DeviceChannel::new();
        let t = RecordingTransport::new(true);
        ch.attach_transport(t.clone());
        // 握手未完成（ready=false）：不写运动帧
        assert!(!ch.move_to(50.0, Some(100), false).await);
        assert!(t.frames().is_empty());
    }

    #[tokio::test]
    async fn remap_and_scale_use_kotlin_round() {
        let (ch, _t) = ready_channel(true);
        ch.apply_motion(Some(0.0), Some(50.0), None, None);
        // span 50：1% → 0.5 → Kotlin 上取 1（银行家舍入会下取 0）
        assert_eq!(ch.remap(1.0), 1);
        assert_eq!(ch.scale_speed(1.0), 1);
    }

    #[tokio::test]
    async fn reversed_flips_position() {
        let (ch, _t) = ready_channel(true);
        ch.apply_motion(Some(0.0), Some(100.0), None, Some(true));
        assert_eq!(ch.remap(0.0), 100);
        assert_eq!(ch.remap(100.0), 0);
        assert_eq!(ch.invert(25.0), 75);
    }

    #[tokio::test]
    async fn raw_move_is_not_clamped_to_device_limit() {
        // 爆发/缓动帧速度不得按设备上限夹紧（否则行程被截短、随抖动漂移）
        let (ch, t) = ready_channel(true);
        ch.apply_motion(Some(0.0), Some(100.0), Some(350), None);

        ch.move_to(100.0, Some(500), true).await;
        let raw = t.frames().last().unwrap().clone();
        assert_eq!(raw[..2], [0x01, 100]);
        assert_eq!(raw[2] as i32, crate::protocols::convert_speed(500), "raw 帧被夹到上限了");

        ch.move_to(100.0, Some(500), false).await;
        let scripted = t.frames().last().unwrap().clone();
        assert_eq!(
            scripted[2] as i32,
            crate::protocols::convert_speed(350),
            "脚本路径应夹紧到设备速度上限"
        );
    }

    #[tokio::test]
    async fn write_is_chunked_by_twenty_bytes() {
        let (ch, t) = ready_channel(true);
        let payload: Vec<u8> = (0..45).collect();
        assert!(ch.write(&payload).await);
        let frames = t.frames();
        assert_eq!(frames.len(), 3, "45 字节应分 20/20/5 三片");
        assert_eq!(frames[0].len(), 20);
        assert_eq!(frames[2].len(), 5);
    }

    #[tokio::test]
    async fn write_fails_when_transport_disconnected() {
        let ch = DeviceChannel::new();
        let t = RecordingTransport::new(false);
        ch.attach_transport(t.clone());
        ch.set_ready(true);
        assert!(!ch.move_to(50.0, Some(100), false).await);
        assert!(t.frames().is_empty());
    }

    #[tokio::test]
    async fn move_counters_and_recent_window() {
        let (ch, _t) = ready_channel(true);
        for i in 0..15 {
            assert!(ch.move_to(i as f64, Some(100), false).await);
        }
        let s = ch.snapshot();
        assert_eq!(s.moves, 15);
        assert_eq!(s.recent.len(), 12, "recent 只保留最后 12 个");
        assert_eq!(s.recent.last().copied(), Some(14));
    }

    #[tokio::test]
    async fn cleanup_resets_session_state() {
        let (ch, _t) = ready_channel(true);
        ch.set_allow_move(false);
        ch.apply_motion(Some(0.0), Some(100.0), Some(350), None);
        ch.move_to(10.0, Some(100), false).await;
        ch.cleanup();

        let s = ch.snapshot();
        assert!(s.allow_move, "断开必须复位急停态");
        assert!(!ch.is_ready());
        assert!(s.recent.is_empty());
        assert_eq!(s.limit_min, 0);
        assert_eq!(s.limit_speed, 0);
        assert!(!s.connected);
    }

    /// 退出流程用的异步复位：等待断开后同样清掉会话状态。
    #[tokio::test]
    async fn cleanup_async_resets_session_state() {
        let (ch, _t) = ready_channel(true);
        ch.mark_connected("AA:BB", Some(crate::protocols::SERVEU));
        ch.set_allow_move(false);
        ch.cleanup_async().await;

        let s = ch.snapshot();
        assert!(s.allow_move, "退出时必须复位急停态");
        assert!(!s.connected);
        assert!(!ch.is_ready());
        assert!(ch.forced_toy().is_none());
    }

    #[tokio::test]
    async fn notify_updates_limits_and_info() {
        let (ch, _t) = ready_channel(true);
        ch.on_notify(&[0x42, 1, 0x00, 0x07]);
        ch.on_notify(&[0x42, 2, 0x01, 0x2C]);
        let s = ch.snapshot();
        assert_eq!(s.limit_min, 7);
        assert_eq!(s.limit_speed, 300);

        let mut b = vec![b'S'];
        b.extend_from_slice(&[0x00, 0x96, 0x00, 0x02, 0x27, 0x10, 0x00, 0x00, 0x01, 0x5E]);
        ch.on_notify(&b);
        assert_eq!(ch.snapshot().info.unwrap().max_speed, 350);
    }

    #[tokio::test]
    async fn set_limit_blocked_for_old_hardware() {
        let (ch, t) = ready_channel(true);
        let mut info = DeviceInfo::default();
        info.hardware_version = 100; // < 150 不支持临时限位
        ch.on_notify(&{
            let mut b = vec![b'S'];
            b.extend_from_slice(&[0x00, 100, 0x00, 0x01, 0x27, 0x10, 0x00, 0x00, 0x01, 0x5E]);
            b
        });
        assert!(!ch.set_limit(10, 90, 350).await);
        assert!(t.frames().is_empty(), "旧硬件不得下发限位帧");
        let _ = info;
    }
}
