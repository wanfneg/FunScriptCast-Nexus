# 仓库目录结构（2026-10-19 整理）

> 目标：产品运行时 / 构建工具 / 产物 / 运行数据 / 文档 各有明确归属。
> 维护：新增顶层目录或调整归属时，同步本文件与 `docs/文档地图.md`。

## 顶层

| 路径 | 分类 | 说明 | 入库 |
|---|---|---|---|
| `app/` | 产品源码 | 新底座（config / log / bridge / registry / routes / web static） | ✅ |
| `web/` | 产品源码 | 新前端（Vite + React + TS），`dist/` 为构建产物 | ✅（dist/ 忽略） |
| `ui/` | 产品源码 | 旧前端（迁移期保留，`/` 仍走它） | ✅ |
| `vendor/` | 产品源码 | 运行性子系统：subtitle / vision / dlna / device / mpv / llama / audiocpp | ✅（运行库按 .gitignore） |
| `models/` | 资产 | 识别/翻译模型（下载后放这里） | ❌ |
| `host_server.py` | 产品源码 | 宿主入口（HTTP / DLNA / 托盘 / 子进程） | ✅ |
| `start.bat` / `version.json` | 入口/版本 | 绿色版启动器与版本号 | ✅ |
| `tools/` | 开发工具 | 下载/构建/同步/校验脚本 + `tools/vision` 分析工具 | ✅ |
| `tests/` | 测试 | 自检脚本（不启动服务器） | ✅ |
| `build/` | 构建 | 构建脚本 + `installer/setup.iss` | ✅（暂存目录忽略） |
| `docs/` | 文档 | 见下 | ✅ |
| `dist-app/` | 构建产物 | 应用组装目录（EXE + ui + vendor + runtime） | ❌ |
| `dist-installer/` | 构建产物 | 安装包输出 | ❌ |
| `dist-modules/` | 构建产物 | 模块 zip（vision 等） | ❌ |
| `data/` | 运行数据 | 配置 / 设置 / 术语表 / DLNA 数据 | ❌ |
| `logs/` | 运行数据 | host.log / 服务日志 | ❌ |
| `run/` | 运行数据 | 子进程配置、临时文件 | ❌ |
| `backup/` | 运行数据 | 术语表换表备份 | ❌ |
| `.venv/` | 开发环境 | 本机 Python 环境（默认 onnx 后端，torch-free） | ❌ |
| `.zcode/` / `.zcodeignore` | 工具状态 | zcode 工作流 | ❌ |

## docs/

| 路径 | 说明 |
|---|---|
| `docs/文档地图.md` | 文档索引与权威性（**先看这个**） |
| `docs/design/` | 设计规格与过程截图（原 `design/`） |
| `docs/review/` | 审查与修复台账 + 只读探针（原 `review/`） |
| `docs/dev-history/iteration_shturl.md` | 逐轮开发档案（原仓库根 `iteration_shturl.md`） |
| `docs/dev-archive/` | 历史报告存档（只读） |
| `docs/VISION-ONNX.md`、`docs/REFACTOR-PLAN.md` 等 | 专项文档 |

## 为什么这些留在根目录

- `dist-app/`、`dist-installer/`、`dist-modules/`、`data/`、`logs/` 是安装包与运行时的**约定路径**：
  `build/installer/setup.iss`、`host_server.py`、`vendor/subtitle/user_paths.py` 都按这个布局工作，
  随意改名会破坏已安装用户的升级与配置。
- `host_server.py` 固定在根：PyInstaller 入口与开发启动都依赖它。
- `ui/` 与 `web/` 并存是迁移期设计（旧界面 `/`、新界面 `/app`），迁移完成后再收敛。

## 新增文件放哪

- 新接口/底座代码 → `app/`；新前端页面 → `web/src/`；视觉/字幕等运行时 → `vendor/<子系统>/`。
- 新构建脚本 → `build/`；安装器配置 → `build/installer/`。
- 新文档 → `docs/`（并更新 `docs/文档地图.md`）；审查记录 → `docs/review/`。
- 新工具 → `tools/`；测试 → `tests/`。
- 构建产物只写 `dist-*`，运行数据只写 `data/ logs/ run/`，都已 gitignore。
