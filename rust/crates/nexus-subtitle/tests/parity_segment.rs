//! 混合分段与 Python `HybridBuffer` 的逐次裁决对拍。
//!
//! 基准：`tests/golden_segment.json` —— 同一段合成音频、同样的 1 秒分块、
//! 每次 `feed()` 的 (原因, 起点, 切下长度) 全部记下来。

use nexus_subtitle::segment::{HybridBuffer, Reason};
use serde_json::Value;

fn load() -> Value {
    let p = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/golden_segment.json");
    serde_json::from_str(&std::fs::read_to_string(&p).expect("读不到分段基准")).expect("分段基准解析失败")
}

#[test]
fn hybrid_buffer_matches_python_trace() {
    let g = load();
    let pcm: Vec<f32> = g["pcm"]
        .as_array()
        .unwrap()
        .iter()
        .map(|x| x.as_f64().unwrap() as f32)
        .collect();
    let trace = g["trace"].as_array().unwrap();
    let step = 16000usize; // 1 秒块

    let mut b = HybridBuffer::new();
    let mut mismatches = Vec::new();
    let mut pos = 0usize;
    for (i, want) in trace.iter().enumerate() {
        if pos >= pcm.len() {
            break;
        }
        let end = (pos + step).min(pcm.len());
        let block = &pcm[pos..end];
        let start_ms = (pos * 1000 / 16000) as i64;
        let (span, s_ms, reason) = b.feed(block, "ja", start_ms, None);

        let want_reason = want["reason"].as_str().unwrap();
        let want_len = want["span_len"].as_i64().unwrap();
        let want_start = want["span_start"].as_i64().unwrap();
        let got_len = span.as_ref().map(|s| s.len() as i64).unwrap_or(-1);
        let got_start = if span.is_some() { s_ms } else { 0 };

        if reason.as_str() != want_reason || got_len != want_len || got_start != want_start {
            mismatches.push((i, reason.as_str(), want_reason, got_len, want_len, got_start, want_start));
        }
        pos = end;
    }
    assert!(
        mismatches.is_empty(),
        "有 {} 次裁决与 Python 不一致（索引, Rust原因, Py原因, Rust长度, Py长度, Rust起点, Py起点）：{:?}",
        mismatches.len(),
        &mismatches[..mismatches.len().min(5)]
    );
}

/// 原因字符串映射必须与 Python 一致（前端按字符串判断）。
#[test]
fn reason_strings_match_python() {
    assert_eq!(Reason::None.as_str(), "");
    assert_eq!(Reason::Standard.as_str(), "standard");
    assert_eq!(Reason::Hard.as_str(), "hard");
    assert_eq!(Reason::Vad.as_str(), "vad");
    assert_eq!(Reason::Silent.as_str(), "silent");
    assert_eq!(Reason::VadRescued.as_str(), "vad-rescued");
}
