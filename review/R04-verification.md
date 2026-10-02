# R04 独立验证报告（红队复核）

> **⚠️ 第一轮审查存档**：本文是 2026-10-02 凌晨的第 1 轮审查（当时尚未以手机端为
> 真源），**其中部分结论已被修正或推翻**——修正记录见
> `review/REVIEW-全面审查报告-R2.md` §7 与用户裁定（Q1-Q4）；
> 实施状态以 `review/最终修复清单-R2.md` 为准。


- 任务：task-4（owner: verifier）｜复核对象：`review/R01-backend-linkage.md`、`review/R02-frontend-linkage.md`、`review/R03-broad-audit.md`
- 方法：只读源码逐条回核 + 5 个自写只读探针（`review/probes/v4_*.py|js`）+ 语法检查；**未启动 GUI/服务器、未改任何产品代码、未写任何设置文件**（探针首尾对 `data/*.json` 做 sha256 守卫，见 §6）
- 基线（复核时的工作副本，`git HEAD = cb81b21`，工作区有未提交改动 `ui/app.js` / `version.json`）：

| 文件 | sha256(前16) | 备注 |
|---|---|---|
| host_server.py | 2F4D0C70C44D3275 | 4139 行，无孤立 CR |
| ui/app.js | CD562F04BABAF3FF | 2612 行（LF 口径）；**含 2 个孤立 CR**，见 §5 |
| ui/index.html | 60BEAB984E12E488 | 937 行 |
| ui/presets.json | 23E58909AFFC98AB | 24 条 |
| vendor/device/channel.py | 5905E50F37A701DB | 369 行 |
| vendor/device/preset_player.py | 0EAF632C047E6833 | 177 行 |
| vendor/device/quick_moves.py | B047E4F1C14422B9 | 214 行 |
| vendor/device/sync_engine.py | 1D87BBB22313A078 | 133 行 |
| vendor/subtitle/server_app.py | 226CB459290D4ED0 | 1151 行 |
| data/integrated_settings.json | 01CE2ABCB00F7EB9 | 复核前后一致 |
| data/subtitle_config.json | 95E27827B3E0DAEC | 复核前后一致 |

## 0. 结论速览

三份报告共 **15 条「阻断/高」条目**（R01 8 条、R02 3 条、R03 4 条），其中 R01-A7 = R02-D2、R02-D1 = R03-§1.1 是重复条目 → **去重后 13 条独立高危缺陷**。

| 判定 | 条数 | 说明 |
|---|---|---|
| **确认（结论成立）** | **14** | 13 条高危去重后**全部成立**，另加 Lead 指定复核的中危项 R01-A9（`video_link.reversed` 死读） |
| **误报（整条结论不成立）** | **0** | 没有一条高危结论是"把有意设计当 bug"；但有 **4 条子结论/影响描述需要降级修正**（见 §2） |
| **存疑（无法离线定论）** | **3** | 见 §3：A4 的产品意图、A6 的真机表现、R03-§1.4 的跨机可达性 |

**最关键 5 条**（按修复优先级）：

