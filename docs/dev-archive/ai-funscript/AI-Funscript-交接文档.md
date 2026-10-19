# AI Funscript → Nexus 合并交接文档

> 生成：2026-10-19 11:35 ｜ 用途：换会话后直接照此执行 ｜ 语言：中文

## 0. 一句话

AI Funscript 工作区（19.44 GB）只是 AI 识别模块的调研/原型环境。正式产品在 `E:\Development\FunScriptCast-Nexus`。
现在缺的只是把 **ONNX bundle（212MB）+ 分析脚本** 迁进 Nexus、验证一遍，然后就可以删掉整个 AI Funscript 工作区。

---

## 1. 现状

| 项 | 值 |
|---|---|
| Nexus 当前分支 | `refactor/foundation` |
| HEAD | `10b56f6`（工作区干净） |
| 快照分支 | `snapshot/pre-refactor-20261019`（`1be0c7c`，重构前完整留档） |
| `main` | `ed91569`，未动 |
| AI Funscript 工作区 | 19.44 GB，**无 git**（只有 GTG 是 git 仓库） |
| 外部测试素材 | `E:\testvideo`（4.61 GB，不在工作区内） |

---

## 2. 已完成的工作

### 2.1 产品底座（Nexus）

- `app/` 包：`config.py` / `log.py` / `bridge.py` / `api/registry.py` / `api/services.py` / `api/routes/{meta,state}.py` / `web/static.py`
- 桥接：请求先给新路由，未命中**原样回落**旧 `if/elif` 链
- 新接口：`/api/v2/ping`、`/system`、`/routes`、`/state`、`/logs`、`/library/state`
- 新前端：`web/`（Vite 6 + React 18 + TS 5），构建 177 KB，托管在 `/app/`
- 旧界面 `ui/` 与旧接口全部原样保留
- 自检：`tests/test_app_bridge.py` **14 项全过**；`host_server.py` 改动仅 +23/−7 行
- 文档：`docs/REFACTOR-PLAN.md`

### 2.2 视觉推理去 PyTorch（ONNX）

- `vendor/vision/onnx_backend.py`：编码 / 切点 / 解码（不依赖 torch）
- `vendor/vision/onnx_stream.py`：增量流式引擎（接口与 `StreamEngine` 一致）
- `vendor/vision/goblintrain/jepa_infer.py`：torch 相关导入改为惰性（纯样式解码不再需要 torch）
- 文档：`docs/VISION-ONNX.md`

### 2.3 已验证的数字

| 检查 | 结果 |
|---|---|
| 离线整片动作数（ONNX 编码+解码 vs torch） | **3133 = 3133**（K1cztm） |
| 轨道相关 | vmarg 0.999977；level/env/plat/rev/conf ≈ 0.9999+ |
| 切点 | ONNX 22 = torch 22，100% 互相覆盖（±0.5s） |
| 流式 120s 冒烟 | DML 79s / **CUDA 61s**；轨道 vmarg 0.999848 |
| 编码吞吐 | DML 2.01x；**CUDA 2.59x** 实时 |
| 无 torch 环境 | CUDA venv 内 `torch loaded: False`，全链路跑通 |

---

## 3. 待办（按优先级）

1. **迁移 + 验证 + 删除 AI Funscript**（本文档第 4 节，约 30 分钟）
2. 视觉后端开关：`FSC_VISION_BACKEND=torch|onnx` 接进 `vendor/vision/service.py` / `proto3/main_stream.py`（默认 torch）
3. 打包：默认 DirectML + 模型 222MB；CUDA 运行库（约 1.5GB）作为可选组件；删“安装运行时（装 torch）”逻辑
4. P1 剩余只读接口：`/api/device/state`、`/api/library/items`
5. P2 写接口：设置保存、扫描触发、启动/停止服务

---

## 4. 合并方案

### 4.1 迁移清单（必须先做）

