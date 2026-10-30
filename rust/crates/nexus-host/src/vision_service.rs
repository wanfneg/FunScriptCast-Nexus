//! 视觉服务门面：装配引擎（当前为官方 CLI 过渡实现）+ 静止门配置。

use std::path::PathBuf;
use std::sync::Arc;

use nexus_vision::inference::StreamInference;
use nexus_vision::service::VisionService;
use nexus_vision::still_gate::{GateConfig, GateMode};

use crate::paths;
use crate::settings::Settings;

/// 解析 ONNX Runtime 动态库并写进 `ORT_DYLIB_PATH`。
///
/// 查找顺序：
/// 1. 已设置的 `ORT_DYLIB_PATH`
/// 2. 安装目录 `runtime/ort/onnxruntime.dll`（安装包随附）
/// 3. 开发机的 `.venv/.../onnxruntime.dll`
///
/// 找不到就**不设**：`ort` 会在真正加载模型时报错，而不是在这里静默降级。
pub fn resolve_ort_dylib() -> Option<PathBuf> {
    let mut cands: Vec<PathBuf> = Vec::new();
    if let Ok(p) = std::env::var("ORT_DYLIB_PATH") {
        cands.push(PathBuf::from(p));
    }
    cands.push(paths::app_dir().join("runtime").join("ort").join("onnxruntime.dll"));
    if let Some(p) = nexus_vision::onnx::default_ort_dylib() {
        cands.push(p);
    }
    pick_ort_dylib(cands)
}

/// 纯选择逻辑：按顺序取第一个真实存在的文件。
///
/// 抽出来是为了**能测**：直接读环境变量的版本没法单测——
/// 而且 AppState 构造时会写 ORT_DYLIB_PATH，和测试抢同一个全局变量（踩过一次）。
pub fn pick_ort_dylib(cands: Vec<PathBuf>) -> Option<PathBuf> {
    cands.into_iter().find(|p| p.is_file())
}

/// 按 `vendor/vision/config.json` + 宿主设置构造服务。
/// 启动最早期调用（main 最前、任何线程启动之前）：写 ORT_DYLIB_PATH 与
/// NEXUS_VISION_CACHE。多线程并发 spawn 子进程时并发读写环境块在 Windows 上
/// 是未定义行为（sysinfo 的 nvidia-smi、GPU 探测线程都在启动早期并发起进程），
/// 所以环境变量写入必须从 build() 上移到这里。
pub fn init_env() {
    if let Some(p) = resolve_ort_dylib() {
        if std::env::var("ORT_DYLIB_PATH").is_err() {
            std::env::set_var("ORT_DYLIB_PATH", &p);
        }
    }
    let cache = paths::app_dir().join("vendor").join("vision").join("cache");
    if cache.is_dir() {
        std::env::set_var("NEXUS_VISION_CACHE", &cache);
    }
}

pub fn build(settings: &Settings) -> Arc<VisionService> {
    // 环境变量写入已上移到 init_env()（main 最先执行）：这里只读不写
    match resolve_ort_dylib() {
        Some(p) => tracing::info!("ONNX 运行库：{}", p.display()),
        None => tracing::warn!(
            "找不到 onnxruntime.dll：视觉的 ONNX 推理不可用（安装包应随带 runtime/ort/）"
        ),
    }
    let gate = gate_from(settings);
    let bundle = load_bundle();
    // 原生流式：Rust 自己跑 ONNX（编码 DirectML + 解码 CPU），不依赖外部 exe。
    // 只有显式设了 NEXUS_GOBLINSCRIPT 才退回官方 CLI 批处理（对拍/兜底用）。
    let engine: Arc<dyn nexus_vision::inference::Inference> =
        if let Ok(exe) = std::env::var("NEXUS_GOBLINSCRIPT") {
            let out_dir = paths::run_dir().join("vision_out");
            let _ = std::fs::create_dir_all(&out_dir);
            // 注意：GoblinCli::new 已经返回 Arc<Self>，不要再套一层 Arc::new
            nexus_vision::inference::GoblinCli::new(
                PathBuf::from(exe),
                bundle.as_ref().map(|b| b.dir.clone()),
                out_dir,
            ) as Arc<dyn nexus_vision::inference::Inference>
        } else {
            let mut params = nexus_vision::compose::StyleParams::default();
            if let Some(f) = bundle.as_ref() {
                // manifest 才是生产参数的唯一来源；默认值只用于没有 bundle 的测试
                params.fps = f.grid_fps;
                params.still_eps = f.pack.still_eps;
                params.ext_snap = f.pack.ext_snap;
                params.amp_cap_x = f.pack.amp_cap_x;
                params.plat_thr = f.pack.plat_thr;
                params.plat_lo = f.pack.plat_lo;
                params.plat_peak = f.pack.plat_peak;
                params.plat_veto = f.pack.plat_veto;
                params.plat_soft = Some(f.pack.plat_soft);
                params.plat_rail_track = f.pack.plat_rail_track;
                params.plat_shift_cap = f.pack.plat_shift_cap;
                params.viterbi = f.pack.rev_source.eq_ignore_ascii_case("viterbi");
                params.rev_snap = nexus_vision::style::rows_at(
                    f.pack.rev_snap_s,
                    params.fps,
                    false,
                );
                params.subframe_rev = f.pack.subframe.eq_ignore_ascii_case("rev");
            }
            Arc::new(StreamInference::new(bundle.clone(), params))
        };
    VisionService::new(engine, bundle, gate)
}

