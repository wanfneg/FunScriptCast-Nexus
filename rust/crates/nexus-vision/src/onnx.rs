//! ONNX 解码（ort）：读 latent → head + env AR → 原始轨道。
//!
//! 编码（vjepa_pca.onnx）需要 GPU（packed-QKV 注意力无 CPU kernel），不在本模块。
//!
//! 语义逐条对齐 Python `onnx_backend.forward_chunk`：
//! - head 输入：`x_i8` (1, n, dim, grid, grid) int8 + `cut` (1, n) float32
//! - 输出按块内相对偏移取 `[off, off+keep)`
//! - env 是 AR：逐行喂 `h_t`，状态块内滚动，`e0` 由 SplitMix64 按**绝对行号**取
//!
//! 张量一律用 `(shape, Vec<T>)` 元组构造 —— 不引 ndarray，避免与 ort 内部版本耦合。

use std::path::{Path, PathBuf};

use ort::environment::Environment;
use ort::session::Session;
use ort::value::Tensor;

use crate::bundle::Bundle;

/// ORT 每进程**只允许一个环境**：编码器/解码器/切点模型共用同一个。
/// 各自 init() 会报 "only one environment is allowed per process"。
static SHARED_ENV: std::sync::OnceLock<Environment> = std::sync::OnceLock::new();
static ENV_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

/// ort panic 信息提取（payload 可能是 &str / String / 其他）。
fn panic_message(p: &Box<dyn std::any::Any + Send>) -> String {
    p.downcast_ref::<&str>().map(|s| s.to_string())
        .or_else(|| p.downcast_ref::<String>().cloned())
        .unwrap_or_else(|| "未知 panic".to_string())
}

pub fn shared_env() -> Result<Environment, String> {
    if let Some(e) = SHARED_ENV.get() {
        return Ok(e.clone());
    }
    // 锁只保护"初始化竞态"，真值在上面的 OnceLock：持锁的 ort 初始化若 panic
    // 毒化了锁，恢复后重试是安全的——报"环境锁中毒"并永久锁死反而是错的
    let _guard = ENV_LOCK.lock().unwrap_or_else(|p| p.into_inner());
    if let Some(e) = SHARED_ENV.get() {
        return Ok(e.clone());
    }
    // ort（2.0-rc）在显卡/DirectML 异常时不保证返回 Err，可能直接 panic：
    // 必须抓成错误上抛，不能让线程带着锁死掉（锁中毒+状态卡 warming 的根源）
    let e = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        ort::environment::init()
            .with_name("nexus-vision")
            .build()
            .map_err(|e| format!("ORT 环境初始化失败：{e}"))
    }))
    .unwrap_or_else(|p| {
        Err(format!(
            "ORT 环境初始化崩溃（常见于显卡不支持 DirectML 或驱动异常）：{}",
            panic_message(&p)
        ))
    })?;
    let _ = SHARED_ENV.set(e.clone());
    Ok(e)
}

pub type Track = Vec<f64>;

#[derive(Debug, Default, Clone)]
pub struct Tracks {
    pub vmarg: Track,
    pub level: Track,
    pub env: Track,
    pub blo: Track,
    pub bhi: Track,
    pub plat_top: Track,
    pub plat_bot: Track,
    pub rev_top: Track,
    pub rev_bot: Track,
    pub conf: Track,
}

impl Tracks {
    pub fn empty(n: usize) -> Self {
        Self {
            vmarg: vec![f64::NAN; n],
            level: vec![f64::NAN; n],
            env: vec![f64::NAN; n],
            blo: vec![f64::NAN; n],
            bhi: vec![f64::NAN; n],
            plat_top: vec![f64::NAN; n],
            plat_bot: vec![f64::NAN; n],
            rev_top: vec![f64::NAN; n],
            rev_bot: vec![f64::NAN; n],
            conf: vec![f64::NAN; n],
        }
    }

