# R13 · 手机端（真源）↔ FunScriptCast-Nexus 逐功能对齐审计（第 2 轮）

- **真源**：`E:\Development\FunScriptCast`（Kotlin/Compose，手机端）
- **被审**：`E:\Development\FunScriptCast-Nexus`（PC/WebView，`ui/` + `host_server.py` + `vendor/device/`）
- **刻意分叉的第三仓库**：`E:\Development\VRFunScriptCast`（头显端）。凡"本仓库多出来"的功能，先判它是不是头显端职责，是则**不算走偏**（本文按此口径给判定）。
- 纪律：本次只读产品代码；本文件是唯一写入物。未启动任何 App/GUI/服务。

## 0. 口径

| 四态 | 含义 |
|---|---|
| **已对齐** | 行为语义一致（实现语言/机制可不同，如 Compose StateFlow ↔ HTTP 轮询） |
| **语义不同** | 两边都有这个功能，但**用户可感知**的行为不一样 → 必须写清差异 |
| **本仓库多出来** | 手机端没有；需判"是否头显端职责"，否则视为自造 |
| **本仓库缺失** | 手机端有、本仓库没有（或只剩死代码/死键） |

平台性差异的判据（本文只承认 3 类，其余一律按"要对齐"处理）：
1. 手机端有**外置播放器**（HereSphere/DeoVR 的 `PlayerLink`/`PlayerProtocol`），PC 端是内置 `<video>`/mpv → 外置播放器整条链无需对齐；
2. 手机端有 **Android/SAF/ALL_FILES 权限、ExoPlayer 解码、VR 投影、BLE HID 从机角色**，PC 端结构上不存在；
3. 手机端**页面拆分**（控制/视频/预设三页）与 PC 端**单页合并** → 布局差异不算 bug，但**功能点不能因此消失**。

---

## 1. 手机端功能点全清单（与设备/播放有关）

### A. 控制页 `ControlScreen`（`Screens.kt:1217-1236`）

| # | 功能点 | 手机端位置 | 作用 |
|---|---|---|---|
| A1 | 播放器卡（外置播放器） | `ui/Screens.kt:1429-1522` | 控制页专用播放器卡 |
| A2 | 脚本状态三态（脚本名/正在加载/播放器未连接/无对应脚本） | `ui/Screens.kt:1453-1459` | 顶栏左上状态 |
| A3 | 播放/暂停图标（只操作外置播放器） | `ui/Screens.kt:1487-1494` → `AppViewModel.kt:3616-3628` | `externalPlay/externalPause` |
| A4 | 进度时间 `mm:ss · mm:ss` | `ui/Screens.kt:1497-1502` | — |
| A5 | 热力图 + 点击跳转 | `ui/Screens.kt:1506-1511` | `ScriptSeekHeatmap` |
| A6 | 三角滑块微调 | `ui/Screens.kt:1515-1518` | `TriangleSeekSlider` |
| A7 | 快捷动作：待机缓动 | `ui/Screens.kt:1248-1254` → `AppViewModel.kt:3471-3478` | 激活变绿、文字切「停止缓动」 |
| A8 | 快捷动作：一键急停 | `ui/Screens.kt:1255-1260` → `AppViewModel.kt:3453-3460` | 激活变蓝、文字切「一键继续」 |
| A9 | 快捷动作：一键爆发 | `ui/Screens.kt:1261-1267` → `AppViewModel.kt:3462-3469` | 激活变红、文字切「停止爆发」 |
| A10 | 设备行程与速度：限制输出范围 | `ui/Screens.kt:1278-1287` → `AppViewModel.kt:785-788` | 双点滑轨 0-100 |
| A11 | 设备行程与速度：设备速度上限 | `ui/Screens.kt:1288-1297` → `AppViewModel.kt:780-783` | 0..`speedLimitMax=500`（`AppViewModel.kt:773`） |
| A12 | 待机缓动：运动范围 + 关联输出 | `ui/Screens.kt:1311-1338` | 关联时滑轨禁用并跟主范围 |
| A13 | 待机缓动：运动速度 | `ui/Screens.kt:1339-1349` → `AppViewModel.kt:3562-3566` | 0..500 |
| A14 | 一键爆发：运动范围 + 关联输出 | `ui/Screens.kt:1363-1390` | — |
| A15 | 一键爆发：运动速度 + 关联上限 | `ui/Screens.kt:1391-1411` | 关联时跟主限速 |
| A16 | 「□关联输出」复选框 | `ui/Screens.kt:1417-1422` | 通用组件 |

### B. 视频页 `VideoScreen`（`Screens.kt:2271-2413`）

| # | 功能点 | 手机端位置 | 作用 |
|---|---|---|---|
| B1 | 视频卡头（标题 + 当前视频名/未打开） | `ui/Screens.kt:2316-2326` | — |
| B2 | 继续上次 | `ui/Screens.kt:2348-2354` → `AppViewModel.kt:2043-2055` | 恢复上次视频 + 位置 |
| B3 | 选择视频 | `ui/Screens.kt:2355-2360` → `AppViewModel.kt:1584-1595` | 打开源选择弹窗 |
| B4 | 关闭当前 | `ui/Screens.kt:2361-2367` → `AppViewModel.kt:2289-2301` | `closeVideo()` |
| B5 | 播放错误提示 | `ui/Screens.kt:2370-2378` | 解码失败等 |
| B6 | 脚本热力图卡（视频状态/播放暂停/进度/脚本状态/热力图/三角滑块） | `ui/Screens.kt:3599-3678` | 视频页专用 |
| B7 | 视频表面 + 顶栏/底栏/锁/更多/AI/VR 面板 | `ui/Screens.kt:2552-3120`、`ui/PlayerControls.kt:152/197/261/2416-2515` | — |
| B8 | 上一个/下一个 + 播放结束自动连播 | `ui/PlayerControls.kt:220-230`、`AppViewModel.kt:2260/564-568` | `playNextVideo` |
| B9 | 循环模式/画面比例/倍速 | `ui/PlayerControls.kt:458-471`、`data/VideoPlayerController.kt:237` | `setPlaybackSpeed` |
| B10 | 全屏 / 显示模式 | `AppViewModel.kt:1514-1528` | — |
| B11 | 可选视频源：本地 / WebDAV / SMB / DLNA | `ui/Screens.kt:3305-3578` | `VideoSourceDialog` |
| B12 | 续播记忆（逐视频 key + 5 秒周期保存 + LRU 清理） | `AppViewModel.kt:1976-2028` | `saveVideoResume` |
| B13 | 视频页复用：快捷动作 / 行程速度 / 缓动 / 爆发四卡 | `ui/Screens.kt:2398-2404` | 与控制页同组件 |
| B14 | AI 字幕随视频启停 | `AppViewModel.kt:340-360`、`ui/Screens.kt:2416-2436` | — |
| B15 | 播放/暂停联动快切（急停/缓动让路） | `AppViewModel.kt:508-528` → `sync/QuickMoves.kt:242-263` | `pauseForPlayer/resumeForPlayer` |

### C. 预设页 `PresetScreen`（`Screens.kt:3681-3746`）

