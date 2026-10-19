# R14 补审：上一轮未覆盖区域（player / channel / protocols / 预设契约 / 头显契约）

审查员：untouched（第 2 轮）　日期：本轮　范围：只读产品代码，未启动 GUI/服务器/App。

对照基准：`E:\Development\FunScriptCast\app\src\main\java\com\funscriptcast\`（手机端权威实现）、
`E:\Development\VRFunScriptCast\funscriptcore\src\main\java\com\funscriptcast\engine\AiSubtitleEngine.kt`（头显端权威实现）。

已排除（第 1 轮已报，本轮不重复）：A2（22/24 预设空 segments 导致事件循环空转锁死）、A23（mpv 不联动）、
R03 1.8（`/api/player/open` 只校验扩展名）、R03 1.9/1.10（libmpv 双份 121MB）、R03 1.11（8791 无鉴权）。

---

## 一、确认的缺陷

### 【高】H1 预设播放绕过急停：`force=True` 跳过 `allow_move` 闸门（安全相关）

`vendor/device/preset_player.py:168`
```python
            if dist > 0:
                self._self_moving = True
                try:
                    await self.ch.move_to(e, speed, force=True)   # 先发
```
`vendor/device/channel.py:252-253`
```python
        if not self.state.allow_move and not force:
            return False
