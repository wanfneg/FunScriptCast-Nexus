# 视觉推理去 PyTorch：ONNX 后端（进行中）

> 2026-10-19 ｜ 分支 refactor/foundation
> 目标：用户端不再安装 PyTorch；torch 只在**导出模型**的开发机/CI 上需要。

## 1. 结论（已实测）

| 检查 | 结果 |
|---|---|
| 从我们自己的 v0.6.0 checkpoint 导出 ONNX 包 | 成功，**222 MB**（5 张图 + manifest） |
| 导出器自带的 ORT↔torch 校验 | encoder corr **0.99993**、head 2.1e-4、transnet 8e-8、mask 9e-7、env_step 0 |
| 整片轨道对齐（K1cztm，39110 行） | vmarg corr 0.999983；level/env/blo/bhi/plat/rev/conf **corr 1.000000** |
| 包络 AR 移植 | 与官方 env_base_draw **21 个测试向量逐位一致** |
| 样式出点 | ONNX **3133 个动作 = torch 3133**；torch 点 100% 覆盖（±100ms） |
| CPU 解码耗时 | 34.1 s / 22 分钟片（≈40× 实时） |

## 2. 产物

- `vendor/vision/onnx_backend.py` —— 不依赖 torch 的推理后端
  - `OnnxBundle(bundle_dir)`：读 manifest、加载 head/env 会话（encoder/transnet 懒加载）
  - `decode_latents(lat, n_rows, cut_rows)`：按官方 chunk/ctx/短尾语义跑 head + env，返回原始轨道
  - `encode_window(frames)` / `transnet_probs(frames)`：编码与切点（已接通图，待整链验证）
- 后端选择：环境变量 `FSC_VISION_EP=dml|cuda`（默认 CPU）

## 3. 关键移植点（对照官方 src/heads.rs，MIT）

1. **分块**：chunk=1024、两侧 ctx=1024；短尾用 `min_chunk` 回看，不足 2 s 的信号整段留空。
2. **包络**：状态每块从 0 重新播种；`env_base_draw` 按**绝对行号**取（SplitMix64），因此分块不影响采样。
3. **镜头末行**：原始 vmarg 上 `vmarg[hi-1] = vmarg[hi-2]`（在乘 v_std 之前）。
4. **中性值**：level→50、blo→0、bhi→100、plat/rev→0；conf 保留 NaN（未知）。
5. **量纲**：`tracks_of` 里 vmarg/env 乘 `v_std`；本后端返回原始值，由样式层乘（与现引擎一致）。

## 4. 尚未完成

- [ ] 用 `vjepa_pca.onnx` 跑**编码链路**并与现有 torch 提取逐值对齐
- [ ] `transnet.onnx` 滑窗驱动 + 切点对齐
- [ ] 接入流式引擎（`_predict_shots_live` 的 ORT 版本），支持边播边推
- [ ] NVIDIA/AMD 实测（CUDA EP / DirectML EP）
- [ ] 安装包：onnxruntime 取代 torch；删除“安装运行时”逻辑
- [ ] 把 torch 从 `requirements.txt` 移到“导出工具”依赖

## 5. 复现

```bash
# 导出（开发机，需要 torch）
cd _third_party/GoblinTrainingGrounds
venv/Scripts/python.exe goblintrain.py export drafts --run v0.6.0 --no-parity \
    --bundle E:/Development/FunScriptCast-Nexus/vendor/vision/bundles/v0.6.0 --label nexus-onnx-test
```

## 6. 整片验收（2026-10-19，K1cztm 39040 行）

ONNX **编码 + 解码** 全链路 vs 现有 torch 管线：

| 轨道 | 相关系数 | p99 偏差 |
|---|---|---|
| vmarg | 0.999977 | 0.012 |
| level | 0.999900 | 0.91 |
| env | 0.999949 | 0.022 |
| blo / bhi | 0.999869 / 0.999962 | 1.46 / 0.29 |
| plat_top / plat_bot | 0.999862 / 0.999948 | 0.006 |
| rev_top / rev_bot | 0.999968 / 0.999971 | 0.003 |
| conf | 0.999988 | 0.007 |

| 指标 | 结果 |
|---|---|
| 动作数 | **3133（ONNX）= 3133（torch）** |
| 双向覆盖（±100ms） | 98.9% / 98.9% |
| 编码吞吐（DirectML fp16） | 54 fps ≈ **1.8× 实时**（39040 行 / 723 s） |
| 解码耗时（CPU） | 44.9 s / 整片 |

### 已知细节

- 编码器必须走 GPU EP（packed-QKV 注意力 CPU 不支持）；head/env 走 CPU（int8 输入在 DML 上实测崩溃）。
- 尾部 70 行未产出：整片编码还没实现官方 `extract.end()` 的静态尾帧补齐（生产接入时必须补）。
- 短片段测试不可用于验收：`rev_bias` 在短前缀上拟合会让静音带偏置偏高（300 s 片段动作数翻倍），必须整片对比。

## 7. 切点与尾部补齐（2026-10-19 续）

### 切点（transnet.onnx）

按官方 `src/boundaries.rs` 逐行移植：30 fps 解码到 27×48；前补 25 帧、尾部补齐到步长整数倍；
窗口 100、步长 50，只保留每个窗口的第 25..75 行；连续 `prob > thr` 的一段取峰值帧，按 `min_gap_s` 合并。

