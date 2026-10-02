# R01 后端链路审查：视频联动（设置 → 设备层 → 脚本同步/预设/快捷动作）

> **⚠️ 第一轮审查存档**：本文是 2026-10-02 凌晨的第 1 轮审查（当时尚未以手机端为
> 真源），**其中部分结论已被修正或推翻**——修正记录见
> `review/REVIEW-全面审查报告-R2.md` §7 与用户裁定（Q1-Q4）；
> 实施状态以 `review/最终修复清单-R2.md` 为准。


- 任务：task-1（owner: backend-reviewer）
- 范围：`host_server.py`（video_link / device / sync / quick / preset 路由与 `_apply_device_settings`）、`vendor/device/*`、`vendor/player/{mpv_player,library}.py` 与联动有交集的部分、`ui/{app.js,index.html,presets.json}` 中与这些路由的契约。
- 方法：只读源码 + **三个只读复现脚本**（不启 GUI、不启服务器、不写 `data/integrated_settings.json`）。所有复现都用假通道（`move_to` 精确复刻 `channel.py:252-253` 的 `force` 语义）投到真实事件循环线程里跑真实设备层代码。命令与输出见 §D。
- 严重级口径：**阻断**＝功能不可用/进程级资源被占死；**高**＝安全控制失效或主要联动功能明显错误；**中**＝静默失败、状态不一致、可观测的错行为；**低**＝口径/边界不一致。

---

## 0. 结论速览

| # | 严重级 | 一句话 | 关键证据 |
|---|---|---|---|
| 1 | **阻断** | 「一键急停/继续」按钮必然 500：`q.is_stop` 不存在该属性 | host_server.py:2551 + quick_moves.py（无 is_stop） |
| 2 | **阻断** | 22/24 个预设只有 `keyframes`，选中即把 BLE 事件循环空转锁死（CPU 占满、设备不动、`submit` 全部超时） | preset_player.py:154-173 + ui/presets.json |
| 3 | 高 | 急停（`allow_move=False`）**挡不住**待机缓动：缓动循环用 `force=True` 绕过 | quick_moves.py:156-171 + channel.py:252 |
| 4 | 高 | 急停**挡不住**预设播放：预设循环不看 `allow_move` 且 `force=True` | preset_player.py:168 + channel.py:249-253 |
| 5 | 高 | 待机缓动在**任意一次外部动作后永久停止**（唯一的重启者 `_idle_watch` 被自己杀掉） | quick_moves.py:60-68, 140-154 |
| 6 | 高 | 「伪装设备（VorzePiston 模式）」档案映射**写反了**（1→serveu / 0→vorze） | host_server.py:3860 vs protocols.py:41-42 |
| 7 | 高 | 播视频与预设播放**无互斥**：两个引擎同时以 `force=True` 写帧 | host_server.py:2455-2464,2555-2557 + app.js:1584 |
| 8 | 高 | 设置里「播放视频时同步驱动设备」开关**关掉不生效**（`SET_DEV.script_sync` 只在启动时读一次） | app.js:2372,2389,2393 |
| 9 | 中 | `video_link.reversed` 是死读：同一函数内立刻被 `device.reversed` 覆盖 | host_server.py:3840 vs 3858 |
| 10 | 中 | `device.orgasm.*` / `device.slow.{min,max,max_speed,link_percent}` 全是死设置（写了没人读） | host_server.py:3854 vs 2521-2534 |
| 11 | 中 | 预设速度滑轨是运行时值，但每次保存设置都被磁盘里的 `device.preset_speed` 回灌覆盖，且滑轨值不落盘 | host_server.py:3869-3870 + app.js:2283-2287 |
| 12 | 中 | `_apply_device_settings` 中途异常 → 其余联动项**静默不生效**，只留一行 warning | host_server.py:3838-3841,3871-3872 |
| 13 | 中 | 写帧失败被 `except: pass` 吞掉，且 `_last_pos` 已先更新 → 该段帧永久丢失、不重试、无痕迹 | sync_engine.py:119-127 |
| 14 | 中 | `/api/sync/tick` 无合并/无时间戳：BLE 一卡就排队追帧（旧位置按序重放＝抖动） | sync_engine.py:84-88 + host_server.py:2465-2468 |
| 15 | 中 | 暂停/返回媒体库都**不停止**脚本同步；`syncTick` 也不判播放态 → 残留 + 暂停中 seek 也发帧 | app.js:1575-1586,1615-1636,2406-2412 |
| 16 | 中 | 断开/重连状态泄漏：`_cleanup` 不清 `limit_min/limit_speed/allow_move/moves/recent`；重连后 `sync._last_pos` 残留抑制首帧 | channel.py:196-209 + host 2493-2500 + sync_engine.py:27,120 |
| 17 | 中 | 暂停→播放时待机缓动抢跑（空闲计时把暂停时长算进去） | quick_moves.py:122-131,140-152 |
| 18 | 中 | `idle_link` 关掉后 UI 与设备层不一致（关联期间不写 idle_min/max，`apply()` 只覆盖不回写） | app.js:1939,1968-1971 + host 3845-3852 + quick_moves.py:203-208 |
| 19 | 低 | `/api/sync/delay` 不落盘，重启丢失（UI 有输入框） | host_server.py:2469-2472 |
| 20 | 低 | 脚本 t 落在首/末动作点之外时，速度 0 被 `or None` 变成**满速上限** | sync_engine.py:98,124 + channel.py:254 |
| 21 | 低 | `quick_moves.apply` 把 bool 字段写成 int 0/1（`isinstance(True, int)` 为真） | quick_moves.py:197,201 |
| 22 | 低 | `_forced_toy` 一旦设置永不清除；`/api/device/settings` 无法把字段清空（None 被过滤） | host_server.py:3862,2524 |
| 23 | 低 | 外部 mpv 播放路径完全不联动设备；`sync_engine.py:4` 文档声称"mpv 由宿主 1s 轮询带"并不存在 | sync_engine.py:4 + mpv_player.py:190-208 |
| 24 | 低 | `browse` 判 `has_funscript` 大小写敏感，与 `SyncEngine.resolve_script` 不一致 | host_server.py:2322-2328 vs sync_engine.py:35-42 |
| 25 | 低 | `/api/library/stream` 根目录用**字符串前缀**匹配，缺分隔符（`E:\testvideo-evil` 也通过） | host_server.py:2218 |

