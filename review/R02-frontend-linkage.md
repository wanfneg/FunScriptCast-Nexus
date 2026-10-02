# R02 前端审查报告：视频联动（page-library）与设置页联动项

- 审查人：frontend-reviewer（共享任务 task-2）
- 仓库：`E:\Development\FunScriptCast-Nexus`，基线 HEAD `cb81b21`（工作区有未提交改动：`ui/app.js`、`version.json`）
- 审查对象：`ui/app.js`（2612 行）、`ui/index.html`（937 行）、`ui/styles.css`（993 行），旁证：`host_server.py`、`vendor/device/{preset_player,quick_moves,sync_engine,channel}.py`
- 方法：纯静态阅读 + 三份只读脚本（放在系统临时目录 `C:\Users\admin\AppData\Local\Temp\vlreview\`，未入库）；`node --check ui/app.js` → **exit 0 通过**
- 未做的事：未启动 GUI、未启动服务器、未修改任何产品代码（唯一写入是本文件）
- 未提交改动的逐项评估见 §6

---

## 1. 结论摘要

| 编号 | 严重级 | 位置 | 一句话 |
|---|---|---|---|
| D1 | **高** | app.js:1417-1432 | `renderBrowse` 把后端目录名/文件名直接拼进 `innerHTML`（未 `esc`）→ 同源 XSS，可调 `/api/device/move`、`/api/quit` |
| D2 | **高** | app.js:1584/2394、host_server.py:2464/2555 | 「脚本同步」与「预设/快捷动作」无互斥：先开预设（或爆发）再播带脚本视频，两个写者同时驱动同一 BLE 通道 |
| D3 | **高** | app.js:1846-1850 | `saveVl` 用最多 1 秒前的 `S.settings` 快照做整体合并 → 300ms 内连改两项会**静默回滚**先改的那项，而卡片仍显示新值 |
| D4 | 中 | app.js:2272 | 联动二次确认文案被 2 秒轮询覆盖：`renderDev` 无条件改写 `#vlPresetToggle` 文本，"确定请再点击"最多显示 2 秒 |
| D5 | 中 | app.js:2294/2329/2414-2423 | `DEV.sync` 从未赋值 → 设置页「脚本同步中…」「同步延迟」永不刷新，`data-delay` 按钮永远从 0 起算 |
| D6 | 中 | app.js:1392/2201-2236 | 预设网格不随宿主状态刷新（RANDOM 跳转/选中高亮不跟随），`vlPlayingPreset` 从未赋值 → "▶" 永不显示 |
| D7 | 中 | app.js:2283-2287 | 预设速度滑轨**不落盘**（重启回 100）且每个 pointermove 发一次 POST（无防抖） |
| D8 | 中 | app.js:1936 | 「设备速度上限」滑轨 `min=0`，0 会被下发成硬件限速 0，且 `channel.move_to` 的限幅使**全部动作速度归零** |
| D9 | 中 | app.js:2478-2486 | resize 时 24 个 canvas 读写交错 → 每次 resize 事件最多 24 次强制同步布局 + 24 次波形重绘（抖动） |
| D10 | 中 | app.js:2126/2134/2469 | DPR 修复**不彻底**：重画判据只看宽度，而 canvas 的 CSS 高度由窗口高度（`gridAutoRows`）决定 → **只改窗口高度**的 resize 后波形不会按新高度重画 → 纵向拉伸/模糊 |
| D11 | 中 | app.js:105-136、styles.css:784 | 切到其它页后内置播放器继续后台播放、脚本同步继续 tick，但页面上没有任何播放/急停入口 |
| D12 | 低 | app.js:205-208/810/2326-2327 | `motionSeg`/`themeToggle2`/`setSlowSpeed`/`setOrgasmSpeed` 四个 id 在 index.html 不存在，功能静默失效（有 `if` 护栏，不崩） |
| D13 | 低 | app.js:560-563 | `renderLibRoots` 未转义路径（文本 + `data-del-libroot` 属性），同类渲染器 `renderRoots` 用了 `esc` |
| D14 | 低 | app.js:2523 | `setInterval(redrawPresetWaves, 1500)` 永久空转（隐藏页时 24 次 `clientWidth` 后早退，成本极低，但属治标兜底） |

必答问题逐条回答见 §4；id 契约原始核对结果见 §5。

---

## 2. 确认的缺陷

### D1【高】`renderBrowse` 未转义后端文件名 → 同源 XSS

`ui/app.js:1416-1418`（目录）与 `ui/app.js:1429-1432`（视频）：

```js
1416:     dirs.forEach(function (d) {
1417:       html += '<div class="vl-folder" data-path="' + encodeURIComponent(d.path) + '">' +
1418:         '<svg class="ic"><use href="#i-vk-folder"/></svg><div class="name">' + d.name + '</div></div>';
...
1429:       html += '<div class="lib-card" data-vpath="' + encodeURIComponent(v.path) + '">' +
...
1432:         '<div class="lib-title">' + v.name + '</div></div>';
```

- `d.name` / `v.name` 直接落进 innerHTML 文本位置，**没有任何转义**；`data-*` 属性用 `encodeURIComponent` 是安全的。
- 数据来源是 `os.scandir` 的原始文件名：`host_server.py:2319`（`dirs.append({"name": e2.name, ...})`）与 `host_server.py:2327`（`vids.append({"name": e2.name, ...})`）→ 媒体库里一个名为 `<img src=x onerror=...>.mp4` 的文件即可执行脚本。
- 同文件其它渲染器都转义了，说明是遗漏而非有意的：`app.js:403`（`esc(p)`）、`app.js:419`（`esc(l)`）、`app.js:471`（`esc(l)`）、`app.js:314`（`esc(e.msg)`）、`esc()` 定义在 `app.js:85-89`。
- 影响放大：页面与宿主 API **同源**，注入脚本可直接 `POST /api/device/move`（`host_server.py:2501`，直接驱动设备）、`POST /api/quit`（`host_server.py:2668`，退出应用）；宿主对 POST 的 CSRF 栅栏（`host_server.py:2398-2403`）只挡跨站 Origin，挡不住页内脚本。
- **最小修复**：`'<div class="name">' + esc(d.name) + '</div>'`、`'<div class="lib-title">' + esc(v.name) + '</div>'`（`d.path`/`v.path` 保持现状即可）。

