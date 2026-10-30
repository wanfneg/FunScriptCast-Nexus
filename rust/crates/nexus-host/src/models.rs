//! 模型/运行时目录与下载（对齐 Python `MODELS_CATALOG` / `_model_dl_worker`）。
//!
//! 两类条目：
//! - `Zip`：运行时（llama.cpp / audio.cpp），下载 zip → 解压 → 校验可执行文件
//! - `Files`：模型（HF/ModelScope 直链），逐文件下载，支持断点续传；
//!   1.7B 的分片权重下载完后合并成单文件 `model.safetensors`

use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;

use crate::paths;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Kind {
    Zip,
    Files,
}

struct FileSpec {
    rel: &'static str,
    url: &'static str,
}

struct Entry {
    id: &'static str,
    role: &'static str,
    label: &'static str,
    /// 相对部署根的目标目录
    dest: &'static str,
    kind: Kind,
    files: &'static [FileSpec],
    /// Zip 条目的关键可执行文件（相对 dest）
    check: &'static str,
    size_gb: f64,
    /// 分片权重：下载完合并成单文件
    merge_shards: bool,
}

const HF: &str = "https://hf-mirror.com";
const MS: &str = "https://modelscope.cn/models";

static QWEN_06_FILES: &[FileSpec] = &[
    FileSpec { rel: "config.json", url: "https://modelscope.cn/models/Qwen/Qwen3-ASR-0.6B/resolve/master/config.json" },
    FileSpec { rel: "generation_config.json", url: "https://modelscope.cn/models/Qwen/Qwen3-ASR-0.6B/resolve/master/generation_config.json" },
    FileSpec { rel: "merges.txt", url: "https://modelscope.cn/models/Qwen/Qwen3-ASR-0.6B/resolve/master/merges.txt" },
    FileSpec { rel: "model.safetensors", url: "https://modelscope.cn/models/Qwen/Qwen3-ASR-0.6B/resolve/master/model.safetensors" },
    FileSpec { rel: "preprocessor_config.json", url: "https://modelscope.cn/models/Qwen/Qwen3-ASR-0.6B/resolve/master/preprocessor_config.json" },
    FileSpec { rel: "tokenizer_config.json", url: "https://modelscope.cn/models/Qwen/Qwen3-ASR-0.6B/resolve/master/tokenizer_config.json" },
    FileSpec { rel: "vocab.json", url: "https://modelscope.cn/models/Qwen/Qwen3-ASR-0.6B/resolve/master/vocab.json" },
];

static QWEN_17_FILES: &[FileSpec] = &[
    FileSpec { rel: "config.json", url: "https://modelscope.cn/models/Qwen/Qwen3-ASR-1.7B/resolve/master/config.json" },
    FileSpec { rel: "generation_config.json", url: "https://modelscope.cn/models/Qwen/Qwen3-ASR-1.7B/resolve/master/generation_config.json" },
    FileSpec { rel: "merges.txt", url: "https://modelscope.cn/models/Qwen/Qwen3-ASR-1.7B/resolve/master/merges.txt" },
    FileSpec { rel: "model-00001-of-00002.safetensors", url: "https://modelscope.cn/models/Qwen/Qwen3-ASR-1.7B/resolve/master/model-00001-of-00002.safetensors" },
    FileSpec { rel: "model-00002-of-00002.safetensors", url: "https://modelscope.cn/models/Qwen/Qwen3-ASR-1.7B/resolve/master/model-00002-of-00002.safetensors" },
    FileSpec { rel: "model.safetensors.index.json", url: "https://modelscope.cn/models/Qwen/Qwen3-ASR-1.7B/resolve/master/model.safetensors.index.json" },
    FileSpec { rel: "preprocessor_config.json", url: "https://modelscope.cn/models/Qwen/Qwen3-ASR-1.7B/resolve/master/preprocessor_config.json" },
    FileSpec { rel: "tokenizer_config.json", url: "https://modelscope.cn/models/Qwen/Qwen3-ASR-1.7B/resolve/master/tokenizer_config.json" },
    FileSpec { rel: "vocab.json", url: "https://modelscope.cn/models/Qwen/Qwen3-ASR-1.7B/resolve/master/vocab.json" },
];

static SAKURA_7B_FILES: &[FileSpec] = &[FileSpec {
    rel: "sakura-7b-qwen2.5-v1.0-iq4xs.gguf",
    url: "https://hf-mirror.com/SakuraLLM/Sakura-7B-Qwen2.5-v1.0-GGUF/resolve/main/sakura-7b-qwen2.5-v1.0-iq4xs.gguf",
}];

