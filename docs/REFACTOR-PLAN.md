# Nexus 底座重构：现状、目标与迁移路线

> 创建：2026-10-19 ｜ 分支：`refactor/foundation`
> 快照分支：`snapshot/pre-refactor-20261019`（重构前工作区的完整留档）

## 1. 为什么重构

| 现状 | 代价 |
|---|---|
| `host_server.py` 单文件约 5200 行，路由是巨型 `if/elif` 链 | 任何改动都要在同一个文件里翻找；无法并行、无法单测 |
| 前端 `ui/app.js` 176KB + `index.html` 79KB + `styles.css` 60KB，无构建、无类型、无组件 | 已发生 `bind()` 中断导致全站按钮失效；打包版没有控制台，前端错误不可见 |
| Python 同时承担 HTTP/DLNA/托盘/子进程管理 | 一处异常影响全局；AI 运行时（PyTorch ~2.5GB）与宿主强耦合 |
| 视觉推理走 PyTorch + torch.hub | 安装包巨大、解释器探测/安装逻辑复杂 |

## 2. 目标形态（渐进式，不推倒重来）

```
Nexus/
├─ app/                      # 新底座（Python 包）
│  ├─ config.py              # 路径/版本/运行时注入
│  ├─ log.py                 # 统一 logger
│  ├─ bridge.py              # http.server <-> 新路由的适配层
│  ├─ api/
│  │  ├─ registry.py         # 传输无关的路由注册表
│  │  └─ routes/             # 每个领域一个模块
│  └─ web/static.py          # /app/* 托管 web/dist
├─ web/                      # 新前端（Vite + React + TS）
│  ├─ src/
│  └─ dist/                  # 构建产物（不入库）
├─ host_server.py            # 历史入口：仅 +14 行桥接，其余不动
├─ ui/                       # 旧界面：迁移完成前保留，原样可用
└─ vendor/                   # 历史子系统（字幕/设备/DLNA/视觉）
```

**核心机制：先新后旧。** 每个请求先交给 `app.bridge.handle()`；
命中就由新代码响应，未命中原样落回历史 `if/elif` 链。
所以任何一步迁移都可以独立回滚，且旧界面始终可用。

## 3. 本分支已完成（P0）

- [x] `app/` 包：config / log / registry / bridge / static
- [x] `GET /api/v2/ping`、`/api/v2/system`、`/api/v2/routes`
- [x] `/app/` 托管 Vite 构建产物（SPA 回退、缓存策略、路径穿越防护）
- [x] `web/`：Vite 6 + React 18 + TypeScript 5 + lucide 图标，含侧栏骨架与 5 个页面位
- [x] 开发期代理：`npm run dev` 把 `/api` 转到 `127.0.0.1:8790`
- [x] 桥接自检：`tests/test_app_bridge.py`（不启动服务器，11 项断言）
- [x] host_server 仅 +14 行；旧路由、旧界面零改动

## 4. 迁移阶段

### P1 只读接口（✅ 第一批已完成 2026-10-19）

| 接口 | 状态 | 说明 |
|---|---|---|
| `GET /api/v2/state` | ✅ | 复用宿主的 `state_payload()`（服务注入） |
| `GET /api/v2/logs` | ✅ | 最近 300 条 |
| `GET /api/v2/library/state` | ✅ | 新增 `library_state_payload()`，旧 handler 改为调用它 |
| `GET /api/device/state` | ⬜ | 下一批 |

做法：`app/api/services.py` 让宿主在启动时注入只读数据源，新路由只做路由，
逻辑仍走旧实现——行为不变、可随时回退。

前端：Dashboard 接了 system/state/logs；媒体库页接了 library/state（只读，写操作仍在旧界面）。
自检：`tests/test_app_bridge.py` 14 项全过；`web` 构建通过（177 KB）。

### P2 写接口（中风险，3–5 天）
- `/api/settings`（补丁式保存）、`/api/sync/start`、`/api/vision/*`
- 统一错误格式 `{ok:false,error}`，前端不再各写一套

### P3 前端页面逐个替换（持续）
- 每页替换前，先用新接口做**只读回放**，确认数据一致再上交互
- 旧 `ui/` 保留到最后；切换开关：访问 `/` 走旧界面，访问 `/app` 走新界面

### P4 打包与视觉运行时
- [x] 视觉默认后端切 onnx（DirectML，`FSC_VISION_BACKEND=onnx`），torch 仅保留为对照
- [x] 宿主启动前的依赖检查按后端判定（onnx → numpy + onnxruntime；torch → 原依赖）
- [ ] 安装包把 `app/`、`web/dist` 一并带入（`build/build_exe.ps1` + `installer/setup.iss`）
- [ ] 打包只装对应平台 onnxruntime，不再安装 torch（`.venv` 可减约 4.2 GB；vision-runtime 同理）

## 5. 约定

- **API 版本**：新接口一律 `/api/v2/`；旧接口不改路径。
- **返回体**：JSON；失败统一 `{"ok": false, "error": "..."}`。
- **路由**：`@route("GET", "/api/v2/xxx")`，精确匹配；参数化路径以后再加。
- **静态**：前端构建到 `web/dist`，只由 `/app/*` 提供，不碰旧 `ui/`。
- **日志**：`app.log.get_logger(...)`，最终汇入现有 logging 配置。
- **测试**：每个新路由至少一条 `tests/test_app_*.py`，用假 handler，不启动服务器。

## 6. 风险与回滚

| 风险 | 处置 |
|---|---|
| 新代码影响旧路由 | 桥接只在最前面尝试，未命中立即返回 False；已用自检覆盖 |
| 安装包漏带 `app/` | `bridge` 捕获 ImportError 并回落；P4 再补打包 |
| 前端构建产物过期 | `/api/v2/system` 暴露 `web_dist.built`；打包脚本强制先 build |
| 重构把工作区搞乱 | 快照分支 `snapshot/pre-refactor-20261019` 可随时回退 |

## 7. 下一步（需要人确认）

1. 先跑一次：`cd web && npm run dev`，同时启动宿主，浏览器打开 `http://127.0.0.1:5173/app/`
   验证新界面能读到 `/api/v2/system`。
2. 确认 P1 的接口迁移顺序（媒体库 → 状态 → 日志）。
3. 决定是否把"旧界面 `/`、新界面 `/app`"作为长期双入口，或设定切换日期。
