# FunScriptCast-Nexus 发布说明

## 1.0.2

Windows 桌面端控制中枢：把 **DLNA 媒体服务**、**AI 实时字幕**、**设备同步** 合成一个窗口 + 一个托盘。

### 下载

| 文件 | 说明 |
|---|---|
| `FunScriptCast-Nexus.exe` | 应用本体，17.7 MB（含 pywebview / DLNA 模块） |

解压后把 `.venv\` 与 `models\`（约 5 GB + 8 GB）放到 EXE 旁边即可启用 AI 字幕；
不带也能跑，只是「启动字幕服务」会明确报错提示。

### 本版变化

**术语表页不再渲染条目**
- 词库已扩到 **日语 2091 条 + 英语 2170 条**，渲染会把 DOM 撑到几千节点
- 页面改为只显示两张表的条数，维护走「导入 / 导出 CSV」（或直接改 `vendor\subtitle\glossary_*.json`）
- 导入新增「整体替换 / 合并」开关，默认整体替换
- DOM 节点 1221 → **545**

## 1.0.1

Windows 桌面端控制中枢：把 **DLNA 媒体服务**、**AI 实时字幕**、**设备同步** 合成一个窗口 + 一个托盘。

### 下载

| 文件 | 说明 |
|---|---|
| `FunScriptCast-Nexus.exe` | 应用本体，17.7 MB（含 pywebview / DLNA 模块） |

解压后把 `.venv\` 与 `models\`（约 5 GB + 8 GB）放到 EXE 旁边即可启用 AI 字幕；
不带也能跑，只是「启动字幕服务」会明确报错提示。

### 本版内容

**设备同步（新增页）**
- 脚本 / 视频 → Quest 的 adb 增量推送，真机验证：本地 1 / 设备 2707 / 本次推送 1
- 自动探测 adb，支持 USB 与无线连接，日志实时回传

**术语表 CSV 导入 / 导出**
- 导出 UTF-8 BOM（Excel 直开不乱码）
- 导入自动跳表头、跳过空行、UTF-8/GBK 自动识别

**窗口与托盘**
- 无边框自绘标题栏（可拖动），用 `WS_THICKFRAME` 找回原生缩放边框 + DWM 圆角
- 关闭按钮按设置「最小化到托盘」或退出；托盘菜单可显示 / 退出
- 「启动时直接隐藏到托盘」适合开机自启只跑服务

**打包**
- PyInstaller 单文件 EXE；torch（4 GB）与模型（8 GB）不进包，字幕服务仍走 `.venv` 子进程
- 修掉 Python 3.10.0 `dis._unpack_opargs()` 的 `extended_arg` 未重置 bug（会导致 PyInstaller 分析 `bottle.py` 时崩溃）
- 字幕服务改用管道接输出，秒退时把最后几行作为错误上报，不再假死

### 系统要求

- Windows 10/11 x64，WebView2 运行时
- 字幕服务另需 NVIDIA GPU（CUDA）+ `.venv` + `models`
