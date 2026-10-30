//! 设备文件同步：脚本（.funscript）/ 视频 → Quest 的 adb 增量推送与清理。
//!
//! 行为逐条对齐 Python 版：
//! - 本地扫描：`vendor/dlna/funscript_sync.py::scan_local` /
//!   `video_sync.py::scan_local`（递归、扩展名大小写不敏感、记录扫描是否残缺）；
//! - 设备扫描：`scan_device`（find + stat/ls 解析文件名+大小，路径含空格/中文安全）；
//! - 增量：设备已存在且**大小一致**则跳过；`sync_force_full` 时全部重推；
//! - 推送：`adb push`，目标目录以 `/` 结尾（`video_sync.py` 的直接推送路径）；
//! - 删除：`sync_delete_extra` 只删设备上本地不存在的文件，空本地/扫描残缺时拒绝删除
//!   （`funscript_sync.py:346-393` 的两道防线，文案改用「警告」以便前端高亮）；
//! - 设备目录护栏：`vendor/dlna/device_guard.py` 的 Rust 版（禁止存储根/系统树）。
//!
//! 所有 adb 命令都在调用方（`extras::sync_run`）的 `spawn_blocking` 线程里执行，
//! 不占 tokio worker；前端 15 秒请求超时只影响发起请求，任务会继续跑完。

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::Duration;

use serde::Serialize;

// ---------------------------------------------------------------------------
// 类型与扩展名
// ---------------------------------------------------------------------------

/// 视频扩展名（与 Python `video_sync.VIDEO_EXTS` 完全一致，含点由拼接补上）。
pub const VIDEO_EXTS: &[&str] = &[
    "mp4", "mkv", "mov", "avi", "webm", "ts", "m2ts", "mts", "wmv", "flv", "mpg", "mpeg",
    "3gp", "ogv", "vob",
];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SyncKind {
    Script,
    Video,
}

impl SyncKind {
    /// 请求里的 `kind`：缺省按脚本处理（保持既有 `/api/sync/run` 行为）。
    pub fn from_key(key: &str) -> Option<Self> {
        match key {
            "" | "script" => Some(Self::Script),
            "video" => Some(Self::Video),
            _ => None,
        }
    }

    pub fn key(self) -> &'static str {
        match self {
            Self::Script => "script",
            Self::Video => "video",
        }
    }

    /// UI 槽位标签（Python `SYNC_KINDS[kind]["label"]`）。
    pub fn label(self) -> &'static str {
        match self {
            Self::Script => "脚本（.funscript）",
            Self::Video => "视频",
        }
    }

    pub fn short_label(self) -> &'static str {
        match self {
            Self::Script => "脚本",
            Self::Video => "视频",
        }
    }

    pub fn local_key(self) -> &'static str {
        match self {
            Self::Script => "script_folder",
            Self::Video => "video_folder",
        }
    }

    pub fn device_key(self) -> &'static str {
        match self {
            Self::Script => "device_folder_script",
            Self::Video => "device_folder_video",
        }
    }

    /// 设备目录为空时的回退（Python Config 的默认值）。
    pub fn default_device_folder(self) -> &'static str {
        match self {
            Self::Script => "/sdcard/Funscript",
            Self::Video => "/sdcard/Movies",
        }
    }

    fn index(self) -> usize {
        match self {
            Self::Script => 0,
            Self::Video => 1,
        }
    }

    /// 本地文件名是否是本类型要同步的文件。
    pub fn matches(self, name: &str) -> bool {
        match self {
            Self::Script => is_script_name(name),
            Self::Video => is_video_name(name),
        }
    }
}

fn extension_lower(name: &str) -> Option<String> {
    Path::new(name)
        .extension()
        .and_then(|x| x.to_str())
        .map(|x| x.to_ascii_lowercase())
}

/// `.funscript`（大小写不敏感，与 Python `suffix.lower() == ".funscript"` 一致）。
pub fn is_script_name(name: &str) -> bool {
    extension_lower(name).as_deref() == Some("funscript")
}

/// 常见视频扩展名（大小写不敏感）。
pub fn is_video_name(name: &str) -> bool {
    extension_lower(name)
        .map(|e| VIDEO_EXTS.contains(&e.as_str()))
        .unwrap_or(false)
}

/// Android mksh 兼容的单引号转义（`device_guard`/`funscript_sync._shq` 同款）。
pub fn shq(text: &str) -> String {
    format!("'{}'", text.replace('\'', "'\\''"))
}

// ---------------------------------------------------------------------------
// 设备目录护栏（device_guard.py 的 Rust 版）
// ---------------------------------------------------------------------------

const UNSAFE_EXACT: &[&str] = &[
    "/",
    "/sdcard",
    "/mnt",
    "/mnt/sdcard",
    "/storage",
    "/storage/emulated",
    "/storage/emulated/0",
    "/storage/emulated/legacy",
    "/storage/self",
    "/storage/self/primary",
    "/system",
    "/data",
    "/vendor",
    "/proc",
    "/sys",
    "/dev",
];

const UNSAFE_PREFIX: &[&str] = &[
    "/sdcard/android",
    "/storage/emulated/0/android",
    "/storage/emulated/legacy/android",
    "/storage/emulated/android",
    "/storage/self/primary/android",
    "/system",
    "/data",
    "/vendor",
    "/proc",
    "/sys",
    "/dev",
];

/// 设备端 POSIX 路径归一化：反斜杠转 `/`、补前导 `/`、折叠 `.`/`..`/重复斜杠。
pub fn normalize_posix(raw: &str) -> String {
    let src = raw.replace('\\', "/");
    let mut parts: Vec<&str> = Vec::new();
    for seg in src.split('/') {
        match seg {
            "" | "." => {}
            ".." => {
                parts.pop();
            }
            s => parts.push(s),
        }
    }
    format!("/{}", parts.join("/"))
}

