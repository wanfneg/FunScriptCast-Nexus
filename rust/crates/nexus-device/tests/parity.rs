//! 与 Python 版 `tests/test_device_arbiter.py` 的对拍测试。
//!
//! 每个用例的注释里标出对应的 Python 用例名与断言口径。
//! `calls` = 通道探针记录的 `(原始位置, 压缩前速度)`，与 Python FakeChannel 同义。

use std::sync::{Arc, Mutex};
use std::time::Duration;

use async_trait::async_trait;
use nexus_device::channel::{DeviceChannel, Transport};
use nexus_device::preset_player::{load_presets, to_segments, PresetPlayer};
use nexus_device::protocols::kotlin_round;
use nexus_device::sync_engine::{Action, SyncEngine};
use nexus_device::{build, DeviceArbiter};

// ---------------------------------------------------------------- 测试脚手架

#[derive(Default)]
struct RecordTransport {
    frames: Mutex<Vec<Vec<u8>>>,
    connected: Mutex<bool>,
}

impl RecordTransport {
    fn new(connected: bool) -> Arc<Self> {
        let t = Arc::new(Self::default());
        *t.connected.lock().unwrap() = connected;
        t
    }
    fn frame_count(&self) -> usize {
        self.frames.lock().unwrap().len()
    }
}

#[async_trait]
impl Transport for RecordTransport {
    async fn write(&self, payload: &[u8]) -> bool {
        self.frames.lock().unwrap().push(payload.to_vec());
        true
    }
    fn is_connected(&self) -> bool {
        *self.connected.lock().unwrap()
    }
}

struct World {
    dir: tempdir::TempDir,
    ch: Arc<DeviceChannel>,
    arb: DeviceArbiter,
    calls: Arc<Mutex<Vec<(f64, i32)>>>,
    frames: Arc<RecordTransport>,
}

impl World {
    fn new() -> Self {
        let dir = tempdir::TempDir::new("nexus-device");
        std::fs::write(
            dir.path().join("presets.json"),
            r#"[
                {"id":"normal","name":"标准往复","segments":[[0,100,100],[100,0,100]]},
                {"id":"classic","name":"温柔正弦","keyframes":[[10,0],[50,555],[90,1110],[10,1665]]}
            ]"#,
        )
        .unwrap();
        std::fs::write(dir.path().join("video.mp4"), b"x").unwrap();
        std::fs::write(
            dir.path().join("video.funscript"),
            r#"{"actions":[{"at":0,"pos":10},{"at":1000,"pos":90},{"at":2000,"pos":10}]}"#,
        )
        .unwrap();

        let ch = Arc::new(DeviceChannel::new());
        let frames = RecordTransport::new(true);
        ch.attach_transport(frames.clone());
        ch.mark_connected("AA:BB:CC", None);
        ch.set_ready(true);

        let calls = Arc::new(Mutex::new(Vec::new()));
        let sink = calls.clone();
        ch.set_move_tap(Arc::new(move |percent, speed| {
            sink.lock().unwrap().push((percent, speed));
        }));

        let arb = build(ch.clone(), &dir.path().join("presets.json"));
        Self { dir, ch, arb, calls, frames }
    }

    fn video(&self) -> String {
        self.dir.path().join("video.mp4").to_string_lossy().to_string()
    }

    fn calls(&self) -> Vec<(f64, i32)> {
        self.calls.lock().unwrap().clone()
    }

    fn clear_calls(&self) {
        self.calls.lock().unwrap().clear();
    }
}

mod tempdir {
    //! 极简临时目录（避免为测试引入 tempfile 依赖）。
    use std::path::{Path, PathBuf};
    pub struct TempDir(PathBuf);
    impl TempDir {
        pub fn new(prefix: &str) -> Self {
            let mut p = std::env::temp_dir();
            let n = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos();
            p.push(format!("{prefix}-{n}-{}", std::process::id()));
            std::fs::create_dir_all(&p).unwrap();
            Self(p)
        }
        pub fn path(&self) -> &Path {
            &self.0
        }
    }
    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }
}

// ---------------------------------------------------------------- 预设派生