static SAKURA_15B_FILES: &[FileSpec] = &[FileSpec {
    rel: "sakura-1.5b-qwen2.5-v1.0-q5ks.gguf",
    url: "https://hf-mirror.com/shing3232/Sakura-1.5B-Qwen2.5-v1.0-GGUF-IMX/resolve/main/sakura-1.5b-qwen2.5-v1.0-Q5KS.gguf",
}];

static HYMT2_7B_FILES: &[FileSpec] = &[FileSpec {
    rel: "Hy-MT2-7B-Q4_K_M.gguf",
    url: "https://modelscope.cn/models/Tencent-Hunyuan/Hy-MT2-7B-GGUF/resolve/master/Hy-MT2-7B-Q4_K_M.gguf",
}];

static HYMT2_18B_FILES: &[FileSpec] = &[FileSpec {
    rel: "Hy-MT2-1.8B-Q4_K_M.gguf",
    url: "https://modelscope.cn/models/Tencent-Hunyuan/Hy-MT2-1.8B-GGUF/resolve/master/Hy-MT2-1.8B-Q4_K_M.gguf",
}];

static ZIP_VISION: &[FileSpec] = &[FileSpec {
    rel: "vision-module.zip",
    url: "https://github.com/wanfneg/FunScriptCast-Nexus/releases/download/runtime-assets/vision-module.zip",
}];
/// CUDA 翻译运行时：官方 llama.cpp CUDA 12.4 主包 + cudart 包（可选下载）。
static ZIP_LLAMA_CUDA: &[FileSpec] = &[
    FileSpec {
        rel: "llama-b11000-bin-win-cuda-12.4-x64.zip",
        url: "https://github.com/ggml-org/llama.cpp/releases/download/b11000/llama-b11000-bin-win-cuda-12.4-x64.zip",
    },
    FileSpec {
        rel: "cudart-llama-bin-win-cuda-12.4-x64.zip",
        url: "https://github.com/ggml-org/llama.cpp/releases/download/b11000/cudart-llama-bin-win-cuda-12.4-x64.zip",
    },
];
static ZIP_AUDIO_CPU: &[FileSpec] = &[FileSpec {
    rel: "audiocpp-runtime-windows-cpu.zip",
    url: "https://github.com/wanfneg/FunScriptCast-Nexus/releases/download/runtime-assets/audiocpp-runtime-windows-cpu.zip",
}];
static ZIP_AUDIO_CUDA: &[FileSpec] = &[FileSpec {
    rel: "audiocpp-runtime-windows-cuda.zip",
    url: "https://github.com/wanfneg/FunScriptCast-Nexus/releases/download/runtime-assets/audiocpp-runtime-windows-cuda.zip",
}];

