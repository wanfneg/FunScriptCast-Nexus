//! 开机自启（HKCU `...\CurrentVersion\Run`）。
//!
//! 逐条对应 Python `_autostart_enabled` / `_set_autostart`（`host_server.py:357-389`）：
//! - **真值只在注册表里**：Windows 设置 / 任务管理器侧关掉也要反映到设置页；
//! - 值指向的 exe 与当前程序路径不一致（挪过盘/重新安装）视为失效；
//! - 关闭时值本来就不存在也算成功（Python 忽略 `FileNotFoundError`）。
//!
//! 用 Win32 注册表 API 而不是 `reg.exe`：后者走控制台代码页，中文安装路径
//! 读回来会变成乱码，导致"明明开着却显示关"。

use std::path::Path;

pub const RUN_KEY: &str = r"Software\Microsoft\Windows\CurrentVersion\Run";
pub const RUN_NAME: &str = "FunScriptCast-Nexus";

/// 写进 Run 键的命令行：`"C:\path\app.exe"`（Python `f'"{sys.executable}"'` 同款）。
pub fn command_line(exe: &Path) -> String {
    format!("\"{}\"", exe.display())
}

fn normalize(s: &str) -> String {
    s.trim()
        .trim_matches(|c| c == '"' || c == ' ')
        .replace('/', "\\")
        .to_lowercase()
}

/// 注册表值与当前 exe 是否一致（去引号、斜杠归一、大小写不敏感）。
pub fn value_matches_exe(value: &str, exe: &Path) -> bool {
    !value.trim().is_empty() && normalize(value) == normalize(&exe.display().to_string())
}

#[cfg(windows)]
mod imp {
    use std::path::Path;

    use windows::core::PCWSTR;
    use windows::Win32::Foundation::{ERROR_FILE_NOT_FOUND, ERROR_SUCCESS};
    use windows::Win32::System::Registry::{
        RegCloseKey, RegDeleteValueW, RegOpenKeyExW, RegQueryValueExW, RegSetValueExW, HKEY,
        HKEY_CURRENT_USER, KEY_READ, KEY_WRITE, REG_SZ,
    };

    use super::{command_line, value_matches_exe, RUN_KEY, RUN_NAME};

    fn wide(s: &str) -> Vec<u16> {
        s.encode_utf16().chain(std::iter::once(0)).collect()
    }

    fn open_run(write: bool) -> Option<HKEY> {
        let sub = wide(RUN_KEY);
        let mut key = HKEY::default();
        let access = if write { KEY_READ | KEY_WRITE } else { KEY_READ };
        let rc = unsafe {
            RegOpenKeyExW(HKEY_CURRENT_USER, PCWSTR(sub.as_ptr()), 0, access, &mut key)
        };
        (rc == ERROR_SUCCESS).then_some(key)
    }

    fn read_value() -> Option<String> {
        let key = open_run(false)?;
        let name = wide(RUN_NAME);
        let mut ty = REG_SZ;
        let mut size: u32 = 0;
        let rc = unsafe {
            RegQueryValueExW(
                key,
                PCWSTR(name.as_ptr()),
                None,
                Some(&mut ty),
                None,
                Some(&mut size),
            )
        };
        if rc != ERROR_SUCCESS || size == 0 {
            unsafe {
                let _ = RegCloseKey(key);
            }
            return None;
        }
        let mut buf = vec![0u8; size as usize];
        let rc = unsafe {
            RegQueryValueExW(
                key,
                PCWSTR(name.as_ptr()),
                None,
                Some(&mut ty),
                Some(buf.as_mut_ptr()),
                Some(&mut size),
            )
        };
        unsafe {
            let _ = RegCloseKey(key);
        }
        if rc != ERROR_SUCCESS {
            return None;
        }
        // REG_SZ 是 UTF-16LE、以 NUL 结尾；路径可能含中文，绝不能按当前代码页解。
        let words: Vec<u16> = buf[..(size as usize).min(buf.len())]
            .chunks_exact(2)
            .map(|c| u16::from_le_bytes([c[0], c[1]]))
            .collect();
        let s = String::from_utf16_lossy(&words);
        Some(s.trim_end_matches('\0').to_string())
    }

    pub fn read_raw() -> Option<String> {
        read_value()
    }

    pub fn enabled_for(exe: &Path) -> bool {
        read_value()
            .map(|v| value_matches_exe(&v, exe))
            .unwrap_or(false)
    }