1. **R01-A1（阻断）**：唯一的软件急停入口 `/api/quick {kind:"stop"}` 必然 500，且 `set_allow_move` **一次都不会被调用** —— 实测 `hasattr(QuickMoves,"is_stop")==False`，`set_calls == []`，`allow_move` 恒为 `True`。急停是死的，这是全项目最严重的一条。
2. **R01-A2（阻断）**：24 个预设里 **22 个 `segments: []`**，选中即让 BLE 事件循环纯 CPU 自旋。实测：`normal`（有 segments）1 秒内心跳 31 次正常返回；`classic`（只有 keyframes）心跳冻结在 1，主协程 4 秒都越不过 `await asyncio.sleep(1.0)`，看门狗强制 `os._exit(9)`。
3. **R01-A3 + A4（高）**：即使把 A1 修好，`allow_move=False`（急停）也**挡不住**待机缓动与预设播放 —— 两条循环都用 `force=True` 绕过 `channel.py:252-253` 的闸门。实测急停后 2.2s 内仍写 2 帧（缓动）、1.5s 内写 2 帧（预设），全部 `force=True`。**安全控制名义存在、实际失效。**
4. **R01-A8 + R01-A7/D2（高）**：设置页「播放视频时同步驱动设备」关掉后当页仍然联动（`SET_DEV` 只在启动时加载一次，实测 `device.script_sync` 在宿主侧**零读点**）；且脚本同步与预设播放**无互斥**，两个写者同时以 `force=True` 驱动同一 BLE 通道。
5. **R02-D1/R03-§1.1 + R03-§1.2 + R03-§1.3（高）**：文件名未转义（同源 XSS）→ 目录穿越 → 任意文件读取，三者组合成"媒体库文件名 → 本机 API 全控 / 任意文件读取"的完整链路。实测 `E:\testvideo\..\..\Windows` 通过 browse 校验且 `os.scandir` 真实落到 `E:\Windows`；实测前缀绕过 `E:\testvideo-evil\x.mp4` 对根 `E:\testvideo` 判定放行；源码运行（`sys.frozen=False`）下 `C:\Windows\win.ini` 也被白名单放行。

> 荣誉提名：**R03-§1.4（高）** 已在本机复核为**活的风险**——运行配置 `data/subtitle_config.json` 的 `server.host="0.0.0.0"`、`auth_token=""`，而 `translate.openai.api_key` 确有 35 字符明文；UI 全仓 0 处 `auth_token`，用户无法开启令牌。

---

## 1. 确认清单

> 证据格式：`文件:行号`（**LF 行号口径**，见 §5）+ 我自己的实测/探针输出。
> 「报告 ID」列出所有指向同一缺陷的编号（去重）。

### 1.1 视频联动后端链路（R01）

