//! 新底座只读接口（`/api/v2/*`）。
//!
//! 契约对齐 Python `app/api/routes/meta.py` / `state.py`，消费方是
//! `web/src`（Dashboard / Library）。这些路由只做数据搬运，
//! 真实逻辑全部复用历史 `/api/*` 的实现，避免两份真相。

use axum::extract::State;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::{json, Value};

use crate::paths;
use crate::routes;
use crate::state::AppState;

type St = State<std::sync::Arc<AppState>>;

fn ok(v: Value) -> Response {
    Json(v).into_response()
}

pub async fn ping() -> Response {
    ok(json!({"ok": true, "pong": true}))
}

pub async fn system(State(st): St) -> Response {
    let web = paths::web_dist();
    ok(json!({
        "ok": true,
        "app_root": paths::app_dir().display().to_string(),
        // Rust 版没有 Python，字段照契约保留，前端只会拼进提示文案。
        "version": st.version,
        "version_code": 0,
        "channel": "rust",
        "python": "—",
        "platform": std::env::consts::OS,
        "web_dist": {
            "path": web.display().to_string(),
            "built": web.join("index.html").is_file(),
        },
        "legacy_ui": paths::ui_dir().join("index.html").is_file(),
        "runtime": {"port": st.port, "lan_api_port": crate::lan::lan_port()},
    }))
}

/// 与 `/api/state` 完全同包：前端要的 version/uptime/host/dlna/events 都已在里面。
pub async fn state(State(st): St) -> Response {
    routes::state(State(st)).await
}

/// 前端 `LogEntry.ts` 是**数字**（Unix 秒，`new Date(ts * 1000)`）；
/// 宿主的 `LogLine.ts` 是 "秒.毫秒" 字符串，直接透传会显示 Invalid Date。
pub async fn logs(State(st): St) -> Response {
    let list: Vec<Value> = st
        .logs
        .tail(300)
        .iter()
        .map(|l| {
            json!({
                "ts": l.ts.parse::<f64>().unwrap_or(0.0),
                "level": l.level,
                "msg": l.msg,
            })
        })
        .collect();
    ok(json!({"ok": true, "logs": list}))
}

pub async fn library_state(State(st): St) -> Response {
    routes::library_state(State(st)).await
}
