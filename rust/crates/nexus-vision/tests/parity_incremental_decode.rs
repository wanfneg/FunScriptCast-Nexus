//! 增量解码与全量解码的对拍：同一份 latent、同一组切点，逐轨道必须一致。
//!
//! 流式引擎改成“只解新增 chunk”后，输出不能因此漂移；这个测试锁住那条不变量。

use std::io::Read;
use std::path::{Path, PathBuf};

use nexus_vision::bundle::Bundle;
use nexus_vision::onnx::{default_ort_dylib, Decoder, Tracks};

fn repo() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..")
}

fn prep() -> Option<(Bundle, Vec<i8>, usize)> {
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
    let lat_path = repo().join("vendor/vision/cache/K1drsp.onnx.lat.bin");
    if !lat_path.is_file() {
        eprintln!("跳过：缺 latent 缓存");
        return None;
    }
    let bundle = Bundle::load(&bundle_dir).ok()?;
    let rows = 4096usize;
    let row_len = bundle.row_bytes();
    let mut f = std::fs::File::open(lat_path).ok()?;
    let mut raw = vec![0u8; rows * row_len];
    f.read_exact(&mut raw).ok()?;
    let lat: Vec<i8> = raw.iter().map(|&b| b as i8).collect();
    Some((bundle, lat, rows))
}

fn assert_tracks_eq(full: &Tracks, step: &Tracks) {
    let fields: [(&str, &Vec<f64>, &Vec<f64>); 10] = [
        ("vmarg", &full.vmarg, &step.vmarg),
        ("level", &full.level, &step.level),
        ("env", &full.env, &step.env),
        ("blo", &full.blo, &step.blo),
        ("bhi", &full.bhi, &step.bhi),
        ("plat_top", &full.plat_top, &step.plat_top),
        ("plat_bot", &full.plat_bot, &step.plat_bot),
        ("rev_top", &full.rev_top, &step.rev_top),
        ("rev_bot", &full.rev_bot, &step.rev_bot),
        ("conf", &full.conf, &step.conf),
    ];
    for (name, a, b) in fields {
        assert_eq!(a.len(), b.len(), "轨道 {name} 长度不同");
        for i in 0..a.len() {
            let (x, y) = (a[i], b[i]);
            assert!(
                (x.is_nan() && y.is_nan()) || (x - y).abs() < 1e-9,
                "轨道 {name} 第 {i} 行不同：全量 {x} vs 增量 {y}"
            );
        }
    }
}

#[test]
fn incremental_decode_matches_full_decode() {
    let Some((bundle, lat, n)) = prep() else { return };
    let row = bundle.row_bytes();
    let cuts = [512usize, 1500, 2600];

    let mut full_dec = Decoder::load(&bundle).unwrap();
    let full = full_dec.decode(&lat[..n * row], n, &cuts).unwrap();

    let mut step_dec = Decoder::load(&bundle).unwrap();
    let mut tracks = Tracks::empty(n);
    let mut next = 0usize;
    let mut done = 0usize;
    while done < n {
        done = (done + 64).min(n);
        step_dec
            .decode_step(
                &lat[..done * row],
                done,
                &cuts,
                &mut tracks,
                &mut next,
                false,
            )
            .unwrap();
    }
    step_dec
        .decode_step(&lat[..n * row], n, &cuts, &mut tracks, &mut next, true)
        .unwrap();

    assert_eq!(next, n, "增量解码必须覆盖到末行");
    // 增量路径保留原始轨道（静止门需要），全量 decode 会在末尾修镜头尾行；
    // 测试里补上同样的尾行修正再比较。
    tracks.apply_shot_tails(&cuts, n, true);
    assert_tracks_eq(&full, &tracks);
}
