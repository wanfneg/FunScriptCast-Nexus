# Subtitle Server（VRFunScriptCast AI 字幕 · PC 端）

VR 播放器「AI 实时字幕」的 PC 端服务：接收头显推来的 PCM 音频块 → Qwen3-ASR 转录 → 翻译 → 返回带时间轴的字幕 JSON。

架构要点：客户端**提前解码**（领先播放位置 20–30 秒）分块推送，服务端无状态处理，返回的字幕时间戳是**视频绝对时间**，客户端按时间轴渲染。

## 1. 快速开始

```bash
# 启动（默认读 config.json：audiocpp 引擎 Qwen3-ASR-0.6B + 本地 llama-server 翻译，全离线）
.venv/Scripts/python.exe run_server.py

# 健康检查
curl http://127.0.0.1:8756/health

# 用测试片走一遍完整链路（抽音频 → 分块 POST → 打印字幕 + 写 SRT）
.venv/Scripts/python.exe tools/test_client.py --input "E:/testvideo/VRTEST.mp4" --lang ja \
    --chunk 25 --overlap 2 --max-minutes 2
```

## 2. HTTP 接口

### `GET /health`
```json
{"ok":true,"code_sig":"…","pid":1234,"asr_backend":"audiocpp","segmentation":"hybrid",
 "asr_model":"Qwen3-ASR-0.6B","asr_ready":true,"device":"cuda","vad":true,"aligner":true,
 "gpu_used_gb":3.41,"translate_backend":"local","mt_warm":true,
 "idle_sec":12.3,"idle_release_min":5}
```

关键字段：`code_sig` 管线代码签名（"改了代码但表现没变"的陈旧实例排障：对比两份
/health 即可）；`segmentation` 当前切句口径（`hybrid` 混合切句 / `chunk` 定长块）；
`asr_ready` 识别真实可用性（不只看后端名——上游崩了也会为 false）；`mt_warm`
翻译是否已用真实提示词预热（R104 起手机端放行条件含它）；`idle_sec` /
`idle_release_min` 空闲回收进度（PC 界面据此显示"还有多久自动回收"）。

### `POST /transcribe?lang=ja&video_start_ms=45000&keep_from_ms=0&translate=true`
- body：裸 PCM（**16kHz / 单声道 / s16le 小端**），即 `audio/L16;rate=16000;channels=1`
- `video_start_ms`：本块音频第一帧在视频里的时间（毫秒）→ 返回的时间戳是视频绝对时间
- `keep_from_ms`：可选的"起点早于此毫秒的句子丢弃"（默认 0 = 全部返回，跨块去重交给客户端）
- `translate=false`：只转录不翻译（调试用）
- **鉴权（可选）**：config 里 `server.auth_token` 非空时，本接口（含 `/transcribe/stream`）
  必须携带请求头 `X-FSC-Subtitle-Token: <auth_token>`，不匹配回 401；`auth_token`
  为空（默认）时不校验。`/health` 始终开放

返回：
```json
{"language":"Japanese",
 "segments":[{"start_ms":45800,"end_ms":48400,"text":"…","translation":"…"}],
 "asr_ms":3120,"mt_ms":430,"total_ms":3560,"skipped":false,
 "recommended_chunk_sec":3}
```

`recommended_chunk_sec`：档位建议（秒）。云端翻译后端给 25（单块实测 6.6–15s，客户端固定
3s 块会结构性追不上），本地后端给 3；客户端大于 0 时按它切换分块时长。

`skipped=true` 表示 VAD 判定该块无语音（纯静音/BGM 段），没跑 ASR，直接返回空段。

## 3. 配置与运行时组合（`config.json`）

> 📁 **配置实际在哪**：不在这里的 `config.json`（那份只是**出厂模板**），而在
> **`<安装目录>\data\subtitle_config.json`** —— 模型缓存在
> `<安装目录>\models\hf-cache`。规则只写一份：`user_paths.py`。
> 老版本（配置还混在本目录里、或在 `%APPDATA%`）首次运行会**自动迁移**过来。
> 详见仓库 README「用户数据在哪」。

