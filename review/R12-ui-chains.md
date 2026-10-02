# R12 前端交互信号链穷举审查（第 2 轮）

- 审查人：ui-chains（共享任务 task-6，第 2 轮）
- 仓库：`E:\Development\FunScriptCast-Nexus`（`ui/app.js` 2612 行、`ui/index.html` 937 行、`ui/styles.css` 993 行；工作区含未提交改动，本次未改动任何产品代码）
- 设计真源：`E:\Development\FunScriptCast`（Kotlin/Compose，`app/src/main/java/com/funscriptcast/…`）
- 方法：纯静态阅读 + 两个一次性 node 解析脚本（放在 `C:\Users\admin\AppData\Local\Temp\vlr2\`，未入库）：① 枚举 `index.html` 全部 `button/input/select/[data-*]` 并回查 `app.js` 的 `$()/addEventListener/api()/setInterval` 契约；② 交叉核对"HTML 里的 id ↔ JS 是否引用"。未启动 GUI/服务器。
- 唯一写入：本文件。
- **不复述第 1 轮 R02 的 D1–D14**，仅在相关处一行引用编号。

---

## 0. 结论摘要（本轮新发现）

| 编号 | 严重级 | 位置 | 一句话 |
|---|---|---|---|
| N1 | **阻断** | app.js:1517-1537 / 1584 / 2394-2400；host_server.py:2455-2464 | 预设播放中点视频卡片/开播 → **不停预设**（手机端 2190/2546 强制 stop），预设循环与脚本同步双写 BLE |
| N2 | **高** | app.js:1441-1445 / 923-937 | `browse()` 无 generation 护栏：连点两个目录，先发的响应后到 → 列表回退到旧目录（手机端用 `localLoadGeneration/remoteLoadGeneration` 丢弃过期回调） |
| N3 | **高**（待验证 S2） | app.js:1801-1826；host_server.py:2298-2341 | 媒体库播放热力图**不回显**：`dur/pos` 来自 `lib._cards`（host:2330-2332），只在设置页扫描后建立；直点视频卡片时拿不到时长 → 热力条/总时长空白（与 D4/D6「不回显」同族，但这条**没有自愈路径**） |
| N4 | **高** | app.js:2394-2400 | `syncStart` 的 POST 在途时切页/切视频，**过期响应**把 `SYNC.on=true`、`SYNC.path=旧片`，此后 tick 打的是旧脚本 |
| N5 | **高** | app.js:2283-2287 vs host:2586-2587 + preset_player.py:116-131 | BOOST 状态机：`_boost_prev_speed` 被 `set_speed` 覆写成 500 → **取消 BOOST 后速度锁死 500**；且 `DEV.preset.speed` 未按 `boost` 归一化，拖动被 2s 轮询弹回（手机端 3788-3790 用 `effSpeed` 归一化） |
| N6 | **高** | app.js:1862-1914 vs styles.css:929-941 | 自研滑轨**几何错位**：JS 用 `rect.left + frac×rect.width`，CSS 轨道是 `left/right:7.5px`、把手宽 15px → 圆周边缘约 6% 的行程**永远拖不到**（0-100 滑轨丢 6 个点；预设速度丢 ~30 Units/s） |
| N7 | 中 | app.js:2276-2279 / 2229 / 2272 | 三处可交互控件的**回显被 2s 轮询整段覆盖**，其中 `vlPlayingPreset` 从未赋值 → 预设网格"▶"永不显示（D6 的补强证据，另一处是 `#vlPresetSpeed`） |
| N8 | 中 | app.js:2488-2489 vs 1420-1433 | `display:none` 下 `renderBrowse` 仍被调用（`#vlVBack`→`browse()`），`layoutVl` 量不到高度 → 返回媒体库后卡片行高不更新，需 window resize 才修 |
| N9 | 中 | app.js:2019-2021、2264-2270、2033-2036 | 快捷动作三按钮**无 disabled、无 in-flight 去抖**：连点/未连设备可无限重发（手机端由 `collectAsState` 状态驱动、按状态分支 `if (isSlow) stopSlow() else startSlow()`） |
| N10 | 低 | app.js:1772-1779 与 807-809 | 暂停态切主题：`setTheme` 不清 `heatOffKey` → canvas 底图仍是暗色，而 `.vl-heat` 的 CSS 底色已随主题变（播放中会被 timeupdate 自愈） |
| N11 | 低（待验证 S3） | app.js:1770-1779 与 1820-1825 | `drawHeat` 的 `timelineEnd` 口径：内置播放传 `v.duration`、外挂期传 `p.dur`，而右端刻度取 `max(dur, scriptEnd)` → 脚本比视频长时进度条与热力条右端不同刻度（手机端 ScriptHeatmap.kt:49-53 同取 max，语义**已对齐**，本条只标注刻度差待量化） |
| N12 | 中 | app.js:2334-2335 / 2299 | 设置页「断开设备/扫描并连接」直接 `$("#devBtn").click()` → 复用扫描按钮，**扫描中无法取消**、无 busy 回显（`renderSetDev` 不读 `DEV.connecting`） |
| N13 | 中 | app.js:2345-2356 | 「手动控制位置」滑轨**无回显、无持久化**：`data-hi` 被 `set()` 更新但读值靠 `--b` 反解；`#setManualVal` 只在拖动时改，设备回位/重进页面后两者不一致 |
| N14 | 低（待验证 S4） | app.js:1891-1905 | `initSlider` 的监听挂载方式（每次 pointerdown 新建 move/up 对）本身无泄漏；`#vlPresetSpeed` 常驻、钢蓝滑轨随 `box.innerHTML` 重建会回收旧监听 → 结论降级，仅保留判据 |
| N15 | 低 | app.js:1596-1598 / 1652-1655 | `requestFullscreen()` 被拒时 promise reject → 弹 toast 但 **fullscreenchange 仍可能触发**把图标置为"已全屏"；`onBack` 里 `exitFullscreen()` 未 await 就改 DOM |
| N16 | 低 | app.js:1918-1924 | `queueVl` 的 300ms 防抖**无 beforeunload flush**（对比 `saveSetting`/`flushSettings` 的落盘闸门）→ 关窗即丢 |
| N17 | 低 | app.js:2459-2464 / 936 / 2336 | 绝对定位 `.vl-round` 靠 `layoutVl` 写 inline 宽高；切页隐藏时时长量不到 → 按钮在窄窗首次进入时可能是 48px 而不是计算值 |

---

## 1. 表一：控件全表（穷举）

约定：**「回显」列标 ⚠ 表示"发完请求后界面不回显 / 回显的不是真实值 / 要切页才更新"**。静态 HTML 侧脚本统计：`button` 72、`input` 28、`select` 6、`option` 12，另有 13 处 `[data-*]` 委托控件（`data-page`×6、`data-vl-tab`×3、`data-pick`×2、`data-delay`×4 等），合计 **130+ 个可交互点**；动态生成 5 类（`.vl-folder`、`.lib-card`、`.vl-pcard/.vl-wave`、`vl_*` 钢蓝滑轨、`.steel-link`）。全部已回查处理器：**没有"HTML 里有、JS 里没绑"的控件**（唯一 4 个死引用是 D12：`#motionSeg`/`#themeToggle2`/`#setSlowSpeed`/`#setOrgasmSpeed`，均有 `if` 护栏）。

