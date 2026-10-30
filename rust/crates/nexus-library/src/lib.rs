//! 媒体库（对应 Python `vendor/player/library.py`）。
//!
//! 与 Python 版的差别：**时长/缩略图探测改用 ffprobe/ffmpeg，不再依赖 libmpv**。
//! 理由：宿主里的外挂 mpv 播放器整块已删除（播放走内置 WebView 的 video 标签），
//! libmpv 只剩探时长/抽帧在用；而 ffmpeg 本来就是视觉链路的硬依赖，少一个 DLL。

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};

pub const VIDEO_EXTS: &[&str] = &[
    "mp4", "mkv", "avi", "mov", "wmv", "webm", "m4v", "mpg", "mpeg", "ts", "m2ts",
];

/// ffprobe 探测超时：坏文件/半截文件/网络盘卡住时保扫描不永久挂起。
const PROBE_TIMEOUT: Duration = Duration::from_secs(60);
/// ffmpeg 抽缩略图超时：大文件 seek 抽帧较慢，给宽一倍。
const THUMB_TIMEOUT: Duration = Duration::from_secs(120);

/// GUI 子系统下启动控制台程序会弹黑框，统一加 CREATE_NO_WINDOW。
#[cfg(windows)]
fn hide_console(cmd: &mut tokio::process::Command) {
    cmd.creation_flags(0x0800_0000);
}

#[cfg(not(windows))]
fn hide_console(_cmd: &mut tokio::process::Command) {}

fn ext_of(name: &str) -> String {
    Path::new(name)
        .extension()
        .map(|e| e.to_string_lossy().to_lowercase())
        .unwrap_or_default()
}

pub fn is_video(name: &str) -> bool {
    VIDEO_EXTS.contains(&ext_of(name).as_str())
}

/// 拼完整路径：**必须用 Path::join**，保证与索引键（to_string_lossy 的原生路径）
/// 完全一致。手拼 "dir/name" 在 Windows 上会得到混合分隔符的路径，
/// 与索引键不相等 ⇒ set_progress 找不到条目、前端进度也对不上。
pub fn full_path(dir: &str, name: &str) -> String {
    Path::new(dir).join(name).to_string_lossy().to_string()
}

fn now_secs() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// 索引里单个文件的记录。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct FileEntry {
    pub dir: String,
    pub name: String,
    pub size: u64,
    /// 修改时间（纳秒），用于判断是否需要重新探测
    pub mtime: u64,
    pub dur: f64,
    pub thumb: String,
    pub local: bool,
    pub funscript: String,
    pub pos: f64,
    pub played_at: f64,
}

/// 播放进度（前端 reportProgress 上报）。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Progress {
    pub path: String,
    pub pos: f64,
    pub dur: f64,
}

/// 媒体库卡片（按目录聚合的一组同名视频，如 CD1/CD2）。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Card {
    pub name: String,
    pub dir: String,
    pub cover: String,
    pub parts: Vec<FileEntry>,
    pub paths: Vec<String>,
    pub last_played: f64,
    pub progress: Option<Progress>,
    pub total_sec: f64,
}

#[derive(Debug, Default, Serialize, Deserialize)]
struct Index {
    #[serde(default)]
    files: HashMap<String, FileEntry>,
    #[serde(default)]
    last_scan: f64,
}

pub struct Library {
    index_file: PathBuf,
    thumb_dir: PathBuf,
    inner: Mutex<Inner>,
}

struct Inner {
    index: Index,
    cards: Vec<Card>,
    scanning: bool,
    progress_line: String,
    last_scan: f64,
}

impl Library {
    pub fn new(index_file: PathBuf, thumb_dir: PathBuf) -> Self {
        let _ = std::fs::create_dir_all(&thumb_dir);
        let index = Self::load_index(&index_file);
        let cards = Self::aggregate(&index.files);
        let last_scan = index.last_scan;
        Self {
            index_file,
            thumb_dir,
            inner: Mutex::new(Inner {
                index,
                cards,
                scanning: false,
                progress_line: String::new(),
                last_scan,
            }),
        }
    }

