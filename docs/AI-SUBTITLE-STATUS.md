# AI 实时字幕 —— 状态与运维手册

> 更新：2026-09-16（流式链路接手日）
> 范围：AI 字幕一条链路（VR 头显 ↔ PC Nexus ↔ audio.cpp ASR）+ 翻译质量
> 事实来源：本文所有"实测"均来自真实运行；上游行为均读自 audio.cpp 源码（v0.7.4 二进制 + PR#553 源码克隆）

## ⚠️ 先读这个：运行形态决定一切

**用户日常运行的是打包版 `dist-app\FunScriptCast-Nexus.exe`，其字幕服务跑 `dist-app\vendor\subtitle` 的独立快照（run_server.py、系统 Python）**——仓库 `vendor\subtitle` 里的修改对它无效，必须同步文件到打包目录、清 `dist-app\cache\subtitles\`、再经宿主 API（POST :8791/api/subtitle/start）或界面"结束并重启"拉起。完整流程见 README「⚠️ 运行形态」一节。
2026-09-16 一整轮修复因此延迟生效（陈旧实例 8164 + 打包快照不同步），此教训列为排障第一优先级（§6.0 / §6.0.5）。

---

## 0. 一句话现状

**流式实时字幕已打通并在真机验证**：头显 3s 一块推 PCM → PC audio.cpp v0.7.4 流式会话识别 → 原翻译引擎翻中文 → SSE 增量回推 → 头显浮层。
**v1.6.11（2026-09-16）流式端点退役，改走离线 VAD 端点**：全片评测发现定长块推给
`/transcribe/stream` 的根本缺陷——流式不返回时间戳，逐句时间只能按字数估算（中位偏差
−2.2s），浮层按视频时间窗选行时"提前出现/闪得快/后面不显示"都源于此。改为头显 10s 块
直接打 `/transcribe`（离线 VAD 路径，GPU 8081）：VAD 分段给出**真实时间戳**（中位偏差
−0.24s，±0.5s 内 61%），解码质量同级、断句自然。浮层显示保持从 start+3s 改为
**end+4s**（旧行刚到手就已"过期"是"后面不显示"的根因）。PC 侧配套：
8081 注册 offline+streaming 双模型（流式备用），8756 的 audiocpp 配置指向 8081/CUDA
（不再自起 8083 CPU 实例），`/transcribe` 补漏译置空策略（与桥一致）。
翻译缓存按用户要求关闭（`translate.cache=false`）；术语表恢复开启。

**v1.6.9/10（同日）删除影子播放器**：`volume=0` 在 Quest 音频路径上静音不彻底
（用户录屏听到一前一后两份声音），改为直接在主播放器 AudioSink 链挂透传
PcmTapProcessor 抽 PCM；过期判据改"落后多少"；流式默认开。

此前同日落地：

1. **§空译文问题（唯一用户抱怨）已双端修复**：
   - PC 侧（方案B）：桥内句子**攒满 batch_size=10 或流结束时整批翻译**，贴合 translate_engine 的批量/纠错机制；
     同时删掉了桥里一段**死代码**（曾把每个 delta 整段重复翻译一遍且结果不用 ⇒ 每块对 Ollama 打两次翻译，白耗一倍时间）。
   - 头显侧（方案A）：`AiSubtitleController.cs` 空译文整行**不上屏**（原回退显示日文原文），日志打 `[AiSubtitle] 跳过空译文行`。
   - 翻译引擎侧（同日未提交工作）：中文式 JSON 归一化、空译文判不合格、术语表直接修补、逐条兜底（并发4）、历史最好一轮保全、`translate_segments` 永不抛异常——空译文的根因多层堵住。
2. **audio.cpp 上游调研结论**（决定"不再升级/不改协议"）：
   - **v0.7.4 就是最新 release**（2026-09-13），我们已在用。
   - `/v1/audio/transcriptions/live` 持久推流端点**只在未发布源码**（PR#553，09-15），要用上必须自己编译，收益仅省每请求开销，**不值得**。
   - **流式模式上游明确不支持时间戳**：`qwen3_asr/session.cpp` `start_stream()` 对 `return_timestamps` 直接抛
     `Qwen3 ASR streaming does not support return_timestamps`。词级时间只有离线 + ForcedAligner。
     ⇒ 桥里"语速 130ms/字 + 单调游标"的推算时间轴是流式下的唯一选择，不是欠账。
   - **每个 HTTP 请求独立会话**：`run_streaming_model_impl` 每请求 `prepare` + `start_stream→reset()`，
     跨请求（跨 3s 块）没有上下文；"流式会话"的上下文只在请求内部窗口间（默认窗口 30s，feed 1s）。
     3s 块必然每请求恰好 1 个 delta（finalize 时统一出）。

## 1. 全片评测（2026-09-16，SIVR-001 全片 1254s / 人工字幕 124 条，无术语表、无翻译缓存）

评测方法：PC 端完整复现头显行为推流（`tests/stream_to_json.py`，含重叠步进 + 客户端 LCS 去重），
对照 `tests/compare_with_reference.py`（输出 vs 人工字幕）与 `tests/analyze_fidelity.py`（ASR 原文 vs 输出字幕）。

### 最终基线（2026-09-16 迭代完成后，Sakura-1.5B MT + 全部修复）

| 指标 | SIVR-001 全片 | SIVR-002 全片 |
|---|---|---|
| 识别召回 | **88.7%** | **92.1%** |
| 译文内容覆盖 | 0.481（中位 0.500） | 0.512（中位 0.455） |
| 高度一致行（≥0.8） | 17.9% | 22.6% |
| 硬缺陷（空/假名/夹英文） | 0/0/0 | 0/0/0 |
| 时序中位 | −246ms | −182ms |
| 漏识（对照人工） | 7/124（修复前 18） | 7/126 |

### 块长矩阵（桥修复前）

| 指标 | 流式 3s/1s | 流式 10s/2s | 流式 30s/2s | 离线 25s/2s |
|---|---|---|---|---|
| 与人工配对（召回） | 72.6% | 41.9%（时间标签伪影，见下） | 27.4%（同） | **81.5%** |
| 可信配对译文内容覆盖 | 0.401 | **0.433** | 0.238 | 0.362 |
| 语气词噪音行（ASR 把 うん 转成"嗯。"直通上屏） | **61 行** | 9 | 3 | 1 |
| 空译文 | 0 | 0 | 8（喘息"あ"×100 污染） | 0 |
| 假名残留 | 1 | 1 | 0 | 2 |
| 字幕滞后 | ~5s | ~12s | ~33s | 25s+（无提前量后不可用） |

**结论：定 10s/2s。** 3s 块切碎句子 + 小批翻译噪音多；30s 滞后 33s+ 且覆盖最差（喘息污染批次）；
10s 覆盖最高、滞后可接受。头显 `STREAM_CHUNK_SEC=10 / STREAM_OVERLAP_SEC=2`（v1.6.10 起）。
keep_from 修复后补测（全片，Sakura-1.5B）：5s/1s → 召回 90.3%/覆盖 0.490/长度比 1.31（偏啰嗦）；
15s/2s → 87.9%/0.461/长度比 1.12（最干净）；10s/2s → 88.7%/0.481——**10s 均衡最优结论维持**。

### 桥修复（评测驱动，均已落地 stream_bridge.py）

1. **重复退化过滤**：喘息被 ASR 转成 "あ"×100 时整句丢弃（判据用 `text_filters.has_repetition_loop`，
   与离线路径同一实现），消灭整屏"啊啊啊"和空译文。
2. **漏译复读置空**：引擎多轮纠错+逐条兜底后仍夹假名的段（`error=untranslated_leak`），若译文里
   连一个汉字都没有＝整句复读，置空让头显跳过（宁缺不_show 日文）；部分夹字保留。
3. **块内字数比例摊时**（替换 130ms/字）：旧法把整块句子压在块首，30s 块能标早 15s+——
   矩阵里 10s/30s"召回暴跌"主要是这个伪影。比例摊后 10s 配对召回 **41.9% → 87.1%**。
   时序中位 −2.7s（宁早勿晚）；精确时间轴仍需离线 + ForcedAligner（上游流式不支持时间戳）。

### 术语表 / 缓存

- 术语表对全片翻译质量**中性**（有表 0.404 vs 无表 0.424，假名漏译也不因此消失——
  走单条兜底的句子不经过术语表修补），但人名一致性（悠亜→悠亚）LCS 测不出来，**保留开启**。
- 免费兜底后端（Google）偶发输出**繁体**（实测 1 句"有時候覺得…"），量极少，暂不处理。
- **翻译缓存按用户要求关闭**（`translate.cache=false`）。注意：缓存命中路径会跳过漏译标记，
  若将来重开缓存，桥的复读置空依赖 error 标记，需确认 `_cache_get` 命中路径也补标记。

## 2. 早期有界评测（300s 窗口，已被全片评测取代，数据留档）

## 3. 协议规格

### 头显 → Nexus

```
POST http://<PC-IP>:8756/transcribe/stream?lang=ja&translate=true&video_start_ms=<ms>
  Content-Type: application/octet-stream
  body = 裸 PCM（s16le / 16kHz / 单声道）