### A. 顶栏 / 导航 / 窗口

| # | 控件 | 处理器 | 请求/副作用 | 下链 | settings 键 | 回显 |
|---|---|---|---|---|---|---|
| A1 | `#devBtn`(156) | app.js:2430-2458 | scan→connect / disconnect | `/api/device/scan·connect·disconnect` | — | ⚠ 部分：`DEV.connecting` 只在本按钮上回显（2261）；**设置页同功能按钮不回显**（N12） |
| A2 | `#themeToggle`(166) | 807-809 | POST | `/api/settings` | `theme` | ✅ 立即 `data-theme`；⚠ 热力图底色不重画（N10） |
| A3 | `#winMin/…#winClose`(172-174) | 1126-1127 | pywebview 桥 | 宿主窗口 | — | ✅ |
| A4 | `#winMax`(173) | 2499-2516 | 桥 + 失败 toast | 宿主窗口 | — | ✅ 如实报错（1.0.55 bug 已注明） |
| A5 | 6 个 `data-page` 导航(185-210) | 137-140 → `showPage` | 本地 | — | — | ⚠ 切页后 `layoutVl`/`redrawPresetWaves` 各一次 rAF（125/135）——但隐藏期间写入的尺寸类状态不回填（N8/N17） |

### B. 仪表盘 / DLNA / 字幕 / 同步

| # | 控件 | 处理器 | 请求 | 下链路由 | settings 键 | 回显 |
|---|---|---|---|---|---|---|
| B1 | `#btnRefresh`(236) | 1215 | `poll(true)` | `/api/state` | — | ✅ 全页刷新 |
| B2 | `#btnStartAll`(237) | 1216 | startDlna+startSub | `/api/dlna/start`+`/api/subtitle/start` | `dlna_port` | ✅ 1s 轮询 |
| B3 | `#qaDlnaStart/Stop`(314-315) | 1217-1220 | 同上 | 同上 | — | ✅ |
| B4 | `#qaSubStart/Stop`(316-317) | 1221/1231 | 同上 | `/api/subtitle/*` | — | ✅ |
| B5 | `#dlnaStart/Stop`(342-343) | 816-817 | POST | `/api/dlna/start·stop` | `dlna_port` | ✅ |
| B6 | `#dlnaPort`(358) | — | 随 `#dlnaStart` 提交 | `/api/dlna/start` | `dlna_port` | ✅ + dirty 护栏(326) |
| B7 | `#dlnaCopy`(364) | 949 | 剪贴板 | — | — | ✅ |
| B8 | `#pickRoot/#addRoot/#newRoot`(377-379) | 911/864 | 桥+POST | `/api/dlna/roots` | `dlna_roots` | ✅ 1s 轮询 + `renderRoots` 签名去重(392-407) |
| B9 | `#rootList` 删除按钮(动态) | 921-947 | POST | `/api/dlna/roots` | `dlna_roots` | ✅ |
| B10 | `#dlnaLogToggle`(391) | 954-959 | 本地 | — | — | ✅ |
| B11 | `#subStart/Stop`(496-497) | 961-969 | POST | `/api/subtitle/start·stop` | — | ✅ |
| B12 | `#subReclaim`(525) | 1222-1230 | POST | `/api/subtitle/reclaim` | — | ✅ |
| B13 | `#asrModel`(540) | 1027-1040 | POST | `/api/subtitle/config` | subtitle 配置 | ✅ 失败回退 `prev` |
| B14 | `#subIdleRelease`(870) | 1042-1056 | POST | `/api/subtitle/config` | `idle_release_min` | ✅ 失败回退 |
| B15 | `#mtBackend`(545) | 970 | 本地分组显隐 | — | — | ✅ |
| B16 | `#mtLocalModel/#mtLocalModelEn`(557/563) | 595-601 dirty + 971-1003 | POST | `/api/subtitle/config` | 翻译配置 | ✅ dirty 防回填 |
| B17 | `#mtCloudBase/Model/Key`、`#mtModel/#mtBase`(574-594) | 971-1003 | POST | 同上 | 同上 | ✅ |
| B18 | `#saveMt`(602) | 971-1004 | POST + 重启字幕 | `/api/subtitle/config` | 同上 | ✅ 清 dirty |
| B19 | `#testMt`(603) | 1005-1025 | POST | `/api/subtitle/translate-test` | — | ✅ 结果框 |
| B20 | `#syncDevices/#syncConnect/#syncDisconnect`(619-621) | 1059-1070 | POST | `/api/sync/devices·connect·disconnect` | — | ✅ 1s 轮询 |
| B21 | `#syncDeviceSel`(635) | 476-486 | 本地 | — | — | ✅ |
| B22 | `#syncForce/#syncDelete`(640/644) | 1073-1081 | POST | `/api/settings` | `sync_force_full`/`sync_delete_extra` | ✅ + `busyEditing` |
| B23 | `#syncScriptLocal/VideoLocal/Device`(661-694) | 1082-1098 | POST | `/api/settings` | `script_folder`/`video_folder`/`device_folder_*` | ✅ dirty 护栏；⚠ **与设置页 `#setScriptFolder` 同时写 `script_folder`**（同一键两入口，`/api/state` 不做回填，切页才看到对方的值） |
| B24 | `#syncRunScript/#syncRunVideo`(656/678) | 1071-1072 → 500-528 | POST | `/api/sync/run` | — | ✅ busy → `disabled`(455) |

### C. 视频联动页（重点）