/// 对应 Python `test_to_segments_matches_phone`。
#[test]
fn to_segments_matches_phone() {
    let segs = to_segments(&[[10.0, 0.0], [50.0, 555.0], [50.0, 800.0], [10.0, 1665.0]]);
    assert_eq!(segs[0], [10.0, 50.0, 72.0, 555.0]); // 40*1000/555 → 72
    assert_eq!(segs[1][2], 1.0); // dist=0 → speed 兜底 1
    assert_eq!(segs[1][3], 245.0); // 只等待、不发帧
    assert_eq!(segs[2], [50.0, 10.0, 46.0, 865.0]); // 40*1000/865 → 46
    // dur<=0 脏数据不出天文数字
    assert_eq!(to_segments(&[[0.0, 100.0], [100.0, 100.0]])[0][2], 1.0);
}

/// 对应 Python `test_all_24_real_presets_have_play_segments`。
#[test]
fn all_24_real_presets_have_play_segments() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../ui-legacy/presets.json");
    let prs = load_presets(&root);
    assert_eq!(prs.len(), 24, "真实预设表应为 24 个");
    let mut wave = 0;
    for pr in &prs {
        assert!(!pr.play.is_empty(), "{} 派生后仍是空段（会锁死事件循环）", pr.id);
        if pr.segments.is_empty() && !pr.keyframes.is_empty() {
            wave += 1;
        }
    }
    assert_eq!(wave, 22, "2 段式 + 22 波形式，与手机端构成一致");
}

/// 对应 Python `test_keyframes_only_preset_yields`：波形式预设派生后必须有段，
/// 且每段都真的让出（不是纯 CPU 自旋）。
#[tokio::test]
async fn keyframes_only_preset_yields() {
    let w = World::new();
    let pr = w.arb.preset.by_id(Some("classic")).unwrap().clone();
    assert!(!pr.play.is_empty(), "波形式预设必须派生播放段");
    // 派生段的数量 = keyframes-1
    assert_eq!(pr.play.len(), pr.keyframes.len() - 1);
    // 起播后应当真的产出帧（而不是空转）
    w.arb.preset.select(Some("classic")).unwrap();
    w.arb.preset.start().unwrap();
    tokio::time::sleep(Duration::from_millis(120)).await;
    w.arb.preset.stop();
    assert!(!w.calls().is_empty(), "波形式预设没出帧");
}

// ---------------------------------------------------------------- 单一写者

/// 对应 Python `test_a1_single_writer_matrix`。
#[tokio::test]
async fn a1_single_writer_matrix() {
    let w = World::new();
    let vp = w.video();

    w.arb.start_preset().unwrap();
    assert!(w.arb.preset.is_playing());
    assert!(!w.arb.sync.is_active() && !w.arb.quick.is_orgasm() && !w.arb.quick.is_slow());

    w.arb.start_orgasm();
    assert!(w.arb.quick.is_orgasm());
    assert!(!w.arb.preset.is_playing() && !w.arb.quick.is_slow() && !w.arb.sync.is_active());

    w.arb.start_slow();
    assert!(w.arb.quick.is_slow());
    assert!(!w.arb.preset.is_playing() && !w.arb.quick.is_orgasm() && !w.arb.sync.is_active());

    // 脚本启动 → 停预设；**缓动保持"等待空闲"**
    let res = w.arb.start_script(&vp);
    assert!(res.ok && w.arb.sync.is_active());
    assert!(!w.arb.preset.is_playing() && !w.arb.quick.is_orgasm());
    assert!(w.arb.quick.is_slow(), "脚本启动不应取消缓动（只让它等待空闲）");
    w.arb.quick.stop_slow();

    // 反向：预设启动 → 清脚本 + 停快捷动作
    w.arb.start_preset().unwrap();
    assert!(w.arb.preset.is_playing());
    assert!(!w.arb.sync.is_active() && !w.arb.quick.is_orgasm() && !w.arb.quick.is_slow());

    // 爆发接管 → 停脚本/预设/缓动
    w.arb.start_script(&vp);
    w.arb.start_slow();
    w.arb.start_orgasm();
    assert!(w.arb.quick.is_orgasm());
    assert!(!w.arb.quick.is_slow() && !w.arb.preset.is_playing() && !w.arb.sync.is_active());

    // 脚本加载失败：不动现有会话
    w.arb.start_preset().unwrap();
    let bad = w.dir.path().join("nofile.mp4").to_string_lossy().to_string();
    let res = w.arb.start_script(&bad);
    assert!(!res.ok);
    assert!(w.arb.preset.is_playing(), "加载失败不该把预设停掉");
}

