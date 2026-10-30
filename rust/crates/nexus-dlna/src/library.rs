//! 媒体库：根目录、key↔路径、目录枚举、DIDL 条目。

use std::path::{Component, Path, PathBuf};

use serde::{Deserialize, Serialize};

/// DLNA 服务端标志位（与 Python `DLNA_FLAGS` 同值）。
pub const DLNA_FLAGS: &str = "01700000000000000000000000000000";

pub const VIDEO_EXT: &[&str] = &[
    "mp4", "mkv", "avi", "mov", "wmv", "webm", "m4v", "mpg", "mpeg", "ts", "m2ts", "strm",
];
pub const AUDIO_EXT: &[&str] = &["mp3", "flac", "wav", "m4a", "aac", "ogg", "opus", "wma"];
pub const SUBTITLE_EXT: &[&str] = &["srt", "ass", "ssa", "vtt", "sub", "smi"];

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MediaRoot {
    pub path: PathBuf,
    pub label: String,
}

impl MediaRoot {
    /// 由目录路径推断 label（末段名；盘符根退化为 `Videos`）。
    pub fn from_path(p: impl Into<PathBuf>) -> Self {
        let path = p.into();
        let label = path
            .file_name()
            .map(|s| s.to_string_lossy().to_string())
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| "Videos".to_string());
        Self { path, label }
    }
}

fn ext_lower(name: &str) -> String {
    Path::new(name)
        .extension()
        .map(|e| e.to_string_lossy().to_lowercase())
        .unwrap_or_default()
}

pub fn is_video(name: &str) -> bool {
    VIDEO_EXT.contains(&ext_lower(name).as_str())
}
pub fn is_audio(name: &str) -> bool {
    AUDIO_EXT.contains(&ext_lower(name).as_str())
}
pub fn is_subtitle(name: &str) -> bool {
    SUBTITLE_EXT.contains(&ext_lower(name).as_str())
}
pub fn is_strm(name: &str) -> bool {
    ext_lower(name) == "strm"
}

/// 展示标题：去掉扩展名。
pub fn display_title(path: &Path) -> String {
    path.file_stem()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_else(|| path.to_string_lossy().to_string())
}

pub fn mime_for(path: &Path) -> &'static str {
    match ext_lower(&path.to_string_lossy()).as_str() {
        "mp4" | "m4v" => "video/mp4",
        "mkv" => "video/x-matroska",
        "avi" => "video/x-msvideo",
        "mov" => "video/quicktime",
        "wmv" => "video/x-ms-wmv",
        "webm" => "video/webm",
        "mpg" | "mpeg" => "video/mpeg",
        "ts" | "m2ts" => "video/mp2t",
        "mp3" => "audio/mpeg",
        "flac" => "audio/flac",
        "wav" => "audio/wav",
        "m4a" => "audio/mp4",
        "aac" => "audio/aac",
        "ogg" | "opus" => "audio/ogg",
        "wma" => "audio/x-ms-wma",
        "srt" => "text/srt",
        "ass" | "ssa" => "text/x-ssa",
        "vtt" => "text/vtt",
        "smi" => "application/x-sami",
        _ => "application/octet-stream",
    }
}

pub fn subtitle_mime(path: &Path) -> &'static str {
    match ext_lower(&path.to_string_lossy()).as_str() {
        "ass" | "ssa" => "text/x-ssa",
        "vtt" => "text/vtt",
        "smi" => "application/x-sami",
        _ => "text/srt",
    }
}

/// `DLNA.ORG_PN` 常用档位。
pub fn dlna_pn_for(mime: &str) -> &'static str {
    match mime {
        "video/mp4" => "AVC_MP4_BL_CIF15_AAC_520",
        "video/x-matroska" => "MKV",
        "audio/mpeg" => "MP3",
        "audio/mp4" => "AAC_ISO_320",
        "audio/flac" => "FLAC",
        _ => "",
    }
}

/// `protocolInfo` 串。
pub fn dlna_proto(path: &Path) -> String {
    let mime = mime_for(path);
    format!("http-get:*:{mime}:DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS={DLNA_FLAGS}")
}

/// 父容器的 ObjectID：目录的父就是它自己上一级（顶层归 CDS 根 `0`）。
pub fn parent_object_id(key: &str) -> String {
    match key.trim_end_matches('/').rsplit_once('/') {
        Some((parent, _)) if !parent.is_empty() => format!("F:{parent}"),
        _ => "0".to_string(),
    }
}

