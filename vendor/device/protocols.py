"""设备 GATT 标识与线协议（逐条对应手机端 ble/DeviceProtocols.kt）。

设备档案（名称 → service / TX / RX）：
  - ServeU      31bb1111 / 31bb2222 / 31bb3333
  - VorzePiston 40ee1111 / 40ee2222 / 40ee3333

TX 命令（write-without-response，按 20 字节分片）：
  - 文本 "D0"                          设备信息请求
  - 文本 'S{"isA10mode": 0|1}'         ServeU / VorzePiston 模式切换
  - 二进制 [0x42, min, max, speedHi, speedLo]   设置临时限位（硬件 >= 150）
  - 二进制 [0x01, percent, convertSpeed(speed)] 移动到位置

RX 通知：
  - [0x42, 1, uint16BE]  当前 minMovePercent
  - [0x42, 2, uint16BE]  当前 maxMoveSpeed
  - 'S' + 10..11 字节    设备信息：uint16BE hw/sw 版本、maxPos/100、minPos/100、maxSpeed、motorPower
"""
from __future__ import annotations

from dataclasses import dataclass

SERVEU_SERVICE = "31bb1111-33e3-4f3c-a7fb-104288e7cb77"
SERVEU_TX = "31bb2222-33e3-4f3c-a7fb-104288e7cb77"
SERVEU_RX = "31bb3333-33e3-4f3c-a7fb-104288e7cb77"

VORZE_SERVICE = "40ee1111-63ec-4b7f-8ce7-712efd55b90e"
VORZE_TX = "40ee2222-63ec-4b7f-8ce7-712efd55b90e"
VORZE_RX = "40ee3333-63ec-4b7f-8ce7-712efd55b90e"


@dataclass(frozen=True)
class ToyDevice:
    id: str
    name: str
    service: str
    tx: str
    rx: str
    a10_mode: int          # ServeU = 0，VorzePiston = 1


SERVEU = ToyDevice("serveu", "ServeU", SERVEU_SERVICE, SERVEU_TX, SERVEU_RX, 0)
VORZE = ToyDevice("vorze", "VorzePiston", VORZE_SERVICE, VORZE_TX, VORZE_RX, 1)
TOYS = (SERVEU, VORZE)


def match_toy(name: str | None) -> ToyDevice | None:
    """扫描时按广播名匹配（手机端同款：名称包含设备名）。"""
    if not name:
        return None
    low = name.lower()
    for t in TOYS:
        if t.name.lower() in low:
            return t
    return None


@dataclass
class DeviceInfo:
    hardware_version: int = 0
    software_version: int = 0
    max_position: int = 100
    min_position: int = 0
    max_speed: int = 350
    motor_power: int = 0

    @property
    def supports_temporary_limit(self) -> bool:
        return self.hardware_version == 0 or self.hardware_version >= 150

    def as_dict(self) -> dict:
        return {
            "hardware": self.hardware_version, "software": self.software_version,
            "max_pos": self.max_position, "min_pos": self.min_position,
            "max_speed": self.max_speed, "motor_power": self.motor_power,
            "supports_limit": self.supports_temporary_limit,
        }


def convert_speed(v: int) -> int:
    """速度压缩（上线格式）：<=50 → /2；<=750 → (v-50)/4+25；<=2000 → (v-750)/25+200；>2000 → 250。"""
    v = int(v)
    if v <= 50:
        return max(0, v // 2)
    if v <= 750:
        return (v - 50) // 4 + 25
    if v <= 2000:
        return (v - 750) // 25 + 200
    return 250


def cmd_info() -> bytes:
    return b"D0"


def cmd_mode(a10_mode: int) -> bytes:
    return ('S{"isA10mode": %d}' % (1 if a10_mode else 0)).encode("ascii")


def cmd_limit(min_percent: int, max_percent: int, speed: int) -> bytes:
    m = max(0, min(100, int(min_percent)))
    x = max(0, min(100, int(max_percent)))
    s = max(0, min(65535, int(speed)))
    return bytes([0x42, m, x, (s >> 8) & 0xFF, s & 0xFF])


def cmd_move(percent: int, speed: int) -> bytes:
    p = max(0, min(100, int(round(percent))))
    return bytes([0x01, p, max(0, min(255, convert_speed(speed)))])


def parse_notify(data: bytes):
    """通知解析 → (kind, value)。kind ∈ {'limit_min','limit_speed','info',None}。"""
    b = bytes(data)
    if len(b) >= 4 and b[0] == 0x42:
        val = (b[2] << 8) | b[3]
        if b[1] == 1:
            return "limit_min", val
        if b[1] == 2:
            return "limit_speed", val
        return None, val
    if b[:1] == b"S" and len(b) >= 11:
        def u16(i: int) -> int:
            return (b[i] << 8) | b[i + 1]
        info = DeviceInfo(
            hardware_version=u16(1), software_version=u16(3),
            max_position=u16(5) // 100, min_position=u16(7) // 100,
            max_speed=u16(9), motor_power=b[11] if len(b) > 11 else 0,
        )
        return "info", info
    return None, None


def cmd_oc_mode(enabled: bool) -> bytes:
    """狂暴模式（手机端 buildOCMode 同款文本指令）：默认 MotorMaxPower=75，开启=100。"""
    return ('S{"MotorMaxPower":%d}' % (100 if enabled else 75)).encode("ascii")
