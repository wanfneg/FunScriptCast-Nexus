# -*- coding: utf-8 -*-
"""一键评测：起独立测试服务 → 推流 → 对照人工字幕 → 输出指标。

与手工流程等价，但端口/目录隔离（默认 8759，绝不碰生产 8756），
适合迭代循环里反复执行。

用法：
  .venv/Scripts/python.exe tests/run_eval.py --start 90 --sec 180 --tag r1
产出：
  E:/Development/_ref/eval/eval_<tag>.json + 控制台指标摘要
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"
SR = 16000
PCM = Path(r"E:/Development/_ref/eval/sivr001.pcm")
SRT = Path(r"E:/testvideo/SIVR-001-002 Yua Mikami/SIVR-001.srt")
PCM2 = Path(r"E:/Development/_ref/eval/sivr002.pcm")
SRT2 = Path(r"E:/testvideo/SIVR-001-002 Yua Mikami/SIVR-002.srt")
EVAL_DIR = Path(r"E:/Development/_ref/eval")
PORT = 8759


def wait_health(timeout_s: float = 60) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(1.5)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=90, help="切片起点（秒）")
    ap.add_argument("--sec", type=int, default=180, help="切片时长（秒）")
    ap.add_argument("--tag", default="r0")
    ap.add_argument("--chunk-sec", type=float, default=10.0)
    ap.add_argument("--overlap-sec", type=float, default=2.0)
    ap.add_argument("--transport", choices=["offline", "stream"], default="offline",
                    help="offline=当前生产管线（/transcribe 离线 VAD 端点）；stream=旧流式桥")
    ap.add_argument("--video", choices=["sivr001", "sivr002"], default="sivr001")
    ap.add_argument("--keep-service", action="store_true")
    args = ap.parse_args()

    pcm_src = PCM if args.video == "sivr001" else PCM2
    srt_src = SRT if args.video == "sivr001" else SRT2
    pcm_slice = EVAL_DIR / f"eval_slice.pcm"
    with open(pcm_src, "rb") as f:
        f.seek(args.start * SR * 2)
        data = f.read(args.sec * SR * 2)
    pcm_slice.write_bytes(data)

    # 端口已有健康实例则复用（迭代循环省 ~40s 重复加载）；否则自起并负责关闭。
    # 注意 Windows 允许 SO_REUSEADDR 双绑定，盲目重复拉起会造成两个实例抢单端口。
    def _health_ok() -> bool:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2) as r:
                return r.status == 200
        except Exception:
            return False

    reuse = _health_ok()
    proc = None
    if not reuse:
        env = dict(os.environ)
        proc = subprocess.Popen(
            [str(PY), "-m", "uvicorn", "server_app:app", "--host", "127.0.0.1", "--port", str(PORT)],
            cwd=str(ROOT / "vendor" / "subtitle"),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
        )
    try:
        if not wait_health():
            print("测试服务未就绪", file=sys.stderr)
            return 2
        print(f"[eval] 测试服务就绪 :{PORT}（transport={args.transport}，"
              f"{'复用' if reuse else '新起'}）")
        if args.transport == "stream":
            runner = str(ROOT / "tests" / "stream_to_json.py")
        else:
            runner = str(ROOT / "tests" / "offline_to_json.py")
        t0 = time.time()
        cmd = [str(PY), runner, str(pcm_slice),
               str(EVAL_DIR / f"eval_{args.tag}.json"),
               "--url", f"http://127.0.0.1:{PORT}",
               "--start-ms", str(args.start * 1000)]
        if args.transport == "stream":
            cmd += ["--chunk-sec", str(args.chunk_sec), "--overlap-sec", str(args.overlap_sec)]
        else:
            cmd += ["--chunk-sec", str(int(args.chunk_sec)),
                    "--overlap-sec", str(int(args.overlap_sec))]
        r = subprocess.run(cmd, capture_output=True, text=True)
        print(r.stdout.strip() or r.stderr.strip())
        wall = time.time() - t0

        r2 = subprocess.run(
            [str(PY), str(ROOT / "tests" / "compare_with_reference.py"), str(srt_src),
             str(EVAL_DIR / f"eval_{args.tag}.json")],
            capture_output=True, text=True)
        # 只保留指标区（前 25 行），去掉样例明细
        out = r2.stdout.splitlines()
        cut = next((i for i, l in enumerate(out) if "内容覆盖率最低" in l or "漏识样例" in l), len(out))
        print("\n".join(out[:cut]))
        print(f"[eval] 端到端 wall={wall:.1f}s（含翻译，冷缓存）")
        return 0
    finally:
        if proc is not None and not args.keep_service:
            proc.terminate()


if __name__ == "__main__":
    raise SystemExit(main())
