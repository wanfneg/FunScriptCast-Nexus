//! DLNA/UPnP 媒体服务器（对应 Python `vendor/dlna/vr_dlna.py`）。
//!
//! 职责：
//! - **SSDP**：响应 M-SEARCH、周期 NOTIFY，让头显/手机能发现本机
//! - **ContentDirectory**：Browse 目录、返回 DIDL-Lite（含外挂字幕的 CaptionInfo）
//! - **媒体流**：`/media/<key>` 支持 Range（DeoVR 拖进度靠它）
//!
//! 安全边界（与 Python 版同口径）：
//! - key → 路径必须留在媒体根内（拒绝 `..`、盘符、ADS 后缀）
//! - 根之下任一层是 junction/符号链接都拒（否则字符串看着在根内、实际在根外）

pub mod app;
pub mod httpd;
pub mod library;
pub mod ssdp;

pub use app::DlnaApp;
pub use library::{MediaLibrary, MediaRoot};