/// 解析 `Range` 头。`Ok(None)` = 无范围；`Ok(Some((s,e)))` = 闭区间；
/// `Err(())` = 越界/无法满足（应答 416）。
pub fn parse_range_header(value: Option<&str>, total: u64) -> Result<Option<(u64, u64)>, ()> {
    let Some(v) = value else { return Ok(None) };
    let v = v.trim();
    let Some(rest) = v.strip_prefix("bytes=") else {
        return Ok(None);
    };
    let Some((a, b)) = rest.split_once('-') else {
        return Ok(None);
    };
    let (a, b) = (a.trim(), b.trim());
    // 多段 Range（如 `bytes=0-1,3-4`）不支持：忽略 Range 回 200 全文（RFC 9110 合法行为），
    // 不能让后续 parse 失败误答 416。
    if rest.contains(',') {
        return Ok(None);
    }
    if total == 0 {
        return Err(());
    }
    match (a.is_empty(), b.is_empty()) {
        (true, false) => {
            let n: u64 = b.parse().map_err(|_| ())?;
            if n == 0 {
                return Err(());
            }
            Ok(Some((total.saturating_sub(n), total - 1)))
        }
        (false, true) => {
            let start: u64 = a.parse().map_err(|_| ())?;
            if start >= total {
                return Err(());
            }
            Ok(Some((start, total - 1)))
        }
        (false, false) => {
            let start: u64 = a.parse().map_err(|_| ())?;
            let end: u64 = b.parse().map_err(|_| ())?;
            if start >= total || start > end {
                return Err(());
            }
            Ok(Some((start, end.min(total - 1))))
        }
        (true, true) => Err(()),
    }
}

/// 外挂字幕：同名、或 `视频名.语言`。
pub fn find_subtitles(video: &Path, siblings: &[String]) -> Vec<(PathBuf, String)> {
    let Some(stem) = video.file_stem().map(|s| s.to_string_lossy().to_lowercase()) else {
        return Vec::new();
    };
    let Some(dir) = video.parent() else {
        return Vec::new();
    };
    let mut out: Vec<(PathBuf, String)> = Vec::new();
    for name in siblings {
        if !is_subtitle(name) {
            continue;
        }
        let p = dir.join(name);
        let lname = name.to_lowercase();
        let Some((base, _)) = lname.rsplit_once('.') else {
            continue;
        };
        let lang = if base == stem {
            String::new()
        } else if let Some(l) = base.strip_prefix(&format!("{stem}.")) {
            l.to_string()
        } else {
            continue;
        };
        out.push((p, lang));
    }
    // 中文优先，其余按语言名
    out.sort_by_key(|(_, lang)| {
        let l = lang.to_lowercase();
        (if l.starts_with("zh") { 0 } else { 1 }, l)
    });
    out
}

pub struct MediaLibrary {
    pub roots: Vec<MediaRoot>,
}

impl MediaLibrary {
    pub fn new(roots: Vec<MediaRoot>) -> Self {
        Self { roots }
    }

    /// 绝对路径 → URL key。
    pub fn path_to_key(&self, path: &Path) -> Option<String> {
        for root in &self.roots {
            if let Ok(rel) = path.strip_prefix(&root.path) {
                let rel = rel.to_string_lossy().replace('\\', "/");
                if self.roots.len() == 1 {
                    return Some(rel);
                }
                return Some(format!("{}/{}", root.label, rel));
            }
        }
        None
    }