| # | 报告 ID | 级别 | 结论 | 独立证据 |
|---|---|---|---|---|
| C1 | R01-A1 | **阻断** | `/api/quick` 急停/继续必然 500，`set_allow_move` 不可达 | ①`host_server.py:2551` 逐字为 `was = q.is_stop`；②`vendor/device/quick_moves.py:79-81,210-214` 只有 `set_stop/stop_orgasm/stop_slow`，状态在 `state()["stop"]`；③产品代码中 `is_stop` 仅出现 1 次（`host_server.py:2551`）；④运行时探针：`AttributeError: 'QuickMoves' object has no attribute 'is_stop'`，**`set_allow_move 调用 = []`**，`allow_move 终值 = True`；⑤异常被 `host_server.py:2673-2674` 兜成 500。**A1 的特殊问题（是否真的走不到 set_allow_move）：确认走不到**，异常发生在 `q.set_stop(on)`（2552）之前。 |
| C2 | R01-A2 | **阻断** | keyframes-only 预设把事件循环空转锁死 | ①`preset_player.py:154-173`：`for sg in (pr.get("segments") or [])`（155）为空 ⇒ 循环体不执行 ⇒ 无 await；`_run`（139-152）的 `while … await self._play_one_loop(...)` 变成不让出的纯 Python while；②探针 `v4_static_contract.py`：24 个预设 **segments 为空 22 个**（classic/deep/tease/…/takeit），非空仅 `normal`/`mw`；③探针 `v4_keyspin_bounded.py`：`normal` → `{心跳次数:31, 写帧:1, 正常返回}`，`classic` → 心跳**冻结在 1**、3 秒 phase 不变、观察者判词"事件循环被饿死"、`exit=9`。 |
| C3 | R01-A3 | 高 | 急停挡不住待机缓动（`force=True` 绕过 `allow_move`） | ①`quick_moves.py:156-171` 的 `_slow_loop` 全程没有 `allow_move` 判断，且 `move_to(..., force=True)`（165）；对照组 `_orgasm_loop` 明确写了 `if not self.ch.state.allow_move: await asyncio.sleep(1.0); continue`（108-110）；`channel.py:252-253` 的闸门是 `if not self.state.allow_move and not force: return False`；②**模块自己的契约**（`quick_moves.py:3`）写明"一键急停：allow_move=False —— 脚本/爆发/缓动全部'待命不动作'"；③运行时探针 `v4_safety_gate_bounded.py`：缓动起来后 `set_stop(True)`，随后 2.2s 内**仍写出 2 帧** `[(100.0,100,True,True), (0.0,100,True,True)]`，此时 `allow_move=False`。 |
| C4 | R01-A4 | 高 | 急停挡不住预设播放 | ①`preset_player.py:154-173` 无任何 `allow_move`/`allow_move` 判断，固定 `force=True`（168）；②运行时探针：先 `state.allow_move=False` 再 `select("normal")+start()`，1.5s 内写出 **2 帧** `[(100.0,100,True),(0.0,100,True)]`；③`/api/quick` 的 stop 分支也没有 `d["preset"].stop()`（`host_server.py:2550-2552`）。 |
| C5 | R01-A5 | 高 | 一次外部动作后待机缓动**永不重启** | ①`quick_moves.py:60-68`：`_on_move` 在 `is_slow` 时 `self._gen_s += 1`（67）并 `self._slow_task = None`（68）；`_idle_watch(gen)` 的循环条件 `gen == self._gen_s`（146）随即永假；②唯一的重启者 `start_slow()` 在 `is_slow` 已为真时直接返回（122-124），`_idle_watch` 也只在这里 spawn（130）；③运行时探针：外部帧前 `_gen_s=1` → 外部帧后 `_gen_s=2, is_slow=True, _slow_task=None`，随后 **3.0s 内新帧数 = 0**。注释（66 行）承诺的"缓动保持启用"与实现相反。 |
| C6 | R01-A6 | 高（建议降为中） | 「伪装设备（VorzePiston 模式）」档案映射写反 | ①权威表 `protocols.py:38,41-42`：`SERVEU…0` / `VORZE…1`；②`host_server.py:3860` 为 `("vorze", "serveu")[int(dev["a10_mode"])]` → `a10_mode=0→vorze`、`1→serveu`，与权威表**完全相反**；③`channel.set_profile()` 用 `mode_override = toy.a10_mode`（`channel.py:295-299`）保持自洽，反证 3860 是笔误；④运行时注入探针：`a10_mode=1` → `_forced_toy=serveu, mode_override=1`（自相矛盾）；`a10_mode=0` → `_forced_toy=vorze, mode_override=0`。**降级理由见 §2-2**（connect 有候选重试兜底）。 |
| C7 | R01-A7＝R02-D2 | 高 | 脚本同步与预设播放无互斥（两个写者） | ①`host_server.py:2455-2464` 的 `/api/sync/start` 只 `d["sync"].start(vp)`，不停 `preset`、不查 `preset.playing`；②反向 `host_server.py:2555-2557` 的爆发/缓动只 `d["preset"].stop()`，不停 `sync`；③UI 只做单向防护：`app.js:2054-2065`（第二击 `syncStop()` 在 2058），而 `app.js:1584` `play` → `syncStart(vlMed.path)` 不做任何仲裁（`syncStart` 2394-2400 也不检查 `DEV.preset.playing`）；④两条路径都 `force=True`（`preset_player.py:168`、`sync_engine.py:124`）⇒ 同一通道交替写帧。 |
| C8 | R01-A8 | 高 | 「播放视频时同步驱动设备」关掉不生效 | ①`app.js:2372` 写盘、`app.js:2393` `syncEnabled()` 读 `SET_DEV.script_sync`；②`SET_DEV` 只在 `loadSetDev()`（2316-2332）里更新，而它**只被调用一次**（`app.js:2389` 顶层 `loadSetDev();`）；`saveDev`（2310-2315）与 `pollDev`（2414-2426）都不回写 `SET_DEV`，`renderSetDev`（2292-2309）也不回填 `#setScriptSync`；③探针：`device.script_sync` UI 写点 = 1、**宿主读点 = 0**（服务端 `/api/sync/start` 不校验该键）。⇒ 关掉后当页仍会 `syncStart`，直到刷新页面。 |
| C9 | R01-A9（Lead 指定，中） | 中 | `video_link.reversed` 是死读，被 `device.reversed` 无条件覆盖 | ①`host_server.py:3840` 读 `vl.get("reversed")`；`host_server.py:3858` 紧随其后 `d["ch"].reversed = bool(dev.get("reversed"))`；②UI 写反转只走 `app.js:2366` → `saveDev` → `/api/device/settings` → `device.reversed`（`host_server.py:2525-2526`），`video_link.reversed` **UI 写点 = 0**；③`data/integrated_settings.json:18` 实测 `"video_link": {}`（无 reversed 键）；④运行时注入探针：`video_link.reversed=True` 时 `apply_motion(reversed_=True)` 但 `ch.reversed` **终值 False** ⇒ 联动页/头显写的反转 100% 丢失。 |

