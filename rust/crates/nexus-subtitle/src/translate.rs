//! 翻译调度：按 `translate.backend` 路由到本地 llama.cpp / OpenAI 兼容 / Ollama，
//! 并保持"批量、顺序、失败隔离"的语义。
//!
//! 对应 Python `translate_engine` 的 MT 逐句路径（生产默认：
//! `backend=local` + `mt_system` 非空 ⇒ 逐句，不是批量 JSON）：
//! - `local`  → llama-server `/v1/chat/completions`（`LlamaBackend`）
//! - `openai` → 标准 OpenAI 兼容 `/chat/completions`（Bearer key）
//! - `ollama` → `/api/chat`（`stream=false` + `options`，对应 Python `_chat_ollama`）
//! - `none`   → 直接返回空译文（用户显式"关闭翻译"，不发起任何请求）
//!
//! `local.model_by_lang`：英语源/英语目标时用 `en` 模型；切换模型 = 停旧 llama-server
//! 再用新 `-m` 拉起（对应 Python `llama_backend.use_model`）。

use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use tokio::sync::Semaphore;

use crate::config::{lang_key, BackendKind, TranslateConfig};
use crate::llama::LlamaBackend;

/// Ollama 空模型名的默认值（Python `_OLLAMA_DEFAULT_MODEL`）。
///
/// 发运配置里 `"model": ""` 这个键**存在**，所以不能靠 `get(...).unwrap_or(default)`
/// —— 空串会被原样发进请求体，Ollama 报 model not found。
pub const OLLAMA_DEFAULT_MODEL: &str = "qwen2.5:3b";

/// OpenAI 兼容接口的默认密钥环境变量（配置缺 `api_key_env` 时用）。
const OPENAI_DEFAULT_KEY_ENV: &str = "OPENAI_API_KEY";

/// 熔断参数：连续 N 批全失败 → 冷却期快速失败（Python F21/F22 的
/// CircuitOpen/半开冷却）。冷却期满自动放行（半开），再失败再熔断。
const CIRCUIT_TRIP_BATCHES: u32 = 3;
const CIRCUIT_COOLDOWN: Duration = Duration::from_secs(60);

/// OpenAI 兼容服务的容差修正（对齐 Python `_openai_compat`"学到就记住"）：
/// 有的服务不收 system 角色 / 不收 temperature / 只认 max_completion_tokens。
#[derive(Debug, Default, Clone, Copy, PartialEq)]
struct OpenAiCompat {
    fold_system: bool,
    drop_temperature: bool,
    comp_tokens: bool,
}

#[derive(Debug, Clone, PartialEq)]
pub struct TranslateResult {
    pub text: String,
    pub translation: String,
    pub error: String,
}

#[derive(Clone)]
pub struct TranslateEngine {
    cfg: TranslateConfig,
    backend: Arc<LlamaBackend>,
    client: reqwest::Client,
    /// 当前选定的本地模型（None = 尚未确定/沿用配置默认）。
    /// 用 std Mutex：只在取/放快照时短暂持锁，绝不跨 await。
    local_model: Arc<Mutex<Option<PathBuf>>>,
    /// 本地模型"选择 + 拉起"串行化：并发批量翻译冷启动时不能同时 spawn 多个
    /// llama-server（会互相杀进程）。openai/ollama 不走这把锁。
    local_setup: Arc<tokio::sync::Mutex<()>>,
    /// llama-server 进程代数：每次切换/重拉 +1。并发请求里"切换掐断了在飞请求"
    /// 靠代数变化识别并对新模型重发一次（Python F23）。
    model_epoch: Arc<AtomicU64>,
    /// 本地翻译是否已热（真实 prefill / 首次成功翻译过后）。切换模型即失效。
    warmed: Arc<Mutex<bool>>,
    /// OpenAI 兼容容差（学到就记住，进程内生效）。
    openai_compat: Arc<Mutex<OpenAiCompat>>,
    /// 连续全失败批数（熔断计数）。
    fail_streak: Arc<Mutex<u32>>,
    /// 熔断到期时刻：Some(t) 且未到期 = 冷却中，直接快速失败。
    circuit_until: Arc<Mutex<Option<Instant>>>,
}

