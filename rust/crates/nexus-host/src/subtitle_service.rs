//! 字幕服务门面：配置、两个后端、翻译引擎，以及启停。

use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use futures_util::StreamExt;

use nexus_subtitle::audiocpp::{AudioCppBackend, STREAM_MODEL_ID};
use nexus_subtitle::config::{resolve_asr_model, BackendKind, SubtitleConfig};
use nexus_subtitle::llama::LlamaBackend;
use nexus_subtitle::translate::TranslateEngine;

/// 字幕请求活动时钟（对应 Python `server_app` 的
/// `_LAST_REQ_MONO` / `_LAST_REQ_WALL` / `_INFLIGHT`）。
///
/// - 空闲判定只用**单调钟**（墙钟被 NTP 校正/回拨时不会误杀或永不回收）；
/// - 对外展示的 `last_req_ts` 用**墙钟**（界面按 `Date.now()/1000 - ts` 算"多久以前"）；
/// - `inflight` 挡住"请求处理到一半被后台看护回收"。
#[derive(Debug)]
pub struct ActivityClock {
    last_mono: Mutex<Instant>,
    last_wall: Mutex<f64>,
    inflight: AtomicUsize,
}

impl ActivityClock {
    pub fn new() -> Arc<Self> {
        Arc::new(Self {
            last_mono: Mutex::new(Instant::now()),
            last_wall: Mutex::new(epoch_now()),
            inflight: AtomicUsize::new(0),
        })
    }

    pub fn touch(&self) {
        if let Ok(mut g) = self.last_mono.lock() {
            *g = Instant::now();
        }
        if let Ok(mut g) = self.last_wall.lock() {
            *g = epoch_now();
        }
    }

    /// 开始一次请求：刷新活动时间并占用一个在飞名额；guard 释放时归还。
    pub fn begin(self: &Arc<Self>) -> ActivityGuard {
        self.touch();
        self.inflight.fetch_add(1, Ordering::SeqCst);
        ActivityGuard(Arc::clone(self))
    }

    pub fn idle_secs(&self) -> f64 {
        self.last_mono
            .lock()
            .map(|g| g.elapsed().as_secs_f64())
            .unwrap_or(0.0)
    }

    pub fn last_req_ts(&self) -> f64 {
        self.last_wall.lock().map(|g| *g).unwrap_or(0.0)
    }

    pub fn inflight(&self) -> usize {
        self.inflight.load(Ordering::SeqCst)
    }

    /// 空闲到点？`min <= 0` = 关闭回收；有在飞请求时一律不回收。
    pub fn idle_due(&self, min: f64) -> bool {
        min > 0.0 && self.inflight() == 0 && self.idle_secs() >= min * 60.0
    }
}

pub struct ActivityGuard(Arc<ActivityClock>);

impl Drop for ActivityGuard {
    fn drop(&mut self) {
        self.0.inflight.fetch_sub(1, Ordering::SeqCst);
    }
}

/// GPU 探测结果缓存：启动时后台线程预热。
///
/// 探测要同步跑子进程（`--list-devices` 加载 Vulkan 运行时可达秒级、驱动异常时
/// 还可能挂起）——绝不能在持 subtitle 锁的路径上现算（历史教训：锁内慢子进程
/// = 整个宿主假死）。预热未完成时按 false 处理（保守回退 CPU），就绪后用缓存。
static VK_GPU_OK: Mutex<Option<bool>> = Mutex::new(None);
static NVIDIA_OK: Mutex<Option<bool>> = Mutex::new(None);

/// 启动时调用一次：后台预计算 GPU 探测结果。
pub fn prewarm_gpu_probes(vulkan_exe: &std::path::Path) {
    let exe = vulkan_exe.to_path_buf();
    let _ = std::thread::Builder::new()
        .name("gpu-probe".into())
        .spawn(move || {
            let v = probe_vulkan_gpu(&exe);
            if let Ok(mut g) = VK_GPU_OK.lock() {
                *g = Some(v);
            }
        });
    let _ = std::thread::Builder::new().name("nv-probe".into()).spawn(|| {
        let v = probe_nvidia_gpu();
        if let Ok(mut g) = NVIDIA_OK.lock() {
            *g = Some(v);
        }
    });
}