/// 对应 Python `test_a2_estop_stops_all_sessions_and_no_auto_resume`。
#[tokio::test]
async fn a2_estop_stops_all_and_no_auto_resume() {
    let w = World::new();
    let vp = w.video();
    w.arb.start_script(&vp);
    w.arb.start_preset().unwrap();
    w.arb.start_orgasm();
    w.arb.start_slow();

    w.arb.stop_all();
    w.ch.set_allow_move(false);
    assert!(!w.arb.sync.is_active());
    assert!(!w.arb.preset.is_playing());
    assert!(!w.arb.quick.is_orgasm());
    assert!(!w.arb.quick.is_slow());

    let n = w.frames.frame_count();
    tokio::time::sleep(Duration::from_millis(600)).await;
    assert_eq!(w.frames.frame_count(), n, "急停期间不得写帧");

    w.ch.set_allow_move(true);
    tokio::time::sleep(Duration::from_millis(600)).await;
    assert_eq!(w.frames.frame_count(), n, "解除急停不自动恢复任何会话（I6）");
    assert!(!w.arb.sync.is_active() && !w.arb.preset.is_playing());
    assert!(!w.arb.quick.is_orgasm() && !w.arb.quick.is_slow());
}

// ---------------------------------------------------------------- 急停闸门

/// 对应 Python `test_f5_channel_gate_and_defense`。
#[tokio::test]
async fn f5_channel_gate_and_defense() {
    let w = World::new();
    w.ch.set_allow_move(false);
    assert!(!w.ch.move_to(50.0, Some(100), false).await);
    assert!(!w.ch.move_to(50.0, Some(100), true).await);
    w.ch.set_allow_move(true);
    assert!(w.ch.move_to(50.0, Some(100), false).await);

    // 爆发循环的纵深防御：会话在、闸门关 → 空转不写帧
    w.arb.quick.start_orgasm();
    w.ch.set_allow_move(false);
    let n = w.frames.frame_count();
    tokio::time::sleep(Duration::from_millis(600)).await;
    assert_eq!(w.frames.frame_count(), n);
    w.arb.quick.stop_orgasm();
}

/// 对应 Python `test_a8_estop_toggle_path`。
#[test]
fn a8_estop_toggle_path() {
    let w = World::new();
    w.arb.quick.set_stop(true);
    assert!(!w.ch.allow_move());
    assert!(w.arb.quick.state().stop);
    let was = !w.ch.allow_move();
    w.arb.quick.set_stop(false);
    assert!(was);
    assert!(w.ch.allow_move());
    assert!(!w.arb.quick.state().stop);
}

/// 对应 Python `test_f6_cleanup_resets_session_state`。
#[tokio::test]
async fn f6_cleanup_resets_session_state() {
    let w = World::new();
    w.ch.set_allow_move(false);
    w.ch.move_to(10.0, Some(100), false).await;
    w.ch.cleanup();
    let s = w.ch.snapshot();
    assert!(s.allow_move);
    assert!(!w.ch.is_ready());
    assert!(s.recent.is_empty());
    assert_eq!(s.limit_min, 0);
    assert_eq!(s.limit_speed, 0);
}

/// 对应 Python `test_f6_submit_timeout_cancels_coroutine`：
/// 超时后协程必须被取消（否则迟到帧照写）。
#[tokio::test]
async fn f6_timeout_cancels_pending_work() {
    let flag = Arc::new(Mutex::new(false));
    let f2 = flag.clone();
    let res = tokio::time::timeout(Duration::from_millis(200), async move {
        struct Guard(Arc<Mutex<bool>>);
        impl Drop for Guard {
            fn drop(&mut self) {
                *self.0.lock().unwrap() = true;
            }
        }
        let _g = Guard(f2);
        tokio::time::sleep(Duration::from_secs(30)).await;
    })
    .await;
    assert!(res.is_err(), "应当超时");
    tokio::time::sleep(Duration::from_millis(50)).await;
    assert!(*flag.lock().unwrap(), "超时后被丢弃的 future 必须执行 Drop（=取消语义）");
}

