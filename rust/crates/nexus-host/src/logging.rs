//! 宿主日志：环形缓冲（给 `/api/logs`）+ 追加写 `var/logs/host.log`。

use std::collections::VecDeque;
use std::io::Write;
use std::path::PathBuf;
use std::sync::Mutex;

pub const RING_CAPACITY: usize = 2000;

#[derive(Debug, Clone, serde::Serialize)]
pub struct LogLine {
    pub ts: String,
    pub level: String,
    pub msg: String,
}

pub struct LogStore {
    ring: Mutex<VecDeque<LogLine>>,
    path: Mutex<Option<PathBuf>>,
}

impl LogStore {
    pub fn new(path: Option<PathBuf>) -> Self {
        Self {
            ring: Mutex::new(VecDeque::with_capacity(RING_CAPACITY)),
            path: Mutex::new(path),
        }
    }

    pub fn add(&self, level: &str, msg: impl Into<String>) {
        let msg = msg.into();
        let line = LogLine {
            ts: now_stamp(),
            level: level.to_string(),
            msg: msg.clone(),
        };
        {
            let mut r = self.ring.lock().unwrap();
            if r.len() >= RING_CAPACITY {
                r.pop_front();
            }
            r.push_back(line);
        }
        if let Some(p) = self.path.lock().unwrap().as_ref() {
            if let Some(dir) = p.parent() {
                let _ = std::fs::create_dir_all(dir);
            }
            if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(p) {
                let _ = writeln!(f, "[{}] {} {}", now_stamp(), level.to_uppercase(), msg);
            }
        }
    }

    pub fn info(&self, msg: impl Into<String>) {
        self.add("info", msg)
    }
    pub fn warn(&self, msg: impl Into<String>) {
        self.add("warn", msg)
    }
    pub fn error(&self, msg: impl Into<String>) {
        self.add("error", msg)
    }

    /// 取最近 `limit` 行（返回时间正序）。
    pub fn tail(&self, limit: usize) -> Vec<LogLine> {
        let r = self.ring.lock().unwrap();
        let skip = r.len().saturating_sub(limit);
        r.iter().skip(skip).cloned().collect()
    }
}

fn now_stamp() -> String {
    // 不引 chrono：用 UNIX 秒 + 毫秒，够给日志排序用
    let d = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    format!("{}.{:03}", d.as_secs(), d.subsec_millis())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ring_keeps_tail_and_respects_capacity() {
        let s = LogStore::new(None);
        for i in 0..(RING_CAPACITY + 10) {
            s.info(format!("line {i}"));
        }
        let t = s.tail(5);
        assert_eq!(t.len(), 5);
        assert!(t.last().unwrap().msg.ends_with("line 2009"));
        assert_eq!(s.tail(RING_CAPACITY + 100).len(), RING_CAPACITY);
    }
}
/// 全局日志句柄：给没有 AppState 的地方（如 IPC 桥）写日志用。
/// 界面「事件流」看的就是这份，所以对话框的开/关必须写进来。
static GLOBAL: std::sync::OnceLock<std::sync::Arc<LogStore>> = std::sync::OnceLock::new();

pub fn set_global(store: std::sync::Arc<LogStore>) {
    let _ = GLOBAL.set(store);
}

pub fn global_info(msg: impl Into<String>) {
    if let Some(l) = GLOBAL.get() {
        l.info(msg);
    }
}

pub fn global_warn(msg: impl Into<String>) {
    if let Some(l) = GLOBAL.get() {
        l.warn(msg);
    }
}
