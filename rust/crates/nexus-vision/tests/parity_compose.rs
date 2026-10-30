//! 位置合成 + 出点 与 Python 全链路的对拍。
//!
//! 基准由 Python 实跑生成：`decode_latents` → `style_positions_composed`
//! → `extrema_actions`（生产配置：viterbi 分割 + 平台锁定 + 静止门 + 幅度上限）。
//!
//! 判据分两层：
//! 1. 位置曲线 `p` 逐行比对（这是样式机的直接输出）
//! 2. 动作点逐点比对（这是用户真正看到的东西）

use std::collections::HashMap;

use nexus_vision::compose::{style_positions_composed, StyleParams};
use nexus_vision::style::extrema_actions;
use serde_json::Value;

fn load() -> Value {
    let p = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/golden_compose.json");
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

struct Fixture {
    rows: usize,
    vmarg: Vec<f64>,
    level: Vec<f64>,
    env: Vec<f64>,
    blo: Vec<f64>,
    bhi: Vec<f64>,
    plat_top: Vec<f64>,
    plat_bot: Vec<f64>,
    rev_top: Vec<f64>,
    rev_bot: Vec<f64>,
    bias: Vec<f64>,
    p_want: Vec<f64>,
    actions_want: Vec<(f64, f64)>,
    edges: Vec<usize>,
    fps: f64,
}

fn fixture() -> Fixture {
    let g = load();
    let rows = g["rows"].as_u64().unwrap() as usize;
    let cut_rows: Vec<usize> = g["cut_rows"]
        .as_array()
        .unwrap()
        .iter()
        .map(|x| x.as_u64().unwrap() as usize)
        .collect();
    let mut edges = vec![0usize];
    edges.extend(cut_rows.iter().copied().filter(|r| *r < rows));
    edges.push(rows);
    Fixture {
        rows,
        vmarg: f64s(&g, "vmarg"),
        level: f64s(&g, "level"),
        env: f64s(&g, "env"),
        blo: f64s(&g, "blo"),
        bhi: f64s(&g, "bhi"),
        plat_top: f64s(&g, "plat_top"),
        plat_bot: f64s(&g, "plat_bot"),
        rev_top: f64s(&g, "rev_top"),
        rev_bot: f64s(&g, "rev_bot"),
        bias: f64s(&g, "bias"),
        p_want: f64s(&g, "p"),
        actions_want: g["actions"]
            .as_array()
            .unwrap()
            .iter()
            .map(|a| {
                let arr = a.as_array().unwrap();
                (arr[0].as_f64().unwrap(), arr[1].as_f64().unwrap())
            })
            .collect(),
        edges,
        fps: 30.0,
    }
}

fn params() -> StyleParams {
    StyleParams {
        fps: 30.0,
        still_eps: 22.0,
        ext_snap: 0.0,
        amp_cap_x: 4.0,
        plat_thr: 0.25,
        plat_lo: 0.15,
        plat_peak: 0.45,
        plat_veto: 15.0,
        plat_soft: Some((0.5, 1.0)),
        plat_rail_track: true,
        plat_shift_cap: 25.0,
        viterbi: true,
        rev_snap: 2,
        subframe_rev: true,
    }
}

#[test]
fn composed_positions_match_python() {
    let f = fixture();
    let p = style_positions_composed(
        &f.vmarg,
        &f.level,
        &f.env,
        &f.edges,
        Some((&f.blo, &f.bhi)),
        Some((&f.plat_top, &f.plat_bot)),
        Some((&f.rev_top, &f.rev_bot)),
        &f.bias,
        &params(),
    );
    assert_eq!(p.len(), f.rows);

    let mut maxd = 0.0f64;
    let mut worst = 0usize;
    for i in 0..f.rows {
        let d = (p[i] - f.p_want[i]).abs();
        if d > maxd {
            maxd = d;
            worst = i;
        }
    }
    let mean = p
        .iter()
        .zip(f.p_want.iter())
        .map(|(a, b)| (a - b).abs())
        .sum::<f64>()
        / f.rows as f64;
    println!("[p] 行数 {} 平均差 {mean:.6} 最大差 {maxd:.6}（行 {worst}）", f.rows);
    assert!(
        maxd < 0.51,
        "位置曲线最大偏差 {maxd:.6}（行 {worst}：Rust {} vs Python {}）",
        p[worst],
        f.p_want[worst]
    );
}

#[test]
fn actions_match_python() {
    let f = fixture();
    let p = style_positions_composed(
        &f.vmarg,
        &f.level,
        &f.env,
        &f.edges,
        Some((&f.blo, &f.bhi)),
        Some((&f.plat_top, &f.plat_bot)),
        Some((&f.rev_top, &f.rev_bot)),
        &f.bias,
        &params(),
    );
    let times: Vec<f64> = (0..f.rows)
        .map(|i| (i as f64 + 1.0) * 1000.0 / f.fps)
        .collect();
    let empty: HashMap<usize, f64> = HashMap::new();
    let got = extrema_actions(&p, &times, &f.edges, Some(&empty), None, 250.0, 1.0, f.fps);

    println!(
        "[actions] Rust {} 个，Python {} 个",
        got.len(),
        f.actions_want.len()
    );
    let n = got.len().min(f.actions_want.len());
    let mut diffs = Vec::new();
    for i in 0..n {
        if (got[i].at - f.actions_want[i].0).abs() > 0.5
            || (got[i].pos - f.actions_want[i].1).abs() > 0.5
        {
            diffs.push((i, got[i].at, f.actions_want[i].0, got[i].pos, f.actions_want[i].1));
        }
    }
    assert_eq!(
        got.len(),
        f.actions_want.len(),
        "动作点数量不一致（逐点差异 {} 处，前 3：{:?}）",
        diffs.len(),
        &diffs[..diffs.len().min(3)]
    );
    assert!(
        diffs.is_empty(),
        "有 {} 处动作点不一致，前 5：{:?}",
        diffs.len(),
        &diffs[..diffs.len().min(5)]
    );
}

/// 亚帧时刻（`subframe="rev"`）与 Python 的逐点对拍。
///
/// 复用 `golden_compose.json` 的轨道 + 新生成的 `golden_subframe.json`（sub 表 + 动作点）。
#[test]
fn subframe_matches_python() {
    let g = load();
    let sg: Value = serde_json::from_str(
        &std::fs::read_to_string(
            std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/golden_subframe.json"),
        )
        .expect("读不到亚帧基准"),
    )
    .expect("亚帧基准解析失败");

    let rows = g["rows"].as_u64().unwrap() as usize;
    let cut_rows: Vec<usize> = g["cut_rows"]
        .as_array()
        .unwrap()
        .iter()
        .map(|x| x.as_u64().unwrap() as usize)
        .collect();
    let mut edges = vec![0usize];
    edges.extend(cut_rows.iter().copied().filter(|r| *r < rows));
    edges.push(rows);

    let vmarg = f64s(&g, "vmarg");
    let level = f64s(&g, "level");
    let env = f64s(&g, "env");
    let blo = f64s(&g, "blo");
    let bhi = f64s(&g, "bhi");
    let pt = f64s(&g, "plat_top");
    let pb = f64s(&g, "plat_bot");
    let rt = f64s(&g, "rev_top");
    let rb = f64s(&g, "rev_bot");
    let bias = f64s(&g, "bias");
    let times: Vec<f64> = (0..rows).map(|i| (i as f64 + 1.0) * 1000.0 / 30.0).collect();

    let (p, sub, force) = nexus_vision::compose::style_positions_with_sub(
        &vmarg,
        &level,
        &env,
        &edges,
        Some((&blo, &bhi)),
        Some((&pt, &pb)),
        Some((&rt, &rb)),
        &bias,
        &params(),
        &times,
        true,
    );

    eprintln!("viterbi force rows: {}", force.len());
    assert!(!force.is_empty(), "Viterbi apex 行必须导出，否则快速反转会被平滑抹平");

    // 1) sub 表：键必须一致，值必须逐点一致
    let want_sub: std::collections::HashMap<usize, f64> = sg["sub"]
        .as_object()
        .unwrap()
        .iter()
        .map(|(k, v)| (k.parse::<usize>().unwrap(), v.as_f64().unwrap()))
        .collect();
    assert_eq!(sub.len(), want_sub.len(), "亚帧条目数不一致");
    let mut bad = Vec::new();
    for (k, v) in &want_sub {
        match sub.get(k) {
            Some(got) if (got - v).abs() < 1e-6 => {}
            other => bad.push((*k, other.copied(), *v)),
        }
    }
    assert!(
        bad.is_empty(),
        "亚帧时刻有 {} 处不一致，前 5：{:?}",
        bad.len(),
        &bad[..bad.len().min(5)]
    );

    // 2) 动作点：带亚帧应当与 Python 一致
    let want_acts: Vec<(f64, f64)> = sg["actions"]
        .as_array()
        .unwrap()
        .iter()
        .map(|a| {
            let arr = a.as_array().unwrap();
            (arr[0].as_f64().unwrap(), arr[1].as_f64().unwrap())
        })
        .collect();
    // ⚠ 口径边界（2026-10 审查）：golden_subframe.json 是**无 force**基准
    // （实测传入 Some(&force) 后与 Python 基准不匹配）——本断言只覆盖无 force
    // 路径；force（快速反转强制出点）路径由下方 force_path_self_consistent
    // 自洽测试锁定基本行为，重新生成 golden 后才能做逐点对拍。
    let got = extrema_actions(&p, &times, &edges, Some(&sub), None, 250.0, 1.0, 30.0);
    println!("[subframe] Rust {} 点，Python {} 点", got.len(), want_acts.len());
    assert_eq!(got.len(), want_acts.len(), "动作点数量不一致");
    let mut diffs = Vec::new();
    for (i, (g, w)) in got.iter().zip(want_acts.iter()).enumerate() {
        if (g.at - w.0).abs() > 0.5 || (g.pos - w.1).abs() > 0.5 {
            diffs.push((i, g.at, w.0, g.pos, w.1));
        }
    }
    assert!(
        diffs.is_empty(),
        "有 {} 处动作点不一致，前 3：{:?}",
        diffs.len(),
        &diffs[..diffs.len().min(3)]
    );
}

/// force 路径的自洽锁定（不依赖 golden）：生产 stream.rs 走的是
/// `Some(&force)`，此前对拍测试恒传 None，生产路径零覆盖——回归（含 panic）
/// 只能到用户机器上才暴露。断言：输出非空、时间轴单调、位置在 0..100。
#[test]
fn force_path_self_consistent() {
    // 复用 subframe 测试加载的同一份轨道数据
    let gp = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/golden_subframe.json");
    let sg: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(gp).unwrap()).unwrap();
    let gd = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/golden_compose.json");
    let g: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(gd).unwrap()).unwrap();

    let rows = g["rows"].as_u64().unwrap() as usize;
    // 基准浮点数组是字符串编码（与文件内 f64s 同口径）
    let to_vec = |k: &str| -> Vec<f64> {
        g[k].as_array()
            .unwrap()
            .iter()
            .map(|x| x.as_str().unwrap_or_else(|| panic!("{k} 应为字符串：{x}")).parse::<f64>().unwrap())
            .collect()
    };
    let p = to_vec("p");
    let times: Vec<f64> = (0..rows).map(|i| (i as f64) * 1000.0 / 30.0).collect();
    let edges: Vec<usize> = g["cut_rows"]
        .as_array()
        .unwrap()
        .iter()
        .map(|x| x.as_u64().unwrap() as usize)
        .collect();

    // force 行集合直接取 subframe 基准的 Viterbi apex（生成管线同源）
    let sg2 = &g["subframe"];
    let _ = sg2;
    let sub: std::collections::HashMap<usize, f64> = sg["sub"]
        .as_object()
        .unwrap()
        .iter()
        .map(|(k, v)| (k.parse::<usize>().unwrap(), v.as_f64().unwrap()))
        .collect();
    // force 表：用与 subframe 测试一致的 alternating_events 输入构造
    // （golden_compose 提供的 bias/rev 轨道）；这里简化为把 sub 键当 force 行，
    // 目的是锁定 extrema_actions 对 Some(force) 输入的基本行为，不追求与
    // Python 逐点一致。
    let force: Vec<usize> = {
        let mut v: Vec<usize> = sub.keys().copied().collect();
        v.sort_unstable();
        v
    };

    let got = nexus_vision::style::extrema_actions(&p, &times, &edges, Some(&sub), Some(&force), 250.0, 1.0, 30.0);
    assert!(!got.is_empty(), "force 路径必须有输出");
    for w in got.windows(2) {
        assert!(w[1].at >= w[0].at, "动作点时间必须单调：{:?} vs {:?}", w[0], w[1]);
    }
    for a in &got {
        assert!((0.0..=100.0).contains(&a.pos), "位置越界：{a:?}");
    }
}
