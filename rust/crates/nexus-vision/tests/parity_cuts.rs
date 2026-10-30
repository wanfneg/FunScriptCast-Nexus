//! 切点（transnet）与 Python 的逐点对拍。
//!
//! 基准由 `onnx_backend.transnet_cuts` 实跑生成。切点决定"镜头边界"，
//! 边界错了，样式机每段的端点就整体错位 —— 这是视觉里最后一个未对拍的环节。

use std::path::{Path, PathBuf};

use nexus_vision::bundle::Bundle;
use nexus_vision::onnx::default_ort_dylib;
use nexus_vision::transnet::TransNet;
use serde_json::Value;

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
fn transnet_cuts_match_python() {
    let Some((bundle, video)) = prep() else { return };
    let golden_path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/golden_cuts.json");
    if !golden_path.is_file() {
        eprintln!("跳过：没有 Python 切点基准");
        return;
    }
    let g: Value = serde_json::from_str(&std::fs::read_to_string(&golden_path).unwrap()).unwrap();
    let want: Vec<usize> = g["cuts_rows"]
        .as_array()
        .unwrap()
        .iter()
        .map(|x| x.as_u64().unwrap() as usize)
        .collect();

    let mut tn = TransNet::load(&bundle).expect("加载切点模型失败");
    let got = tn.cuts(&video, 30.0, None, None).expect("切点计算失败");

    eprintln!("Rust 切点 {:?}", got);
    eprintln!("Python 切点 {:?}", want);

    assert_eq!(
        got.len(),
        want.len(),
        "切点数量不一致：Rust {:?} vs Python {:?}",
        got,
        want
    );
    // 允许 1 行的量化抖动（ms→行 的取整方式可能差一行）
    let mut diffs = Vec::new();
    for (i, (a, b)) in got.iter().zip(want.iter()).enumerate() {
        if (*a as i64 - *b as i64).abs() > 1 {
            diffs.push((i, *a, *b));
        }
    }
    assert!(diffs.is_empty(), "切点位置偏差超过 1 行：{:?}", diffs);
}