// ---------------------------------------------------------------- 脚本同步

/// 对应 Python `test_f16_no_frames_outside_action_range`。
#[tokio::test]
async fn f16_no_frames_outside_action_range() {
    let w = World::new();
    std::fs::write(
        w.dir.path().join("video.funscript"),
        r#"{"actions":[{"at":0,"pos":10},{"at":1000,"pos":90},{"at":2000,"pos":10},
                        {"at":92000,"pos":10},{"at":94000,"pos":90}]}"#,
    )
    .unwrap();
    w.arb.sync.start(&w.video()).unwrap();
    w.clear_calls();

    w.arb.sync.apply(-1.0, 1.0).await; // 首动作点之前
    assert_eq!(w.calls().len(), 0);
    w.arb.sync.apply(95.0, 1.0).await; // 末动作点之后
    assert_eq!(w.calls().len(), 0);
    w.arb.sync.apply(0.5, 1.0).await; // 区间内正常发
    assert_eq!(w.calls().len(), 1);
}

/// 对应 Python `test_slow_segment_speed_zero_not_max`：
/// 斜率 < 1%/s 的慢段发 speed=0，不是 None→max_speed 冲刺。
#[tokio::test]
async fn slow_segment_speed_is_zero_not_max() {
    let w = World::new();
    std::fs::write(
        w.dir.path().join("video.funscript"),
        r#"{"actions":[{"at":0,"pos":10},{"at":30000,"pos":15}]}"#,
    )
    .unwrap();
    w.arb.sync.start(&w.video()).unwrap();
    w.clear_calls();
    w.arb.sync.apply(10.0, 1.0).await;
    let calls = w.calls();
    assert_eq!(calls.len(), 1);
    assert_eq!(calls[0].1, 0, "斜率<1%/s 应取整为 0（对齐手机端 clamp 下限）");
}

/// 对应 Python `test_f17_skip_idle_defaults_and_seek_semantics`。
#[tokio::test]
async fn f17_skip_idle_defaults_and_seek_semantics() {
    let w = World::new();
    std::fs::write(
        w.dir.path().join("video.funscript"),
        r#"{"actions":[{"at":0,"pos":10},{"at":1000,"pos":90},{"at":2000,"pos":10},
                        {"at":92000,"pos":10},{"at":94000,"pos":90}]}"#,
    )
    .unwrap();
    w.arb.sync.start(&w.video()).unwrap();
    w.clear_calls();

    // 默认对齐手机端：关 / 60s
    let st = w.arb.sync.state();
    assert!(!st.skip_idle);
    assert_eq!(st.idle_threshold, 60.0);

    w.arb.sync.apply(0.5, 1.0).await; // 正常段：发帧
    assert_eq!(w.calls().len(), 1);
    w.arb.sync.apply(30.0, 1.0).await; // 平段（默认关）：定位一次后 dedup 静默
    w.arb.sync.apply(40.0, 1.0).await;
    assert_eq!(w.calls().len(), 2);
    assert!(w.arb.sync.take_seek().is_none());

    w.arb.sync.set_skip_idle(true);
    w.arb.sync.reset_last_index();
    w.clear_calls();
    w.arb.sync.apply(30.0, 1.0).await;
    assert_eq!(w.calls().len(), 0, "静止段一帧不发");
    let sk = w.arb.sync.take_seek().expect("应发布 seek 目标");
    assert!((sk - 92.0).abs() < 0.01, "快进到下一动作点，实际 {sk}");
    assert!(w.arb.sync.take_seek().is_none(), "取走即清，防 seek 环");
    assert_eq!(w.arb.sync.skipped(), 1, "每段只发布一次");
}

