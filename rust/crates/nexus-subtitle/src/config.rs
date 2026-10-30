//! 字幕服务配置（对应 Python `vendor/subtitle/config.json`）。
//!
//! 只解析产品实际用到的键；未知键保留在 `raw` 里，方便回写。

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde_json::Value;

/// 翻译后端（对应 Python `translate_engine._BACKEND_ALIAS` 归一后的分支）。
///
/// Python 的 `_chat` 只认 `openai` / `local` 两个分支名，其余一律落到 Ollama；
/// 这里保持同一路由语义：未知后端名按 Ollama 处理。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BackendKind {
    Local,
    OpenAi,
    Ollama,
    None,
}

/// 后端别名归一（UI 历史值 `dashscope` / 旧配置 `cloud` 都表示 OpenAI 兼容接口）。
pub fn normalize_backend_name(raw: &str) -> String {
    match raw.trim().to_lowercase().as_str() {
        "openai" | "cloud" | "dashscope" | "qwen" => "openai".into(),
        "local" | "llamacpp" | "llama" => "local".into(),
        "ollama" => "ollama".into(),
        "none" => "none".into(),
        // 未知值保持原样：Python `_BACKEND_ALIAS.get(raw, raw)` 同款（便于排障）
        _ => raw.trim().to_lowercase(),
    }
}

impl BackendKind {
    /// 配置名 → 分支。未知名走 Ollama（Python `_chat` 的默认分支）。
    pub fn from_name(raw: &str) -> Self {
        match normalize_backend_name(raw).as_str() {
            "local" => Self::Local,
            "openai" => Self::OpenAi,
            "none" => Self::None,
            _ => Self::Ollama,
        }
    }
}

/// 语言主键：`en-US` / `ja_JP` → `en` / `ja`（Python 一律 `split("-")[0]`）。
pub fn lang_key(lang: &str) -> String {
    lang.split(['-', '_'])
        .next()
        .unwrap_or("")
        .trim()
        .to_lowercase()
}

/// `translate.openai` 段（标准 OpenAI 兼容 `/chat/completions`）。
#[derive(Debug, Clone)]
pub struct OpenAiConfig {
    pub base_url: String,
    pub api_key_env: String,
    pub api_key: String,
    pub model: String,
    pub temperature: f64,
    pub max_tokens: u32,
    /// 单次 HTTP 请求超时（秒，<=0 不设上限）；Python 默认 60
    pub timeout_sec: f64,
}

impl Default for OpenAiConfig {
    fn default() -> Self {
        Self {
            base_url: "https://api.openai.com/v1".into(),
            api_key_env: "OPENAI_API_KEY".into(),
            api_key: String::new(),
            model: String::new(),
            temperature: 0.2,
            max_tokens: 8192,
            timeout_sec: 60.0,
        }
    }
}

/// `translate.ollama` 段（`/api/chat`，非流式）。
#[derive(Debug, Clone)]
pub struct OllamaConfig {
    pub base_url: String,
    pub model: String,
    /// Python `_chat_ollama` 的 options.temperature（默认 0.2）
    pub temperature: f64,
    /// Python `_chat_ollama` 的 options.num_predict（默认 2048）
    pub num_predict: u32,
    pub timeout_sec: f64,
}

impl Default for OllamaConfig {
    fn default() -> Self {
        Self {
            base_url: "http://127.0.0.1:11435".into(),
            model: String::new(),
            temperature: 0.2,
            num_predict: 2048,
            timeout_sec: 60.0,
        }
    }
}

/// `server` 段（目前只用到空闲回收与端口展示）。
#[derive(Debug, Clone)]
pub struct ServerConfig {
    pub host: String,
    pub port: u16,
    /// 空闲多少分钟释放模型（0 = 不回收），对应 `server.idle_release_min`
    pub idle_release_min: f64,
    pub auth_token: String,
}

impl Default for ServerConfig {
    fn default() -> Self {
        Self {
            host: "0.0.0.0".into(),
            port: 8756,
            idle_release_min: 5.0,
            auth_token: String::new(),
        }
    }
}

/// `asr.hybrid` 混合切句参数（Python `HybridBuffer` 的构造参数）。
#[derive(Debug, Clone)]
pub struct HybridConfig {
    pub silence_threshold: f64,
    pub silence_tail_sec: f64,
    pub long_silence_tail_sec: f64,
    pub long_phrase_sec: f64,
    pub min_phrase_sec: f64,
    pub max_phrase_sec: f64,
}

impl Default for HybridConfig {
    fn default() -> Self {
        Self {
            // 与 vendor/subtitle/config.json 的生产值一致（不是模块里的 1.0 默认）
            silence_threshold: 0.01,
            silence_tail_sec: 0.7,
            long_silence_tail_sec: 0.4,
            long_phrase_sec: 4.0,
            min_phrase_sec: 2.0,
            max_phrase_sec: 5.0,
        }
    }
}