    fn load_index(path: &Path) -> Index {
        match std::fs::read_to_string(path) {
            Ok(t) => match serde_json::from_str::<Index>(&t) {
                Ok(idx) => idx,
                Err(e) => {
                    // 解析失败：先把坏文件改名留存（<原名>.corrupt）再退回默认，
                    // 避免静默清零把用户全部进度丢掉
                    let mut corrupt = path.as_os_str().to_os_string();
                    corrupt.push(".corrupt");
                    let corrupt = PathBuf::from(corrupt);
                    if let Err(re) = std::fs::rename(path, &corrupt) {
                        tracing::warn!("[library] 索引损坏且备份失败（{}）：{re}", corrupt.display());
                    }
                    tracing::warn!(
                        "[library] 索引解析失败（{e}），已备份为 {} 并使用空索引",
                        corrupt.display()
                    );
                    Index::default()
                }
            },
            // 文件不存在等：直接默认
            Err(_) => Index::default(),
        }
    }

    fn save_index(&self, idx: &Index) {
        if let Some(dir) = self.index_file.parent() {
            if let Err(e) = std::fs::create_dir_all(dir) {
                tracing::warn!("[library] 创建索引目录失败：{e}");
            }
        }
        // 临时文件名带进程内序号：并发 save_index 各写各的 tmp 再原子 rename，
        // 共用同一个 json.tmp 会交错写出损坏索引（LAN 上进度上报是并发的）
        static TMP_SEQ: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
        let seq = TMP_SEQ.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let mut tmp_name = self.index_file.as_os_str().to_os_string();
        tmp_name.push(format!(".tmp{seq}"));
        let tmp = PathBuf::from(tmp_name);
        let text = match serde_json::to_string(idx) {
            Ok(t) => t,
            Err(e) => {
                tracing::warn!("[library] 索引序列化失败：{e}");
                return;
            }
        };
        if let Err(e) = std::fs::write(&tmp, &text) {
            tracing::warn!("[library] 写索引临时文件 {} 失败：{e}", tmp.display());
            return;
        }
        // rename 失败时保留 .tmp 便于排查，不删
        if let Err(e) = std::fs::rename(&tmp, &self.index_file) {
            tracing::warn!(
                "[library] 索引替换失败（临时文件已保留 {}）：{e}",
                tmp.display()
            );
        }
    }

    pub fn cards(&self) -> Vec<Card> {
        self.inner.lock().unwrap().cards.clone()
    }

    pub fn count(&self) -> usize {
        self.inner.lock().unwrap().index.files.len()
    }

    pub fn state(&self) -> (bool, String, f64, usize) {
        let g = self.inner.lock().unwrap();
        (g.scanning, g.progress_line.clone(), g.last_scan, g.index.files.len())
    }

    pub fn thumb_for(&self, path: &str) -> String {
        self.inner
            .lock()
            .unwrap()
            .index
            .files
            .get(path)
            .map(|e| e.thumb.clone())
            .unwrap_or_default()
    }

    pub fn thumb_path(&self, name: &str) -> Option<PathBuf> {
        // `:`：盘符注入（C:evil.jpg）与 NTFS 备用数据流（file.jpg:stream）一并拒绝
        if name.is_empty()
            || name.contains("..")
            || name.contains('/')
            || name.contains('\\')
            || name.contains(':')
        {
            return None;
        }
        let p = self.thumb_dir.join(name);
        // join 后组件级校验仍在缩略图目录之下（与 DLNA key_to_path 同口径的双保险）
        if !p.starts_with(&self.thumb_dir) {
            return None;
        }
        p.is_file().then_some(p)
    }

