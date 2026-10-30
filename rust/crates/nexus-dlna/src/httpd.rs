//! DLNA HTTP 服务（axum）：设备描述、SCPD、媒体流（Range）、字幕、脚本索引、SOAP。

use std::collections::HashMap;
use std::sync::Arc;

use axum::body::Body;
use axum::extract::{Path as AxPath, Query, State};
use axum::http::{header, HeaderMap, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::Router;
use serde_json::json;
use tokio::io::AsyncSeekExt as _;

use crate::app::DlnaApp;

pub type Shared = Arc<DlnaApp>;

fn xml(body: String) -> Response {
    (
        StatusCode::OK,
        [(header::CONTENT_TYPE, "text/xml; charset=\"utf-8\"")],
        body,
    )
        .into_response()
}

pub async fn description(State(app): State<Shared>) -> Response {
    xml(app.device_description())
}
pub async fn cds(State(app): State<Shared>) -> Response {
    xml(app.cds_scpd().to_string())
}
pub async fn cm(State(app): State<Shared>) -> Response {
    xml(app.cm_scpd().to_string())
}

/// `/media/<key>`：支持 Range 的流式读取（DeoVR 拖进度靠它）。
/// `.strm` 指针文件走代理转发：读文件里的 URL 透传给客户端（对齐 Python
/// `_proxy_strm`）——不转发的话客户端拿到的是一行 URL 文本，播放必失败。
pub async fn media(
    State(app): State<Shared>,
    AxPath(key): AxPath<String>,
    headers: HeaderMap,
) -> Response {
    let Some(path) = app.library.key_to_path(&key) else {
        return (StatusCode::NOT_FOUND, "not found").into_response();
    };
    if path.extension().and_then(|e| e.to_str()).map(|e| e.eq_ignore_ascii_case("strm")).unwrap_or(false) {
        return proxy_strm(&path, &headers).await;
    }
    let total = match tokio::fs::metadata(&path).await {
        Ok(m) if m.is_file() => m.len(),
        _ => return (StatusCode::NOT_FOUND, "not found").into_response(),
    };
    if total == 0 {
        // 空文件：直接 200 + Content-Length: 0。不进 Range 逻辑，
        // 否则无 Range 时会按 (0,0) 谎报 1 字节、带 Range 时又误答 416。
        let ctype = crate::library::mime_for(&path);
        let mut h = HeaderMap::new();
        h.insert(header::CONTENT_TYPE, HeaderValue::from_static(ctype));
        h.insert(header::ACCEPT_RANGES, HeaderValue::from_static("bytes"));
        h.insert(header::CONTENT_LENGTH, HeaderValue::from(0u64));
        return (StatusCode::OK, h, Body::empty()).into_response();
    }

    let range_hdr = headers.get(header::RANGE).and_then(|v| v.to_str().ok());
    let rng = match crate::library::parse_range_header(range_hdr, total) {
        Ok(r) => r,
        Err(()) => {
            return (
                StatusCode::RANGE_NOT_SATISFIABLE,
                [(header::CONTENT_RANGE, format!("bytes */{total}"))],
                "",
            )
                .into_response();
        }
    };
    let (start, end) = rng.unwrap_or((0, total.saturating_sub(1)));
    let length = end.saturating_sub(start) + 1;
    let ctype = crate::library::mime_for(&path);
    let pn = crate::library::dlna_pn_for(ctype);

    let mut f = match tokio::fs::File::open(&path).await {
        Ok(f) => f,
        Err(e) => return (StatusCode::NOT_FOUND, e.to_string()).into_response(),
    };
    if start > 0 && f.seek(std::io::SeekFrom::Start(start)).await.is_err() {
        return (StatusCode::INTERNAL_SERVER_ERROR, "seek failed").into_response();
    }

    let status = if rng.is_some() {
        StatusCode::PARTIAL_CONTENT
    } else {
        StatusCode::OK
    };
    let mut h = HeaderMap::new();
    h.insert(header::CONTENT_TYPE, HeaderValue::from_static(ctype));
    h.insert(header::ACCEPT_RANGES, HeaderValue::from_static("bytes"));
    h.insert(header::CONTENT_LENGTH, HeaderValue::from(length));
    if let Ok(v) = HeaderValue::from_str("Streaming") {
        h.insert("transferMode.dlna.org", v);
    }
    if let Ok(v) = HeaderValue::from_str(&format!(
        "DLNA.ORG_PN={pn};DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS={}",
        crate::library::DLNA_FLAGS
    )) {
        h.insert("contentFeatures.dlna.org", v);
    }
    if rng.is_some() {
        if let Ok(v) = HeaderValue::from_str(&format!("bytes {start}-{end}/{total}")) {
            h.insert(header::CONTENT_RANGE, v);
        }
    }

    // 精确截断到 Range 长度：不多吐字节，Content-Length 才可信
    let limited = tokio::io::AsyncReadExt::take(f, length);
    let stream = tokio_util::io::ReaderStream::new(limited);
    let body = Body::from_stream(stream);
    (status, h, body).into_response()
}

/// `.strm` 代理：读首行 URL，向上游透传 Range、向客户端透传状态/长度/类型。
async fn proxy_strm(path: &std::path::Path, headers: &HeaderMap) -> Response {
    let text = match tokio::fs::read_to_string(path).await {
        Ok(t) => t,
        Err(e) => return (StatusCode::NOT_FOUND, format!("strm 读取失败：{e}")).into_response(),
    };
    let url = text.lines().next().unwrap_or("").trim().to_string();
    if !(url.starts_with("http://") || url.starts_with("https://")) {
        return (StatusCode::NOT_FOUND, "strm 里的不是 http(s) 地址").into_response();
    }
    let client = match reqwest::Client::builder()
        .connect_timeout(std::time::Duration::from_secs(10))
        .build()
    {
        Ok(c) => c,
        Err(e) => return (StatusCode::BAD_GATEWAY, format!("建客户端失败：{e}")).into_response(),
    };
    let mut req = client.get(&url);
    if let Some(range) = headers.get(header::RANGE).and_then(|v| v.to_str().ok()) {
        req = req.header(header::RANGE, range);
    }
    let resp = match req.send().await {
        Ok(r) => r,
        Err(e) => return (StatusCode::BAD_GATEWAY, format!("strm 上游失败：{e}")).into_response(),
    };
    let status = StatusCode::from_u16(resp.status().as_u16())
        .unwrap_or(StatusCode::BAD_GATEWAY);
    let mut h = HeaderMap::new();
    h.insert(header::CONTENT_TYPE, HeaderValue::from_static("video/mp4"));
    h.insert(header::ACCEPT_RANGES, HeaderValue::from_static("bytes"));
    for key in [header::CONTENT_LENGTH, header::CONTENT_RANGE] {
        if let Some(v) = resp.headers().get(&key).and_then(|v| v.to_str().ok()) {
            if let Ok(hv) = HeaderValue::from_str(v) {
                h.insert(&key, hv);
            }
        }
    }
    let status = if resp.status() == reqwest::StatusCode::PARTIAL_CONTENT {
        StatusCode::PARTIAL_CONTENT
    } else {
        status
    };
    let body = Body::from_stream(resp.bytes_stream());
    (status, h, body).into_response()
}

/// `/subs/<key>`：字幕原样输出。
pub async fn subtitle(State(app): State<Shared>, AxPath(key): AxPath<String>) -> Response {
    let Some(path) = app.library.key_to_path(&key) else {
        return (StatusCode::NOT_FOUND, "not found").into_response();
    };
    match tokio::fs::read(&path).await {
        Ok(bytes) => (
            StatusCode::OK,
            [(header::CONTENT_TYPE, crate::library::subtitle_mime(&path))],
            bytes,
        )
            .into_response(),
        Err(_) => (StatusCode::NOT_FOUND, "not found").into_response(),
    }
}

/// `/scripts?base=<key>`：一次性导出 .funscript 索引（远程脚本源用）。
pub async fn scripts(
    State(app): State<Shared>,
    Query(q): Query<HashMap<String, String>>,
) -> Response {
    let base = q.get("base").cloned().unwrap_or_default();
    // collect_scripts 是同步递归遍历（上限 20000 项，网盘可达分钟级）：
    // 放阻塞线程池，别占 tokio worker 拖慢 /media 流
    let lib = Arc::clone(&app.library);
    let collected = match tokio::task::spawn_blocking(move || lib.collect_scripts(&base, 20000)).await {
        Ok(v) => v,
        Err(e) => {
            // JoinError（panic/取消）：如实报错，不能谎报 ok:true + 空列表
            return axum::Json(
                json!({"ok": false, "error": format!("脚本索引任务失败：{e}")}),
            )
            .into_response();
        }
    };
    let items: Vec<_> = collected
        .into_iter()
        .map(|(name, key)| {
            json!({
                "name": name,
                "url": format!("{}/media/{}", app.base_url(), urlencoding::encode(&key))
            })
        })
        .collect();
    axum::Json(json!({"ok": true, "scripts": items})).into_response()
}

// ---------------------------------------------------------------- SOAP

/// ContentDirectory 实际支持的 action 白名单（与 app.rs 的 CDS_SCPD actionList 一致）。
const CDS_ACTIONS: &[&str] = &[
    "Browse",
    "GetSortCapabilities",
    "GetSearchCapabilities",
    "GetSystemUpdateID",
];
/// ConnectionManager 实际支持的 action 白名单（与 app.rs 的 CM_SCPD actionList 一致）。
const CM_ACTIONS: &[&str] = &[
    "GetProtocolInfo",
    "GetCurrentConnectionIDs",
    "GetCurrentConnectionInfo",
];

fn soap_action(body: &str) -> String {
    if let Some(start) = body.find("<u:") {
        let rest = &body[start + 3..];
        let end = rest
            .find(|c: char| c.is_whitespace() || c == '>' || c == '/')
            .unwrap_or(rest.len());
        return rest[..end].to_string();
    }
    // 回退：部分客户端用 `<m:Browse>` 等任意命名空间前缀（Python 的备用正则同义）。
    let mut rest = body;
    while let Some(i) = rest.find('<') {
        let after = &rest[i + 1..];
        if after.starts_with(['?', '!', '/']) {
            rest = after;
            continue;
        }
        let end = after
            .find(|c: char| c.is_whitespace() || c == '>' || c == '/')
            .unwrap_or(after.len());
        let tag = &after[..end];
        if let Some((prefix, name)) = tag.split_once(':') {
            if prefix != "s" && !prefix.eq_ignore_ascii_case("SOAP-ENV") {
                return name.to_string();
            }
        } else if !tag.eq_ignore_ascii_case("Envelope") && !tag.eq_ignore_ascii_case("Body") {
            return tag.to_string();
        }
        rest = after;
    }
    String::new()
}

fn soap_arg(body: &str, name: &str) -> String {
    let open = format!("<{name}>");
    let close = format!("</{name}>");
    if let Some(s) = body.find(&open) {
        let rest = &body[s + open.len()..];
        if let Some(e) = rest.find(&close) {
            return xml_unescape(&rest[..e]);
        }
    }
    String::new()
}

fn xml_unescape(s: &str) -> String {
    s.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", "\"")
        .replace("&apos;", "'")
        .replace("&amp;", "&")
}