#[derive(Debug, Clone)]
pub struct AsrConfig {
    /// ASR 后端类型（当前仅 `audiocpp`）
    pub backend: String,
    /// audiocpp 的计算设备：`cpu` / `cuda`
    pub device_backend: String,
    pub port: u16,
    pub threads: u32,
    /// true = 首块音频到达才装载模型；生产默认 false（热启动）
    pub lazy_load: bool,
    pub language: String,
    pub model: String,
    pub min_speech_ms: u32,
    pub merge_gap_sec: f64,
    pub pad_sec: f64,
    /// 混合切句参数（`/transcribe` 块级路径使用）
    pub hybrid: HybridConfig,
    /// 短叹词幻觉门控（R101，默认关）：低语音段循环吐同一短叹词的抑制
    pub hallucination_gate: HallucinationGateCfg,
    /// 行长合并门控（R102，默认关）：不足 target_len 的行扣住与下一块合并
    pub line_merge: LineMergeCfg,
    /// 本地后端的分块档位建议（秒，1..25）；头显据此自适应音频块长
    pub recommended_chunk_sec: Option<u32>,
}

/// 短叹词幻觉门控配置（对齐 Python `asr.hallucination_gate`，默认关）。
#[derive(Debug, Clone)]
pub struct HallucinationGateCfg {
    pub enabled: bool,
    /// 重复判定窗口（秒）
    pub window_s: f64,
    /// 归一化后长度上限：≤ 此长度的文本才参与判重
    pub max_len: usize,
}

impl Default for HallucinationGateCfg {
    fn default() -> Self {
        Self { enabled: false, window_s: 45.0, max_len: 4 }
    }
}

/// 行长合并门控配置（对齐 Python `asr.line_merge`，默认关）。
#[derive(Debug, Clone)]
pub struct LineMergeCfg {
    pub enabled: bool,
    /// 去标点日文字数不足此值的行扣住合并
    pub target_len: usize,
    /// 与上一行的间隙超过此值（毫秒）= 真停顿，放行
    pub gap_ms: i64,
    /// 扣住超过此时长（毫秒）无后续 → 原样放行
    pub max_hold_ms: i64,
    /// 总跨度上限（毫秒）
    pub max_span_ms: i64,
}

impl Default for LineMergeCfg {
    fn default() -> Self {
        Self { enabled: false, target_len: 12, gap_ms: 3000, max_hold_ms: 5000, max_span_ms: 10000 }
    }
}

impl Default for AsrConfig {
    fn default() -> Self {
        Self {
            backend: "audiocpp".into(),
            device_backend: "cpu".into(),
            port: 8081,
            threads: 4,
            lazy_load: false,
            language: "Japanese".into(),
            model: "../../models/Qwen3-ASR-0.6B".into(),
            min_speech_ms: 250,
            merge_gap_sec: 0.5,
            pad_sec: 0.25,
            hybrid: HybridConfig::default(),
            hallucination_gate: HallucinationGateCfg::default(),
            line_merge: LineMergeCfg::default(),
            recommended_chunk_sec: None,
        }
    }
}

/// 把配置里的模型路径解析成绝对路径。
///
/// 相对路径先按配置文件所在目录解析（开发版 `vendor/subtitle`、安装版
/// `var/data` 都能落到 `{app}/models`）；不存在时再按 `models_dir` 兜底。
pub fn resolve_asr_model(raw: &str, base_dir: &Path, models_dir: &Path) -> PathBuf {
    let p = Path::new(raw);
    if p.is_absolute() {
        return p.to_path_buf();
    }
    let from_base = base_dir.join(p);
    if from_base.exists() {
        return from_base;
    }
    match p.file_name() {
        Some(name) => models_dir.join(name),
        None => from_base,
    }
}

#[derive(Debug, Clone)]
pub struct TranslateConfig {
    pub backend: String,
    pub mt_system: String,
    pub mt_user_prefix: String,
    /// 按源语言覆盖 user 前缀（`translate.mt_user_prefix_by_lang`，如 ja/en）
    pub mt_user_prefix_by_lang: BTreeMap<String, String>,
    /// auto / mt / batch（Rust 当前只有逐句 MT 路径，保留解析供状态与后续扩展）
    pub mode: String,
    pub target_lang: String,
    pub batch_size: usize,
    pub thread_num: usize,
    pub openai: OpenAiConfig,
    pub ollama: OllamaConfig,
    pub local_dir: String,
    pub local_exe: String,
    pub local_model: String,
    pub local_alias: String,
    pub local_base_url: String,
    pub local_ctx: u32,
    pub local_ngl: u32,
    /// `translate.local.model_by_lang`：源语言 → 本地 GGUF（相对路径按配置文件目录解析）
    pub local_model_by_lang: BTreeMap<String, String>,
    pub local_start_timeout_sec: f64,
    /// 模型路径解析基准（配置文件所在目录）；由 `SubtitleConfig::load` 注入
    pub base_dir: PathBuf,
}

