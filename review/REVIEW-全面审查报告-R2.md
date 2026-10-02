# FunScriptCast-Nexus 全面审查报告（第 2 轮 · 交互链与手机端对齐）

- 仓库：`E:\Development\FunScriptCast-Nexus`，基线 `HEAD = cb81b21`；工作区另有未提交改动 `ui/app.js`(+36/−7) 与 `version.json`
- **真源参照**：手机端 `E:\Development\FunScriptCast`（Kotlin/Compose）、头显端 `E:\Development\VRFunScriptCast`
- 审查方式：6 名成员分线（后端交互链 / 前端控件穷举 / 手机端对齐 / 未覆盖区域 / 独立验证）+ Lead 自带只读探针
- 产品代码**零改动**；未启动 GUI/服务器/App；`data/` 未写入
- 分册：
  - [R01 后端链路（第 1 轮）](R01-backend-linkage.md) · [R02 前端（第 1 轮）](R02-frontend-linkage.md) · [R03 宽面（第 1 轮）](R03-broad-audit.md) · [R04 第 1 轮验证](R04-verification.md)
  - [R11 后端交互链（第 2 轮）](R11-backend-chains.md) · [R12 前端控件穷举（第 2 轮）](R12-ui-chains.md) · [R13 手机端对齐（第 2 轮）](R13-parity-phone.md) · [R14 未覆盖区域（第 2 轮）](R14-untouched-areas.md)
  - [归属规范](设置页与联动页归属-对照手机端.md) · [探针](probes/)

---

## 0. 结论摘要

**第 1 轮**：发现 2 条阻断、13 条高危，但**审查面偏窄、且当时不知道有手机端真源**，导致 2 条结论错误（见 §7）。

**第 2 轮**（本次）：以"**一次交互 → 发什么信号 → 下链是否合理 → 会不会异常**"为主线重审，并逐条对照手机端，结果为：

| 维度 | 数量 |
|---|---|
| 手机端功能点与决策点的对齐审计 | **61 条**：已对齐 28 · 语义不同 14 · 本仓库多出来 6 · **本仓库缺失 13** |
| 其中平台性差异（**不需对齐**） | 外置播放器整链、SAF/ExoPlayer/VR 投影/唤醒锁、远脚本源(WebDAV/SMB/DLNA)、BLE HID 主机角色 |
| 第 1 轮结论被**推翻** | **3 条**（反转方向无效 / 滑块 BOOST 锁死 / 暂停置 allow_move） |
| 新增确认缺陷 | 阻断 1 · 高 5 · 中 8 · 低 10（另有跨轮重复项不计） |
| Lead 亲自复现的探针 | 6 个（见 §6） |

**一句话**：功能骨架与手机端一致度不低（28/61 完全对齐），但**三个"安全/一致性闸门"失效**（急停、互斥、持久化），且**四条主交互链在链路上就断了**（§4）。

> ⚠️ **验证状态**：本报告标注 ✅ 的结论均有 Lead 亲自运行的只读探针（见 §6）；标注 ❌ 的 3 条为**被推翻项**（含 1 条 Lead 自己的假设）。
> 第 3 方独立验证员因**并发成员上限（8）占满**未能开通，未产出 `R15-verification-r2.md`；本轮以 Lead 复现作为验证来源，
> 建议下轮腾出名额后补做一次对抗验证（尤其 R11 的"三方仲裁矩阵"与 R12 的竞态表 R1–R14）。

---

## 1. 判定基准：手机端规范（本次新增的"真源"）

| 规范 | 手机端依据 | 本仓库现状 |
|---|---|---|
| ~~P-1 打开视频 = 关闭视频~~ → **更正：只需清脚本，不停视频** | 手机端 `AppViewModel.kt:2190`（本地）、`:2546`（远程）确实 `stop()` 预设 **且 `closeVideo()`**，但那是**视频页与预设页分离**的产物 | ⚠️ **PC 端集成页是有意分叉**：进预设**不清视频**、只需**清脚本**。见 [设备写入所有权设计](设备写入所有权设计.md) §5 |
| **P-2 预设播放中加载脚本 → 自动退出预设**（用户明确要求） | `loadScript()` 确认后 `:3546` 停预设；`scriptLoadConfirm` `:3541-3549` | ❌ 无任何对应 |
| **P-3 进预设模式 = 三件事**：**清脚本** / **停快捷动作** / **开预设** | `startPresetPlayback()` `:3530-3539` 四件事减去"关视频"这件平台性动作 | ⚠️ 只做 2 件（停同步 + 开预设），**漏"停快捷动作"** |
| **P-4 进预设判据** = 脚本已加载 **OR 视频在播** | `requestPresetPlay()` `:3514` | ⚠️ 只看 `SYNC.on` |
| **P-5 所有写帧循环都受急停约束** | `SyncEngine`/`QuickMoves` 每拍查 `allowMove`；`moveTo` 是受约束的那个 | ❌ 三处用 `force=True` 绕过 |
| **P-6 预设速度防抖落盘** | `presetSpeed` → prefs（`:206-217`、`:3495-3496`） | ❌ 不落盘，且被别的设置拨回 |

