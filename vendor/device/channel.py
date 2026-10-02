"""BLE 设备通道（对应手机端 ble/BleDeviceService.kt 的设备侧职责）。

一个进程一份：`get_channel()`。所有 BLE 调用都在同一条后台 asyncio 事件循环里跑，
宿主 HTTP 线程通过 `submit(coro)` 投递（bleak 必须在自己创建循环的线程里用）。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from dataclasses import dataclass, field

from .protocols import (TOYS, DeviceInfo, ToyDevice, cmd_info, cmd_limit, cmd_mode,
                        cmd_move, cmd_oc_mode, kotlin_round, match_toy, parse_notify)

CHUNK = 20          # TX 分片（手机端同款）


@dataclass
class ChannelState:
    connected: bool = False
    connecting: bool = False
    address: str = ""
    name: str = ""
    toy: str = ""
    error: str = ""
    info: dict = field(default_factory=dict)
    limit_min: int = 0
    limit_speed: int = 0
    allow_move: bool = True
    last_move: tuple = (0, 0)      # (percent, speed)
    moves: int = 0
    recent: list = field(default_factory=list)   # 最近下发的位置（排查"乱跑"用）


class DeviceChannel:
    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="ble-loop", daemon=True)
        self._thread.start()
        self._client = None
        self._toy: ToyDevice | None = None
        self._lock = asyncio.Lock()
        self.state = ChannelState()
        # 运动参数（与视频联动页"设备行程与速度"卡一致；由宿主从 settings 注入）
        self.range_lo = 0.0
        self.range_hi = 100.0
        self.max_speed = 500
        self.reversed = False
        self.mode_override: int | None = None     # 伪装设备（A10 模式）
        self.oc_mode = False                      # 狂暴模式（MotorMaxPower 75/100）
        self._ready = False                       # 手机端同款：握手完成（含限位下发）前不写帧
        self._forced_toy = None                   # 伪装设备：强制使用的 GATT 档案
        self.on_move = None                       # 每次写帧回调（脚本同步/快捷动作用来判空闲）

    # ---------- 宿主线程调用 ----------
    def submit(self, coro, timeout: float = 30.0):
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout)
        except concurrent.futures.TimeoutError:
            # 超时不取消的话协程还在循环里跑：断开流程超时后"迟到的帧"会继续写下去。
            # （cancel 对已开跑的任务是请求取消，能停的都停。）
            fut.cancel()
            raise

    def state_dict(self) -> dict:
        s = self.state
        # 设备可能掉线或被别处抢走：以 bleak 客户端真实连接态为准。
        # 否则界面会一直显示"已连接"（用户反馈：明明断开了还显示"断开设备"）。
        try:
            if s.connected and (self._client is None or not getattr(self._client, "is_connected", False)):
                s.connected = False
                s.address = s.name = s.toy = ""
                s.info = {}
        except Exception:
            pass
        return {
            "connected": s.connected, "connecting": s.connecting, "address": s.address,
            "name": s.name, "toy": s.toy, "error": s.error, "info": s.info,
            "limit_min": s.limit_min, "limit_speed": s.limit_speed,
            "allow_move": s.allow_move, "moves": s.moves,
            "range": [self.range_lo, self.range_hi], "max_speed": self.max_speed,
            "reversed": self.reversed, "a10_mode": self.mode_override,
        }

    def apply_motion(self, range_lo=None, range_hi=None, max_speed=None, reversed_=None) -> None:
        if range_lo is not None:
            self.range_lo = float(range_lo)
        if range_hi is not None:
            self.range_hi = float(range_hi)
        if max_speed is not None:
            self.max_speed = int(max_speed)
        if reversed_ is not None:
            self.reversed = bool(reversed_)

    # ---------- BLE ----------
    async def scan(self, timeout: float = 10.0, stop_on_supported: bool = True) -> list[dict]:
        """扫描。**扫到受支持的设备就立刻返回**（手机端也是扫到即连，不是干等固定时长）——
        之前界面点一下要等满 6 秒，就是这里在干等。"""
        from bleak import BleakScanner
        seen: dict = {}
        hit = asyncio.Event()

        def _cb(dev, adv):  # noqa: ANN001
            toy = match_toy(dev.name)
            seen[dev.address] = {"address": dev.address, "name": dev.name or "",
                                 "rssi": getattr(adv, "rssi", 0),
                                 "supported": bool(toy), "toy": toy.id if toy else ""}
            if toy and stop_on_supported:
                hit.set()

        scanner = BleakScanner(detection_callback=_cb)
        await scanner.start()
        try:
            try:
                await asyncio.wait_for(hit.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
        finally:
            try:
                await scanner.stop()
            except Exception:
                pass
        out = list(seen.values())
        out.sort(key=lambda x: (not x["supported"], -(x["rssi"] or 0)))
        return out

    async def connect(self, address: str, toy_id: str | None = None) -> dict:
        """连接。toy_id 由界面扫描结果透传（省掉重复扫描）；没有就现场扫一次认型号。"""
        from bleak import BleakClient
        async with self._lock:
            if self.state.connected:
                return {"ok": True, "already": True}
            self.state.connecting = True
            self.state.error = ""
            try:
                toy = self._forced_toy or (next((t for t in TOYS if t.id == toy_id), None) if toy_id else None)
                # Windows BLE 必须先扫一次（把设备放进缓存），否则按地址直连常失败。
                # 同时拿到**设备当前广播的身份** —— 同一台 A10 硬件在不同模式下
                # 广播名不同（ServeU / VorzePiston），档案必须以它为准，
                # 否则会去订阅一套根本不存在的特征（CharacteristicNotFound）。
                # 有透传型号就不用预扫（界面刚扫过、设备在系统缓存里）——手机端也是秒连。
                # 只有型号未知时才扫一次认身份。档案不匹配由下面的候选重试兜底。
                advertised = None
                if toy is None:
                    for d in await self.scan(3.0):
                        if d["address"].lower() == address.lower() and d["supported"]:
                            advertised = next((t for t in TOYS if t.id == d["toy"]), None)
                            if advertised:
                                toy = advertised
                client = BleakClient(address, timeout=20.0)
                await client.connect()
                self._client = client
                self._toy = toy
                self.state.connected = True
                self.state.address = address
                self.state.name = toy.name if toy else address
                self.state.toy = toy.id if toy else ""
                cands = [t for t in (advertised, toy) if t]
                for t in TOYS:
                    if t not in cands:
                        cands.append(t)
                last_err = None
                for t in cands:
                    try:
                        self._toy = t
                        await self._start_notify(t.rx)
                        self.state.name = t.name
                        self.state.toy = t.id
                        self.state.info = {}
                        # 手机端 handshake 顺序（BleDeviceService.handshake，逐条对齐）：
                        # ① D0 读设备信息（最多 3 次、重试间隔 250ms）
                        # ② 写临时限位 0x42（applyLimits）
                        # ③ 写 A10 模式
                        # ④ **最后才置 ready**（ready 之前不写运动帧）
                        # 旧版顺序是"模式 → ready → 限位 → 读信息"：ready 早于限位，
                        # 存在"限位还没下发就允许写帧"的窗口，且与手机完全相反。
                        # 连接时**不补发**狂暴/oc（手机端连接流程里也没有；用户切换时
                        # setOCMode 才写设备，设备端自行持久化）。
                        await self._read_info_with_retry() # D0：最多 3 次、每次等 1s、间隔 250ms
                        await self.apply_limits()          # 临时限位 0x42（手机端 applyLimits）
                        await self._write(cmd_mode(self.mode_override if self.mode_override is not None else t.a10_mode))
                        self._ready = True
                        last_err = None
                        break
                    except Exception as ex:
                        last_err = ex
                        try:
                            if self._client:
                                await self._client.stop_notify(t.rx)
                        except Exception:
                            pass
                if last_err is not None:
                    raise last_err
                return {"ok": True, "state": self.state_dict()}
            except Exception as ex:
                self.state.error = f"{type(ex).__name__}: {ex}"
                await self._cleanup()
                return {"ok": False, "error": self.state.error}
            finally:
                self.state.connecting = False

    async def disconnect(self) -> dict:
        async with self._lock:
            await self._cleanup()
            return {"ok": True, "state": self.state_dict()}

    async def _cleanup(self) -> None:
        try:
            if self._client is not None and getattr(self._client, "is_connected", False):
                await self._client.disconnect()
        except Exception:
            pass
        self._client = None
        self._toy = None
        self._ready = False
        # 会话边界复位（手机端 AppViewModel 断开时显式复位同款）：重连后算"新设备"，
        # 急停态/限位记忆不能跨连接残留 —— 否则"连上了但设备不动"还没处查。
        self.state.allow_move = True
        self.state.limit_min = 0
        self.state.limit_speed = 0
        self.state.recent = []
        self.state.connected = False
        self.state.address = ""
        self.state.name = ""
        self.state.toy = ""
        self.state.info = {}

    async def _start_notify(self, toy: ToyDevice) -> None:
        def _cb(_sender, data: bytearray) -> None:
            kind, val = parse_notify(bytes(data))
            if kind == "info" and isinstance(val, DeviceInfo):
                self.state.info = val.as_dict()
            elif kind == "limit_min":
                self.state.limit_min = int(val)
            elif kind == "limit_speed":
                self.state.limit_speed = int(val)
        await self._client.start_notify(toy.rx, _cb)

    async def _write(self, payload: bytes) -> bool:
        if not (self._client and getattr(self._client, "is_connected", False)) or self._toy is None:
            return False
        # 握手（模式/限位/信息）没走完就不写运动帧 —— 手机端 forceMoveTo 同样要求 _ready
        for i in range(0, len(payload), CHUNK):
            await self._client.write_gatt_char(self._toy.tx, payload[i:i + CHUNK], response=False)
        return True

    # ---------- 运动 ----------
    def _scale_speed(self, speed: float) -> int:
        """手机端 forceMoveTo：scaledSpeed = Math.round(speed * span/100)——速度按行程跨度缩放。"""
        span = self.range_hi - self.range_lo
        return kotlin_round(float(speed) * span / 100.0)

    def _remap(self, percent: float) -> int:
        """行程范围重映射（对应手机端 moveTo：先按 range 映射，再按需反转）。"""
        span = self.range_hi - self.range_lo
        remapped = kotlin_round(float(percent) * span / 100.0) + int(self.range_lo)
        # 手机端：target = if (invert) 100 - remapped else remapped
        target = (100 - remapped) if self.reversed else remapped
        return int(max(0, min(100, target)))

    def _invert(self, percent: float) -> int:
        v = 100.0 - float(percent) if self.reversed else float(percent)
        return int(max(0, min(100, kotlin_round(v))))

    async def move_to(self, percent: float, speed: int | None = None, raw: bool = False,
                      *, bypass_estop: bool = False) -> bool:
        """percent 0..100。

        raw=True（爆发/缓动，手机端 forceMoveToInverted）：只加反转，**不重映射、
        不按设备速度上限夹紧**——这两条循环的间隔按发出去的速度计算（行程÷速度），
        帧速度一旦被夹到上限以下，设备走不完就被下一帧叫回，行程被截短且随抖动
        漂移（用户报"飘忽"；手机端 forceMoveToInverted 明确 no clamp）。
        raw=False（脚本/预设/手动，手机端 forceMoveTo）：重映射进行程范围、速度按
        跨度缩放，并夹紧到设备速度上限（手机端 moveTo 同款）。

        急停（allow_move=False）时一律拒写；只有急停的复位路径允许 bypass_estop，
        四个运动写点（脚本/预设/爆发/缓动）和手动移动都**不传**——
        旧版的 force=True 同时背"跳过重映射/跳过急停/强制写"三种含义，把这道闸门吃成了死代码。"""
        if not self.state.allow_move and not bypass_estop:
            return False
        # speed=None 才表示"用速度上限"（手动移动的旧语义）；显式给 0 就是 0
        # （手机端 moveTo: clamp(scaledSpeed, 0, maxSpeed)，慢段爬行靠它）
        base = self.max_speed if speed is None else max(0, int(speed))
        if raw:
            target = self._invert(percent)          # forceMoveToInverted：只加反转
            sp = base                              # 不 remap、不夹紧（速度上限只管硬件限位帧）
        else:
            target = self._remap(percent)           # forceMoveTo
            sp = max(0, min(self.max_speed, self._scale_speed(base)))   # 缩放后夹紧
        if not self._ready:
            return False
        ok = await self._write(cmd_move(target, sp))
        if ok:
            self.state.last_move = (percent, sp)
            self.state.moves += 1
            self.state.recent = (self.state.recent + [int(target)])[-12:]
            cb = self.on_move
            if cb:
                try:
                    cb(percent, sp)
                except Exception:
                    pass
        return ok

    async def set_limit(self, lo: int, hi: int, speed: int) -> bool:
        info = self.state.info or {}
        if info and not info.get("supports_limit", True):
            return False
        return await self._write(cmd_limit(lo, hi, speed))

    async def set_oc_mode(self, enabled: bool) -> bool:
        """狂暴模式：MotorMaxPower 100（开）/ 75（关）。"""
        self.oc_mode = bool(enabled)
        return await self._write(cmd_oc_mode(self.oc_mode))

    async def set_profile(self, toy_id: str) -> dict:
        """伪装设备：切换**整套 GATT 档案**（UUID + A10 模式），重订阅通知并重读设备信息。

        手机端「伪装设备（VorzePiston 模式）」就是这个语义：同一套 A10 硬件在
        不同固件下广播身份不同，只发一条 S 指令是换不过来的 —— 必须换 UUID。
        未连接时记下来，下次连接直接用该档案。
        """
        toy = next((t for t in TOYS if t.id == toy_id), None)
        if toy is None:
            return {"ok": False, "error": "未知设备档案"}
        self.mode_override = toy.a10_mode
        self._forced_toy = toy
        if not self.state.connected or self._client is None:
            return {"ok": True, "pending": True, "toy": toy.id}
        try:
            if self._toy and self._toy.id != toy.id:
                try:
                    await self._client.stop_notify(self._toy.rx)
                except Exception:
                    pass
            self._toy = toy
            await self._start_notify(toy)
            await self._write(cmd_mode(toy.a10_mode))
            if self.oc_mode:
                await self._write(cmd_oc_mode(True))
            self.state.info = {}
            await self._write(cmd_info())
            self.state.name = toy.name
            self.state.toy = toy.id
            return {"ok": True, "toy": toy.id}
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

    INFO_RETRIES = 3
    INFO_WAIT_S = 1.0
    INFO_GAP_S = 0.25    # 手机端 handshake 重试间隔 delay(250)

    async def _read_info_with_retry(self) -> None:
        """D0 设备信息：对齐手机端 handshake——最多 3 次、每次等 1 秒、重试间隔 250ms
        （注释原文：部分设备刚连上时 D0 响应慢，避免设置页读不到硬件/固件版本）。"""
        for attempt in range(1, self.INFO_RETRIES + 1):
            if self.state.info:
                return
            await self._write(cmd_info())
            steps = int(self.INFO_WAIT_S / 0.1)
            for _ in range(steps):
                await asyncio.sleep(0.1)
                if self.state.info or not self.state.connected:
                    break
            if self.state.info or not self.state.connected:
                return
            if attempt < self.INFO_RETRIES:
                await asyncio.sleep(self.INFO_GAP_S)

    async def apply_limits(self) -> bool:
        """下发临时限位 [0x42, min, max, speedHi, speedLo]（手机端 applyLimits）。
        行程范围/速度上限改一次就发一次；硬件 <150 会被 _write 之外的上层忽略（同手机端）。"""
        info = self.state.info or {}
        if info and not info.get("supports_limit", True):
            return False
        return await self._write(cmd_limit(int(self.range_lo), int(self.range_hi), int(self.max_speed)))

    async def refresh_info(self) -> dict:
        """设备信息「刷新」：重发 D0。"""
        ok = await self._write(cmd_info())
        return {"ok": bool(ok)}

    async def set_mode(self, a10_mode: int) -> bool:
        self.mode_override = 1 if a10_mode else 0
        return await self._write(cmd_mode(self.mode_override))

    def set_allow_move(self, allow: bool) -> None:
        self.state.allow_move = bool(allow)


_CHANNEL: DeviceChannel | None = None
_CH_LOCK = threading.Lock()


def get_channel() -> DeviceChannel:
    global _CHANNEL
    with _CH_LOCK:
        if _CHANNEL is None:
            _CHANNEL = DeviceChannel()
        return _CHANNEL
