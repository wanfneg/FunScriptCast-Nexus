# -*- coding: utf-8 -*-
"""把流水线输出与人工字幕对比，量化"识别度"和"翻译准确度"。

参考字幕是**纯中文**（人工翻译），没有日文原文。这一点决定了能测什么：

  能测（硬指标，错了就是缺陷）：
    · 译文为空          —— 观众看到空白，铁定是缺陷
    · 译文残留假名/英文  —— 漏译
    · 覆盖/漏识         —— 人工听到并翻译的地方，我有没有出字幕（召回率）
    · 时序偏差          —— 配对段落的起点差

  能测（软指标，需人判断）：
    · 译文与人工的**内容覆盖度**（LCS recall）—— 人工译文里的字有多大比例
      按顺序出现在我的译文里。比 SequenceMatcher 对称比值稳健得多：
      我这边一条人工字幕常被切成 6 条碎片，拼接后必然更长，对称比值天然偏低，
      那是**对齐假象**，不是翻译错。

  不能测：
    · ASR 的字符错误率（CER）—— 需要**日文**参考文本，中文参考做不到

两个必须小心的解读陷阱：

  1. **"多余输出"多数不是错误**。人工字幕只覆盖对白，流水线会把所有发声
     （语气词、拟声、喘息）都转出来，还常常把一句话切成多条。所以"我有、
     人工没有"的条目里绝大多数是正常的。
  2. **对齐不可靠的配对不能算进质量指标**。一条 10s 的人工字幕若被我的
     20 条碎片覆盖，或我的配对段落时长是人工的 5 倍，那 `sim` 低只反映
     切分差异。本脚本按 `--trust-*` 阈值过滤，并单独列出被剔除的数量。

  还有三个口径问题（2026-09 修正，都会改变结论）：

  3. **短人工句的"假漏识"**。配对门槛是绝对毫秒数（默认 300ms），而重叠量上限
     是 min(人工句长, 我的句长)：参考字幕里短于门槛的句子**无论识别多完美都
     必然判为漏识**，恒进 missed。现在门槛与该句人工时长绑定
     （thr = min(--min-overlap-ms, 句长/2)），并把"短到连放宽后的门槛都够不着"
     的条目单列为"不可判定（人工句过短）"，**不计入漏识的分子与分母**。
  4. **"发声时长"必须是区间并集**。sum(end-start) 在相邻段重叠时把同一秒算两次，
     分块越碎高估越多（实测 3s 块 +20%、流式 +28%、25s 块 0%），跨配置对比被系统性带偏。
  5. **覆盖率是上界**。`my_zh` 是把所有够门槛的段**整段**拼起来的，落在人工句
     时间范围外的字也会算进 LCS；碎片数（n_frag）也参与可信度判定。报告里
     额外给出碎片数与区间外时长，读者才知道 0.478 这类数字是上界而非实测值。

用法：
  .venv\\Scripts\\python.exe tests\\compare_with_reference.py <参考.srt> <我的.json> [--out 报告.tsv]
"""
from __future__ import annotations

import argparse
import difflib
import json
import pathlib
import re
import sys

KANA = re.compile(r"[\u3041-\u309f\u30a0-\u30ff]")
LATIN = re.compile(r"[A-Za-z]{2,}")


# ------------------------------------------------------------------ 解析
def parse_srt(path: pathlib.Path) -> list:
    """解析 SRT → [(start_ms, end_ms, text)]。"""
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    out = []
    for block in re.split(r"\n\s*\n", raw.strip()):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if len(lines) < 2:
            continue
        m = re.search(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)", block)
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        start = ((g[0] * 60 + g[1]) * 60 + g[2]) * 1000 + g[3]
        end = ((g[4] * 60 + g[5]) * 60 + g[6]) * 1000 + g[7]
        text = " ".join(ln.strip() for ln in lines
                        if "-->" not in ln and not ln.strip().isdigit())
        out.append((start, end, text.strip()))
    return out