    /// 播放进度落索引，并同步到内存卡片。
    pub fn set_progress(&self, path: &str, pos: f64, dur: f64) {
        let played_at = now_secs();
        let mut g = self.inner.lock().unwrap();
        let r_pos = (pos * 10.0).round() / 10.0;
        let r_dur = (dur * 10.0).round() / 10.0;
        // 只接受索引里已存在的文件：拒绝伪造路径产生幽灵条目
        let Some(e) = g.index.files.get_mut(path) else {
            tracing::warn!("[library] 进度上报忽略未知路径：{path}");
            return;
        };
        e.pos = r_pos;
        e.dur = r_dur;
        e.played_at = played_at;
        let snapshot = Index {
            files: g.index.files.clone(),
            last_scan: g.index.last_scan,
        };
        // 同步内存卡片
        for c in &mut g.cards {
            if c.paths.iter().any(|p| p == path) {
                c.last_played = played_at;
                c.progress = Some(Progress {
                    path: path.to_string(),
                    pos: r_pos,
                    dur: r_dur,
                });
                for p in &mut c.parts {
                    if full_path(&p.dir, &p.name) == path {
                        p.pos = r_pos;
                        p.dur = r_dur;
                        p.played_at = played_at;
                    }
                }
            }
        }
        drop(g);
        self.save_index(&snapshot);
    }

    /// 全量扫描（增量利用索引）。阻塞调用，宿主放后台任务。
    pub async fn rescan(&self, roots: Vec<String>) -> Result<ScanReport, String> {
        {
            let mut g = self.inner.lock().unwrap();
            if g.scanning {
                return Err("扫描已在进行".to_string());
            }
            g.scanning = true;
        }
        // 从这里起到函数结束，无论哪条退出路径都由 guard 复位 scanning
        let _scan_guard = ScanFlagReset(&self.inner);
        let started = now_secs();
        let old = {
            let g = self.inner.lock().unwrap();
            g.index.files.clone()
        };

        let mut found: HashMap<String, FileEntry> = HashMap::new();
        let mut dirs = 0usize;
        // 本轮不可达的根（网盘掉线/未挂载）：没扫到不等于内容没了，
        // 合并时保留这些根的旧条目与进度，否则一次扫描就静默清空整根媒体库
        let mut unreachable_roots: Vec<String> = Vec::new();

        for root in &roots {
            let rp = Path::new(root);
            if !rp.is_dir() {
                unreachable_roots.push(root.clone());
                continue;
            }
            // 本地盘分级（Python GetDriveTypeW 同款）：网盘/可移动盘只列文件名，
            // 不跑 ffprobe/ffmpeg——探测失败的网盘文件每轮重扫都会重跑，扫描假死级缓慢
            let local = drive_is_local(rp);
            // 目录遍历 + 逐文件 metadata + funscript 探测是同步阻塞 IO
            // （UNC/网盘可达秒级）：放 blocking 线程，不占 tokio worker
            // （与 routes.rs 的 spawn_blocking 约定一致）
            let root_owned = rp.to_path_buf();
            let discovered = tokio::task::spawn_blocking(move || discover_videos(&root_owned))
                .await
                .map_err(|e| format!("扫描任务失败：{e}"))?;
            for (dirpath, files_meta) in discovered {
                dirs += 1;
                {
                    let mut g = self.inner.lock().unwrap();
                    g.progress_line = format!("扫描 {}", dirpath.display());
                }
                if files_meta.is_empty() {
                    continue;
                }
                for (vp, size, mtime, fs) in files_meta {
                    let key = vp.to_string_lossy().to_string();
                    let prev = old.get(&key).cloned().unwrap_or_default();
                    let need_probe = prev.mtime != mtime
                        || prev.size != size
                        || prev.dur <= 0.0
                        || prev.thumb.is_empty();
                    let mut e = FileEntry {
                        dir: dirpath.to_string_lossy().to_string(),
                        name: vp
                            .file_name()
                            .map(|n| n.to_string_lossy().to_string())
                            .unwrap_or_default(),
                        size,
                        mtime,
                        dur: prev.dur,
                        thumb: prev.thumb.clone(),
                        local,
                        funscript: fs
                            .map(|p| p.to_string_lossy().to_string())
                            .unwrap_or_default(),
                        pos: prev.pos,
                        played_at: prev.played_at,
                    };
                    if need_probe && local {
                        let (dur, thumb) = probe(&vp, &self.thumb_dir).await;
                        if dur > 0.0 {
                            e.dur = dur;
                        }
                        if !thumb.is_empty() {
                            e.thumb = thumb;
                        }
                    }
                    found.insert(key, e);
                }
            }
        }

        let mut g = self.inner.lock().unwrap();
        // 以**当前活索引**（g.index.files）为准保留进度：扫描期间 set_progress 写入的
        // played_at 在活 map 上，old 快照（扫描开始时）看不到它，用它对比会丢进度。
        // 活 map 中该文件 played_at 更新（更晚播放）就保留活的。
        for (k, e) in found.iter_mut() {
            if let Some(prev) = g.index.files.get(k) {
                if prev.played_at > e.played_at {
                    e.pos = prev.pos;
                    e.played_at = prev.played_at;
                }
            }
        }
        // 不可达根的旧条目整根保留（含进度）：用户主动移除的根不在 roots 里，
        // 不受此保护，仍会被正常清掉
        for (k, prev) in g.index.files.iter() {
            if found.contains_key(k) {
                continue;
            }
            // 组件级前缀（Path::starts_with），字符串 starts_with 会把 D:\media2
            // 误判进 D:\media
            if unreachable_roots.iter().any(|r| std::path::Path::new(k).starts_with(r)) {
                found.insert(k.clone(), prev.clone());
            }
        }
        let files_count = found.len();
        g.index.files = found;
        g.index.last_scan = now_secs();
        g.cards = Self::aggregate(&g.index.files);
        let cards_count = g.cards.len();
        g.last_scan = g.index.last_scan;
        g.scanning = false;
        g.progress_line.clear();
        let snapshot = Index {
            files: g.index.files.clone(),
            last_scan: g.index.last_scan,
        };
        drop(g);
        self.save_index(&snapshot);

        Ok(ScanReport {
            files: files_count,
            cards: cards_count,
            dirs,
            sec: ((now_secs() - started) * 10.0).round() / 10.0,
        })
    }