### D2【高】脚本同步与预设/快捷动作无互斥 → 两个写者抢设备

联动页只在**一处**做了仲裁（`vlPresetButton` 里 `syncStop()`），启动脚本同步的方向完全没做：

`ui/app.js:1584`（视频一开始播就起同步）：

```js
1584:     v.addEventListener("play", function () { syncStart(vlMed.path); });
```
`ui/app.js:2394-2399`（`syncStart` 只发 `/api/sync/start`，不停预设、不停快捷动作）：

```js
2394:   function syncStart(path) {
2395:     if (!DEV.connected || !syncEnabled() || !path) return;
2396:     api("/api/sync/start", "POST", { path: path }).then(function (r) {
2397:       if (r && r.ok) { SYNC.on = true; SYNC.path = r.script || path; }
```

宿主侧同样无仲裁：`host_server.py:2464` `self._json(d["sync"].start(vp))` 只启动同步引擎；`SyncEngine.start`（`vendor/device/sync_engine.py:64-72`）不碰预设；而 `PresetPlayer._run`（`vendor/device/preset_player.py:139-152`）一旦 `playing` 就持续 `move_to`。反向也一样：`host_server.py:2555-2557`「爆发/缓动接管设备」只 `preset.stop()`，不停 `sync`。

- **复现**：① 先点「播放预设」（此时 `SYNC.on=false`，无确认）→ 预设循环在跑；② 回到媒体库点一个带 `.funscript` 的视频 → `play` → `syncStart` 成功（不检查 `DEV.preset.playing`）→ 同步引擎与预设循环交替写同一 BLE 通道，设备来回抖。
- 同类第二条路径：视频同步进行中点左侧「待机缓动 / 一键爆发」（`app.js:2019-2021` 直接 `quickCmd`，无任何联动确认）→ `/api/quick` 只停预设不停同步（`host_server.py:2555-2557`）。
- **最小修复**：`syncStart()` 内先 `api("/api/preset","POST",{action:"stop"}).then(...)` + `api("/api/quick","POST",{kind:"orgasm",on:false})`/`{kind:"slow",on:false}`，再发 `/api/sync/start`；更稳的做法是宿主在 `/api/sync/start`（及 `/api/quick` 的爆发/缓动分支）里统一做互斥，前端只做展示。

### D3【高】`saveVl` 用陈旧快照整体合并 → 静默回滚刚保存的联动项

`ui/app.js:1845-1850`：

```js
1845:   function vlCfg() { return (S.settings && S.settings.video_link) || {}; }
1846:   function saveVl(patch) {
1847:     api("/api/settings", "POST", { video_link: Object.assign({}, vlCfg(), patch) }).then(function (r) {
1848:       if (!r || r.ok === false) toast("保存失败", (r && r.error) || "", "err");
1849:     });
1850:   }
```
`ui/app.js:1918-1924`（`queueVl` 只做防抖，**不更新** `S.settings`）：

```js
1918:   function queueVl(patch) {
1919:     vlSavePending = Object.assign(vlSavePending || {}, patch);
1920:     clearTimeout(vlSaveTimer);
1921:     vlSaveTimer = setTimeout(function () {
1922:       var p = vlSavePending; vlSavePending = null;
1923:       if (p) saveVl(p);
1924:     }, 300);
```
`S.settings` 只在 1 秒轮询里刷新：`app.js:250` `S.settings = st.settings || {};`（`/api/state` 返回 settings，`host_server.py:2175`）。对比 `bindLink` 就**手动**补了本地值（`app.js:1996-1998`），说明作者知道这个坑，但滑轨路径漏了。

- **复现**：拖动「限制输出范围」（t=0 入队，t=0.3 落盘）→ 在 t≈0.5 再拖「设备速度上限」→ t=0.8 的 POST 带的是 t=-0.5 轮询时的旧 `range_min/range_max` → 服务端行程范围被改回旧值，而卡片上仍显示新范围（`renderVlTabCard` 不重画），宿主按旧值下发设备（`host_server.py:3839-3841`）。
- 窗口期 = 轮询间隔（1s）内连续改两项即触发；`/api/settings` POST 的响应里其实带了新 settings（`host_server.py:2423`），前端丢掉了。
- **最小修复**：`saveVl` 内先本地认值再发：
  ```js
  var next = Object.assign({}, vlCfg(), patch);
  if (S.settings) S.settings.video_link = next;
  api("/api/settings", "POST", { video_link: next })
  ```

### D4【中】联动二次确认文案被 2 秒轮询覆盖

- 设置告警态：`app.js:2049-2052` `vlArmed = {id: btn.id, text: btn.textContent}; btn.classList.add("arm"); btn.textContent = VL_ARM_TEXT;`
- 但 `renderDev` 每 2 秒无条件改写同一个按钮的文字（`app.js:2272`，`setInterval(pollDev, 2000)` 在 `app.js:2427`）：

```js
2272:     $("#vlPresetToggle").textContent = p.playing ? "暂停预设" : "播放预设";
```

- 用户实际看到：点一下 → 按钮变红并显示「将退出联动，确定请再点击」→ **最多 2 秒后文字被改回「播放预设」**，红色 `.arm` 样式仍在（`styles.css:985-992`）→ 关键提示消失。状态机本身没坏（`vlArmed.id` 仍在，第二次点击依然会 `syncStop()`+执行），丢的是"告诉用户再点一次"这个唯一的安全提示。
- **最小修复**：`renderDev` 里跳过正在 arm 的按钮：`if (!(vlArmed && vlArmed.id === "vlPresetToggle")) $("#vlPresetToggle").textContent = ...;`
- 附带：`vlDisarm(reset)` 的 `reset` 形参未被使用（可删，不影响功能）。

### D5【中】设置页「脚本同步」状态/延迟永不刷新：`DEV.sync` 从未赋值

`app.js:2294` / `2303-2308` / `2329` 都读 `DEV.sync`：

```js
2294:     var i = DEV.info || {}, sy = DEV.sync || {}, on = !!DEV.connected;
2303:     if ($("#setSyncState")) {
2304:       $("#setSyncState").textContent = sy.active ? ("脚本同步中 · " + (sy.script || "")) : "脚本同步待命";
...
2329:       if ($("#setSyncDelay") && DEV.sync) $("#setSyncDelay").value = DEV.sync.delay_ms || 0;
```
但 `DEV` 初始对象没有 `sync` 键（`app.js:2251-2253`），`pollDev` 也只赋 4+2 个字段（`app.js:2417-2423`）：