---

## A. 确认的缺陷

### A1【阻断】`/api/quick` 的「一键急停/继续」必然 500：`q.is_stop` 属性不存在

```python
# host_server.py:2550-2554
                if kind == "stop":
                    was = q.is_stop
                    res = q.set_stop(on)
                    if was and not on:
                        d["sync"].reset_last_index()   # 手机端：急停→继续 强制重发当前段
```

`QuickMoves` 只有 `set_stop/stop_orgasm/stop_slow`，没有 `is_stop`（状态在 `state()["stop"]`）：
`vendor/device/quick_moves.py:79-81,210-214`。实测 `hasattr(QuickMoves, "is_stop") is False`。
异常被路由兜底吞成 HTTP 500：`host_server.py:2673-2674`。

- 影响：UI `#vqStop`（`ui/app.js:2021`）永远拿到 `ok:false, error:"AttributeError: 'QuickMoves' object has no attribute 'is_stop'"`
  → **急停/继续完全不可用**；`allow_move` 永远是 `True`；`DEV.quick.stop` 恒 false，按钮文案也永远停在「一键急停」；
  `reset_last_index()`（急停→继续后强制重发当前段，`sync_engine.py:74-77`）成为不可达代码。
- 最小修复：`was = not q.ch.state.allow_move`（或把 `is_stop` 做成 `state()["stop"]` 的 property），并给这一分支加一条单测。

### A2【阻断】只有 keyframes 的预设会把 BLE 事件循环空转锁死

```python
# vendor/device/preset_player.py:154-173（节选）
    async def _play_one_loop(self, pr: dict, gen: int) -> None:
        for sg in (pr.get("segments") or []):          # ← keyframes-only 预设：空列表
            ...
            if dist > 0:                               # ← 一帧都不发
                await self.ch.move_to(e, speed, force=True)
```

`_run()` 的 `while self.playing and gen == self._gen: await self._play_one_loop(...)`
（`preset_player.py:139-152`）里，被 await 的协程**一次都不让出**（无 await 即返回），
于是变成纯 CPU 空转的 while；`playing/_gen` 仍由 HTTP 线程可改，所以只有 `stop()` 能救。

- 实测：`ui/presets.json` 24 个预设里 **22 个 `segments: []`、只有 `keyframes`**（classic/deep/tease/edge/rhythm/…）。
  选中这类卡片点「播放预设」后 1.2s 内：看门狗任务 tick 次数 **0**、写帧 **0**、`playing=True`；
  此时 `submit()` 立即 `TimeoutError`（真实路由用的超时是 10s/20s/40s）。
- 影响：用户视角是「点了播放，设备一动不动」（静默失败），同时一个 CPU 核被占满；
  所有依赖 `ch.submit()` 的路由（`/api/device/scan|connect|disconnect|move|limit|refresh`、`/api/settings` 里的 `apply_limits`）
  在此期间全部阻塞到超时；`sync_engine.tick()` 排队的协程会后置执行 → 恢复瞬间**批量补发旧位置帧**（追帧/抖动）。
- 最小修复：`_play_one_loop` 里对空段/`dist==0` 且 `wait_ms<=0` 的情况补一次 `await asyncio.sleep(TICK_MS/1000)`；
  更彻底的是让 `PresetPlayer` 支持 `keyframes`（或加载时把 keyframes 展开成 segments）。

### A3【高】急停挡不住「待机缓动」

```python
# vendor/device/quick_moves.py:161-167
            while self.is_slow and gen == self._gen_s:
                self._slow_index += 1
                target = lo if self._slow_index % 2 else hi
                self._self_moving = True
                await self.ch.move_to(target, speed, force=True, raw=True)   # ← force=True 绕过 allow_move
```
```python
# vendor/device/channel.py:252-253
        if not self.state.allow_move and not force:
            return False
```

`set_stop(True)` 只做 `ch.set_allow_move(False)`（`quick_moves.py:79-81`），`_slow_loop` 里没有任何 `allow_move` 判断
（对比 `_orgasm_loop:108-110` 是有的，`sync_engine._apply:93-94` 也有）→ 不对称。

- 实测：`start_slow()` 2.6s 后 `set_stop(True)`，随后 2.6s 内仍写出 3 帧 `(0.0,50)/(100.0,50)/(0.0,50)`，全部 `force=True`。
- 影响：安全控制失效——用户按下急停后设备继续在 min/max 之间往复。
- 最小修复：`_slow_loop` 每轮开头 `if not self.ch.state.allow_move: await asyncio.sleep(0.25); continue`（与 `_orgasm_loop` 对齐）。

### A4【高】急停挡不住「预设播放」