/// 拼成功响应。**`action` 只能传白名单内的字面量**（客户端字符串一律先过
/// CDS_ACTIONS/CM_ACTIONS 校验），防止任意字符串注入响应 XML。
fn soap_response(action: &str, service: &str, inner: &str) -> Response {
    let body = format!(
        "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n<s:Envelope xmlns:s=\"http://schemas.xmlsoap.org/soap/envelope/\" s:encodingStyle=\"http://schemas.xmlsoap.org/soap/encoding/\"><s:Body><u:{action}Response xmlns:u=\"urn:schemas-upnp-org:service:{service}:1\">{inner}</u:{action}Response></s:Body></s:Envelope>"
    );
    let mut h = HeaderMap::new();
    h.insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("text/xml; charset=\"utf-8\""),
    );
    h.insert("EXT", HeaderValue::from_static(""));
    (StatusCode::OK, h, body).into_response()
}

fn soap_fault(code: u32, desc: &str) -> Response {
    let body = format!(
        "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n<s:Envelope xmlns:s=\"http://schemas.xmlsoap.org/soap/envelope/\" s:encodingStyle=\"http://schemas.xmlsoap.org/soap/encoding/\"><s:Body><s:Fault><faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring><detail><UPnPError xmlns=\"urn:schemas-upnp-org:control-1-0\"><errorCode>{code}</errorCode><errorDescription>{desc}</errorDescription></UPnPError></detail></s:Fault></s:Body></s:Envelope>"
    );
    (
        StatusCode::INTERNAL_SERVER_ERROR,
        [(header::CONTENT_TYPE, "text/xml; charset=\"utf-8\"")],
        body,
    )
        .into_response()
}

