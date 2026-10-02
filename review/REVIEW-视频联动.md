# REVIEW-视频联动.md —— 全面审查主报告（视频联动功能 + 设置联动项）

- 仓库：`E:\Development\FunScriptCast-Nexus`，基线 `HEAD = cb81b21`，工作区另有未提交改动 `ui/app.js`（+36/−7）与 `version.json`（1.0.61/62 → 1.0.62/63）
- 审查方式：4 名审查员分线并行（后端链路 / 前端联动页+设置页 / 项目宽面 / 独立复核），外加 Lead 本人的只读探针复现
- 覆盖产物（详细证据在各分册，本报告是**汇总与排序**）：
  - `review/R01-backend-linkage.md` —— 后端链路 2 阻断 + 6 高 + 10 中 + 7 低
  - `review/R02-frontend-linkage.md` —— 前端联动页/设置页 3 高 + 8 中 + 3 低
  - `review/R03-broad-audit.md` —— 项目宽面 4 高 + 10 中 + 7 低
  - `review/R04-verification.md` —— 上述高危结论的独立复核（确认/误报/存疑）
  - `review/probes/` —— 本报告用到的可复现只读探针
- 产品代码零改动；未启动 GUI/服务器；未写 `data/integrated_settings.json`

---

## 1. 一句话结论

视频联动这条链路**设计意图清楚、骨架完整**（浏览⇄播放双模、脚本同步引擎、快捷动作、预设引擎、行程/限速/关联三个联动卡），但**落地质量不足**：三个"控制面"（急停、急停后的状态恢复、预设播放）各有一个致命缺陷，导致「一键急停按了报 500」「22/24 个预设点了设备不动且界面卡死」「设置里改的联动项被联动页悄悄覆盖」。**建议在修复阻断项之前不要把这个版本发出去。**

---

## 2. 视频联动功能的完整链路（审查基线）

```
ui/page-library（联动页）
├── ① 大框框：browse ⇄ <video>（内置播放器，走 /api/library/stream Range/206）
│     └─ play/pause/timeupdate/ended 事件 → /api/quick{pause,resume} + /api/sync{start,tick,stop}
├── ② 时间轴：vlHeat 热力图（脚本动作点） + 进度
├── ③ 按钮行：三个联动卡页签 | ⏸ | RANDOM/播放预设/BOOST
└── ④ 下三列：联动卡（video_link.*）| 快捷动作 | 预设网格（presets.json 24 个）

宿主 host_server.py
├── GET  /api/settings → settings.video_link / settings.device
├── POST /api/settings → save_settings + **_apply_device_settings()**（唯一 settings→设备 的注入点）
├── POST /api/sync/{start,tick,stop,delay} → SyncEngine（funscript 插值写帧）
├── POST /api/quick{stop,orgasm,slow,pause,resume} → QuickMoves
├── POST /api/preset{select,play,pause,toggle_play,random,boost,speed} → PresetPlayer
└── POST /api/device/settings → settings.device.* + set_mode/set_oc_mode

vendor/device/
├── channel.py：唯一 BLE 事件循环（ble-loop 线程）+ submit() + move_to/限位/反转
├── sync_engine.py：进度 → 动作点插值 → move_to
├── quick_moves.py：爆发 / 待机缓动 / 急停（allow_move）
└── preset_player.py：预设循环（segments）
```

**关键设计事实（决定了很多问题的性质）**：
- 所有设备写入都在**同一条 asyncio 事件循环**（`channel.py:37`）上串行执行，HTTP 线程通过 `submit()` 投递并**同步等待**结果（`channel.py:56-58`）。
- `allow_move=False` 是全局"急停"闸门，但 `move_to(force=True)` 会**绕过**它（`channel.py:252`）。
- `settings.video_link` 与 `settings.device.{orgasm,slow}` 是**同一批物理参数的两种表示**，后者被前者覆盖（`host_server.py:3844-3852`）。

---

## 3. 修复优先级总表