← text/event-stream
  data: {"type":"delta","ja":"…","zh":"…","first_ms":…,"start_ms":…,"end_ms":…}
  data: {"type":"done","ja":"…","timing":{"ttft_ms":…},"total_ms":…}
  data: {"type":"error","error":"…"}
  data: [DONE]
```

- `ja`=切句后原文；`zh`=整批翻译后的中文；`start_ms/end_ms`=视频绝对时间（`video_start_ms`+语速游标）。
- Kotlin 侧映射 `{text, translation, source_text, start_ms, end_ms}`，`text = zh非空 ? zh : ja`（C# 侧现已不显示 zh 为空的行）。

### Nexus → audio.cpp（上游硬性要求，实测）

1. **只收 WAV 容器**（裸 PCM 400）——桥内 `_wav_wrap()` 内存补 44 字节头。
2. 模型必须 `"mode": "streaming"` 注册，否则回 `Qwen3 ASR currently supports offline sessions`。
3. 流式开关是 **`stream=true`**（无 `/live` 路由，见 §0）。
4. `context`（热词）参数有效（解码确定、带热词输出稳定不同）；`prompt`/`hotwords` 被忽略。

### 服务配置与启动

```json
// E:\Development\_ref\audiocpp-asr-stream.json —— 8081 / cuda / qwen3-asr-stream (mode=streaming, lazy_load)
```

```powershell
# ASR 流式实例（8081）
Start-Process 'E:\audiocpp-portable\gpu\audiocpp_server.exe' -ArgumentList @('--config','E:\Development\_ref\audiocpp-asr-stream.json','--host','127.0.0.1','--port','8081','--device','0') -WorkingDirectory 'E:\audiocpp-portable\gpu' -WindowStyle Hidden
# Ollama（11434，qwen2.5:3b）——手动起字幕服务时必须手动一起起，否则翻译熔断全变日文
Start-Process "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" -ArgumentList 'serve' -WindowStyle Hidden
# 字幕服务（8756；lifespan 还会按 config 自起一个 8083 的 CPU audiocpp 离线实例，与 8081 互不干扰）
cd E:\Development\FunScriptCast-Nexus\vendor\subtitle
E:\Development\FunScriptCast-Nexus\.venv\Scripts\python.exe -m uvicorn server_app:app --host 0.0.0.0 --port 8756
```

## 4. 验证命令

```powershell
# 三服务健康（Ollama 无 /health，404=活着）
foreach ($p in 8081,8756,11434) { try { Invoke-RestMethod "http://127.0.0.1:$p/health" -TimeoutSec 3 } catch { "port $p down" } }

