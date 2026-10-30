//! 宿主入口：装配状态、路由、监听、窗口。
//!
//! 运行形态与 Python 版一致：**一个真窗口**（WebView2）+ 托盘 + 本地 HTTP 服务。
//! - 想跑无窗口（服务器/CI）→ 设 `NEXUS_NO_WINDOW=1`，届时改用浏览器打开界面

#![allow(dead_code)]
// release 版是 GUI 程序：不弹控制台窗口（日志照样写 var/logs/host.log）
#![cfg_attr(all(windows, not(debug_assertions)), windows_subsystem = "windows")]

mod app_icon;
mod autostart;
mod bridge;
mod dlna_service;
mod extras;
mod lan;
mod logging;
mod models;
mod paths;
mod proc_util;
mod routes;
mod server;
mod settings;
mod state;
mod subtitle_http;
mod subtitle_service;
mod sync;
mod sysinfo;
mod tray;
mod update;
mod v2;
mod vision_service;

use std::sync::Arc;

/// 版本号取 `version.json` 的发布号（安装包与 UI 一致），不要再读 Cargo 的 0.1.0。
fn release_version() -> String {
    let name = std::fs::read_to_string(crate::paths::app_dir().join("version.json"))
        .ok()
        .and_then(|t| serde_json::from_str::<serde_json::Value>(&t).ok())
        .and_then(|v| v.get("versionName").and_then(|x| x.as_str()).map(str::to_string))
        .unwrap_or_else(|| env!("CARGO_PKG_VERSION").to_string());
    name
}

fn main() -> anyhow::Result<()> {
    // 视觉运行库环境变量必须先于一切线程设置（见 vision_service::init_env）：
    // sysinfo 的 nvidia-smi、GPU 探测线程都会 spawn 子进程读环境块
    crate::vision_service::init_env();
    let addr = std::env::var("NEXUS_BIND").unwrap_or_else(|_| "127.0.0.1:8890".into());
    let port: u16 = addr
        .rsplit(':')
        .next()
        .and_then(|p| p.parse().ok())
        .unwrap_or(8890);
    let no_window = std::env::var("NEXUS_NO_WINDOW").is_ok();

    // 服务跑在后台线程的 tokio 运行时里；主线程留给窗口事件循环
    // （Windows 的窗口消息泵必须在主线程）。
    let (tx, rx) = std::sync::mpsc::channel::<u16>();
    let addr2 = addr.clone();
    let server_thread = std::thread::Builder::new()
        .name("host-server".into())
        .spawn(move || -> anyhow::Result<()> {
            let rt = tokio::runtime::Builder::new_multi_thread()
                .enable_all()
                .build()?;
            let r = rt.block_on(async move { run_server(addr2, port, tx).await });
            // 绑定失败等原因必须落日志：recv Err 只知道线程结束了，不知道为什么
            if let Err(e) = &r {
                tracing::error!("[nexus-host] 服务线程异常退出：{e:#}");
            }
            r
        })?;

    // 等监听真正就绪再开窗口，避免窗口先加载到一个空白页
    if rx.recv().is_err() {
        let _ = server_thread.join();
        return Ok(());
    }
    let url = format!("http://127.0.0.1:{port}");

    if no_window {
        // 无窗口模式：顺带开浏览器，方便在服务器上调试
        let u = url.clone();
        std::thread::spawn(move || {
            std::thread::sleep(std::time::Duration::from_millis(600));
            let _ = open::that(&u);
        });
        let _ = server_thread.join();
        return Ok(());
    }

    run_window(&url);
    // 窗口关闭 → 进程退出（服务线程随进程结束）
    Ok(())
}