fn cached(cache: &Mutex<Option<bool>>) -> bool {
    cache.lock().map(|g| g.unwrap_or(false)).unwrap_or(false)
}

/// NVIDIA 驱动是否可用（GPU 包存在但机器没驱动时回退 CPU）。
/// 直接问 audio.cpp 的 Vulkan 构建，避免用显卡厂商字符串猜。
fn vulkan_gpu_available() -> bool {
    cached(&VK_GPU_OK)
}

fn has_nvidia_gpu() -> bool {
    cached(&NVIDIA_OK)
}

/// 带超时的同步探测：驱动异常时子进程可能永久挂起，无超时会让探测线程
/// 与子进程双双泄漏（上文注释自认"还可能挂起"却没有兜底）。超时 kill 后
/// 按探测失败处理（保守回退 CPU）。
fn probe_output_with_timeout(
    mut cmd: std::process::Command,
    timeout: std::time::Duration,
) -> Option<String> {
    use std::io::Read;
    cmd.stdout(std::process::Stdio::piped());
    cmd.stderr(std::process::Stdio::null());
    crate::proc_util::hide_console(&mut cmd);
    let mut child = cmd.spawn().ok()?;
    let deadline = std::time::Instant::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => {
                let mut out = String::new();
                if let Some(mut o) = child.stdout.take() {
                    let _ = o.read_to_string(&mut out);
                }
                return Some(out);
            }
            Ok(None) => {}
            Err(_) => return None,
        }
        if std::time::Instant::now() > deadline {
            let _ = child.kill();
            let _ = child.wait();
            tracing::warn!("[subtitle] GPU 探测超时（{}s），按无 GPU 处理", timeout.as_secs());
            return None;
        }
        std::thread::sleep(std::time::Duration::from_millis(50));
    }
}

const GPU_PROBE_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(20);

fn probe_vulkan_gpu(exe: &std::path::Path) -> bool {
    let mut cmd = std::process::Command::new(exe);
    cmd.args(["--backend", "vulkan", "--list-devices"]);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000);
    }
    probe_output_with_timeout(cmd, GPU_PROBE_TIMEOUT)
        .map(|out| out.contains("[GPU]"))
        .unwrap_or(false)
}

fn probe_nvidia_gpu() -> bool {
    let mut cmd = std::process::Command::new("nvidia-smi");
    cmd.arg("-L");
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000);
    }
    probe_output_with_timeout(cmd, GPU_PROBE_TIMEOUT)
        .map(|out| out.to_ascii_lowercase().contains("gpu"))
        .unwrap_or(false)
}

fn epoch_now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

pub struct SubtitleService {
    pub cfg: SubtitleConfig,
    pub audiocpp: Arc<AudioCppBackend>,
    pub llama: Arc<LlamaBackend>,
    pub engine: Arc<TranslateEngine>,
    pub config_path: PathBuf,
    pub models_dir: PathBuf,
    /// audiocpp 服务配置 JSON 的输出目录（`var/run`）
    pub run_dir: PathBuf,
    /// 请求活动时钟（空闲回收判据）
    pub activity: Arc<ActivityClock>,
    /// 流式桥跨块去重参照：`(语言键, 最近几句)`，随服务实例存活。
    pub stream_recent: nexus_subtitle::stream::RecentStore,
    /// 流式转写专用客户端：连接 10s、单次读 120s（读与读之间的超时，不限制整个流式会话）。
    /// 网络调用一律发生在 subtitle 锁之外，这里超时是最后一道防线。
    client: reqwest::Client,
}

/// 锁内取参、锁外发请求的载体。
///
/// subtitle 是全局单把 tokio 锁，任何持锁 await（HTTP/子进程）都会冻结全部字幕与
/// 头显接口——所以对外只提供"先取快照，再放锁调用"的两段式接口。
pub struct StreamParts {
    pub client: reqwest::Client,
    pub base_url: String,
    pub model: String,
    pub engine: Option<Arc<TranslateEngine>>,
    pub batch: usize,
    /// 跨块去重参照（请求间共享）
    pub recent: nexus_subtitle::stream::RecentStore,
    /// 请求活动 guard：随流一起 drop，保证整个流式响应期间不会被空闲回收。
    pub activity: ActivityGuard,
}