const CATALOG: &[Entry] = &[
    Entry {
        id: "vision-module",
        role: "vision-module",
        label: "AI 实时识别模块（边播边推理）",
        dest: "vendor/vision",
        kind: Kind::Zip,
        files: ZIP_VISION,
        check: "bundles/v0.6.0/manifest.json",
        size_gb: 0.02,
        merge_shards: false,
    },
    Entry {
        id: "llama-vulkan",
        role: "translate-runtime",
        label: "翻译运行时 · Vulkan（随包，AMD/Intel/NVIDIA）",
        dest: "vendor/llama-vulkan",
        kind: Kind::Zip,
        files: &[],
        check: "llama-server.exe",
        size_gb: 0.09,
        merge_shards: false,
    },
    Entry {
        id: "llama-runtime",
        role: "translate-runtime",
        label: "翻译 CUDA 加速包（可选下载 · NVIDIA）",
        dest: "vendor/llama-cuda",
        kind: Kind::Zip,
        files: ZIP_LLAMA_CUDA,
        check: "llama-server.exe",
        size_gb: 1.1,
        merge_shards: false,
    },
    Entry {
        id: "audiocpp-runtime",
        role: "asr-runtime",
        label: "识别运行时 · audio.cpp（CPU）",
        dest: "vendor/audiocpp/cpu",
        kind: Kind::Zip,
        files: ZIP_AUDIO_CPU,
        check: "audiocpp_server.exe",
        size_gb: 0.03,
        merge_shards: false,
    },
    Entry {
        id: "audiocpp-vulkan",
        role: "asr-runtime",
        label: "识别运行时 · Vulkan（随包，AMD/Intel/NVIDIA）",
        dest: "vendor/audiocpp/vulkan",
        kind: Kind::Zip,
        files: &[],
        check: "audiocpp_server.exe",
        size_gb: 0.16,
        merge_shards: false,
    },
    Entry {
        id: "audiocpp-cuda",
        role: "asr-runtime",
        label: "识别运行时 · CUDA（需 NVIDIA 显卡）",
        dest: "vendor/audiocpp/gpu",
        kind: Kind::Zip,
        files: ZIP_AUDIO_CUDA,
        check: "audiocpp_server.exe",
        size_gb: 1.1,
        merge_shards: false,
    },
    Entry {
        id: "qwen3-asr-0.6b",
        role: "asr",
        label: "识别模型 · Qwen3-ASR-0.6B（显存约 1.3GB）",
        dest: "models/Qwen3-ASR-0.6B",
        kind: Kind::Files,
        files: QWEN_06_FILES,
        check: "",
        size_gb: 1.8,
        merge_shards: false,
    },
    Entry {
        id: "qwen3-asr-1.7b",
        role: "asr",
        label: "识别模型 · Qwen3-ASR-1.7B（显存约 3.6GB，质量更高）",
        dest: "models/Qwen3-ASR-1.7B",
        kind: Kind::Files,
        files: QWEN_17_FILES,
        check: "",
        size_gb: 4.4,
        merge_shards: true,
    },
    Entry {
        id: "sakura-7b",
        role: "translate",
        label: "翻译模型 · Sakura-7B（日语，显存约 4.4GB）",
        dest: "models/Sakura-7B-Qwen2.5-v1.0",
        kind: Kind::Files,
        files: SAKURA_7B_FILES,
        check: "",
        size_gb: 4.0,
        merge_shards: false,
    },
    Entry {
        id: "sakura-1.5b",
        role: "translate",
        label: "翻译模型 · Sakura-1.5B（日语，显存约 1.4GB，低显存推荐）",
        dest: "models/Sakura-1.5B-Qwen2.5-v1.0",
        kind: Kind::Files,
        files: SAKURA_15B_FILES,
        check: "",
        size_gb: 1.2,
        merge_shards: false,
    },
    Entry {
        id: "hymt2-7b",
        role: "translate",
        label: "翻译模型 · Hy-MT2-7B（英语，显存约 4.7GB）",
        dest: "models/Hy-MT2-7B",
        kind: Kind::Files,
        files: HYMT2_7B_FILES,
        check: "",
        size_gb: 4.3,
        merge_shards: false,
    },
    Entry {
        id: "hymt2-1.8b",
        role: "translate",
        label: "翻译模型 · Hy-MT2-1.8B（英语，显存约 1.2GB，低显存推荐）",
        dest: "models/Hy-MT2-1.8B",
        kind: Kind::Files,
        files: HYMT2_18B_FILES,
        check: "",
        size_gb: 1.1,
        merge_shards: false,
    },
];

fn installed(e: &Entry) -> bool {
    let dir = paths::app_dir().join(e.dest);
    if e.kind == Kind::Zip {
        return dir.join(e.check).is_file();
    }
    if e.merge_shards && !dir.join("model.safetensors").is_file() {
        return false;
    }
    e.files.iter().all(|f| {
        if e.merge_shards
            && (f.rel.starts_with("model-") || f.rel == "model.safetensors.index.json")
        {
            return true; // 合并后已删除
        }
        dir.join(f.rel).is_file()
    })
}

pub async fn catalog() -> Response {
    let items: Vec<_> = CATALOG
        .iter()
        .map(|e| {
            let has = installed(e);
            let d = snapshot(e.id);
            let (dl_state, dl_pct, dl_err) = match d {
                Some(s) if s.state == "downloading" => {
                    ("downloading".to_string(), s.pct, String::new())
                }
                Some(s) if s.state == "error" => ("error".to_string(), s.pct, s.error.clone()),
                // done 态必须透出：前端靠 m.state=="done" 在下载完成后自动刷新
                // 模型下拉（Python 版有此分支）；折叠成 installed 会让新下载的
                // 模型完成后不出现在下拉框里
                Some(s) if s.state == "done" => ("done".to_string(), 100, String::new()),
                _ if has => ("installed".to_string(), 100, String::new()),
                _ => ("absent".to_string(), 0, String::new()),
            };
            json!({
                "id": e.id,
                "role": e.role,
                "label": e.label,
                "size_gb": e.size_gb,
                "installed": has,
                "state": dl_state,
                "pct": dl_pct,
                "error": dl_err,
                "downloadable": !e.files.is_empty(),
            })
        })
        .collect();
    Json(json!({"ok": true, "items": items})).into_response()
}

