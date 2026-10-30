//! 补齐前端在用、但之前没接的接口。
//!
//! ⚠ **返回结构必须逐字对齐 Python 版**（`host_server.py`）+ 前端消费点
//! （`ui-legacy/app.js`）。我第一版是照自己想法编的字段名，结果：
//! `/api/library/browse` 回 `items` 而前端读 `dirs`/`videos` ⇒ 媒体目录永远空
//! （用户实测"不能添加媒体目录"），模型下拉也全空。这类错误编译期完全看不出来。
//!
//! 每个函数上方都注明"谁在读它、读哪些字段"。

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use axum::extract::{Query, State};
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::{json, Value};

use crate::paths;
use crate::state::AppState;

type St = State<std::sync::Arc<AppState>>;

fn ok(v: Value) -> Response {
    Json(v).into_response()
}

/// 媒体库认的视频扩展名（与 Python /api/library/browse 的判据同一组）。
const VIDEO_EXTS: &[&str] = &["mp4", "mkv", "wmv", "avi", "mov", "webm", "m2ts", "ts"];

fn is_video(name: &str) -> bool {
    Path::new(name)
        .extension()
        .and_then(|x| x.to_str())
        .map(|e| VIDEO_EXTS.contains(&e.to_ascii_lowercase().as_str()))
        .unwrap_or(false)
}

/// 规整用户输入的路径 —— 逐行对齐 `host_server.py` 的 `norm_path()`。
///
/// **必须做**：用户习惯把带空格的路径连引号一起粘进来（`"D:\my folder"`），
/// 不处理就会把引号**当成文件名的一部分**存下来——那个路径永远不存在，
/// DLNA 列不出任何东西（头显里"目录为空"），界面上却显示"已启用"，
/// 完全给不出线索。顺带处理中文输入法的全角引号与前后空白。
pub fn norm_path(v: &str) -> String {
    let mut s = v.trim().to_string();
    loop {
        let cs: Vec<char> = s.chars().collect();
        if cs.len() < 2 {
            break;
        }
        let first = cs[0];
        let last = cs[cs.len() - 1];
        let quotes = ['"', '\'', '“', '”', '‘', '’'];
        if quotes.contains(&first) && quotes.contains(&last) {
            s = cs[1..cs.len() - 1].iter().collect::<String>().trim().to_string();
        } else {
            break;
        }
    }
    s
}

fn library_roots(st: &AppState) -> Vec<String> {
    st.settings
        .get_path("library_roots")
        .and_then(|v| v.as_array().cloned())
        .map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect())
        .unwrap_or_default()
}

// ---------------------------------------------------------------- 媒体库浏览

/// `GET /api/library/browse?path=`
///
/// **消费点**：`app.js renderBrowse(data)` 读 `data.dirs[{name,path}]` 与
/// `data.videos[{name,path,has_funscript,has_srt,dur,thumb,pos}]`。
/// 返回 `items` 之类别的字段名 = 界面永远空白。
pub async fn library_browse(State(st): St, Query(q): Query<HashMap<String, String>>) -> Response {
    // 目录遍历（尤其网络盘）是同步 IO：放阻塞线程池
    let want = q.get("path").cloned().unwrap_or_default();
    let st2 = std::sync::Arc::clone(&st);
    match tokio::task::spawn_blocking(move || browse_blocking(&st2, want)).await {
        Ok(v) => return ok(v),
        Err(e) => return ok(json!({"ok": false, "error": format!("任务失败：{e}")})),
    }
}

/// 去掉 Windows 长路径前缀（`\\?\` / `\\?\UNC\`）。
///
/// `canonicalize` 会给路径加这个前缀；直接回给前端会显示成 `\\?\E:\...`，
/// 媒体库卡片按路径匹配也会失败（缩略图/进度丢失）。
fn strip_verbatim(p: &Path) -> PathBuf {
    let s = p.to_string_lossy();
    if let Some(rest) = s.strip_prefix(r"\\?\UNC\") {
        PathBuf::from(format!(r"\\{rest}"))
    } else if let Some(rest) = s.strip_prefix(r"\\?\") {
        PathBuf::from(rest)
    } else {
        p.to_path_buf()
    }
}

