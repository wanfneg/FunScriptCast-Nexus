# proto1 — PC 直连玩具快速验证原型

验证整条链路：`funscript → 播放时钟 → 同步计算 → BLE 直驱玩具`（不依赖手机、不依赖 Intiface）。
同步算法与协议逐行对齐 FunScriptCast 的 `Funscript.kt` / `SyncEngine.kt` / `DeviceProtocols.kt`。

## 运行

```bash
# 生成测试脚本（120 秒，含慢/中/快节奏 + 60~75s 无动作段）
python make_test_script.py

# 模拟设备（无玩具验证前半段链路）
python main.py --device sim --script demo.funscript

# 真机（VorzePiston；ServeU 换 --device serveu）
python main.py --device vorze --script demo.funscript
# 已知地址可跳过扫描: --address 78:1C:3C:04:76:2A

# 常用参数
#   --invert               反转方向
#   --min 10 --max 90      行程限制（%）
#   --delay-ms 200         脚本偏移（正=滞后）
#   --rate 1.5             倍速
```

按键：`空格` 播放/暂停 · `a`/`d` seek ±5s · `x` 急停 · `q` 退出

## 一致性测试

```bash
python test_parity.py   # convertSpeed 分段/帧字节/解析/二分边界/computeMove 全断言
```

## 与 Kotlin 端的对应关系

| 本原型 | FunScriptCast | 说明 |
|---|---|---|
| `funscript.py` | `data/Funscript.kt` | 解析/at 毫秒→秒/pos 夹取/二分/按段 (pos, speed) |
| `device.py convert_speed` | `ble/DeviceProtocols.kt` | 线上速度压缩，分段完全一致 |
| `device.py ToyDevice` | `ble/BleDeviceService` | 行程重映射→速度缩放夹取→反转→写帧 |
| `main.py` 主循环 | `sync/SyncEngine.kt` | 20ms 循环、事件驱动（段变化才发）、暂停/急停复位 lastIndex |
| `clock.py` | SyncEngine 媒体时钟 | 原型为键盘模拟；生产替换为"播放器上报锚定"实现 |

## 已验证 / 待验证

- ✅ 协议帧与同步计算与 Kotlin 端逐字节/逐值一致（test_parity.py）
- ✅ 本机 bleak/WinRT BLE 栈可用（扫描发现 VorzePiston）
- ✅ 无头模拟：20ms 循环、事件驱动帧率符合脚本节奏
- ✅ 真机联调通过（2026-10-01，VorzePiston 实机跟随 demo 脚本运动正常）
- ✅ 实测设备信息格式：'S'+11 字节 = hw/sw uint16BE、maxPos/100、minPos/100、maxSpeed uint16BE、motor uint8

## 下一步（生产化路径）

1. `clock.py` 换成真实媒体时钟（libmpv time-pos / DeoVR TCP / SSE 远程时钟）
2. 设备层并排两个后端：本原型直驱（自家玩具）+ Intiface WebSocket（750+ 生态设备）
3. 位置源并排三路：funscript 文件 / AI 识别 track 流（GoblinScript 栈集成）/ Nexus 远程位置流
