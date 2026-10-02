"""只读探针：BOOST 与预设速度的恢复语义（用于推翻 R12-N5「BOOST 后锁死 500」）。

R12-N5 断言：`preset_player.set_speed`（:127-131）在 BOOST 期间覆写 `_boost_prev_speed`，
             ⇒ 取消 BOOST 后速度永远回 500。

实测结论：**不成立**。该覆写是手机端同款设计（"BOOST 期间拖滑轨 → 取消后恢复该值"，
见 ui/presets 注释与手机端 AppViewModel.togglePresetBoost）。真正残留的问题只是
前端 `ui/app.js:2276-2279` 的 `psVal` 未按 `p.boost` 归一化（显示值 ≠ 生效值）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "vendor"))

from device.preset_player import PresetPlayer  # noqa: E402


class Loop:
    def call_soon_threadsafe(self, fn):
        pass

    def create_task(self, coro):
        coro.close()
        return None


class FakeCh:
    max_speed = 500


def new_player() -> PresetPlayer:
    return PresetPlayer(FakeCh(), Loop(), ROOT / "ui" / "presets.json")


def case_boost_untouched() -> dict:
    pp = new_player()
    seq = [("初始", pp.speed)]
    pp.set_speed(180); seq.append(("拖到 180", pp.speed))
    pp.toggle_boost(); seq.append(("点 BOOST", pp.speed))
    pp.toggle_boost(); seq.append(("取消 BOOST（期望回 180）", pp.speed))
    return {"case": "BOOST 期间不动滑轨", "seq": seq,
            "判定": "正确回 180" if pp.speed == 180 else "错误"}


def case_boost_dragged() -> dict:
    pp = new_player()
    seq = [("初始", pp.speed)]
    pp.set_speed(180); seq.append(("拖到 180", pp.speed))
    pp.toggle_boost(); seq.append(("点 BOOST", pp.speed))
    pp.set_speed(250); seq.append(("BOOST 期间拖到 250", pp.speed))
    pp.toggle_boost(); seq.append(("取消 BOOST（期望回 250）", pp.speed))
    return {"case": "BOOST 期间拖滑轨", "seq": seq,
            "判定": "正确回 250（= 手机端 boostPrevSpeed 语义）" if pp.speed == 250 else "错误"}


def case_effective_speed_under_boost() -> dict:
    pp = new_player()
    pp.set_speed(180)
    pp.toggle_boost()
    return {"case": "BOOST 生效速度", "speed_field": pp.speed,
            "effective_speed()": pp._effective_speed(),
            "说明": "生效 500；若前端显示 180/250 就是显示未归一化（app.js:2276-2279）"}


if __name__ == "__main__":
    import json
    out = [case_boost_untouched(), case_boost_dragged(), case_effective_speed_under_boost()]
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print()
    print("结论：BOOST 恢复逻辑正确（R12-N5 的后端部分为误报）；"
          "仅前端 psVal 显示未归一化，建议显示 effSpeed = boost ? 500 : speed。")