    /// URL key → 绝对路径。**任何越界/穿越/联接都返回 None**。
    ///
    /// 调用链已核实（勿再加 urlencoding::decode 做二次解码）：
    /// - httpd `media`/`subtitle`：axum `Path` 提取器已 percent-decode 一次
    /// - httpd `scripts` → `collect_scripts`：axum `Query` 已 percent-decode 一次
    /// - app `browse`：SOAP `ObjectID` 是 XML 文本（xml_unescape 后），无 URL 编码层
    /// 文件名含 `%` 字面量（如 `50%off.mp4`）时二次解码会失败或出错，正是要避免的。
    pub fn key_to_path(&self, key: &str) -> Option<PathBuf> {
        let rel = key.replace('\\', "/");
        let rel = rel.trim_matches('/');
        if rel.is_empty() {
            return None;
        }
        // 盘符注入与 Windows 备用数据流（`file.mp4:stream`）
        if rel.contains(':') {
            return None;
        }
        let mut parts: Vec<String> = Vec::new();
        for c in Path::new(rel).components() {
            match c {
                Component::Normal(s) => parts.push(s.to_string_lossy().to_string()),
                Component::CurDir => {}
                _ => return None, // ParentDir / RootDir / Prefix 一律拒
            }
        }
        if parts.is_empty() {
            return None;
        }

        let (root, rest) = if self.roots.len() == 1 {
            (self.roots.first()?, parts.join("/"))
        } else {
            let (label, tail) = parts.split_first()?;
            let root = self
                .roots
                .iter()
                .find(|r| r.label.eq_ignore_ascii_case(label))?;
            (root, tail.join("/"))
        };
        if rest.is_empty() {
            return None;
        }
        let candidate = root.path.join(&rest);
        if !self.inside_root(root, &candidate) {
            return None;
        }
        if !self.reparse_free_below(&root.path, &candidate) {
            return None;
        }
        Some(candidate)
    }

    fn inside_root(&self, root: &MediaRoot, path: &Path) -> bool {
        let Ok(rp) = root.path.canonicalize() else {
            return false;
        };
        let cp = match path.canonicalize() {
            Ok(p) => p,
            Err(_) => match path.parent().and_then(|p| p.canonicalize().ok()) {
                Some(parent) => match path.file_name() {
                    Some(name) => parent.join(name),
                    None => return false,
                },
                None => return false,
            },
        };
        cp.starts_with(&rp)
    }