---

## 2. 交互链总览（本次主线的核心产出）

按"点击 → 信号 → 下链 → 回程"列出 4 条**在链路上就断了**的主链（完整 72 button/28 input/6 select 清单见 [R12](R12-ui-chains.md)）：

### 链 A：预设播放中 → 点视频卡片 → 开播（**断点：全程无人停预设**）```
[点击 .lib-card]
 └─ app.js:1446-1456  取 vlLastBrowse 里的 path/name，调 openVideo(vp,name,list,idx)
     └─ app.js:1517-1537  设置 vlMed、显示 playView、v.src = /api/library/stream?path=…
         └─ v.play()  ← 自动播放（pr.catch 吞掉被拦）
             └─ [事件] app.js:1584  play → syncStart(vlMed.path)
                 └─ app.js:2394-2400  只校验 DEV.connected / syncEnabled() / path
                     └─ POST /api/sync/start
                         └─ host_server.py:2455-2464  只校验 path 非空 + 设备已连
                             └─ SyncEngine.start()  仅加载 funscript，不碰 preset
                    ⇒ 预设循环（preset_player.py:139-173，force=True 写帧）
                      与脚本同步（sync_engine.py:124，force=True 写帧）**同时驱动同一 BLE 通道**
回程：/api/device/state 2s 轮询 → renderDev() 仍显示「暂停预设」（因为没人停它）
```
**断点定位**：`app.js:1584` / `host_server.py:2464` 两处都缺 `preset.stop()`。手机端在同一位置有 `AppViewModel.kt:2190`。
**异常面**：两个 `force=True` 写者交错 → 设备来回抖；`vlPresetButton` 的告警态仍挂在按钮上，UI 与真实状态不一致。
**修复（推荐放宿主，一处覆盖所有调用方）**：
```python
# host_server.py:2464 之前
_pp = d["preset"]
if _pp.playing and not _pp.stop().get("state", {}).get("playing"):
    d["quick"].stop_orgasm(); d["quick"].stop_slow()   # = 手机端 startPresetPlayback 的第③件
self._json(d["sync"].start(vp))
```
> ⚠️ 注意时序：本仓库是 `<video>` **异步**起播，若在"点击"时就停预设，遇到打不开的编码（`vlVideoFail`）会白停一次。
> 建议：**点击时停**（对齐手机端"打开即退出"语义）或用 `play` 事件 + 宿主仲裁双保险。二者择一，**不要只在 `/api/sync/start` 里停**——
> 因为无脚本的视频 `SyncEngine.start` 返回失败，预设就不会被停。
>
> ✅ **但"退出预设"不等于"关闭视频"**（用户口径）：电脑端是**集成页面**，可以边看视频边用预设模式。
> 手机端 `AppViewModel.kt:2190/2546` 之所以同时 `closeVideo()`，是因为**视频页与预设页是两个独立页面**——
> 那是**平台性差异，PC 端刻意不对齐**。本仓库要保的是"**清脚本**"，不是"关视频"。
> 完整的写者所有权模型与三件事落地见 [设备写入所有权设计](设备写入所有权设计.md)。

### 链 B：暂停/继续视频（**断点：与快捷动作、预设的仲裁不对称**）
```
[video pause 事件] app.js:1575 → POST /api/quick {kind:"pause",on:true}
                    app.js:1585 timeupdate →（SYNC.on 仍为 true）→ POST /api/sync/tick  ← ⚠️
 └─ host:2562-2563 → QuickMoves.pause_for_player()  → 停 orgasm/slow、记 _*_resume 旗标
                                                      ✅ 不动 allow_move（探针确认）
[video play 事件]  app.js:1576 → POST /api/quick {kind:"resume",on:true}
 └─ host:2564-2565 → QuickMoves.resume_for_player() → 若旗标还在就**重新启动** orgasm/slow
```
**异常面（实测）**：暂停时 `SYNC.on` 不置 false → 暂停中拖动进度条，`timeupdate` 仍每 180ms 发 tick（`sync_engine._apply` 不判播放态）→ 设备跟着 seek 目标跑。
**异常面（语义）**：用户暂停 → 退出联动去点预设 → 再按播放 → `resume` 把之前暂停的爆发/缓动**又拉起来**，与预设抢设备。
**修复**：① `vlSyncVlUi` 的 pause 分支同时 `SYNC.on = false`（或 `syncTick` 里判 `v.paused`）；② `resume_for_player` 增加"预设正在播则不复位"的判断；③ 反之预设启动时清 `_orgasm_resume/_slow_resume` 旗标。

### 链 C：拖动任意联动滑轨（**断点：几何错位 + 快照过期 + 保存风暴**）
```
[pointerdown/move] app.js:1891-1905
 └─ app.js:1896  set(side, min + (ev.clientX - r.left)/r.width * (max-min))   ← ⚠️ 用整宽
     └─ app.js:1886 onChange(lo,hi) → 数值文本更新 + queueVl(patch)
         └─ app.js:1918-1924  300ms 防抖 → saveVl(patch)
             └─ app.js:1847  POST /api/settings { video_link: Object.assign({}, vlCfg(), patch) }  ← ⚠️ vlCfg() 是最长 1s 前的快照
                 └─ host:2416 save_settings（顶层整键替换）
                     └─ host:2422 _apply_device_settings() → apply_limits() 每次 submit 超时 10s