impl Default for TranslateConfig {
    fn default() -> Self {
        let mut mt_user_prefix_by_lang = BTreeMap::new();
        mt_user_prefix_by_lang.insert("ja".into(), "将下面的日文文本翻译成中文：".into());
        mt_user_prefix_by_lang.insert("en".into(), "将下面的英语文本翻译成中文：".into());
        let mut local_model_by_lang = BTreeMap::new();
        local_model_by_lang.insert("en".into(), "../../models/Hy-MT2-7B-Q4_K_M.gguf".into());
        Self {
            backend: "local".into(),
            mt_system: "你是专业的实时字幕翻译器。把用户输入翻译成简体中文，只输出译文本身。"
                .into(),
            mt_user_prefix: "将下面的日文文本翻译成中文：".into(),
            mt_user_prefix_by_lang,
            mode: "auto".into(),
            target_lang: "zh".into(),
            batch_size: 10,
            thread_num: 4,
            openai: OpenAiConfig::default(),
            ollama: OllamaConfig::default(),
            local_dir: "../../vendor/llama".into(),
            local_exe: "llama-server.exe".into(),
            local_model:
                "../../models/Sakura-7B-Qwen2.5-v1.0/sakura-7b-qwen2.5-v1.0-iq4xs.gguf".into(),
            local_alias: "sakura-7b".into(),
            local_base_url: "http://127.0.0.1:8082".into(),
            local_ctx: 2048,
            local_ngl: 99,
            local_model_by_lang,
            local_start_timeout_sec: 180.0,
            base_dir: PathBuf::new(),
        }
    }
}

impl TranslateConfig {
    /// 真实后端（别名归一后的名字，用于状态展示）。
    pub fn normalized_backend(&self) -> String {
        normalize_backend_name(&self.backend)
    }

    pub fn backend_kind(&self) -> BackendKind {
        BackendKind::from_name(&self.backend)
    }

    pub fn is_local(&self) -> bool {
        self.backend_kind() == BackendKind::Local
    }

    /// 该后端展示/连接用的 base_url（local/openai/ollama 各取各的段）。
    pub fn backend_base_url(&self) -> String {
        match self.backend_kind() {
            BackendKind::OpenAi => self.openai.base_url.trim_end_matches('/').to_string(),
            BackendKind::Ollama => self.ollama.base_url.trim_end_matches('/').to_string(),
            BackendKind::Local => self.local_base_url.trim_end_matches('/').to_string(),
            BackendKind::None => String::new(),
        }
    }

    /// 解析本地模型路径：绝对路径原样，相对路径按配置文件目录解析
    /// （Python `llama_backend._resolve` 的相对基准是 vendor/subtitle）。
    pub fn resolve_local_path(&self, raw: &str) -> PathBuf {
        let p = Path::new(raw);
        if p.is_absolute() || self.base_dir.as_os_str().is_empty() {
            p.to_path_buf()
        } else {
            self.base_dir.join(p)
        }
    }

    /// 语言键对应的本地模型原始引用（映射缺失/空串时回落 `local.model`，
    /// 与 Python `_apply_local_lang_model` 的 `or` 链一致）。
    pub fn model_for_lang(&self, lang: &str) -> String {
        let key = lang_key(lang);
        self.local_model_by_lang
            .get(&key)
            .map(|s| s.trim())
            .filter(|s| !s.is_empty())
            .map(|s| s.to_string())
            .unwrap_or_else(|| self.local_model.clone())
    }

    /// 语言对应的 user 前缀（缺失回落 `mt_user_prefix`）。
    pub fn mt_prefix_for(&self, lang: &str) -> String {
        self.mt_user_prefix_by_lang
            .get(&lang_key(lang))
            .map(|s| s.trim())
            .filter(|s| !s.is_empty())
            .map(|s| s.to_string())
            .unwrap_or_else(|| self.mt_user_prefix.clone())
    }
}

#[derive(Debug, Clone)]
pub struct SegmentConfig {
    pub max_sec: f64,
    pub max_chars: usize,
    pub pause_sec: f64,
    pub min_sec: f64,
    pub min_chars: usize,
}

impl Default for SegmentConfig {
    fn default() -> Self {
        Self {
            max_sec: 8.0,
            max_chars: 50,
            pause_sec: 0.8,
            min_sec: 1.2,
            min_chars: 4,
        }
    }
}

#[derive(Debug, Clone, Default)]
pub struct SubtitleConfig {
    pub asr: AsrConfig,
    pub translate: TranslateConfig,
    pub segment: SegmentConfig,
    pub server: ServerConfig,
    pub raw: Value,
    pub base_dir: PathBuf,
}

fn s(v: &Value, path: &[&str]) -> Option<String> {
    let mut cur = v;
    for p in path {
        cur = cur.get(p)?;
    }
    cur.as_str().map(|x| x.to_string())
}

fn n(v: &Value, path: &[&str]) -> Option<f64> {
    let mut cur = v;
    for p in path {
        cur = cur.get(p)?;
    }
    // Python 侧用 float(...)，配置文件里手写成字符串（"5"）也应生效
    cur.as_f64()
        .or_else(|| cur.as_str().and_then(|s| s.trim().parse::<f64>().ok()))
}

/// 裸数值转换收紧：夹到 [lo, hi]，越界/非有限值打一条中文提示。
/// 直接 `as u16` 会把 99999 截断成 1695 这种看似合法的错端口，必须显式 clamp。
fn clamp_num(v: f64, lo: f64, hi: f64, name: &str) -> f64 {
    if !v.is_finite() || v < lo || v > hi {
        eprintln!("配置 {name}={v} 超出合法区间 [{lo}, {hi}]，已夹紧");
    }
    if v.is_nan() {
        lo
    } else {
        v.clamp(lo, hi)
    }
}