fn browse_blocking(st: &AppState, want: String) -> Value {
    let roots = library_roots(&st);

    // 空路径 = 根列表（只列真实存在的根）
    if want.is_empty() {
        let dirs: Vec<Value> = roots
            .iter()
            .filter(|r| Path::new(r).is_dir())
            .map(|r| {
                let name = Path::new(r)
                    .file_name()
                    .map(|x| x.to_string_lossy().to_string())
                    .unwrap_or_else(|| r.clone());
                json!({"name": name, "path": r})
            })
            .collect();
        return json!({"ok": true, "path": "", "dirs": dirs, "videos": []});
    }

    // 路径必须在某个根之下（折叠 .. 后判断，防"过校验落别处"）
    let want_real = match Path::new(&want).canonicalize() {
        Ok(p) => p,
        Err(e) => return json!({"ok": false, "error": format!("目录不存在：{e}")}),
    };
    let inside = roots.iter().any(|r| {
        Path::new(r)
            .canonicalize()
            .map(|rr| want_real == rr || want_real.starts_with(&rr))
            .unwrap_or(false)
    });
    if !inside {
        return json!({"ok": false, "error": "路径不在媒体库目录内"});
    }

    let rd = match std::fs::read_dir(strip_verbatim(&want_real)) {
        Ok(x) => x,
        Err(e) => return json!({"ok": false, "error": e.to_string()}),
    };

    let cards = st.library.cards();
    let mut dirs: Vec<Value> = Vec::new();
    let mut videos: Vec<Value> = Vec::new();
    for e in rd.flatten() {
        let name = e.file_name().to_string_lossy().to_string();
        // 与 Python 一致：跳过隐藏项与系统项
        if name.starts_with('.') || name.starts_with('$') {
            continue;
        }
        let p = e.path();
        let path_str = p.to_string_lossy().to_string();
        if p.is_dir() {
            dirs.push(json!({"name": name, "path": path_str}));
            continue;
        }
        if !is_video(&name) {
            continue;
        }
        // 同名 .funscript / .srt 判定（去掉最后一个扩展名）
        let stem = path_str.rsplit_once('.').map(|(a, _)| a).unwrap_or(&path_str);
        let has_funscript = Path::new(&format!("{stem}.funscript")).is_file();
        let has_srt = Path::new(&format!("{stem}.srt")).is_file();
        // 时长/进度/缩略图来自媒体库卡片（没扫过就是 0/空）
        let card = cards.iter().find(|c| c.paths.iter().any(|x| x == &path_str));
        let dur = card.map(|c| c.total_sec).unwrap_or(0.0);
        let pos = card
            .and_then(|c| c.progress.as_ref().map(|p| p.pos))
            .unwrap_or(0.0);
        let thumb = st.library.thumb_for(&path_str);
        videos.push(json!({
            "name": name,
            "path": path_str,
            "has_funscript": has_funscript,
            "has_srt": has_srt,
            "dur": dur,
            "thumb": thumb,
            "pos": pos,
        }));
    }
    dirs.sort_by_key(|v| v["name"].as_str().unwrap_or("").to_lowercase());
    videos.sort_by_key(|v| v["name"].as_str().unwrap_or("").to_lowercase());
    json!({"ok": true, "path": want, "dirs": dirs, "videos": videos})
}

// ---------------------------------------------------------------- 模型列表

/// `GET /api/subtitle/models` —— 本地翻译模型下拉。
///
/// **消费点**：`app.js fillLocalModelSelect` 读 `r.models[{path}]`。
/// `path` 是**相对 vendor/subtitle** 的路径（与 config 既有格式一致）。
pub async fn subtitle_models() -> Response {
    // 遍历 16GB 的 models 目录是同步 IO：进阻塞线程池
    match tokio::task::spawn_blocking(subtitle_models_blocking).await {
        Ok(v) => return ok(v),
        Err(e) => return ok(json!({"ok": false, "error": format!("任务失败：{e}"), "models": []})),
    }
}

