//! 推理后端抽象。
//!
//! 最终目标是把官方 goblinscript 的 MIT Rust 实现（heads.rs / style.rs 等）接进来做
//! **流式**前缀重算。当前提供：
//!
//! - GoblinCli：调用 goblinscript.exe（整片批处理），先用它把"能出片"打通；
//! - Mock：测试用，按给定轨迹造动作点，完全不碰模型。
//!
//! 接口刻意做成"喂播放头 → 拿动作"，因为流式与批处理只在**何时**产出上不同，
//! 消费侧（宿主 / 设备下发）不关心。

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use async_trait::async_trait;

use crate::buffer::Action;

#[derive(Debug, Clone, Default)]
pub struct EngineStatus {
    pub state: String,
    pub coverage_ms: f64,
    pub n_actions: usize,
    pub note: String,
    /// 预热/编码进度（0..100），供前端进度条使用
    pub pct: f64,
}

#[async_trait]
pub trait Inference: Send + Sync {
    async fn open(&self, video: &PathBuf) -> Result<(), String>;
    async fn poll(&self, playhead_ms: f64) -> (Vec<Action>, f64);
    async fn close(&self);
    /// 引擎发布代数（脏检查用）：每次动作表/信号发布 +1。
    /// 默认 0 = 永远脏（实现方不支持时退化为每拍重算，行为不变）。
    fn publish_epoch(&self) -> u64 {
        0
    }

    fn status(&self) -> EngineStatus;

    /// 逐行轨道信号（原始 vmarg、conf、秒时间轴），供静止门使用。
    /// 默认空 = 该后端给不出信号，门控按"关闭"处理。
    fn signals(&self) -> (Vec<f64>, Vec<f64>, Vec<f64>) {
        (Vec::new(), Vec::new(), Vec::new())
    }
}

/// 把引擎内部错误翻成用户能读懂的提示，原始技术详情保留在括号里。
///
/// 外来机测试的高频坑是显卡：编码器**必须**走 DirectML（GPU），老核显、
/// 虚拟机、远程桌面环境会在这里失败，而原始文本（"启用 DirectML 失败：…"）
/// 对终端用户没有可操作性。未知类别的错误原样透传，不强行加壳。
pub fn friendly_vision_error(err: &str) -> String {
    let e = err.trim();
    if e.is_empty() {
        return String::new();
    }
    let brief = e.chars().take(300).collect::<String>();
    let lower = brief.to_ascii_lowercase();
    if lower.contains("directml") || lower.contains("d3d12") || lower.contains("device removed") {
        format!(
            "显卡不支持 AI 识别所需的 DirectML 加速：请把显卡驱动更新到最新；老核显、虚拟机、远程桌面环境通常无法使用（技术详情：{brief}）"
        )
    } else if lower.contains("does not export") {
        // "does not export OrtGetApiBase"：加载到了 DLL 但导出表不对——
        // 本机验证包内 DLL 是好的，这个形态基本就是杀毒软件隔离/替换了文件
        format!(
            "AI 识别运行库文件异常：很可能被杀毒软件误删或隔离，或安装不完整——请把安装目录加入杀毒软件白名单后重新安装（技术详情：{brief}）"
        )
    } else if lower.contains("onnxruntime") || lower.contains("load library") {
        format!("AI 识别运行库加载失败，程序安装可能不完整，请重新安装（技术详情：{brief}）")
    } else if lower.contains("bundle") || lower.contains("缺少编码器图") {
        format!("AI 识别模型包缺失或不完整，请重新安装（技术详情：{brief}）")
    } else if lower.contains("ffmpeg") || lower.contains("未解出任何帧") {
        format!("视频无法解码：请确认视频文件完好且格式受支持（技术详情：{brief}）")
    } else {
        brief
    }
}

// ---------------------------------------------------------------- Mock

pub struct Mock {
    actions: Vec<Action>,
    open: AtomicBool,
}

impl Mock {
    pub fn new(actions: Vec<Action>) -> Arc<Self> {
        Arc::new(Self {
            actions,
            open: AtomicBool::new(false),
        })
    }
}

#[async_trait]
impl Inference for Mock {
    async fn open(&self, _video: &PathBuf) -> Result<(), String> {
        self.open.store(true, Ordering::SeqCst);
        Ok(())
    }
    async fn poll(&self, playhead_ms: f64) -> (Vec<Action>, f64) {
        if !self.open.load(Ordering::SeqCst) {
            return (Vec::new(), 0.0);
        }
        let visible: Vec<Action> = self
            .actions
            .iter()
            .filter(|a| a.at <= playhead_ms)
            .copied()
            .collect();
        let until = visible.last().map(|a| a.at).unwrap_or(0.0);
        (visible, until)
    }
    async fn close(&self) {
        self.open.store(false, Ordering::SeqCst);
    }
    fn status(&self) -> EngineStatus {
        EngineStatus {
            state: if self.open.load(Ordering::SeqCst) {
                "running".into()
            } else {
                "idle".into()
            },
            ..Default::default()
        }
    }
}

