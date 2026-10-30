//! 外部控制台程序的启动参数。
//!
//! release 版是 `windows_subsystem = "windows"`，自身没有控制台。此时用
//! `Command` 启动控制台程序（nvidia-smi、adb、cmd…）会**新建一个控制台窗口**；
//! nvidia-smi 每 3 秒刷新一次，不隐藏就是黑框无限闪烁。

/// 给即将启动的进程加 `CREATE_NO_WINDOW`（仅 Windows 生效）。
#[cfg(windows)]
pub fn hide_console(cmd: &mut std::process::Command) {
    use std::os::windows::process::CommandExt;
    cmd.creation_flags(0x0800_0000);
}

#[cfg(not(windows))]
pub fn hide_console(_cmd: &mut std::process::Command) {}
