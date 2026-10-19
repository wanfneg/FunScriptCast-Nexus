use axum::{routing::get, Json, Router};
use serde_json::{json, Value};

async fn state() -> Json<Value> {
    Json(json!({
        "ok": true,
        "version": "0.1.0-rust",
        "host": { "port": 8790 },
        "vision": { "running": false },
    }))
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let app = Router::new().route("/api/state", get(state));
    let addr = std::env::var("NEXUS_BIND").unwrap_or_else(|_| "127.0.0.1:8790".into());
    let listener = tokio::net::TcpListener::bind(&addr).await?;
    println!("nexus-host listening on http://{addr}");
    axum::serve(listener, app).await?;
    Ok(())
}
