//! 设备层：BLE 通道、脚本同步、快捷动作、预设播放、单一写者仲裁。
//!
//! 从 Python 版 `vendor/device/` 逐条移植（1,317 行 → 本 crate），
//! 线协议、帧语义、舍入口径、急停闸门与手机端 `E:\Development\FunScriptCast` 对齐。

pub mod arbiter;
#[cfg(feature = "ble")]
pub mod ble;
pub mod channel;
pub mod preset_player;
pub mod protocols;
pub mod quick_moves;
pub mod sync_engine;

pub use arbiter::{DeviceArbiter, SessionResult};
pub use channel::{ChannelState, DeviceChannel, MotionParams, MoveCallback, Transport};
pub use preset_player::{load_presets, to_segments, Preset, PresetPlayer, PresetState};
pub use protocols::{
    cmd_info, cmd_limit, cmd_mode, cmd_move, cmd_oc_mode, convert_speed, kotlin_round, match_toy,
    parse_notify, toy_by_id, DeviceInfo, Notify, ToyDevice, CHUNK, SERVEU, TOYS, VORZE,
};
pub use quick_moves::{OrgasmSettings, QuickMoves, QuickState, SlowSettings};
pub use sync_engine::{Action, SyncEngine, SyncState};

use std::path::Path;
use std::sync::Arc;

/// 组装一套设备层世界（测试与生产共用）。
pub fn build(ch: Arc<DeviceChannel>, presets_path: &Path) -> DeviceArbiter {
    let quick = QuickMoves::new(ch.clone());
    let preset = PresetPlayer::new(ch.clone(), presets_path);
    let sync = Arc::new(SyncEngine::new(ch.clone()));
    DeviceArbiter {
        ch,
        quick,
        preset,
        sync,
    }
}