| # | 功能点 | 手机端位置 | 作用 |
|---|---|---|---|
| C1 | 设备行程卡（只有限制输出范围） | `ui/Screens.kt:3750-3765` | — |
| C2 | 预设网格（两列、一屏 3 行、可滚动、选中自动定位） | `ui/Screens.kt:3772-3843` | — |
| C3 | 预设卡片：名称 + **播放中「▶」** + 循环配速 + 波形 + 选中高亮 | `ui/Screens.kt:3936-3969`、`4100-4104` | 配速 = 行程÷速度（跟滑轨） |
| C4 | 预设定义 24 个 | `sync/PresetDefs.kt:90-452`（`DEFAULT_ID="normal"` `:466`） | — |
| C5 | 预设速度滑轨 1..500 + 「恢复默认」+ 100 时显示「（原始）」 | `ui/Screens.kt:3854-3869` | — |
| C6 | 预设速度**防抖持久化** | `AppViewModel.kt:206-221`（`persistDebounced` `:247-253`） | 落 `prefs["presetSpeed"]` |
| C7 | BOOST 圆钮（记速/滑块跳 500/取消恢复并写回） | `ui/Screens.kt:3884-3893` → `AppViewModel.kt:3489-3505` | 500 不持久化 |
| C8 | 播放/暂停圆钮（**二次确认唯一入口**） | `ui/Screens.kt:3895-3908` → `AppViewModel.kt:3508-3519` | `requestPresetPlay` |
| C9 | RANDOM 圆钮（无确认） | `ui/Screens.kt:3910-3918` → `AppViewModel.kt:3485-3487` | — |
| C10 | 进入预设模式确认框 | `ui/Screens.kt:3718-3730` | 「切换到预设模式？」 |
| C11 | 预设模式中加载脚本确认框 | `ui/Screens.kt:3733-3745` | 「加载脚本？」 |
| C12 | 点网格卡片 = 选中并退出 RANDOM | `sync/PresetPlayer.kt:56-59` | — |
| C13 | RANDOM 语义（关→保持当前；开→在播立即跳） | `sync/PresetPlayer.kt:94-104` | — |
| C14 | `startPresetPlayback()` 四件事 | `AppViewModel.kt:3531-3539` | 清脚本+关视频+停快捷动作+开预设 |
| C15 | 播放循环：先发后等、20ms 步进、`waitMs=natural×segSpeed/speed` | `sync/PresetPlayer.kt:126-147` | — |
| C16 | `effectiveSpeed()` = clamp(1..500) 再 min(设备上限) | `sync/PresetPlayer.kt:116-120` | — |

### D. 设置页 · 设备与同步 `DeviceSettingsPage`（`Screens.kt:1738-2063`）

| # | 功能点 | 手机端位置 | 作用 |
|---|---|---|---|
| D1 | 待机缓动 · 空闲判定秒数（步进 1-600） | `ui/Screens.kt:1766-1784` | `slow.idleDetectSeconds` |
| D2 | 跳过无动作部分 开关 + 判定时长（5-3600，默认 60） | `ui/Screens.kt:1787-1817` → `AppViewModel.kt:896-913` | 默认 **关** |
| D3 | 手动控制位置（滑轨 + 「移动到该位置」） | `ui/Screens.kt:1820-1840` → `AppViewModel.kt:819-821` | **固定速度 200** |
| D4 | 延迟调整（-100/-10/+10/+100，无输入框） | `ui/Screens.kt:1843-1857` → `AppViewModel.kt:775-778` | — |
| D5 | 设备信息（设备名/硬件/固件/**行程范围**/最大速度）+ 刷新 | `ui/Screens.kt:1862-1886` → `AppViewModel.kt:825-828` | — |
| D6 | 伪装设备（VorzePiston 模式） | `ui/Screens.kt:1891-1906` → `AppViewModel.kt:807-810` | `setA10Mode(1)` |
| D7 | 反转方向 | `ui/Screens.kt:1907-1922` → `AppViewModel.kt:790-794` | 通道 invert |
| D8 | 电机狂暴模式 + 版本门槛 + 确认框 | `ui/Screens.kt:1923-1953`、`1955-1977` | 门槛 `:1756`（hw≥150 且 fw≥897） |
| D9 | 固件更新 / OTA（检查更新/扫 WiFi/SSID+密码/开始更新/进度） | `ui/Screens.kt:1980-2061` → `AppViewModel.kt:830-901` | — |
| D10 | 设备实况回读种子（`motorPower==100`→狂暴；通道身份→伪装） | `AppViewModel.kt:715-739` | 意图状态为唯一真源 |

### E. 设置页其他子页 / 其他页

| # | 功能点 | 手机端位置 | 作用 |
|---|---|---|---|
| E1 | 脚本与脚本源（脚本文件夹/索引进度/手动选脚本/清除/加载状态） | `ui/Screens.kt:1650-1735` → `AppViewModel.kt:979-1018/1174-1290/3556-3610` | 播放相关 |
| E2 | 视频设置（视频文件夹） | `ui/Screens.kt:2141-2156` | — |
| E3 | AI 字幕设置（含字幕样式 3 项） | `ui/Screens.kt:2066-2139` → `AppViewModel.kt:224-308` | 播放相关 |
| E4 | 遥控器页（HID 键盘从机：F13-F19 键位表） | `ui/RemoteScreen.kt:101-620`、`ble/HidRemoteController.kt:51-60` | **PC 端绑定用** |
| E5 | 设置首页菜单（7 个二级页） | `ui/Screens.kt:1568-1599` | — |

### F. 引擎层（跨页，设备语义的真正所在）

| # | 功能点 | 手机端位置 | 作用 |
|---|---|---|---|
| F1 | 脚本同步循环 20ms、时钟外推+上报校正、`externalControl` 让路 | `sync/SyncEngine.kt:211-340`、`120-131`、`343` | 爆发接管时暂停同步 |
| F2 | 跳无动作段 = **seek 播放器**（含头部段/1s 冷却/延迟换算） | `sync/SyncEngine.kt:139-201` | 默认关、阈值 60 |
| F3 | 急停→继续强制重发当前段 | `sync/SyncEngine.kt:99-102` | `resetLastIndex` |
| F4 | 快捷动作：爆发/缓动互斥、空闲判定、外部动作让路、权威存档 | `sync/QuickMoves.kt:41-55/141-192/242-263/346-372` | `coerceIn` 读档夹紧 |
| F5 | 缓动循环节奏 = 行程÷速度（下限 100ms） | `sync/QuickMoves.kt:330-343` | 速度滑轨真正生效处 |
| F6 | 设备通道：`moveTo`(重映射+速度缩放) / `forceMoveTo` / `forceMoveToInverted`(只反转) | `ble/BleDeviceService.kt:711-746` | — |
| F7 | 临时限位 0x42 下发 + 反转 + 连接期唤醒锁 | `ble/BleDeviceService.kt:748-761`、`AppViewModel.kt:3400-3421` | — |

---

## 2. 四态对照表

> 计数：**已对齐 28 · 语义不同 14 · 本仓库多出来 6 · 本仓库缺失 13**（明细见下，编号与第 1 节对应）

### 2.1 已对齐（28 条）

