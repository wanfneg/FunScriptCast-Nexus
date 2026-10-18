# AI 实时识别（FunScriptCast Nexus · PC 端）

边播边推理：视频解码 → V-JEPA 2.1 视觉编码 → trunk+heads → funscript 动作流。
播放器在播的同时后台识别，宿主每 ~150ms 拉一次增量动作，走**现有脚本联动路径**
（`/api/sync/tick`）驱动设备——BLE 仍只由宿主碰，本模块不接触设备。

## 1. 快速开始

```bash
# 首次：拉权重（checkpoint ~17MB + V-JEPA 编码器 ~1.66GB 全机共享）
powershell -ExecutionPolicy Bypass -File tools\fetch_vision.ps1

# 启动（默认读 config.json，端口 8757）
.venv\Scripts\python.exe vendor\vision\run_server.py

# 健康检查
curl http://127.0.0.1:8757/health
```

依赖：宿主 `.venv` 已含 torch/torchvision/numpy/transformers；本模块另需
`einops timm transnetv2-pytorch opencv-python-headless`（见 `requirements.txt`）。
**torchvision 必须与 torch 同 CUDA 版本**（cu128 ↔ torchvision 0.26.0+cu128），
版本不匹配会报 `operator torchvision::nms does not exist`。

### 安装版（`%LOCALAPPDATA%` 安装）没有 torch 怎么办

安装包体积限制不可能带 torch（4GB+），所以安装版首次用 AI 识别时会提示
「AI 识别还缺运行环境」，点**「安装运行时」**即可：宿主会在
`<安装目录>ision-runtime\` 建一个独立 venv 并安装 torch(cu128) + 视觉依赖，
约 3-5GB，不影响系统 Python，装完自动重新预热。

也可以手动指定解释器：设置 → 启动 → **AI 识别 Python 解释器**，
填一个已装好 `requirements.txt` 的解释器路径（留空=自动探测）。

宿主的探测顺序（`host_server._vision_python`）：
`vision-runtime\python.exe` → 应用目录 `.venv` → 上一级 `.venv` →
设置里填的路径 → PATH 上第一个能 `import torch` 的解释器 → 字幕解释器兜底。
选中的来源会打在设计日志与 `/api/vision/state.py_source` 里。

## 2. HTTP 接口

`/health` 始终开放；其余接口在 `config.json` 的 `server.auth_token` 非空时要求
请求头 `X-FSC-Vision-Token`。

### `POST /open` `{video, duration_ms?, start_ms?}`
打开识别任务（异步）。转码 480p30（约 9x 实时，产物缓存复用）→ 加载模型 →
边播边推理。同一时间只能有一个任务。

### `GET /state`
```json
{"ok":true,"state":"warming|running|done|error","n_actions":217,
 "coverage_ms":179200,"warmup_pct":13.1,"elapsed_sec":180.4,
 "job":{"video":"…","duration_ms":1365320},"error":""}
```
`coverage_ms` 是**已识别到的时间点**：播放头越过它就该显示"AI 跟上中"。

### `GET /actions?from_ms&to_ms`
```json
{"ok":true,"actions":[{"at":170194,"pos":52},…],"coverage_ms":247466}
```
增量拉取。宿主应维护 `last_at`，每次带 `from_ms=last_at` 取新点。

### `POST /close` / `POST /shutdown`
关闭任务（释放显存、删 latent、保留转码缓存）/ 进程退出。

## 3. 配置（`config.json`）

| 键 | 默认 | 说明 |
|---|---|---|
| `server.port` | 8757 | 服务端口 |
| `server.auth_token` | "" | 非空则启用鉴权 |
| `engine.device` | cuda | 推理设备 |
| `engine.publish_rows` | 256 | 每批发布行数（30行/秒）。256=8.5s/批；官方语义 1024 |
| `model.checkpoint` | "" | 空=用 `weights/checkpoints/v0.6.0.pt` |
| `paths.cache_dir` | "" | 空=模块内 `cache/`。latent ≈36KB/行（22分钟片≈1.4GB） |
| `runtime.idle_release_min` | 10 | 空闲多久释放并退出（宿主用） |

## 4. 许可与来源

- `goblintrain/` 与 `stream_engine.py` 派生自 [GoblinTrainingGrounds](https://github.com/ofs69/GoblinTrainingGrounds)
  （MIT，见 `LICENSE.goblintrain.txt`），只保留推理所需子集。
- 模型权重 `v0.6.0.pt`：MIT。V-JEPA 2.1 编码器：Meta，MIT（torch.hub 缓存，不进仓库）。
- 本目录代码（HTTP 服务、宿主接线部分）随 Nexus 分发。

## 5. 已知限制

- 开场预热 ~30-40s（转码 + 模型加载 + 首批前向）；转码产物会缓存，二次打开秒级。
- 吞吐约 1.0-1.3x 实时（RTX 4070 Laptop 实测），缓冲持续领先播放头。
- seek 到未识别区间会留洞（该段无动作，播放端显示"AI 跟上中"）；未做定向补段。
- 单任务：同一时间只识别一个视频。