// ---------------------------------------------------------------- 下载

use std::collections::HashMap;
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};

#[derive(Debug, Clone, Default)]
pub struct DlState {
    pub state: String,
    pub pct: u32,
    pub bytes: u64,
    pub total: u64,
    pub error: String,
}

fn registry() -> &'static Mutex<HashMap<String, DlState>> {
    static REG: OnceLock<Mutex<HashMap<String, DlState>>> = OnceLock::new();
    REG.get_or_init(|| Mutex::new(HashMap::new()))
}

pub fn snapshot(id: &str) -> Option<DlState> {
    registry().lock().unwrap().get(id).cloned()
}

fn download_dir() -> PathBuf {
    let d = paths::app_dir().join("models").join("_download");
    let _ = std::fs::create_dir_all(&d);
    d
}

fn find_entry(id: &str) -> Option<&'static Entry> {
    CATALOG.iter().find(|e| e.id == id)
}

/// 启动下载（幂等：已在下载则直接返回）。
pub fn start(id: &str) -> Result<(), String> {
    let e = find_entry(id).ok_or_else(|| "未知的模型".to_string())?;
    if e.files.is_empty() {
        return Err("该组件随安装包提供，无需下载".to_string());
    }
    {
        let mut reg = registry().lock().unwrap();
        if let Some(s) = reg.get(id) {
            if s.state == "downloading" {
                return Ok(());
            }
        }
        reg.insert(
            id.to_string(),
            DlState {
                state: "downloading".into(),
                ..Default::default()
            },
        );
    }
    let id_owned = id.to_string();
    std::thread::Builder::new()
        .name(format!("model-dl-{id}"))
        .spawn(move || {
            // panic 兜底：下载线程带 panic 死掉会让状态永久卡 downloading 且
            // 无法再启动（sync.rs run_job 的 catch_unwind 同款防线）
            let res = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| worker(e)))
                .unwrap_or_else(|_| Err("下载线程异常退出（panic）".to_string()));
            let mut reg = registry().lock().unwrap();
            let st = reg.entry(id_owned).or_default();
            match res {
                Ok(()) => {
                    st.state = "done".into();
                    st.pct = 100;
                    st.error.clear();
                }
                Err(err) => {
                    st.state = "error".into();
                    st.error = err;
                }
            }
        })
        .map_err(|e| {
            let msg = format!("起不了下载线程：{e}");
            let mut reg = registry().lock().unwrap();
            let st = reg.entry(id.to_string()).or_default();
            st.state = "error".into();
            st.error = msg.clone();
            msg
        })?;
    Ok(())
}

fn set_pct(id: &str, pct: u32, bytes: u64, total: u64) {
    let mut reg = registry().lock().unwrap();
    if let Some(s) = reg.get_mut(id) {
        s.pct = pct;
        s.bytes = bytes;
        s.total = total;
    }
}

/// 下载代理：NEXUS_PROXY 优先；否则读 Windows 系统代理（Clash 等）。
/// GitHub / 运行时包在部分网络必须走代理，模型直链则通常可直连。
fn system_proxy() -> Option<String> {
    static PROXY: OnceLock<Option<String>> = OnceLock::new();
    PROXY
        .get_or_init(|| {
            if let Ok(v) = std::env::var("NEXUS_PROXY") {
                if !v.trim().is_empty() {
                    return Some(v.trim().to_string());
                }
            }
            #[cfg(windows)]
            {
                // ProxyEnable 必须先查：Clash 退出后注册表常残留 ProxyServer
                // （如 127.0.0.1:7897）而系统代理实际已关。不查开关就把死代理
                // 挂上全部下载——三个源同一秒全部 connection refused 的真因。
                let enabled = read_reg_dword(
                    r"HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings",
                    "ProxyEnable",
                )
                .unwrap_or(0)
                    != 0;
                if !enabled {
                    return None;
                }
                let mut reg_cmd = std::process::Command::new("reg");
                reg_cmd.args([
                    "query",
                    r"HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings",
                    "/v",
                    "ProxyServer",
                ]);
                // GUI（windows_subsystem）下 reg 会闪控制台黑框：统一 CREATE_NO_WINDOW
                crate::proc_util::hide_console(&mut reg_cmd);
                let out = reg_cmd.output().ok()?;
                let text = String::from_utf8_lossy(&out.stdout);
                let raw = text
                    .lines()
                    .find(|l| l.contains("ProxyServer"))
                    .and_then(|l| l.split_whitespace().last())
                    .unwrap_or("");
                if raw.is_empty() {
                    return None;
                }
                // `http=host:port;https=host:port` → 取 https；否则原样。
                let server = raw
                    .split(';')
                    .find_map(|p| p.strip_prefix("https="))
                    .or_else(|| raw.split(';').find_map(|p| p.strip_prefix("http=")))
                    .unwrap_or(raw);
                return Some(format!("http://{server}"));
            }
            #[allow(unreachable_code)]
            None
        })
        .clone()
}

