//! audiocpp ASR 后端：拉起 `audiocpp_server.exe`，按 JSON 调
//! `/v1/audio/transcriptions`。
//!
//! 协议（与 Python `audiocpp_backend.transcribe_wav` 逐字段一致）：
//! ```json
//! {"model": "<STREAM_MODEL_ID>", "audio": "<wav 绝对路径>", "language": "Japanese", "context": "..."}
//! ```
//! 关键点：`audio` 传的是**服务端可读的本地路径**，不是文件内容；
//! `context` 是热词偏置字段，**只认这个名字**（`prompt`/`hotwords` 被静默忽略）。

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use serde::{Deserialize, Serialize};

use crate::config::AsrConfig;
use crate::process::{wait_ready, Managed};

/// audiocpp 的流式模型 id（Python `STREAM_MODEL_ID`）。
pub const STREAM_MODEL_ID: &str = "qwen3-asr-stream";

#[derive(Debug, Clone, Serialize)]
struct TranscribeRequest<'a> {
    model: &'a str,
    audio: String,
    language: String,
    #[serde(skip_serializing_if = "str::is_empty")]
    context: &'a str,
}

#[derive(Debug, Clone, Deserialize, Default)]
pub struct TranscribeResponse {
    #[serde(default)]
    pub text: String,
    #[serde(default)]
    pub rtf: f64,
}

pub struct AudioCppBackend {
    cfg: AsrConfig,
    /// 已解析成绝对路径的模型目录：audiocpp 的 cwd 不是配置目录，相对路径会错位
    model_path: PathBuf,
    /// 服务配置 JSON 的输出目录（`var/run`）
    run_dir: PathBuf,
    managed: Arc<Managed>,
    client: reqwest::Client,
    /// VAD 临时目录序号（并发请求防同名互覆）
    seq: std::sync::atomic::AtomicU64,
}

/// audiocpp 的启动配置：**必须走 `--config`**，它的 CLI 没有 `--model`。
///
/// 只注册一个 `mode=streaming` 模型：流式请求会拒绝 offline 模型，而离线请求
/// 用同一个 id 也能跑（与 Python `audiocpp_backend` 的取舍一致）。
pub fn build_server_config(cfg: &AsrConfig, model: &Path) -> serde_json::Value {
    build_server_config_with_backend(cfg, model, &cfg.device_backend)
}

pub fn build_server_config_with_backend(
    cfg: &AsrConfig,
    model: &Path,
    backend: &str,
) -> serde_json::Value {
    serde_json::json!({
        "host": "127.0.0.1",
        "port": cfg.port,
        "backend": backend,
        "device": 0,
        "threads": cfg.threads,
        "lazy_load": cfg.lazy_load,
        "models": [{
            "id": STREAM_MODEL_ID,
            "family": "qwen3_asr",
            "path": model.to_string_lossy(),
            "task": "asr",
            "mode": "streaming",
        }],
    })
}

impl AudioCppBackend {
    pub fn new(cfg: AsrConfig, model_path: PathBuf, run_dir: PathBuf) -> Self {
        Self {
            cfg,
            model_path,
            run_dir,
            managed: Managed::new("audiocpp_server"),
            client: reqwest::Client::builder()
                .timeout(Duration::from_secs(600))
                .build()
                .unwrap_or_default(),
            seq: std::sync::atomic::AtomicU64::new(0),
        }
    }

    pub fn base_url(&self) -> String {
        format!("http://127.0.0.1:{}", self.cfg.port)
    }

    /// `/health` 探活（服务在跑返回 true）。
    pub async fn probe(&self) -> bool {
        let url = format!("{}/health", self.base_url());
        match self.client.get(&url).timeout(Duration::from_secs(2)).send().await {
            Ok(r) => r.status().is_success(),
            Err(_) => false,
        }
    }

    pub async fn is_running(&self) -> bool {
        self.managed.is_running().await
    }

    /// 确保常驻服务在跑（幂等）。
    pub async fn ensure_server(&self, exe: &Path, log: Option<&Path>, wait_s: f64) -> Result<bool, String> {
        self.ensure_server_with_backend(exe, log, wait_s, None).await
    }