impl SubtitleConfig {
    pub fn load(path: &Path) -> Self {
        let base_dir = path
            .parent()
            .map(|p| p.to_path_buf())
            .unwrap_or_else(|| PathBuf::from("."));
        // 文件不存在=首次运行，静默用默认；文件存在但 JSON 坏必须留痕，
        // 否则用户改坏的配置静默失效（错误传播断链）
        let raw: Value = match std::fs::read_to_string(path) {
            Ok(t) => match serde_json::from_str(&t) {
                Ok(v) => v,
                Err(e) => {
                    eprintln!(
                        "[config] 字幕配置文件解析失败（{}）：{}，已回退全默认配置",
                        path.display(),
                        e
                    );
                    Value::Null
                }
            },
            Err(_) => Value::Null,
        };
        let mut cfg = Self {
            asr: AsrConfig::default(),
            translate: TranslateConfig::default(),
            segment: SegmentConfig::default(),
            server: ServerConfig::default(),
            raw: raw.clone(),
            base_dir: base_dir.clone(),
        };
        cfg.translate.base_dir = base_dir;
        if raw.is_null() {
            return cfg;
        }

        if let Some(v) = s(&raw, &["asr", "backend"]) {
            cfg.asr.backend = v;
        }
        if let Some(v) = s(&raw, &["asr", "audiocpp", "backend"]) {
            cfg.asr.device_backend = v;
        }
        if let Some(v) = n(&raw, &["asr", "audiocpp", "threads"]) {
            cfg.asr.threads = clamp_num(v, 1.0, 256.0, "asr.audiocpp.threads") as u32;
        }
        if let Some(v) = raw
            .get("asr")
            .and_then(|x| x.get("audiocpp"))
            .and_then(|x| x.get("lazy_load"))
            .and_then(|x| x.as_bool())
        {
            cfg.asr.lazy_load = v;
        }
        if let Some(v) = n(&raw, &["asr", "audiocpp", "port"]) {
            cfg.asr.port = clamp_num(v, 1.0, 65535.0, "asr.audiocpp.port") as u16;
        }
        if let Some(v) = s(&raw, &["asr", "audiocpp", "language"]) {
            cfg.asr.language = v;
        }
        if let Some(v) = s(&raw, &["asr", "audiocpp", "model"]) {
            cfg.asr.model = v;
        }
        if let Some(v) = n(&raw, &["asr", "audiocpp", "min_speech_ms"]) {
            cfg.asr.min_speech_ms = clamp_num(v, 0.0, 3_600_000.0, "asr.audiocpp.min_speech_ms") as u32;
        }
        if let Some(v) = n(&raw, &["asr", "recommended_chunk_sec"]) {
            cfg.asr.recommended_chunk_sec = Some((v as u32).clamp(1, 25));
        }
        if let Some(v) = n(&raw, &["asr", "audiocpp", "merge_gap_sec"]) {
            cfg.asr.merge_gap_sec = v;
        }
        if let Some(v) = n(&raw, &["asr", "audiocpp", "pad_sec"]) {
            cfg.asr.pad_sec = v;
        }
        // 混合切句：Python 用 **config 值**构造 HybridBuffer（不是模块默认 1.0）
        if let Some(v) = n(&raw, &["asr", "hybrid", "silence_threshold"]) {
            cfg.asr.hybrid.silence_threshold = v;
        }
        if let Some(v) = n(&raw, &["asr", "hybrid", "silence_tail_sec"]) {
            cfg.asr.hybrid.silence_tail_sec = v;
        }
        if let Some(v) = n(&raw, &["asr", "hybrid", "long_silence_tail_sec"]) {
            cfg.asr.hybrid.long_silence_tail_sec = v;
        }
        if let Some(v) = n(&raw, &["asr", "hybrid", "long_phrase_sec"]) {
            cfg.asr.hybrid.long_phrase_sec = v;
        }
        if let Some(v) = n(&raw, &["asr", "hybrid", "min_phrase_sec"]) {
            cfg.asr.hybrid.min_phrase_sec = v;
        }
        if let Some(v) = n(&raw, &["asr", "hybrid", "max_phrase_sec"]) {
            cfg.asr.hybrid.max_phrase_sec = v;
        }
        // 短叹词幻觉门控（R101）与行长合并门控（R102）：默认关，评测 A/B 注入
        if raw
            .get("asr")
            .and_then(|x| x.get("hallucination_gate"))
            .and_then(|x| x.get("enabled"))
            .and_then(|x| x.as_bool())
            .unwrap_or(false)
        {
            cfg.asr.hallucination_gate.enabled = true;
        }
        if let Some(v) = n(&raw, &["asr", "hallucination_gate", "window_s"]) {
            cfg.asr.hallucination_gate.window_s = clamp_num(v, 1.0, 600.0, "asr.hallucination_gate.window_s");
        }
        if let Some(v) = n(&raw, &["asr", "hallucination_gate", "max_len"]) {
            cfg.asr.hallucination_gate.max_len = (v as usize).max(1);
        }
        if raw
            .get("asr")
            .and_then(|x| x.get("line_merge"))
            .and_then(|x| x.get("enabled"))
            .and_then(|x| x.as_bool())
            .unwrap_or(false)
        {
            cfg.asr.line_merge.enabled = true;
        }
        if let Some(v) = n(&raw, &["asr", "line_merge", "target_len"]) {
            cfg.asr.line_merge.target_len = (v as usize).max(4);
        }
        if let Some(v) = n(&raw, &["asr", "line_merge", "gap_ms"]) {
            cfg.asr.line_merge.gap_ms = clamp_num(v, 500.0, 60_000.0, "asr.line_merge.gap_ms") as i64;
        }
        if let Some(v) = n(&raw, &["asr", "line_merge", "max_hold_ms"]) {
            cfg.asr.line_merge.max_hold_ms = clamp_num(v, 1500.0, 60_000.0, "asr.line_merge.max_hold_ms") as i64;
        }
        if let Some(v) = n(&raw, &["asr", "line_merge", "max_span_ms"]) {
            cfg.asr.line_merge.max_span_ms = clamp_num(v, 1500.0, 120_000.0, "asr.line_merge.max_span_ms") as i64;
        }

        if let Some(v) = s(&raw, &["translate", "backend"]) {
            cfg.translate.backend = v;
        }
        if let Some(v) = s(&raw, &["translate", "mode"]) {
            cfg.translate.mode = v;
        }
        if let Some(v) = s(&raw, &["translate", "mt_system"]) {
            cfg.translate.mt_system = v;
        }
        if let Some(v) = s(&raw, &["translate", "mt_user_prefix"]) {
            cfg.translate.mt_user_prefix = v;
        }
        if let Some(v) = s(&raw, &["translate", "target_lang"]) {
            cfg.translate.target_lang = v;
        }
        if let Some(v) = n(&raw, &["translate", "batch_size"]) {
            cfg.translate.batch_size = clamp_num(v, 1.0, 1000.0, "translate.batch_size") as usize;
        }
        if let Some(v) = n(&raw, &["translate", "thread_num"]) {
            // 与 batch_size 同款夹紧：负数/超大值不再符号扩展或饱和成异常值
            cfg.translate.thread_num = clamp_num(v, 1.0, 64.0, "translate.thread_num") as usize;
        }
        if let Some(v) = s(&raw, &["translate", "local", "dir"]) {
            cfg.translate.local_dir = v;
        }
        if let Some(v) = s(&raw, &["translate", "local", "exe"]) {
            cfg.translate.local_exe = v;
        }
        if let Some(v) = s(&raw, &["translate", "local", "model"]) {
            cfg.translate.local_model = v;
        }
        if let Some(v) = s(&raw, &["translate", "local", "alias"]) {
            cfg.translate.local_alias = v;
        }
        if let Some(v) = s(&raw, &["translate", "local", "base_url"]) {
            cfg.translate.local_base_url = v;
        }
        if let Some(v) = n(&raw, &["translate", "local", "ctx"]) {
            cfg.translate.local_ctx = clamp_num(v, 1.0, 131_072.0, "translate.local.ctx") as u32;
        }
        if let Some(v) = n(&raw, &["translate", "local", "ngl"]) {
            cfg.translate.local_ngl = clamp_num(v, 0.0, 999.0, "translate.local.ngl") as u32;
        }
        if let Some(key) = raw.get("translate") {
            if let Some(v) = key
                .get("local")
                .and_then(|x| x.get("model_by_lang"))
                .and_then(|x| x.as_object())
            {
                cfg.translate.local_model_by_lang = v
                    .iter()
                    .filter_map(|(k, val)| {
                        val.as_str()
                            .map(|s| (lang_key(k), s.trim().to_string()))
                    })
                    .filter(|(k, v)| !k.is_empty() && !v.is_empty())
                    .collect();
            }
            if let Some(v) = key.get("mt_user_prefix_by_lang").and_then(|x| x.as_object()) {
                cfg.translate.mt_user_prefix_by_lang = v
                    .iter()
                    .filter_map(|(k, val)| {
                        val.as_str()
                            .map(|s| (lang_key(k), s.trim().to_string()))
                    })
                    .filter(|(k, v)| !k.is_empty() && !v.is_empty())
                    .collect();
            }
        }
        if let Some(v) = n(&raw, &["translate", "local", "start_timeout_sec"]) {
            cfg.translate.local_start_timeout_sec = clamp_num(
                v,
                1.0,
                3600.0,
                "translate.local.start_timeout_sec",
            );
        }

        // ---- translate.openai（标准 OpenAI 兼容接口） ----
        if let Some(v) = s(&raw, &["translate", "openai", "base_url"]) {
            cfg.translate.openai.base_url = v;
        }
        if let Some(v) = s(&raw, &["translate", "openai", "api_key_env"]) {
            cfg.translate.openai.api_key_env = v;
        }
        if let Some(v) = s(&raw, &["translate", "openai", "api_key"]) {
            cfg.translate.openai.api_key = v;
        }
        if let Some(v) = s(&raw, &["translate", "openai", "model"]) {
            cfg.translate.openai.model = v;
        }
        if let Some(v) = n(&raw, &["translate", "openai", "temperature"]) {
            cfg.translate.openai.temperature =
                clamp_num(v, 0.0, 2.0, "translate.openai.temperature");
        }
        if let Some(v) = n(&raw, &["translate", "openai", "max_tokens"]) {
            cfg.translate.openai.max_tokens =
                clamp_num(v, 1.0, 1_000_000.0, "translate.openai.max_tokens") as u32;
        }
        if let Some(v) = n(&raw, &["translate", "openai", "timeout_sec"]) {
            cfg.translate.openai.timeout_sec =
                clamp_num(v, 0.0, 3600.0, "translate.openai.timeout_sec");
        }

        // ---- translate.ollama（/api/chat） ----
        if let Some(v) = s(&raw, &["translate", "ollama", "base_url"]) {
            cfg.translate.ollama.base_url = v;
        }
        if let Some(v) = s(&raw, &["translate", "ollama", "model"]) {
            cfg.translate.ollama.model = v;
        }
        if let Some(v) = n(&raw, &["translate", "ollama", "temperature"]) {
            cfg.translate.ollama.temperature =
                clamp_num(v, 0.0, 2.0, "translate.ollama.temperature");
        }
        if let Some(v) = n(&raw, &["translate", "ollama", "num_predict"]) {
            cfg.translate.ollama.num_predict =
                clamp_num(v, 1.0, 1_000_000.0, "translate.ollama.num_predict") as u32;
        }
        if let Some(v) = n(&raw, &["translate", "ollama", "timeout_sec"]) {
            cfg.translate.ollama.timeout_sec =
                clamp_num(v, 0.0, 3600.0, "translate.ollama.timeout_sec");
        }

        // ---- server（空闲回收） ----
        if let Some(v) = s(&raw, &["server", "host"]) {
            cfg.server.host = v;
        }
        if let Some(v) = n(&raw, &["server", "port"]) {
            cfg.server.port = clamp_num(v, 1.0, 65535.0, "server.port") as u16;
        }
        if let Some(v) = n(&raw, &["server", "idle_release_min"]) {
            // 0（或负数，Python 语义）都表示"不自动回收"
            cfg.server.idle_release_min =
                clamp_num(v, 0.0, 7.0 * 24.0 * 60.0, "server.idle_release_min");
        }
        if let Some(v) = s(&raw, &["server", "auth_token"]) {
            cfg.server.auth_token = v;
        }

        if let Some(v) = n(&raw, &["segment", "max_sec"]) {
            cfg.segment.max_sec = v;
        }
        if let Some(v) = n(&raw, &["segment", "max_chars"]) {
            cfg.segment.max_chars = clamp_num(v, 1.0, 1000.0, "segment.max_chars") as usize;
        }
        if let Some(v) = n(&raw, &["segment", "pause_sec"]) {
            cfg.segment.pause_sec = v;
        }
        if let Some(v) = n(&raw, &["segment", "min_sec"]) {
            cfg.segment.min_sec = v;
        }
        if let Some(v) = n(&raw, &["segment", "min_chars"]) {
            cfg.segment.min_chars = clamp_num(v, 0.0, 1000.0, "segment.min_chars") as usize;
        }

        // Rust 只支持 audio.cpp；旧配置里的 whisper/lccapture 等后端名统一迁移。
        if cfg.asr.backend.trim() != "audiocpp" {
            let old = cfg.asr.backend.clone();
            cfg.asr.backend = "audiocpp".to_string();
            if let Some(asr) = cfg.raw.get_mut("asr").and_then(|v| v.as_object_mut()) {
                asr.insert(
                    "backend".into(),
                    serde_json::Value::String("audiocpp".into()),
                );
            }
            eprintln!("旧配置 asr.backend={old} 已移除，回退到 audiocpp");
        }

        // Win11 实时字幕已移除：旧配置里的 lccapture 迁移到默认 Qwen3 模型。
        // 否则新版本会把 "lccapture" 当模型路径去启动 audiocpp，界面永远停在加载中。
        if cfg.asr.model.trim() == "lccapture" {
            let fallback = "../../models/Qwen3-ASR-0.6B";
            cfg.asr.model = fallback.to_string();
            if cfg.asr.backend.trim() == "lccapture" {
                cfg.asr.backend = "audiocpp".to_string();
            }
            if let Some(asr) = cfg.raw.get_mut("asr").and_then(|v| v.as_object_mut()) {
                if let Some(ac) = asr.get_mut("audiocpp").and_then(|v| v.as_object_mut()) {
                    ac.insert(
                        "model".into(),
                        serde_json::Value::String(fallback.to_string()),
                    );
                }
                if asr.get("backend").and_then(|v| v.as_str()) == Some("lccapture") {
                    asr.insert("backend".into(), serde_json::Value::String("audiocpp".into()));
                }
            }
            eprintln!("旧配置 asr.model=lccapture 已移除，回退到 {fallback}");
        }
        cfg
    }

