# R11 后端 / 设备层交互链审查（第 2 轮 · 对照手机端 FunScriptCast）

- 方法：以**完整交互链**为单位（用户动作 → 前端信号 → 宿主路由 → 设备层状态机 → 回程 → 界面反馈），
  逐环节判「语义是否与手机端一致」+「异常在哪被吞」。
- 真源：`E:\Development\FunScriptCast`（Kotlin/Compose）。凡"该不该/语义是什么"均给手机端 `文件:行号`。
- 判定基准（用户指定）：**预设播放中加载脚本 → 自动退出预设模式**。手机端依据：
  `ui/AppViewModel.kt:3568-3576`（播放中加载脚本 → 暂存并弹确认）、`:3542-3548`（确认后**先 `_presetPlayer.stop()`** 再加载）、
  `:2190` / `:2546`（打开内置/远程视频前 `_presetPlayer.value?.stop()`）、`:3531-3539`（进预设模式前清脚本、停爆发/缓动）。
- 第 1 轮已确认的缺陷只在第 7 节引用编号，不重报。

---

## 1. 仲裁矩阵：谁该赢 vs 现在谁赢

| 事件 | 手机端怎么做 | 本仓库现状 | 结论 |
|---|---|---|---|
| 加载脚本 / 打开视频 | 停预设（2190 / 2546 / 3546） | **不停**（2455-2464、2430-2437 无 preset.stop） | **缺 C1** |
| 开始预设播放 | 停爆发 + 停缓动 + 清脚本（3531-3539） | 只由前端停 sync；**不停爆发/缓动** | **缺 C2** |
| 开始爆发/缓动 | 停预设（3463 / 3472） | 停预设 ✓（2556） | 一致 |
| 爆发激活 | `_sync.setExternalControl(true)` 暂停脚本（3339-3344） | **脚本照发**（sync_engine 无此判据） | **缺 C3** |
| 断开设备 | 停三方 + `setStopMove(false)` 复位急停（3441-3450） | 停三方（顺序错、异常时全跳过）；**不复位急停** | 缺 C5/C8 |
| 退出/销毁 | `resetBleEngines()` 停三方（3296-3311） | **零清理** | **缺 C9** |

结论：三处「停预设」的调用点只有 `/api/device/disconnect:2498`、`/api/quick` 爆发缓动 `:2556`、
以及前端**反方向**的 `ui/app.js:2058`（进预设前停脚本）。**没有任何一处实现"脚本侧赢"**。

---

## 2. 确认的缺陷（阻断 / 高）

### C1 [高] 预设播放中加载脚本不退出预设 —— 用户新规则未实现

`host_server.py:2455-2464`
```python
elif path == "/api/sync/start":
    d = _get_device()
    vp = str(body.get("path") or "")
    if not d["ch"].state.connected: ... "设备未连接" ...
    self._json(d["sync"].start(vp))      # ← 全程不碰 d["preset"]
```
`host_server.py:2430-2437`（`/api/player/open` 同样只管播放器）

- 触发链：预设播放中 → 用户在内置播放器打开视频（`ui/app.js:1585` `play` 事件 → `syncStart()`）
  → `/api/sync/start` → `SyncEngine.active=True` → `timeupdate`（`:1586`，180ms 节流 `:2409`）→ `/api/sync/tick`
  → `sync_engine._apply` → `move_to(force=True)`；**同时** `preset_player._play_one_loop:168` 继续发自己的 `move_to`。
- 异常点：两路都走 `force=True`（闸门见 C5），同一个 `DeviceChannel` 上目标位置交错写 → 设备在脚本插值位置与预设段末位置之间抖动；
  回程 `GET /api/device/state:2353` 仍回 `preset.playing=true` → 界面按钮显示「暂停预设」（`app.js:2272`），用户看到的与设备实际行为不符。
- 最小修复：`/api/sync/start` 与 `/api/player/open` 进入即
  `if d["preset"].playing: d["preset"].stop(); stopped = True`，响应带 `preset_stopped`，前端提示「已退出预设模式」。