```python
# vendor/device/preset_player.py:165-170
            if dist > 0:
                self._self_moving = True
                try:
                    await self.ch.move_to(e, speed, force=True)   # 先发
```

预设播放没有 `allow_move` 判断，且固定 `force=True`（同 A3 的通道语义）。

- 实测：先 `set_allow_move(False)`（模拟已急停）再 `select("normal")+start()`，1.5s 内仍写 2 帧 `(100.0)/(0.0)`。
- 最小修复：`/api/quick` 的 `kind=="stop" && on` 分支同时 `d["preset"].stop()`；并在 `_play_one_loop` 里检查 `ch.state.allow_move`。

### A5【高】待机缓动「一次外部动作后永久不再启动」

```python
# vendor/device/quick_moves.py:60-68
    def _on_move(self, percent, speed) -> None:
        if self._self_moving:
            return
        ...
        if self.is_slow:
            # 手机端 onAnyMove()：cancel 正在跑的缓动循环并重新计时，**缓动保持启用**
            self._gen_s += 1          # ← 同时让 _idle_watch（唯一能重启 _slow_loop 的人）失效
            self._slow_task = None
```

`_idle_watch(gen)` 的循环条件 `gen == self._gen_s`（`quick_moves.py:146`）在世代自增后必假 → 它退出后没有任何代码再创建它
（`start_slow()` 在 `is_slow` 已为真时直接 `return`，`quick_moves.py:122-124`）。于是 `is_slow=True` 但缓动永不恢复，
注释里承诺的"缓动保持启用"与实际行为相反。

- 实测：缓动运行中注入 1 次外部帧（`ch.on_move(42,100)`，等价脚本/手动/爆发帧）→ `_gen_s` 1→2、`is_slow=True`，
  随后 3.5s 内新帧数 **0**（此后也不会再有）。
- 最小修复：`_on_move` 里改为重建看门狗：`self._gen_s += 1; ...; self._spawn(self._idle_watch(self._gen_s))`（或让 `_idle_watch` 不绑世代、只由 `is_slow` 控制）。

### A6【高】「伪装设备（VorzePiston 模式）」的档案映射是反的

```python
# host_server.py:3859-3862
        if dev.get("a10_mode") is not None:
            _tp = ("vorze", "serveu")[int(dev["a10_mode"])] if int(dev["a10_mode"]) in (0, 1) else None
            if _tp:
                d["ch"]._forced_toy = next((t for t in ...TOYS if t.id == _tp), None)
```

权威映射在 `vendor/device/protocols.py:38-43`：`a10_mode: int  # ServeU = 0，VorzePiston = 1`，
`SERVEU = ToyDevice("serveu", ..., 0)`、`VORZE = ToyDevice("vorze", ..., 1)`。
`channel.set_profile()` 也正是用 `mode_override = toy.a10_mode` 保持一致的（`channel.py:295-299`）。
UI 语义见 `ui/index.html:802-804`（「伪装设备（VorzePiston 模式）」开关）与 `ui/app.js:2365`（`a10_mode: this.checked ? 1 : 0`）。

- 影响：勾上「VorzePiston 模式」→ `a10_mode=1` → 代码强制 **serveu** 档案（31bb…UUID），发的是 `S{"isA10mode":1}`；
  二者自相矛盾，连接会去订阅 Vorze 设备上不存在的特征（`CharacteristicNotFound`，与 `channel.py:132-136` 注释描述的失败模式一致）。
  反向（a10_mode=0）同样错。`_forced_toy` 粘住后本进程内一直生效（`channel.py:131`）。
- 最小修复：`_tp = ("serveu", "vorze")[int(dev["a10_mode"])]`，并补一条"档案 ↔ a10_mode"的一致性断言/单测。

### A7【高】播视频与预设播放无互斥（重复发帧/抖动）

```python
# host_server.py:2455-2464
            elif path == "/api/sync/start":
                ...
                self._json(d["sync"].start(vp))        # 不停预设、不检查 preset.playing
```
```javascript
// ui/app.js:1584
    v.addEventListener("play", function () { syncStart(vlMed.path); });
```

UI 只做了单向防护：进入预设前二次确认并 `syncStop()`（`app.js:2054-2065`）；
反向没有——预设正在跑时点视频播放，`/api/sync/start` 与预设循环同时写帧（两者都 `force=True`）。
`/api/quick` 的爆发/缓动分支会先 `d["preset"].stop()`（`host_server.py:2555-2557`），脚本同步分支不会。

- 影响：设备在 5Hz 脚本帧与预设帧之间来回跳（抖动、定位错乱），且 `_on_move` 会把缓动/看门狗一起打乱（见 A5）。
- 最小修复：`/api/sync/start` 成功时 `d["preset"].stop()`（或改为返回"预设占用中"由 UI 二次确认）；播放开始时同理停掉 preset/orgasm/slow。

### A8【高】设置里「播放视频时同步驱动设备」开关关掉不生效

```javascript
// ui/app.js:2372
    $("#setScriptSync") && $("#setScriptSync").addEventListener("change", function () { saveDev({ script_sync: this.checked }); });
```
```javascript
// ui/app.js:2389 / 2393
  loadSetDev();                                            // ← 全文件唯一一次加载 SET_DEV
  function syncEnabled() { return SET_DEV.script_sync !== false; }
```

`SET_DEV` 只在启动时由 `loadSetDev()`（`app.js:2316-2332`）填充；`saveDev()`/`pollDev()` 都不回写它，
`renderSetDev()`（`app.js:2292-2309`）也不回填 `#setScriptSync`。

