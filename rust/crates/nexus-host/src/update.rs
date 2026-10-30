//! 自更新：查版本 → 下载安装包 → 交接给安装器。
//!
//! **三条安全底线**（缺一条就不做）：
//! 1. 只有**已安装的副本**才允许就地升级；开发副本一律拒绝
//! 2. 下载必须有大小校验，不符就报错，绝不拿半截文件去覆盖
//! 3. 交接进程**不能住在安装目录里** —— Inno 的 RestartManager 会把它当成
//!    "占着待覆盖文件的应用"，关不掉 → 静默安装中止回滚
//!    （Python 版 1.0.39 实测踩过：界面卡"正在关闭应用程序…"30 秒后"正在撤销修改…"）

use std::path::PathBuf;
use std::sync::{Mutex, OnceLock};

use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::{json, Value};

use crate::paths;

#[derive(Debug, Clone, Default)]
struct UpdState {
    state: String,
    current: String,
    latest: String,
    has_update: bool,
    asset_url: String,
    size: u64,
    pct: u32,
    file: String,
    error: String,
}

fn state() -> &'static Mutex<UpdState> {
    static S: OnceLock<Mutex<UpdState>> = OnceLock::new();
    S.get_or_init(|| Mutex::new(UpdState::default()))
}

pub fn snapshot() -> Value {
    let s = state().lock().unwrap();
    json!({
        "ok": s.error.is_empty(),
        "state": s.state,
        "current": s.current,
        "latest": s.latest,
        "has_update": s.has_update,
        "asset_url": s.asset_url,
        "size": s.size,
        "pct": s.pct,
        "file": s.file,
        "error": s.error,
    })
}

/// 版本号 → 可比较的整数序列。
///
/// 必须剥掉"（开发副本）"这类非数字尾巴：Python 版曾因 int() 失败回退 (0,)，
/// 对任何远端版本都恒判"有更新"。
pub fn ver_tuple(v: &str) -> Vec<u64> {
    let mut out = Vec::new();
    for part in v.trim().trim_start_matches(['v', 'V']).split('.') {
        let digits: String = part.trim().chars().take_while(|c| c.is_ascii_digit()).collect();
        if digits.is_empty() {
            break;
        }
        out.push(digits.parse().unwrap_or(0));
    }
    if out.is_empty() {
        out.push(0);
    }
    out
}

fn newer(latest: &str, current: &str) -> bool {
    let a = ver_tuple(latest);
    let b = ver_tuple(current);
    let n = a.len().max(b.len());
    for i in 0..n {
        let x = a.get(i).copied().unwrap_or(0);
        let y = b.get(i).copied().unwrap_or(0);
        if x != y {
            return x > y;
        }
    }
    false
}

fn api_url() -> String {
    std::env::var("NEXUS_UPDATE_API")
        .unwrap_or_else(|_| "https://api.github.com/repos/wanfneg/FunScriptCast-Nexus/releases/latest".into())
}

/// 从 GitHub release JSON 里取版本号与安装包地址。
fn safe_version(v: &str) -> bool {
    !v.is_empty()
        && v.chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'))
}

fn parse_release(v: &Value) -> (String, String, u64) {
    let tag = v
        .get("tag_name")
        .and_then(|x| x.as_str())
        .unwrap_or("")
        .trim_start_matches(['v', 'V'])
        .to_string();
    let mut url = String::new();
    let mut size = 0u64;
    if let Some(arr) = v.get("assets").and_then(|x| x.as_array()) {
        for a in arr {
            let name = a.get("name").and_then(|x| x.as_str()).unwrap_or("");
            if name.to_lowercase().ends_with(".exe") && name.to_lowercase().contains("setup") {
                url = a
                    .get("browser_download_url")
                    .and_then(|x| x.as_str())
                    .unwrap_or("")
                    .to_string();
                size = a.get("size").and_then(|x| x.as_u64()).unwrap_or(0);
                break;
            }
        }
    }
    (tag, url, size)
}