### C2 [高] 预设启动时不反向停爆发 / 缓动（仲裁不对称）

`host_server.py:2576-2577`
```python
elif act in ("play", "start"):
    res = p.start()                       # 手机端方法名是 start()
```
对照手机端 `AppViewModel.kt:3531-3539`：`clearLoadedScript` → `closeVideo` → `stopOrgasm()` → `stopSlow()` → `presetPlayer.start()`。

- 触发链：视频暂停（`app.js:1576` → `/api/quick {kind:"pause"}`，爆发被停）→ 用户点「播放预设」
  → 前端 `vlPresetButton`（`app.js:2055-2065`）只 `syncStop()` → `/api/preset {action:"toggle_play"}` → `p.start()`
  → 预设循环与仍在跑的爆发/缓动循环同时驱动设备。
- 异常点：反向（爆发开始 → 停预设，`:2556`）已实现，正向缺失 → 谁先谁后决定设备行为，非交换律。
- 最小修复：`play/start` 分支开头 `d["quick"].stop_orgasm(); d["quick"].stop_slow()`。

### C3 [高] 爆发/缓动接管时脚本同步不挂起

`sync_engine.py:90-96`
```python
async def _apply(self, t_sec: float) -> None:
    if not self.active or not self.actions: return
    if not self.ch.state.connected or not self.ch.state.allow_move: return
```
- 触发链：脚本联动中 → 用户点「一键爆发」→ `/api/quick {kind:"orgasm",on:true}`（`:2555-2557`，只停预设）
  → `start_orgasm()` → 爆发循环每 `(max-min)/speed` 秒发一帧；**脚本 tick 照旧**每 180ms 发一帧。
- 异常点：手机端由 `QuickMoves.isOrgasmActive` 采集 → `_sync.setExternalControl(true)`（`AppViewModel.kt:3339-3344`）
  → `SyncEngine.kt:255` 判 `externalControl` 后整轮不发帧、`lastIndex=-1` 释放后强制重发。
  本仓库没有等价判据（`SyncEngine` 无 external_control 字段，宿主路由也不通知它）→ 两路同驱，且爆发结束瞬间跳段。
- 最小修复：设备层加一个统一的 `busy_owner`（取值 sync/preset/quick）仲裁器，三处 start/stop 都改它；最小改法：
  `quick.start_orgasm/start_slow` 内 `self.ch.owner="quick"`、`sync_engine._apply` 首行 `if self.ch.owner=="quick": return`，
  停止时 `owner=None` + `reset_last_index()`。

### C4 [高] 脚本首 / 末动作点被当成"全速移动"下发（视频一开始就有一条最高速帧）

`sync_engine.py:97-100` + `:124`
```python
if ms <= acts[0][0]:
    target, speed = acts[0][1], 0.0        # ← speed=0.0
...
await self.ch.move_to(target, int(min(speed, self.ch.max_speed)) or None, force=True)
```
`channel.py:254`：`base = self.max_speed if speed is None else int(speed)` —— `0.0 or None` → `None` → 用 `max_speed`。

- 触发链：视频 `play` → 首批 `tick(t≈0)` → `ms <= acts[0][0]`（脚本首点 `at=0` 时也成立：`0<=0`）→ `speed=0.0`
  → `int(0) or None` → `move_to(target, None, force=True)` → `_scale_speed(max_speed)` → `convert_speed` 后 = 设备最高速
  → 设备**以最高速冲到脚本首位置**（可能是 0 或 100，即整个行程）。末尾 `ms >= acts[-1][0]` 同构。
- 语义对照：手机端 `SyncEngine.kt:284-289` 对 `n !in 1 until count` 只置 `reason="等待脚本开始"/"脚本已结束"` 并 `active=false`，**一帧都不发**。
- 异常点：`or None` 把"速度为 0（应保持/不发）"和"未指定（用最大速度）"两种语义合并了 —— 静默升级为最大速度，无日志无提示。
- 最小修复：头/尾分支直接 `return`（与手机端同义）；或 `_apply` 内 `if speed <= 0: return`。