/// 真窗口（WebView2，无系统标题栏——页面自带标题栏，与 Python 版一致）。
///
/// 建不出来就退回浏览器，不让用户"什么都看不到"。
#[cfg(windows)]
fn run_window(url: &str) {
    use tao::event::{Event, WindowEvent};
    use tao::event_loop::{ControlFlow, EventLoop};
    use tao::window::WindowBuilder;
    use wry::WebViewBuilder;

    /// 窗口控制命令入口：tao 的 Window 不能 clone，且 wry 的 IPC 回调要求
    /// 'static + Send，所以回调只投递命令，真正的窗口操作在事件循环线程执行。

    struct Ctl;
    impl bridge::WindowCtl for Ctl {
        fn minimize(&self) {
            WINDOW_CMDS.lock().unwrap().push(WinCmd::Minimize);
        }
        fn toggle_maximize(&self) {
            WINDOW_CMDS.lock().unwrap().push(WinCmd::ToggleMax);
        }
        fn set_fullscreen(&self, on: bool) {
            WINDOW_CMDS.lock().unwrap().push(WinCmd::Fullscreen(on));
        }
        fn start_drag(&self) {
            // 无边框窗口没有系统拖动区：交给事件循环调用 tao 的 drag_window()，
            // 它会在 UI 线程 ReleaseCapture 后发 WM_NCLBUTTONDOWN(HTCAPTION)。
            // 在 IPC 线程直接调 ReleaseCapture 无效（捕获属于 WebView2 的线程）。
            post_window_cmd(WinCmd::Drag);
        }
    }

    let event_loop = EventLoop::new();
    // start_minimized：一开始就不显示（而不是建好再藏，避免启动闪一下；
    // Python 版是在 loaded 事件里 window.hide()，Rust 直接从源头隐藏）。
    let start_minimized = setting_bool("start_minimized", false);
    let window_icon = app_icon::rgba()
        .and_then(|(rgba, w, h)| tao::window::Icon::from_rgba(rgba, w, h).ok());
    let window = match WindowBuilder::new()
        .with_title("FunScriptCast-Nexus")
        .with_decorations(false)
        .with_visible(!start_minimized)
        // 窗口比例由“视频播放区固定 16:9 + 固定侧栏/标题栏/控制区”反推：
        // 1280 宽时视频区约 980×551，窗口 1280×1056（约 1.212:1）。
        .with_inner_size(tao::dpi::LogicalSize::new(1280.0, 1056.0))
        .with_min_inner_size(tao::dpi::LogicalSize::new(1024.0, 845.0))
        .with_window_icon(window_icon)
        .build(&event_loop)
    {
        Ok(w) => w,
        Err(e) => {
            tracing::error!("建窗口失败：{e}（改用浏览器打开）");
            let _ = open::that(url);
            // run_window 返回后 main 就退出、服务线程随进程死掉——浏览器打开的
            // 页面什么都连不上。改为保持进程存活（服务继续跑），Ctrl+C 退出。
            wait_without_window();
            return;
        }
    };

    let webview = WebViewBuilder::new()
        .with_url(url)
        .with_initialization_script(bridge::SHIM)
        .with_ipc_handler(move |req| {
            let body = req.body().to_string();
            let ctl = Ctl;
            // 对话框是阻塞调用：不能卡住窗口线程
            // 排障用：IPC 每次都记一行（按字符截断——按字节切会把中文路径切在
            // UTF-8 字符中间直接 panic，而且 tracing 不进 host.log，炸了也无痕）。
            // 无限循环类问题只能靠"谁在反复调用"这条线索定位。
            let brief: String = body.chars().take(120).collect();
            tracing::info!("IPC {brief}");
            std::thread::spawn(move || {
                let (resp, close) = bridge::handle(&body, &ctl);
                if let Some(v) = resp {
                    let id = serde_json::from_str::<serde_json::Value>(&body)
                        .ok()
                        .and_then(|x| x.get("id").and_then(|i| i.as_u64()))
                        .unwrap_or(0);
                    IPC_RESULTS.lock().unwrap().push((id, v));
                }
                if close {
                    tracing::info!("界面请求关闭窗口");
                    // 不能直接 process::exit：close_to_tray 为真时要藏到托盘，
                    // 由事件循环（拥有 tao::Window 的线程）决定隐藏还是退出。
                    post_window_cmd(WinCmd::RequestClose);
                }
            });
        })
        .build(&window);

    match webview {
        Ok(v) => {
            tracing::info!("窗口已就绪：{url}");
            // 视频全屏期间禁止比例修正，否则会把原生全屏窗口缩回比例尺寸。
            let mut native_fullscreen = false;
            event_loop.run(move |event, _, control_flow| {
                // 100ms 轮询：后台线程算好的结果要能被及时送回页面
                // （ControlFlow::Wait 在没有窗口事件时会一直睡着）
                *control_flow = ControlFlow::WaitUntil(
                    std::time::Instant::now() + std::time::Duration::from_millis(100),
                );
                // 窗口比例锁定（全屏/最大化除外）：由视频区 16:9 反推的固定比例。
                // 拖动窗口边缘时以宽度为准修正高度。
                if let Event::WindowEvent {
                    event: WindowEvent::Resized(size),
                    ..
                } = &event
                {
                    if size.width > 0
                        && !native_fullscreen
                        && !window.is_maximized()
                        && window.fullscreen().is_none()
                    {
                        const ASPECT: f64 = 1280.0 / 1056.0;
                        let want = (size.width as f64 / ASPECT).round() as u32;
                        if (size.height as i64 - want as i64).abs() > 2 {
                            let scale = window.scale_factor();
                            let _ = window.set_inner_size(tao::dpi::LogicalSize::new(
                                size.width as f64 / scale,
                                want as f64 / scale,
                            ));
                        }
                    }
                }
                // 先执行窗口命令（必须在拥有 Window 的线程上）
                let cmds: Vec<WinCmd> = WINDOW_CMDS.lock().unwrap().drain(..).collect();
                for c in cmds {
                    match c {
                        WinCmd::Minimize => window.set_minimized(true),
                        WinCmd::ToggleMax => window.set_maximized(!window.is_maximized()),
                        WinCmd::Fullscreen(on) => {
                            native_fullscreen = on;
                            window.set_fullscreen(if on {
                                Some(tao::window::Fullscreen::Borderless(None))
                            } else {
                                None
                            });
                        }
                        WinCmd::Drag => {
                            let _ = window.drag_window();
                        }
                        WinCmd::Show => {
                            // 托盘双击/菜单：从隐藏或最小化恢复（Windows 上只
                            // set_focus 不会把隐藏窗口拉出来，必须先 set_visible）
                            window.set_visible(true);
                            window.set_minimized(false);
                            window.set_focus();
                        }
                        WinCmd::RequestClose => {
                            if hide_on_close() {
                                tracing::info!("关闭到托盘");
                                window.set_visible(false);
                            } else {
                                tracing::info!("窗口关闭 → 退出");
                                // 先藏窗再清理：停字幕子进程可能要一两秒，别让界面像卡死
                                window.set_visible(false);
                                if let Some(st) = APP_STATE.get() {
                                    shutdown_services(st);
                                }
                                *control_flow = ControlFlow::Exit;
                            }
                        }
                    }
                }
                let pending: Vec<(u64, serde_json::Value)> =
                    IPC_RESULTS.lock().unwrap().drain(..).collect();
                for (id, payload) in pending {
                    let js = format!(
                        "window.__nexusResolve({}, {})",
                        id,
                        serde_json::to_string(&payload).unwrap_or_else(|_| "null".into())
                    );
                    let _ = v.evaluate_script(&js);
                }
                if let Event::WindowEvent {
                    event: WindowEvent::CloseRequested,
                    ..
                } = event
                {
                    // 与 Python `on_closing` 一致：**现读** close_to_tray（设置页刚改的要生效）；
                    // 托盘不可用时不能藏——否则窗口没入口再出来。
                    if hide_on_close() {
                        tracing::info!("Alt+F4/关闭按钮 → 隐藏到托盘");
                        window.set_visible(false);
                    } else {
                        tracing::info!("窗口关闭 → 退出");
                        window.set_visible(false);
                        if let Some(st) = APP_STATE.get() {
                            shutdown_services(st);
                        }
                        *control_flow = ControlFlow::Exit;
                    }
                }
            });
        }
        Err(e) => {
            tracing::error!("建 WebView2 失败：{e}（改用浏览器打开）");
            let _ = open::that(url);
            // 同建窗失败：保持进程存活（服务线程继续跑），Ctrl+C 退出
            wait_without_window();
        }
    }
}

