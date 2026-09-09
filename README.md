# FunScriptCast-Nexus（PC 端）

把 **VR-DLNA** 与 **AI 字幕服务端** 合并为一个桌面软件。

## 运行

```bat
app\start.bat
```

或用字幕服务的 venv 直接跑：

```powershell
cd E:\Development\FunScriptCast-Nexus
"E:\Development\Subtitle Server\.venv\Scripts\python.exe" host_server.py
```

调试（不开窗口，只起 API）：

```powershell
... host_server.py --no-window
```

## 架构

```
┌─ 宿主进程（host_server.py）──────────────────────────────┐
│  · 托管前端静态资源 + JSON API   127.0.0.1:8790          │
│  · DLNA 服务（import VR-DLNA 复用）  0.0.0.0:8899        │
│  · pywebview 窗口（WebView2）                            │
└───────────────┬──────────────────────────────────────────┘
                │ subprocess.Popen / terminate
        ┌───────▼──────────────────────────────┐
        │ 字幕服务子进程（Subtitle Server venv）│
        │ FastAPI  127.0.0.1:8756              │
        │ 停止即释放显存（约 3.7 GB）           │
        └──────────────────────────────────────┘
```

- **单入口**：一个启动脚本、一个窗口；DLNA 与 UI 同进程（import 复用，0.06s）。
- **字幕服务独立子进程**：崩溃不拖垮主程序，停止后显存立刻回收——这是解决"ASR + Ollama 抢显存导致 67s 卡顿"的关键。

## 目录

| 文件 | 说明 |
|---|---|
| `host_server.py` | 宿主进程：DLNA 控制、字幕子进程管理、静态托管、JSON API |
| `ui/index.html` | 前端结构（无框架） |
| `ui/styles.css` | 设计系统（Token 与 `ui-design/integrated-app/DESIGN_SPEC.md` 一致） |
| `ui/app.js` | 前端逻辑：1s 状态轮询、数据绑定、交互 |
| `start.bat` | 启动器（自动定位 venv Python） |
| `_ui_check.js` | 前端集成自查（无头 Chrome + CDP） |

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
| 宿主 API | `/api/state` 200，返回 LAN IP / GPU 5632/8188 MB |
| 静态资源 | `index.html` / `styles.css` / `app.js` 均 200 |
| DLNA 启动 | `running=true`，`http://192.168.2.2:8899`，`description.xml` 200，SOAP Browse 200 |
| 字幕子进程 | 启动 → `status=ready alive=True pid=2456` → 停止 → `stopped alive=False` |
| 前端渲染 | 无控制台错误；胶囊/轨道/指标/环形进度/术语表计数全部绑定成功；溢出 0×0 |
| DOM 预算 | **1101** 节点（预算 < 1500；术语表渲染上限 60 条） |
| 窗口 | pywebview + WebView2 创建成功（`created=True, error=None`） |

## 待办

1. 托盘图标 + 关闭到托盘的实际行为（当前设置项已落库，托盘逻辑未接）
2. 同步页（脚本/视频文件夹同步）——`VR-DLNA` 的 `funscript_sync.py` / `video_sync.py` 尚未接入
3. 术语表 CSV 导入/导出
4. 打包成单 EXE（PyInstaller，注意 torch 体积）