/// 归一化并做安全护栏；不安全时返回中文原因（不抛异常，由调用方转成响应）。
///
/// 规则与 `vendor/dlna/device_guard.py` 一致：禁止根/存储根、禁止它们的祖先、
/// 禁止应用私有/系统子树、禁止挂在根下的单段目录。
pub fn normalize_device_folder(raw: &str, kind: SyncKind) -> Result<String, String> {
    let src = raw.trim();
    let src = if src.is_empty() {
        kind.default_device_folder()
    } else {
        src
    };
    let dev_path = normalize_posix(src);
    let low = dev_path.to_lowercase();

    if UNSAFE_EXACT.iter().any(|u| *u == low) {
        return Err(format!(
            "设备目录过宽，禁止使用根目录/存储根目录进行同步，以免误删文件（{dev_path}）"
        ));
    }
    if UNSAFE_EXACT
        .iter()
        .filter(|u| **u != "/")
        .any(|u| u.starts_with(&(low.clone() + "/")))
    {
        return Err(format!(
            "设备目录是存储根目录的上级，同步删除会越界，请选一个专用子目录（{dev_path}）"
        ));
    }
    if UNSAFE_PREFIX.iter().any(|p| {
        low == *p || low.starts_with(&format!("{p}/"))
    }) {
        return Err(format!(
            "该目录属于应用私有/系统目录，禁止作为同步目标，以免误删其他应用的数据（{dev_path}）"
        ));
    }
    if low.split('/').filter(|s| !s.is_empty()).count() < 2 {
        return Err(format!(
            "设备目录必须是一个专用子目录（如 /sdcard/Funscript），当前：{dev_path}"
        ));
    }
    Ok(dev_path)
}

// ---------------------------------------------------------------------------
// 本地扫描
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LocalFile {
    /// 相对本地根目录的 POSIX 路径（`/` 分隔，供设备端拼接）
    pub rel: String,
    pub size: u64,
}

#[derive(Debug, Clone, Default)]
pub struct LocalScan {
    pub files: Vec<LocalFile>,
    /// 是否有无法读取的子树：为真时**禁止删除**设备文件（防误删）
    pub incomplete: bool,
}

/// 扫描本地目录（递归）。根目录不可读直接报错；子目录不可读记 `incomplete`。
pub fn scan_local(root: &Path, kind: SyncKind) -> Result<LocalScan, String> {
    if !root.is_dir() {
        return Err(format!(
            "本地{}目录不存在：{}",
            kind.short_label(),
            root.display()
        ));
    }
    let mut out = LocalScan::default();
    let mut stack: Vec<PathBuf> = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        let entries = match std::fs::read_dir(&dir) {
            Ok(e) => e,
            Err(e) => {
                if dir == root {
                    return Err(format!("读取本地目录失败：{}（{e}）", dir.display()));
                }
                out.incomplete = true;
                continue;
            }
        };
        for entry in entries {
            let entry = match entry {
                Ok(e) => e,
                Err(_) => {
                    out.incomplete = true;
                    continue;
                }
            };
            let path = entry.path();
            // 目录链接不跟随（与 Python os.walk 默认一致），避免环
            let is_dir = entry.file_type().map(|t| t.is_dir()).unwrap_or(false);
            if is_dir {
                stack.push(path);
                continue;
            }
            if !path.is_file() {
                continue;
            }
            let name = entry.file_name().to_string_lossy().to_string();
            if !kind.matches(&name) {
                continue;
            }
            let rel = match path.strip_prefix(root) {
                Ok(r) => r.to_string_lossy().replace('\\', "/"),
                Err(_) => continue,
            };
            if rel.is_empty() {
                continue;
            }
            let size = std::fs::metadata(&path).map(|m| m.len()).unwrap_or(0);
            out.files.push(LocalFile { rel, size });
        }
    }
    out.files.sort_by(|a, b| a.rel.cmp(&b.rel));
    // 去重（大小写不敏感，与设备端判定同一口径）
    let mut seen: HashSet<String> = HashSet::new();
    out.files.retain(|f| seen.insert(f.rel.to_lowercase()));
    Ok(out)
}

// ---------------------------------------------------------------------------
// 设备扫描解析
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeviceFile {
    pub rel: String,
    pub size: u64,
}

/// 解析 find 的输出。支持两种行格式：
/// - `stat -c '%s|%n'`：`12345|/sdcard/Funscript/a.funscript`
/// - `ls -l`：`-rw-rw---- 1 owner group 12345 2024-01-02 03:04 /sdcard/...`
///
/// 路径用「行内查找设备目录前缀」定位，不依赖 ls 的字段数/空格对齐：
/// 路径含空格、中文、文件名含 `|` 都不会被拆坏。只接受**确实位于目标目录下**的
/// 绝对路径，解析不出来的一律丢掉（绝不据此删除）。
pub fn parse_device_listing(text: &str, device_folder: &str) -> Vec<DeviceFile> {
    let folder = normalize_posix(device_folder);
    let prefix = format!("{}/", folder.trim_end_matches('/'));
    let prefix_lower = prefix.to_ascii_lowercase();
    let mut out: Vec<DeviceFile> = Vec::new();
    let mut seen: HashSet<String> = HashSet::new();
    for raw_line in text.lines() {
        let line = raw_line.trim_end_matches('\r');
        if line.trim().is_empty() {
            continue;
        }
        let Some((size, abs_path)) = parse_listing_line(line, &prefix_lower) else {
            continue;
        };
        let Some(rel) = relative_under(&abs_path, &prefix) else {
            continue;
        };
        if !is_safe_rel(&rel) {
            continue;
        }
        let lower = rel.to_lowercase();
        if seen.insert(lower) {
            out.push(DeviceFile { rel, size });
        }
    }
    out.sort_by(|a, b| a.rel.cmp(&b.rel));
    out
}

/// 单行 →（大小, 绝对路径）。`prefix_lower` 是设备目录 + `/` 的 ASCII 小写。
fn parse_listing_line(line: &str, prefix_lower: &str) -> Option<(u64, String)> {
    // 大小：优先 stat 的 `数字|路径` 格式；否则按 ls -l 的第 5 个字段
    let size = if let Some((size_s, _)) = line.split_once('|') {
        let t = size_s.trim();
        if !t.is_empty() && t.chars().all(|c| c.is_ascii_digit()) {
            t.parse::<u64>().ok()
        } else {
            None
        }
    } else {
        None
    }
    .or_else(|| parse_ls_size(line))?;

    // 路径：从行内设备目录前缀出现的位置到行尾（保留空格/中文/竖线）
    let idx = line.to_ascii_lowercase().find(prefix_lower)?;
    let path = line[idx..].trim_end().to_string();
    if path.is_empty() {
        return None;
    }
    Some((size, path))
}

/// ls -l 的第 5 个非空字段是文件大小（前 4 个：权限/链接数/owner/group）。
fn parse_ls_size(line: &str) -> Option<u64> {
    let bytes = line.as_bytes();
    if bytes.first().copied()? != b'-' {
        // 只认普通文件；目录/符号链接等一律不参与同步
        return None;
    }
    let mut idx = 0usize;
    let mut starts: Vec<usize> = Vec::new();
    while starts.len() < 8 {
        while idx < bytes.len() && bytes[idx] == b' ' {
            idx += 1;
        }
        if idx >= bytes.len() {
            return None;
        }
        starts.push(idx);
        while idx < bytes.len() && bytes[idx] != b' ' {
            idx += 1;
        }
    }
    line[starts[4]..].split_whitespace().next()?.parse::<u64>().ok()
}