/// 对应 Python `test_delay_positive_lags_like_phone`。
#[tokio::test]
async fn delay_positive_lags_like_phone() {
    let w = World::new();
    w.arb.sync.start(&w.video()).unwrap();
    w.clear_calls();
    w.arb.sync.set_delay_ms(100.0);
    w.arb.sync.apply(0.85, 1.0).await; // ms = 850+100 = 950 → 仍在第 1 段
    assert_eq!(w.calls().last().unwrap().0, 90.0);
    w.arb.sync.apply(0.95, 1.0).await; // ms = 1050 → 已到第 2 段（滞后）
    assert_eq!(w.calls().last().unwrap().0, 10.0);
}

/// 对应 Python `test_segment_boundary_frames`：
/// 每段恰好一帧、目标=段末、速度=段斜率。
#[tokio::test]
async fn segment_boundary_frames() {
    let w = World::new();
    std::fs::write(
        w.dir.path().join("video.funscript"),
        r#"{"actions":[{"at":0,"pos":10},{"at":500,"pos":90},{"at":1000,"pos":10},
                        {"at":1500,"pos":90},{"at":2000,"pos":10}]}"#,
    )
    .unwrap();
    w.arb.sync.start(&w.video()).unwrap();
    w.clear_calls();
    let mut t = 0.0;
    while t < 2.2 {
        w.arb.sync.apply(t, 1.0).await;
        t += 0.025;
    }
    let calls = w.calls();
    assert_eq!(calls.iter().map(|c| c.0).collect::<Vec<_>>(), vec![90.0, 10.0, 90.0, 10.0]);
    assert_eq!(calls.iter().map(|c| c.1).collect::<Vec<_>>(), vec![160, 160, 160, 160]);
    assert_eq!(w.arb.sync.sent(), 4);
}

/// 对应 Python `test_dedup_within_segment`。
#[tokio::test]
async fn dedup_within_segment() {
    let w = World::new();
    std::fs::write(
        w.dir.path().join("video.funscript"),
        r#"{"actions":[{"at":0,"pos":10},{"at":500,"pos":90},{"at":1000,"pos":10},
                        {"at":1500,"pos":90},{"at":2000,"pos":10}]}"#,
    )
    .unwrap();
    w.arb.sync.start(&w.video()).unwrap();
    w.clear_calls();
    for dt in [0.01, 0.1, 0.2, 0.3, 0.4, 0.49] {
        w.arb.sync.apply(dt, 1.0).await;
    }
    assert_eq!(w.calls().len(), 1);
    w.arb.sync.apply(0.55, 1.0).await;
    assert_eq!(w.calls().len(), 2);
}

/// 对应 Python `test_rate_scales_speed`。
#[tokio::test]
async fn rate_scales_speed() {
    let w = World::new();
    std::fs::write(
        w.dir.path().join("video.funscript"),
        r#"{"actions":[{"at":0,"pos":10},{"at":500,"pos":90},{"at":1000,"pos":10},
                        {"at":1500,"pos":90},{"at":2000,"pos":10}]}"#,
    )
    .unwrap();
    w.arb.sync.start(&w.video()).unwrap();
    w.clear_calls();
    w.arb.sync.apply(0.1, 2.0).await;
    assert_eq!(w.calls()[0].1, 320, "160 × 2");
}

/// 对应 Python `test_reset_resends_current_segment`。
#[tokio::test]
async fn reset_resends_current_segment() {
    let w = World::new();
    std::fs::write(
        w.dir.path().join("video.funscript"),
        r#"{"actions":[{"at":0,"pos":10},{"at":500,"pos":90},{"at":1000,"pos":10},
                        {"at":1500,"pos":90},{"at":2000,"pos":10}]}"#,
    )
    .unwrap();
    w.arb.sync.start(&w.video()).unwrap();
    w.clear_calls();
    w.arb.sync.apply(0.1, 1.0).await;
    assert_eq!(w.calls().len(), 1);
    w.arb.sync.apply(0.2, 1.0).await;
    assert_eq!(w.calls().len(), 1, "同段不发");
    w.arb.sync.reset_last_index();
    w.arb.sync.apply(0.3, 1.0).await;
    assert_eq!(w.calls().len(), 2, "reset 后强制重发");
}

// ---------------------------------------------------------------- AI 实时模式