/// `reg query … /v <name>` 读一个 REG_DWORD（返回解析出的数值）。
fn read_reg_dword(key: &str, name: &str) -> Option<u32> {
    let mut reg_cmd = std::process::Command::new("reg");
    reg_cmd.args(["query", key, "/v", name]);
    // 同上：GUI 下闪黑框
    crate::proc_util::hide_console(&mut reg_cmd);
    let out = reg_cmd.output().ok()?;
    let text = String::from_utf8_lossy(&out.stdout);
    let line = text.lines().find(|l| l.contains(name))?;
    // 形如 `    ProxyEnable    REG_DWORD    0x1`
    let v = line.split_whitespace().last()?;
    u32::from_str_radix(v.trim_start_matches("0x"), 16).ok()
}

/// 把错误的 source 链拼出来：reqwest 只显示外层 "error sending request for url (...)"，
/// 真正的 connection refused / dns / tls 藏在 source 里。不带链，排障只能靠猜。
pub(crate) fn err_chain(e: &dyn std::error::Error) -> String {
    let mut s = e.to_string();
    let mut cur = e.source();
    while let Some(inner) = cur {
        s.push_str(" ← ");
        s.push_str(&inner.to_string());
        cur = inner.source();
    }
    s
}

fn client() -> Result<reqwest::blocking::Client, String> {
    let mut b = reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_secs(3600));
    if let Some(p) = system_proxy() {
        if let Ok(proxy) = reqwest::Proxy::all(&p) {
            b = b.proxy(proxy);
        }
    }
    b.build()
        .map_err(|e| format!("建下载客户端失败：{e}"))
}

/// 远端文件大小（HEAD）；拿不到就返回 None（不因为无法判断而删除用户文件）。
fn remote_size(url: &str) -> Option<u64> {
    let c = client().ok()?;
    let r = c
        .head(url)
        .timeout(std::time::Duration::from_secs(20))
        .send()
        .ok()?;
    if !r.status().is_success() {
        return None;
    }
    r.content_length()
}

