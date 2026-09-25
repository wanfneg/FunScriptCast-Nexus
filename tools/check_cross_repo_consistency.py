# -*- coding: utf-8 -*-
"""check_cross_repo_consistency.py —— 双端复制文件一致性【信息性】检查（F32 配套）

手机端 FunScriptCast 与头显端 VRFunScriptCast 各持一份全量复制的核心文件
（清单内嵌于下方 PAIRS）。本脚本逐对比较 SHA256 与
diff 行数量级，帮助回移前后对照。

⚠️ 这不是硬门禁：
  * 两侧本就允许"刻意分叉"（预设数量、平台接线等），差异不等于错误；
  * 任一仓库不存在时安静跳过（脚本在 Nexus 仓库里单独分发时也能跑）；
  * 退出码仅提示 —— 0 = 全一致或无法比对；1 = 存在内容差异；2 = 脚本自身出错。
    不要把它接进"失败即中止"的 CI 门禁。

用法：
    python tools/check_cross_repo_consistency.py
    python tools/check_cross_repo_consistency.py --phone <手机仓库根> --vr <头显仓库根>
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

# 脚本位于 Nexus 仓库 tools\ 下，两个客户端仓库默认假设与 Nexus 同级
# （开发机上三个仓库都直接放在 E:/Development 下）。
_NEXUS_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_PHONE = _NEXUS_ROOT.parent / "FunScriptCast"
_DEFAULT_VR = _NEXUS_ROOT.parent / "VRFunScriptCast"

_PHONE_SRC = "app/src/main/java/com/funscriptcast"
_VR_SRC = "funscriptcore/src/main/java/com/funscriptcast"

# 双端复制文件清单（本脚本内嵌，即唯一权威清单）。
# 每行：(phone 相对路径, vr 相对路径, 说明)
PAIRS = [
    ("data/AiSubtitleEngine.kt", "engine/AiSubtitleEngine.kt", "AI 字幕引擎"),
    ("ble/BleDeviceService.kt", "ble/BleDeviceService.kt", "BLE 设备服务"),
    ("ble/DeviceProtocols.kt", "ble/DeviceProtocols.kt", "BLE 协议"),
    ("data/Funscript.kt", "data/Funscript.kt", ".funscript 解析"),
    ("sync/SyncEngine.kt", "sync/SyncEngine.kt", "同步引擎"),
    ("sync/QuickMoves.kt", "sync/QuickMoves.kt", "一键动作"),
    ("sync/PresetDefs.kt", "sync/PresetDefs.kt", "预设定义"),
    ("sync/PresetPlayer.kt", "sync/PresetPlayer.kt", "预设播放"),
    ("data/DlnaClient.kt", "data/DlnaClient.kt", "DLNA 客户端"),
    ("data/SmbClient.kt", "data/SmbClient.kt", "SMB 客户端"),
    ("data/WebDavClient.kt", "data/WebDavClient.kt", "WebDAV 客户端"),
    ("data/ServeuApi.kt", "data/ServeuApi.kt", "通知/固件接口"),
]


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()[:12]


def diff_lines(a: Path, b: Path) -> int:
    """两侧 unified diff 变更行合计（< / > 行数）。git 不可用等异常时返回 -1。"""
    try:
        out = subprocess.run(
            ["git", "diff", "--no-index", "--numstat", str(a), str(b)],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        if out:
            add, dele = out.split("\t")[:2]
            return int(add) + int(dele)
        # git diff --numstat 对无差异文件输出空；再用普通 diff 兜底确认
        return 0
    except Exception:
        return -1


def main() -> int:
    ap = argparse.ArgumentParser(description="双端复制文件一致性信息性检查（非门禁）")
    ap.add_argument("--phone", default=str(_DEFAULT_PHONE), help="手机端仓库根")
    ap.add_argument("--vr", default=str(_DEFAULT_VR), help="头显端仓库根")
    args = ap.parse_args()

    phone_root, vr_root = Path(args.phone), Path(args.vr)
    print(f"[parity] phone = {phone_root}")
    print(f"[parity] vr    = {vr_root}")

    if not phone_root.is_dir() or not vr_root.is_dir():
        print("[parity] 任一仓库不存在：跳过（信息性检查，不作失败）")
        return 0

    differ = 0
    compared = 0
    print(f"[parity] {'文件':<34}{'状态':<10}diff行数  说明")
    for ph, vr, note in PAIRS:
        a, b = phone_root / _PHONE_SRC / ph, vr_root / _VR_SRC / vr
        label = ph if ph == vr else f"{ph} <-> {vr}"
        if not a.is_file() or not b.is_file():
            print(f"[parity] {label:<34}{'缺文件':<10}-         {note}")
            continue
        compared += 1
        if a.read_bytes() == b.read_bytes():
            print(f"[parity] {label:<34}{'一致':<10}0         {note}")
            continue
        differ += 1
        dl = diff_lines(a, b)
        dl_s = str(dl) if dl >= 0 else "?"
        print(f"[parity] {label:<34}{'不同':<10}{dl_s:<9} {note}"
              f"  ({sha256(a)} / {sha256(b)})")

    print(f"[parity] 比对 {compared}/{len(PAIRS)} 对，其中 {differ} 对内容不同")
    print("[parity] 提示：差异属预期（刻意分叉 + 待回移并存），处置以各仓库迭代记录为准；"
          "退出码仅提示，不作为门禁。")
    return 1 if differ else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:      # pragma: no cover - 脚本自身出错才走到这里
        print(f"[parity] 脚本出错（信息性检查跳过）：{type(e).__name__}: {e}")
        sys.exit(2)
