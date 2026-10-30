//! axum 路由装配。

use std::sync::Arc;

use axum::routing::{get, post};
use axum::Router;
use tower_http::services::{ServeDir, ServeFile};
use tower_http::trace::TraceLayer;

use crate::paths;
use crate::routes;
use crate::state::AppState;

/// 静态资源与接口一律 `no-cache`。
///
/// ui-legacy 的 app.js/styles.css 不带内容哈希，WebView2 会按启发式缓存旧脚本：
/// 前端逻辑改了、安装包也更新了，界面却还在跑上一版（AI 按钮双触发修完后仍复现）。
async fn no_cache(
    req: axum::extract::Request,
    next: axum::middleware::Next,
) -> axum::response::Response {
    let mut res = next.run(req).await;
    res.headers_mut().insert(
        axum::http::header::CACHE_CONTROL,
        axum::http::HeaderValue::from_static("no-cache"),
    );
    res
}

/// 未实现的 /api/* 一律 404（带 JSON 说明），绝不回落到静态页。
async fn unknown_api(req: axum::extract::Request) -> axum::response::Response {
    use axum::response::IntoResponse;
    let path = req.uri().path().to_string();
    tracing::warn!("未知接口：{path}");
    (
        axum::http::StatusCode::NOT_FOUND,
        axum::Json(serde_json::json!({
            "ok": false,
            "error": format!("接口未实现：{path}"),
        })),
    )
        .into_response()
}

/// 面向 0.0.0.0 服务的通用跨站写防护（8791 LAN API / 8756 头显字幕服务）。
///
/// 与主服务的 `reject_cross_site_writes` 同一威胁模型：本机任意网页都能对
/// `http://127.0.0.1:{port}` 发 no-cors POST——在这里等于"跨站拉起 3GB 的
/// 模型进程"或"触发 GPU 推理"。写方法带 Origin 且 Origin 主机不是本机
/// （回环 / localhost / 本机局域网 IP）一律 403；
/// curl/头显等非浏览器调用不带 Origin，不受影响。
pub async fn deny_foreign_writes(
    req: axum::extract::Request,
    next: axum::middleware::Next,
) -> axum::response::Response {
    use axum::http::{header, Method, StatusCode};
    use axum::response::IntoResponse;
    let is_write = matches!(
        *req.method(),
        Method::POST | Method::PUT | Method::DELETE | Method::PATCH
    );
    if is_write {
        if let Some(origin) = req.headers().get(header::ORIGIN).and_then(|v| v.to_str().ok()) {
            let host = origin_host(origin);
            let allowed = [
                "127.0.0.1".to_string(),
                "::1".to_string(),
                "localhost".to_string(),
                crate::sysinfo::lan_ip(),
            ];
            let foreign = match &host {
                Some(h) => !allowed.iter().any(|a| a.eq_ignore_ascii_case(h)),
                // Origin: null（sandboxed iframe 等）一律视为外来
                None => true,
            };
            if foreign {
                return (
                    StatusCode::FORBIDDEN,
                    axum::Json(serde_json::json!({
                        "ok": false,
                        "error": "跨站写请求已拒绝",
                    })),
                )
                    .into_response();
            }
        }
    }
    next.run(req).await
}

/// 从 Origin 头取主机名（去协议、去端口；IPv6 保留 [] 内部分）。
fn origin_host(origin: &str) -> Option<String> {
    let rest = origin.split("://").nth(1)?;
    let authority = rest.split('/').next()?;
    let host = if let Some(rest6) = authority.strip_prefix('[') {
        rest6.split(']').next()?.to_string()
    } else {
        authority
            .rsplit_once(':')
            .map(|(h, _)| h.to_string())
            .unwrap_or_else(|| authority.to_string())
    };
    if host.is_empty() {
        None
    } else {
        Some(host.to_string())
    }
}