    /// 对已解码轨道应用镜头末行速度修正（离线/测试需要与全量 decode 对齐）。
    pub fn apply_shot_tails(&mut self, cut_rows: &[usize], n_rows: usize, fix_tail: bool) {
        fix_shot_tails(self, cut_rows, n_rows, fix_tail);
    }

    /// 扩容到至少 `n` 行，新行保持“未解码”（NaN）。
    pub fn grow(&mut self, n: usize) {
        if self.vmarg.len() >= n {
            return;
        }
        for t in [
            &mut self.vmarg,
            &mut self.level,
            &mut self.env,
            &mut self.blo,
            &mut self.bhi,
            &mut self.plat_top,
            &mut self.plat_bot,
            &mut self.rev_top,
            &mut self.rev_bot,
            &mut self.conf,
        ] {
            t.resize(n, f64::NAN);
        }
    }
}

/// 折点末行沿用前行速度（与官方一致）。`n_rows` 只覆盖已解码部分，未解码行不碰。
///
/// `fix_tail` 只在真正解到片尾时为真：流式中间块的末尾不是镜头边界，
/// 提前按片尾改写会污染之后再也不会重算的那一行。
fn fix_shot_tails(out: &mut Tracks, cut_rows: &[usize], n_rows: usize, fix_tail: bool) {
    let mut cs: Vec<usize> = cut_rows
        .iter()
        .copied()
        .filter(|r| *r > 0 && *r < n_rows)
        .collect();
    cs.sort_unstable();
    cs.dedup();
    let mut edges: Vec<usize> = vec![0];
    edges.extend(cs);
    if fix_tail {
        edges.push(n_rows);
    }
    for w in edges.windows(2) {
        let (lo, hi) = (w[0], w[1]);
        if hi - lo >= 2 && out.vmarg[hi - 2].is_finite() {
            out.vmarg[hi - 1] = out.vmarg[hi - 2];
        }
    }
}

/// 包络基采样：SplitMix64 finalizer over 绝对行号，取高 53 位做尾数。
///
/// 必须与 `goblintrain/common.py:env_base_draw` **逐位一致** —— 这是两个语言唯一
/// 可能解码不同的地方（Python 注释原话：这是 jepa_infer 与 goblinscript 唯一可能
/// 解码不同的点，所以定义成整数混合而不是每次从 RNG 抽）。
pub fn env_base_draw(row: i64, seed: u64, hi: f64) -> f64 {
    const G: u64 = 0x9E37_79B9_7F4A_7C15;
    let mut z = (row as u64).wrapping_add(seed.wrapping_mul(G)).wrapping_add(G);
    z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
    z ^= z >> 31;
    (z >> 11) as f64 * 2f64.powi(-53) * hi
}

pub struct Decoder {
    /// 必须先于 Session 创建、并持有到最后一个 Session 释放
    #[allow(dead_code)]
    env: Environment,
    head: Session,
    env_session: Session,
    bundle: Bundle,
}

impl Decoder {
    pub fn load(bundle: &Bundle) -> Result<Self, String> {
        let head_path = bundle.pack_graph_path(&bundle.graph_head);
        let env_path = bundle.pack_graph_path(&bundle.graph_env_step);
        for p in [&head_path, &env_path] {
            if !p.is_file() {
                return Err(format!("缺少模型图：{}", p.display()));
            }
        }
        let env = shared_env()?;
        let head = open_session(&env, &head_path)?;
        let env_session = open_session(&env, &env_path)?;
        Ok(Self {
            env,
            head,
            env_session,
            bundle: bundle.clone(),
        })
    }

