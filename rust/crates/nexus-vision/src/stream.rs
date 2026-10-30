//! 流式推理：视频 → 切点 → 逐组编码 → 增量解码 → 位置合成 → 动作点。
//!
//! 与 Python `onnx_stream.py` 同形，四段各用已验证的实现，这一层只负责推进与发布。
//!
//! 为什么流式：整片跑完再出点，用户要等几分钟；分块推进可以边播边出点。
//! 代价是包络 AR 状态按块重置（与 Python 的 chunk 语义一致），流式结果与离线
//! 整片结果**不逐位相同** —— 这是流式的取舍，不是移植偏差。

use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use crate::bundle::Bundle;
use crate::buffer::Action;
use crate::compose::StyleParams;
use crate::onnx::{Decoder, EncodeStreamer, Encoder, Tracks, ENC_RES_DEFAULT, GROUP};
use crate::style::extrema_actions;
use crate::transnet::TransNet;

/// 每次发布新动作所需的最少新增行数（与编码组对齐）。
pub const PUBLISH_STEP: usize = GROUP;

/// latent 行数硬上限：≈2.3 小时 @30fps（250_000 行 × row_len 36864 字节 ≈ 8.6GB 内存预算）。
/// 超限直接报错；分块落盘是后续项，暂不实现。
const MAX_LATENT_ROWS: usize = 250_000;

#[derive(Default)]
pub struct StreamState {
    pub actions: Vec<Action>,
    pub rows_done: usize,
    pub total_rows: usize,
    pub cuts: Vec<usize>,
    pub finished: bool,
    pub error: String,
    /// 当前阶段的中文提示（转码/切点/编码中/完成），给预热遮罩显示
    pub stage: String,
    /// 静止门用：逐行原始 vmarg / conf / 秒时间轴（与动作表同一次发布）
    pub vm: Vec<f64>,
    pub conf: Vec<f64>,
    pub times_sec: Vec<f64>,
    /// 每次动作表/信号发布 +1：消费方（pump）据此做脏检查，避免每拍
    /// 全量 clone 三条全长轨道信号（长片 25 万行 ≈ 6MB×3/秒级）
    pub publish_epoch: u64,
}

pub struct StreamEngine {
    bundle: Bundle,
    params: StyleParams,
    state: Arc<Mutex<StreamState>>,
    stop: Arc<AtomicBool>,
    running: Arc<AtomicUsize>,
}

impl StreamEngine {
    pub fn new(bundle: Bundle, params: StyleParams) -> Self {
        Self {
            bundle,
            params,
            state: Arc::new(Mutex::new(StreamState::default())),
            stop: Arc::new(AtomicBool::new(false)),
            running: Arc::new(AtomicUsize::new(0)),
        }
    }

    pub fn snapshot(&self) -> (Vec<Action>, usize, usize, bool, String) {
        let st = self.state.lock().unwrap_or_else(|p| p.into_inner());
        (
            st.actions.clone(),
            st.rows_done,
            st.total_rows,
            st.finished,
            st.error.clone(),
        )
    }

    /// 当前发布的轨道信号（原始 vmarg/conf + 秒时间轴）。
    pub fn signals(&self) -> (Vec<f64>, Vec<f64>, Vec<f64>) {
        let st = self.state.lock().unwrap_or_else(|p| p.into_inner());
        (st.vm.clone(), st.conf.clone(), st.times_sec.clone())
    }

    /// 发布代数：动作表或信号每次发布后递增。
    pub fn publish_epoch(&self) -> u64 {
        self.state
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .publish_epoch
    }

    /// 播放头之前的动作（有序表前缀）与覆盖末行。
    ///
    /// take_while 在锁内截断后只拷前缀——先整表 clone 再过滤在长片后期
    /// （25 万行）是每拍一次的全量大拷贝。
    pub fn head_before(&self, playhead_ms: f64) -> (Vec<crate::Action>, f64) {
        let st = self.state.lock().unwrap_or_else(|p| p.into_inner());
        let head: Vec<crate::Action> = st
            .actions
            .iter()
            .take_while(|a| a.at <= playhead_ms)
            .cloned()
            .collect();
        let until = st.times_sec.last().map(|t| t * 1000.0).unwrap_or(0.0);
        (head, until)
    }