| # | 控件 | 处理器 | 请求/副作用 | 下链 | settings 键/设备态 | 回显 |
|---|---|---|---|---|---|---|
| C1 | `.vl-folder`(动态146-1448) | 1446-1448 | GET | `/api/library/browse?path=` | — | ⚠ **无 generation**（N2） |
| C2 | `.lib-card`(动态1449-1456) | 同上 → `openVideo` | GET `/api/library/stream` + `play` | 宿主流 | — | ⚠ 开播**不停预设**（N1）；⚠ 热力图空（N3） |
| C3 | `#vlBack`(408) | 1461-1473 | GET browse | 同上 | — | ⚠ N2 |
| C4 | `#vlVBack`(417) | 1615-1636 | 本地清流 + `browse()` | 同上 | — | ⚠ `#vlBrowse` 正被隐藏时调 `layoutVl`（N8） |
| C5 | `#vlPrev/#vlNext`(423/425) | 1588-1589 → `vlStep` | `openVideo` | 同上 | — | ⚠ 换片 `syncStart` 竞态（N4） |
| C6 | `#vlPlay`(424) | 1587 | `v.play()/pause()` | `/api/quick{pause,resume}`(1575-1576) | quick 状态 | ⚠ pollDev 2s 才回显按钮态 |
| C7 | `video#vlVideo` 本体(416) | 1583 | 同上 | 同上 | — | ✅ |
| C8 | `#vlVSeek`(427) | `initSlider`(1639-1643) | `v.currentTime` | — | — | ✅ 但每次 pointermove 都 seek（手机端有 100ms 节流，见 §4） |
| C9 | `#vlVVol`(432) | 1644-1648 | `v.volume/muted` | — | — | ✅ |
| C10 | `#vlMute`(431) | 1590 | `v.muted` | — | — | ✅ |
| C11 | `#vlFull`(436) | 1593-1599 | Fullscreen API | — | — | ⚠ N15 |
| C12 | `#vlPip`(435) | 1600-1605 | PiP API | — | — | ✅（异常有 toast） |
| C13 | `#vlHeat`(446) | 1786-1800 → `vlSeekTo` | `v.currentTime` | — | — | ⚠ N3/N10/N11；⚠ seek 无节流 |
| C14 | `#vlPause`(457) | 1607-1614 | 本地 | `/api/quick`（经 play/pause 事件） | quick | ✅ 图标双向同步(1504-1509) |
| C15 | `data-vl-tab` ×3(453-455) | 2003-2010 | 本地重画 | — | — | ⚠ 卡片**只在此时重画**，宿主侧变动不回显（§3-R7） |
| C16 | `#vqIdle/#vqBurst/#vqStop`(469-471) | 2019-2021 | POST `/api/quick` | `/api/quick` | quick.* | ⚠ N9 |
| C17 | `#vlRandom`(459) | 2074 | POST `/api/preset{action:random}` | `/api/preset` | preset.random | ✅ 2s 轮询变色 |
| C18 | `#vlPresetToggle`(460) | 2076 → `vlPresetButton`(2055-2065) | 两段式确认 + POST | `/api/preset{toggle_play}` | preset.playing | ⚠ 文字被 2s 覆写（D4 已记）；⚠ 臂章只看 `SYNC.on`，**不看 `DEV.preset.playing`** → 预设正在播时点它=直接停，无确认 |
| C19 | `#vlBoost`(461) | 2073 | POST `{action:boost}` | `/api/preset`→`toggle_boost` | preset.boost/speed | ⚠ N5 |
| C20 | `#vlPresetSpeed`(476) | 2283-2287 | POST `{action:speed}` **每 pointermove 一次** | `/api/preset` | preset.speed（**不落盘**，D7 已记） | ⚠ N5/N6；⚠ 拖动中 2s 轮询回写 `--b`（2278） |
| C21 | `.vl-pcard`(动态2238-2245) | 2238-2245 | POST `{action:select}` | `/api/preset` | preset.selected | ✅ 宿主为准(2205-2206) + `scrollIntoView`(2232-2236) |
| C22 | `vl_range`/`vl_max_speed`(动态1959-1966) | 同左 | POST 防抖 | `/api/settings` | `video_link.range_min/max/max_speed` | ⚠ N6 几何；⚠ 快照过期（D3 已记） |
| C23 | `vl_idle_range/speed/link`(1968-1976) | 同左 | 同上 | 同上 | `video_link.idle_*` | ⚠ N6；✅ link 勾选本地认值后重画(1996-2000) |
| C24 | `vl_burst_range/speed/link/…`(1978-1987) | 同左 | 同上 | 同上 | `video_link.burst_*` | ⚠ N6 |

### D. 设置页

| # | 控件 | 处理器 | 请求 | settings 键 | 回显 |
|---|---|---|---|---|---|
| D1 | `#setDlnaAuto/#setSubAuto/#setCloseTray/#setStartMin/#setAutoStart`(731-747) | 1135-1196 | POST | 同名 | ✅ `busyEditing` 护栏(533-537) |
| D2 | `#libRootAdd`(757) + `#libRootList` 删除(894-908) | 866/884-890 | POST+rescan | `library_roots` | ✅ |
| D3 | `#setDevConnect`(766) | 2335 | 转发 `#devBtn` | 设备 | ⚠ N12 |
| D4 | `#setDevRefresh`(771) | 2336-2338 | POST | — | ✅ toast「稍后更新」 |
| D5 | `#setSlowIdle`(774) | 2342 | POST | `device.slow.idle_detect_seconds` | ✅ pollDev |
| D6 | `#setSkipIdle`(778) | 2343 | POST | `device.skip_idle` | ✅ |
| D7 | `#setIdleThreshold`(781) | 2344 | POST | `device.idle_threshold` | ✅ |
| D8 | `#setManualPos`(784) | 2345-2347 | 本地 | — | ⚠ N13 |
| D9 | `#setManualMove`(789) | 2348-2356 | POST | — | ✅ toast 带实际百分比 |
| D10 | `data-delay` ×4(794-797) | 2357-2364 | POST | 运行时 `sync.delay_ms` | ⚠ 从 `DEV.sync` 起算（D5 已记）；⚠ 连点累积正确，但**响应前连点会丢增量** |
| D11 | `#setA10/#setReversed`(804/808) | 2365-2366 | POST | `device.a10_mode`/`reversed` | ✅ pollDev |
| D12 | `#setOcMode`(813) | 2367-2371 | confirm + POST | `device.oc_mode` | ✅（activeElement 护栏 2302） |
| D13 | `#setGotoSync`(823) | 2339-2341 | 本地切页 | — | ✅ |
| D14 | `#setScriptFolder`+`#setScriptFolderPick`(828-829) | 2376-2387 | POST | `script_folder` | ✅ 但**与 B23 同键双入口** |
| D15 | `#setScriptSync`(835) | 2372 | POST | `device.script_sync` | ✅ `syncEnabled()`(2393) 立即生效 |
| D16 | `#setSyncDelay`(838) | 2373-2375 | POST | 运行时 | ⚠ 无 `pollDev`，值靠下次 `loadSetDev` 才回写 |
| D17 | `#chkUpdate/#updAction/#updModalGo/Later`(853-931) | 1149-1178 | POST | — | ✅ 有 `dismissed/progDismissed` 状态位 |
| D18 | `#subIdleRelease`(870) | 见 B14 | — | — | ✅ |
| D19 | `#copyIp/#copyLanApi/#quitApp`(893-906) | 1197-1210 | 剪贴板 / `/api/quit` | — | ✅ |

---

## 2. 表二：手机端 ↔ 本仓库对照

四态：✅ 有对齐 ｜ ≠ 语义不同 ｜ ➕ 本仓库多出来 ｜ **➖ 本仓库缺失**

### 2.1 控制页 / 视频页（预设与控制）

