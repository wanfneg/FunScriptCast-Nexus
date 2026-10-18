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
    --bundle E:/Development/tmp/onnx-bundle --label nexus-onnx-test
```