```
**三个断点**：
1. **几何错位（已量化）**：CSS 轨道两端各内缩 7.5px、把手 15px（`styles.css:929-937`），JS 却按整宽换算（`app.js:1896,1911`）⇒ 容器 260px 时，0-100 滑轨两端各丢 **≈5.8 点**，预设速度 1..500 两端各丢 **≈29 Units/s**，用户**拖不到 0 和 100**（也拖不到 1 和 500）。
2. **快照过期**：`vlCfg()` 读 `S.settings`（1s 轮询回填，`app.js:250`）而 `queueVl` 不更新本地值 ⇒ 300ms 内连改两项时，后一次 POST 带旧值把前一项**回滚**；对比 `bindLink`（`app.js:1996-1998`）手工补了本地值——同文件两种写法。
3. **保存风暴**：每次拖拽落盘都会走 `_apply_device_settings()`，其中 `apply_limits()` 是一个 `submit(timeout=10)` 的 BLE 往返；拖动中会连续触发。
**修复**：`(ev.clientX - r.left - 7.5)/(r.width - 15)`（或把 CSS 改成 0 内缩 + `transform` 定位）；`saveVl` 先本地认值；拖动期间只更新 UI、`pointerup` 才落盘设备限位。

### 链 D：点视频卡片后的卡片信息（**断点：数据源字段不存在**）
```
[GET /api/library/browse] host:2323-2333
 └─ host:2324-2325  with lib.lock: card = next((c for c in lib._cards if e2.path in c.get("paths", [])), None)
                                    └─ library.py:250-266 构造的卡片**没有 paths 键**（只有 parts[]）
                                        ⇒ card 恒为 None
                    ⇒ dur=0.0、pos=0.0、thumb=""（host:2330-2332）