    /// 聚合：一文件一卡（对齐 Python 版 R119 裁定）——同目录的独立视频
    /// 各自成卡，不再整目录合成一张名字只属于第一个文件卡。
    fn aggregate(files: &HashMap<String, FileEntry>) -> Vec<Card> {
        let mut cards: Vec<Card> = files
            .values()
            .map(|p| {
                let path = full_path(&p.dir, &p.name);
                let name = p
                    .name
                    .rsplit_once('.')
                    .map(|(s, _)| s.to_string())
                    .unwrap_or_else(|| p.name.clone());
                Card {
                    name,
                    dir: p.dir.clone(),
                    cover: p.thumb.clone(),
                    parts: vec![p.clone()],
                    paths: vec![path.clone()],
                    last_played: p.played_at,
                    progress: if p.played_at > 0.0 {
                        Some(Progress {
                            path,
                            pos: p.pos,
                            dur: p.dur,
                        })
                    } else {
                        None
                    },
                    total_sec: p.dur,
                }
            })
            .collect();
        cards.sort_by(|a, b| b.last_played.partial_cmp(&a.last_played).unwrap_or(std::cmp::Ordering::Equal));
        cards
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct ScanReport {
    pub files: usize,
    pub cards: usize,
    pub dirs: usize,
    pub sec: f64,
}

/// RAII：rescan 无论从哪条路径退出（正常返回 / 将来新增的提前 return /
/// 扫描中 panic 的 unwind），都保证 `scanning` 复位、进度行清空，
/// 扫描状态永不永久卡在"进行中"。
struct ScanFlagReset<'a>(&'a Mutex<Inner>);

impl Drop for ScanFlagReset<'_> {
    fn drop(&mut self) {
        // 若扫描线程 panic 导致锁中毒，也要拿到锁复位标志，故用 into_inner 恢复
        let mut g = match self.0.lock() {
            Ok(g) => g,
            Err(poisoned) => poisoned.into_inner(),
        };
        g.scanning = false;
        g.progress_line.clear();
    }
}