### 1.2 前端链路（R02）

| # | 报告 ID | 级别 | 结论 | 独立证据 |
|---|---|---|---|---|
| C10 | R02-D1＝R03-§1.1 | 高 | `renderBrowse` 未转义后端文件名 → 同源 XSS | ①`app.js:1417-1418`（`d.name` 直接进 innerHTML）、`app.js:1429-1432`（`v.name`）、`app.js:1435` 写 DOM；`data-path/data-vpath` 用 `encodeURIComponent` 但文本位裸拼；②`esc()` 确实存在（`app.js:85-89`）且同类渲染器都用了（403/405/419/471），属遗漏；③数据源原样回传文件名：`host_server.py:2319`（`dirs.append({"name": e2.name, …})`）、`host_server.py:2327`（`vids.append({"name": e2.name, …})`）；④影响面复核：页面与 API 同源（`host_server.py:4016` 绑 `127.0.0.1:8790`），`do_POST` 的 Origin 栅栏（`2398-2403`）对页内脚本无效，可达 `/api/device/move`（2501-2506，`force=True`）、`/api/quit`（2668-2670）、`/api/settings`（2415），且 `js_api=JSAPI`（4070）暴露原生窗口控制。 |
| C11 | R02-D3 | 高 | `saveVl` 用最多 1s 前的快照整体合并 ⇒ 静默回滚 | ①`app.js:1845-1850`：`Object.assign({}, vlCfg(), patch)` 里的 `vlCfg()` 读的是 `S.settings.video_link`；②`S.settings` 只在 `app.js:250` 由轮询回填，轮询间隔 **1000ms（可见）/5000ms（隐藏）**（`app.js:1382-1383`）；③`queueVl`（1918-1924）只防抖 300ms，**不更新 `S.settings`**；对照 `bindLink`（1996-1998）手动补了本地值 ⇒ 作者知道这个坑但滑轨路径漏了；④服务端 `save_settings` 是**顶层整键替换**（`host_server.py:551-563`，`s[k] = v`，无深合并）⇒ 后一次 POST 带的旧 `range_min/range_max` 会整段覆盖刚存的新值，而卡片不重画、宿主按旧值下发（3839-3841）。 |
| C12 | R02-D5（附带复核） | 中 | `DEV.sync` 从未赋值 ⇒ 设置页同步状态/延迟永不刷新 | `app.js:2294/2303-2307/2329/2359` 读 `DEV.sync`；`DEV` 初始化（2251-2253）无 `sync` 键；`pollDev`（2417-2423）只赋 6 个字段，从不赋 `sync`；而宿主**确实返回** `st["sync"]`（`host_server.py:2354`）。⇒ `#setSyncDelay` 恒显示 0、`data-delay` 按钮恒从 0 起算。 |

### 1.3 宽面安全（R03）

