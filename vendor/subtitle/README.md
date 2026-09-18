# Subtitle Server（VRFunScriptCast AI 字幕 · PC 端）

VR 播放器「AI 实时字幕」的 PC 端服务：接收头显推来的 PCM 音频块 → Qwen3-ASR 转录 → 翻译 → 返回带时间轴的字幕 JSON。

架构要点：客户端**提前解码**（领先播放位置 20–30 秒）分块推送，服务端无状态处理，返回的字幕时间戳是**视频绝对时间**，客户端按时间轴渲染并缓存 `.srt`。

## 1. 快速开始

```bash
# 启动（默认读 config.json：0.6B ASR + 本地 Ollama 翻译，全离线）
.venv/Scripts/python.exe run_server.py

# 健康检查
curl http://127.0.0.1:8756/health

# 用测试片走一遍完整链路（抽音频 → 分块 POST → 打印字幕 + 写 SRT）
.venv/Scripts/python.exe test_client.py --input "E:/testvideo/VRTEST.mp4" --lang ja \
    --chunk 25 --overlap 2 --max-minutes 2
```

## 2. HTTP 接口

### `GET /health`
```json
{"ok":true,"asr_model":"models/Qwen3-ASR-0.6B","device":"cuda:0","vad":true,"aligner":true,
 "gpu_used_gb":3.41,"translate_backend":"ollama","glossary":{"ja":110,"en":81}}
```

### `POST /transcribe?lang=ja&video_start_ms=45000&keep_from_ms=0&translate=true`
- body：裸 PCM（**16kHz / 单声道 / s16le 小端**），即 `audio/L16;rate=16000;channels=1`
- `video_start_ms`：本块音频第一帧在视频里的时间（毫秒）→ 返回的时间戳是视频绝对时间
- `keep_from_ms`：可选的"起点早于此毫秒的句子丢弃"（默认 0 = 全部返回，跨块去重交给客户端）
- `translate=false`：只转录不翻译（调试用）

返回：
```json
{"language":"Japanese",
 "segments":[{"start_ms":45800,"end_ms":48400,"text":"…","translation":"…"}],
 "asr_ms":3120,"mt_ms":430,"total_ms":3560,"skipped":false}
```

`skipped=true` 表示 VAD 判定该块无语音（纯静音/BGM 段），没跑 ASR，直接返回空段。

## 3. 配置与两个档位（`config.json`）