# 流式全链路：期望 delta 全部 zh 非空、无假名、start_ms 单调
curl.exe -N -sS --max-time 240 -X POST 'http://127.0.0.1:8756/transcribe/stream?lang=ja&translate=true&video_start_ms=45000' -H 'Content-Type: application/octet-stream' --data-binary '@E:\Development\_ref\ja60.pcm'

# 真机三日志一起看
adb -s 192.168.2.129:5555 logcat -d | Select-String 'AiSubtitle\] \+|跳过空译文行|SubtitleOverlay\] show|上传器心跳'
```

测试素材：`E:\Development\_ref\ja60.pcm`（60s）、`c3/c6/c12/c25.pcm`（分块）、`SIVR-001.mkv`（源视频）。

## 5. 参考项目研究（2026-09-16，用户指定；克隆在 `E:\Development\_ref\ref-projects\`）

| 项目 | 栈 | 可借鉴 | 结论 |
|---|---|---|---|
| **sub-title**（本地实时字幕） | Python/Qt，多 ASR 引擎 | **VAD 静音切句**：RMS 能量检测，"缓冲≥最短时长 且 尾部静音≥阈值"即切分句，纯静音段直接跳过不推理。Qwen3-ASR 用"段式伪流式"（和我们离线路径同思路）；翻译 worker 是**逐句**翻译 + 200 句 LRU 去重 | **已部分采纳**：定长块改打离线 VAD 端点（见 §0）；纯客户端 VAD 切句做过全片对照实验，时序极准（−0.14s）但碎片翻译把内容覆盖拖到 0.22~0.28，不如"10s 块 + 服务端 VAD"，暂不引入头显 |
| **auto-caption**（桌面字幕） | Electron + Python 子进程 | 引擎子进程 + JSON stdout/TCP 协议；gummy/vosk/glm 多后端 | 架构参考，暂无可直接搬的点 |
| **LiveSubtitles**（桌面字幕） | C++/Qt | vosk + CTranslate2 本地 CPU 栈 | 全离线 CPU 路线，质量上限低，不跟随 |
| **realtime-subtitle**（Vanyoo，桌面实时字幕） | Python，whisper/MLX/FunASR 三后端 | 见下方 FunASR 细节与可落地清单 | 引擎本身不建议换（paraformer 系中文特化、SenseVoice ja 精度未必优于现有）；**两个零风险细节值得抄**，见下 |

**realtime-subtitle 的 FunASR 板块要点**（FUNASR_GUIDE.md + transcriber.py）：
- 模型：paraformer-zh/-streaming（中文）、paraformer-en、SenseVoiceSmall（多语含 ja，234M，CPU 可跑）、
  Fun-ASR-Nano（31 语，800M）；FunASR 内建 VAD/标点/情绪/说话人分离（AutoModel 挂 vad_model/punc_model）
- 热词机制：`model.generate(..., hotword=prompt)`，**自动把上一句转写结果作为热词上下文喂回去**
  （prompt-based bias）——治人名/专名听错，与 audiocpp 离线路径的 context 参数同思路
- 幻觉防护：同词重复 >4 判幻觉；词级信息密度（unique/total < 0.4）判幻觉；**输出是热词/prompt 的
  回显或尾部子串即丢弃**（与我们的 is_glossary_echo 同族，endswith 检查是补充）
- 翻译层：OpenAI 兼容 LLM（可指向 Ollama），**带上下文承接**（previous_text + previous_translation
  一并进提示词，解决句子间主语断裂）；剥 `<think>` 标签兼容推理模型

**可落地清单（2026-09-16 已实施 1~3，4 为远期项）**：
1. ✅ 翻译上下文承接：`translate_segments(segs, lang, context)` 新增 context 参数（**只拼进 user
   提示词、不进 system**——缓存键含 system 哈希，进 system 会让缓存随剧情滚动失效）；server_app
   滚动 `_LAST_CTX`（上一块原文+译文各截 80 字），stream_bridge 同款
2. ✅ ASR 热词追加上一句原文（截 60 字）：两个后端 `transcribe(..., extra_context=)` 都已接；
   audiocpp 的复读重试判定同步补 `text_filters.is_prompt_echo`（上一句回显也触发无热词重试）
3. ✅ `text_filters.is_prompt_echo`：规范化后与 prompt 全等或为其尾部子串即判回显
4. ⏳ 远期：SenseVoiceSmall（ja）做 CPU 备用引擎 / Fun-ASR-Nano，精度需先实测对比 Qwen3-ASR

### 云端接入调研（2026-09-16，等用户提供 API Key 后才动手）

| 痛点 | 云端方案 | 改动量 |
|---|---|---|
| 翻译不对 | qwen-mt-turbo / qwen-flash（DashScope） | **配置翻转级**（translate_engine 已支持 backend=openai，config 已预置） |
| ASR 听错（人名/同音） | DashScope qwen3-asr-flash（本地同家族云端版，按块文件式调用） | 加一个 ASR 后端分支 |
| 延迟 | Gummy 实时语音直出中文（auto-caption gummy.py 是完整参考） | 大（持久会话重构） |

- **零代码通路已内置**：translate_engine 支持 `backend="openai"`，config 已预置 DashScope
  compatible-mode + qwen-mt-turbo + api_key_env=DASHSCOPE_API_KEY。注意 qwen-mt 系对
  "JSON 批量指令"服从性未验证；备选 qwen-flash 或 qwen-mt 逐句模式（MT 模式已具备）
- 免费兜底已有（Google gtx + Bing），Google 偶发输出繁体
- 隐私权衡（音频/文本出境）必须用户拍板；成本量级：翻译每部片几毛~几块，ASR 每小时几块（以官网为准）

### 翻译特化模型 Sakura（2026-09-16 已接入，逐句 MT 模式）

`translate.mt_system`（+ 可选 `mt_user_prefix`）配置非空即启用**逐句 MT 模式**：
翻译特化模型（Sakura 系，galgame/轻小说领域微调，Qwen2.5 底座）按"单文本 +
专用系统提示词"直翻，带缓存/术语表修补/漏译退化检查，并发 thread_num 路。
推荐提示词与采样参数（temp 0.1 / top_p 0.3）来自 https://github.com/SakuraLLM/SakuraLLM 。
实测 1.5B（180s 切片）：覆盖 0.267→0.392、速度持平。注意：
① 许可 CC BY-NC-SA 4.0（禁商用，发布机翻需显著标注）；
② 风格偏意译，偶有增译（评测可见"脱下这条内裤"为原文所无），漏译/退化判据保留兜底；
③ GGUF 用 hf-mirror.com 下载（直连 HF 不可达）；④ 大 GGUF 下载需 `--proxy ""`（系统代理残留会劫持）。

**关键数据（SIVR-001 全片，对照人工字幕）**：

| 方案 | 召回 | 内容覆盖 | 时序中位 | 滞后 |
|---|---|---|---|---|
| 流式端点 10s 块（v1.6.10，估算时间） | 88.7% | 0.367 | −2230ms | ~12s |
| 客户端 VAD 切句（激进 0.35s/1s） | 71.8% | 0.227 | **−140ms** | ~3-6s |
| 客户端 VAD 切句（保守 0.6s/4s） | 79.8% | 0.275 | −860ms | ~5-9s |
| **离线 VAD 端点 10s 块（v1.6.12 定案）** | 80.6% | 0.354 | **−240ms（±0.5s 内 61%）** | ~12s |
| 离线 25s 块 | 81.5% | 0.362 | −230ms | 25s+ |

评测工具：`tests/stream_to_json.py`（含 `--vad` 模式）、`tests/offline_to_json.py`、`tests/analyze_fidelity.py`、`tests/compare_with_reference.py`。
产物在 `E:\Development\_ref\eval\sivr001_*.json`。

## 6. 踩坑速查（历史验证过，别再踩）

0. **8756 上挂着宿主拉起的陈旧实例 = 一切修复"看起来无效"**（2026-09-16 真机排查半天的根因）：头显会话经
   `/api/subtitle/start` 让宿主拉起服务，该进程会一直存活并持有 8756；之后磁盘上所有代码/配置修改它都**不知道**，
   而 Nexus UI 会警示"不是本程序启动的（PID xxx）"。排查：看 UI 警告 / 查进程 StartTime /
   `netstat -ano | findstr :8756`。改完代码必须**杀旧实例并经宿主 API 重启**（POST :8791/api/subtitle/start）。
0.5 **宿主是打包版（dist-app\FunScriptCast-Nexus.exe），它拉起的服务跑 `dist-appendor\subtitle` 的独立快照
   （入口 run_server.py），不是仓库代码**：仓库里改 vendor/subtitle 对它无效（快照缺新文件、config 独立、自带
   `dist-app/cache/`）。仓库修复完成后必须同步这些文件到 dist-app/vendor/subtitle 并清 dist-app/cache/subtitles，
   再经宿主 API 重启。判断当前进程跑的是哪份代码：`Get-CimInstance Win32_Process` 看命令行（run_server.py
   =打包快照；`-m uvicorn server_app:app`=仓库）。
1. **覆盖 exe 前先停进程**：旧进程占用时 `Copy-Item` 失败会被 `-EA SilentlyContinue` 吞掉 ⇒ "升级了还是旧版"。
2. **手动起字幕服务 = 绕过 host 编排** ⇒ Ollama 不会被拉起 ⇒ 翻译熔断 ⇒ 屏幕全日文。
3. **`Nexus\cache\subtitles\` 命中缓存完全不跑识别**：测实时链路前先清空。
4. **翻译缓存版本 `_CACHE_VERSION`**：改提示词/判据必须 +1，否则旧坏译文"命中"。
5. **漏译复读置空判据 = 假名计数 ≥2**（`text_filters.count_kana`，流式桥与离线路径共用）：不能用
   "有没有汉字"——术语表修补把人名换成汉字（悠亜→悠亚），整句日文掺两个汉字就绕过判据（实测开头
   第一句 'こんにちは、三上悠亚です。' 天天上屏）。
6. **离线路径与桥的显示策略必须一致**：引号剥离、同响应互为子串碎片去重（保留更长）、复读置空——
   server_app `/transcribe` 与 stream_bridge 各自调用，判据本体只在 text_filters。
5. **git-bash 调 powershell**：双引号里 `$env:XXX` 会被 bash 先吃掉（本次实测又踩一次）；`cmd /c` 要写 `cmd //c`。提示文本用单引号、路径用绝对路径。
6. **PowerShell `Set-Content -Encoding UTF8` 写 BOM** 会弄坏 Groovy/Java 解析；`tools/*.ps1` 反而必须带 BOM。
7. **桥/引擎职责边界**：漏译判定、重试、兜底全在 `translate_engine.py`，桥只做「封 WAV、切句、时间、转发、攒批」。不要在新代码里重复实现判据。
8. **判据只写一份**：PyTorch/audiocpp 两侧共用的文本判据统一放 `vendor/subtitle/text_filters.py`（历史上漂移过两次）。
9. **头显两 Unity 工程必须逐字节一致**：`unity-prototype`（Pico）与 `unity-prototype-meta`（Meta）各有全套 C#，改一份必须同步另一份，否则 `build_apks.ps1` 第 4 步 parity 门禁直接中止构建（本次实测被拦一次；同步后 OK）。