    /// 当前阶段提示；空串表示还没进入任何阶段。
    pub fn stage(&self) -> String {
        let st = self.state.lock().unwrap_or_else(|p| p.into_inner());
        st.stage.clone()
    }

    /// 已解码时间轴末行（毫秒），对应 Python 的 `gen_until=times[-1]`。
    pub fn coverage_ms(&self) -> f64 {
        let st = self.state.lock().unwrap_or_else(|p| p.into_inner());
        st.times_sec.last().map(|t| t * 1000.0).unwrap_or(0.0)
    }

    pub fn stop(&self) {
        self.stop.store(true, Ordering::SeqCst);
    }

    pub fn is_running(&self) -> bool {
        self.running.load(Ordering::SeqCst) > 0
    }

    /// 启动流式处理。
    pub fn start(&self, video: &Path, total_rows: usize, fps: f64) -> Result<(), String> {
        if self.is_running() {
            return Err("已有流式任务在跑".into());
        }
        let video = video.to_path_buf();
        let bundle = self.bundle.clone();
        let params = self.params.clone();
        let state = Arc::clone(&self.state);
        let stop = Arc::clone(&self.stop);
        let running = Arc::clone(&self.running);

        {
            let mut st = state.lock().unwrap_or_else(|p| p.into_inner());
            *st = StreamState {
                total_rows,
                ..Default::default()
            };
        }
        stop.store(false, Ordering::SeqCst);
        running.fetch_add(1, Ordering::SeqCst);

        std::thread::Builder::new()
            .name("vision-stream".into())
            .spawn(move || {
                // 推理线程不允许带着 panic 死掉：panic 也必须置 finished、归还 running，
                // 否则 is_running 永远为真，后续 open/start 全部卡死
                let res = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    run(&video, total_rows, fps, &bundle, &params, &state, &stop)
                }));
                let mut st = state.lock().unwrap_or_else(|p| p.into_inner());
                st.finished = true;
                match res {
                    Ok(Ok(())) => {}
                    Ok(Err(e)) => {
                        if !stop.load(Ordering::SeqCst) {
                            // 错误必须落日志：st.error 只活在内存状态里，
                            // 没有这行 /api/logs 和 host.log 都查不到现场
                            tracing::error!("视觉推理线程失败：{e}");
                            st.error = e;
                        }
                    }
                    Err(_) => {
                        if !stop.load(Ordering::SeqCst) {
                            tracing::error!("视觉推理线程 panic");
                            st.error = "推理线程异常退出".to_string();
                        }
                    }
                }
                running.fetch_sub(1, Ordering::SeqCst);
            })
            .map_err(|e| {
                // spawn 失败必须归还计数：否则 is_running() 永真，后续 open 全被挡
                self.running.fetch_sub(1, Ordering::SeqCst);
                format!("起不了流式线程：{e}")
            })?;
        Ok(())
    }
}

/// run() 主循环期间持有 ffmpeg 子进程：任何 `?` 提前返回（编码/解码/内存上限
/// 等错误路径）都经 Drop 先 kill+wait，不留僵尸 ffmpeg；正常 break 路径用
/// take_child 自行回收后 guard 已空。
struct ChildGuard(Option<std::process::Child>);

impl ChildGuard {
    fn child_mut(&mut self) -> &mut std::process::Child {
        self.0.as_mut().expect("ChildGuard 空了")
    }
    fn take_child(&mut self) -> std::process::Child {
        self.0.take().expect("ChildGuard 空了")
    }
}

impl Drop for ChildGuard {
    fn drop(&mut self) {
        if let Some(mut c) = self.0.take() {
            let _ = c.kill();
            let _ = c.wait();
        }
    }
}

