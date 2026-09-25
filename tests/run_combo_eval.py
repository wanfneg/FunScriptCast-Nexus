# -*- coding: utf-8 -*-
"""组合评测驱动：同一段音频一次跑「质量推流 + 延迟推流」，起一次服务付一次模型加载。

机制与既有脚本**同源**（直接复用它们的 import，不另造轮子）：
  · 服务起法/端口隔离/code_sig 校验 → tests/run_eval.py（:8759，NEXUS_USER_DIR 隔离）
  · 质量推流 → tests/offline_to_json.py（离线分块端点，translate=true）
  · 延迟推流 → tests/measure_emission_lag.py 的口径（2s 步进 3s 块、keep_from 去重），
    但按**实时节奏**推（第 k 块在其音频末端对应的墙钟时刻发出），统计口径改为
    「每句的最终中文译文首次随某块返回」：对每个识别段记 段 end_ms、该句最终译文、
    首次携带它的块的音频末端 ms（video_start_ms + 块长）。

与 run_eval.py 的差异（为什么不能直接跑它两次）：
  · run_eval 不预置配置 → 会从 %APPDATA% 迁移真实用户配置，测的不是所配模型；
  · measure_emission_lag 不预置配置、不按实时节奏（尽快推完）；
  · 本驱动把两者合成一遍，且服务只起一次（ audiocpp + llama 冷启动 ~1 分钟 ×1）。

用法：
  .venv/Scripts/python.exe tests/run_combo_eval.py --video sivr002 --start 90 \
      --sec 180 --lag-sec 120 --tag r1 \
      --asr-model ../../models/Qwen3-ASR-1.7B \
      --mt-model ../../models/Sakura-7B-Qwen2.5-v1.0/sakura-7b-qwen2.5-v1.0-iq4xs.gguf

产出（都在 E:/Development/_ref/eval/）：
  eval_<tag>.json   质量推流（offline_to_json.py 产物，translate=true）
  lag_<tag>.json    延迟推流（逐句明细 + p50/p90，单位秒）

退出码：0 成功 / 2 服务起不来（含 code_sig、模型指纹校验失败）/ 3 推流失败 / 4 产物缺失。

纪律：
  · 不修改 vendor/ 下任何文件（只读模板与代码，签名算法按 run_eval.local_code_sig 只读复现）；
  · 驱动里不读任何翻译缓存（不碰 :8790 的 /api/subtitle/cache，也不读仓库 cache/ 目录）；
    服务侧经预置配置获得干净的用户数据目录，translate.cache=false。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
# 与 run_eval / measure_emission_lag 同源：直接复用其常量与辅助函数
from run_eval import (EVAL_DIR, PCM, PCM2, PY, SR, SUBTITLE_DIR, health_info,  # noqa: E402
                      local_code_sig, port_busy, wait_gone, wait_health)

PORT = 8759          # 与 run_eval / measure_emission_lag 同一隔离端口（绝不碰生产 8756）
ASR_PORT = 18081     # audiocpp 常驻服务（本评测专用端口，避开生产 8081）
MT_PORT = 18082      # llama-server（避开生产 8082）
LANG = "ja"


# ---------------------------------------------------------------- 配置预置
def deep_merge(base: dict, override: dict) -> dict:
    """深合并：override 的标量/整段 dict 覆盖 base，嵌套 dict 逐键下钻。"""
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def seed_user_dir(iso: Path, asr_model: str, mt_model: str) -> Path:
    """以 vendor/subtitle/config.json 为模板深合并出评测配置，写入隔离目录。

    为什么必须预置：user_paths 首次运行会按「新方案优先」迁移历史位置——
    %APPDATA%\\FunScriptCast-Nexus\\subtitle_config.json 排在出厂模板**前面**，
    开发机上那份是真实用户配置（0.6B / 8081 / 8082），不预置就会拿它跑评测。

    为什么还要写 .layout-v3 标记：user_paths._recover_layout 的"一次性补救扫描"
    在标记不存在且预置配置 api_key 为空时，会用**历史位置里带 key 的那份**把我
    们预置的配置整个换掉（模型/端口覆盖全丢）。预写标记把这步关掉。
    """
    template = json.loads((SUBTITLE_DIR / "config.json").read_text(encoding="utf-8"))
    override = {
        "asr": {
            "segmentation": "hybrid",
            "audiocpp": {"model": asr_model, "port": ASR_PORT},
        },
        "translate": {
            "cache": False,
            "local": {"model": mt_model, "port": MT_PORT,
                      "base_url": f"http://127.0.0.1:{MT_PORT}"},
        },
        "server": {"idle_release_min": 0},
    }
    cfg = deep_merge(template, override)
    p = iso / "subtitle_config.json"
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    (iso / ".layout-v3").write_text(
        "combo 评测预置目录：布局补救扫描已跳过（user_paths._check_layout 的标记）\n",
        encoding="utf-8")
    return p


# ---------------------------------------------------------------- 端口清理
def pids_on_port(port: int) -> list[int]:
    """netstat 找出 LISTENING 在该端口的 pid（含 IPv4/IPv6 行）。"""
    r = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=30)
    pids: set[int] = set()
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3].upper() == "LISTENING" \
                and parts[1].rsplit(":", 1)[-1] == str(port):
            try:
                pids.add(int(parts[4]))
            except ValueError:
                pass
    return sorted(pids)


def taskkill(pids: list[int], label: str) -> None:
    """taskkill /F /T：残留字幕服务下面还挂着 audiocpp/llama 孙进程，必须带 /T
    （与 host_server._kill_tree 同一理由，见 host_server.py「必须走 _kill_tree」注释）。"""
    for pid in pids:
        print(f"[combo] taskkill /F /T /PID {pid}（{label}）")
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=15)
        except Exception as e:
            print(f"[combo] taskkill {pid} 失败：{e}")


def kill_port(port: int, label: str, timeout_s: float = 20) -> bool:
    """杀掉该端口的全部监听者并等端口释放。返回是否已空。"""
    taskkill(pids_on_port(port), label)
    t0 = time.time()
    while pids_on_port(port) or port_busy_port(port):
        if time.time() - t0 > timeout_s:
            return not pids_on_port(port) and not port_busy_port(port)
        time.sleep(0.5)
    return True


def port_busy_port(port: int) -> bool:
    """run_eval.port_busy 只认模块常量 PORT，这里按参数版复刻同一判据。"""
    import socket
    with socket.socket() as s:
        s.settimeout(1.0)
        return s.connect_ex(("127.0.0.1", port)) == 0


def asr_backend_healthy(port: int = ASR_PORT) -> bool:
    """audiocpp_server 的健康判据（与 audiocpp_backend.AudioCppBackend.probe 同一）。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
            return json.loads(r.read().decode("utf-8")).get("status") == "ok"
    except Exception:
        return False