pub struct StatusParts {
    pub asr_backend: String,
    pub asr_device: String,
    pub asr_port: u16,
    pub asr_language: String,
    pub asr_model: String,
    pub mt_enabled: bool,
    pub translate_backend: String,
    pub translate_model: String,
    /// true = 本地 llama.cpp（只有这个后端需要探 8082 / 拉起进程）
    pub translate_local: bool,
    /// 本地翻译是否已热（真实 prefill 过）。冷启动放行会撞 15-40s 首句延迟。
    pub mt_warm: bool,
    pub base_url: String,
    pub idle_release_min: f64,
    pub last_req_ts: f64,
    pub max_sec: f64,
    pub max_chars: usize,
    pub recommended_chunk_sec: Option<u32>,
    pub asr: Arc<AudioCppBackend>,
    pub mt: Arc<LlamaBackend>,
}

pub struct StartParts {
    pub audiocpp: Arc<AudioCppBackend>,
    pub asr_exe: PathBuf,
    /// 实际可用后端（cuda / cpu）：有 GPU 包且 nvidia-smi 可用才是 cuda
    pub asr_backend: String,
    pub llama: Arc<LlamaBackend>,
    pub llama_dir: PathBuf,
    pub local_exe: String,
    pub llama_model: PathBuf,
    /// 翻译引擎句柄：拉起后做真实 prefill（R63.2：/health ok 不算热）
    pub engine: Arc<TranslateEngine>,
    /// false = openai/ollama/none：不拉起 llama-server
    pub translate_local: bool,
}

impl SubtitleService {
    pub fn new(config_path: PathBuf, models_dir: PathBuf, run_dir: PathBuf) -> Self {
        let cfg = SubtitleConfig::load(&config_path);
        let model_path = resolve_asr_model(&cfg.asr.model, &cfg.base_dir, &models_dir);
        let audiocpp = Arc::new(AudioCppBackend::new(
            cfg.asr.clone(),
            model_path,
            run_dir.clone(),
        ));
        let llama = Arc::new(LlamaBackend::new(cfg.translate.clone()));
        let engine = Arc::new(TranslateEngine::new(
            cfg.translate.clone(),
            Arc::clone(&llama),
        ));
        let client = reqwest::Client::builder()
            .connect_timeout(std::time::Duration::from_secs(10))
            .read_timeout(std::time::Duration::from_secs(120))
            .build()
            .unwrap_or_default();
        Self {
            cfg,
            audiocpp,
            llama,
            engine,
            config_path,
            models_dir,
            run_dir,
            activity: ActivityClock::new(),
            stream_recent: Arc::new(std::sync::Mutex::new((String::new(), Vec::new()))),
            client,
        }
    }

    /// 计时一次字幕请求（`/health`/状态查询不算活动，与 Python 一致）。
    pub fn touch(&self) {
        self.activity.touch();
    }

    pub fn idle_release_min(&self) -> f64 {
        self.cfg.server.idle_release_min
    }

    pub fn idle_due(&self) -> bool {
        self.activity.idle_due(self.idle_release_min())
    }

    pub fn last_req_ts(&self) -> f64 {
        self.activity.last_req_ts()
    }

    /// 流式转写第一步（锁内）：只收集参数，不发任何网络请求。
    pub fn stream_parts(&self, translate: bool) -> StreamParts {
        StreamParts {
            client: self.client.clone(),
            base_url: self.audiocpp.base_url(),
            // 请求里要的是服务端注册的模型 id，不是模型目录路径
            model: STREAM_MODEL_ID.to_string(),
            engine: if translate { Some(Arc::clone(&self.engine)) } else { None },
            batch: nexus_subtitle::stream::batch_size(&self.cfg),
            recent: Arc::clone(&self.stream_recent),
            activity: self.activity.begin(),
        }
    }

