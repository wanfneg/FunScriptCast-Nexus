//! 头显（VR）专用局域网接口，绑 `0.0.0.0:8791`。
//!
//! **只放开四个路由，其余一律 403**。白名单是"正向枚举"的：新增路由必须显式加进来，
//! 不可能因为漏了一处判断而意外暴露。
//!
//! 为什么另起一个端口：8790 上挂着设置、设备同步（会调 adb）、退出…… 把它绑到
//! 局域网就等于把这些全开了。头显是可信设备，但局域网不一定只有头显。
//!
//! 字段刻意只回**标识类**（后端名、版本、档位建议），不含本机路径、日志、设备序列号。

use std::collections::HashMap;

use axum::extract::{Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde_json::{json, Value};

use crate::state::AppState;

/// 默认端口（可用环境变量覆盖）。
pub const DEFAULT_LAN_PORT: u16 = 8791;

pub fn lan_port() -> u16 {
    std::env::var("FS_HOST_LAN_PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(DEFAULT_LAN_PORT)
}

type St = State<std::sync::Arc<AppState>>;

/// 只回"最小字段"：头显轮询用。
///
/// subtitle 锁内只取快照，probe（HTTP）在锁外做——头显每秒轮询这条，
/// 持锁等网络会把本地字幕接口一起冻住。
async fn headset_status(st: &AppState) -> Json<Value> {
    let parts = {
        let sub = st.subtitle.lock().await;
        sub.status_parts()
    };
    // 只有本地 llama 才有可探的常驻进程：openai/ollama/none 不拉 llama-server，
    // 拿 8082 的探活结果去判断就会永远"未就绪"
    let (asr_ok, mt_ok) = if parts.translate_local {
        let (a, m) = tokio::join!(parts.asr.probe(), parts.mt.probe());
        (a, m)
    } else {
        (parts.asr.probe().await, false)
    };
    let running = asr_ok || mt_ok;
    let backend = parts.translate_backend.as_str();
    let cloud = backend == "openai" || backend == "cloud";
    let local_tr = backend == "local";
    // 档位建议：云端单块 6.6–15s，头显 3s 档的过期阈值只有 6s，结构性追不上，
    // 所以云端一律建议 25s（切块），本地按配置给（默认 3）
    let chunk = if cloud {
        25
    } else {
        parts.recommended_chunk_sec.unwrap_or(3).clamp(1, 25)
    };
    Json(json!({
        "ok": true,
        // 本地翻译时必须等翻译也就绪（否则头显在 llama 冷加载途中就恢复播放，首句撞冷启动）
        "ready": running && asr_ok && (!local_tr || mt_ok),
        "status": if running { "ready" } else { "stopped" },
        "error": "",
        // 只给模型/后端的**名字**，不给路径
        "asr": asr_name(&parts.asr_model),
        "translate": backend,
        "version": st.version,
        "translate_backend": backend,
        "recommended_chunk_sec": chunk,
    }))
}

/// 模型标识只取最后一段（不再暴露本机绝对路径）。
fn asr_name(model: &str) -> String {
    model
        .rsplit(['/', '\\'])
        .next()
        .unwrap_or(model)
        .to_string()
}

async fn status_route(State(st): St) -> Response {
    headset_status(&st).await.into_response()
}

/// 兼容桩：字幕缓存已删，恒回"未命中"，让已发布的旧版头显走正常识别路径。
async fn cache_route(Query(q): Query<HashMap<String, String>>) -> Response {
    Json(json!({
        "ok": true,
        "hit": false,
        "resolved": q.get("video").cloned().unwrap_or_default(),
    }))
    .into_response()
}

async fn cache_save_route() -> Response {
    Json(json!({"ok": false, "error": "字幕缓存功能已移除"})).into_response()
}

async fn start_route(State(st): St) -> Response {
    st.logs.info("头显请求启动字幕服务");
    // 与 Python 一致：立即返回，拉起常驻进程放后台（头显随后轮询 /api/headset/status）。
    // 同步等待最长 120s，会撞头显/前端的请求超时。
    crate::routes::subtitle_start(State(st)).await
}

/// 白名单之外的路径：403（不回内部细节）。
async fn deny() -> Response {
    (
        StatusCode::FORBIDDEN,
        Json(json!({"ok": false, "error": "该接口不对局域网开放"})),
    )
        .into_response()
}

pub fn router(st: std::sync::Arc<AppState>) -> Router {
    Router::new()
        .route("/api/headset/status", get(status_route))
        .route("/api/subtitle/cache", get(cache_route))
        .route("/api/subtitle/start", post(start_route))
        .route("/api/subtitle/cache/save", post(cache_save_route))
        // 未列出的路径一律 403（含根路径）——用 fallback 而不是通配路由，
        // 通配路由匹配不到 "/"，会漏成 404
        .fallback(deny)
        // 0.0.0.0 监听含回环：本机恶意网页可跨站 POST 拉起 3GB 模型进程，
        // 与主服务同一道防线（头显/curl 等非浏览器调用不带 Origin，不受影响）
        .layer(axum::middleware::from_fn(crate::server::deny_foreign_writes))
        .with_state(st)
}

/// 后台起监听；端口占用等失败只记警告，不影响主服务。
pub async fn serve(st: std::sync::Arc<AppState>, port: u16) -> std::io::Result<()> {
    let app = router(st);
    let listener = tokio::net::TcpListener::bind(("0.0.0.0", port)).await?;
    tracing::info!("头显接口已就绪：0.0.0.0:{port}");
    axum::serve(listener, app).await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn asr_name_strips_paths() {
        assert_eq!(asr_name("models/qwen3-asr"), "qwen3-asr");
        assert_eq!(asr_name("C:/x/y/model"), "model");
        assert_eq!(asr_name("laccapture"), "laccapture");
    }

    #[test]
    fn lan_port_default() {
        // 环境变量未设时为 8791（测试进程里通常未设）
        let p = lan_port();
        assert!(p == DEFAULT_LAN_PORT || p > 1024, "端口异常：{p}");
    }
}

#[cfg(test)]
mod tests_whitelist {
    use super::*;
    use axum::body::Body;
    use axum::http::Request;
    use tower::ServiceExt;

    fn app() -> Router {
        let st = crate::state::AppState::new("0.1.0-rust-test", 8890);
        router(st)
    }

    async fn status_of(path: &str) -> u16 {
        let res = app()
            .oneshot(Request::builder().uri(path).body(Body::empty()).unwrap())
            .await
            .unwrap();
        res.status().as_u16()
    }

    #[tokio::test]
    async fn whitelisted_paths_are_allowed() {
        assert_eq!(status_of("/api/headset/status").await, 200);
        assert_eq!(status_of("/api/subtitle/cache?video=a.mp4").await, 200);
    }

    #[tokio::test]
    async fn everything_else_is_403() {
        // 这是这个端口存在的唯一理由：8790 上的设置/设备/退出绝不能被局域网摸到
        for p in [
            "/",
            "/api/state",
            "/api/settings",
            "/api/device/move",
            "/api/quit",
            "/nope",
        ] {
            assert_eq!(status_of(p).await, 403, "{p} 应当被拒绝");
        }
    }
}
