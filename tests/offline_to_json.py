# -*- coding: utf-8 -*-
"""把一段裸 PCM 按**旧头显分块模式**推给 /transcribe（离线端点），汇出同构 JSON。

对照流式评测的"天花板"参考：25s 大块 + 2s 重叠 + keep_from_ms 去重，
与 AiSubtitleEngine 分块模式的上传行为一致（keep_from = 块起点 + overlap/2，
服务端丢弃起点早于 keep_from 的句子）。输出与 stream_to_json.py 同构。

用法：
  .venv/Scripts/python.exe tests/offline_to_json.py <in.pcm> <out.json> \
      [--chunk-sec 25] [--overlap-sec 2] [--start-ms 0]
"""
from __future__ import annotations

import argparse
import http.client
import json
import time
import urllib.parse

SR = 16000
BYTES_PER_SAMPLE = 2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pcm")
    ap.add_argument("out")
    ap.add_argument("--url", default="http://127.0.0.1:8756")
    ap.add_argument("--chunk-sec", type=float, default=25)
    ap.add_argument("--overlap-sec", type=float, default=2)
    ap.add_argument("--lang", default="ja")
    ap.add_argument("--start-ms", type=int, default=0)
    args = ap.parse_args()

    pcm = open(args.pcm, "rb").read()
    if len(pcm) % 2:
        pcm = pcm[:-1]
    chunk_bytes = int(args.chunk_sec * SR * BYTES_PER_SAMPLE)
    stride_bytes = int(max(args.chunk_sec - args.overlap_sec, 1) * SR * BYTES_PER_SAMPLE)
    u = urllib.parse.urlparse(args.url)

    segs: list[dict] = []
    timings = []
    n_chunk = 0
    # 失败计数：以前只取 data["segments"]，HTTP 状态和响应里的 error 都不看，
    # 于是「服务端明确报错」被当成「这块没人说话」——评测表现为漏识变多，
    # 而推流脚本照样 return 0，人看到的是"分数变差了"而不是"根本没跑成功"。
    n_http_err = 0
    n_bad_json = 0
    n_srv_err = 0
    t_all = time.perf_counter()
    off = 0
    while off < len(pcm):
        piece = pcm[off:off + chunk_bytes]
        if len(piece) < BYTES_PER_SAMPLE * SR // 10:
            break
        video_start_ms = args.start_ms + (off // BYTES_PER_SAMPLE) * 1000 // SR
        keep_from = video_start_ms + args.overlap_sec * 500
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=600)
        body = (f"/transcribe?lang={args.lang}&video_start_ms={video_start_ms}"
                f"&keep_from_ms={keep_from}&translate=true")
        t_req = time.perf_counter()
        conn.request("POST", body, piece, {"Content-Type": "application/octet-stream"})
        resp = conn.getresponse()
        status = resp.status
        buf = resp.read().decode("utf-8", "replace")
        timings.append(time.perf_counter() - t_req)
        conn.close()
        n_chunk += 1
        if status != 200:
            n_http_err += 1
            print(f"[chunk@{video_start_ms}ms] HTTP {status}: {buf[:160]}")
            off += stride_bytes
            continue
        try:
            data = json.loads(buf)
        except Exception:
            n_bad_json += 1
            print(f"[chunk@{video_start_ms}ms] bad response: {buf[:120]}")
            off += stride_bytes
            continue
        if not isinstance(data, dict):
            # 老服务/代理可能回一个裸数组：这里按失败计，别静默当空块
            n_bad_json += 1
            print(f"[chunk@{video_start_ms}ms] 非预期响应形状（{type(data).__name__}）")
            off += stride_bytes
            continue
        err = data.get("error")
        if err:
            # 超限/音频过短等都是 200 + error：这一块确实没结果，必须显式计数，
            # 否则它在指标里会伪装成"静音块"
            n_srv_err += 1
            print(f"[chunk@{video_start_ms}ms] 服务端报错: {err}")
        for s in data.get("segments") or []:
            segs.append({"start_ms": int(s.get("start_ms") or 0),
                         "end_ms": int(s.get("end_ms") or 0),
                         "text": (s.get("text") or "").strip(),
                         "translation": (s.get("translation") or "").strip()})
        off += stride_bytes

    segs.sort(key=lambda s: s["start_ms"])
    n_fail = n_http_err + n_bad_json + n_srv_err
    json.dump({"segments": segs,
               "meta": {"chunk_sec": args.chunk_sec, "overlap_sec": args.overlap_sec,
                        "chunks": n_chunk, "path": "offline",
                        # 失败计数进产物：下游（run_eval）与后来的读者才看得出
                        # 这份 JSON 是残缺的，而不是"这段音频本来就没人说话"
                        "failed_chunks": n_fail, "http_errors": n_http_err,
                        "bad_json": n_bad_json, "server_errors": n_srv_err,
                        "partial": bool(n_fail)}},
              open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    timings.sort()
    med = timings[len(timings) // 2] if timings else 0
    print(f"chunks={n_chunk} segments={len(segs)} "
          f"empty_zh={sum(1 for s in segs if not s['translation'])} "
          f"req_time med={med:.1f}s max={max(timings):.1f}s "
          f"wall={time.perf_counter() - t_all:.1f}s -> {args.out}")
    print(f"failed_chunks={n_fail}（HTTP {n_http_err} / 坏响应 {n_bad_json} / 服务端报错 {n_srv_err}）")
    # 退出码：整段全失败才算"没跑成功"（非零）。部分失败只标记 partial 并告警——
    # 这是刻意的：run_eval 把非零当成推流失败并拒绝评分，若一个偶发 500 就整轮作废
    # 反而让人放弃评测。但失败块数进了 meta，指标是"部分残缺"这件事有据可查，
    # 不会像以前那样无声无息。
    if n_chunk and n_fail >= n_chunk:
        print(f"全部 {n_chunk} 块都失败：{args.out} 没有有效内容，退出码 1")
        return 1
    if n_chunk == 0:
        # 一块都没发出去（切片短于 0.1s 就被 break 掉）：产物是个空壳，
        # 下游拿去评分只会得到"什么都没有"，必须当成失败而不是成功
        print(f"没有发出任何块（切片 {len(pcm)} 字节太短？）：{args.out} 是空结果，退出码 1")
        return 1
    if n_fail:
        print(f"！有 {n_fail}/{n_chunk} 块失败：{args.out} 已标记 partial，指标会被低估")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