/// 本地盘判定（Windows `GetDriveTypeW == DRIVE_FIXED`）：只有固定盘跑
/// ffprobe/ffmpeg 探测；网盘/可移动盘只列文件名。UNC 路径一律按网盘。
#[allow(clippy::needless_return)]
fn drive_is_local(path: &Path) -> bool {
    #[cfg(windows)]
    {
        use windows::core::PCWSTR;
        use windows::Win32::Storage::FileSystem::GetDriveTypeW;
        // 取盘符前缀（"C:"）拼成根 "C:\"；UNC（\\server\share）不是固定盘
        let prefix = path.components().find_map(|c| match c {
            std::path::Component::Prefix(p) => Some(p.as_os_str().to_string_lossy().to_string()),
            _ => None,
        });
        let Some(prefix) = prefix else {
            return true; // 相对路径按本地处理
        };
        if prefix.starts_with("\\\\") {
            return false;
        }
        let root: Vec<u16> = format!("{prefix}\\")
            .encode_utf16()
            .chain(std::iter::once(0))
            .collect();
        // SAFETY：root 是以 NUL 结尾的有效宽字符串。
        // 返回值 3 = DRIVE_FIXED（windows 0.58 未导出该常量，SDK 值）
        const DRIVE_FIXED: u32 = 3;
        unsafe { GetDriveTypeW(PCWSTR(root.as_ptr())) == DRIVE_FIXED }
    }
    #[cfg(not(windows))]
    {
        let _ = path;
        true
    }
}

/// 遍历目录（不进入隐藏目录与本机回收站）。
fn walk(root: &Path) -> Vec<(PathBuf, Vec<String>)> {    let mut out = Vec::new();
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        let Ok(rd) = std::fs::read_dir(&dir) else {
            continue;
        };
        let mut files = Vec::new();
        for e in rd.flatten() {
            let name = e.file_name().to_string_lossy().to_string();
            let Ok(ft) = e.file_type() else { continue };
            if ft.is_dir() {
                // junction/符号链接目录跳过：目录环会扫描爆炸并重复索引
                // （与 DLNA 侧 reparse_free_below 同款防护）
                if ft.is_symlink() {
                    continue;
                }
                #[cfg(windows)]
                {
                    use std::os::windows::fs::MetadataExt;
                    const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x0400;
                    if let Ok(md) = e.metadata() {
                        if md.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
                            continue;
                        }
                    }
                }
                if name.starts_with('.') || name.starts_with('$') {
                    continue;
                }
                stack.push(e.path());
            } else {
                files.push(name);
            }
        }
        if !files.is_empty() {
            out.push((dir, files));
        }
    }
    out
}

/// 单根目录发现：walk + 逐视频 metadata + funscript 探测，纯同步阻塞 IO。
/// 供 rescan 在 spawn_blocking 里调用；返回每目录的视频元数据草稿，
/// 异步 probe（缩略图）留在 async 侧做。
fn discover_videos(root: &Path) -> Vec<(PathBuf, Vec<(PathBuf, u64, u64, Option<PathBuf>)>)> {
    walk(root)
        .into_iter()
        .map(|(dirpath, filenames)| {
            let metas = filenames
                .iter()
                .filter(|f| is_video(f))
                .filter_map(|f| {
                    let vp = dirpath.join(f);
                    let md = std::fs::metadata(&vp).ok()?;
                    let mtime = md
                        .modified()
                        .ok()
                        .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
                        .map(|d| d.as_nanos() as u64)
                        .unwrap_or(0);
                    let stem = f.rsplit_once('.').map(|(a, _)| a).unwrap_or(f.as_str());
                    let fs_candidate = dirpath.join(format!("{stem}.funscript"));
                    let fs = fs_candidate.is_file().then_some(fs_candidate);
                    Some((vp, md.len(), mtime, fs))
                })
                .collect();
            (dirpath, metas)
        })
        .collect()
}

