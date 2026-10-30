# Rust 全量重写：完成状态

> 2026-10-20 ｜ 分支 `refactor/foundation`
> **全部功能已移植，无 501 桩。**

---

## 1. 总览

| crate | Python 源 | 状态 | 测试 |
|---|---|---|---:|
| `nexus-device` | `vendor/device` 1,317 行 | ✅ 完成 | 51 |
| `nexus-dlna` | `vendor/dlna` 4,598 行 | ✅ 完成 | 17 |
| `nexus-library` | `vendor/player/library.py` 300 行 | ✅ 完成 | 6 |
| `nexus-subtitle` | `vendor/subtitle` 6,030 行 | ✅ 完成 | 46 |
| `nexus-vision` | `vendor/vision` 6,214 行 | ✅ 完成 | 59 |
| `nexus-host` | `host_server.py` 4,791 行 | ✅ 完成 | 40 |
| **合计** | **~26,000 行** | | **219** |

---

## 2. 安装形态（与 Python 版一致）

**真窗口（WebView2）+ 托盘 + 本地 HTTP 服务**，不是"只开个浏览器标签页"。

- 窗口：`tao` + `wry`（WebView2），1280×840，标题 `FunScriptCast-Nexus`
- 托盘：显示 / 退出
- release 版**不弹控制台窗口**（`windows_subsystem = "windows"`）
- 建窗口失败（如缺 WebView2 运行时）→ 自动退回浏览器打开，不让用户"什么都看不到"
- 端口已被占用 → 直接打开指向已有实例的窗口，不会起第二个服务

事件循环必须在主线程，所以服务跑在后台线程的 tokio 运行时里。

### 安装位置（本机）

```
D:\FunScriptCast-Nexus-Rust      ← Rust 版（本版）
D:\FunScriptCast-Nexus           ← Python 版（原样保留）
```

大文件用**目录联接**共享，不重复占盘：

```
Rust\models       →  Python\models        （16.4 GB）
Rust\vendor\llama →  Python\vendor\llama  （0.8 GB）
```

「应用和功能」里显示为 **FunScriptCast-Nexus (Rust 版)**，与 Python 版可区分。


## 2.1 pywebview 兼容桥（`bridge.rs`）

前端用 `window.pywebview.api` 判断"是不是在桌面壳里"。WebView2 里没有这套 API，
所以**所有选目录/选文件/窗口按钮**都会走到"仅桌面应用中可用"。

`bridge.rs` 把同名方法桥回 Rust（JS → IPC → Rust → 原生对话框/窗口操作）：

| 方法 | 实现 |
|---|---|
| `pick_folder` / `pick_file` / `save_file` | `rfd` 原生对话框 |
| `win_minimize` / `win_maximize` / `win_close` | 排进命令队列，由**事件循环**执行 |
| `drag_window` | `ReleaseCapture` + `WM_NCLBUTTONDOWN(HTCAPTION)` |

三个非显然的细节：

1. **无边框窗口上 `ShowWindow(SW_MAXIMIZE)` 无效** —— 必须让 tao 自己算显示器工作区，
   所以窗口命令要走队列回到事件循环（那里才拥有 `tao::Window`）。
2. **对话框线程必须先 `CoInitializeEx`**（STA）—— 不初始化时 rfd 直接返回 None，
   表现成"点了没反应"。
3. `tao::Window` **不能 clone**，而 wry 的 IPC 回调要 `'static + Send` ——
   所以窗口控制只持有 HWND。

页面里已经验证：`window.pywebview.api` 有 7 个方法；点「选择目录」弹出原生
「选择文件夹」对话框（类 `#32770`）。


## 2.2 全面审查（2026-10-20 第二轮）

用户报告"加目录时窗口无限弹出关闭"，顺势做了一次系统性审查。查出 **8 个真问题**：

| # | 问题 | 后果 | 修法 |
|---|---|---|---|
| 1 | `pick_file` 签名错（实为 `(mode, filename, file_types)`） | 设置里"浏览解释器"参数错位 | 垫片按 Python 版逐字对齐 |
| 2 | 取消返回 `{ok:false,error:"已取消"}` | 每次取消都弹错误提示 | 改回 `{ok:false,cancelled:true}` |
| 3 | **对话框没有单飞保护** | 界面重复调用 → 反复弹窗（用户看到的循环） | `AtomicBool` 单飞 + 日志 |
| 4 | **未实现的 `/api/*` 回落到静态文件** | 返回 **200 + index.html**，静默假装成功 | 加 `/api/{*rest}` → 404 JSON |
| 5 | **10 个接口前端在用、后端没有** | 下拉框空、按钮无反应 | 全部补齐（见下） |
| 6 | 缺 `/api/js-error` | 前端异常完全不可见 | 实现：写进应用日志 |
| 7 | 缺 `/api/vision/runtime/install` | "安装运行时"按钮报错 | 实现：如实回报"随包自带" |
| 8 | **adb 备用路径被转义吃掉** | 永远"找不到 adb" | 改用 `join` 拼路径 + 防回归测试 |

