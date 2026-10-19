"""R04 只读探针 3：运行时语义复核（task-4 verifier 独立编写）。

不启动 GUI / 服务器；不写任何设置文件（脚本首尾对 data/*.json 做 sha256 守卫并打印）。
覆盖：
  A) /api/quick 的 stop 分支（host_server.py:2551-2552）是否真的走不到 set_allow_move（R01-A1）
  B) _apply_device_settings 里 video_link.reversed 是否被 device.reversed 覆盖（R01-A9）
  C) device.* 死设置 / 联动卡优先级（R01-A10）
  D) _apply_device_settings 中途异常是否静默跳过其余联动项（R01-A12）
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

WATCH = [ROOT / "data" / "integrated_settings.json",
         ROOT / "data" / "subtitle_config.json",
         ROOT / "data" / "library_index.json"]


def digest() -> dict:
    out = {}
    for f in WATCH:
        out[str(f.relative_to(ROOT))] = (hashlib.sha256(f.read_bytes()).hexdigest()[:16]
                                         if f.is_file() else "<missing>")
    return out


BEFORE = digest()
print("=== A) /api/quick stop 分支复现（逐字照抄 host_server.py:2551-2552） ===")
from device.quick_moves import QuickMoves  # noqa: E402


class _FakeState:
    allow_move = True


class _FakeCh:
    def __init__(self):
        self.state = _FakeState()
        self.set_calls = []

    def set_allow_move(self, allow):
        self.set_calls.append(bool(allow))
        self.state.allow_move = bool(allow)


loop = asyncio.new_event_loop()
ch = _FakeCh()
q = QuickMoves(ch, loop)
err = None
try:
    was = q.is_stop                        # host_server.py:2551
    res = q.set_stop(True)                 # host_server.py:2552
except Exception as e:                     # host_server.py:2673-2674 的兜底
    err = f"{type(e).__name__}: {e}"
print(f"异常                = {err}")
print(f"set_allow_move 调用 = {ch.set_calls}   (空 = 完全没走到 set_stop)")
print(f"allow_move 终值     = {ch.state.allow_move}")
print(f"state()['stop']     = {q.state()['stop']}  (正确写法 was = not q.ch.state.allow_move -> {not ch.state.allow_move})")
print(f"[断言] A1: {'PASS' if err and not ch.set_calls and ch.state.allow_move else 'FAIL'}")

print()
print("=== B/C/D) _apply_device_settings 注入探针 ===")
import host_server as H  # noqa: E402


class FakeChannel:
    def __init__(self, fail_submit=False):
        self.calls = []
        self.range_lo, self.range_hi = 0.0, 100.0
        self.max_speed = 500
        self.reversed = False
        self.oc_mode = False
        self._forced_toy = None
        self.mode_override = None
        self.fail_submit = fail_submit

    def apply_motion(self, **kw):
        self.calls.append(("apply_motion", kw))
        if kw.get("range_lo") is not None:
            self.range_lo = kw["range_lo"]
        if kw.get("range_hi") is not None:
            self.range_hi = kw["range_hi"]
        if kw.get("max_speed") is not None:
            self.max_speed = kw["max_speed"]
        if kw.get("reversed_") is not None:      # 与 channel.py:87-88 同语义
            self.reversed = bool(kw["reversed_"])

    def apply_limits(self):
        return "fake-coro"

    def submit(self, coro, timeout=10.0):
        self.calls.append(("submit", {"timeout": timeout}))
        if self.fail_submit:
            raise TimeoutError("simulated BLE timeout (R01-A12)")
        return True


class FakeQuick:
    def __init__(self):
        self.slow = SimpleNamespace(min_percent=0, max_percent=100, max_speed=100,
                                    idle_detect_seconds=5, link_percent=False)
        self.orgasm = SimpleNamespace(min_percent=0, max_percent=100, max_speed=500,
                                      link_percent=False, link_speed=False)
        self.applied = []

    def apply(self, orgasm=None, slow=None):
        self.applied.append((orgasm, slow))
        for src, dst in ((orgasm, self.orgasm), (slow, self.slow)):
            for k, v in (src or {}).items():
                setattr(dst, k, v)


class FakePreset:
    def __init__(self):
        self.speed_calls = []

    def set_speed(self, v):
        self.speed_calls.append(int(v))


def run(settings: dict, label: str, fail_submit=False) -> dict:
    ch, quick, preset = FakeChannel(fail_submit), FakeQuick(), FakePreset()
    sync = SimpleNamespace(skip_idle=True, idle_threshold=3.0)
    H._DEV["obj"] = {"ch": ch, "quick": quick, "preset": preset, "sync": sync}
    H.load_settings = lambda: settings          # 注入，绝不读写真实文件
    H._apply_device_settings()
    return {"case": label,
            "apply_motion": [c[1] for c in ch.calls if c[0] == "apply_motion"],
            "ch.reversed_final": ch.reversed,
            "quick.apply": quick.applied,
            "quick.slow.max_speed_final": quick.slow.max_speed,
            "quick.orgasm.max_speed_final": quick.orgasm.max_speed,
            "ch.oc_mode": ch.oc_mode, "sync.skip_idle": sync.skip_idle,
            "sync.idle_threshold": sync.idle_threshold,
            "preset.speed_calls": preset.speed_calls,
            "forced_toy": getattr(ch._forced_toy, "id", None),
            "mode_override": ch.mode_override}


cases = [
    ({"video_link": {"reversed": True}, "device": {}}, "video_link.reversed=True, device 空", False),
    ({"video_link": {}, "device": {"reversed": True}}, "device.reversed=True, video_link 空", False),
    ({"video_link": {"idle_speed": 250, "burst_speed": 400, "range_min": 20, "range_max": 80},
      "device": {"slow": {"max_speed": 100, "idle_detect_seconds": 9},
                 "orgasm": {"max_speed": 300}, "script_sync": False,
                 "preset_speed": 180, "oc_mode": True, "skip_idle": False,
                 "idle_threshold": 7}}, "联动卡 vs device.*（含 script_sync=False）", False),
    ({"video_link": {"range_min": 10, "range_max": 90},
      "device": {"reversed": True, "oc_mode": True, "skip_idle": False,
                 "idle_threshold": 7, "preset_speed": 180, "a10_mode": 1}},
     "submit 抛 TimeoutError -> 后续联动项是否被跳过", True),
    ({"video_link": {}, "device": {"a10_mode": 1}}, "A6: UI 勾选 VorzePiston(a10_mode=1)", False),
    ({"video_link": {}, "device": {"a10_mode": 0}}, "A6: UI 取消(a10_mode=0)", False),
]
rows = [run(s, l, f) for s, l, f in cases]
print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))

r1, r2, r3, r4, r5, r6 = rows
print()
print("判据：")
print(f"  A9  video_link.reversed 传入 apply_motion = "
      f"{r1['apply_motion'][0].get('reversed_')} 但 ch.reversed 终值 = {r1['ch.reversed_final']}  "
      f"-> {'PASS（联动键被 3858 覆盖，死读）' if r1['ch.reversed_final'] is False else 'FAIL'}")
print(f"  A9' device.reversed 才是生效键 = {r2['ch.reversed_final']} "
      f"(apply_motion 收到 {r2['apply_motion'][0].get('reversed_')})")
print(f"  A10 idle_speed=250 覆盖 device.slow.max_speed=100 -> "
      f"{r3['quick.slow.max_speed_final']}（device 段被无视）")
print(f"  A10 burst_speed=400 覆盖 device.orgasm.max_speed=300 -> {r3['quick.orgasm.max_speed_final']}")
print(f"  A8  device.script_sync=False 对设备层零影响（没有任何读点可观测）")
print(f"  A12 submit 抛错后：oc_mode={r4['ch.oc_mode']} "
      f"skip_idle={r4['sync.skip_idle']} idle_threshold={r4['sync.idle_threshold']} "
      f"preset.set_speed={r4['preset.speed_calls']} "
      f"-> {'PASS（后续联动项全被跳过）' if (r4['ch.oc_mode'] is False and r4['sync.skip_idle'] is True and not r4['preset.speed_calls']) else 'FAIL'}")
print()
print("A6 档案映射（真实 _apply_device_settings 的产物）:")
print(f"  a10_mode=1（勾选 VorzePiston）-> _forced_toy={r5['forced_toy']}  mode_override={r5['mode_override']}"
      f"   {'PASS（倒置：应 vorze）' if r5['forced_toy'] == 'serveu' and r5['mode_override'] == 1 else 'FAIL'}")
print(f"  a10_mode=0（取消）           -> _forced_toy={r6['forced_toy']}  mode_override={r6['mode_override']}"
      f"   {'PASS（倒置：应 serveu）' if r6['forced_toy'] == 'vorze' and r6['mode_override'] == 0 else 'FAIL'}")

AFTER = digest()
print()
print("=== 副作用守卫（只读承诺） ===")
print("before:", BEFORE)
print("after :", AFTER)
print(f"[断言] 未改动任何 data/*.json: {'PASS' if BEFORE == AFTER else 'FAIL'}")