/// 对应 Python 版没有的 live 链路（由 `tests/test_vision_live_sync.py` 覆盖）。
#[tokio::test]
async fn live_feed_replaces_actions_and_produces_frames() {
    let w = World::new();
    w.arb.start_live("v.mp4");
    assert!(w.arb.sync.is_live());
    w.clear_calls();
    w.arb.sync.feed_live(
        vec![
            Action { at: 0.0, pos: 10.0 },
            Action { at: 1000.0, pos: 90.0 },
            Action { at: 2000.0, pos: 10.0 },
        ],
        3000.0,
    );
    w.arb.sync.apply(0.5, 1.0).await;
    assert_eq!(w.calls().len(), 1);
    assert_eq!(w.calls()[0].0, 90.0, "段末位置 = 段上界 b.pos");

    // 覆盖变长 → 段索引复位，同段内也重发
    w.arb.sync.feed_live(
        vec![
            Action { at: 0.0, pos: 0.0 },
            Action { at: 4000.0, pos: 55.0 },
            Action { at: 8000.0, pos: 100.0 },
        ],
        8000.0,
    );
    w.arb.sync.apply(1.0, 1.0).await;
    assert_eq!(w.calls().len(), 2);
    assert_eq!(w.calls()[1].0, 55.0, "重算后的段末位置应立刻生效");
}

/// 对应 Python `test_estop_blocks_live_frames`。
#[tokio::test]
async fn estop_blocks_live_frames() {
    let w = World::new();
    w.arb.start_live("v.mp4");
    w.arb.sync
        .feed_live(vec![Action { at: 0.0, pos: 0.0 }, Action { at: 1000.0, pos: 100.0 }], 2000.0);
    w.ch.set_allow_move(false);
    w.clear_calls();
    w.arb.sync.apply(0.5, 1.0).await;
    assert_eq!(w.calls().len(), 0, "急停时 live 帧必须被拒写");
    w.ch.set_allow_move(true);
    w.arb.sync.apply(0.6, 1.0).await;
    assert_eq!(w.calls().len(), 1, "解除急停后应恢复下发");
}

// ---------------------------------------------------------------- 爆发 / 缓动

/// 对应 Python `test_f14_slow_interval_follows_speed`：
/// 缓动间隔 = 行程×1000/速度（下限 100ms）。
#[tokio::test]
async fn f14_slow_interval_follows_speed() {
    let w = World::new();
    w.arb.quick.set_slow_settings(|s| {
        s.min_percent = 0;
        s.max_percent = 20;
        s.max_speed = 100;
    });
    w.clear_calls();
    w.arb.quick.start_slow(); // start_slow 走 restart_slow_idle（默认空闲 5s）
    // 直接把空闲判定改成 0 秒不合法（min 1）；改为手动触发循环等价路径：
    w.arb.quick.stop_slow();
    // 直接用引擎语义：设置 idle=1 并等待到期
    w.arb.quick.set_slow_settings(|s| s.idle_detect_seconds = 1);
    w.arb.quick.start_slow();
    // 空闲 1s 到期后每 200ms 一拍：1.25s / 1.45s / 1.65s → 至少 3 拍
    tokio::time::sleep(Duration::from_millis(1750)).await;
    w.arb.quick.stop_slow();
    assert!(w.calls().len() >= 3, "缓动速度滑轨没生效（仍是 1s/拍？）实际 {}", w.calls().len());
}

/// 对应 Python `test_slow_starts_only_after_full_idle_countdown`。
#[tokio::test]
async fn slow_starts_only_after_full_idle_countdown() {
    let w = World::new();
    w.clear_calls();
    w.arb.quick.set_slow_settings(|s| s.idle_detect_seconds = 1);
    w.arb.quick.start_slow();
    tokio::time::sleep(Duration::from_millis(500)).await;
    assert_eq!(w.calls().len(), 0, "缓动在空闲秒数未满时就开始了");
    tokio::time::sleep(Duration::from_millis(900)).await;
    assert!(!w.calls().is_empty(), "计满 1s 后应开跑");
    w.arb.quick.stop_slow();
}