    pub fn audiocpp_url(&self) -> String {
        format!("http://127.0.0.1:{}", self.asr.port)
    }

    pub fn llama_dir(&self) -> PathBuf {
        self.base_dir.join(&self.translate.local_dir)
    }

    pub fn llama_model(&self) -> PathBuf {
        self.base_dir.join(&self.translate.local_model)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    /// 旧 Python 配置里的 whisper/lccapture 后端名统一迁移到 audiocpp。
    #[test]
    fn legacy_asr_backend_is_normalized() {
        let dir = std::env::temp_dir().join(format!("nexus-cfg-be-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("config.json");
        std::fs::write(&p, r#"{"asr":{"backend":"whisper"}}"#).unwrap();
        let c = SubtitleConfig::load(&p);
        assert_eq!(c.asr.backend, "audiocpp");
        assert_eq!(c.raw["asr"]["backend"], "audiocpp");
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// Win11 实时字幕已移除；旧配置必须迁移，否则新版本拿 "lccapture" 当路径启动 ASR。
    #[test]
    fn lccapture_config_falls_back_to_default_asr() {
        let dir = std::env::temp_dir().join(format!("nexus-cfg-lc-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("config.json");
        std::fs::write(&p, r#"{"asr":{"audiocpp":{"model":"lccapture"}}}"#).unwrap();
        let c = SubtitleConfig::load(&p);
        assert_eq!(c.asr.model, "../../models/Qwen3-ASR-0.6B");
        assert_eq!(
            c.raw["asr"]["audiocpp"]["model"],
            "../../models/Qwen3-ASR-0.6B"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn defaults_match_python_config() {
        let c = SubtitleConfig::load(Path::new("Z:/nope.json"));
        assert_eq!(c.asr.port, 8081);
        assert_eq!(c.asr.language, "Japanese");
        assert_eq!(c.translate.batch_size, 10);
        assert_eq!(c.translate.thread_num, 4);
        assert_eq!(c.translate.local_base_url, "http://127.0.0.1:8082");
        assert!(c.translate.mt_system.contains("实时字幕翻译器"));
        assert!(c.translate.mt_user_prefix.contains("翻译成中文"));
        assert_eq!(c.segment.max_chars, 50);

        // 与 vendor/subtitle/config.json 一致的发运默认值
        assert_eq!(c.translate.backend, "local");
        assert_eq!(c.translate.openai.base_url, "https://api.openai.com/v1");
        assert_eq!(c.translate.openai.api_key_env, "OPENAI_API_KEY");
        assert_eq!(c.translate.openai.temperature, 0.2);
        assert_eq!(c.translate.openai.max_tokens, 8192);
        assert_eq!(c.translate.ollama.base_url, "http://127.0.0.1:11435");
        assert_eq!(c.translate.ollama.model, "");
        assert_eq!(
            c.translate.local_model_by_lang.get("en").map(String::as_str),
            Some("../../models/Hy-MT2-7B-Q4_K_M.gguf")
        );
        assert_eq!(c.translate.local_start_timeout_sec, 180.0);
        assert_eq!(c.server.idle_release_min, 5.0);
        assert_eq!(
            c.translate.mt_user_prefix_by_lang.get("en").map(String::as_str),
            Some("将下面的英语文本翻译成中文：")
        );
        assert_eq!(c.translate.backend_kind(), BackendKind::Local);
    }

    #[test]
    fn parses_backends_openai_ollama_and_none() {
        let dir = std::env::temp_dir().join(format!("nexus-sub-cfg-be-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("config.json");
        std::fs::write(
            &p,
            json!({
                "translate": {
                    "backend": "cloud",
                    "mode": "batch",
                    "target_lang": "en",
                    "openai": {
                        "base_url": "http://127.0.0.1:4010/v1/",
                        "api_key_env": "MY_KEY",
                        "api_key": "sk-test",
                        "model": "gpt-test",
                        "temperature": 0.7,
                        "max_tokens": 1234
                    },
                    "ollama": {
                        "base_url": "http://127.0.0.1:11434/",
                        "model": "qwen2.5:3b",
                        "temperature": 0.4,
                        "num_predict": 512
                    },
                    "local": {
                        "model_by_lang": {"en": "E:/models/en.gguf", "ja": ""},
                        "start_timeout_sec": 30
                    }
                },
                "server": {"idle_release_min": 0}
            })
            .to_string(),
        )
        .unwrap();
        let c = SubtitleConfig::load(&p);
        assert_eq!(c.translate.normalized_backend(), "openai");
        assert_eq!(c.translate.backend_kind(), BackendKind::OpenAi);
        assert_eq!(c.translate.mode, "batch");
        assert_eq!(c.translate.openai.base_url, "http://127.0.0.1:4010/v1/");
        assert_eq!(c.translate.openai.api_key_env, "MY_KEY");
        assert_eq!(c.translate.openai.api_key, "sk-test");
        assert_eq!(c.translate.openai.model, "gpt-test");
        assert_eq!(c.translate.openai.temperature, 0.7);
        assert_eq!(c.translate.openai.max_tokens, 1234);
        assert_eq!(c.translate.ollama.base_url, "http://127.0.0.1:11434/");
        assert_eq!(c.translate.ollama.model, "qwen2.5:3b");
        assert_eq!(c.translate.ollama.temperature, 0.4);
        assert_eq!(c.translate.ollama.num_predict, 512);
        assert_eq!(c.translate.local_start_timeout_sec, 30.0);
        // 空串映射按"没配"丢弃（Python 的 or 链同款）
        assert_eq!(c.translate.model_for_lang("en"), "E:/models/en.gguf");
        assert_eq!(c.translate.model_for_lang("ja"), c.translate.local_model);
        assert_eq!(c.server.idle_release_min, 0.0);

        let mut none_cfg = c.clone();
        none_cfg.translate.backend = "none".into();
        assert_eq!(none_cfg.translate.backend_kind(), BackendKind::None);
        assert_eq!(c.translate.backend_base_url(), "http://127.0.0.1:4010/v1");
    }

    #[test]
    fn backend_alias_routing_matches_python() {
        for (raw, want) in [
            ("local", BackendKind::Local),
            ("llamacpp", BackendKind::Local),
            ("llama", BackendKind::Local),
            ("openai", BackendKind::OpenAi),
            ("cloud", BackendKind::OpenAi),
            ("dashscope", BackendKind::OpenAi),
            ("qwen", BackendKind::OpenAi),
            ("ollama", BackendKind::Ollama),
            ("none", BackendKind::None),
            // 未知名走 Ollama：Python `_chat` 的默认分支
            ("whatever", BackendKind::Ollama),
        ] {
            assert_eq!(BackendKind::from_name(raw), want, "后端 {raw}");
        }
        assert_eq!(normalize_backend_name("DashScope"), "openai");
    }

    #[test]
    fn english_model_and_prefix_selection() {
        let mut c = TranslateConfig::default();
        c.base_dir = PathBuf::from("/base");
        assert_eq!(c.model_for_lang("en"), "../../models/Hy-MT2-7B-Q4_K_M.gguf");
        assert_eq!(c.model_for_lang("en-US"), "../../models/Hy-MT2-7B-Q4_K_M.gguf");
        assert_eq!(c.model_for_lang("ja"), c.local_model);
        assert_eq!(c.model_for_lang("ko"), c.local_model);
        assert_eq!(
            c.resolve_local_path(&c.model_for_lang("en")),
            PathBuf::from("/base/../../models/Hy-MT2-7B-Q4_K_M.gguf")
        );
        assert_eq!(c.mt_prefix_for("en"), "将下面的英语文本翻译成中文：");
        assert_eq!(c.mt_prefix_for("ja"), "将下面的日文文本翻译成中文：");
        assert_eq!(c.mt_prefix_for("ko"), c.mt_user_prefix);

        c.local_model_by_lang.clear();
        assert_eq!(c.model_for_lang("en"), c.local_model, "映射缺失回落默认模型");
    }

    #[test]
    fn loads_from_json_file() {
        let dir = std::env::temp_dir().join(format!("nexus-sub-cfg-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("config.json");
        std::fs::write(
            &p,
            json!({
                "asr": {"backend": "audiocpp", "audiocpp": {"port": 9091, "language": "English"}},
                "translate": {"batch_size": 7, "local": {"base_url": "http://127.0.0.1:9999"}},
                "segment": {"max_sec": 6.5}
            })
            .to_string(),
        )
        .unwrap();
        let c = SubtitleConfig::load(&p);
        assert_eq!(c.asr.port, 9091);
        assert_eq!(c.asr.language, "English");
        assert_eq!(c.translate.batch_size, 7);
        assert_eq!(c.translate.local_base_url, "http://127.0.0.1:9999");
        assert_eq!(c.segment.max_sec, 6.5);
        assert_eq!(c.translate.thread_num, 4, "没写的键保留默认");
        assert_eq!(c.segment.max_chars, 50);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn urls_and_paths() {
        let mut c = SubtitleConfig::load(Path::new("Z:/nope.json"));
        c.base_dir = PathBuf::from("/base");
        assert_eq!(c.audiocpp_url(), "http://127.0.0.1:8081");
        assert!(c.llama_dir().to_string_lossy().contains("vendor"));
        assert!(c.llama_model().to_string_lossy().ends_with(".gguf"));
    }

    #[test]
    fn resolves_asr_model_from_base_or_models_dir() {
        let root = std::env::temp_dir().join(format!("nexus-asr-model-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let base = root.join("vendor/subtitle");
        let models = root.join("models");
        std::fs::create_dir_all(&base).unwrap();
        std::fs::create_dir_all(models.join("Qwen3-ASR-0.6B")).unwrap();

        let via_base = resolve_asr_model("../../models/Qwen3-ASR-0.6B", &base, &models);
        assert_eq!(
            via_base,
            base.join("../../models/Qwen3-ASR-0.6B"),
            "相对路径优先按配置目录解析"
        );
        assert_eq!(
            resolve_asr_model("E:/m/X", &base, &models),
            PathBuf::from("E:/m/X"),
            "绝对路径原样保留"
        );
        let fallback = resolve_asr_model("../../nowhere/Qwen3-ASR-0.6B", &base, &models);
        assert_eq!(
            fallback,
            models.join("Qwen3-ASR-0.6B"),
            "配置目录下找不到时按 models_dir 兜底"
        );
        let _ = std::fs::remove_dir_all(&root);
    }
}