```js
2417:       var d = r.device;
2418:       DEV.available = d.available !== false;
2419:       DEV.connected = !!d.connected;
2420:       DEV.name = d.name || "";
2421:       DEV.info = d.info || {};
2422:       DEV.quick = d.quick || DEV.quick;
2423:       DEV.preset = d.preset || DEV.preset;
```
而宿主**确实返回了** `sync`：`host_server.py:2354` `st["sync"] = d["sync"].state()`。

- 后果：① 视频联动页正在驱动设备时，设置页一直显示「脚本同步待命」；② `#setSyncDelay` 显示恒为 HTML 里的 0，联动页/按钮改过的延迟值不回流；③ `data-delay` 按钮（`app.js:2357-2363`）始终以 `DEV.sync.delay_ms` 的 0 为基准累加，连点 +100 三次后设置页仍显示 0。
- **最小修复**：`pollDev` 里补一行 `DEV.sync = d.sync || {};`（`DEV` 初始化里也可加 `sync: {}`）。

### D6【中】预设网格不随宿主状态刷新（RANDOM 跳转、播放标记失效）

`app.js:2201-2206` 的注释明确承诺了宿主跟随：

```js
2201:     /* 选中态**以宿主为准**（手机端 PhoneViewModel 的 currentPresetId）：
2202:         · 手动点选 → select(id)
2203:         · RANDOM 跳到哪个 → 宿主 selected 变哪个，这里跟着高亮并滚动到可见
```
但实际上 `renderPresetCards()` 只在三处被调用：首次 fetch 回调（`app.js:2197`）、卡片点击（`app.js:2243`）、启动（`app.js:2524`）。`renderDev`/`pollDev`（每 2 秒）**不调用它** → RANDOM 在宿主侧换预设、宿主兜底选中 `normal`（`preset_player.py:89-90`）都不会反映到网格。
另外 `vlPlayingPreset`（`app.js:1392`）从未被赋值，`app.js:2229` 的 `"▶ "` 前缀永远不显示：

```js
2229:         nm.textContent = (on && vlPlayingPreset ? "▶ " : "") + ((base && base.name) || "");
```
- **最小修复**：`pollDev` 里比较 `DEV.preset.selected` 与 `vlSelPreset`，变化时调 `renderPresetCards()`；`renderDev` 里加 `vlPlayingPreset = !!p.playing;`（再调 `renderPresetCards()`，注意只改必要的 DOM，别每 2 秒写 24 张卡）。

### D7【中】预设速度滑轨不落盘 + 每个 pointermove 一个 POST

`app.js:2283-2287`：

```js
2283:   initSlider("vlPresetSpeed", false, function (lo, hi) {
2284:     var v = Math.max(1, Math.round(hi));
2285:     if ($("#vlPresetSpeedVal")) $("#vlPresetSpeedVal").textContent = v;
2286:     presetCmd("speed", { speed: v });
2287:   });
```
`initSlider` 的拖动回调是逐 pointermove 触发的（`app.js:1894-1897` → `set()` → `onChange`），没有 `queueVl` 那种 300ms 防抖（`app.js:1917-1925`）→ 拖一次约发几十个 POST，每个响应还 `pollDev()`（`app.js:2068-2071`）。
宿主侧 `speed` 动作只改内存不落盘：`host_server.py:2586-2587` → `preset_player.set_speed`（`preset_player.py:127-131`）；`settings.device.preset_speed` 在前端**没有任何写入点**（只在 `app.js:2290` 的默认值里出现），而 `_apply_device_settings` 只在它存在时才恢复（`host_server.py:3869-3870`）→ 重启后回到 100。
- **最小修复**：给速度滑轨加同样的 300ms 防抖落盘，例如 `queueVl` 之外再加 `queuePresetSpeed(v)`：`clearTimeout(t); t=setTimeout(function(){ presetCmd("speed",{speed:v}); api("/api/device/settings","POST",{preset_speed:v}); },300)`。

### D8【中】「设备速度上限」允许 0 → 下发硬件限速 0，全部动作速度归零

`app.js:1936`（生成的滑轨 `min=0`，`initSlider` 用 `data-min` 判定，`app.js:1865`）：

```js
1936:         sliderRowHtml("设备速度上限", mainSpd + " Units/s", "vl_max_speed", 0, VL_SPEED_MAX, mainSpd) + '</div>';
```
宿主把它当行程/限速直接下发：`host_server.py:3839-3841` → `channel.py:345` `cmd_limit(int(self.range_lo), int(self.range_hi), int(self.max_speed))`（0 会进 `[0x42, min, max, speedHi, speedLo]`）；并且 `channel.py:254/261`：

```js
254:         base = self.max_speed if speed is None else int(speed)
261:         sp = max(0, min(self.max_speed, sp))
```
→ `max_speed=0` 时任何 `move_to` 的 sp 都是 0（脚本同步 `sync_engine.py:124` 也过这一层）。「一键爆发」勾选「关联上限」（`app.js:1949-1955`）会把这个 0 直接继承给爆发速度。
- **最小修复**：滑轨 `min` 改 1（`sliderRowHtml(..., 1, VL_SPEED_MAX, ...)`），或在 `saveVl` 里 `Math.max(1, ...)` 夹紧；若要保留"0 = 不限速"的语义，则必须让 `channel.apply_motion` 把 0 解释为设备上限而不是 0。

### D9【中】resize 时 24 个 canvas 读写交错 → 强制同步布局抖动

`app.js:2478-2486`（`layoutVl` 内，resize 直接调用，`app.js:2488`）：

