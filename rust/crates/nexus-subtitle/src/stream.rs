//! 流式转写桥：把整块 PCM 推给 audiocpp，边收增量边切句、边翻译、边下发。
//!
//! 逐条对齐 `vendor/subtitle/stream_bridge.py`。三条要点：
//!
//! 1. **时间必须按字数比例摊**：早期版本按固定 130ms/字，会把长块里的句子全压在块首
//!    （30s 块能错 15s+，与画面完全对不上）。比例摊法保证"句子落在自己块里"永远成立。
//! 2. **游标只前进不回头**：整个请求共用一个 `cursor_ms`。各 delta 若从块首重算，
//!    同一块内多条句子的区间会互相重叠。
//! 3. **攒批翻译**：句子先入队，满 `batch_size` 或流结束才整批交给翻译引擎（批量远比逐句快）。

use serde_json::{json, Value};

use crate::config::SubtitleConfig;

/// 句末标点：命中即切。
const SENT_END: &str = "。！？!?…♪";
/// 停顿标点：只有超长时才在它后面切一刀。
const PAUSE_END: &str = "、，,；;";
/// 引号类包裹符号，首尾剥掉（中间不动）。
const WRAP_QUOTES: &str = "\"'「」『』“”‘’（）()【】[]";
/// 合法拖长音：这些字符连排不算退化（对齐 Python `text_filters._LENIENT_REPEAT`，
/// 缺了半角ｰ/〰/‥/· 会把合法连排误判成退化丢弃）。
const LENIENT_REPEAT: &str = "ーｰ〜〰…‥·";

/// 实测的整体提前量补偿（毫秒）。
pub const LABEL_OFFSET_MS: f64 = 2500.0;
/// 上游帧间空闲上限（秒）。
pub const STREAM_IDLE_SEC: f64 = 120.0;
/// 最短显示时长（毫秒）：更短的字幕是"闪一下"。
pub const MIN_DISPLAY_MS: f64 = 1500.0;

/// 剥掉首尾引号类符号。
pub fn strip_wrap_quotes(text: &str) -> String {
    text.trim()
        .trim_matches(|c: char| WRAP_QUOTES.contains(c))
        .trim()
        .to_string()
}

/// 同一字符连续重复过多 → 小模型死循环退化。
///
/// `max_run=3`：4 连基本可判退化（喘息/拟声会把译文放大成几十个"啊"）；
/// 但 `ー`/`〜`/`…` 是日语的合法拖长音，放宽到 15。
pub fn has_repetition_loop(text: &str) -> bool {
    let chars: Vec<char> = text.chars().collect();
    let mut run = 1usize;
    for i in 1..chars.len() {
        if chars[i] == chars[i - 1] {
            run += 1;
            let limit = if LENIENT_REPEAT.contains(chars[i]) { 15 } else { 3 };
            if run > limit {
                return true;
            }
        } else {
            run = 1;
        }
    }
    false
}

/// 把引擎给的原始增量切成句子。
///
/// 不切的话会出现"半句话就换行"（ASR 的一整段约 5s 才吐一次）。
pub fn split_sentences(text: &str, max_chars: usize) -> Vec<String> {
    let mut out = Vec::new();
    let mut cur = String::new();
    for ch in text.chars() {
        cur.push(ch);
        let n = cur.chars().count();
        if SENT_END.contains(ch) {
            let t = cur.trim();
            if !t.is_empty() {
                out.push(t.to_string());
            }
            cur.clear();
        } else if n >= max_chars && PAUSE_END.contains(ch) {
            let t = cur.trim();
            if !t.is_empty() {
                out.push(t.to_string());
            }
            cur.clear();
        } else if n >= max_chars * 2 {
            let t = cur.trim();
            if !t.is_empty() {
                out.push(t.to_string());
            }
            cur.clear();
        }
    }
    let t = cur.trim();
    if !t.is_empty() {
        out.push(t.to_string());
    }
    out
}

/// 待下发的一条字幕。
#[derive(Debug, Clone, PartialEq)]
pub struct Line {
    pub ja: String,
    pub start_ms: f64,
    pub end_ms: f64,
}

/// 时间分摊器：把"一块音频的时长"按字数摊到各句上。
pub struct Segmenter {
    cursor_ms: f64,
    video_start_ms: f64,
    block_ms: f64,
    max_chars: usize,
    recent: Vec<String>,
    /// 流式桥无时间戳，用 2500ms 补偿；块级 `/transcribe` 有 HybridBuffer 起点，必须传 0。
    label_offset_ms: f64,
}