/// 下载单文件到 `out`，支持 `.part` 断点续传；进度映射到 [pct_from, pct_to]。
fn download_file(
    id: &str,
    url: &str,
    out: &Path,
    pct_from: u32,
    pct_to: u32,
) -> Result<(), String> {
    if let Some(parent) = out.parent() {
        std::fs::create_dir_all(parent).map_err(|e| format!("建目录失败：{e}"))?;
    }
    if out.is_file() {
        let local = out.metadata().map(|m| m.len()).unwrap_or(0);
        if local > 0 {
            match remote_size(url) {
                Some(want) if want == local => return Ok(()),
                Some(_) => {
                    let _ = std::fs::remove_file(out);
                }
                None => return Ok(()), // 无法判断：保留用户已下好的文件
            }
        }
    }
    let part = PathBuf::from(format!("{}.part", out.display()));
    let mut resume = part.metadata().map(|m| m.len()).unwrap_or(0);
    let c = client()?;
    let mut req = c.get(url).header("Accept-Encoding", "identity");
    if resume > 0 {
        req = req.header("Range", format!("bytes={resume}-"));
    }
    let mut resp = req.send().map_err(|e| format!("请求失败：{}", err_chain(&e)))?;
    if resp.status() == reqwest::StatusCode::RANGE_NOT_SATISFIABLE && resume > 0 {
        // .part 已经完整：直接落盘
        std::fs::rename(&part, out).map_err(|e| format!("移动文件失败：{e}"))?;
        return Ok(());
    }
    if !resp.status().is_success() {
        return Err(format!("下载失败：HTTP {}", resp.status()));
    }
    let append = resp.status() == reqwest::StatusCode::PARTIAL_CONTENT;
    if !append {
        resume = 0;
        let _ = std::fs::remove_file(&part);
    }
    let total = resp.content_length().unwrap_or(0) + resume;
    let mut f = std::fs::OpenOptions::new()
        .create(true)
        .append(append)
        .write(true)
        .open(&part)
        .map_err(|e| format!("建文件失败：{e}"))?;
    let mut buf = vec![0u8; 256 * 1024];
    let mut done = resume;
    loop {
        let n = resp.read(&mut buf).map_err(|e| format!("读取失败：{e}"))?;
        if n == 0 {
            break;
        }
        f.write_all(&buf[..n]).map_err(|e| format!("写文件失败：{e}"))?;
        done += n as u64;
        let span = pct_to.saturating_sub(pct_from).max(1) as f64;
        let pct = if total > 0 {
            pct_from + ((done as f64 / total as f64) * span) as u32
        } else {
            pct_from
        };
        set_pct(id, pct.min(pct_to), done, total);
    }
    drop(f);
    if total > 0 && done != total {
        return Err(format!("下载不完整：{done}/{total} 字节（已保留断点）"));
    }
    std::fs::rename(&part, out).map_err(|e| format!("移动文件失败：{e}"))?;
    Ok(())
}

fn zip_ok(p: &Path) -> bool {
    std::fs::File::open(p)
        .ok()
        .and_then(|f| zip::ZipArchive::new(f).ok())
        .is_some()
}

fn worker(e: &Entry) -> Result<(), String> {
    let dest = paths::app_dir().join(e.dest);
    std::fs::create_dir_all(&dest).map_err(|x| format!("建目录失败：{x}"))?;
    let n = e.files.len().max(1) as u32;
    match e.kind {
        Kind::Zip => {
            let tmpdir = download_dir();
            let mut zips = Vec::new();
            for (i, f) in e.files.iter().enumerate() {
                let tmp = tmpdir.join(format!("{}--{}", e.id, f.rel));
                if !(tmp.is_file() && zip_ok(&tmp)) {
                    let from = (i as u32 * 95) / n;
                    let to = ((i as u32 + 1) * 95) / n;
                    download_file(e.id, f.url, &tmp, from, to)?;
                    if !zip_ok(&tmp) {
                        let _ = std::fs::remove_file(&tmp);
                        return Err(format!("下载的 zip 无效：{}", f.rel));
                    }
                }
                zips.push(tmp);
            }
            set_pct(e.id, 97, 0, 0);
            for zp in &zips {
                let f = std::fs::File::open(zp).map_err(|x| format!("打开 zip 失败：{x}"))?;
                let mut z = zip::ZipArchive::new(f).map_err(|x| format!("不是有效 zip：{x}"))?;
                z.extract(&dest).map_err(|x| format!("解压失败：{x}"))?;
            }
            for zp in &zips {
                let _ = std::fs::remove_file(zp);
            }
            if !e.check.is_empty() && !dest.join(e.check).is_file() {
                return Err(format!("解压后缺少 {}（安装包内容不符）", e.check));
            }
            Ok(())
        }
        Kind::Files => {
            for (i, f) in e.files.iter().enumerate() {
                let out = dest.join(f.rel);
                let from = (i as u32 * 100) / n;
                let to = ((i as u32 + 1) * 100) / n;
                download_file(e.id, f.url, &out, from, to)?;
            }
            if e.merge_shards {
                merge_safetensor_shards(&dest)?;
            }
            Ok(())
        }
    }
}

