//! 本地 llama.cpp 翻译后端（OpenAI 兼容 `/v1/chat/completions`）。
//!
//! 对应 Python `llama_backend.py` + `translate_engine._chat_local`：
//! - 启动参数逐项对齐：`-m <gguf> --alias <alias> -c <ctx> -ngl <ngl> -t <threads> --host --port --jinja`
//! - 探活走 `/health`（加载中 503，就绪 `{"status":"ok"}`）
//! - 采样参数：MT 逐句模式 temp 0.1 / top_p 0.3 / max_tokens 256

use std::path::Path;
use std::sync::Arc;
use std::time::Duration;

use serde::Deserialize;
use serde_json::json;

use crate::config::TranslateConfig;
use crate::process::{wait_ready, Managed};

#[derive(Debug, Deserialize)]
struct ChatResponse {
    choices: Vec<Choice>,
}
#[derive(Debug, Deserialize)]
struct Choice {
    message: Message,
}
#[derive(Debug, Deserialize)]
struct Message {
    #[serde(default)]
    content: String,
}

pub struct LlamaBackend {
    cfg: TranslateConfig,
    managed: Arc<Managed>,
    client: reqwest::Client,
    /// 已校验"端口上跑的就是这个模型"的记录：探活复用外部/残留服务时，
    /// 只探活不校验模型会把别的模型静默拿来翻当前语言（错模型全程翻译）。
    verified_model: std::sync::Mutex<Option<String>>,
}

impl LlamaBackend {
    pub fn new(cfg: TranslateConfig) -> Self {
        Self {
            cfg,
            managed: Managed::new("llama-server"),
            client: reqwest::Client::builder()
                .timeout(Duration::from_secs(180))
                .build()
                .unwrap_or_default(),
            verified_model: std::sync::Mutex::new(None),
        }
    }

    pub fn base_url(&self) -> String {
        self.cfg.local_base_url.trim_end_matches('/').to_string()
    }

    pub async fn probe(&self) -> bool {
        let url = format!("{}/health", self.base_url());
        match self.client.get(&url).timeout(Duration::from_secs(2)).send().await {
            Ok(r) => serde_json::from_str::<serde_json::Value>(
                &r.text().await.unwrap_or_default(),
            )
            .ok()
            .and_then(|v| v.get("status").and_then(|s| s.as_str()).map(|s| s == "ok"))
            .unwrap_or(false),
            Err(_) => false,
        }
    }

    pub async fn is_running(&self) -> bool {
        self.managed.is_running().await
    }

    /// 端口上正在跑的服务加载的模型路径（`/props` 的 model_path；拿不到 = None）。
    async fn running_model_path(&self) -> Option<String> {
        let url = format!("{}/props", self.base_url());
        let r = self
            .client
            .get(&url)
            .timeout(Duration::from_secs(2))
            .send()
            .await
            .ok()?;
        if !r.status().is_success() {
            return None;
        }
        let v: serde_json::Value = r.json().await.ok()?;
        v.get("model_path")
            .and_then(|m| m.as_str())
            .map(|s| s.to_string())
    }

    /// 确保常驻服务在跑（幂等）。
    pub async fn ensure_server(
        &self,
        dir: &Path,
        exe_name: &str,
        model: &Path,
        log: Option<&Path>,
        wait_s: f64,
    ) -> Result<bool, String> {
        if self.probe().await {
            // 复用前校验模型：残留/外部服务跑的可能是另一个模型（例如残留的
            // en 模型在给 ja 会话翻译）。/props 拿不到（老版本）才退回纯探活。
            // 返回 false = 复用（没有新拉起进程）。
            let want = model.to_string_lossy().to_string();
            let verified_ok = self
                .verified_model
                .lock()
                .ok()
                .and_then(|g| g.clone())
                .map(|v| same_model(&v, model))
                .unwrap_or(false);
            if !verified_ok {
                if let Some(run_path) = self.running_model_path().await {
                    if !same_model(&run_path, model) {
                        return Err(format!(
                            "端口 {} 已有 llama-server 在跑，但加载的是另一个模型：{}（当前需要 {}）。\
                             请结束那个服务后重试（避免静默用错模型翻译）",
                            self.base_url(),
                            run_path,
                            want
                        ));
                    }
                }
                if let Ok(mut g) = self.verified_model.lock() {
                    *g = Some(want);
                }
            }
            return Ok(false);
        }
        // 服务不在了：校验缓存一并失效
        if let Ok(mut g) = self.verified_model.lock() {
            *g = None;
        }
        let exe = dir.join(exe_name);
        if !exe.is_file() {
            return Err(format!(
                "找不到 llama-server.exe：{}（本地翻译运行时未安装）",
                exe.display()
            ));
        }
        if !model.is_file() {
            return Err(format!(
                "找不到翻译模型：{}（模型应放在 models 文件夹下）",
                model.display()
            ));
        }
        let port = self
            .base_url()
            .rsplit(':')
            .next()
            .and_then(|p| p.parse::<u16>().ok())
            .unwrap_or(8082);
        let args: Vec<String> = vec![
            "-m".into(),
            model.to_string_lossy().to_string(),
            "--alias".into(),
            self.cfg.local_alias.clone(),
            "-c".into(),
            self.cfg.local_ctx.to_string(),
            "-ngl".into(),
            self.cfg.local_ngl.to_string(),
            "-t".into(),
            self.cfg.thread_num.to_string(),
            "--host".into(),
            "127.0.0.1".into(),
            "--port".into(),
            port.to_string(),
            "--jinja".into(),
        ];
        self.managed
            .spawn(&exe, &args, Some(dir), log)
            .await
            .map_err(|e| format!("启动 llama-server 失败：{e}"))?;
        let me = self;
        if let Err(e) = wait_ready(wait_s, || async move { me.probe().await }, &self.managed).await
        {
            // 探活失败：清掉刚拉起的子进程，不留 4GB 显存残留
            self.stop().await;
            return Err(e);
        }
        if let Ok(mut g) = self.verified_model.lock() {
            *g = Some(model.to_string_lossy().to_string());
        }
        Ok(true) // true = 本次新拉起了进程
    }