/// 对应 Python `test_external_move_restarts_slow_countdown`。
#[tokio::test]
async fn external_move_restarts_slow_countdown() {
    let w = World::new();
    w.clear_calls();
    w.arb.quick.set_slow_settings(|s| s.idle_detect_seconds = 1);
    w.arb.quick.start_slow();
    tokio::time::sleep(Duration::from_millis(1300)).await;
    assert!(!w.calls().is_empty(), "第一轮已开跑");
    let n = w.calls().len();
    // 外部动作（模拟脚本帧）：直接走通道会把 tap 记进去，这里模拟 on_move 语义
    w.ch.move_to(50.0, Some(100), false).await;
    w.clear_calls();
    tokio::time::sleep(Duration::from_millis(600)).await;
    assert_eq!(w.calls().len(), 0, "外部动作后缓动没有重新等待空闲");
    tokio::time::sleep(Duration::from_millis(900)).await;
    assert!(!w.calls().is_empty(), "计满后应恢复");
    let _ = n;
    w.arb.quick.stop_slow();
}

/// 对应 Python `test_orgasm_params_live_and_links`。
#[test]
fn orgasm_params_live_and_links() {
    let w = World::new();
    w.arb.quick.set_orgasm_settings(|s| {
        s.link_percent = true;
        s.link_speed = true;
    });
    w.ch.apply_motion(Some(20.0), Some(80.0), Some(350), None);
    assert_eq!(w.arb.quick.orgasm_params(), (20, 80, 350));
    w.arb.quick.set_orgasm_settings(|s| {
        s.link_percent = false;
        s.link_speed = false;
    });
    assert_eq!(w.arb.quick.orgasm_params(), (0, 100, 500));
}

/// 对应 Python `test_orgasm_cadence_speed_consistent`：
/// 帧速度与间隔同源；首帧=lo、两目标交替。
#[tokio::test]
async fn orgasm_cadence_speed_consistent() {
    let w = World::new();
    w.ch.apply_motion(Some(0.0), Some(100.0), Some(350), None); // 上限低于爆发速度
    w.arb.quick.set_orgasm_settings(|s| s.max_speed = 500);
    w.clear_calls();
    w.arb.quick.start_orgasm();
    tokio::time::sleep(Duration::from_millis(550)).await;
    w.arb.quick.stop_orgasm();
    let calls = w.calls();
    assert!(calls.len() >= 2, "爆发应至少两拍，实际 {}", calls.len());
    assert_eq!(calls[0].0, 0.0, "首帧应为 lo（手机端 idx0→min）");
    let first_two: Vec<f64> = calls.iter().take(2).map(|c| c.0).collect();
    assert!(first_two.contains(&0.0) && first_two.contains(&100.0));
    for (_, sp) in &calls {
        assert_eq!(*sp, 500, "帧速度与间隔必须同源（被夹紧→行程截短飘忽）");
    }
}

/// 对应 Python `test_slow_own_frames_do_not_reset_idle`：
/// 缓动自己的帧不得被误判为外部动作（否则自触发掐死循环）。
#[tokio::test]
async fn slow_own_frames_do_not_reset_idle() {
    let w = World::new();
    w.arb.quick.set_slow_settings(|s| {
        s.min_percent = 0;
        s.max_percent = 20;
        s.max_speed = 100;
        s.idle_detect_seconds = 1;
    });
    w.arb.quick.start_slow();
    tokio::time::sleep(Duration::from_millis(1400)).await;
    let gen_before = w.arb.quick.gen_s();
    w.clear_calls();
    tokio::time::sleep(Duration::from_millis(600)).await;
    assert!(!w.calls().is_empty(), "缓动被自己的帧掐死了（每隔几秒停一下）");
    assert_eq!(w.arb.quick.gen_s(), gen_before, "缓动自己的帧重启了空闲倒计时（自触发）");
    w.arb.quick.stop_slow();
}

/// 对应 Python `test_script_defers_while_orgasm_runs`。
#[tokio::test]
async fn script_defers_while_orgasm_runs() {
    let w = World::new();
    let vp = w.video();
    w.arb.start_orgasm();
    let res = w.arb.start_script(&vp);
    assert!(res.ok && res.deferred, "爆发期间脚本应让路");
    assert!(w.arb.quick.is_orgasm(), "脚本启动把爆发掐掉了");
    assert!(!w.arb.sync.is_active(), "脚本在爆发期间抢了设备");

    w.arb.quick.stop_orgasm();
    let res = w.arb.start_script(&vp);
    assert!(res.ok && !res.deferred);
    assert!(w.arb.sync.is_active());
}

