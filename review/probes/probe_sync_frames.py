# -*- coding: utf-8 -*-
"""端到端验证：脚本同步"段一帧"机制的实际帧序列（对比旧机制的顿挫根因）。

旧机制（已废弃）：每 ~250ms tick 发一次**当前位置**（timeupdate 节流 180ms），
且"位移 <1% 不发"——设备每帧只被喂一小步、到位就停等下一帧 → 一顿一顿；
慢段尤甚（<1%/s 的段几十拍才发一帧）、快段段边界迟发最多 250ms+、倍速无补偿。

新机制（手机同构）：25-40ms tick 只做段边界检测；**每段恰好一帧**，
帧内容 = 段末位置 + 段斜率×播放倍速，整段行程交给设备固件插值。

本探针：40ms 步进驱动真实 SyncEngine（假通道记录帧），断言：
  · 帧数 == 段数；
  · 每帧目标 == 段末位置；
  · 每次发帧时刻与段边界理论时刻之差 ≤ 45ms（旧机制为 0~250ms+ 抖动）。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, ".")
sys.path.insert(0, "vendor")

from device.channel import ChannelState          # noqa: E402
from device.sync_engine import SyncEngine        # noqa: E402


class FakeChannel:
    def __init__(self):
        self.state = ChannelState()
        self.state.connected = True
        self.calls = []            # (tick 时刻秒, 目标, 速度)

    async def move_to(self, percent, speed=None, raw=False, *, bypass_estop=False):
        self.calls.append((round(self._now, 3), float(percent),
                           None if speed is None else int(speed)))
        return True


async def main():
    # 一个"真实形状"的脚本：动作点间隔 200-500ms 不等（模拟常见 funscript）
    acts = [[0, 10], [300, 80], [600, 30], [1100, 95], [1400, 20], [1900, 70], [2400, 10]]
    ch = FakeChannel()
    sync = SyncEngine(ch, asyncio.get_running_loop())
    sync.actions = [[float(a), float(p)] for a, p in acts]
    sync._times = [a[0] for a in sync.actions]
    sync.active = True

    ch._now = 0.0
    t = 0.0
    while t < 2.6:
        ch._now = t
        await sync._apply(t, 1.0)      # 40ms 步进（模拟前端高频 tick）
        t += 0.04

    # 期望：每段一帧（6 段），目标=段末，发帧时刻≈段起点（≤45ms 迟到）
    print(f"帧数: {len(ch.calls)}（应 = 段数 {len(acts)-1}）")
    ok = True
    for i, (sent_t, target, speed) in enumerate(ch.calls):
        seg_start = acts[i][0] / 1000.0          # 第 i+1 段的理论起点
        seg_end_pos = acts[i + 1][1]             # 段末位置
        dt = acts[i + 1][0] - acts[i][0]
        want_speed = round(abs(acts[i + 1][1] - acts[i][1]) / (dt / 1000.0))
        late_ms = (sent_t - seg_start) * 1000
        good = (abs(late_ms) <= 45) and (target == seg_end_pos) and (speed == want_speed)
        ok = ok and good
        print(f"  段{i+1}: 目标={target:>5} 速度={speed:>4} 迟到={late_ms:6.1f}ms "
              f"（应为 {seg_end_pos}/{want_speed}） {'✓' if good else '✗'}")
    print("判定:", "✓ 每一段一帧、目标/速度/时序全对（设备可平滑插值整段）"
          if ok and len(ch.calls) == len(acts) - 1 else "✗ 有偏差")


asyncio.run(main())