fn subtitle_models_blocking() -> Value {
    let models_dir = paths::app_dir().join("models");
    fn walk(dir: &Path, acc: &mut Vec<PathBuf>) {
        if let Ok(rd) = std::fs::read_dir(dir) {
            for e in rd.flatten() {
                let p = e.path();
                if p.is_dir() {
                    walk(&p, acc);
                } else if p.extension().and_then(|x| x.to_str()) == Some("gguf") {
                    acc.push(p);
                }
            }
        }
    }
    let mut files: Vec<PathBuf> = Vec::new();
    walk(&models_dir, &mut files);
    files.sort();
    let mut out: Vec<Value> = Vec::new();
    let mut seen: std::collections::HashSet<String> = std::collections::HashSet::new();
    for p in files {
        let rel = match p.strip_prefix(paths::app_dir()) {
            Ok(r) => r,
            Err(_) => continue,
        };
        let rel_posix = format!("../../{}", rel.to_string_lossy().replace('\\', "/"));
        let size_gb = std::fs::metadata(&p)
            .map(|m| (m.len() as f64 / (1024.0 * 1024.0 * 1024.0) * 10.0).round() / 10.0)
            .unwrap_or(0.0);
        let parent = p.parent().unwrap_or(&models_dir);
        let mut label = if parent == models_dir {
            p.file_stem()
                .map(|x| x.to_string_lossy().to_string())
                .unwrap_or_default()
        } else {
            parent
                .file_name()
                .map(|x| x.to_string_lossy().to_string())
                .unwrap_or_default()
        };
        // 同一目录多个量化：退回文件名（自带量化后缀，可区分）
        if seen.contains(&label) {
            label = p
                .file_stem()
                .map(|x| x.to_string_lossy().to_string())
                .unwrap_or_default();
        }
        seen.insert(label.clone());
        out.push(json!({"name": label, "path": rel_posix, "size_gb": size_gb}));
    }
    json!({"ok": true, "models": out})
}

/// `GET /api/subtitle/asr-models` —— 识别模型下拉。
///
/// **消费点**：`app.js fillAsrModelSelect` 读 `r.models[{name, value}]`。
/// `value` 存进 `asr.audiocpp.model`：`../../models/<目录>`。
pub async fn subtitle_asr_models() -> Response {
    match tokio::task::spawn_blocking(subtitle_asr_models_blocking).await {
        Ok(v) => return ok(v),
        Err(e) => return ok(json!({"ok": false, "error": format!("任务失败：{e}"), "models": []})),
    }
}

fn subtitle_asr_models_blocking() -> Value {
    let mut out: Vec<Value> = Vec::new();
    let models_dir = paths::app_dir().join("models");
    let mut dirs: Vec<PathBuf> = Vec::new();
    if let Ok(rd) = std::fs::read_dir(&models_dir) {
        for e in rd.flatten() {
            let p = e.path();
            if p.is_dir() && !e.file_name().to_string_lossy().starts_with('.') {
                dirs.push(p);
            }
        }
    }
    dirs.sort();
    for d in dirs {
        if !d.join("model.safetensors").is_file() || !d.join("config.json").is_file() {
            continue;
        }
        // 只认 audio.cpp qwen3_asr 的单文件形态
        let mt = std::fs::read_to_string(d.join("config.json"))
            .ok()
            .and_then(|s| serde_json::from_str::<Value>(&s).ok())
            .and_then(|v| v.get("model_type").and_then(|x| x.as_str()).map(String::from))
            .unwrap_or_default();
        if mt != "qwen3_asr" {
            continue;
        }
        let name = d.file_name().unwrap_or_default().to_string_lossy().to_string();
        out.push(json!({"name": name, "value": format!("../../models/{name}")}));
    }
    json!({"ok": true, "models": out})
}

// ---------------------------------------------------------------- 翻译自检

/// `POST /api/subtitle/translate-test` —— UI「测试」按钮。
///
/// **消费点**：`app.js testMt` 读 `ok/backend/ms/text/error`（其余字段可选）。
/// Python 转发到 8756 的 `/translate/selftest`；Rust 版没有那个服务，
/// 直接调本进程的翻译引擎，语义一致。
pub async fn subtitle_translate_test(State(st): St, Json(body): Json<Value>) -> Response {
    let text = body
        .get("text")
        .and_then(|v| v.as_str())
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "こんにちは、今日はいい天気ですね。".to_string());
    // 锁内只取引擎与活动 guard，HTTP 请求在锁外（subtitle 是全局单把锁）
    let (backend, engine, _activity) = {
        let svc = st.subtitle.lock().await;
        (
            svc.engine.backend_name(),
            std::sync::Arc::clone(&svc.engine),
            svc.activity.begin(),
        )
    };
    // 「关闭翻译」不是失败，但"测试"必须如实告诉用户没有发请求
    // （Python `/translate/selftest` 同样把 disabled 报成未通过）
    if engine.backend_kind() == nexus_subtitle::config::BackendKind::None {
        return ok(json!({
            "ok": false, "backend": backend, "ms": 0, "text": text,
            "error": "翻译已关闭（translate.backend = none，不发起任何请求）",
        }));
    }
    let t0 = std::time::Instant::now();
    let res = engine.translate_for(&text, "ja").await;
    let ms = t0.elapsed().as_millis() as u64;
    match res {
        Ok(zh) => ok(json!({
            "ok": true, "backend": backend, "ms": ms,
            "text": text, "pipeline": zh,
        })),
        Err(e) => ok(json!({
            "ok": false, "backend": backend, "ms": ms,
            "text": text, "error": e,
        })),
    }
}