| 源 | 目标 | 体积 |
|---|---|---|
| `E:\Development\tmp\onnx-bundle\` | `Nexus\vendor\vision\bundles\v0.6.0\` | 211.7 MB |
| `AI Funscript\proto3\dump_tracks_conf.py` | `Nexus\tools\vision\` | 10 KB |
| `AI Funscript\proto3\still_gate.py` | 同上 | 3 KB |
| `AI Funscript\proto3\gate_and_compare.py` | 同上 | 9 KB |
| `AI Funscript\proto3\analyze_confidence_gate.py` | 同上 | 9 KB |
| `AI Funscript\proto3\analyze_conf_threshold.py` | 同上 | 6 KB |
| `AI Funscript\proto3\analyze_stillness_fair.py` | 同上 | 7 KB |
| `AI Funscript\proto3\extract_latent.py`、`dump_tracks_full.py` | 同上（可选，仅离线分析） | 13 KB |
| `C:\Users\admin\AppData\Local\Temp\onnx_stream_smoke.py` | `Nexus\tools\vision\smoke_onnx_stream.py`（改路径） | 3 KB |
| `E:\Development\tmp\onnx_full_compare.py`、`onnx_full_encode.py`、`bench_enc.py` | `Nexus\tools\vision\`（改路径） | 11 KB |

脚本迁移时必须改两处：

1. `sys.path` 里加入 `Nexus/vendor/vision`（不要再指向 GTG）；
2. 硬编码路径（`E:\Development\AI Funscript\...`、`E:\Development\tmp\...`）改成参数或常量。

> 迁移脚本时**不要**复制 `proto3` 里的旧缓存/日志；只复制源码。

### 4.2 归档清单（留存，不参与构建）

目标：`Nexus\docs\dev-archive\ai-funscript\`

- `proto1\*.py` + `README.md`、`proto2\*.py` + `README.md`、`proto3\*.py`（仅顶层源码，8.1 MB）
- `.zcode\workflow-drafts\*.dwf.ts`、`proto3\.zcode\workflow-drafts\*.dwf.ts`
- 结果证据（约 10 MB）：`proto3\compare\fresh2\*_stream*.funscript`、`*_tracks.npz`、`gate_report.json`、`conf_analysis.txt`、`conf_rules.txt`、`param_sweep.txt`、`*_compare.png`
- 桌面这份文档也可以复制一份进去

### 4.3 可不迁（可重建，直接删）

| 东西 | 体积 | 重建方法 |
|---|---|---|
| `_third_party\GoblinTrainingGrounds` | 7.95 GB | `git clone https://github.com/ofs69/GoblinTrainingGrounds` + `pip install -r requirements.txt`（导出 ONNX 时需要 torch） |
| `_third_party\goblinscript` | 2.12 GB | GitHub Releases 下载 v0.6.0；**它同时是 CUDA 运行库（cudart/cublas/cuDNN）的来源** |
| `proto3\compare`（latent/转码缓存） | 7.83 GB | 重跑 `extract_latent.py`（每片约 11 分钟） |
| `proto3\sessions`、`__pycache__`、`.venv` | ~0.2 GB | 无需 |

### 4.4 验证步骤（迁移后、删除前）

```powershell
cd "E:/Development/FunScriptCast-Nexus"

# 1) Python 语法与桥接自检（14 项）
.\.venv\Scripts\python.exe -X utf8 tests\test_app_bridge.py

# 2) 前端构建
cd web; npm run build; cd ..

# 3) 用新 bundle 路径做一次 ONNX 冒烟（需要 tools/vision/smoke_onnx_stream.py 路径已改）
$env:PATH = "E:/Development/AI Funscript/_third_party/goblinscript;" + $env:PATH
$env:FSC_VISION_EP = "cuda"
$env:FSC_VISION_BUNDLE = "E:/Development/FunScriptCast-Nexus/vendor/vision/bundles/v0.6.0"
.\tools\vision\smoke_onnx_stream.py  # 若用 CUDA，请用 E:\Development\tmp\ortgpu\Scripts\python.exe
```

验收标准：冒烟到 `state=done`、动作数非 0、轨道与离线解码一致性 > 0.999。

### 4.5 删除步骤（确认 4.4 通过后再执行）

```powershell
# 先确认迁移完成、验证通过
Remove-Item "E:/Development/AI Funscript/_third_party" -Recurse -Force
Remove-Item "E:/Development/AI Funscript/proto3/compare" -Recurse -Force
Remove-Item "E:/Development/AI Funscript/proto3/sessions" -Recurse -Force
Remove-Item "E:/Development/AI Funscript/.venv" -Recurse -Force
Remove-Item "E:/Development/AI Funscript" -Recurse -Force
```

**注意**：`E:\Development\tmp`（bundle、CUDA venv、Rust 参考源码）**不在** AI Funscript 内，删工作区不会影响它。

### 4.6 需要拍板的选择（默认建议）