| 指标 | 结果 |
|---|---|
| 切点数 | ONNX **22** = torch **22** |
| 互相覆盖（±0.5 s） | **100% / 100%** |
| 逐点误差 | ≤ 1 帧（33 ms） |
| CPU 耗时 | 197.5 s / 21.7 分钟片（≈6.6× 实时，可后台并行） |

### 编码尾部补齐

`encode_video()` 实现了官方 `extract.end()` 的等价逻辑：EOF 时把末帧补 K 份再排空，
保证最后 2~65 帧仍拥有自己的行。12 秒短段验证：写入 **360/360 行**，与缓存 int8 完全相等 84.25%、p99 偏差 1。

## 8. 流式引擎（onnx_stream.py）

`OnnxStreamEngine` 与 torch 版 `stream_engine.StreamEngine` 公开接口一致
（buffer / times_ms / n_rows / _trk / rev_bias_log / set_playhead / request_jump），
可通过环境变量切换后端。

链路：ffmpeg 解码 → 64 帧分组 → `vjepa_pca.onnx`（GPU）→ int8 latent memmap →
`head.onnx` + `env_step.onnx`（CPU，按 chunk 增量）→ 前缀样式 + 发布。

| 指标（120 秒片段冒烟） | 结果 |
|---|---|
| 完成状态 | done，3608 行（120s×30+8） |
| 动作数 | 141，覆盖至 120.3s |
| 端到端耗时 | 79 s（切点 18s + 编码 ~55s + 解码/样式）≈ **1.5× 实时** |
| 与离线解码的轨道一致性 | vmarg 0.999843；level 0.999984；env 0.999980；conf 0.999999 |

### 两个实测约束

1. **切点必须先跑完**：DirectML 在“编码 + 新建 transnet 会话”并发时会 E_FAIL
   （`DmlGraphFusionHelper` 0xC0000005 族）。官方也是先 `find_cuts` 再编码，已按此顺序实现。
2. **尾段是近似**：官方 Rust 把定长窗口向后滑过真实历史；我们的定长 ONNX 图改用
   “重复末帧补满整组”，只影响最后约 2 秒的上下文。

## 9. CUDA 实测（2026-10-19）

环境：RTX 4070 Laptop 8GB / 驱动 617.42 / `onnxruntime-gpu==1.24.4` / CUDA 12 运行库取自带目录。
**整个测试进程没有安装 torch**（CUDA venv 只含 numpy + onnxruntime-gpu）。

### 编码器吞吐（vjepa_pca.onnx，32 帧窗口）

| 后端 | windows/s | 30fps 帧速 | 相对实时 | 单窗耗时 |
|---|---|---|---|---|
| DirectML | 3.76 | 60.2 fps | 2.01x | 0.266 s |
| **CUDA** | **4.85** | **77.6 fps** | **2.59x** | **0.206 s** |
| CPU | — | — | — | 不支持（packed QKV 无 CPU kernel） |

### 端到端流式冒烟（120 秒片段）

| 后端 | 完成 | 动作数 | 端到端耗时 | vmarg 一致性 |
|---|---|---|---|---|
| DirectML | done | 141 | 79 s | 0.999843 |
| **CUDA** | done | 147 | **61 s** | 0.999848 |

两个后端出点数相差 6 个（约 4%），属于 EP 数值差异（官方文档也记录了 CUDA 与 DML 的
“backend numeric difference”），不是逻辑差异。

### 版本约束（重要）

- **必须钉住 `onnxruntime(-gpu)==1.24.4`**：1.31.0 上 packed-QKV 的 MultiHeadAttention
  在 CUDA 后端会退回 CPU 并报 “not implemented for CPU”。
- 编码器只能走 GPU（DirectML / CUDA）；head/env 走 CPU（int8 在 DML 元素上会崩）。
- CUDA 运行库（cublasLt 约 0.5GB + cuDNN 约 0.9GB）**不放进主安装包**，作为 NVIDIA 用户的可选组件。

### 实测环境的精确版本（2026-10-19，便于复现）

| 组件 | 版本 / 来源 |
|---|---|
| onnxruntime-gpu | 1.24.4（CUDA venv `E:\Development\tmp\ortgpu`，Python 3.11.9，无 torch） |
| cudart64_12 | 6.14.11.12090 |
| cublas64_12 / cublasLt64_12 | 6.14.11.1292 |
| cuDNN | 9.10.2.21（cudnn64_9 + cnn / ops / graph / heuristic / engines_* 共 9 个 DLL） |

这 10 个 DLL 当时取自 `_third_party\goblinscript`（官方 v0.6.0 发行包，合计 1.39 GB）。
**工作区删除后不再保留副本**：CUDA 复跑改用系统 CUDA 12 + cuDNN 9，或重新下载 goblinscript。
**不要用 torch 自带的 CUDA/cuDNN 库顶替**——视觉链路的验收口径是「无 torch 环境」
（CUDA venv 只含 numpy + onnxruntime-gpu）；torch 只属于离线导出工具
（`dump_tracks_*` / `extract_latent`），产品运行时不得依赖它。

产品侧不受影响：默认 DirectML（ORT 自带，无需任何额外运行库），CUDA 运行库仍作为可选组件。
