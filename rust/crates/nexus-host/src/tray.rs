//! 系统托盘：显示 / 退出，与 Python `vendor/dlna/tray_icon.py` 同语义。
//!
//! Python 版用纯 Win32（NOTIFYICONDATAW + 自建消息泵）。Rust 侧用 `tray-icon` +
//! `muda`：跨平台、不用自己写消息循环，菜单行为一致（左键双击=显示，右键=菜单）。
//!
//! 托盘必须在**有消息泵的线程**上跑：Windows 要求创建窗口的线程负责派发消息，
//! 所以这里起一个专用线程跑 `tray_icon::TrayIcon` 并保持存活。

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver, Sender};

/// 托盘当前是否可用（创建成功且未停止）。
///
/// 窗口关闭逻辑（`main.rs::hide_on_close`）必须现读它：托盘不可用时把窗口
/// 藏起来等于让用户没有入口再打开（Python `close_to_tray` 的同一道门）。
static ACTIVE: AtomicBool = AtomicBool::new(false);

/// 托盘是否处于可用状态。
pub fn is_active() -> bool {
    ACTIVE.load(Ordering::SeqCst)
}

/// 托盘事件。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TrayEvent {
    /// 显示主界面（打开浏览器到宿主地址）
    Show,
    /// 退出程序
    Quit,
}

pub struct TrayHandle {
    stop_tx: Sender<()>,
    join: Option<std::thread::JoinHandle<()>>,
}

impl TrayHandle {
    /// 停止并等待托盘线程退出。
    pub fn stop(mut self) {
        ACTIVE.store(false, Ordering::SeqCst);
        let _ = self.stop_tx.send(());
        if let Some(j) = self.join.take() {
            let _ = j.join();
        }
    }
}

/// 启动托盘。返回事件接收端与句柄；托盘创建失败时返回 `None`（不阻止宿主启动）。
#[cfg(windows)]
pub fn start(title: &str, tooltip: &str) -> Option<(Receiver<TrayEvent>, TrayHandle)> {
    use muda::{Menu, MenuEvent, MenuItem, PredefinedMenuItem};
    use tray_icon::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};

    let (evt_tx, evt_rx) = mpsc::channel();
    let (stop_tx, stop_rx) = mpsc::channel::<()>();
    let (ready_tx, ready_rx) = mpsc::channel::<bool>();

    let title = title.to_string();
    let tooltip = tooltip.to_string();

    let join = std::thread::Builder::new()
        .name("tray".into())
        .spawn(move || {
            let menu = Menu::new();
            let show = MenuItem::with_id("show", "显示 FunScriptCast-Nexus", true, None);
            let quit = MenuItem::with_id("quit", "退出", true, None);
            let sep = PredefinedMenuItem::separator();
            if menu.append_items(&[&show, &sep, &quit]).is_err() {
                let _ = ready_tx.send(false);
                return;
            }

            let Some(icon) = make_icon() else {
                tracing::warn!("托盘图标生成失败");
                let _ = ready_tx.send(false);
                return;
            };
            let built = TrayIconBuilder::new()
                .with_menu(Box::new(menu))
                // 左键 = 显示窗口（双击/单击都算），右键才弹菜单；
                // 默认左键也会弹菜单，和"双击显示"抢焦点。
                .with_menu_on_left_click(false)
                .with_tooltip(&tooltip)
                .with_title(&title)
                .with_icon(icon)
                .build();

            let tray = match built {
                Ok(t) => t,
                Err(e) => {
                    tracing::warn!("托盘创建失败：{e}");
                    let _ = ready_tx.send(false);
                    return;
                }
            };
            ACTIVE.store(true, Ordering::SeqCst);
            let _ = ready_tx.send(true);
            tracing::info!("托盘已就绪");

            // 事件循环：Windows 消息泵 + 托盘/菜单事件 + 停止信号。
            //
            // ⚠ 必须自己 pump：tray-icon 的隐藏窗口建在本线程，点击/托盘回调都靠
            // 本线程派发消息；只 sleep 轮询的话图标收不到任何鼠标事件
            //（此前只有菜单事件偶尔可用，双击显示完全失效）。
            loop {
                pump_thread_messages();

                while let Ok(ev) = TrayIconEvent::receiver().try_recv() {
                    match ev {
                        // 左键单击/双击 = 显示（右键留给菜单，避免菜单刚弹出又抢焦点）
                        TrayIconEvent::DoubleClick {
                            button: MouseButton::Left,
                            ..
                        }
                        | TrayIconEvent::Click {
                            button: MouseButton::Left,
                            button_state: MouseButtonState::Up,
                            ..
                        } => {
                            let _ = evt_tx.send(TrayEvent::Show);
                        }
                        _ => {}
                    }
                }

                while let Ok(ev) = MenuEvent::receiver().try_recv() {
                    match ev.id().0.as_str() {
                        "show" => {
                            let _ = evt_tx.send(TrayEvent::Show);
                        }
                        "quit" => {
                            let _ = evt_tx.send(TrayEvent::Quit);
                            break;
                        }
                        _ => {}
                    }
                }

                if stop_rx.try_recv().is_ok() {
                    break;
                }
                std::thread::sleep(std::time::Duration::from_millis(40));
            }
            ACTIVE.store(false, Ordering::SeqCst);
            drop(tray);
        })
        .ok()?;

    // 等托盘真正建好（最多 3 秒），失败就当没托盘
    let ok = ready_rx
        .recv_timeout(std::time::Duration::from_secs(3))
        .unwrap_or(false);
    if !ok {
        let _ = stop_tx.send(());
        let _ = join.join();
        return None;
    }
    ACTIVE.store(true, Ordering::SeqCst);
    Some((
        evt_rx,
        TrayHandle {
            stop_tx,
            join: Some(join),
        },
    ))
}

