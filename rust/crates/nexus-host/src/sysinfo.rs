//! 系统指标：GPU / CPU / 内存 / 局域网 IP。
//!
//! 逐条对齐 `host_server.py` 的 `gpu_info()` / `sys_info()` / `lan_ip()`。
//!
//! ⚠ **绝不能在 handler 里同步跑 nvidia-smi**：那是阻塞调用，会把 tokio worker
//! 卡住，`/api/state` 直接超时（用户实测 15 秒无响应 + toast 风暴）。
//! 改成**后台刷新线程**：每 1 秒采样一次，handler 只克隆缓存。
//! 最坏情况是拿到略旧的值，但永不阻塞。

use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::{Mutex, OnceLock};

use serde_json::{json, Value};

static GPU: Mutex<Option<Value>> = Mutex::new(None);
static SYS: Mutex<Option<Value>> = Mutex::new(None);
static LAN_IP: Mutex<Option<String>> = Mutex::new(None);
static PREV_CPU: Mutex<Option<(u64, u64, u64)>> = Mutex::new(None);
static STARTED: OnceLock<()> = OnceLock::new();
static GPU_AT: AtomicI64 = AtomicI64::new(0);

fn now_ms() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

/// 启动后台刷新线程（幂等）。宿主启动时调一次。
pub fn start_refresher() {
    if STARTED.set(()).is_err() {
        return;
    }
    // 局域网 IP 只需算一次（网卡变动极少）
    if let Ok(mut g) = LAN_IP.lock() {
        *g = Some(detect_lan_ip());
    }
    let _ = std::thread::Builder::new()
        .name("sysinfo".into())
        .spawn(|| {
            let mut last_gpu = 0i64;
            loop {
                // CPU / 内存：每秒一次（CPU 占比必须两次采样做差）
                let (ram_used, ram_total) = sample_mem();
                let cur = unsafe { read_system_times() };
                let cpu = compute_cpu_pct(cur);
                let v = json!({
                    "cpu_pct": cpu,
                    "ram_used_mb": ram_used,
                    "ram_total_mb": ram_total,
                });
                if let Ok(mut g) = SYS.lock() {
                    *g = Some(v);
                }
                // GPU：3 秒一次（nvidia-smi 贵，且绝不能挡住请求路径）
                let t = now_ms();
                if t - last_gpu >= 3000 {
                    last_gpu = t;
                    let g = query_nvidia().unwrap_or_else(
                        || json!({"name": "", "used_mb": 0, "total_mb": 0, "util": 0}),
                    );
                    if let Ok(mut s) = GPU.lock() {
                        *s = Some(g);
                    }
                    GPU_AT.store(t, Ordering::SeqCst);
                }
                std::thread::sleep(std::time::Duration::from_secs(1));
            }
        });
}

fn compute_cpu_pct(cur: Option<(u64, u64, u64)>) -> i64 {
    let Some(cur) = cur else { return 0 };
    let mut g = match PREV_CPU.lock() {
        Ok(x) => x,
        Err(_) => return 0,
    };
    let out = match g.as_ref() {
        Some(prev) => {
            let d_idle = cur.0.saturating_sub(prev.0);
            let d_total = (cur.1.saturating_sub(prev.1)) + (cur.2.saturating_sub(prev.2));
            if d_total > 0 {
                (((d_total - d_idle) as f64 / d_total as f64) * 100.0).round() as i64
            } else {
                0
            }
        }
        None => 0,
    };
    *g = Some(cur);
    out
}

#[cfg(windows)]
unsafe fn read_system_times() -> Option<(u64, u64, u64)> {
    use windows::Win32::Foundation::FILETIME;
    use windows::Win32::System::Threading::GetSystemTimes;
    let mut idle = FILETIME::default();
    let mut kern = FILETIME::default();
    let mut user = FILETIME::default();
    if GetSystemTimes(Some(&mut idle), Some(&mut kern), Some(&mut user)).is_err() {
        return None;
    }
    let t = |f: FILETIME| -> u64 { ((f.dwHighDateTime as u64) << 32) | f.dwLowDateTime as u64 };
    Some((t(idle), t(kern), t(user)))
}

