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
└── _ui_check.js            前端集成自查（无头 Chrome + CDP）
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

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/state` | 全量状态（DLNA / 字幕 / GPU / 设置 / 事件） |
| GET/POST | `/api/settings` | 读写设置 |
| POST | `/api/dlna/start` `/api/dlna/stop` | 启停 DLNA |
| POST | `/api/subtitle/start` `/api/subtitle/stop` | 启停字幕子进程 |
| GET/POST | `/api/subtitle/config` | 读写字幕服务 `config.json` |
| GET | `/api/glossary` | 读取术语表 |
| POST | `/api/glossary/save` | 保存术语表并触发热重载 |
| POST | `/api/quit` | 退出应用 |

## 已验证

| 项 | 结果 |
|---|---|
| 宿主 API | `/api/state` 200，返回 LAN IP / GPU 占用 |
| 静态资源 | `index.html` / `styles.css` / `app.js` 均 200 |
| DLNA（vendor） | `running=true`，`http://192.168.2.2:8899`，`description.xml` 200，SOAP Browse 200 |
| 字幕子进程（vendor） | 启动 → `ready`，模型从 `./models` 加载，`cuda:0` |
| 翻译后端 | Ollama 可达（5 个模型） |
| 前端 | 控制台错误 0；溢出 0×0；DOM 1095 节点（预算 <1500） |
| 窗口 | pywebview + WebView2 创建成功 |
| 显存回收 | 停止字幕服务后 5720 MB → 1736 MB |

## 显存说明

字幕服务启动后 ASR 模型常驻约 **3.9 GB**（`Qwen3-ASR-0.6B` + `ForcedAligner`），这是实时字幕的必要代价。Ollama 另占约 0.8 GB。8 GB 卡上两者同时驻留接近上限，因此：

- 不用字幕时，在界面点「停止字幕服务」即可释放；
- 或把翻译后端切到云端（`dashscope`），PC 端只留 ASR。

## 待办

1. 托盘图标 + 关闭到托盘的实际行为（设置项已落库，逻辑未接）
2. 同步页（脚本/视频文件夹同步）——`vendor/dlna` 里的 `funscript_sync.py` / `video_sync.py` 尚未接入
3. 术语表 CSV 导入/导出
4. 打包单 EXE（PyInstaller，注意 torch 体积）