/// 路径必须在 `prefix`（以 `/` 结尾）之下，返回相对路径。
fn relative_under(path: &str, prefix: &str) -> Option<String> {
    if path.len() <= prefix.len() {
        return None;
    }
    let head = path.get(..prefix.len())?;
    if !head.eq_ignore_ascii_case(prefix) {
        return None;
    }
    let rel = &path[prefix.len()..];
    if rel.is_empty() || rel.starts_with('/') {
        return None;
    }
    Some(rel.to_string())
}

/// 相对路径不得含空段、`.`、`..`（防 `rm` 拼出目录外路径）。
fn is_safe_rel(rel: &str) -> bool {
    !rel.is_empty()
        && !rel.starts_with('/')
        && rel
            .split('/')
            .all(|s| !s.is_empty() && s != "." && s != "..")
}

// ---------------------------------------------------------------------------
// 增量 / 删除计划（纯函数，单测重点）
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Default)]
pub struct PushPlan {
    pub to_push: Vec<LocalFile>,
    /// 设备已存在且大小一致
    pub skipped: usize,
    pub new: usize,
    /// 设备存在但大小不一致
    pub changed: usize,
}

/// 增量 diff：已存在且大小一致则跳过；`force_full` 时全部推送。
pub fn plan_push(local: &[LocalFile], device: &[DeviceFile], force_full: bool) -> PushPlan {
    let mut plan = PushPlan::default();
    if force_full {
        plan.new = local.len();
        plan.to_push = local.to_vec();
        return plan;
    }
    // 设备路径大小写不敏感匹配（与 Python `casefold` 同口径）
    let mut dev: HashMap<String, u64> = HashMap::new();
    for d in device {
        dev.entry(d.rel.to_lowercase()).or_insert(d.size);
    }
    for f in local {
        match dev.get(&f.rel.to_lowercase()) {
            Some(&size) if size == f.size => plan.skipped += 1,
            Some(_) => {
                plan.changed += 1;
                plan.to_push.push(f.clone());
            }
            None => {
                plan.new += 1;
                plan.to_push.push(f.clone());
            }
        }
    }
    plan
}

#[derive(Debug, Clone, Default)]
pub struct DeletePlan {
    pub stale: Vec<DeviceFile>,
    /// 非空 = 拒绝删除的原因（本地集合不可信）
    pub protected_reason: String,
}

/// 计算设备上多余的文件。**本地扫描为空或残缺时一律不删**。
pub fn plan_delete(
    local: &[LocalFile],
    device: &[DeviceFile],
    local_scan_incomplete: bool,
) -> DeletePlan {
    if local.is_empty() {
        return DeletePlan {
            stale: Vec::new(),
            protected_reason: "本地扫描结果为 0 个".into(),
        };
    }
    if local_scan_incomplete {
        return DeletePlan {
            stale: Vec::new(),
            protected_reason: "本地扫描不完整（有子树无法读取）".into(),
        };
    }
    let keep: HashSet<String> = local.iter().map(|f| f.rel.to_lowercase()).collect();
    let stale: Vec<DeviceFile> = device
        .iter()
        .filter(|d| !keep.contains(&d.rel.to_lowercase()))
        .cloned()
        .collect();
    DeletePlan {
        stale,
        protected_reason: String::new(),
    }
}

// ---------------------------------------------------------------------------
// adb 进程封装
// ---------------------------------------------------------------------------

fn read_lossy(mut r: impl std::io::Read) -> String {
    let mut buf = Vec::new();
    let _ = r.read_to_end(&mut buf);
    String::from_utf8_lossy(&buf).to_string()
}

/// 执行子进程并等待退出，带超时（std 的 `output()` 没有超时，adb 卡死会永远占线程）。
fn run_cmd(exe: &Path, args: &[&str], timeout: Duration) -> Result<(i32, String, String), String> {
    use std::process::{Command, Stdio};
    let mut cmd = Command::new(exe);
    cmd.args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    crate::proc_util::hide_console(&mut cmd);
    let mut child = cmd
        .spawn()
        .map_err(|e| format!("adb 启动失败：{}（{e}）", exe.display()))?;
    let out_h = child.stdout.take().map(|s| std::thread::spawn(move || read_lossy(s)));
    let err_h = child.stderr.take().map(|s| std::thread::spawn(move || read_lossy(s)));

    let start = std::time::Instant::now();
    let status = loop {
        match child.try_wait() {
            Ok(Some(s)) => break s,
            Ok(None) => {
                if start.elapsed() >= timeout {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err(format!(
                        "adb 命令超时（>{} 秒）：{}",
                        timeout.as_secs(),
                        args.join(" ")
                    ));
                }
                std::thread::sleep(Duration::from_millis(50));
            }
            Err(e) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(format!("等待 adb 进程失败：{e}"));
            }
        }
    };
    let out = out_h.and_then(|h| h.join().ok()).unwrap_or_default();
    let err = err_h.and_then(|h| h.join().ok()).unwrap_or_default();
    Ok((status.code().unwrap_or(-1), out, err))
}

struct Adb {
    exe: PathBuf,
    serial: String,
}

impl Adb {
    fn run(&self, args: &[&str], timeout: Duration) -> Result<(i32, String, String), String> {
        run_cmd(&self.exe, args, timeout)
    }

    fn verify_device(&self) -> Result<(), String> {
        let (code, out, err) = self.run(
            &["-s", &self.serial, "get-state"],
            Duration::from_secs(60),
        )?;
        let state = out.trim();
        if code == 0 && state == "device" {
            return Ok(());
        }
        let detail = if err.trim().is_empty() {
            state.to_string()
        } else {
            err.trim().to_string()
        };
        Err(format!(
            "设备 {} 未连接或不在线（state={state}）：{detail}",
            self.serial
        ))
    }

    fn shell_status(&self, cmd: &str) -> Result<(i32, String, String), String> {
        self.run(&["-s", &self.serial, "shell", cmd], Duration::from_secs(600))
    }

    fn shell(&self, cmd: &str) -> Result<String, String> {
        let (code, out, err) = self.shell_status(cmd)?;
        if code != 0 {
            let detail = if err.trim().is_empty() {
                out.trim().to_string()
            } else {
                err.trim().to_string()
            };
            return Err(format!("adb shell 返回 {code}：{detail}"));
        }
        Ok(out)
    }