```js
2480:     if (_g) $$(".vl-wave", _g).forEach(function (cv) {
2481:       var _w = cv.clientWidth, _d = (window.devicePixelRatio || 1);
2482:       if (_w && Math.abs(cv.width - Math.round(_w * _d)) > 2) {
2483:         var _pr = (VL_PRESETS || []).find(function (x) { return x.id === cv.getAttribute("data-preset"); });
2484:         if (_pr) drawPresetWave(cv, _pr);
```
`drawPresetWave` 会写 backing store（`app.js:2138-2139` `cv.width = ...; cv.height = ...`）。`presets.json` 有 **24** 个预设（已核对：24 条，id 从 `normal` 到 `takeit`），网格 `grid-template-columns:1fr 1fr` + `overflow-y:auto`（`styles.css:955-956`）→ DOM 里始终有 24 个 canvas。于是每读一个 `clientWidth`（强制布局）→ 写 `cv.width/height`（脏化布局）→ 再读下一个：一次**改变宽度**的 resize 事件最多 24 次强制同步布局 + 24 次 Path2D 重绘，拖动窗口时每个 resize 事件都全额重绘（只改高度的 resize 因判据只看宽度而完全不重画，那是 D10 的另一面）。
- **最小修复**：读写分离——先遍历收集 `[{cv, w, h, pr}]`，再统一 `drawPresetWave`；并把 resize 处理用 `requestAnimationFrame` 合并成一帧一次（两个 resize 监听器 `app.js:141`、`app.js:2488` 可合并）。

### D10【中】DPR 修复不彻底：重画判据只看宽度，高度变化（含 40px 兜底）不会被纠正

`drawPresetWave` 的高度来源带兜底，且"要不要重画"只比宽度：

```js
2133:     var dpr = window.devicePixelRatio || 1;
2134:     var w = cv.clientWidth || 130, h = cv.clientHeight || 40;
```
```js
2124:       var w = cv.clientWidth;
2125:       if (!w) return;
2126:       if (Math.abs(cv.width - Math.round(w * d)) > 2 || !cv.height || cv.height < 20) {
```
而 canvas 的 CSS 高度**只随窗口高度变化**（卡片行高由 `layoutVl` 算出）：

```js
2467:     var g = $("#vlPGrid");
2468:     if (g) {
2469:       var h = g.clientHeight;
2470:       if (h > 0) g.style.gridAutoRows = Math.max(46, (h - 10) / 2) + "px";
```

**可证路径（不依赖任何布局假设）**：只拖动窗口的上下边缘（宽度不变）→ `resize` → `layoutVl`（`app.js:2488`）→ `gridAutoRows` 变化 → 卡片与 canvas 的 CSS 高度变化 → 但两处重画判据（`app.js:2126` 与 `app.js:2482`）都只比较 `cv.width`，而 `height` 那半个条件（`!cv.height || cv.height < 20`）对已画过的 backing store（≥40）恒为假 → **不重画**，画布内容被 CSS 纵向拉伸/压缩（线宽与渐变一起变形、走高 DPI 时还会糊）。改窗口宽度才会自愈。

**次要疑点（需实测量化）**：`h = cv.clientHeight || 40` 的兜底与 `cv.height < 20` 阈值相配合，当真实 backing 高度落在 20 设备像素以下时（dpr=1 时 CSS 高度 < 20px，即窗口很矮、`gridAutoRows` 触到 46px 下限并扣掉标题/内边距），`redrawPresetWaves` 每 1.5 秒都会把 24 个 canvas 全量重画一次（`app.js:2523`）；宽度恰好等于 130（隐藏页兜底值，约 680px 窗宽时卡片宽度落在该值附近，容差 ±2）时，首次可见绘制也不会按真实高度纠正。

- **最小修复**：把高度并入判据，并把 40px 兜底换成"记下已绘制尺寸"（避免用 `cv.height` 反推）：

```js
var h = cv.clientHeight, d = window.devicePixelRatio || 1;
if (Math.abs(cv.width - Math.round(w * d)) > 2 ||
    Math.abs(cv.height - Math.round((h || 40) * d)) > 2) { /* drawPresetWave(cv, pr) */ }
```
（`layoutVl` 里那份拷贝 `app.js:2482` 必须同步改，见 Q4 的"逻辑重复"。）

### D11【中】切页后内置播放器后台继续播、同步继续跑，但页面无任何控制入口

`showPage`（`app.js:105-136`）没有任何暂停/收敛逻辑；`.page{display:none}`（`styles.css:346`）不会暂停媒体元素；播放中每 `timeupdate` 仍在推设备（`app.js:1585` `v.addEventListener("timeupdate", ... syncTick(v.currentTime))`）。顶栏只有设备/主题/最大化按钮，"一键急停"只在联动页（`app.js:2021`）→ 用户切到设置页后，视频在后台继续出声、设备继续被脚本驱动，屏幕上没有任何指示或急停入口。
- **最小修复**：`showPage` 离开 `library` 时若 `vlPlaying()` 则 `v.pause()`（并让 `pause` 事件走既有的 `/api/quick {kind:"pause"}` 让路，`app.js:1575`）；或保留后台播放但加一个全局"正在播放 · 急停"入口。

### D12【低】四个 id 在 index.html 不存在 → 功能静默失效（不崩溃）

脚本核对（§5）结论：静态引用但 HTML 无此 id 的只有 4 个，调用点都有 `if` 护栏，所以不会抛错，但功能是死的：
- `motionSeg`/`motionThumb`：`app.js:205-208`、`initSeg("motionSeg","motionThumb",...)`（`app.js:813`）→ `initSeg` 首行 `if (!seg || !thumb) return;` 直接返回；`$$("#motionSeg button")` 得到空列表 → 「动效」分段控件在 UI 里根本不存在。
- `setSlowSpeed`/`setOrgasmSpeed`：`app.js:2326-2327` 永不执行 → `settings.device.slow.max_speed` / `orgasm.max_speed` 是僵尸键（宿主侧已被 `video_link.idle_speed`/`burst_speed` 覆盖，`host_server.py:3844-3852`）。
- **最小修复**：要么删掉这些死绑定（并在 `loadSetDev` 里停止读僵尸键），要么补上对应 UI；`device.slow.max_speed`/`orgasm.max_speed` 建议在 `_apply_device_settings` 里一并清理，避免"两套键名"长期并存。

### D13【低】`renderLibRoots` 未转义路径

`app.js:560-563`：

```js
560:     box.innerHTML = roots.map(function (p) {
561:       return '<div class="row flush"><div class="grow"><div class="name mono">' + p + '</div></div>' +
562:              '<button class="btn ghost" data-del-libroot="' + p + '">移除</button></div>';
```
同为路径渲染的 `renderRoots`（`app.js:401-406`）用的是 `esc(p)`。Windows 路径不允许 `<`/`"`，所以现实可利用性低于 D1，但 `settings.library_roots` 可被手改/被其它工具写入。
- **最小修复**：`esc(p)`；`data-del-libroot` 里的路径更稳的做法是改存下标或 `encodeURIComponent`。