- 影响：把开关**关掉**后（磁盘已存 `device.script_sync=false`），当前页面里 `syncEnabled()` 仍返回 true
  → 播放视频**照样**启动脚本同步（`app.js:2394-2400`）。用户看到的就是"关了这个联动项，它还在联动"。
  反之从 false 打开也一样无效，直到刷新页面。
- 最小修复：`saveDev` 成功回调里 `SET_DEV = Object.assign(SET_DEV, patch)`（或统一让 `renderSetDev` 从 `/api/settings` 回填该开关）。
  （本条与 task-2/R02 的设置页 id↔键矩阵重叠，归属 backend 语义侧，请 R04 合并去重。）

### A9【中】`video_link.reversed` 是死读，且同函数内被 `device.reversed` 覆盖

```python
# host_server.py:3839-3840
        d["ch"].apply_motion(range_lo=vl.get("range_min", 0), range_hi=vl.get("range_max", 100),
                             max_speed=vl.get("max_speed", 500), reversed_=bool(vl.get("reversed")))
# host_server.py:3858
        d["ch"].reversed = bool(dev.get("reversed"))     # ← 无条件覆盖上面刚设的 reversed
```

UI 的联动卡从不写 `video_link.reversed`（`app.js:1959-1987` 只写 range/idle/burst 系列），
唯一写反转到磁盘的是 `device.reversed`（`app.js:2366` → `host_server.py:2525-2526`）。
此处 `vl.get("reversed")` 之前已被探针独立记录：`review/probes/probe_device_settings.py:8-14`。

- 影响：双份真相 + 静默覆盖。任何调用方（含头显/手机端）写 `video_link.reversed=true` 都不生效；
  读代码的人会以为反转来自联动页。
- 最小修复：删掉 `apply_motion(reversed_=...)` 里的联动取值（把 `reversed` 明确归属 `device.reversed`），或反过来只认联动页并迁移旧键。

### A10【中】`device.orgasm.*` 与 `device.slow.{min,max,max_speed,link_percent}` 是死设置

```python
# host_server.py:3851-3855
        d["quick"].apply({k: v for k, v in vl_cards["orgasm"].items() if v is not None},
                         {k: v for k, v in vl_cards["slow"].items() if v is not None})
        # 设置页只管"空闲判定秒数"，单独补上，不被卡片覆盖
        if dev.get("slow", {}).get("idle_detect_seconds") is not None:
            d["quick"].slow.idle_detect_seconds = int(dev["slow"]["idle_detect_seconds"])
```
```python
# host_server.py:326-329（DEFAULT_SETTINGS）
        "orgasm": {"min_percent": 0, "max_percent": 100, "max_speed": 500, ...},
        "slow": {"min_percent": 0, "max_percent": 100, "max_speed": 100, "idle_detect_seconds": 5, ...},
```

`/api/device/settings` 仍然**照收**这两个字典（`host_server.py:2521-2524`，且 `data/integrated_settings.json` 里确实存着
`device.orgasm.*`、`device.slow.max_speed=100`），但读点只剩 `slow.idle_detect_seconds` 一个。

- 影响：写进去的值 100% 静默无效——例如外部客户端 POST `{"orgasm":{"max_speed":300}}` 返回 `ok:true`，
  实际爆发速度由 `video_link.burst_speed` 决定。
- 最小修复：二选一——要么在 `_apply_device_settings` 里把 `device.*` 当作"联动页未设置时的回退"（`v if v is not None else dev...`），
  要么让 `/api/device/settings` 拒收/提示这两个段（避免"保存成功但没生效"）。

### A11【中】预设速度：运行时值被磁盘值回灌覆盖，且滑轨值根本不落盘

```python
# host_server.py:3869-3870
        if dev.get("preset_speed"):
            d["preset"].set_speed(int(dev["preset_speed"]))
```
```javascript
// ui/app.js:2283-2287（预设速度滑轨：只发运行时命令，不写 settings）
  initSlider("vlPresetSpeed", false, function (lo, hi) {
    var v = Math.max(1, Math.round(hi));
    ...
    presetCmd("speed", { speed: v });      // → /api/preset {action:"speed"}，不落盘
  });
```

而 `_apply_device_settings()` 在**每一次** `/api/settings` POST 后都会跑（`host_server.py:2422`）。

- 影响：用户把预设速度拖到 250 → 之后任意一次设置保存（改主题、动一下联动滑轨）就被磁盘里的
  `device.preset_speed`（本机现值 180）覆盖回去；重启后又回到 180。UI 滑轨由 `pollDev` 回填（`app.js:2276-2279`）会当场跳回去。
- 最小修复：`/api/preset {action:"speed"}` 同时 `save_settings({"device": {...preset_speed}})`；或 `_apply_device_settings` 只在初始化时应用 `preset_speed`。

### A12【中】`_apply_device_settings` 中途异常 → 其余联动项静默不生效

```python
# host_server.py:3838-3841
    try:
        d["ch"].apply_motion(...)
        d["ch"].submit(d["ch"].apply_limits(), timeout=10)   # ← 10s 超时/异常即跳到 except
# host_server.py:3871-3872
    except Exception as e:
        log.warning("应用设备设置失败：%s", e)                 # ← 只写日志，HTTP 仍回 ok:true
```

`submit()` 会 `fut.result(timeout)`（`channel.py:56-58`）→ 抛 `TimeoutError`。A2 的循环锁死期间这是**必然**发生的
（实测 2s 超时立刻触发）。触发后：`apply_limits`(0x42 限位)、快捷动作卡片、`oc_mode/reversed/skip_idle/idle_threshold/preset_speed`
**全部跳过**，用户看到"设置已保存"。

