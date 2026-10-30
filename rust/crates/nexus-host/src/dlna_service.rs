//! DLNA 服务生命周期：起停 HTTP + SSDP，报告状态。

use std::net::IpAddr;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use nexus_dlna::{DlnaApp, MediaLibrary, MediaRoot};

/// 挑一个局域网 IPv4（非回环、非链路本地），失败退回 127.0.0.1。
pub fn lan_ip() -> String {
    // 不依赖系统 API：connect 一个 UDP 目标读本地端地址（不实际发包）
    if let Ok(s) = std::net::UdpSocket::bind("0.0.0.0:0") {
        if s.connect("223.5.5.5:53").is_ok() {
            if let Ok(addr) = s.local_addr() {
                let ip = addr.ip();
                if !ip.is_loopback() && !ip.is_unspecified() {
                    return ip.to_string();
                }
            }
        }
    }
    for ip in local_ips() {
        if let IpAddr::V4(v4) = ip {
            if !v4.is_loopback() && !v4.is_link_local() {
                return v4.to_string();
            }
        }
    }
    "127.0.0.1".to_string()
}

/// 本机全部 IPv4（通过 hostname 解析）。
pub fn local_ips() -> Vec<IpAddr> {
    let hostname = std::env::var("COMPUTERNAME")
        .or_else(|_| std::env::var("HOSTNAME"))
        .unwrap_or_default();
    let mut out = Vec::new();
    if let Ok(addrs) = std::net::ToSocketAddrs::to_socket_addrs(&format!("{hostname}:0")) {
        for a in addrs {
            out.push(a.ip());
        }
    }
    out
}

#[derive(Default)]
pub struct DlnaStatus {
    pub running: bool,
    pub starting: bool,
    pub error: String,
    pub port: u16,
    pub url: Option<String>,
    pub roots: Vec<String>,
}

struct Inner {
    app: Option<Arc<DlnaApp>>,
    http_task: Option<tokio::task::JoinHandle<()>>,
    ssdp_task: Option<tokio::task::JoinHandle<()>>,
    /// SSDP 停机信号：send 后任务广播 ssdp:byebye 再退出（abort 不发 byebye，
    /// 客户端会把已停服务残留成僵尸 30 分钟）
    ssdp_stop: Option<tokio::sync::oneshot::Sender<()>>,
    port: u16,
    error: String,
    /// start 期间发生了 stop：start_inner 完成写入前据此放弃
    stopped_during_start: bool,
}

pub struct DlnaService {
    /// 用 Arc 包起来：http_task 的 'static 闭包在意外退出时要能复位 running / 写 error
    inner: Arc<Mutex<Inner>>,
    running: Arc<AtomicBool>,
    starting: AtomicBool,
}

impl Default for DlnaService {
    fn default() -> Self {
        Self::new()
    }
}