| 手机端控件（文件:行号） | 语义 | 本仓库 | 说明 |
|---|---|---|---|
| `QuickActionsCard` 三按钮 Screens.kt:1248-1267 | 待机缓动/急停/爆发，`filled` 变色 + 文字切换 | ✅ app.js:2019-2021、2264-2270 | 顺序不同（手机：缓动·急停·爆发；本仓库：缓动·爆发·急停，app.js:469-471），语义一致；N9 是差在"无 disabled" |
| 三圆钮 BOOST/播放/RANDOM Screens.kt:3846-3870 | 播放中切换 + `randomMode/boostMode` 高亮 | ✅ app.js:2073-2076 | 手机端进预设前弹确认（3721-3727）；本仓库两段式改按钮（2055-2065） |
| **进预设 → `clearLoadedScript` + `closeVideo` + 停快捷动作 AppViewModel.kt:3530-3539** | 预设模式是独占态 | **➖ 缺失**（app.js:2073-2076 只发 preset 命令，不停同步、不关视频） | **判定基准 ①**：预设播放中开视频/开同步 → 本仓库不停预设 |
| **进内置视频 → `_presetPlayer.value?.stop()` AppViewModel.kt:2190（本地）、2546（远程）** | 视频模式是独占态 | **➖ 缺失**（app.js:1517-1537/1584 无 stop） | **判定基准 ②**（N1） |
| 手动加载脚本 → 停预设 AppViewModel.kt:3568-3576、3542-3549 | 确认后 `presetPlayer.stop()` | ➖ 缺失（本仓库外挂脚本靠 `syncStart`，见 N1） | 同一条互斥的两种入口 |
| 自动加载脚本：无确认、`canKeepLoadedScript` 判定 AppViewModel.kt:3578-3605 | 有视频才保留脚本 | ≠ | 本仓库 `syncStart` 只判 `DEV.connected && syncEnabled && path` |
| 预设速度卡 Screens.kt:3847-3868 | 滑轨 1-500 + **「恢复默认」按钮** + 「（原始）」标签 | ➖ **缺"恢复默认"与"（原始）"标注**（index.html:474-479 只有滑轨+数字） | D7 已记不落盘；此处记的是**缺控件** |
| 预设卡按 `effSpeed` 显示当前配速 Screens.kt:3786-3790、3830-3832 | BOOST 时拉满 500，波形按速度缩放 | ➖ 缺失（app.js:2209-2215 固定 `presetLoopSec` 标签，不随速度变） | N5 的另一面 |
| 预设网格滚动到选中项 Screens.kt:3804-3812 | `animateScrollTo(idx/2*rowPx)` | ✅ app.js:2232-2236 | 语义对齐 |
| RANDOM 每个循环结束换预设 PresetPlayer.kt:78-81 | 宿主/引擎侧随机 | ✅ vendor preset_player `_run` | 前端高亮跟宿主（2205-2206） |
| **外挂播放器连接（HereSphere/DeoVR 23554）** Screens.kt:322-373 | 手机专有：连 VR 播放器 | ➖ 缺失 | 电脑端自身即播放端，属合理差异（设计差异，非缺陷） |
| **内置播放器外挂字幕 .srt/.ass/.vtt** Screens.kt:2184 | 手机播放页有字幕渲染 | **➖ 缺失**：`ui/index.html:416` 的 `<video>` **没有 `<track>`**；但 app.js:1426 会给 `.srt` 显示「字幕」徽章（host_server.py:2329 提供 `has_srt`）| **结论：徽章是空承诺**——用户看到"字幕"标记却没有任何开关/渲染路径 |
| 视频源 WebDAV/SMB/DLNA 浏览 Screens.kt:689/756/826 | 手机多源浏览 | ➕/➖ | 本仓库只有本地 `library_roots` + DLNA 出站共享；属合理的电脑/手机分工 |
| 远端脚本源索引/清除 Screens.kt:565-600 | 手机兜底脚本源 | ➖ 缺失 | 电脑端用 `script_folder` 兜底（`/api/sync/start`） |
| 固件更新/OTA、WiFi Screens.kt:1981-2059 | 手机 BLE OTA | ➖ 缺失 | 本仓库只有 App 自身更新（D17） |
| 遥控器页 RemoteScreen.kt | 手机变蓝牙 HID | ➖ 缺失（反向能力） | 合理差异 |

### 2.2 设置页

| 手机端（Screens.kt） | 本仓库 | 态 |
|---|---|---|
| 空闲判定秒数 1770-1786 → `idle_detect_seconds` | `#setSlowIdle`(774) | ✅ |
| 跳过无动作部分 1789-1801 → `skip_idle` | `#setSkipIdle`(778) | ✅ |
| 无动作判定时长 1805-1819 → `idle_threshold` | `#setIdleThreshold`(781) | ✅ |
| 手动控制位置 1821-1841 → `manualMoveTo(%, 200)` | `#setManualPos`+`#setManualMove`(783-789) | ≠ 本仓库 `percent` 不带 speed（host_server.py:2501-2506 支持 `speed`）→ 与手机不同速 |
| 延迟调整 ±10/±100 1844-1856 | `data-delay` ×4(794-797) | ✅（D5 的起算 bug 另记） |
| 设备信息刷新 1864-1888 | `#setDevRefresh`(771) | ✅ |
| 伪装设备/反转方向/狂暴模式 1890-1975 | `#setA10`/`#setReversed`/`#setOcMode`(804-813) | ✅（狂暴确认：手机用 AlertDialog，本仓库用 `window.confirm`，2367-2371） |
| 待机缓动/一键爆发卡内速度 1339-1347/1391-1405 | 视频联动页卡内速度（1944/1954） | ✅ |
| 脚本文件夹 1665-1722 | `#setScriptFolder`(828) | ✅ |
| 视频文件夹 2145-2152 | ➖ 缺失（本仓库无 `video_folder` 的 UI，只有同步页 `#syncVideoLocal` B23） | ➖ |
| AI 字幕服务地址/密码 2089-2130 | 字幕页只有本机服务控制 | ➖（本仓库是服务端，合理） |
| 说明页 2157-2205 | ➖ | 低优先 |
| — | `#setDlnaAuto/#setSubAuto/#setCloseTray/#setStartMin/#setAutoStart`(731-747) | ➕ 电脑端多出来（开机自启/托盘/最小化） |

### 2.3 预设数据面

`ui/presets.json` 与 `sync/PresetDefs.kt:87-466` **24 个 id 完全一致**（normal…takeit），`DEFAULT_ID="normal"` 与宿主兜底一致（preset_player `start()`）。**这一层没有缺失。**

---

## 3. 表三：竞态与异常