| # | 选择 | 默认建议 |
|---|---|---|
| 1 | bundle 放哪 | `Nexus\vendor\vision\bundles\v0.6.0\`，并加入 `.gitignore` |
| 2 | goblinscript 二进制（2.12GB） | 不保留（可重下）；若要留作对照，放 `Nexus\tools\goblinscript\` 并 gitignore |
| 3 | compare 里的 tracks/funscript（~10MB） | 归档到 `docs/dev-archive/ai-funscript/results/` |
| 4 | CUDA venv `E:\Development\tmp\ortgpu`（494MB） | 保留（没有 torch、可复跑 CUDA 测试） |
| 5 | V-JEPA hub 权重 `~/.cache/torch/hub`（1.6GB） | 保留（重新导出 ONNX 需要）；确定不再导出可删 |

---

## 5. 关键技术事实（新会话必读，避免重踩）

1. **ORT 版本必须 1.24.4**。1.31.0 在 CUDA 上会让 packed-QKV MultiHeadAttention 退回 CPU 并报 `not implemented for CPU`。
2. **编码器只能走 GPU**（DirectML 或 CUDA）；`head.onnx` / `env_step.onnx` 走 **CPU**——int8 输入在 DirectML 上会崩（access violation）。
3. **必须先找切点、再编码**。DirectML 在“编码 + 新建 transnet 会话”并发时会 `E_FAIL`（DmlGraphFusionHelper）。
4. **尾段是近似**：ONNX 图定长 32 帧，官方 Rust 把窗口后滑过真实历史；我们用重复末帧补满整组，只影响最后约 2 秒。
5. **短片段不能做验收**：`rev_bias` 在短前缀上拟合会让静音带偏置偏高（300s 片段动作数会翻倍）。必须整片对比。
6. **DML/CUDA 数值差异**：同一片两端后端动作数会差几个点（官方文档也记录为 backend numeric difference），不是逻辑问题。
7. **导出 ONNX 需要 torch + GTG**：`cd _third_party/GoblinTrainingGrounds && venv/python goblintrain.py export drafts --run v0.6.0 --no-parity --bundle <dir> --label <name>`。删 GTG 后要重新 clone。
8. **产品规矩**：禁止自行启动应用做端到端测试；运行时验证由用户装包后进行。改动只落在 Nexus / proto3；GTG 与 GoblinScript 只读。
9. 当前 bundle 是用**我们自己的** `v0.6.0.pt` 导出的（`E:\Development\tmp\onnx-bundle`），不是第三方包。
10. **CUDA EP 需要 cuDNN 9 + CUDA 12 运行库**。本机目前这两套 DLL 来自 `_third_party\goblinscript`（以及 torch venv）。删掉工作区后，若要继续跑 CUDA 测试，需要：安装系统 CUDA/cuDNN，或保留/重新下载一份运行库（约 1.5GB）。DirectML 不受影响（ORT 自带）。

---

## 6. 新会话开场 prompt（复制即用）

```text
读 C:\Users\admin\Desktop\AI-Funscript-交接文档.md，按第 4 节「合并方案」执行：
1) 把 ONNX bundle 和分析脚本迁入 Nexus，归档原型与结果；
2) 按 4.4 验证（桥接自检 + 前端构建 + 新路径冒烟）；
3) 验证通过后先向我确认，再执行 4.5 删除 AI Funscript 工作区。
当前在分支 refactor/foundation（HEAD 10b56f6）。先只做迁移和验证，不要删任何东西。
```

---

## 7. 文件索引

| 用途 | 路径 |
|---|---|
| 产品仓库 | `E:\Development\FunScriptCast-Nexus` |
| 新底座 | `app\`、`web\` |
| 视觉 ONNX 后端 | `vendor\vision\onnx_backend.py`、`onnx_stream.py` |
| 视觉模块文档 | `docs\VISION-ONNX.md` |
| 重构路线 | `docs\REFACTOR-PLAN.md` |
| 桥接自检 | `tests\test_app_bridge.py` |
| ONNX bundle | `E:\Development\tmp\onnx-bundle`（待迁） |
| CUDA 测试 venv | `E:\Development\tmp\ortgpu`（无 torch） |
| Rust 参考源码 | `E:\Development\tmp\goblin-src`（heads/bundle/encode/boundaries） |
| 测试视频与人工脚本 | `E:\testvideo\funscript test\` |
| 重构前快照分支 | `snapshot/pre-refactor-20261019` |

---

## 8. 迁移完成后的状态（预期）

- `Nexus`：唯一开发/发布仓库，自包含（代码 + bundle + 工具 + 文档）
- `AI Funscript`：已删除，释放约 **19.4 GB**
- `E:\testvideo`：保留（4.61 GB，测试素材）
- `E:\Development\tmp`：保留 bundle 副本/venv 可选清理