/// 自检用：单独加载一份 bundle（不影响正在跑的服务）。
pub fn load_bundle_for_check() -> Option<nexus_vision::Bundle> {
    load_bundle()
}

fn load_bundle() -> Option<nexus_vision::Bundle> {
    let dir = paths::vision_dir().join("bundles").join("v0.6.0");
    match nexus_vision::Bundle::load(&dir) {
        Ok(b) => Some(b),
        Err(e) => {
            tracing::warn!("视觉 bundle 未加载：{e}");
            None
        }
    }
}

fn vision_config() -> Option<serde_json::Value> {
    let p = paths::vision_dir().join("config.json");
    let text = std::fs::read_to_string(p).ok()?; // 文件不存在：未配置，静默
    match serde_json::from_str(&text) {
        Ok(v) => Some(v),
        // 文件存在但解析失败必须留痕：否则用户改坏的阈值静默回退默认值，
        // 没有任何线索（错误传播断链）
        Err(e) => {
            tracing::warn!("[vision] config.json 解析失败：{e}（静止门阈值回退默认值）");
            None
        }
    }
}

fn gate_from(settings: &Settings) -> GateConfig {
    let mut cfg = GateConfig::default();
    let vc = vision_config();
    let engine = vc.as_ref().and_then(|v| v.get("engine"));
    // 优先级与 Python 一致：环境变量 > integrated_settings > vendor/vision/config.json
    let mode = std::env::var("FSC_VISION_STILL_GATE")
        .ok()
        .or_else(|| settings.get_str("vision.still_gate"))
        .or_else(|| {
            engine
                .and_then(|e| e.get("still_gate"))
                .and_then(|v| v.as_str())
                .map(str::to_string)
        });
    if let Some(m) = mode {
        cfg.mode = GateMode::parse(&m);
    }
    cfg.conf_thr = settings
        .get_f64("vision.still_conf_thr")
        .or_else(|| {
            engine
                .and_then(|e| e.get("still_conf_thr"))
                .and_then(|v| v.as_f64())
        })
        .unwrap_or(cfg.conf_thr);
    cfg.vm_thr = settings
        .get_f64("vision.still_vm_thr")
        .or_else(|| {
            engine
                .and_then(|e| e.get("still_vm_thr"))
                .and_then(|v| v.as_f64())
        })
        .unwrap_or(cfg.vm_thr);
    cfg.score_thr = settings
        .get_f64("vision.still_score_thr")
        .or_else(|| {
            engine
                .and_then(|e| e.get("still_score_thr"))
                .and_then(|v| v.as_f64())
        })
        .unwrap_or(cfg.score_thr);
    cfg
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp_settings(tag: &str) -> Settings {
        let d = std::env::temp_dir().join(format!("nexus-vis-svc-{}-{tag}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        Settings::load(d.join("s.json"))
    }

    #[test]
    fn default_gate_is_combo_with_python_thresholds() {
        let s = tmp_settings("default");
        let g = gate_from(&s);
        assert_eq!(g.mode, GateMode::Combo);
        assert!((g.conf_thr - 0.70).abs() < 1e-9);
        assert!((g.vm_thr - 50.0).abs() < 1e-9);
        assert!((g.score_thr - 0.70).abs() < 1e-9);
    }

    #[test]
    fn gate_reads_thresholds_from_settings() {
        let s = tmp_settings("thr");
        s.patch(&serde_json::json!({
            "vision": {"still_gate": "and", "still_conf_thr": 0.8, "still_vm_thr": 60, "still_score_thr": 0.5}
        }))
        .unwrap();
        let g = gate_from(&s);
        assert_eq!(g.mode, GateMode::And);
        assert!((g.conf_thr - 0.8).abs() < 1e-9);
        assert!((g.vm_thr - 60.0).abs() < 1e-9);
        assert!((g.score_thr - 0.5).abs() < 1e-9);
    }

    #[test]
    fn ort_lookup_picks_first_existing() {
        // 纯函数：不碰环境变量，所以不会和其他用例抢全局状态
        let d = std::env::temp_dir().join(format!("nexus-ort-{}", std::process::id()));
        std::fs::create_dir_all(&d).unwrap();
        let fake = d.join("onnxruntime.dll");
        std::fs::write(&fake, b"x").unwrap();
        let missing = d.join("nope.dll");
        let got = pick_ort_dylib(vec![missing.clone(), fake.clone()]);
        assert_eq!(got.as_deref(), Some(fake.as_path()), "应跳过不存在的候选");
        assert!(pick_ort_dylib(vec![missing]).is_none(), "全都不存在时回 None");
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn build_wires_service() {
        let s = tmp_settings("build");
        let svc = build(&s);
        // 没起引擎时状态应为 idle、动作数 0
        let info = svc.buffer.info();
        assert_eq!(info.actions, 0);
    }
}