/// 写方法（POST/PUT/DELETE/PATCH）必须来自本机页面：带 Origin 头且不是
/// `http://127.0.0.1:{port}` / `localhost` / `[::1]` 一律 403。
///
/// 为什么需要它：浏览器里任何网页都能向 `http://127.0.0.1:{port}` 发"简单跨站请求"
/// （no-cors POST 无需预检），曾经 `/api/quit` 就是这么被任意网页静默杀掉宿主的。
/// 带写方法必然携带 Origin 头，所以这一个判断就能挡住全部跨站写；
/// curl/头显等非浏览器调用不带 Origin，不受影响。
async fn reject_cross_site_writes(
    axum::extract::State(st): axum::extract::State<std::sync::Arc<AppState>>,
    req: axum::extract::Request,
    next: axum::middleware::Next,
) -> axum::response::Response {
    use axum::http::{header, Method, StatusCode};
    use axum::response::IntoResponse;
    let is_write = matches!(
        *req.method(),
        Method::POST | Method::PUT | Method::DELETE | Method::PATCH
    );
    // GET 的 Host 栅栏（Python `do_GET` 同款）：防 DNS rebinding 读取
    // `/api/state`（含完整设置与云端 key）。缺失 Host 的 HTTP/1.0 请求放行。
    let is_get = matches!(*req.method(), Method::GET | Method::HEAD);
    if is_get {
        if let Some(host) = req.headers().get(header::HOST).and_then(|v| v.to_str().ok()) {
            let local = [
                format!("127.0.0.1:{}", st.port),
                format!("localhost:{}", st.port),
                format!("[::1]:{}", st.port),
            ];
            if !local.iter().any(|a| a == host) {
                return (
                    StatusCode::FORBIDDEN,
                    axum::Json(serde_json::json!({
                        "ok": false,
                        "error": "非本机 Host 的读取请求已拒绝",
                    })),
                )
                    .into_response();
            }
        }
    }
    if is_write {
        if let Some(origin) = req.headers().get(header::ORIGIN).and_then(|v| v.to_str().ok()) {
            let local = [
                format!("http://127.0.0.1:{}", st.port),
                format!("http://localhost:{}", st.port),
                format!("http://[::1]:{}", st.port),
            ];
            if !local.iter().any(|a| a == origin) {
                return (
                    StatusCode::FORBIDDEN,
                    axum::Json(serde_json::json!({
                        "ok": false,
                        "error": "跨站写请求已拒绝",
                    })),
                )
                    .into_response();
            }
        }
    }
    next.run(req).await
}