| # | 竞态 | 复现步骤 | 后果 | 级别 |
|---|---|---|---|---|
| R1 | **轮询覆盖用户操作**（拖动中回写） | 拖动 `#vlPresetSpeed` → 2s 内 `renderDev` 用宿主旧值写 `--b`(2278) | 数字弹回，用户以为拖动无效；BOOST 期间拖了也会被拉回 500（N5） | 高 |
| R2 | 同上（联动卡） | 拖动 `vl_*` 滑轨 → 300ms 后 POST 用的 `vlCfg()` 是 ≤1s 前的快照 | 静默回滚**另一项**（D3）；本轮补：卡片不重画所以看不到 | 高 |
| R3 | 防抖窗口内快照过期 | 改 A → 0.3s 落盘 → 0.5s 改 B → 0.8s POST 带旧 A | 双写覆盖（D3 同源） | 高 |
| R4 | **过期异步回调（无 generation）** | ① 连点两个目录 ② 快速点两片视频 ③ `syncStart` 在途换片 | ① 列表回退旧目录(N2) ② `vlHeatScript` 与 `vlMed.path` 短暂错配 ③ `SYNC.path` 指向旧片(N4) | 高 |
| R5 | 拖动期间重画 canvas | 拖 `#vlVSeek` → 每次 pointermove 都 `v.currentTime=` + `vlSyncVlUi` 全量重画热力图(1551) | 无节流 seek（手机端 100ms 节流 AppViewModel.kt:3638-3650）；本地流每次 seek 触发 Range 请求 | 中 |
| R6 | 隐藏元素量不到尺寸 | 播放态点「返回媒体库」→ `browse()` 在 `#vlBrowse` 仍 `display:none` 时返回 → `layoutVl` 读 `clientHeight=0` 跳过 `gridAutoRows`(2474) | 卡片行高用旧值/默认 118px，需 resize 或换页修正（N8） | 中 |
| R7 | `display:none` 下的定时器 | 切到别的页后：`setInterval(loadHeat,1000)`(1828) 早退、`setInterval(pollDev,2000)`(2427) 全量跑、`setInterval(redrawPresetWaves,1500)`(2523) 空转、`loadLibrary` 8s(1683) 常驻 | 后台 CPU（D14 已记 1500ms 那条）；本轮补：`pollDev` 在隐藏页仍 2s 一次驱动 24 处 DOM 写 | 低 |
| R8 | 同一状态多处写入 | `script_folder` 有 `#setScriptFolder`(2385) 与 `#syncScriptLocal`(1084) 两个入口；两者 `DIRTY` 按 **id** 记，互不保护 | 在设置页改完 1s 内，同步页输入框被回填成服务端旧值（`renderSync` 只在 445 行判 `busyEditing(自己)`） | 中 |
| R9 | 同一定时器多处清理 | `vlChromeTimer` 在 `vlTouch`(1501) 清、`vlShowChrome` 不清；`vlArmTimer` 只由 `vlDisarm` 清(2038)；`pageTimer` 只由 `showPage` 清(116) | 目前单写者，无泄漏；**但 `#vlPresetToggle` 的臂章与 `renderDev` 之间没有互斥** → 5s 内轮询改字后 `vlArmed.text` 存的是**被改过的**文本，`vlDisarm` 还原成"暂停预设"而不是原文（2043） | 中 |
| R10 | 点击双重触发 | `#vlHeat` 的 pointerdown→seek 与 click→seek（1786） | 同一次点击 seek 两遍（对内部播放器是幂等的，但会两次 Range 请求） | 低 |
| R11 | 异步回调过期（尺寸） | `redrawPresetWaves` 只比宽度(2126)，`vl-pcard` 高度随 `.vl-lower` 变 | 只改窗口高度时波形纵向拉伸（D10 的同源补证） | 中 |
| R12 | 未 await 的状态迁移 | `#vlFull` 与 `#vlVBack`：`requestFullscreen()`/`exitFullscreen()` 的 promise 未等就改 DOM(1596/1621) | 被拒时图标与实际状态不一致（N15） | 低 |
| R13 | 卸载丢写 | `queueVl` 300ms 窗口内关窗(1918-1924) | 最后一次拖动丢失（无 `beforeunload` flush） | 低 |
| R14 | 键位冲突 | 拖滑轨时按空格 → 焦点若曾落在 `#vlPlay` 则触发播放/暂停（浏览器默认激活按钮） | 拖动被打断；无 preventDefault | 低 |

---

## 4. 确认的缺陷（本轮新增）

### N1【阻断】预设播放中开视频/开同步 → 不停预设，双写 BLE
本仓库前端唯一的预设仲裁是"点预设按钮时停同步"（app.js:2055-2065），**反方向完全没做**：
```js
1517: function openVideo(path, name, list, idx) { … }
1584:     v.addEventListener("play", function () { syncStart(vlMed.path); });
2394:   function syncStart(path) {
2395:     if (!DEV.connected || !syncEnabled() || !path) return;
2396:     api("/api/sync/start", "POST", { path: path }).then(function (r) {
```
宿主侧 `/api/sync/start`（host_server.py:2455-2464）只 `d["sync"].start(vp)`；全文件的 `preset.stop()` 只有两处：**断开设备**（2498）与**爆发/缓动接管**（2556）——`grep` 确认无第三处。
- 判定基准：`AppViewModel.kt:2190`（本地视频）、`2546`（远程视频）都有一行 `_presetPlayer.value?.stop()  // 互斥：进入内置视频模式前停止预设播放`；`AppViewModel.kt:3530-3539` 反向（进预设先清脚本/关视频/停快捷动作）；`3542-3549` 手动加载脚本也先 `stop()`。
- 复现：① 设备已连 → 点「播放预设」；② 回媒体库点带 `.funscript` 的视频。`play` 事件 → `syncStart`（不检查 `preset.playing`）→ 预设循环与同步引擎交替 `move_to`，设备来回抖（与 D2 的"先预设再视频"是同一现象，但这里给出**手机基线证据**与"两侧都缺"的结论）。
- 最小修复：`syncStart` 首行 `if (DEV.preset && DEV.preset.playing) api("/api/preset","POST",{action:"stop"});`；更稳的是宿主在 `/api/sync/start` 里 `d["preset"].stop()`（与 2556 行同口径），前端只做展示。

### N2【高】`browse()` 无 generation → 连点两个目录，列表回退
```js
1440:     vlBrowsePath = path || "";
1441:     api("/api/library/browse?path=" + encodeURIComponent(vlBrowsePath)).then(function (r) {
1442:       if (r && r.ok) renderBrowse(r);
```
`renderBrowse` 无条件用响应重写 `#vlBrowseBody` 与 `vlLastBrowse`（1435-1436）。大目录（`os.scandir` 上千项）先点 A 再点 B，A 的响应后到 → 面包屑显示 B、内容显示 A；随后点卡片会 `openVideo` 到 A 里的文件。
- 手机基线：`AppViewModel.kt:2173-2185` 用 `localLoadGeneration/remoteLoadGeneration` + `if (gen != remoteLoadGeneration) return@launch`（2522）丢弃过期打开。
- 最小修复：`var vlBrowseGen = 0;`，`browse()` 里 `var g = ++vlBrowseGen;` 回调首行 `if (g !== vlBrowseGen) return;`（`vlApplyHash` 与 `#vlVBack` 走同一函数即可）。

