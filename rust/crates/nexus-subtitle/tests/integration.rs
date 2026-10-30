//! 协议级集成测试：用本地假服务顶替 audiocpp / llama-server，
//! 验证请求形状与响应解析（不需要真模型、不需要 GPU）。

use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use axum::extract::State;
use axum::routing::{get, post};
use axum::{Json, Router};
use nexus_subtitle::audiocpp::AudioCppBackend;
use nexus_subtitle::config::{AsrConfig, TranslateConfig};
use nexus_subtitle::llama::LlamaBackend;
use nexus_subtitle::translate::TranslateEngine;
use serde_json::{json, Value};

/// 假服务共用的状态（axum 只允许一种 State 类型）。
#[derive(Default)]
struct FakeState {
    /// 收到的请求体
    sink: Mutex<Vec<Value>>,
    /// llama 要回的译文
    reply: Mutex<String>,
    /// 调用计数（验证重试）
    calls: AtomicUsize,
}

async fn spawn(app: Router) -> u16 {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });
    port
}

async fn fake_audiocpp(State(st): State<Arc<FakeState>>, Json(b): Json<Value>) -> Json<Value> {
    st.sink.lock().unwrap().push(b);
    st.calls.fetch_add(1, Ordering::SeqCst);
    Json(json!({"text": " こんにちは ", "rtf": 0.1}))
}

/// 复读版 audiocpp：第一次原样回 context，第二次给真文本。
async fn fake_audiocpp_echo(State(st): State<Arc<FakeState>>, Json(b): Json<Value>) -> Json<Value> {
    let n = st.calls.fetch_add(1, Ordering::SeqCst);
    st.sink.lock().unwrap().push(b.clone());
    let ctx = b.get("context").and_then(|v| v.as_str()).unwrap_or("").to_string();
    if n == 0 && !ctx.is_empty() {
        Json(json!({"text": ctx}))
    } else {
        Json(json!({"text": "本当のテキスト"}))
    }
}

async fn fake_llama(State(st): State<Arc<FakeState>>, Json(b): Json<Value>) -> Json<Value> {
    st.sink.lock().unwrap().push(b);
    st.calls.fetch_add(1, Ordering::SeqCst);
    let text = st.reply.lock().unwrap().clone();
    Json(json!({"choices": [{"message": {"content": text}}]}))
}

#[tokio::test]
async fn audiocpp_request_shape_and_parse() {
    let st: Arc<FakeState> = Arc::default();
    let app = Router::new()
        .route("/health", get(|| async { "ok" }))
        .route("/v1/audio/transcriptions", post(fake_audiocpp))
        .with_state(st.clone());
    let port = spawn(app).await;

    let mut cfg = AsrConfig::default();
    cfg.port = port;
    let be = AudioCppBackend::new(cfg, PathBuf::from("Z:/fake-model"), PathBuf::from("."));

    let wav = std::env::temp_dir().join("nexus-fake.wav");
    std::fs::write(&wav, b"RIFF").unwrap();

    let text = be.transcribe_span(&wav, "上一句", "Japanese").await.unwrap();
    assert_eq!(text, "こんにちは", "结果要 trim");

    let reqs = st.sink.lock().unwrap().clone();
    assert_eq!(reqs.len(), 1, "没有复读就该只请求一次");
    let r = &reqs[0];
    assert_eq!(r["model"], "qwen3-asr-stream");
    assert_eq!(r["language"], "Japanese");
    assert_eq!(r["context"], "上一句");
    assert!(r["audio"].as_str().unwrap().ends_with("nexus-fake.wav"));
}

#[tokio::test]
async fn audiocpp_retries_without_context_on_echo() {
    let st: Arc<FakeState> = Arc::default();
    let app = Router::new()
        .route("/v1/audio/transcriptions", post(fake_audiocpp_echo))
        .with_state(st.clone());
    let port = spawn(app).await;

    let mut cfg = AsrConfig::default();
    cfg.port = port;
    let be = AudioCppBackend::new(cfg, PathBuf::from("Z:/fake-model"), PathBuf::from("."));
    let wav = std::env::temp_dir().join("nexus-fake2.wav");
    std::fs::write(&wav, b"RIFF").unwrap();

    let text = be.transcribe_span(&wav, "上一句", "Japanese").await.unwrap();
    assert_eq!(text, "本当のテキスト", "复读后应改用无热词重试");
    assert_eq!(st.calls.load(Ordering::SeqCst), 2);
    let reqs = st.sink.lock().unwrap().clone();
    assert_eq!(reqs.len(), 2);
    assert!(reqs[0].get("context").is_some(), "第一次带热词");
    assert!(reqs[1].get("context").is_none(), "重试不带热词");
}

#[tokio::test]
async fn llama_chat_completions_shape() {
    let st: Arc<FakeState> = Arc::default();
    *st.reply.lock().unwrap() = "你好，今天天气不错".to_string();
    let app = Router::new()
        .route("/health", get(|| async { Json(json!({"status":"ok"})) }))
        .route("/v1/chat/completions", post(fake_llama))
        .with_state(st.clone());
    let port = spawn(app).await;

    let mut cfg = TranslateConfig::default();
    cfg.local_base_url = format!("http://127.0.0.1:{port}");
    let be = LlamaBackend::new(cfg);

    assert!(be.probe().await, "/health 应报告就绪");
    let out = be
        .translate_one("系统提示", "将日文翻译成中文：こんにちは")
        .await
        .unwrap();
    assert_eq!(out, "你好，今天天气不错");

    let reqs = st.sink.lock().unwrap().clone();
    let r = &reqs[0];
    assert_eq!(r["messages"][0]["role"], "system");
    assert_eq!(r["messages"][0]["content"], "系统提示");
    assert_eq!(r["messages"][1]["role"], "user");
    assert_eq!(r["temperature"], 0.1);
    assert_eq!(r["top_p"], 0.3);
    assert_eq!(r["max_tokens"], 256, "MT 模式必须收窄，提示词回显会拖成长循环");
}

