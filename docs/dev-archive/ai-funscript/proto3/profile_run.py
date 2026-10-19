"""独立性能剖析器 —— 运行时包装(wrap), 不改 stream_engine.py 本体。

    <GTG venv python> profile_run.py [--video E:/testvideo/K1drsp.mp4] [--secs 150]

测量点(全部 time.perf_counter, 按线程归属, 嵌套调用自动扣子项=独占时间):
  解码ffmpeg   _ffmpeg_decode 生成器每次 next() 阻塞(管道背压≈ffmpeg 节拍) + 链建立
  V-JEPA编码   Encoder.tokens(GPU 前向, batch=2 窗口)
  h0构建       StreamEngine._build_h0(点状前端; 主线程与 cut 回填线程分开计)
  TCN+heads    stream_engine._predict_shots_live(分块 trunk+heads 前向)
  样式解码     style_positions_composed + extrema_actions + fit_rev_bias
  其余         生成期墙钟 - 上述(帧编排/int8/memmap 写/chunk 编排/init/空闲)
每 20s 采样 nvidia-smi(util+mem), 每 2s 记录 n_rows; 播放头贴 gen_until 防
节流(THROTTLE_AHEAD_MS), 测的是引擎最大吞吐。--secs 到点先拍快照再停机。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import stream_engine as SE  # noqa: E402
from goblintrain import jepa_infer  # noqa: E402
from goblintrain.encoders import Encoder  # noqa: E402

LOCK = threading.Lock()
# key=(线程名, 标签) -> [inclusive 累计, 子项累计, 次数, 单次最大]
REC: dict[tuple[str, str], list] = defaultdict(lambda: [0.0, 0.0, 0, 0.0])
_tls = threading.local()
MAIN_PROC = [None]                 # 主解码链 ffmpeg 句柄(探活/退出码)

MAIN_THR = "stream-engine"          # StreamEngine.start() 里命名的提取/推理线程


def _enter(label: str) -> None:
    st = getattr(_tls, "st", None)
    if st is None:
        st = []
        _tls.st = st
    st.append((label, time.perf_counter(), st[-1][0] if st else None))


def _exit() -> None:
    label, t0, parent = _tls.st.pop()
    dt = time.perf_counter() - t0
    th = threading.current_thread().name
    with LOCK:
        r = REC[(th, label)]
        r[0] += dt
        r[2] += 1
        if dt > r[3]:
            r[3] = dt
        if parent is not None:
            REC[(th, parent)][1] += dt


def wrap(obj, name: str, label: str) -> None:
    fn = getattr(obj, name)

    def replacement(*a, **k):
        _enter(label)
        try:
            return fn(*a, **k)
        finally:
            _exit()
    replacement.__name__ = getattr(fn, "__name__", name)
    setattr(obj, name, replacement)


# ---- 包装点 ----
wrap(SE.StreamEngine, "_run", "run总成")
wrap(SE.StreamEngine, "_stream", "stream主循环")
wrap(SE.StreamEngine, "_flush_group", "flush总成")
wrap(SE.StreamEngine, "_build_h0", "h0构建")
wrap(SE.StreamEngine, "_decode_chunk", "chunk总成")
wrap(SE.StreamEngine, "_cuts_worker", "cut回填总成")
wrap(SE.StreamEngine, "_shot_cuts", "TransNet切分")
wrap(SE, "_predict_shots_live", "TCN+heads")
wrap(jepa_infer, "style_positions_composed", "样式Viterbi")
wrap(jepa_infer, "extrema_actions", "样式extrema")
wrap(jepa_infer, "fit_rev_bias", "rev_bias拟合")
wrap(Encoder, "tokens", "V-JEPA编码")

_orig_ffmpeg_decode = SE._ffmpeg_decode


def _wrapped_ffmpeg_decode(video, fps, start_s, w, h):
    _enter("解码链建立")
    try:
        gen, proc = _orig_ffmpeg_decode(video, fps, start_s, w, h)
        MAIN_PROC[0] = proc
    finally:
        _exit()

    def g():
        it = iter(gen)
        while True:
            _enter("解码ffmpeg")
            try:
                item = next(it, None)
            finally:
                _exit()
            if item is None:
                return
            item = yield item
    return g(), proc


SE._ffmpeg_decode = _wrapped_ffmpeg_decode


def rec(thr: str, label: str) -> list:
    return REC.get((thr, label), [0.0, 0.0, 0])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", default=r"E:/testvideo/K1drsp.mp4")
    ap.add_argument("--secs", type=float, default=150.0)
    ap.add_argument("--ckpt", default=None)
    args = ap.parse_args()

    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "format=duration", "-of", "csv=p=0", args.video],
                         capture_output=True, text=True).stdout.strip()
    duration_ms = float(out) * 1000.0

    lat = Path(args.video).with_suffix(".stream_lat.bin")
    try:
        lat.unlink(missing_ok=True)
    except PermissionError:
        pass

    notes: list[tuple[float, str]] = []

    def on_note(s: str) -> None:
        notes.append((round(time.perf_counter() - T0[0], 2), s))
        print(f"[engine +{time.perf_counter()-T0[0]:6.1f}s] {s}", flush=True)

    T0 = [0.0]
    engine = SE.StreamEngine(args.video, duration_ms, device="cuda",
                             ckpt=args.ckpt, on_note=on_note)

    snap: dict[str, list] = {"gpu": [], "prog": [], "gpusvc": []}
    stop_evt = threading.Event()
    T0[0] = time.perf_counter()

    def sampler() -> None:
        next_gpu, next_prog, next_ph = 0.0, 0.0, 0.0
        while not stop_evt.is_set():
            now = time.perf_counter() - T0[0]
            if now >= next_ph:      # 播放头贴 gen_until: 永不节流, 测最大吞吐
                engine.set_playhead(engine.buffer.gen_until_ms)
                next_ph = now + 0.5
            if now >= next_prog:
                with engine.lock:
                    n = engine.n_rows
                snap["prog"].append((round(now, 2), n,
                                     round(engine.buffer.gen_until_ms, 1),
                                     engine.buffer.state))
                next_prog = now + 2.0
            if now >= next_gpu:
                try:
                    r = subprocess.run(
                        ["nvidia-smi",
                         "--query-gpu=utilization.gpu,memory.used,memory.total",
                         "--format=csv,noheader,nounits"],
                        capture_output=True, text=True, timeout=15).stdout.strip()
                    r2 = subprocess.run(
                        ["nvidia-smi", "--query-compute-apps=pid,used_memory",
                         "--format=csv,noheader,nounits"],
                        capture_output=True, text=True, timeout=15).stdout.strip()
                except Exception as e:  # noqa: BLE001
                    r, r2 = f"err {e}", ""
                snap["gpu"].append((round(now, 1), r))
                snap["gpusvc"].append((round(now, 1), r2.replace("\n", " | ")))
                next_gpu = now + 20.0
            time.sleep(0.2)

    th = threading.Thread(target=sampler, daemon=True, name="prof-sampler")
    th.start()
    engine.start()
    print(f"[prof] 跑 {args.secs:.0f}s, pid={__import__('os').getpid()}, "
          f"video={args.video} ({duration_ms/1000:.0f}s)", flush=True)

    time.sleep(args.secs)

    # ---- 到点快照(先拍再停, 停机尾处理不进统计) ----
    with LOCK:
        rec_snap = {k: list(v) for k, v in REC.items()}
    with engine.lock:
        n_rows = engine.n_rows
    prog = list(snap["prog"])
    gpu = list(snap["gpu"])
    gpusvc = list(snap["gpusvc"])
    wall = time.perf_counter() - T0[0]
    # 主解码链探活: returncode None=仍在解码; 否则 ffmpeg 已退出(<0=被杀,
    # >0=崩溃 → 引擎会把半截流当正常 EOF, 即遗留发现#10 的现场)
    ff_rc = MAIN_PROC[0].poll() if MAIN_PROC[0] is not None else "no-proc"

    engine.stop(cleanup=False)
    stop_evt.set()
    th.join(timeout=5)
    t_stop = time.perf_counter()
    for t in engine._threads:
        t.join(timeout=300)
    tail_s = time.perf_counter() - t_stop
    engine.stop(cleanup=True)   # 关 memmap + 删 lat 文件(线程已停)

    # ---- 汇总 ----
    R = {k: tuple(v) for k, v in rec_snap.items()}
    m = MAIN_THR

    def inc(lab):
        return R.get((m, lab), (0.0, 0.0, 0))[0]

    def chd(lab):
        return R.get((m, lab), (0.0, 0.0, 0))[1]

    def cnt(lab):
        return R.get((m, lab), (0.0, 0.0, 0))[2]

    video_sec = n_rows / SE.ROW_HZ
    throughput_x = video_sec / wall

    # 生成活跃期: 以"识别流水线启动"为界(外层 wrap 在快照时未返回, 其 inc
    # 不可用; 首行样本晚 ~3s, 会把首批 flush 排在窗外造成负残差)
    t_gen0 = next((p[0] for p in prog if p[1] > 0), None)
    t_start = next((ts for ts, s in notes if "识别流水线启动" in s), None)
    if t_start is None:
        t_start = t_gen0 if t_gen0 is not None else 0.0
    base = wall - t_start

    dec = inc("解码ffmpeg") + inc("解码链建立")
    enc = inc("V-JEPA编码")
    h0 = inc("h0构建")
    tcn = inc("TCN+heads")
    style = inc("样式Viterbi") + inc("样式extrema") + inc("rev_bias拟合")
    flush_cpu = inc("flush总成") - chd("flush总成")
    chunk_orch = inc("chunk总成") - chd("chunk总成")
    # 主循环杂务 = 窗口 - 已量测各段(外层未返回, 不能用其 inc-chd)
    loop_misc = max(0.0, base - dec - inc("flush总成") - inc("chunk总成"))
    other = flush_cpu + chunk_orch + loop_misc
    stages = {"解码ffmpeg": dec, "V-JEPA编码": enc, "h0构建": h0,
              "TCN+heads": tcn, "样式解码": style, "其余": other}
    ssum = sum(stages.values())
    if ssum > base > 0:  # 窗外混入(链建立/首批), 按比例归一保 100%
        kscale = base / ssum
        stages = {k: v * kscale for k, v in stages.items()}

    # 生成速率曲线(相邻样本斜率)
    rates = []
    for (t1, n1, _, _), (t2, n2, _, _) in zip(prog, prog[1:]):
        if t2 > t1:
            rates.append((t2 - t1) and (n2 - n1) / SE.ROW_HZ / (t2 - t1))
    rate_last60 = [r for (t1, n1, _, _), (t2, n2, _, _) in zip(prog, prog[1:])
                   if t1 >= wall - 60.0 and t2 > t1
                   for r in [(n2 - n1) / SE.ROW_HZ / (t2 - t1)]]

    # 并行线程(cut 工作线程, 时间与主线程重叠, 不进 100%)
    par = {f"{lab}@{thr}": v for (thr, lab), v in rec_snap.items()
           if thr not in (m, "prof-sampler") and v[0] > 0.05}

    expected_rows = engine.total_rows
    truncated = ff_rc not in (None, "no-proc") and n_rows < expected_rows * 0.98
    # 快照先落盘, 后续任何汇总 bug 都不再丢数据
    snap_path = Path(__file__).with_name("profile_snap.json")
    snap_path.write_text(json.dumps({
        "wall": wall, "n_rows": n_rows, "expected_rows": expected_rows,
        "ffmpeg_returncode": str(ff_rc), "truncated": truncated,
        "rec": {f"{k[0]}|{k[1]}": v for k, v in rec_snap.items()},
        "prog": prog, "gpu": gpu, "gpusvc": gpusvc, "notes": notes,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    result = {
        "wall_s": round(wall, 2),
        "run_secs": args.secs,
        "video": args.video,
        "rows_generated": n_rows,
        "row_hz": SE.ROW_HZ,
        "video_sec_generated": round(video_sec, 1),
        "throughput_x_wall": round(throughput_x, 3),
        "throughput_x_steady": round(
            sum(r for r in rate_last60) / max(1, len(rate_last60)), 3),
        "gen_window_s": round(base, 2),
        "t_first_row_s": t_gen0,
        "engine_tail_after_stop_s": round(tail_s, 1),
        "breakdown_pct_of_gen_window": {
            k: round(100.0 * v / base, 2) for k, v in stages.items()},
        "breakdown_s": {k: round(v, 2) for k, v in stages.items()},
        "breakdown_pct_sum": round(
            sum(100.0 * v / base for v in stages.values()), 2),
        "stage_exclusive_s": {
            "解码ffmpeg": round(dec, 2), "V-JEPA编码": round(enc, 2),
            "h0构建": round(h0, 2), "TCN+heads": round(tcn, 2),
            "样式解码": round(style, 2),
            "flush内后处理(basis/下载/int8/memmap, 含GPU同步等待)": round(flush_cpu, 2),
            "chunk编排(edges/cut/publish)": round(chunk_orch, 2),
            "主循环杂务": round(loop_misc, 2)},
        "stage_counts": {lab: cnt(lab) for lab in
                         ["解码ffmpeg", "解码链建立", "flush总成", "h0构建",
                          "chunk总成", "TCN+heads", "样式Viterbi",
                          "样式extrema", "rev_bias拟合", "V-JEPA编码"]},
        "engine_PERF_crosscheck": {k: round(v, 2) for k, v in SE.PERF.items()},
        "ffmpeg_returncode_at_snap": str(ff_rc),
        "decode_stream_truncated": truncated,
        "concurrent_cuts_thread_s": {k: round(v[0], 2) for k, v in
                                     sorted(par.items(), key=lambda x: -x[1][0])},
        "buffer_state": engine.buffer.state,
        "actions_published": len(engine.buffer.actions),
        "gen_until_ms": round(engine.buffer.gen_until_ms, 1),
        "gpu_samples_util_memMB_totMB": gpu,
        "gpu_compute_apps": gpusvc,
        "progress_samples_t_rows_genuntil_state": prog,
        "rate_per_sample_x": [round(r, 3) for r in rates],
        "stage_max_single_s": {lab: round(R.get((m, lab), (0, 0, 0, 0.0))[3], 3)
                               for lab in ["解码ffmpeg", "V-JEPA编码", "h0构建",
                                           "TCN+heads", "样式Viterbi",
                                           "样式extrema", "rev_bias拟合"]},
        "notes_timeline": notes,
    }
    print("PROFILE_RESULT_JSON", flush=True)
    print(json.dumps(result, ensure_ascii=False, indent=1), flush=True)

    hdr = (f"{'阶段':<28}{'独占s':>9}{'占比%':>8}{'次数':>8}")
    print(hdr, flush=True)
    for k, v in stages.items():
        print(f"{k:<28}{v:>9.2f}{100*v/base:>8.1f}", flush=True)
    print(f"\n吞吐(墙钟150s): {throughput_x:.2f}x | "
          f"稳态(末60s): {result['throughput_x_steady']:.2f}x | "
          f"生成 {video_sec:.0f}s 视频 / 窗口 {base:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