// ---------------------------------------------------------------- 命令行后端

/// 调用官方 goblinscript.exe 做整片草稿。
///
/// 这是**过渡实现**：官方是批处理（整片转码 → 编码 → 解码 → 样式），没有增量轨道，
/// 所以只能"跑完再一次性给动作"。真正的流式要靠接 style.rs 后自己重写。
pub struct GoblinCli {
    pub exe: PathBuf,
    pub bundle: Option<PathBuf>,
    pub out_dir: PathBuf,
    state: std::sync::Mutex<EngineStatus>,
}

impl GoblinCli {
    pub fn new(exe: PathBuf, bundle: Option<PathBuf>, out_dir: PathBuf) -> Arc<Self> {
        Arc::new(Self {
            exe,
            bundle,
            out_dir,
            state: std::sync::Mutex::new(EngineStatus {
                state: "idle".into(),
                ..Default::default()
            }),
        })
    }

    fn set_state(&self, s: &str, note: &str) {
        let mut g = self.state.lock().unwrap();
        g.state = s.to_string();
        g.note = note.to_string();
    }

    /// 读官方产出的 .funscript（actions 数组，每项 {at, pos}）。
    pub fn read_funscript(path: &Path) -> Result<Vec<Action>, String> {
        let text = std::fs::read_to_string(path).map_err(|e| format!("读不到草稿：{e}"))?;
        let v: serde_json::Value =
            serde_json::from_str(&text).map_err(|e| format!("草稿解析失败：{e}"))?;
        let arr = v
            .get("actions")
            .and_then(|a| a.as_array())
            .ok_or("草稿缺少 actions")?;
        Ok(arr
            .iter()
            .filter_map(|a| {
                Some(Action {
                    at: a.get("at")?.as_f64()?,
                    pos: a.get("pos")?.as_f64()?,
                })
            })
            .collect())
    }

    /// 在输出目录里找最新的草稿文件。
    pub fn latest_funscript(&self) -> Option<PathBuf> {
        let rd = std::fs::read_dir(&self.out_dir).ok()?;
        rd.flatten()
            .map(|e| e.path())
            .filter(|p| {
                p.extension()
                    .map(|x| x.eq_ignore_ascii_case("funscript"))
                    .unwrap_or(false)
            })
            .max_by_key(|p| std::fs::metadata(p).and_then(|m| m.modified()).ok())
    }
}

#[async_trait]
impl Inference for GoblinCli {
    async fn open(&self, video: &PathBuf) -> Result<(), String> {
        if !self.exe.is_file() {
            return Err(format!("找不到 goblinscript.exe：{}", self.exe.display()));
        }
        if !video.is_file() {
            return Err(format!("视频不存在：{}", video.display()));
        }
        self.set_state("warming", "启动官方草稿（整片批处理）");
        let mut cmd = tokio::process::Command::new(&self.exe);
        cmd.arg(video)
            .arg("--out")
            .arg(&self.out_dir)
            .args([
                "--no-autocrop",
                "--no-exposure",
                "--no-vr",
                "--no-crop-edit",
                "--quiet",
                "--force",
            ]);
        if let Some(b) = &self.bundle {
            cmd.arg("--bundle").arg(b);
        }
        cmd.kill_on_drop(true);
        crate::hide_console_tokio(&mut cmd);
        let out = cmd.output().await.map_err(|e| format!("调用失败：{e}"))?;
        if !out.status.success() {
            let err = String::from_utf8_lossy(&out.stderr).to_string();
            // 按字符截断：字节切片会把多字节中文路径切在中间，导致 panic
            let brief = err.chars().take(400).collect::<String>();
            self.set_state("error", &friendly_vision_error(&brief));
            return Err(format!("官方草稿失败：{brief}"));
        }
        self.set_state("running", "草稿完成");
        Ok(())
    }

    async fn poll(&self, _playhead_ms: f64) -> (Vec<Action>, f64) {
        let Some(p) = self.latest_funscript() else {
            return (Vec::new(), 0.0);
        };
        match Self::read_funscript(&p) {
            Ok(acts) => {
                let until = acts.last().map(|a| a.at).unwrap_or(0.0);
                let mut g = self.state.lock().unwrap();
                g.n_actions = acts.len();
                g.coverage_ms = until;
                (acts, until)
            }
            Err(e) => {
                // 兜底路径（生产不走 GoblinCli）也要留痕：解析失败时状态停在
                // running 与实际不符，排查时没有任何线索
                tracing::warn!("[nexus-vision] funscript 解析失败（{}）：{e}", p.display());
                (Vec::new(), 0.0)
            }
        }
    }