/// 探测时长 + 抽缩略图。返回 (秒, 缩略图文件名) —— 失败时给 0 / 空。
pub async fn probe(video: &Path, thumb_dir: &Path) -> (f64, String) {
    let dur = ffprobe_duration(video).await.unwrap_or(0.0);
    let mut thumb = String::new();
    if dur > 0.0 {
        // 12% 处先抽；失败再试 45%（避开片头黑场）
        for pct in [0.12_f64, 0.45] {
            if let Some(name) = ffmpeg_thumb(video, thumb_dir, dur * pct, pct).await {
                thumb = name;
                break;
            }
        }
    }
    (dur, thumb)
}

async fn ffprobe_duration(video: &Path) -> Option<f64> {
    // timeout 到点会取消下面的 Future：Child 随之 drop，kill_on_drop(true) 负责杀掉子进程，
    // 超时按该文件探测失败处理（返回 None），扫描继续不卡死。
    let mut cmd = tokio::process::Command::new("ffprobe");
    cmd.args([
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "csv=p=0",
    ])
    .arg(video)
    .kill_on_drop(true);
    hide_console(&mut cmd);
    let out = tokio::time::timeout(PROBE_TIMEOUT, cmd.output())
        .await
        .ok()?
        .ok()?;
    if !out.status.success() {
        return None;
    }
    String::from_utf8_lossy(&out.stdout)
        .trim()
        .parse::<f64>()
        .ok()
        .filter(|d| *d > 0.0)
}

async fn ffmpeg_thumb(video: &Path, thumb_dir: &Path, at_sec: f64, pct: f64) -> Option<String> {
    // 缩略图名带"完整视频路径哈希前缀"：不同目录的同名视频不再互相覆盖
    let name = thumb_file_name(video, pct);
    let out_path = thumb_dir.join(&name);
    // timeout 到点取消 Future：Child 随之 drop，kill_on_drop(true) 杀掉 ffmpeg
    let mut cmd = tokio::process::Command::new("ffmpeg");
    cmd.args(["-v", "error", "-y", "-ss", &format!("{at_sec:.3}")])
        .arg("-i")
        .arg(video)
        .args(["-frames:v", "1", "-vf", "scale=320:-2"])
        .arg(&out_path)
        .kill_on_drop(true);
    hide_console(&mut cmd);
    let status = match tokio::time::timeout(THUMB_TIMEOUT, cmd.status()).await {
        Ok(Ok(s)) => s,
        // 超时/失败都要删掉 ffmpeg 被杀前写出的半截文件，
        // 且 12% 档失败换 45% 档时前档残file不会自清（pct 在文件名里）
        _ => {
            let _ = tokio::fs::remove_file(&out_path).await;
            return None;
        }
    };
    if status.success() && out_path.is_file() {
        Some(name)
    } else {
        let _ = tokio::fs::remove_file(&out_path).await;
        None
    }
}

