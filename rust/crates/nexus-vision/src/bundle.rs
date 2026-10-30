//! ONNX bundle 的 manifest 解析（`bundles/v0.6.0/manifest.json`）。
//!
//! 只取推理/样式要用的字段；模型图文件名原样保留，供后续接 `ort` 时用。

use std::path::{Path, PathBuf};

use serde_json::Value;

#[derive(Debug, Clone, Default)]
pub struct PackParams {
    /// 速度量纲：原始 vmarg/env 乘它
    pub v_std: f64,
    /// 静止速度下限（pos/s），官方 22
    pub still_eps: f64,
    pub ext_snap: f64,
    pub amp_cap_x: f64,
    pub plat_thr: f64,
    pub plat_lo: f64,
    pub plat_peak: f64,
    pub plat_veto: f64,
    pub plat_soft: (f64, f64),
    pub plat_shift_cap: f64,
    pub plat_rail_track: bool,
    /// 过零吸附半径（秒）；viterbi 无事件镜头回退时使用
    pub rev_snap_s: f64,
    pub subframe: String,
    pub rev_source: String,
    // ---- 切点模型 transnet（manifest transnet 段） ----

    // ---- 解码分块与包络（Python pack 里的同名字段） ----
    /// 每次前向的"新行"数
    pub chunk: usize,
    /// 两侧上下文行数
    pub ctx: usize,
    /// 尾块最少行数（不足则回看补齐）
    pub min_chunk: usize,
    /// 包络状态维度
    pub env_state: usize,
    pub env_seed: u64,
    pub env_base_hi: f64,
    /// true = 走 env_step 的 AR 路径（v0.6.0 为 true）
    pub env_flow: bool,
}

impl PackParams {
    pub fn chunk_rows(&self) -> usize {
        self.chunk.max(1)
    }
    pub fn ctx_rows(&self) -> usize {
        self.ctx
    }
    pub fn min_chunk_rows(&self) -> usize {
        self.min_chunk.max(1)
    }
}

#[derive(Debug, Clone, Default)]
pub struct Bundle {
    pub dir: PathBuf,
    pub bundle_version: i64,
    pub encoder: String,
    pub grid: usize,
    pub dim: usize,
    pub row_hz: f64,
    pub grid_fps: f64,
    pub enc_res: usize,
    /// 模型图文件名
    pub graph_encoder: String,
    pub graph_transnet: String,
    // ---- 切点模型 transnet（manifest transnet 段） ----
    pub input_w: usize,
    pub input_h: usize,
    pub window: usize,
    pub step: usize,
    pub thr: f64,
    pub min_gap_s: f64,
    pub graph_head: String,
    pub graph_mask: String,
    pub graph_env_step: String,
    pub pack: PackParams,
    pub default_pack: String,
    pub raw: Value,
}

fn f(v: &Value, k: &str, d: f64) -> f64 {
    v.get(k).and_then(|x| x.as_f64()).unwrap_or(d)
}
fn i(v: &Value, k: &str, d: i64) -> i64 {
    v.get(k).and_then(|x| x.as_i64()).unwrap_or(d)
}
fn s(v: &Value, k: &str, d: &str) -> String {
    v.get(k)
        .and_then(|x| x.as_str())
        .unwrap_or(d)
        .to_string()
}