### D14【低】1.5s 兜底定时器

`app.js:2523` `setInterval(redrawPresetWaves, 1500);` —— 库页隐藏时每个 canvas 的 `clientWidth` 为 0，`app.js:2124-2125` 立即返回，实测成本可忽略（每 1.5s：1 次 `querySelectorAll` + 24 次属性读取）；**无内存增长**（`Path2D`/渐变都是局部的，无累积容器）。但它是治标：把高度并入判据（D10）后，用"可见性 + 尺寸变化"触发即可删掉它；保留也无害。唯一的非零成本场景是 D10 里那个"backing 高度 < 20 设备像素 → 每 1.5s 全量重画 24 个 canvas"的窄条件。

---

## 3. 可疑待验证（需要运行时/产品确认）

1. **BOOST/RANDOM 的语义边界（与 Q3 相关）**：目前 `toggle_boost`/`toggle_random` 只在"预设已在播放"时才有设备动作（`preset_player.py:114-125`、`103-112`），所以移除二次确认是**对的**。但这条正确性依赖宿主实现；一旦有人把 `toggle_boost` 改成"未播放也启动预设"，BOOST 就变成真实的绕过通道。建议在 `app.js:2073-2074` 加注释说明该前提，或在宿主侧对"预设启动"统一仲裁（同 D2 的修复）。
2. **`.vl-wave` 默认 300×150 与目标尺寸碰撞 / 40px 兜底高度**：未绘制过的 canvas 尺寸恰好等于 `round(w*dpr)` 时会被判为"已画好"。当前布局下不可达（初始总会以 130×40 兜底画一次），但**宽度恰好 ≈130px**（约 680px 窗宽，容差 ±2）时，隐藏页的兜底绘制会被判为"尺寸正确"，波形就一直用 40px 高度绘制 → 与 D10 同一个根因。属防御性缺口，建议把"已绘制尺寸"记进 `cv.dataset` 而不是反推 `cv.width`。
3. **`cv.height < 20` 阈值导致的周期性全量重画（待实测）**：dpr=1 且 canvas 的 CSS 高度 < 20px（窗口很矮、`gridAutoRows` 触到 46px 下限）时，`app.js:2126` 的 `cv.height < 20` 恒真 → `redrawPresetWaves` 每 1.5s 把 24 个 canvas 全部重画。需要用 1366×768 / 更矮的窗口在 Performance 面板确认是否真落到该区间。
4. **「关联」取消后的数值回落**：勾选 `idle_link`/`burst_link` 时滑轨被禁用且**不写** `idle_min/idle_max`（`app.js:1939-1944`）；取消关联后卡片回落到上次存的 `idle_min/idle_max`（从未设置过就是 0–100）。这可能就是"恢复自身值"的有意语义，但若产品期望"继承主范围"，则去掉勾选时应把主范围写回 `idle_min/max`。
5. **`#setScriptFolder`（设置页）与 `#syncScriptLocal`（设备同步页）是同一个键** `settings.script_folder`（宿主 `host_server.py:3199` `"local_key": "script_folder"`、`3241`；index.html:832 的文案也承认了这一点）。两侧各自轮询刷新，但 `busyEditing` 护栏（`app.js:445`）会让正在编辑的一侧保留旧值——"改完 A 页 B 页多久更新"需要实测。
6. **XSS 的治理口径**：D1 是前端转义缺失，宿主侧也**不**清洗 `os.scandir` 名字（`host_server.py:2319/2327`）。是否需要"文件名白名单/替换"由产品定；最小修复在前端 `esc` 即可闭合。
7. **`video_link.reversed` 是死键**：宿主会读它（`host_server.py:3840` `reversed_=bool(vl.get("reversed"))`），但前端从不写（`setReversed` 写的是 `settings.device.reversed`，`app.js:2366`）→ 每次设置保存都会先 `apply_motion(reversed_=False)` 再靠 `host_server.py:3858` 覆盖回来。当前顺序下结果正确（`apply_limits`/`cmd_limit` 不依赖 `reversed`，`channel.py:345`），但属脆弱耦合，建议删掉 `video_link.reversed` 或让它与 `device.reversed` 同源。

---

## 4. 必答问题逐条回答

### Q1 设置页与视频联动页是否存在「同一设置两套 UI / 两套键名 / 一边改另一边不刷新」？

**答：存在 3 类问题（1 个真回滚见 D3，1 个僵尸键见 D12，1 个永不刷新的状态显示见 D5），另有两处键名重复但当前无害。**

#### (1) 联动页（page-library）id → settings 键映射表

| 控件 id（生成方式） | 位置 | settings 键 | 写入 | 读回 |
|---|---|---|---|---|
| `#vl_range`（双点滑轨，模板生成） | app.js:1935 | `video_link.range_min` / `range_max` | `queueVl`→`saveVl`（1961） | `renderVlTabCard`←`S.settings`(250) |
| `#vl_max_speed`（单点滑轨） | app.js:1936 | `video_link.max_speed` | queueVl（1965） | 同上 |
| `#vl_idle_link`（勾选） | app.js:1943 | `video_link.idle_link` | `bindLink`（1976/1994-2001） | 同上（本地即时认值） |
| `#vl_idle_range`（双点） | app.js:1942 | `video_link.idle_min` / `idle_max` | queueVl（1970） | 同上；勾选关联时禁用且不写 |
| `#vl_idle_speed`（单点） | app.js:1944 | `video_link.idle_speed` | queueVl（1974） | 同上 |
| `#vl_burst_link`（勾选） | app.js:1953 | `video_link.burst_link` | bindLink（1986） | 同上 |
| `#vl_burst_range`（双点） | app.js:1952 | `video_link.burst_min` / `burst_max` | queueVl（1980） | 同上 |
| `#vl_burst_speed`（单点） | app.js:1954 | `video_link.burst_speed` | queueVl（1984） | 同上；勾选关联上限时禁用 |
| `#vl_burst_speed_link`（勾选） | app.js:1955 | `video_link.burst_speed_link` | bindLink（1987） | 同上 |
| `#vlPresetSpeed`（单点） | index.html:476 | **无 settings 键**（只 POST `/api/preset {action:speed}`→宿主内存） | app.js:2286 | `renderDev`←`DEV.preset.speed`（2276-2278） |
| `#vlPGrid` 卡片点击 | index.html:481 | **无**（POST `select`，宿主内存 `selected`） | app.js:2244 | 名义上 `DEV.preset.selected`（2205），实际不刷新 → D6 |
| `#vlBoost`/`#vlRandom`/`#vlPresetToggle` | index.html:459-461 | **无**（宿主内存 boost/random/playing） | app.js:2073-2076 | `renderDev`（2272-2275） |

