"""ServeU / VorzePiston BLE 驱动 —— 协议逐字节对齐 DeviceProtocols.kt。

线上协议（write-without-response，20 字节分块——本协议帧均 <= 20 字节）:
  - 文本 "D0"                     -> 请求设备信息
  - 文本 'S{"isA10mode": 0|1}'    -> 模式切换（ServeU=0, VorzePiston=1）
  - [0x42, min, max, speedHi, speedLo] -> 临时行程限制（hw >= 150）
  - [0x01, percent, convertSpeed(speed)] -> 移动到指定位置

速度压缩（与 Kotlin convertSpeed 完全一致）:
  v <= 50   -> v/2                -> 0..25
  v <= 750  -> (v-50)/4 + 25      -> 25..200
  v <= 2000 -> (v-750)/25 + 200   -> 200..250
  v > 2000  -> 250
"""
from __future__ import annotations

import asyncio
import struct

from bleak import BleakScanner, BleakClient, BleakError

PROFILES = {
    "serveu": {
        "name": "ServeU",
        "service": "31bb1111-33e3-4f3c-a7fb-104288e7cb77",
        "tx": "31bb2222-33e3-4f3c-a7fb-104288e7cb77",
        "rx": "31bb3333-33e3-4f3c-a7fb-104288e7cb77",
        "a10": 0,
    },
    "vorze": {
        "name": "VorzePiston",
        "service": "40ee1111-63ec-4b7f-8ce7-712efd55b90e",
        "tx": "40ee2222-63ec-4b7f-8ce7-712efd55b90e",
        "rx": "40ee3333-63ec-4b7f-8ce7-712efd55b90e",
        "a10": 1,
    },
}


def convert_speed(v: int) -> int:
    if v <= 50:
        return v // 2
    if v <= 750:
        return (v - 50) // 4 + 25
    if v <= 2000:
        return (v - 750) // 25 + 200
    return 250


def build_move_frame(percent: int, speed: int) -> bytes:
    return bytes([0x01, max(0, min(100, percent)), convert_speed(speed)])


def build_a10_mode(mode: int) -> bytes:
    return f'S{{"isA10mode": {mode}}}'.encode("utf-8")


def build_set_temporary_limit(min_percent: int, max_percent: int, max_speed: int) -> bytes:
    return bytes([0x42, min_percent, max_percent, (max_speed >> 8) & 0xFF, max_speed & 0xFF])


def parse_device_info(data: bytes) -> dict:
    """'S' + 11 字节：hw/sw uint16BE、maxPos/100、minPos/100、maxSpeed uint16BE、motor uint8。

    （实机样本 53 00ac 03b1 2710 0000 01f4 64 -> hw=172 sw=945 maxPos=100 minPos=0 maxSpeed=500 motor=100）
    """
    body = data[1:]
    try:
        hw, sw, max_pos, min_pos, max_speed = struct.unpack(">HHHHH", body[:10])
        motor = body[10]
        return {
            "hw": hw, "sw": sw,
            "max_pos": max_pos / 100.0, "min_pos": min_pos / 100.0,
            "max_speed": max_speed, "motor_power": motor,
        }
    except struct.error:
        return {"raw": data.hex()}


class SimDevice:
    """无硬件回环：打印等价输出，校验帧生成与同步时序。"""

    def __init__(self, label: str = "sim"):
        self.label = label
        self.connected = True
        self.allow_move = True
        self.min_percent = 0
        self.max_percent = 100
        self.invert = False
        self.max_speed = 500
        self.write_failures = 0
        self.last_error = ""
        self.frames_sent = 0
        self.last = None

    async def connect(self) -> bool:
        print(f"[{self.label}] 模拟设备已连接")
        return True

    async def disconnect(self) -> None:
        pass

    async def reconnect(self) -> bool:
        return True

    async def move_to(self, percent: int, speed: int) -> bool:
        if not self.allow_move or not self.connected:
            return False
        span = self.max_percent - self.min_percent
        remapped = round(percent * span / 100.0) + self.min_percent
        target = 100 - remapped if self.invert else remapped
        s = min(max(round(speed * span / 100.0), 0), self.max_speed)
        self.frames_sent += 1
        self.last = (target, s)
        return True

    async def request_info(self) -> None:
        print(f"[{self.label}] (模拟设备无 info 应答)")