### N3【高】直点视频卡片时热力图永久空白（"不回显"）
```js
1804:  if (vlMed.path && !vlMed.failed) {
1806:      if (vlHeatKey !== vlMed.path || !vlHeatScript) {
1808:        api("/api/library/script?path=" + encodeURIComponent(vlMed.path)).then(…)
```
脚本请求本身没问题；问题是**只有 `loadHeat` 的 else 分支才拉 `p.dur`**，而直点卡片的播放路径不写任何进度。媒体库的 `dur/pos` 来自 `lib._cards`（host_server.py:2330-2332），而 `_cards` 只在设置页「扫描」时建立；从未扫描时 `dur=0`（同处 2330 的 `card["duration"] if card else 0.0`）。
- 与 D4/D6 的"不回显"同族但根因不同：**热力图/时长列没有任何自愈路径**，`drawHeat(pos,dur)`（1755）拿 `dur=0` → `timelineEnd=max(0, scriptEnd)`，`#vlDur` 恒 `0:00`。
- 最小修复：`openVideo` 后补一次 `api("/api/library/items")` 或让宿主在 `/api/library/stream` 响应头带 `X-Duration`；至少把 `#vlDur` 用 `loadedmetadata` 的 `v.duration` 回填（现在 1542 只在 `vlSyncVlUi` 里读，而 `vlSyncVlUi` 由 `timeupdate/durationchange` 触发——已覆盖，真正缺的是**从未扫描时的高度/进度列空白**）。
- 可疑度：**可疑待验证**（未运行 GUI；`_cards` 是否随 `/api/library/rescan` 自动建立需实测）。

### N4【高】`syncStart` 过期响应 → 同步打旧脚本
```js
2396:     api("/api/sync/start", "POST", { path: path }).then(function (r) {
2397:       if (r && r.ok) { SYNC.on = true; SYNC.path = r.script || path; }
```
换片/点返回都会触发 `play`（1517-1537 → 1584）或 `ended`（1586 → `syncStop`）。`syncStop()` 在 2401-2405 有 `if (!SYNC.on) return;`：**若在 start 响应回来之前就换片**，`SYNC.on` 仍是 false → stop 被吞掉 → 随后 start 的响应把 `SYNC.on=true`，此刻视频已是另一部，`timeupdate`(1585)→`syncTick` 用**新片时间轴**驱动**旧脚本**。
- 手机基线：无此路径（预设在视频开始前就已 stop，且 `_sync` 由 `setPlayer(activeLink)` 在换源时重绑，AppViewModel.kt:3286）。
- 最小修复：给同步加代际/路径校验——`syncTick` 内 `if (SYNC.path && vlMed.path && SYNC.path !== expectedScriptPath) …`；或 `syncStop()` 改为无论 `SYNC.on` 都发 `/api/sync/stop`，并用 `SYNC.gen` 丢弃过期 start 响应。

### N5【高】BOOST 状态机把预设速度锁死在 500；回显不按 boost 归一化
```python
# vendor/device/preset_player.py:127-131
def set_speed(self, v: int) -> dict:
    self.speed = max(1, min(500, int(v)))
    if self.boost:
        self._boost_prev_speed = self.speed   # ← 注释说"手机端"，但手机端不是这么做的
```
```python
# preset_player.py:114-125
if self.boost:  … self.speed = self._boost_prev_speed
else: self._boost_prev_speed = self.speed; self.boost = True; self.speed = BOOST_SPEED
```
链路：`#vlBoost`（app.js:2073）→ `/api/preset{action:boost}`（host_server.py:2584-2585）→ `toggle_boost()` 把 `self.speed` 也置 500 → 任何一次 `#vlPresetSpeed` 拖动（2286）都发 `{action:speed}`（2586-2587）→ 把 `_boost_prev_speed` 覆写成拖动值 → 取消 BOOST 时"恢复"到 500，`_effective_speed`（preset_player.py:67-70）`v = BOOST_SPEED if self.boost else int(self.speed)` 于是在**非 BOOST 状态下也恒发 500**。
- 回显侧：`var psVal = Number(p.speed || 100);`（app.js:2277）没有 `boost ? 500 : speed` 归一化；手机端明确有 `val effSpeed = if (boostMode) 500 else presetSpeed`（Screens.kt:3788-3790），且 BOOST 期间"拖滑块"改的是本地下次生效的值（`setPresetSpeed` 只写 `_presetSpeed`，`boostPrevSpeed` 不动，AppViewModel.kt:3495-3503）。
- 复现：BOOST 开 → 拖预设速度到 200 → 关 BOOST → 设备仍跑 500；且拖动过程数字被 2s 轮询拉回（R1）。
- 最小修复（两处）：① `set_speed` 在 `self.boost` 时**不要**改 `_boost_prev_speed`（或改为只记"用户意图值"）；② 前端 `psVal = p.boost ? 500 : Number(p.speed || 100)` 并给滑轨 `data-disabled`/只读提示。

### N6【高】自研滑轨几何与拖拽换算不一致 → 边缘行程拖不到
```css
929: .vl-slider .track{ position:absolute; left:7.5px; right:7.5px; … }
936: .vl-slider .thumb{ … width:15px; … transform:translate(-50%,-50%); }
940: .vl-slider .thumb[data-side="lo"]{ left:calc(7.5px + (100% - 15px) * var(--a,0)); }
```
```js
1896:           set(side, min + (ev.clientX - r.left) / r.width * (max - min));
1911:       var v = min + (e.clientX - r.left) / r.width * (max - min);
```
轨道可见/可点区间是 `[7.5, width-7.5]`，而 JS 把 `[0, width]` 线性映射到 `[min,max]`：把手 x 与值 x 不一致，且 **`[0,7.5)` 与 `(width-7.5, width]` 两端永远取不到真端点**。15px/(340px 轨道) ≈ 4.4%、7.5px 边距再加一段，实测口径约 **6% 的行程不可达**：
- `vl_range/vl_idle_range/vl_burst_range`（0-100）：两端各约 6 个点拖不到（`range_max` 永远只能到 ~94）；
- `#vlPresetSpeed`（1-500）：约 30 Units/s 不可达；
- `#vlVSeek`（0-1）与 `#vlVVol`（0-100）同样。
- 手机端是 Compose `Slider/SteelRangeSlider`（Screens.kt:1282/1292）由框架保证一致，不存在该偏差。
- 最小修复：把换算改成与 CSS 同口径：`var pad = 7.5; var usable = r.width - 15; v = min + (ev.clientX - r.left - pad) / usable * (max - min)`，并把 `paint()` 的 `--a/--b` 保持当前公式（或两处都用 `(x-7.5)/usable`）。

### N7【中】三处回显被 2s 轮询覆盖；`vlPlayingPreset` 从未赋值
- `#vlPresetSpeed`：2278-2279 每 2s 写 `--b` 与文本（R1）。
- `.vl-pname`：2229 `nm.textContent = (on && vlPlayingPreset ? "▶ " : "") + name;`，而 `var vlPlayingPreset = false;`（1393）**全文件再无赋值**（`grep` 确认）→ 预设网格的"▶"永远不出现。手机端 `PresetTile(playing = playing && currentId == def.id)`（Screens.kt:3830）。
- `#vlPresetToggle` 文本与臂章互踩（R9 末段）：`vlArmed.text` 在 2049 保存 `btn.textContent`，但 2272 每 2s 改写它 → `vlDisarm`（2043）把按钮还原成"暂停预设/播放预设"而不是用户点之前看到的字。
- 最小修复：`vlPlayingPreset = !!(DEV.preset && DEV.preset.playing)`（在 `renderDev` 里赋）；`vlArm` 时用常量记录原文（`vlArmed.text = btn.dataset.origText || btn.textContent`）并在 `renderDev` 里 `if (!vlArmed || vlArmed.id !== "#vlPresetToggle")` 才改文本。