宿主消费：`host_server.py:3836-3852`（`video_link` → `ch.apply_motion` + `quick.apply`），即这三张卡是**设备侧待机缓动/一键爆发的唯一主设置**（宿主注释明确写了 `host_server.py:3842-3843`）。

#### (2) 设置页（page-settings）联动相关 id → 键映射表

| 控件 id | index.html | settings 键 | 写入 | 读回 |
|---|---|---|---|---|
| `#setScriptSync` | 835 | `device.script_sync` | `saveDev`（2372） | `loadSetDev`（2322），**仅启动一次** |
| `#setSlowIdle` | 774 | `device.slow.idle_detect_seconds` | saveDev（2342） | loadSetDev（2325） |
| `#setSkipIdle` | 778 | `device.skip_idle` | saveDev（2343） | loadSetDev（2323） |
| `#setIdleThreshold` | 781 | `device.idle_threshold` | saveDev（2344） | loadSetDev（2324） |
| `#setSyncDelay` | 838 | **不落盘**（POST `/api/sync/delay`→宿主内存 `sync.delay_ms`，`host_server.py:2469-2472`） | 2373-2375 | ❌ `if (DEV.sync)` 永不成立（2329）→ D5 |
| `#setSyncState`/`#setSyncSub` | 840 | 只读展示（应来自 `device/state.sync`） | — | ❌ `DEV.sync` 缺失 → 永远"脚本同步待命" → D5 |
| `data-delay` 四按钮 | 794-797 | 同上（内存） | 2357-2363 | ❌ 基准恒为 0 → D5 |
| `#setManualPos`/`#setManualMove` | 784/789 | 无（POST `/api/device/move`，不落盘） | 2345-2356 | — |
| `#setReversed` | 808 | `device.reversed` | 2366 | 2301 ← `DEV.reversed` |
| `#setA10` / `#setOcMode` | 804/813 | `device.a10_mode` / `device.oc_mode` | 2365 / 2367-2370 | 2300 / 2302 |
| `#setScriptFolder` | 828 | `settings.script_folder`（与设备同步页 `#syncScriptLocal` **同键**） | 2381/2386 | 2330 |
| **缺失** | — | `device.slow.max_speed`（2326）、`device.orgasm.max_speed`（2327） | ❌ 控件不存在 → D12 | 读僵尸键 |
| **缺失** | — | 待机缓动范围/速度、一键爆发范围/速度 | ❌ 设置页**没有任何入口**，只在联动页三张卡 | — |

#### (3) 结论

- **真回滚（高）**：D3 —— 联动页内部两项连续修改，后一次会把前一次的整体覆盖掉。
- **两套键名并存（低→中）**：`device.slow.max_speed` / `device.orgasm.max_speed`（僵尸，无 UI）与 `video_link.idle_speed` / `burst_speed`（生效）语义重叠，宿主侧前者被后者覆盖（`host_server.py:3844-3852`），只在 `loadSetDev`（2326-2327）里还被读取 → 建议清理。
- **一边改另一边不刷新（中）**：D5（设置页同步状态/延迟不走轮询）+ D6（预设网格不跟宿主）。
- **同键双 UI（可接受）**：`script_folder` 在设置页与设备同步页各有一份输入框（index.html:832 已注明是同一设置），风险仅是显示刷新时机（见 §3-4）。
- **可发现性缺口**：设置页完全看不到「限制输出范围 / 设备速度上限 / 待机缓动范围 / 一键爆发范围」，用户不进联动页就改不了这几项联动设置。

### Q2 事件绑定是否有 null 解引用风险？id 契约是否完整？

**答：静态契约完整，未发现会抛 null 的解引用；风险只在"加载顺序"这一层。**

- `node --check ui/app.js` → **exit 0**（语法通过）。
- 三份脚本核对（§5）：`index.html` 313 个 id；`app.js` 里 334 处 id 引用（195 个不同 id）。
  - **静态引用但 HTML 无此 id：4 个**（`motionSeg`、`themeToggle2`、`setSlowSpeed`、`setOrgasmSpeed`），调用点全部有护栏（`app.js:810` `if (_t2)`、`app.js:208` `if ($("#motionSeg"))`、`app.js:2326-2327` `if (...)`），**不会抛错**，但功能静默失效（D12）。
  - **拼接 id 全部命中**：`"#sync"+cap+"Badge/Local/Device/Result"`、`"#syncRun"+cap`（cap ∈ Script/Video）生成的 10 个 id（syncScriptBadge…syncRunVideo）在 index.html 中**全部存在**。
  - **运行时生成的 id 全部配套**：`dualSliderHtml`/`sliderRowHtml`/`linkBoxHtml` 生成的 9 个 `*_val` 覆盖了 JS 取用的 6 个 `#vl_*_val`（`#vl_range_val`、`#vl_max_speed_val`、`#vl_idle_range_val`、`#vl_idle_speed_val`、`#vl_burst_range_val`、`#vl_burst_speed_val`），无"取用但模板不生成"的情况 → **不会出现 `$("#x_val").textContent` 抛 TypeError**。
  - **属性选择器**：`[data-delay]`（index.html 794-797 有）、`[data-pick]`、`[data-mt-group]`、`[data-side]`、`[data-page]` 均存在；`[data-del-root]`/`[data-del-libroot]`/`[data-preset]`/`[data-motion-opt]` 属运行时生成或（`data-motion-opt`）随缺失的 `motionSeg` 一起不存在——`$$()` 返回空数组，安全。
  - `renderPresetCards` 的前置条件也已核对：`ui/presets.json` 24 条记录都有非空 `segments`、`keyframes` 数组与 `previewFrom/previewTo`，`presetLoopSec`（2108-2117）不会踩到 `kfs.length` 的空引用。