# ---------------------------------------------------------------- 服务起停
def start_service(iso: Path) -> subprocess.Popen:
    """与 run_eval.py 同一拉起方式（cwd=vendor/subtitle 的 uvicorn，环境继承 NEXUS_USER_DIR）。"""
    return subprocess.Popen(
        [str(PY), "-m", "uvicorn", "server_app:app", "--host", "127.0.0.1",
         "--port", str(PORT)],
        cwd=str(SUBTITLE_DIR), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=dict(os.environ))


def verify_health(info: dict, asr_model: str, mt_model: str) -> "str | None":
    """R60 教训：测前必看 asr_model——code_sig 只证代码一致，不证**配置**一致。
    返回 None=通过，否则返回拒绝原因（退出码 2）。"""
    want_sig = local_code_sig()
    if info.get("code_sig") != want_sig:
        return f"code_sig 不一致（本地 {want_sig} ≠ 实例 {info.get('code_sig')}）"
    # asr_model 在 /health 里是绝对路径脱敏后的最后两级（server_app._model_label）
    if Path(asr_model).name not in str(info.get("asr_model") or ""):
        return (f"asr_model 不含所配模型：/health asr_model={info.get('asr_model')!r} "
                f"不含 {Path(asr_model).name!r}")
    # segmentation 是预置配置的直接指纹（config 换档不换代码签名，R60 的教训本体）
    if info.get("segmentation") != "hybrid":
        return f"segmentation={info.get('segmentation')!r} ≠ hybrid（预置配置没生效？）"
    if info.get("idle_release_min") not in (0, 0.0):
        return f"idle_release_min={info.get('idle_release_min')!r} ≠ 0（预置配置没生效？）"
    if info.get("translate_backend") != "local":
        return f"translate_backend={info.get('translate_backend')!r} ≠ local"
    mt_label = str(info.get("translate") or "")
    if Path(mt_model).name not in mt_label and Path(mt_model).stem not in mt_label:
        print(f"[combo] ⚠️ /health translate 描述里未见所配翻译模型名：{mt_label!r}")
    return None


