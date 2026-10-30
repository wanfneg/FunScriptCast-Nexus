//! pywebview 兼容桥：给页面补上 `window.pywebview.api`。
//!
//! 前端用 `window.pywebview && window.pywebview.api` 判断"是不是在桌面壳里"，
//! 并调用这些方法。**签名必须与 Python 版逐字一致**（`host_server.NexusApi`），
//! 否则参数会错位。
//!
//! | 方法 | 签名 |
//! |---|---|
//! | `pick_folder` | `(current, multiple)` |
//! | `pick_file` | `(mode, filename, file_types)` |
//! | `win_minimize` / `win_maximize` / `win_close` | `()` |
//!
//! 另加两条**防循环**措施（踩过：界面反复调 pick_folder 会不断弹对话框）：
//! 1. **单飞**：同一时刻只允许一个对话框，重复请求立刻返回错误，不排队。
//! 2. **拖动节流**：`drag_window` 300ms 内只认一次，避免 mousedown 刷屏。
//!
//! 对话框的开/关都写进应用日志（界面「事件流」可见），出问题时不用靠猜。

use std::panic::AssertUnwindSafe;
use std::sync::atomic::{AtomicBool, AtomicI64, Ordering};

use serde_json::{json, Value};

/// 对话框单飞标志：true = 已经有一个在打开。
static DIALOG_OPEN: AtomicBool = AtomicBool::new(false);
/// 对话框结束后的冷却截止时刻（毫秒时间戳）：结束后 800ms 内的新请求直接拒绝。
///
/// 单飞只挡"并发"，挡不住"顺序循环"——对话框关掉的瞬间若立刻又来一个 pick 请求，
/// 单飞已经释放，就会再弹。冷却期把这条通道也堵上。
static DIALOG_COOLDOWN_UNTIL_MS: AtomicI64 = AtomicI64::new(0);
/// 上次拖动的时间戳（毫秒），用于节流。
static LAST_DRAG_MS: AtomicI64 = AtomicI64::new(0);

fn now_ms() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

/// 对话框结束后的冷却时长。
const DIALOG_COOLDOWN_MS: i64 = 800;

fn log_info(msg: &str) {
    crate::logging::global_info(msg);
}
fn log_warn(msg: &str) {
    crate::logging::global_warn(msg);
}

/// 注入到每个页面的垫片（在页面脚本之前执行）。
pub const SHIM: &str = r##"
(function () {
  if (window.pywebview) return;
  var pending = {};
  var seq = 0;
  function call(method, args) {
    return new Promise(function (resolve, reject) {
      var id = ++seq;
      pending[id] = { resolve: resolve, reject: reject };
      try {
        window.ipc.postMessage(JSON.stringify({ id: id, method: method, args: args || [] }));
      } catch (e) { delete pending[id]; reject(String(e)); }
    });
  }
  window.__nexusResolve = function (id, payload) {
    var p = pending[id];
    if (!p) return;
    delete pending[id];
    p.resolve(payload);
  };
  window.pywebview = {
    api: {
      pick_folder:  function (current, multiple) { return call('pick_folder', [current || '', !!multiple]); },
      pick_file:    function (mode, filename, file_types) { return call('pick_file', [mode || 'open', filename || '', file_types || []]); },
      win_minimize:  function () { return call('win_minimize', []); },
      win_maximize:  function () { return call('win_maximize', []); },
      win_fullscreen:function (on) { return call('win_fullscreen', [!!on]); },
      win_close:     function () { return call('win_close', []); },
      drag_window:  function () { return call('drag_window', []); }
    }
  };
  document.addEventListener('mousedown', function (ev) {
    var el = ev.target;
    if (!el || !el.closest) return;
    if (ev.button !== 0) return;
    if (!el.closest('.titlebar')) return;
    if (el.closest('button') || el.closest('.pills') || el.closest('input')
        || el.closest('select') || el.closest('a')) return;
    call('drag_window', []);
  }, true);
})();
"##;

fn try_claim_dialog() -> bool {
    if !DIALOG_OPEN.swap(true, Ordering::SeqCst) {
        // 抢到占用，还要过冷却期
        if now_ms() < DIALOG_COOLDOWN_UNTIL_MS.load(Ordering::SeqCst) {
            DIALOG_OPEN.store(false, Ordering::SeqCst);
            return false;
        }
        true
    } else {
        false
    }
}

fn release_dialog() {
    DIALOG_OPEN.store(false, Ordering::SeqCst);
    DIALOG_COOLDOWN_UNTIL_MS.store(now_ms() + DIALOG_COOLDOWN_MS, Ordering::SeqCst);
}

fn dialog_in_cooldown() -> bool {
    now_ms() < DIALOG_COOLDOWN_UNTIL_MS.load(Ordering::SeqCst)
}

#[cfg(windows)]
fn com_init() -> ComGuard {
    unsafe {
        use windows::Win32::System::Com::{CoInitializeEx, COINIT_APARTMENTTHREADED};
        let _ = CoInitializeEx(None, COINIT_APARTMENTTHREADED);
    }
    ComGuard
}