impl Bundle {
    pub fn load(dir: &Path) -> Result<Self, String> {
        let manifest = dir.join("manifest.json");
        let text = std::fs::read_to_string(&manifest)
            .map_err(|e| format!("读不到 {}：{e}", manifest.display()))?;
        let raw: Value =
            serde_json::from_str(&text).map_err(|e| format!("manifest 解析失败：{e}"))?;

        let default_pack = s(&raw, "default", "");
        let packs = raw.get("packs").and_then(|p| p.as_object());
        let pack_v = packs
            .and_then(|p| p.get(&default_pack))
            .or_else(|| packs.and_then(|p| p.values().next()))
            .cloned()
            .unwrap_or(Value::Null);

        let graphs = raw.get("graphs").cloned().unwrap_or(Value::Null);
        let tn = raw.get("transnet").cloned().unwrap_or(Value::Null);
        let pack_graphs = pack_v.get("graphs").cloned().unwrap_or(Value::Null);

        let soft = pack_v
            .get("plat_soft")
            .and_then(|v| v.as_array())
            .filter(|a| a.len() >= 2)
            .map(|a| {
                (
                    a[0].as_f64().unwrap_or(0.5),
                    a[1].as_f64().unwrap_or(1.0),
                )
            })
            .unwrap_or((0.5, 1.0));

        // 与 transnet 同款：字段存在但非法直接报错——负数 as usize 会符号扩展成
        // 巨大值（-1 → usize::MAX），chunk/min_chunk 为 0 更是让窗口逻辑跑不了
        let chunk_v = i(&pack_v, "chunk", 1024);
        let ctx_v = i(&pack_v, "ctx", 1024);
        let min_chunk_v = i(&pack_v, "min_chunk", 60);
        let env_state_v = i(&pack_v, "env_state", 16);
        if chunk_v < 1 || ctx_v < 0 || min_chunk_v < 1 || env_state_v < 0 {
            return Err(format!(
                "manifest pack 参数非法：chunk={chunk_v} ctx={ctx_v} min_chunk={min_chunk_v} env_state={env_state_v}（不允许负值，chunk/min_chunk 须 >= 1）"
            ));
        }

        let pack = PackParams {
            v_std: f(&pack_v, "v_std", 1.0),
            still_eps: f(&pack_v, "still_eps", 22.0),
            ext_snap: f(&pack_v, "ext_snap", 0.0),
            amp_cap_x: f(&pack_v, "amp_cap_x", 4.0),
            plat_thr: f(&pack_v, "plat_thr", 0.25),
            plat_lo: f(&pack_v, "plat_lo", 0.15),
            plat_peak: f(&pack_v, "plat_peak", 0.45),
            plat_veto: f(&pack_v, "plat_veto", 15.0),
            plat_soft: soft,
            plat_shift_cap: f(&pack_v, "plat_shift_cap", 25.0),
            plat_rail_track: pack_v
                .get("plat_rail_track")
                .and_then(|v| v.as_bool())
                .unwrap_or(true),
            rev_snap_s: f(&pack_v, "rev_snap_s", 0.066667),
            subframe: s(&pack_v, "subframe", "rev"),
            rev_source: s(&pack_v, "rev_source", "viterbi"),
            chunk: chunk_v as usize,
            ctx: ctx_v as usize,
            min_chunk: min_chunk_v as usize,
            env_state: env_state_v as usize,
            env_seed: i(&pack_v, "env_seed", 0) as u64,
            env_base_hi: f(&pack_v, "env_base_hi", 6.0),
            env_flow: pack_v
                .get("env_flow")
                .and_then(|v| v.as_bool())
                .unwrap_or(true),
        };

        let tn_window = i(&tn, "window", 100) as usize;
        let tn_step = i(&tn, "step", 50) as usize;
        // 字段存在但非法直接报错：step=0 会让 n % step 除零，window 过小无法切窗
        if tn_step < 1 || tn_window < 2 {
            return Err(format!(
                "manifest transnet.step/window 非法：step={tn_step} window={tn_window}（要求 step>=1 且 window>=2）"
            ));
        }

        Ok(Self {
            dir: dir.to_path_buf(),
            bundle_version: i(&raw, "bundle_version", 0),
            encoder: s(&raw, "encoder", ""),
            grid: i(&raw, "grid", 24) as usize,
            dim: i(&raw, "dim", 64) as usize,
            row_hz: f(&raw, "row_hz", 30.0),
            grid_fps: f(&raw, "grid_fps", 30.0),
            enc_res: i(&raw, "enc_res", 384) as usize,
            graph_encoder: s(&graphs, "encoder", "vjepa_pca.onnx"),
            graph_transnet: s(&graphs, "transnet", "transnet.onnx"),
            graph_head: s(&pack_graphs, "head", "head.onnx"),
            graph_mask: s(&pack_graphs, "mask", "mask.onnx"),
            graph_env_step: s(&pack_graphs, "env_step", "env_step.onnx"),
            input_w: i(&tn, "input_w", 48) as usize,
            input_h: i(&tn, "input_h", 27) as usize,
            window: tn_window,
            step: tn_step,
            thr: f(&tn, "thr", 0.5),
            min_gap_s: f(&tn, "min_gap_s", 1.0),
            pack,
            default_pack,
            raw,
        })
    }

    /// pack 私有图的目录：`<bundle>/packs/<default_pack>/`
    ///
    /// manifest 里 `graphs.encoder/transnet` 在 bundle 根，而
    /// `packs.<id>.graphs.head/mask/env_step` 在 pack 子目录 —— 两者不能混用同一个基准。
    pub fn pack_dir(&self) -> PathBuf {
        self.dir.join("packs").join(&self.default_pack)
    }