```

手机端：`sync/PresetPlayer.kt:149-152` → `ble.serveu.moveTo(pos, speed)`；`ble/BleDeviceService.kt:711-714`
`fun moveTo(...) { if (!allowMove) return false; return forceMoveTo(...) }` —— **预设播放受急停约束**。

结论：PC 端 `preset_player` 传 `force=True`，`set_stop(True)`（只切 `allow_move`，`quick_moves.py:80`）之后
预设循环仍每段写帧，急停按钮在预设播放期间**不生效**。手机端不会这样。
最小修复：`preset_player.py:168` 去掉 `force=True`（`raw` 本来就是 False，语义不变），或在
`_play_one_loop` 循环头加 `if not self.ch.state.allow_move: return`。

### 【高】H2 卡片契约字段不存在：`_aggregate` 从不产出 `paths`，两处消费方恒空

`vendor/player/library.py:250-266`（卡片字面量，只有 `parts`，无 `paths`）
```python
            card = {
                "id": hashlib.md5(f"{d}|{base}".encode("utf-8")).hexdigest()[:16],
                ...
                "parts": [{"path": p["path"], "name": p["name"], ...
```
消费方 1：`host_server.py:2323-2332`
```python
                                    with lib.lock:
                                        card = next((c for c in lib._cards
                                                     if e2.path in c.get("paths", [])), None)
                                    vids.append({
                                        ...
                                        "dur": (card["duration"] if card else 0.0),
                                        "pos": (card["progress"]["pos"] if card and card.get("progress") else 0.0),
```
消费方 2：`library.py:99-105`
```python
            for c in self._cards:
                if path in c.get("paths", []):
                    c["last_played"] = played_at
                    c["progress"] = {"path": path, ...}
                    for p in c["parts"]:
```
全仓 `grep '"paths"'` 仅 3 处命中：上面两处是**读**，第三处 `host_server.py:3744`
`return {"ok": True, "path": paths[0], "paths": paths}` 是另一个接口（视频路径列表）的写出，
与卡片字典无关 ⇒ 卡片字典的 `paths` **零处写入**，两处读取恒不命中。

结论：(1) `/api/library/browse` 的每条视频 `dur`/`pos` 恒为 0.0（浏览列表没有时长与观看进度）；
(2) `set_progress` 对内存 `_cards` 的更新是**死代码** —— `/api/library/items` 的卡片进度与
"最近播放"排序要等下一次全量扫描（`_aggregate` 从索引 `played_at` 重算）才刷新。
最小修复：卡片加一行 `"paths": [p["path"] for p in parts],`。

### 【中】M1 `submit()` 超时后协程继续跑并继续写帧（已实测）

`vendor/device/channel.py:56-58`
```python
    def submit(self, coro, timeout: float = 30.0):
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout)
```
实测（本机 Python 3.10.0，转述输出）：
```
raised: TimeoutError | fut.cancelled= False
writes after caller gave up = 6 | cancelled= False | done= True
```
`run_coroutine_threadsafe` 的 `concurrent.futures.Future` 超时只是**调用方放弃等待**，
不取消 loop 里的 task。

结论：`/api/device/move|limit|mode|refresh|connect|disconnect`（`host_server.py:2492-2518`）在
`TimeoutError` 后走到全局兜底 `host_server.py:2673-2674` 返回 500，但协程仍在事件循环里排队执行，
**迟到的运动帧照样落到设备上**（"点了没反应，几秒后设备自己动了"）；异常也被 future 吞掉
（"Task exception was never retrieved"）。没有残留协程对象泄漏，但副作用不受控、无上界。
最小修复：`try: return fut.result(timeout) except TimeoutError: fut.cancel(); raise`（`fut.cancel()`
会经 `call_soon_threadsafe` 取消 loop 内 task）。

### 【中】M2 扫描读改写跨越锁外：`set_progress` 的进度会被扫描结果覆盖

`vendor/player/library.py:122-124` 与 `173-176`
```python
    def _scan(self, roots: list[str]) -> dict:
        t0 = time.time()
        idx = self._load_index()          # ← 无锁读
        ...
        self._save_index(idx)             # ← 无锁写（整份覆盖）
        cards = self._aggregate(found, idx)
```
`_scan` 只在 `rescan()`（110-120）拿锁改 `scanning` 标志，真正的读与写在锁外；`set_progress`
（93-98）全程持锁做"读→改→整份写"。扫描是分钟级（每文件最多 80×0.25s libmpv 探测）。

结论：扫描期间播放并落盘的 `pos`（`_on_progress` 每 5s 一次，`mpv_player.py:204-206`）会在扫描结束时
被扫描线程用**扫描开始时**的快照覆盖 → 观看进度回退。最小修复：保存前重新读盘并合并
`pos/dur/played_at/thumb`（键同时存在的取磁盘值），或把 `_scan` 的 load…save 段纳入锁。

### 【中】M3 `/api/library/browse` 每个视频整份重读索引文件

`vendor/player/library.py:86-89`
```python
    def thumb_name_for(self, path: str) -> str:
        """索引里该视频的缩略图文件名（无则空串）。浏览列表轻量查询用。"""
        e = self._load_index().get("files", {}).get(path) or {}
        return e.get("thumb", "")
```
调用点 `host_server.py:2314-2331`（`os.scandir` 每命中一个视频调一次，且在 `lib.lock` 之外）。

结论：一个目录 N 个视频 = N 次 `read_text + json.loads` 整份索引；本机 `data/library_index.json`
目前 2116 B / 8 条，但库按设计是"全盘媒体库"，几千文件时单次浏览请求会做几千次全量 JSON 解析，
UI 媒体库列表直接卡住。无锁本身不致损坏（`_save_index` 用 `os.replace` 原子替换）。
最小修复：`browse` 分支开头 `idx = lib._load_index()` 一次，把 dict 传进去（或给 `Library`
加 `thumb_map()` 一次性返回 `{path: thumb}`）。

### 【中】M4 同步引擎在脚本区间外发"满速帧"，手机端此时不发帧

`vendor/device/sync_engine.py:97-101` 与 `123-124`
```python
        if ms <= acts[0][0]:
            target, speed = acts[0][1], 0.0
        elif ms >= acts[-1][0]:
            target, speed = acts[-1][1], 0.0
        ...
            await self.ch.move_to(target, int(min(speed, self.ch.max_speed)) or None, force=True)
```
`int(0) or None` → `None` → `channel.move_to` 的 `base = self.max_speed`（默认 500）。
手机端 `data/Funscript.kt:46` `if (n < 1 || n > count - 1) return null` —— 区间外返回 null，
`SyncEngine.kt:298` 直接不发帧，`reason` 置"等待脚本开始/脚本已结束"。

结论：PC 端在视频开头（t < 第一个动作点）与结尾各发一帧**按设备最高速**的位移，
手机端完全不发；开头一帧是可见的冲击，且状态里没有"脚本已结束/未开始"的表达。
最小修复：把区间外两种情况改成 `return`（与手机一致），`speed or None` 改为显式判断。

### 【中】M5 预设"循环 Ns"标签不随速度变化（与手机口径不一致）

`ui/app.js:2209-2211`
```javascript
      grid.innerHTML = VL_PRESETS.map(function (pr) {
        var loopSec = presetLoopSec(pr);
        var label = "循环 " + (loopSec >= 100 ? Math.round(loopSec) + "s" : loopSec.toFixed(1) + "s");
```
`ui/app.js:2116`：`return travel / 100.0;   // 速度 100 时的秒数（默认配速）`
手机端 `ui/Screens.kt:3962-3964`：`presetLoopLabel(def, speed)` —— 标签用**当前速度**
（`PresetDefs.kt:73-74 loopDurationMs(speed) = loopTravel*1000/speed`）。

结论：位移口径（travel = Σ|end-start|）两边一致，实测 24 条全部吻合
（classic 3.6s / takeit 10.6s / premiumwave 20.6s = 手机 `loopDurationMs(100)`）；
但 PC 标签把速度写死成 100 且只在网格重建时算一次 —— 预设速度滑轨改到 200、BOOST=500
（实际循环快 2~5 倍）标签仍显示原值，手机端此时已经跟着变。
最小修复：`presetLoopSec(pr, spd)`，调用处传 `DEV.preset.boost ? 500 : (DEV.preset.speed || 100)`，
并在 `pollDev` 检测到速度变化时重渲染标签。

### 【低】L1 `presetPoints` 的段式时间累加表达式永不算出预期值

`ui/app.js:2101`
```javascript
        t += sg[2] || 0 || (Math.abs(sg[1] - sg[0]) * 1000 / Math.max(1, sg[2] || 1));
```
手机端权威式 `ui/Screens.kt:4090`
```kotlin
                t += (s.durationMs ?: (abs(s.end - s.start) * 1000f / s.speed.coerceAtLeast(1))).toFloat()
```
`sg[2]` 恒为 100（真值）⇒ `sg[2] || 0 || …` 恒取 `sg[2]`，**每段只加 100ms**；`durationMs`（`sg[3]`）
也被丢掉，而宿主 `preset_player.py:160` `dur = sg[3] if len(sg) > 3 else None` 是认第 4 元素的
—— 一旦 `presets.json` 补上 durationMs，两端解释立刻分叉。
现状影响：该分支只服务 `normal`（唯一非 felt 的段式预设），波形按 loopX 归一化绘制，视觉无差；
x 单位（200ms）与标签（2.0s）不一致。
最小修复：`t += sg[3] || (Math.abs(sg[1]-sg[0]) * 1000 / Math.max(1, sg[2] || 1));`

### 【低】L2 注释声称的 `_ready` 闸门不在 `_write` 里

`vendor/device/channel.py:225-228`
```python
        # 握手（模式/限位/信息）没走完就不写运动帧 —— 手机端 forceMoveTo 同样要求 _ready
        for i in range(0, len(payload), CHUNK):
            await self._client.write_gatt_char(self._toy.tx, payload[i:i + CHUNK], response=False)
```
真正的闸门在 `move_to`（262 `if not self._ready: return False`）。`_write` 自身不查，于是
`set_limit`/`apply_limits`/`cmd_info`/`set_profile` 都能在未 ready 时写（这是**有意**的：
握手本身要用它）。结论：注释与实现位置不符，易被后续维护者误信为统一闸门。
最小修复：把注释移到 `move_to` 的 `_ready` 判断处，`_write` 处改为"仅底层写，不做 ready 判定"。

### 【低】L3 `state_dict()` 在 HTTP 线程改共享状态，且 `_cleanup` 不重置限位

`vendor/device/channel.py:64-70`
```python
        try:
            if s.connected and (self._client is None or not getattr(self._client, "is_connected", False)):
                s.connected = False
                s.address = s.name = s.toy = ""
                s.info = {}
```
同时 `_cleanup`（196-209）重置了 connected/address/name/toy/info，但**不重置**
`limit_min/limit_speed/last_move/moves/recent`（27-32）。结论：UI 每 2s 轮询
（`ui/app.js:2427`）触发的状态修正与 loop 线程的写入无锁竞争（单字段赋值原子，实际风险低）；
断开/重连后 `/api/device/state` 仍会回上一台设备的限位值（`limit_min/limit_speed`）。
最小修复：`_cleanup` 里一并清零限位与 `last_move`。

### 【低】L4 `_ALLOW_HEADERS` 是死代码，且列的是错的头名

`host_server.py:3004-3006`
```python
    # 头显端 OkHttp 不需要 CORS（不是浏览器）；去掉 ACAO * 是安全收紧——
    _ALLOW_HEADERS = ("Content-Type", "Authorization")
```
全仓 `grep _ALLOW_HEADERS` 仅此一处；`do_OPTIONS`（3079-3080）只回 `{"ok": True}`，不发任何
CORS 头。真实客户端要带的是 `X-FSC-Subtitle-Token`（手机端 `data/AiSubtitleEngine.kt:341-344`
经 `authedRequest` 发给 8756 与 8791）。最小修复：删掉该常量，或按实际头名补齐并真的在
`do_OPTIONS` 里回 `Access-Control-Allow-Headers`。

### 【低】L5 头显缓存桩的保留理由对**当前**客户端已过期

`host_server.py:2993-2995`
```python
    字幕缓存功能已移除（Round 53），但**已发布的头显 APK 仍会调这两个接口**——
    路由保留成空壳，让旧版头显拿到 hit:false 走正常识别路径、存档失败被它
```
头显端现状：`AiSubtitleEngine.kt:401-403` 明确"客户端查询/保存路径已一并删除，**不存在**
'恒未命中的兼容桩'"；全仓 grep 头显端已无 `/api/subtitle/cache` 调用。结论：两条路由对当前
头显/手机端都是零调用（仅对历史 APK 有意义），注释需标注"仅历史 APK"。保留无害，
最小修复：注释补一句"当前客户端已不再调用，仅为 ≤v1.6.x 旧 APK 保留"。

### 【低】L6 "契约 B：启动响应带档位建议"只有一个客户端在吃

`host_server.py:3067-3070`
```python
                # 契约 B：启动响应带档位建议（数值，秒；与 /api/headset/status、
                # 8756 /transcribe 同一口径），客户端据此自适应音频分块时长
                res["recommended_chunk_sec"] = headset_status()["recommended_chunk_sec"]
```
头显端 `AiSubtitleEngine.kt:519-522` 只判 `isSuccessful`，**不解析响应体**；手机端
`data/AiSubtitleEngine.kt:379-392` 才读该字段。两边 `GET /api/headset/status`
（头显 `:462`/`:537`）都读了 `recommended_chunk_sec`，所以功能没坏，只是"启动响应"这条通道
对头显是空转。最小修复：把注释改成"手机端消费；头显端走 /api/headset/status"。

### 【低】L7 DLL 缺失时 `ensure_dll()` 不再重试

`vendor/player/mpv_player.py:32-39`
```python
    d = str(DLL_DIR)
    if (DLL_DIR / "libmpv-2.dll").is_file():
        try:
            os.add_dll_directory(d)
        except Exception:
            pass
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
    _imported = True
```
`_imported = True` 无条件置位：DLL 不在时第一次调用把状态标记成"已处理"，之后即使用户
放进 `vendor\mpv\libmpv-2.dll` 也不会再 `add_dll_directory`（只能重启进程）。本仓库当前靠
`vendor/player/libmpv-2.dll` 兜底（R03 1.9/1.10 已报），故实际可跑。
最小修复：仅在真正加进搜索路径后置位，或把 `_imported` 拆成"已尝试/已就绪"两个标志。

### 【低】L8 `_get_library()` / `_get_player()` 懒加载无锁

`host_server.py:3912-3920`（`_get_player` 3896-3909 同构）
```python
def _get_library():
    global LIB
    if LIB is None:
        sys.path.insert(0, str(APP_DIR / "vendor" / "player"))
        from library import Library   # noqa: PLC0415
        LIB = Library(APP_DIR / "data" / "library_index.json",
```
两个接口同时冷启动（UI 首屏 `/api/library/items` + `/api/library/browse`）可各建一个实例：
全局 `LIB` 只留最后一个，先建的那个仍被在途请求持有（其 `_cards` 为空、`scanning` 标志互不可见），
且两者会并发写同一个索引文件。对比 `_get_device()`（3877）是带锁的。
最小修复：加一把 `threading.Lock()`（`_get_device` 同款）。

---

## 二、可疑待验证

### S1 `cmd_limit` 不规范化 min≤max，手机端在服务层做了

`vendor/device/protocols.py:99-103`
```python
def cmd_limit(min_percent: int, max_percent: int, speed: int) -> bytes:
    m = max(0, min(100, int(min_percent)))
    x = max(0, min(100, int(max_percent)))
```
手机端 `ble/BleDeviceService.kt:748-753`
```kotlin
        val max = maxPercent.coerceIn(0, 100)
        val min = minPercent.coerceIn(0, 100).coerceAtMost(max)
```
PC 侧 `apply_limits`（channel.py:339-345）直接发 `int(range_lo)/int(range_hi)`，
`_remap`（236-242）用 `span = range_hi - range_lo`。未验证 UI 是否可能提交
`range_min > range_max`（设置页两条滑轨的联动校验未审）：若能，则 0x42 帧的 min>max、
`_remap` 的 span<0 会把位置算成负值再被 clamp 到 0（设备贴底）。
建议：`cmd_limit` 内 `lo, hi = min(m,x), max(m,x)`，或在 `apply_motion` 入口规范化。

### S2 `sync_engine.tick` 无合并/无背压

`vendor/device/sync_engine.py:84-88`
```python
        if not self.active:
            return
        asyncio.run_coroutine_threadsafe(self._apply(float(t_sec)), self.loop)
```
UI 侧 180ms 节流（`ui/app.js:2406-2412`）≈5.5 次/秒，每次都排一个协程；`_apply` 里的
`move_to` 要等 `write_gatt_char` 完成（`channel.py:226-227`）。若 GATT 写堆积，协程按 FIFO
累积 → 位置指令滞后（不会乱序）。真实延迟需上机验证。
建议：`_apply` 入口做"仅保留最后一个 tick"的合并（如 `self._pending = t` + 单飞行标志）。

### S3 待机缓动循环运行中不检查急停

`vendor/device/quick_moves.py:156-171`：`_slow_loop` 循环体内只判 `is_slow/gen`，
`allow_move` 只在 `_idle_watch`（148）里判。急停后已在跑的缓动会继续每 1s 写帧直到
`stop_slow()`。该项属 `quick_moves.py`（不在我这轮的任务范围），需上机确认手机端
`QuickMoves.kt` 的对应语义。建议同 H1 处理。

---

## 三、五个问题的直接回答

**1. `sync_engine.py:4` 声称"mpv 由宿主的 1s 轮询带"—— 不属实。**
全仓 `tick` 只有一个调用点 `host_server.py:2465-2468`（`POST /api/sync/tick`），
唯一发起者是内置 `<video>` 的 `timeupdate`（`ui/app.js:1585`）。`mpv_player._poll_loop`
（190-208）只读 `core_idle` 并每 5s 存进度，`_get_player()`（3908）只传 `on_progress`
（存 `LIB.set_progress`），没有 `sync` 句柄。外部 mpv 播放时**没有任何 tick 进同步引擎**，
设备完全不动。附带事实：UI 里已无 `/api/player/open` 调用方（仅 `host_server.py:2430` 提供），
外挂 mpv 现在是"只有 API 用户能走"的路径（第 1 轮 A23 已报此结论，此处补全证据链）。

**2. `submit()` 超时后的协程：残留且继续写帧；`recent` 有界。**
实测（Python 3.10.0）：`fut.result(0.5)` 抛 `TimeoutError` 后 `cancelled=False`，1.6s 后
协程已完成的 6 次写全部发生 → 迟到帧照落设备、异常被 future 吞（M1）。
`state.recent` **不会无界增长**：`channel.py:268` `(self.state.recent + [int(target)])[-12:]`
是唯一赋值点，恒 ≤12 个 int。`state.moves` 是无界计数器但只是 int（无内存问题）。
没有协程对象泄漏，泄漏的是"不可取消的副作用"。

**3. `lib._cards` 直读与 `lib.lock` 边界。**
`_cards` 的 3 个读取点全部持 `lib.lock`（`host_server.py:2323-2325`、`2344-2345`、`2359-2360`），
写入点 `library.py:175-176` 也持锁，类型又是 RLock（注释 54-56 说明过 Lock 死锁史）→ **保护到位**。
没保护到的是：`thumb_name_for`（88）整份读索引文件且无锁（M3）、`_scan` 的
load→save 跨越锁外导致丢更新（M2）、`scanning/scan_progress/last_scan` 在 2347/2361-2364
无锁读（标量，无害）。索引文件的并发安全靠 `_save_index` 的 `os.replace` 原子替换兜住，
不会读到半截 JSON。

**4. `mpv.py`（92910 B）的危险调用：没有。**
`grep 'eval\(|exec\(|shell=True|os\.system|subprocess|pickle|__import__'` → 仅 2 处命中，都是
`ctypes.find_library` 失败时的报错文案（56-60）。`command()`（1246-1260）走 `_make_node_str_list`
→ `_mpv_command_node` 的**节点数组**接口，不存在字符串命令拼接注入；`seek_rel`
（`mpv_player.py:107`）传的是 float 且全仓无调用方。异常处理一致：`MpvPlayer` 每个属性写入都
独立 try/except，`terminate` 的 `UserWarning` 分支（mpv.py:1164-1169）有实际保护意义。
唯一可议的是 `MpvPlayer.open` 在持锁状态等待 seek 就绪最长 15s（75-81，60×0.25s），期间
`state()`/`/api/player/state` 被阻塞；不构成缺陷（线程化服务：`ThreadingHTTPServer`）。

**5. 预设数据契约与"为什么 22/24 只有 keyframes"。**
逐条程序化比对：`ui/presets.json` 与 `PresetDefs.kt` 的**源字段完全一致**——24/24 id 相同，
`keyframes`（[pos, atMs] 二元组）、`segments`（[start, end, speed] 三元组）、
`previewFrom/previewTo` 全部逐值相等，0 处差异。
手机端权威定义的关键是**派生字段**：
`PresetDefs.kt:66-67`
```kotlin
    val playSegments: List<PresetSegment> =
        if (segments.isNotEmpty()) segments else toSegments(keyframes)
```
`toSegments`（29-43）把相邻关键帧无损转成段，`speed = dist*1000/durMs`、`durationMs = 时间差`；
`PresetPlayer.kt:126-133` 播放的是 `def.playSegments`（不是 keyframes，也不是原始 segments）。
**根因**：宿主只移植了"播放端"（`preset_player.py:160` 甚至保留了 `sg[3]` = durationMs 的读取），
**漏掉了 `toSegments` 这一步派生**，于是 `for sg in (pr.get("segments") or [])`（155）对 22 个
预设拿到空列表 → 一个循环 0 秒、无写帧（第 1 轮 A2 的锁死空转是它的次生现象）。
`segments` 格式 = `[start, end, speed]`（host 额外容忍第 4 位 `durationMs`，今天 JSON 里没有）。
时长口径：`presetLoopSec` = Σ|end-start|/100 = 手机 `loopDurationMs(100)`，24 条实测全等
（classic 3.6s、takeit 10.6s、premiumwave 20.6s）；**但**PC 标签不随速度变（M5），
且对这 22 个预设而言标签承诺的循环时长与宿主实际下发的 0 秒位移直接矛盾（H2/A2 同源不同面）。
`presetPoints` 总体是 `ui/Screens.kt:4068-4097` 的忠实移植（felt 分支用累计行程、
非 felt 波形用源时间轴，与手机 4065-4066 的说明一致），仅 2101 行的时间累加写坏（L1）。

**6. 头显 8791 契约：两个"活"路由一一对应，两个缓存路由已是空壳。**
| 头显端调用处 | 宿主机路由 | 字段 |
|---|---|---|
| `AiSubtitleEngine.kt:462`（`probeServerReady`） | `GET /api/headset/status`（host 3041-3042） | 读 `ready`、`recommended_chunk_sec` ✓（host 1395/1405） |
| `AiSubtitleEngine.kt:537`（`holdAndWait` 轮询） | 同上 | 读 `status`("ready"/"error")、`error`、`recommended_chunk_sec` ✓（host 1396/1398/1405） |
| `AiSubtitleEngine.kt:519`（`POST /api/subtitle/start`，空体、8s 超时） | `host 3064-3070` | 只判 `isSuccessful`，响应体不解析（L6） |
| 无调用（`:401-403` 已删） | `GET /api/subtitle/cache`、`POST /api/subtitle/cache/save`（host 3043-3046、3071-3073） | 死路由（L5） |
`hostPort = 8791` 的推导（头显 `229-236`）与宿主 `LAN_API_PORT`（host 112/4024）一致；
8791 只白名单 4 条路由（host 3047-3048/3074-3075 兜底 403），与 `_ALLOW_HEADERS` 无关（L4）。
额外发现：**手机端 `FunScriptCast/app/.../data/AiSubtitleEngine.kt:158/323/377/411` 是 8791 的
第二个客户端**，按 `X-FSC-Subtitle-Token` 带鉴权头（该头只被 8756 的 `/transcribe` 校验，
`vendor/subtitle/server_app.py:401-421`；8791 侧不校验也不转发），并额外消费 start 响应里的
`recommended_chunk_sec`（`:379-392`）。两端字段口径一致，无失配。

---

## 四、核对过的"非缺陷"（负结果，避免后续重复排查）

- `protocols.convert_speed`（79-88）与手机 `convertSpeed`（`DeviceProtocols.kt:75-80`）**公式等价**
  （含负数截断行为）；`cmd_move` 的 0..100 clamp 与 `buildMoveFrame`（103-107）一致。
- `channel._remap`/`_scale_speed`/`_invert` 与手机 `forceMoveTo`（`BleDeviceService.kt:717-725`）、
  `forceMoveToInverted`（740-746）**语义一致**（raw 分支多一次 min(max_speed) 收紧，方向安全）。
- 协议常量（UUID、`0x42` 限位帧、`S{"isA10mode":0|1}`、`S{"MotorMaxPower":75|100}`）与手机逐字段相同。
- `get_channel()`（360-369）**是线程安全的**（`_CH_LOCK`），且是唯一构造点；
  `asyncio.new_event_loop()` + `run_forever` 线程 + `run_coroutine_threadsafe` /
  `call_soon_threadsafe` 的跨线程用法正确（`preset_player.py:72-73`、`quick_moves.py:56-57`）。
- `state.recent` 有界（≤12）；`ChannelState` 的 `field(default_factory=list)` 不会跨实例共享。
- 设备**无自动重连**：`channel.py` 未注册 bleak 的 `disconnected_callback`，掉线只靠
  `state_dict()`（65）在 UI 2s 轮询时惰性修正；`connect()`（126）直接读 `state.connected`，
  若无人轮询则可能"以为还连着"而拒绝重连。手上没有"承诺自动重连"的文档，故只作提示，不计缺陷。
- `/api/library/thumb`（2365-2372）的 `[0-9a-f]{16}\.jpg` 白名单与 `thumb_name_for` 的
  `md5[:16]` 命名一致，无路径穿越。

---

### 建议修复顺序（本轮新增项）
1. H1（一行，安全：预设期间的急停闸门）
2. H2（一行，恢复浏览时长/进度与卡片实时进度）
3. M2 + M3（媒体库并发与性能，同一处文件）
4. M1（`submit` 超时取消）
5. M4 / M5 / L1（同步与 UI 口径）
