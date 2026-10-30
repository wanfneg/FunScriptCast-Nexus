//! 流式端到端：真视频 → 切点 → 编码 → 解码 → 合成 → 动作点。
//!
//! 判据不看"与离线逐点相同"（流式的包络 AR 按块重置，本来就不该相同），
//! 而看三条硬性要求：
//! 1. 能跑完、无错误
//! 2. 动作点时间严格递增、位置在 0..100
//! 3. 覆盖整片（最后一个点的位置接近片尾）
//!
//! 缺素材或 ORT 库时跳过。

use std::path::{Path, PathBuf};

use nexus_vision::bundle::Bundle;
use nexus_vision::compose::StyleParams;
use nexus_vision::onnx::default_ort_dylib;
use nexus_vision::stream::{estimate_rows, StreamEngine};

fn repo() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..")
}

fn prep() -> Option<(Bundle, PathBuf)> {
    if std::env::var("ORT_DYLIB_PATH").is_err() {
        if let Some(p) = default_ort_dylib() {
            std::env::set_var("ORT_DYLIB_PATH", p);
        }
    }
    if default_ort_dylib().is_none() {
        eprintln!("跳过：找不到 onnxruntime 动态库");
        return None;
    }
    let bundle_dir = repo().join("vendor/vision/bundles/v0.6.0");
    if !bundle_dir.join("manifest.json").is_file() {
        eprintln!("跳过：缺 bundle");
        return None;
    }
    let video = repo().join("vendor/vision/cache/confirm_150s.480p30.mp4");
    if !video.is_file() {
        eprintln!("跳过：缺测试视频");
        return None;
    }
    let b = Bundle::load(&bundle_dir).ok()?;
    Some((b, video))
}

#[test]
fn streaming_pipeline_end_to_end() {
    let Some((bundle, video)) = prep() else { return };
    let fps = 30.0;
    let (total, _dur) = estimate_rows(&video, fps).unwrap_or((4510, 0.0));
    eprintln!("预估行数 {total}");

    let engine = StreamEngine::new(bundle, StyleParams { fps, ..Default::default() });
    engine.start(&video, total, fps).expect("起不了流式任务");

    // 最多等 10 分钟
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(600);
    loop {
        let (_, rows, _, finished, err) = engine.snapshot();
        if !err.is_empty() {
            panic!("流式报错：{err}");
        }
        if finished {
            break;
        }
        if std::time::Instant::now() > deadline {
            engine.stop();
            panic!("超时（已处理 {rows} 行）");
        }
        std::thread::sleep(std::time::Duration::from_millis(200));
    }

    let (actions, rows, total_rows, _, err) = engine.snapshot();
    assert!(err.is_empty(), "错误：{err}");
    eprintln!("处理 {rows} 行（预估 {total_rows}），得到 {} 个动作点", actions.len());

    assert!(rows > 1000, "行数太少：{rows}");
    assert!(actions.len() > 20, "动作点太少：{}", actions.len());

    // 时间严格递增
    for w in actions.windows(2) {
        assert!(w[1].at > w[0].at, "时间必须严格递增：{:?}", (w[0].at, w[1].at));
    }
    // 位置在范围内
    for a in &actions {
        assert!((0.0..=100.0).contains(&a.pos), "位置越界：{}", a.pos);
    }
    // 覆盖到片尾（最后一个点应接近最后一行的时间）
    let last_ms = actions.last().unwrap().at;
    let total_ms = rows as f64 * 1000.0 / fps;
    assert!(
        last_ms > total_ms * 0.9,
        "未覆盖片尾：最后动作点 {last_ms:.0}ms，片长 {total_ms:.0}ms"
    );
}
