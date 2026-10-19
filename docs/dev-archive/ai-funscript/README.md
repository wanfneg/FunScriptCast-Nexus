# AI Funscript 原型归档

> 归档时间：2026-10-19 ｜ 来源：`E:\Development\AI Funscript`（已按合并方案删除）
> 这里只留**源码与结果证据**，用于回溯，不参与构建、不被产品代码引用。

## 为什么归档

AI 识别模块（proto1/2/3）在独立工作区里完成了调研与验证：动作识别核心、流式引擎、
置信度/静止门、ONNX 导出与整片验收。产品侧只保留两条落地路径：

- 运行时实现：`vendor/vision/`（`onnx_backend.py`、`onnx_stream.py`、`stream_engine.py`）
- 分析/复算工具：`tools/vision/`

原型源码与结果证据搬到此处，作为设计依据与历史对照。

## 目录

| 路径 | 内容 |
|---|---|
| `proto1/` | 早期设备/时钟/funscript 基础原型（Python + README） |
| `proto2/` | 播放器与 IPC 原型（mpv、媒体时钟） |
| `proto3/` | 识别核心原型：流式引擎 `stream_engine.py`、离线导出、门控分析、各 A/B 实验脚本 |
| `workflow-drafts/` | 两份 zcode 工作流草案（`.dwf.ts`） |
| `results/` | 2026-10-18/19 的整片对比结果（见下） |
| `AI-Funscript-交接文档.md` | 迁移时的交接文档副本 |

## results 说明

K1cztm / K1drsp 两部片子（`E:\testvideo\funscript test\`）的冻结产物：

- `*_stream.funscript` — 当前 torch 管线离线解码输出
- `*_stream_onnx.funscript` / `*_stream_onnx_full.funscript` — ONNX 编码+解码输出（整片验收用后者）
- `*_stream_gated*.funscript` — 静止门三档（严格 / 平衡 / 温和）
- `*_tracks.npz` / `*_tracks_onnx*.npz` — 逐行轨道（含 `conf`、`times_ms`）
- `*_meta.json`、`*_cuts.npy` — 导出参数与镜头切点
- `gate_report.json` / `gate_compare.json` / `conf_analysis.txt` / `conf_rules.txt` / `param_sweep.txt` — 门控与阈值分析报告
- `*_compare.png` — 人工脚本 vs 生成脚本的窗口对比图

已验证的关键数字（摘自交接文档）：

- 离线整片动作数：ONNX 3133 = torch 3133（K1cztm）
- 轨道相关性：vmarg 0.999977，其余 ≈0.9999+
- 切点：双方 22 个，100% 互相覆盖（±0.5s）
- 流式 120s 冒烟：CUDA 61s / DML 79s，轨道 vmarg 0.999848

复算方式见 `tools/vision/README.md`；结果目录可用 `FSC_VISION_RESULTS` 指过来。

## 未归档（可重建）

| 内容 | 体积 | 重建 |
|---|---|---|
| `_third_party\GoblinTrainingGrounds` | 7.95 GB | `git clone https://github.com/ofs69/GoblinTrainingGrounds` + `pip install -r requirements.txt`（导出 ONNX 需要 torch） |
| `_third_party\goblinscript` | 2.12 GB | GitHub Releases v0.6.0；同时是 CUDA 运行库（cudart/cublas/cuDNN）来源 |
| `proto3\compare`（latent/转码缓存） | 7.83 GB | 重跑 `tools/vision/extract_latent.py`（每片约 11 分钟） |
| `proto3\sessions`、`__pycache__`、`.venv` | ~0.2 GB | 无需 |

## 相关文档

- `docs/VISION-ONNX.md` — ONNX 后端与导出流程
- `vendor/vision/STILLNESS_ANALYSIS.md` — 静止段幻影动作分析
- `tools/vision/README.md` — 迁移后的分析脚本用法