| 优先级 | 问题 | 位置 | 影响 | 分册 |
|---|---|---|---|---|
| **P0** | 「一键急停/继续」必然 HTTP 500 | host_server.py:2551 | 安全控制完全不可用 | R01-A1 |
| **P0** | 22/24 个预设把 BLE 事件循环空转锁死 | preset_player.py:154-173 | 设备不动 + 界面全卡 + 所有设备接口超时 | R01-A2 |
| **P1** | 急停挡不住待机缓动 / 预设播放 | quick_moves.py:161-167、preset_player.py:168 | 急停后设备继续动（安全） | R01-A3/A4 |
| **P1** | 媒体库文件名未转义 → 同源 XSS | app.js:1417-1432 | 文件名即可驱动设备/退出程序 | R02-D1、R03-1.1 |
| **P1** | 联动页与设置页参数互相覆盖（联动项谎报） | host_server.py:3840/3844-3852 | 设置里改的值被静默改回 | 本报告 §4 + 归属文档 §3.4 |
| **P1** | 「反转方向」开关无效 | host_server.py:3840 | 用户设置被强制清零 | 本报告 §4（探针）+ 归属文档 §3.1 |
| **P2** | 「伪装设备(VorzePiston)」映射写反 | host_server.py:3860 | 勾选后反而强制 ServeU 档案（有候选重试兜底，故降为中） | R01-A6 |
| **P2** | 脚本同步与预设/快捷动作无互斥 | app.js:1584/2394、host:2464/2555 | 两个写者抢同一设备 | R01-A7、R02-D2 |
| **P2** | 300ms 内连改两项联动项 → 静默回滚 | app.js:1847 | 联动项保存不可靠 | R02-D3 |
| **P2** | 媒体库目录穿越 / stream 白名单失效 | host_server.py:2308-2314、2217-2222 | 越权枚举/读取本机文件 | R03-1.2/1.3 |
| **P2** | 速度上限滑轨允许 0 → 全部动作速度归零 | app.js:1936 + channel.py:254/261 | 一次误拖即"设备装死" | R02-D8 |
| **P2** | 设置页有 2 项 AI 自造、手机端没有（"播放视频时同步驱动设备"开关、"同步延迟补偿"输入框） | index.html:833-838 | 语义与手机端不一致 + 开关关掉不生效 | 归属文档 §3.3 |
| **P2** | 预设速度滑轨不落盘，反被 `device.preset_speed` 回灌 | app.js:2283-2287、host:3869-3870 | 拖了就丢；手机端是防抖落盘的 | 归属文档 §3.6 |
| **P2** | 进预设模式漏"停快捷动作"，且同步↔预设互斥只有单向（反向开播视频无仲裁） | app.js:2058/2394、host:2455-2464 | 与手机端 `startPresetPlayback` 四件事不符；反向缺 `scriptLoadConfirm` 那一半 | 归属文档 §3.7/§3.8 |
| **P3** | 待机缓动一次外部动作后永久停止 | quick_moves.py:60-68 | 功能名存实亡 | R01-A5 |
| **P3** | 8756 字幕服务默认 0.0.0.0 + 空 token | subtitle/config.json:84-88 | 局域网可烧云端额度 | R03-1.4 |
| **P3** | 暂停/返回不停同步、DEV.sync 未赋值、二次确认文案被轮询覆盖等 | 见分册 | 体验/状态不一致 | R01/R02 中危组 |

---

## 4. 设置里的联动项：逐项体检表（本报告重点）

> **归属规范已按手机端仓库核实**（用户要求）：完整对照见
> [设置页与联动页归属-对照手机端.md](设置页与联动页归属-对照手机端.md)。
> 一句话：手机端**控制页**的 4 张卡（快捷动作 / 设备行程与速度 / 待机缓动 / 一键爆发）+
> **预设页**的速度滑轨 = 本仓库**联动页**该有的东西；
> 手机端**设置页·设备与同步**的 6 张卡 = 本仓库**设置页**该有的东西。
> 本仓库设置页**多了 2 项、没有缺项**。

联动项分布在**两个地方**，这是唯一真相冲突的根源：
- **A. 联动页左侧三卡**（`ui/index.html:453-467`，渲染在 `ui/app.js:1926-1988`）→ 写 `settings.video_link.*`
- **B. 设置页「设备与同步」卡**（`ui/index.html:763-815`）→ 写 `settings.device.*`