第 8 条值得单独说：源码里写成 `r"D:\...\tools\adb\adb.exe"`，反斜杠在生成过程中被当成
转义序列，路径变成 `D:FunScriptCast-Nexus\toolsadbadb.exe` —— 表现是"adb 明明在，就是说找不到"。

### 补齐的 12 个接口

`/api/subtitle/models`、`/api/subtitle/asr-models`、`/api/subtitle/translate-test`、
`/api/subtitle/reclaim`、`/api/library/browse`、`/api/device/settings`、
`/api/sync/devices`、`/api/sync/connect`、`/api/sync/disconnect`、`/api/sync/run`、
`/api/js-error`、`/api/vision/runtime/install`。

### 审查结果

- 前端引用的 **45 个接口**：全部可达且返回 JSON（不再是 200+HTML）
- 前端用到的 **3 个桥方法**：全部实现
- 连点 5 次"选择目录"：**只弹 1 个对话框**，其余 4 次被拒并记日志
- 对话框开/关都写进「事件流」，出问题能直接看到

---

## 3. 视觉：七环全部对拍

| 环节 | 对拍方式 | 结果 |
|---|---|---|
| 切点（transnet） | 逐点 | 完全一致 |
| 编码 | 逐字节 | **100.00% 相等** |
| 解码 | 逐点 | 8 条 corr > 0.9999 |
| 位置合成 | 逐行 | **最大差 0.000000** |
| 出点 | 逐点 | 122 vs 122 全等 |
| 亚帧时刻 | 逐点 | 217 条全等 |
| 静止门 | 逐点 | 完全一致 |

---

## 4. 字幕

- **流式**：`/api/subtitle/stream`（PCM → SSE）
- **分段**：`HybridBuffer` 移植，与 Python 逐次裁决对拍（14 次 feed 全一致）
- **Win11 实时字幕（lccapture）已整体移除**：Rust 端删除 `lc.rs`/`/api/lc/*`，前端删除 LC 面板与入口；Python 参照实现保留但不再提供选项

---

## 5. 仍需真机确认

代码写完、失败路径已验证，但成功路径需要真设备/真环境：

| 项 | 为什么 |
|---|---|
| BLE 设备连接 | 没有真机 |
| Live Captions 成功路径 | 需先在 Win11 设置里打开「实时字幕」 |
| 自更新的实际覆盖安装 | 需要真发一个新版 |
| 设备同步长时间稳定性 | 需要真机跑完整片 |

---

## 6. 命令

```powershell
cd rust
cargo test --workspace -- --skip streaming_pipeline_end_to_end   # 219 个
cargo run -p nexus-host                                          # 带窗口（调试版有控制台）
$env:NEXUS_NO_WINDOW="1"; cargo run -p nexus-host                # 无窗口 + 浏览器
```

环境变量：`FS_HOST_LAN_PORT`（8791）、`NEXUS_BIND`（127.0.0.1:8890）、
`ORT_DYLIB_PATH`（自动找安装目录 `runtime/ort/`）、`NEXUS_NO_WINDOW`、`NEXUS_UPDATE_API`。

---

## 附：2026-10-20 交接时的状态

**能交付的**：视觉七环逐环对拍（编码 100% 逐字节、解码 8 条 corr>0.9999、
合成最大差 0.000000、出点 122 vs 122 全等）、字幕流式与分段、DLNA、媒体库、
设备层、真窗口与托盘、安装包（218 MB，无 Python，自带 ONNX 运行时）。
**229 个测试全绿，零编译告警。**

**不能交付的**：添加媒体根的文件对话框会顺序循环弹出；选完目录后"打不开"。
详见桌面《交接文档-Rust版-2026-10-20.md》第 2 节——那里有日志证据、
已试过的修复、以及建议的下一步。

**一句话结论**：底层（视觉/字幕/DLNA/设备）可信且有数据支撑；
**上层交互（文件对话框这条链路）还不可靠**，不要当成成品。
