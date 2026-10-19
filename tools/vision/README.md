# tools/vision — 视觉识别分析脚本

ONNX 后端 / 流式引擎 / 离线解码的复算与分析工具。
运行时实现不在这里，在 `vendor/vision/`；这里只放离线脚本。

## 约定

- `sys.path` 指向仓库 `vendor/vision`，不再依赖任何外部工作区。
- 数据路径一律走参数或环境变量，脚本内不写死机器路径。
- 归档结果在 `docs/dev-archive/ai-funscript/results/`。

### 环境变量

| 变量 | 作用 | 默认 |
|---|---|---|
| `FSC_VISION_EP` | ONNX 执行后端 `cuda` / `dml` / `cpu` | `cpu`（编码器必须 GPU） |
| `FSC_VISION_BUNDLE` | bundle 目录（`vendor/vision/onnx_stream.py` 读取） | 无；本目录脚本默认 `vendor/vision/bundles/v0.6.0` |
| `FSC_TESTVIDEO` | 人工脚本（真值）目录 | `E:/testvideo/funscript test` |
| `FSC_VISION_RESULTS` | 结果/轨道目录 | `docs/dev-archive/ai-funscript/results` |
| `FSC_VISION_LEGACY` | 旧工作区兼容布局（`compare/`、`sessions/`） | 无（缺失项显示"缺失"） |

## 脚本

### ONNX / 流式

| 脚本 | 用途 | 依赖 |
|---|---|---|
| `smoke_onnx_stream.py <video>` | 流式引擎冒烟：state=done、动作数、轨道与离线解码一致性 >0.999 | onnxruntime + ffmpeg |
| `onnx_full_encode.py <video> <out.bin>` | 整片 ONNX 编码，写 int8 latent 缓存 | onnxruntime + ffmpeg |
| `onnx_full_compare.py --base DIR` | 整片验收：ONNX 解码 vs torch 轨道/动作对比 | onnxruntime（+ torch 环境用于对照数据） |
| `bench_enc.py [cuda|dml|cpu]` | 编码器吞吐基准 | onnxruntime |

### torch 管线（离线导出）

> 仓库 `.venv` 已不再安装 torch（默认 onnx 后端）；这些脚本需要单独的 torch 环境，
> 或从 `E:\Development\tmp\torch-backup-2.11.0` 恢复后再跑。

| 脚本 | 用途 |
|---|---|
| `extract_latent.py <video> <out前缀>` | 只跑编码器，填满 `<stem>.lat.bin` |
| `dump_tracks_conf.py <video> <out前缀>` | 全片解码 + 额外收集 `conf` 轨道（当前推荐） |
| `dump_tracks_full.py <video> <out前缀>` | 旧版全片导出（有三处口径偏差，仅作对照，见文件头注释） |

这两个 dump 脚本都要求同目录已有 `<stem>.lat.bin` 与权重
`vendor/vision/weights/checkpoints/v0.6.0.pt`（权重由 `tools/fetch_vision.ps1` 下载，不入库）。

### 门控与分析

| 脚本 | 用途 |
|---|---|
| `still_gate.py` | 静止门实现（conf/速度阈值，三种模式） |
| `gate_and_compare.py <前缀> ...` | 应用静止门并与人工脚本逐项对比，出图 |
| `analyze_confidence_gate.py <前缀> ...` | conf 等信号区分"该动/不该动"的 AUC 与阈值 |
| `analyze_conf_threshold.py` | conf 分布、阈值扫描、动作级验证 |
| `analyze_stillness_fair.py` | 静止段幻影动作的公平口径复核 |

`<前缀>` 指不含后缀的结果前缀，例如
`docs/dev-archive/ai-funscript/results/K1cztm`（同目录需有 `_tracks.npz` /
`_meta.json` / `_stream.funscript`）。

依赖：`gate_and_compare.py` 画 `*_compare.png` 需要 **matplotlib**（可选）；
缺它时其余分析照常，只打印"绘图失败"。

## bundle

`vendor/vision/bundles/v0.6.0/`（212 MB，已 gitignore）：

```
manifest.json  transnet.onnx  vjepa_pca.onnx
packs/v0.6.0/{head,mask,env_step}.onnx
```

由我们自己的 `v0.6.0.pt` 导出：

```bash
cd _third_party/GoblinTrainingGrounds
venv/python goblintrain.py export drafts --run v0.6.0 --no-parity --bundle <Nexus>/vendor/vision/bundles/v0.6.0 --label nexus-onnx-test
```

## 冒烟示例

DirectML（ORT 自带，无需额外运行库）：

```powershell
$env:FSC_VISION_EP = "dml"
.\.venv\Scripts\python.exe tools\vision\smoke_onnx_stream.py "E:\testvideo\funscript test\K1cztm.mp4" --duration-s 120
```

CUDA（ORT 必须 1.24.4；需要系统 CUDA 12 + cuDNN 9，**不要用 torch 自带的库顶替**——
视觉链路的验收口径是无 torch 环境）：

```powershell
$env:PATH = "<cuda-runtime-dir>;" + $env:PATH
$env:FSC_VISION_EP = "cuda"
E:\Development\tmp\ortgpu\Scripts\python.exe tools\vision\smoke_onnx_stream.py "E:\testvideo\funscript test\K1cztm.mp4" --duration-s 120
```

工作区删除后，本机不再随 goblinscript 提供 CUDA 运行库；只想验证功能用 DirectML 即可（ORT 自带）。

## 注意

- 编码器只能走 GPU；`head.onnx` / `env_step.onnx` 走 CPU（int8 输入在 DirectML 上会崩）。
- 必须先找切点再编码；DirectML 下"编码 + 新建 transnet 会话"并发会 `E_FAIL`。
- 短片段不能做验收（`rev_bias` 会拟合偏），必须整片对比。
- `E:\testvideo` 是外部测试素材，不在仓库内。