    fn push(&self, local: &Path, remote: &str) -> Result<(), String> {
        let local_s = local.to_string_lossy().to_string();
        let (code, out, err) = self.run(
            &["-s", &self.serial, "push", &local_s, remote],
            Duration::from_secs(1800),
        )?;
        if code != 0 {
            let detail = [out.trim(), err.trim()]
                .into_iter()
                .filter(|s| !s.is_empty())
                .collect::<Vec<_>>()
                .join(" ");
            return Err(format!("adb push 失败（退出码 {code}）：{detail}"));
        }
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// find 表达式 / 设备扫描 / 推送 / 删除
// ---------------------------------------------------------------------------

/// `find` 的扩展名条件。
pub fn find_expr(kind: SyncKind) -> String {
    match kind {
        SyncKind::Script => "-iname '*.funscript'".to_string(),
        SyncKind::Video => {
            let parts: Vec<String> = VIDEO_EXTS
                .iter()
                .map(|e| format!("-iname '*.{e}'"))
                .collect();
            format!("\\( {} \\)", parts.join(" -o "))
        }
    }
}

/// 设备扫描。优先 stat（大小+路径一行），失败回退 ls（批量 / 逐个）。
fn scan_device(
    adb: &Adb,
    folder: &str,
    kind: SyncKind,
    log: &mut dyn FnMut(String),
) -> Result<Vec<DeviceFile>, String> {
    let expr = find_expr(kind);
    // 只接受目标扩展名的相对路径：即使解析到异常行（例如文件名含换行被截断），
    // 也不会把非本类型的路径带进增量/删除列表。
    let parse = |out: &str| -> Vec<DeviceFile> {
        parse_device_listing(out, folder)
            .into_iter()
            .filter(|f| kind.matches(&f.rel))
            .collect()
    };

    let stat_cmd = format!(
        "find {} -type f {} -exec stat -c '%s|%n' {{}} + 2>/dev/null",
        shq(folder),
        expr
    );
    match adb.shell_status(&stat_cmd) {
        Ok((0, out, _)) => {
            if out.trim().is_empty() {
                return Ok(Vec::new());
            }
            let files = parse(&out);
            if !files.is_empty() {
                return Ok(files);
            }
            log("警告：设备 stat 输出无法解析，改用 ls 重试".into());
        }
        Ok((code, _, err)) => log(format!(
            "设备 stat 不可用（退出码 {code}）：{}，改用 ls",
            err.trim()
        )),
        Err(e) => return Err(format!("扫描设备目录失败：{e}")),
    }

    let ls_cmd = format!(
        "find {} -type f {} -exec ls -l {{}} +",
        shq(folder),
        expr
    );
    match adb.shell_status(&ls_cmd) {
        Ok((0, out, _)) => {
            if out.trim().is_empty() {
                return Ok(Vec::new());
            }
            let files = parse(&out);
            if !files.is_empty() {
                return Ok(files);
            }
            log("警告：设备 ls 输出无法解析，改用逐个 ls 重试".into());
        }
        Ok((code, _, err)) => log(format!(
            "设备 find -exec ls + 不可用（退出码 {code}）：{}",
            err.trim()
        )),
        Err(e) => return Err(format!("扫描设备目录失败：{e}")),
    }

    let slow_cmd = format!(
        "find {} -type f {} -exec ls -l {{}} \\;",
        shq(folder),
        expr
    );
    let (code, out, err) = adb
        .shell_status(&slow_cmd)
        .map_err(|e| format!("扫描设备目录失败：{e}"))?;
    if code != 0 {
        return Err(format!(
            "扫描设备目录失败（退出码 {code}）：{}",
            err.trim()
        ));
    }
    if out.trim().is_empty() {
        return Ok(Vec::new());
    }
    let files = parse(&out);
    if files.is_empty() {
        return Err("扫描设备目录失败：无法解析设备返回的文件列表".into());
    }
    Ok(files)
}

fn remote_dir_for(folder: &str, rel: &str) -> String {
    let root = folder.trim_end_matches('/');
    match rel.rsplit_once('/') {
        Some((dir, _)) => format!("{root}/{dir}"),
        None => root.to_string(),
    }
}

/// 按命令行长度分批（避免超过 Windows/Android 的命令行上限）。
fn batches(items: &[String], prefix_len: usize, max_len: usize) -> Vec<Vec<String>> {
    let mut out: Vec<Vec<String>> = Vec::new();
    let mut cur: Vec<String> = Vec::new();
    let mut len = prefix_len;
    for item in items {
        let add = item.len() + 1;
        if !cur.is_empty() && len + add > max_len {
            out.push(std::mem::take(&mut cur));
            len = prefix_len;
        }
        cur.push(item.clone());
        len += add;
    }
    if !cur.is_empty() {
        out.push(cur);
    }
    out
}

fn push_files(
    adb: &Adb,
    root: &Path,
    folder: &str,
    files: &[LocalFile],
    log: &mut dyn FnMut(String),
    result: &mut SyncResult,
) {
    // 先批量建远程子目录（一个命令建多个，避免每个文件一次 shell）
    let mut dirs: Vec<String> = Vec::new();
    let mut seen_dirs: HashSet<String> = HashSet::new();
    for f in files {
        let dir = remote_dir_for(folder, &f.rel);
        if seen_dirs.insert(dir.clone()) {
            dirs.push(dir);
        }
    }
    let quoted: Vec<String> = dirs.iter().map(|d| shq(d)).collect();
    for batch in batches(&quoted, "mkdir -p ".len(), 28000) {
        let cmd = format!("mkdir -p {}", batch.join(" "));
        if let Err(e) = adb.shell(&cmd) {
            log(format!("警告：创建设备子目录失败：{e}"));
        }
    }

    let total = files.len();
    let mut consecutive_fail = 0u32;
    for (idx, f) in files.iter().enumerate() {
        let src = root.join(f.rel.replace('/', std::path::MAIN_SEPARATOR_STR));
        if !src.is_file() {
            log(format!("跳过已消失文件：{}", f.rel));
            // 单独计数：skipped 的既有语义是"设备已有且大小一致"
            result.missing += 1;
            continue;
        }
        let target = format!("{}/", remote_dir_for(folder, &f.rel));
        match adb.push(&src, &target) {
            Ok(()) => {
                result.pushed += 1;
                consecutive_fail = 0;
            }
            Err(e) => {
                result.failed += 1;
                consecutive_fail += 1;
                log(format!("推送失败：{}（{e}）", f.rel));
                if consecutive_fail >= 5 {
                    log("警告：连续 5 个文件推送失败，停止本轮推送".into());
                    break;
                }
            }
        }
        if (idx + 1) % 10 == 0 || idx + 1 == total {
            log(format!(
                "已处理 {}/{}（成功 {}，失败 {}）",
                idx + 1,
                total,
                result.pushed,
                result.failed
            ));
        }
    }
}

fn delete_files(
    adb: &Adb,
    folder: &str,
    stale: &[DeviceFile],
    log: &mut dyn FnMut(String),
    result: &mut SyncResult,
) {
    let quoted: Vec<String> = stale
        .iter()
        .map(|d| shq(&format!("{}/{}", folder.trim_end_matches('/'), d.rel)))
        .collect();
    let total = quoted.len();
    let mut done = 0usize;
    for batch in batches(&quoted, "rm -f ".len(), 28000) {
        let cmd = format!("rm -f {}", batch.join(" "));
        match adb.shell(&cmd) {
            Ok(_) => {
                done += batch.len();
                result.deleted += batch.len();
                log(format!("已删除 {done}/{total}"));
            }
            Err(e) => {
                result.failed += batch.len();
                log(format!("删除失败（本批 {} 个）：{e}", batch.len()));
            }
        }
    }

    // 清理多余文件残留的空目录（保留设备根目录本身；失败不影响结果）
    let cmd = format!(
        "find {} -mindepth 1 -depth -type d -empty -delete 2>/dev/null || true",
        shq(folder)
    );
    if let Err(e) = adb.shell(&cmd) {
        log(format!("警告：清理空目录失败：{e}"));
    }
}

// ---------------------------------------------------------------------------
// 同步执行
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct SyncOptions {
    pub kind: SyncKind,
    pub local_folder: PathBuf,
    pub device_folder: String,
    pub serial: String,
    pub force_full: bool,
    pub delete_extra: bool,
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct SyncResult {
    /// 本地文件数（Python result.local）
    pub local: usize,
    /// 同步结束后设备文件数（Python result.device）
    pub device: usize,
    /// 本次推送成功数（Python result.pushed）
    pub pushed: usize,
    pub skipped: usize,
    /// 推送阶段"本地文件已消失"的个数：与 skipped（设备已有、无需推送）
    /// 语义不同，混在一个计数里会让汇总日志误导
    pub missing: usize,
    pub deleted: usize,
    pub failed: usize,
}

/// 执行一次同步。所有 adb 调用都是阻塞的 —— 只允许在 `spawn_blocking` 里调。
pub fn run_sync(
    opts: &SyncOptions,
    adb_exe: &Path,
    log: &mut dyn FnMut(String),
) -> Result<SyncResult, String> {
    let kind = opts.kind;
    let noun = kind.short_label();
    log("==== 开始同步 ====".into());

    // 设备目录再归一化+护栏一次（handler 已做过，防御直接调用）
    let folder = normalize_device_folder(&opts.device_folder, kind)?;
    if opts.serial.trim().is_empty() {
        return Err("请先连接设备".into());
    }
    log(format!("本地目录：{}", opts.local_folder.display()));
    log(format!("设备目录：{folder}"));
    if opts.force_full {
        log("强制全量模式：全部重推".into());
    }
    if opts.delete_extra {
        log("已启用「删除设备多余文件」".into());
    }

    // 1) 本地扫描
    let local = scan_local(&opts.local_folder, kind)?;
    log(format!("本地{noun}共 {} 个", local.files.len()));
    if local.incomplete {
        log("警告：本地扫描不完整（有子树无法读取），本轮不会删除设备文件".into());
    }

    // 2) 设备校验 + 建目标目录
    let adb = Adb {
        exe: adb_exe.to_path_buf(),
        serial: opts.serial.clone(),
    };
    adb.verify_device()?;
    log(format!("设备校验通过：{}", opts.serial));
    adb.shell(&format!("mkdir -p {}", shq(&folder)))
        .map_err(|e| format!("创建设备目录失败：{e}"))?;

    // 3) 增量计划
    let mut result = SyncResult {
        local: local.files.len(),
        missing: 0,
        ..Default::default()
    };
    let mut device_files: Vec<DeviceFile> = Vec::new();
    if opts.force_full {
        result.skipped = 0;
    } else {
        device_files = scan_device(&adb, &folder, kind, log)?;
        log(format!("设备现有 {} 个", device_files.len()));
        result.device = device_files.len();
    }
    let plan = plan_push(&local.files, &device_files, opts.force_full);
    if opts.force_full {
        log(format!("待推送 {} 个", plan.to_push.len()));
    } else {
        log(format!(
            "跳过 {} 个（已存在且大小一致），待推送 {} 个（新增 {} / 大小变化 {}）",
            plan.skipped,
            plan.to_push.len(),
            plan.new,
            plan.changed
        ));
        result.skipped = plan.skipped;
    }

    // 4) 推送（目标目录以 / 结尾）
    if !plan.to_push.is_empty() {
        log(format!("开始推送 {} 个{noun}文件…", plan.to_push.len()));
        push_files(&adb, &opts.local_folder, &folder, &plan.to_push, log, &mut result);
    }

    // 5) 删除设备多余文件
    if opts.delete_extra {
        match scan_device(&adb, &folder, kind, log) {
            Ok(after) => {
                let dp = plan_delete(&local.files, &after, local.incomplete);
                if !dp.protected_reason.is_empty() {
                    log(format!(
                        "警告：{}，已跳过「删除设备多余文件」以免误删设备上文件；请确认本地目录是否选错或缺少读取权限",
                        dp.protected_reason
                    ));
                } else if dp.stale.is_empty() {
                    log("设备上没有多余文件".into());
                } else {
                    log(format!("删除设备上多余文件 {} 个…", dp.stale.len()));
                    delete_files(&adb, &folder, &dp.stale, log, &mut result);
                }
            }
            Err(e) => {
                // 设备清单不可信时绝不删除
                log(format!("警告：重新扫描设备失败（{e}），已跳过删除以免误删"));
            }
        }
    }

    // 6) 数量校验（失败只警告，不影响已完成的结果）
    match scan_device(&adb, &folder, kind, log) {
        Ok(files) => {
            result.device = files.len();
            if files.len() < local.files.len() {
                log(format!(
                    "警告：设备数量少于本地（差 {}），请检查设备目录配置",
                    local.files.len() - files.len()
                ));
            }
        }
        Err(e) => log(format!("警告：无法统计设备文件数量（{e}）")),
    }

    log(format!(
        "同步完成：本地 {} 个，设备 {} 个，本次推送 {} 个",
        result.local, result.device, result.pushed
    ));
    if result.skipped > 0 {
        log(format!("跳过 {} 个（无需推送）", result.skipped));
    }
    if result.missing > 0 {
        log(format!("本地已消失 {} 个（未推送）", result.missing));
    }
    if result.deleted > 0 {
        log(format!("删除设备多余文件 {} 个", result.deleted));
    }
    if result.failed > 0 {
        log(format!("警告：{} 个文件推送/删除失败", result.failed));
    }
    log("==== 同步结束 ====".into());
    Ok(result)
}

// ---------------------------------------------------------------------------
// 运行状态（供 /api/state 的 sync.slots 展示）
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Default)]
pub struct SlotSnapshot {
    pub busy: bool,
    pub logs: Vec<String>,
    pub result: Option<SyncResult>,
    pub error: String,
    pub serial: String,
}

#[derive(Default)]
struct SlotState {
    busy: bool,
    logs: Vec<String>,
    result: Option<SyncResult>,
    error: String,
    serial: String,
    device: String,
}

impl SlotState {
    fn reset_for_run(&mut self, serial: &str) {
        self.busy = true;
        self.error.clear();
        self.result = None;
        self.logs.clear();
        self.serial = serial.to_string();
        self.device = serial.to_string();
    }
}

#[derive(Default)]
struct Slots {
    slots: [SlotState; 2],
}

/// 两个同步槽的运行状态（脚本 / 视频各一个，互不阻塞）。
pub struct SyncService {
    inner: Mutex<Slots>,
}

impl Default for SyncService {
    fn default() -> Self {
        Self::new()
    }
}

impl SyncService {
    pub fn new() -> Self {
        Self {
            inner: Mutex::new(Slots::default()),
        }
    }