| # | 功能点 | 手机端 | 本仓库 | 备注 |
|---|---|---|---|---|
| 1 | 预设集合 24 个、ID/顺序一致 | `sync/PresetDefs.kt:90-452` | `ui/presets.json`（24 条，`normal` 起） | 逐 ID 比对一致 |
| 2 | 预设速度滑轨 1..500/默认 100 | `ui/Screens.kt:3859-3864` | `ui/index.html:476-479` + `ui/app.js:2283-2287` | 仅缺默认按钮，见 2.4-11 |
| 3 | 播放循环"先发后等"+20ms 步进+速度倍率 | `sync/PresetPlayer.kt:126-147` | `vendor/device/preset_player.py:154-173` | 公式逐项一致 |
| 4 | `effectiveSpeed` = clamp(1..500) → min(设备上限) | `sync/PresetPlayer.kt:116-120` | `vendor/device/preset_player.py:67-70` | — |
| 5 | BOOST：记速 / 滑轨跳 500 / 取消恢复且 500 不落盘 | `AppViewModel.kt:3489-3505` | `preset_player.py:114-125` | 恢复值落盘问题见 2.3-11 |
| 6 | RANDOM：关→保持当前继续；开→在播立即跳 | `sync/PresetPlayer.kt:94-104` | `preset_player.py:103-112` | — |
| 7 | 点卡片 = 选中并退出 RANDOM | `sync/PresetPlayer.kt:56-59` | `preset_player.py:76-81` | — |
| 8 | 播放/暂停一键切换 | `sync/PresetPlayer.kt:62-64` | `preset_player.py:83-84` | — |
| 9 | 预设页行程卡（仅限制输出范围） | `ui/Screens.kt:3750-3765` | `ui/app.js:1933-1936`（stroke 页签） | 布局不同、内容一致 |
| 10 | 快捷动作三按钮 + 绿/蓝/红激活态 + 文字切换 | `ui/Screens.kt:1238-1270` | `ui/app.js:2013-2021`、`2265-2270` | — |
| 11 | 急停 = `allowMove` 开关；急停→继续强制重发当前段 | `AppViewModel.kt:3453-3460`、`SyncEngine.kt:99-102` | `host_server.py:2550-2554`、`sync_engine.py:74-77` | — |
| 12 | 爆发/缓动互斥 | `sync/QuickMoves.kt:152-153/125-126` | `quick_moves.py:87-88/125-126` | — |
| 13 | 爆发间隔 = 行程÷速度、50ms 下限、急停期空转 | `sync/QuickMoves.kt:194-207` | `quick_moves.py:99-115` | — |
| 14 | 缓动：设备空闲 N 秒后才动；外部动作重置计时并取消循环但保持启用 | `sync/QuickMoves.kt:116-132/290-310/330-343` | `quick_moves.py:60-76/140-154` | 节奏公式除外→2.3-2 |
| 15 | 硬件保护上限 500 | `AppViewModel.kt:773` | `preset_player.py:11/30`、`channel.py:261` | — |
| 16 | 行程与速度卡：范围 + 速度上限 | `ui/Screens.kt:1274-1299` | `ui/app.js:1933-1936` | — |
| 17 | 待机缓动卡：范围 + 关联 + 速度 | `ui/Screens.kt:1303-1351` | `ui/app.js:1937-1944` | — |
| 18 | 一键爆发卡：范围 + 关联 + 速度 + 关联上限 | `ui/Screens.kt:1355-1413` | `ui/app.js:1945-1955` | — |
| 19 | 空闲判定秒数（设备段） | `ui/Screens.kt:1766-1784` | `index.html:773-775` + `app.js:2342` + `host_server.py:3854-3855` | 范围夹紧见 2.3-8 |
| 20 | 手动控制位置（滑轨 + 移动按钮） | `ui/Screens.kt:1820-1840` | `index.html:783-790` + `app.js:2345-2356` | 速度差异→2.3-8 |
| 21 | 延迟调整 ± 按钮 | `ui/Screens.kt:1843-1857` | `index.html:791-799` + `app.js:2357-2364` | 多一个输入框→2.3-2 |
| 22 | 设备信息 + 刷新 | `ui/Screens.kt:1862-1886` | `app.js:2295-2298/2336-2338`、`host_server.py:2516-2518` | 缺"行程范围"→2.4-4 |
| 23 | 伪装/反转/狂暴三开关 + 狂暴二次确认 | `ui/Screens.kt:1888-1977` | `index.html:800-814` + `app.js:2365-2371` | 映射表反了→2.2；无门槛→2.4-7 |
| 24 | 扫描并连接 / 断开设备 | `ui/Screens.kt:252-278` | `app.js:2430-2458`、`host_server.py:2477-2500` | — |
| 25 | 热力图：7 档色带 / 速度窗 50 / 位置窗 15 / 间隙 5s / 基准 max(时长,脚本末帧) | `ui/ScriptHeatmap.kt:138-267` | `ui/app.js:1688-1800` | 逐参数一致（含 `n=120`、条形几何、gap 重置 `continue`） |
| 26 | 播放/暂停联动快切（暂停停缓动+爆发，恢复续上） | `AppViewModel.kt:508-528`、`QuickMoves.kt:242-263` | `ui/app.js:1575-1576`、`host_server.py:2562-2565` | — |
| 27 | 播视频自动开始脚本同步 | `sync/SyncEngine.kt:211-340`（跟播放器状态） | `ui/app.js:1584` → `host_server.py:2455-2464` | — |
| 28 | 预设网格两列 + 选中高亮 + 以宿主为准 + 滚动到可见 | `ui/Screens.kt:3772-3843` | `ui/app.js:2191-2245` | 缺「▶」→2.4-12 |

### 2.2 语义不同（14 条）

#### 2.2-1【高·安全】急停期间"待机缓动"仍在驱动设备
```kotlin
// 手机端 sync/QuickMoves.kt:330-338
private suspend fun slowLoop() {
    while (_isSlowActive.value) {
        val (min, max, speed) = linkedSlow()
        if (speed <= 0 || !ble.allowMove) { delay(1000); continue }   // ← 每拍检查急停
```
```python
# 本仓库 vendor/device/quick_moves.py:156-167
async def _slow_loop(self, gen: int) -> None:
    s = self.slow
    ...
    while self.is_slow and gen == self._gen_s:
        self._slow_index += 1
        target = lo if self._slow_index % 2 else hi
        self._self_moving = True
        await self.ch.move_to(target, speed, force=True, raw=True)   # ← force=True 绕开 allow_move
```
结论：`move_to` 的急停检查是 `if not allow_move and not force`（`channel.py:252-253`），而缓动循环恒定 `force=True` 且**循环内没有任何 `allow_move` 检查**（爆发循环有，`quick_moves.py:108`）→ 用户点「一键急停」后，正在跑的缓动循环继续往返写帧，**急停对缓动无效**。平台性差异：否。
建议（最小）：在 `_slow_loop` 的 while 内加与爆发一致的门：
```python
if not self.ch.state.allow_move:
    await asyncio.sleep(1.0); continue
```

#### 2.2-2【中高】"待机缓动 · 运动速度"滑轨在本仓库不生效
```kotlin
// 手机端 sync/QuickMoves.kt:337-341
val target = if (slowIndex % 2 == 0) min else max
slowForceMove(target, speed)
slowIndex++
val intervalMs = ((max - min) * 1000.0 / speed).toLong().coerceAtLeast(100)
```
```python
# 本仓库 vendor/device/quick_moves.py:163-167（固定 1 秒，速度不参与）
target = lo if self._slow_index % 2 else hi
self._self_moving = True
await self.ch.move_to(target, speed, force=True, raw=True)
self._self_moving = False
await asyncio.sleep(1.0)
```
结论：手机端间隔 = 行程÷速度（下限 100ms）；本仓库固定 1s → **拖动「运动速度」滑轨完全看不出变化**（默认 0-100/速度 100 时两边恰好都是 1s，所以自测不易发现）。平台性差异：否。建议照抄手机公式，并把 `span/speed` 的 100ms 下限补上。

