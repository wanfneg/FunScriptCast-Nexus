//! 字幕服务（对应 Python `vendor/subtitle/`）。
//!
//! 设计要点：**这里没有模型推理**。识别交给 `audiocpp_server.exe`（Qwen3-ASR），
//! 翻译交给 `llama-server.exe`（Sakura-7B GGUF）；Rust 只负责拉起进程、发请求、
//! 拼装结果。所以移植的是调度与协议，不是算法。

pub mod audiocpp;
pub mod config;
pub mod llama;
pub mod process;
pub mod segment;
pub mod stream;
pub mod text_filters;
pub mod translate;

pub use config::SubtitleConfig;