    fn guard(&self) -> MutexGuard<'_, Slots> {
        self.inner.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// 原子地检查 busy 并置位；失败时返回中文原因（与 Python `SyncService.sync` 同锁）。
    pub fn begin(&self, kind: SyncKind, serial: &str) -> Result<(), String> {
        let mut g = self.guard();
        let slot = &mut g.slots[kind.index()];
        if slot.busy {
            return Err("该类型正在同步中".into());
        }
        slot.reset_for_run(serial);
        Ok(())
    }

    pub fn finish_ok(&self, kind: SyncKind, result: SyncResult) {
        let mut g = self.guard();
        let slot = &mut g.slots[kind.index()];
        slot.busy = false;
        slot.error.clear();
        slot.result = Some(result);
    }

    pub fn finish_err(&self, kind: SyncKind, error: impl Into<String>) {
        let mut g = self.guard();
        let slot = &mut g.slots[kind.index()];
        slot.busy = false;
        slot.error = error.into();
    }

    /// 追加一条同步日志（带本地时间前缀，最多保留 400 行）。
    pub fn log(&self, kind: SyncKind, msg: impl Into<String>) {
        let msg = msg.into();
        let stamp = now_hms();
        let line = if stamp.is_empty() {
            msg
        } else {
            format!("[{stamp}] {msg}")
        };
        let mut g = self.guard();
        let slot = &mut g.slots[kind.index()];
        slot.logs.push(line);
        if slot.logs.len() > 400 {
            let drop_n = slot.logs.len() - 400;
            slot.logs.drain(..drop_n);
        }
    }