#### 2.2-3【高】"预设模式 ⇄ 脚本"只有单向互斥，反向零仲裁
```kotlin
// 手机端 AppViewModel.kt:2190（本地视频）与 :2546（远程视频）
_presetPlayer.value?.stop()  // 互斥：进入内置视频模式前停止预设播放
```
```javascript
// 本仓库 ui/app.js:2394-2400：只判设备/开关/路径，不判预设
function syncStart(path) {
  if (!DEV.connected || !syncEnabled() || !path) return;
  api("/api/sync/start", "POST", { path: path }).then(function (r) {
```
结论：预设播放中点媒体库视频 → `<video> play` → `syncStart()`；`/api/sync/start`（`host_server.py:2455-2464`）只做 `d["sync"].start(vp)`，**不 `preset.stop()`** → 预设循环与脚本同步同时以 `force=True` 写同一 BLE 通道。平台性差异：否（用户已明确要求对齐）。改法与规范见 §4。
附带证据：`DEV.preset.playing` 全仓只被读来做按钮文案（`ui/app.js:2272`），从未参与仲裁。

#### 2.2-4【中】进预设模式的触发判据比手机端窄
```kotlin
// 手机端 AppViewModel.kt:3514
if (_script.value != null || _videoActive.value) { _presetModeConfirm.value = true }
```
```javascript
// 本仓库 ui/app.js:2033-2036
function vlInScriptLink() {
  /* 联动模式 = 脚本同步正在跑（宿主已接受 /api/sync/start）*/
  return !!(typeof SYNC !== "undefined" && SYNC.on);
}
```
结论：手机端判"脚本已加载 **或** 内置视频在播"；本仓库只判 `SYNC.on`（= 设备已连 + `script_sync` 开 + 脚本解析成功）。**视频在播但没脚本 / 没连设备时，本仓库不拦**，用户点一下预设就直接切断播放语境。平台性差异：否。建议：`vlInScriptLink()` 追加 `|| vlPlaying()`（`ui/app.js:1484` 已有该函数）。

#### 2.2-5【中】`startPresetPlayback()` 四件事只做了两件的一半
```kotlin
// 手机端 AppViewModel.kt:3531-3538
private fun startPresetPlayback() {
    clearLoadedScript(clearPathMemory = false)   // ① 清脚本
    closeVideo()                                 // ② 关内置视频
    _quickMoves.value?.let { it.stopOrgasm(); it.stopSlow() }  // ③ 停快捷动作
    _presetPlayer.value?.start()                 // ④ 开预设
```
```javascript
// 本仓库 ui/app.js:2056-2060（第二击）
if (vlArmed && vlArmed.id === btn.id) {
  vlDisarm();
  syncStop();                     // ← 只对应 ①
  run();                          // ← ④ presetCmd("toggle_play")
  return;
}
```
结论：①✅（`syncStop`）④✅；②本仓库**不该做**（视频页就是播放器本体，平台性/结构性差异，无需对齐）；③**缺失** → `/api/preset` 的 `play/start`（`host_server.py:2576-2577`）不 `stop_orgasm()/stop_slow()`，而反向 `/api/quick` 却会 `d["preset"].stop()`（`host_server.py:2556`）→ **互斥是单向的**。建议（宿主侧，双入口一起修）：`/api/preset` 的 start 分支补 `q.stop_orgasm(); q.stop_slow()`。

#### 2.2-6【中】"跳过无动作部分"：默认值 / 范围 / 语义三处都不同
| | 手机端 | 本仓库 |
|---|---|---|
| 默认开关 | **关** `AppViewModel.kt:896` | **开** `ui/app.js:2323`（`d.skip_idle !== false`，缺键即开） |
| 默认阈值 | 60s `AppViewModel.kt:899` | 3s `ui/app.js:2324`（`d.idle_threshold \|\| 3`）+ `sync_engine.py:24` |
| UI 范围 | 5–3600 步进 5 `ui/Screens.kt:1810-1813` | 1–60 步进 1 `ui/index.html:781` |
| 语义 | 命中无动作段 → **seek 播放器跳过该段** `SyncEngine.kt:181-201` | 命中 → **只是不推设备帧**，视频照播 `sync_engine.py:112-115` |

结论：本仓库默认"开 + 3 秒门槛"，脚本里任何 ≥3 秒的静止段都会让设备停住（视频继续），用户会以为设备卡了；而手机上这个开关默认关、且开启时是"跳过视频"。平台性差异：局部（"seek 内置 `<video>`"完全可做，`vlSeekTo` 已存在）。建议：默认改 `false/60`；若要齐语义，则在 `sync_engine` 命中时回一个"请求 seek 到 gap 尾"的信号（前端已有 `vlSeekTo`）。

#### 2.2-7【中】`QuickMoves` 权威存档 vs 本仓库"死键 + 双份真相"
```kotlin
// 手机端 sync/QuickMoves.kt:360-372（唯一权威存档，读档即夹紧）
private fun loadSlow(): SlowSettings {
    val raw = prefs.getString(KEY_SLOW, null) ?: return SlowSettings()
    ... o.optInt("maxMoveSpeed", 100).coerceIn(0, SPEED_LIMIT_MAX) ...
    ... o.optInt("idleDetectSeconds", 5).coerceIn(1, 60) ...
}
```
```javascript
// 本仓库 ui/app.js:2325-2327（读 HTML 里根本不存在的 id）
if ($("#setSlowIdle")) $("#setSlowIdle").value = SET_DEV.slow.idle_detect_seconds;
if ($("#setSlowSpeed")) $("#setSlowSpeed").value = SET_DEV.slow.max_speed;   // ← 死引用
if ($("#setOrgasmSpeed")) $("#setOrgasmSpeed").value = SET_DEV.orgasm.max_speed; // ← 死引用
```
结论：`DEFAULT_SETTINGS.device.{slow,orgasm}`（`host_server.py:326-329`）里的 `max_speed`/`min/max_percent`/`link_*` 全部是**只写不读的死键**：`/api/device/settings` 会合并存盘（`:2522-2524`），`_apply_device_settings()` 只读 `slow.idle_detect_seconds`（`:3854-3855`），真实设备参数一律取自 `video_link.{idle_*,burst_*}`（`:3844-3852`）；前端也从不 POST 它们。所以"死键"是真的，但**功能没丢**（`video_link` 那一份是活的且已接通）。平台性差异：否。建议：删死键与死引用（与 `review/设置页与联动页归属-对照手机端.md` §3.4 一致），并给 `video_link` 的读档加夹紧（手机端读档一律 `coerceIn`）：`slow.max_speed`→`[0,500]`、`idle_detect_seconds`→`[1,60]`、`min/max`→`[0,100]`。