/// `POST /api/subtitle/reclaim` —— 检查并清理残留字幕服务。
///
/// **消费点**：`app.js #subReclaim` 读 `r.nothing / r.ok / r.error / r.killed`。
///
/// Rust 版没有独立的 8756 服务（识别/翻译由本进程调度），真正要查的是
/// audiocpp / llama 两个后端端口：端口有响应但不由本进程管理，就是上次宿主
/// 被强杀留下的残留（它们带着旧配置的模型占显存）。与 Python `sub_reclaim`
/// 同语义：找到残留就 taskkill /T /F 清掉（下面的注释若与行为冲突以行为为准）。
pub async fn subtitle_reclaim(State(st): St) -> Response {
    let parts = {
        let svc = st.subtitle.lock().await;
        svc.status_parts()
    };
    let (asr_up, mt_up) = tokio::join!(parts.asr.probe(), parts.mt.probe());
    let (asr_ours, mt_ours) = tokio::join!(parts.asr.is_running(), parts.mt.is_running());
    let mut foreign: Vec<(&'static str, u16)> = Vec::new();
    if asr_up && !asr_ours {
        foreign.push(("ASR", parts.asr_port));
    }
    if mt_up && !mt_ours {
        let port = port_of(&parts.mt.base_url());
        if port != 0 {
            foreign.push(("翻译", port));
        }
    }
    if foreign.is_empty() {
        return ok(json!({"ok": true, "nothing": true}));
    }

    // 与 Python `sub_reclaim` 同语义：结束残留（taskkill /T 连带子进程），
    // 再按当前配置重启自己那份——否则旧进程会一直占端口、改设置不生效。
    let mut killed: Vec<u32> = Vec::new();
    #[cfg(windows)]
    {
        for (_, port) in &foreign {
            if let Ok(out) = std::process::Command::new("netstat")
                .args(["-ano", "-p", "tcp"])
                .output()
            {
                for pid in parse_netstat_pids(&String::from_utf8_lossy(&out.stdout), *port) {
                    if kill_tree(pid) {
                        killed.push(pid);
                    }
                }
            }
        }
    }
    if killed.is_empty() {
        return ok(json!({
            "ok": false,
            "error": format!(
                "这些字幕后端在跑但不由本程序管理（可能是上次残留），且自动结束失败：{}",
                foreign.iter().map(|(n, p)| format!("{n}（:{p}）")).collect::<Vec<_>>().join("；")
            ),
        }));
    }
    st.logs
        .warn(format!("已结束残留字幕服务：{killed:?}"));
    tokio::time::sleep(std::time::Duration::from_millis(1200)).await;
    let restart = crate::routes::start_subtitle(&st).await;
    ok(json!({"ok": true, "killed": killed, "restart": restart}))
}

fn port_of(url: &str) -> u16 {
    url.rsplit(':')
        .next()
        .and_then(|s| s.trim_end_matches('/').parse().ok())
        .unwrap_or(0)
}

/// 从 `netstat -ano` 输出提取监听指定端口的 PID（只认 LISTENING 行）。
fn parse_netstat_pids(text: &str, port: u16) -> Vec<u32> {
    let mut out = Vec::new();
    for line in text.lines() {
        if !line.to_ascii_uppercase().contains("LISTENING") {
            continue;
        }
        // 按列解析（本地地址是第 2 列）并取**冒号后整段**精确比对端口：
        // contains(":8081") 会把监听 80810-80819 的无关进程一起抓来
        // 交给 taskkill /T /F 强杀（误杀=对方数据丢失）。
        let cols: Vec<&str> = line.split_whitespace().collect();
        let local = cols.get(1).copied().unwrap_or("");
        let actual: u16 = local
            .rsplit(':')
            .next()
            .and_then(|p| p.parse().ok())
            .unwrap_or(0);
        if actual != port {
            continue;
        }
        if let Some(pid) = cols.last().and_then(|s| s.parse().ok()) {
            if !out.contains(&pid) {
                out.push(pid);
            }
        }
    }
    out
}

#[cfg(windows)]
fn kill_tree(pid: u32) -> bool {
    use std::os::windows::process::CommandExt;
    let mut cmd = std::process::Command::new("taskkill");
    cmd.args(["/PID", &pid.to_string(), "/T", "/F"]);
    cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    cmd.status().map(|s| s.success()).unwrap_or(false)
}

/// 兼容旧前端的 `/api/vision/runtime/remove`：Rust 版没有独立 Python 运行时。
pub async fn vision_runtime_remove() -> Response {
    ok(json!({
        "ok": true,
        "removed": false,
        "note": "Rust 版 ONNX 运行时随安装包提供，无独立运行时可删除",
    }))
}

// ---------------------------------------------------------------- 设备设置

/// `POST /api/device/settings` —— 设备段设置合并进 settings.device。
pub async fn device_settings(State(st): St, Json(body): Json<Value>) -> Response {
    let mut dev = st.settings.get_path("device").unwrap_or_else(|| json!({}));
    if !dev.is_object() {
        dev = json!({});
    }
    if let Some(obj) = body.as_object() {
        for (k, v) in obj {
            if v.is_null() {
                continue;
            }
            // 子对象深合并（orgasm/slow 是 {速度: 值} 的字典）
            if let Some(slot) = dev.get_mut(k) {
                if slot.is_object() && v.is_object() {
                    if let (Some(a), Some(b)) = (slot.as_object_mut(), v.as_object()) {
                        for (kk, vv) in b {
                            if !vv.is_null() {
                                a.insert(kk.clone(), vv.clone());
                            }
                        }
                    }
                    continue;
                }
            }
            if let Some(m) = dev.as_object_mut() {
                m.insert(k.clone(), v.clone());
            }
        }
    }
    match st.settings.patch(&json!({"device": dev})) {
        Ok(_) => {
            // 保存即生效：重算通道行程/快捷动作/同步参数（Python `_apply_device_settings()`）
            crate::state::apply_device_settings(&st.settings, &st.arb);
            // 立即下发（对齐 Python `host_server.py:3467-3474`）：
            // 只在请求里**显式**带了开关时发，避免普通保存（如预设速度）也重启设备。
            if let Some(a10) = body.get("a10_mode").and_then(|v| v.as_i64()) {
                let _ = st.arb.ch.set_mode(a10 as i32).await;
            }
            if let Some(oc) = body.get("oc_mode").and_then(|v| v.as_bool()) {
                let _ = st.arb.ch.set_oc_mode(oc).await;
            }
            // 行程/限速可能随保存变化：未连接时 apply_limits 只返回 false，不阻塞
            let _ = st.arb.ch.apply_limits().await;
            ok(json!({"ok": true, "device": dev}))
        }
        Err(e) => ok(json!({"ok": false, "error": e.to_string()})),
    }
}

// ---------------------------------------------------------------- 同步（adb）

/// 找 adb：本安装 → 同盘另一个安装 → PATH。
///
/// ⚠ 路径一律用 `join` 拼，**不写反斜杠字面量**：之前写成带反斜杠的原始字符串，
/// 反斜杠在生成源码时被当成转义序列，路径变成 `D:FunScriptCast-Nexus...`，
/// 表现为永远"找不到 adb"。
pub(crate) fn adb_exe() -> Option<PathBuf> {
    let base = paths::app_dir();
    let mut cands: Vec<PathBuf> = vec![
        base.join("tools").join("adb").join("adb.exe"),
        base.join("..")
            .join("FunScriptCast-Nexus")
            .join("tools")
            .join("adb")
            .join("adb.exe"),
    ];
    if let Ok(p) = std::env::var("PATH") {
        for dir in std::env::split_paths(&p) {
            let c = dir.join("adb.exe");
            if c.is_file() {
                cands.push(c);
            }
        }
    }
    cands.into_iter().find(|p| p.is_file())
}

/// 给 UI 显示的 adb 路径（找不到就是空串）。
pub fn adb_path_for_ui() -> String {
    adb_exe().map(|p| p.to_string_lossy().to_string()).unwrap_or_default()
}

/// adb push 的目标目录以 `/` 结尾才会被当作目录（同步实现见 `sync.rs`）。
fn run_adb(args: &[&str]) -> Result<String, String> {
    use std::process::{Command, Stdio};
    let exe = adb_exe().ok_or_else(|| "找不到 adb：请把 adb 放到 tools/adb/adb.exe".to_string())?;
    let mut cmd = Command::new(&exe);
    cmd.args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    crate::proc_util::hide_console(&mut cmd);
    // std 的 output() 没有超时，adb 卡死会永远占线程（sync.rs:518 同款认知）；
    // 管道必须持续读走，否则 adb 写满缓冲会自己阻塞住（超时反而误杀）
    let mut child = cmd.spawn().map_err(|e| format!("adb 启动失败：{e}"))?;
    let out_h = child
        .stdout
        .take()
        .map(|s| std::thread::spawn(move || read_pipe_lossy(s)));
    let err_h = child
        .stderr
        .take()
        .map(|s| std::thread::spawn(move || read_pipe_lossy(s)));
    let start = std::time::Instant::now();
    let status = loop {
        match child.try_wait() {
            Ok(Some(s)) => break s,
            Ok(None) => {
                if start.elapsed() >= std::time::Duration::from_secs(15) {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err(format!("adb 命令超时（>15s）：{}", args.join(" ")));
                }
                std::thread::sleep(std::time::Duration::from_millis(50));
            }
            Err(e) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(format!("等待 adb 进程失败：{e}"));
            }
        }
    };
    let out = out_h.and_then(|h| h.join().ok()).unwrap_or_default();
    let err = err_h.and_then(|h| h.join().ok()).unwrap_or_default();
    let _ = status;
    Ok(out + &err)
}

