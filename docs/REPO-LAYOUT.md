# 仓库目录结构（2026-10-19 整理）

> 目标：产品源码 / 文档 / 构建工具 / 构建产物 / 运行数据 各有唯一归属。
> 维护：调整顶层目录时同步本文件与 `docs/文档地图.md`。

## 顶层

```
Nexus/
├─ app/             新底座（Python 包：config / log / bridge / registry / routes / web static）
├─ web/             新前端（Vite + React + TS；dist/ 为构建产物）
├─ ui-legacy/       旧前端（迁移期保留，`/` 仍由它提供）
├─ vendor/          运行性子系统（subtitle / vision / dlna / device / mpv / llama / audiocpp）
├─ models/          模型资产（识别 / 翻译 / HF 缓存；gitignore）
├─ host_server.py   宿主入口
├─ start.bat        绿色版启动器
├─ version.json     版本号
├─ tools/           开发与运维脚本（含 tools/vision 分析工具）
├─ tests/           自检与测试（不启动服务器）
├─ docs/            全部文档（见下）
├─ build/           构建脚本 + build/installer/setup.iss + build/.cache/（暂存，gitignore）
├─ artifacts/       构建产物：app/ installer/ modules/（gitignore）
├─ var/             运行数据：data/ logs/ run/ backup/（gitignore）
└─ .venv/ .zcode/   开发环境 / 工具状态（gitignore）
```

## docs/

| 路径 | 说明 |
|---|---|
| `docs/文档地图.md` | 文档索引与权威性（**先看这个**） |
| `docs/REPO-LAYOUT.md` | 本文件 |
| `docs/design/` | 设计规格与过程截图（原 `design/`） |
| `docs/review/` | 审查与修复台账 + 只读探针（原 `review/`） |
| `docs/dev-history/iteration_shturl.md` | 逐轮开发档案（原仓库根） |
| `docs/dev-archive/` | 历史报告存档（只读） |
| `docs/VISION-ONNX.md`、`docs/REFACTOR-PLAN.md` 等 | 专项文档 |

## 路径边界（重要）

- `var/` 是**安装目录运行数据**的统一根：宿主与字幕服务都从
  `vendor/subtitle/user_paths.py` 取 `var/data`、`var/logs`、`var/run`。
  旧版本把 `data/logs/run/backup` 直接放在安装目录根下，**首次运行会自动迁移**到 `var/`。
- `artifacts/app` 是安装包组装的源目录（`build/build_exe.ps1` 产出），
  `artifacts/installer` 是安装包输出，`artifacts/modules` 是模块 zip。
- `build/installer/setup.iss` 的 `Source` 相对路径 = `..\..\artifacts\app\...`，
  `OutputDir = ..\..\artifacts\installer`。
- `ui-legacy/` 由宿主 `UI_DIR` 提供；新界面在 `web/dist`，由 `/app/` 提供。
- `models/` 留在根目录：它是安装目录的模型根，升级与用户数据都依赖这个位置。

## 新增文件放哪

- 底座代码 → `app/`；新前端 → `web/src/`；运行性子系统 → `vendor/<子系统>/`。
- 构建脚本 → `build/`；安装器配置 → `build/installer/`。
- 文档 → `docs/`（并更新文档地图）；审查记录 → `docs/review/`。
- 工具 → `tools/`；测试 → `tests/`。
- 构建产物只写 `artifacts/`，运行数据只写 `var/`，暂存写 `build/.cache/`，都已 gitignore。