impl DlnaService {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(Mutex::new(Inner {
                app: None,
                http_task: None,
                ssdp_task: None,
                ssdp_stop: None,
                port: 0,
                error: String::new(),
                stopped_during_start: false,
            })),
            running: Arc::new(AtomicBool::new(false)),
            starting: AtomicBool::new(false),
        }
    }

    pub fn is_running(&self) -> bool {
        self.running.load(Ordering::SeqCst)
    }

    pub fn status(&self, roots: Vec<String>) -> DlnaStatus {
        let g = self.inner.lock().unwrap();
        let running = self.running.load(Ordering::SeqCst);
        DlnaStatus {
            running,
            starting: self.starting.load(Ordering::SeqCst),
            error: g.error.clone(),
            port: g.port,
            url: if running {
                Some(format!("http://{}:{}", lan_ip(), g.port))
            } else {
                None
            },
            roots,
        }
    }

    /// 起服务（幂等）。`roots` 是媒体根目录。
    pub async fn start(&self, port: u16, roots: Vec<String>) -> Result<String, String> {
        if self.is_running() {
            // 幂等：返回真实地址而不是提示文案——调用方把返回值当 URL 记日志，
            // 文案会拼出 "http://http://…" 这种畸形串
            let g = self.inner.lock().unwrap();
            return Ok(format!("http://{}:{}", lan_ip(), g.port));
        }
        // CAS 互斥：并发 start 只放一个进来，其余的立即拒绝，
        // 避免两个实例同时 bind、同时写状态互相踩
        if self
            .starting
            .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
            .is_err()
        {
            return Err("已有一次启动在进行，请稍候".into());
        }
        let result = self.start_inner(port, roots).await;
        // 无论成败都要复位 starting（本函数自此再无提前 return）
        self.starting.store(false, Ordering::SeqCst);
        match &result {
            Ok(url) => {
                self.running.store(true, Ordering::SeqCst);
                tracing::info!("DLNA 已启动：{url}");
            }
            Err(e) => {
                // 只在最终确实没跑起来时才落错误：
                // 别让后来并发实例的失败覆盖已成功实例的干净状态（running=true 却 error 非空）
                if !self.running.load(Ordering::SeqCst) {
                    self.inner.lock().unwrap().error = e.clone();
                }
                tracing::error!("DLNA 启动失败：{e}");
            }
        }
        result
    }

    async fn start_inner(&self, port: u16, roots: Vec<String>) -> Result<String, String> {
        let mut media_roots: Vec<MediaRoot> = roots
            .iter()
            .map(std::path::PathBuf::from)
            .filter(|p| p.is_dir())
            .map(MediaRoot::from_path)
            .collect();
        // label 强制唯一（Python 同款）：两个根末段同名时 key 歧义——
        // 第二个根的文件要么 404、要么串到第一个根的同路径文件（播错内容）
        let mut seen: std::collections::HashMap<String, usize> = std::collections::HashMap::new();
        for r in &mut media_roots {
            let n = seen.entry(r.label.to_lowercase()).or_insert(0);
            *n += 1;
            if *n > 1 {
                r.label = format!("{} ({})", r.label, n);
            }
        }
        if media_roots.is_empty() {
            return Err("没有可用的媒体根目录".to_string());
        }
        let lib = Arc::new(MediaLibrary::new(media_roots));
        let ip = lan_ip();
        let app = Arc::new(DlnaApp::new(lib, port, ip.clone()));
        let router = nexus_dlna::httpd::router(app.clone());

        let listener = tokio::net::TcpListener::bind(("0.0.0.0", port))
            .await
            .map_err(|e| format!("监听 {port} 失败：{e}"))?;
        // 克隆共享句柄给 'static 任务：HTTP 意外退出时由它自己复位状态
        let running = Arc::clone(&self.running);
        let inner = Arc::clone(&self.inner);
        let http_task = tokio::spawn(async move {
            if let Err(e) = axum::serve(listener, router).await {
                tracing::error!("DLNA HTTP 退出：{e}");
                // HTTP 主循环意外退出：复位运行状态并记下原因，
                // 免得 UI 一直显示"运行中"却连不上
                running.store(false, Ordering::SeqCst);
                inner.lock().unwrap().error = format!("HTTP 服务意外退出：{e}");
            }
        });

        let (ssdp_task, ssdp_stop) = match nexus_dlna::ssdp::serve(app.clone()).await {
            Ok((h, stop)) => (Some(h), Some(stop)),
            Err(e) => {
                tracing::warn!("SSDP 未启动（{e}）：DLNA 只能靠手动地址访问");
                (None, None)
            }
        };

        let mut stopped_during_start = false;
        {
            let mut g = self.inner.lock().unwrap();
            stopped_during_start = g.stopped_during_start;
            if stopped_during_start {
                // start 期间用户点了 stop：句柄不写入 inner（stop 已返回"已停止"），
                // 已起资源就地回收，按失败退出
                http_task.abort();
                if let Some(t) = ssdp_task {
                    t.abort();
                }
            } else {
                g.app = Some(app);
                g.http_task = Some(http_task);
                g.ssdp_task = ssdp_task;
                g.ssdp_stop = ssdp_stop;
                g.port = port;
                g.error.clear();
            }
        }
        if stopped_during_start {
            return Err("启动期间服务已被停止".to_string());
        }
        Ok(format!("http://{ip}:{port}"))
    }

    /// 停服务。SSDP 先发停机信号（任务广播 byebye 后自行退出），
    /// abort 只用于兜底（信号丢失/任务卡住）。
    pub fn stop(&self) {
        let (http_task, ssdp_task) = {
            let mut g = self.inner.lock().unwrap();
            if let Some(stop) = g.ssdp_stop.take() {
                let _ = stop.send(());
            }
            let http = g.http_task.take();
            let ssdp = g.ssdp_task.take();
            g.app = None;
            // 启动还在途：标记给 start_inner，让它在写入句柄前放弃，
            // 否则"stop 已返回已停止"而 start 完成写入后服务实际在运行
            if self.starting.load(Ordering::SeqCst) {
                g.stopped_during_start = true;
            }
            (http, ssdp)
        };
        self.running.store(false, Ordering::SeqCst);
        // SSDP 任务要先广播 ssdp:byebye 再自行退出：无条件 abort 会在它的下一个
        // await 点直接取消，下线信号发不出去，客户端把已停服务残留成僵尸 30 分钟。
        // 先等它自然退出（上限 3s），超时才 abort 兜底（信号丢失/任务卡住）。
        if let Some(t) = ssdp_task {
            let deadline = std::time::Instant::now() + std::time::Duration::from_secs(3);
            while !t.is_finished() && std::time::Instant::now() < deadline {
                std::thread::sleep(std::time::Duration::from_millis(50));
            }
            if !t.is_finished() {
                t.abort();
            }
        }
        // HTTP 任务不涉及 byebye，直接 abort（axum::serve 的取消是安全的）
        if let Some(t) = http_task {
            t.abort();
        }
        tracing::info!("DLNA 已停止");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lan_ip_is_never_empty() {
        let ip = lan_ip();
        assert!(!ip.is_empty());
    }

    #[test]
    fn status_reports_not_running_by_default() {
        let s = DlnaService::new();
        let st = s.status(vec!["/tmp".into()]);
        assert!(!st.running);
        assert!(st.url.is_none());
        assert_eq!(st.roots, vec!["/tmp".to_string()]);
    }

    #[tokio::test]
    async fn start_without_roots_fails_clearly() {
        let s = DlnaService::new();
        let r = s.start(18899, vec![]).await;
        assert!(r.is_err());
        assert!(r.unwrap_err().contains("媒体根目录"));
        assert!(!s.is_running());
    }

    #[tokio::test]
    async fn start_and_stop_real_server() {
        let dir = std::env::temp_dir().join(format!("nexus-dlna-svc-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("a.mp4"), b"hello").unwrap();
        let s = DlnaService::new();
        let r = s.start(18898, vec![dir.to_string_lossy().to_string()]).await;
        assert!(r.is_ok(), "启动失败：{r:?}");
        assert!(s.is_running());
        s.stop();
        assert!(!s.is_running());
        let _ = std::fs::remove_dir_all(&dir);
    }
}