#[cfg(not(windows))]
pub fn start(_title: &str, _tooltip: &str) -> Option<(Receiver<TrayEvent>, TrayHandle)> {
    None
}

/// 派发本线程窗口消息（托盘/菜单的事件都是通过隐藏窗口的消息回调发出的）。
#[cfg(windows)]
fn pump_thread_messages() {
    use windows::Win32::Foundation::HWND;
    use windows::Win32::UI::WindowsAndMessaging::{
        DispatchMessageW, PeekMessageW, TranslateMessage, MSG, PM_REMOVE,
    };
    unsafe {
        let mut msg = MSG::default();
        while PeekMessageW(&mut msg, HWND::default(), 0, 0, PM_REMOVE).as_bool() {
            let _ = TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
    }
}

/// 生成一个 16×16 的占位图标（蓝色圆点）。
///
/// 正式版应换成 `ui-legacy` 里的品牌图标；这里先用确定性的像素，
/// 避免为一个图标把 PNG 解码库拉进来。
#[cfg(windows)]
fn make_icon() -> Option<tray_icon::Icon> {
    // 优先用产品图标（与 exe/窗口一致）；解码失败才退回程序生成的圆点。
    if let Some((rgba, w, h)) = crate::app_icon::rgba() {
        if let Ok(icon) = tray_icon::Icon::from_rgba(rgba, w, h) {
            return Some(icon);
        }
    }
    const N: u32 = 16;
    let mut rgba = Vec::with_capacity((N * N * 4) as usize);
    let c = (N as f32 - 1.0) / 2.0;
    for y in 0..N {
        for x in 0..N {
            let dx = x as f32 - c;
            let dy = y as f32 - c;
            let d = (dx * dx + dy * dy).sqrt();
            // 半径 6.5 实心，边缘 1px 抗锯齿
            let a = ((6.5 - d).clamp(0.0, 1.0) * 255.0) as u8;
            rgba.extend_from_slice(&[58, 130, 246, a]);
        }
    }
    tray_icon::Icon::from_rgba(rgba, N, N).ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn event_enum_is_comparable() {
        assert_eq!(TrayEvent::Show, TrayEvent::Show);
        assert_ne!(TrayEvent::Show, TrayEvent::Quit);
    }

    /// 托盘是 GUI 资源，测试环境里可能没有桌面会话 —— 只验证"不 panic、失败走 None"。
    #[test]
    fn start_never_panics() {
        let r = start("测试", "测试托盘");
        if let Some((_rx, handle)) = r {
            handle.stop();
        }
    }
}