    pub async fn stop(&self) {
        self.managed.stop().await;
        if let Ok(mut g) = self.verified_model.lock() {
            *g = None;
        }
    }

    /// MT 逐句翻译：system + prefix+text。
    pub async fn translate_one(&self, system: &str, user: &str) -> Result<String, String> {
        let url = format!("{}/v1/chat/completions", self.base_url());
        let payload = json!({
            "model": self.cfg.local_alias,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user}
            ],
            // Sakura 官方采样；MT 模式必须收窄 max_tokens（提示词回显会拖成长循环）
            "temperature": 0.1,
            "top_p": 0.3,
            "max_tokens": 256,
        });
        let resp = self
            .client
            .post(&url)
            .json(&payload)
            .send()
            .await
            .map_err(|e| format!("翻译请求失败：{e}"))?;
        if !resp.status().is_success() {
            let code = resp.status();
            let text = resp.text().await.unwrap_or_default();
            // 按 char 截断：按字节切会把中文/日文切成半个 UTF-8 序列直接 panic
            let head: String = text.chars().take(300).collect();
            return Err(format!("翻译 HTTP {code}：{head}"));
        }
        let data: ChatResponse = resp
            .json()
            .await
            .map_err(|e| format!("翻译响应解析失败：{e}"))?;
        let content = data
            .choices
            .first()
            .map(|c| c.message.content.trim().to_string())
            .unwrap_or_default();
        if content.is_empty() {
            return Err("翻译返回空译文".to_string());
        }
        Ok(content)
    }
}

/// 服务端上报的模型路径与期望模型是否同一份：全路径归一比较，
/// 服务端报相对路径时退一步比文件名（误报比漏报好——校验不过只是不复用）。
fn same_model(running: &str, want: &Path) -> bool {
    let norm = |s: &str| s.replace('/', "\\");
    if norm(running).eq_ignore_ascii_case(&norm(&want.to_string_lossy())) {
        return true;
    }
    match (
        Path::new(running).file_name().map(|s| s.to_string_lossy().to_string()),
        want.file_name().map(|s| s.to_string_lossy().to_string()),
    ) {
        (Some(a), Some(b)) => a.eq_ignore_ascii_case(&b),
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn base_url_trims_trailing_slash() {
        let mut c = TranslateConfig::default();
        c.local_base_url = "http://127.0.0.1:8082/".into();
        let b = LlamaBackend::new(c);
        assert_eq!(b.base_url(), "http://127.0.0.1:8082");
    }

    #[tokio::test]
    async fn ensure_server_reports_missing_exe() {
        let mut c = TranslateConfig::default();
        c.local_base_url = "http://127.0.0.1:59998".into();
        let b = LlamaBackend::new(c);
        let r = b
            .ensure_server(
                Path::new("Z:/nope"),
                "llama-server.exe",
                Path::new("Z:/nope/m.gguf"),
                None,
                1.0,
            )
            .await;
        assert!(r.is_err());
        assert!(r.unwrap_err().contains("llama-server.exe"));
    }

    #[tokio::test]
    async fn ensure_server_reports_missing_model() {
        let mut c = TranslateConfig::default();
        c.local_base_url = "http://127.0.0.1:59997".into();
        let b = LlamaBackend::new(c);
        let dir = std::env::temp_dir().join(format!("nexus-llama-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("llama-server.exe"), b"x").unwrap();
        let r = b
            .ensure_server(&dir, "llama-server.exe", Path::new("Z:/nope/m.gguf"), None, 1.0)
            .await;
        assert!(r.is_err());
        assert!(r.unwrap_err().contains("找不到翻译模型"));
        let _ = std::fs::remove_dir_all(&dir);
    }
}