    fn reparse_free_below(&self, root: &Path, target: &Path) -> bool {
        let Ok(rel) = target.strip_prefix(root) else {
            return false;
        };
        let mut cur = root.to_path_buf();
        for comp in rel.components() {
            cur = cur.join(comp);
            let Ok(md) = std::fs::symlink_metadata(&cur) else {
                continue; // 不存在：交给调用方 404
            };
            if md.file_type().is_symlink() {
                return false;
            }
            #[cfg(windows)]
            {
                use std::os::windows::fs::MetadataExt;
                const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x400;
                if md.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
                    return false;
                }
            }
        }
        true
    }

    /// 列目录：返回 (子目录名, 可播文件名, 全部文件名)，均已排序。
    pub fn list_dir(&self, dir: &Path) -> std::io::Result<(Vec<String>, Vec<String>, Vec<String>)> {
        let mut dirs = Vec::new();
        let mut media = Vec::new();
        let mut all = Vec::new();
        for e in std::fs::read_dir(dir)? {
            let Ok(e) = e else { continue };
            let name = e.file_name().to_string_lossy().to_string();
            let Ok(ft) = e.file_type() else { continue };
            all.push(name.clone());
            if ft.is_dir() {
                dirs.push(name);
            } else if is_video(&name) || is_audio(&name) {
                media.push(name);
            }
        }
        dirs.sort_by_key(|s| s.to_lowercase());
        media.sort_by_key(|s| s.to_lowercase());
        all.sort_by_key(|s| s.to_lowercase());
        Ok((dirs, media, all))
    }

    /// 收集 `.funscript`（`base` 之下），供"远程脚本源"一次性建索引。
    pub fn collect_scripts(&self, base: &str, limit: usize) -> Vec<(String, String)> {
        let start = if base.is_empty() {
            None
        } else {
            self.key_to_path(base)
        };
        let mut out = Vec::new();
        let mut stack: Vec<PathBuf> = match start {
            Some(p) => vec![p],
            None => self.roots.iter().map(|r| r.path.clone()).collect(),
        };
        while let Some(dir) = stack.pop() {
            if out.len() >= limit {
                break;
            }
            let Ok(rd) = std::fs::read_dir(&dir) else { continue };
            for e in rd.flatten() {
                let p = e.path();
                let Ok(ft) = e.file_type() else { continue };
                if ft.is_dir() {
                    if self.reparse_free_below(&dir, &p) {
                        stack.push(p);
                    }
                } else if ext_lower(&p.to_string_lossy()) == "funscript" {
                    if let Some(key) = self.path_to_key(&p) {
                        let name = p
                            .file_name()
                            .map(|s| s.to_string_lossy().to_string())
                            .unwrap_or_default();
                        out.push((name, key));
                        if out.len() >= limit {
                            break;
                        }
                    }
                }
            }
        }
        out.sort_by(|a, b| a.0.to_lowercase().cmp(&b.0.to_lowercase()));
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn lib_with(root: PathBuf) -> MediaLibrary {
        MediaLibrary::new(vec![MediaRoot {
            path: root,
            label: "videos".into(),
        }])
    }

    #[test]
    fn range_parsing() {
        assert_eq!(parse_range_header(None, 100), Ok(None));
        assert_eq!(parse_range_header(Some("bytes=0-9"), 100), Ok(Some((0, 9))));
        assert_eq!(parse_range_header(Some("bytes=10-"), 100), Ok(Some((10, 99))));
        assert_eq!(parse_range_header(Some("bytes=-10"), 100), Ok(Some((90, 99))));
        assert_eq!(parse_range_header(Some("bytes=100-"), 100), Err(()));
        assert_eq!(parse_range_header(Some("bytes=50-10"), 100), Err(()));
        assert_eq!(parse_range_header(Some("bytes=50-999"), 100), Ok(Some((50, 99))));
        assert_eq!(parse_range_header(Some("items=0-1"), 100), Ok(None));
        // 多段 Range 忽略（回 200 全文），不误答 416
        assert_eq!(parse_range_header(Some("bytes=0-1,3-4"), 100), Ok(None));
    }

    #[test]
    fn key_traversal_is_rejected() {
        let dir = std::env::temp_dir().join(format!("nexus-dlna-t-{}", std::process::id()));
        std::fs::create_dir_all(dir.join("sub")).unwrap();
        std::fs::write(dir.join("sub/a.mp4"), b"x").unwrap();
        let lib = lib_with(dir.clone());

        assert!(lib.key_to_path("sub/a.mp4").is_some(), "正常相对路径应可用");
        assert!(lib.key_to_path("../evil.mp4").is_none(), ".. 必须拒绝");
        assert!(lib.key_to_path("sub/../../evil.mp4").is_none());
        assert!(lib.key_to_path("C:/Windows/system32/cmd.exe").is_none(), "盘符必须拒绝");
        assert!(lib.key_to_path("sub/a.mp4:hidden").is_none(), "ADS 后缀必须拒绝");
        assert!(lib.key_to_path("/abs/path").is_none(), "绝对路径必须拒绝");
        assert!(lib.key_to_path("").is_none());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn key_roundtrip_single_root() {
        let dir = std::env::temp_dir().join(format!("nexus-dlna-rt-{}", std::process::id()));
        std::fs::create_dir_all(dir.join("a")).unwrap();
        let f = dir.join("a/v.mp4");
        std::fs::write(&f, b"x").unwrap();
        let lib = lib_with(dir.clone());
        let key = lib.path_to_key(&f).unwrap();
        assert_eq!(key, "a/v.mp4");
        assert_eq!(lib.key_to_path(&key).unwrap(), f);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn parent_ids() {
        assert_eq!(parent_object_id("a.mp4"), "0");
        assert_eq!(parent_object_id("a/b.mp4"), "F:a");
        assert_eq!(parent_object_id("a/b/c.mp4"), "F:a/b");
    }

    #[test]
    fn subtitle_discovery_prefers_chinese() {
        let dir = std::env::temp_dir().join(format!("nexus-dlna-s-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let video = dir.join("clip.mp4");
        std::fs::write(&video, b"x").unwrap();
        let siblings = vec![
            "clip.mp4".to_string(),
            "clip.en.srt".to_string(),
            "clip.zh.srt".to_string(),
            "other.srt".to_string(),
        ];
        let subs = find_subtitles(&video, &siblings);
        assert_eq!(subs.len(), 2, "只认 clip 的同名/语言后缀");
        assert!(subs[0].0.to_string_lossy().contains("clip.zh"), "中文优先");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn list_dir_splits_dirs_and_media() {
        let dir = std::env::temp_dir().join(format!("nexus-dlna-l-{}", std::process::id()));
        std::fs::create_dir_all(dir.join("sub")).unwrap();
        std::fs::write(dir.join("a.mp4"), b"x").unwrap();
        std::fs::write(dir.join("b.srt"), b"x").unwrap();
        let lib = lib_with(dir.clone());
        let (dirs, media, all) = lib.list_dir(&dir).unwrap();
        assert_eq!(dirs, vec!["sub"]);
        assert_eq!(media, vec!["a.mp4"], "字幕不算可播条目");
        assert_eq!(all.len(), 3);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