pub async fn check(current: String) -> Response {
    {
        let mut s = state().lock().unwrap();
        if s.state == "downloading" {
            return Json(snapshot()).into_response();
        }
        s.state = "checking".into();
        s.error.clear();
        s.current = current.clone();
    }
    let client = match reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(15))
        .build()
    {
        Ok(c) => c,
        Err(e) => {
            let mut s = state().lock().unwrap();
            s.state = "error".into();
            s.error = format!("建客户端失败：{e}");
            return Json(snapshot()).into_response();
        }
    };
    // 注意：**不能**在 await 期间持有 MutexGuard —— 那会让 future 不是 Send，
    // 进而不能作为 axum handler（编译期报 "Handler not satisfied"，很难看出根因）
    let outcome: Result<(String, String, u64), String> = match client
        .get(api_url())
        .header("User-Agent", "FunScriptCast-Nexus")
        .header("Accept", "application/vnd.github+json")
        .send()
        .await
    {
        Ok(r) if r.status().is_success() => match r.json::<Value>().await {
            Ok(v) => Ok(parse_release(&v)),
            Err(e) => Err(format!("解析版本信息失败：{e}")),
        },
        Ok(r) => Err(format!("版本源返回 HTTP {}", r.status())),
        Err(e) => Err(format!("连不上版本源：{e}")),
    };
    {
        let mut s = state().lock().unwrap();
        match outcome {
            Ok((latest, url, size)) => {
                let has = !latest.is_empty() && newer(&latest, &current);
                s.state = if has { "available" } else { "none" }.into();
                s.has_update = has;
                s.latest = latest;
                s.asset_url = url;
                s.size = size;
                s.pct = 0;
                s.file.clear();
                s.error.clear();
            }
            Err(e) => {
                s.state = "error".into();
                s.error = e;
            }
        }
    }
    Json(snapshot()).into_response()
}

pub fn download_start() -> Result<(), String> {
    let (url, latest, size) = {
        let mut s = state().lock().unwrap();
        if s.state == "downloading" || s.state == "ready" {
            return Ok(());
        }
        if s.asset_url.is_empty() || s.latest.is_empty() {
            return Err("请先检查更新".into());
        }
        // 版本号会进安装包文件名和交接脚本：只放行字母数字与 ._-，
        // 防止远程元数据被篡改后注入 cmd 命令。
        if !safe_version(&s.latest) {
            return Err(format!("版本号含非法字符，拒绝下载：{}", s.latest));
        }
        // size=0 时唯一的完整性校验（声明大小比对）会整体旁路，
        // 任意长度的坏文件都会被标 ready——没有大小信息就拒绝下载
        if s.size == 0 {
            return Err("远端版本信息缺少安装包大小（size=0），拒绝下载".into());
        }
        s.state = "downloading".into();
        s.pct = 0;
        s.error.clear();
        (s.asset_url.clone(), s.latest.clone(), s.size)
    };
    // 下载前清掉上一轮升级残留（旧 Setup exe 接近 GB 级 + 交接脚本），
    // 只保留本次要写的文件名
    let dir0 = paths::data_dir().join("update");
    if let Ok(rd) = std::fs::read_dir(&dir0) {
        let keep = format!("FunScriptCast-Nexus-Setup-{latest}.exe");
        for e in rd.flatten() {
            let name = e.file_name().to_string_lossy().to_string();
            if name == keep {
                continue;
            }
            if name.ends_with(".exe") || name.ends_with(".cmd") || name.ends_with(".part") {
                let _ = std::fs::remove_file(e.path());
            }
        }
    }
    std::thread::Builder::new()
        .name("update-dl".into())
        .spawn(move || {
            let dir = paths::data_dir().join("update");
            let _ = std::fs::create_dir_all(&dir);
            let dest = dir.join(format!("FunScriptCast-Nexus-Setup-{latest}.exe"));
            let r = download(&url, &dest, size);
            let mut s = state().lock().unwrap();
            match r {
                Ok(()) => {
                    s.state = "ready".into();
                    s.pct = 100;
                    s.file = dest.to_string_lossy().to_string();
                }
                Err(e) => {
                    s.state = "error".into();
                    s.error = e;
                }
            }
        })
        .map_err(|e| format!("起不了下载线程：{e}"))?;
    Ok(())
}

