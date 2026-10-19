"""A2 定向验证: TransNet 该在哪个输入上跑?

官方 boundaries.ensure 跑的是 project.video_path() = **转码产物**(480p30),
本引擎曾跑原始 1080p50 —— 官方对 clip 000001(K1cztm) 测出 22 个切点, 而引擎
对 K1drsp 实测只有 12 个。

本脚本在同一部片上各跑一次 TransNetV2, 与官方 cuts_ms 逐点比对:
  ① 官方转码产物 drafts/videos/000001.mp4  -> 应能复现官方 22 个切点(真值校验)
  ② 该片的原始 1080p 源                    -> 看换输入源会差多少(A2 的因果)
只测切分, 不跑完整引擎。

运行: <GTG venv python> verify_cuts.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
GTG = ROOT.parent / "_third_party" / "GoblinTrainingGrounds"
sys.path.insert(0, str(GTG))

from goblintrain.boundaries import analyze_video, load_transnet   # noqa: E402

BPATH = GTG / "projects" / "drafts" / "boundaries" / "000001.json"
OFFICIAL = [float(c) for c in
            json.load(open(BPATH, encoding="utf-8"))["cuts_ms"]]
META = json.load(open(GTG / "projects" / "drafts" / "meta" / "000001.json",
                      encoding="utf-8"))
DUR_MS = float(META["duration_ms"])
TRANSCODED = GTG / "projects" / "drafts" / "videos" / "000001.mp4"
ORIGINAL = Path(r"E:\testvideo\funscript test\K1cztm.mp4")


def cover(a, b, tol_ms=100.0):
    """a 中有多少点能在 b 里找到 tol 内的对应。"""
    if not a or not b:
        return 0, len(a)
    bb = np.asarray(b, dtype=np.float64)
    return sum(1 for x in a if np.min(np.abs(bb - x)) <= tol_ms), len(a)


def main() -> int:
    print(f"官方 boundaries(跑在转码产物上): {len(OFFICIAL)} 个切点")
    print(f"clip 000001 时长 {DUR_MS / 1000:.0f}s; 转码产物 {TRANSCODED.name} "
          f"({TRANSCODED.stat().st_size / 1e6:.0f} MB)")
    tn = load_transnet("cuda")
    res = {}
    for label, path in (("官方转码产物 480p30", TRANSCODED),
                        ("原始 1080p 源", ORIGINAL)):
        if not path.exists():
            print(f"  {label}: 不存在, 跳过 ({path})")
            continue
        payload = analyze_video("verify", str(path), DUR_MS, tn, "cuda")
        cuts = [float(c) for c in payload["cuts_ms"]]
        res[label] = cuts
        h, n = cover(cuts, OFFICIAL)
        print(f"  {label:<22} -> {len(cuts):>3} 个切点, 与官方重合 {h}/{n}")

    if len(res) == 2:
        (la, a), (lb, b) = list(res.items())
        h, n = cover(a, b)
        print(f"\n换输入源的影响: {la} vs {lb} 重合 {h}/{n}, 不一致 {n - h} 个")

    out = {"official": OFFICIAL, "runs": {k: v for k, v in res.items()}}
    (ROOT / "verify_cuts_result.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("结果已存 verify_cuts_result.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
