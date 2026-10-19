"""与 Kotlin 实现的一致性断言（数值手工按 Kotlin 逻辑推得）。"""
import math

from device import build_a10_mode, build_move_frame, convert_speed
from funscript import Funscript, Keyframe, kround

# ---- convertSpeed 分段边界（Kotlin: v/2 | (v-50)/4+25 | (v-750)/25+200 | 250）----
assert convert_speed(0) == 0
assert convert_speed(50) == 25          # 50/2
assert convert_speed(51) == 25          # (1)/4+25 = 25 (整除)
assert convert_speed(100) == 37         # 50/4+25 = 12+25
assert convert_speed(750) == 200        # 700/4+25 = 175+25
assert convert_speed(751) == 200        # 1/25+200 = 200
assert convert_speed(2000) == 250       # 1250/25+200 = 50+200
assert convert_speed(2001) == 250

# ---- move 帧字节 ----
assert build_move_frame(55, 100) == bytes([0x01, 55, 37])
assert build_move_frame(150, 99999) == bytes([0x01, 100, 250])   # percent 夹取 + speed 饱和
assert build_move_frame(-5, 10) == bytes([0x01, 0, 5])

# ---- A10 模式帧 ----
assert build_a10_mode(0) == b'S{"isA10mode": 0}'
assert build_a10_mode(1) == b'S{"isA10mode": 1}'

# ---- kround 对齐 Java Math.round（.5 向上）----
assert kround(0.5) == 1 and kround(1.5) == 2 and kround(2.4) == 2

# ---- 解析: at/1000 秒, pos/100 夹取, NaN 拦截, 排序 ----
fs = Funscript.from_json('{"actions":[{"at":2000,"pos":150},{"at":1000,"pos":0},{"at":500,"pos":50},{"at":3000,"pos":5}]}')
kfs = fs.keyframes()
assert [k.position for k in kfs] == [0.5, 1.0, 2.0, 3.0]
assert kfs[2].value == 1.0              # pos 150 -> 1.0

# ---- searchForIndexAfter 边界 ----
assert fs.index_after(0.0) == 0         # 首帧之前
assert fs.index_after(0.5) == 1         # 等于某帧 -> 该帧属于前段
assert fs.index_after(0.51) == 1
assert fs.index_after(99) == fs.count   # 结束之后

# ---- computeMove: speed = |Δv|*100/dt, 目标取后一帧 ----
fs2 = Funscript([Keyframe(0.0, 0.0), Keyframe(2.0, 1.0)])
assert fs2.compute_move(1.0) == (100, 50)      # |1-0|*100/2 = 50
assert fs2.compute_move(-1) is None            # 脚本未开始
assert fs2.compute_move(2.0) is None           # 已结束
fs3 = Funscript([Keyframe(0.0, 0.2), Keyframe(0.5, 0.7)])
assert fs3.compute_move(0.25, 0.0, 2.0) == (70, 200)  # speed=100, ×2 倍速=200

print("全部一致性断言通过 ✓")