    /// 给 UI 的快照（只取最近 120 行，与 Python `public()` 一致）。
    pub fn snapshot(&self, kind: SyncKind) -> SlotSnapshot {
        let g = self.guard();
        let slot = &g.slots[kind.index()];
        let skip = slot.logs.len().saturating_sub(120);
        SlotSnapshot {
            busy: slot.busy,
            logs: slot.logs[skip..].to_vec(),
            result: slot.result.clone(),
            error: slot.error.clone(),
            serial: slot.serial.clone(),
        }
    }

    pub fn is_busy(&self, kind: SyncKind) -> bool {
        self.guard().slots[kind.index()].busy
    }
}

/// 后台任务入口：执行并落结果/日志，任何错误都清 busy（含 panic）。
pub fn run_job(
    svc: &Arc<SyncService>,
    host_logs: &crate::logging::LogStore,
    opts: SyncOptions,
    adb_exe: PathBuf,
) {
    let kind = opts.kind;
    let label = kind.short_label();
    let svc2 = Arc::clone(svc);
    let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        run_sync(&opts, &adb_exe, &mut |msg| svc2.log(kind, msg))
    }));
    match outcome {
        Ok(Ok(r)) => {
            let line = format!(
                "{label}同步完成：本地 {} / 设备 {} / 推送 {}（跳过 {}，删除 {}，失败 {}）",
                r.local, r.device, r.pushed, r.skipped, r.deleted, r.failed
            );
            svc.log(kind, line.clone());
            host_logs.info(line);
            svc.finish_ok(kind, r);
        }
        Ok(Err(e)) => {
            svc.log(kind, format!("同步失败：{e}"));
            host_logs.error(format!("{label}同步失败：{e}"));
            svc.finish_err(kind, e);
        }
        Err(_) => {
            let e = "同步任务异常中止（内部错误）".to_string();
            svc.log(kind, format!("同步失败：{e}"));
            host_logs.error(format!("{label}同步失败：{e}"));
            svc.finish_err(kind, e);
        }
    }
}

#[cfg(windows)]
fn now_hms() -> String {
    use windows::Win32::System::SystemInformation::GetLocalTime;
    let t = unsafe { GetLocalTime() };
    format!("{:02}:{:02}:{:02}", t.wHour, t.wMinute, t.wSecond)
}

#[cfg(not(windows))]
fn now_hms() -> String {
    String::new()
}

