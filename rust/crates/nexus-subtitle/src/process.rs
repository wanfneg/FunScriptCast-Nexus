//! 子进程生命周期：拉起常驻服务、探活、停止。
//!
//! 对应 Python 的 `ensure_server/_attach_kill_on_close/stop_server`。
//! Windows 上句柄随进程关闭（Python 用 Job Object）；Rust 侧用 `Child` 的
//! kill-on-drop 语义 + 显式 stop，把"父进程退出后残留 4GB 显存"这条焊死。

use std::path::Path;
use std::process::Stdio;
use std::sync::Arc;
use std::time::Duration;

use tokio::process::{Child, Command};
use tokio::sync::Mutex;

/// GUI 子系统下启动控制台程序会弹黑框，统一加 CREATE_NO_WINDOW。
#[cfg(windows)]
fn hide_console(cmd: &mut Command) {
    cmd.creation_flags(0x0800_0000);
}

#[cfg(not(windows))]
fn hide_console(_cmd: &mut Command) {}

/// std::process::Command 版的防黑窗（VAD CLI 等同步子进程用；不带会每次调用
/// 都闪一个命令行黑窗）。
#[cfg(windows)]
pub fn hide_console_pub(cmd: &mut std::process::Command) {
    use std::os::windows::process::CommandExt;
    cmd.creation_flags(0x0800_0000);
}

#[cfg(not(windows))]
pub fn hide_console_pub(_cmd: &mut std::process::Command) {}

/// 同步等待子进程退出，带超时（秒级 CLI 用；std 没有带超时的 wait）。
/// 返回 `(是否成功退出码, stderr 全文)`；超时 kill 后返回 Err。
pub fn wait_with_timeout(
    cmd: &mut std::process::Command,
    timeout_secs: f64,
) -> Result<(bool, Vec<u8>), String> {
    use std::io::Read;
    use std::process::Stdio;
    cmd.stdin(Stdio::null()).stderr(Stdio::piped());
    let mut child = cmd.spawn().map_err(|e| format!("起子进程失败：{e}"))?;
    let mut err_pipe = child.stderr.take();
    // stderr 用读线程持续收走：CLI 输出多时写满管道会把自己阻塞住
    let err_thread = std::thread::spawn(move || {
        let mut buf = Vec::new();
        if let Some(p) = err_pipe.as_mut() {
            let _ = p.read_to_end(&mut buf);
        }
        buf
    });
    let start = std::time::Instant::now();
    let status = loop {
        match child.try_wait() {
            Ok(Some(s)) => break s,
            Ok(None) => {
                if start.elapsed().as_secs_f64() >= timeout_secs {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err(format!("超时（>{timeout_secs:.0}s）"));
                }
                std::thread::sleep(Duration::from_millis(50));
            }
            Err(e) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(format!("等待子进程失败：{e}"));
            }
        }
    };
    let stderr = err_thread.join().unwrap_or_default();
    Ok((status.success(), stderr))
}

/// 一个受管子进程。
pub struct Managed {
    child: Mutex<Option<Child>>,
    name: String,
}

impl Managed {
    pub fn new(name: impl Into<String>) -> Arc<Self> {
        Arc::new(Self {
            child: Mutex::new(None),
            name: name.into(),
        })
    }

    pub fn name(&self) -> &str {
        &self.name
    }

    pub async fn is_running(&self) -> bool {
        let mut g = self.child.lock().await;
        match g.as_mut() {
            Some(c) => match c.try_wait() {
                Ok(None) => true,
                Ok(Some(_)) | Err(_) => {
                    *g = None;
                    false
                }
            },
            None => false,
        }
    }

    /// 拉起进程；`log_path` 收 stdout/stderr（排障要看 llama/audiocpp 的日志）。
    ///
    /// 已有旧实例时先停掉再起新的：端口不冲突、显存不双份。
    pub async fn spawn(
        &self,
        exe: &Path,
        args: &[String],
        cwd: Option<&Path>,
        log_path: Option<&Path>,
    ) -> std::io::Result<()> {
        // take 旧实例 → spawn 新实例 → 写回，必须在同一临界区：中间释放锁的话，
        // 并发 spawn 会互相覆盖 child，存活进程失去 kill_on_drop 管理（显存残留）
        let mut g = self.child.lock().await;
        if let Some(mut old) = g.take() {
            // 旧实例还活着：kill 后带 5 秒上限等它退出，超时也继续（kill_on_drop 兜底）
            if old.try_wait().map(|st| st.is_none()).unwrap_or(false) {
                let _ = old.start_kill();
                let _ = tokio::time::timeout(Duration::from_secs(5), old.wait()).await;
            }
        }
        let mut cmd = Command::new(exe);
        cmd.args(args)
            .stdin(Stdio::null())
            .kill_on_drop(true);
        hide_console(&mut cmd);
        if let Some(d) = cwd {
            cmd.current_dir(d);
        }
        match log_path {
            Some(p) => {
                if let Some(dir) = p.parent() {
                    let _ = std::fs::create_dir_all(dir);
                }
                let f = std::fs::OpenOptions::new()
                    .create(true)
                    .append(true)
                    .open(p)?;
                let f2 = f.try_clone()?;
                cmd.stdout(Stdio::from(f)).stderr(Stdio::from(f2));
            }
            None => {
                cmd.stdout(Stdio::null()).stderr(Stdio::null());
            }
        }
        let child = cmd.spawn()?;
        *g = Some(child);
        Ok(())
    }