/// 缩略图文件名：对完整视频路径做哈希前缀 + 抽帧百分比。
/// 用 `DefaultHasher::new()`（固定种子，跨进程稳定，不引新依赖）；
/// 旧命名的缩略图会因名字变化重新生成一次，可接受。
fn thumb_file_name(video: &Path, pct: f64) -> String {
    use std::collections::hash_map::DefaultHasher;
    use std::hash::{Hash, Hasher};
    let mut h = DefaultHasher::new();
    video.to_string_lossy().hash(&mut h);
    format!("{:016x}_{}.jpg", h.finish(), pct)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("nexus-lib-{}-{}", name, std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn video_extension_detection() {
        assert!(is_video("a.mp4"));
        assert!(is_video("A.MKV"), "大小写不敏感");
        assert!(!is_video("a.srt"));
        assert!(!is_video("noext"));
    }

    #[tokio::test]
    async fn scan_indexes_videos_and_reports() {
        let root = tmp("scan");
        std::fs::create_dir_all(root.join("sub")).unwrap();
        std::fs::write(root.join("a.mp4"), b"x").unwrap();
        std::fs::write(root.join("a.funscript"), b"{}").unwrap();
        std::fs::write(root.join("sub/b.mkv"), b"x").unwrap();
        std::fs::write(root.join("note.txt"), b"x").unwrap();

        let idx = root.join("idx.json");
        let lib = Library::new(idx.clone(), root.join("thumbs"));
        let rep = lib.rescan(vec![root.to_string_lossy().to_string()]).await.unwrap();
        assert_eq!(rep.files, 2, "只索引视频");
        assert!(rep.dirs >= 2);

        let files = &lib.inner.lock().unwrap().index.files;
        let a = files
            .values()
            .find(|e| e.name == "a.mp4")
            .expect("a.mp4 应在索引里");
        assert!(!a.funscript.is_empty(), "同名 funscript 要关联上");
        // 索引落盘
        assert!(idx.is_file());
        let _ = std::fs::remove_dir_all(&root);
    }

    #[tokio::test]
    async fn progress_survives_rescan() {
        let root = tmp("prog");
        std::fs::write(root.join("v.mp4"), b"x").unwrap();
        let lib = Library::new(root.join("idx.json"), root.join("thumbs"));
        lib.rescan(vec![root.to_string_lossy().to_string()]).await.unwrap();
        let key = root.join("v.mp4").to_string_lossy().to_string();
        lib.set_progress(&key, 123.456, 600.0);

        let saved = lib.inner.lock().unwrap().index.files.get(&key).cloned().unwrap();
        assert!((saved.pos - 123.5).abs() < 0.01, "进度保留一位小数");

        // 再扫一次：进度不能被清掉
        lib.rescan(vec![root.to_string_lossy().to_string()]).await.unwrap();
        let after = lib.inner.lock().unwrap().index.files.get(&key).cloned().unwrap();
        assert!((after.pos - 123.5).abs() < 0.01, "重扫不得覆盖进度");
        let _ = std::fs::remove_dir_all(&root);
    }

    #[tokio::test]
    async fn rescan_is_incremental_for_unchanged_files() {
        let root = tmp("incr");
        std::fs::write(root.join("v.mp4"), b"x").unwrap();
        let lib = Library::new(root.join("idx.json"), root.join("thumbs"));
        lib.rescan(vec![root.to_string_lossy().to_string()]).await.unwrap();
        let key = root.join("v.mp4").to_string_lossy().to_string();
        let mtime_before = lib.inner.lock().unwrap().index.files[&key].mtime;
        lib.rescan(vec![root.to_string_lossy().to_string()]).await.unwrap();
        let mtime_after = lib.inner.lock().unwrap().index.files[&key].mtime;
        assert_eq!(mtime_before, mtime_after, "文件没变，mtime 应一致（不重复探测的前提）");
        let _ = std::fs::remove_dir_all(&root);
    }

    #[tokio::test]
    async fn card_paths_match_index_keys() {
        // 回归：卡片路径必须与索引键逐字节相同，否则进度上报写不进正确条目
        let root = tmp("paths");
        std::fs::write(root.join("v.mp4"), b"x").unwrap();
        let lib = Library::new(root.join("idx.json"), root.join("thumbs"));
        lib.rescan(vec![root.to_string_lossy().to_string()]).await.unwrap();

        let cards = lib.cards();
        let card_path = cards[0].paths[0].clone();
        {
            let g = lib.inner.lock().unwrap();
            assert!(
                g.index.files.contains_key(&card_path),
                "卡片路径不在索引里（分隔符不一致？）：{card_path}"
            );
        }
        lib.set_progress(&card_path, 42.0, 100.0);
        let e = lib.inner.lock().unwrap().index.files.get(&card_path).cloned().unwrap();
        assert!((e.pos - 42.0).abs() < 0.01, "进度没写进索引条目");
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn thumb_path_rejects_traversal() {
        let root = tmp("thumb");
        let lib = Library::new(root.join("idx.json"), root.join("thumbs"));
        assert!(lib.thumb_path("../secret").is_none());
        assert!(lib.thumb_path("a/b.jpg").is_none());
        assert!(lib.thumb_path("").is_none());
        // 盘符与 NTFS 备用数据流必须拒绝
        assert!(lib.thumb_path("C:evil.jpg").is_none());
        assert!(lib.thumb_path("x.jpg:stream").is_none());
        let _ = std::fs::remove_dir_all(&root);
    }
}