pub fn build(state: Arc<AppState>) -> Router {
    let api = Router::new()
        // ---- 状态 / 设置 / 日志 ----
        .route("/api/state", get(routes::state))
        .route("/api/settings", get(routes::get_settings).post(routes::post_settings))
        .route("/api/logs", get(routes::logs))
        // ---- 设备 ----
        .route("/api/device/state", get(routes::device_state))
        .route("/api/device/scan", post(routes::device_scan))
        .route("/api/device/connect", post(routes::device_connect))
        .route("/api/device/disconnect", post(routes::device_disconnect))
        .route("/api/device/move", post(routes::device_move))
        .route("/api/device/limit", post(routes::device_limit))
        .route("/api/device/mode", post(routes::device_mode))
        .route("/api/device/refresh", post(routes::device_refresh))
        // ---- 脚本同步 ----
        .route("/api/sync", get(routes::get_sync))
        .route("/api/sync/start", post(routes::sync_start))
        .route("/api/sync/tick", post(routes::sync_tick))
        .route("/api/sync/delay", post(routes::sync_delay))
        .route("/api/sync/stop", post(routes::sync_stop))
        // 兼容旧接口：Python 里就是 save_settings(body) 的别名
        .route("/api/sync/settings", post(routes::post_settings))
        // ---- 快捷动作 / 预设 ----
        .route("/api/quick", get(routes::get_quick).post(routes::post_quick))
        .route("/api/preset", get(routes::get_preset).post(routes::post_preset))
        // ---- 未移植子系统（明确 501，不假装成功） ----
        .route(
            "/api/update/check",
            get(crate::update::check_route).post(crate::update::check_route),
        )
        .route("/api/update/download", post(crate::update::download_route))
        .route("/api/update/install", post(crate::update::install))
        .route("/api/models/catalog", get(crate::models::catalog))
        .route("/api/models/download", post(crate::models::download_route))
        .route("/api/vision/state", get(routes::vision_status))
        .route("/api/vision/open", post(routes::vision_open))
        .route("/api/vision/actions", get(routes::vision_actions))
        .route("/api/vision/close", post(routes::vision_close))
        .route("/api/vision/selfcheck", get(routes::vision_selfcheck))
        // 旧前端/旧脚本仍在调用的兼容接口
        .route("/api/vision/auto", post(routes::vision_auto))
        .route("/api/vision/start", post(routes::vision_start_compat))
        .route("/api/vision/stop", post(routes::vision_close))
        .route("/api/vision/runtime/remove", post(crate::extras::vision_runtime_remove))
        .route("/api/win/show", post(routes::win_show))
        .route("/api/subtitle/status", get(routes::subtitle_status))
        .route("/api/subtitle/start", post(routes::subtitle_start))
        .route("/api/subtitle/stop", post(routes::subtitle_stop))
        .route("/api/subtitle/config", get(routes::subtitle_config).post(routes::subtitle_config_save))
        .route("/api/subtitle/translate", post(routes::subtitle_translate))
        .route("/api/subtitle/stream", post(routes::subtitle_stream))
        .route("/api/subtitle/models", get(crate::extras::subtitle_models))
        .route("/api/subtitle/asr-models", get(crate::extras::subtitle_asr_models))
        .route("/api/subtitle/translate-test", post(crate::extras::subtitle_translate_test))
        .route("/api/subtitle/reclaim", post(crate::extras::subtitle_reclaim))
        .route("/api/library/browse", get(crate::extras::library_browse))
        .route("/api/device/settings", post(crate::extras::device_settings))
        .route("/api/sync/devices", post(crate::extras::sync_devices))
        .route("/api/sync/connect", post(crate::extras::sync_connect))
        .route("/api/sync/disconnect", post(crate::extras::sync_disconnect))
        .route("/api/sync/run", post(crate::extras::sync_run))
        .route("/api/js-error", post(crate::extras::js_error))
        .route("/api/vision/runtime/install", post(crate::extras::vision_runtime_install))
        .route("/api/library/state", get(routes::library_state))
        .route("/api/library/rescan", post(routes::library_rescan))
        .route("/api/library/items", get(routes::library_items))
        .route("/api/library/progress", post(routes::library_progress))
        .route("/api/library/thumb", get(routes::library_thumb))
        .route("/api/library/stream", get(routes::library_stream))
        .route(
            "/api/library/script",
            get(routes::library_script).post(routes::manual_script),
        )
        .route("/api/dlna/state", get(routes::dlna_state))
        .route("/api/dlna/start", post(routes::dlna_start))
        .route("/api/dlna/stop", post(routes::dlna_stop))
        .route("/api/dlna/roots", get(routes::dlna_state).post(routes::dlna_roots))
        // ---- 新底座只读接口（web/src 消费）----
        .route("/api/v2/ping", get(crate::v2::ping))
        .route("/api/v2/system", get(crate::v2::system))
        .route("/api/v2/state", get(crate::v2::state))
        .route("/api/v2/logs", get(crate::v2::logs))
        .route("/api/v2/library/state", get(crate::v2::library_state))
        // ---- 退出 ----
        .route("/api/quit", post(routes::quit))
        // ---- 未知接口兜底 ----
        // 不加这条的话，未实现的 /api/* 会落到静态文件的 SPA 回落，
        // 返回 **200 + index.html** —— 前端 JSON 解析炸掉，而日志里看不出任何异常。
        // 这类"静默成功"是最难查的一类问题，宁可让它们响亮地 404。
        .route("/api/{*rest}", axum::routing::any(unknown_api))
        // 跨站写防护：必须覆盖全部 /api/*（在所有路由注册之后 layer 才生效到全部）
        .layer(axum::middleware::from_fn_with_state(
            std::sync::Arc::clone(&state),
            reject_cross_site_writes,
        ));

    // 与 Python 一致：根路径是旧界面（ui-legacy），新界面挂在 /app。
    // Vite 构建的 base 是 /app/，产物引用 /app/assets/* —— 必须按前缀挂载，
    // 直接根挂会让 /app/assets/x.js 查不到后被 SPA 回落成 index.html
    // （200 + text/html），浏览器模块加载失败，界面白屏。
    let web = paths::web_dist();
    let legacy = paths::ui_dir();
    let web_index = web.join("index.html");
    let app_assets = ServeDir::new(&web).fallback(ServeFile::new(web_index.clone()));
    let has_legacy = legacy.join("index.html").is_file();
    let root_dir = if has_legacy { legacy.clone() } else { web.clone() };
    let root_index = if has_legacy { legacy.join("index.html") } else { web_index };
    let spa = ServeDir::new(&root_dir).fallback(ServeFile::new(root_index));

    let router = Router::new()
        .merge(api)
        .nest_service("/app", app_assets);
    let router = if has_legacy {
        router.fallback_service(spa)
    } else {
        // 没有旧界面时，根路径直接送到新界面（否则 index 引用 /app/* 会白屏）
        router.route("/", get(|| async { axum::response::Redirect::temporary("/app/") }))
    };

    router
        .layer(axum::middleware::from_fn(no_cache))
        .layer(TraceLayer::new_for_http())
        .with_state(state)
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::Body;
    use axum::http::{Request, StatusCode};
    use tower::ServiceExt;

    fn app() -> Router {
        build(AppState::new("0.1.0-rust-test", 8890))
    }

    async fn get_json(app: Router, uri: &str) -> (StatusCode, serde_json::Value) {
        let res = app
            .oneshot(Request::builder().uri(uri).body(Body::empty()).unwrap())
            .await
            .unwrap();
        let status = res.status();
        let bytes = axum::body::to_bytes(res.into_body(), 1 << 20).await.unwrap();
        let v = serde_json::from_slice(&bytes).unwrap_or(serde_json::Value::Null);
        (status, v)
    }

    #[tokio::test]
    async fn state_endpoint_returns_ok() {
        let (status, v) = get_json(app(), "/api/state").await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(v["ok"], true);
        assert_eq!(v["version"], "0.1.0-rust-test");
        assert!(v["device"].is_object());
        assert!(v["sync"].is_object());
        assert!(v["settings"].is_object());
    }

    #[tokio::test]
    async fn device_state_has_motion_fields() {
        let (status, v) = get_json(app(), "/api/device/state").await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(v["device"]["allow_move"], true);
        assert_eq!(v["device"]["max_speed"], 500);
        assert_eq!(v["device"]["range"], serde_json::json!([0.0, 100.0]));
    }

    #[tokio::test]
    async fn preset_endpoint_lists_24_presets() {
        let (status, v) = get_json(app(), "/api/preset").await;
        assert_eq!(status, StatusCode::OK);
        let n = v["presets"].as_array().map(|a| a.len()).unwrap_or(0);
        assert_eq!(n, 24, "真实预设表应为 24 个，实际 {n}");
    }

    /// Win11 实时字幕（lccapture）已整体移除：旧接口必须 404，不能留半套。
    #[tokio::test]
    async fn live_captions_endpoints_are_gone() {
        for p in ["/api/lc/state", "/api/lc/start", "/api/lc/stop"] {
            let (status, _) = get_json(app(), p).await;
            assert_eq!(status, StatusCode::NOT_FOUND, "{p} 应已移除");
        }
    }

    #[tokio::test]
    async fn all_subsystems_are_wired() {
        // 所有子系统接线后不再有 501 桩：这些曾经是 not_ported 的接口都应正常回 200
        for p in [
            "/api/models/catalog",
            "/api/vision/state",
            "/api/subtitle/status",
            "/api/dlna/state",
        ] {
            let (status, v) = get_json(app(), p).await;
            assert_eq!(status, StatusCode::OK, "{p} 应已接线");
            assert!(v.get("not_ported").is_none(), "{p} 不应再有 not_ported");
        }
    }

    /// Vite 构建 base 是 /app/：产物必须从 /app/assets/* 取到本体。
    /// 曾直接根挂静态目录，导致这里被 SPA 回落成 index.html（200 + text/html），
    /// 浏览器模块加载失败，界面白屏。
    #[tokio::test]
    async fn app_assets_are_served_not_spa_fallback() {
        let assets = crate::paths::web_dist().join("assets");
        let Ok(entries) = std::fs::read_dir(&assets) else {
            return; // 没构建前端时跳过
        };
        let name = entries
            .filter_map(|e| e.ok())
            .map(|e| e.file_name().to_string_lossy().to_string())
            .find(|n| n.ends_with(".js"));
        let Some(name) = name else { return };
        let res = app()
            .oneshot(
                Request::builder()
                    .uri(format!("/app/assets/{name}"))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::OK);
        let ct = res
            .headers()
            .get("content-type")
            .and_then(|v| v.to_str().ok())
            .unwrap_or("");
        assert!(ct.contains("javascript"), "应返回 JS 本体，实际 Content-Type={ct}");
    }

    /// web/src 的 Dashboard / Library 只靠这 5 条 v2 只读接口取数。
    #[tokio::test]
    async fn v2_readonly_endpoints_match_frontend_contract() {
        let (status, v) = get_json(app(), "/api/v2/ping").await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(v["pong"], true);

        let (status, v) = get_json(app(), "/api/v2/system").await;
        assert_eq!(status, StatusCode::OK);
        assert!(v["app_root"].is_string());
        assert!(v["web_dist"].is_object());
        assert!(v["legacy_ui"].is_boolean());

        let (status, v) = get_json(app(), "/api/v2/state").await;
        assert_eq!(status, StatusCode::OK);
        assert!(v["uptime"].is_number());
        assert!(v["host"].is_object());

        let (status, v) = get_json(app(), "/api/v2/logs").await;
        assert_eq!(status, StatusCode::OK);
        let logs = v["logs"].as_array().expect("logs 应为数组");
        assert!(!logs.is_empty(), "启动期应有日志");
        assert!(logs[0]["ts"].is_number(), "前端按数字秒解析，ts 不能是字符串");

        let (status, v) = get_json(app(), "/api/v2/library/state").await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(v["ok"], true);
    }

    #[tokio::test]
    async fn sync_start_without_device_reports_no_device() {
        let app = app();
        let res = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/sync/start")
                    .header("content-type", "application/json")
                    .body(Body::from(r#"{"path":"C:/nope.mp4"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        let bytes = axum::body::to_bytes(res.into_body(), 1 << 20).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(v["ok"], false);
        assert_eq!(v["no_device"], true);
    }

    /// DNS rebinding 防护：带外站 Host 的 GET 必须 403，本机 Host 正常。
    #[tokio::test]
    async fn get_rejects_foreign_host() {
        let res = app()
            .oneshot(
                Request::builder()
                    .uri("/api/state")
                    .header("host", "evil.example")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::FORBIDDEN);

        let res = app()
            .oneshot(
                Request::builder()
                    .uri("/api/state")
                    .header("host", "127.0.0.1:8890")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::OK);
    }

    /// 手动选择任意 `.funscript`：返回动作数组并装载到同步引擎。
    #[tokio::test]
    async fn manual_script_loads_arbitrary_funscript() {
        let dir = std::env::temp_dir().join(format!("nexus-manual-script-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let video = dir.join("v.mp4");
        let script = dir.join("other-name.funscript");
        std::fs::write(&video, b"x").unwrap();
        std::fs::write(
            &script,
            r#"{"actions":[{"at":0,"pos":0},{"at":1000,"pos":100}]}"#,
        )
        .unwrap();
        let body = serde_json::json!({
            "video": video.to_string_lossy(),
            "script": script.to_string_lossy(),
        });
        let req = Request::builder()
            .method("POST")
            .uri("/api/library/script")
            .header("content-type", "application/json")
            .body(Body::from(body.to_string()))
            .unwrap();
        let res = app().oneshot(req).await.unwrap();
        assert_eq!(res.status(), StatusCode::OK);
        let bytes = axum::body::to_bytes(res.into_body(), 1 << 20).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(v["ok"], true);
        assert_eq!(v["actions"].as_array().unwrap().len(), 2);
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// `/api/device/limit` 只发临时限位帧，不得改写本地行程/速度（Python 同款）。
    #[tokio::test]
    async fn device_limit_does_not_mutate_motion_settings() {
        let app = app();
        let res = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/device/limit")
                    .header("content-type", "application/json")
                    .body(Body::from(r#"{"min":20,"max":80,"speed":300}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::OK);
        let (_, v) = get_json(app, "/api/device/state").await;
        assert_eq!(v["device"]["range"], serde_json::json!([0.0, 100.0]));
        assert_eq!(v["device"]["max_speed"], 500);
    }

    #[tokio::test]
    async fn settings_post_persists() {
        let app = app();
        let res = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/settings")
                    .header("content-type", "application/json")
                    .body(Body::from(r#"{"theme":"dark"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::OK);
        let (_, v) = get_json(app, "/api/settings").await;
        assert_eq!(v["settings"]["theme"], "dark");
    }

    /// 前端读 `r.stopped` 区分"已停止"与"本来就没运行"（Python `sub_stop()` 同款）。
    #[tokio::test]
    async fn subtitle_stop_reports_stopped_flag() {
        let app = app();
        let res = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/subtitle/stop")
                    .header("content-type", "application/json")
                    .body(Body::from("{}"))
                    .unwrap(),
            )
            .await
            .unwrap();
        let bytes = axum::body::to_bytes(res.into_body(), 1 << 20).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(v["ok"], true);
        assert_eq!(v["stopped"], false, "测试进程没拉起过字幕服务，应为 false");
    }
}
