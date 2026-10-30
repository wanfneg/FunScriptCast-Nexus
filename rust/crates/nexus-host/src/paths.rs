//! 运行期路径解析（与 Python 版 `var/{data,logs,run,backup}` 布局一致）。

use std::path::{Path, PathBuf};
use std::sync::OnceLock;

static APP_DIR: OnceLock<PathBuf> = OnceLock::new();

/// 应用根目录：exe 所在目录（打包版）或仓库根（开发版）。
pub fn app_dir() -> &'static Path {
    APP_DIR.get_or_init(|| {
        if let Ok(exe) = std::env::current_exe() {
            if let Some(dir) = exe.parent() {
                if dir.join("var").is_dir() || dir.join("vendor").is_dir() {
                    return dir.to_path_buf();
                }
                if let Some(root) = find_repo_root(dir) {
                    return root;
                }
                return dir.to_path_buf();
            }
        }
        std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."))
    })
}

fn find_repo_root(start: &Path) -> Option<PathBuf> {
    let mut cur = Some(start);
    for _ in 0..6 {
        let p = cur?;
        if p.join("vendor").is_dir() && p.join("host_server.py").is_file() {
            return Some(p.to_path_buf());
        }
        cur = p.parent();
    }
    None
}

pub fn var_dir() -> PathBuf {
    app_dir().join("var")
}
pub fn data_dir() -> PathBuf {
    var_dir().join("data")
}
pub fn log_dir() -> PathBuf {
    var_dir().join("logs")
}
pub fn run_dir() -> PathBuf {
    var_dir().join("run")
}
pub fn settings_file() -> PathBuf {
    data_dir().join("integrated_settings.json")
}
pub fn host_log() -> PathBuf {
    log_dir().join("host.log")
}
pub fn presets_file() -> PathBuf {
    app_dir().join("ui-legacy").join("presets.json")
}
pub fn ui_dir() -> PathBuf {
    app_dir().join("ui-legacy")
}
pub fn web_dist() -> PathBuf {
    app_dir().join("web").join("dist")
}
pub fn vision_dir() -> PathBuf {
    app_dir().join("vendor").join("vision")
}
pub fn ensure_dirs() -> std::io::Result<()> {
    for d in [data_dir(), log_dir(), run_dir()] {
        std::fs::create_dir_all(d)?;
    }
    Ok(())
}