#[cfg(windows)]
struct ComGuard;
#[cfg(windows)]
impl Drop for ComGuard {
    fn drop(&mut self) {
        unsafe {
            use windows::Win32::System::Com::CoUninitialize;
            CoUninitialize();
        }
    }
}
#[cfg(not(windows))]
struct ComGuard;
#[cfg(not(windows))]
fn com_init() -> ComGuard {
    ComGuard
}

/// 处理一条 IPC 请求。返回 (响应 JSON, 是否要求宿主关闭窗口)。
pub fn handle(body: &str, win: &dyn WindowCtl) -> (Option<Value>, bool) {
    let Ok(v) = serde_json::from_str::<Value>(body) else {
        return (None, false);
    };
    let method = v.get("method").and_then(|x| x.as_str()).unwrap_or("");
    let args = v
        .get("args")
        .and_then(|x| x.as_array())
        .cloned()
        .unwrap_or_default();
    let sarg = |i: usize| -> String {
        args.get(i).and_then(|x| x.as_str()).unwrap_or("").to_string()
    };
    let bargs = |i: usize| args.get(i).and_then(|x| x.as_bool()).unwrap_or(false);

    let (resp, close) = match method {
        "pick_folder" | "pick_file" => {
            if !try_claim_dialog() {
                let msg = if dialog_in_cooldown() {
                    "上一个对话框刚结束，请稍候再点"
                } else {
                    "对话框已打开，请先完成或取消它"
                };
                log_warn(&format!("拒绝新的对话框请求：{msg}"));
                return (Some(json!({ "ok": false, "error": msg })), false);
            }
            // 对话框是阻塞调用，内部（COM/rfd）异常不能吞掉单飞标志——
            // 否则标志永久 true，之后所有 pick 都会被拒。冷却点在正常与 panic 路径都要打。
            let result = std::panic::catch_unwind(AssertUnwindSafe(|| {
                let _com = com_init();
                let is_folder = method == "pick_folder";
                let multiple = if is_folder { bargs(1) } else { false };
                let mode = if is_folder { "open".to_string() } else { sarg(0) };
                let initial = if is_folder { sarg(0) } else { String::new() };
                let filename = if is_folder { String::new() } else { sarg(1) };
                // 第三个参数是扩展名列表（如 ["funscript"]）；不传则不过滤。
                let types: Vec<String> = args
                    .get(2)
                    .and_then(|x| x.as_array())
                    .map(|a| {
                        a.iter()
                            .filter_map(|v| v.as_str())
                            .map(|s| s.trim_start_matches('.').to_string())
                            .filter(|s| !s.is_empty())
                            .collect()
                    })
                    .unwrap_or_default();

                log_info(&format!(
                    "打开{}对话框…",
                    if is_folder { "选择文件夹" } else { "选择文件" }
                ));
                let mut d = rfd::FileDialog::new();
                if !is_folder && !types.is_empty() {
                    let exts: Vec<&str> = types.iter().map(String::as_str).collect();
                    d = d.add_filter("支持的文件", &exts);
                }
                if !initial.is_empty() && std::path::Path::new(&initial).is_dir() {
                    d = d.set_directory(&initial);
                }
                if !filename.is_empty() {
                    d = d.set_file_name(&filename);
                }
                let picked: Option<Vec<std::path::PathBuf>> = if is_folder {
                    if multiple {
                        d.pick_folders()
                    } else {
                        d.pick_folder().map(|p| vec![p])
                    }
                } else if mode == "save" {
                    d.save_file().map(|p| vec![p])
                } else {
                    d.pick_file().map(|p| vec![p])
                };
                match picked {
                    Some(paths) if !paths.is_empty() => {
                        let list: Vec<String> = paths
                            .iter()
                            .map(|p| p.to_string_lossy().to_string())
                            .collect();
                        log_info(&format!("已选择：{}", list.join("；")));
                        if is_folder {
                            (json!({ "ok": true, "path": list[0], "paths": list }), false)
                        } else {
                            (json!({ "ok": true, "path": list[0] }), false)
                        }
                    }
                    _ => {
                        log_info("对话框已取消");
                        (json!({ "ok": false, "cancelled": true }), false)
                    }
                }
            }));
            release_dialog();
            match result {
                Ok(pair) => pair,
                Err(_) => {
                    log_warn("对话框内部异常，已恢复");
                    (json!({ "ok": false, "error": "对话框内部错误，请重试" }), false)
                }
            }
        }
        "win_minimize" => {
            win.minimize();
            (json!({"ok": true}), false)
        }
        "win_maximize" => {
            win.toggle_maximize();
            (json!({"ok": true}), false)
        }
        "win_fullscreen" => {
            win.set_fullscreen(bargs(0));
            (json!({"ok": true}), false)
        }
        "win_close" => (json!({"ok": true}), true),
        "drag_window" => {
            let now = now_ms();
            let last = LAST_DRAG_MS.load(Ordering::SeqCst);
            if now - last >= 300 {
                LAST_DRAG_MS.store(now, Ordering::SeqCst);
                win.start_drag();
            }
            (json!({"ok": true}), false)
        }
        other => (
            json!({"ok": false, "error": format!("未知方法：{other}")}),
            false,
        ),
    };
    (Some(resp), close)
}

