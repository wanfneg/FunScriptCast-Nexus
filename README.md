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

面向 VR 观影的 PC 端控制中枢：**DLNA 媒体服务 + AI 实时字幕 + 设备联动**，一个窗口、一个托盘。

仓库：<https://github.com/wanfneg/FunScriptCast-Nexus>（`main` 分支，tag `v1.0.0`）

原 `VR-DLNA`（抚物器）与 `Subtitle Server` 两个独立项目已 **vendor 进本仓库**，不再依赖 `E:\Development` 下的其他目录。

> **推送注意**：本机 git 全局配了 `http.proxy=http://127.0.0.1:7897`（代理当前未开启），
> 直连 GitHub 可用但走代理会失败。推送时显式清空代理：
>
> ```powershell
> git -c http.proxy= -c https.proxy= push origin main
> ```
>
> （`git config --local http.proxy ""` 无效——git 把空值当未设置，会回退到全局配置。）

## ⚠️ 运行形态（改任何服务端代码前必读）

**用户的日常运行形态有两种，共同点：字幕服务跑的都是 `vendor\subtitle` 的"快照"，不是仓库工作目录**：

| 形态 | 服务代码来源 | 说明 |
|---|---|---|
| **安装版（推荐）** | `dist-installer\*-Setup.exe` 安装后的 `vendor\subtitle` | 自包含：内嵌 `runtime\`（embeddable Python + 依赖，audiocpp 模式不需要 torch），装完即用，支持目录选择/桌面快捷方式/开机自启/卸载器 |
| **便携目录** | `dist-app\`（exe + ui + vendor + runtime） | 绿色版，拷走即用 |

**开发/修复字幕服务的固定顺序（跳步 = 改了白改，2026-09-16 一整轮修复因此"看起来无效"）**：

1. 在仓库 `vendor\subtitle\` 改代码与配置；
2. 重新打包：`powershell -ExecutionPolicy Bypass -File builduild_installer.ps1`（自动串联 PyInstaller →
   自带运行时 → Inno Setup；需要 ISCC.exe，winget 装 `JRSoftware.InnoSetup` 即可，中文语言包随仓库分发并自动装入编译器目录）；
3. 快速验证（不重打安装包）时，把变更文件**同时**拷到 `dist-appendor\subtitle\`（和已安装目录的
   `vendor\subtitle\`），再清 `cache\subtitles\`，重启服务；
4. 判断当前进程跑的是哪份代码：`Get-CimInstance Win32_Process` 看命令行——`run_server.py` = 快照
   （安装版/便携版），`-m uvicorn server_app:app` = 仓库源码。Nexus 界面出现"不是本程序启动的（PID xxx）"
   黄色警告 = 端口被外来进程占用，点"结束并重启"。

自带运行时的解释器解析优先级（host_server._subtitle_python）：`runtime\python.exe`（自包含安装）→
`.venv`（应用目录 → 上一级目录，非自包含的旧形态）→ PATH 上的 python（多半缺依赖）。
`vendor\subtitlesr_engine` 的 torch/qwen_asr 已懒加载：轻量运行时只带 fastapi/uvicorn/numpy
（约 79 MB），audiocpp 主路径不需要 torch。

详细协议与排障见 [docs/AI-SUBTITLE-STATUS.md](docs/AI-SUBTITLE-STATUS.md)。

## 赞助与支持

## 运行

```bat
start.bat
```

或用自带 venv 直接跑：

```powershell
cd E:\Development\FunScriptCast-Nexus
.\.venv\Scripts\python.exe host_server.py
```

调试（不开窗口，只起 API）：

```powershell
.\.venv\Scripts\python.exe host_server.py --no-window
```

可用环境变量覆盖：`NEXUS_PY`（Python 路径）、`FS_HOST_PORT`（默认 8790）、`FS_SUBTITLE_PORT`（默认 8756）、`VRDLNA_DIR` / `SUBTITLE_DIR` / `ASR_MODEL`。

## 目录结构（自包含）

```
FunScriptCast-Nexus\
├── host_server.py          宿主进程：DLNA 控制 + 字幕子进程 + 静态托管 + JSON API
├── start.bat               启动器
├── ui\                     前端（index.html / styles.css / app.js，无框架）
├── design\                 设计交付
│   ├── DESIGN_SPEC.md      设计规范（Token / 布局 / 组件 / 动效 / 性能预算）
│   ├── prototype.html      高保真交互原型（单文件，双击即看）
│   ├── architecture.html   进程架构图（可交互）
│   ├── architecture.json   架构图源（Archify 9/9 校验）
│   └── _selfcheck.js       原型结构化自查
├── vendor\dlna\            DLNA 服务（原 VR-DLNA，纯标准库）
│   ├── vr_dlna.py          MediaLibrary / DlnaApp / DlnaHTTPServer / SSDPServer
│   ├── funscript_sync.py   脚本文件夹同步
│   ├── video_sync.py       视频文件夹同步
│   └── tray_icon.py        托盘图标
├── vendor\subtitle\        AI 字幕服务（原 Subtitle Server，FastAPI）
│   ├── server_app.py       /health /transcribe /glossary
│   ├── asr_engine.py       Qwen3-ASR + ForcedAligner
│   ├── translate_engine.py 翻译调度（local / ollama / openai 三种可插拔后端）
│   ├── llama_backend.py    本地翻译模型：按需拉起并复用 llama-server（直读 GGUF）
│   ├── glossary.py         术语表（mtime 热加载）
│   └── config.json         ASR / VAD / 分段 / 翻译配置
├── vendor\llama\           llama.cpp（llama-server.exe + CUDA 运行时，约 1.1 GB）
├── models\                 安装目录模型：ASR 约 8 GB + 翻译 Sakura GGUF 约 5 GB
├── .venv\                  5.0 GB：torch(cu128) + transformers + fastapi + pywebview
├── version.json            版本号（versionName / versionCode / channel）
├── tools\make_icon.py      生成托盘 / 窗口图标（icon.ico + png 多尺寸）
└── tests\                  自动化测试（ui_check.js / test_tray_run.py / test_frameless.py）
```

## 架构

```
┌─ 宿主进程（host_server.py）──────────────────────────────┐
│  · 托管前端静态资源 + JSON API   127.0.0.1:8790          │
│  · DLNA 服务（import vendor/dlna）  0.0.0.0:8899         │
│  · pywebview 窗口（WebView2）                            │
└───────────────┬──────────────────────────────────────────┘
                │ subprocess.Popen / terminate
        ┌───────▼──────────────────────────────┐
        │ 字幕服务子进程（vendor/subtitle）     │
        │ FastAPI  127.0.0.1:8756              │
        │ 模型：./models（绝对路径注入）        │
        │ 停止即释放显存（约 3.9 GB）           │
        └──────────────────────────────────────┘