fn read_pipe_lossy(mut s: impl std::io::Read + Send + 'static) -> String {
    let mut buf = Vec::new();
    let _ = s.read_to_end(&mut buf);
    String::from_utf8_lossy(&buf).into_owned()
}

/// `POST /api/sync/devices` —— 设备扫描。
///
/// **消费点**：`app.js scanDevices` 读 `r.ok / r.error / r.devices[{serial,model,usb}]`。
pub async fn sync_devices() -> Response {
    // adb 是同步进程调用，可能耗时数秒：放到阻塞线程池，别占 tokio worker
    match tokio::task::spawn_blocking(sync_devices_blocking).await {
        Ok(v) => ok(v),
        Err(e) => ok(json!({"ok": false, "error": format!("任务失败：{e}"), "devices": []})),
    }
}

fn sync_devices_blocking() -> Value {
    let adb_path = adb_exe()
        .map(|p| p.to_string_lossy().to_string())
        .unwrap_or_default();
    let text = match run_adb(&["devices"]) {
        Ok(t) => t,
        Err(e) => {
            return json!({"ok": false, "error": e, "adb": adb_path, "devices": []});
        }
    };
    let mut list = Vec::new();
    for line in text.lines().skip(1) {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let mut it = line.split_whitespace();
        let serial = it.next().unwrap_or("");
        let state = it.next().unwrap_or("");
        if serial.is_empty() || state != "device" {
            continue;
        }
        // 型号 best-effort（失败不影响列出设备）
        let model = run_adb(&["-s", serial, "shell", "getprop", "ro.product.model"])
            .map(|s| s.trim().to_string())
            .unwrap_or_default();
        list.push(json!({
            "serial": serial,
            "model": model,
            // 没有冒号 = USB，有冒号 = 无线（与 Python 版同判据）
            "usb": !serial.contains(':'),
        }));
    }
    json!({"ok": true, "adb": adb_path, "devices": list})
}