pub async fn control_cds(State(app): State<Shared>, body: String) -> Response {
    // 提取自客户端报文的 action 先过白名单：不在集合内直接 fault，
    // 杜绝任意字符串拼进响应 XML。（解析一次存变量，不重复扫报文）
    let action = soap_action(&body);
    if !CDS_ACTIONS.contains(&action.as_str()) {
        return soap_fault(401, "Invalid Action");
    }
    match action.as_str() {
        "Browse" => {
            let obj_id = soap_arg(&body, "ObjectID");
            let flag = soap_arg(&body, "BrowseFlag");
            let start: usize = soap_arg(&body, "StartingIndex").parse().unwrap_or(0);
            let count: usize = soap_arg(&body, "RequestedCount").parse().unwrap_or(0);
            // browse 内部是同步目录枚举（网盘上可达秒级）：放阻塞线程池，
            // 别拖慢同一 axum 服务上的 /media 流响应调度
            let app2 = Arc::clone(&app);
            let flag2 = flag.clone();
            let (result, mut returned, mut total) =
                tokio::task::spawn_blocking(move || app2.browse(&obj_id, &flag2, start, count))
                    .await
                    .unwrap_or_else(|_| {
                        // JoinError（browse panic/任务取消）：回格式良好的空 DIDL，
                        // 空串 Result 会让 DeoVR 的 LoadXml 直接崩
                        (crate::app::empty_didl(), 0, 0)
                    });
            if flag == "BrowseMetadata" {
                returned = usize::from(total > 0);
                total = returned;
            }
            // DIDL 作为 XML 文本再嵌进 SOAP：必须转义
            let escaped = crate::app::xml_escape(&result);
            let inner = format!(
                "<Result>{escaped}</Result><NumberReturned>{returned}</NumberReturned><TotalMatches>{total}</TotalMatches><UpdateID>0</UpdateID>"
            );
            soap_response("Browse", "ContentDirectory", &inner)
        }
        "GetSortCapabilities" => soap_response(
            "GetSortCapabilities",
            "ContentDirectory",
            "<SortCaps></SortCaps>",
        ),
        "GetSearchCapabilities" => soap_response(
            "GetSearchCapabilities",
            "ContentDirectory",
            "<SearchCaps></SearchCaps>",
        ),
        "GetSystemUpdateID" => {
            soap_response("GetSystemUpdateID", "ContentDirectory", "<Id>0</Id>")
        }
        _ => soap_fault(401, "Invalid Action"),
    }
}