    async fn close(&self) {
        self.set_state("idle", "");
    }

    fn status(&self) -> EngineStatus {
        self.state.lock().unwrap().clone()
    }
}


/// 原生流式引擎：Rust 自己跑 ONNX（编码 + 解码 + 合成 + 出点），不依赖外部 exe。
///
/// 与 `GoblinCli` 的区别：那个是调官方 exe 做整片批处理（过渡方案），
/// 这个是真的边解码边出点。
pub struct StreamInference {
    engine: Mutex<Option<std::sync::Arc<crate::stream::StreamEngine>>>,
    bundle: Option<crate::bundle::Bundle>,
    params: crate::compose::StyleParams,
}

impl StreamInference {
    pub fn new(bundle: Option<crate::bundle::Bundle>, params: crate::compose::StyleParams) -> Self {
        Self {
            engine: Mutex::new(None),
            bundle,
            params,
        }
    }
}

#[async_trait::async_trait]
impl Inference for StreamInference {
    async fn open(&self, video: &PathBuf) -> Result<(), String> {
        // 早退也必须先停旧引擎：旧视频的推理线程还在发布动作，而 pump 无会话
        // 校验——open 失败后旧动作会"复活"下发（service 层失败路径不再兜底停）
        let bundle = match self.bundle.clone() {
            Some(b) => b,
            None => {
                self.close().await;
                return Err("视觉 bundle 未加载，无法推理".to_string());
            }
        };
        if !video.is_file() {
            self.close().await;
            return Err(format!("视频不存在：{}", video.display()));
        }
        let fps = self.params.fps;
        // ffprobe 是同步子进程（可达数百 ms~秒级）：放阻塞线程池，
        // 不占 tokio worker，也绝不在 engine 锁内做
        let v2 = video.clone();
        let total =
            tokio::task::spawn_blocking(move || crate::stream::estimate_rows(&v2, fps).map(|(r, _)| r).unwrap_or(0))
                .await
                .unwrap_or(0);
        // 停旧 + 起新 + 换位压进同一临界区：并发 open 时第二个会 take 到第一个
        // 刚放进来的引擎并停掉它——不会留下失去 stop 通道、跑完整片的孤儿引擎
        // （start 只起线程，锁内停留毫秒级；等待旧引擎退出在锁外做）
        let old = {
            let mut guard = self.engine.lock().unwrap();
            let eng = std::sync::Arc::new(crate::stream::StreamEngine::new(
                bundle,
                self.params.clone(),
            ));
            eng.start(video, total, fps)?;
            guard.replace(eng)
        };
        if let Some(old) = old {
            old.stop();
            let deadline = std::time::Instant::now() + std::time::Duration::from_secs(10);
            while old.is_running() && std::time::Instant::now() < deadline {
                tokio::time::sleep(std::time::Duration::from_millis(50)).await;
            }
            if old.is_running() {
                eprintln!("nexus-vision: 旧流式引擎等待 10s 仍未退出，强行启动新引擎");
            }
        }
        Ok(())
    }

    async fn poll(&self, playhead_ms: f64) -> (Vec<Action>, f64) {
        let guard = self.engine.lock().unwrap();
        let Some(eng) = guard.as_ref() else {
            return (Vec::new(), 0.0);
        };
        // head_before 在锁内截断前缀：整表 clone 后再过滤在长片后期是
        // 每拍一次的全量大拷贝（Python `_style` 的 coverage 语义见 head_before）
        eng.head_before(playhead_ms)
    }

    async fn close(&self) {
        if let Some(eng) = self.engine.lock().unwrap().take() {
            eng.stop();
        }
    }

    fn publish_epoch(&self) -> u64 {
        let guard = self.engine.lock().unwrap();
        guard
            .as_ref()
            .map(|e| e.publish_epoch())
            .unwrap_or(0)
    }

    fn status(&self) -> EngineStatus {
        let guard = self.engine.lock().unwrap();
        match guard.as_ref() {
            Some(eng) => {
                let (acts, rows, total, finished, err) = eng.snapshot();
                let stage = eng.stage();
                let n_acts = acts.len();
                let eng_actions_ms = eng.coverage_ms();
                let pct = if total > 0 {
                    (rows as f64 / total as f64 * 100.0).clamp(0.0, 100.0)
                } else {
                    0.0
                };
                EngineStatus {
                    coverage_ms: eng_actions_ms,
                    n_actions: n_acts,
                    // 完成必须报 done：报 idle 会让前端在暂停时误判“还没预热完”；
                    // 出错必须报 error：报 done 会让前端永远等不到错误分支、
                    // 卡在预热遮罩上（错误传播链不能断在状态这里）
                    state: if !err.is_empty() {
                        "error"
                    } else if finished {
                        "done"
                    } else {
                        "running"
                    }
                    .to_string(),
                    note: if !err.is_empty() {
                        format!("原生流式出错：{}", friendly_vision_error(&err))
                    } else if finished {
                        "识别完成".to_string()
                    } else if !stage.is_empty() {
                        stage
                    } else {
                        format!("原生流式：{rows}/{total} 行")
                    },
                    pct,
                }
            }
            None => EngineStatus {
                coverage_ms: 0.0,
                n_actions: 0,
                state: "idle".to_string(),
                note: "原生流式引擎（未启动）".to_string(),
                pct: 0.0,
            },
        }
    }