    /// 流式转写第二步（锁外）：真正发起请求。
    pub async fn run_stream(
        p: StreamParts,
        pcm: Vec<u8>,
        lang: String,
        translate: bool,
        video_start_ms: f64,
    ) -> impl futures_util::Stream<Item = String> {
        let stream = nexus_subtitle::stream::stream_transcribe(
            p.client,
            p.base_url,
            p.model,
            pcm,
            lang,
            translate,
            video_start_ms,
            p.engine,
            p.batch,
            Some(p.recent),
        )
        .await;
        // 让活动 guard 跟着流走：流没被消费完（或中途断开）也不提前归还。
        let guard = p.activity;
        stream.map(move |item| {
            let _keep = &guard;
            item
        })
    }

    /// 状态查询第一步（锁内）：取探针句柄与展示字段快照。
    pub fn status_parts(&self) -> StatusParts {
        StatusParts {
            asr_backend: self.cfg.asr.backend.clone(),
            asr_device: self.effective_backend(),
            asr_port: self.cfg.asr.port,
            asr_language: self.cfg.asr.language.clone(),
            asr_model: self.cfg.asr.model.clone(),
            mt_enabled: self.engine.mt_enabled(),
            translate_backend: self.engine.backend_name(),
            translate_model: self.engine.model_name(),
            translate_local: self.engine.backend_kind() == BackendKind::Local,
            mt_warm: self.engine.mt_warm(),
            base_url: self.engine.base_url(),
            idle_release_min: self.idle_release_min(),
            last_req_ts: self.last_req_ts(),
            max_sec: self.cfg.segment.max_sec,
            max_chars: self.cfg.segment.max_chars,
            recommended_chunk_sec: self.cfg.asr.recommended_chunk_sec,
            asr: Arc::clone(&self.audiocpp),
            mt: Arc::clone(&self.llama),
        }
    }

    /// 状态查询第二步（锁外）：先 probe 再组装 JSON（probe 是 HTTP 调用）。
    ///
    /// `mt_up` 只对 local 有意义：openai/ollama 是外部服务，none 不翻译，
    /// 三者都不该拿 llama-server 的探活结果去影响 `running`。
    pub fn status_json(p: &StatusParts, asr_up: bool, mt_up: bool) -> serde_json::Value {
        let mt_active = p.translate_local && mt_up;
        // 严格就绪：识别热 + （非本地翻译 或 本地翻译已拉起且已预热）。
        // R63.2 教训：只有 asr_ready 就放行 = 头显首句撞 llama 冷加载（15-40s）。
        let ready = asr_up && (!p.translate_local || (mt_up && p.mt_warm));
        serde_json::json!({
            "running": asr_up || mt_active,
            "ready": ready,
            "mt_warm": p.mt_warm,
            "asr": {
                "backend": p.asr_backend,
                "device": p.asr_device,
                "port": p.asr_port,
                "language": p.asr_language,
                "up": asr_up,
            },
            "translate": {
                "backend": p.translate_backend,
                "model": p.translate_model,
                "mt_enabled": p.mt_enabled,
                "local": p.translate_local,
                "base_url": p.base_url,
                "up": mt_active,
            },
            "segment": {
                "max_sec": p.max_sec,
                "max_chars": p.max_chars,
            },
            "idle_release_min": p.idle_release_min,
            "last_req_ts": p.last_req_ts,
        })
    }

    /// 启动第一步（锁内）：取句柄与启动参数。
    /// 实际 ASR 后端：
    /// - 配置 cuda + CUDA 包 + NVIDIA → cuda
    /// - 配置非 cpu + Vulkan 包 + Vulkan GPU（AMD/Intel/NVIDIA）→ vulkan
    /// - 否则 cpu
    pub fn effective_backend(&self) -> String {
        let vendor = self.models_dir.join("..").join("vendor").join("audiocpp");
        let cuda = vendor.join("gpu").join("audiocpp_server.exe");
        let vulkan = vendor.join("vulkan").join("audiocpp_server.exe");
        let want_gpu = !self
            .cfg
            .asr
            .device_backend
            .eq_ignore_ascii_case("cpu");
        if want_gpu
            && self.cfg.asr.device_backend.eq_ignore_ascii_case("cuda")
            && cuda.is_file()
            && has_nvidia_gpu()
        {
            return "cuda".into();
        }
        if want_gpu && vulkan.is_file() && vulkan_gpu_available() {
            return "vulkan".into();
        }
        "cpu".into()
    }