pub async fn control_cm(body: String) -> Response {
    // 同 control_cds：白名单前置校验（解析一次）
    let action = soap_action(&body);
    if !CM_ACTIONS.contains(&action.as_str()) {
        return soap_fault(401, "Invalid Action");
    }
    match action.as_str() {
        "GetProtocolInfo" => soap_response(
            "GetProtocolInfo",
            "ConnectionManager",
            "<Source></Source><Sink></Sink>",
        ),
        "GetCurrentConnectionIDs" => soap_response(
            "GetCurrentConnectionIDs",
            "ConnectionManager",
            "<ConnectionIDs>0</ConnectionIDs>",
        ),
        "GetCurrentConnectionInfo" => soap_response(
            "GetCurrentConnectionInfo",
            "ConnectionManager",
            "<RcsID>0</RcsID><AVTransportID>0</AVTransportID><ProtocolInfo></ProtocolInfo><PeerConnectionManager></PeerConnectionManager><PeerConnectionID>-1</PeerConnectionID><Direction>Output</Direction><Status>OK</Status>",
        ),
        _ => soap_fault(401, "Invalid Action"),
    }
}

pub fn router(app: Shared) -> Router {
    Router::new()
        .route("/description.xml", get(description))
        .route("/cds.xml", get(cds))
        .route("/cm.xml", get(cm))
        .route("/media/{*key}", get(media))
        .route("/subs/{*key}", get(subtitle))
        .route("/scripts", get(scripts))
        .route("/control/cds", post(control_cds))
        .route("/control/cm", post(control_cm))
        .with_state(app)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn soap_action_extraction() {
        let body = r#"<u:Browse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1"><ObjectID>0</ObjectID></u:Browse>"#;
        assert_eq!(soap_action(body), "Browse");
        assert_eq!(soap_arg(body, "ObjectID"), "0");
        assert_eq!(soap_arg(body, "Missing"), "");
    }

    #[test]
    fn soap_action_accepts_other_prefixes() {
        let body = r#"<s:Envelope xmlns:s="x"><s:Body><m:Browse xmlns:m="urn:schemas-upnp-org:service:ContentDirectory:1"><ObjectID>0</ObjectID></m:Browse></s:Body></s:Envelope>"#;
        assert_eq!(soap_action(body), "Browse");
    }

    #[test]
    fn soap_arg_unescapes_entities() {
        // 客户端把名字里的 & 发成 &amp;，不反转义就会去查一个不存在的文件名
        let body = "<ObjectID>F:a &amp; b</ObjectID>";
        assert_eq!(soap_arg(body, "ObjectID"), "F:a & b");
    }
}