```

- **单入口**：一个启动脚本、一个窗口；DLNA 与 UI 同进程。
- **字幕服务独立子进程**：崩溃不拖垮主程序；停止后显存立刻回收——这是解决「ASR + Ollama 抢显存导致 67s 卡顿」的关键。

### 翻译后端（本地 llama.cpp，2026-09-16 起为默认）

翻译**不再依赖 Ollama**：模型以 GGUF 形式放在**安装目录的 `models\` 下**，由字幕服务
按需拉起 `vendor\llama\llama-server.exe`（幂等复用、随字幕服务一起回收，父进程崩溃由
Job Object 带走，不留孤儿显存）。配置见 `vendor\subtitle\config.json`：

```jsonc
"translate": {
  "backend": "local",                                    // local | ollama | openai
  "local": {
    "model": "../../models/Sakura-7B-Qwen2.5-v1.0/sakura-7b-qwen2.5-v1.0-iq4xs.gguf",
    "port": 8082, "ctx": 2048, "ngl": 99
  }
}
```

换模型 = 换 `models\` 下的文件 + 改这一行路径（`ollama` 段保留为可选回退）。

### ASR 后端（audio.cpp，只注册一个 streaming 模型）

`audiocpp_backend` 拉起 :8081 时写入的配置**只注册一个模型**，且必须是 `mode=streaming`：

- 头显实时字幕走 `/transcribe/stream`，上游要求 `mode=streaming`（否则 500）；
- 后端的离线请求用同一个 id **照样能跑**（实测 6s 音频 234ms），所以无需第二个注册。

**不要注册成「离线 + 流式」两个**：同一份权重会驻留两遍（audiocpp 内存 737→2507 MB、
总显存 7745/8188 MiB），把 llama-server 挤到 CPU —— 实测流式中位 0.29→2.34 s、整段 6× 变慢。

ASR 权重同样取自**安装目录的 `models\`**（`asr.audiocpp.model = ../../models/Qwen3-ASR-0.6B`）。
注意 audio.cpp 是按**它那份配置文件的所在目录**解析相对路径的，而临时配置写在 `%TEMP%` ——
所以 `audiocpp_backend` 会先解析成绝对路径再写进去（否则上游找不到模型文件）。

## 打包成 EXE

```powershell
powershell -ExecutionPolicy Bypass -File build\build_exe.ps1
```

产物在 `dist-app\`：

| 内容 | 说明 |
|---|---|
| `FunScriptCast-Nexus.exe` | 应用本体，约 17.7 MB（含 pywebview / DLNA 模块） |
| `ui\` `vendor\` `tools\` | **外置数据**：前端与两套服务源码，改完即生效，不用重打包 |
| `version.json` | 版本号 |

**为什么不做成「一个文件」**：torch 约 4 GB、模型约 8 GB，塞进 EXE 既慢又没意义。
字幕服务继续以子进程调用 `.venv`，所以独立运行需要：

```
dist-app\
├── FunScriptCast-Nexus.exe
├── .venv\        ← 从仓库根目录复制（或建目录联接：mklink /J）
└── models\       ← 同上（8 GB）
```

不带 `.venv` 也能跑：DLNA、设备同步、术语表都正常，只有「启动字幕服务」会
直接报 `ModuleNotFoundError: No module named 'uvicorn'` 这类可读错误。

### 构建期踩到的坑（已修）

`build\_pyi_patch.py`：Python 3.10.0 的 `dis._unpack_opargs()` 在遇到不带参数的
指令时忘了重置 `extended_arg`，导致 `dis.get_instructions()` 在分析 `bottle.py`
（pywebview 的依赖）时抛 `IndexError`，PyInstaller 直接崩。spec 里在分析前替换掉
这个函数，不需要动系统 Python。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/state` | 全量状态（DLNA / 字幕 / 同步 / GPU / 设置 / 事件） |
| GET/POST | `/api/settings` | 读写设置 |
| POST | `/api/dlna/start` `/api/dlna/stop` | 启停 DLNA |
| POST | `/api/subtitle/start` `/api/subtitle/stop` | 启停字幕子进程 |
| GET/POST | `/api/subtitle/config` | 读写字幕服务 `config.json` |
| GET | `/api/glossary` | 读取术语表 |
| POST | `/api/glossary/save` | 保存术语表并触发热重载 |
| POST | `/api/glossary/export` | 导出为 CSV（UTF-8 BOM，Excel 直开不乱码） |
| POST | `/api/glossary/import` | 解析 CSV（`mode=replace` 整表替换，否则合并） |

