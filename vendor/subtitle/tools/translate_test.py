#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""本地翻译效果测试（Ollama，不依赖 App）

读取 validate_asr.py 产出的 ASR JSON，把碎片化的识别结果先合并成"句级单元"，
再逐单元送本地 Ollama 翻译（带上文上下文），输出双语 SRT + JSON。

为什么不是逐条翻译：ASR 输出没有标点，字幕级碎片会把句子切成半句，
小模型逐条翻译必然翻车；合并成 10 秒左右的语义单元后质量明显改善。

用法：
  .venv/Scripts/python.exe translate_test.py --input out/VRTEST_Qwen3-ASR-1.7B_chunk25.json --model qwen2.5:3b
"""

import argparse
import json
import time
import urllib.request
from pathlib import Path

OLLAMA = "http://127.0.0.1:11434/api/chat"

SYSTEM = ("你是专业的日译中字幕翻译。译文要口语自然、简洁，符合中文字幕习惯；"
          "不要解释，不要添加原文没有的内容；保持人称和专有名词前后一致。只输出译文。")

USER = "把下面的日文翻译成简体中文，只输出译文，不要解释：\n{text}"
USER_CTX = ("上文（仅供理解，不要翻译）：{prev_orig}\n上文译文：{prev_tr}\n\n"
            "把下面的日文翻译成简体中文，只输出译文，不要解释：\n{text}")


def ollama_chat(model, user, temperature=0.2, num_predict=512, system=SYSTEM):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "stream": False,
        "options": {"temperature": temperature, "num_predict": num_predict},
    }).encode("utf-8")
    req = urllib.request.Request(OLLAMA, data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (data.get("message", {}).get("content", "").strip(),
            time.perf_counter() - t0, data.get("eval_count", 0))


def join_tokens(parts):
    """拼接 ASR token：中日文直接相连，英文/数字之间补空格。"""
    out = ""
    for t in parts:
        if not t:
            continue
        if out and out[-1].isascii() and out[-1].isalnum() and t[0].isascii() and t[0].isalnum():
            out += " "
        out += t
    return out


def dedupe(segments):
    """去掉重叠块造成的重复句（前句尾部与后句开头重复）。"""
    out = []
    for s in segments:
        if out:
            prev = out[-1]
            if prev["end"] - s["start"] > 0.3:
                a, b = prev["text"], s["text"]
                if b in a:
                    continue
                if a in b:
                    out[-1] = s
                    continue
                for k in range(min(len(a), len(b)), 3, -1):
                    if a[-k:] == b[:k]:
                        s = dict(s, text=b[k:].strip())
                        break
        if s["text"]:
            out.append(s)
    return out


def merge_units(segs, max_sec=12.0, max_chars=60):
    """把字幕级碎片合并成语义单元（约 10 秒 / 60 字）。"""
    units, cur = [], []
    for s in segs:
        cur.append(s)
        dur = cur[-1]["end"] - cur[0]["start"]
        chars = sum(len(x["text"]) for x in cur)
        if dur >= max_sec or chars >= max_chars:
            units.append(cur)
            cur = []
    if cur:
        units.append(cur)
    return units


def srt_time(t):
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="validate_asr.py 产出的 JSON")
    ap.add_argument("--model", default="qwen2.5:3b")
    ap.add_argument("--max-sec", type=float, default=12.0, help="语义单元最长时长")
    ap.add_argument("--glossary", nargs="*", default=None,
                    help="术语表 JSON（{\"原文\": \"译文\"}），可传多个（日/英各一个）")
    ap.add_argument("--out", default="out")
    args = ap.parse_args()

    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    segs = dedupe(data["segments"])
    units = merge_units(segs, max_sec=args.max_sec)
    print(f"识别 {len(data['segments'])} 条 → 去重 {len(segs)} 条 → 合并 {len(units)} 个语义单元"
          f"，模型 {args.model}\n")

    gloss = {}
    for gpath in (args.glossary or []):
        gloss.update(json.loads(Path(gpath).read_text(encoding="utf-8")))
    if gloss:
        print(f"已加载术语表 {len(gloss)} 条（按句命中注入）\n")

    results, total_dt, total_tok = [], 0.0, 0
    prev_orig = prev_tr = ""
    for i, unit in enumerate(units):
        text = join_tokens([x["text"] for x in unit])
        user = USER_CTX.format(prev_orig=prev_orig, prev_tr=prev_tr, text=text) if prev_tr else USER.format(text=text)
        system = SYSTEM
        if gloss:
            # 只注入当前句里真正出现的术语：既省 token，也避免术语表被乱套到别的词上
            hit = {k: v for k, v in gloss.items() if k in text}
            if hit:
                system += "\n\n术语表（原文→译文，必须严格遵守；未出现的词不要套用）：\n" + \
                          "\n".join(f"{k}→{v}" for k, v in hit.items())
        tr, dt, ntok = ollama_chat(args.model, user, system=system)
        tr = tr.splitlines()[0].strip() if tr else ""
        total_dt += dt
        total_tok += ntok
        results.append({"start": unit[0]["start"], "end": unit[-1]["end"],
                        "text": text, "translation": tr, "latency": round(dt, 3)})
        print(f"[{unit[0]['start']:6.1f}s] {text}")
        print(f"     → {tr}   ({dt:.2f}s, {ntok / dt if dt else 0:.0f} tok/s)")
        prev_orig, prev_tr = text, tr

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    stem = f"{Path(args.input).stem}_{args.model.replace(':', '-')}_units" + ("_gloss" if gloss else "")

    srt = []
    for i, r in enumerate(results, 1):
        srt += [str(i), f"{srt_time(r['start'])} --> {srt_time(r['end'])}",
                r["translation"], r["text"], ""]
    (outdir / f"{stem}.srt").write_text("\n".join(srt), encoding="utf-8")
    (outdir / f"{stem}.json").write_text(
        json.dumps({"source": args.input, "model": args.model, "segments": results},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 56)
    print(f"翻译总耗时 {total_dt:.1f}s  |  {total_tok} tokens  |  "
          f"{total_tok / total_dt if total_dt else 0:.1f} tok/s  |  "
          f"平均每单元 {total_dt / len(units):.2f}s")
    print(f"产物: {outdir / (stem + '.srt')}")


if __name__ == "__main__":
    main()