/// 无窗口待机：浏览器模式的回退路径。服务线程在另一个 std 线程上，
/// main 返回即全灭——在这里等 Ctrl+C，让"改用浏览器打开"真正可用。
fn wait_without_window() {
    let rt = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("建不出待机 runtime");
    rt.block_on(async {
        let _ = tokio::signal::ctrl_c().await;
    });
    tracing::info!("收到退出信号，无窗口模式结束");
}

/// 窗口命令：真正的执行交给事件循环（那里才拥有 tao::Window）。
/// 直接用 Win32 ShowWindow 对**无边框**窗口无效（SW_MAXIMIZE 会被忽略），
/// 必须让 tao 自己去算显示器工作区。
#[derive(Debug, Clone, Copy)]
pub enum WinCmd {
    Minimize,
    ToggleMax,
    Fullscreen(bool),
    /// 按住标题栏拖动（必须在拥有 Window 的事件循环线程执行）
    Drag,
    /// 从托盘恢复窗口（双击/菜单「显示」）
    Show,
    /// 请求关闭（自绘标题栏的 win_close；按 close_to_tray 决定隐藏还是退出）
    RequestClose,
}

/// 待执行的窗口命令（后台线程 → 事件循环）。
static WINDOW_CMDS: std::sync::Mutex<Vec<WinCmd>> = std::sync::Mutex::new(Vec::new());