### C5 [高] 急停的通道级闸门是死代码；急停态没有复位路径

`channel.py:252-253`
```python
if not self.state.allow_move and not force:
    return False
```
全部 4 个运动调用点都传 `force=True`：`host_server.py:2505`（手动 move）、`sync_engine.py:124`、`preset_player.py:168`、`quick_moves.py:113/165`
→ `channel.move_to` 的 `allow_move` 判断**从未生效**；真正生效的只有调用方自查：
`quick_moves.py:108`（爆发）、`:148`（空闲计时）；`preset_player` 与 `quick_moves._slow_loop`（`:156-171`）**完全不查**
→ 第 1 轮「急停挡不住缓动/预设」的机制即此。

- 复位链（本轮新增）：`allow_move` 只由 `quick_moves.set_stop:79-81` 改写 ← `/api/quick {kind:"stop"}`，
  该路由恒 500（第 1 轮 R1-急停）；即使修好，`channel._cleanup:196-209` 也**不复位** `allow_move`
  → 「断开 → 重连」后仍是急停态，界面上没有独立恢复入口（只有同一个按钮 toggle，`app.js:2021`）。
  手机端在两处显式复位：`AppViewModel.kt:3445`（disconnectDevices）、`:3300`（resetBleEngines）。
- 最小修复：`_cleanup()` 末尾 `self.state.allow_move = True`；把 `force` 参数收窄为"仅爆发/缓动/手动"三项豁免，
  preset/sync 传 `force=False`；`/api/device/connect` 成功后回一个 `stop_reset:true` 让前端清按钮态。

---

## 3. 确认的缺陷（中）

### C6 [中] `_orgasm_resume` / `_slow_resume` 粘滞：用户关掉的爆发会自己回来

`quick_moves.py:94-97` / `:133-138` / `:174-190`
```python
def stop_orgasm(self) -> dict:
    self.is_orgasm = False
    self._gen_o += 1
    return self.state()          # ← 不清 _orgasm_resume
```
手机端 `QuickMoves.kt:160-163`（`stopOrgasm` 首行 `orgasmResumeOnPlay = false`）、`:224-227`（`stopSlow` 同）。

- 触发链：视频暂停 → `/api/quick {kind:"pause"}` → `pause_for_player` 置 `_orgasm_resume=True` 并停循环
  → 用户点「一键爆发」把它关掉（`/api/quick {kind:"orgasm",on:false}` → `stop_orgasm()`，旗标仍在）
  → 视频继续 → `app.js:1577` `/api/quick {kind:"resume"}` → `resume_for_player` → `start_orgasm()` **自动重新开爆发**。
- 异常点：旗标是 `getattr(self, "_orgasm_resume", False)` 惰性字段（`__init__` 未初始化），生命周期无人管理。
- 最小修复：`stop_orgasm/stop_slow` 首行清旗标，`__init__` 里初始化为 `False`。

### C7 [中] 限位有两个写入源且无 `min<=max` 钳制 → 用户手动限位被静默覆盖

`channel.py:277-281`
```python
async def set_limit(self, lo: int, hi: int, speed: int) -> bool:
    info = self.state.info or {}
    if info and not info.get("supports_limit", True): return False
    return await self._write(cmd_limit(lo, hi, speed))      # ← 不写回 range_lo/hi，不钳 min<=max
```
`host_server.py:3841`（`_apply_device_settings`，任何 `/api/settings`、`/api/device/settings` 保存都会走到）：
```python
d["ch"].submit(d["ch"].apply_limits(), timeout=10)   # 用 settings 的 range_min/range_max 重发 0x42
```
- 触发链①（覆盖）：设置页/联动页点「下发限位」→ `/api/device/limit:2507-2511` → 0x42 生效
  → 用户随后改任意无关设置（例如待机秒数）→ `/api/device/settings` → `_apply_device_settings` → 限位被行程卡的值覆盖。