/// 转码过程中的临时文件：保留 `.mp4` 扩展名，否则 ffmpeg 无法推断容器。
fn part_path(out: &Path) -> PathBuf {
    let name = out
        .file_name()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_default();
    out.with_file_name(format!("{name}.{}.part.mp4", std::process::id()))
}

/// 删除同 stem 的孤儿 `.part.mp4`（mtime > 2h）：上次崩溃/强杀留下的半成品，
/// 名字带旧 pid 不会被本轮复用。超时门槛避免误删双实例正在写的文件。
fn cleanup_orphan_parts(video: &Path, stem: &str) {
    let Some(dir) = video.parent() else { return };
    let Ok(rd) = std::fs::read_dir(dir) else { return };
    let me = std::process::id().to_string();
    let now = std::time::SystemTime::now();
    for e in rd.flatten() {
        let name = e.file_name().to_string_lossy().to_string();
        if !name.starts_with(&format!("{stem}.")) || !name.ends_with(".part.mp4") {
            continue;
        }
        if name.contains(&format!(".{me}.part.mp4")) {
            continue; // 本轮自己的
        }
        let stale = e
            .metadata()
            .ok()
            .and_then(|m| m.modified().ok())
            .and_then(|t| now.duration_since(t).ok())
            .map(|d| d.as_secs() > 7200)
            .unwrap_or(false);
        if stale {
            let _ = std::fs::remove_file(e.path());
        }
    }
}

/// 缓存命中结局：命中 / 本次转好 / 转码失败回退原片。
pub enum TranscodeOutcome {
    /// 直接复用既有缓存。
    Hit(PathBuf),
    /// 本次新转出来的。
    Fresh(PathBuf),
    /// 转码失败或被取消，回退用原片直解（~0.4x 实时，很慢）。
    Fallback(PathBuf),
}

/// 源文件指纹（大小+mtime）：同 stem 不同内容（a.mp4 / a.mkv、跨目录同名）
/// 必须拿到各自的缓存，否则推理会跑在错误视频上。
fn source_fingerprint(video: &Path) -> String {
    match std::fs::metadata(video) {
        Ok(m) => {
            let secs = m
                .modified()
                .ok()
                .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                .map(|d| d.as_secs())
                .unwrap_or(0);
            format!("{}-{:x}", m.len(), secs)
        }
        Err(_) => "unknown".into(),
    }
}