| # | 报告 ID | 级别 | 结论 | 独立证据 |
|---|---|---|---|---|
| C13 | R03-§1.2 | 高 | `/api/library/browse` 目录穿越（`..` 未折叠 + `startswith`） | ①`host_server.py:2308-2314` 逐字：`bp_norm = str(Path(bp))`（不折叠 `..`）→ `bp_norm.startswith(str(Path(r)) + os.sep)`（2309）→ `os.scandir(bp_norm)`（2314）；②探针 `v4_path_and_lan.py` 用真实 `library_roots=['E:\\testvideo']`：请求 `E:\testvideo\..\..\Windows` → `Path(bp)` 保持原样、**校验通过 True**、真实目标 `E:\Windows`；`os.scandir(E:\testvideo\..)` 实测列出 `E:\` 内容（`$RECYCLE.BIN, 2026_TOCC_Script.zip, audiocpp-portable, Development, …`）；③同一文件 `2122-2124` 的注释本身就是"必须用 `is_relative_to`，`startswith` 会放行兄弟目录前缀"的教训 —— 此处没落地。 |
| C14 | R03-§1.3 | 高 | `/api/library/stream` 前缀白名单缺分隔符 + 源码运行整体关闭白名单 | ①`host_server.py:2217-2222` 逐字复刻；②探针：`E:\testvideo-evil\x.mp4` 对根 `E:\testvideo`：`startswith=True` 而 `is_relative_to=False`；③`2219-2220` 的 `if not _allowed and not getattr(sys, "frozen", False): _allowed = True` —— 探针实测本进程 `sys.frozen=False`，连 `C:\Windows\win.ini` 都被放行（源码/开发运行 = 无白名单）；④无扩展名限制：`2224-2232` 缺省 `application/octet-stream`，且支持 `Range`（2237-2263），可分段拖走大文件。 |
| C15 | R03-§1.4 | 高 | 8756 字幕服务默认 `0.0.0.0` + 空令牌 + `/transcribe` 默认翻译 | ①出厂模板 `vendor/subtitle/config.json:84-89` 与**本机运行配置** `data/subtitle_config.json` 均为 `host="0.0.0.0", port=8756, auth_token=""`；②`server_app.py:378-392` 的 `_LoopbackGuard` 把 `/transcribe`、`/health` 排除在回环栅栏之外（`_LAN_OPEN_PREFIXES=("/transcribe","/health")`）；③`server_app.py:407-425` 的令牌守卫仅当 `auth_token` 非空才生效（默认空 ⇒ 不校验）；④`server_app.py:831-833` `/transcribe(..., translate: bool = True)`；⑤探针实测本机 `translate.openai.api_key` **非空（35 字符，未回显明文）**；⑥全仓 `ui/` 对 `auth_token`/`X-FSC-Subtitle-Token` **0 命中**，`host_server.py` 也不写该键 ⇒ 界面无法开启令牌。 |
| C16 | R03-§1.1 | 高 | 见 C10（同一缺陷，已去重） | — |

### 1.4 附带确认为真的中危项（探针顺手证实，供 Lead 参考）

| 报告 ID | 级别 | 结论 | 证据 |
|---|---|---|---|
| R01-A10 | 中 | `device.orgasm.*` / `device.slow.{min,max,max_speed,link_percent}` 是死设置 | 探针：`dev["orgasm"]` 宿主读点 **0**、`dev["slow"]` 只有 3854 的 `idle_detect_seconds`；`/api/device/settings` 照收（2522-2524），`data/integrated_settings.json:30-43` 里确实存着。注入实测：`video_link.idle_speed=250` 覆盖 `device.slow.max_speed=100` → 终值 250；`burst_speed=400` 覆盖 `device.orgasm.max_speed=300` → 终值 400。 |
| R01-A11 | 中 | `device.preset_speed` 反向覆盖运行时滑轨值，且 UI 不落盘 | 探针：`preset_speed` 的 **UI 写点 = 0**、宿主读点 = 1（3869-3870）；注入实测 `preset.set_speed(180)` 被调用；本机磁盘值确为 180。 |
| R01-A12 | 中 | `_apply_device_settings` 中途异常 ⇒ 其余联动项静默跳过 | 注入 `submit` 抛 `TimeoutError` 后：`quick.apply` **一次都没调用**、`oc_mode=False`、`skip_idle=True`（未采用磁盘的 False）、`idle_threshold=3.0`（未采用 7）、`preset.set_speed` 为空；仅 `log.warning("应用设备设置失败：…")`（3871-3872），HTTP 仍回 `ok:true`（2422-2423）。 |
| R02-D12 | 低 | 4 个 id 在 index.html 不存在（有护栏、不抛错） | 探针 `v4_ui_contract.js`：313 个 HTML id、328 处静态选择器引用 / 193 个唯一 id；缺失恰好 4 个 = `motionSeg`(208,813)、`themeToggle2`(810)、`setSlowSpeed`(2326)、`setOrgasmSpeed`(2327)，且调用点均有 `if` 护栏；模板生成的 `vl_*` + `_val` 全部配套 ⇒ **断言 PASS**。`node --check ui/app.js` → **exit 0**。 |

---

## 2. 误报清单

**严格意义（整条高危结论不成立 / 把有意设计当 bug）的误报：0 条。**
以下 4 条是**子结论或影响描述需要修正/降级**的，它们不影响主结论成立，但会影响修复排序与对外表述：

| # | 位置 | 被质疑的说法 | 复核结论 | 证据 |
|---|---|---|---|---|
| M1 | R01-A6 影响描述 | "连接会去订阅 Vorze 设备上不存在的特征（`CharacteristicNotFound`）"，暗示连不上 | **部分不成立（影响被夸大）**：`channel.connect` 有候选档案重试兜底 —— `toy = self._forced_toy or …`（`channel.py:131`）→ `cands = [t for t in (advertised, toy) if t]` 再补齐全部 TOYS（153-156）→ `for t in cands:` 逐个 `_start_notify` + `cmd_mode`，失败即换下一个（158-180）。所以实际后果是"多一次失败重试 + 档案与 A10 模式自相矛盾（GATT 用一套 UUID、S 指令报另一套）"，通常**仍能连上**。建议 A6 从**高降为中**。 | `channel.py:131,153-180,162`；探针输出 `_forced_toy=serveu / mode_override=1` |
| M2 | R01-A2 影响描述 | "只有 `stop()` 能救" | **过强**：`/api/quick {kind:"orgasm"｜"slow"}` 的分支会先 `d["preset"].stop()`（`host_server.py:2555-2557`），`/api/device/disconnect` 也会 `preset.stop()`（2498）。"只有 stop() 能救"应改为"只有预设 stop 路径能救（`/api/preset{stop\|pause\|toggle_play}`、`/api/quick` 爆发/缓动、`/api/device/disconnect`）"。主结论（选中即锁死）不受影响。 | `host_server.py:2498,2555-2557,2578-2579` |
| M3 | R02-Q3（Lead 特别问的第 2 条） | "移除 `vlBoost`/`vlRandom` 的联动二次确认 = 行为回退" | **误报（不是回退，R02 的判定正确）**。判据：①`toggle_boost`（`preset_player.py:114-125`）只翻 `boost` 标志并把 `speed` 记成 500，**不 `_spawn`**，该标志只在 `_play_one_loop` 的 `_effective_speed`（69,162）里被读；②`toggle_random`（103-112）只在 `if self.playing:` 时才重新起循环；③旧包装的第二击会先 `syncStop()`（`app.js:2058`）—— 即"用户按提示退出联动，结果设备什么都不做"（误伤）；④`#vlPresetToggle`（会真的 `start()`）保留两段式确认是必要的（`app.js:2076`）。**残余前提**：这条正确性依赖"BOOST/RANDOM 不启动预设循环"这个宿主不变量（今天成立，建议加注释锁住）；若将来 BOOST 改成"未播放也启动"，它就会变成绕过联动的真实通道。 | `preset_player.py:69,103-125,162`；`app.js:2058,2073-2076` |
| M4 | R02-§5 计数 | "app.js 里 334 处 id 引用（195 个不同 id）" | **数字口径偏差，结论不变**：我用 `$("#id")`/`getElementById("id")` 严格静态匹配实测 **328 处 / 193 个唯一 id**（另外 6 处是 `"#sync"+cap`、`"#syncRun"+cap` 拼接型）。差异来自计数口径（是否把拼接前缀算作引用），**"缺失恰好 4 个 id"的核心结论已被独立复现（PASS）**。 | `review/probes/v4_ui_contract.js` 输出 |