# ---------------------------------------------------------------- 质量推流
def quality_pass(args, pcm_src: Path) -> "tuple[int, float, Path]":
    """按 run_eval.py 的方式切片并调 offline_to_json.py（translate=true）。

    返回 (退出码, wall 秒, 切片路径)；切片由调用方在 finally 里删。
    offline_to_json 的请求体固定带 translate=true（offline_to_json.py:61），无需传参。
    """
    sig = hashlib.sha1(
        f"{args.video}|{args.start}|{args.sec}|{args.tag}|offline|"
        f"{args.chunk_sec}|{args.overlap_sec}|{args.asr_model}|{args.mt_model}"
        .encode("utf-8")).hexdigest()[:8]
    pcm_slice = EVAL_DIR / f"eval_slice_{args.video}_{args.start}s_{args.sec}s_{sig}.pcm"
    with open(pcm_src, "rb") as f:
        f.seek(args.start * SR * 2)
        data = f.read(args.sec * SR * 2)
    pcm_slice.write_bytes(data)
    out = EVAL_DIR / f"eval_{args.tag}.json"
    # ⚠️ 必须显式 utf-8：子进程打印日文/中文，GBK 解不动会在读取线程里抛
    # UnicodeDecodeError（run_eval.py 踩过的坑，同款修复）。
    t0 = time.time()
    r = subprocess.run(
        [str(PY), str(ROOT / "tests" / "offline_to_json.py"), str(pcm_slice), str(out),
         "--url", f"http://127.0.0.1:{PORT}",
         "--start-ms", str(args.start * 1000),
         "--chunk-sec", str(args.chunk_sec),
         "--overlap-sec", str(args.overlap_sec)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    wall = time.time() - t0
    print(r.stdout.strip() or r.stderr.strip())
    print(f"[combo] 推流脚本退出码={r.returncode}  质量推流 wall={wall:.1f}s")
    return r.returncode, wall, pcm_slice


# ---------------------------------------------------------------- 延迟推流
def lag_pass(args, pcm_src: Path) -> "tuple[int, dict]":
    """按 measure_emission_lag.py 的口径 + **实时节奏**重推同一片。

    口径（measure_emission_lag.py）：step=chunk-overlap、span=chunk 的分块，keep_from
    =块起点+overlap/2；emission_lag = 首次携带该段的块的音频末端(video_start_ms+块长)
    − 段 end_ms。实时节奏：第 k 块在其音频末端对应的墙钟时刻发出（t0+chunk_sec+k·step），
    于是「块的音频末端」≈ 它到达的墙钟时刻，lag ≈ 句子说完到**最终中文译文**可用的等待。

    统计口径（本驱动的新定义）：对每个识别段记 段 end_ms、该句**最终**译文（同一句被
    重叠块多次携带时取最后一次返回的翻译）、首次携带它的块的音频末端 ms。
    """
    # 只把本窗口读进内存（与 measure_emission_lag.py 的 raw = PCM2.read_bytes() 同款，
    # 整片也才 ~40MB）；窗口起点 = --start，块内偏移一律相对窗口。
    with open(pcm_src, "rb") as f:
        f.seek(args.start * SR * 2)
        raw = f.read(args.lag_sec * SR * 2)
    total_n = len(raw) // 2                      # 本窗口样本数
    chunk_n = int(args.chunk_sec * SR)           # 每块样本数
    stride_n = max(int((args.chunk_sec - args.overlap_sec) * SR), 1)  # 块起点步进
    keep_off_ms = int(args.overlap_sec * 500)    # 与 offline_to_json 同一去重偏移

    seen: dict = {}          # (start_ms,end_ms,text) -> {first_chunk_end_ms, final_zh, times}
    n_fail = 0
    n_chunks = 0
    t_all = time.time()
    t_stream0 = t_all                            # 实时节奏的时间原点
    k = 0
    while k * stride_n < total_n - chunk_n // 2:
        a = k * stride_n
        chunk = raw[a * 2:(a + chunk_n) * 2]
        if len(chunk) < SR // 5:                 # <0.05s 的尾巴不发（与旧脚本同判据量级）
            break
        vstart = args.start * 1000 + a * 1000 // SR
        # 实时节奏：等到这块的音频末端对应的墙钟时刻再发
        due = t_stream0 + args.chunk_sec + k * (stride_n / SR)
        delay = due - time.time()
        if delay > 0:
            time.sleep(delay)
        url = (f"http://127.0.0.1:{PORT}/transcribe?lang={LANG}"
               f"&video_start_ms={vstart}&keep_from_ms={vstart + keep_off_ms}"
               f"&translate=true")
        try:
            req = urllib.request.Request(
                url, data=chunk, method="POST",
                headers={"Content-Type": "application/octet-stream"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                segs = json.loads(resp.read().decode("utf-8")).get("segments") or []
        except Exception as e:
            n_fail += 1
            print(f"[combo-lag] 块@{vstart}ms 失败：{type(e).__name__}: {e}")
            k += 1
            continue
        n_chunks += 1
        chunk_end_ms = vstart + chunk_n * 1000 // SR
        for s in segs:
            key = (s.get("start_ms"), s.get("end_ms"), s.get("text") or "")
            rec = seen.setdefault(key, {"first_chunk_end_ms": chunk_end_ms,
                                        "final_zh": "", "times": 0})
            # 「最终译文」：同一句被后续块再次携带时，以最后一次返回为准
            rec["final_zh"] = (s.get("translation") or "").strip()
            rec["times"] += 1
        k += 1
    wall = time.time() - t_all

    if n_chunks == 0:
        print("[combo-lag] 一块都没成功发出：延迟推流失败", file=sys.stderr)
        return 1, {}

    sentences = []
    for (s_ms, e_ms, text), rec in seen.items():
        lag_ms = rec["first_chunk_end_ms"] - (e_ms or 0)
        sentences.append({"start_ms": s_ms, "end_ms": e_ms, "text": text,
                          "translation": rec["final_zh"],
                          "first_chunk_end_ms": rec["first_chunk_end_ms"],
                          "lag_ms": lag_ms, "lag_s": round(lag_ms / 1000, 3),
                          "carried_times": rec["times"]})
    sentences.sort(key=lambda d: (d["start_ms"], d["end_ms"]))
    lags = sorted(d["lag_ms"] for d in sentences)
    n = len(lags)

    def pct(p: float) -> float:
        return lags[min(n - 1, int(n * p))] if n else 0

    stats = {
        "p50_s": round(pct(0.5) / 1000, 3),
        "p90_s": round(pct(0.9) / 1000, 3),
        "mean_s": round(sum(lags) / n / 1000, 3) if n else 0,
        "max_s": round(lags[-1] / 1000, 3) if n else 0,
        # ms 版本并存：与 measure_emission_lag.py 的旧产物字段对得上
        "p50_ms": pct(0.5), "p90_ms": pct(0.9),
        "lag_le_1s_pct": round(100 * sum(1 for x in lags if x <= 1000) / n, 1) if n else 0,
        "lag_le_2s_pct": round(100 * sum(1 for x in lags if x <= 2000) / n, 1) if n else 0,
        "segments": n, "chunks": n_chunks, "failed_chunks": n_fail,
        "empty_zh": sum(1 for d in sentences if not d["translation"]),
        "wall_s": round(wall, 1),
    }
    out = {
        "tag": args.tag, "video": args.video, "start_s": args.start,
        "lag_sec": args.lag_sec, "chunk_sec": args.chunk_sec,
        "overlap_sec": args.overlap_sec, "translate": True, "pacing": "realtime",
        "note": "emission_lag = 首次携带该句的块的音频末端(video_start_ms+块长) − 段end_ms；"
                "实时节奏推流；translation 为该句最后一次被携带时的最终中文译文",
        "stats": stats, "sentences": sentences,
    }
    outp = EVAL_DIR / f"lag_{args.tag}.json"
    outp.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[combo-lag] {json.dumps(stats, ensure_ascii=False)}")
    print(f"[combo-lag] 写出 {outp}")
    return 0, out


# ---------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", choices=["sivr001", "sivr002"], default="sivr002")
    ap.add_argument("--start", type=int, required=True, help="切片起点（秒）")
    ap.add_argument("--sec", type=int, required=True, help="质量推流时长（秒）")
    ap.add_argument("--lag-sec", type=int, required=True, help="延迟推流时长（秒，同起点）")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--asr-model", required=True,
                    help="asr.audiocpp.model（相对 vendor/subtitle，如 ../../models/Qwen3-ASR-1.7B）")
    ap.add_argument("--mt-model", required=True,
                    help="translate.local.model（相对 vendor/subtitle 的 GGUF 路径）")
    ap.add_argument("--chunk-sec", type=float, default=3.0)   # 与生产一致（v1.6.14 定案 3s/1s）
    ap.add_argument("--overlap-sec", type=float, default=1.0)
    ap.add_argument("--keep-asr", action="store_true",
                    help="结束时保留 :18081 上健康的 audiocpp 实例（beam_size 等实验预注入用）；"
                         "起服务前也不杀健康实例")
    ap.add_argument("--keep-service", action="store_true", help="结束时保留 :8759 字幕服务")
    args = ap.parse_args()

    pcm_src = PCM if args.video == "sivr001" else PCM2
    if not pcm_src.exists():
        print(f"[combo] 找不到源音频 {pcm_src}", file=sys.stderr)
        return 4

    # 用户数据隔离（与 run_eval 同一理由：可复现 + 不动真实 key 的迁移顺序）
    iso = Path(tempfile.mkdtemp(prefix="nexus-combo-"))
    os.environ["NEXUS_USER_DIR"] = str(iso)
    seeded = seed_user_dir(iso, args.asr_model, args.mt_model)
    print(f"[combo] 用户数据隔离目录：{iso}")
    print(f"[combo] 预置配置：{seeded}")
    _chk = json.loads(seeded.read_text(encoding="utf-8"))
    print(f"[combo]   asr.audiocpp.model={_chk['asr']['audiocpp']['model']} "
          f"port={_chk['asr']['audiocpp']['port']} "
          f"segmentation={_chk['asr']['segmentation']}")
    print(f"[combo]   translate.local.model={_chk['translate']['local']['model']} "
          f"port={_chk['translate']['local']['port']} cache={_chk['translate']['cache']}")

    # ---- 起服务前清场：:8759 必杀；:18081 默认必杀，--keep-asr 时保留健康实例
    info = health_info()
    if info:
        print(f"[combo] :{PORT} 已有实例 pid={info.get('pid')} "
              f"code_sig={info.get('code_sig')} asr_model={info.get('asr_model')} → 杀掉"
              "（本驱动每次用全新预置配置，复用实例必然带着别人的配置）")
        if info.get("pid"):
            taskkill([int(info["pid"])], "旧字幕服务")
    if not kill_port(PORT, f"占用:{PORT} 的进程"):
        print(f"[combo] :{PORT} 清不掉，拒绝继续", file=sys.stderr)
        return 2
    if args.keep_asr and asr_backend_healthy():
        print(f"[combo] --keep-asr：:{ASR_PORT} 上有健康 audiocpp 实例，保留"
              "（服务端 ensure_server 会直接复用它）")
    elif not kill_port(ASR_PORT, f"占用:{ASR_PORT} 的进程"):
        print(f"[combo] :{ASR_PORT} 清不掉，拒绝继续", file=sys.stderr)
        return 2

    proc = start_service(iso)
    pcm_slice = None
    try:
        if not wait_health(timeout_s=150):   # audiocpp 拉起 60s + llama 预热 join 12s，留余量
            print(f"[combo] 测试服务未就绪（:{PORT} /health 超时）", file=sys.stderr)
            return 2
        info = health_info()
        print(f"[combo] 服务就绪 :{PORT} code_sig={info.get('code_sig')} "
              f"asr_backend={info.get('asr_backend')} asr_model={info.get('asr_model')} "
              f"segmentation={info.get('segmentation')} "
              f"translate_backend={info.get('translate_backend')}")
        print(f"[combo]   translate={info.get('translate')} "
              f"mt_warm={info.get('mt_warm')} idle_release_min={info.get('idle_release_min')}")
        reason = verify_health(info, args.asr_model, args.mt_model)
        if reason:
            print(f"[combo] 服务指纹校验失败：{reason}", file=sys.stderr)
            return 2

        # ---- 质量推流（offline_to_json.py，translate=true）
        rc, q_wall, pcm_slice = quality_pass(args, pcm_src)
        if rc != 0:
            print(f"[combo] 质量推流失败（returncode={rc}）", file=sys.stderr)
            return 3
        eval_json = EVAL_DIR / f"eval_{args.tag}.json"
        if not eval_json.exists():
            print(f"[combo] 推流脚本没写出 {eval_json}", file=sys.stderr)
            return 3

        # ---- 延迟推流（实时节奏，translate=true）
        lrc, lout = lag_pass(args, pcm_src)
        if lrc != 0:
            return 3
        lag_json = EVAL_DIR / f"lag_{args.tag}.json"
        if not lag_json.exists():
            print(f"[combo] 延迟推流没写出 {lag_json}", file=sys.stderr)
            return 4

        # ---- 产物核验：存在且非空壳
        for name, must_key in ((eval_json, "segments"), (lag_json, "sentences")):
            try:
                d = json.loads(name.read_text(encoding="utf-8"))
                if must_key not in d:
                    raise KeyError(must_key)
            except Exception as e:
                print(f"[combo] 产物核验失败 {name}：{e}", file=sys.stderr)
                return 4
        n_seg = len(json.loads(eval_json.read_text(encoding="utf-8")).get("segments") or [])
        n_sen = len(json.loads(lag_json.read_text(encoding="utf-8")).get("sentences") or [])
        if n_seg == 0 or n_sen == 0:
            print(f"[combo] ⚠️ 产物为空壳（eval segments={n_seg}, lag sentences={n_sen}）"
                  "——检查服务日志与该时段是否有人声")
        print(f"[combo] 完成：{eval_json.name}（{n_seg} 段，wall={q_wall:.1f}s）+ "
              f"{lag_json.name}（{n_sen} 句）")
        return 0
    finally:
        if pcm_slice is not None:
            try:
                pcm_slice.unlink()                # 切片是纯中间产物（run_eval 同款）
            except Exception:
                pass
        if proc is not None:
            if args.keep_service:
                print(f"[combo] --keep-service：保留 :{PORT} 服务（pid={proc.pid}）")
            else:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except Exception:
                    pass
                # uvicorn 死后兜底：8759 必清；18081 在 --keep-asr 时留给实验注入，
                # 否则一并清（它由服务以 Job Object 拉起，通常已随父进程一起死）
                kill_port(PORT, "残留字幕服务")
                if not args.keep_asr:
                    kill_port(ASR_PORT, "残留 audiocpp")


if __name__ == "__main__":
    raise SystemExit(main())