    /// 解码 `n_rows` 行；`lat` 行优先 int8，每行 `dim*grid*grid` 字节。
    pub fn decode(
        &mut self,
        lat: &[i8],
        n_rows: usize,
        cut_rows: &[usize],
    ) -> Result<Tracks, String> {
        let b = self.bundle.clone();
        let row_len = b.row_bytes();
        if lat.len() < n_rows * row_len {
            return Err(format!("latent 长度不足：{} < {}", lat.len(), n_rows * row_len));
        }
        let mut cut = vec![0f32; n_rows];
        let mut cs: Vec<usize> = cut_rows
            .iter()
            .copied()
            .filter(|r| *r > 0 && *r < n_rows)
            .collect();
        cs.sort_unstable();
        cs.dedup();
        for &r in &cs {
            cut[r] = 1.0;
        }

        let mut out = Tracks::empty(n_rows);
        let (chunk, ctx, min_chunk) = (b.pack.chunk_rows(), b.pack.ctx_rows(), b.pack.min_chunk_rows());

        let mut nxt = 0usize;
        while n_rows > nxt + chunk {
            let (s, e) = (nxt, nxt + chunk);
            let fs = s.saturating_sub(ctx);
            let fe = (e + ctx).min(n_rows);
            self.forward(lat, &cut, row_len, fs, s, e, fe, &mut out)?;
            nxt = e;
        }
        if n_rows > nxt {
            let (s, e) = (nxt, n_rows);
            let fs0 = if e - s >= min_chunk {
                s
            } else {
                s.saturating_sub(min_chunk - (e - s))
            };
            let fs = fs0.saturating_sub(ctx);
            if e - fs >= min_chunk {
                self.forward(lat, &cut, row_len, fs, s, e, e, &mut out)?;
            }
        }

        fix_shot_tails(&mut out, cut_rows, n_rows, true);
        Ok(out)
    }

    /// 增量解码：只处理 `next` 之后的新 chunk（对齐 Python `onnx_stream.py:_pump`）。
    ///
    /// 返回是否真的解出了新行。`final_pass=false` 时要求右侧 `ctx` 已就绪，
    /// 所以首批要等 `chunk + ctx` 行；`final_pass=true` 时把剩余行按尾块规则解完。
    /// 这样总解码量是 O(n)，不会像“每次从头 decode”那样退化成 O(n²)。
    pub fn decode_step(
        &mut self,
        lat: &[i8],
        n_rows: usize,
        cut_rows: &[usize],
        tracks: &mut Tracks,
        next: &mut usize,
        final_pass: bool,
    ) -> Result<bool, String> {
        let b = self.bundle.clone();
        let row_len = b.row_bytes();
        if n_rows > 0 && lat.len() < n_rows * row_len {
            return Err(format!(
                "latent 长度不足：{} < {}",
                lat.len(),
                n_rows * row_len
            ));
        }
        if n_rows == 0 || n_rows <= *next {
            return Ok(false);
        }
        tracks.grow(n_rows);
        let (chunk, ctx, min_chunk) = (
            b.pack.chunk_rows(),
            b.pack.ctx_rows(),
            b.pack.min_chunk_rows(),
        );

        let mut did = false;
        loop {
            let (s, e, fs, fe) = if !final_pass {
                if n_rows < *next + chunk + ctx {
                    break;
                }
                let s = *next;
                let e = s + chunk;
                (s, e, s.saturating_sub(ctx), (e + ctx).min(n_rows))
            } else {
                let s = *next;
                let e = n_rows;
                let mut fs = s;
                if e - s < min_chunk {
                    fs = s.saturating_sub(min_chunk - (e - s));
                }
                fs = fs.saturating_sub(ctx);
                if e - fs < min_chunk {
                    if *next == 0 {
                        // 一片都凑不出最小窗口：静默 break 会报"识别完成"且 0 动作，
                        // 用户以为识别成功；必须走错误通道让前端看见
                        return Err(format!(
                            "视频有效行数不足（共 {e} 行，不足最小识别窗口 {min_chunk} 行），无法识别"
                        ));
                    }
                    // 已解过片段的短尾：沿用既有行为，放弃尾部
                    break;
                }
                (s, e, fs, e)
            };

            let mut cut = vec![0f32; fe];
            for &r in cut_rows {
                if r > 0 && r < fe {
                    cut[r] = 1.0;
                }
            }
            self.forward(lat, &cut, row_len, fs, s, e, fe, tracks)?;
            *next = e;
            did = true;
            if final_pass {
                break;
            }
        }
        // 不在这里改写 tracks.vmarg：Python 的静止门读原始 `_trk`，
        // 镜头末行的速度修正只在 publish 的 vel 副本上做。
        Ok(did)
    }

