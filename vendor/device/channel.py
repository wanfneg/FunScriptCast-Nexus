"""BLE 设备通道（对应手机端 ble/BleDeviceService.kt 的设备侧职责）。

一个进程一份：`get_channel()`。所有 BLE 调用都在同一条后台 asyncio 事件循环里跑，
宿主 HTTP 线程通过 `submit(coro)` 投递（bleak 必须在自己创建循环的线程里用）。
"""
from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field

from .protocols import (TOYS, DeviceInfo, ToyDevice, cmd_info, cmd_limit, cmd_mode,
                        cmd_move, cmd_oc_mode, match_toy, parse_notify)

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
        self.on_move = None                       # 每次写帧回调（脚本同步/快捷动作用来判空闲）

    # ---------- 宿主线程调用 ----------
    def submit(self, coro, timeout: float = 30.0):
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout)

    def state_dict(self) -> dict:
        s = self.state
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
    async def scan(self, timeout: float = 6.0) -> list[dict]:
        from bleak import BleakScanner
        found = await BleakScanner.discover(timeout=timeout, return_adv=True)
        out = []
        for addr, (dev, adv) in found.items():
            toy = match_toy(dev.name)
            out.append({"address": addr, "name": dev.name or "", "rssi": getattr(adv, "rssi", 0),
                        "supported": bool(toy), "toy": toy.id if toy else ""})
        out.sort(key=lambda x: (not x["supported"], -(x["rssi"] or 0)))
        return out

    async def connect(self, address: str) -> dict:
        from bleak import BleakClient
        async with self._lock:
            if self.state.connected:
                return {"ok": True, "already": True}
            self.state.connecting = True
            self.state.error = ""
            try:
                # 先从已扫描到的广播名认出玩具类型；认不出就按地址连上后再匹配
                toy = None
                try:
                    found = await self.scan(4.0)
                    for d in found:
                        if d["address"].lower() == address.lower() and d["supported"]:
                            toy = next(t for t in TOYS if t.id == d["toy"])
                            break
                except Exception:
                    pass
                client = BleakClient(address, timeout=20.0)
                await client.connect()
                self._client = client
                self._toy = toy
                self.state.connected = True
                self.state.address = address
                self.state.name = toy.name if toy else address
                self.state.toy = toy.id if toy else ""
                if toy:
                    await self._start_notify(toy)
                    await self._write(cmd_mode(self.mode_override if self.mode_override is not None else toy.a10_mode))
                    if self.oc_mode:                       # 狂暴模式是设备侧状态，重连要补发
                        await self._write(cmd_oc_mode(True))
                    await self._write(cmd_info())
                return {"ok": True, "state": self.state_dict()}
            except Exception as e:
                self.state.error = f"{type(e).__name__}: {e}"
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
        for i in range(0, len(payload), CHUNK):
            await self._client.write_gatt_char(self._toy.tx, payload[i:i + CHUNK], response=False)
        return True

    # ---------- 运动 ----------
    def _remap(self, percent: float) -> int:
        """行程范围重映射（对应手机端 moveTo：先按 range 映射，再按需反转）。"""
        lo, hi = self.range_lo, self.range_hi
        if hi <= lo:
            return int(max(0, min(100, round(percent))))
        v = lo + (hi - lo) * (percent / 100.0)
        if self.reversed:
            v = self.range_lo + self.range_hi - v
        return int(max(0, min(100, round(v))))

    def _invert(self, percent: float) -> int:
        v = 100.0 - float(percent) if self.reversed else float(percent)
        return int(max(0, min(100, round(v))))

    async def move_to(self, percent: float, speed: int | None = None, force: bool = False,
                      raw: bool = False) -> bool:
        """percent 0..100。raw=True 表示**跳过行程重映射**（只保留反转）——
        快捷动作/预设播放用手机端 forceMoveToInverted 的语义。"""
        if not self.state.allow_move and not force:
            return False
        sp = self.max_speed if speed is None else int(speed)
        sp = max(0, min(self.max_speed, sp))
        target = self._invert(percent) if raw else self._remap(percent)
        ok = await self._write(cmd_move(target, sp))
        if ok:
            self.state.last_move = (percent, sp)
            self.state.moves += 1
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