    /// 与 `ensure_server` 相同，但允许用**实际可用**的后端（cuda/cpu）覆盖配置，
    /// 用于"有 GPU 包但机器没有 NVIDIA 驱动"时回退 CPU。
    pub async fn ensure_server_with_backend(
        &self,
        exe: &Path,
        log: Option<&Path>,
        wait_s: f64,
        backend: Option<&str>,
    ) -> Result<bool, String> {
        if self.probe().await {
            return Ok(true); // 复用外部已在跑的服务
        }
        if !exe.is_file() {
            return Err(format!(
                "找不到 audiocpp_server.exe：{}（ASR 运行时未安装）",
                exe.display()
            ));
        }
        if !self.model_path.exists() {
            return Err(format!(
                "ASR 模型不存在：{}（检查 asr.audiocpp.model）",
                self.model_path.display()
            ));
        }
        std::fs::create_dir_all(&self.run_dir)
            .map_err(|e| format!("创建 ASR 配置目录失败：{e}"))?;
        let cfg_path = self
            .run_dir
            .join(format!("audiocpp_asr_{}.json", self.cfg.port));
        let effective_backend = backend.unwrap_or(&self.cfg.device_backend);
        let text = serde_json::to_vec_pretty(&build_server_config_with_backend(
            &self.cfg,
            &self.model_path,
            effective_backend,
        ))
        .map_err(|e| format!("序列化 ASR 配置失败：{e}"))?;
        std::fs::write(&cfg_path, text).map_err(|e| format!("写入 ASR 配置失败：{e}"))?;

        let args: Vec<String> = vec![
            "--config".into(),
            cfg_path.to_string_lossy().to_string(),
            "--host".into(),
            "127.0.0.1".into(),
            "--port".into(),
            self.cfg.port.to_string(),
            "--backend".into(),
            effective_backend.to_string(),
            "--threads".into(),
            self.cfg.threads.to_string(),
        ];
        self.managed
            .spawn(exe, &args, exe.parent(), log)
            .await
            .map_err(|e| format!("启动 audiocpp_server 失败：{e}"))?;
        let me = self;
        if let Err(e) = wait_ready(wait_s, || async move { me.probe().await }, &self.managed).await
        {
            // 探活失败：清掉刚拉起的子进程，不留显存残留
            self.stop().await;
            return Err(e);
        }
        Ok(true)
    }

    pub async fn stop(&self) {
        self.managed.stop().await;
    }

    /// 转写单个 wav。`context` 是热词偏置（上一句原文）。
    pub async fn transcribe_wav(
        &self,
        wav: &Path,
        context: &str,
        language: &str,
    ) -> Result<TranscribeResponse, String> {
        let url = format!("{}/v1/audio/transcriptions", self.base_url());
        let body = TranscribeRequest {
            model: STREAM_MODEL_ID,
            audio: wav.to_string_lossy().to_string(),
            language: if language.is_empty() {
                self.cfg.language.clone()
            } else {
                language.to_string()
            },
            context,
        };
        let resp = self
            .client
            .post(&url)
            .json(&body)
            .send()
            .await
            .map_err(|e| format!("ASR 请求失败：{e}"))?;
        if !resp.status().is_success() {
            let code = resp.status();
            let text = resp.text().await.unwrap_or_default();
            return Err(format!("ASR HTTP {code}：{text}"));
        }
        resp.json::<TranscribeResponse>()
            .await
            .map_err(|e| format!("ASR 响应解析失败：{e}"))
    }