def load_mine(path: pathlib.Path) -> list:
    """读流水线输出（JSON）→ [(start_ms, end_ms, jp, zh)]。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("segments") or []
    out = []
    for s in data:
        out.append((int(s.get("start_ms") or 0), int(s.get("end_ms") or 0),
                    (s.get("text") or "").strip(), (s.get("translation") or "").strip()))
    return sorted(out)


def overlap(a0, a1, b0, b1) -> int:
    return max(0, min(a1, b1) - max(a0, b0))


def merge_spans(spans: list) -> list:
    """区间并集。spans = [(start, end)] → 合并后的 [(start, end)]。"""
    out: list = []
    for s, e in sorted(spans):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def union_ms(spans: list) -> int:
    """区间并集的总时长（毫秒）。

    sum(end-start) 在相邻段重叠时会把同一秒算两次——分块越碎高估越多
    （实测 3s 块 +20%、流式 +28%、25s 块 0%），跨配置对比会被系统性带偏，
    所以"发声时长"必须按并集口径算。
    """
    return sum(e - s for s, e in merge_spans(spans))


def interval_overlap_ms(groups: list) -> int:
    """这些段自身时间轴上的并集时长（groups = [(start_ms, end_ms)]）。

    用于回答"这条人工句里，我的哪些段**真的落在**这条人工句的时间范围内"——
    拼接文本里超出范围的部分不能算作对它内容的覆盖，所以要先按并集量出来。
    """
    if not groups:
        return 0
    return union_ms(groups)


def clean(s: str) -> str:
    """比较前统一：去掉空白与句读，避免标点差异干扰。"""
    return re.sub(r"[\s。，、！？!?.,；;：:「」『』…—－\-\"“”‘’()（）]", "", s or "")


def similar(a: str, b: str) -> float:
    """对称相似度（仅对长度接近的配对有参考价值）。"""
    return difflib.SequenceMatcher(None, clean(a), clean(b)).ratio()


def lcs_recall(ref: str, mine: str) -> float:
    """人工译文里有多大比例的字**按顺序**出现在我的译文里。

    对"我的译文更长"天然免疫（多余的字不扣分），所以它衡量的是
    "人工说的内容我有没有说出来"，这正是我们要的翻译准确度代理指标。
    """
    r, m = clean(ref), clean(mine)
    if not r:
        return 1.0
    sm = difflib.SequenceMatcher(None, r, m, autojunk=False)
    matched = sum(b.size for b in sm.get_matching_blocks())
    return matched / len(r)


# ------------------------------------------------------------------ 主流程
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("reference", help="人工字幕（.srt，中文）")
    ap.add_argument("mine", help="流水线输出（.json）")
    ap.add_argument("--out", default="", help="写出逐句对照 TSV")
    ap.add_argument("--min-overlap-ms", type=int, default=300,
                    help="配对重叠门槛（毫秒）。实际门槛还会按人工句长收紧到 句长/2，"
                         "见 docstring 第 3 条：绝对毫秒数会让短句必然判漏识")
    # 对齐可信度阈值：不满足的配对只列出、不计入质量统计
    ap.add_argument("--trust-min-cov", type=float, default=0.5,
                    help="配对需覆盖人工段落时长的比例（默认 0.5）")
    ap.add_argument("--trust-max-ratio", type=float, default=3.0,
                    help="我的配对段落时长 / 人工段落时长 的上限（默认 3.0）")
    ap.add_argument("--trust-max-frag", type=int, default=8,
                    help="配对允许的最大碎片数（默认 8）。docstring 承诺「碎片过多"
                         "要剔除」，旧代码算了 n_frag 却从没用它判定，等于没兑现")
    ap.add_argument("--dump-all", action="store_true",
                    help="打印全部可信配对（默认只打印差异最大的）")
    args = ap.parse_args()

    ref = parse_srt(pathlib.Path(args.reference))
    mine = load_mine(pathlib.Path(args.mine))
    if not ref or not mine:
        print(f"读不到内容：参考 {len(ref)} 条，我的 {len(mine)} 条")
        return 2

    # 发声时长必须用区间并集：sum(end-start) 在段与段重叠时重复计入
    # （分块越碎高估越多），跨配置比较会被系统性带偏
    ref_union = union_ms([(s, e) for s, e, _ in ref]) / 1000
    my_spans = [(s, e) for s, e, _, _ in mine]
    my_union = union_ms(my_spans) / 1000
    my_naive = sum(e - s for s, e in my_spans) / 1000
    ref_span_total = max(e for _, e, _ in ref) / 1000
    print(f"人工字幕 {len(ref)} 条（发声 {ref_union:.0f}s＝区间并集，"
          f"占比 {ref_union / ref_span_total * 100:.0f}%）")
    print(f"流水线   {len(mine)} 条（发声 {my_union:.0f}s＝区间并集；"
          f"逐段相加是 {my_naive:.0f}s，高出 {(my_naive / my_union - 1) * 100 if my_union else 0:.0f}%"
          f"——重叠被重复计入的部分）")

    # ---- 逐条对齐：一条人工字幕可能对应我这边多条
    pairs, missed, used = [], [], set()
    undecidable: list = []      # 人工句短到无法判定（不计入漏识分子/分母）
    n_relaxed = 0               # 因放宽门槛才配上的条数（与上面对照看"假漏识"）
    for rs, re_, rtext in ref:
        ref_span = max(1, re_ - rs)
        # 门槛与该句人工时长绑定：重叠量上限是 min(人工句长, 我的句长)，
        # 用绝对 300ms 的话，参考字幕里短于 300ms 的句子（本片 124 条里有 9 条）
        # 无论识别多完美都必然判为漏识——结构性假漏识。
        thr = min(args.min_overlap_ms, max(1, ref_span // 2))
        if thr < args.min_overlap_ms:
            n_relaxed += 1
        hit = [i for i, (ms, me, _, _) in enumerate(mine)
               if overlap(rs, re_, ms, me) >= thr]
        if not hit:
            # 人工句本身太短：连放宽后的门槛都够不着，无法判断是我漏识还是
            # 参考字幕切得碎。单列成"不可判定"，绝不进 missed。
            if ref_span < args.min_overlap_ms:
                undecidable.append((rs, re_, rtext))
            else:
                missed.append((rs, re_, rtext))
            continue
        for i in hit:
            used.add(i)
        my_jp = " ".join(mine[i][2] for i in hit)
        my_zh = " ".join(mine[i][3] for i in hit)
        best = max(hit, key=lambda i: overlap(rs, re_, mine[i][0], mine[i][1]))
        span = max(1, mine[best][1] - mine[best][0])
        ov = sum(overlap(rs, re_, mine[i][0], mine[i][1]) for i in hit)
        cov = ov / ref_span
        # 落在人工句时间范围外的部分：my_zh 是整段拼接，这些字也进了 LCS，
        # 所以覆盖率天然是上界。单独量出来，读者才知道能信到什么程度。
        in_ref = interval_overlap_ms([(mine[i][0], mine[i][1]) for i in hit])
        out_ms = sum(max(0, mine[i][1] - mine[i][0]) for i in hit) - in_ref
        pairs.append({
            "ref_start": rs, "ref_end": re_, "ref": rtext,
            "my_start": mine[best][0], "my_end": mine[best][1],
            "my_jp": my_jp, "my_zh": my_zh,
            "dt": mine[best][0] - rs,
            "n_frag": len(hit),
            "span_ratio": span / ref_span,
            "thr": thr,
            "cov": cov,
            "out_ms": out_ms,
            "trusted": (cov >= args.trust_min_cov
                        and span / ref_span <= args.trust_max_ratio
                        # 碎片过多也是对齐假象：docstring 承诺了要剔除，
                        # 旧代码算了 n_frag 却从不参与判定
                        and len(hit) <= args.trust_max_frag),
            "sim": similar(rtext, my_zh),
            "recall": lcs_recall(rtext, my_zh),
            "empty": not my_zh,
        })

    extra = [mine[i] for i in range(len(mine)) if i not in used]
    trusted = [p for p in pairs if p["trusted"]]

    # ---- 1) 硬缺陷（我的输出自身问题，与对齐无关）
    empty = [(s, e, jp, zh) for s, e, jp, zh in mine if not zh]
    leaks = [(s, e, jp, zh) for s, e, jp, zh in mine if KANA.search(zh or "")]
    latins = [(s, e, jp, zh) for s, e, jp, zh in mine
              if LATIN.search(zh or "") and not LATIN.search(jp or "")]
    n = len(mine)
    print("\n===== 1. 硬缺陷（越低越好，0 为合格）=====")
    print(f"  译文为空        {len(empty):4d}  ({len(empty)/n*100:5.1f}%)")
    print(f"  译文残留假名    {len(leaks):4d}  ({len(leaks)/n*100:5.1f}%)   ← 漏译")
    print(f"  译文夹英文      {len(latins):4d}  ({len(latins)/n*100:5.1f}%)")

    # ---- 2) 覆盖
    # 分母只算"可判定"的人工句：短到无法判定的那些既不算命中也不算漏识
    n_dec = len(pairs) + len(missed)
    print("\n===== 2. 语音覆盖（识别召回）=====")
    print(f"  人工有、我也有   {len(pairs):4d} / {n_dec}  ({len(pairs)/n_dec*100:5.1f}%)"
          f"   ← 分母＝可判定 {n_dec} 条")
    print(f"  人工有、我没有   {len(missed):4d} / {n_dec}  ({len(missed)/n_dec*100:5.1f}%)   ← 漏识")
    print(f"  不可判定（人工句过短，<{args.min_overlap_ms}ms，既不计命中也不计漏识）"
          f"  {len(undecidable):4d} / {len(ref)}")
    if n_relaxed:
        # 这些条用旧口径必然判漏识；现在够门槛就按命中算，直接改变漏识条数
        print(f"  其中因「门槛随句长放宽」才可能配上的短句 {n_relaxed:4d} 条"
              f"（旧口径下必然假漏识）")
    print(f"  我有、人工没有   {len(extra):4d} / {n:4d}  ({len(extra)/n*100:5.1f}%)   ← 多为语气词/切分，非错误")

    # ---- 3) 时序：只用可信配对（脚本自己在上文判定过 trusted，docstring 也写明
    #      碎片过多/时长比过大的配对不能算进质量指标）。两组数字都打印，标明口径。
    def _dt_line(label: str, ps: list) -> None:
        if not ps:
            return
        dts = sorted(p["dt"] for p in ps)
        med = dts[len(dts) // 2]
        within = sum(1 for d in dts if abs(d) <= 500)
        within1s = sum(1 for d in dts if abs(d) <= 1000)
        print(f"  {label}（{len(dts)} 对） 中位 {med:+d}ms   |偏差|≤500ms {within/len(dts)*100:.1f}%   "
              f"≤1000ms {within1s/len(dts)*100:.1f}%   最大 {abs(dts[-1]):+d}ms")

    print("\n===== 3. 时序（起点偏差）=====")
    _dt_line("可信配对", trusted)
    if len(trusted) != len(pairs):
        _dt_line("全部配对（含对齐不可信，仅供对照）", pairs)

    # ---- 4) 译文内容覆盖（软指标，仅可信配对）
    print("\n===== 4. 译文内容覆盖（软指标，仅供参考）=====")
    dropped = len(pairs) - len(trusted)
    print(f"  配对 {len(pairs)} 对，其中对齐可信 {len(trusted)} 对"
          f"（剔除 {dropped} 对：碎片过多或时长比过大，低了属于对齐假象）")
    if trusted:
        rec = sorted(p["recall"] for p in trusted)
        avg = sum(rec) / len(rec)
        b_hi = sum(1 for x in rec if x >= 0.8)
        b_mid = sum(1 for x in rec if 0.5 <= x < 0.8)
        b_lo = sum(1 for x in rec if x < 0.5)
        print(f"  内容覆盖率 平均 {avg:.3f}  中位 {rec[len(rec)//2]:.3f}  "
              f"最低 {rec[0]:.3f}   ← **上界**（口径见下两行）")
        print(f"  ≥0.80 高度一致  {b_hi:4d}  ({b_hi/len(rec)*100:5.1f}%)")
        print(f"  0.50~0.80 大致一致 {b_mid:4d}  ({b_mid/len(rec)*100:5.1f}%)")
        print(f"  <0.50 需人工核对  {b_lo:4d}  ({b_lo/len(rec)*100:5.1f}%)")
        lr = sorted(len(clean(p["my_zh"])) / max(1, len(clean(p["ref"]))) for p in trusted)
        print(f"  长度比(我/人工) 中位 {lr[len(lr)//2]:.2f}  → <1 偏简略，>2 偏啰嗦")
        # 上界的两处成因：拼接文本越界 + 碎片多
        frags = sorted(p["n_frag"] for p in trusted)
        n_multi = sum(1 for f in frags if f > 1)
        out_tot = sum(p["out_ms"] for p in trusted) / 1000
        out_share = out_tot / my_union if my_union else 0
        print(f"  可信配对碎片数 中位 {frags[len(frags)//2]}  最多 {frags[-1]}  "
              f"其中多碎片 {n_multi}/{len(frags)} 对（1 条人工句被切成多条）")
        print(f"  越界时长：可信配对的段总长里有 {out_tot:.1f}s 落在人工句时间范围外"
              f"（占全片发声 {out_share*100:.0f}%），这部分文字也进了 LCS，"
              f"所以上面的覆盖率是上界")
    else:
        print("  没有可信配对：覆盖率无法给出（对齐全被判为不可信）")

    # ---- 明细
    if args.out:
        p = pathlib.Path(args.out)
        with p.open("w", encoding="utf-8", newline="") as f:
            f.write("类型\t置信\t人工起点\t我的起点\t偏差ms\t碎片数\t覆盖率\t相似度\t"
                    "人工译文\t我的译文\t我的原文\n")
            for x in sorted(pairs, key=lambda y: y["recall"]):
                f.write(f"配对\t{'可信' if x['trusted'] else '剔除'}\t{x['ref_start']}\t"
                        f"{x['my_start']}\t{x['dt']:+d}\t{x['n_frag']}\t"
                        f"{x['recall']:.3f}\t{x['sim']:.3f}\t"
                        f"{x['ref']}\t{x['my_zh']}\t{x['my_jp']}\n")
            for rs, re_, t in missed:
                f.write(f"漏识\t\t{rs}\t\t\t\t\t\t{t}\t\t\n")
            for ms, me, jp, zh in extra:
                f.write(f"我的\t\t\t{ms}\t\t\t\t\t\t{zh}\t{jp}\n")
        print(f"\n逐句对照已写出：{p}")

    # ---- 最差的可信配对，直接给人看
    shown = trusted if args.dump_all else sorted(trusted, key=lambda y: y["recall"])[:20]
    if shown:
        title = "全部可信配对" if args.dump_all else "内容覆盖率最低的 20 对（优先人工核对）"
        print(f"\n===== {title} =====")
        for x in (shown if args.dump_all else sorted(shown, key=lambda y: y["recall"])):
            flag = "空译文" if x["empty"] else ("" if x["recall"] >= 0.8 else "←查")
            print(f"  [{x['recall']:.2f} {x['dt']:+5d}ms {x['n_frag']}碎片] {flag}")
            print(f"      人工 {x['ref']}")
            print(f"      我的 {x['my_zh']}")
            print(f"      原文 {x['my_jp']}")

    if missed:
        print("\n===== 漏识样例（前 10 条）=====")
        for rs, re_, t in missed[:10]:
            print(f"  {rs/1000:7.1f}s  {t}")

    if undecidable:
        # 单列出来：这些是旧口径下必然进 missed 的"假漏识"，改口径后不计入
        # 漏识的分子与分母。打印全量（通常只有个位数），人工核对时一眼能看完。
        print(f"\n===== 不可判定：人工句过短（<{args.min_overlap_ms}ms，{len(undecidable)} 条）=====")
        print("  这些句子无论识别多完美都凑不够重叠量，既不计命中也不计漏识")
        for rs, re_, t in undecidable:
            print(f"  {rs/1000:7.1f}s ({re_-rs:4d}ms)  {t}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