出厂默认 = **audiocpp 引擎**（Qwen3-ASR-0.6B，`asr.segmentation=hybrid` 混合切句）
+ **本地 llama-server 翻译**（日语 Sakura-7B；英语按 `translate.local.model_by_lang.en`
路由到 Hy-MT2-7B）。界面「AI 字幕」页装完模型会自动接好配置（R55 起切换模型自动重启服务）。

| 组合 | ASR | 翻译 | 显存约 | 说明 |
|---|---|---|---|---|
| 出厂默认 | Qwen3-ASR-0.6B | 本地 llama-server：Sakura-7B / Hy-MT2-7B | 6–8GB | 开箱即用，不联网 |
| 生产实测推荐（R98） | Qwen3-ASR-1.7B | 本地：Sakura-1.5B | 约 6.9GB | 召回 / 精度 / 盲评全面胜 0.6B；**1.7B + 7B 同卡会爆压**（实测单块 351s，不可用） |
| 云端翻译（可选） | 任意 | `translate.backend=openai` 兼容 API | 仅识别部分 | 慢机器备选；服务端会建议 25s 大块（见 §2） |

切换组合：改 `data\subtitle_config.json`，或环境变量临时覆盖：
```bash
ASR_MODEL=... TRANSLATE_BACKEND=openai .venv/Scripts/python.exe run_server.py
```
云端翻译需要 key（见 `translate.openai.api_key_env`，如 `DASHSCOPE_API_KEY`）。

## 4. 术语表（已移除）

术语表/热词功能已随 Round 53 整体删除：词表文件、`config.json` 的 `glossary` 段与
`asr.use_glossary_context` 键、`/glossary*` 接口都不复存在，README 不再描述其行为。

## 5. 验证与评测脚本

```bash
# 单机验证（vendor/subtitle/tools/）
.venv/Scripts/python.exe tools/fetch_models.py          # 下载模型（含 modelscope bug 绕过）
.venv/Scripts/python.exe tools/translate_test.py --input out/X.json    # 单跑翻译后端
# 评测（tests/；评分口径改动前先读 tests/score_combo.py 文件头）
.venv/Scripts/python.exe ../../tests/run_eval.py              # 单组合真片评测
.venv/Scripts/python.exe ../../tests/run_combo_eval.py        # ASR×翻译组合矩阵
```

## 6. 实测数据

- 模型组合矩阵评测（0.6B/1.7B × 翻译档：召回 / 精度 / 盲评 / 显存）：
  `docs/dev-archive/2026-09-25-AI字幕模型组合评测报告.md`。
- 评分口径大修（R98）、幻觉门控与 2s 延迟档位（R101）、混合切句上线（R63）：
  见仓库根 `iteration_shturl.md`。

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
- 分块时长按服务端 `recommended_chunk_sec` 建议（本地后端 3 秒块，云端 25 秒块，
  均带重叠）POST `/transcribe`；跨块用"时间重叠 + 最长公共子串"去重
- seek/暂停/切集：影子解码器自动重新对齐（漂移 >3 秒触发），离开播放页停止引擎

## 8. 已知限制

- 跨块去重在客户端按"时间重叠 + 最长公共子串"判定，仍属启发式；边界处偶有轻微重复或截断。
- 0.6B 档的日语专有名词错误明显多于 1.7B（这是默认档的代价）。
- 尚无 `.srt` 缓存（重复播放/回拖会重算）；局域网鉴权是**可选的预共享令牌**
  （`server.auth_token` + 请求头 `X-FSC-Subtitle-Token`），不配置时不校验。
- AC3/EAC3/DTS 音轨在 Quest 端（ExoPlayer 无 FFmpeg 扩展）无法解码 → 无音频也就没有字幕。
