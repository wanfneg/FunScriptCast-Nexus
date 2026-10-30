//! 设备写入仲裁（单一写者不变量 I1）。
//!
//! 用户口径：「最终发送到设备上的动作要干净，不能是两种模式的动作同时发送给设备」。
//! BLE 上只有一条写帧流，任一时刻至多一个"运动会话"该处于 active；会话转移矩阵
//! 以前散落在两处 `preset.stop()` 里、其余格子全空 —— 现在全部收口到这里。
//!
//! 急停（I6，用户裁定"四个全受"）：`stop_all()` 把四个会话**全部停止本身**，再由路由
//! 把 `allow_move` 置 false；解除急停只复位闸门，**不重启任何会话**。

use std::sync::Arc;

use serde::Serialize;

use crate::channel::DeviceChannel;
use crate::preset_player::{PresetPlayer, PresetState};
use crate::quick_moves::{QuickMoves, QuickState};
use crate::sync_engine::SyncEngine;

#[derive(Debug, Clone, Serialize)]
pub struct SessionResult {
    pub ok: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    pub deferred: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub script: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub actions: Option<usize>,
}

impl SessionResult {
    fn ok() -> Self {
        Self {
            ok: true,
            error: None,
            deferred: false,
            script: None,
            actions: None,
        }
    }
    fn err(e: impl Into<String>) -> Self {
        Self {
            ok: false,
            error: Some(e.into()),
            deferred: false,
            script: None,
            actions: None,
        }
    }
}

pub struct DeviceArbiter {
    pub ch: Arc<DeviceChannel>,
    pub quick: Arc<QuickMoves>,
    pub preset: Arc<PresetPlayer>,
    pub sync: Arc<SyncEngine>,
}

impl DeviceArbiter {
    // ---- 内部：停掉除 keep 外的所有会话 ----
    fn stop_all_except(&self, keep: &str) {
        if keep != "sync" {
            self.sync.stop();
        }
        if keep != "preset" {
            self.preset.stop();
        }
        if keep != "orgasm" {
            self.quick.stop_orgasm();
        }
        if keep != "slow" {
            self.quick.stop_slow();
        }
    }

    /// 脚本会话开始。
    ///
    /// 手机端 `setExternalControl` 语义（只由**一键爆发**驱动）：
    /// 「一键爆发开启时暂停脚本同步（避免两个循环同时驱动设备）」，
    /// 爆发停止后脚本从当前进度恢复。所以爆发活动期间脚本**让路**：
    /// 不掐爆发、不抢设备，只校验脚本能加载。
    pub fn start_script(&self, path: &str) -> SessionResult {
        if self.quick.is_orgasm() {
            return match self.sync.load(path) {
                Ok(sp) => SessionResult {
                    ok: true,
                    deferred: true,
                    // Python 返回解析后的实际脚本路径，不是视频路径
                    script: Some(sp.to_string_lossy().to_string()),
                    actions: Some(self.sync.state().actions),
                    error: None,
                },
                Err(e) => SessionResult::err(e),
            };
        }
        match self.sync.start(path) {
            Ok(n) => {
                // 清预设（手机端：加载脚本退出预设模式）
                self.preset.stop();
                // **不动缓动**：脚本帧经 onAnyMove 把缓动按在"等待空闲"，脚本停后缓动自动开始。
                // script 字段统一回解析后的实际脚本路径（与 deferred 分支、Python 基准一致，
                // 回视频路径会让前端 SYNC.path 语义漂移）
                SessionResult {
                    actions: Some(n),
                    script: Some(self.sync.state().script),
                    ..SessionResult::ok()
                }
            }
            Err(e) => SessionResult::err(e),
        }
    }

    /// 手动选择任意 `.funscript` 路径启动（不要求与视频同名）。
    ///
    /// 爆发让路与 `start_script` 同一套：爆发期间启动脚本会形成"脚本帧 + 爆发帧"
    /// 双写者（历史"爆发飘忽/互抢"的根因同型），defer 到爆发结束。
    pub fn start_script_file(&self, script: &std::path::Path) -> SessionResult {
        if self.quick.is_orgasm() {
            return match self.sync.load_file(script) {
                Ok(()) => SessionResult {
                    ok: true,
                    deferred: true,
                    script: Some(script.to_string_lossy().to_string()),
                    actions: Some(self.sync.state().actions),
                    error: None,
                },
                Err(e) => SessionResult::err(e),
            };
        }
        match self.sync.start_file(script) {
            Ok(n) => {
                self.preset.stop();
                SessionResult {
                    actions: Some(n),
                    script: Some(script.to_string_lossy().to_string()),
                    ..SessionResult::ok()
                }
            }
            Err(e) => SessionResult::err(e),
        }
    }

    /// AI 实时模式：动作由识别引擎灌入，不走磁盘脚本。与 `start_script` 同一套仲裁语义。
    pub fn start_live(&self, source: &str) -> SessionResult {
        if self.quick.is_orgasm() {
            return SessionResult {
                ok: true,
                deferred: true,
                script: Some(source.to_string()),
                error: None,
                actions: None,
            };
        }
        self.sync.start_live(source);
        self.preset.stop();
        SessionResult {
            ok: true,
            deferred: false,
            script: Some(source.to_string()),
            actions: Some(0),
            error: None,
        }
    }

    pub fn start_preset(&self) -> Result<PresetState, String> {
        self.stop_all_except("preset"); // = 三件事里的"清脚本 + 停快捷动作"
        self.quick.discard_resume(); // 预设在播，视频再播放不许把爆发/缓动拉起来抢设备
        self.preset.start()
    }

    pub fn toggle_preset(&self) -> Result<PresetState, String> {
        if self.preset.is_playing() {
            Ok(self.preset.stop())
        } else {
            self.start_preset()
        }
    }

    pub fn start_orgasm(&self) -> QuickState {
        self.stop_all_except("orgasm");
        self.quick.start_orgasm()
    }

    /// 手机端 `startSlow`：只与爆发互斥。**不停脚本** ——
    /// 否则前端看护会把脚本拉回来又顶掉缓动（造成"点待机缓动立马就自动取消"）。
    pub fn start_slow(&self) -> QuickState {
        self.preset.stop(); // 预设在播则让位（单一写者）
        self.quick.start_slow()
    }

    // ---- 急停 / 断开 / 退出共用 ----
    pub fn stop_all(&self) {
        self.stop_all_except("");
        // 内聚闸门：停会话的同时落下急停闸，会话间切换窗口没有漏帧。
        // 解除急停仍走 quick.set_stop(false) / set_allow_move(true) 独立入口，
        // 不在这里做复位 —— I6：解除不自动恢复任何会话。
        self.ch.set_allow_move(false);
    }
}

