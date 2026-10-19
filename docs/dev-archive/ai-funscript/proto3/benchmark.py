"""端到端基准: 跑一次流式冒烟, 与离线基准对比, 输出机器可读指标。

用法: benchmark.py [RUN_SEC]   (默认 220)
输出: 最后一行 "METRICS: {...JSON...}"; 失败时退出码 1。
运行前自动清场(停 main_stream 实例与 mpv, 避免GPU争抢), 不动本进程。
"""
import bisect
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUN_SEC = int(sys.argv[1]) if len(sys.argv) > 1 else 220
GTG_PY = HERE.parent / "_third_party" / "GoblinTrainingGrounds" / "venv" / "Scripts" / "python.exe"
HAND = Path("E:/testvideo/funscript test/K1drsp.funscript")   # 人工打点(真值基准, 覆盖 262-1307s)
PARTIAL = HERE / "stream_partial.funscript"
LAT = Path("E:/testvideo/K1drsp.stream_lat.bin")


def clear_gpu_instances() -> None:
    subprocess.run(["taskkill", "/F", "/IM", "mpv.exe"], capture_output=True)
    ps = ("Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | "
          "Where-Object { $_.CommandLine -match 'main_stream' } | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)
    time.sleep(2)
    LAT.unlink(missing_ok=True)


def metrics(stream: list[dict], hand: list[dict]) -> dict:
    """只在与人工脚本覆盖的重叠窗内对比(人工=真值)。"""
    h0, h1 = hand[0]["at"], hand[-1]["at"]
    w0, w1 = h0, min(stream[-1]["at"], h1)
    overlap_s = (w1 - w0) / 1000.0
    out = {
        "n_actions": len(stream),
        "coverage_s": round(stream[-1]["at"] / 1000.0, 1),
        "overlap_s": round(overlap_s, 1),
    }
    if overlap_s < 30:
        out["insufficient_overlap"] = True
        return out
    sw = [a for a in stream if w0 <= a["at"] <= w1]
    hw = [a for a in hand if w0 <= a["at"] <= w1]
    gaps = sorted((b["at"] - a["at"]) / 1000.0 for a, b in zip(sw, sw[1:]))
    ots = [a["at"] for a in hw]
    deltas = []
    for a in sw:
        i = bisect.bisect_left(ots, a["at"])
        best = min((abs(a["at"] - ots[j]) for j in (i - 1, i) if 0 <= j < len(ots)),
                   default=10 ** 9)
        deltas.append(best)
    s_amp = statistics.mean(abs(b["pos"] - a["pos"]) for a, b in zip(sw, sw[1:])
                            if b["at"] > a["at"])
    h_amp = statistics.mean(abs(b["pos"] - a["pos"]) for a, b in zip(hw, hw[1:])
                            if b["at"] > a["at"])
    out.update({
        "density_ratio": round((len(sw) / overlap_s) / (len(hw) / overlap_s), 3),
        "amplitude_ratio": round(s_amp / h_amp, 3) if h_amp else 0.0,
        "reversal_median_dev_s": round(statistics.median(deltas) / 1000.0, 3),
        "max_gap_s": round(gaps[-1], 2),
        "p95_gap_s": round(gaps[int(len(gaps) * 0.95)], 2),
        "hand_density": round(len(hw) / overlap_s, 2),
    })
    return out


def main() -> int:
    clear_gpu_instances()
    t0 = time.time()
    r = subprocess.run([str(GTG_PY), "-u", "smoke_stream.py", str(RUN_SEC)],
                       cwd=str(HERE), capture_output=True, text=True)
    if r.returncode != 0 or not PARTIAL.exists():
        print("BENCH_FAILED", file=sys.stderr)
        print(r.stdout[-3000:], file=sys.stderr)
        print(r.stderr[-3000:], file=sys.stderr)
        return 1
    stream = json.load(open(PARTIAL))["actions"]
    hand = json.load(open(HAND))["actions"]
    if len(stream) < 10:
        print("BENCH_FAILED: too few actions", file=sys.stderr)
        return 1
    m = metrics(stream, hand)
    m["wall_s"] = round(time.time() - t0, 1)
    print("METRICS: " + json.dumps(m))
    return 0


if __name__ == "__main__":
    sys.exit(main())