/// 分片权重合并（移植 Python `_merge_safetensor_shards`：两遍流式，峰值一个 4MB 块）。
fn merge_safetensor_shards(model_dir: &Path) -> Result<(), String> {
    let idx_path = model_dir.join("model.safetensors.index.json");
    if !idx_path.is_file() {
        return Ok(());
    }
    let idx: serde_json::Value = serde_json::from_str(
        &std::fs::read_to_string(&idx_path).map_err(|e| e.to_string())?,
    )
    .map_err(|e| e.to_string())?;
    let weight_map = idx
        .get("weight_map")
        .and_then(|x| x.as_object())
        .ok_or("index 缺少 weight_map")?;
    let mut by_shard: std::collections::BTreeMap<String, Vec<String>> =
        std::collections::BTreeMap::new();
    for (tname, shard) in weight_map {
        let shard = shard.as_str().ok_or("shard 名称非法")?;
        by_shard
            .entry(shard.to_string())
            .or_default()
            .push(tname.clone());
    }

    struct Plan {
        shard: String,
        data_start: u64,
        tname: String,
        s0: u64,
        e0: u64,
        dtype: String,
        shape: serde_json::Value,
    }
    let mut plan: Vec<Plan> = Vec::new();
    for (shard, tensors) in &by_shard {
        let mut f = std::fs::File::open(model_dir.join(shard))
            .map_err(|e| format!("打开分片 {shard} 失败：{e}"))?;
        let mut lenb = [0u8; 8];
        f.read_exact(&mut lenb).map_err(|e| e.to_string())?;
        let hlen = u64::from_le_bytes(lenb);
        let mut hbuf = vec![0u8; hlen as usize];
        f.read_exact(&mut hbuf).map_err(|e| e.to_string())?;
        let hdr: serde_json::Value = serde_json::from_slice(&hbuf).map_err(|e| e.to_string())?;
        let mut entries = Vec::new();
        for tname in tensors {
            let meta = hdr.get(tname).ok_or("分片头缺少张量")?;
            let off = meta
                .get("data_offsets")
                .and_then(|x| x.as_array())
                .ok_or("data_offsets 非法")?;
            let s0 = off.first().and_then(|x| x.as_u64()).ok_or("offset 非法")?;
            let e0 = off.get(1).and_then(|x| x.as_u64()).ok_or("offset 非法")?;
            entries.push((
                tname.clone(),
                s0,
                e0,
                meta.get("dtype").and_then(|x| x.as_str()).unwrap_or("").to_string(),
                meta.get("shape").cloned().unwrap_or(serde_json::Value::Null),
            ));
        }
        entries.sort_by_key(|x| x.1);
        for (tname, s0, e0, dtype, shape) in entries {
            plan.push(Plan {
                shard: shard.clone(),
                data_start: 8 + hlen,
                tname,
                s0,
                e0,
                dtype,
                shape,
            });
        }
    }

    let mut out_hdr = serde_json::Map::new();
    let mut off: u64 = 0;
    for p in &plan {
        let n = p.e0 - p.s0;
        out_hdr.insert(
            p.tname.clone(),
            json!({"dtype": p.dtype, "shape": p.shape, "data_offsets": [off, off + n]}),
        );
        off += n;
    }
    let mut hdr_bytes = serde_json::to_vec(&serde_json::Value::Object(out_hdr))
        .map_err(|e| e.to_string())?;
    let pad = (8 - hdr_bytes.len() % 8) % 8;
    hdr_bytes.extend(std::iter::repeat_n(b' ', pad));

    let out_path = model_dir.join("model.safetensors");
    let tmp_path = model_dir.join("model.safetensors.tmp");
    let mut out = std::fs::File::create(&tmp_path).map_err(|e| e.to_string())?;
    out.write_all(&(hdr_bytes.len() as u64).to_le_bytes())
        .map_err(|e| e.to_string())?;
    out.write_all(&hdr_bytes).map_err(|e| e.to_string())?;
    let mut cur_shard = String::new();
    let mut f: Option<std::fs::File> = None;
    for p in &plan {
        if p.shard != cur_shard {
            f = Some(
                std::fs::File::open(model_dir.join(&p.shard))
                    .map_err(|e| format!("打开分片 {} 失败：{e}", p.shard))?,
            );
            cur_shard = p.shard.clone();
        }
        let fh = f.as_mut().unwrap();
        fh.seek(SeekFrom::Start(p.data_start + p.s0))
            .map_err(|e| e.to_string())?;
        let mut remaining = p.e0 - p.s0;
        while remaining > 0 {
            let want = remaining.min(4 * 1024 * 1024) as usize;
            let mut buf = vec![0u8; want];
            fh.read_exact(&mut buf).map_err(|e| {
                format!("分片 {} 数据提前结束（张量 {}）：{e}", p.shard, p.tname)
            })?;
            out.write_all(&buf).map_err(|e| e.to_string())?;
            remaining -= want as u64;
        }
    }
    drop(out);
    std::fs::rename(&tmp_path, &out_path).map_err(|e| e.to_string())?;
    for shard in by_shard.keys() {
        let _ = std::fs::remove_file(model_dir.join(shard));
    }
    let _ = std::fs::remove_file(&idx_path);
    Ok(())
}

