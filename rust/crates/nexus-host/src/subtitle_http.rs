//! 头显字幕服务：监听 **8756**，保持 Python `server_app.py` 的对外契约。
//!
//! 头显协议（docs/AI-SUBTITLE-STATUS.md §3）：
//! - `GET  /health`
//! - `POST /transcribe`       块级：PCM → 混合切句 → ASR → 翻译
//! - `POST /transcribe/stream` 流式：PCM → SSE（`delta/done/error/[DONE]`）
//!
//! Rust 版把识别/翻译调度放在宿主进程内，但**端口与协议不变**：
//! 头显零改动，仍按 `http://<PC-IP>:8756/...` 推流。其它路径一律 403
//! （Python 的回环门卫语义）。

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use axum::body::Bytes;
use axum::extract::{DefaultBodyLimit, Query, State};
use axum::http::{StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde_json::{json, Value};

use nexus_subtitle::audiocpp::AudioCppBackend;
use nexus_subtitle::config::HybridConfig;
use nexus_subtitle::segment::HybridBuffer;
use nexus_subtitle::stream::{Segmenter, lang_name, wav_wrap};
use nexus_subtitle::translate::TranslateEngine;

use crate::state::AppState;
use crate::subtitle_service::SubtitleService;

/// 请求体上限：与 Python `MAX_BODY_BYTES` 一致（100MB）。
const MAX_BODY_BYTES: usize = 100 * 1024 * 1024;
/// 小于 0.1s 的音频直接跳过（Python `_transcribe_impl` 同款）。
const MIN_PCM_BYTES: usize = 3200;
/// partial 节流：连续说话时每块都跑 ASR 太费 GPU（Python 2s 节流）。
const PARTIAL_MIN_GAP: Duration = Duration::from_secs(2);
const PARTIAL_MIN_SEC: f64 = 1.5;

pub struct Ctx {
    pub app: Arc<AppState>,
    // Arc 包装：VAD feed 的同步子进程调用要整段挪进 spawn_blocking（不占
    // tokio worker），锁必须能 move 进 'static 闭包
    buffer: std::sync::Arc<Mutex<HybridBuffer>>,
    last_partial: Mutex<Instant>,
    last_context: Mutex<String>,
    counter: AtomicU64,
    /// 当前切句缓冲对应的 hybrid 配置签名：界面保存配置后无需重启宿主。
    hybrid_sig: Mutex<String>,
    /// 短叹词幻觉门控状态（R101）：[(end_ms, 归一化文本)]，跨请求窗口判重。
    hallu: Mutex<HalluState>,
    /// 行长合并门控扣住的行（R102）：None = 没有扣住的行。
    lm: Mutex<Option<LinePending>>,
    /// 两个门控的配置快照（与 hybrid 缓冲一起热更新）
    gates: Mutex<Gates>,
}

/// 行长合并扣住的行（对齐 Python `_LM_PENDING`）。
#[derive(Debug, Clone)]
struct LinePending {
    text: String,
    start_ms: f64,
    end_ms: f64,
}

#[derive(Default)]
struct HalluState {
    recent: Vec<(i64, String)>,
    last_end: i64,
}

#[derive(Debug, Clone, Default)]
struct Gates {
    hallu: nexus_subtitle::config::HallucinationGateCfg,
    lm: nexus_subtitle::config::LineMergeCfg,
}

/// hybrid 切句参数签名（配置热更新比对用）。
fn hybrid_sig(c: &HybridConfig) -> String {
    format!(
        "{}|{}|{}|{}|{}|{}",
        c.silence_threshold,
        c.silence_tail_sec,
        c.long_silence_tail_sec,
        c.long_phrase_sec,
        c.min_phrase_sec,
        c.max_phrase_sec
    )
}

impl Ctx {
    pub async fn new(app: Arc<AppState>) -> Arc<Self> {
        let (cfg, sig) = {
            let svc = app.subtitle.lock().await;
            let sig = hybrid_sig(&svc.cfg.asr.hybrid);
            (svc.cfg.asr.hybrid.clone(), sig)
        };
        let ctx = Self::with_buffer(app, HybridBuffer::from_config(&cfg));
        *ctx.hybrid_sig.lock().unwrap() = sig;
        ctx
    }

    fn with_buffer(app: Arc<AppState>, buffer: HybridBuffer) -> Arc<Self> {
        Arc::new(Self {
            app,
            buffer: std::sync::Arc::new(Mutex::new(buffer)),
            last_partial: Mutex::new(Instant::now() - PARTIAL_MIN_GAP),
            last_context: Mutex::new(String::new()),
            counter: AtomicU64::new(0),
            hybrid_sig: Mutex::new(String::new()),
            hallu: Mutex::new(HalluState::default()),
            lm: Mutex::new(None),
            gates: Mutex::new(Gates::default()),
        })
    }

    /// 配置签名变了就重建切句缓冲（丢掉旧缓冲，语义等同重启）；
    /// 两个门控的配置快照一并刷新。
    fn refresh_buffer_if_stale(&self, cfg: &nexus_subtitle::config::AsrConfig) {
        let sig = hybrid_sig(&cfg.hybrid);
        {
            let mut cur = self.hybrid_sig.lock().unwrap();
            if *cur != sig {
                let mut b = self.buffer.lock().unwrap();
                *b = HybridBuffer::from_config(&cfg.hybrid);
                *cur = sig;
            }
        }
        *self.gates.lock().unwrap() = Gates {
            hallu: cfg.hallucination_gate.clone(),
            lm: cfg.line_merge.clone(),
        };
    }

    fn recommended_chunk(&self, parts: &crate::subtitle_service::StatusParts) -> u32 {
        if parts.translate_backend == "openai" || parts.translate_backend == "cloud" {
            25
        } else {
            parts.recommended_chunk_sec.unwrap_or(3).clamp(1, 25)
        }
    }
}

type St = State<Arc<Ctx>>;

fn pcm_to_samples(pcm: &[u8]) -> Vec<f32> {
    pcm.chunks_exact(2)
        .map(|b| i16::from_le_bytes([b[0], b[1]]) as f32 / 32768.0)
        .collect()
}

fn samples_to_wav(samples: &[f32]) -> Vec<u8> {
    let mut pcm = Vec::with_capacity(samples.len() * 2);
    for s in samples {
        let v = (s.clamp(-1.0, 1.0) * 32767.0).round() as i16;
        pcm.extend_from_slice(&v.to_le_bytes());
    }
    wav_wrap(&pcm, 16000, 1, 16)
}

fn truncate_chars(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// ASR 重拉参数：连接级失败时幂等重拉一次再试（Python F12/R125 防线）。
struct AsrRestart {
    exe: PathBuf,
    backend: String,
}

/// 单段音频：ASR → 按字数分摊时间 → 翻译。返回 segments 与耗时。
///
/// 门控流水线（对齐 Python 生产口径，全部只作用于 final）：
/// 拉丁幻觉过滤（默认开）→ 行长合并（R102，默认关，扣住不足 target_len 的行）
/// → 翻译 → 短叹词幻觉门控（R101，默认关）。
#[allow(clippy::too_many_arguments)]
async fn transcribe_span(
    ctx: &Ctx,
    samples: &[f32],
    start_ms: i64,
    lang: &str,
    translate: bool,
    partial: bool,
    context: &str,
    audiocpp: Arc<AudioCppBackend>,
    engine: Arc<TranslateEngine>,
    restart: &AsrRestart,
) -> Result<(Vec<Value>, f64, f64), String> {
    let t_asr = Instant::now();
    let wav = samples_to_wav(samples);
    let tmp = std::env::temp_dir().join(format!(
        "nexus-headset-{}-{}.wav",
        std::process::id(),
        ctx.counter.fetch_add(1, Ordering::Relaxed)
    ));
    std::fs::write(&tmp, &wav).map_err(|e| format!("写临时音频失败：{e}"))?;
    // transcribe_span 带热词复读检测；连接级失败先幂等重拉服务再试一次
    let asr = match audiocpp.transcribe_span(&tmp, context, lang_name(lang)).await {
        Ok(t) => Ok(t),
        Err(e) => {
            let _ = audiocpp
                .ensure_server_with_backend(&restart.exe, None, 60.0, Some(&restart.backend))
                .await;
            audiocpp
                .transcribe_span(&tmp, context, lang_name(lang))
                .await
                .map_err(|e2| format!("{e}；重拉后仍失败：{e2}"))
        }
    };
    let _ = std::fs::remove_file(&tmp);
    let text = asr?.trim().to_string();
    let asr_ms = t_asr.elapsed().as_secs_f64() * 1000.0;
    if text.is_empty() {
        return Ok((Vec::new(), asr_ms, 0.0));
    }

    let block_ms = samples.len() as f64 / 16.0;
    // 块级时间来自 HybridBuffer 的 span 起点，不能再叠流式桥的 +2.5s 补偿。
    let mut seg = Segmenter::new_with_offset(start_ms as f64, block_ms, 0.0);
    let mut lines = seg.push(&text);
    // 拉丁幻觉过滤（对齐 drop_latin_hallucination，默认开）：日语音频里
    // "一个日文字符都没有"的短输出（`Thank`/`I`/`2`）直接丢，不上屏不进翻译
    // 门控（仅 ja 生效）已内置在 is_latin_hallucination 里，这里无条件过滤；
    // 非 ja 语言函数恒返回 false，等于直通
    {
        let before = lines.len();
        lines.retain(|l| !nexus_subtitle::text_filters::is_latin_hallucination(&l.ja, lang));
        let dropped_latin = before - lines.len();
        if dropped_latin > 0 {
            ctx.app.logs.info(format!("已丢弃 {dropped_latin} 行拉丁幻觉输出"));
        }
    }
    let gates = ctx.gates.lock().unwrap().clone();
    let req_end_ms = start_ms as f64 + block_ms;
    // 行长合并门控（R102，默认关）：只作用于 final——partial 临时稿原样放行，
    // 否则客户端会卡在临时稿上（Python R102 复核同款结论）
    let mut gated_lines = if partial {
        lines
    } else {
        apply_line_merge(ctx, lines, req_end_ms, gates.lm)
    };
    let t_mt = Instant::now();
    let translations: Vec<String> = if translate && !gated_lines.is_empty() {
        if lang.split('-').next().unwrap_or("").eq_ignore_ascii_case("zh") {
            // 源语言就是目标语言：不翻，直接显示原文（Python 同样跳过翻译）。
            gated_lines.iter().map(|l| l.ja.clone()).collect()
        } else {
            let texts: Vec<String> = gated_lines.iter().map(|l| l.ja.clone()).collect();
            engine
                .translate_batch_for(&texts, lang)
                .await
                .into_iter()
                .enumerate()
                .map(|(i, r)| {
                    // 单句失败只丢该句译文，但必须留日志——静默丢弃会让
                    // 整行字幕无声消失，谁也查不到原因
                    if !r.error.is_empty() {
                        ctx.app
                            .logs
                            .warn(format!("第 {} 句翻译失败：{}", i + 1, r.error));
                    }
                    r.translation
                })
                .collect()
        }
    } else {
        vec![String::new(); gated_lines.len()]
    };
    let mt_ms = t_mt.elapsed().as_secs_f64() * 1000.0;

    // 短叹词幻觉门控（R101，默认关）：只作用于 final
    if !partial {
        let dropped = apply_hallucination_gate(ctx, &mut gated_lines, gates.hallu);
        if dropped > 0 {
            ctx.app.logs.info(format!("幻觉门控丢弃 {dropped} 行"));
        }
    }

    let mut out = Vec::with_capacity(gated_lines.len());
    for (i, line) in gated_lines.iter().enumerate() {
        let zh = translations.get(i).cloned().unwrap_or_default();
        let mut item = json!({
            "start_ms": line.start_ms,
            "end_ms": line.end_ms,
            "text": line.ja,
            "translation": zh,
        });
        if partial {
            item["partial"] = json!(true);
        }
        out.push(item);
    }
    let held_ctx = ctx_held_last(ctx);
    if let Some(last) = gated_lines.last().or(held_ctx.as_ref()) {
        *ctx.last_context.lock().unwrap() = truncate_chars(&last.ja, 60);
    }
    Ok((out, asr_ms, mt_ms))
}

/// 热词上下文兜底：行被行长门控扣住时，context 用扣住的行（否则下一块没有
/// 热词偏置，专名跨块听错）。只在扣住时非 None。
fn ctx_held_last(ctx: &Ctx) -> Option<nexus_subtitle::stream::Line> {
    let g = ctx.lm.lock().unwrap();
    g.as_ref().map(|p| nexus_subtitle::stream::Line {
        ja: p.text.clone(),
        start_ms: p.start_ms,
        end_ms: p.end_ms,
    })
}

/// 行长合并门控（R102）：不足 target_len 的行扣住，与下一块合并；
/// 真停顿（间隙超 gap_ms）或超时/超跨度则原样放行。返回放行的行。
fn apply_line_merge(
    ctx: &Ctx,
    lines: Vec<nexus_subtitle::stream::Line>,
    req_end_ms: f64,
    cfg: nexus_subtitle::config::LineMergeCfg,
) -> Vec<nexus_subtitle::stream::Line> {
    if !cfg.enabled {
        return lines;
    }
    use nexus_subtitle::text_filters::{join_tokens, strip_punct};
    let mut segs = lines;
    let mut held_now = false;
    let mut g = ctx.lm.lock().unwrap();
    if let Some(held) = g.take() {
        if let Some(first) = segs.first_mut() {
            let gap = first.start_ms - held.end_ms;
            if gap <= cfg.gap_ms as f64 {
                // 与下一块的第一行合并（中日文直接相连，英文补空格）
                first.ja = join_tokens(&[held.text, first.ja.clone()]);
                first.start_ms = first.start_ms.min(held.start_ms);
            } else {
                // 真停顿：半句按原样放行成独立行（翻译在本请求内照做）
                segs.insert(
                    0,
                    nexus_subtitle::stream::Line {
                        ja: held.text,
                        start_ms: held.start_ms,
                        end_ms: held.end_ms,
                    },
                );
            }
        } else if req_end_ms - held.end_ms > cfg.max_hold_ms as f64
            || req_end_ms - held.start_ms > cfg.max_span_ms as f64
        {
            segs.insert(
                0,
                nexus_subtitle::stream::Line {
                    ja: held.text,
                    start_ms: held.start_ms,
                    end_ms: held.end_ms,
                },
            );
        } else {
            // 行还不足长、也没超时：继续扣，本响应不给 final
            *g = Some(held);
            held_now = true;
        }
    }
    if !held_now {
        if let Some(last) = segs.last() {
            if strip_punct(&last.ja).chars().count() < cfg.target_len
                && req_end_ms - last.start_ms <= cfg.max_span_ms as f64
            {
                let l = segs.pop().expect("last 已判非空");
                *g = Some(LinePending {
                    text: l.ja,
                    start_ms: l.start_ms,
                    end_ms: l.end_ms,
                });
            }
        }
    }
    segs
}

/// 短叹词幻觉门控（R101）：归一化后 ≤max_len 字的行，窗口内出现过相同文本
/// → 保留首次、丢弃后续（首次永远保留：228s 的"もっと"对应真命中）。
/// partial 不进窗口、不参与判重（调用侧保证）。
fn apply_hallucination_gate(
    ctx: &Ctx,
    lines: &mut Vec<nexus_subtitle::stream::Line>,
    cfg: nexus_subtitle::config::HallucinationGateCfg,
) -> usize {
    if !cfg.enabled {
        return 0;
    }
    use nexus_subtitle::text_filters::normalize_for_dedup;
    let win_ms = (cfg.window_s * 1000.0) as i64;
    let mut st = ctx.hallu.lock().unwrap();
    let cur_end = lines
        .iter()
        .map(|l| l.end_ms as i64)
        .max()
        .unwrap_or(0);
    if st.last_end > 0 && cur_end < st.last_end - 60_000 {
        st.recent.clear(); // 回退/换片：清空历史
    }
    let mut kept = Vec::with_capacity(lines.len());
    let mut dropped = 0usize;
    for l in lines.drain(..) {
        let pos = l.end_ms as i64;
        let norm = normalize_for_dedup(&l.ja);
        let dup = (0 < norm.chars().count() && norm.chars().count() <= cfg.max_len)
            && st
                .recent
                .iter()
                .any(|(t2, n2)| *t2 <= pos && pos - *t2 < win_ms && n2 == &norm);
        if dup {
            dropped += 1;
            continue;
        }
        if 0 < norm.chars().count() && norm.chars().count() <= cfg.max_len {
            st.recent.push((pos, norm));
        }
        kept.push(l);
    }
    st.recent.retain(|(t2, _)| cur_end - *t2 < win_ms * 3);
    st.last_end = st.last_end.max(cur_end);
    *lines = kept;
    dropped
}

async fn health(State(ctx): St) -> Response {
    let parts = {
        let svc = ctx.app.subtitle.lock().await;
        svc.status_parts()
    };
    let (asr_ok, mt_ok) = if parts.translate_local {
        let (a, m) = tokio::join!(parts.asr.probe(), parts.mt.probe());
        (a, m)
    } else {
        (parts.asr.probe().await, false)
    };
    let mut v = SubtitleService::status_json(&parts, asr_ok, mt_ok);
    if let Some(o) = v.as_object_mut() {
        o.insert("ok".into(), json!(true));
        o.insert("pid".into(), json!(std::process::id()));
        o.insert("recommended_chunk_sec".into(), json!(ctx.recommended_chunk(&parts)));
        o.insert(
            "model".into(),
            json!(parts.asr_model.rsplit(['/', '\\']).next().unwrap_or("")),
        );
        o.insert("backend".into(), json!("onnx"));
        o.insert("still_gate".into(), json!(ctx.app.vision.mode_name()));
    }
    Json(v).into_response()
}

async fn transcribe(State(ctx): St, Query(q): Query<HashMap<String, String>>, body: Bytes) -> Response {
    let lang = q.get("lang").cloned().unwrap_or_else(|| "ja".into());
    let translate = q
        .get("translate")
        .map(|v| v != "false" && v != "0")
        .unwrap_or(true);
    let start_ms = q
        .get("video_start_ms")
        .and_then(|v| v.parse::<i64>().ok())
        .unwrap_or(0);
    let partial = q
        .get("partial")
        .map(|v| v == "1" || v == "true")
        .unwrap_or(false);

    let _guard = {
        let svc = ctx.app.subtitle.lock().await;
        svc.activity.begin()
    };

    let mut pcm = body.to_vec();
    if pcm.len() % 2 == 1 {
        pcm.pop();
    }
    let (recommended, audiocpp, engine, restart, asr_cfg) = {
        let svc = ctx.app.subtitle.lock().await;
        let parts = svc.status_parts();
        let sp = svc.start_parts();
        (
            ctx.recommended_chunk(&parts),
            Arc::clone(&svc.audiocpp),
            Arc::clone(&svc.engine),
            AsrRestart {
                exe: sp.asr_exe,
                backend: sp.asr_backend,
            },
            svc.cfg.asr.clone(),
        )
    };
    // 界面保存过切句配置的话，这里就地重建缓冲（等效重启，无需重启宿主）；
    // 两个门控的配置快照一并刷新
    ctx.refresh_buffer_if_stale(&asr_cfg);
    let empty = |reason: &str, skipped: bool| {
        json!({
            "language": lang,
            "segments": [],
            "asr_ms": 0.0,
            "mt_ms": 0.0,
            "skipped": skipped,
            "reason": reason,
            "backend": "hybrid",
            "recommended_chunk_sec": recommended,
        })
    };
    if pcm.len() < MIN_PCM_BYTES {
        return Json(empty("音频过短", true)).into_response();
    }

    let samples = pcm_to_samples(&pcm);
    let context = ctx.last_context.lock().unwrap().clone();
    // silero VAD（②VAD 语音区间隙切点 + 轻声救回的裁判）：audiocpp_cli 承担，
    // 与 audiocpp_server.exe 同目录；模型在 assets/framework/models/silero_vad。
    // 失败（CLI 缺失/崩溃）退 None → 切句降级 RMS+硬切，绝不静默丢块。
    let vad = {
        let cli = restart
            .exe
            .parent()
            .map(|d| d.join("audiocpp_cli.exe"));
        let model_dir = restart
            .exe
            .parent()
            .and_then(|d| d.parent())
            .map(|d| d.join("assets").join("framework").join("models").join("silero_vad"));
        let asr2 = Arc::clone(&audiocpp);
        let logs = Arc::clone(&ctx.app.logs);
        move |pcm: &[f32]| -> Option<nexus_subtitle::segment::Regions> {
            let (Some(cli), Some(model)) = (cli.as_ref(), model_dir.as_ref()) else {
                return None;
            };
            match asr2.speech_spans(cli, model, pcm) {
                Ok(v) => Some(v),
                Err(e) => {
                    logs.warn(format!("VAD 不可用，切句降级 RMS+硬切：{e}"));
                    None
                }
            }
        }
    };
    // VAD CLI 是同步子进程（CPU 推理几百 ms）：整个 feed 放 blocking 线程，
    // 不占 tokio worker（buffer 锁一并挪进闭包，避免 std Mutex 跨 await）。
    // 与 Python 生产行为一致（单客户端串行假设），并发请求在这里排队。
    // PCM 克隆一份进闭包：后面 transcribe_span 还要用原 samples。
    let (span, span_start) = {
        let buf2 = std::sync::Arc::clone(&ctx.buffer);
        let samples2 = samples.clone();
        let lang2 = lang.clone();
        let vad2 = vad;
        tokio::task::spawn_blocking(move || {
            let mut b = buf2.lock().unwrap();
            let (span, start, _reason) = b.feed(&samples2, &lang2, start_ms, Some(&vad2));
            (span, start)
        })
        .await
        // JoinError（罕见）：降级为无切句区间，后续按整块走
        .unwrap_or((None, 0))
    };

    if let Some(span) = span {
        let (segments, asr_ms, mt_ms) = match transcribe_span(
            &ctx,
            &span,
            span_start,
            &lang,
            translate,
            false,
            &context,
            Arc::clone(&audiocpp),
            Arc::clone(&engine),
            &restart,
        )
        .await
        {
            Ok(v) => v,
            Err(e) => {
                let mut v = empty(&e, false);
                v["error"] = json!(e);
                return Json(v).into_response();
            }
        };
        return Json(json!({
            "language": lang,
            "segments": segments,
            "asr_ms": asr_ms,
            "mt_ms": mt_ms,
            "skipped": false,
            "backend": "hybrid",
            "recommended_chunk_sec": recommended,
        }))
        .into_response();
    }

    // 还没切出整句：按 2s 节流返回 partial 临时稿（客户端先上屏、定稿覆盖）。
    if partial {
        let due = {
            let mut last = ctx.last_partial.lock().unwrap();
            if last.elapsed() >= PARTIAL_MIN_GAP {
                *last = Instant::now();
                true
            } else {
                false
            }
        };
        if due {
            let snap = {
                let b = ctx.buffer.lock().unwrap();
                b.snapshot(PARTIAL_MIN_SEC)
            };
            if let Some((pcm_snap, snap_start)) = snap {
                match transcribe_span(
                    &ctx,
                    &pcm_snap,
                    snap_start,
                    &lang,
                    translate,
                    true,
                    &context,
                    Arc::clone(&audiocpp),
                    Arc::clone(&engine),
                    &restart,
                )
                .await
                {
                    Ok((segments, asr_ms, mt_ms)) => {
                        return Json(json!({
                            "language": lang,
                            "segments": segments,
                            "asr_ms": asr_ms,
                            "mt_ms": mt_ms,
                            "skipped": false,
                            "backend": "hybrid-partial",
                            "recommended_chunk_sec": recommended,
                        }))
                        .into_response();
                    }
                    Err(e) => {
                        // partial 失败不能静默吞掉落空响应，否则 ASR 挂了无任何痕迹
                        ctx.app.logs.warn(format!("partial 转写失败：{e}"));
                    }
                }
            }
        }
    }

    Json(empty("", false)).into_response()
}

async fn transcribe_stream(
    State(ctx): St,
    Query(q): Query<HashMap<String, String>>,
    body: Bytes,
) -> Response {
    let lang = q.get("lang").cloned().unwrap_or_else(|| "ja".into());
    let translate = q
        .get("translate")
        .map(|v| v != "false" && v != "0")
        .unwrap_or(true);
    let start_ms = q
        .get("video_start_ms")
        .and_then(|v| v.parse::<f64>().ok())
        .unwrap_or(0.0);
    let mut pcm = body.to_vec();
    if pcm.len() % 2 == 1 {
        pcm.pop();
    }
    let parts = {
        let svc = ctx.app.subtitle.lock().await;
        svc.stream_parts(translate)
    };
    let stream =
        SubtitleService::run_stream(parts, pcm, lang, translate, start_ms).await;
    Response::builder()
        .header(header::CONTENT_TYPE, "text/event-stream")
        .header(header::CACHE_CONTROL, "no-cache")
        .header("X-Accel-Buffering", "no")
        .body(axum::body::Body::from_stream(futures_util::StreamExt::map(
            stream,
            |s| Ok::<_, std::convert::Infallible>(Bytes::from(s)),
        )))
        .unwrap()
        .into_response()
}

async fn deny(uri: axum::http::Uri) -> Response {
    (
        StatusCode::FORBIDDEN,
        Json(json!({"ok": false, "error": format!("该接口不对局域网开放：{}", uri.path())})),
    )
        .into_response()
}

pub fn router(ctx: Arc<Ctx>) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/transcribe", post(transcribe))
        .route("/transcribe/stream", post(transcribe_stream))
        .fallback(deny)
        // 0.0.0.0 监听含回环：本机恶意网页可跨站 POST 触发 GPU 推理；
        // 头显/原生客户端不带 Origin，不受影响
        .layer(axum::middleware::from_fn(crate::server::deny_foreign_writes))
        .layer(DefaultBodyLimit::max(MAX_BODY_BYTES))
        .with_state(ctx)
}

/// 启动 8756 监听。端口被占用（例如旧 Python 服务在跑）只记日志，不阻塞宿主。
pub async fn serve(app: Arc<AppState>, port: u16) {
    if port == 0 {
        return;
    }
    let ctx = Ctx::new(Arc::clone(&app)).await;
    let listener = match tokio::net::TcpListener::bind(("0.0.0.0", port)).await {
        Ok(l) => l,
        Err(e) => {
            app.logs.warn(format!(
                "头显字幕服务端口 {port} 启动失败：{e}（头显将无法推流）"
            ));
            return;
        }
    };
    app.logs
        .info(format!("头显字幕服务监听 0.0.0.0:{port}"));
    if let Err(e) = axum::serve(listener, router(ctx)).await {
        app.logs.error(format!("头显字幕服务退出：{e}"));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::Body;
    use axum::http::Request;
    use tower::ServiceExt;

    fn test_ctx() -> Arc<Ctx> {
        let app = AppState::new("test", 8890);
        Ctx::with_buffer(app, HybridBuffer::new())
    }

    async fn status_of(uri: &str, method: &str, body: Vec<u8>) -> StatusCode {
        let req = Request::builder()
            .method(method)
            .uri(uri)
            .header("content-type", "application/octet-stream")
            .body(Body::from(body))
            .unwrap();
        router(test_ctx()).oneshot(req).await.unwrap().status()
    }

    #[tokio::test]
    async fn health_is_available() {
        assert_eq!(status_of("/health", "GET", vec![]).await, StatusCode::OK);
    }

    #[tokio::test]
    async fn unknown_path_is_forbidden() {
        assert_eq!(status_of("/api/state", "GET", vec![]).await, StatusCode::FORBIDDEN);
    }

    /// 短音频不触发 ASR，直接按 Python 契约回 `skipped:true`。
    #[tokio::test]
    async fn short_audio_is_skipped() {
        let req = Request::builder()
            .method("POST")
            .uri("/transcribe?lang=ja&translate=true&video_start_ms=0")
            .header("content-type", "application/octet-stream")
            .body(Body::from(vec![0u8; 100]))
            .unwrap();
        let res = router(test_ctx()).oneshot(req).await.unwrap();
        assert_eq!(res.status(), StatusCode::OK);
        let bytes = axum::body::to_bytes(res.into_body(), 1 << 20).await.unwrap();
        let v: Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(v["skipped"], true);
        assert_eq!(v["segments"].as_array().unwrap().len(), 0);
        assert_eq!(v["recommended_chunk_sec"], 3);
    }
}