fn download(url: &str, dest: &std::path::Path, want: u64) -> Result<(), String> {
    // 安全底线：只接受 https
    if !url.starts_with("https://") {
        return Err(format!("拒绝非 https 的更新源：{url}"));
    }
    use std::io::{Read, Write};
    // 重定向也要逐跳校验 https：release 资产会 302 到 CDN，但任何一跳落回 http
    // 都等于把安装包暴露给明文劫持。同时限制跳数，防重定向环。
    let client = reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_secs(3600))
        .redirect(reqwest::redirect::Policy::custom(|attempt| {
            if attempt.url().scheme() != "https" {
                attempt.error("更新下载重定向到非 https 地址，已拒绝")
            } else if attempt.previous().len() >= 5 {
                attempt.stop()
            } else {
                attempt.follow()
            }
        }))
        .build()
        .map_err(|e| format!("建客户端失败：{e}"))?;
    let mut resp = client
        .get(url)
        .send()
        .map_err(|e| format!("下载失败：{}", crate::models::err_chain(&e)))?;
    if !resp.status().is_success() {
        return Err(format!("下载失败：HTTP {}", resp.status()));
    }
    let mut f = std::fs::File::create(dest).map_err(|e| format!("建文件失败：{e}"))?;
    let mut buf = vec![0u8; 64 * 1024];
    let mut done: u64 = 0;
    loop {
        let n = resp.read(&mut buf).map_err(|e| format!("读取失败：{e}"))?;
        if n == 0 {
            break;
        }
        f.write_all(&buf[..n]).map_err(|e| format!("写文件失败：{e}"))?;
        done += n as u64;
        if want > 0 {
            // 超过声明大小立即中断：数据源不可信时防写爆磁盘
            if done > want {
                let _ = std::fs::remove_file(dest);
                return Err(format!(
                    "远端数据超出声明大小：本地 {done} / 远端 {want}，已中止下载"
                ));
            }
            let mut s = state().lock().unwrap();
            s.pct = ((done as f64 / want as f64) * 100.0) as u32;
        }
    }
    // 安全底线：大小不符就是坏包，绝不拿去覆盖
    if want > 0 && done != want {
        let _ = std::fs::remove_file(dest);
        return Err(format!("安装包大小不符：本地 {done} / 远端 {want}"));
    }
    Ok(())
}

/// 判断当前是不是"已安装的副本"（开发副本不支持就地升级）。
fn installed_exe() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let s = exe.to_string_lossy().to_lowercase();
    // 只认 cargo target 目录为开发副本；裸 contains("debug") 会把
    // 正式路径里恰好含 debug 字样的安装（D:\debug-tools\Nexus\）误判掉
    if s.contains("\\target\\") || s.contains("/target/") {
        return None;
    }
    exe.is_file().then_some(exe)
}

pub async fn install() -> Response {
    let st = state().lock().unwrap().clone();
    if st.state != "ready" || st.file.is_empty() {
        return Json(json!({"ok": false, "error": "安装包还没下载完成"})).into_response();
    }
    let Some(exe) = installed_exe() else {
        return Json(json!({
            "ok": false,
            "error": "开发副本不支持就地安装，请直接运行 artifacts/installer 里的安装包"
        }))
        .into_response();
    };
    let setup = PathBuf::from(&st.file);
    if !setup.is_file() {
        return Json(json!({"ok": false, "error": format!("安装包不在磁盘上：{}", setup.display())}))
            .into_response();
    }
    // 交接脚本必须放在 data/update/，**不能放安装目录**（RestartManager 会挡安装）
    let updir = paths::data_dir().join("update");
    let _ = std::fs::create_dir_all(&updir);
    let cmd_path = updir.join("_apply_update.cmd");
    // 批处理只用 ASCII，避免代码页问题；中文提示由日志/界面承担
    let script = format!(
        "@echo off\r\n\
         rem written by FunScriptCast-Nexus updater\r\n\
         :wait\r\n\
         timeout /t 1 /nobreak >nul\r\n\
         tasklist /fi \"IMAGENAME eq {exe_name}\" | find /i \"{exe_name}\" >nul && goto wait\r\n\
         start \"\" \"{setup}\" /SILENT /SUPPRESSMSGBOXES /NORESTART\r\n\
         exit\r\n",
        exe_name = exe.file_name().unwrap_or_default().to_string_lossy(),
        setup = setup.display(),
    );
    if let Err(e) = std::fs::write(&cmd_path, script) {
        return Json(json!({"ok": false, "error": format!("写交接脚本失败：{e}")})).into_response();
    }
    let mut cmd = std::process::Command::new("cmd.exe");
    cmd.arg("/c").arg("start").arg("").arg("/b").arg(&cmd_path);
    crate::proc_util::hide_console(&mut cmd);
    match cmd.spawn() {
        Ok(_) => {
            tracing::info!("更新交接已启动，准备退出");
            tokio::spawn(async {
                tokio::time::sleep(std::time::Duration::from_millis(800)).await;
                // 必须先做子进程清理再退出：裸 exit(0) 不走 Drop，llama/audiocpp
                // 会变成孤儿占着 GB 级显存与端口（Python request_quit 同款职责）
                if let Some(st) = crate::app_state() {
                    crate::shutdown_services_async(&st).await;
                }
                std::process::exit(0);
            });
            Json(json!({"ok": true, "restarting": true})).into_response()
        }
        Err(e) => Json(json!({"ok": false, "error": format!("启动交接进程失败：{e}")})).into_response(),
    }
}