/// 下载接口：`POST /api/models/download {"id": "..."}`
pub async fn download_route(
    axum::Json(body): axum::Json<serde_json::Value>,
) -> axum::response::Response {
    use axum::response::IntoResponse;
    let id = body.get("id").and_then(|v| v.as_str()).unwrap_or("");
    if id.is_empty() {
        return axum::Json(json!({"ok": false, "error": "缺少 id"})).into_response();
    }
    match start(id) {
        Ok(()) => axum::Json(json!({"ok": true, "state": "downloading"})).into_response(),
        Err(e) => axum::Json(json!({"ok": false, "error": e})).into_response(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn catalog_has_all_expected_entries() {
        let ids: Vec<&str> = CATALOG.iter().map(|e| e.id).collect();
        for id in [
            "vision-module",
            "llama-vulkan",
            "llama-runtime",
            "audiocpp-runtime",
            "audiocpp-vulkan",
            "audiocpp-cuda",
            "qwen3-asr-0.6b",
            "qwen3-asr-1.7b",
            "sakura-7b",
            "sakura-1.5b",
            "hymt2-7b",
            "hymt2-1.8b",
        ] {
            assert!(ids.contains(&id), "目录缺少 {id}");
        }
    }

    #[test]
    fn dest_paths_are_relative() {
        for e in CATALOG {
            assert!(!e.dest.starts_with('/'), "{} dest 非法", e.id);
            assert!(!e.dest.contains(':'), "{} dest 带盘符", e.id);
        }
    }

    #[test]
    fn model_file_urls_are_direct_downloads() {
        // 模型条目必须是直链，不能是 HF/ModelScope 的仓库首页（旧实现的根因）
        for e in CATALOG.iter().filter(|e| e.kind == Kind::Files) {
            for f in e.files {
                assert!(f.url.contains("/resolve/"), "{} 的 {} 不是直链", e.id, f.rel);
                assert!(!f.url.ends_with(&format!("/{}", e.id)), "{} 指向仓库页", e.id);
            }
        }
    }

    /// 1.7B 分片合并：头偏移/数据顺序/分片删除。
    #[test]
    fn merges_safetensor_shards() {
        use std::io::Write;
        let dir = std::env::temp_dir().join(format!("nexus-merge-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let mk = |name: &str, tensor: &str, data: &[u8]| {
            let hdr = serde_json::json!({
                tensor: {"dtype": "F32", "shape": [2], "data_offsets": [0, data.len()]}
            });
            let mut hb = serde_json::to_vec(&hdr).unwrap();
            while hb.len() % 8 != 0 { hb.push(b' '); }
            let mut f = std::fs::File::create(dir.join(name)).unwrap();
            f.write_all(&(hb.len() as u64).to_le_bytes()).unwrap();
            f.write_all(&hb).unwrap();
            f.write_all(data).unwrap();
        };
        mk("a.safetensors", "t1", &[1, 2, 3, 4, 5, 6, 7, 8]);
        mk("b.safetensors", "t2", &[9, 10, 11, 12, 13, 14, 15, 16]);
        std::fs::write(
            dir.join("model.safetensors.index.json"),
            r#"{"weight_map":{"t1":"a.safetensors","t2":"b.safetensors"}}"#,
        )
        .unwrap();
        merge_safetensor_shards(&dir).unwrap();
        let out = std::fs::read(dir.join("model.safetensors")).unwrap();
        let hlen = u64::from_le_bytes(out[..8].try_into().unwrap()) as usize;
        let hdr: serde_json::Value = serde_json::from_slice(&out[8..8 + hlen]).unwrap();
        assert_eq!(hdr["t1"]["data_offsets"], serde_json::json!([0, 8]));
        assert_eq!(hdr["t2"]["data_offsets"], serde_json::json!([8, 16]));
        assert_eq!(&out[8 + hlen..], &[1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16]);
        assert!(!dir.join("a.safetensors").exists());
        assert!(!dir.join("model.safetensors.index.json").exists());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn start_unknown_id_errors() {
        let e = start("no-such-model");
        assert!(e.is_err());
        assert!(e.unwrap_err().contains("未知"));
    }
}