impl Segmenter {
    pub fn new(video_start_ms: f64, block_ms: f64) -> Self {
        Self::new_with_offset(video_start_ms, block_ms, LABEL_OFFSET_MS)
    }

    pub fn new_with_offset(video_start_ms: f64, block_ms: f64, offset_ms: f64) -> Self {
        Self {
            cursor_ms: video_start_ms,
            video_start_ms,
            block_ms,
            max_chars: 42,
            recent: Vec::new(),
            label_offset_ms: offset_ms,
        }
    }

    /// 吃一段增量，吐出若干条定时好的字幕。
    pub fn push(&mut self, delta: &str) -> Vec<Line> {
        // 先切句 + 清洗 + 去碎片
        let mut kept: Vec<String> = Vec::new();
        for raw in split_sentences(delta, self.max_chars) {
            let s = strip_wrap_quotes(&raw);
            if s.is_empty() {
                continue;
            }
            if has_repetition_loop(&s) {
                continue;
            }
            // 上一块 / 本批句子的前缀碎片（重叠区重复识别）
            let dup_prev = self.recent.iter().any(|r| r.len() > s.len() && r.contains(&s));
            let dup_keep = kept.iter().any(|k| k.len() > s.len() && k.contains(&s));
            if dup_prev || dup_keep {
                continue;
            }
            kept.push(s);
        }
        if kept.is_empty() {
            return Vec::new();
        }

        let total_chars: usize = kept.iter().map(|s| s.chars().count()).sum::<usize>().max(1);
        let block_end = self.video_start_ms + self.block_ms;
        let mut out = Vec::with_capacity(kept.len());
        for s in kept {
            let n = s.chars().count();
            let dur = self.block_ms * n as f64 / total_chars as f64;
            self.cursor_ms = (self.cursor_ms + dur).min(block_end);
            let mut s0 = self.cursor_ms - dur + self.label_offset_ms;
            let mut s1 = self.cursor_ms + self.label_offset_ms;
            if s1 <= s0 {
                s1 = s0 + 400.0;
            }
            // 最短显示 1.5s：向后借时间，但不越过块尾+偏移
            if dur < MIN_DISPLAY_MS
                && s1 - s0 < MIN_DISPLAY_MS
                && s1 < block_end + self.label_offset_ms
            {
                s1 = (block_end + self.label_offset_ms).min(s0 + MIN_DISPLAY_MS);
            }
            s0 = s0.max(0.0);
            self.recent.push(s.clone());
            // 对齐 Python `_recent_ja` 的 maxlen=6：窗口太大会把真重复短句误杀
            if self.recent.len() > 6 {
                self.recent.remove(0);
            }
            out.push(Line {
                ja: s,
                start_ms: s0,
                end_ms: s1,
            });
        }
        out
    }

    pub fn block_ms(&self) -> f64 {
        self.block_ms
    }

    /// 跨块去重参照：由调用方在请求间保存（头显相邻块带 1s 重叠，
    /// 只活在本请求内的参照拦不住上一块句子的前缀碎片重复上屏）。
    pub fn set_recent(&mut self, recent: Vec<String>) {
        self.recent = recent;
    }

    pub fn take_recent(&mut self) -> Vec<String> {
        std::mem::take(&mut self.recent)
    }
}

/// 给裸 PCM 套 44 字节 WAV 头（上游只接受 WAV 容器，裸 PCM 会被 400 拒）。
pub fn wav_wrap(pcm: &[u8], sr: u32, ch: u16, bits: u16) -> Vec<u8> {
    let n = pcm.len() as u32;
    let byte_rate = sr * ch as u32 * bits as u32 / 8;
    let block_align = ch * bits / 8;
    let mut out = Vec::with_capacity(44 + pcm.len());
    out.extend_from_slice(b"RIFF");
    out.extend_from_slice(&(36 + n).to_le_bytes());
    out.extend_from_slice(b"WAVEfmt ");
    out.extend_from_slice(&16u32.to_le_bytes());
    out.extend_from_slice(&1u16.to_le_bytes()); // PCM
    out.extend_from_slice(&ch.to_le_bytes());
    out.extend_from_slice(&sr.to_le_bytes());
    out.extend_from_slice(&byte_rate.to_le_bytes());
    out.extend_from_slice(&block_align.to_le_bytes());
    out.extend_from_slice(&bits.to_le_bytes());
    out.extend_from_slice(b"data");
    out.extend_from_slice(&n.to_le_bytes());
    out.extend_from_slice(pcm);
    out
}