// ---------------------------------------------------------------------------
// 单元测试（纯函数；不调用真实 adb）
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    fn tmp_dir(tag: &str) -> PathBuf {
        static N: AtomicUsize = AtomicUsize::new(0);
        let n = N.fetch_add(1, Ordering::SeqCst);
        let d = std::env::temp_dir().join(format!(
            "nexus-sync-{tag}-{}-{n}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    fn lf(rel: &str, size: u64) -> LocalFile {
        LocalFile {
            rel: rel.into(),
            size,
        }
    }

    fn df(rel: &str, size: u64) -> DeviceFile {
        DeviceFile {
            rel: rel.into(),
            size,
        }
    }

    // ---- 扩展名过滤 ----

    #[test]
    fn script_extension_is_case_insensitive() {
        assert!(is_script_name("a.funscript"));
        assert!(is_script_name("A.FUNSCRIPT"));
        assert!(is_script_name("带 空格.中文.funscript"));
        assert!(!is_script_name("a.funscript.bak"));
        assert!(!is_script_name("funscript"));
        assert!(!is_script_name("a.mp4"));
    }

    #[test]
    fn video_extension_list_matches_python() {
        for n in [
            "a.mp4", "B.MKV", "c.mov", "d.AVI", "e.webm", "f.ts", "g.m2ts", "h.mts",
            "i.wmv", "j.flv", "k.mpg", "l.mpeg", "m.3gp", "n.ogv", "o.vob",
        ] {
            assert!(is_video_name(n), "{n} 应认作视频");
        }
        for n in ["a.funscript", "b.srt", "c.txt", "noext", ".mp4"] {
            assert!(!is_video_name(n), "{n} 不该认作视频");
        }
    }

    #[test]
    fn kind_filters_and_find_expr() {
        assert!(SyncKind::Script.matches("x.FUNSCRIPT"));
        assert!(!SyncKind::Script.matches("x.mp4"));
        assert!(SyncKind::Video.matches("x.MP4"));
        assert!(!SyncKind::Video.matches("x.funscript"));

        assert_eq!(find_expr(SyncKind::Script), "-iname '*.funscript'");
        let video = find_expr(SyncKind::Video);
        assert!(video.starts_with("\\( "), "视频表达式需要括号：{video}");
        assert!(video.contains("-iname '*.mkv'"));
        assert!(video.contains(" -o "));
        assert!(video.ends_with(" \\)"));
    }

    // ---- 本地扫描 ----

    #[test]
    fn local_scan_recurses_and_filters() {
        let root = tmp_dir("scan");
        std::fs::write(root.join("a.funscript"), b"12345").unwrap();
        std::fs::write(root.join("b.MP4"), b"video").unwrap();
        std::fs::write(root.join("note.txt"), b"x").unwrap();
        std::fs::create_dir_all(root.join("sub")).unwrap();
        std::fs::write(root.join("sub").join("c.FUNSCRIPT"), b"12").unwrap();

        let script = scan_local(&root, SyncKind::Script).unwrap();
        assert_eq!(
            script.files,
            vec![lf("a.funscript", 5), lf("sub/c.FUNSCRIPT", 2)],
            "递归且只认 .funscript"
        );
        assert!(!script.incomplete);

        let video = scan_local(&root, SyncKind::Video).unwrap();
        assert_eq!(video.files, vec![lf("b.MP4", 5)]);

        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn local_scan_missing_root_errors() {
        let p = std::env::temp_dir().join("nexus-sync-definitely-missing");
        let e = scan_local(&p, SyncKind::Script).unwrap_err();
        assert!(e.contains("不存在"), "错误应说明目录不存在：{e}");
    }

    // ---- 设备清单解析 ----

    #[test]
    fn device_listing_parses_stat_and_ls_with_spaces_and_chinese() {
        let out = concat!(
            "123|/sdcard/Funscript/sub/a.funscript\n",
            "-rw-rw---- 1 u0_a123 media_rw 456 2024-05-06 07:08 /sdcard/Funscript/中文 名称.funscript\r\n",
            "0|/sdcard/Funscript/empty.funscript\n",
        );
        let files = parse_device_listing(out, "/sdcard/Funscript");
        assert_eq!(
            files,
            vec![
                df("empty.funscript", 0),
                df("sub/a.funscript", 123),
                df("中文 名称.funscript", 456),
            ],
            "stat/ls 两种格式都要能解析，空格与中文不能拆坏"
        );
    }

    #[test]
    fn device_listing_handles_ls_column_padding() {
        // GNU/部分 toybox 的 ls -l 会在 size、日期前补空格（双空格），
        // 解析不能依赖固定字符偏移。
        let out = concat!(
            "-rw-rw---- 1 u0_a123 media_rw   1234 2024-05-06 07:08 /sdcard/Funscript/a b.funscript\n",
            "-rw-rw---- 1 u0_a123 media_rw 567 2024-05-06  7:08 /sdcard/Funscript/c.funscript\n",
        );
        let files = parse_device_listing(out, "/sdcard/Funscript");
        assert_eq!(
            files,
            vec![df("a b.funscript", 1234), df("c.funscript", 567)]
        );
    }

    #[test]
    fn device_listing_ignores_outside_and_traversal_paths() {
        let out = concat!(
            "1|/sdcard/Other/x.funscript\n",
            "2|/sdcard/Funscript/../Secret/y.funscript\n",
            "3|/sdcard/Funscript/ok.funscript\n",
            "4|/sdcard/Funscript/\n",
            "total 0\n",
            "garbage line\n",
        );
        let files = parse_device_listing(out, "/sdcard/Funscript");
        assert_eq!(files, vec![df("ok.funscript", 3)], "目录外/越界路径必须丢弃");
    }

    #[test]
    fn device_listing_matches_folder_case_insensitively() {
        let out = "9|/SDCARD/FUNSCRIPT/a.funscript\n";
        let files = parse_device_listing(out, "/sdcard/Funscript");
        assert_eq!(files, vec![df("a.funscript", 9)]);
    }

    #[test]
    fn device_listing_tolerates_trailing_slash_in_folder() {
        let out = "7|/sdcard/Funscript/a.funscript\n";
        let files = parse_device_listing(out, "/sdcard/Funscript///");
        assert_eq!(files, vec![df("a.funscript", 7)]);
    }

    #[test]
    fn device_listing_handles_folder_with_spaces_and_chinese() {
        let out = "42|/sdcard/我的 视频/sub/片 名.mp4\n";
        let files = parse_device_listing(out, "/sdcard/我的 视频");
        assert_eq!(files, vec![df("sub/片 名.mp4", 42)]);
        assert_eq!(
            normalize_device_folder(" /sdcard/我的 视频/ ", SyncKind::Video).unwrap(),
            "/sdcard/我的 视频"
        );
    }

    // ---- 增量 diff ----

    #[test]
    fn incremental_skips_only_same_size() {
        let local = vec![
            lf("same.funscript", 100),
            lf("changed.funscript", 200),
            lf("new.funscript", 300),
        ];
        let device = vec![
            df("same.funscript", 100),
            df("changed.funscript", 999),
            df("old-extra.funscript", 1),
        ];
        let plan = plan_push(&local, &device, false);
        assert_eq!(plan.skipped, 1);
        assert_eq!(plan.new, 1);
        assert_eq!(plan.changed, 1);
        let names: Vec<&str> = plan.to_push.iter().map(|f| f.rel.as_str()).collect();
        assert_eq!(names, vec!["changed.funscript", "new.funscript"]);
    }

    #[test]
    fn incremental_matching_is_case_insensitive() {
        let local = vec![lf("Sub/A.funscript", 10)];
        let device = vec![df("sub/a.FUNSCRIPT", 10)];
        let plan = plan_push(&local, &device, false);
        assert_eq!(plan.skipped, 1);
        assert!(plan.to_push.is_empty());
    }

    #[test]
    fn force_full_pushes_everything() {
        let local = vec![lf("a.funscript", 1), lf("b.funscript", 2)];
        let device = vec![df("a.funscript", 1), df("b.funscript", 2)];
        let plan = plan_push(&local, &device, true);
        assert_eq!(plan.to_push.len(), 2);
        assert_eq!(plan.skipped, 0);
        assert!(plan.changed == 0);
    }

    // ---- delete_extra 保护 ----

    #[test]
    fn delete_protection_when_local_empty() {
        let device = vec![df("a.funscript", 1), df("b.funscript", 2)];
        let plan = plan_delete(&[], &device, false);
        assert!(plan.stale.is_empty(), "本地为空时绝不能删任何设备文件");
        assert!(plan.protected_reason.contains("0 个"));
    }

    #[test]
    fn delete_protection_when_scan_incomplete() {
        let local = vec![lf("keep.funscript", 1)];
        let device = vec![df("keep.funscript", 1), df("maybe.funscript", 2)];
        let plan = plan_delete(&local, &device, true);
        assert!(plan.stale.is_empty(), "扫描残缺时必须拒绝删除");
        assert!(plan.protected_reason.contains("不完整"));
    }

    #[test]
    fn delete_plan_lists_only_device_extras() {
        let local = vec![lf("keep.funscript", 1), lf("sub/keep2.funscript", 2)];
        let device = vec![
            df("keep.funscript", 1),
            df("SUB/KEEP2.FUNSCRIPT", 2),
            df("extra.funscript", 3),
            df("sub/extra2.funscript", 4),
        ];
        let plan = plan_delete(&local, &device, false);
        assert!(plan.protected_reason.is_empty());
        let names: Vec<&str> = plan.stale.iter().map(|d| d.rel.as_str()).collect();
        assert_eq!(names, vec!["extra.funscript", "sub/extra2.funscript"]);
    }

    // ---- 设备目录护栏 ----

    #[test]
    fn device_folder_guard_rejects_unsafe_roots() {
        for bad in [
            "/",
            "/sdcard",
            "/sdcard/",
            "/storage",
            "/storage/emulated",
            "/storage/emulated/0",
            "/sdcard/Android",
            "/sdcard/Android/data",
            "/system",
            "/data/local/tmp",
            "/foo",
            "/sdcard/Funscript/..",
            "/sdcard/Funscript/../../storage",
        ] {
            let r = normalize_device_folder(bad, SyncKind::Script);
            assert!(r.is_err(), "{bad} 应被拒绝，实际 {r:?}");
        }
    }

    #[test]
    fn device_folder_guard_accepts_normal_targets() {
        assert_eq!(
            normalize_device_folder("", SyncKind::Script).unwrap(),
            "/sdcard/Funscript"
        );
        assert_eq!(
            normalize_device_folder("", SyncKind::Video).unwrap(),
            "/sdcard/Movies"
        );
        assert_eq!(
            normalize_device_folder("/sdcard/Funscript/", SyncKind::Script).unwrap(),
            "/sdcard/Funscript"
        );
        assert_eq!(
            normalize_device_folder("sdcard//Funscript/../Download", SyncKind::Script).unwrap(),
            "/sdcard/Download"
        );
        // 反斜杠（Windows 习惯输入）也要能归一化
        assert_eq!(
            normalize_device_folder(r"\sdcard\Video\", SyncKind::Video).unwrap(),
            "/sdcard/Video"
        );
    }

    #[test]
    fn shell_quote_escapes_single_quotes() {
        assert_eq!(shq("/sdcard/Funscript"), "'/sdcard/Funscript'");
        assert_eq!(shq("/sdcard/a b/中文"), "'/sdcard/a b/中文'");
        assert_eq!(shq("it's"), "'it'\\''s'");
    }

    #[test]
    fn remote_dirs_keep_relative_structure() {
        assert_eq!(
            remote_dir_for("/sdcard/Funscript", "a.funscript"),
            "/sdcard/Funscript"
        );
        assert_eq!(
            remote_dir_for("/sdcard/Funscript", "sub/a.funscript"),
            "/sdcard/Funscript/sub"
        );
    }

    #[test]
    fn command_batches_respect_length_limit() {
        let items: Vec<String> = vec!["'aaaa'".into(), "'bbbb'".into(), "'cccc'".into()];
        let bs = batches(&items, 6, 20);
        assert_eq!(bs.len(), 2, "超长时必须分批");
        assert_eq!(bs.iter().map(|b| b.len()).sum::<usize>(), 3);
        for b in &bs {
            let len = 6 + b.iter().map(|s| s.len() + 1).sum::<usize>();
            assert!(len <= 20, "单条命令 {len} 字节超过上限");
        }
    }

    // ---- 槽位状态 ----

    #[test]
    fn slot_busy_is_per_kind_and_finishes() {
        let svc = SyncService::new();
        svc.begin(SyncKind::Script, "quest-1").unwrap();
        assert!(svc.is_busy(SyncKind::Script));
        assert!(!svc.is_busy(SyncKind::Video));
        assert!(svc.begin(SyncKind::Script, "quest-1").is_err());
        svc.finish_ok(
            SyncKind::Script,
            SyncResult {
                local: 3,
                device: 2,
                pushed: 1,
                skipped: 1,
                missing: 0,
                deleted: 0,
                failed: 0,
            },
        );
        assert!(!svc.is_busy(SyncKind::Script));
        let snap = svc.snapshot(SyncKind::Script);
        assert_eq!(snap.result.unwrap().pushed, 1);
        assert!(snap.error.is_empty());
    }

    #[test]
    fn slot_error_clears_busy() {
        let svc = SyncService::new();
        svc.begin(SyncKind::Video, "quest-1").unwrap();
        svc.finish_err(SyncKind::Video, "设备未连接");
        assert!(!svc.is_busy(SyncKind::Video));
        assert_eq!(svc.snapshot(SyncKind::Video).error, "设备未连接");
    }
}