- **唯一的前提**：`<script src="app.js">` 在 `index.html:935`（body 末尾），所以 `app.js` 顶层那些无护栏的 `$("#vlBrowseBody").addEventListener`（1446）、`$("#vlHeat")`（1786）、`$("#vlPGrid")`（2238）、`$("#devBtn")`（2430）在 IIFE 执行时元素都存在。**一旦有人把 script 移到 `<head>` 或给页面加异步注入的元素，这批顶层绑定会立刻变成 TypeError 且整页脚本停摆**。建议把顶层绑定收进 `bind()`/`boot()`（`app.js:2583`），或给这几处补护栏——这是"契约靠位置而不是靠代码"的隐患。

### Q3 移除 BOOST/RANDOM 的联动二次确认，是行为回退还是有意为之？

**答：不是回退，是正确的修正；但同一改动暴露了真正的护栏盲区（见 D2）。**

证据链（宿主语义）：
- `toggle_boost`（`preset_player.py:114-125`）只翻 `boost` 标志并把 `speed` 记成 500；**不启动循环**，`_effective_speed` 只在 `_play_one_loop`（`preset_player.py:162`）里被用到。
- `toggle_random`（`preset_player.py:103-112`）只翻 `random_mode`；`if self.playing` 才重新起循环（`:108-111`）。
- 对比 `toggle_play`（`:83-84`）→ `start()`（`:86-96`）会真的 `_spawn(self._run(gen))` 开始推设备。

因此旧代码 `vlPresetButton(this, function () { presetCmd("boost"); })` 在联动态（`SYNC.on`，预设未播放）下第二击会执行 `syncStop()` + `presetCmd("boost")`：
- `syncStop()`（`app.js:2401-2405`）真的停掉脚本同步 = **退出联动**；
- 而 BOOST 在没有预设播放时不产生任何设备动作。
即"用户按提示确认退出联动，结果什么都没发生"——这是**误伤**。移除后 `app.js:2073-2074` 直接 `presetCmd(...)`，在"预设未播放"的常态下对设备无副作用（`boost`/`random` 标志只影响之后的预设播放），是**有意且正确**的语义收敛。
中间那颗 `#vlPresetToggle` 保留两段式确认（`app.js:2076`）也是对的，因为只有它会真的启动循环（且 `vlPresetButton` 的第二击会先 `syncStop()`，`app.js:2056-2061`）。

**但要注意两点**：
1. 这条正确性依赖"BOOST/RANDOM 不会启动预设循环"这个宿主不变量（见 §3-1），建议在代码里写明前提。
2. 真正该有护栏的是**启动同步**（`syncStart`，D2）和**快捷动作**（`vqIdle`/`vqBurst`，`app.js:2019-2020`）——这三个才是会在联动进行中抢设备写权的入口，而它们目前都没有任何"退出联动"确认或互斥。

### Q4 波形绘制：DPR 修复是否彻底？还有 canvas 0 尺寸/backing store 反复重分配/定时器空转/resize 抖动吗？

**答：主体修复正确，但不彻底（D10）；无反复重分配、无内存增长；有 resize 抖动；定时器接近零成本但属治标。**

| 检查项 | 结论 | 依据 |
|---|---|---|
| 双重乘 dpr / 改元素宽度 | ✅ 已修好 | `app.js:2138-2139` backing = CSS×dpr；不再写 `cv.style.width`；`.vl-wave{width:100%; flex:1; min-height:0}`（`styles.css:970`）保证布局尺寸不被 backing store 反向影响 → 不会出现"波形只占左边 1/dpr"，与 `design/_shots/98-预设波形.png` 的期望表现一致（波形满宽、两列） |
| 重画判据完整性 | ❌ **不彻底** | D10：两处判据（`app.js:2126`、`2482`）都只比较 `cv.width`，而 canvas 的 CSS 高度只随窗口高度变化（`gridAutoRows`，`2469-2470`）→ 只改窗口高度的 resize 不触发重画；`!cv.height \|\| cv.height < 20` 对已画过的 backing（≥40）恒为假，兜底不住 |
| canvas 0 尺寸 | ⚠️ 部分 | 隐藏页 `clientWidth=0` → `redrawPresetWaves` 早退；`renderPresetCards` 首次绘制在隐藏时以 `w=130,h=40` 兜底（可见后按宽度纠正；**高度纠正不了**，见 D10/§3-2） |
| backing store 反复重分配 | ✅ 无 | `redrawPresetWaves` 只在尺寸不符时调用 `drawPresetWave`；`drawHeat` 只在 `cv.width!==pw` 时赋值（`app.js:1762`）→ 不会每帧重置画布 |
| 1.5s 定时器空转/内存增长 | ✅ 基本无 | `app.js:2523`：隐藏页时每轮只是 1 次 `querySelectorAll` + 24 次 `clientWidth` 后 `return`（2124-2125）；`Path2D`/渐变均为局部变量，无累积引用；`vlHeatScript`/`heatOff` 复用不增长。例外：backing 高度 < 20 设备像素时每 1.5s 全量重画 24 个 canvas（§3-3，待实测） |
| resize 抖动 | ❌ 有 | D9：24 个 canvas 的"读 clientWidth → 写 cv.width/height → 再读"交错，一次 resize 事件最多 24 次强制同步布局 + 24 次重绘；`app.js:141` 与 `app.js:2488` 两个 resize 监听器未合并、未做 rAF 节流 |
| 逻辑重复 | ⚠️ 维护风险 | 同一套"按宽度重画"逻辑写了两份：`redrawPresetWaves`（2119-2131）与 `layoutVl` 内联块（2478-2486），两处判据还不一致（后者没有 `height` 兜底条件也不检查 `!w`）。修 D10 必须同时改两处，否则再次漂移 |
| 其余 DPR 路径 | ✅ | `drawHeat`（1755-1785）backing = CSS×dpr + `setTransform(dpr,…)`；离屏 `heatOff` 用 `setTransform(1,0,0,1,0,0)`（`renderHeatBitmap`，1717）→ 无双重缩放；`heatOffKey` 已含 `pw x ph` 与主题（1772） |

### Q5 XSS/注入：`renderBrowse` 等 innerHTML 是否转义了来自后端的目录名/文件名？

**答：`renderBrowse` 没有转义（D1，高）；同页 `renderLibRoots` 也没转义（D13，低）；其余 innerHTML 点均转义。**