/// 组装 multipart 表单（字段名以 audiocpp 自报的路由为准：file/model/language/prompt/stream）。
pub fn multipart(wav: &[u8], model: &str, language: &str) -> (Vec<u8>, String) {
    // boundary 每次随机：固定串撞上音频字节流会让服务端解析错乱（Python 同为 uuid）
    static BOUNDARY_SEQ: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    let boundary = format!(
        "----nexus-subtitle-{}-{}",
        std::process::id(),
        BOUNDARY_SEQ.fetch_add(1, std::sync::atomic::Ordering::Relaxed)
    );
    let mut body = Vec::with_capacity(wav.len() + 512);
    for (name, value) in [("model", model), ("language", language), ("stream", "true")] {
        if value.is_empty() {
            continue;
        }
        body.extend_from_slice(format!("--{boundary}\r\n").as_bytes());
        body.extend_from_slice(
            format!("Content-Disposition: form-data; name=\"{name}\"\r\n\r\n").as_bytes(),
        );
        body.extend_from_slice(value.as_bytes());
        body.extend_from_slice(b"\r\n");
    }
    body.extend_from_slice(format!("--{boundary}\r\n").as_bytes());
    body.extend_from_slice(
        b"Content-Disposition: form-data; name=\"file\"; filename=\"audio.wav\"\r\n",
    );
    body.extend_from_slice(b"Content-Type: audio/wav\r\n\r\n");
    body.extend_from_slice(wav);
    body.extend_from_slice(format!("\r\n--{boundary}--\r\n").as_bytes());
    (body, boundary.to_string())
}

/// 语言键 → 上游要求的语言名（空串表示不发该字段）。
pub fn lang_name(lang: &str) -> &'static str {
    match lang {
        "ja" | "jp" => "Japanese",
        "en" => "English",
        "zh" | "cn" => "Chinese",
        "ko" => "Korean",
        _ => "",
    }
}

/// SSE 一行。
/// 上游错误可能是 {"error": "文本"} 或 {"error": {"message": "文本"}} 两种形态：
/// 只认 dict 会把字符串形态的真实错误覆盖成兜底文案。
fn upstream_error_text(ev: &Value, fallback: &str) -> String {
    match ev.get("error") {
        Some(Value::String(t)) => {
            let t = t.trim();
            if t.is_empty() { fallback.to_string() } else { t.to_string() }
        }
        Some(e) => e
            .get("message")
            .and_then(|m| m.as_str())
            .map(str::to_string)
            .unwrap_or_else(|| fallback.to_string()),
        None => fallback.to_string(),
    }
}

pub fn sse(v: &Value) -> String {
    format!("data: {}\n\n", v)
}

/// delta 事件。
pub fn delta_event(line: &Line, zh: &str, first_ms: Option<f64>) -> String {
    sse(&json!({
        "type": "delta",
        "ja": line.ja,
        "zh": zh,
        "first_ms": first_ms,
        "start_ms": line.start_ms,
        "end_ms": line.end_ms,
    }))
}

/// 攒批阈值（与翻译引擎的批量大小同源）。
pub fn batch_size(cfg: &SubtitleConfig) -> usize {
    cfg.translate.batch_size.max(1)
}


// ---------------------------------------------------------------- 流式桥

use futures_util::Stream;
use std::pin::Pin;
use std::task::{Context, Poll};
use tokio::sync::mpsc;

/// 跨块去重参照（请求间共享）：`(语言键, 最近几句)`。
/// 头显相邻块带 1s 重叠，参照只活在本请求内拦不住上一块句子的碎片重复上屏。
pub type RecentStore = std::sync::Arc<std::sync::Mutex<(String, Vec<String>)>>;

/// rx 断开（客户端断开）时中断上游任务：否则任务要等满上游空闲/翻译超时
/// 才靠 send 失败自杀，期间可能继续读流并白烧一次批翻译（Python F15 兜底）。
struct AbortOnDrop<S> {
    inner: S,
    handle: tokio::task::JoinHandle<()>,
}

impl<S: Stream + Unpin> Stream for AbortOnDrop<S> {
    type Item = S::Item;
    fn poll_next(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        Pin::new(&mut self.inner).poll_next(cx)
    }
}

impl<S> Drop for AbortOnDrop<S> {
    fn drop(&mut self) {
        self.handle.abort();
    }
}