#### 2.2-8【中高】"移动到该位置"的速度不同（安全相关）
```kotlin
// 手机端 ui/Screens.kt:1835 → AppViewModel.kt:819-821
onClick = { vm.manualMoveTo(manualPosition.roundToInt(), 200) }
fun manualMoveTo(percent: Int, speed: Int) { activeChannel()?.forceMoveTo(percent, speed) }
```
```python
# 本仓库 host_server.py:2504-2505（speed 缺省 → channel.py:254 取 self.max_speed）
sp = body.get("speed")
ok = d["ch"].submit(d["ch"].move_to(pct, int(sp) if sp is not None else None, force=True), timeout=10)
```
结论：手机端固定 200 Units/s；本仓库用「设备速度上限」（默认 500，用户可调更高）→ 同一按钮在本仓库**最快可达 2.5 倍速**，且"手动控制位置"本是精细操作入口。平台性差异：否。建议：`ui/app.js:2353` 的 `/api/device/move` 带上 `speed: 200`（或宿主缺省改 200）。

#### 2.2-9【低】关联勾选：手机端循环内实时求值，本仓库在 `apply()` 时烘焙并改写存档
```kotlin
// 手机端 sync/QuickMoves.kt:312-317（每拍现读 provider）
private fun linkedSlow(): Triple<Int, Int, Int> {
    val s = _slowSettings.value
    val min = if (s.linkPercent) rangeProvider().start.toInt() else s.minPercent
```
```python
# 本仓库 vendor/device/quick_moves.py:203-208（把结果写回设置本身）
if self.orgasm.link_percent:
    self.orgasm.min_percent, self.orgasm.max_percent = int(self.ch.range_lo), int(self.ch.range_hi)
```
结论：本仓库在 `_apply_device_settings()` 时机烘焙，且**改写 `min/max` 存档**；手机端不改写、每拍现算（行程滑轨改动立刻生效）。实际影响有限（下一次 `/api/settings` 会用 `video_link.idle_min/max` 重新覆盖回来，`host_server.py:3845-3852`），但"关联期间改主行程 → 爆发/缓动范围不跟手"仍可感知。平台性差异：否。建议（低优先）：把 `link_*` 改成循环内求值（`quick_moves.py:100/157` 附近），不再回写设置。

#### 2.2-10【低】爆发/缓动运行中修改设置不实时生效
```python
# 本仓库 vendor/device/quick_moves.py:99-104（循环外取一次快照）
async def _orgasm_loop(self, gen: int) -> None:
    s = self.orgasm
    lo, hi = int(s.min_percent), int(s.max_percent)
```
```kotlin
// 手机端 sync/QuickMoves.kt:194-197（每拍重读，且 linkedOrgasm() 现算）
while (_isOrgasmActive.value) {
    val (min, max, speed) = linkedOrgasm()
```
结论：本仓库在拖动范围/速度时必须先停再开才生效。平台性差异：否。建议：把 `s = self.orgasm` 挪进 while（与 2.2-9 同一处改）。

#### 2.2-11【中】预设速度：不落盘，且会被**其它设置保存**静默拨回
```kotlin
// 手机端 AppViewModel.kt:212-216（防抖落盘）+ 3495-3496（BOOST 取消也写回）
_presetSpeed.value = clamped
persistDebounced("presetSpeed") { prefs.edit().putInt("presetSpeed", clamped).apply() }
```
```javascript
// 本仓库 ui/app.js:2283-2287（每帧 POST 一次，且不落盘）
initSlider("vlPresetSpeed", false, function (lo, hi) {
  var v = Math.max(1, Math.round(hi));
  presetCmd("speed", { speed: v });
});
```
```python
# 本仓库 host_server.py:3869-3870（任何一次设置保存都会把运行值拨回磁盘旧值）
if dev.get("preset_speed"):
    d["preset"].set_speed(int(dev["preset_speed"]))
```
结论：`preset_player.set_speed()`（`preset_player.py:127-131`）只改内存，`device.preset_speed` 前端从不写 → ①重启回 100；②**拖完预设速度，再拖任意其它滑轨（会 POST `/api/settings`）→ 预设速度被 `_apply_device_settings()` 拨回磁盘旧值 100**，且 2 秒轮询把它显示回滑轨上（用户看到"滑轨自己跳回去"）。平台性差异：否。建议（照手机端）：`#vlPresetSpeed` 加 300ms 防抖，同时发 `/api/preset {action:"speed"}` 与 `/api/device/settings {preset_speed:v}`；启动用 `/api/settings` 的 `device.preset_speed` 预置滑轨；并给滑轨补「恢复默认」按钮与 100 时的「（原始）」文案（`ui/Screens.kt:3856/3865-3867`）。

#### 2.2-12【低】预设卡片不显示"播放中"标记，配速标签不跟速度
```kotlin
// 手机端 ui/Screens.kt:3954 / 3963-3968
if (playing) "▶ " + def.name else def.name
Text(presetLoopLabel(def, speed), ...)   // 循环时长 = 行程 ÷ 当前速度
```
```javascript
// 本仓库 ui/app.js:2229（vlPlayingPreset 在 :1392 声明后**从未赋值**）+ :2116
nm.textContent = (on && vlPlayingPreset ? "▶ " : "") + ((base && base.name) || "");
return travel / 100.0;   // 速度 100 时的秒数（默认配速）
```
结论：`vlPlayingPreset`/`vlRandomMode`/`vlBoostMode` 三个变量是死变量（`ui/app.js:1392`），"▶"永远不出现；配速标签固定按速度 100 计算，调预设速度时不变。平台性差异：否。建议：`renderDev()` 里补 `vlPlayingPreset = !!p.playing; vlRandomMode = !!p.random; vlBoostMode = !!p.boost;` 并在 `renderPresetCards()` 后重画；配速标签除以 `p.speed/100`。

#### 2.2-13【低】设备高级设置的"回读"口径不同
```kotlin
// 手机端 AppViewModel.kt:731-737（以设备实况为种子：motorPower==100 → 狂暴开）
combine(serveuDeviceInfo, vorzeDeviceInfo) { a, b -> a.motorPower to b.motorPower }
    .collect { (sm, vmPower) -> if (power in 1..100) _ocModeEnabled.value = power >= 100 }
```
```javascript
// 本仓库 ui/app.js:2300-2302（只读 settings.json 里的意图值）
$("#setA10").checked = Number(DEV.a10_mode) === 1;
$("#setReversed").checked = !!DEV.reversed;
if (document.activeElement !== $("#setOcMode")) $("#setOcMode").checked = !!SET_DEV.oc_mode;
```
结论：本仓库从不读 `info.motor_power`（`channel.py:74` 的 `info` 里已有），也不按通道身份推伪装态；设备侧被别的途径改过（或上次连接改过）时，设置页显示与实际不一致。平台性差异：否。建议：`renderSetDev()` 里用 `DEV.info.motor_power` 校正 `#setOcMode`（并沿用"正在编辑时不覆盖"的护栏），用 `DEV.toy` 校正 `#setA10`。

#### 2.2-14【低】`vlPresetButton` 的注释与实现不符（文档级走偏）
```javascript
// 本仓库 ui/app.js:2024-2029（注释）
预设类按钮的两段式确认（用户指定）：
当前处于"脚本联动模式"…点这三个按钮 不直接执行…
```
```javascript
// 本仓库 ui/app.js:2073-2076（实现：只有中间按钮走两段式）
$("#vlBoost").addEventListener("click", function () { presetCmd("boost"); });
$("#vlRandom").addEventListener("click", function () { presetCmd("random"); });
$("#vlPresetToggle").addEventListener("click", function () { vlPresetButton(this, function () { presetCmd("toggle_play"); }); });
```
结论：**实现与手机端一致**（手机端也只有中间按钮有确认：`Screens.kt:3907` → `AppViewModel.kt:3508-3519`；BOOST `:3892`、RANDOM `:3917` 都是直连，且二者在未播放时只翻标志、不产生运动，`PresetPlayer.kt:94-108`）→ **行为不算 bug，注释是错的**。平台性差异：不适用。建议：改注释，别去给 BOOST/RANDOM 加确认（会与手机端不一致）。