> 术语表页**不渲染条目**——4000+ 条会把 DOM 撑到几千节点。界面只显示两张表的条数，
> 维护走 CSV 导入/导出（或直接改 `vendor/subtitle/glossary_*.json`）。
| GET | `/api/sync` | 同步状态（设备连接 / 两个槽位 / 日志） |
| POST | `/api/sync/devices` | 扫描 adb 设备（含型号） |
| POST | `/api/sync/connect` `/api/sync/disconnect` | 连接 / 断开设备 |
| POST | `/api/sync/run` | 开始同步（`kind=script|video`，后台线程执行） |
| POST | `/api/quit` | 退出应用 |

前端 JS 桥（`window.pywebview.api`）：`win_minimize` / `win_close` / `win_hide` / `pick_folder`。

## 窗口与托盘

- 窗口 **无边框自绘标题栏**（`frameless=True` + `.pywebview-drag-region` 拖动）；
  `tune_frameless_window()` 用 `WS_THICKFRAME` 找回原生缩放边框并打开 DWM 圆角。
- 托盘常驻：关闭按钮按设置「最小化到托盘」或直接退出；托盘菜单可显示/退出。
- 「启动时直接隐藏到托盘」适合开机自启只跑服务的场景。

## 已验证

| 项 | 结果 |
|---|---|
| 宿主 API | `/api/state` 200，返回 LAN IP / GPU 占用 |
| 静态资源 | `index.html` / `styles.css` / `app.js` 均 200 |
| DLNA（vendor） | `running=true`，`http://192.168.2.2:8899`，`description.xml` 200，SOAP Browse 200 |
| 字幕子进程（vendor） | 启动 → `ready`，模型从 `./models` 加载，`cuda:0` |
| 翻译后端 | 本地 llama.cpp（`vendor\llama`，GGUF 取自安装目录 `models\`，**不依赖 Ollama**） |
| 前端 | 控制台错误 0；溢出 0×0；DOM 545 节点（预算 <1500） |
| 窗口 | pywebview + WebView2；无原生标题栏 + 可缩放 + 圆角；关闭→隐藏、托盘→恢复 |
| 设备同步 | 真机 Quest 3（`192.168.2.129:5555`）脚本同步：本地 1 / 设备 2707 / 推送 1 |
| 术语表 CSV | 导入解析（跳过表头/空行）、导出 UTF-8 BOM；现为 2091 / 2170 条 |
| 打包 EXE | 17.7 MB 单文件；DLNA `description.xml` 200；字幕服务 `ready` + `cuda:0` |
| 显存回收 | 停止字幕服务后 5720 MB → 1736 MB |

## 自动化测试

```powershell
# 前端渲染 + 数据绑定（需先启动 host_server.py）
node tests\ui_check.js

# 托盘：关闭到托盘、托盘恢复、/api/quit 真正退出
.\.venv\Scripts\python.exe tests\test_tray_run.py

# 无边框窗口：样式位 / 最小化 / 关闭到托盘 / 缩放边框
.\.venv\Scripts\python.exe tests\test_frameless.py
```

## 显存说明

字幕服务启动后 ASR 模型常驻约 **3.9 GB**（`Qwen3-ASR-0.6B` + `ForcedAligner`），这是实时字幕的必要代价。翻译模型（Sakura-7B Q4，约 4 GB）由 llama-server 在**首次翻译时**拉起、随字幕服务一起回收。8 GB 卡上两者同时驻留接近上限，因此：

- 不用字幕时，在界面点「停止字幕服务」即可释放；
- 或把翻译后端切到云端（`dashscope`），PC 端只留 ASR。

## 待办

1. 同步页的进度条（当前是日志 + 结果计数）
2. 首次启动引导（自动检测 .venv / models，缺失时给出一键说明）