    /// bundle 根下的图（encoder / transnet）。
    pub fn graph_path(&self, name: &str) -> PathBuf {
        self.dir.join(name)
    }

    /// pack 私有图（head / mask / env_step）。
    pub fn pack_graph_path(&self, name: &str) -> PathBuf {
        self.pack_dir().join(name)
    }

    /// 三张 pack 私有图是否都在盘上（缺失时不能开推理）。
    pub fn pack_graphs_present(&self) -> bool {
        [&self.graph_head, &self.graph_mask, &self.graph_env_step]
            .iter()
            .all(|g| self.pack_graph_path(g).is_file())
    }

    /// 每行 latent 的字节数（int8）。
    pub fn row_bytes(&self) -> usize {
        self.dim * self.grid * self.grid
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp_dir(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("nexus-vision-b-{}-{tag}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn parses_real_repo_bundle() {
        let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../vendor/vision/bundles/v0.6.0");
        if !dir.join("manifest.json").is_file() {
            return; // 打包环境里没有 bundle 就跳过
        }
        let b = Bundle::load(&dir).expect("真实 bundle 必须能解析");
        assert_eq!(b.bundle_version, 5);
        assert_eq!(b.grid, 24);
        assert_eq!(b.dim, 64);
        assert_eq!(b.row_hz, 30.0);
        assert_eq!(b.enc_res, 384);
        assert_eq!(b.graph_encoder, "vjepa_pca.onnx");
        assert_eq!(b.graph_transnet, "transnet.onnx");
        assert_eq!(b.graph_head, "head.onnx");
        assert_eq!(b.default_pack, "v0.6.0");
        // v_std 是速度量纲的关键常数
        assert!((b.pack.v_std - 193.57327270507812).abs() < 1e-6);
        assert_eq!(b.pack.still_eps, 22.0);
        assert_eq!(b.pack.plat_peak, 0.45);
        assert_eq!(b.pack.subframe, "rev");
        assert_eq!(b.pack.rev_source, "viterbi");
        assert_eq!(b.row_bytes(), 64 * 24 * 24);

        // pack 私有图在 packs/<id>/ 下，不是 bundle 根 —— 路径拼错会导致推理开不起来
        assert_eq!(b.pack_dir(), dir.join("packs").join("v0.6.0"));
        assert!(b.pack_graph_path("head.onnx").is_file(), "head.onnx 应在 packs/v0.6.0/ 下");
        assert!(b.pack_graphs_present(), "三张 pack 私有图都应存在");
        assert!(b.graph_path("vjepa_pca.onnx").is_file(), "encoder 在 bundle 根");
    }

    #[test]
    fn missing_manifest_errors() {
        let d = tmp_dir("missing");
        let e = Bundle::load(&d).unwrap_err();
        assert!(e.contains("读不到"), "实际：{e}");
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn defaults_when_fields_absent() {
        let d = tmp_dir("defaults");
        std::fs::write(
            d.join("manifest.json"),
            r#"{"packs":{"p":{"v_std":100.0}},"default":"p"}"#,
        )
        .unwrap();
        let b = Bundle::load(&d).unwrap();
        assert_eq!(b.grid, 24, "缺失时用官方默认 24");
        assert_eq!(b.dim, 64);
        assert_eq!(b.row_hz, 30.0);
        assert_eq!(b.pack.v_std, 100.0);
        assert_eq!(b.pack.still_eps, 22.0, "缺失时用官方 STILL_EPS");
        assert_eq!(b.pack.plat_peak, 0.45);
        assert_eq!(b.input_w, 48, "manifest 缺省时 input_w 是 48");
        assert_eq!(b.input_h, 27, "manifest 缺省时 input_h 是 27");
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn picks_default_pack_over_first() {
        let d = tmp_dir("packs");
        std::fs::write(
            d.join("manifest.json"),
            r#"{"packs":{"aaa":{"v_std":1.0},"bbb":{"v_std":2.0}},"default":"bbb"}"#,
        )
        .unwrap();
        let b = Bundle::load(&d).unwrap();
        assert_eq!(b.pack.v_std, 2.0, "必须按 default 选 pack，不是取第一个");
        let _ = std::fs::remove_dir_all(&d);
    }
}
