"""只读探针：核实「反转方向」开关的最终生效性（修正第 1 轮结论）。

第 1 轮结论：host_server.py:3840 从 `vl.get("reversed")` 取 → 任何保存都清零。
第 2 轮 phone-parity 更正：紧随其后的 :3858 又用 `dev.get("reversed")` 赋了正确值
                            → :3840 是**死参数**，用户开关实际有效。

判据：只看 **最终**的 ch.reversed，不看中间的 apply_motion 参数。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import host_server as H  # noqa: E402


class FakeCh:
    def __init__(self):
        self.range_lo, self.range_hi, self.max_speed = 0.0, 100.0, 500
        self.reversed = False
        self.oc_mode = False
        self.mode_override = None
        self.applied = []

    def apply_motion(self, **kw):
        self.applied.append(kw)
        for k_src, k_dst in (("range_lo", "range_lo"), ("range_hi", "range_hi"),
                             ("max_speed", "max_speed")):
            if kw.get(k_src) is not None:
                setattr(self, k_dst, kw[k_src])
        if kw.get("reversed_") is not None:
            self.reversed = bool(kw["reversed_"])

    def apply_limits(self):
        return "fake"

    def submit(self, coro, timeout=10.0):
        return True


class FakeQuick:
    def __init__(self):
        self.orgasm = type("O", (), {"min_percent": 0, "max_percent": 100,
                                    "max_speed": 500, "link_percent": False,
                                    "link_speed": False})()
        self.slow = type("S", (), {"min_percent": 0, "max_percent": 100,
                                   "max_speed": 100, "idle_detect_seconds": 5,
                                   "link_percent": False})()

    def apply(self, orgasm=None, slow=None):
        pass


class FakePreset:
    speed = 100

    def set_speed(self, v):
        self.speed = v


class FakeSync:
    skip_idle = True
    idle_threshold = 3.0


def probe(settings: dict) -> dict:
    ch = FakeCh()
    H._DEV["obj"] = {"ch": ch, "quick": FakeQuick(), "preset": FakePreset(), "sync": FakeSync()}
    H.load_settings = lambda: settings
    H._apply_device_settings()
    return {
        "device.reversed 输入": settings.get("device", {}).get("reversed"),
        "video_link.reversed 输入": settings.get("video_link", {}).get("reversed"),
        "apply_motion(reversed_=)": ch.applied[0].get("reversed_") if ch.applied else None,
        ">>> ch.reversed 终值": ch.reversed,
    }


if __name__ == "__main__":
    rows = [
        ("设置页勾选反转（device.reversed=True）", {"device": {"reversed": True}, "video_link": {}}),
        ("设置页取消反转（device.reversed=False）", {"device": {"reversed": False}, "video_link": {}}),
        ("只有 video_link.reversed=True（UI 写不到）", {"device": {}, "video_link": {"reversed": True}}),
        ("两者都 True", {"device": {"reversed": True}, "video_link": {"reversed": True}}),
        ("两者都 False", {"device": {"reversed": False}, "video_link": {"reversed": False}}),
    ]
    out = []
    for label, st in rows:
        r = probe(st)
        r["用例"] = label
        r["判定"] = ("✅ 与 device.reversed 一致（开关有效）"
                     if r[">>> ch.reversed 终值"] == bool(st.get("device", {}).get("reversed"))
                     else "❌ 被 video_link 覆盖（开关无效）")
        out.append(r)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print()
    print("结论：:3840 的 reversed_ 是**死参数**（值立刻被 :3858 覆盖），"
          "但用户开关最终有效 ⇒ 第 1 轮「反转方向无效」结论**应撤回**，"
          "降级为「代码异味：同名参数两处写入，前者恒被覆盖」。")