- 最小修复：把 submit 失败与解析失败分开处理（`except Exception` 内继续执行后面各段，逐段 try 并收集错误），
  并在响应里带 `warnings`；`apply_limits` 改异步/去抖，不要挂在每次设置保存的同步路径上。

### A13【中】写帧失败被吞 + `_last_pos` 先更新 → 帧永久丢失

```python
# vendor/device/sync_engine.py:119-127
        pos = int(max(0, min(100, round(target))))
        if self._last_pos is not None and abs(pos - self._last_pos) < 1:
            return                          # 位置没变就别刷 BLE
        self._last_pos = pos                # ← 先记账
        try:
            await self.ch.move_to(target, int(min(speed, self.ch.max_speed)) or None, force=True)
            self.sent += 1
        except Exception:
            pass                            # ← 失败无重试、无计数、无日志
```

- 实测：写入抛错时 `_last_pos=50, sent=0`；下一次 t 仍落在同一取整位置（50）→ **不做任何重试**（attempts 仍是 1），
  设备停在该段之外，无任何可观测量（`state()["sent"]` 不加、无日志）。
- 最小修复：`except` 里回滚 `self._last_pos = None`（强制下一 tick 重发）并 `log.warning`/计数 `dropped`。

### A14【中】tick 无合并/无时间戳：BLE 卡顿后按序补发旧位置

```python
# vendor/device/sync_engine.py:84-88
    def tick(self, t_sec: float) -> None:
        """HTTP 线程调用：投递到 BLE 事件循环，不阻塞请求。"""
        if not self.active:
            return
        asyncio.run_coroutine_threadsafe(self._apply(float(t_sec)), self.loop)
```
```python
# host_server.py:2465-2468
            elif path == "/api/sync/tick":
                d = _get_device()
                d["sync"].tick(float(body.get("t") or 0))
```

每个 tick 都排一个协程，没有"只保留最新 t"的合并，也没有在 `_apply` 里比较 t 的新旧。

- 影响：BLE 一旦卡顿（连接握手 20s、A2 的空转、扫描 6s），积压的 tick 会在恢复后按序全部执行 → 设备重放已经过时的位置序列（追帧/抖动）。
  另外 `_apply` 本身没有世代号：`start()`/`load()`（`sync_engine.py:59-72`，跑在 HTTP 线程）会在 `_apply` 在途时替换 `actions`
  → 旧视频的 tick 可能用新视频的脚本发帧。
- 最小修复：`self._pending_t = t_sec`（只存最新），或在 `_apply` 里 `if t_sec < self._last_t: return`；
  给 `SyncEngine` 加 `_gen`，`start()/stop()` 自增并在 `_apply` 开头校验（照抄 `preset_player._can`）。

### A15【中】暂停/返回媒体库都不停止脚本同步，`syncTick` 也不判播放态

```javascript
// ui/app.js:1584-1586
    v.addEventListener("play", function () { syncStart(vlMed.path); });
    v.addEventListener("timeupdate", function () { syncTick(v.currentTime); });
    v.addEventListener("ended", function () { syncStop(); });
```
```javascript
// ui/app.js:2406-2412
  function syncTick(t) {
    if (!SYNC.on) return;                    // ← 只看 SYNC.on，不看 v.paused
    ...
    api("/api/sync/tick", "POST", { t: t });
  }
```

`pause`/`ended` 只发 `/api/quick {kind:"pause"}`（`app.js:1575`，服务端 `host_server.py:2562-2563` 只停做缓动/爆发），
**没有任何 `/api/sync/stop`**；`vlVBack()`（`app.js:1615-1636`）也只 `v.pause()` + `removeAttribute("src")`，
`SYNC.on` 与宿主 `SyncEngine.active` 都留在 true。

- 影响：① 暂停中拖动进度条（`vlVSeek` → 设 currentTime → 浏览器补发 `timeupdate`）会照常 `syncTick` → **暂停状态下写帧**；
  ② 离开播放页后引擎仍 armed，下一次 `timeupdate` 仍会推帧（"停止后残留"）；
  ③ 设置页「脚本同步中」会与实际不一致（`app.js:2303-2306` 读的是宿主 `sync.active`）。
- 最小修复：`pause` 分支补 `if (SYNC.on && v.ended !== true) ...`；更直接：`pause` → `syncStop()`，`play` → `syncStart()`；
  并在 `syncTick` 里加 `if (v.paused || v.seeking) return;`。

### A16【中】断开/重连的状态泄漏

```python
# vendor/device/channel.py:196-209
    async def _cleanup(self) -> None:
        ...
        self._client = None
        self._toy = None
        self._ready = False
        self.state.connected = False
        self.state.address = ""
        self.state.name = ""
        self.state.toy = ""
        self.state.info = {}
        # ← limit_min / limit_speed / allow_move / last_move / moves / recent 都不清
```
```python
# host_server.py:2493-2500（/api/device/disconnect）
                res = d["ch"].submit(d["ch"].disconnect(), timeout=20)
                d["quick"].stop_slow(); d["quick"].stop_orgasm(); d["preset"].stop(); d["sync"].stop()
                # ← d["sync"]._last_pos 没重置（只有急停→继续才 reset_last_index）
```

- 影响：① 换一台设备后设置页仍显示上一台的 `limit_min/limit_speed`；② 若断开前处于急停，重连后 `allow_move` 仍为 False
  （UI 因 A1 恒显示未急停，与实际相反）；③ 重连后第一帧会被 `abs(pos-_last_pos)<1` 抑制，直到脚本位置变化 ≥1% 才动。