### 2.3 本仓库多出来（6 条）

| # | 多出来的东西 | 手机端对照 | 判定 / 建议 |
|---|---|---|---|
| 1 | 设置页「**播放视频时同步驱动设备**」开关 `#setScriptSync`（`ui/index.html:833-836`、`ui/app.js:2372`、`2393`） | **不存在**：手机端视频在播就同步，只有"跳过无动作"这类粒度开关（`ui/Screens.kt:1787-1817`） | **自造**（第 1 轮已提）。且关掉**不影响在跑的同步**：`syncStop()` 只在预设第二击（`ui/app.js:2058`）与 `ended`（`:1586`）调用，`syncEnabled()` 只在 `syncStart()` 读（`ui/app.js:2394-2395`）→ 关开关后设备继续被驱动。建议：删开关；若要保留，则 `change` 时同时 `syncStop()` |
| 2 | 设置页「同步延迟补偿（ms）」数字输入框 `#setSyncDelay`（`ui/index.html:837-838`、`ui/app.js:2373-2375`、`2329`） | 只有 ±100/±10 按钮（`ui/Screens.kt:1843-1857`） | 冗余入口（同一状态两个控件），保留按钮、删输入框（与第 1 轮 §3.3 一致） |
| 3 | `ui/app.js:2290/2320-2321/2326-2327` 中 `SET_DEV.orgasm/slow/preset_speed` 残骸 | 手机端单一权威存档 `prefs["slow_move"/"orgasm_move"]`（`sync/QuickMoves.kt:346-372`） | **死代码**（HTML 无对应 id）。删（同 2.2-7） |
| 4 | 设置页「脚本与同步」整卡（脚本文件夹 + `#setGotoSync`「打开设备同步」）（`ui/index.html:817-836`、`ui/app.js:2339-2341`） | 手机端「脚本与脚本源」是**另一个二级页**（`ui/Screens.kt:1650-1735`），且 `script_folder` 在本仓库同时是 DLNA 出站目录（`ui/index.html:832`、`host_server.py:3199`） | 部分合理（PC 只有一个脚本目录），但归属与文案混了两个用途；建议拆开或改文案，别让用户以为它参与本地播放（见 2.4-3） |
| 5 | 「设备同步」页 + `/api/sync/{devices,connect,disconnect,run,settings}`（`ui/index.html:611-711`、`host_server.py:2654-2662`） | 手机端无对应 | **不算走偏**：这是给 Quest 推送脚本/视频的出站联动，属头显端职责 |
| 6 | `/api/quick {kind:"pause"/"resume"}` 由内置播放器 play/pause 直接驱动（`ui/app.js:1575-1576`） | 手机端由 `player.isPlaying` 转换驱动（`AppViewModel.kt:508-528`） | 机制差异而非功能差异，**等价**；仅提示：`<video>` 每次 play/pause 都发一次请求，事件抖动时靠 `pause_for_player` 幂等（`quick_moves.py:174-190`）兜住 |

### 2.4 本仓库缺失（13 条）

| # | 缺失项 | 手机端位置 | 严重级 | 最小改动建议 / 平台性判定 |
|---|---|---|---|---|
| 1 | **预设播放中加载脚本/开视频 → 自动退出预设模式**（用户明确要求） | `AppViewModel.kt:2190`、`2546`、`3568-3576`、`3542-3549` | **高** | 宿主侧 `/api/sync/start` 仲裁：`d["preset"].stop()` + `d["quick"].stop_orgasm()/stop_slow()`；前端可选加确认。**规范见 §4** |
| 2 | 预设播放启动时不停快捷动作（爆发/缓动与预设同驱） | `AppViewModel.kt:3534-3537` | **高** | `/api/preset` 的 `play/start` 分支补 `q.stop_orgasm(); q.stop_slow()`（与 `/api/quick` 已有的反向互斥对称，`host_server.py:2556`） |
| 3 | **脚本文件夹索引 / 跨目录同名脚本匹配** | `AppViewModel.kt:1031-1090`（`autoLoadForPath`）、`1174-1290`（`lookupOrRebuild`/`buildIndexFast`） | 中高 | 本仓库只认 `<视频同名>.funscript` 同目录：`host_server.py:2281-2283`、`sync_engine.py:30-45`。建议：`resolve_script` 未命中时回退到 `settings.script_folder` 递归索引（大小写不敏感），并把"未找到"回给前端出提示 |
| 4 | 手动加载 `.funscript` / 「清除脚本」 | `ui/Screens.kt:1719-1724`、`AppViewModel.kt:3568-3610` | 中 | 本仓库无任何脚本选择入口 → 脚本不在视频同目录就完全无法驱动设备。建议：设置/联动页加"选择脚本文件"（`window.pywebview.api.pick_file` 已有文件夹选择的能力，可复用）+ `/api/sync/start {script:...}` 增加显式脚本路径 |
| 5 | 固件更新 / OTA 整卡（检查更新 / 扫 WiFi / SSID+密码 / 开始更新 / 进度） | `ui/Screens.kt:1980-2061`、`AppViewModel.kt:830-901` | 中 | 本仓库 `ui/`+`host_server.py` 全无 firmware/ota/wifi 接口（grep 为 0）。`ServeuApi.checkFirmwareUpdate` 是 HTTP，PC 可直接移植；BLE OTA 段需评估 → 可分两步：先做"检查更新 + 版本展示"，OTA 标注"暂不支持" |
| 6 | 设备信息缺「**行程范围**」（`min_pos ~ max_pos`） | `ui/Screens.kt:1880-1883` | 中 | `channel.py:74` 的 `info` 已含 `min_pos/max_pos`，前端只拼了 3 项（`ui/app.js:2296-2298`）。建议补 `" · 行程范围：" + i.min_pos + " ~ " + i.max_pos` |
| 7 | 狂暴模式无"设备版本支持"门槛与说明 | `ui/Screens.kt:1756`（`hw≥150 && fw≥897`）、`1935/1945-1949` | 中 | `ui/app.js:2367-2371` 只弹风险确认，不看版本；不支持的老设备上仍可开。建议：`renderSetDev()` 按 `DEV.info.hardware/software` 禁用开关并显示手机端同款提示文案 |
| 8 | `idle_detect_seconds` 无读档夹紧（手机端 `coerceIn(1,60)`） | `sync/QuickMoves.kt:368` | 低 | `host_server.py:3854-3855` 直收 `int()`；UI 允许 1-600（`ui/index.html:774`）而手机端内存口径上限 60。建议按手机端夹紧 `[1,60]`（第 1 轮 §3.5 同结论） |
| 9 | 遥控器 F13–F19 的 PC 端绑定 | `ble/HidRemoteController.kt:51-60`、`ui/RemoteScreen.kt:583-599` | 中 | 手机端明确写「**PC 端按同一键绑定**」；本仓库 `ui/` 无任何 `keydown` 监听（grep=0）→ 手机遥控器按下去 PC 端毫无反应（键落到系统/焦点窗口）。建议：`ui/app.js` 加 `window.addEventListener("keydown")` 映射 F13 急停、F14 爆发、F15/16 行程±、F17/18 速度±、F19 缓动 |
| 10 | 「继续上次」+ 逐视频续播位置 | `ui/Screens.kt:2348-2354`、`AppViewModel.kt:1976-2055` | 中 | 本仓库无 `lastVideo`/续播（grep=0）。建议：宿主 `settings.device` 之外单开 `video_resume` 段（key=路径哈希，5 秒周期保存），前端加「继续上次」按钮 |
| 11 | 播放列表自动连播 | `AppViewModel.kt:2260`、`564-568` | 中低 | 本仓库 `ended` 只 `syncStop()`（`ui/app.js:1586`），不 `vlStep(1)`。建议：`ended` 时若 `vlMed.list.length>1` 则 `vlStep(1)`（并把 `syncStop` 保留） |
| 12 | 预设卡片"▶ 播放中"标记（`vlPlayingPreset` 死变量） | `ui/Screens.kt:3830/3954` | 低 | 见 2.2-12 |
| 13 | 倍速播放（顺带：本地同步时钟随之缩放） | `data/VideoPlayerController.kt:237`、`sync/SyncEngine.kt:224` | 低 | 本仓库无 `playbackRate` UI（grep=0）。同类但非必需；若要齐，加 `v.playbackRate` 控件即可（`syncTick` 用的 `currentTime` 天然已含倍速，无需改同步） |