    #[allow(clippy::too_many_arguments)]
    fn forward(
        &mut self,
        lat: &[i8],
        cut: &[f32],
        row_len: usize,
        fs: usize,
        s: usize,
        e: usize,
        fe: usize,
        out: &mut Tracks,
    ) -> Result<(), String> {
        let b = self.bundle.clone();
        let n = fe - fs;
        let off = s - fs;
        let keep = e - s;
        let (dim, grid) = (b.dim, b.grid);

        let x_shape: Vec<i64> = vec![1, n as i64, dim as i64, grid as i64, grid as i64];
        let x_data: Vec<i8> = lat[fs * row_len..fe * row_len].to_vec();
        let cut_shape: Vec<i64> = vec![1, n as i64];
        let cut_data: Vec<f32> = cut[fs..fe].to_vec();

        let outputs = self
            .head
            .run(ort::inputs![
                "x_i8" => Tensor::from_array((x_shape, x_data)).map_err(|e| e.to_string())?,
                "cut" => Tensor::from_array((cut_shape, cut_data)).map_err(|e| e.to_string())?,
            ])
            .map_err(|e| format!("head 推理失败：{e}"))?;

        // 取整条输出（含两侧 ctx）后按 [off, off+keep) 切块；
        // 每条轨道先校验长度再切，防止模型输出与 manifest 不一致时越界 panic
        let grab = |name: &str| -> Result<Vec<f64>, String> {
            let v = outputs.get(name).ok_or_else(|| format!("head 输出缺少 {name}"))?;
            let (_shape, data) = v.try_extract_tensor::<f32>().map_err(|e| e.to_string())?;
            let v: Vec<f64> = data.iter().map(|x| *x as f64).collect();
            if v.len() < off + keep {
                return Err(format!(
                    "轨道 {name}：模型输出长度与 manifest 不一致（{} < {}）",
                    v.len(),
                    off + keep
                ));
            }
            Ok(v)
        };

        let g_vmarg = grab("vmarg")?;
        let g_level = grab("level")?;
        let g_blo = grab("ext_lo")?;
        let g_bhi = grab("ext_hi")?;
        let g_plat_top = grab("plat_top")?;
        let g_plat_bot = grab("plat_bot")?;
        let g_rev_top = grab("rev_top")?;
        let g_rev_bot = grab("rev_bot")?;
        let slice = |v: &Vec<f64>| -> Vec<f64> { v[off..off + keep].to_vec() };

        out.vmarg[s..e].copy_from_slice(&slice(&g_vmarg));
        out.level[s..e].copy_from_slice(&slice(&g_level));
        out.blo[s..e].copy_from_slice(&slice(&g_blo));
        out.bhi[s..e].copy_from_slice(&slice(&g_bhi));
        out.plat_top[s..e].copy_from_slice(&slice(&g_plat_top));
        out.plat_bot[s..e].copy_from_slice(&slice(&g_plat_bot));
        out.rev_top[s..e].copy_from_slice(&slice(&g_rev_top));
        out.rev_bot[s..e].copy_from_slice(&slice(&g_rev_bot));
        // conf 缺失不再静默降级：没有置信度，静止门会静默失效
        if outputs.get("conf").is_none() {
            return Err("head 模型缺少 conf 输出".to_string());
        }
        let g_conf = grab("conf")?;
        out.conf[s..e].copy_from_slice(&slice(&g_conf));

        if !b.pack.env_flow {
            if let Ok(eg) = grab("eg") {
                out.env[s..e].copy_from_slice(&slice(&eg));
            }
            return Ok(());
        }

        // 包络 AR：逐行喂 h[:, i]
        let h = {
            let v = outputs.get("h").ok_or("head 输出缺少 h")?;
            let (_sh, data) = v.try_extract_tensor::<f32>().map_err(|e| e.to_string())?;
            data.to_vec()
        };
        let c = if n > 0 { h.len() / n } else { 0 };
        let mut state = vec![0f32; b.pack.env_state];
        for i in 0..n {
            let mut ht = vec![0f32; c];
            for ci in 0..c {
                ht[ci] = h[ci * n + i];
            }
            let e0 = env_base_draw((fs + i) as i64, b.pack.env_seed, b.pack.env_base_hi);

            let ev = self
                .env_session
                .run(ort::inputs![
                    "h_t" => Tensor::from_array((vec![1i64, c as i64, 1], ht))
                        .map_err(|e| e.to_string())?,
                    "state" => Tensor::from_array((vec![1i64, b.pack.env_state as i64], state.clone()))
                        .map_err(|e| e.to_string())?,
                    "e0" => Tensor::from_array((vec![1i64, 1], vec![e0 as f32]))
                        .map_err(|e| e.to_string())?,
                ])
                .map_err(|e| format!("env 推理失败：{e}"))?;

            let val = {
                let v = ev.get("env").ok_or("env 输出缺失")?;
                let (_s, d) = v.try_extract_tensor::<f32>().map_err(|e| e.to_string())?;
                d.first().copied().unwrap_or(0.0) as f64
            };
            if i >= off && i < off + keep {
                out.env[fs + i] = val;
            }
            state = {
                let v = ev.get("state_next").ok_or("env 状态输出缺失")?;
                let (_s, d) = v.try_extract_tensor::<f32>().map_err(|e| e.to_string())?;
                d.to_vec()
            };
        }
        Ok(())
    }
}

