"""只读探针：验证 settings -> 设备层的映射是否自洽。

不启动 GUI / 服务器 / 不写任何设置文件。做法：
  1) 导入 host_server（模块级只有常量与目录创建，无副作用）；
  2) 把 host_server._DEV["obj"] 换成记录参数的假对象；
  3) 直接调用真实的 _apply_device_settings()，看它到底把哪个键喂给了设备层。

结论判据：
  · /api/device/settings 把"反转方向"写进 settings["device"]["reversed"]；
  · _apply_device_settings 却从 settings["video_link"]["reversed"] 取反转；
  · video_link 里从来没有 reversed 这个键（UI 只写 idle_link/burst_link/...）。
  → 若探针显示 apply_motion(reversed_=False) 且 ch.reversed 最终为 False，
    那么"反转方向"开关在任何一次设置保存后都会被静默重置。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import host_server as H  # noqa: E402


class FakeChannel:
    def __init__(self):
        self.calls = []
        self.range_lo, self.range_hi = 0.0, 100.0
        self.max_speed = 500
        self.reversed = False
        self.oc_mode = False

    def apply_motion(self, **kw):
        self.calls.append(("apply_motion", kw))
        if kw.get("range_lo") is not None:
            self.range_lo = kw["range_lo"]
        if kw.get("range_hi") is not None:
            self.range_hi = kw["range_hi"]
        if kw.get("max_speed") is not None:
            self.max_speed = kw["max_speed"]
        if kw.get("reversed_") is not None:
            self.reversed = bool(kw["reversed_"])

    def apply_limits(self):
        self.calls.append(("apply_limits", {}))
        return "fake-future"

    def submit(self, coro, timeout=10.0):
        self.calls.append(("submit", {"timeout": timeout}))
        return True


class FakeQuick:
    def __init__(self):
        self.orgasm = type("O", (), {"min_percent": 0, "max_percent": 100,
                                    "max_speed": 500, "link_percent": False,
                                    "link_speed": False})()
        self.slow = type("S", (), {"min_percent": 0, "max_percent": 100,
                                   "max_speed": 100, "idle_detect_seconds": 5,
                                   "link_percent": False})()
        self.applied = []

    def apply(self, orgasm=None, slow=None):
        self.applied.append(("apply", orgasm, slow))


class FakePreset:
    def __init__(self):
        self.speed = 100

    def set_speed(self, v):
        self.speed = int(v)


class FakeSync:
    skip_idle = True
    idle_threshold = 3.0


def run(settings: dict, label: str) -> dict:
    ch, quick = FakeChannel(), FakeQuick()
    H._DEV["obj"] = {"ch": ch, "quick": quick, "preset": FakePreset(), "sync": FakeSync()}
    H.load_settings = lambda: settings          # 注入被测设置，绝不读/写真实文件
    H._apply_device_settings()
    return {
        "case": label,
        "apply_motion_kwargs": [c[1] for c in ch.calls if c[0] == "apply_motion"],
        "channel_reversed_after": ch.reversed,
        "channel_range": [ch.range_lo, ch.range_hi],
        "channel_max_speed": ch.max_speed,
        "quick_apply": quick.applied,
    }


def main() -> int:
    out = []
    # 用例 1：只按 /api/device/settings 打开"反转方向"
    out.append(run({"device": {"reversed": True}, "video_link": {}},
                   "device.reversed=True, video_link 空"))
    # 用例 2：用户同时在联动卡里设了行程/速度
    out.append(run({"device": {"reversed": True},
                    "video_link": {"range_min": 20, "range_max": 80, "max_speed": 300}},
                   "device.reversed=True + 联动卡 20-80/300"))
    # 用例 3：device.slow.max_speed=100 与联动卡 idle_speed=250 谁赢
    out.append(run({"device": {"slow": {"max_speed": 100, "idle_detect_seconds": 9},
                               "orgasm": {"max_speed": 500}},
                    "video_link": {"idle_speed": 250, "burst_speed": 400}},
                   "device.slow.max_speed=100 vs 联动 idle_speed=250"))
    # 用例 4：完全没有设置（首次运行）时联动卡默认值
    out.append(run({"device": {}, "video_link": {}}, "全默认"))
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