    /// silero VAD：写临时 wav → `audiocpp_cli --task vad` → [(start_sec, end_sec)]
    /// （相对本块，对齐 Python `speech_spans`）。
    ///
    /// - `cli_exe`：audiocpp_cli.exe（与 audiocpp_server.exe 同目录）；
    /// - `vad_model_dir`：assets/framework/models/silero_vad；
    /// - **Err 表示 VAD 本身失败**（CLI 缺失/崩溃/超时），空 Vec 才是"真没有语音"
    ///   ——两者不可混淆，否则 CLI 一坏整片字幕无声消失（Python 的教训）。
    pub fn speech_spans(
        &self,
        cli_exe: &Path,
        vad_model_dir: &Path,
        samples: &[f32],
    ) -> Result<Vec<(f64, f64)>, String> {
        if !cli_exe.is_file() {
            return Err(format!("找不到 audiocpp_cli.exe：{}", cli_exe.display()));
        }
        let dir = std::env::temp_dir().join(format!(
            "nexus-vad-{}-{}",
            std::process::id(),
            self.seq.fetch_add(1, std::sync::atomic::Ordering::Relaxed)
        ));
        std::fs::create_dir_all(&dir).map_err(|e| format!("建 VAD 临时目录失败：{e}"))?;
        let wav_in = dir.join("vad_in.wav");
        let out_json = dir.join("vad_in.chunks.json");
        let r = (|| -> Result<Vec<(f64, f64)>, String> {
            let pcm16: Vec<u8> = samples
                .iter()
                .flat_map(|s| {
                    let v = (s.clamp(-1.0, 1.0) * 32767.0).round() as i16;
                    v.to_le_bytes()
                })
                .collect();
            std::fs::write(&wav_in, crate::stream::wav_wrap(&pcm16, 16000, 1, 16))
                .map_err(|e| format!("写 VAD 临时音频失败：{e}"))?;
            // 超时按音频时长收紧（Python 同款）：CLI 挂死时请求线程不干等
            let duration = (samples.len() as f64 / 16000.0).max(0.1);
            let timeout = (duration * 2.0).clamp(30.0, 300.0);
            let mut cmd = std::process::Command::new(cli_exe);
            cmd.args([
                "--task", "vad",
                "--family", "silero_vad",
                "--model",
            ])
            .arg(vad_model_dir)
            .args(["--backend", "cpu", "--audio"])
            .arg(&wav_in)
            .arg("--vad-chunks-out")
            .arg(&out_json)
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::piped());
            crate::process::hide_console_pub(&mut cmd);
            #[cfg(windows)]
            {
                use std::os::windows::process::CommandExt;
                const CREATE_NO_WINDOW: u32 = 0x0800_0000;
                cmd.creation_flags(CREATE_NO_WINDOW);
            }
            let status = crate::process::wait_with_timeout(&mut cmd, timeout)
                .map_err(|e| format!("VAD CLI 执行失败：{e}"))?;
            let stderr = String::from_utf8_lossy(&status.1).to_string();
            if !status.0 {
                return Err(format!(
                    "VAD CLI 退出码异常：{}",
                    if stderr.is_empty() { "无 stderr".to_string() } else { stderr.chars().take(200).collect() }
                ));
            }
            let text = std::fs::read_to_string(&out_json)
                .map_err(|_| "VAD CLI 未产出 chunks 文件（可能被秒退）".to_string())?;
            let data: serde_json::Value =
                serde_json::from_str(&text).map_err(|e| format!("VAD 输出解析失败：{e}"))?;
            let arr = data.as_array().cloned().unwrap_or_default();
            let mut spans = Vec::with_capacity(arr.len());
            for c in arr {
                let a = c.get("start_sample").and_then(|x| x.as_f64()).unwrap_or(0.0) / 16000.0;
                let b = c.get("end_sample").and_then(|x| x.as_f64()).unwrap_or(0.0) / 16000.0;
                if b > a {
                    spans.push((a, b));
                }
            }
            Ok(spans)
        })();
        let _ = std::fs::remove_dir_all(&dir);
        r
    }

    /// 热词回显检测：audiocpp 在气声/静音段有概率把 `context` 整段复读当结果。
    /// 命中则用无热词重试一次（对齐 Python `_transcribe_span`）。
    pub async fn transcribe_span(
        &self,
        wav: &Path,
        context: &str,
        language: &str,
    ) -> Result<String, String> {
        let r = self.transcribe_wav(wav, context, language).await?;
        let text = r.text.trim().to_string();
        if context.is_empty() || !is_prompt_echo(&text, context) {
            return Ok(text);
        }
        let r2 = self.transcribe_wav(wav, "", language).await?;
        Ok(r2.text.trim().to_string())
    }
}

