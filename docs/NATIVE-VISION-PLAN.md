# 视觉识别原生重写（C++）方案

> 2026-10-19 立项 ｜ 目标：**去掉独立的 Python 识别进程**，把 ONNX 编码/解码、
> 流式样式、静止门做成原生 DLL，由宿主直接调用。
> 说明：宿主本身仍是 PyInstaller 打包的 Python（这不是本次范围）；本次只重写
> `vendor/vision` 的推理链路。

## 为什么不是小改

现在识别进程里的逻辑量：

| 模块 | 现有实现 | 原生重写量 |
|---|---|---|
| 视频转码/取帧 | ffmpeg 子进程 | 先继续用 ffmpeg.exe，后期换 libav |
| 编码器 `vjepa_pca.onnx` | `onnx_backend.encode_window` | ORT C++ + DirectML，窗口/对齐/packing |
| 切点 `transnet.onnx` | `transnet_cuts` | ORT C++，滑窗/合并 |
| 解码 `head/env_step/mask.onnx` | `forward_chunk` | ORT C++ + 包络 AR + trace |
| 轨道/样式 | `goblintrain/jepa_infer.py`（约 2000 行） | Viterbi、plat、rev、env、extrema、RDP |
| 静止门 | `still_gate.py` | 直接移植 |
| 流式前缀 | `onnx_stream.py` | 线程、前缀重算、动作发布 |

## 技术选型

- **C++20 + CMake + MSVC 2022**（本机已有 VS2022/CMake；无 Rust 工具链）
- ONNX Runtime C++ API + **DirectML EP**（NuGet `Microsoft.ML.OnnxRuntime.DirectML`）
- 数值：Eigen（或自写 ndarray）；Viterbi/样式逐步与 Python 对齐
- 视频：一期继续调用 `ffmpeg.exe`（只去掉 Python，不引入新视频栈）；
  二期再评估 libav
- 产物：`vision_engine.dll`（C ABI），宿主用 ctypes 加载；不再启动 `run_server.py`

## ABI（首版）

```c
typedef struct { const char* video; double duration_ms; const char* bundle; const char* cache; } fsc_vision_open_req;
int  fsc_vision_create(const char* config_json);      // 返回句柄
int  fsc_vision_open(void* h, const fsc_vision_open_req* req);
int  fsc_vision_state(void* h, char* buf, int cap);   // JSON: state/coverage/n_actions/note
int  fsc_vision_actions(void* h, double from_ms, double to_ms, char* buf, int cap); // JSON
void fsc_vision_close(void* h);
void fsc_vision_destroy(void* h);
```

宿主侧保留 `FSC_VISION_BACKEND=onnx|native`，先并存、可回退。

## 分阶段

| 阶段 | 产出 | 验收 |
|---|---|---|
| 0 行为冻结 | 用当前 Python 引擎跑 K1cztm/K1drsp 固定片段，存 tracks/actions/gate 金标 | 金标可复现 |
| 1 ORT 原生冒烟 | C++ 加载 `vjepa_pca.onnx` + DirectML，单窗口输出与 Python latent 对比 | corr >0.999 |
| 2 整片轨道 | 编码 + 切点 + head/env 解码，导出 tracks | vmarg/level/env/conf corr >0.999 |
| 3 样式移植 | Viterbi/plat/rev/env/extrema/RDP/前缀 | 动作数/位置与 Python 对齐 |
| 4 静止门 + 流式 DLL | combo 门、ABI、看护 | A/B 70→46 等一致 |
| 5 宿主接入/打包 | ctypes 直连、去 vision-runtime、安装包 | 无识别 Python 进程 |

## 风险/现实预期

- 这是**数周级**工程，不是一次会话能完成；样式机是最大不确定项。
- 数值对齐需逐段对拍，不能"看起来像"。
- DirectML EP 的算子覆盖与 Python 版一致（同一 ORT 1.23.0 bundle）。
- 一期仍然有 `ffmpeg.exe` 子进程（非 Python）；若要连它也去掉，需 libav 静态链接，另加 1–2 周。