    /// 翻译运行时选择：NVIDIA 优先可选 CUDA（新目录或旧版 vendor/llama），
    /// 否则用随包的 Vulkan 运行时。
    pub fn effective_llama_dir(&self) -> PathBuf {
        let app = self.models_dir.parent().unwrap_or_else(|| std::path::Path::new("."));
        let cuda = app.join("vendor").join("llama-cuda");
        let legacy = app.join("vendor").join("llama");
        let vulkan = app.join("vendor").join("llama-vulkan");
        if has_nvidia_gpu() {
            if cuda.join("llama-server.exe").is_file() {
                return cuda;
            }
            if legacy.join("llama-server.exe").is_file() {
                return legacy;
            }
        }
        if vulkan.join("llama-server.exe").is_file() {
            return vulkan;
        }
        self.cfg.llama_dir()
    }

    pub fn start_parts(&self) -> StartParts {
        let translate_local = self.engine.backend_kind() == BackendKind::Local;
        // 已经按语言切过模型就用当前模型，避免重启后回到默认模型
        let llama_model = self
            .engine
            .current_model_path()
            .unwrap_or_else(|| self.cfg.llama_model());
        if translate_local {
            // 让引擎记住宿主实际拉起的模型：否则首个英语请求会误以为
            // "当前模型就是 en" 而跳过切换（实际跑的是默认日文模型）。
            self.engine.set_current_model(Some(llama_model.clone()));
        }
        let asr_backend = self.effective_backend();
        StartParts {
            audiocpp: Arc::clone(&self.audiocpp),
            asr_exe: {
                let vendor = self.models_dir.join("..").join("vendor").join("audiocpp");
                match asr_backend.as_str() {
                    "cuda" => vendor.join("gpu").join("audiocpp_server.exe"),
                    "vulkan" => vendor.join("vulkan").join("audiocpp_server.exe"),
                    _ => vendor.join("cpu").join("audiocpp_server.exe"),
                }
            },
            asr_backend,
            llama: Arc::clone(&self.llama),
            llama_dir: self.effective_llama_dir(),
            local_exe: self.cfg.translate.local_exe.clone(),
            llama_model,
            engine: Arc::clone(&self.engine),
            translate_local,
        }
    }

    /// 启动第二步（锁外）：拉起两个常驻进程（幂等）。
    ///
    /// **翻译后端不是 local 时不拉 llama-server**：openai/ollama 是外部服务，
    /// none 是用户显式关闭。
    pub async fn run_start(p: &StartParts) -> (bool, bool, String) {
        let asr: Result<bool, String> = p
            .audiocpp
            .ensure_server_with_backend(&p.asr_exe, None, 60.0, Some(&p.asr_backend))
            .await
            .map(|_| true);
        let mt = if p.translate_local {
            let ok = p
                .llama
                .ensure_server(&p.llama_dir, &p.local_exe, &p.llama_model, None, 120.0)
                .await;
            match ok {
                Ok(_) => {
                    // 真实 prefill：llama 冷加载 15-40s，/health ok 不算热，
                    // 首句字幕请求会撞冷启动（R63.2/R63.3 的教训）
                    let _ = p.engine.warm_up_local("ja").await;
                    Ok(true)
                }
                Err(e) => Err(e),
            }
        } else {
            Ok(true) // 外部翻译后端/关闭翻译：不拉 llama，也不算失败
        };
        let mut errs = Vec::new();
        if let Err(e) = &asr {
            errs.push(format!("ASR: {e}"));
        }
        if let Err(e) = &mt {
            errs.push(format!("翻译: {e}"));
        }
        (asr.is_ok(), mt.is_ok(), errs.join("；"))
    }

    /// 停止第二步（锁外）：只停 start_parts 给出的这两个实例。
    ///
    /// 返回停止前是否真有本程序拉起的进程在跑：前端据此区分"已停止"与
    /// "本来就没在运行"（Python `sub_stop()` 的 `stopped` 字段）。
    pub async fn run_stop(p: &StartParts) -> bool {
        let was_running = p.audiocpp.is_running().await || p.llama.is_running().await;
        p.audiocpp.stop().await;
        p.llama.stop().await;
        was_running
    }