/// `POST /api/sync/connect` —— 连接指定设备。
/// `POST /api/sync/connect` —— 连接指定设备。
pub async fn sync_connect(State(st): St, Json(body): Json<Value>) -> Response {
    let serial = body
        .get("serial")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim()
        .to_string();
    if serial.is_empty() {
        return ok(json!({"ok": false, "error": "未选择设备"}));
    }
    // adb 是同步进程调用：进阻塞线程池，别占 tokio worker
    let s2 = serial.clone();
    match tokio::task::spawn_blocking(move || adb_connect_blocking(&s2)).await {
        Ok(v) if v.get("ok").and_then(|x| x.as_bool()).unwrap_or(false) => {
            // connected 由 sync_serial 判定：连上必须落盘，否则 UI 永远显示未连接
            if let Err(e) = st.settings.patch(&json!({"sync_serial": serial})) {
                return ok(json!({
                    "ok": false, "serial": serial,
                    "error": format!("保存设备序列号失败：{e}")
                }));
            }
            // 先开 AI 后连设备：补一次 live，别等用户重开视频
            let video = st.vision.current_video().await;
            let estate = st.vision.status().await["engine_state"]
                .as_str()
                .unwrap_or("idle")
                .to_string();
            if !video.as_os_str().is_empty()
                && matches!(estate.as_str(), "running" | "done")
            {
                st.arb.start_live(&video.to_string_lossy());
            }
            ok(v)
        }
        Ok(v) => {
            // 连接失败必须清掉残留 serial（Python R125 教训：不清的话
            // /api/sync/run 会拿旧 serial 把文件推到非预期设备）
            let _ = st.settings.patch(&json!({"sync_serial": ""}));
            ok(v)
        }
        Err(e) => ok(json!({"ok": false, "serial": serial, "error": format!("任务失败：{e}")})),
    }
}