/// 打开编码器会话：**必须走 GPU**（packed-QKV 注意力在 CPU 上没有 kernel）。
#[cfg(windows)]
pub fn open_encoder(env: &Environment, path: &Path) -> Result<Session, String> {
    // DirectML EP 初始化是 ort 在劣质显卡/旧驱动上 panic 的高发点：
    // 抓成错误上抛（friendly_vision_error 会把它映射成显卡提示）
    let load = || -> Result<Session, String> {
        use ort::ep::DirectML;
        let b = Session::builder(env).map_err(|e| format!("ORT 会话构建失败：{e}"))?;
        let b = b
            .with_execution_providers([DirectML::default().build()])
            .map_err(|e| format!("启用 DirectML 失败：{e}"))?;
        let mut b = b;
        b.commit_from_file(path)
            .map_err(|e| format!("加载编码器 {} 失败：{e}", path.display()))
    };
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(load)).unwrap_or_else(|p| {
        Err(format!(
            "编码器初始化崩溃（常见于显卡不支持 DirectML 或驱动异常）：{}",
            panic_message(&p)
        ))
    })
}

#[cfg(not(windows))]
pub fn open_encoder(env: &Environment, path: &Path) -> Result<Session, String> {
    let _ = (env, path);
    Err("编码器需要 GPU EP；当前平台未接线".to_string())
}


// ---------------------------------------------------------------- 编码

/// 编码器：把视频编码成 int8 latent（逐条对齐 Python `onnx_backend.encode_video`）。
///
/// 分组语义：
/// - `GROUP = (CLIP/2)*2*K = 64` 帧一组，每次 flush 产 64 行
/// - 每组按 4 个时间对齐（`N_ALIGN`）各取 16 个 32 帧窗口
/// - EOF 时把末帧补满一整组再 flush（对应官方 `extract.end()`，保证尾部还有行）
pub struct Encoder {
    /// 环境由 `SHARED_ENV` 静态持有（ORT 每进程只允许一个环境），
    /// 字段只为语义完整：会话必须比环境先失效，静态能保证这点。
    #[allow(dead_code)]
    env: Environment,
    session: Session,
    bundle: Bundle,
    int8_scale: f64,
}

