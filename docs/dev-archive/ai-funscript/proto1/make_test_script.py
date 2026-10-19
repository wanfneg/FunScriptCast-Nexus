"""生成合成 demo.funscript：多种节奏 + 一个长无动作段，用于无头验证。"""
import json

actions = []
t = 0


def add(seconds, pos):
    actions.append({"at": int(seconds * 1000), "pos": max(0, min(100, int(pos)))})


# 0-30s: 慢速正弦（0.5Hz，行程 10-90）
while t < 30:
    import math
    add(t, 50 + 40 * math.sin(2 * math.pi * 0.5 * t))
    t += 0.5
# 30-60s: 中速（1.2Hz，全行程）
while t < 60:
    add(t, 50 + 50 * math.sin(2 * math.pi * 1.2 * t))
    t += 0.25
# 60-75s: 无动作段（位置恒定，用于测试"脚本已开始但静止"）
add(60, 50)
add(75, 50)
# 75-120s: 快速冲刺（2.5Hz）
while t < 120:
    add(t, 50 + 50 * math.sin(2 * math.pi * 2.5 * t))
    t += 0.12
add(120, 5)

with open("demo.funscript", "w", encoding="utf-8") as f:
    json.dump({"version": "1.0", "actions": actions}, f)
print(f"demo.funscript: {len(actions)} 个动作点, 120 秒")