    fn signals(&self) -> (Vec<f64>, Vec<f64>, Vec<f64>) {
        let guard = self.engine.lock().unwrap();
        guard.as_ref().map(|e| e.signals()).unwrap_or_default()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn a(at: f64, pos: f64) -> Action {
        Action { at, pos }
    }

    #[tokio::test]
    async fn mock_emits_only_up_to_playhead() {
        let m = Mock::new(vec![a(0.0, 10.0), a(1000.0, 50.0), a(5000.0, 90.0)]);
        let (acts, _) = m.poll(999.0).await;
        assert!(acts.is_empty(), "未 open 时不出动作");
        m.open(&PathBuf::from("v.mp4")).await.unwrap();
        let (acts, until) = m.poll(1200.0).await;
        assert_eq!(acts.len(), 2, "只吐播放头之前的");
        assert_eq!(until, 1000.0);
        let (all, _) = m.poll(10_000.0).await;
        assert_eq!(all.len(), 3);
        m.close().await;
        assert_eq!(m.status().state, "idle");
    }

    #[tokio::test]
    async fn goblin_cli_errors_when_exe_missing() {
        let g = GoblinCli::new(
            PathBuf::from("Z:/nope/goblinscript.exe"),
            None,
            std::env::temp_dir(),
        );
        let e = g.open(&PathBuf::from("v.mp4")).await.unwrap_err();
        assert!(e.contains("找不到 goblinscript.exe"), "实际：{e}");
    }

    #[tokio::test]
    async fn goblin_cli_errors_when_video_missing() {
        // 用一个一定存在的可执行文件（当前测试进程自己），确认拦在"视频不存在"这一步
        let exe = std::env::current_exe().unwrap();
        let g = GoblinCli::new(exe, None, std::env::temp_dir());
        let e = g.open(&PathBuf::from("Z:/nope/v.mp4")).await.unwrap_err();
        assert!(e.contains("视频不存在"), "实际：{e}");
    }

    #[test]
    fn funscript_parsing() {
        let d = std::env::temp_dir().join(format!("nexus-vis-fs-{}", std::process::id()));
        std::fs::create_dir_all(&d).unwrap();
        let p = d.join("x.funscript");
        std::fs::write(&p, r#"{"actions":[{"at":0,"pos":10},{"at":500,"pos":90}]}"#).unwrap();
        let acts = GoblinCli::read_funscript(&p).unwrap();
        assert_eq!(acts.len(), 2);
        assert_eq!(acts[1].pos, 90.0);
        std::fs::write(&p, "not json").unwrap();
        assert!(GoblinCli::read_funscript(&p).is_err(), "坏 JSON 必须报错");
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn friendly_error_maps_known_classes_and_keeps_detail() {
        // 显卡类：外来机最常见（老核显/虚拟机/远程桌面），必须给可操作提示
        for raw in [
            "启用 DirectML 失败：some ort internals",
            "加载编码器 C:\\x\\encoder.onnx 失败：D3D12 device creation failed",
        ] {
            let out = friendly_vision_error(raw);
            assert!(out.contains("显卡"), "应归到显卡类，实际：{out}");
            assert!(out.contains(raw), "技术详情必须保留，实际：{out}");
        }
        // 运行库 / 模型包 / 视频解码三类
        assert!(
            friendly_vision_error("load library onnxruntime.dll failed").contains("重新安装"),
            "应归到运行库类"
        );
        assert!(
            friendly_vision_error("缺少编码器图：D:\\x\\decoder.onnx").contains("模型包"),
            "应归到模型包类"
        );
        assert!(
            friendly_vision_error("ffmpeg 未解出任何帧（文件损坏或编码不支持）").contains("视频无法解码"),
            "应归到视频解码类"
        );
        // 未知类别原样透传，不强行加壳
        assert_eq!(friendly_vision_error("别的错误"), "别的错误");
        // 空输入 = 空（调用方按非空判断错误）
        assert_eq!(friendly_vision_error("  "), "");
        // 超长技术详情截断，避免 toast 被撑爆
        let long = "x".repeat(500);
        assert_eq!(
            friendly_vision_error(&long).chars().count(),
            300,
            "应截断到 300 字符"
        );
    }
}