/// 对应 Python `test_slow_survives_script_watchdog`。
#[tokio::test]
async fn slow_survives_script_watchdog() {
    let w = World::new();
    let vp = w.video();
    w.arb.start_script(&vp);
    assert!(w.arb.sync.is_active());
    w.arb.start_slow();
    assert!(w.arb.quick.is_slow());
    assert!(w.arb.sync.is_active(), "start_slow 停了脚本 → 看护会把它拉回来顶掉缓动");
    let res = w.arb.start_script(&vp);
    assert!(res.ok && w.arb.quick.is_slow(), "脚本启动把缓动取消了");
}

// ---------------------------------------------------------------- 逐值对齐

/// 对应 Python `test_kotlin_round_boundaries`（协议层已有；这里再焊一次口径）。
#[test]
fn kotlin_round_boundaries_parity() {
    assert_eq!(kotlin_round(0.5), 1);
    assert_eq!(kotlin_round(1.5), 2);
    assert_eq!(kotlin_round(2.5), 3);
    assert_eq!(kotlin_round(-0.5), 0);
}

/// 对应 Python `test_channel_remap_scale_use_kotlin_round`。
#[test]
fn channel_remap_scale_use_kotlin_round() {
    let w = World::new();
    w.ch.apply_motion(Some(0.0), Some(50.0), None, None);
    assert_eq!(w.ch.remap(1.0), 1);
    assert_eq!(w.ch.scale_speed(1.0), 1);
}

/// 对应 Python `test_raw_move_not_clamped_to_device_limit`（通道层已覆盖，这里走 arbiter 世界）。
#[tokio::test]
async fn raw_move_not_clamped_through_world() {
    let w = World::new();
    w.ch.apply_motion(Some(0.0), Some(100.0), Some(350), None);
    w.clear_calls();
    w.ch.move_to(100.0, Some(500), true).await;
    assert_eq!(w.calls().last().unwrap().1, 500, "raw 帧速度不得被夹到 350");
    w.ch.move_to(100.0, Some(500), false).await;
    assert_eq!(w.calls().last().unwrap().1, 350, "脚本路径应夹紧到设备上限");
}

// ---------------------------------------------------------------- 预设

/// 预设播放：段式预设应真的出帧，且 stop 后停。
#[tokio::test]
async fn preset_plays_and_stops() {
    let w = World::new();
    w.arb.preset.select(Some("normal")).unwrap();
    w.clear_calls();
    w.arb.preset.start().unwrap();
    tokio::time::sleep(Duration::from_millis(150)).await;
    assert!(!w.calls().is_empty(), "预设应出帧");
    w.arb.preset.stop();
    let n = w.calls().len();
    tokio::time::sleep(Duration::from_millis(200)).await;
    assert_eq!(w.calls().len(), n, "stop 后不得继续出帧");
}

/// BOOST：开启时把滑块跳到 500 并记住原速，取消后恢复。
#[test]
fn preset_boost_roundtrip() {
    let w = World::new();
    w.arb.preset.set_speed(150);
    assert_eq!(w.arb.preset.speed(), 150);
    w.arb.preset.toggle_boost();
    assert_eq!(w.arb.preset.speed(), 500);
    w.arb.preset.toggle_boost();
    assert_eq!(w.arb.preset.speed(), 150, "取消 BOOST 应恢复原速");
}

/// 未知预设必须报错（不静默）。
#[test]
fn preset_select_unknown_is_error() {
    let w = World::new();
    assert!(w.arb.preset.select(Some("nope")).is_err());
    assert!(w.arb.preset.select(Some("normal")).is_ok());
}

/// 未选中时 start 兜底 Normal。
#[tokio::test]
async fn preset_start_falls_back_to_normal() {
    let w = World::new();
    assert!(w.arb.preset.selected().is_none());
    w.arb.preset.start().unwrap();
    assert_eq!(w.arb.preset.selected().as_deref(), Some("normal"));
    w.arb.preset.stop();
}

// 让未使用的导入不告警
#[allow(dead_code)]
fn _unused(_p: &PresetPlayer, _s: &SyncEngine) {}