- 未转义：`app.js:1418`（`d.name`）、`app.js:1432`（`v.name`）、`app.js:561-562`（库根路径）。
- 已转义：`renderRoots`（403/405）、`renderDlnaLogs`（419）、`renderSyncLogs`（471）、时间线 `timeline`（314）都走 `esc()`（定义 85-89）；`loadModels` 用 `textContent`（682/685）；`renderSync`/`renderDev` 用 `textContent`。
- 缩略图路径有双重防护：`encodeURIComponent`（`app.js:1422`）+ 宿主正则 `re.fullmatch(r"[0-9a-f]{16}\.jpg", name)`（`host_server.py:2370`）。
- 影响面（已在代码上确认可达）：注入脚本与 `/api/*` 同源 → `POST /api/device/move`（`host_server.py:2501`）、`POST /api/quit`（`host_server.py:2668`）、`POST /api/settings`（`host_server.py:2415`）都无需额外凭据；`do_POST` 的 Origin 栅栏（2398-2403）对页内脚本无效。
- 触发条件：媒体库目录里存在名字带 `<`/`>`（或对 `"`/`'` 敏感的属性位置）的文件/目录，用户浏览到该目录即触发。

---

## 5. `index.html` id 与 `app.js` `$("#id")` 契约核对（原始清单）

脚本（只读，临时目录，未入库）：

- `idcheck.js`：全量 id 双向 diff（313 个 HTML id vs 334 处引用，195 个唯一 id）
- `idcheck2.js`：区分"静态引用 / 拼接前缀 / 模板生成"，并核对 `data-*` 与 class 选择器
- `idcheck3.js`：枚举拼接 id 与 `*_val` 生成 id，逐条核对存在性

**缺失 id 清单（app.js 引用但 index.html 无此 id）—— 共 4 个真缺失，全部有护栏、不抛错：**

| id | 引用点 | 判定 |
|---|---|---|
| `motionSeg` | app.js:208、813（`initSeg("motionSeg","motionThumb",...)`） | 真缺失；`initSeg` 首行 return、`$$` 返回空 → 静默失效（D12） |
| `themeToggle2` | app.js:810（`if (_t2)` 护栏） | 真缺失；无害死代码 |
| `setSlowSpeed` | app.js:2326（`if (...)` 护栏） | 真缺失；僵尸键读写（D12） |
| `setOrgasmSpeed` | app.js:2327（同上） | 真缺失；同上 |

**其余"疑似缺失"均为误报，已逐条排除：**

| 疑似 | 排除依据 |
|---|---|
| `sync`（441/444/447）、`syncRun`（454/503） | 拼接前缀；生成的 10 个 id 在 index.html 全部存在（idcheck3 输出"缺失: 无"） |
| `vl_range_val`、`vl_max_speed_val`、`vl_idle_range_val`、`vl_idle_speed_val`、`vl_burst_range_val`、`vl_burst_speed_val` | 由 `dualSliderHtml`/`sliderRowHtml` 模板运行时生成；"取用但模板不生成"= 无（不会 null.textContent） |
| `page-*` | `showPage` 用 `document.getElementById("page-" + name)` 拼接 |
| `themeLabel` 等 | `document.getElementById(id)` 变量取值（app.js:192/195/2597） |

**"多余" id（HTML 有、app.js 未直接引用）**：`subCard`、`winCtrl`、`vlVChrome`、`syncCardScript`、`syncCardVideo`、`syncLogBox`、`syncLogSeg`、`syncLogThumb`、`syncScriptBadge`、`syncScriptResult`、`syncVideoBadge`、`syncVideoResult`、`themeLabel`、`page-*`、`i-*`（SVG symbol）。其中 `sync*` 与 `themeLabel` 实际是拼接/变量引用的；真正未被引用的只有 `subCard`/`winCtrl`/`vlVChrome`（纯容器，`vlVChrome` 的状态由 `.vl-vwrap.idle` 类驱动），**不影响功能**。

`node --check ui/app.js` → `exit 0`。

---

## 6. 本次未提交改动（`git diff ui/app.js`）逐项评估

1. **`showPage` 增加 `requestAnimationFrame(redrawPresetWaves)`（app.js:135）** — 方向正确（切页后按新尺寸重画），rAF 注册顺序也正确（`layoutVl` 在前）。函数声明提升，`vlApplyHash` 在启动期调用它不会报未定义。遗留：判据只看宽度（D10）。
2. **移除 `#vlBoost`/`#vlRandom` 的 `vlPresetButton` 包装（app.js:2073-2074）** — 判定为**正确修正**（详见 Q3），不是回退；真正的护栏盲区在 `syncStart`/快捷动作（D2）。
3. **`drawPresetWave` 的 DPR 修复（app.js:2132-2141）** — 主体正确：backing = CSS×dpr、保留 `setTransform(dpr,…)`、不再写 `style.width/height`、不再双重乘 dpr。与 `design/_shots/98-预设波形.png` 的期望（波形满宽、卡片两列、无左侧偏移）一致。不彻底处见 Q4/D10。
4. **`layoutVl` 内新增按宽度重画块（app.js:2478-2486）** — 与 `redrawPresetWaves` 逻辑重复且判据更弱（不查高度、不查 `!w`）；建议合并成一个函数（D9/D10 一起修）。
5. **`setInterval(redrawPresetWaves, 1500)`（app.js:2523）** — 无内存增长、空转成本低（D14）；有了正确的判据与 resize/切页触发后即可删除。
6. `version.json` 1.0.61→1.0.62（版本号同步，无风险）。

---

## 7. 覆盖边界（未审查/未能验证的部分）

- 未运行 GUI/未启动服务器，所有"视觉/时序"结论均由代码路径推导；D9/D10 的表现（模糊程度、抖动帧数）建议由实测截图或 Performance 面板复核。
- 未实际连接 BLE 设备，D8 的"0 → 设备不动"是 `channel.py`/`quick_moves.py` 的限幅算术推导结论，未在真机验证。
- 后端 `host_server.py` 的路径校验、Range 流、CSRF 栅栏只做了与前端结论相关的旁证阅读，未做完整审查（属其它任务范围）。
- `vendor/device/*` 仅阅读了与联动语义直接相关的函数（preset_player / quick_moves / sync_engine / channel 的限幅与状态），未做逐行审查。