/// 当前版本：读 version.json 的 versionName（与安装包同源）。
fn current_version() -> String {
    let p = paths::app_dir().join("version.json");
    std::fs::read_to_string(p)
        .ok()
        .and_then(|s| serde_json::from_str::<Value>(&s).ok())
        .and_then(|v| v.get("versionName").and_then(|x| x.as_str()).map(String::from))
        .unwrap_or_else(|| "0.0.0".to_string())
}

pub async fn check_route() -> Response {
    check(current_version()).await
}

pub async fn download_route() -> Response {
    match download_start() {
        // 与 Python `update_download()` 一致：回完整状态快照，而不是只有 ok/state
        Ok(()) => Json(snapshot()).into_response(),
        Err(e) => Json(json!({"ok": false, "error": e})).into_response(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn version_compare_ignores_non_numeric_tail() {
        assert!(newer("1.0.93", "1.0.92"));
        assert!(!newer("1.0.92", "1.0.93"));
        assert!(!newer("1.0.93", "1.0.93"));
        // 开发副本带后缀，不能被误判成"永远有新版"
        assert!(!newer("1.0.93", "1.0.93（开发副本）"));
        assert!(newer("1.1", "1.0.99"), "段数不同时按缺失位补 0");
    }

    #[test]
    fn ver_tuple_parses_common_forms() {
        assert_eq!(ver_tuple("v1.2.3"), vec![1, 2, 3]);
        assert_eq!(ver_tuple("1.0.93-rust"), vec![1, 0, 93]);
        assert_eq!(ver_tuple("abc"), vec![0]);
    }

    #[test]
    fn parse_release_picks_setup_exe() {
        let v: Value = serde_json::from_str(
            r#"{"tag_name":"v1.2.0","assets":[
                 {"name":"notes.txt","browser_download_url":"https://x/n.txt","size":1},
                 {"name":"FunScriptCast-Nexus-Setup-1.2.0.exe",
                  "browser_download_url":"https://x/s.exe","size":123}]}"#,
        )
        .unwrap();
        let (tag, url, size) = parse_release(&v);
        assert_eq!(tag, "1.2.0");
        assert_eq!(url, "https://x/s.exe");
        assert_eq!(size, 123);
    }

    #[test]
    fn version_string_rejects_cmd_metacharacters() {
        assert!(safe_version("1.2.3"));
        assert!(safe_version("1.2.3-rc_1"));
        assert!(!safe_version("1.2.3 & calc"));
        assert!(!safe_version("1.2.3\" > x"));
        assert!(!safe_version(""));
    }

    #[test]
    fn http_url_is_refused() {
        let dest = std::env::temp_dir().join("nexus-upd-test.exe");
        let e = download("http://example.com/a.exe", &dest, 0).unwrap_err();
        assert!(e.contains("非 https"), "实际：{e}");
    }

    /// 版本源可达时：能解析出更新并给出安装包地址。
    #[tokio::test]
    async fn check_parses_release_from_local_feed() {
        use std::io::{Read, Write};
        use std::net::TcpListener;

        let body = br#"{"tag_name":"v9.9.9","assets":[{"name":"FunScriptCast-Nexus-Setup-9.9.9.exe","browser_download_url":"https://example.com/s.exe","size":42}]}"#.to_vec();
        let blen = body.len();
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        std::thread::spawn(move || {
            if let Ok((mut s, _)) = listener.accept() {
                let mut req = [0u8; 1024];
                let _ = s.read(&mut req);
                let hdr = format!("HTTP/1.1 200 OK\r\nContent-Length: {blen}\r\nContent-Type: application/json\r\n\r\n");
                let _ = s.write_all(hdr.as_bytes());
                let _ = s.write_all(&body);
            }
        });
        std::env::set_var("NEXUS_UPDATE_API", format!("http://127.0.0.1:{port}/latest"));

        let resp = check("1.0.93".to_string()).await;
        // 取 JSON 内容判断
        let bytes = axum::body::to_bytes(resp.into_body(), 1 << 20).await.unwrap();
        let v: Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(v["state"], "available", "应判为有更新：{v}");
        assert_eq!(v["latest"], "9.9.9");
        assert_eq!(v["has_update"], true);
        assert_eq!(v["asset_url"], "https://example.com/s.exe");
        assert_eq!(v["size"], 42);
        std::env::remove_var("NEXUS_UPDATE_API");
    }
}