- 最小修复：`_cleanup` 里一并重置 `limit_min/limit_speed/last_move/recent`；`/api/device/disconnect` 与 `connect()` 成功后
  调 `d["sync"].reset_last_index()`。

### A17【中】暂停→播放时待机缓动抢跑

```python
# vendor/device/quick_moves.py:122-131
    def start_slow(self) -> dict:
        ...
        self.is_slow = True
        self._slow_index = 0
        self._gen_s += 1
        self._spawn(self._idle_watch(self._gen_s))     # ← 没有 note_external()，空闲计时接着上一次走
```
```python
# vendor/device/quick_moves.py:143-150
        if self._last_external == 0.0:
            self._last_external = time.time()
        ...
                if time.time() - self._last_external >= secs and self._slow_task is None:
```

- 实测：`idle_detect_seconds=5`、把 `_last_external` 置为 600s 前（等价"暂停了很久再恢复"）→ `start_slow()` 后 **0.32s**
  就写出第一帧（应为 5s）。
- 影响：暂停→播放时（`/api/quick resume`，`host_server.py:2564-2565`）缓动与刚启动的脚本同步同刻抢发帧，随后被 A5 永久关掉。
- 最小修复：`start_slow()` 里调用 `self.mark_idle_ok()`（以及 `resume_for_player()` 前先 `note_external()`）。

### A18【中】`idle_link` 关掉后 UI 与设备层不一致

```javascript
// ui/app.js:1938-1943
      var iLink = !!cfg.idle_link;
      var iLo = iLink ? mainLo : g("idle_min", 0), iHi = iLink ? mainHi : g("idle_max", 100);
      ...
        dualSliderHtml("vl_idle_range", 0, 100, iLo, iHi, "运动范围", iTxt, iLink)   // iLink → data-disabled
```
```python
# vendor/device/quick_moves.py:203-208
        if self.orgasm.link_percent:
            self.orgasm.min_percent, self.orgasm.max_percent = int(self.ch.range_lo), int(self.ch.range_hi)
        ...
        if self.slow.link_percent:
            self.slow.min_percent, self.slow.max_percent = int(self.ch.range_lo), int(self.ch.range_hi)
```

勾上关联时滑轨被 `data-disabled`（`app.js:1876` 直接 return）→ **不会**写 `idle_min/idle_max`；
`apply()` 只做单向拷贝。取消关联后 UI 按 `g("idle_min",0)/g("idle_max",100)` 显示 0–100，而设备层仍是上次拷贝的主范围值。

- 最小修复：`_apply_device_settings` 在 `link_percent` 为假时把 `ch.range_lo/hi` 之外的值回填到 `settings.video_link.idle_min/max`
  （或在 `bindLink` 取消时把当前值写进 `idle_min/idle_max`/`burst_min/max`）。

### A19–A25【低】（证据一行一条）

- **A19** `host_server.py:2469-2472`：`/api/sync/delay` 只改内存 `sync.delay_ms`，`DEFAULT_SETTINGS` 无该键、`/api/settings` 不收 → 重启丢；UI 输入框 `ui/index.html:838` 形同虚设。
- **A20** `sync_engine.py:97-98,124`：`int(min(0.0, max_speed)) or None` → 0 被当成 None；`channel.py:254` `base = self.max_speed if speed is None` → t 在首/末动作点之外时以**满速上限**冲向目标（实测记录 `speed=None`）。
- **A21** `quick_moves.py:197,201`：`isinstance(getattr(self.orgasm,k), int)` 对 bool 字段恒真（`isinstance(False,int)==True`）→ `link_percent/link_speed` 被写成 `1/0`（实测 `{'link_percent':(1,'int'),'link_speed':(0,'int')}`），`state()` 对外不再是布尔。
- **A22** `host_server.py:3862`：`_forced_toy` 只在 `a10_mode is not None` 时设置，永不清除；`host_server.py:2524` 过滤 `vv is not None` → 通过 `/api/device/settings` 无法把某字段清空/回默认。
- **A23** `sync_engine.py:3-6` 文档写"mpv 由宿主的 1s 轮询带"，实际宿主侧没有任何 `sync.tick()` 调用（`grep` 只有 `host_server.py:2467` 一处，由 JS 触发）；`mpv_player.py:190-208` 的 1s 轮询只存进度 → 外部 mpv 播放完全不联动设备。
- **A24** `host_server.py:2322-2328` 用 `rsplit(".",1)[0]+".funscript"` 判存在（大小写敏感），而真正取脚本的 `sync_engine.resolve_script`（`sync_engine.py:35-42`）大小写不敏感 → 卡片不显示"脚本"角标但同步能跑。
- **A25** `host_server.py:2217-2218`：`_abs.startswith(str(_P(r).resolve()).lower())` 缺分隔符 → 根 `E:\testvideo` 也会放行 `E:\testvideo-evil\x.mp4`（仅 127.0.0.1 + 用户自选根，故为低）。

---

## B. 可疑待验证（不足以定性）

1. **速度口径不对称**：`sync_engine._apply` 走 `move_to(raw=False)`（`channel.py:258-260` 会按 `span/100` 缩放速度），
   而快捷动作/预设走 `raw=True`（不缩放）。于是同一个 UI 数字（"Units/s"）在脚本路径与动作路径上含义不同。
   与手机端 `forceMoveTo` / `forceMoveToInverted` 两个函数对应，**可能是有意**；需要真机比对同一 max_speed 下的实际速度。