> 📁 **配置实际在哪**：不在这里的 `config.json`（那份只是**出厂模板**），而在
> **`<安装目录>\data\subtitle_config.json`** —— 术语表同样在 `data\`，模型缓存在
> `<安装目录>\models\hf-cache`。规则只写一份：`user_paths.py`。
> 老版本（配置还混在本目录里、或在 `%APPDATA%`）首次运行会**自动迁移**过来。
> 详见仓库 README「用户数据在哪」。

| 档位 | ASR | 翻译 | 显存 | 说明 |
|---|---|---|---|---|
| **离线档（默认）** | `models/Qwen3-ASR-0.6B` | `ollama` / qwen2.5:3b | 3.4 + 2.2 ≈ 5.6GB | 开箱即用，不联网 |
| **质量档** | `models/Qwen3-ASR-1.7B` | `openai` / qwen-mt-turbo | 6.1GB（翻译在云端） | 准确率最高，需 API Key |

⚠️ **8GB 显存上 ASR 与本地翻译不能同时驻留**：实测 1.7B（6.13GB）+ qwen2.5:3b（2.2GB）超过 8GB 后，
ASR 单块延迟从 3.1s 涨到 23s（RTF 6.8x → 1.1x）。所以质量档必须配云端翻译。

切换档位：改 `config.json`，或用环境变量临时覆盖
```bash
ASR_MODEL=models/Qwen3-ASR-1.7B TRANSLATE_BACKEND=openai .venv/Scripts/python.exe run_server.py
```
云端翻译需要 `DASHSCOPE_API_KEY`（阿里百炼）等环境变量，见 `config.json` 的 `translate.openai.api_key_env`。

## 4. 术语表（PC 侧管理，双阶段生效）

`glossary_ja_zh.json`（110 条）/ `glossary_en_zh.json`（81 条）：

- **ASR 阶段**：术语原文拼成 `context` 提示，减少专有名词识别错误
  （实测 `小ギャル→コギャル`、`おさわりカブ→おさわりキャバ` 被纠正，耗时不变）
- **翻译阶段**：只注入"当前句真正出现"的术语，避免术语被乱套到别的词上

**管理方式（重要）**：术语表**只在 PC 侧维护**（直接编辑这两个 JSON 文件即可），
**改完即时生效**（服务端按 mtime 热加载，无需重启）。**头显端不提供术语表管理界面**，
头显只需要选语言。后续会做一个 PC 端管理程序，对接下面这两个接口：

```
GET  /glossary?lang=ja        # 查看当前术语表（原文→译文）
POST /glossary/reload         # 手动触发重新读取（正常情况自动热加载，不需要调）
```

文件格式：`{"原文": "译文"}`，UTF-8 JSON。语言键在 `config.json` 的 `glossary` 段里配置
（`ja` → 日文片源，`en` → 英文片源）。

## 5. 验证脚本（阶段 0 产物，仍可用于对比）

```bash
.venv/Scripts/python.exe fetch_models.py                 # 下载模型（含 modelscope bug 绕过）
.venv/Scripts/python.exe validate_asr.py --input X.mp4 --lang ja --plan-only
.venv/Scripts/python.exe validate_asr.py --input X.mp4 --lang ja --model models/Qwen3-ASR-1.7B \
    --context "ヘア解禁、セクキャバ、ピンサロ"              # 带热词
.venv/Scripts/python.exe translate_test.py --input out/X.json --model qwen2.5:3b --glossary glossary_ja_zh.json
```

## 6. 实测数据

见 [RESULTS.md](RESULTS.md)：0.6B/1.7B 速度与准确率对比、翻译模型对比、显存冲突实测。

## 7. 头显端使用（已实现）

客户端在 VR 播放器里，代码位于 `funscriptcore/.../AiSubtitleEngine.kt`（取音+推流）与
Unity 的 `AiSubtitleController.cs` / `SubtitleOverlay.cs`（UI+渲染）。

1. **设置页**（主页 → 设置 → 侧栏「AI 字幕」）：填 PC 服务地址（默认 `http://192.168.2.2:8756`）、
   提前量（默认 25 秒）、是否"打开视频后自动开启"。
2. **播放页顶栏**点「AI」按钮 → 选语言（日语/英语/韩语/中文）→ 开始推流；
   再点一次关闭。
3. 字幕按视频时间轴显示译文（可选同时显示原文）。

客户端实现要点：
- 影子解码器：第二个 ExoPlayer，**禁用视频轨**、静音，位置 = 主播放器 + 提前量
- 音频链：透传保音质 + 混单 + 线性重采样到 16kHz int16
- 每 25 秒一块（重叠 2 秒）POST `/transcribe`；跨块用"时间重叠 + 最长公共子串"去重
- seek/暂停/切集：影子解码器自动重新对齐（漂移 >3 秒触发），离开播放页停止引擎

## 8. 已知限制

- 跨块去重在客户端按"时间重叠 + 最长公共子串"判定，仍属启发式；边界处偶有轻微重复或截断。
- 0.6B 档的日语专有名词错误明显多于 1.7B（这是默认档的代价，可用术语表/热词部分弥补）。
- 尚无 `.srt` 缓存（重复播放/回拖会重算）；尚无会话与鉴权（仅限可信局域网）。
- AC3/EAC3/DTS 音轨在 Quest 端（ExoPlayer 无 FFmpeg 扩展）无法解码 → 无音频也就没有字幕。