    pub fn set_enabled(on: bool, exe: &Path) -> Result<(), String> {
        let key = open_run(true)
            .ok_or_else(|| "无法打开 HKCU Run 注册表键（权限不足？）".to_string())?;
        let name = wide(RUN_NAME);
        if on {
            let value = wide(&command_line(exe));
            let bytes = unsafe {
                std::slice::from_raw_parts(value.as_ptr() as *const u8, value.len() * 2)
            };
            let rc = unsafe {
                RegSetValueExW(key, PCWSTR(name.as_ptr()), 0, REG_SZ, Some(bytes))
            };
            unsafe {
                let _ = RegCloseKey(key);
            }
            if rc != ERROR_SUCCESS {
                return Err(format!("写 HKCU Run 失败（错误码 {}）", rc.0));
            }
        } else {
            let rc = unsafe { RegDeleteValueW(key, PCWSTR(name.as_ptr())) };
            unsafe {
                let _ = RegCloseKey(key);
            }
            // 值本来就不存在 = 已经处于关闭状态（Python FileNotFoundError 同义）
            if rc != ERROR_SUCCESS && rc != ERROR_FILE_NOT_FOUND {
                return Err(format!("删除 HKCU Run 值失败（错误码 {}）", rc.0));
            }
        }
        Ok(())
    }
}

/// 注册表里当前记录的值（排障用）。
#[cfg(windows)]
pub fn current_value() -> Option<String> {
    imp::read_raw()
}

#[cfg(not(windows))]
pub fn current_value() -> Option<String> {
    None
}

/// 当前程序是否已开机自启（值必须指向本 exe）。
///
/// 开发目录（`target/debug|release`）不算：Python `_autostart_enabled` 在
/// 非 frozen 时直接返回 False，避免把临时 exe 写进注册表。
pub fn enabled() -> bool {
    is_packaged() && enabled_for(&std::env::current_exe().unwrap_or_default())
}

/// 是否是安装形态：exe 同目录带 `var/` 或 `vendor/`。
///
/// 开发版的 `paths::app_dir()` 会向上找到仓库根，而 exe 在 `target/...` 下，
/// 用它区分"安装包里的程序"与"cargo run 出来的临时 exe"。
pub fn is_packaged() -> bool {
    std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|d| d.to_path_buf()))
        .map(|d| d.join("var").is_dir() || d.join("vendor").is_dir())
        .unwrap_or(false)
}

pub fn enabled_for(exe: &Path) -> bool {
    #[cfg(windows)]
    {
        imp::enabled_for(exe)
    }
    #[cfg(not(windows))]
    {
        let _ = exe;
        false
    }
}

#[cfg(windows)]
pub fn set_enabled(on: bool, exe: &Path) -> Result<(), String> {
    imp::set_enabled(on, exe)
}

#[cfg(not(windows))]
pub fn set_enabled(on: bool, _exe: &Path) -> Result<(), String> {
    if on {
        Err("当前平台不支持开机自启".to_string())
    } else {
        Ok(())
    }
}

/// 按当前 exe 开关自启（HTTP 设置保存入口用）。
pub fn set_launch_on_boot(on: bool) -> Result<(), String> {
    if !is_packaged() {
        // Python `_set_autostart`：源码运行不写注册表（没有可注册的安装 exe）
        return Ok(());
    }
    let exe = std::env::current_exe().map_err(|e| format!("无法取得程序路径：{e}"))?;
    set_enabled(on, &exe)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn command_line_quotes_exe_path() {
        let exe = Path::new(r"C:\Program Files\FunScriptCast-Nexus\nexus-host.exe");
        assert_eq!(
            command_line(exe),
            r#""C:\Program Files\FunScriptCast-Nexus\nexus-host.exe""#
        );
    }

    #[test]
    fn registry_value_matches_same_exe_only() {
        let exe = Path::new(r"C:\Apps\Nexus\nexus-host.exe");
        assert!(value_matches_exe(
            r#""C:\Apps\Nexus\nexus-host.exe""#,
            exe
        ));
        // 斜杠方向与大小写不影响
        assert!(value_matches_exe("c:/apps/nexus/NEXUS-HOST.EXE", exe));
        // 挪过盘的值视为失效（UI 显示关，重新打开会用新路径重写）
        assert!(!value_matches_exe(
            r#""D:\Old\nexus-host.exe""#,
            exe
        ));
        assert!(!value_matches_exe("", exe));
    }

    /// 关闭自启时值不存在也应当成功（不能因为没装过就报错）。
    #[test]
    fn disabling_is_idempotent() {
        // 只验证非 Windows 分支与参数校验，不真的读写注册表
        #[cfg(not(windows))]
        assert!(set_enabled(false, Path::new("x.exe")).is_ok());
    }
}