### N8【中】隐藏页里调用 `layoutVl` → 卡片行高不更新
```js
2472:     var bg = $("#vlBrowseBody");
2473:     if (bg) {
2474:       var bh = bg.clientHeight;
2475:       if (bh > 0) bg.style.gridAutoRows = Math.max(118, (bh - 10) / 2) + "px";
```
`#vlVBack`（1615-1636）先 `$("#vlPlayView").style.display="none"; $("#vlBrowse").style.display="";` **再** `browse(...)`；`renderBrowse` 的 rAF（1437）里 `layoutVl` 读到的 `clientHeight` 是切回瞬间的值（通常已 >0，但若用户是"播放中点导航切到别的页、再从别的页点返回"的组合路径，`#page-library` 自身仍是 `display:none`，则两个 `clientHeight` 全为 0）。同时 `.vl-lower`（styles.css:908）与 `#vlPGrid` 也量不到 → `gridAutoRows` 保持旧值。
- 手机端无对应问题（Compose 按 constraints 布局，见 Screens.kt:3795-3803 的 `BoxWithConstraints`）。
- 最小修复：`layoutVl` 里加 `if (!document.querySelector("#page-library.active")) return;`，并让 `showPage("library")` 的 rAF 已覆盖（125 行已有），避免隐藏态写入半成品尺寸。

### N9【中】快捷动作三按钮无 disabled / 无 in-flight 去抖
```js
2019: $("#vqIdle").addEventListener("click", function () { quickCmd("slow", !DEV.quick.slow); });
2020: $("#vqBurst").addEventListener("click", function () { quickCmd("orgasm", !DEV.quick.orgasm); });
2021: $("#vqStop").addEventListener("click", function () { quickCmd("stop", !DEV.quick.stop); });
```
`DEV.quick` 只在 2s 轮询/`pollDev` 后更新；设备未连接时按钮**仍然可点**并 `toast("命令失败"…)`（2015）。手机端由状态流驱动且按钮始终反映真实态（Screens.kt:1242-1267），并带 `filled` 反馈。最小修复：`quickCmd` 加 in-flight 标志 + 未连接时 `disabled`（`renderDev` 里 `$("#vqIdle").disabled = !DEV.connected`）。

### N10【低】切主题不清热力图位图缓存（暂停时不自愈）
```js
1772:       var key = (vlHeatKey || "") + "|" + pw + "x" + ph + "|" + heatTheme();
1773:       if (heatOffKey !== key) { … renderHeatBitmap(heatOff, sc, timelineEnd, pw, ph); heatOffKey = key; }
```
`heatOffKey` 的 key 里含 `heatTheme()`（1772），**播放中**每次 `timeupdate`→`vlSyncVlUi`(1551)→`drawHeat` 都会因 key 变化而重画 → 自愈。真正的问题在**未播放/暂停**时：没有事件驱动 `drawHeat`，而 `#themeToggle`（807-809）只改 `data-theme` + POST，**不清 `heatOffKey`、不调 `layoutVl`**；此时 `.vl-heat` 的 CSS 底色 `var(--heat-bg)`（styles.css:878-882）已经变亮，canvas 底图还是旧的 `#1c1728`（1765 `heatPaint(ctx,w,h)` 用 `heatBg()`，但位图缓存 `heatOff` 仍是暗色）→ 暂停态下整条热力条与描边"一半亮一半暗"。最小修复：`setTheme` 末尾加 `heatOffKey = ""; heatBgCache…; drawHeat(vlVid() && vlVid().currentTime, …);`（或直接 `drawHeat()` 后清 key）。级别 **低**（仅暂停态、仅视觉）。

### N11【低，待验证 S3】`drawHeat` 的 `timelineEnd` 两套口径
`vlSyncVlUi`（1551）在**内置播放**时传 `dur = isFinite(v.duration) ? v.duration : 0`（视频口径）；`loadHeat` 的外挂分支（1820/1824）传 `p.pos/p.dur`。而 `drawHeat` 内部右端刻度一律取 `timelineEnd = max(dur, scriptEnd)`（1770）。脚本比视频长时：`#vlDur` 显示视频时长（1542 用 `v.duration`），热力条右端却按脚本末帧 —— 两者不同刻度，点击 `#vlHeat` 又是按 `max(d, scriptEnd)` 换算（1792-1796），三处口径不统一。
- 手机基线：`scriptTimelineEndSeconds(script, durationSeconds)`（ScriptHeatmap.kt:49-53）只有**一处**取 max，`timeAt(x)`（90-92）与进度线（124-125）共用它 → 手机端**语义已对齐**，本仓库是"三处各算一次"。
- 最小修复：抽一个 `vlTimelineEnd()` 供 `vlSyncVlUi`/`drawHeat`/`#vlHeat` 点击共用。本条标注**待验证**：需实测脚本比视频长的样本才能观察到可见偏差（S3）。

### N12【中】设置页设备按钮复用 `#devBtn` → 无 busy 回显
```js
2335:     $("#setDevConnect").addEventListener("click", function () { $("#devBtn").click(); });
```
`renderSetDev`（2292-2308）只写 `"断开设备"/"扫描并连接"`（2299），**不读 `DEV.connecting`**；`#devBtn` 有 `renderDev` 的 `"连接中"+spinner`（2261）。手机端 `ScanButton(connected, scanning, …)`（Screens.kt:252-261）两处一致。最小修复：2299 行改为 `on ? "断开设备" : (DEV.connecting ? "连接中…" : "扫描并连接")` 并加 disabled。

### N13【中】手动控制位置滑轨无回显、无持久化
```js
2345:     initSlider("setManualPos", false, function (lo, hi) {
2346:       if ($("#setManualVal")) $("#setManualVal").textContent = Math.round(hi) + "%";
2348:     $("#setManualMove").addEventListener("click", function () {
2350:       var pct = Number(el && el.getAttribute("data-val") || 50);
2351:       var b = el && el.style.getPropertyValue("--b");
```
`set()`（1877-1887）写的是 `data-lo/data-hi`，**不写 `data-val`**，所以 2350 行拿到的恒是 HTML 初值 50；2351-2352 靠 `--b` 反解——两条路径口径不同（`--b` 是 0-1 的归一化，`pct` 是 0-100）。`initSlider` 在**每次进设置页**都重新跑（`bindSetDev` 在 `bind()` 内一次），`#setManualVal` 的文本与 `data-val` 在刷新后不同步。最小修复：`set()` 内同步 `el.setAttribute("data-val", which === "hi" ? hi : el.getAttribute("data-val"))`；`#setManualVal` 初值由 `initSlider` 的 `onChange` 首帧统一写。