**平台性差异（明确不需要对齐，避免误报）**：
- 外置播放器整条链：`data/PlayerLink.kt`、`data/PlayerProtocol.kt`、控制页播放器卡（`ui/Screens.kt:1429-1522`）、`externalPlay/Pause/Seek`（`AppViewModel.kt:3616-3665`）、`connectPlayer/confirmExternalConnect`（`:1399-1464`）、`ExternalConnectConfirm`（`:198-200`）→ PC 用内置 `<video>`/mpv，**无需对齐**。
- Android 侧：SAF/ALL_FILES 权限（`ui/Screens.kt:1673-1697`）、`pickScriptFolder`（`AppViewModel.kt:979-1018`）、ExoPlayer 解码错误文案（`AppViewModel.kt:1813-1833`）、VR 投影（`ui/Screens.kt:2476-2515`）、唤醒锁（`AppViewModel.kt:3400-3421`）→ 结构性不存在。
- 远端脚本源 WebDAV/SMB/DLNA（`AppViewModel.kt:675-916`）→ PC 是本地宿主，**无需对齐**。
- BLE HID **从机**角色（`ble/HidRemoteController.kt`）→ PC 是被连接方，**角色本身无需对齐**；但键位绑定要补（见 2.4-9）。

---

## 3. Lead 指定 8 条 · 逐条核实结论

| # | 指定项 | 手机端 | 本仓库 | 结论 |
|---|---|---|---|---|
| 1 | `startPresetPlayback()` 四件事 | `AppViewModel.kt:3531-3539`（清脚本/关视频/停快捷动作/开预设） | `ui/app.js:2055-2065`（`syncStop()` + `presetCmd("toggle_play")`） | ①✅④✅；②不该做（结构性）；**③缺失** → 见 2.2-5 / 2.4-2 |
| 2 | `loadScript()` 预设中确认 + `confirmScriptLoad` | `AppViewModel.kt:3568-3576`（弹确认）+ `:3541-3549`（确认后 `_presetPlayer.value?.stop()` 再加载）+ 对话框 `ui/Screens.kt:3732-3745` | **完全没有**：`DEV.preset.playing` 只被赋值（`ui/app.js:2272`）、`syncStart()`（`:2394-2400`）不查预设、`/api/sync/start`（`host_server.py:2455-2464`）不 `preset.stop()` | **本仓库缺失**（高） |
| 3 | "打开视频前停预设"（`:2190`/`:2546`）本仓库是否有对应 | 两处 `_presetPlayer.value?.stop()` | **无对应**（全仓 `preset` 与 `openVideo/syncStart` 无交集） | **缺失**；用户要求写成规范 → §4 |
| 4 | `requestPresetPlay()` 判据 `_script != null \|\| _videoActive`（`:3514`）vs `vlInScriptLink()`（`ui/app.js:2033-2036`） | 判"脚本已加载 **或** 视频在播" | 只判 `SYNC.on`（设备已连+同步已起） | **语义不同（窄）** → 见 2.2-4 |
| 5 | `presetSpeed` 防抖持久化（`:206-217`、`:3495-3496`）vs 本仓库不落盘 | `persistDebounced("presetSpeed")`（`:247-253` 300ms） | `ui/app.js:2283-2287` 每帧 POST `/api/preset{speed}`；`preset_player.py:127-131` 只改内存；`host_server.py:3869-3870` 反而用磁盘旧值覆盖 | **缺失 + 反噬**（拖别的滑轨会把预设速度拨回）→ 见 2.2-11 |
| 6 | `QuickMoves.loadSlow/loadOrgasm` 权威存档 + `coerceIn`（`:345-372`）vs `device.{slow,orgasm}` 死键 | `prefs["slow_move"/"orgasm_move"]` 单一存档，读档即夹紧 | `device.slow/orgasm.*` 只写不读（`ui/app.js:2320-2327` 死引用）；真值在 `video_link.{idle_*,burst_*}` 且无夹紧 | **确认为死键**（功能未丢）；缺读档夹紧 → 见 2.2-7 |
| 7 | BOOST/RANDOM/预设播放三按钮确认语义（`:3485-3519` vs `ui/app.js:2073-2076`） | 只有中键确认（`:3907`）；BOOST `:3892`、RANDOM `:3917` 直连 | 只有 `#vlPresetToggle` 走 `vlPresetButton`；BOOST/RANDOM 直连 | **已对齐**；`ui/app.js:2024-2029` 注释错误（说三个按钮）→ 见 2.2-14 |
| 8 | 已知但需展开的其它 | — | — | 新增 4 条 Lead 未点名的：**急停对缓动无效**（2.2-1，安全）、**缓动速度滑轨无效**（2.2-2）、**手动移动速度 500 vs 200**（2.2-8）、**遥控器键位无绑定**（2.4-9） |

---

## 4. 规范：预设播放中"加载脚本/打开视频"必须自动退出预设模式

### 4.1 规范条款（与手机端等价）

> **规范 P-1**：设备同一时刻只能有一个"写帧者"。预设播放（`PresetPlayer`）、脚本同步（`SyncEngine`）、快捷动作（爆发/缓动）三者互斥；任何一方接管设备时，必须显式停止另外两方。
> **规范 P-2**：进入"脚本/视频播放"语境（选视频、开视频、加载脚本、脚本同步开始）时，**自动退出预设模式**（等价手机端 `AppViewModel.kt:2190/2546/3546` 的 `_presetPlayer.value?.stop()`）；预设不自动恢复（手机端对话框原文："停止预设后脚本同步不会自动恢复"）。
> **规范 P-3**：进入"预设模式"时，自动停脚本同步、停快捷动作（等价 `AppViewModel.kt:3531-3539` 的 ①③）。

手机端三处证据：`AppViewModel.kt:2190`（本地视频）、`:2546`（远程视频）、`:3546`（手动加载脚本，先弹确认 `:3568-3576`）。本仓库目前 P-2 完全缺、P-3 缺一半。