---

## 3. 存疑清单

| # | 条目 | 为什么存疑 | 需要什么才能定论 |
|---|---|---|---|
| Q1 | R01-A4：急停期间**预设播放**是否"应当"完全静默 | 代码层面缺陷成立（无 `allow_move` 判断 + `force=True` 绕过唯一的全局闸门，且与 `_orgasm_loop`/`sync_engine` 的不对称明显）；但 `quick_moves.py:3` 的契约只点名"脚本/爆发/缓动"，没点名"预设"；产品若有意让预设不受急停影响，则 A4 属设计选择而非 bug。 | 产品口径确认（建议按"急停=设备停"处理；即便有意保留，也应写进注释并让 UI 提示）。 |
| Q2 | R01-A6：真机上的最终表现 | 映射倒置已 100% 确认（静态 + 运行时注入）；但"真机上到底哪个档案可用、重试代价多大、会不会出现部分固件下两套 UUID 都不通"无法离线判定。 | 用 ServeU/VorzePiston 真机各连一次，抓 `channel.connect` 的候选轮次与最终 `state.toy`。 |
| Q3 | R03-§1.4：跨机可达性（"同网任意设备可烧额度"） | 配置（`0.0.0.0` + 空令牌）、代码路径（`/transcribe` 免回环、免令牌、`translate=True`）、真实云端 Key（35 字符）都已在本机确认；但 Windows 防火墙是否放行 8756、路由器是否隔离，未做跨机实测（本任务禁启服务器）。 | 在另一台局域网设备上 `POST http://<本机IP>:8756/health` 与一条最小 `/transcribe`（不实际翻译）。 |