#[tokio::test]
async fn translate_batch_end_to_end_keeps_order() {
    let st: Arc<FakeState> = Arc::default();
    *st.reply.lock().unwrap() = "译文".to_string();
    let app = Router::new()
        // 本地翻译路径会先 ensure_server（探活 /health）再发请求
        .route("/health", get(|| async { Json(json!({"status":"ok"})) }))
        .route("/v1/chat/completions", post(fake_llama))
        .with_state(st.clone());
    let port = spawn(app).await;

    let mut cfg = TranslateConfig::default();
    cfg.local_base_url = format!("http://127.0.0.1:{port}");
    cfg.thread_num = 2;
    let be = Arc::new(LlamaBackend::new(cfg.clone()));
    let eng = TranslateEngine::new(cfg, be);

    let texts: Vec<String> = vec!["一".into(), "二".into(), "三".into()];
    let out = eng.translate_batch(&texts).await;
    assert_eq!(out.len(), 3);
    assert_eq!(
        out.iter().map(|r| r.text.as_str()).collect::<Vec<_>>(),
        vec!["一", "二", "三"]
    );
    for r in &out {
        assert!(r.error.is_empty());
        assert_eq!(r.translation, "译文");
    }
    assert_eq!(st.calls.load(Ordering::SeqCst), 3);
}

#[tokio::test]
async fn llama_reports_backend_failure_without_panicking() {
    let mut cfg = TranslateConfig::default();
    cfg.local_base_url = "http://127.0.0.1:59994".into();
    let be = LlamaBackend::new(cfg);
    assert!(be.translate_one("s", "u").await.is_err());
    assert!(!be.probe().await);
}

// ---------------------------------------------------------------- 流式

/// 假上游 ASR：回一段 SSE，两条 delta + done。
async fn fake_asr_sse(body: axum::body::Bytes) -> impl axum::response::IntoResponse {
    // 真实上游收的是 multipart WAV，这里只确认收到了内容
    assert!(body.len() > 44, "应收 multipart 体");
    let sse = concat!(
        "data: {\"type\":\"transcript.text.delta\",\"delta\":\"おはよう。\"}\n\n",
        "data: {\"type\":\"transcript.text.delta\",\"delta\":\"元気ですか。\"}\n\n",
        "data: {\"type\":\"transcript.text.done\",\"text\":\"おはよう。元気ですか。\"}\n\n",
        "data: [DONE]\n\n"
    );
    (
        [(axum::http::header::CONTENT_TYPE, "text/event-stream")],
        sse.to_string(),
    )
}

#[tokio::test]
async fn stream_bridge_emits_delta_then_done() {
    use futures_util::StreamExt;

    let app = Router::new().route("/v1/audio/transcriptions", post(fake_asr_sse));
    let port = spawn(app).await;

    let pcm = vec![0u8; 32000]; // 1s 的 16k s16le
    let stream = nexus_subtitle::stream::stream_transcribe(
        reqwest::Client::new(),
        format!("http://127.0.0.1:{port}"),
        "qwen3-asr".to_string(),
        pcm,
        "ja".to_string(),
        false, // 不翻译：本测试只验切句与事件
        0.0,
        None,
        10,
        None, // recent：单请求测试不需要跨块去重
    )
    .await;

    let lines: Vec<String> = stream.collect().await;
    let joined = lines.join("");

    assert!(joined.contains("\"type\":\"delta\""), "应有 delta 事件：{joined}");
    assert!(joined.contains("おはよう。"), "应带原文：{joined}");
    assert!(joined.contains("元気ですか。"), "第二句也应下发：{joined}");
    assert!(joined.contains("\"type\":\"done\""), "应有 done 事件：{joined}");
    assert!(joined.trim_end().ends_with("[DONE]"), "最后必须是 [DONE]：{joined}");

    // 时间必须递增且落在合理范围
    for line in &lines {
        let Some(rest) = line.strip_prefix("data: ") else { continue };
        let Ok(v) = serde_json::from_str::<Value>(rest.trim()) else { continue };
        if v["type"] == "delta" {
            let s = v["start_ms"].as_f64().unwrap();
            let e = v["end_ms"].as_f64().unwrap();
            assert!(e > s, "结束必须晚于开始：{v}");
            assert!(s >= 0.0, "起点不能为负：{v}");
        }
    }
}

#[tokio::test]
async fn stream_bridge_reports_upstream_down_without_panicking() {
    use futures_util::StreamExt;

    // 一个必定连不上的端口
    let stream = nexus_subtitle::stream::stream_transcribe(
        reqwest::Client::new(),
        "http://127.0.0.1:1".to_string(),
        "m".to_string(),
        vec![0u8; 32000],
        "ja".to_string(),
        false,
        0.0,
        None,
        10,
        None, // recent：单请求测试不需要跨块去重
    )
    .await;
    let joined: String = stream.collect::<Vec<String>>().await.join("");
    assert!(joined.contains("\"type\":\"error\""), "应报错：{joined}");
    assert!(joined.contains("[DONE]"), "出错也要收尾：{joined}");
}
