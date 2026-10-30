//! 切点模型（transnet）：把视频切成镜头，逐条对齐 `onnx_backend.transnet_cuts`。
//!
//! 30fps 解码到 27×48，前补 25 帧、尾补齐到步长整数倍；窗口 100、步长 50，
//! 只保留每个窗口输出的第 25..75 行；连续 prob>thr 的一段取峰值帧作为切点，
//! 按 min_gap 合并。

use std::io::Read;
use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};

use ort::environment::Environment;
use ort::session::Session;
use ort::value::Tensor;

use crate::bundle::Bundle;

/// 前补帧数（窗口内"真正属于本帧"的起始偏移）。
const LEAD: usize = 25;

pub struct TransNet {
    #[allow(dead_code)]
    env: Environment,
    session: Session,
    bundle: Bundle,
}

impl TransNet {
    pub fn load(bundle: &Bundle) -> Result<Self, String> {
        let path = bundle.graph_path(&bundle.graph_transnet);
        if !path.is_file() {
            return Err(format!("缺少切点模型：{}", path.display()));
        }
        let env = crate::onnx::shared_env()?;
        let b = Session::builder(&env).map_err(|e| format!("ORT 会话构建失败：{e}"))?;
        // 与 Python 版一致：切点模型也走 DirectML。
        // 用默认 CPU EP 时整片切点会把 8+ 核跑满好几分钟，编码器的 GPU 阶段迟迟不开始。
        #[cfg(windows)]
        let b = b
            .with_execution_providers([ort::ep::DirectML::default().build()])
            .map_err(|e| format!("切点模型启用 DirectML 失败：{e}"))?;
        #[cfg(not(windows))]
        let b = b;
        let mut b = b;
        let session = b
            .commit_from_file(&path)
            .map_err(|e| format!("加载切点模型失败：{e}"))?;
        Ok(Self {
            env,
            session,
            bundle: bundle.clone(),
        })
    }

    /// 整片切点，返回**行号**（0 与 n 不包含在内）。
    ///
    /// `stop` 为 Some 时每轮窗口循环检查，置位即中止（用于旧引擎被覆盖时快速退出）。
    pub fn cuts(
        &mut self,
        video: &Path,
        fps: f64,
        stop: Option<&AtomicBool>,
        progress: Option<(&std::sync::Arc<std::sync::Mutex<crate::stream::StreamState>>, usize)>,
    ) -> Result<Vec<usize>, String> {
        let (w, h) = (self.bundle.input_w, self.bundle.input_h);
        let (window, step) = (self.bundle.window, self.bundle.step);
        let fb = w * h * 3;

        let mut cmd = Command::new("ffmpeg");
        cmd.args([
            "-v", "error", "-nostdin", "-i",
            &video.to_string_lossy(),
            "-vf", &format!("fps={fps},scale={w}:{h}"),
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ])
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
        crate::hide_console_std(&mut cmd);
        let mut child = cmd
            .spawn()
            .map_err(|e| format!("起不了 ffmpeg：{e}"))?;
        let mut out = child.stdout.take().ok_or("拿不到 ffmpeg 输出")?;

        let mut frames: Vec<Vec<u8>> = Vec::new();
        loop {
            // 读帧循环也要检查 stop：ffmpeg 解码长片可达分钟级，只在窗口循环
            // 检查的话旧引擎被覆盖后要等整片解完才退出（双引擎并行占双份显存）
            if stop.map_or(false, |s| s.load(Ordering::Relaxed)) {
                let _ = child.kill();
                let _ = child.wait();
                return Err("切点计算被中止".to_string());
            }
            let mut buf = vec![0u8; fb];
            match out.read_exact(&mut buf) {
                Ok(()) => frames.push(buf),
                Err(_) => break,
            }
            // 整片解码是无声的长任务（长片可达数分钟）：定期回写 stage，
            // 否则前端一直显示"正在计算镜头切点"、用户误判卡死
            if let Some((state, total)) = progress.as_ref() {
                let n = frames.len();
                if n % 3000 == 0 {
                    let mut st = state.lock().unwrap_or_else(|p| p.into_inner());
                    st.stage = format!("正在解码镜头切点（{n}/{total} 帧）…");
                }
            }
        }
        let _ = child.kill();
        let _ = child.wait();

        let n = frames.len();
        if n == 0 {
            return Ok(Vec::new());
        }
        // 尾部补齐到步长整数倍
        let tail = LEAD + step - if n % step == 0 { step } else { n % step };
        let padded = LEAD + n + tail;

        let frame_at = |pi: usize| -> &[u8] {
            if pi < LEAD {
                &frames[0]
            } else if pi >= LEAD + n {
                &frames[n - 1]
            } else {
                &frames[pi - LEAD]
            }
        };

        let mut preds: Vec<f64> = Vec::new();
        let mut ptr = 0usize;
        while ptr + window <= padded {
            if stop.map_or(false, |s| s.load(Ordering::Relaxed)) {
                let _ = child.kill();
                let _ = child.wait();
                return Err("切点计算被中止".to_string());
            }
            let mut flat = Vec::with_capacity(window * fb);
            for k in 0..window {
                flat.extend_from_slice(frame_at(ptr + k));
            }
            let shape: Vec<i64> = vec![1, window as i64, h as i64, w as i64, 3];
            let res = self
                .session
                .run(ort::inputs![
                    "frames" => Tensor::from_array((shape, flat)).map_err(|e| e.to_string())?,
                ])
                .map_err(|e| format!("切点推理失败：{e}"))?;
            let v = res.iter().next().map(|(_, v)| v).ok_or("切点模型无输出")?;
            let (_s, d) = v.try_extract_tensor::<f32>().map_err(|e| e.to_string())?;
            for x in d.iter().skip(LEAD).take(step) {
                preds.push(*x as f64);
            }
            ptr += step;
        }
        preds.truncate(n);

        // 连续超阈的一段取峰值帧，按 min_gap 合并
        let gap_ms = self.bundle.min_gap_s * 1000.0;
        let thr = self.bundle.thr;
        let mut cuts: Vec<usize> = Vec::new();
        let mut last_ms = f64::NEG_INFINITY;
        let mut i = 0usize;
        while i < preds.len() {
            if preds[i] <= thr {
                i += 1;
                continue;
            }
            let mut j = i;
            let mut peak = i;
            while j < preds.len() && preds[j] > thr {
                if preds[j] > preds[peak] {
                    peak = j;
                }
                j += 1;
            }
            let time_ms = peak as f64 / fps * 1000.0;
            // Python `_cuts_to_rows`：行钟是 (i+1)/hz，帧 peak 对应行 peak+1
            let row = peak + 1;
            if time_ms - last_ms >= gap_ms || cuts.is_empty() {
                if row < n {
                    cuts.push(row);
                    last_ms = time_ms;
                }
            }
            i = j;
        }
        Ok(cuts)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lead_is_half_window() {
        // 100 帧窗口只保留 25..75，是"窗口中心"的约定
        assert_eq!(LEAD, 25);
    }
}
