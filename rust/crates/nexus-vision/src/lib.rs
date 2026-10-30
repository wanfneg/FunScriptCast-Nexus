//! 视觉服务（对应 Python `vendor/vision/`）。
//!
//! 分工：
//! - **静止门**（`still_gate`）：项目自研的纯数学后处理，必须逐值保留 —— 它砍掉约
//!   22% 的动作点，是"用户今天看到的成品"的一部分。
//! - **动作缓冲**（`buffer`）：流式前缀重算的发布语义。
//! - **bundle**（`bundle`）：解析 ONNX bundle 的 manifest（v_std / grid / dim / packs）。
//! - **推理**（`inference`）：抽象成 trait —— 官方 MIT 的 Rust 实现是最终目标；
//!   当前提供 `goblinscript.exe` 命令行后端与测试用假后端。
//! - **服务**（`service`）：与 Python `run_server.py` 同形的 HTTP 接口。

pub mod buffer;
pub mod bundle;
pub mod compose;
pub mod inference;
pub mod onnx;
pub mod service;
pub mod stream;
pub mod style;
pub mod transnet;
pub mod still_gate;

pub use buffer::{Action, ActionBuffer};
pub use bundle::Bundle;
pub use still_gate::{gate_actions, keep_flags, GateConfig, GateMode};

/// GUI 子系统下启动控制台程序会弹黑框，统一加 CREATE_NO_WINDOW。
#[cfg(windows)]
pub(crate) fn hide_console_std(cmd: &mut std::process::Command) {
    use std::os::windows::process::CommandExt;
    cmd.creation_flags(0x0800_0000);
}

#[cfg(not(windows))]
pub(crate) fn hide_console_std(_cmd: &mut std::process::Command) {}

#[cfg(windows)]
pub(crate) fn hide_console_tokio(cmd: &mut tokio::process::Command) {
    cmd.creation_flags(0x0800_0000);
}

#[cfg(not(windows))]
pub(crate) fn hide_console_tokio(_cmd: &mut tokio::process::Command) {}