2. **双重限速**：`sync_engine.py:124` 先按 `%/s` 夹 `ch.max_speed`，通道内又按跨度缩放并再次 `min(max_speed, sp)`（`channel.py:261`）。
   是否会把用户设定的上限压得比预期更低，取决于 `max_speed` 的定义口径（"设备原始速度"还是"行程单位/秒"）。
3. **暂停中 seek 是否必然触发 `timeupdate`**：A15 的第①条依赖浏览器在 paused 状态 seek 后补发 `timeupdate`（Chromium 常见，规范未强制）。
   触发不确定性只影响"暂停后仍发帧"的复现路径，代码缺陷本身（`syncTick` 不判 `paused`）成立。
4. **`_slow_loop` 忽略 `allow_move` 是否"故意的空转"**：`_orgasm_loop` 明确写了 `if not allow_move: sleep(1.0); continue`（`quick_moves.py:108-110`），
   `_slow_loop` 没有，两点行为不一致；倾向缺陷，但需产品确认"急停期间缓动是否应完全静默"。
5. **`/api/settings` 每次都下发 0x42 限位**（`host_server.py:3841`）：拖动联动滑轨（300ms 防抖，`app.js:1917-1925`）会连续下发硬件限位；
   是否有硬件副作用（写入次数/寿命）无法从代码判定。

---

## C. 逐条回答任务问题

### C1 联动项语义 / 是否到达设备 / 死设置

语义链：`ui 滑轨/勾选 → POST /api/settings {video_link:{…}} → save_settings → _apply_device_settings() → channel.apply_motion / quick.apply / sync.*`。

| settings 键 | UI 写点 | host 读点 | 到达设备？ |
|---|---|---|---|
| `video_link.range_min/max` | app.js:1961 | 3839 | ✅ `ch.range_lo/hi` + `apply_limits` 0x42 |
| `video_link.max_speed` | app.js:1965 | 3840 | ✅ `ch.max_speed`（限速上限 + 所有 move 的夹取） |
| `video_link.idle_min/max` | app.js:1970 | 3845 | ✅ 但关联开启期间不写、取消关联后不回写（A18） |
| `video_link.idle_speed` | app.js:1974 | 3846 | ✅ `slow.max_speed` |
| `video_link.idle_link` | app.js:1976 | 3846 | ✅ `slow.link_percent`（apply() 拷贝主范围） |
| `video_link.burst_min/max` | app.js:1980 | 3847 | ✅ `orgasm.min/max_percent` |
| `video_link.burst_speed` | app.js:1984 | 3848 | ✅ `orgasm.max_speed` |
| `video_link.burst_link` | app.js:1986 | 3848 | ✅ `orgasm.link_percent` |
| `video_link.burst_speed_link` | app.js:1987 | 3849 | ✅ `orgasm.link_speed` |
| `video_link.reversed` | **无 UI** | 3840 | ❌ 死读：被 3858 `device.reversed` 立即覆盖（A9） |
| `device.orgasm.*` | 无 UI（仅默认值/外部客户端） | 无读点 | ❌ 全死（A10） |
| `device.slow.min/max/max_speed/link_percent` | 无 UI | 无读点 | ❌ 全死（A10） |
| `device.slow.idle_detect_seconds` | index.html:774 | 3854 | ✅ `slow.idle_detect_seconds` |
| `device.script_sync` | index.html:835 | **host 无读点** | △ 半死：服务端 `/api/sync/start` 不校验，前端开关又滞后（A8） |
| `device.preset_speed` | 无 UI | 3869 | △ 反向生效：覆盖运行时滑轨值（A11） |
| `device.skip_idle` / `idle_threshold` | index.html:778/781 | 3863-3866 | ✅ `sync.skip_idle/idle_threshold` |
| `device.reversed` | index.html:808 | 3858 | ✅（且覆盖联动页同名字段） |
| `device.a10_mode` | index.html:804 | 3860/3867-3868 | ❌ 档案映射反了（A6） |
| `device.oc_mode` | index.html:813 | 2531/2541 | ✅ `ch.oc_mode` + `MotorMaxPower` |
| 同步延迟（无 settings 键） | index.html:838 | 2471 | △ 运行时有效、重启丢失（A19） |

**死设置汇总**：`video_link.reversed`、`device.orgasm.*`(5 项)、`device.slow.min_percent/max_percent/max_speed/link_percent`（4 项）、
`device.script_sync`（服务端不读）、同步延迟补偿（不落盘）。

### C2 双份真相

存在三组：①`video_link.reversed` vs `device.reversed`（A9，后者无条件赢）；
②`video_link.{idle,burst}_*` vs `device.{slow,orgasm}.*`（A10，前者赢、"写后者无效"）；
③预设速度：运行时 `/api/preset speed` vs 磁盘 `device.preset_speed`（A11，每次保存设置时磁盘赢）。
合并规则在 `host_server.py:3844-3855`：显式构造 `vl_cards` 并按 `v is not None` 过滤 → 不会整段丢字段，
但**读侧单源 + 写侧多源**正是"静默不生效"的来源。单位方面：`%`(0-100 位置)、`%/s`(脚本斜率，`sync_engine.py:111,118`)、
`Units/s`（UI 文案，`app.js:1936/1944/1954`）、`percent`(设备字段名 `*_percent`) 在数值上同域（都是 0-100 域），
真正的不一致在**是否按行程跨度缩放**（见 B1/B2），以及 `move_to(speed=0) → None → max_speed`（A20）。