impl TranslateEngine {
    pub fn new(cfg: TranslateConfig, backend: Arc<LlamaBackend>) -> Self {
        let client = reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(10))
            .build()
            .unwrap_or_default();
        Self {
            cfg,
            backend,
            client,
            local_model: Arc::new(Mutex::new(None)),
            local_setup: Arc::new(tokio::sync::Mutex::new(())),
            model_epoch: Arc::new(AtomicU64::new(0)),
            warmed: Arc::new(Mutex::new(false)),
            openai_compat: Arc::new(Mutex::new(OpenAiCompat::default())),
            fail_streak: Arc::new(Mutex::new(0)),
            circuit_until: Arc::new(Mutex::new(None)),
        }
    }

    // ------------------------------------------------------------ 后端路由

    pub fn backend_kind(&self) -> BackendKind {
        self.cfg.backend_kind()
    }

    /// 真实后端名（别名归一，状态 JSON 用）。
    pub fn backend_name(&self) -> String {
        self.cfg.normalized_backend()
    }

    /// 当前后端的模型标识（状态 JSON 用；local 优先显示实际选定的 GGUF）。
    pub fn model_name(&self) -> String {
        match self.backend_kind() {
            BackendKind::Local => self
                .current_model_path()
                .map(|p| p.to_string_lossy().to_string())
                .unwrap_or_else(|| self.cfg.local_model.clone()),
            BackendKind::OpenAi => self.cfg.openai.model.clone(),
            BackendKind::Ollama => {
                let m = self.cfg.ollama.model.trim();
                if m.is_empty() {
                    OLLAMA_DEFAULT_MODEL.to_string()
                } else {
                    m.to_string()
                }
            }
            BackendKind::None => String::new(),
        }
    }

    /// 状态/诊断展示用的一句话（对应 Python `Translator.describe()` 的精简版）。
    pub fn describe(&self) -> String {
        if self.backend_kind() == BackendKind::None {
            return "已关闭（translate.backend = none，不发起任何 LLM 请求）".to_string();
        }
        format!(
            "{}/{} batch={} threads={} mode=mt逐句",
            self.backend_name(),
            self.model_name(),
            self.cfg.batch_size,
            self.cfg.thread_num
        )
    }

    /// 该后端连接用的 base_url（local/openai/ollama 各取各的配置段）。
    pub fn base_url(&self) -> String {
        self.cfg.backend_base_url()
    }

    pub fn target_lang(&self) -> &str {
        &self.cfg.target_lang
    }

    /// 翻译是否可用：`backend=none` ⇒ false（用户显式关闭，不留 error）。
    pub fn mt_enabled(&self) -> bool {
        self.backend_kind() != BackendKind::None && !self.cfg.mt_system.trim().is_empty()
    }

    /// 英语源/英语目标时用 `local.model_by_lang.en`，其余按源语言主键。
    fn model_lang_key(&self, lang: &str) -> String {
        let src = lang_key(lang);
        let tgt = lang_key(&self.cfg.target_lang);
        if src == "en" || tgt == "en" {
            "en".to_string()
        } else if src.is_empty() {
            tgt
        } else {
            src
        }
    }

    /// 某次请求会选用的本地模型**原始引用**（测试与状态排查用）。
    pub fn model_for_request_lang(&self, lang: &str) -> String {
        self.cfg.model_for_lang(&self.model_lang_key(lang))
    }

    /// 当前实际选定的本地模型路径（未确定时为 None）。
    pub fn current_model_path(&self) -> Option<PathBuf> {
        self.local_model.lock().ok().and_then(|g| g.clone())
    }

    /// 记录"宿主刚用哪个模型拉起了 llama-server"。
    ///
    /// 宿主 `run_start` 可能先于第一次翻译请求启动默认模型；不同步这条状态的话，
    /// 首个英语请求会以为"当前就是 en 模型"而跳过切换（用的是日文模型）。
    pub fn set_current_model(&self, model: Option<PathBuf>) {
        if let Ok(mut g) = self.local_model.lock() {
            *g = model;
        }
    }

    // ------------------------------------------------------------ 主入口

    /// 单句翻译（兼容旧调用：无语言上下文，按默认前缀/默认模型）。
    pub async fn translate(&self, text: &str) -> Result<String, String> {
        self.translate_for(text, "").await
    }

    /// 单句翻译，带语言键（`ja`/`en`…）：决定前缀与本地模型。
    pub async fn translate_for(&self, text: &str, lang: &str) -> Result<String, String> {
        let t = text.trim();
        if t.is_empty() {
            return Ok(String::new());
        }
        // none：UI 的「关闭翻译」必须真正短路（一个请求都不发）
        if self.backend_kind() == BackendKind::None {
            return Ok(String::new());
        }
        let user = format!("{}{}", self.cfg.mt_prefix_for(lang), t);
        self.chat(&self.cfg.mt_system, &user, lang).await
    }

    /// 批量翻译（兼容旧调用）。
    pub async fn translate_batch(&self, texts: &[String]) -> Vec<TranslateResult> {
        self.translate_batch_for(texts, "").await
    }

    /// 批量翻译：**保持输入顺序**，并发上限 = thread_num。
    ///
    /// 单句失败只让该句 `error` 有值、译文留空，整批继续 ——
    /// 对应 Python 里"逐条兜底"的语义：一条烂句子不该让整段字幕消失。
    pub async fn translate_batch_for(
        &self,
        texts: &[String],
        lang: &str,
    ) -> Vec<TranslateResult> {
        // none：直接空译文，不发任何请求，也不留 error（不是失败）
        if self.backend_kind() == BackendKind::None {
            return texts
                .iter()
                .map(|t| TranslateResult {
                    text: t.clone(),
                    translation: String::new(),
                    error: String::new(),
                })
                .collect();
        }

        // 熔断冷却中：直接快速失败，不让每句都白等完整超时
        // （后端挂起时 openai/ollama 60s、local 180s，批量会拖到分钟级）
        if texts
            .iter()
            .any(|t| !t.trim().is_empty())
            && self.circuit_open().is_some()
        {
            let left = self.circuit_open().unwrap_or_default().as_secs();
            let msg = format!(
                "翻译后端连续 {} 批失败，冷却中（约 {}s 后自动重试）",
                CIRCUIT_TRIP_BATCHES, left
            );
            return texts
                .iter()
                .map(|t| TranslateResult {
                    text: t.clone(),
                    translation: String::new(),
                    error: msg.clone(),
                })
                .collect();
        }

        let sem = Arc::new(Semaphore::new(self.cfg.thread_num.max(1)));
        let mut handles = Vec::with_capacity(texts.len());
        for text in texts {
            let me_text = text.clone();
            let sem = Arc::clone(&sem);
            let eng = self.clone();
            let lang = lang.to_string();
            handles.push(tokio::spawn(async move {
                let _permit = sem.acquire().await;
                let trimmed = me_text.trim();
                if trimmed.is_empty() {
                    return TranslateResult {
                        text: me_text,
                        translation: String::new(),
                        error: String::new(),
                    };
                }
                match eng.translate_for(trimmed, &lang).await {
                    Ok(tr) => TranslateResult {
                        text: me_text,
                        translation: tr,
                        error: String::new(),
                    },
                    Err(e) => TranslateResult {
                        text: me_text,
                        translation: String::new(),
                        error: e,
                    },
                }
            }));
        }
        let mut out = Vec::with_capacity(handles.len());
        for h in handles {
            match h.await {
                Ok(r) => out.push(r),
                Err(e) => out.push(TranslateResult {
                    text: String::new(),
                    translation: String::new(),
                    error: format!("翻译任务异常：{e}"),
                }),
            }
        }
        self.update_circuit(&out);
        out
    }

    /// 熔断剩余冷却时间；None = 未熔断（或冷却已过、放行重试）。
    fn circuit_open(&self) -> Option<Duration> {
        let g = self.circuit_until.lock().ok()?;
        g.map(|t| t.saturating_duration_since(Instant::now()))
            .filter(|d| !d.is_zero())
    }

    /// 批后统计：全失败批累加计数、连续 N 批触发熔断；有任何成功就复位。
    fn update_circuit(&self, out: &[TranslateResult]) {
        // 空句在批量任务里恒返回 error 为空的"成功"结果，混进 all() 会把
        // 后端全失败也判成"有成功"、熔断永不触发——失败判据只看非空句
        let non_empty: Vec<&TranslateResult> = out
            .iter()
            .filter(|r| !r.text.trim().is_empty())
            .collect();
        if non_empty.is_empty() {
            return; // 全是空句的批次不算失败证据
        }
        let all_failed = non_empty.iter().all(|r| !r.error.is_empty());
        let mut streak = match self.fail_streak.lock() {
            Ok(g) => g,
            Err(_) => return,
        };
        if all_failed {
            *streak += 1;
            if *streak >= CIRCUIT_TRIP_BATCHES {
                if let Ok(mut c) = self.circuit_until.lock() {
                    *c = Some(Instant::now() + CIRCUIT_COOLDOWN);
                }
                eprintln!(
                    "[translate] 后端连续 {} 批全失败，熔断 {}s（期间快速失败，不白等超时）",
                    *streak,
                    CIRCUIT_COOLDOWN.as_secs()
                );
            }
        } else {
            *streak = 0;
            if let Ok(mut c) = self.circuit_until.lock() {
                *c = None;
            }
        }
    }

    // ------------------------------------------------------------ 后端调用

    async fn chat(&self, system: &str, user: &str, lang: &str) -> Result<String, String> {
        match self.backend_kind() {
            BackendKind::None => Ok(String::new()),
            BackendKind::Local => {
                let epoch0 = self.model_epoch.load(Ordering::SeqCst);
                self.ensure_local_model(lang).await?;
                match self.backend.translate_one(system, user).await {
                    Ok(t) => {
                        if let Ok(mut g) = self.warmed.lock() {
                            *g = true;
                        }
                        Ok(t)
                    }
                    Err(e) => {
                        // 并发批量里别的请求切换了模型，会掐断本请求的在飞连接：
                        // 代数变了说明不是这句的问题，对新模型重发一次（Python F23）
                        if self.model_epoch.load(Ordering::SeqCst) != epoch0 {
                            self.backend.translate_one(system, user).await
                                .map_err(|e2| format!("{e}；切换模型后重发仍失败：{e2}"))
                        } else {
                            Err(e)
                        }
                    }
                }
            }
            BackendKind::OpenAi => self.chat_openai(system, user).await,
            BackendKind::Ollama => self.chat_ollama(system, user).await,
        }
    }

    /// 本地模型选择 + 拉起（对应 Python `_apply_local_lang_model` + `use_model`）。
    ///
    /// 幂等：目标与当前一致时直接复用（`ensure_server` 先探活）；
    /// 切换先停旧进程再用新 `-m` 拉起。目标文件不存在时**拒绝切换**、沿用当前模型
    /// （Python 的 R68/R103 修复：不能先把好模型停掉再换上起不来的路径）。
    async fn ensure_local_model(&self, lang: &str) -> Result<(), String> {
        let _setup = self.local_setup.lock().await;
        let target = self
            .cfg
            .resolve_local_path(&self.cfg.model_for_lang(&self.model_lang_key(lang)));
        let current = self.current_model_path();
        let mut switched = false;
        let chosen = match current.clone() {
            Some(cur) if cur == target => cur,
            Some(cur) => {
                // 换模型必须重启 llama-server（权重随进程走）。
                // 停旧之前先验证新运行时可用：若 exe 缺失/损坏，旧好模型已被杀
                // 且 ensure_server 起不来，就无法回退了（R68/R103 语义）
                let exe = self
                    .cfg
                    .resolve_local_path(&self.cfg.local_dir)
                    .join(&self.cfg.local_exe);
                if target.is_file() && exe.is_file() {
                    self.backend.stop().await;
                    switched = true;
                    if let Ok(mut g) = self.local_model.lock() {
                        *g = Some(target.clone());
                    }
                    target
                } else {
                    eprintln!(
                        "[translate] 拒绝切换翻译模型：模型 {} 或 llama-server.exe 不可用（{}），沿用 {}",
                        target.display(),
                        exe.display(),
                        cur.display()
                    );
                    cur
                }
            }
            None => {
                // 首次请求：英语模型缺失时回落到默认模型（Python use_model
                // 拒绝切换后仍是默认模型），不能拿一个不存在的 -m 去拉进程。
                let fallback = self.cfg.resolve_local_path(&self.cfg.local_model);
                let chosen = if target != fallback && !target.is_file() {
                    eprintln!(
                        "[translate] 英语翻译模型不存在 {}，回落默认模型 {}",
                        target.display(),
                        fallback.display()
                    );
                    fallback
                } else {
                    target
                };
                if let Ok(mut g) = self.local_model.lock() {
                    *g = Some(chosen.clone());
                }
                chosen
            }
        };

        // 已选定的模型且本进程拉起的 llama-server 还活着：直接复用，
        // 不再每句都发一次 /health（批量 10 句会白白多 10 次 HTTP）。
        if !switched
            && current.as_deref() == Some(chosen.as_path())
            && self.backend.is_running().await
        {
            return Ok(());
        }

        let dir = self.cfg.resolve_local_path(&self.cfg.local_dir);
        self.backend
            .ensure_server(
                &dir,
                &self.cfg.local_exe,
                &chosen,
                None,
                self.cfg.local_start_timeout_sec,
            )
            .await
            .map(|spawned| {
                if spawned {
                    // llama-server 进程变化（切换/重拉/首次拉起）→ 推进代数，
                    // 让并发在飞的失败请求走"对新模型重发一次"的兜底；
                    // 新进程=权重重新加载，预热状态作废
                    self.model_epoch.fetch_add(1, Ordering::SeqCst);
                    if let Ok(mut g) = self.warmed.lock() {
                        *g = false;
                    }
                }
            })
    }

    /// 本地翻译是否已热（做过真实 prefill / 首次成功翻译过后）。
    pub fn mt_warm(&self) -> bool {
        self.warmed.lock().map(|g| *g).unwrap_or(false)
    }

    /// 本地后端预热：真实 prefill 一次（权重加载 + Jinja 模板全链路）。
    ///
    /// R63.2 教训：`/health ok` 只说明进程在，首次请求仍会撞 15-40s 的冷加载。
    /// 英语等按语言切换的模型各自首次仍会经历一次切换，这里暖的是默认路径。
    pub async fn warm_up_local(&self, lang: &str) -> bool {
        if self.backend_kind() != BackendKind::Local {
            return true;
        }
        let user = format!("{}ok", self.cfg.mt_prefix_for(lang));
        self.chat(&self.cfg.mt_system, &user, lang).await.is_ok()
    }

    /// OpenAI 兼容 `/chat/completions` 的 URL（base_url 去尾斜杠）。
    fn openai_url(&self) -> String {
        format!(
            "{}/chat/completions",
            self.cfg.openai.base_url.trim().trim_end_matches('/')
        )
    }

    fn openai_api_key(&self) -> String {
        let c = &self.cfg.openai;
        let direct = c.api_key.trim();
        if !direct.is_empty() {
            return direct.to_string();
        }
        let env_name = if c.api_key_env.trim().is_empty() {
            OPENAI_DEFAULT_KEY_ENV
        } else {
            c.api_key_env.trim()
        };
        std::env::var(env_name).unwrap_or_default().trim().to_string()
    }

    /// 标准 OpenAI 请求体（按当前已学到的兼容修正渲染；新引擎=标准形态）。
    #[cfg_attr(not(test), allow(dead_code))]
    fn openai_payload(&self, system: &str, user: &str) -> Value {
        let c = *self.openai_compat.lock().unwrap_or_else(|p| p.into_inner());
        self.openai_payload_with(system, user, c)
    }

    /// 组装请求体（带兼容修正）：fold_system 把系统提示并进 user、
    /// drop_temperature 删掉采样字段、comp_tokens 换成新字段名。
    fn openai_payload_with(&self, system: &str, user: &str, c: OpenAiCompat) -> Value {
        let model = self.cfg.openai.model.trim();
        let mut messages = Vec::with_capacity(2);
        if !(c.fold_system && !system.is_empty()) {
            messages.push(json!({"role": "system", "content": system}));
        }
        let user_content = if c.fold_system && !system.is_empty() {
            format!("{system}\n\n{user}")
        } else {
            user.to_string()
        };
        messages.push(json!({"role": "user", "content": user_content}));
        let mut v = json!({
            "model": model,
            "messages": messages,
            "max_tokens": self.cfg.openai.max_tokens,
        });
        if !c.drop_temperature {
            v["temperature"] = json!(self.cfg.openai.temperature);
        }
        if c.comp_tokens {
            if let Some(obj) = v.as_object_mut() {
                if let Some(t) = obj.remove("max_tokens") {
                    obj.insert("max_completion_tokens".into(), t);
                }
            }
        }
        v
    }

    async fn chat_openai(&self, system: &str, user: &str) -> Result<String, String> {
        let key = self.openai_api_key();
        if key.is_empty() {
            return Err(format!(
                "未设置云端 API Key：请在「翻译后端」里填入（或设环境变量 {}）",
                if self.cfg.openai.api_key_env.trim().is_empty() {
                    OPENAI_DEFAULT_KEY_ENV
                } else {
                    self.cfg.openai.api_key_env.trim()
                }
            ));
        }
        let model = self.cfg.openai.model.trim();
        if model.is_empty() {
            return Err("未填写云端模型名（如 gpt-4o-mini / deepseek-chat 等）".to_string());
        }
        let base = self.cfg.openai.base_url.trim();
        if base.is_empty() {
            return Err("未填写云端 base_url（形如 https://api.openai.com/v1）".to_string());
        }

        let url = self.openai_url();
        // 兼容阶梯：4xx 且错误体点名了哪个字段不收，就修哪个重试（最多 4 次），
        // 修好的结论进程内记住——下一句直接用对的请求体（Python `_openai_compat`）
        let mut compat = *self.openai_compat.lock().unwrap_or_else(|p| p.into_inner());
        let mut attempt = 0usize;
        loop {
            attempt += 1;
            let mut req = self
                .client
                .post(&url)
                .bearer_auth(&key)
                .json(&self.openai_payload_with(system, user, compat));
            if let Some(t) = request_timeout(self.cfg.openai.timeout_sec) {
                req = req.timeout(t);
            }
            let resp = req
                .send()
                .await
                .map_err(|e| format!("云端翻译请求失败（{url}）：{e}"))?;
            let status = resp.status();
            let text = resp.text().await.unwrap_or_default();
            if !status.is_success() {
                let head: String = text.chars().take(300).collect();
                if matches!(status.as_u16(), 400 | 404 | 422) && attempt < 4 {
                    let low = head.to_ascii_lowercase();
                    let mut changed = false;
                    let mut next = compat;
                    if !next.fold_system && low.contains("system") {
                        next.fold_system = true;
                        changed = true;
                    }
                    if !next.drop_temperature && low.contains("temperature") {
                        next.drop_temperature = true;
                        changed = true;
                    }
                    if !next.comp_tokens && low.contains("max_completion_tokens") {
                        next.comp_tokens = true;
                        changed = true;
                    }
                    if changed {
                        compat = next;
                        *self
                            .openai_compat
                            .lock()
                            .unwrap_or_else(|p| p.into_inner()) = compat;
                        eprintln!(
                            "[translate] 云端服务不收标准请求体（HTTP {status}），已降级重试 {attempt}/4"
                        );
                        continue;
                    }
                }
                return Err(format!("云端翻译失败 HTTP {status}：{head}"));
            }
            let data: Value = serde_json::from_str(&text)
                .map_err(|e| format!("云端翻译响应解析失败（HTTP 200）：{e}"))?;
            let content = data
                .pointer("/choices/0/message/content")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .trim()
                .to_string();
            if content.is_empty() {
                let fr = data
                    .pointer("/choices/0/finish_reason")
                    .and_then(|v| v.as_str())
                    .unwrap_or("?");
                return Err(format!(
                    "云端返回空译文（finish_reason={fr}）：推理类模型可能把预算都花在思考上，\
                     请调大 max_tokens 或换非推理模型"
                ));
            }
            return Ok(content);
        }
    }

    /// Ollama `/api/chat` 的 URL。
    fn ollama_url(&self) -> String {
        format!(
            "{}/api/chat",
            self.cfg.ollama.base_url.trim().trim_end_matches('/')
        )
    }

    fn ollama_payload(&self, system: &str, user: &str) -> Value {
        let model = {
            let m = self.cfg.ollama.model.trim();
            if m.is_empty() {
                OLLAMA_DEFAULT_MODEL
            } else {
                m
            }
        };
        json!({
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user}
            ],
            "stream": false,
            "options": {
                "temperature": self.cfg.ollama.temperature,
                "num_predict": self.cfg.ollama.num_predict,
            },
        })
    }

    async fn chat_ollama(&self, system: &str, user: &str) -> Result<String, String> {
        let url = self.ollama_url();
        let mut req = self
            .client
            .post(&url)
            .json(&self.ollama_payload(system, user));
        if let Some(t) = request_timeout(self.cfg.ollama.timeout_sec) {
            req = req.timeout(t);
        }
        let resp = req
            .send()
            .await
            .map_err(|e| format!("Ollama 翻译请求失败（{url}）：{e}"))?;
        let status = resp.status();
        let text = resp.text().await.unwrap_or_default();
        if !status.is_success() {
            let head: String = text.chars().take(300).collect();
            return Err(format!("Ollama 翻译失败 HTTP {status}：{head}"));
        }
        let data: Value = serde_json::from_str(&text)
            .map_err(|e| format!("Ollama 响应解析失败（HTTP 200）：{e}"))?;
        let content = data
            .pointer("/message/content")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim()
            .to_string();
        if content.is_empty() {
            return Err("Ollama 返回空译文".to_string());
        }
        Ok(content)
    }
}