/// 窗口控制抽象：让 bridge 不直接依赖 tao，方便单测。
pub trait WindowCtl {
    fn minimize(&self);
    fn toggle_maximize(&self);
    fn set_fullscreen(&self, on: bool);
    fn start_drag(&self);
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::AtomicUsize;
    use std::sync::{Mutex, MutexGuard};

    // 对话框单飞与冷却是进程级全局状态，测试默认并行会互相污染。
    static DIALOG_TEST_LOCK: Mutex<()> = Mutex::new(());

    fn lock_dialog_tests() -> MutexGuard<'static, ()> {
        DIALOG_TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner())
    }

    #[derive(Default)]
    struct FakeCtl {
        min: AtomicUsize,
        max: AtomicUsize,
        drag: AtomicUsize,
    }
    impl WindowCtl for FakeCtl {
        fn minimize(&self) {
            self.min.fetch_add(1, Ordering::SeqCst);
        }
        fn toggle_maximize(&self) {
            self.max.fetch_add(1, Ordering::SeqCst);
        }
        fn set_fullscreen(&self, _on: bool) {}
        fn start_drag(&self) {
            self.drag.fetch_add(1, Ordering::SeqCst);
        }
    }

    #[test]
    fn window_methods_reach_the_window() {
        let c = FakeCtl::default();
        let (r, close) = handle(r#"{"id":1,"method":"win_minimize","args":[]}"#, &c);
        assert_eq!(r.unwrap()["ok"], true);
        assert!(!close);
        assert_eq!(c.min.load(Ordering::SeqCst), 1);

        let (_, close) = handle(r#"{"id":2,"method":"win_close","args":[]}"#, &c);
        assert!(close);
    }

    #[test]
    fn drag_is_rate_limited() {
        let c = FakeCtl::default();
        for _ in 0..3 {
            handle(r#"{"id":1,"method":"drag_window","args":[]}"#, &c);
        }
        assert_eq!(c.drag.load(Ordering::SeqCst), 1, "拖动必须节流");
    }

    #[test]
    fn unknown_method_reports_error() {
        let c = FakeCtl::default();
        let (r, close) = handle(r#"{"id":9,"method":"nope","args":[]}"#, &c);
        assert_eq!(r.unwrap()["ok"], false);
        assert!(!close);
    }

    #[test]
    fn malformed_json_does_not_panic() {
        let c = FakeCtl::default();
        let (r, close) = handle("not json", &c);
        assert!(r.is_none());
        assert!(!close);
    }

    /// 垫片暴露的方法名与参数顺序必须与 Python 版一致。
    #[test]
    fn shim_matches_python_signatures() {
        assert!(SHIM.contains("pick_folder:  function (current, multiple)"));
        assert!(SHIM.contains("pick_file:    function (mode, filename, file_types)"));
        assert!(
            !SHIM.contains("save_file"),
            "Python 版没有 save_file，保存走 pick_file('save')"
        );
        assert!(SHIM.contains("window.pywebview"));
    }

    /// 单飞标志：占用后必须拒绝第二个请求（防对话框无限弹）。
    #[test]
    fn dialog_single_flight_guard() {
        let _guard = lock_dialog_tests();
        DIALOG_OPEN.store(true, Ordering::SeqCst);
        DIALOG_COOLDOWN_UNTIL_MS.store(0, Ordering::SeqCst);
        let c = FakeCtl::default();
        let (r, _) = handle(r#"{"id":1,"method":"pick_folder","args":["",false]}"#, &c);
        let v = r.unwrap();
        assert_eq!(v["ok"], false, "已有对话框时必须拒绝");
        assert!(v["error"].as_str().unwrap().contains("对话框已打开"));
        DIALOG_OPEN.store(false, Ordering::SeqCst);
    }

    /// 冷却期：对话框刚结束的 800ms 内，新请求必须被拒绝——
    /// 单飞只挡并发，挡不住"关掉的瞬间立刻再弹"的顺序循环。
    #[test]
    fn dialog_cooldown_rejects_rapid_retry() {
        let _guard = lock_dialog_tests();
        DIALOG_OPEN.store(false, Ordering::SeqCst);
        DIALOG_COOLDOWN_UNTIL_MS.store(i64::MAX / 2, Ordering::SeqCst); // 模拟刚结束
        let c = FakeCtl::default();
        let (r, _) = handle(r#"{"id":1,"method":"pick_folder","args":["",false]}"#, &c);
        let v = r.unwrap();
        assert_eq!(v["ok"], false, "冷却期内必须拒绝");
        assert!(v["error"].as_str().unwrap().contains("稍候"));
        DIALOG_COOLDOWN_UNTIL_MS.store(0, Ordering::SeqCst);
    }
}