/// 转码缓存：480p30 x264（参数与 Python `_transcode` 的差别：preset 用
/// **veryfast**——这份缓存只喂 48×27 切点与低分辨率识别，medium 的编码耗时
/// 是 veryfast 的 3-5 倍，对 4K-8K 源就是"预热卡 0% 半小时"（用户实测）；
/// 命名多一段源指纹防同名互拿）。
///
/// 直接从 4K 源解码只有 ~0.4x 实时，编码永远追不上播放；原型的 1.8x 靠的
/// 就是先转 480p 再解码。缓存命中时直接复用。
///
/// 先写唯一临时文件、成功后原子改名：取消/重开时不会把半成品当成缓存。
/// `-progress pipe:1` 把转码进度回写 stage——转码阶段没有任何行进度，
/// 不回写的话前端只能显示 0%，用户无从判断是在跑还是死了。
fn transcode_cached(
    video: &Path,
    stop: &AtomicBool,
    state: &Arc<Mutex<StreamState>>,
    duration_s: f64,
) -> TranscodeOutcome {
    let stem = video
        .file_stem()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_default();
    let name = format!("{stem}.{}-480p30.mp4", source_fingerprint(video));
    let mut cands: Vec<PathBuf> = Vec::new();
    if let Some(dir) = video.parent() {
        cands.push(dir.join(&name));
    }
    if let Ok(dir) = std::env::var("NEXUS_VISION_CACHE") {
        cands.push(PathBuf::from(dir).join(&name));
    }
    if let Some(p) = cands.iter().find(|p| p.is_file()) {
        return TranscodeOutcome::Hit(p.clone());
    }
    // 输出位置必须可写：视频可能在只读媒体目录/网络盘上，直接往同目录写
    // 会让 ffmpeg 立刻失败并回退 4K 直解（慢到像卡死）。逐个试建 part 文件
    // 挑第一个可写位置，全不可写才退系统临时目录。
    let out = cands
        .iter()
        .find(|c| {
            if let Some(dir) = c.parent() {
                let _ = std::fs::create_dir_all(dir);
            }
            let probe = part_path(c);
            let ok = std::fs::File::create(&probe).is_ok();
            let _ = std::fs::remove_file(&probe);
            ok
        })
        .cloned()
        .unwrap_or_else(|| std::env::temp_dir().join(&name));
    let part = part_path(&out);
    let _ = std::fs::remove_file(&part);
    // 清理同 stem 的孤儿 part（上次崩溃/强杀残留，文件名带旧 pid）：只删
    // mtime 超 2 小时的，避免误删双实例正在写的文件。这类残留会一直躺在
    // 用户视频目录里（"多出来的 480p 附件"），且占磁盘。
    cleanup_orphan_parts(video, &stem);
    set_stage(state, "正在准备 480p30 转码（首次较慢，之后秒开）…");

    let mut cmd = Command::new("ffmpeg");
    cmd.args([
        "-y", "-nostdin", "-v", "error",
        "-progress", "pipe:1", "-nostats",
        "-i",
    ])
    .arg(video)
    .args([
        "-vf", "scale=-2:480,fps=30",
        "-c:v", "libx264", "-crf", "23", "-preset", "veryfast", "-an",
    ])
    .arg(&part)
    .stdout(Stdio::piped())
    .stderr(Stdio::null());
    crate::hide_console_std(&mut cmd);
    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(_) => return TranscodeOutcome::Fallback(video.to_path_buf()),
    };

    // -progress 的 key=value 块打到 stdout：读线程持续收，主循环取最新
    // out_time_us 换算百分比回写 stage（转码是预热最慢的一段，必须有反馈）
    let progress: Arc<Mutex<String>> = Arc::new(Mutex::new(String::new()));
    let mut progress_pipe = child.stdout.take();
    {
        let progress = Arc::clone(&progress);
        std::thread::spawn(move || {
            if let Some(p) = progress_pipe.as_mut() {
                use std::io::Read;
                let mut buf = Vec::new();
                let _ = p.read_to_end(&mut buf);
                if let Ok(mut g) = progress.lock() {
                    *g = String::from_utf8_lossy(&buf).into_owned();
                }
            }
        });
    }

    // 停滞检测：part 文件大小 N 秒不涨 = ffmpeg 卡在坏帧/坏容器上（用户实测
    // "6MB 后半小时不动"）。没有这道防线就无限期等，前端永远停在 0%。
    let mut last_size = 0u64;
    let mut last_grow = std::time::Instant::now();
    const STALL_LIMIT: std::time::Duration = std::time::Duration::from_secs(180);
    let status = loop {
        match child.try_wait() {
            Ok(Some(s)) => break s,
            Ok(None) => {
                if stop.load(Ordering::SeqCst) {
                    let _ = child.kill();
                    let _ = child.wait();
                    let _ = std::fs::remove_file(&part);
                    return TranscodeOutcome::Fallback(video.to_path_buf());
                }
                // 停滞检测（先查文件是否还在长）
                let size = std::fs::metadata(&part).map(|m| m.len()).unwrap_or(0);
                if size > last_size {
                    last_size = size;
                    last_grow = std::time::Instant::now();
                } else if last_grow.elapsed() >= STALL_LIMIT && size > 0 {
                    eprintln!(
                        "nexus-vision: 转码停滞 {}s（停在 {:.1}MB），放弃转码改用原片直解",
                        STALL_LIMIT.as_secs(),
                        size as f64 / 1_048_576.0
                    );
                    let _ = child.kill();
                    let _ = child.wait();
                    let _ = std::fs::remove_file(&part);
                    set_stage(
                        state,
                        "转码停滞（源文件可能有坏帧），改用原片直解（会更慢）…",
                    );
                    return TranscodeOutcome::Fallback(video.to_path_buf());
                }
                // 解析最新进度（key=value 块，取最后一个 out_time_us）
                if duration_s > 0.0 {
                    let text = progress.lock().unwrap_or_else(|p| p.into_inner()).clone();
                    if let Some(pos) = text.rfind("out_time_us=") {
                        let rest = &text[pos + "out_time_us=".len()..];
                        let us: f64 = rest
                            .lines()
                            .next()
                            .and_then(|l| l.trim().parse::<f64>().ok())
                            .unwrap_or(0.0);
                        if us > 0.0 {
                            let pct = (us / 1_000_000.0 / duration_s * 100.0).clamp(0.0, 99.0);
                            set_stage(
                                state,
                                format!("转码中 {pct:.0}%（480p 缓存，仅首次需要）…"),
                            );
                        }
                    }
                }
                std::thread::sleep(std::time::Duration::from_millis(500));
            }
            Err(_) => {
                let _ = child.kill();
                let _ = child.wait();
                let _ = std::fs::remove_file(&part);
                return TranscodeOutcome::Fallback(video.to_path_buf());
            }
        }
    };

    if status.success() && part.is_file() {
        // 另一个引擎可能已经写好了同一份缓存：有就直接复用
        if out.is_file() {
            let _ = std::fs::remove_file(&part);
            return TranscodeOutcome::Hit(out);
        }
        match std::fs::rename(&part, &out) {
            Ok(()) => TranscodeOutcome::Fresh(out),
            Err(_) => {
                let _ = std::fs::remove_file(&part);
                TranscodeOutcome::Fallback(video.to_path_buf())
            }
        }
    } else {
        let _ = std::fs::remove_file(&part);
        TranscodeOutcome::Fallback(video.to_path_buf())
    }
}