### N14【低，待验证 S4】`initSlider` 的监听挂载方式
1891-1905 每个把手的 `pointerdown` 都新建并挂 `pointermove/pointerup`，`up` 里只摘自己刚挂的那对。`#vlPresetSpeed` 在启动时 init 一次、元素常驻（`renderDev` 2276-2279 只改 `--a/--b/data-val`，不重建 DOM）→ OK；`renderVlTabCard`（1957 `box.innerHTML = html`）虽重建 DOM，但旧监听随旧节点一起回收 → OK。`initSlider("setManualPos"/"vlVSeek"/"vlVVol")` 各只跑一次 → 复核后**未发现真实泄漏**。本条降为 **低** 并保留为待验证项（S4，需 GUI 实测 `getEventListeners` 计数），给出的是判据而非结论。

### N15【低】全屏状态不实
1596：`var pr = document.fullscreenElement ? document.exitFullscreen() : (root.requestFullscreen ? root.requestFullscreen() : null);` 后只挂 `.catch` 弹 toast，**没有**"被拒后回滚图标"；1652-1655 的 `fullscreenchange` 监听只看 `document.fullscreenElement`。`#vlVBack`（1619-1626）在 `exitFullscreen()` 之后**立即** `$("#vlPlayView").style.display="none"` 并 `v.removeAttribute("src")`——注释声称"退出动作放前面更稳"，但实际未等 promise。最小修复：`exitFullscreen()` 后 `.finally(() => { …改 DOM… })`（或监听一次 `fullscreenchange` 再走隐藏）。

### N16【低】`queueVl` 无卸载 flush
1918-1924 的 300ms 防抖与 `saveSetting`/`flushSettings`（35-54）不是一套；对比 `runSync` 有"先落盘再开工"的闸门（app.js:34/508）。最小修复：`window.addEventListener("beforeunload", function(){ if (vlSavePending) { navigator.sendBeacon("/api/settings", …) } })`。

### N17【低】`.vl-round` 尺寸依赖隐藏期测量
```js
2462:     var row = $(".vl-btnrow"), b = $("#vlPause");
2463:     if (row && b) {
2464:       var s = Math.max(26, Math.min(row.clientHeight, row.clientWidth * 40 / 770));
```
`#page-library` 非激活时 `clientHeight=rowWidth=0` → `s=26`，而 CSS 默认宽高 48px（styles.css:901）。首次进入媒体库若 `layoutVl` 的 rAF 早于样式生效，暂停按钮会短暂/持续变成 26px 圆（`showPage` 的 125 行 rAF 通常能救回）。最小修复：`if (!row.clientHeight) return;` 后再写 inline 样式。

---

## 5. 可疑待验证（未运行 GUI，不宣称成立）

| 编号 | 疑点 | 需要什么证据 |
|---|---|---|
| S1 | `renderBrowse` 的 `data-path` 未 `esc`（D1 同类）在含 `&`/`#` 的真实目录名上是否会二次解码成错误路径（`encodeURIComponent` 后再被 HTML 实体解码） | 建一个名为 `a&b`、`c#d` 的目录实测 |
| S2 | 直点视频卡片（N3）在**已扫描过**媒体库时是否仍拿不到 `dur` | 需要 `/api/library/items` 的 `_cards` 建立时序 |
| S3 | `#vlHeat` 点击区域与 `#vlVPos/#vlVDur` 的刻度是否真的不一致（N11 的量化） | 运行截图比对 |
| S4 | `initSlider` 是否存在真实监听泄漏（N14 降级依据） | DevTools `getEventListeners` 计数 |
| S5 | `renderSync` 的 445 行是否真的会覆盖刚在设置页改过的 `#syncScriptLocal`（R8） | 1s 轮询下两页对拉的实测 |
| S6 | 主题切换后 `.vl-heat` 的 CSS 底色与 canvas 底图不一致窗口（N10） | 录屏逐帧 |
| S7 | `#setManualMove` 在 `data-val` 恒 50 时的真实下发值（N13） | 抓一次 `/api/device/move` 的请求体 |

---

## 6. 与手机端"缺失项"总览（本轮新增，供排期）

按"手机有、本仓库没有"排序（不含手机专有的 OTA/遥控器/VR 播放器连接）：
1. **预设↔脚本/视频的独占互斥**（判定基准 ①②，N1）—— 手机端 3 处显式调用（2190/2546/3532-3538），本仓库 0 处。
2. **异步代际丢弃**（2191/2548/2522）—— 本仓库 `browse/syncStart/openVideo` 全无。
3. **预设速度的"恢复默认 100"与当前配速回显**（Screens.kt:3856/3865）—— 本仓库无。
4. **内置播放器字幕渲染**（Screens.kt:2184）—— 本仓库只有 `has_srt` 徽章，无 `<track>`。
5. **seek 节流**（AppViewModel.kt:3638-3650，100ms）—— 本仓库滑轨/热力图每次事件都 seek。
6. **视频文件夹设置入口**（Screens.kt:2145-2152）—— 本仓库仅同步页可改。
7. 设置页"设备速度上限"等滑轨的 `data-disabled` 语义（手机 `enabled = writable && !linked`，Screens.kt:1332/1384/1405）—— 本仓库用 `data-disabled` + `pointer-events:none`（styles.css:942）**已对齐**。

---

## 7. 复核：任务点名的 12 个重点

`vlPresetSpeed` 拖动（N5/N6/R1）、三段双点滑轨与页签切换（N6/§1-C22~C24）、`#vlPresetToggle` 两段式确认（D4 + R9 + N7）、快捷动作三按钮（N9）、`#devBtn` 连接流程（N12 + §1-A1）、设置页各开关（§1-D，`busyEditing`/`DIRTY` 护栏齐全）、`#vlHeat` 点击跳转（N3/N10/N11 + R5/R10）、全屏/画中画（N15）、`browse` 目录导航与刷新（N2：**注意媒体库浏览没有"刷新"入口**，宿主 rescan 后必须退出再进目录）、`layoutVl` 重排时机（N8/N17/R6）、隐藏页尺寸（R6/R7）、轮询覆盖用户操作（R1/R2/R3/R8）。

---

## 8. 自证与边界

- 未修改任何产品代码、未启动 GUI/服务器；`git status` 里 `ui/app.js`、`version.json` 是**进本次审查前就存在的**未提交改动（与 R02 同一工作区状态），本轮唯一写入是本文件。
- 两个解析脚本写在 `C:\Users\admin\AppData\Local\Temp\vlr2\`（`extract.js` 枚举 id↔处理器/路由/定时器；`controls.js`+`count.js` 枚举控件并回查绑定），**未留在仓库**。
- 所有行号取自本次读到的文件内容（`ui/app.js` 2612 行、`ui/index.html` 937 行、`ui/styles.css` 993 行、`host_server.py` 4139 行、`vendor/device/preset_player.py`、手机端 `AppViewModel.kt` 4134 行 / `Screens.kt` 4125 行 / `PresetPlayer.kt` 158 行 / `ScriptHeatmap.kt` / `PresetDefs.kt`）。
- 无法在本环境验证的部分（需运行 GUI/设备）已全部放进 §5「可疑待验证」，未与"确认的缺陷"混排。