/// 编码参数（与官方常量一致，改了就对不上 Python）。
pub const CLIP_LEN: usize = 32;
pub const K_STRIDE: usize = 2;
pub const N_ALIGN: usize = 4;
pub const GROUP: usize = (CLIP_LEN / 2) * 2 * K_STRIDE; // 64
/// 编码分辨率（manifest `enc_res`，v0.6.0 = 384）
pub const ENC_RES_DEFAULT: usize = 384;

impl Encoder {
    pub fn load(bundle: &Bundle) -> Result<Self, String> {
        let path = bundle.graph_path(&bundle.graph_encoder);
        if !path.is_file() {
            return Err(format!("缺少编码器图：{}", path.display()));
        }
        let env = shared_env()?;
        let session = open_encoder(&env, &path)?;
        let int8_scale = bundle
            .raw
            .get("int8_scale")
            .and_then(|v| v.as_f64())
            .unwrap_or(8.0 / 127.0);
        Ok(Self {
            env,
            session,
            bundle: bundle.clone(),
            int8_scale,
        })
    }

    /// 单窗口：(32,384,384,3) uint8 → (16,64,24,24) float32
    pub fn encode_window(&mut self, frames: &[u8], res: usize) -> Result<Vec<f32>, String> {
        let shape: Vec<i64> = vec![1, CLIP_LEN as i64, res as i64, res as i64, 3];
        let out = self
            .session
            .run(ort::inputs![
                "frames" => Tensor::from_array((shape, frames.to_vec()))
                    .map_err(|e| e.to_string())?,
            ])
            .map_err(|e| format!("编码器推理失败：{e}"))?;
        let v = out.iter().next().map(|(_, v)| v).ok_or("编码器无输出")?;
        let (_s, d) = v.try_extract_tensor::<f32>().map_err(|e| e.to_string())?;
        Ok(d.to_vec())
    }

    /// 一整组 → 最多 64 行 int8（按时间顺序还原）。
    fn flush_rows(&mut self, buf: &mut Vec<Vec<u8>>, res: usize) -> Result<Vec<i8>, String> {
        let row_len = self.bundle.row_bytes();
        let limit = GROUP.min(buf.len().saturating_sub(K_STRIDE));
        if limit == 0 {
            return Ok(Vec::new());
        }
        let fsz = res * res * 3;
        let mut wins: Vec<Vec<f32>> = Vec::new();
        let mut aa_all: Vec<usize> = Vec::new();
        for j in 0..N_ALIGN {
            let aa: Vec<usize> = (j..limit).step_by(2 * K_STRIDE).take(CLIP_LEN / 2).collect();
            if aa.is_empty() {
                continue;
            }
            let mut flat = Vec::with_capacity(CLIP_LEN * fsz);
            for &a in &aa {
                flat.extend_from_slice(&buf[a]);
                flat.extend_from_slice(&buf[a + K_STRIDE]);
            }
            wins.push(self.encode_window(&flat, res)?);
            aa_all.extend(aa);
        }
        if wins.is_empty() {
            return Ok(Vec::new());
        }
        // 按 aa 排序还原时间顺序（Python: argsort(concat(aa), kind="stable")）
        let mut order: Vec<usize> = (0..aa_all.len()).collect();
        order.sort_by_key(|&i| (aa_all[i], i));

        let row_elems = self.bundle.dim * self.bundle.grid * self.bundle.grid;
        let mut out: Vec<i8> = Vec::with_capacity(order.len() * row_len);
        for &src_row in order.iter() {
            // src_row 才是"拼接后的第几行"；用它定位窗口，不是用输出位次
            let w = src_row / (CLIP_LEN / 2);
            let r = src_row % (CLIP_LEN / 2);
            if w >= wins.len() {
                break;
            }
            let base = r * row_elems;
            let win = &wins[w];
            if base + row_elems > win.len() {
                return Err(format!(
                    "编码器输出长度异常：窗口 {w} 有 {} 个元素，读取 [{base}, {}) 越界——模型输出与 manifest（dim={}, grid={}）可能不匹配",
                    win.len(),
                    base + row_elems,
                    self.bundle.dim,
                    self.bundle.grid
                ));
            }
            for v in &win[base..base + row_elems] {
                let q = ((*v as f64) / self.int8_scale).round().clamp(-127.0, 127.0) as i8;
                out.push(q);
            }
        }
        buf.drain(0..limit);
        Ok(out)
    }
}

