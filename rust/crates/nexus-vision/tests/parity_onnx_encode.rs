//! 编码器与 Python 的逐字节对拍。
//!
//! 素材：`vendor/vision/cache/confirm_150s.480p30.mp4`（480p30 转码产物）
//! 基准：`%TEMP%\fsc_gate0_cache\confirm_150s.480p30.onnx.lat.bin`（Python 编出来的 int8 latent）
//!
//! 只编前 640 行（10 组）—— 够验证分组/对齐/量化，又不至于让测试跑几分钟。

use std::io::Read;
use std::process::{Command, Stdio};

use nexus_vision::bundle::Bundle;
use nexus_vision::onnx::{default_ort_dylib, Encoder, ENC_RES_DEFAULT, GROUP};

const ROWS: usize = 640;

fn repo() -> std::path::PathBuf {
    std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..")
}

fn prep() -> Option<(Bundle, String)> {
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
    Some((b, video.to_string_lossy().to_string()))
}

/// 起 ffmpeg 逐帧吐 rgb24（与 Python `encode_video` 的命令行一致）。
fn frame_reader(video: &str, res: usize) -> std::io::Result<std::process::Child> {
    Command::new("ffmpeg")
        .args([
            "-v",
            "error",
            "-nostdin",
            "-i",
            video,
            "-vf",
            &format!("fps=30,scale={res}:{res}"),
            "-frames:v",
            &(ROWS + GROUP).to_string(),
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ])
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
}

#[test]
fn encoder_matches_python_latent() {
    let Some((b, video)) = prep() else { return };
    let res = ENC_RES_DEFAULT;
    let fsz = res * res * 3;

    let mut child = match frame_reader(&video, res) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("跳过：起不了 ffmpeg（{e}）");
            return;
        }
    };
    let mut stdout = child.stdout.take().unwrap();

    let mut enc = match Encoder::load(&b) {
        Ok(e) => e,
        Err(e) => {
            eprintln!("跳过：{e}");
            let _ = child.kill();
            return;
        }
    };

    // 预读所有帧（编码器闭包按需调用）
    let mut frames: Vec<Vec<u8>> = Vec::new();
    loop {
        let mut buf = vec![0u8; fsz];
        match stdout.read_exact(&mut buf) {
            Ok(()) => frames.push(buf),
            Err(_) => break,
        }
        if frames.len() > ROWS + GROUP {
            break;
        }
    }
    let _ = child.kill();
    let _ = child.wait();
    eprintln!("读到 {} 帧", frames.len());
    assert!(frames.len() > GROUP, "帧数不足：{}", frames.len());

    // 推送式编码：与流式推理走同一条路径
    let mut st = nexus_vision::onnx::EncodeStreamer::new(enc, res);
    let mut got: Vec<i8> = Vec::new();
    for f in frames.iter() {
        got.extend(st.push(f.clone()).expect("编码失败"));
        if got.len() >= ROWS * b.row_bytes() {
            break;
        }
    }
    got.extend(st.finish().expect("收尾失败"));

    // 与 Python 的 latent 比对
    let lat_path = std::env::temp_dir()
        .join("fsc_gate0_cache")
        .join("confirm_150s.480p30.onnx.lat.bin");
    if !lat_path.is_file() {
        eprintln!("跳过比对：没有 Python latent 参考");
        return;
    }
    let want = std::fs::read(&lat_path).unwrap();
    let row_len = b.row_bytes();
    let n = ROWS.min(want.len() / row_len);
    assert!(n > 0);

    let mut same = 0usize;
    let mut close = 0usize;
    let mut total = 0usize;
    let mut maxd = 0i32;
    let mut sumd: i64 = 0;
    for r in 0..n {
        for k in 0..row_len {
            let a = got[r * row_len + k] as i32;
            let bb = want[r * row_len + k] as i8 as i32;
            let d = (a - bb).abs();
            total += 1;
            if d == 0 {
                same += 1;
            }
            if d <= 1 {
                close += 1;
            }
            maxd = maxd.max(d);
            sumd += d as i64;
        }
    }
    let same_pct = 100.0 * same as f64 / total as f64;
    let close_pct = 100.0 * close as f64 / total as f64;
    let mean = sumd as f64 / total as f64;
    eprintln!(
        "行数 {n}  完全相等 {same_pct:.2}%  |差|<=1 {close_pct:.2}%  平均差 {mean:.4}  最大差 {maxd}"
    );

    // 判据：量化后应当高度一致（整数量化会把极小浮点差吸掉）
    assert!(
        close_pct > 99.0,
        "与 Python latent 差异过大：|差|<=1 仅 {close_pct:.2}%（同一视频、同一模型，应该几乎全等）"
    );
    assert!(mean < 0.05, "平均差 {mean:.4} 过大");
}