- 触发链②（非法区间）：`/api/device/limit` 收 `min=80,max=20` → `protocols.py:99-103 cmd_limit` 各自独立钳制 → 原样发出 `[0x42,80,20,…]`；
  手机端 `BleDeviceService.kt:748-757` 是 `min.coerceAtMost(max)`，且 `_minPercent/_maxPercent/_maxSpeed` 同时是 remap 的真源（单一真源）。
- 最小修复：`set_limit` 内 `lo, hi = min(lo,hi), max(lo,hi)` 并写回 `range_lo/range_hi`（或引入 `limit_override` 并被 `_apply_device_settings` 尊重）。

### C8 [中] `/api/device/disconnect` 顺序错误 + `submit` 超时不取消协程 → 断开失败但循环残留

`host_server.py:2493-2500`
```python
res = d["ch"].submit(d["ch"].disconnect(), timeout=20)   # 先断连；抛异常则下面 4 行全跳过
d["quick"].stop_slow(); d["quick"].stop_orgasm(); d["preset"].stop(); d["sync"].stop()
```
`channel.py:56-58`：`fut.result(timeout)` 抛 `TimeoutError` 后**协程仍在 BLE 循环里跑**（未 `cancel()`）。

- 触发链：设备在范围外 → 用户点「扫描并连接」→ `connect()` 持 `_lock`（`BleakClient(timeout=20)` + 3s 预扫 + 3s D0 重试）
  → 用户改点「断开设备」→ `disconnect()` 等锁 20s 超时 → 异常冒到 `do_POST` 兜底 `:2673` → 500
  → **四个 stop 全部未执行**（`preset.playing` 仍为真）→ 随后 connect 协程成功落定 → 设备连上、预设继续驱动。
- 界面表现：前端 `app.js:2433-2435` 只 toast「断开失败」，2 秒后的 `pollDev` 又把状态刷成已连接。
- 最小修复：断开路由先 `set_allow_move(False)` + 停三方，最后才 `submit(disconnect)`；`submit` 超时时 `fut.cancel()`。

### C9 [中] 退出 / 托盘 / 关机路径不清理设备层

`host_server.py:3560-3565`
```python
for _step in (TRAY.stop, sub_stop, dlna_stop):
    try: _step()
    except Exception: pass
os._exit(0)          # 跳过 run() 的 finally
```
`host_server.py:4132-4135`（有窗口模式）：`TRAY.stop(); sub_stop(); dlna_stop()` —— 同样没有设备层。

- 触发链：托盘「退出」→ `request_quit`（`:3539`）→ 只收三个服务 → `os._exit(0)`；预设/爆发/缓动循环、BLE 连接、在飞写帧全部不管。
- 异常点：`os._exit` 跳过 finally 是有意为之（注释说明），但设备层不在那三个 `_step` 里 → 与手机端 `resetBleEngines`（`AppViewModel.kt:3296-3311`）不对称。
- 最小修复：抽 `_device_shutdown()`（= `/api/device/disconnect` 那五行的语义 + `ch.disconnect()`），在 `request_quit._do` 与 `run()` 的 finally 各调一次。

### C10 [中] 预设速度不持久化，且被"任意一次设置保存"重置回 100

`host_server.py:3869-3870`（`_apply_device_settings`）
```python
if dev.get("preset_speed"):
    d["preset"].set_speed(int(dev["preset_speed"]))
```
`ui/app.js:2283-2287`：滑轨只发 `/api/preset {action:"speed"}` → `preset_player.set_speed:127-131`（纯内存，无持久化）。

- 触发链：用户把预设速度拖到 200（`/api/preset speed`，内存生效）→ 之后保存任意设置
  （如切换「跳过无动作」`app.js:2345`）→ `/api/settings` → `_apply_device_settings` → `dev["preset_speed"]` 仍是默认 100
  → `set_speed(100)` **静默重置**；下一轮 `pollDev`（`:2277`）把滑轨也拉回 100。重启后同样丢失。