### C3 play/pause/timeupdate 与 quick/sync 的互相打架

- 重复发帧：A7（脚本 vs 预设无互斥）、A17（缓动与脚本同刻抢发）。`/api/quick pause` 只停缓动/爆发，
  不碰 preset/sync（`host_server.py:2562-2563`），`/api/sync/start` 也不碰 preset（2455-2464）。
- 暂停后仍 tick：A15（`syncTick` 不判 `paused`；`SYNC.on`/`engine.active` 在 pause/返回时不清理）。
- 停止后残留：A15②；另外 A2 的排队协程在卡顿解除后会补发旧帧。
- `timeupdate` 5Hz（JS 侧 180ms 节流，`app.js:2406-2410`）与 A14 的追帧叠加时会明显抖动。

### C4 并发/线程安全、tick 基准、断连泄漏

- `sync_engine` 无锁无世代（A14）；`preset_player` 用 `_gen` + `_can` 做世代隔离（正确），
  但 `_spawn` 用 `call_soon_threadsafe`（`preset_player.py:72-73`）和 `quick_moves.py:56-57` 的跨线程投递是正确做法；
- tick 基准来自 `timeupdate` 的 `currentTime`（秒），宿主只做 `t*1000-delay_ms`（`sync_engine.py:95`），
  没有用单调时钟做去重/顺序校验；
- 断开/重连泄漏见 A16；`state_dict()` 会在 HTTP 线程直接改 `ChannelState`（`channel.py:64-70`，低风险的跨线程字段写入）。

---

## D. 复现方式（只读，无副作用）

假通道的 `move_to` 精确复刻 `channel.py:252-253`（`not allow_move and not force → False`），
其余按真实属性（`state.allow_move/moves`、`max_speed=500`、`range_lo/hi`、`on_move`）。

```powershell
cd E:\Development\FunScriptCast-Nexus
@'
import asyncio, sys, threading, time
sys.path.insert(0, "vendor")
from pathlib import Path
from device.preset_player import PresetPlayer
from device.quick_moves import QuickMoves
from device.sync_engine import SyncEngine
class St:
    def __init__(s): s.connected=True; s.allow_move=True; s.moves=0
class Ch:
    def __init__(s):
        s.state=St(); s.max_speed=500; s.range_lo=0.0; s.range_hi=100.0
        s.reversed=False; s.on_move=None; s.calls=[]; s.fail=False
        s._loop=asyncio.new_event_loop(); threading.Thread(target=s._loop.run_forever, daemon=True).start()
    def set_allow_move(s,a): s.state.allow_move=bool(a)
    def submit(s,coro,timeout=30.0): return asyncio.run_coroutine_threadsafe(coro,s._loop).result(timeout)
    async def move_to(s,p,speed=None,force=False,raw=False):
        if not s.state.allow_move and not force: return False
        s.calls.append((round(float(p),2),speed,force)); s.state.moves+=1
        if s.on_move: s.on_move(p,speed)
        if s.fail: raise RuntimeError("ble fail")
        return True
# ... 见正文；三组实验分别覆盖 A2/A3/A4/A5/A13/A14/A17
'@ | .\.venv\Scripts\python.exe -
```

实测输出（原文，省略无关行）：

```
PRESETS: [('normal',2,0), ('mw',6,0), ('classic',0,19), ('deep',0,13), ...]   # 24 个里 22 个 segments=[]
KEYFRAME_ONLY: ['classic','deep','tease','edge','rhythm','zipper','risingheat',...]
A) watchdog ticks during keyframe-preset play: 0   -> 0 means BLE loop starved
A) frames written while starved: 0   playing flag: True
A) watchdog ticks after stop(): 52
H) submit during preset spin -> TimeoutError after 2.05 s   (真实路由超时 10s/20s/40s)
B) slow-loop frames after 2.6s: 2
B) after one external move -> is_slow: True _gen_s: 2
B) frames during 3.5s after that single external move: 0   -> idle-ease never re-arms
C) set_stop(True) -> stop= True allow_move= False
C) frames written AFTER 急停: 3 [(0.0,50,True),(100.0,50,True),(0.0,50,True)]
I) 急停(allow_move=False)期间 预设 frames: 2 [(100.0,100),(0.0,100)]
J) first slow frame appeared after 0.32 s of start_slow (idle_detect=5s)
E1) failed write -> _last_pos= 50 sent= 0 attempts= 1
E2) next tick rounds to same pos(50) -> attempts= 1  (no retry, no error)
D) tick before first action -> (pos, speed, raw): (0.0, None, False)   # None => ch.max_speed
F) bool coercion: {'link_percent': (1,'int'), 'link_speed': (0,'int')}
```

其他静态核对命令（无副作用）：
`python -c "import sys;sys.path.insert(0,'vendor');from device.quick_moves import QuickMoves;print(hasattr(QuickMoves,'is_stop'))"` → `False`。

---

## E. 最小修复优先级建议（给 Lead 排序用）

1. A1（一行）与 A2（几行 await/展开 keyframes）——两个阻断项，改动都极小，先修。
2. A3/A4（急停全链路生效：`stop` 分支同时停 preset/slow，两处循环补 `allow_move`）。
3. A6（一个元组顺序）、A7（`/api/sync/start` 停 preset）、A8（回填 `SET_DEV`）。
4. A9–A12（配置面收口：单一真相 + 逐段容错 + 不静默）。
5. A13–A18（同步引擎世代/合并、重连重置、tick 播放态、缓动计时基准）。