| 联动项（界面） | 界面位置 | 存储键 | 谁真正读它 | 状态 |
|---|---|---|---|---|
| 限制输出范围 | 联动页·设备行程与速度 | `video_link.range_min/max` | `_apply_device_settings`→`ch.range_lo/hi` | ✅ 生效 |
| 设备速度上限 | 联动页·同上 | `video_link.max_speed` | 同上→`ch.max_speed` | ⚠️ 允许拖到 0（R02-D8） |
| 待机缓动·运动范围 | 联动页·待机缓动设置 | `video_link.idle_min/max` | `quick.apply`→`slow.min/max_percent` | ✅ 生效 |
| 待机缓动·运动速度 | 联动页·同上 | `video_link.idle_speed` | `quick.apply`→`slow.max_speed` | ⚠️ 覆盖设置页 `device.slow.max_speed` |
| 待机缓动·关联主输出 | 联动页·同上 | `video_link.idle_link` | `quick_moves.py:207-208` | ⚠️ 关闭后 UI 与设备层不一致（R01-A18） |
| 一键爆发·运动范围 | 联动页·一键爆发设置 | `video_link.burst_min/max` | `quick.apply`→`orgasm.min/max_percent` | ✅ 生效 |
| 一键爆发·运动速度 | 联动页·同上 | `video_link.burst_speed` | `quick.apply`→`orgasm.max_speed` | ⚠️ 覆盖设置页 `device.orgasm.max_speed` |
| 一键爆发·关联主输出 / 关联上限 | 联动页·同上 | `video_link.burst_link` / `burst_speed_link` | `quick_moves.py:203-206` | ✅ 生效 |
| 待机缓动·空闲判定秒数 | **设置页** | `device.slow.idle_detect_seconds` | `_apply_device_settings`（**唯一**从 device 段读的项） | ✅ 生效 |
| 跳过无动作部分 / 无动作判定时长 | **设置页** | `device.skip_idle` / `idle_threshold` | `sync.skip_idle/idle_threshold` | ✅ 生效 |
| 播放视频时同步驱动设备 | **设置页** | `device.script_sync` | `ui/app.js:2393 syncEnabled()` | ⚠️ 关掉后当次运行不生效（R01-A8） |
| 同步延迟补偿 | **设置页** | `/api/sync/delay`（**不落盘**） | `sync.delay_ms` | ⚠️ 重启丢失（R01-A19） |
| 反转方向 | **设置页** | `device.reversed` | `ch.reversed`（`:3858`） | ⚠️ **开关有效**；`:3840` 的 `reversed_` 是死参数（第 2 轮更正，见 §4.1） |
| 伪装设备(VorzePiston) | **设置页** | `device.a10_mode` | `host_server.py:3860` | ❌ **映射反了** |
| 电机狂暴模式 | **设置页** | `device.oc_mode` | `ch.oc_mode` + `set_oc_mode` | ✅ 生效 |
| 待机缓动/爆发 **速度与范围**（设置页） | 设置页里**根本没有这两个控件**（`setSlowSpeed`/`setOrgasmSpeed` 见下） | — | — | ⛔ 只能去联动页改 |
| `device.orgasm.*`、`device.slow.{min,max,max_speed,link_percent}` | 设置页**无对应输入控件**；`ui/app.js:2326-2327` 读的 `#setSlowSpeed`/`#setOrgasmSpeed` 在 `index.html` 里**不存在**（被 `if` 静默跳过） | 存在但被覆盖 | 无人以它为准 | ⛔ **孤儿/僵尸键**（写点 0） |
| `device.preset_speed` | 设置页**无对应输入控件**；前端从不写它 | 存在但前端从不写 | `_apply_device_settings` 用它**回灌**内存值 | ⛔ **僵尸键 + 反向覆盖** |