#[cfg(not(windows))]
unsafe fn read_system_times() -> Option<(u64, u64, u64)> {
    None
}

#[cfg(windows)]
fn sample_mem() -> (i64, i64) {
    use windows::Win32::System::SystemInformation::{GlobalMemoryStatusEx, MEMORYSTATUSEX};
    let mut m = MEMORYSTATUSEX {
        dwLength: std::mem::size_of::<MEMORYSTATUSEX>() as u32,
        ..Default::default()
    };
    let mut used = 0i64;
    let mut total = 0i64;
    unsafe {
        if GlobalMemoryStatusEx(&mut m).is_ok() {
            total = (m.ullTotalPhys / (1024 * 1024)) as i64;
            used = ((m.ullTotalPhys - m.ullAvailPhys) / (1024 * 1024)) as i64;
        }
    }
    (used, total)
}

#[cfg(not(windows))]
fn sample_mem() -> (i64, i64) {
    (0, 0)
}

/// `gpu_info()`：`{name, used_mb, total_mb, util}` —— 只读缓存，绝不阻塞。
pub fn gpu_info() -> Value {
    GPU.lock()
        .ok()
        .and_then(|g| g.clone())
        .unwrap_or_else(|| json!({"name": "", "used_mb": 0, "total_mb": 0, "util": 0}))
}

/// `sys_info()`：`{cpu_pct, ram_used_mb, ram_total_mb}` —— 只读缓存。
pub fn sys_info() -> Value {
    SYS.lock()
        .ok()
        .and_then(|g| g.clone())
        .unwrap_or_else(|| json!({"cpu_pct": 0, "ram_used_mb": 0, "ram_total_mb": 0}))
}

/// `lan_ip()`：本机在局域网里的地址（UDP connect 探出口网卡，不发包）。
pub fn lan_ip() -> String {
    LAN_IP
        .lock()
        .ok()
        .and_then(|g| g.clone())
        .unwrap_or_else(detect_lan_ip)
}

fn detect_lan_ip() -> String {
    if let Ok(s) = std::net::UdpSocket::bind("0.0.0.0:0") {
        if s.connect("8.8.8.8:80").is_ok() {
            if let Ok(a) = s.local_addr() {
                return a.ip().to_string();
            }
        }
    }
    "127.0.0.1".to_string()
}

fn query_nvidia() -> Option<Value> {
    let mut cmd = std::process::Command::new("nvidia-smi");
    cmd.args([
        "--query-gpu=name,memory.used,memory.total,utilization.gpu",
        "--format=csv,noheader,nounits",
    ]);
    crate::proc_util::hide_console(&mut cmd);
    let out = cmd.output().ok()?;
    if !out.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&out.stdout);
    let line = text.lines().next()?.trim();
    let parts: Vec<&str> = line.split(',').map(|s| s.trim()).collect();
    if parts.len() < 4 {
        return None;
    }
    Some(json!({
        "name": parts[0],
        "used_mb": parts[1].parse::<f64>().unwrap_or(0.0) as i64,
        "total_mb": parts[2].parse::<f64>().unwrap_or(0.0) as i64,
        "util": parts[3].parse::<f64>().unwrap_or(0.0) as i64,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn readers_never_block_and_have_python_keys() {
        for k in ["name", "used_mb", "total_mb", "util"] {
            assert!(gpu_info().get(k).is_some(), "gpu 缺 {k}");
        }
        for k in ["cpu_pct", "ram_used_mb", "ram_total_mb"] {
            assert!(sys_info().get(k).is_some(), "sys 缺 {k}");
        }
        assert!(!lan_ip().is_empty());
    }
}