## 7. 未决 / 后续

| 项 | 状态 |
|---|---|
| 时间轴精确化 | **已解决（v1.6.12）**：改走离线 VAD 端点拿真实时间戳（中位偏差 −0.24s）； ForcedAligner 词级时间仍留作远期选项 |
| 翻译质量天花板 | 当前 ~0.35-0.42 内容覆盖的瓶颈是 **qwen2.5:3b**；下一个杠杆是换更大翻译模型（qwen2.5:7b 需显存预算评估） |
| 流式 vs 离线质量 A/B | **已完成有界评测（300s 窗口）**：硬缺陷与译文内容打平，差距全在时间轴；全片评测可复用同一套脚本 |
| 影子播放器 | **已删除（v1.6.9）**：双声音根除、省一整份解码；代价是无提前量、字幕必然滞后。落点 `VideoPlayerBridge.ensurePlayerInstance`（tap 常驻主播放器 AudioSink）+ `AiSubtitleEngine.beginCapture/stopInternal`（消费者注入/摘除） |
| 头显装机 | 装机命令 `C:\platform-tools\adb.exe -s 192.168.2.129:5555 install -r VRFunScriptCast\dist\VRFunScriptCast-Meta.apk`（**v1.6.9 已于 2026-09-16 装机**，versionCode 111 回读确认） |
| 上游新版本 | 关注 release；`/live` 端点进入正式版后再评估（届时桥可改持久推流，省每请求会话重建） |
| 工作区未提交 | Nexus 与 VRFunScriptCast 两仓都有大量未提交工作（见 git status），建议按功能分批提交 |