class ToyDevice:
    """bleak 直连驱动。move_to 与 Kotlin BleDeviceService.forceMoveTo 同构：
    行程重映射 -> 速度按行程缩放并夹取 -> 反转 -> 写移动帧。"""

    def __init__(self, profile_key: str, address: str | None = None,
                 min_percent: int = 0, max_percent: int = 100,
                 invert: bool = False, max_speed: int | None = None):
        self.p = PROFILES[profile_key]
        self.profile_key = profile_key
        self.address = address  # 指定则跳过扫描; 首次扫描后回存供重连复用
        self.client: BleakClient | None = None
        self.connected = False
        self.allow_move = True
        self.min_percent = min_percent
        self.max_percent = max_percent
        self.invert = invert
        # None = 跟随设备上报值(通常 500); 给了就取 min(用户值, 上报值)
        self._user_max_speed = max_speed
        self.max_speed = max_speed if max_speed else 500
        self.write_failures = 0
        self.last_error = ""
        self.frames_sent = 0
        self.device_info: dict | None = None
        self._notify_lines: list[str] = []

    # ---- 通知 ----
    def _on_notify(self, _sender, data: bytearray) -> None:
        if data and data[0] == 0x42 and len(data) >= 4:
            kind = data[1]
            value = int.from_bytes(data[2:4], "big")
            if kind == 1:
                self._notify_lines.append(f"设备上报 minMovePercent={value}")
            elif kind == 2:
                self._notify_lines.append(f"设备上报 maxMoveSpeed={value}")
        elif data and data[0:1] == b"S":
            self.device_info = parse_device_info(bytes(data))
            self._notify_lines.append(f"设备信息: {self.device_info}")
            di = self.device_info
            if "max_speed" in di:
                reported = di["max_speed"]
                self.max_speed = (min(self._user_max_speed, reported)
                                  if self._user_max_speed else reported)

    # ---- 连接 ----
    async def connect(self, scan_seconds: float = 8.0) -> bool:
        address = self.address
        if address is None:
            print(f"扫描 {self.p['name']} ...")
            devices = await BleakScanner.discover(timeout=scan_seconds)
            match = next((d for d in devices if d.name == self.p["name"]), None)
            if match is None:
                names = sorted({d.name for d in devices if d.name})
                print(f"未找到 {self.p['name']}。附近的 BLE 设备: {names[:15]}")
                return False
            address = match.address
            self.address = address  # 回存: 断线重连免扫描
            print(f"找到 {self.p['name']} @ {address}")
        self.client = BleakClient(address)
        await self.client.connect()
        service = self.client.services.get_service(self.p["service"])
        tx = service.get_characteristic(self.p["tx"])
        rx = service.get_characteristic(self.p["rx"])
        await self.client.start_notify(rx, self._on_notify)
        await self.client.write_gatt_char(tx, build_a10_mode(self.p["a10"]), response=False)
        await self.client.write_gatt_char(tx, b"D0", response=False)  # 请求设备信息
        await asyncio.sleep(0.5)  # 等 info 应答
        for line in self._notify_lines:
            print(f"  {line}")
        # 固件侧行程/限速与软件夹取对齐(hw >= 150 支持, 实机 172)
        di = self.device_info or {}
        if (di.get("hw", 0) == 0 or di.get("hw", 0) >= 150) and "max_speed" in di:
            await self.client.write_gatt_char(
                tx, build_set_temporary_limit(
                    self.min_percent, self.max_percent, self.max_speed),
                response=False)
            self._notify_lines.append(
                f"已下发临时限制: 行程 {self.min_percent}-{self.max_percent}%, "
                f"限速 {self.max_speed} %/s")
        self.connected = True
        return True

    async def reconnect(self) -> bool:
        """写失败掉线后的重连: 沿用已知地址免扫描。"""
        if self.connected:
            return True
        try:
            await self.disconnect()
        except Exception:
            pass
        try:
            return await self.connect()
        except (BleakError, asyncio.TimeoutError, OSError) as e:
            self.connected = False
            self.last_error = repr(e)
            return False

    async def disconnect(self) -> None:
        if self.client and self.client.is_connected:
            await self.client.disconnect()
        self.connected = False

    # ---- 运动 ----
    async def move_to(self, percent: int, speed: int) -> bool:
        if not self.allow_move or not self.connected:
            return False
        span = self.max_percent - self.min_percent
        remapped = round(percent * span / 100.0) + self.min_percent
        target = 100 - remapped if self.invert else remapped
        s = min(max(round(speed * span / 100.0), 0), self.max_speed)
        # BLE 写零容错曾是整机退出点: 外设缓冲满/干扰/掉线抛 BleakError 时
        # 不能让它穿出主循环, 记失败并标记断线, 由上层按发送失败语义处理
        try:
            tx = self.client.services.get_service(self.p["service"]).get_characteristic(self.p["tx"])
            await self.client.write_gatt_char(tx, build_move_frame(target, s), response=False)
        except (BleakError, asyncio.TimeoutError, OSError) as e:
            self.write_failures += 1
            self.connected = False
            print(f"[{self.p['name']}] BLE 写失败(#{self.write_failures}): "
                  f"{e!r} -> 已标记断线, 停止发送")
            return False
        self.frames_sent += 1
        return True
