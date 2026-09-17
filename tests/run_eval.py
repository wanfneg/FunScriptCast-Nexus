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
import hashlib
import json
import os
import shutil
import socket
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
SUBTITLE_DIR = ROOT / "vendor" / "subtitle"


def local_code_sig() -> str:
    """本地管线源码签名，算法必须与 vendor/subtitle/server_app.py::_code_signature 一致。

    照抄那边的实现（顶层 *.py 逐个喂进 sha256，server_app.py 本身只喂内容、
    其余文件先喂文件名再喂内容），否则算出来的值永远对不上 /health 里的 code_sig，
    这个校验就废了。这里是**只读**地复现算法——不改 vendor 的任何文件。
    """
    h = hashlib.sha256()
    for f in sorted(SUBTITLE_DIR.glob("*.py")):
        if f.name == "server_app.py":
            h.update(f.read_bytes())
        else:
            h.update(f.name.encode("utf-8"))
            h.update(f.read_bytes())
    return h.hexdigest()[:12]


def health_info() -> dict:
    """读 :PORT/health；端口空着/服务未起时返回 {}。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2) as r:
            if r.status != 200:
                return {}
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return {}


def port_busy() -> bool:
    with socket.socket() as s:
        s.settimeout(1.0)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def wait_gone(timeout_s: float = 20) -> bool:
    """等端口彻底空出来（陈旧实例被 taskkill 后需要一点时间释放监听）。"""
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if not port_busy():
            return True
        time.sleep(0.5)
    return not port_busy()


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
    ap.add_argument("--chunk-sec", type=float, default=3.0)   # 与生产一致（v1.6.14 定案 3s/1s）；曾是 10.0
    ap.add_argument("--overlap-sec", type=float, default=1.0)  # 直接用旧默认会量出旧配置的成绩（实测漏识 14 vs 12）
    ap.add_argument("--transport", choices=["offline", "stream"], default="offline",
                    help="offline=当前生产管线（/transcribe 离线 VAD 端点）；stream=旧流式桥")
    ap.add_argument("--video", choices=["sivr001", "sivr002"], default="sivr001")
    ap.add_argument("--keep-service", action="store_true")
    args = ap.parse_args()

    pcm_src = PCM if args.video == "sivr001" else PCM2
    srt_src = SRT if args.video == "sivr001" else SRT2

    # 端口已有健康实例则复用（迭代循环省 ~40s 重复加载）；否则自起并负责关闭。
    # 注意 Windows 允许 SO_REUSEADDR 双绑定，盲目重复拉起会造成两个实例抢单端口。
    #
    # ⚠ 复用必须校验 code_sig：只看「端口有人应答、status==200」就复用，等于把
    # 改完的代码拿去测一个跑着旧代码的陈旧实例（2026-09-16 的陈旧实例坑，
    # server_app 的 /health 专门为此带了 code_sig）。签名不一致就必须换掉它。
    want_sig = local_code_sig()
    info = health_info()
    print(f"[eval] 本地管线签名 local_code_sig={want_sig}")
    reuse = False
    proc = None
    if info:
        got_sig = info.get("code_sig")
        print(f"[eval] :{PORT} 已有实例 code_sig={got_sig} pid={info.get('pid')} "
              f"started_at={info.get('started_at')} asr_model={info.get('asr_model')} "
              f"translate_backend={info.get('translate_backend')}")
        if got_sig == want_sig:
            reuse = True
        else:
            # 陈旧实例：留着它这次评测就是在测旧代码，指标不可信
            print(f"[eval] code_sig 不一致（本地 {want_sig} ≠ 实例 {got_sig}）→ "
                  f"杀掉陈旧实例并重启 :{PORT}")
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(info.get("pid"))],
                               capture_output=True, timeout=15)
            except Exception as e:
                print(f"[eval] taskkill 失败：{e}")
            wait_gone(timeout_s=20)
    if not reuse and port_busy():
        # 端口有人应答但拿不到 /health（不是我们的服务，或签名校验后没退干净）：
        # 占有者未知就绝不能继续——跟陈旧实例抢同一端口只会测出脏数据
        print(f"[eval] :{PORT} 被未知进程占用且 /health 不可用，拒绝继续", file=sys.stderr)
        return 2
    if not reuse:
        env = dict(os.environ)
        proc = subprocess.Popen(
            [str(PY), "-m", "uvicorn", "server_app:app", "--host", "127.0.0.1", "--port", str(PORT)],
            cwd=str(SUBTITLE_DIR),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
        )
    try:
        if not wait_health():
            print("测试服务未就绪", file=sys.stderr)
            return 2
        info = health_info()
        print(f"[eval] 测试服务就绪 :{PORT}（transport={args.transport}，"
              f"{'复用' if reuse else '新起'}）code_sig={info.get('code_sig')} "
              f"started_at={info.get('started_at')} asr_model={info.get('asr_model')} "
              f"translate_backend={info.get('translate_backend')}")
        if info.get("code_sig") != want_sig:
            # 起完了还不一致（例如另有人往这个端口塞了实例）：结果不能代表当前代码
            print(f"[eval] 服务签名仍不一致（{info.get('code_sig')} ≠ {want_sig}）"
                  f"，指标不可信", file=sys.stderr)
            return 2

        # 切片文件名要带上 --video/--start/--sec/--tag/--transport/chunk/overlap：
        # 旧代码固定写 eval_slice.pcm，两个并发评测（不同配置）会互相覆盖切片，
        # 结果 A 跑的是 B 的音频，还查不出来。
        sig = hashlib.sha1(
            f"{args.video}|{args.start}|{args.sec}|{args.tag}|{args.transport}|"
            f"{args.chunk_sec}|{args.overlap_sec}".encode("utf-8")).hexdigest()[:8]
        pcm_slice = EVAL_DIR / f"eval_slice_{args.video}_{args.start}s_{args.sec}s_{sig}.pcm"
        with open(pcm_src, "rb") as f:
            f.seek(args.start * SR * 2)
            data = f.read(args.sec * SR * 2)
        pcm_slice.write_bytes(data)
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
        # ⚠️ 必须显式指定 utf-8：Windows 上 text=True 会用本地编码（GBK）解子进程输出，
        # 而这两个子进程会打印日文/中文，解不动就在读取线程里抛 UnicodeDecodeError，
        # 结果 r.stdout / r2.stdout 变成 None，下一行 .splitlines() 直接崩（实测踩过）。
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        print(r.stdout.strip() or r.stderr.strip())
        wall = time.time() - t0

        # 推流脚本的退出码以前从不检查：它非零退出（服务连不上/整段失败）时，
        # 后面拿着一个旧 JSON 或干脆不存在的 JSON 继续评分，末尾还 return 0。
        print(f"[eval] 推流脚本退出码={r.returncode}")
        if r.returncode != 0:
            print(f"[eval] 推流脚本失败（returncode={r.returncode}）：评测结果无效", file=sys.stderr)
            return 3
        if not (EVAL_DIR / f"eval_{args.tag}.json").exists():
            print(f"[eval] 推流脚本没写出 eval_{args.tag}.json：评测结果无效", file=sys.stderr)
            return 3
        print(f"[eval] 产物 eval_{args.tag}.json，"
              f"mtime={time.strftime('%H:%M:%S', time.localtime((EVAL_DIR / f'eval_{args.tag}.json').stat().st_mtime))}")

        r2 = subprocess.run(
            [str(PY), str(ROOT / "tests" / "compare_with_reference.py"), str(srt_src),
             str(EVAL_DIR / f"eval_{args.tag}.json")],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        # 只保留指标区（前 25 行），去掉样例明细
        out = r2.stdout.splitlines()
        cut = next((i for i, l in enumerate(out) if "内容覆盖率最低" in l or "漏识样例" in l), len(out))
        print("\n".join(out[:cut]))
        print(f"[eval] 评分脚本退出码={r2.returncode}")
        if r2.returncode != 0:
            # 评分脚本非零（读不到内容/参数错）时不打印的失败最容易被当成"分数没变"
            print(f"[eval] 评分脚本失败（returncode={r2.returncode}）："
                  f"{(r2.stderr or '').strip()[:500]}", file=sys.stderr)
            return 4
        print(f"[eval] 端到端 wall={wall:.1f}s（含翻译，冷缓存）")
        return 0
    finally:
        # 切片是纯中间产物：以前从不删除，换个 tag/配置就再堆一份几百 MB
        try:
            pcm_slice.unlink()
        except OSError:
            pass
        if proc is not None and not args.keep_service:
            proc.terminate()


if __name__ == "__main__":
    raise SystemExit(main())