### 4.2 改法（推荐：宿主侧仲裁，前端只展示）

**改动点 1（核心，`host_server.py:2455-2464`）** —— `/api/sync/start` 里统一仲裁：
```python
elif path == "/api/sync/start":
    d = _get_device()
    vp = str(body.get("path") or "")
    ...
    if not d["ch"].state.connected:
        self._json({"ok": False, "error": "设备未连接", "no_device": True}); return
    # 规范 P-1/P-2：脚本同步接管设备 → 预设与快捷动作让路（等价手机端 2190/2546/3546）
    if d["preset"].playing:
        d["preset"].stop()
    d["quick"].stop_orgasm(); d["quick"].stop_slow()
    self._json(d["sync"].start(vp))
```
好处：前端 `syncStart()`（`ui/app.js:2394-2400`）、mpv 路径、以及头显/脚本等任何调用方**一并受保护**，且不需要新增弹窗。

**改动点 2（对称，`host_server.py:2576-2577`）** —— `/api/preset` 的 `play/start` 分支补 P-3 的③：
```python
elif act in ("play", "start"):
    d["quick"].stop_orgasm(); d["quick"].stop_slow()   # 手机端 3534-3537
    res = p.start()
```

**改动点 3（可选，前端体感）** —— `vlPresetButton` 第二击（`ui/app.js:2056-2060`）保留现有 `syncStop()`；若要复刻手机端的三个动作，宿主改完后前端无需再补（避免双写）。`vlInScriptLink()`（`:2033-2036`）按 2.2-4 追加 `|| vlPlaying()`；`renderDev()`（`:2271-2275`）把 `DEV.preset.playing` 落到 `vlPlayingPreset`（顺带修 2.2-12）。

**不建议**：照抄手机端的"加载脚本？"确认弹窗。本仓库视频页与预设同屏，直接仲裁 + toast（"已退出预设模式"）比弹窗更顺手；手机端弹窗是因为它有两个独立页面、动作不可逆。若产品坚持要确认，则在 `syncStart()` 前插一次 arm 式确认（复用 `vlArm`）。

### 4.3 验收判据
1. 预设播放中，在媒体库点任意视频 → `/api/device/state` 的 `preset.playing == false` 且 `sync.active == true`；
2. 预设播放中调用 `/api/sync/start` → 同上；
3. 爆发/缓动运行中点预设播放 → `quick.orgasm == false && quick.slow == false`；
4. 预设播放中点爆发 → `preset.playing == false`（现状已满足，防回归）。

---

## 5. 修复清单（按最小改动 / 用户踩坑程度排序）

| 优先级 | 改动 | 位置 | 预估 |
|---|---|---|---|
| **P0** | 缓动循环加 `allow_move` 门（急停失效，安全） | `vendor/device/quick_moves.py:161-167` | 2 行 |
| **P0** | `/api/sync/start` 仲裁停预设 + 停快捷动作（规范 P-1/P-2） | `host_server.py:2455-2464` | 3 行 |
| **P0** | `/api/preset` start 分支停爆发/缓动（P-3） | `host_server.py:2576-2577` | 1 行 |
| **P1** | 缓动间隔改 `span/speed`（下限 100ms） | `vendor/device/quick_moves.py:167` | 1 行 |
| **P1** | 预设速度落盘 + 防抖 + 不被 `_apply_device_settings` 拨回 | `ui/app.js:2283-2287`、`host_server.py:3869-3870`、`preset_player.py:127-131` | ~10 行 |
| **P1** | 手动移动速度固定 200 | `ui/app.js:2353` 或 `host_server.py:2504-2505` | 1 行 |
| **P1** | `vlInScriptLink()` 追加"视频在播" | `ui/app.js:2033-2036` | 1 行 |
| **P1** | 跳过无动作：默认改 `false/60`，范围改 5-3600 | `ui/index.html:780-781`、`ui/app.js:2323-2324`、`host_server.py` DEFAULT_SETTINGS | 3 行 |
| **P2** | 删死键/死引用（`device.slow/orgasm` 死键、`#setSlowSpeed/#setOrgasmSpeed`、`#setScriptSync`、`#setSyncDelay` 输入框） | `ui/app.js:2290/2320-2327/2372-2375`、`ui/index.html:833-838`、`host_server.py:326-329` | ~15 行 |
| **P2** | 设备信息补「行程范围」 | `ui/app.js:2296-2298` | 1 行 |
| **P2** | 狂暴开关按版本禁用 + 按 `motor_power` 回读 | `ui/app.js:2292-2309/2367-2371` | ~10 行 |
| **P2** | 遥控器 F13–F19 键位绑定 | `ui/app.js`（新增 keydown） | ~20 行 |
| **P2** | 脚本文件夹索引回退 + 手动选脚本 | `sync_engine.py:30-45`、`host_server.py:2277-2285` | 中等 |
| **P3** | `vlPlayingPreset` 赋值 + 配速标签跟速度 | `ui/app.js:1392/2116/2229` | 3 行 |
| **P3** | 「继续上次」+ 续播 + 自动连播 + 倍速 | 新增 | 中等 |
| **P3** | 固件更新/OTA（可分两步） | 新增 | 大 |

---

## 6. 计数与对第 1 轮的更正

**四态计数：已对齐 28 · 语义不同 14 · 本仓库多出来 6 · 本仓库缺失 13 = 61 条。**

对既有文档的修正（避免第 3 轮继续引用错误结论）：
1. `review/设置页与联动页归属-对照手机端.md` §3.1 说"`host_server.py:3840` 从 `vl.get("reversed")` 取 → 任何设置保存都把它清零"——**不成立**：紧随其后的 `:3858` 又用 `dev["reversed"]` 赋了正确值，且 `:3841` 的 `apply_limits()` 只写 0x42 限位、不含反转。该参数是**死参数**，建议删掉以免误导，但不是 bug。
2. 同一文档 §3.2（`("vorze","serveu")[a10]` 映射反了）**确认仍存在**（`host_server.py:3860` vs `protocols.py:41-42`：`serveu=0`、`vorze=1`）→ 伪装设备会切到**错误的 GATT 档案**。本报告未在第 2 节重复展开，保留第 1 轮结论为有效。
3. 同一文档 §2 "方向 B 缺失"的结论**经本轮独立复核成立**，并已按用户要求升格为规范 P-2（§4）。
4. 新增第 1 轮未发现的 4 条：急停对缓动无效（2.2-1）、缓动速度滑轨无效（2.2-2）、手动移动 500 vs 200（2.2-8）、遥控器键位无绑定（2.4-9）。

**最关键的 5 条缺失**：① 预设↔脚本反向仲裁（规范 P-2，用户明确要求）；② 预设启动不停快捷动作（互斥单向）；③ 脚本文件夹索引/手动加载脚本（脚本不在视频同目录就完全没用）；④ 预设速度不落盘且被其它设置拨回；⑤ 固件 OTA + 行程范围显示 + 狂暴版本门槛。
**最关键的 5 条走偏**：① 急停期间缓动仍驱动（安全）；② 缓动节奏不随速度；③ 手动移动用 500 而非 200 的速度（安全）；④ `vlInScriptLink` 判据比手机端窄；⑤ 跳过无动作的默认值/范围/语义三不同（默认开+3 秒，脚本一静止设备就停）。