- 对照手机端：`AppViewModel.kt:206`（`prefs.getInt("presetSpeed",100)`）、`:216`（节流落盘）。
- 补充：`settings.device.preset_speed` 在界面上**没有任何写入方**（`app.js:2290` 仅作前端默认值；
  `/api/device/settings:2531-2533` 虽接受该键，但前端从不发送），所以该键长期停在 100。
- 最小修复：`/api/preset` 的 `speed` 分支同时 `save_settings({"device": {..., "preset_speed": v}})`；`_apply_device_settings` 改为仅在设置确实变化时下发。

### C11 [中] `video_link` 无数值校验 → 负行程跨度使所有帧速度变 0（且静默）

`host_server.py:551-563`（`save_settings`）：`for k, v in patch.items(): if k not in DEFAULT_SETTINGS: continue; s[k] = v`
→ `video_link` 是整体替换、内部键零校验（默认值本来就只是 `{}`，`:313`）。
`channel.py:231-234` / `:236-242`
```python
span = self.range_hi - self.range_lo            # 可为负
return int(round(float(speed) * span / 100.0))  # 负数 → 被 max(0, min(max_speed, sp)) 压成 0
remapped = int(round(percent * span / 100.0)) + int(self.range_lo)   # 反向映射
```
- 触发链：`POST /api/settings {"video_link":{"range_min":80,"range_max":20}}` → `_apply_device_settings:3839-3841`
  → `apply_motion(80,20,…)` → `apply_limits()` 发 `[0x42,80,20,…]` → 之后所有 `move_to` 速度被压到 0（`convert_speed(0)=0`），
  界面仍显示「同步中 / 已发送」。
- 说明：界面双滑轨自身有 `hi >= lo+1` 约束（`app.js:1880-1881`），所以只有 API / 手工编辑设置文件能触发 → 定级中。
- 最小修复：`save_settings` 对数值键做类型+范围钳制（缺失即 400）；`apply_motion` 内 `if hi < lo: lo, hi = hi, lo`。

---

## 4. 低（输入校验与服务端错误码一致性）

- **C12 [低]** 类型/范围校验缺失 → 非数字一律 500 + 原始异常文本：
  `/api/sync/delay:2471` `float(body.get("ms") or 0)`、`/api/preset speed:2587` `int(...)`、
  `/api/device/move:2503` `float(...)`、`/api/player/seek:2445`。
  前端 `api()`（`ui/app.js:64-73`）不因 5xx 抛错，会把 `{"ok":false,"error":"ValueError: …"}` 原样 `toast` 给用户。
  最小修复：统一 `_num(body, key, default, lo, hi)` 校验器，失败回 400 + 中文文案。
- **C13 [低]** `bool(body.get("on"))`（`:2548`）把字符串 `"false"` 判成真；`/api/quick` 的 `kind` 白名单是 `if/elif` 链，
  且 `elif False: pass`（`:2560-2561`）是死分支残留。
- **C14 [低]** `GET /api/device/state` 每 2s 调 `state_dict()`，后者在 HTTP 线程读 `self._client.is_connected`（bleak 对象跨线程读）
  并**就地改状态**（`channel.py:60-70`）—— 读接口有副作用，且只清显示字段、不清 `_ready`/不 `_cleanup`（见 S3）。

---

## 5. 可疑待验证

- **S1 [中]** 掉线后循环不感知：`state_dict` 发现 `is_connected=False` 只把 `connected` 置假（`channel.py:65-68`），
  `_cleanup` 不被调用 → `_ready` 仍 True、预设/爆发/缓动循环继续跑（`_write` 静默 `False`）→ 用户重连后**无需任何操作预设自动续跑**。
  手机端有同样结构（`forceMoveTo` 同样只看 `_ready`），但手机端 `disconnectDevices/resetBleEngines` 是常态入口；
  本仓库需要确认 PC 上掉线是否更常见（USB/蓝牙适配器休眠）。建议在 `state_dict` 检出掉线时统一走一次"停三方"。
