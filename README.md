# FunScriptCast-Nexus

面向 VR 观影的 PC 端控制中枢：**DLNA 媒体服务 + AI 实时字幕 + 设备联动**，一个窗口、一个托盘。

原 `VR-DLNA`（抚物器）与 `Subtitle Server` 两个独立项目已 **vendor 进本仓库**，不再依赖 `E:\Development` 下的其他目录。

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
│   ├── translate_engine.py Ollama / dashscope
│   ├── glossary.py         术语表（mtime 热加载）
│   └── config.json         ASR / VAD / 分段 / 翻译配置
├── models\                 8.0 GB：Qwen3-ASR-0.6B + Qwen3-ASR-1.7B + ForcedAligner-0.6B
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
| POST | `/api/glossary/import` | 解析 CSV 并返回词条（写盘仍走 save） |
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
| 翻译后端 | Ollama 可达（5 个模型） |
| 前端 | 控制台错误 0；溢出 0×0；DOM 1198 节点（预算 <1500） |
| 窗口 | pywebview + WebView2；无原生标题栏 + 可缩放 + 圆角；关闭→隐藏、托盘→恢复 |
| 设备同步 | 真机 Quest 3（`192.168.2.129:5555`）脚本同步：本地 1 / 设备 2707 / 推送 1 |
| 术语表 CSV | 导入解析（跳过表头/空行）、导出 UTF-8 BOM；110 / 81 条 |
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

字幕服务启动后 ASR 模型常驻约 **3.9 GB**（`Qwen3-ASR-0.6B` + `ForcedAligner`），这是实时字幕的必要代价。Ollama 另占约 0.8 GB。8 GB 卡上两者同时驻留接近上限，因此：

- 不用字幕时，在界面点「停止字幕服务」即可释放；
- 或把翻译后端切到云端（`dashscope`），PC 端只留 ASR。

## 待办

1. 同步页的进度条（当前是日志 + 结果计数）
2. 首次启动引导（自动检测 .venv / models，缺失时给出一键说明）