/// 流式转写：把整块 PCM 推给 audiocpp，边收增量边切句、边翻译、边下发 SSE 行。
///
/// 与 Python `stream_bridge.transcribe_stream` 同语义。上游连接、逐块读取全在
/// 独立任务里跑——阻塞读会卡住整个事件循环（Python 那边是靠线程池绕开的）。
#[allow(clippy::too_many_arguments)]
pub async fn stream_transcribe(
    client: reqwest::Client,
    asr_base: String,
    model: String,
    pcm: Vec<u8>,
    lang: String,
    translate: bool,
    video_start_ms: f64,
    translator: Option<std::sync::Arc<crate::translate::TranslateEngine>>,
    batch: usize,
    recent: Option<RecentStore>,
) -> impl Stream<Item = String> {
    let (tx, rx) = mpsc::channel::<String>(64);

    let handle = tokio::spawn(async move {
        // 短音频直接收尾（少于 3200 字节 ≈ 100ms 16k s16le，不值得上模型）
        if pcm.len() < 3200 {
            let _ = tx.send(sse(&json!({"type": "done"}))).await;
            let _ = tx.send("data: [DONE]\n\n".to_string()).await;
            return;
        }
        let t0 = std::time::Instant::now();
        let block_ms = (pcm.len() / 2 * 1000 / 16000) as f64;
        let mut seg = Segmenter::new(video_start_ms, block_ms);
        let lang_key = lang.split('-').next().unwrap_or("").to_lowercase();
        // 语言没变才带入上一块的参照：换语言的参照只会误杀新语言的句子
        if let Some(store) = recent.as_ref() {
            let g = store.lock().unwrap();
            if g.0 == lang_key {
                seg.set_recent(g.1.clone());
            }
        }
        let mut pending: Vec<Line> = Vec::new();
        let mut first_ms: Option<f64> = None;

        // 主体收进内层块：任何出口（上游错误 / [DONE] / 客户端断开）都落回
        // 下面的 recent 写回，跨块参照不因中途退出而丢
        async {
        let wav = wav_wrap(&pcm, 16000, 1, 16);
        let lang_field = lang_name(&lang);
        let (body, boundary) = multipart(&wav, &model, lang_field);
        let url = format!("{}/v1/audio/transcriptions", asr_base.trim_end_matches('/'));

        let resp = client
            .post(&url)
            .header(
                "Content-Type",
                format!("multipart/form-data; boundary={boundary}"),
            )
            .header("Accept", "text/event-stream")
            .body(body)
            .send()
            .await;

        let mut resp = match resp {
            Ok(r) if r.status().is_success() => r,
            Ok(r) => {
                let _ = tx
                    .send(sse(&json!({"type": "error", "error": format!("upstream {}", r.status())})))
                    .await;
                let _ = tx.send("data: [DONE]\n\n".to_string()).await;
                return;
            }
            Err(e) => {
                let _ = tx
                    .send(sse(&json!({"type": "error", "error": format!("upstream: {e}")})))
                    .await;
                let _ = tx.send("data: [DONE]\n\n".to_string()).await;
                return;
            }
        };

        // 攒批翻译并下发。`client_gone`：客户端断开后不再白烧翻译 GPU。
        async fn flush(
            pending: &mut Vec<Line>,
            translator: &Option<std::sync::Arc<crate::translate::TranslateEngine>>,
            translate: bool,
            lang: &str,
            first_ms: Option<f64>,
            tx: &mpsc::Sender<String>,
            client_gone: &mut bool,
        ) {
            if pending.is_empty() {
                return;
            }
            if *client_gone {
                pending.clear();
                return;
            }
            let texts: Vec<String> = pending.iter().map(|l| l.ja.clone()).collect();
            let zhs: Vec<String> = if translate {
                match translator {
                    // 带语言键：英语片选 en 模型与英文提示词前缀
                    Some(tr) => tr
                        .translate_batch_for(&texts, lang)
                        .await
                        .into_iter()
                        .enumerate()
                        .map(|(i, r)| {
                            // 失败句译文留空不上屏，但必须留痕（否则整行字幕无声消失）
                            if !r.error.is_empty() {
                                tracing::warn!("[nexus-subtitle] 第 {} 句流式翻译失败：{}", i + 1, r.error);
                            }
                            r.translation
                        })
                        .collect(),
                    None => vec![String::new(); texts.len()],
                }
            } else {
                vec![String::new(); texts.len()]
            };
            for (i, line) in pending.iter().enumerate() {
                let zh = zhs.get(i).cloned().unwrap_or_default();
                if tx.send(delta_event(line, &zh, first_ms)).await.is_err() {
                    *client_gone = true;
                    break;
                }
            }
            pending.clear();
        }

        let mut client_gone = false;
        // 字节缓冲：SSE chunk 可能在多字节字符（日文）中间截断，
        // 逐 chunk from_utf8_lossy 会把跨块字符打散成乱码。按完整行解码。
        let mut buf: Vec<u8> = Vec::new();
        loop {
            let chunk = match tokio::time::timeout(
                std::time::Duration::from_secs_f64(STREAM_IDLE_SEC),
                resp.chunk(),
            )
            .await
            {
                Ok(Ok(Some(c))) => c,
                Ok(Ok(None)) => break,
                Ok(Err(e)) => {
                    if tx
                        .send(sse(&json!({"type": "error", "error": format!("读上游失败：{e}")})))
                        .await
                        .is_err()
                    {
                        client_gone = true;
                    }
                    break;
                }
                Err(_) => {
                    if tx
                        .send(sse(&json!({
                            "type": "error",
                            "error": format!("上游 {STREAM_IDLE_SEC}s 无数据（流式识别卡住）")
                        })))
                        .await
                        .is_err()
                    {
                        client_gone = true;
                    }
                    break;
                }
            };
            buf.extend_from_slice(&chunk);

            while let Some(pos) = buf.iter().position(|&b| b == b'\n') {
                let line = String::from_utf8_lossy(&buf[..pos]).trim().to_string();
                buf.drain(..=pos);
                let Some(data) = line.strip_prefix("data:") else {
                    continue;
                };
                let data = data.trim();
                if data == "[DONE]" {
                    flush(&mut pending, &translator, translate, &lang, first_ms, &tx, &mut client_gone).await;
                    let _ = tx.send("data: [DONE]\n\n".to_string()).await;
                    return;
                }
                let Ok(ev) = serde_json::from_str::<Value>(data) else {
                    continue;
                };
                let etype = ev.get("type").and_then(|v| v.as_str()).unwrap_or("");
                // 上游错误事件没有 type 字段，不识别就会被静默吞成"一句都没有"
                if etype.is_empty() && ev.get("error").is_some() {
                    let msg = upstream_error_text(&ev, "upstream error");
                    flush(&mut pending, &translator, translate, &lang, first_ms, &tx, &mut client_gone).await;
                    if client_gone {
                        break;
                    }
                    if tx.send(sse(&json!({"type": "error", "error": msg}))).await.is_err() {
                        return;
                    }
                    continue;
                }
                match etype {
                    "transcript.text.delta" => {
                        let delta = ev.get("delta").and_then(|v| v.as_str()).unwrap_or("");
                        if first_ms.is_none() && !delta.trim().is_empty() {
                            first_ms = Some(t0.elapsed().as_millis() as f64);
                        }
                        pending.extend(seg.push(delta));
                        if pending.len() >= batch {
                            flush(&mut pending, &translator, translate, &lang, first_ms, &tx, &mut client_gone).await;
                            if client_gone {
                                break;
                            }
                        }
                    }
                    "transcript.text.done" => {
                        flush(&mut pending, &translator, translate, &lang, first_ms, &tx, &mut client_gone).await;
                        if client_gone {
                            break;
                        }
                        if tx
                            .send(sse(&json!({
                                "type": "done",
                                "ja": ev.get("text").cloned().unwrap_or(Value::Null),
                                "total_ms": t0.elapsed().as_millis() as f64,
                            })))
                            .await
                            .is_err()
                        {
                            return;
                        }
                    }
                    "error" => {
                        let msg = upstream_error_text(&ev, "unknown");
                        flush(&mut pending, &translator, translate, &lang, first_ms, &tx, &mut client_gone).await;
                        if client_gone {
                            break;
                        }
                        if tx.send(sse(&json!({"type": "error", "error": msg}))).await.is_err() {
                            return;
                        }
                    }
                    _ => {}
                }
            }
            if client_gone {
                return;
            }
        }
        flush(&mut pending, &translator, translate, &lang, first_ms, &tx, &mut client_gone).await;
        let _ = tx.send("data: [DONE]\n\n".to_string()).await;
        }
        .await;

        // 无论走到哪个出口（含 client_gone 的 break）都把本块的去重参照写回
        // （换语言时从零重建）。已知限制：客户端断开触发 AbortOnDrop.abort 时，
        // 任务在 await 点被取消会跳过这里——最多丢当前块进行中的几句参照，
        // 重连后重叠区可能多去重失败一次，不丢已下发行。
        if let Some(store) = recent {
            let mut g = store.lock().unwrap();
            *g = (lang_key, seg.take_recent());
        }
    });

    let stream = tokio_stream::wrappers::ReceiverStream::new(rx);
    AbortOnDrop { inner: stream, handle }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 块级路径（有 HybridBuffer 起点）不能叠流式桥的 +2.5s 延迟补偿。
    #[test]
    fn block_segmenter_has_no_label_offset() {
        let mut seg = Segmenter::new_with_offset(10_000.0, 3_000.0, 0.0);
        let lines = seg.push("こんにちは。");
        assert!(!lines.is_empty());
        assert!(lines[0].start_ms < 12_000.0, "块级起点不应被推后 2.5s");
        assert!(lines[0].end_ms <= 13_000.0 + 1.0);
    }

    #[test]
    fn split_at_sentence_end() {
        let out = split_sentences("おはよう。元気？", 42);
        assert_eq!(out, vec!["おはよう。", "元気？"]);
    }

    #[test]
    fn split_long_without_sentence_end() {
        let s = "あ".repeat(60) + "、";
        let out = split_sentences(&s, 20);
        assert!(out.len() >= 2, "超长无句末标点应按停顿再切：{:?}", out.len());
    }

    #[test]
    fn strip_quotes_only_on_edges() {
        assert_eq!(strip_wrap_quotes("\"「やあ」\""), "やあ");
        // 中间的引号不动
        assert_eq!(strip_wrap_quotes("a\"b"), "a\"b");
    }

    #[test]
    fn repetition_loop_detected() {
        assert!(has_repetition_loop("あああああ"));
        assert!(!has_repetition_loop("あああ"));
        // 拖长音是合法的
        assert!(!has_repetition_loop("えーーーーーーっと"));
    }

    #[test]
    fn wav_header_is_44_bytes() {
        let pcm = vec![0u8; 100];
        let w = wav_wrap(&pcm, 16000, 1, 16);
        assert_eq!(w.len(), 144);
        assert_eq!(&w[0..4], b"RIFF");
        assert_eq!(&w[8..12], b"WAVE");
        assert_eq!(&w[36..40], b"data");
        assert_eq!(u32::from_le_bytes([w[40], w[41], w[42], w[43]]), 100);
    }

    #[test]
    fn time_is_allocated_by_char_share() {
        // 一块 10s 的音频里有两句，各 6 字 → 各占 5s
        // （用不重复的字，否则会被 has_repetition_loop 当退化丢掉）
        let mut seg = Segmenter::new(0.0, 10_000.0);
        let lines = seg.push("あいうえおか。きくけこさし。");
        assert_eq!(lines.len(), 2);
        let d0 = lines[0].end_ms - lines[0].start_ms;
        let d1 = lines[1].end_ms - lines[1].start_ms;
        assert!((d0 - 5000.0).abs() < 1.0, "第一句应占 5s，实际 {d0}");
        assert!((d1 - 5000.0).abs() < 1.0, "第二句应占 5s，实际 {d1}");
        // 游标单调：第二句起点不早于第一句终点减去偏移
        assert!(lines[1].start_ms >= lines[0].start_ms, "时间必须递增");
    }

    #[test]
    fn cursor_never_exceeds_block_end() {
        let mut seg = Segmenter::new(0.0, 1000.0);
        let lines = seg.push("あ。い。う。え。お。");
        for l in &lines {
            assert!(l.start_ms <= 1000.0 + LABEL_OFFSET_MS + 1.0, "越过块尾：{l:?}");
        }
    }

    #[test]
    fn duplicate_fragments_are_dropped() {
        let mut seg = Segmenter::new(0.0, 5000.0);
        // 不带句末标点，整段作为一句存下来
        let first = seg.push("こんにちは世界");
        assert_eq!(first.len(), 1);
        // 下一块把重叠区又识别了一遍，结果是上一句**更短的子串** → 丢掉。
        // 判据是"子串且更短"，所以等长重复**不会**被丢（那可能是真的重复台词）。
        let second = seg.push("こんにちは");
        assert!(second.is_empty(), "上一句的子串应被丢掉：{second:?}");
    }

    #[test]
    fn lang_name_mapping() {
        assert_eq!(lang_name("ja"), "Japanese");
        assert_eq!(lang_name("en"), "English");
        assert_eq!(lang_name("xx"), "");
    }
}