- **S2 [低]** `_ready` 置位时机：`channel.py:169-170` 先 `_ready=True` 再 `apply_limits()`；
  手机端是**限位写完后**才 `_ready=true`（`BleDeviceService.kt:524-530`）。两者之间只隔一个 await（`_write` 内），
  理论上存在"排队的运动帧先于 0x42 限位到达设备"的窗口；需真机复现确认。
- **S3 [低]** `state_dict()` 用 `state.connected` + `_client.is_connected` 联合判断，但**不改 `_client`/`_ready`**：
  再点「扫描并连接」时旧 `BleakClient` 未被 `disconnect()` 就被覆盖（`connect:145-147`），
  在 Windows 上可能撞 "device already connected"；需真机确认是否可复现。
- **S4 [低]** 发送节奏：手机端**每个关键帧段只发一帧**（`SyncEngine.kt:296` `if (n != lastIndex)`），
  本仓库每 ~180ms 按插值位置发一帧（`sync_engine.py:120-124`）。位置去重（`<1%` 不刷）与 180ms 节流叠加，
  在慢速段会明显低于脚本斜率 → 设备滞后/到不了峰值。需要真机对比才能定性（可能是 PC 侧有意的"连续伺服"设计）。

---

## 6. 已核实与手机端一致（避免误判，勿再报）

- `/api/device/move` 传 `force=True`（`host_server.py:2505`）**不算缺陷**：手机端手动移动同样走
  `manualMoveTo → activeChannel()?.forceMoveTo(...)`（`AppViewModel.kt:819-820`），本就豁免急停。
- 播放中 `select(id)` 换预设（`preset_player.py:76-81` 不重启循环，由 `_can` 在段边界退出）与手机端 `PresetPlayer.kt:56-59 + 122-123` 同构；
  `toggle_random`（`:103-112`）与 `PresetPlayer.kt:94-104` 同构；`_effective_speed`（`:67-70`）与 `effectiveSpeed`（`PresetPlayer.kt:116-120`）同构。
- `start/stop` 幂等、随机池排除当前、保持段（dist=0）只等不发、20ms 步进可打断：逐条与 `PresetPlayer.kt:67-85 / 126-147` 一致。
- 快进行程/限速按 span 缩放（`channel.py:231-242` vs `BleDeviceService.kt:717-725`）、`forceMoveToInverted` 只做反转（`:740-746`）一致。
- `skip_idle` 判据（slope<0.01 且段长≥阈值）与手机端 `rebuildIdleGaps`（`SyncEngine.kt:139-161`）一致；
  手机端额外会 seek 播放器跳过空闲段，PC 侧不 seek 是**有意的**（不能跳用户正在看的视频），只抑制发帧。

---

## 7. 第 1 轮结论引用（不复述）

R1-1 `q.is_stop` 不存在 → 急停 500（`host_server.py:2551`，连带 C5 的恢复链）；
R1-2 22/24 预设 `segments` 为空 → 事件循环自旋（`preset_player.py:154-173`，本轮实测 `ui/presets.json` 仅 normal(2 段)/mw(6 段) 有段）；
R1-3 急停挡不住缓动/预设（机制见 C5）；R1-4 待机缓动永久停（`quick_moves.py:60-68` 的 `_gen_s` 自增同时废掉 `_idle_watch`）；
R1-5 伪装设备映射反（`host_server.py:3860`）；R1-6 `video_link.reversed` 死读（`:3858` 覆盖 `:3840`）；
R1-7 `device.{orgasm,slow}` 死键（`_apply_device_settings:3844-3852` 已改读 `video_link` 卡片）；
R1-8 sync/preset 无互斥（本轮扩为完整矩阵，见第 1、2 节）；R1-9 `_apply_device_settings` 中途异常被吞（`:3871-3872`）；
R1-10 `DEV.sync` 未赋值（前端）。