    pub async fn stop(&self) {
        let mut g = self.child.lock().await;
        if let Some(mut c) = g.take() {
            let _ = c.start_kill();
            let _ = c.wait().await;
        }
    }

    /// 进程提前退出时的退出码（用于报错文案）。
    pub async fn exit_code(&self) -> Option<i32> {
        let mut g = self.child.lock().await;
        match g.as_mut() {
            Some(c) => match c.try_wait() {
                Ok(Some(st)) => st.code(),
                _ => None,
            },
            None => None,
        }
    }
}

/// 轮询等到 `probe` 返回 true；超时或进程退出立即失败。
pub async fn wait_ready<F, Fut>(
    wait_s: f64,
    mut probe: F,
    managed: &Managed,
) -> Result<f64, String>
where
    F: FnMut() -> Fut,
    Fut: std::future::Future<Output = bool>,
{
    let t0 = std::time::Instant::now();
    while t0.elapsed().as_secs_f64() < wait_s {
        if probe().await {
            return Ok(t0.elapsed().as_secs_f64());
        }
        if !managed.is_running().await {
            let code = managed.exit_code().await;
            return Err(format!(
                "{} 启动即退出（code {:?}）",
                managed.name(),
                code
            ));
        }
        tokio::time::sleep(Duration::from_millis(500)).await;
    }
    Err(format!(
        "{} 未在 {}s 内就绪",
        managed.name(),
        wait_s.round() as i64
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn spawn_and_stop_real_process() {
        let m = Managed::new("test");
        // 用一个必然存在且能立刻退出的命令：Windows 下 cmd /c exit
        #[cfg(windows)]
        {
            let exe = Path::new("cmd.exe");
            m.spawn(exe, &["/c".into(), "exit".into()], None, None)
                .await
                .unwrap();
        }
        #[cfg(not(windows))]
        {
            let exe = Path::new("/bin/true");
            m.spawn(exe, &[], None, None).await.unwrap();
        }
        tokio::time::sleep(Duration::from_millis(300)).await;
        // 已退出
        assert!(!m.is_running().await);
        m.stop().await;
    }

    #[tokio::test]
    async fn wait_ready_times_out_when_probe_never_true() {
        // 起一个**活着**的进程，探活永远 false → 必须是"超时"而不是"已退出"
        let m = Managed::new("never");
        #[cfg(windows)]
        m.spawn(Path::new("cmd.exe"), &["/c".into(), "ping".into(), "127.0.0.1".into(), "-n".into(), "12".into()], None, None)
            .await
            .unwrap();
        #[cfg(not(windows))]
        m.spawn(Path::new("/bin/sleep"), &["10".into()], None, None)
            .await
            .unwrap();
        tokio::time::sleep(Duration::from_millis(200)).await;
        let r = wait_ready(0.6, || async { false }, &m).await;
        assert!(r.is_err());
        let e = r.unwrap_err();
        assert!(e.contains("未在"), "实际：{e}");
        m.stop().await;
    }

    #[tokio::test]
    async fn wait_ready_reports_premature_exit() {
        // 进程立刻退出：报"启动即退出"，不要傻等到超时
        let m = Managed::new("dies");
        #[cfg(windows)]
        m.spawn(Path::new("cmd.exe"), &["/c".into(), "exit".into()], None, None)
            .await
            .unwrap();
        #[cfg(not(windows))]
        m.spawn(Path::new("/bin/true"), &[], None, None).await.unwrap();
        tokio::time::sleep(Duration::from_millis(400)).await;
        let r = wait_ready(5.0, || async { false }, &m).await;
        assert!(r.is_err());
        let e = r.unwrap_err();
        assert!(e.contains("启动即退出"), "实际：{e}");
    }

    #[tokio::test]
    async fn wait_ready_returns_when_probe_true() {
        let m = Managed::new("ok");
        let r = wait_ready(2.0, || async { true }, &m).await;
        assert!(r.is_ok());
    }

    #[tokio::test]
    async fn spawn_missing_exe_is_an_error() {
        let m = Managed::new("missing");
        let r = m
            .spawn(Path::new("Z:/definitely/not/here.exe"), &[], None, None)
            .await;
        assert!(r.is_err(), "不存在的可执行文件必须报错，不能静默");
    }
}