> 说明：R02 的中危项 D9/D10（resize 抖动、DPR 高度判据）与 R03 的中危项（§1.5–§1.22）**不在本次复核范围**（任务只要求复核「阻断/高」），上面只对能顺手用探针证实/证伪的几条（A10/A11/A12/D5/D12）做了旁证，未做真机或浏览器实测。

---

## 4. 可执行证据汇总

| 命令 | 结果 |
|---|---|
| `node --check ui/app.js` | **exit 0**（语法通过，与 R02 一致） |
| `python -m py_compile host_server.py vendor\device\*.py`（`.venv` 3.10.0） | **exit 0** |
| `node review/probes/v4_ui_contract.js` | 313 HTML id / 328 处引用 / 193 唯一 id；缺失恰 4 个；**断言 PASS** |
| `python review/probes/v4_static_contract.py` | 10 条断言全 PASS（22/24 预设、映射倒置、键闭环、`is_stop` 不存在） |
| `python review/probes/v4_runtime_semantics.py` | A1 `set_allow_move=[]` PASS；A6 `_forced_toy=serveu` PASS；A9 `reversed` 终值 False PASS；A12 后续项全跳过 PASS；**`data/*.json` sha256 前后一致 PASS** |
| `python review/probes/v4_keyspin_bounded.py normal` | 心跳 31 次、正常返回、**exit 0** |
| `python review/probes/v4_keyspin_bounded.py classic` | 心跳冻结在 1、4s 未越过 await、**exit 9**（看门狗强制收尾） |
| `python review/probes/v4_safety_gate_bounded.py` | A3 急停后 2 帧 / A4 急停中 2 帧 / A5 外部帧后 0 帧且 `is_slow=True` / A17 首帧 0.31s；4 条断言全 PASS |
| `python review/probes/v4_path_and_lan.py` | §1.2 穿越 PASS、§1.3 前缀绕过+开发放宽 PASS、§1.4 绑定/令牌/UI 缺失/Key 非空 PASS |