fn adb_connect_blocking(serial: &str) -> Value {
    // 严格判定（对齐 sync.rs：输出恰为 "device"）：
    // contains("device") 会把 "error: device 'X' not found" 误判成已连接
    match run_adb(&["-s", serial, "get-state"]) {
        Ok(t) => {
            let state = t.trim();
            if state == "device" {
                json!({"ok": true, "serial": serial})
            } else {
                json!({"ok": false, "serial": serial, "error": state})
            }
        }
        Err(e) => json!({"ok": false, "serial": serial, "error": e}),
    }
}

/// `POST /api/sync/disconnect` —— 断开无线设备。
pub async fn sync_disconnect(State(st): St) -> Response {
    match tokio::task::spawn_blocking(|| match run_adb(&["disconnect"]) {
        Ok(t) => json!({"ok": true, "output": t.trim()}),
        Err(e) => json!({"ok": false, "error": e}),
    })
    .await
    {
        Ok(v) => {
            if v.get("ok").and_then(|x| x.as_bool()).unwrap_or(false) {
                let _ = st.settings.patch(&json!({"sync_serial": ""}));
            }
            ok(v)
        }
        Err(e) => ok(json!({"ok": false, "error": format!("任务失败：{e}")})),
    }
}

/// `POST /api/sync/run` —— 跑一次同步。
///
/// **消费点**：`app.js runSync` 只判 `r.ok`/`r.error`，成功后会 poll `/api/state`
/// 看 `sync.slots.{script,video}` 的 `busy / result{local,device,pushed} / error / logs`。
///
/// adb push 可能要几分钟：这里只做**同步的入参校验与槽位占位**，真正的扫描/推送
/// 交给 `spawn_blocking` 后台执行并立即返回 `{ok:true, busy:true}`。前端 15 秒
/// fetch 超时只会 abort 这次 HTTP 请求，不会中断后台同步任务。
/// 目录不存在/设备未连接/设备目录不安全时直接回明确中文错误（不启动任务、不误删）。
pub async fn sync_run(State(st): St, Json(body): Json<Value>) -> Response {
    let kind_key = body.get("kind").and_then(|v| v.as_str()).unwrap_or("script");
    let Some(kind) = crate::sync::SyncKind::from_key(kind_key) else {
        return ok(json!({"ok": false, "error": "未知的同步类型"}));
    };
    // 键名必须与 UI 一致：设置页/同步页写的是 script_folder / video_folder
    let raw_path = st.settings.get_str(kind.local_key()).unwrap_or_default();
    let local_path = norm_path(&raw_path);
    if local_path.trim().is_empty() {
        return ok(json!({
            "ok": false,
            "error": format!("还没设置本地{}路径", kind.short_label())
        }));
    }
    let local = PathBuf::from(&local_path);
    if !local.is_dir() {
        return ok(json!({
            "ok": false,
            "error": format!("本地{}目录不存在：{}", kind.short_label(), local.display())
        }));
    }
    let serial = st.settings.get_str("sync_serial").unwrap_or_default();
    if serial.trim().is_empty() {
        return ok(json!({"ok": false, "error": "请先连接设备"}));
    }
    // 空设置回退到 Python Config 的默认目录；带 .. 的路径先归一化，再做专用子目录护栏
    let raw_device = st.settings.get_str(kind.device_key()).unwrap_or_default();
    let device_folder = match crate::sync::normalize_device_folder(&raw_device, kind) {
        Ok(p) => p,
        Err(e) => return ok(json!({"ok": false, "error": e})),
    };
    let adb = match adb_exe() {
        Some(p) => p,
        None => {
            return ok(json!({
                "ok": false,
                "error": "找不到 adb：请把 adb 放到 tools/adb/adb.exe"
            }))
        }
    };

    let opts = crate::sync::SyncOptions {
        kind,
        local_folder: local,
        device_folder: device_folder.clone(),
        serial: serial.clone(),
        force_full: st.settings.get_bool("sync_force_full").unwrap_or(false),
        delete_extra: st.settings.get_bool("sync_delete_extra").unwrap_or(false),
    };
    if let Err(e) = st.sync.begin(kind, &serial) {
        return ok(json!({"ok": false, "error": e}));
    }
    st.logs.info(format!(
        "开始同步{} → {}（{} → {}/）",
        kind.label(),
        serial,
        opts.local_folder.display(),
        device_folder
    ));

    // 长任务：spawn_blocking 在后台线程池跑，不占 tokio worker；请求返回后照常继续
    let svc = std::sync::Arc::clone(&st.sync);
    let logs = std::sync::Arc::clone(&st.logs);
    tokio::task::spawn_blocking(move || {
        crate::sync::run_job(&svc, &logs, opts, adb);
    });

    ok(json!({
        "ok": true,
        "busy": true,
        // 兼容旧响应字段（前端不读，排障时有用）
        "target": format!("{device_folder}/"),
        "output": format!("已开始同步{}", kind.short_label()),
    }))
}


