//! 与 Python `vendor/vision/still_gate.py` 的逐值对拍。
//!
//! 基准向量由 Python 生成（`tests/golden_still_gate.json`）。
//!
//! ⚠ 浮点以**字符串**存储：`serde_json` 解析超长小数会丢精度
//! （实测 `"3900.0000000000005"` 被解析成 `3900.000000000001`，足以让窗口边界
//! 少算一行、门控结果翻转）。这里一律走 `str::parse::<f64>()`，它是正确舍入的。

use nexus_vision::still_gate::{keep_flags, signals, GateConfig, GateMode};
use serde_json::Value;

fn load() -> Value {
    let p = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/golden_still_gate.json");
    let text = std::fs::read_to_string(&p)
        .unwrap_or_else(|e| panic!("读不到基准向量 {}：{e}", p.display()));
    serde_json::from_str(&text).unwrap()
}

/// 基准里的浮点都是字符串，精确还原。
fn f64s(v: &Value, key: &str) -> Vec<f64> {
    v[key]
        .as_array()
        .unwrap()
        .iter()
        .map(|x| {
            x.as_str()
                .unwrap_or_else(|| panic!("{key} 的项应为字符串：{x}"))
                .parse::<f64>()
                .unwrap()
        })
        .collect()
}

fn scalar(v: &Value, key: &str) -> f64 {
    v[key].as_str().unwrap().parse::<f64>().unwrap()
}

fn run(mode: GateMode) -> Vec<bool> {
    let g = load();
    let (v, c) = signals(&f64s(&g, "vm"), &f64s(&g, "conf"), scalar(&g, "v_std"));
    let cfg = GateConfig {
        mode,
        ..Default::default()
    };
    keep_flags(&f64s(&g, "actions_at"), &f64s(&g, "times_sec"), &v, &c, &cfg).0
}

fn expect(mode: &str) -> Vec<bool> {
    let g = load();
    g["expect"][mode]
        .as_array()
        .unwrap()
        .iter()
        .map(|x| x.as_bool().unwrap())
        .collect()
}

fn assert_same(mode: &str, got: &[bool]) {
    let want = expect(mode);
    assert_eq!(got.len(), want.len(), "[{mode}] 动作点数量必须一致");
    let diff: Vec<usize> = (0..got.len()).filter(|&i| got[i] != want[i]).collect();
    assert!(
        diff.is_empty(),
        "[{mode}] 有 {} 处与 Python 不一致，索引：{:?}",
        diff.len(),
        diff
    );
}

#[test]
fn combo_matches_python_exactly() {
    let got = run(GateMode::Combo);
    assert_same("combo", &got);
    assert_eq!(got.iter().filter(|k| **k).count(), 23, "基准应保留 23 个");
}

#[test]
fn and_matches_python_exactly() {
    let got = run(GateMode::And);
    assert_same("and", &got);
    assert_eq!(got.iter().filter(|k| **k).count(), 7, "and 最严，只留 7 个");
}

#[test]
fn or_matches_python_exactly() {
    let got = run(GateMode::Or);
    assert_same("or", &got);
    assert_eq!(got.iter().filter(|k| **k).count(), 21);
}

/// `off` 在 Python 的 `keep_flags` 里**没有分支**（会落进 combo）；关断是在调用方
/// （`onnx_stream._apply_still_gate`）短路实现的。Rust 把它做成显式模式，
/// 语义与"调用方短路"一致 —— 全部保留。
#[test]
fn off_keeps_everything_like_python_caller_short_circuit() {
    let got = run(GateMode::Off);
    assert!(got.iter().all(|k| *k), "off 必须全保留");
}

/// `and ⊆ combo`：两边都达标的点，乘积必然 >= 1 >= score_thr。
///
/// 但 **combo ⊄ or** —— combo 是乘积判据：(v/vm_thr)*(c/conf_thr) >= 0.7 在
/// v 与 c 各自都不达标时仍可能通过（例如 0.9 × 0.85 = 0.765）。别把"更严"想当然。
#[test]
fn and_is_subset_of_combo() {
    let and = run(GateMode::And);
    let combo = run(GateMode::Combo);
    for i in 0..and.len() {
        if and[i] {
            assert!(combo[i], "and 保留的点 combo 必须也保留（索引 {i}）");
        }
    }
}

/// 记录 combo 与 or 的关系：存在 combo 保留而 or 丢弃的点（乘积判据的特性）。
#[test]
fn combo_is_not_a_subset_of_or() {
    let combo = run(GateMode::Combo);
    let or = run(GateMode::Or);
    let diff: Vec<usize> = (0..combo.len())
        .filter(|&i| combo[i] && !or[i])
        .collect();
    assert!(
        !diff.is_empty(),
        "这批基准里应当存在 combo 留而 or 丢的点；若为空说明数据或公式变了，需重核"
    );
}
