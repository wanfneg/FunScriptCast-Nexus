# FunScriptCast-Nexus

本项目由我个人借助 AI 工具开发。个人精力的付出、AI 工具的使用都有成本，而项目**免费**提供给大家使用。

欢迎大家体验，并提出 **BUG 反馈、功能需求、优化建议**。也欢迎大家投喂白饭给大肥鱼，助力项目开发。

<p align="center">
  <img src="docs/sponsor/meme.jpg" alt="梗图" width="420">
</p>

<p align="center">
  <img src="docs/sponsor/wechat.png" alt="微信赞助" width="300">
  &nbsp;&nbsp;&nbsp;
  <img src="docs/sponsor/alipay.jpg" alt="支付宝赞助" width="300">
</p>

**沟通渠道**：QQ `2831691505`
---
面向 VR 观影的 PC 端控制中枢：**DLNA 媒体服务 + AI 实时字幕 + 设备同步**，一个窗口、一个托盘。

- 源码仓库（私有）：<https://github.com/wanfneg/FunScriptCast-Nexus>
- 安装包下载：[Releases](https://github.com/wanfneg/FunScriptCast-Nexus/releases/latest)

原 `VR-DLNA` 与 `Subtitle Server` 两个独立项目已 vendor 进本仓库（`vendor\dlna`、`vendor\subtitle`）。

## 功能

**DLNA 媒体服务**（vendor/dlna，纯标准库）
- SSDP 自动发现 + UPnP ContentDirectory，DeoVR 兼容（UTF-8 文件名、外挂字幕 srt/ass/vtt 切换）
- 媒体根目录界面管理，路径引号/空白自动规整

**AI 实时字幕**（vendor/subtitle，FastAPI）
- 识别：Qwen3-ASR（audio.cpp 常驻推理，日语 / 英语），安装包**内置 CPU 运行时**，零手工安置
- 切句：混合切句——静音定界 + 语音检测赋时，整句出字、时间戳准
- 渐进出字：句子未切出先发临时稿（partial），定稿自动覆盖
- 翻译：本地 llama.cpp（GGUF），按源语言路由——日语 Sakura / 英语 Hy-MT2-7B，档位自选、自动热切换
- 翻译预热：启动即付完系统提示词与首包账，首句字幕不等冷启动
- 模型中心：7 个条目界面内一键下载（ModelScope/HF 镜像直连 + 系统代理兜底 + 断点续传），带显存标注与组合估算

**设备同步**：脚本（.funscript）/ 视频经 adb 增量同步到 Quest / 手机

## 下载与安装（普通用户看这里）

从 [Releases](https://github.com/wanfneg/FunScriptCast-Nexus/releases/latest) 下载 `FunScriptCast-Nexus-Setup-*.exe`：

1. 安装向导选目录——**选空间充足的盘**（模型以十 GB 计），别用系统盘；
2. 启动后进「识别与翻译」卡下载识别模型 + 翻译模型（下载完自动切换配置）；
3. 「DLNA 服务器」添加媒体根目录；头显/播放器里设备名 **FunScriptCast-DLNA**；
4. 播放时在 PC 端「AI 字幕」卡启动字幕服务（默认空闲 5 分钟自动回收显存）。

用户数据（配置 / 模型 / 日志）全部落安装目录，C 盘一个字节不落；升级覆盖只动程序文件。

## ⚠️ 运行形态（改任何服务端代码前必读）

**字幕服务跑的永远是 `vendor\subtitle` 的"快照"，不是仓库工作目录**：

| 形态 | 服务代码来源 | 说明 |
|---|---|---|
| **安装版** | `dist-installer\*-Setup.exe` 安装后的目录 | 自包含：`runtime\`（embeddable Python + fastapi/uvicorn/numpy，约 111MB），装完即用 |
| **便携目录** | `dist-app\`（exe + ui + vendor + runtime） | 绿色版，拷走即用；也是构建产物 |

**开发/修复的固定顺序（跳步 = 改了白改，曾浪费一整轮）**：

1. 在仓库 `vendor\subtitle\` 改代码；`ui\` 同理；
2. 只同步代码：`powershell -ExecutionPolicy Bypass -File tools\sync_distapp.ps1`；
   改了 `host_server.py`（宿主本体）则必须重编：`build\build_exe.ps1`；
3. 部署到安装目录：把变更文件拷过去，**重启字幕服务**（改了宿主则重启整个应用）；
4. 排障三件套：字幕服务 `/health` 的 `code_sig`（代码签名，两份对不上 = 跑的旧代码）、
   UI 品牌区「（开发副本）」标记（exe 在 dist-app/Development 下自动亮明身份）、
   `Get-CimInstance Win32_Process` 看命令行分辨快照与源码。

**自带运行时的解释器解析**（host_server._subtitle_python）：`runtime\python.exe`（自包含安装）→
`.venv\`（应用目录 → 上一级）→ PATH 上的 python（多半缺依赖）。来源会记进启动日志。

## 目录结构

```
FunScriptCast-Nexus\
├── host_server.py            宿主：DLNA 控制 + 字幕子进程 + 托管前端 + JSON API
├── ui\                       前端（无框架，WebView2 渲染；字体/图标许可见 ui\fonts、ui\LICENSE-*）
├── vendor\dlna\              DLNA（vr_dlna.py）+ funscript/video 同步 + 托盘
├── vendor\subtitle\          AI 字幕服务
│   ├── server_app.py         /health /transcribe(/stream) /translate/*；混合切句调度
│   ├── hybrid_segmenter.py   混合切句状态机（静音定界 + VAD 赋时 + 段-组对齐）
│   ├── translate_engine.py   批量 JSON / 逐句 MT / 按语言路由 / 熔断与兜底
│   ├── llama_backend.py      llama.cpp 常驻翻译（热切换、Job Object 保护）
│   ├── audiocpp_backend.py   audio.cpp 常驻识别 + silero VAD（错误脱敏回局域网）
│   └── config.json           出厂模板（用户配置在 data\subtitle_config.json）
├── vendor\audiocpp\          audio.cpp CPU 运行时 + silero（fetch_audiocpp.ps1 拉取/内置安装包）
├── vendor\llama\             llama.cpp CUDA 运行时（fetch_llama.ps1 拉取；安装包不含，走下载条目）
├── tools\                    sync_distapp / fetch_* / 图标生成 / 下载器
├── build\                    build_exe.ps1 + nexus.spec（PyInstaller）
├── installer\setup.iss       Inno Setup 安装包脚本
├── docs\                     THIRD-PARTY-NOTICES.md（许可台账）、AI-SUBTITLE-STATUS.md、审查报告
├── tests\                    test_pipeline_unit.py（17 项）/ test_merge_shards.py / 托盘·窗口·前端检查
└── version.json              versionName / versionCode / channel
```

## 用户数据在哪（升级 / 重装 / 复位）

**全在你选的安装目录里，C 盘一个字节都不落**（连 `%TEMP%` 该干的活——子进程配置、
VAD 转储、下载暂存、WebView2 profile——都指到安装目录；唯一例外是 PyInstaller 单文件
EXE 每次启动往 `%TEMP%` 解包自己，约 35MB，退出即删）。

```
<安装目录>\
├── data\     subtitle_config.json（字幕配置/云端 key）、integrated_settings.json（DLNA 目录等）
├── models\   识别模型 + 翻译 GGUF + hf-cache\ + _download\（下载暂存，装完可删）
├── logs\     host.log + run_server.log + llama_server_*.log
├── run\      运行期临时文件（可随时删）
└── ui\ vendor\ tools\ runtime\   程序
```

| 想要 | 怎么做 |
|---|---|
| 升级 / 覆盖安装 | 数据自动保留（安装包不装也不删 `data\`，只在新装时铺出厂种子） |
| 彻底复位 | 关程序，删 `data\`——唯一复位入口 |
| 备份 / 换机 | 拷走 `data\`（几十 KB）；模型太大可不带 |
| 老版本升上来 | 首次运行自动迁移（`%APPDATA%` → `data\`，日志有 `[paths] 已迁移…`） |

配置走界面改，或直接编辑 `data\subtitle_config.json`。`vendor\subtitle\config.json` 只是
出厂模板；**用户配置是首次运行的快照**，新增配置键必须在代码里带默认值。路径规则只在
`vendor\subtitle\user_paths.py` 写一份（宿主与字幕服务共用）。

## 架构

```
┌─ 宿主进程（host_server.py，PyInstaller 单文件）──────────┐
│  · 前端托管 + JSON API        127.0.0.1:8790            │
│  · DLNA（import vendor/dlna）      0.0.0.0:8899          │
│  · 头显专用接口（白名单四路由）    0.0.0.0:8791          │
│  · pywebview 窗口（WebView2）+ 托盘                      │
└──────────────┬───────────────────────────────────────────┘
               │ 子进程（停止即回收显存）
       ┌───────▼───────────────────────────────┐
       │ 字幕服务 vendor/subtitle   :8756       │
       │ 识别 audio.cpp :8081（CPU/CUDA 可选）  │
       │ 翻译 llama.cpp :8082（按语言热切换）   │
       └───────────────────────────────────────┘
```

- **字幕服务独立子进程**：崩溃不拖垮主程序；停止即释放显存；空闲 5 分钟自动回收退出（可配）。
- **识别**（audio.cpp）：混合切句切出的整段交常驻服务转写，silero VAD 裁决语音区做对齐；
  只注册一个 streaming 模型 id，离线请求同 id 复用（注册两个 = 权重驻留两遍，实测慢 6 倍）。
- **翻译**（llama.cpp）：按请求语言热切换 GGUF（`translate.local.model_by_lang`），切换失败自动回滚；
  批量 JSON + 键校验纠错 + 失败熔断；云端 OpenAI 兼容后端可选（不碰厂商专有协议）。
- **显存档位估算**：/health 回 `vram_estimate`（识别 + max(日译, 英译) + 固定开销），8GB 卡选组合前先看它。

## 打包与发布

```powershell
powershell -ExecutionPolicy Bypass -File build\build_exe.ps1      # exe + 组装 dist-app（自动递增版本号）
powershell -ExecutionPolicy Bypass -File build\build_installer.ps1 # 串联 ISCC 出安装包
```

| 情形 | 行为 |
|---|---|
| 全新安装 | 选目录（选大盘）；`data\` 只铺出厂种子 |
| 升级安装 | 同 AppId 沿用目录，只覆盖程序文件（约 110MB），`data\ models\ logs\` 不动 |
| 降级保护 | 旧包默认拦下；运行中升级由互斥量引导先关程序 |

发行：安装包与两个运行时 zip 发布到**本仓库 Releases**（2026-09-22 起源码与分发同仓，
单一项目）。`tools\fetch_llama.ps1` / `tools\fetch_audiocpp.ps1` 从该仓库
复现两个运行时目录。构建前 `adb kill-server`（构建期文件锁死过一回）。

## API（宿主 :8790，节选）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/state` | 全量状态（DLNA / 字幕 / 同步 / GPU / 事件） |
| GET/POST | `/api/settings` `/api/subtitle/config` | 设置与字幕配置（key 打码回显） |
| POST | `/api/dlna/start|stop` `/api/subtitle/start|stop|reclaim` | 服务控制 |
| GET | `/api/models/catalog` | 模型目录（状态/进度/显存标注） |
| POST | `/api/models/download` | 发起模型/运行时下载 |
| POST | `/api/sync/run` | 设备同步（kind=script/video） |
| POST | `/api/quit` | 退出 |

字幕服务（:8756）：`GET /health`（含 code_sig / segmentation / vram_estimate / mt_warm）、
`POST /transcribe?lang=&video_start_ms=&partial=`、`POST /transcribe/stream`（SSE）、
`GET /translate/stats`、`GET /translate/selftest`。

## 第三方与许可

第三方组件台账与许可证文本见 [docs/THIRD-PARTY-NOTICES.md](docs/THIRD-PARTY-NOTICES.md)
（分发物、运行时下载、参考项目一览）。发行物不含 GPL 组件；详细协议与排障史见
[docs/AI-SUBTITLE-STATUS.md](docs/AI-SUBTITLE-STATUS.md)。