探针源码与原始输出（均在 `review/probes/`，可直接复跑）：

- `v4_static_contract.py` / `.out.txt`
- `v4_ui_contract.js`
- `v4_runtime_semantics.py` / `.out.txt`
- `v4_keyspin_bounded.py` / `v4_keyspin_normal.out.txt` / `v4_keyspin_classic.out.txt`
- `v4_safety_gate_bounded.py` / `.out.txt`
- `v4_path_and_lan.py` / `.out.txt`

---

## 5. 行号口径说明（复核时踩到的坑，供后续报告引用）

`ui/app.js` 里混进了 **2 个孤立 CR**（LF 行 134 与 1616 的**行尾多一个 CR**，字节偏移 6962 / 85194）：

- `git diff` / `ripgrep` / 编辑器 / 本报告的 `read` 工具按 **LF 分行**：app.js = **2612 行**，`saveVl` 在 1846、`setReversed` 在 2366；
- Python `str.splitlines()` / PowerShell `Get-Content` / `Select-String` 会把孤立 CR 也当换行：app.js = **2614 行**，同一函数分别显示在 1848 / 2368（1616 行之后整体 **+2**，134-1616 之间 **+1**）。

⇒ R01/R02/R03 引用的 app.js 行号与 `git`/`ripgrep` 口径一致，**没有偏移、也没有人在复核期间改过文件**（app.js mtime = 2026-10-01 20:30:18，早于三份报告）。我最初用 python 探针时出现的 +2 已修正为 LF 口径。顺带建议：这 2 个孤立 CR 是此前编辑留下的行尾污染，可在下次改 app.js 时顺手清掉。

---

## 6. 只读纪律核对

- 本次只写了 `review/R04-verification.md` 与本报告引用的 `review/probes/v4_*`（含 `.out.txt` 输出）；
- 未修改任何产品代码；`git status` 仍是 `M ui/app.js / M version.json / ?? review/`（与开工时一致）；
- 未写 `data/integrated_settings.json`：`v4_runtime_semantics.py` 用 `H.load_settings = lambda: settings` 注入假设置，且首尾对 3 个 `data/*.json` 做 sha256 守卫（**PASS**）；`v4_path_and_lan.py` 只做路径计算与 `os.scandir` 列目录；subtitle 的 api_key 只打印"是否非空/长度"，未回显明文；
- 未启动 GUI、未启动任何服务器（8756/8790/8791/8899 均未监听）。

## 7. 给 Lead 的修复排序建议（基于验证结果）

1. **A1（一行）+ A2（几行 await/展开 keyframes）**：两个阻断项，一行/几行的改动，且 A1 修好后 A3/A4 才有意义（否则急停按钮根本到不了 `set_allow_move`）。
2. **A3/A4**：`_slow_loop` 与 `_play_one_loop` 补 `allow_move` 判断（与 `_orgasm_loop:108-110` 对齐），`/api/quick` 的 stop 分支同时 `d["preset"].stop()`。
3. **D1/§1.1 + §1.2 + §1.3**：前端两处 `esc()`、`Path(bp).resolve()` + `is_relative_to`、`_allowed` 改 `is_relative_to` 并把 `sys.frozen` 放宽改成显式环境变量开关。
4. **A8 + A7/D2**：`saveDev` 成功回调回填 `SET_DEV`；`/api/sync/start` 与 `/api/quick` 的爆发/缓动分支统一互斥（宿主侧仲裁，前端只展示）。
5. **§1.4**：`server.host` 默认改回环，或 UI 暴露 `server.auth_token` 并在非回环 + 空令牌时显著告警（本机已是活风险，且配置里有真实云端 Key）。
6. **D3、A6、A9–A12**：按 R01 §E 的顺序收口。