fn set_stage(state: &Arc<Mutex<StreamState>>, stage: impl Into<String>) {
    let mut st = state.lock().unwrap_or_else(|p| p.into_inner());
    st.stage = stage.into();
}

fn run(
    video: &Path,
    total_rows: usize,
    fps: f64,
    bundle: &Bundle,
    params: &StyleParams,
    state: &Arc<Mutex<StreamState>>,
    stop: &Arc<AtomicBool>,
) -> Result<(), String> {
    let res = ENC_RES_DEFAULT;
    let fsz = res * res * 3;
    let row_len = bundle.row_bytes();

    // 先转码到 480p30（缓存复用）：切点和编码都从缓存解码，不再反复解 4K
    set_stage(state, "正在准备 480p30 转码（首次较慢）…");
    // 转码进度需要时长（estimate_rows 给不出时进度条退化为纯文字）
    let duration_s = total_rows as f64 / fps.max(0.1);
    let trans = match transcode_cached(video, stop.as_ref(), state, duration_s) {
        TranscodeOutcome::Hit(p) => p,
        TranscodeOutcome::Fresh(p) => {
            set_stage(state, "转码完成，正在计算镜头切点…");
            p
        }
        TranscodeOutcome::Fallback(p) => {
            if !stop.load(Ordering::SeqCst) {
                set_stage(state, "转码失败/被取消，正在用原片直解（速度受限）…");
            }
            p
        }
    };
    if stop.load(Ordering::SeqCst) {
        return Ok(());
    }

    // 1) 切点（整片一次算，快）：分离的 ffmpeg 通道，尺寸小
    set_stage(state, "正在计算镜头切点…");
    let cuts = match TransNet::load(bundle)
        .and_then(|mut tn| tn.cuts(&trans, fps, Some(&*stop), Some((state, total_rows))))
    {
        Ok(c) => c,
        Err(e) => {
            if stop.load(Ordering::SeqCst) {
                return Ok(());
            }
            // Python `_cuts_worker` 失败只提示、按无切点继续；Rust 不能因此 0 动作
            set_stage(state, format!("镜头切点失败，按无切点继续：{e}"));
            Vec::new()
        }
    };
    {
        let mut st = state.lock().unwrap_or_else(|p| p.into_inner());
        st.cuts = cuts.clone();
    }

    // 2) 编码 + 增量解码：只解新增 chunk，绝不回头重算
    set_stage(state, "正在加载模型…");
    let enc = Encoder::load(bundle)?;
    let mut streamer = EncodeStreamer::new(enc, res);
    let mut dec = Decoder::load(bundle)?;
    let mut tracks = Tracks::empty(total_rows.max(1024));
    let mut next_row = 0usize;
    let mut rev_bias: Vec<f64> = Vec::new();
    let mut rev_bias_rows = 0usize;

    let mut cmd = Command::new("ffmpeg");
    cmd.args([
        "-v", "error", "-nostdin", "-i",
        &trans.to_string_lossy(),
        "-vf", &format!("fps={fps},scale={res}:{res}"),
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ])
    .stdout(Stdio::piped())
    .stderr(Stdio::null());
    crate::hide_console_std(&mut cmd);
    let mut child_guard = ChildGuard(Some(
        cmd.spawn().map_err(|e| format!("起不了 ffmpeg：{e}"))?,
    ));
    let mut out = child_guard.child_mut().stdout.take().ok_or("拿不到 ffmpeg 输出")?;

    // 预分配按上限钳制：损坏容器让 ffprobe 报出异常超大时长时，
    // 未钳制的巨量分配失败会直接 abort 整个宿主进程。
    let mut lat: Vec<i8> = Vec::with_capacity(total_rows.clamp(1024, MAX_LATENT_ROWS) * row_len);
    let mut last_publish = 0usize;
    let mut n_frames = 0usize;

    loop {
        if stop.load(Ordering::SeqCst) {
            // kill 后 wait 收尸，不留僵尸进程
            let mut c = child_guard.take_child();
            let _ = c.kill();
            let _ = c.wait();
            return Ok(());
        }
        let mut buf = vec![0u8; fsz];
        match out.read_exact(&mut buf) {
            Ok(()) => n_frames += 1,
            Err(_) => break,
        }
        let rows = streamer.push(buf)?;
        if rows.is_empty() {
            continue;
        }
        lat.extend_from_slice(&rows);
        if lat.len() / row_len > MAX_LATENT_ROWS {
            return Err("视频过长：latent 超出内存上限".to_string());
        }
        let done = lat.len() / row_len;
        {
            let mut st = state.lock().unwrap_or_else(|p| p.into_inner());
            st.rows_done = done;
            // 首批解码要等 chunk+ctx 行；这段编码期也必须让用户看到进度在走
            st.stage = format!("正在编码并识别（{done}/{total_rows} 行）…");
        }
        if done >= last_publish + PUBLISH_STEP {
            last_publish = done;
            // 只有积够 chunk+ctx 才真正解码；否则继续攒行，避免每 64 行都做无用功
            if dec.decode_step(&lat, done, &cuts, &mut tracks, &mut next_row, false)? {
                publish(
                    bundle,
                    params,
                    &tracks,
                    next_row,
                    &cuts,
                    fps,
                    state,
                    &mut rev_bias,
                    &mut rev_bias_rows,
                )?;
            }
        }
    }
    let mut child = child_guard.take_child();
    let _ = child.kill();
    let _ = child.wait();

    // 一帧都没解出来：不再静默成功，否则上层会拿到空结果却以为正常
    if n_frames == 0 {
        return Err("ffmpeg 未解出任何帧（文件损坏或编码不支持）".to_string());
    }

    // 3) 收尾：补满一组再 flush（与 extract.end() 一致）
    let tail = streamer.finish()?;
    if !tail.is_empty() {
        lat.extend_from_slice(&tail);
        if lat.len() / row_len > MAX_LATENT_ROWS {
            return Err("视频过长：latent 超出内存上限".to_string());
        }
    }
    let done = lat.len() / row_len;
    if done > 0 {
        {
            let mut st = state.lock().unwrap_or_else(|p| p.into_inner());
            st.rows_done = done;
        }
        dec.decode_step(&lat, done, &cuts, &mut tracks, &mut next_row, true)?;
        if next_row > 0 {
            publish(
                bundle,
                params,
                &tracks,
                next_row,
                &cuts,
                fps,
                state,
                &mut rev_bias,
                &mut rev_bias_rows,
            )?;
        }
    }
    set_stage(state, "识别完成");
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn publish(
    bundle: &Bundle,
    params: &StyleParams,
    tracks: &Tracks,
    rows: usize,
    cuts: &[usize],
    fps: f64,
    state: &Arc<Mutex<StreamState>>,
    rev_bias: &mut Vec<f64>,
    rev_bias_rows: &mut usize,
) -> Result<(), String> {
    if rows == 0 {
        return Ok(());
    }
    // onnx_stream 的行时间取窗口两帧中点：tbuf[a]+tbuf[a+K] 的均值
    let times: Vec<f64> = (0..rows).map(|i| (i as f64 + 2.0) * 1000.0 / fps).collect();
    let v_std = bundle.pack.v_std;
    let mut vel: Vec<f64> = tracks.vmarg[..rows]
        .iter()
        .map(|x| if x.is_finite() { x * v_std } else { 0.0 })
        .collect();
    let env: Vec<f64> = tracks.env[..rows].iter().map(|x| x * v_std).collect();

    // 切点行：镜头末行沿用前行速度（与离线一致）
    let mut edges: Vec<usize> = vec![0];
    let mut cs: Vec<usize> = cuts.iter().copied().filter(|c| *c > 0 && *c < rows).collect();
    cs.sort_unstable();
    cs.dedup();
    for &c in &cs {
        if c >= 2 && tracks.vmarg[c - 2].is_finite() {
            vel[c - 1] = tracks.vmarg[c - 2] * v_std;
        }
    }
    // onnx_stream 每次发布都把前缀末行当镜头末行沿用前行速度
    if rows >= 2 && tracks.vmarg[rows - 2].is_finite() {
        vel[rows - 1] = tracks.vmarg[rows - 2] * v_std;
    }
    edges.extend(cs.iter().copied());
    edges.push(rows);

    // 反转发射先验：bias=0 会让 Viterbi 欠发射、慢行程合并（原型 fit_rev_bias）。
    // 前缀每 30s 或翻倍时重拟合一次，和 Python `_style` 的节奏一致。
    let fit_step = crate::style::rows_at(30.0, fps, false).max(rows / 2);
    if rows >= crate::style::rows_at(30.0, fps, false)
        && (*rev_bias_rows == 0 || rows - *rev_bias_rows >= fit_step)
    {
        *rev_bias = crate::compose::fit_rev_bias(
            &tracks.rev_top[..rows],
            &tracks.rev_bot[..rows],
            &vel,
            fps,
            params.still_eps,
        );
        *rev_bias_rows = rows;
    }
    // 与 Python `_style` 一致：上次拟合只覆盖旧前缀时，用已拟合 bias 的均值
    // 补齐新行；直接退成 0 会让每个重拟合间隔内的发布重新丢失动作。
    if rev_bias.len() < rows && !rev_bias.is_empty() {
        let mean = rev_bias.iter().sum::<f64>() / rev_bias.len() as f64;
        rev_bias.resize(rows, mean);
    }
    let bias: &[f64] = if rev_bias.len() >= rows {
        &rev_bias[..rows]
    } else {
        &[0.0]
    };

    // 带亚帧时刻：动作点时间精确到行内，不只到 1/30 秒
    let (p, sub, force) = crate::compose::style_positions_with_sub(
        &vel,
        &tracks.level[..rows],
        &env,
        &edges,
        Some((&tracks.blo[..rows], &tracks.bhi[..rows])),
        Some((&tracks.plat_top[..rows], &tracks.plat_bot[..rows])),
        Some((&tracks.rev_top[..rows], &tracks.rev_bot[..rows])),
        bias,
        params,
        &times,
        params.subframe_rev,
    );
    let acts = extrema_actions(
        &p,
        &times,
        &edges,
        Some(&sub),
        Some(&force),
        250.0,
        1.0,
        fps,
    );

    let mut st = state.lock().unwrap_or_else(|p| p.into_inner());
    st.actions = acts;
    st.publish_epoch = st.publish_epoch.wrapping_add(1);
    st.vm = tracks.vmarg[..rows].to_vec();
    st.conf = tracks.conf[..rows].to_vec();
    st.times_sec = times.iter().map(|t| t / 1000.0).collect();
    Ok(())
}

/// 预算整片行数（用于预分配与进度显示），同时返回时长秒。
///
/// ffprobe 带 30s 超时：网络盘/损坏容器上 ffprobe 会挂死，调用方在
/// spawn_blocking 里同步等它——没有超时就是 open 永卡。
pub fn estimate_rows(video: &Path, fps: f64) -> Option<(usize, f64)> {
    let mut cmd = Command::new("ffprobe");
    cmd.args([
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        &video.to_string_lossy(),
    ]);
    crate::hide_console_std(&mut cmd);
    let out = probe_with_timeout(&mut cmd, 30.0)?;
    let s = String::from_utf8_lossy(&out).trim().to_string();
    let dur: f64 = s.parse().ok()?;
    Some(((dur * fps).round() as usize, dur))
}

/// 同步跑子进程收全 stdout，带超时秒；超时/失败返回 None。
pub(crate) fn probe_with_timeout(cmd: &mut Command, timeout_secs: f64) -> Option<Vec<u8>> {
    use std::process::Stdio;
    cmd.stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    let mut child = cmd.spawn().ok()?;
    let mut pipe = child.stdout.take()?;
    // stdout 用读线程持续收走；主线程轮询退出状态
    let reader = std::thread::spawn(move || {
        let mut buf = Vec::new();
        use std::io::Read;
        let _ = pipe.read_to_end(&mut buf);
        buf
    });
    let start = std::time::Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(_status)) => {
                let buf = reader.join().ok()?;
                return Some(buf);
            }
            Ok(None) => {
                if start.elapsed().as_secs_f64() >= timeout_secs {
                    let _ = child.kill();
                    let _ = child.wait();
                    return None;
                }
                std::thread::sleep(std::time::Duration::from_millis(50));
            }
            Err(_) => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
        }
    }
}

/// 输出目录（流式中间产物）。
pub fn default_out_dir(base: &Path) -> PathBuf {
    base.join("vision_stream")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn publish_step_is_group_aligned() {
        assert_eq!(PUBLISH_STEP, GROUP, "发布步长应与编码组一致");
    }

    #[test]
    fn estimate_rows_none_on_missing_file() {
        // 不存在的文件：ffprobe 失败，返回 None 而不是 panic
        assert!(estimate_rows(Path::new("Z:/nope.mp4"), 30.0).is_none());
    }

    #[test]
    fn transcode_part_path_keeps_mp4_extension() {
        // ffmpeg 靠扩展名推断容器；.part 结尾会直接报错，必须是 .part.mp4
        let p = part_path(Path::new("E:/v/K1.480p30.mp4"));
        assert_eq!(p.extension().and_then(|x| x.to_str()), Some("mp4"));
        assert!(p.file_name().unwrap().to_string_lossy().contains(".part."));
    }
}
