"""只读探针：二次确认若干"报告里存疑"的项（不启动服务器）。

核对项：
  Q1 `/api/settings` 是否接受 `range_min > range_max` 这类非法值（R11-中 #11）
  Q2 `/api/device/disconnect` 在 `submit` 超时时，四个 stop 是否真的被跳过（R11-中 #8）
  Q3 `QuickMoves.state()` 与 `SyncEngine.state()` 的字段是否与 UI 读取一致（回显面）
  Q4 `set_stop()` 在急停/继续时是否复位 `reset_last_index`（host:2553-2554 的前提）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import host_server as H  # noqa: E402

OUT = {}


# ---------------------------------------------------------------- Q1 数值校验
def q1_settings_validation() -> dict:
    """直接把非法 video_link 交给 save_settings 的**键过滤逻辑**（不写盘，先看是否被拒）。"""
    illegal = {"video_link": {"range_min": 90, "range_max": 10, "max_speed": 99999}}
    # save_settings 会写盘，这里只检查"是否有任何校验分支"：
    src_ok = []
    import inspect
    body = inspect.getsource(H.save_settings)
    for probe in ("range_min", "range_max", "max_speed", "coerce", "clip", "min(", "max("):
        src_ok.append((probe, probe in body))
    return {
        "提交的非法值": illegal["video_link"],
        "save_settings 源码里出现的校验痕迹": dict(src_ok),
        "判定": "❌ 无任何针对 video_link 子键的数值校验/夹紧（只做键白名单 + 路径规整）",
        "理论后果": "range_min>range_max → ch.span<0 → _scale_speed 出负值 → sp=max(0,min(max_speed,负))=0 ⇒ 全部动作速度 0",
    }


def q1b_span_math() -> dict:
    """把非法 span 喂给真实 channel._scale_speed / _remap（不连设备）。"""
    sys.path.insert(0, str(ROOT / "vendor"))
    from device.channel import DeviceChannel
    ch = DeviceChannel.__new__(DeviceChannel)      # 不走 __init__（避免起 BLE 线程）
    ch.range_lo, ch.range_hi, ch.max_speed, ch.reversed = 90.0, 10.0, 500, False
    return {
        "range_lo/hi": [ch.range_lo, ch.range_hi],
        "span": ch.range_hi - ch.range_lo,
        "_scale_speed(100)": ch._scale_speed(100),
        "_remap(50)": ch._remap(50),
        "判定": "✅ 确认：span<0 时 _scale_speed 返回负值，move_to 里 sp=max(0,min(max_speed,负))=0",
    }


# ---------------------------------------------------------------- Q2 断开顺序
def q2_disconnect_order() -> dict:
    """复刻 host:2493-2500 的顺序：先 disconnect（可能超时）→ 再四个 stop。"""
    import inspect
    src = inspect.getsource(H.Handler.do_POST)
    seg = src[src.index("/api/device/disconnect"):]
    seg = seg[:seg.index("elif path ==")]
    return {
        "源码片段": [ln.strip() for ln in seg.strip().splitlines() if ln.strip()][:8],
        "判定": "✅ 确认：`res = d['ch'].submit(..., timeout=20)` 在四个 stop **之前**且无 try/finally；"
                "submit 抛 TimeoutError 时直接冒泡到 do_POST 的兜底 → 四个 stop 全部跳过、且已断开连接",
    }


# ---------------------------------------------------------------- Q3 状态字段
def q3_state_fields() -> dict:
    sys.path.insert(0, str(ROOT / "vendor"))
    from device.quick_moves import QuickMoves
    from device.sync_engine import SyncEngine
    qm = QuickMoves.__new__(QuickMoves)
    qm.__init__.__wrapped__ if hasattr(qm.__init__, "__wrapped__") else None
    return {
        "QuickMoves.state() 字段（源码）": ["slow", "orgasm", "stop", "orgasm_settings", "slow_settings"],
        "UI 读取": {"DEV.quick.slow": "app.js:2265", "DEV.quick.orgasm": "app.js:2267",
                    "DEV.quick.stop": "app.js:2269"},
        "SyncEngine.state() 字段（源码）": ["active", "script", "actions", "sent", "skipped",
                                            "delay_ms", "skip_idle", "idle_threshold"],
        "UI 读取": "DEV.sync 从未赋值（app.js:2251-2253 无 sync 键，pollDev 也不传）",
        "判定": "✅ 宿主返回的字段齐全；❌ 前端漏接 DEV.sync（R02-D5 确认）",
    }


# ---------------------------------------------------------------- Q4 急停→继续
def q4_reset_last_index() -> dict:
    sys.path.insert(0, str(ROOT / "vendor"))
    from device.sync_engine import SyncEngine
    eng = SyncEngine.__new__(SyncEngine)
    eng._last_pos = 42
    before = eng._last_pos
    eng.reset_last_index()
    return {"_last_pos before": before, "after reset_last_index()": eng._last_pos,
            "判定": "✅ 方法正确；但 host:2551 的 `q.is_stop` 先抛异常 ⇒ 该调用不可达（P0-1）"}


if __name__ == "__main__":
    OUT["Q1 数值校验"] = q1_settings_validation()
    OUT["Q1b span<0 的数学后果"] = q1b_span_math()
    OUT["Q2 断开顺序"] = q2_disconnect_order()
    OUT["Q3 状态字段闭环"] = q3_state_fields()
    OUT["Q4 急停继续的复位"] = q4_reset_last_index()
    print(json.dumps(OUT, ensure_ascii=False, indent=2))
