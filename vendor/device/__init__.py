"""设备通道（M2）：BLE 玩具 + 预设播放 + 快捷动作。

移植自手机端 E:\\Development\\FunScriptCast：
  ble/DeviceProtocols.kt  → protocols.py
  ble/BleDeviceService.kt → channel.py
  sync/QuickMoves.kt      → quick_moves.py
  sync/PresetPlayer.kt    → preset_player.py
"""

from .protocols import TOYS, DeviceInfo, ToyDevice, convert_speed  # noqa: F401
from .channel import DeviceChannel, get_channel  # noqa: F401