    /// 重新读配置（界面改完参数后调用）。
    ///
    /// 旧实例可能已经拉起了 audiocpp/llama 子进程：先停干净再换新实例，
    /// 否则旧进程失去管理者，占端口/显存直到进程退出。
    /// 锁内快步版 reload：读配置、换上**新实例**（不停旧进程），旧实例交回
    /// 调用方在锁外停。原 `reload` 在锁内 await 秒级杀进程，会冻结全部字幕
    /// 接口（历史"锁内慢操作=假死"同型；慢操作必须放锁外）。
    pub fn reload_swap(&mut self) -> (Arc<AudioCppBackend>, Arc<LlamaBackend>) {
        let old_audiocpp = Arc::clone(&self.audiocpp);
        let old_llama = Arc::clone(&self.llama);
        let cfg = SubtitleConfig::load(&self.config_path);
        let model_path = resolve_asr_model(&cfg.asr.model, &cfg.base_dir, &self.models_dir);
        self.audiocpp = Arc::new(AudioCppBackend::new(
            cfg.asr.clone(),
            model_path,
            self.run_dir.clone(),
        ));
        self.llama = Arc::new(LlamaBackend::new(cfg.translate.clone()));
        self.engine = Arc::new(TranslateEngine::new(
            cfg.translate.clone(),
            Arc::clone(&self.llama),
        ));
        self.cfg = cfg;
        self.touch();
        (old_audiocpp, old_llama)
    }

    pub async fn reload(&mut self) {
        // 停旧移到实例替换之后：停的是交回来的旧实例（管理权没丢），
        // 语义与旧实现（先停后换）等价
        let (old_a, old_l) = self.reload_swap();
        old_a.stop().await;
        old_l.stop().await;
    }

    /// 拉起两个常驻进程（幂等）。便捷入口 = 两段式组合；持锁方不要用它（会拿锁跨网络）。
    pub async fn start(&self) -> (bool, bool, String) {
        let p = self.start_parts();
        Self::run_start(&p).await
    }