## 8. 关键文件

```
PC（FunScriptCast-Nexus）
  vendor\subtitle\stream_bridge.py      ★ 流式桥（攒批/切句/时间/WAV 封装/错误透出）
  vendor\subtitle\server_app.py         末尾 /transcribe/stream 路由 + 空闲回收防误杀
  vendor\subtitle\translate_engine.py   翻译引擎（批量/纠错/最好一轮/逐条兜底/术语修补）
  vendor\subtitle\text_filters.py       两侧共用文本判据（退化复读/热词复读）
  vendor\subtitle\audiocpp_backend.py   离线路径后端（热词 context 转发、Job Object 带走子进程）
  host_server.py                        按需拉起模型（缓存 :155/:192）
  tests\compare_with_reference.py       与人工字幕的质量对比工具

头显（VRFunScriptCast）
  funscriptcore\...\engine\AiSubtitleEngine.kt     流式上传分支（SSE 解析、STREAM_MAX_LAG_MS）
  unity-prototype-meta\...\AiSubtitleController.cs  空译文行不上屏（§8.1 方案A，2026-09-16）
  unity-prototype-meta\...\SubtitleOverlay.cs       浮层（AI 模式分支）

上游参考
  E:\audiocpp-portable\                 v0.7.4 便携包（gpu\ 已含 .bak-0712 可回退）
  E:\Development\_ref\audio.cpp\        源码克隆（PR#553，比 v0.7.4 新两天）
  E:\Development\_ref\audiocpp-asr-stream.json   流式服务配置
```
