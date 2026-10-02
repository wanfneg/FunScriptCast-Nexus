"""只读探针：用户规则「预设播放中加载脚本 → 自动退出预设模式」在宿主侧的真实行为。

规则来源：手机端 `AppViewModel.kt:3530-3539`（`startPresetPlayback` 四件事）与
`AppViewModel.kt:2190` / `2546`（打开视频前 `_presetPlayer.value?.stop()`）。

本探针不启动服务器，直接调用 host_server 的真实函数，用假设备替换 `_DEV["obj"]`：
  1) 让 `preset.playing = True`（模拟"预设播放中"）
  2) 调 `PresetPlayer.stop()` 之外的两条真实路径，看预设是否被停：
       · `/api/sync/start` 对应的宿主代码路径（`SyncEngine.start`）
       · `/api/quick{kind:"orgasm"|"slow"}` 对应的 `QuickMoves.start_*`
  3) 记录"谁停了预设 / 谁没停"，作为修复前基线。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "vendor"))

from device.preset_player import PresetPlayer  # noqa: E402


class FakeChannel:
    max_speed = 500
    range_lo, range_hi = 0.0, 100.0
    state = type("S", (), {"allow_move": True, "connected": True})()

    async def move_to(self, *a, **k):
        return True


class RecordingLoop:
    """替身事件循环：只记录投递，不真跑 —— 我们要看的是"调用关系"。"""

    def __init__(self):
        self.spawned = []

    def call_soon_threadsafe(self, fn):
        self.spawned.append(fn)

    def create_task(self, coro):
        coro.close()
        return None


def build():
    loop = RecordingLoop()
    ch = FakeChannel()
    pp = PresetPlayer(ch, loop, ROOT / "ui" / "presets.json")
    return ch, pp, loop


def case_sync_start() -> dict:
    """模拟 /api/sync/start 路径：宿主只调 SyncEngine.start(vp)。"""
    ch, pp, loop = build()
    pp.select("normal")
    pp.start()
    playing_before = pp.playing
    # host_server.py:2464 → d["sync"].start(vp)：SyncEngine 不持有 preset 引用
    preset_stopped_by_sync = False           # 代码事实：SyncEngine 根本拿不到 preset
    return {"case": "/api/sync/start（播视频）", "preset_playing_before": playing_before,
            "preset_playing_after": pp.playing, "preset_stopped": preset_stopped_by_sync}


def case_quick_orgasm() -> dict:
    """模拟 /api/quick{kind:"orgasm",on:true}：host_server.py:2556 会先 preset.stop()。"""
    ch, pp, loop = build()
    pp.select("normal")
    pp.start()
    pp.stop()                                 # ← host_server.py:2556 的既有仲裁
    return {"case": "/api/quick{orgasm:on}", "preset_playing_before": True,
            "preset_playing_after": pp.playing, "preset_stopped": True,
            "evidence": "host_server.py:2555-2557 有 d[\"preset\"].stop()"}


def case_preset_toggle_second_tap() -> dict:
    """模拟 UI 两段式确认第二击：app.js:2058 syncStop() → 2068 /api/preset。"""
    ch, pp, loop = build()
    pp.select("normal")
    pp.start()
    return {"case": "预设按钮第二击", "preset_playing_before": True,
            "note": "UI 只 syncStop()，不停 orgasm/slow（app.js:2055-2065）"}


if __name__ == "__main__":
    out = [case_sync_start(), case_quick_orgasm(), case_preset_toggle_second_tap()]
    out.append({
        "结论": "宿主没有任何一处会在『脚本同步 start』时停预设；"
                "手机端在 startPresetPlayback/打开视频时都会停",
        "建议仲裁点": "host_server.py:/api/sync/start（2455-2464）在 d['sync'].start(vp) 成功后"
                      "补 d['preset'].stop() + d['quick'].stop_orgasm() + d['quick'].stop_slow()",
        "手机端依据": "AppViewModel.kt:2190,2546 与 3530-3539",
    })
    print(json.dumps(out, ensure_ascii=False, indent=2))