/// 可**逐帧推送**的编码流：满了 64 帧就吐 64 行。
///
/// 流式推理需要"边解码边编码"，不能等整片读完；`Encoder::encode_frames` 那种
/// 拉取式闭包喂不了管道，所以单独给一个推送式封装，两者共用同一套 flush。
pub struct EncodeStreamer {
    enc: Encoder,
    buf: Vec<Vec<u8>>,
    res: usize,
}

impl EncodeStreamer {
    pub fn new(enc: Encoder, res: usize) -> Self {
        Self {
            enc,
            buf: Vec::with_capacity(GROUP + K_STRIDE),
            res,
        }
    }

    /// 推一帧；返回本次产生的行（0 或 64 行 × row_len）。
    pub fn push(&mut self, frame: Vec<u8>) -> Result<Vec<i8>, String> {
        self.buf.push(frame);
        if self.buf.len() >= GROUP + K_STRIDE {
            return self.enc.flush_rows(&mut self.buf, self.res);
        }
        Ok(Vec::new())
    }

    /// 收尾：把末帧补满一整组再 flush（与官方 `extract.end()` 一致）。
    pub fn finish(&mut self) -> Result<Vec<i8>, String> {
        if self.buf.is_empty() {
            return Ok(Vec::new());
        }
        while self.buf.len() < GROUP + K_STRIDE {
            let last = self.buf.last().cloned().unwrap_or_default();
            self.buf.push(last);
        }
        self.enc.flush_rows(&mut self.buf, self.res)
    }
}


fn open_session(env: &Environment, path: &Path) -> Result<Session, String> {
    let load = || {
        Session::builder(env)
            .and_then(|mut b| b.commit_from_file(path))
            .map_err(|e| format!("加载模型 {} 失败：{e}", path.display()))
    };
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(load)).unwrap_or_else(|p| {
        Err(format!(
            "模型加载崩溃（常见于显卡不支持 DirectML 或驱动异常）：{}",
            panic_message(&p)
        ))
    })
}

/// 运行时可用的 ORT 动态库（优先环境变量，其次仓库 .venv 里那份）。
pub fn default_ort_dylib() -> Option<PathBuf> {
    if let Ok(p) = std::env::var("ORT_DYLIB_PATH") {
        let p = PathBuf::from(p);
        if p.is_file() {
            return Some(p);
        }
        // 环境变量已设置但指向的文件不存在：明确提示，避免误以为环境变量生效了
        tracing::warn!(
            "ORT_DYLIB_PATH 指向的文件不存在（{}），回退默认搜索",
            p.display()
        );
    }
    let p = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../.venv/Lib/site-packages/onnxruntime/capi/onnxruntime.dll");
    p.is_file().then_some(p)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn env_base_draw_is_deterministic_and_bounded() {
        let a = env_base_draw(0, 0, 6.0);
        assert_eq!(a, env_base_draw(0, 0, 6.0), "同输入必须同输出");
        assert!((0.0..6.0).contains(&a), "实际 {a}");
        assert_ne!(env_base_draw(1, 0, 6.0), env_base_draw(2, 0, 6.0), "不同行号样本应不同");
        assert_ne!(env_base_draw(5, 0, 6.0), env_base_draw(5, 1, 6.0), "换 seed 应改结果");
    }
}
