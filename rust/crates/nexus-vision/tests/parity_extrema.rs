//! 转折点抽取与 Python `jepa_infer.extrema_actions` 的逐点对拍。
//!
//! 基准由 Python 生成（`tests/golden_extrema.json`），覆盖四种配置：
//! 纯抽取 / 只 RDP / 只 ease / ease+RDP 全开。
//!
//! 浮点以字符串存储 —— `serde_json` 解析超长小数会丢精度（见 still_gate 的对拍）。

use nexus_vision::style::extrema_actions;
use serde_json::Value;

fn load() -> Value {
    let p = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/golden_extrema.json");
    serde_json::from_str(&std::fs::read_to_string(&p).expect("读不到基准")).expect("基准解析失败")
}

fn f64s(v: &Value, key: &str) -> Vec<f64> {
    v[key]
        .as_array()
        .unwrap()
        .iter()
        .map(|x| {
            x.as_str()
                .unwrap_or_else(|| panic!("{key} 应为字符串：{x}"))
                .parse::<f64>()
                .unwrap()
        })
        .collect()
}

fn usize_s(v: &Value, key: &str) -> Vec<usize> {
    v[key]
        .as_array()
        .unwrap()
        .iter()
        .map(|x| x.as_u64().unwrap() as usize)
        .collect()
}

fn check(case: &str, cut_ease: f64, rdp_eps: f64) {
    let g = load();
    let fps = g["fps"].as_f64().unwrap();
    let p = f64s(&g, "p");
    let times = f64s(&g, "times");
    let edges = usize_s(&g, "edges");

    let got = extrema_actions(&p, &times, &edges, None, None, cut_ease, rdp_eps, fps);

    let want: Vec<(f64, f64)> = g["cases"][case]
        .as_array()
        .unwrap()
        .iter()
        .map(|a| {
            let arr = a.as_array().unwrap();
            (arr[0].as_f64().unwrap(), arr[1].as_f64().unwrap())
        })
        .collect();

    assert_eq!(
        got.len(),
        want.len(),
        "[{case}] 动作点数量不一致：Rust {} vs Python {}",
        got.len(),
        want.len()
    );
    let mut diffs = Vec::new();
    for (i, (g, w)) in got.iter().zip(want.iter()).enumerate() {
        if (g.at - w.0).abs() > 1e-6 || (g.pos - w.1).abs() > 1e-6 {
            diffs.push((i, g.at, w.0, g.pos, w.1));
        }
    }
    assert!(
        diffs.is_empty(),
        "[{case}] 有 {} 处不一致，前 5 个：{:?}",
        diffs.len(),
        &diffs[..diffs.len().min(5)]
    );
    println!("[{case}] {} 个动作点，逐点一致", got.len());
}

#[test]
fn plain_extraction_matches_python() {
    check("plain", 0.0, 0.0);
}

#[test]
fn with_rdp_matches_python() {
    check("rdp", 0.0, 1.0);
}

#[test]
fn with_cut_ease_matches_python() {
    check("ease", 250.0, 0.0);
}

#[test]
fn full_pipeline_matches_python() {
    check("full", 250.0, 1.0);
}