/// 超时秒数 → Duration；<=0 / 非有限值表示不设上限（Python `timeout_sec <= 0` 语义）。
fn request_timeout(sec: f64) -> Option<Duration> {
    if !sec.is_finite() || sec <= 0.0 {
        None
    } else {
        Some(Duration::from_secs_f64(sec))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    use axum::extract::State;
    use axum::http::HeaderMap;
    use axum::routing::{get, post};
    use axum::{Json, Router};

    fn localize(cfg: &mut TranslateConfig, port: u16) {
        cfg.local_base_url = format!("http://127.0.0.1:{port}");
    }

    fn engine(cfg: TranslateConfig) -> TranslateEngine {
        let be = Arc::new(LlamaBackend::new(cfg.clone()));
        TranslateEngine::new(cfg, be)
    }

    #[test]
    fn backend_routing_matches_python_aliases() {
        let cases = [
            ("local", BackendKind::Local),
            ("llamacpp", BackendKind::Local),
            ("llama", BackendKind::Local),
            ("openai", BackendKind::OpenAi),
            ("cloud", BackendKind::OpenAi),
            ("dashscope", BackendKind::OpenAi),
            ("qwen", BackendKind::OpenAi),
            ("ollama", BackendKind::Ollama),
            ("none", BackendKind::None),
            ("unknown-kind", BackendKind::Ollama),
        ];
        for (raw, want) in cases {
            let mut cfg = TranslateConfig::default();
            cfg.backend = raw.into();
            let eng = engine(cfg);
            assert_eq!(eng.backend_kind(), want, "后端 {raw}");
            assert_eq!(eng.backend_name() == "openai", want == BackendKind::OpenAi);
        }
    }

    #[test]
    fn mt_enabled_is_false_only_for_none_backend() {
        // auto = mt_system 非空；none 是用户显式关闭
        let mut cfg = TranslateConfig::default();
        let be = Arc::new(LlamaBackend::new(cfg.clone()));
        assert!(TranslateEngine::new(cfg.clone(), Arc::clone(&be)).mt_enabled());

        cfg.mt_system = String::new();
        assert!(!TranslateEngine::new(cfg.clone(), Arc::clone(&be)).mt_enabled());

        let mut openai = TranslateConfig::default();
        openai.backend = "openai".into();
        assert!(
            TranslateEngine::new(openai, Arc::clone(&be)).mt_enabled(),
            "云端后端也能翻译（Rust 只有逐句 MT 路径）"
        );

        let mut none = TranslateConfig::default();
        none.backend = "none".into();
        assert!(!TranslateEngine::new(none, be).mt_enabled());
    }

    #[tokio::test]
    async fn none_backend_short_circuits_without_requests() {
        let mut cfg = TranslateConfig::default();
        cfg.backend = "none".into();
        // 即使地址上真有服务也不该发请求；这里用必定连不上的端口再强化一次
        cfg.local_base_url = "http://127.0.0.1:59996".into();
        let eng = engine(cfg);

        assert_eq!(eng.translate_for("你好", "en").await.unwrap(), "");
        assert_eq!(eng.translate("你好").await.unwrap(), "");

        let out = eng
            .translate_batch_for(&["一".to_string(), "二".to_string()], "ja")
            .await;
        assert_eq!(out.len(), 2);
        for r in &out {
            assert!(r.error.is_empty(), "关闭翻译不是失败：{}", r.error);
            assert!(r.translation.is_empty());
        }
        assert_eq!(eng.backend_name(), "none");
        assert_eq!(eng.model_name(), "");
        assert!(!eng.mt_enabled());
    }

    #[test]
    fn english_language_selects_model_by_lang_en() {
        let mut cfg = TranslateConfig::default();
        cfg.base_dir = PathBuf::from("/base");
        cfg.local_model_by_lang
            .insert("en".into(), "../../models/en.gguf".into());
        let eng = engine(cfg);

        assert_eq!(eng.model_for_request_lang("en"), "../../models/en.gguf");
        assert_eq!(eng.model_for_request_lang("en-US"), "../../models/en.gguf");
        assert_eq!(eng.model_for_request_lang("ja"), eng.cfg.local_model);
        assert_eq!(eng.model_for_request_lang("ko"), eng.cfg.local_model);

        // 英语目标语言同样路由到 en 模型（Python 按源语言；这里补齐"目标=英语"）
        let mut cfg2 = TranslateConfig::default();
        cfg2.base_dir = PathBuf::from("/base");
        cfg2.target_lang = "en".into();
        cfg2.local_model_by_lang
            .insert("en".into(), "../../models/en.gguf".into());
        let eng2 = engine(cfg2);
        assert_eq!(eng2.model_for_request_lang("ja"), "../../models/en.gguf");
    }

    #[test]
    fn openai_payload_is_standard_chat_completions() {
        let mut cfg = TranslateConfig::default();
        cfg.backend = "openai".into();
        cfg.openai.base_url = "https://api.example.com/v1/".into();
        cfg.openai.model = "gpt-test".into();
        cfg.openai.temperature = 0.7;
        cfg.openai.max_tokens = 1234;
        let eng = engine(cfg);

        assert_eq!(
            eng.openai_url(),
            "https://api.example.com/v1/chat/completions"
        );
        let p = eng.openai_payload("sys", "usr");
        assert_eq!(p["model"], "gpt-test");
        assert_eq!(p["messages"][0]["role"], "system");
        assert_eq!(p["messages"][0]["content"], "sys");
        assert_eq!(p["messages"][1]["role"], "user");
        assert_eq!(p["messages"][1]["content"], "usr");
        assert_eq!(p["temperature"], 0.7);
        assert_eq!(p["max_tokens"], 1234);
        assert!(p.get("stream").is_none(), "OpenAI 兼容接口不带 stream");
    }

    #[test]
    fn ollama_payload_uses_api_chat_and_default_model() {
        let mut cfg = TranslateConfig::default();
        cfg.backend = "ollama".into();
        cfg.ollama.base_url = "http://127.0.0.1:11435/".into();
        cfg.ollama.model = String::new(); // 发运配置就是空串
        cfg.ollama.temperature = 0.3;
        cfg.ollama.num_predict = 256;
        let eng = engine(cfg);

        assert_eq!(eng.ollama_url(), "http://127.0.0.1:11435/api/chat");
        let p = eng.ollama_payload("sys", "usr");
        assert_eq!(p["model"], OLLAMA_DEFAULT_MODEL);
        assert_eq!(p["stream"], false);
        assert_eq!(p["options"]["temperature"], 0.3);
        assert_eq!(p["options"]["num_predict"], 256);
        assert_eq!(p["messages"][1]["content"], "usr");
    }

    #[test]
    fn status_model_names_follow_backend() {
        let mut cfg = TranslateConfig::default();
        cfg.backend = "openai".into();
        cfg.openai.model = "gpt-4o-mini".into();
        let eng = engine(cfg);
        assert_eq!(eng.model_name(), "gpt-4o-mini");
        assert_eq!(eng.base_url(), "https://api.openai.com/v1");

        let mut cfg = TranslateConfig::default();
        cfg.backend = "ollama".into();
        cfg.ollama.model = String::new();
        let eng = engine(cfg);
        assert_eq!(eng.model_name(), OLLAMA_DEFAULT_MODEL);

        let cfg = TranslateConfig::default();
        let eng = engine(cfg);
        assert!(eng.model_name().ends_with(".gguf"));
        assert_eq!(eng.base_url(), "http://127.0.0.1:8082");
    }

    #[tokio::test]
    async fn empty_texts_short_circuit_without_requests() {
        // 后端地址指向没人监听的端口：只要能返回就说明空串没发请求
        let mut cfg = TranslateConfig::default();
        cfg.local_base_url = "http://127.0.0.1:59997".into();
        let eng = engine(cfg);

        assert_eq!(eng.translate("").await.unwrap(), "");
        assert_eq!(eng.translate("   ").await.unwrap(), "");

        let out = eng
            .translate_batch(&["".to_string(), "  ".to_string()])
            .await;
        assert_eq!(out.len(), 2);
        for r in &out {
            assert!(r.error.is_empty(), "空串不该产生错误：{}", r.error);
            assert!(r.translation.is_empty());
        }
    }

    #[tokio::test]
    async fn batch_preserves_order_and_isolates_failures() {
        let mut cfg = TranslateConfig::default();
        cfg.local_base_url = "http://127.0.0.1:59995".into(); // 连不上
        let eng = engine(cfg);

        let texts = vec!["一".to_string(), "二".to_string(), "三".to_string()];
        let out = eng.translate_batch(&texts).await;
        assert_eq!(out.len(), 3, "顺序与数量必须与输入一致");
        assert_eq!(out[0].text, "一");
        assert_eq!(out[1].text, "二");
        assert_eq!(out[2].text, "三");
        // 后端不可达：每条都带 error，但不 panic、不丢条目
        for r in &out {
            assert!(!r.error.is_empty());
            assert!(r.translation.is_empty());
        }
    }

    // ---------------------------------------------------------- 本地 mock HTTP

    #[derive(Default)]
    struct MockState {
        bodies: Mutex<Vec<Value>>,
        auth: Mutex<Vec<String>>,
        calls: AtomicUsize,
    }

    async fn spawn(app: Router) -> u16 {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });
        port
    }

    async fn mock_openai(
        State(st): State<Arc<MockState>>,
        headers: HeaderMap,
        Json(b): Json<Value>,
    ) -> Json<Value> {
        st.calls.fetch_add(1, Ordering::SeqCst);
        st.bodies.lock().unwrap().push(b);
        st.auth.lock().unwrap().push(
            headers
                .get("authorization")
                .and_then(|v| v.to_str().ok())
                .unwrap_or("")
                .to_string(),
        );
        Json(json!({"choices": [{"message": {"content": " 云端译文 "}, "finish_reason": "stop"}]}))
    }

    async fn mock_ollama(State(st): State<Arc<MockState>>, Json(b): Json<Value>) -> Json<Value> {
        st.calls.fetch_add(1, Ordering::SeqCst);
        st.bodies.lock().unwrap().push(b);
        Json(json!({"message": {"content": " Ollama译文 "}}))
    }

    async fn mock_llama(
        State(st): State<Arc<MockState>>,
        Json(b): Json<Value>,
    ) -> Json<Value> {
        st.calls.fetch_add(1, Ordering::SeqCst);
        st.bodies.lock().unwrap().push(b);
        Json(json!({"choices": [{"message": {"content": " 本地译文 "}}]}))
    }

    #[tokio::test]
    async fn openai_route_posts_chat_completions_with_bearer() {
        let st: Arc<MockState> = Arc::default();
        let app = Router::new()
            .route("/chat/completions", post(mock_openai))
            .with_state(Arc::clone(&st));
        let port = spawn(app).await;

        let mut cfg = TranslateConfig::default();
        cfg.backend = "dashscope".into(); // 别名必须归一成 openai
        cfg.openai.base_url = format!("http://127.0.0.1:{port}");
        cfg.openai.api_key = "sk-unit-test".into();
        cfg.openai.model = "gpt-test".into();
        cfg.openai.temperature = 0.2;
        cfg.openai.max_tokens = 8192;
        let eng = engine(cfg);

        let out = eng.translate_for("こんにちは", "ja").await.unwrap();
        assert_eq!(out, "云端译文", "响应内容要 trim");

        let bodies = st.bodies.lock().unwrap().clone();
        assert_eq!(bodies.len(), 1);
        let b = &bodies[0];
        assert_eq!(b["model"], "gpt-test");
        assert_eq!(b["max_tokens"], 8192);
        assert_eq!(b["messages"][1]["content"], "将下面的日文文本翻译成中文：こんにちは");
        assert_eq!(
            st.auth.lock().unwrap().first().map(String::as_str),
            Some("Bearer sk-unit-test")
        );
    }

    #[tokio::test]
    async fn ollama_route_posts_api_chat() {
        let st: Arc<MockState> = Arc::default();
        let app = Router::new()
            .route("/api/chat", post(mock_ollama))
            .with_state(Arc::clone(&st));
        let port = spawn(app).await;

        let mut cfg = TranslateConfig::default();
        cfg.backend = "ollama".into();
        cfg.ollama.base_url = format!("http://127.0.0.1:{port}");
        cfg.ollama.model = String::new();
        let eng = engine(cfg);

        let out = eng.translate_for("hello", "en").await.unwrap();
        assert_eq!(out, "Ollama译文");

        let bodies = st.bodies.lock().unwrap().clone();
        assert_eq!(bodies.len(), 1);
        assert_eq!(bodies[0]["model"], OLLAMA_DEFAULT_MODEL);
        assert_eq!(bodies[0]["stream"], false);
        assert_eq!(
            bodies[0]["messages"][1]["content"],
            "将下面的英语文本翻译成中文：hello"
        );
    }

    #[tokio::test]
    async fn local_route_uses_llama_chat_completions() {
        let st: Arc<MockState> = Arc::default();
        let app = Router::new()
            .route("/health", get(|| async { Json(json!({"status": "ok"})) }))
            .route("/v1/chat/completions", post(mock_llama))
            .with_state(Arc::clone(&st));
        let port = spawn(app).await;

        let mut cfg = TranslateConfig::default();
        localize(&mut cfg, port);
        let eng = engine(cfg);

        let out = eng.translate_for("おはよう", "ja").await.unwrap();
        assert_eq!(out, "本地译文");
        assert!(eng.current_model_path().is_some(), "本地模型选择要落地");
        assert_eq!(st.calls.load(Ordering::SeqCst), 1);
        let bodies = st.bodies.lock().unwrap().clone();
        assert_eq!(bodies[0]["messages"][1]["content"], "将下面的日文文本翻译成中文：おはよう");
    }

    #[tokio::test]
    async fn english_request_switches_local_model_and_missing_model_is_refused() {
        let dir = std::env::temp_dir().join(format!("nexus-mt-switch-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let ja = dir.join("ja.gguf");
        let en = dir.join("en.gguf");
        std::fs::write(&ja, b"ja").unwrap();
        std::fs::write(&en, b"en").unwrap();
        // 切换分支现在会在停旧进程前校验 llama-server.exe 可用（R68/R103），
        // 测试里放一个假 exe 并把 local_dir 指过来
        let exe = dir.join("llama-server.exe");
        std::fs::write(&exe, b"exe").unwrap();

        let st: Arc<MockState> = Arc::default();
        let app = Router::new()
            .route("/health", get(|| async { Json(json!({"status": "ok"})) }))
            .route("/v1/chat/completions", post(mock_llama))
            .with_state(Arc::clone(&st));
        let port = spawn(app).await;

        let mut cfg = TranslateConfig::default();
        cfg.local_base_url = format!("http://127.0.0.1:{port}");
        cfg.local_dir = dir.to_string_lossy().to_string();
        cfg.local_exe = "llama-server.exe".to_string();
        cfg.local_model = ja.to_string_lossy().to_string();
        cfg.local_model_by_lang
            .insert("en".into(), en.to_string_lossy().to_string());
        let eng = engine(cfg);

        eng.translate_for("こんにちは", "ja").await.unwrap();
        assert_eq!(eng.current_model_path(), Some(ja.clone()));

        // 英语源 → 切到 model_by_lang.en（切换入口：停旧进程 + 新 -m 重启）
        eng.translate_for("hello", "en").await.unwrap();
        assert_eq!(
            eng.current_model_path(),
            Some(en.clone()),
            "英语请求必须切到 en 模型"
        );
        assert_eq!(
            st.bodies.lock().unwrap().last().unwrap()["messages"][1]["content"],
            "将下面的英语文本翻译成中文：hello"
        );

        // 目标文件不存在：拒绝切换、沿用当前模型（Python use_model 的 R68 判据）
        let missing = dir.join("missing.gguf");
        let mut cfg2 = TranslateConfig::default();
        cfg2.local_base_url = format!("http://127.0.0.1:{port}");
        cfg2.local_dir = dir.to_string_lossy().to_string();
        cfg2.local_exe = "llama-server.exe".to_string();
        cfg2.local_model = en.to_string_lossy().to_string();
        cfg2.local_model_by_lang
            .insert("en".into(), missing.to_string_lossy().to_string());
        let eng2 = engine(cfg2);
        eng2.set_current_model(Some(en.clone()));
        eng2.translate_for("hello", "en").await.unwrap();
        assert_eq!(
            eng2.current_model_path(),
            Some(en.clone()),
            "缺失的英语模型不能顶掉正在用的模型"
        );

        // 首次请求（尚未选定模型）遇到缺失的英语模型：回落默认模型
        let mut cfg3 = TranslateConfig::default();
        cfg3.local_base_url = format!("http://127.0.0.1:{port}");
        cfg3.local_model = ja.to_string_lossy().to_string();
        cfg3.local_model_by_lang
            .insert("en".into(), missing.to_string_lossy().to_string());
        let eng3 = engine(cfg3);
        eng3.translate_for("hello", "en").await.unwrap();
        assert_eq!(
            eng3.current_model_path(),
            Some(ja.clone()),
            "首次请求也不能用不存在的英语模型"
        );

        let _ = std::fs::remove_dir_all(&dir);
    }

    #[tokio::test]
    async fn openai_missing_key_fails_before_any_request() {
        let mut cfg = TranslateConfig::default();
        cfg.backend = "openai".into();
        cfg.openai.base_url = "http://127.0.0.1:59994".into();
        cfg.openai.api_key = String::new();
        cfg.openai.api_key_env = "NEXUS_TEST_NO_SUCH_KEY_ENV".into();
        cfg.openai.model = "gpt-test".into();
        let eng = engine(cfg);

        let err = eng.translate_for("hi", "en").await.unwrap_err();
        assert!(err.contains("API Key"), "实际：{err}");
    }
}
