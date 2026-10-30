//! ONNX 解码与 Python 的逐轨道对拍。
//!
//! 素材来自 Gate 0：
//! - latent：`%TEMP%\fsc_gate0_cache\confirm_150s.480p30.onnx.lat.bin`（4510 行）
//! - Python 轨道：`var/gate0_py_150s_nogate.json`（每 10 行抽样）
//!
//! 缺任一素材或 ORT 动态库时**跳过**（不假装通过）。

use nexus_vision::bundle::Bundle;
use nexus_vision::onnx::{default_ort_dylib, Decoder};
use serde_json::Value;

fn bundle_dir() -> std::path::PathBuf {
    std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../vendor/vision/bundles/v0.6.0")
}

fn latent_path() -> std::path::PathBuf {
    std::env::temp_dir()
        .join("fsc_gate0_cache")
        .join("confirm_150s.480p30.onnx.lat.bin")
}

fn python_tracks() -> std::path::PathBuf {
    std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../var/gate0_py_150s_nogate.json")
}

fn prep() -> Option<(Bundle, Vec<i8>, usize, Vec<usize>)> {
    // ort 用 load-dynamic：运行时按 ORT_DYLIB_PATH 找库。没设就自动探测仓库 .venv 里那份，
    // 省得每次手动 export（CI 上没有就跳过）。
    if std::env::var("ORT_DYLIB_PATH").is_err() {
        if let Some(p) = default_ort_dylib() {
            std::env::set_var("ORT_DYLIB_PATH", p);
        }
    }
    if default_ort_dylib().is_none() {
        eprintln!("跳过：找不到 onnxruntime 动态库（设 ORT_DYLIB_PATH）");
        return None;
    }
    if !bundle_dir().join("manifest.json").is_file() || !latent_path().is_file() {
        eprintln!("跳过：缺 bundle 或 latent");
        return None;
    }
    let b = Bundle::load(&bundle_dir()).ok()?;
    let bytes = std::fs::read(latent_path()).ok()?;
    let rows = bytes.len() / b.row_bytes();
    let lat: Vec<i8> = bytes.into_iter().map(|x| x as i8).collect();
    // 与 Python 那次一致：22 切点在 confirm_150s 上对应这些行
    let cuts = vec![188usize, 1711, 2590, 3877, 4069];
    Some((b, lat, rows, cuts))
}

fn cmp_track(name: &str, rust: &[f64], py: &[f64], step: usize, p99_limit: f64) {
    let mut pairs = Vec::new();
    for (i, pv) in py.iter().enumerate() {
        if !pv.is_finite() {
            continue;
        }
        let ri = i * step;
        if ri >= rust.len() {
            break;
        }
        let rv = rust[ri];
        if !rv.is_finite() {
            continue;
        }
        pairs.push((rv, *pv));
    }
    assert!(!pairs.is_empty(), "[{name}] 没有可比对的有效点");
    let n = pairs.len() as f64;
    let (mx, my) = (
        pairs.iter().map(|p| p.0).sum::<f64>() / n,
        pairs.iter().map(|p| p.1).sum::<f64>() / n,
    );
    let (mut sxy, mut sxx, mut syy, mut maxd) = (0.0, 0.0, 0.0, 0.0f64);
    for (x, y) in &pairs {
        let (dx, dy) = (x - mx, y - my);
        sxy += dx * dy;
        sxx += dx * dx;
        syy += dy * dy;
        maxd = maxd.max((x - y).abs());
    }
    let corr = if sxx > 0.0 && syy > 0.0 {
        sxy / (sxx.sqrt() * syy.sqrt())
    } else {
        1.0
    };
    // 判据用 p99 而不是 max：整片 4510 行里个别点（切点附近）偏差大是正常现象，
    // 项目自己的 ONNX-vs-torch 验收也是这么给的（level p99 0.91、bhi 0.29…）。
    let mut diffs: Vec<f64> = pairs.iter().map(|(x, y)| (x - y).abs()).collect();
    diffs.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let p99 = diffs[((diffs.len() as f64 * 0.99) as usize).min(diffs.len() - 1)];
    println!(
        "[{name}] n={} corr={corr:.6} p99diff={p99:.4} maxdiff={maxd:.4}",
        pairs.len()
    );
    assert!(
        corr > 0.999,
        "[{name}] 与 Python 轨道相关性 {corr:.6} < 0.999"
    );
    assert!(
        p99 < p99_limit,
        "[{name}] p99 偏差 {p99:.4} >= 阈值 {p99_limit}"
    );
}

#[test]
fn decode_matches_python_tracks() {
    let Some((b, lat, rows, cuts)) = prep() else {
        return;
    };
    let mut dec = match Decoder::load(&b) {
        Ok(d) => d,
        Err(e) => {
            eprintln!("跳过：{e}");
            return;
        }
    };
    let n = rows.min(4510);
    let tracks = match dec.decode(&lat, n, &cuts) {
        Ok(t) => t,
        Err(e) => panic!("解码失败：{e}"),
    };
    assert_eq!(tracks.vmarg.len(), n);

    let py_path = python_tracks();
    if !py_path.is_file() {
        eprintln!("跳过比对：没有 Python 轨道存档");
        return;
    }
    let pv: Value = serde_json::from_str(&std::fs::read_to_string(&py_path).unwrap()).unwrap();
    let step = pv["track_step"].as_u64().unwrap_or(10) as usize;
    let get = |k: &str| -> Vec<f64> {
        pv["tracks"][k]
            .as_array()
            .unwrap()
            .iter()
            .map(|x| x.as_f64().unwrap_or(f64::NAN))
            .collect()
    };

    cmp_track("vmarg", &tracks.vmarg, &get("vmarg"), step, 0.1);
    cmp_track("level", &tracks.level, &get("level"), step, 2.0);
    cmp_track("blo", &tracks.blo, &get("blo"), step, 2.5);
    cmp_track("bhi", &tracks.bhi, &get("bhi"), step, 1.0);
    cmp_track("plat_top", &tracks.plat_top, &get("plat_top"), step, 0.5);
    cmp_track("rev_top", &tracks.rev_top, &get("rev_top"), step, 0.5);
    cmp_track("conf", &tracks.conf, &get("conf"), step, 0.1);
    // env 是 ODE/AR 路径，最容易与 Python 分叉（e0 全靠 SplitMix64 逐行对齐）
    cmp_track("env", &tracks.env, &get("env"), step, 1.0);
}