/// 结果是否整段复读了提示词。
pub fn is_prompt_echo(text: &str, echo_ref: &str) -> bool {
    let t = text.trim();
    let r = echo_ref.trim();
    if t.is_empty() || r.is_empty() {
        return false;
    }
    if t == r {
        return true;
    }
    // 复读往往带标点差异：去空白与常见标点后比较
    let norm = |s: &str| -> String {
        s.chars()
            .filter(|c| !c.is_whitespace() && !"，。！？、,.!?".contains(*c))
            .collect()
    };
    let (tn, rn) = (norm(t), norm(r));
    !tn.is_empty() && tn == rn
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    #[test]
    fn prompt_echo_detection() {
        assert!(is_prompt_echo("こんにちは", "こんにちは"));
        assert!(is_prompt_echo("こんにちは。", "こんにちは"), "标点差异算复读");
        assert!(is_prompt_echo(" こんにちは ", "こんにちは"));
        assert!(!is_prompt_echo("おはよう", "こんにちは"));
        assert!(!is_prompt_echo("", "こんにちは"));
        assert!(!is_prompt_echo("こんにちは", ""));
    }

    #[test]
    fn request_omits_empty_context() {
        let r = TranscribeRequest {
            model: STREAM_MODEL_ID,
            audio: "a.wav".into(),
            language: "Japanese".into(),
            context: "",
        };
        let j = serde_json::to_value(&r).unwrap();
        assert!(j.get("context").is_none(), "空 context 不应出现在请求里");
        assert_eq!(j["model"], STREAM_MODEL_ID);
        assert_eq!(j["audio"], "a.wav");
    }

    #[test]
    fn request_includes_context_when_present() {
        let r = TranscribeRequest {
            model: STREAM_MODEL_ID,
            audio: "a.wav".into(),
            language: "Japanese".into(),
            context: "上一句",
        };
        let j = serde_json::to_value(&r).unwrap();
        assert_eq!(j["context"], "上一句");
    }

    #[test]
    fn base_url_uses_config_port() {
        let mut c = AsrConfig::default();
        c.port = 9999;
        let b = AudioCppBackend::new(c, PathBuf::from("m"), PathBuf::from("r"));
        assert_eq!(b.base_url(), "http://127.0.0.1:9999");
    }

    #[test]
    fn server_config_registers_stream_model_with_absolute_path() {
        let mut c = AsrConfig::default();
        c.port = 18081;
        c.device_backend = "cpu".into();
        let model = Path::new("E:/app/models/Qwen3-ASR-0.6B");
        let j = build_server_config(&c, model);
        assert_eq!(j["host"], "127.0.0.1");
        assert_eq!(j["port"], 18081);
        assert_eq!(j["backend"], "cpu");
        assert_eq!(j["device"], 0);
        assert_eq!(j["threads"], 4);
        assert_eq!(j["lazy_load"], false);
        let m = &j["models"][0];
        assert_eq!(m["id"], STREAM_MODEL_ID, "服务端只注册流式模型 id");
        assert_eq!(m["family"], "qwen3_asr");
        assert_eq!(m["task"], "asr");
        assert_eq!(m["mode"], "streaming", "offline 模式会被流式请求拒绝");
        assert_eq!(m["path"], "E:/app/models/Qwen3-ASR-0.6B");
    }

    #[tokio::test]
    async fn ensure_server_reports_missing_exe() {
        let mut c = AsrConfig::default();
        c.port = 59999; // 没人监听
        let b = AudioCppBackend::new(c, PathBuf::from("m"), PathBuf::from("r"));
        let r = b
            .ensure_server(Path::new("Z:/nope/audiocpp_server.exe"), None, 1.0)
            .await;
        assert!(r.is_err());
        assert!(r.unwrap_err().contains("找不到 audiocpp_server.exe"));
    }
}