// ---------------------------------------------------------------- JS 错误上报

/// `POST /api/js-error` —— 前端未捕获异常。
///
/// 打包版是 WebView2 无控制台，前端异常此前完全不可见：一个元素缺失导致
/// bind() 中途抛错，后面的按钮全部静默失效，用户只看到"点了没反应"。
pub async fn js_error(State(st): St, Json(body): Json<Value>) -> Response {
    let kind = body.get("kind").and_then(|v| v.as_str()).unwrap_or("error");
    let msg = body.get("message").and_then(|v| v.as_str()).unwrap_or("");
    let detail = body.get("detail").and_then(|v| v.as_str()).unwrap_or("");
    let url = body.get("url").and_then(|v| v.as_str()).unwrap_or("");
    st.logs.error(format!("前端{kind}：{msg}（{url}）"));
    if !detail.is_empty() {
        st.logs.error(format!("  详情：{}", detail.replace('\n', " | ")));
    }
    ok(json!({"ok": true}))
}

/// `POST /api/vision/runtime/install` —— 视觉运行时。
///
/// Rust 版随安装包分发 `runtime/ort/`，无需下载；如实回报，不假装"正在安装"。
pub async fn vision_runtime_install(State(st): St) -> Response {
    match crate::vision_service::resolve_ort_dylib() {
        Some(p) => {
            st.logs.info("视觉运行时已随安装包分发，无需安装");
            ok(json!({
                "ok": true, "installed": true,
                "path": p.to_string_lossy(),
                "note": "Rust 版随安装包自带 ONNX 运行时，无需下载",
            }))
        }
        None => {
            st.logs.warn("视觉运行时缺失：runtime/ort/onnxruntime.dll 不存在");
            ok(json!({
                "ok": false, "installed": false,
                "error": "缺少 runtime/ort/onnxruntime.dll：请重新安装本程序",
            }))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn video_extension_list_matches_python() {
        for n in ["a.mp4", "b.MKV", "c.webm", "d.m2ts", "e.ts"] {
            assert!(is_video(n), "{n} 应认作视频");
        }
        for n in ["a.txt", "b.funscript", "c.srt", "noext"] {
            assert!(!is_video(n), "{n} 不该认作视频");
        }
    }

    #[test]
    fn netstat_pid_parser_only_takes_listening() {
        let text = "  TCP    0.0.0.0:8081   0.0.0.0:0    LISTENING    1234\n  TCP    127.0.0.1:8081   1.2.3.4:50000    ESTABLISHED    9999\n";
        assert_eq!(parse_netstat_pids(text, 8081), vec![1234]);
        assert!(parse_netstat_pids(text, 8082).is_empty());
    }

    #[test]
    fn adb_candidates_have_no_escaped_artifacts() {
        let p = paths::app_dir().join("tools").join("adb").join("adb.exe");
        let s = p.to_string_lossy().to_string();
        assert!(!s.chars().any(|c| c.is_control()), "路径含控制字符：{s:?}");
    }
}