/// 全局宿主状态：窗口事件循环与托盘线程要现读设置、做退出清理。
static APP_STATE: std::sync::OnceLock<Arc<state::AppState>> = std::sync::OnceLock::new();
/// 服务线程的 tokio 句柄：同步线程（窗口/托盘）退出前停字幕子进程用。
static RT_HANDLE: std::sync::OnceLock<tokio::runtime::Handle> = std::sync::OnceLock::new();

/// 现读一个布尔设置（窗口事件循环里**不能**用启动时的旧值）。
pub fn setting_bool(path: &str, default: bool) -> bool {
    APP_STATE
        .get()
        .and_then(|st| st.settings.get_bool(path))
        .unwrap_or(default)
}

/// 全局宿主状态（未初始化时 None）：托盘/更新退出等后台路径做清理用。
pub fn app_state() -> Option<Arc<state::AppState>> {
    APP_STATE.get().cloned()
}

/// 后台线程投递窗口命令。
pub fn post_window_cmd(cmd: WinCmd) {
    WINDOW_CMDS.lock().unwrap().push(cmd);
}

/// 关闭窗口时是否藏到托盘（Python `on_closing`：现读设置 + 托盘可用）。
///
/// 托盘没建起来时绝不能藏：窗口没有入口再回来（任务栏也不显示隐藏窗口）。
fn hide_on_close() -> bool {
    setting_bool("close_to_tray", true) && tray::is_active()
}

/// 退出清理（同步上下文：窗口事件循环 / 托盘线程）。
///
/// 对应 Python `request_quit` 的 `_device_cleanup / sub_stop / dlna_stop`：
/// 先停四个运动会话，再停字幕常驻进程（audiocpp 约 3GB，不能留成孤儿），最后停 DLNA。
pub fn shutdown_services(st: &Arc<state::AppState>) {
    st.arb.stop_all();
    let _ = st.dlna.stop();
    if let Some(h) = RT_HANDLE.get() {
        let st = Arc::clone(st);
        let _ = h.block_on(async move {
            stop_subtitle_bounded(&st).await;
            st.arb.ch.cleanup_async().await;
        });
    }
}

/// 退出清理（异步上下文：`/api/quit` handler 已经在 tokio 里，不能嵌套 block_on）。
pub async fn shutdown_services_async(st: &Arc<state::AppState>) {
    st.arb.stop_all();
    let _ = st.dlna.stop();
    stop_subtitle_bounded(st).await;
    st.arb.ch.cleanup_async().await;
}

/// 停字幕子进程（最多等 5s）：退出流程不能因为字幕锁/进程回收卡住不退。
async fn stop_subtitle_bounded(st: &Arc<state::AppState>) {
    let stop = async {
        let parts = {
            let svc = st.subtitle.lock().await;
            svc.start_parts()
        };
        crate::subtitle_service::SubtitleService::run_stop(&parts).await;
    };
    let _ = tokio::time::timeout(std::time::Duration::from_secs(5), stop).await;
}

/// IPC 结果队列（后台线程 → 事件循环 → 页面）。
static IPC_RESULTS: std::sync::Mutex<Vec<(u64, serde_json::Value)>> =
    std::sync::Mutex::new(Vec::new());

#[cfg(not(windows))]
fn run_window(url: &str) {
    let _ = open::that(url);
}