回程：app.js:1421-1428  thumb 为空 → 显示通用图标（海报墙失效）；vlCount 正常但卡片无时长/进度
```
**证据**：`library.py` 全文 "paths" 只出现 **1 次**（`:100` 的读处），**没有任何写入点**（Lead 实测）。
**修复**：卡片加一行 `"paths": [p["path"] for p in parts]`，即可同时修好 `browse` 的 dur/pos/thumb 与 `set_progress` 的内存更新路径。

---

## 3. 阻断与高危（按修复优先级）

### 🔴 P0-1 急停按钮必然 500（第 1 轮确认，仍为最高优先）
`host_server.py:2551` `was = q.is_stop` —— `QuickMoves` 无此属性（`hasattr == False`），异常在 `q.set_stop(on)`（2552）**之前**抛出，被 2673 兜成 500。
⇒ `set_allow_move` 永不可达，`reset_last_index()` 也永不可达。**修复**：`was = not q.ch.state.allow_move`。

### 🔴 P0-2 22/24 个预设锁死 BLE 事件循环（第 1 轮确认，第 2 轮找到根因）
根因（[R14](R14-untouched-areas.md)）：手机端权威派生是
```kotlin
val playSegments = if (segments.isNotEmpty()) segments else toSegments(keyframes)  // PresetDefs.kt:66-67
```
宿主移植了播放端（`preset_player.py:160` 甚至保留了 `sg[3]` durationMs 的读取），**漏掉了 `toSegments` 这一步派生** ⇒ 22 个只有 keyframes 的预设拿到空 `segments` ⇒ `_play_one_loop` 里一个 `await` 都没有 ⇒ 纯 CPU 自旋。
Lead 实测：`normal`（有 segments）1 秒心跳 33 次正常返回；`classic`（只有 keyframes）**连 `await asyncio.sleep(1.0)` 都回不来**，8 秒看门狗强制杀进程。
**修复**：加载时派生 `playSegments`（把 `keyframes` 按相邻点转段，带 `durationMs`），与手机端 `toSegments` 对齐。

### 🔴 P0-3 用户规则未实现：预设播放中加载脚本不退出预设（§2 链 A）
**这是本次用户明确提出的判定基准，判定为不通过。** 修复见链 A。

### 🟠 P1-1 急停闸门结构性失效（三方里只有一方受约束）
`channel.py:252` 的闸门是 `if not allow_move and not force: return False`，但**五个调用点全部 `force=True`**（`sync_engine.py:124`、`preset_player.py:168`、`quick_moves.py:113/165`、`host:2505`）⇒ 闸门是**死代码**。
另外 `_slow_loop`（`quick_moves.py:156-171`）与 `preset_player` 循环内**完全不查** `allow_move`（`_orgasm_loop:108` 与 `_idle_watch:148` 查了）。
手机端对照：`BleDeviceService.kt:711-714` 的 `moveTo` 就是受约束的那个，`SyncEngine.kt:290`、`QuickMoves.kt:198/333` 每拍都查。
**修复**：区分"跳过重映射"与"跳过急停"两个概念——把 `force` 拆成 `raw`（只跳过 remap）与"允许在急停中写"（默认禁止），或让三个循环统一走 `moveTo`。

### 🟠 P1-2 断线后无法恢复动作 + 无复位路径
`host:2493-2500` 的 disconnect 顺序是**先 `disconnect()` 再停三方**：`ch.submit(d["ch"].disconnect(), timeout=20)` 一旦超时（`connect` 持锁时常见）就抛异常 → 后面 4 个 `stop` **全部跳过**；而 `channel.submit` 超时**不取消协程**（`channel.py:56-58`），迟到帧照写。
且 `channel._cleanup`（`:196-209`）不重置 `limit_min/limit_speed/allow_move`，`_ready` 也不清 ⇒ 重连后 `_ready` 仍真、急停态仍真，用户看到"连上了但设备不动"。
手机端对照：`AppViewModel.kt:3445/3300` 显式复位。
**修复**：`try/finally` 保证三方 stop；`submit` 超时后 `fut.cancel()`；`_cleanup` 复位 `allow_move=True`、`_ready=False`、限位。

### 🟠 P1-3 媒体库文件名未转义 → 同源 XSS（第 1 轮确认）
`app.js:1417-1432` 的 `d.name`/`v.name` 未过同文件已有的 `esc()`（`:85`）。同源可 `POST /api/device/move`、`/api/quit`。
**修复**：两处 `esc()`（一行改动）。

### 🟠 P1-4 目录穿越 + stream 白名单失效（第 1 轮确认）
- `host:2308-2314` `str(Path(bp))` 不折叠 `..`，`startswith(root+os.sep)` 通过后 `os.scandir` 落到真实目录（Lead 实测 `E:\testvideo\..\..\Windows` 通过校验且列出 `E:\`）。
- `host:2217-2222` 裸前缀比较（`E:\testvideo-evil` 命中 `E:\testvideo`），且非 frozen 时**整段放行**。
**修复**：`Path(bp).resolve()` + `is_relative_to`；开发放宽改用显式环境变量。

### 🟠 P1-5 预设速度不落盘 + 被别的设置拨回
`app.js:2283-2287` 滑轨每个 pointermove 发一次 `POST /api/preset{speed}`（无防抖），`preset_player.set_speed`（`:127-131`）只改内存；而 `host:3869-3870` 每次 `/api/settings` 保存都用磁盘里的 `device.preset_speed` 调 `set_speed()` ⇒ 拖完预设速度再拖任意滑轨，速度被拨回旧值，2s 轮询再把滑轨显示回去。
手机端对照：`presetSpeed` 防抖落盘（`AppViewModel.kt:206-217`）。
**修复**：加 300ms 防抖 + 同时写 `device.preset_speed`；启动时用它预置滑轨。

---

## 4. 手机端四态对照（61 条摘要）

### 4.1 本仓库**缺失**（13 条，按影响排序）
| # | 缺失 | 手机端依据 | 影响 |
|---|---|---|---|
| 1 | **预设↔脚本反向仲裁**（规范 P-1/P-2） | `AppViewModel.kt:2190,2546,3546` | 两个写者抢设备（链 A） |
| 2 | **预设启动时停快捷动作** | `:3534-3537` | 互斥是单向的（反向 `:2556` 却做了） |
| 3 | **脚本文件夹索引 + 手动加载/清除脚本** | `autoLoadForPath` `:1031-1290`；`Screens.kt:1719-1724` | 只认"视频同目录同名 .funscript" |
| 4 | **预设速度持久化** | `:206-217` | 见 P1-5 |
| 5 | **固件更新/OTA 整卡** | `Screens.kt:1980-2061` | `grep firmware|ota|wifi` = 0 |
| 6 | **内置播放器字幕渲染**（`<track>`） | 手机用 ExoPlayer 字幕轨 | 本仓库只显示 `has_srt` 徽章 |
| 7 | **异步代际丢弃**（过期响应） | `:2191/2548/2522` | 连点两个目录/视频会回退旧结果 |
| 8 | **seek 节流** | `SEEK_THROTTLE_MS` | 拖动进度条每帧都发 |
| 9 | **预设「恢复默认 100」+ 当前配速回显** | `Screens.kt:3856,3865-3867` | 无法一键复位 |
| 10 | **视频文件夹设置入口** | `VideoSettingsPage` `:2141` | — |
| 11 | **遥控器按键绑定** | `HidRemoteController.kt:51-60` 明写"PC 端按同一键绑定" | `ui/` 里 `keydown` 命中 0 |
| 12 | 设备信息「行程范围」 | `Screens.kt:1881` | `app.js:2296-2298` 少 `min_pos~max_pos` |
| 13 | 狂暴模式的版本门槛 | `Screens.kt:1756` | 无 `hw≥150 && fw≥897` 检查 |

### 4.2 本仓库**语义走偏**（14 条里最要紧的 6 条）
| # | 走偏 | 手机端 | 本仓库 |
|---|---|---|---|
| 1 | **急停对"待机缓动"无效** | `QuickMoves.kt:333` 每拍查 `allowMove` | `quick_moves.py:156-167` 无检查 + `force=True` |
| 2 | **待机缓动"运动速度"滑轨无效** | 间隔 = 行程÷速度（下限 100ms），`QuickMoves.kt:340` | `quick_moves.py:167` 写死 `sleep(1.0)`（默认值下恰好也是 1s，**自测难发现**） |
| 3 | **手动移动速度 500 vs 200** | `Screens.kt:1835` → `AppViewModel.kt:819-821` 固定 200 | `host:2504-2505` `speed=None` → `max_speed`（默认 500，**2.5 倍**） |
| 4 | **进预设判据窄** | `_script != null \|\| _videoActive` `:3514` | `SYNC.on`（`app.js:2033-2036`） |
| 5 | **跳过无动作三不同** | 默认 `false`/60s，语义是**seek 视频** | 默认 `true`/3s，语义只是**不推设备** ⇒ 脚本一静止 3s 设备就停、视频照播 |
| 6 | **区间外发满速帧** | `SyncEngine.kt:285-289` 一帧不发 | `sync_engine.py:97-100` `speed=0` → `:124 int(0) or None` → `channel.py:254` 取 `max_speed` ⇒ 播放头在**动作点区间之外**时发满速帧（**首/末动作点各一次**，每次开播或跳到末尾都会命中；探针实测 `speed参数=None → base=500`） |

### 4.3 本仓库**多出来**（6 条，建议删除或补说明）
设置页「播放视频时同步驱动设备」开关（手机端无，且关掉当次不生效）、设置页「同步延迟补偿」输入框（手机端只有 ±按钮）、`motionSeg`/`themeToggle2`/`setSlowSpeed`/`setOrgasmSpeed` 四个死引用、`elif False: pass` 死分支等。

---

## 5. 第 2 轮新增的其它确认缺陷

| 严重 | 问题 | 位置 |
|---|---|---|
| 中 | **BOOST 期间速度显示不归一化**：后端恢复逻辑正确（探针证伪了"锁死 500"），但前端 `psVal` 未按 `p.boost` 归一化，BOOST 中拖滑轨显示的是拖动值而非生效值 | `app.js:2276-2279` vs 手机 `Screens.kt:3788-3790` |
| 中 | **`browse()` 无 generation**：连点两个目录，慢的响应后到会覆盖 | `app.js:1439-1445` vs 手机 `:2191/2548` |
| 中 | **`vlPlayingPreset` 从未赋值** ⇒ 预设卡"▶"永不显示；且三处 2s 轮询覆盖用户操作回显 | `app.js:1392,2229` |
| 中 | **`allow_move=False` 后无 UI 提示**：一键急停后预设/缓动"看起来在播、设备不动" | `app.js` 全域 |
| 中 | **`_cleanup` 不重置限位与 `_ready`** | `channel.py:196-209` |
| 中 | **`thumb_name_for` 每个视频整份重读索引**（browse 内 O(N) 次 JSON 解析）；`_scan` 的 load→save 在锁外会覆盖 `set_progress` | `library.py:86-89,122-176` |
| 中 | **`video_link` 无数值校验**：`range_min > range_max` 时 `span<0` ⇒ 全部帧速度压成 0 且静默 | `host:551-563` |
| 中 | **退出/托盘零清理**：`request_quit` 走 `os._exit(0)`，不停设备循环、不断 BLE | `host:3560-3565,4132-4135` |
| 低 | `app.js:2101` 时间累加写坏（`sg[2]||0||…` 恒加 100ms）且丢掉 `sg[3]` durationMs；`_ALLOW_HEADERS` 死代码且头名写错；mpv DLL 缺失后不重试；懒加载无锁可建两份实例 | 见 [R14](R14-untouched-areas.md) |

---

## 6. 探针复现证据（Lead 亲自跑，全部只读）

| 探针 | 结论 |
|---|---|
| `probe_preset_keyspin_bounded.py` | `normal` 心跳 33 次正常返回；`classic`（只有 keyframes）**连 sleep 都回不来**，看门狗 exit 9 ⇒ 事件循环锁死 |
| `probe_device_settings.py` | `device.slow.max_speed=100` 被 `video_link.idle_speed=250` 覆盖；`device.{orgasm,slow}` 是只写不读的僵尸键 |
| `probe_reversed_final.py` | **推翻第 1 轮**：5 个用例 `ch.reversed` 终值全部跟随 `device.reversed` ⇒ 反转开关**有效**，`:3840` 只是死参数 |
| `probe_quick_gate.py` | **推翻 Lead 自己的假设**：`pause_for_player()` **不动** `allow_move`；急停后爆发被正确拦住；`allow_move` 的复位路径只有 `kind:"stop"` |
| `probe_quick_gate.py`（栅栏） | 急停期间预设/缓动仍写帧（`force=True`） |
| `probe_boost_restore.py` | **推翻 R12-N5**：180→BOOST(500)→拖 250→取消 = **250**（后端恢复正确，手机端同款语义） |
| `v_lead_sync_bounds.out.txt` | 播放头在动作点区间外时 `move_to(pos=10, speed=None)` → `channel` 解析为 **base=500 满速**；区间内正常（`speed=80`）。手机端此处不写帧 |
| 滑块几何计算 | 260px 容器下 0-100 两端各丢 ≈5.8 点；预设速度两端各丢 ≈29 Units/s |
| `paths` 字段核对 | `library.py` 里 "paths" **仅 1 次出现**（读），无写入 ⇒ `browse` 的 dur/pos/thumb 恒空 |
| `node --check ui/app.js` / `py_compile` | 全部通过（无语法问题） |

## 6.1 Lead 本轮亲自复核的结论（含 3 条推翻）

| # | 被复核的说法 | 裁定 | 依据 |
|---|---|---|---|
| 1 | 反转方向开关无效（第 1 轮） | ❌ **推翻** | `probe_reversed_final.py` |
| 2 | BOOST 后速度锁死 500（R12-N5） | ❌ **推翻** | `probe_boost_restore.py` |
| 3 | 暂停会把 `allow_move` 置 false（Lead 假设） | ❌ **自我推翻** | `probe_quick_gate.py` |
| 4 | 待机缓动"运动速度"滑轨无效 | ✅ **确认**：`quick_moves.py:159` 取了 `speed` 但 `:165` 用的是它、`:167` 却 `sleep(1.0)` 写死；手机端 `QuickMoves.kt:340` = `(max-min)*1000/speed`，下限 100ms | 两侧源码对照 |
| 5 | 手动移动速度 500 vs 手机 200 | ✅ **确认**：手机 `Screens.kt:1835` 传 200；本仓库 `app.js:2353` POST **不带 speed** → `host:2504-2505` → `channel.py:254` 取 `max_speed`(500) | 信号链逐跳核对 |
| 6 | 区间外发满速帧 | ✅ **确认**（"每次开播"表述收敛为"播放头在动作点区间外时"） | `v_lead_sync_bounds` |
| 7 | `paths` 字段只读不写 | ✅ **确认** | `library.py` grep + 模拟查找 |

---

## 7. 第 1 轮结论更正（重要：旧文档勿再引用）

| # | 第 1 轮结论 | 第 2 轮裁定 | 依据 |
|---|---|---|---|
| 1 | 「反转方向」开关无效、任何保存都清零 | ❌ **推翻**。开关有效；`:3858` 才是最终写入，`:3840` 是死参数 | `probe_reversed_final.py` |
| 2 | `device.{orgasm,slow}` 是"僵尸键"，建议删 | ✅ 成立（但"在设置页补控件"的备选方案作废——手机端设置页本来就没有） | [归属规范](设置页与联动页归属-对照手机端.md) §3.4 |
| 3 | "BOOST/RANDOM 绕过联动确认不是回退" | ✅ 成立（`toggle_boost` 不启动循环），但当时**不知道手机端有对称的反向确认**，所以"确认机制已完整"的说法不成立 | [R13](R13-parity-phone.md) §P-1/P-2 |
| 4 | 「同步与预设无互斥」 | ✅ 成立且**升级为阻断**（用户明确定为判定基准） | §2 链 A |
| 5 | 22/24 预设空转 | ✅ 成立，且**找到根因**（漏 `toSegments` 派生） | [R14](R14-untouched-areas.md) |
| 6 | 滑块/波形 DPR 修复"主体修对了" | ✅ 成立；但新增**几何错位**（轨道内缩 vs 整宽换算） | §2 链 C |
| 7 | R12-N5「BOOST 后速度锁死 500」 | ❌ **推翻**（后端恢复正确，只是前端不回显归一化） | `probe_boost_restore.py` |
| 8 | Lead 假设「暂停会把 allow_move 置 false 且无复位」 | ❌ **自我推翻**（`pause_for_player` 不动该闸门） | `probe_quick_gate.py` |

---

## 8. 修复路线图（按依赖排序，可直接照做）

### 第 1 批：安全与阻断（建议同批发布）
1. `host_server.py:2551` → `was = not q.ch.state.allow_move`（**急停复活**）
2. `preset_player` 加载时派生 `playSegments`（`segments` 空则用 `keyframes` 转段）→ **预设可用**
3. **急停闸门**：`channel.move_to` 的 `force` 拆成语义明确的 `bypass_estop`（[设备写入所有权设计](设备写入所有权设计.md) §4.2）；`_slow_loop`/`preset_player` 循环内补 `allow_move` 检查
4. **引入 `DeviceArbiter`**（单一写者不变量 I1）：`/api/sync/start`、`/api/preset`、`/api/quick` 三个入口统一仲裁（同文 §4.1）——一次性覆盖链 A、P-2、P-3、P-5 与"三方抢设备"的全部问题
5. **进预设 = 三件事**（清脚本 / 停快捷动作 / 开预设，**不关视频**）：宿主 `/api/preset` 的 play 分支原子完成（同文 §5）
6. `host_server.py:2493-2500` 用 `try/finally` 保证三方 stop；`submit` 超时 `fut.cancel()`；`_cleanup` 复位 `allow_move/_ready/limit/_last_pos`
7. `app.js:1418/1432` 两处 `esc()`；`host:2308-2314` 与 `2217-2222` 路径校验改 `resolve()+is_relative_to`

### 第 2 批：交互链正确性
7. 滑块几何：`(ev.clientX - r.left - 7.5) / (r.width - 15)`（或 CSS 改 0 内缩）
8. `saveVl` 先本地认值；拖动期间只更新 UI，`pointerup` 才下发设备限位
9. 链 B：暂停时 `SYNC.on = false` 或 `syncTick` 判 `v.paused`；`resume_for_player` 增加预设互斥
10. 预设速度：300ms 防抖 + 落盘 `device.preset_speed` + 启动预置
11. `app.js:1484` 的 `vlPlaying()` 并入 `vlInScriptLink()` 判据（对齐手机端 P-4）
12. **缓动速度真正生效**：`quick_moves.py:167` 的 `sleep(1.0)` 改成 `((hi-lo)*1000/speed)/1000` 且下限 0.1s（对齐 `QuickMoves.kt:340`）——否则"运动速度"滑轨是装饰
13. **手动移动速度**：`app.js:2353` 带上 `speed: 200`（对齐 `Screens.kt:1835`），或宿主 `/api/device/move` 缺省改成 200
14. **区间外不发帧**：`sync_engine.py:97-100` 在首/末动作点之外直接 return（对齐 `SyncEngine.kt:285-289`），避免满速冲刺
15. `library.py` 卡片补 `"paths"`（修 dur/pos/thumb）

### 第 3 批：设置面收敛（对照手机端）
16. 删设置页「播放视频时同步驱动设备」开关与「同步延迟补偿」输入框（手机端无）
17. 删 `device.{orgasm,slow}` 的死键与 `app.js:2320-2327` 残骸；`_apply_device_settings` 只保留 `idle_detect_seconds`
18. 「跳过无动作」默认值/语义对齐手机端（`false`/60s + seek 语义）
19. 清除 `:3840` 的 `reversed_` 死参数（可选清理）

### 第 4 批：优化与工程改进
20. `browse()`/`syncStart()` 加 generation；`vlPlayingPreset` 赋值；`pollDev` 补 `DEV.sync`；BOOST 期间 `psVal` 按 `p.boost` 归一化
21. `thumb_name_for` 改内存索引；`_scan` 的读改写收进锁
22. `range_min/max`、`max_speed` 在 `save_settings` 层夹紧并互相约束
23. 退出/托盘路径补设备清理（停循环 → 断 BLE → 再退出）
24. **补测试**（当前 `tests/` 对 `vendor/device/*` 零覆盖）：`QuickMoves` 属性契约、24 个预设各跑一轮断言让出、`_apply_device_settings` 的键映射、滑块几何单测

---

## 9. 改进方向（结构性建议）

1. **建立"真源对照"机制**：本轮的 61 条四态对照说明——跨三端（手机/头显/PC）的功能语义必须有**机器可读的对照清单**。建议把 `review/R13-parity-phone.md` 的四态表纳入 `tools/check_cross_repo_consistency.py`，作为发布前的人工核对项（该脚本目前只比文件哈希）。
2. **把"互斥矩阵"写进代码而不是散落在各处**：现状是 `preset.stop()` 只出现在 2 处、`allow_move` 语义被 `force` 破坏。建议在设备层引入一个显式的 `DeviceArbiter`：`acquire(owner)` / `release(owner)`，owner ∈ {script, preset, orgasm, slow}，所有写帧统一走它。两个 P0 与三条 P1 都能被它一次性解决。
3. **前端状态回显必须"以宿主为准"且集中**：`S.settings`(1s) / `DEV`(2s) / `libPollTimer`(1s/8s) 三套轮询 + 多处本地乐观更新，是链 C 与多处"不回显"的根因。建议引入单一 `applyState(state)` 渲染函数，禁止在事件处理器里直接改 DOM 值。
4. **协议层禁止"魔法布尔"**：`force=True` 同时承担"跳过重映射""跳过急停""强制写入"三种含义，是安全闸门失效的根因。建议改成独立的具名参数。
5. **异常必须可观测**：`except: pass` 与 500 兜底让 3 个 P0 长期潜伏（急停 500、预设锁死、`paths` 恒空）。建议设备层所有写入失败计数并暴露到 `/api/device/state`，UI 上可见。
6. **`host_server.py` 4139 行单文件**：建议按 `routes/` + `services/` 拆分（至少把 `/api/library/*`、`/api/device/*`、`/api/sync/*`、`/api/preset/*` 拆出去），否则每次审查都要靠 grep 定位，漏项概率高（本轮 61 条对照就是证据）。

---

## 10. 二次确认台账与用户裁定（2026-10-02 补记）

> 第 2 轮收尾时 Lead 做了二次确认探针并提出 4 个产品取舍（Q1–Q4）；因服务器问题会话中断，
> 结论一度只存在于对话里。本节由接手会话按用户原话补写进文档。**§8 路线图被 §10.3 的最终修复清单取代。**

### 10.1 二次确认台账（Lead 逐条复核）

| # | 说法 | 复核方式 | 裁定 |
|---|---|---|---|
| 1 | `range_min > range_max` 无校验 → 全部动作速度归零 | `probe_second_confirm.py` 喂真实 `_scale_speed` 90/10 | ✅ 成立：`span=-80 → _scale_speed(100)=-80 → sp=max(0,min(500,-80))=0`；`save_settings` 源码对 video_link 子键**零数值校验**（range/coerce/clip/min/max 全 false） |
| 2 | 断开先 `submit(timeout=20)` 再四个 stop，超时则四个 stop 全跳过 | 读 `do_POST` 源码顺序 | ✅ 成立：无 `try/finally`，`TimeoutError` 直接冒泡到兜底 500 |
| 3 | `_cleanup` 不复位 `allow_move`/`limit_*` | `channel.py:196-209` | ✅ 成立（但 `_ready=False` 有复位——R11 此点原文写错，已更正） |
| 4 | `state.recent` 无界增长 | `channel.py:268` | ❌ **不成立**：末尾 `[-12:]` 截断（R14 正确，R11 措辞过强），**不需修** |
| 5 | BOOST 速度锁死 500 | `probe_boost_restore.py` | ❌ 推翻（180→BOOST→拖250→取消 = 250） |
| 6 | 反转方向开关无效 | `probe_reversed_final.py` | ❌ 推翻（终值跟随 `device.reversed`） |
| 7 | 暂停置 `allow_move=false` | `probe_quick_gate.py` | ❌ 推翻 |
| 8 | `paths` 只读不写 → dur/pos/thumb 恒空 | grep + 模拟查找 | ✅ 成立（链 D） |
| 9 | `/api/device/move` 接受 `speed`，是前端不传 | grep 两侧 | ✅ 成立：host 收 `sp=body.get("speed")`，`app.js:2353` 只发 `{percent}` → 永远走 `max_speed`(500) |

### 10.2 用户裁定（Q1–Q4 + §三 4 条，2026-10-02）

| 问题 | 裁定 | 落地含义 |
|---|---|---|
| **Q1 急停范围** | **四个全受** | 急停 = `allow_move=false` **且四个会话（script/preset/orgasm/slow）全部停止本身**，不是"播放中但不写帧"；UI 显示已停止（预设按钮回未播放态、急停键激活态）；**解除急停不自动恢复任何会话**，要动需重新启动。备注：手机端是"按拍拦帧、解除即续"，PC 端**有意从严**——急停解除后动作自动恢复属于安全隐患；若用户不要这个从严可改回拦帧语义，成本低 |
| **Q2 跳过无动作** | **和手机保持一致** | 默认**关**、阈值 **60 秒**（UI 步进 5–3600）、语义 = **seek 视频**快进到下一动作点（不只是"不推设备"） |
| **Q3 固件更新/OTA** | **先不做** | 缺失 #5 结案：PC 端不做 OTA 卡 |
| **Q4 遥控器按键绑定** | **计划内，本轮不做** | 对应四大功能规划 **M3（遥控接收侧）**。用户硬件判断（M3 立项依据）：手机遥控用**手机的蓝牙**收 HID 键，**设备的蓝牙是外设端**，与宿主 BLE 控制链路是两个不同角色，可并存（同蓝牙键盘+鼠标并存）；键位规范以手机端 `HidRemoteController.kt:51-60` 为准 |
| §三#5 缓动"运动速度"滑轨 | 按建议（未改主意） | 跟手机端公式 `(max-min)*1000/speed`，下限 100ms |
| §三#6 手动"移动到该位置"速度 | 按建议 | 固定 **200** |
| §三#7 暂停后自动恢复 | 按建议 | 保留自动恢复，但**预设播放中不恢复**（不与预设抢设备） |
| §三#8 视频开播/脚本 vs 预设互斥 | 按建议 | **清脚本 + 停预设**即可，**视频继续播**（PC 集成页口径，不关视频） |

### 10.3 最终修复清单

以 [最终修复清单-R2.md](最终修复清单-R2.md) 为准：F1–F32 按四批排序，每条带验收判据，验收总表 A1–A9 建议落成 `tests/test_device_arbiter.py`。