> 更正说明（第一版报告的表述有误）：第一版把这一行写成"待机缓动/一键爆发 速度（设置页）"，容易被读成
> "设置页上有一个同名速度项、与联动页打架"。实际上**设置页从来没有这两个速度/范围控件**：
> `ui/index.html` 的设置页「设备与同步」卡里与本主题相关的只有 `#setSlowIdle`（待机缓动·空闲判定秒数，`index.html:773-774`）、
> `#setSkipIdle` 与 `#setIdleThreshold`（`index.html:776-782`）。
> `ui/app.js:2326-2327` 仍在读 `#setSlowSpeed` / `#setOrgasmSpeed` 并赋给 `.value`，但这两个 id 在 `index.html` 里**0 命中**
> ⇒ 有 `if` 护栏所以不报错，只是**静默不做任何事**（`#setSlowIdle` 是同一批代码里的第三个，它存在，所以能生效 —— 看得出这两行是被删掉的控件留下的残骸）。
> 因此真实的冲突不是"设置页 vs 联动页"两个控件打架，而是：磁盘上 `device.slow.max_speed` / `device.orgasm.max_speed`
> **没有任何界面能改、也没有任何代码以它为准**，却被 [host_server.py:3844-3852](host_server.py#L3844-L3852) 的联动卡值静默覆盖。

### 4.1【已撤回·见第 2 轮更正】「反转方向」开关无效的说法不成立

> **⛔ 本节结论已被第 2 轮审查 + 探针 `review/probes/probe_reversed_final.py` 推翻，请勿再引用。**
>
> 第 1 轮我只看 `host_server.py:3840` 的 `reversed_=bool(vl.get("reversed"))`，就断定"任何保存都把反转清零"。
> 漏看了紧随其后的 **`:3858` `d["ch"].reversed = bool(dev.get("reversed"))`** —— 它才是最终生效的写入。
> 探针只看**终值**，5 个用例全部与 `device.reversed` 一致：
> ```
> device.reversed=True,  video_link={}                → apply_motion(reversed_=False)，ch.reversed 终值 = True  ✅
> device.reversed=False, video_link={}                → ch.reversed 终值 = False ✅
> video_link.reversed=True, device={}                 → apply_motion(reversed_=True)， ch.reversed 终值 = False
> ```
> ⇒ **用户在设置页点「反转方向」是有效的**。`:3840` 是一个**死参数**（值立刻被 `:3858` 覆盖）——属于代码异味，
> 不是功能缺陷。**修复建议降级为可选清理**：删掉 `:3840` 的 `reversed_` 参数，避免下一个人误读。

### 4.2【P1】联动卡与设置页是「双份真相」，设置页的值会被悄无声息地改掉

`host_server.py:3844-3852` 明确让联动卡接管 orgasm/slow：
```python
vl_cards = {"slow": {"min_percent": vl.get("idle_min"), ..., "max_speed": vl.get("idle_speed"), ...},
            "orgasm": {"min_percent": vl.get("burst_min"), ..., "max_speed": vl.get("burst_speed"), ...}}
d["quick"].apply({k: v for k, v in vl_cards["orgasm"].items() if v is not None},
                 {k: v for k, v in vl_cards["slow"].items() if v is not None})
```
探针用例 3 实测：设置 `device.slow.max_speed=100` 且联动卡 `idle_speed=250` →
```json
{"quick_apply": [["apply", {"max_speed": 400}, {"max_speed": 250}]]}
```
`250` 胜出。而**设置页并没有这两个速度控件**（`index.html` 无 `setSlowSpeed`/`setOrgasmSpeed`；
`ui/app.js:2326-2327` 对不存在的 id 赋值被 `if` 静默跳过）⇒ 用户永远无法从界面确认设备真正在用的速度。

**结果**：`data/integrated_settings.json` 里 `device.slow.max_speed: 100`、`device.orgasm.max_speed: 500` 是
**孤儿/误导性残留**（写进去了、界面上看不到、设备层也不认），而 `device.preset_speed: 180` 更进一步——
它会被 [host_server.py:3869-3870](host_server.py#L3869-L3870) 用来**反向回灌**覆盖预设速度滑轨的内存值。

**最小修复（二选一）**：
1. 删掉 `settings.device.{orgasm,slow}` 的 min/max/max_speed/link_* 键，只保留 `idle_detect_seconds`，并把「谁是真源」写进注释；
2. 或在设置页把这两组控件补上，并让它们与联动页共用同一份 UI 状态（不要两套键）。

> **⛔ 上面这个"二选一"已作废**：已按用户要求对照手机端仓库（`E:\Development\FunScriptCast`）核实，
> 手机端 `设备与同步`（`Screens.kt:1738-1953`）**从来就没有**待机缓动/爆发的速度或范围控件 ——
> 它们属于手机端**控制页**的四张卡（`Screens.kt:1217-1415`），也就是本仓库的**联动页**。
> 正确处置是**只删不加**：见 [设置页与联动页归属-对照手机端.md](设置页与联动页归属-对照手机端.md) §3.4。
> 用户确认：设置页这两项是早前让删的，当时只删了 UI 没删代码。

### 4.3【P2】300ms 内连改两项 → 前一项被静默回滚（`ui/app.js:1846-1850`）

```js
function saveVl(patch) {
  api("/api/settings", "POST", { video_link: Object.assign({}, vlCfg(), patch) }).then(...)
}
```
`vlCfg()` 读的是 `S.settings.video_link`，而 `S.settings` 只在 `/api/state` 的 **1 秒轮询**里刷新（`ui/app.js:250`）。
`queueVl`（`ui/app.js:1918-1924`）只做 300ms 防抖、**不更新**本地值；对比 `bindLink`（`ui/app.js:1996-1998`）**手工补了**本地值——说明作者知道这个坑，滑轨路径漏了。

**复现**：拖「限制输出范围」(t=0，t=0.3 落盘) → t=0.5 再拖「设备速度上限」→ t=0.8 的 POST 携带 t≈−0.5 轮询时的旧 `range_min/max` → 服务端行程范围被改回旧值，而卡片仍显示新值（`renderVlTabCard` 不重画）。

**最小修复**：`saveVl` 内先本地认值再发（与 `bindLink` 一致）：
```js
var next = Object.assign({}, vlCfg(), patch);
if (S.settings) S.settings.video_link = next;
api("/api/settings", "POST", { video_link: next }).then(...);
```

---

## 5. 两个 P0（功能不可用，必须先修）

### P0-1 「一键急停/继续」必然 500 —— `host_server.py:2551`
```python
if kind == "stop":
    was = q.is_stop          # ← QuickMoves 没有 is_stop 这个属性
```
`vendor/device/quick_moves.py` 全文只有 `is_orgasm` / `is_slow`，`state()` 里叫 `"stop": not ch.state.allow_move`。
`hasattr(QuickMoves, "is_stop") == False`（Lead 实测）。异常被 `host_server.py:2673-2674` 兜成
`{"ok": false, "error": "AttributeError: ..."}, 500` ⇒ **`set_stop()` 永远执行不到**，急停按钮完全失效，
连带 `d["sync"].reset_last_index()`（急停→继续强制重发当前段）也永不可达。
**最小修复**：`was = not q.ch.state.allow_move`（或给 QuickMoves 补 `is_stop` property）。

### P0-2 22/24 个预设把 BLE 事件循环空转锁死 —— `vendor/device/preset_player.py:154-173`
`_play_one_loop` 只遍历 `pr["segments"]`；而 `ui/presets.json` 24 个预设里 **22 个 `segments: []`、只有 `keyframes`**
（classic/deep/tease/edge/rhythm/…，只有 `normal`/`mw` 有 segments）。空列表 ⇒ 循环体一次不执行 ⇒
该协程**没有任何 await**，外层 `while self.playing` 变成纯 CPU 自旋，而它跑在**唯一**的 BLE 事件循环上。

Lead 用 `review/probes/probe_preset_keyspin_bounded.py` 实测（同一进程、各起新循环）：
```
{"preset": "normal",  "segments": [[0,100,100],[100,0,100]], "heartbeat_ticks_in_1s": 33}   → 正常返回
[watchdog] 8s 到，仍在 phase='classic: sleep(1.0) 等待' —— 事件循环被饿死（进程强制退出）
```
`classic` 连 `await asyncio.sleep(1.0)` 都回不来。用户视角：**点「播放预设」→ 设备一动不动 + 界面全卡 +
所有设备接口（scan/connect/move/limit/refresh）超时到 10/20/40s 上限**，一个 CPU 核占满。

**最小修复**：`_play_one_loop` 在「无段可发」或 `wait_ms<=0` 时补一次 `await asyncio.sleep(TICK_MS/1000)`；
根治办法是让 `PresetPlayer` 支持 `keyframes`，或加载时把 keyframes 展开成 segments。

> 复核修正（R04）：不只是 `stop()` 能救 —— `/api/quick` 的爆发/缓动分支（`host_server.py:2555-2557`）与
> `/api/device/disconnect`（`2498`）也会调 `preset.stop()`，但它们都要先经过同一个被锁死的事件循环，
> 所以实际上"能救"的窗口仍然取决于 HTTP 线程能否及时被调度；结论不变，措辞收敛。

---

## 6. 其余值得马上处理的高危项（详见分册）

| 项 | 位置 | 要点 |
|---|---|---|
| 急停挡不住缓动/预设 | quick_moves.py:161-167、preset_player.py:168 | 两者都用 `force=True` 绕过 `allow_move`；`_slow_loop` 甚至完全不判 `allow_move`（与 `_orgasm_loop:108` 不对称）。**复核确认不是有意设计**：`quick_moves.py:3` 的模块契约自己写着急停应让缓动"待命不动作" |
| 待机缓动一次外部动作后永久死 | quick_moves.py:60-68 | `_on_move` 自增 `_gen_s` 杀死了唯一的重启者 `_idle_watch` |
| 伪装设备映射反了（影响降为中） | host_server.py:3860 | `("vorze","serveu")[a10]` vs `protocols.py:41-42`（Vorze=1）；`channel.py:153-180` 有候选档案重试兜底，通常仍能连上，真实后果是"多一次失败重试 + 档案与 A10 模式自相矛盾" |
| 同步与预设无互斥（**单向**） | app.js:1584、host:2464/2555 | 反向「开播视频」无仲裁：`DEV.preset.playing` 只被赋值从未被读取，预设循环继续跑，两个 `force=True` 写者抢通道。（正向「脚本→预设」已有 `#vlPresetToggle` 两段式确认，但判据比手机端窄、且漏"停快捷动作"） |
| 文件名 XSS | app.js:1417-1432 | `d.name`/`v.name` 未过同文件的 `esc()`；同源可 `POST /api/device/move`、`/api/quit` |
| browse 目录穿越 | host_server.py:2308-2314 | `str(Path(bp))` 不折叠 `..`；Lead 实测 `E:\testvideo\..\..\Windows` 通过校验且 `os.scandir` 真落到 `E:\` |
| stream 白名单失效 | host_server.py:2217-2222 | 裸前缀比较 + 非 frozen 时整段放行 |
| 速度上限可拖到 0 | app.js:1936 + channel.py:254/261 | `max_speed=0` ⇒ 所有 `sp=0`，设备"装死" |

---

## 7. 本次未提交改动（`ui/app.js`）的评估

| hunk | 内容 | 评估 |
|---|---|---|
| `ui/app.js:135` | 切页后 `redrawPresetWaves()` | ✅ 方向正确 |
| `ui/app.js:2073-2074` | **移除** BOOST/RANDOM 的"退出联动"二次确认 | ✅ **不是回退**：`toggle_boost`/`toggle_random`（preset_player.py:114-125/103-112）只翻标志、不启动循环，旧逻辑第二击会真的 `syncStop()` 却没有任何设备动作 = 误伤。正确做法是给 `syncStart`（R02-D2）加护栏 |
| `ui/app.js:2118-2139` | `drawPresetWave` DPR 修复（backing = CSS×dpr，不再改 `style.width`） | ✅ **主因修对了**，与设计原型 `design/video-link-sketch-v1.html:722-727` 一致；⚠️ 但重画判据只看宽度（`:2126`、`:2482`），只改窗口高度后波形不按新高度重画（R02-D10） |
| `ui/app.js:2523` | `setInterval(redrawPresetWaves, 1500)` 兜底 | ⚠️ 治标；隐藏页时 24 次 `clientWidth` 后早退，成本≈0、无内存增长。建议改为 `ResizeObserver` 或把高度并入判据后删掉 |
| `ui/app.js:2478-2486` | `layoutVl` 里重画波形 | ⚠️ 读写交错：读 `clientWidth`（强制布局）→ 写 `cv.width`（脏化布局）×24，resize 时抖动（R02-D9） |

---

## 8. 独立复核结果（R04）

第五名成员（verifier）对三份报告全部「阻断/高」结论做了独立回源码复核 + 自写只读探针重跑：

- 三份报告共 **15 条**阻断/高（R01 8 + R02 3 + R03 4），其中 R01-A7 = R02-D2、R03-§1.1 = R02-D1 重复；
  **去重后 13 条全部成立**，另确认中危 A9（`reversed` 死读）。**整条误报 0 条。**
- 4 条子结论修正（不影响成立性，已在上文相应位置就地更新）：
  1. **A6 由高降为中** —— `channel.py:153-180` 有候选档案重试兜底，映射倒置本身确认，但实际后果是"多一次失败重试 + 档案与 A10 模式自相矛盾"，不是"连不上"。
  2. **A2 的"只有 stop() 能救"过强** —— `/api/quick` 爆发/缓动分支与 `/api/device/disconnect` 也会 `preset.stop()`。
  3. R02 §5 的 id 计数 334/195 实测为 328/193（口径差异），"缺 4 个 id"结论不变。
  4. R02-Q3 判定"移除 BOOST/RANDOM 二次确认不是回退"**正确**（`toggle_boost` 不 `_spawn`、`toggle_random` 有 `if self.playing` 护栏）。
- 存疑 3 条（未定性）：A4 的产品意图（预设是否属急停语义范围）、A6 的真机表现、R03-§1.4 的跨机可达性。
- **额外风险（复核顺带发现，建议单独按安全事件处理）**：`vendor/subtitle/config.json` 的运行配置是
  `host=0.0.0.0` + `auth_token=""`，且该文件里确有 **35 字符明文云端 API Key**，而 `ui/` 里 **0 处**令牌入口
  ⇒ 局域网上任何设备都能 `POST /transcribe?translate=true` 借用该 Key。建议立刻轮换 Key 并改回 `127.0.0.1`。
- **额外工程坑**：`ui/app.js` 里混进 2 个孤立 CR（LF 行 134/1616 行尾），会让 python `splitlines()`/`Get-Content`
  的行号比 git/ripgrep 口径 +1/+2。三份报告的 app.js 行号本身是 git 口径、无偏移，但后续用 Python 复核会被坑，建议顺手清掉这两个 CR。

---

## 9. 已验证为**干净**的面（避免过度修复）

- `node --check ui/app.js` 通过；关键 Python 模块 `py_compile` 通过
- `index.html` 的 id ↔ `app.js` 的 `$("#id")` 契约完整：313 个 id / 334 处引用，拼接 id（sync×10）与模板生成的 9 个 `*_val` 全部配套，4 个真缺失 id（`motionSeg`/`themeToggle2`/`setSlowSpeed`/`setOrgasmSpeed`）都有 `if` 护栏 ⇒ 无 null 解引用
- `save_settings`：RLock + 原子 `os.replace` + 缓存键值成对写入 + 未知键过滤 + 路径规整；坏 JSON 时拒绝覆盖并备份
- 无 `shell=True`/`eval`/`exec`；无关闭 SSL 校验；DLNA 的 `key_to_path`/`_inside_root`/`_reparse_free_below` 确实防住了穿越、盘符、ADS、联接逃逸；SOAP 走正则（无 XXE）
- 8790 有 Host 校验（DNS rebinding）+ Origin 校验（CSRF）；8791 是正向枚举白名单，只放 4 个路由
- 打包：`dist-app/vendor/device/*` 六个文件齐全，`dist-app/.venv` 里 `bleak` 可用（BLE 依赖没漏）
- 版本号唯一来源是 `version.json`（`build_installer.ps1:125/150` 注入），未发现多值漂移

---

## 10. 测试覆盖缺口（工程建议）

`tests/` 下**没有任何针对 `vendor/device/*`（sync_engine / preset_player / quick_moves / channel）或 video_link 的测试**；
现有文件多为一次性脚本，且 26 个文件硬编码 `E:\` 绝对路径。这次的两个 P0（`is_stop`、空 segments 自旋）
都是**一分钟单元测试就能拦住**的类型，建议优先补：
1. `QuickMoves.state()/set_stop()` 的属性契约测试；
2. `PresetPlayer` 对 `ui/presets.json` 全部 24 个预设各跑一个循环（用 fake channel + `asyncio.wait_for` 断言让出）。