async fn run_server(
    addr: String,
    port: u16,
    ready: std::sync::mpsc::Sender<u16>,
) -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info,tower_http=warn".into()),
        )
        .init();

    // 系统指标后台刷新：handler 只读缓存，绝不在请求路径上跑 nvidia-smi
    crate::sysinfo::start_refresher();

    let version = release_version();
    let st = state::AppState::new(&version, port);
    // 窗口事件循环/托盘线程要读设置（close_to_tray / start_minimized）与做退出清理。
    // 必须在 `ready.send` 之前注册：主线程收到就绪信号就会立刻建窗口。
    let _ = APP_STATE.set(Arc::clone(&st));
    let _ = RT_HANDLE.set(tokio::runtime::Handle::current());
    st.logs.info(format!("nexus-host 启动，监听 {addr}"));

    // 字幕状态后台刷新：/api/state 每秒轮询，探测绝不能在请求路径上做
    tokio::spawn(crate::routes::subtitle_refresher(std::sync::Arc::clone(&st)));

    // 头显字幕服务：8756，头显零改动的硬契约（/health、/transcribe、/transcribe/stream）。
    // 端口被旧 Python 实例占用时只记日志，不阻塞宿主启动。
    let subtitle_port = {
        let svc = st.subtitle.lock().await;
        svc.cfg.server.port
    };
    if subtitle_port != 0 {
        tokio::spawn(crate::subtitle_http::serve(
            std::sync::Arc::clone(&st),
            subtitle_port,
        ));
    }

    // dlna_auto_start：根目录配好就自动起（与 Python 版一致）
    if st.settings.get_bool("dlna_auto_start").unwrap_or(true) {
        let roots: Vec<String> = st
            .settings
            .get_path("dlna_roots")
            .and_then(|v| v.as_array().cloned())
            .map(|a| {
                a.iter()
                    .filter_map(|x| x.as_str().map(|s| s.to_string()))
                    .collect()
            })
            .unwrap_or_default();
        if !roots.is_empty() {
            // 端口范围校验：as u16 静默截断会把 70000 起在 4464
            let raw = st.settings.get_i64("dlna_port").unwrap_or(8899);
            let dport = if (1..=65535).contains(&raw) { raw as u16 } else { 8899 };
            match st.dlna.start(dport, roots).await {
                Ok(url) => st.logs.info(format!("DLNA 自动启动：{url}")),
                Err(e) => st.logs.error(format!("DLNA 自动启动失败：{e}")),
            }
        }
    }

    let app = server::build(Arc::clone(&st));
    let url_for_ui = format!("http://127.0.0.1:{port}");
    // 端口已被占用 = 已经有一份在跑：交给主线程开窗口（指向已有实例），本线程收工
    let listener = match tokio::net::TcpListener::bind(&addr).await {
        Ok(l) => l,
        Err(e) if e.kind() == std::io::ErrorKind::AddrInUse => {
            st.logs
                .info(format!("已有实例在跑，直接打开界面：{url_for_ui}"));
            let _ = ready.send(port);
            return Ok(());
        }
        Err(e) => return Err(e.into()),
    };
    let _ = ready.send(port);

    // subtitle_auto_start：服务就绪后按设置后台拉起（Python `host_server.py:5173-5177`）。
    // 必须放后台：冷启动最长 60/120s，绝不能阻塞监听/开窗。
    if st.settings.get_bool("subtitle_auto_start").unwrap_or(false) {
        st.logs.info("字幕自动启动：按设置后台拉起");
        let st_sub = Arc::clone(&st);
        tokio::spawn(async move {
            let r = crate::routes::start_subtitle(&st_sub).await;
            let chunk = r
                .get("recommended_chunk_sec")
                .and_then(|v| v.as_u64())
                .unwrap_or(0);
            st_sub
                .logs
                .info(format!("字幕自动启动已提交（建议分块 {chunk}s）"));
        });
    }

    // 托盘：显示 / 退出。托盘起不来不影响宿主（无桌面会话的环境照样能跑）。
    let tray = tray::start("FunScriptCast-Nexus", "FunScriptCast-Nexus");
    if let Some((rx, handle)) = tray {
        st.logs.info("托盘已就绪（双击/菜单=显示，右键=退出）");
        let logs = Arc::clone(&st.logs);
        let st2 = Arc::clone(&st);
        let url2 = url_for_ui.clone();
        tokio::task::spawn_blocking(move || {
            for ev in rx {
                match ev {
                    tray::TrayEvent::Show => {
                        logs.info("托盘：显示界面");
                        if std::env::var("NEXUS_NO_WINDOW").is_ok() {
                            let _ = open::that(&url2);
                        } else {
                            // 有窗口模式：交给窗口事件循环恢复（隐藏/最小化都能拉出来）
                            post_window_cmd(WinCmd::Show);
                        }
                    }
                    tray::TrayEvent::Quit => {
                        logs.info("托盘：退出");
                        // 把会话/字幕子进程/DLNA 收干净再退（Python `request_quit` 同义）。
                        // 当前在 tokio 阻塞线程里，不能直接 block_on：另起系统线程做同步清理。
                        let st3 = Arc::clone(&st2);
                        std::thread::spawn(move || {
                            shutdown_services(&st3);
                            std::process::exit(0);
                        });
                        break;
                    }
                }
            }
            handle.stop();
        });
    } else {
        st.logs.warn("托盘未启动（无桌面会话或图标加载失败）");
    }

    // 头显局域网接口：白名单四路由，绑 0.0.0.0。起不来只记警告，不影响主服务。
    {
        let st_lan = Arc::clone(&st);
        let lp = lan::lan_port();
        tokio::spawn(async move {
            if let Err(e) = lan::serve(st_lan, lp).await {
                tracing::warn!("头显接口未启动（{lp}）：{e}");
            }
        });
    }

    axum::serve(listener, app).await?;
    Ok(())
}