    pub async fn stop(&self) {
        self.audiocpp.stop().await;
        self.llama.stop().await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // 每个用例独立目录：同进程并行跑，共用目录会互相删文件（进程 id 相同挡不住）
    fn tmp_cfg(tag: &str) -> (PathBuf, PathBuf) {
        let d = std::env::temp_dir().join(format!("nexus-sub-svc-{}-{tag}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        (d.join("subtitle_config.json"), d)
    }

    #[test]
    fn loads_and_exposes_config() {
        let (p, d) = tmp_cfg("load");
        let s = SubtitleService::new(p, d.clone(), d.join("run"));
        assert_eq!(s.cfg.asr.port, 8081);
        assert!(s.engine.mt_enabled());
        // 宿主拉起 llama 时要把"实际模型"同步给引擎（否则首个英语请求会跳过切换）
        let sp = s.start_parts();
        assert_eq!(
            s.engine.current_model_path(),
            Some(s.cfg.llama_model()),
            "启动参数与引擎记录的模型必须一致"
        );
        assert_eq!(sp.llama_model, s.cfg.llama_model());
        let _ = std::fs::remove_dir_all(&d);
    }

    #[tokio::test]
    async fn reload_picks_up_changes() {
        let (p, d) = tmp_cfg("reload");
        let mut s = SubtitleService::new(p.clone(), d.clone(), d.join("run"));
        std::fs::write(&p, r#"{"asr":{"audiocpp":{"port":7777}}}"#).unwrap();
        s.reload().await;
        assert_eq!(s.cfg.asr.port, 7777);
        let _ = std::fs::remove_dir_all(&d);
    }

    #[tokio::test]
    async fn status_reports_down_when_nothing_running() {
        // 必须把端口指到没人听的地方：开发机上 8081/8082 很可能正跑着真服务，
        // 用默认配置断言 "up=false" 会随环境飘（这个测试自己就飘过一次）
        let (p, d) = tmp_cfg("status");
        std::fs::write(
            &p,
            r#"{"asr":{"audiocpp":{"port":1}},"translate":{"local":{"base_url":"http://127.0.0.1:1"}}}</b>"#
                .replace("</b>", ""),
        )
        .unwrap();
        let s = SubtitleService::new(p, d.clone(), d.join("run"));
        let parts = s.status_parts();
        let (asr_up, mt_up) = tokio::join!(parts.asr.probe(), parts.mt.probe());
        let st = SubtitleService::status_json(&parts, asr_up, mt_up);
        assert_eq!(st["running"], false);
        assert_eq!(st["asr"]["up"], false);
        assert_eq!(st["translate"]["up"], false);
        assert_eq!(st["translate"]["backend"], "local");
        assert_eq!(st["translate"]["local"], true);
        assert!(st["translate"]["model"]
            .as_str()
            .unwrap()
            .ends_with(".gguf"));
        let _ = std::fs::remove_dir_all(&d);
    }

    #[tokio::test]
    async fn none_backend_disables_mt_and_never_launches_llama() {
        let (p, d) = tmp_cfg("none");
        std::fs::write(
            &p,
            r#"{"asr":{"audiocpp":{"model":"../../models/Qwen3-ASR-0.6B"}},"translate":{"backend":"none"}}"#,
        )
        .unwrap();
        let s = SubtitleService::new(p, d.clone(), d.join("run"));
        let parts = s.status_parts();
        assert_eq!(parts.translate_backend, "none");
        assert_eq!(parts.translate_model, "");
        assert!(!parts.mt_enabled);
        assert!(!parts.translate_local);

        let sp = s.start_parts();
        assert!(!sp.translate_local, "none 后端不该拉起 llama-server");

        let st = SubtitleService::status_json(&parts, true, false);
        assert_eq!(st["running"], true, "非本地翻译后端不要求 llama 在跑");
        assert_eq!(st["translate"]["mt_enabled"], false);
        assert_eq!(st["translate"]["up"], false);
        let _ = std::fs::remove_dir_all(&d);
    }

    #[tokio::test]
    async fn openai_backend_reports_model_and_skips_local_probe() {
        let (p, d) = tmp_cfg("cloud");
        std::fs::write(
            &p,
            r#"{"asr":{"audiocpp":{"model":"../../models/Qwen3-ASR-0.6B"}},"translate":{"backend":"cloud","openai":{"base_url":"https://api.example.com/v1","model":"gpt-4o-mini"},"local":{"base_url":"http://127.0.0.1:1"}}}"#,
        )
        .unwrap();
        let s = SubtitleService::new(p, d.clone(), d.join("run"));
        let parts = s.status_parts();
        assert_eq!(parts.translate_backend, "openai", "别名 cloud 要归一");
        assert_eq!(parts.translate_model, "gpt-4o-mini");
        assert_eq!(parts.base_url, "https://api.example.com/v1");
        assert!(parts.mt_enabled);
        assert!(!parts.translate_local);
        assert!(!s.start_parts().translate_local);

        // 即使 llama 探活为 false（未拉起），running 仍按 ASR 判定
        let st = SubtitleService::status_json(&parts, true, false);
        assert_eq!(st["running"], true);
        assert_eq!(st["translate"]["backend"], "openai");
        assert_eq!(st["translate"]["model"], "gpt-4o-mini");
        assert_eq!(st["translate"]["mt_enabled"], true);
        assert_eq!(st["translate"]["local"], false);
        let _ = std::fs::remove_dir_all(&d);
    }

    #[tokio::test]
    async fn idle_clock_respects_inflight_and_threshold() {
        let clock = ActivityClock::new();
        assert!(!clock.idle_due(0.0), "0 = 关闭回收");
        assert!(!clock.idle_due(5.0), "刚活动过不该到期");

        // 阈值 0.001 分钟 = 60ms
        tokio::time::sleep(std::time::Duration::from_millis(80)).await;
        assert!(clock.idle_due(0.001));

        // 有在飞请求时绝不回收（防止处理到一半被杀）
        let guard = clock.begin();
        assert_eq!(clock.inflight(), 1);
        assert!(!clock.idle_due(0.001));
        drop(guard);
        assert_eq!(clock.inflight(), 0);
        // begin() 会刷新活动时间，归还后再等满一个窗口才会到期
        tokio::time::sleep(std::time::Duration::from_millis(80)).await;
        assert!(clock.idle_due(0.001));
        assert!(clock.last_req_ts() > 0.0, "对外展示的墙钟时间戳必须有效");
    }
}
