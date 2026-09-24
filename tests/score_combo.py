# -*- coding: utf-8 -*-
"""组合评测评分器：eval_<tag>.json（识别+翻译）× lag_<tag>.json（逐句出字延迟）
× 人工中文字幕（gold .srt）→ 一份组合分数。

stdout 最后一行固定是 `##SCORE## {json}`，同一份 JSON 也写到
score_<tag>.json（默认与 eval 同目录；<tag> 取 --tag，缺省从 eval 文件名解析）。

纯本地计算：不联网、不读缓存、不用随机（--selftest 的打乱用固定种子）。

====================================================================
 指标口径（出处：审计 spec + 复核补充；边界口径逐条写明）
====================================================================

norm(s)（所有文本比较的公共前置，spec §1）
  unicodedata.normalize('NFKC') → 去空白与标点（沿用 compare_with_reference.clean
  的字符类，:129-131）→ Latin 小写。NFKC 顺带完成全角→半角（ＯＫ→ok、１２３→123）
  与半角片假名→全角（ｱ→ア）。假名长音 ー 与浊点不被 NFKC 折叠（日文若引入参考
  文本也不会误折叠）。norm 后为空的文本不参与任何文本命中。

coverage = 识别召回（文本化，spec §2）
  = 文本命中人工句数 / 可判定人工句数。
  · 配对与门槛沿用现状：thr = min(--min-overlap-ms, 人工句长/2)；
    人工句 norm 后 < 4 字（单字 ref 二值化、3 字 ref 有 0.33 底噪）或
    「无命中且句长 < min-overlap」→ 单列「不可判定」，不进分子与分母（复核 §7）。
  · 文本命中 = 存在重叠≥thr 的我的段，其 norm(译文) 与 norm(人工译文) 满足
    char-LCS≥0.3（以人工句长度为分母）或共享 ≥1 个 2-gram。
  · 仅时段命中（时间重叠达标但文本对不上）单列，不进召回分子。

missingCount = 漏识数
  可判定人工句中连时间重叠≥thr 都没有的条数。文本命中/仅时段/漏识三桶互斥，
  其和 = sentenceCount（自测有恒等式断言）。

sentenceCount = 可判定人工句数（召回分母）
  判定外四类全部单列在 detail.gold：窗口外 / norm<4 字 / 句长过短 / （窗口内总数）。

precision（我的段被人工译文佐证的比例）
  分母 = 译文非空 且 与任一「窗口内人工句」时间重叠>0 的我的段。佐证 = 与某条
  重叠人工句满足 char-LCS≥0.3（以**我的段**长度为分母）或共享 ≥1 个 2-gram。
  边界口径：
  · 与人工句零重叠的段（人工字幕只收录对白，语气词/喘息多不收录）不进分母——
    它们既非精确也非不精确，单列 noGoldOverlap（审计 docstring 陷阱 1）；
  · 空译文段没有可佐证的内容，进缺译指标，不进本分母；
  · 幻觉段（见下）若文本上仍与人工句吻合照样算佐证——幻觉率单列，不在这里扣。

幻觉段（spec §3 / 复核 §5，detail.hallucination）
  同一 norm(原文) 全片出现 ≥5 次，或段内存在连续重复 ≥3 次的子串（子串长度上限
  64、只查 norm 后文本）。识别召回/精度不豁免语气词（审计：不再进「多为语气词」
  豁免桶），幻觉率=幻觉段/总段数单列。

时序 timingP50ms / timingP90ms（spec §4）
  对每条有人工命中的句子：start_err = 覆盖段最早 start − 人工 start；
  end_err = 覆盖段最晚 end − 人工 end。headline 取**可信配对**的 |start_err|
  中位 / p90；可信 = cov≥--trust-min-cov 且 时长比≤--trust-max-ratio 且
  碎片数≤--trust-max-frag，其中 cov = 区间并集重叠(裁剪到人工句范围)/人工句长
  并 clamp≤1.0（复核 §13，碎片 cov=1.10 不再可能）。signed 中位、end_err、
  全部配对一套、max 用 max(abs(d))（修 compare_with_reference.py:286 的
  abs(dts[-1]) bug）都在 detail.timing。

lenRatio（spec §6）
  仅可信配对且该对译文非空：ratio = len(norm(各覆盖段译文按重叠比例裁剪到人工句
  时间范围后求和)) / max(1, len(norm(人工)))。headline = p50；p25/50/75/90 与
  异常明细（>2.5 或 <0.4，复核 §10 的示例带）在 detail。ja→zh 正常带的自校准
  需要日文参考文本（本仓库没有），故只给分布 + 我的段 zh/ja 字数比（detail
  .lenRatio.myZhPerJpChar）供人工解读。

lagP50s / lagP90s（spec §5 / 复核 §1-2，单位秒）
  lag 产物逐句 lag = 首次携带该句的块音频末端 − 段 end_ms（run_combo_eval
  .lag_pass 的口径；旧版 measure_emission_lag 产物只有 lags 数组，也兼容）。
  译文为空的句进「未出译」（neverEmitted）计数，不进延迟分布。逐响应
  asr_ms/mt_ms/total_ms 未落盘，协议/处理分量的交叉分解只能在生产侧做，
  评分器无法复核（诚实限制，见下）。

缺译 / 未翻译（spec §7 / 复核 §12、14，detail.defects）
  空译文 / 译文==原文（norm 口径）/ 译文含假名（NFKC 后查 [\u3041-\u309f\u30a0-
  \u30ff]，半角假名经 NFKC 折叠后也能查到）/ 译文夹英文（白名单外的 [A-Za-z]{2,}
  且原文无拉丁字母）各报计数；untranslatedRate = 三类（去重并集）/总段数。
  夹英文白名单：OK/AI 等常见缩写（LATIN_WHITELIST）。

partial 产物（spec §7 / 复核 §4）
  读 eval meta.partial / failed_chunks 与 lag stats.failed_chunks，报告头显式
  告警并写入 detail。**已知限制**：offline_to_json 的 meta 只有失败块计数、没有
  失败块时间区间，漏识分母无法按失败块时段剔除；逐段 error 字段也未落盘，
  空译文无法按「漏译 vs 服务端置空」拆分。这两项要等产物格式补字段后才能在
  评分器侧实现。

CER 不可测（compare_with_reference.py:19 已认）：无日文参考文本；本评分器不造
  日文参考，精度只有上面的译文佐证口径。

====================================================================
 用法
====================================================================
  .venv/Scripts/python.exe tests/score_combo.py \
      --eval E:/Development/_ref/eval/eval_<tag>.json \
      --lag  E:/Development/_ref/eval/lag_<tag>.json \
      --gold "E:/testvideo/SIVR-001-002 Yua Mikami/SIVR-002.srt"
  .venv/Scripts/python.exe tests/score_combo.py --selftest

退出码：0 成功（含自测全过）；1 自测断言失败；2 输入缺失/无法解析/无法定时间窗。
"""
from __future__ import annotations

import argparse
import difflib
import json
import pathlib
import random
import re
import sys
import unicodedata
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
# 复用现有实现，避免口径漂移：SRT 解析、时间重叠、区间并集、clean 的字符类
from compare_with_reference import (  # noqa: E402
    clean, overlap, parse_srt, union_ms)

EVAL_DIR = pathlib.Path("E:/Development/_ref/eval")
DEFAULT_GOLD = pathlib.Path("E:/testvideo/SIVR-001-002 Yua Mikami/SIVR-002.srt")

KANA = re.compile(r"[\u3041-\u309f\u30a0-\u30ff]")      # 与 quality_stats.py:37 同类
LATIN = re.compile(r"[A-Za-z]{2,}")                     # 与 compare_with_reference.py:56 同类
# 夹英文白名单（复核 §12）：常用合法缩写，小写比较
LATIN_WHITELIST = {"ok", "ai", "id", "ip", "app", "cd", "dvd", "tv", "pc", "cpu",
                   "gpu", "vip", "sns", "ng", "kg", "cm", "mm", "km", "pt", "lv",
                   "hp", "mp", "ps", "os", "mv", "pv", "dj", "fans", "live"}

NORM_SHORT = 4                    # norm 后少于该字数的人工句：文本指标不可判定
UNIT_MAX = 64                     # 段内连续重复检测的子串长度上限
REPEAT_CAP = 600                  # 连续重复检测只看 norm 后前 600 字（防御超长段）
LEN_RATIO_ABNORMAL = (0.4, 2.5)   # 正常带（复核 §10 示例带；无日文参考，供人工解读）

SELFTEST_SEED = 20260925          # 固定种子：打乱必须可复现（评分器本体零随机）
SELFTEST_SHUFFLE_DROP = 0.15      # 打乱后 precision 至少要掉的绝对量
SELFTEST_LAG_TOL_S = 0.05         # 整体推后 5s 后 p50/p90 偏差的容差

REQUIRED_KEYS = ["coverage", "precision", "timingP50ms", "timingP90ms",
                 "lenRatio", "lagP50s", "lagP90s", "missingCount", "sentenceCount"]


# ---------------------------------------------------------------- 文本工具
def norm(s: str) -> str:
    """比较前统一（spec §1）：NFKC → 去空白与标点（沿用 clean 字符类）→ 小写。"""
    return clean(unicodedata.normalize("NFKC", s or "")).lower()


def lcs_matched(a: str, b: str) -> int:
    """norm 后两串的最长公共子序列总长（与 compare_with_reference.lcs_recall 同法）。"""
    if not a or not b:
        return 0
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return sum(bl.size for bl in sm.get_matching_blocks())


def bigrams(s: str) -> set:
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else set()


def text_hit(a_n: str, b_n: str, denom: str) -> bool:
    """文本命中判据（spec §2）：char-LCS≥0.3 或共享 ≥1 个 2-gram（都在 norm 后）。

    denom="a"：LCS 以 a 的长度为分母（a=人工译文，召回方向）；
    denom="b"：以 b 为分母（b=我的段译文，精度方向）。
    任一串 norm 后为空 → 不命中（空译文段没有可佐证/可核对的内容）。
    """
    if not a_n or not b_n:
        return False
    if bigrams(a_n) & bigrams(b_n):
        return True
    matched = lcs_matched(a_n, b_n)
    return matched / len(a_n if denom == "a" else b_n) >= 0.3


def has_consecutive_repeat(s: str) -> bool:
    """norm 后文本里是否存在连续出现 ≥3 次的子串（幻觉判据之二，spec §3）。

    子串长度上限 UNIT_MAX、只看前 REPEAT_CAP 字；鸽笼预过滤：重复 3 次的子串里
    每个字符至少出现 3 次，最大字符频次 <3 的段直接排除。
    """
    s = s[:REPEAT_CAP]
    n = len(s)
    if n < 3:
        return False
    if not Counter(s):
        return False
    if max(Counter(s).values()) < 3:
        return False
    for L in range(1, min(UNIT_MAX, n // 3) + 1):
        for i in range(n - 3 * L + 1):
            if s[i:i + L] == s[i + L:i + 2 * L] == s[i + 2 * L:i + 3 * L]:
                return True
    return False


def pct(xs: list, p: float):
    """与 measure_emission_lag.py:95 / compare_with_reference 中位同一口径：
    升序排序后取下标 int(n*p)。空表返回 None。"""
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, int(len(s) * p))]


# ---------------------------------------------------------------- 产物装载
def load_eval(path: pathlib.Path):
    """读 eval JSON → (segments, meta)。兼容裸数组（run_full_with_cache 形状）。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        raw = data.get("segments") or []
        meta = dict(data.get("meta") or {})
    else:
        raw, meta = data, {}
    segs = [{"start_ms": int(s.get("start_ms") or 0),
             "end_ms": int(s.get("end_ms") or 0),
             "text": (s.get("text") or "").strip(),
             "translation": (s.get("translation") or "").strip()}
            for s in raw]
    segs.sort(key=lambda d: (d["start_ms"], d["end_ms"]))
    for s in segs:
        s["n_text"] = norm(s["text"])
        s["n_zh"] = norm(s["translation"])
    return segs, meta


def load_lag(path: pathlib.Path) -> dict:
    """读 lag JSON。两种形状：
    · 新（run_combo_eval.lag_pass）：sentences 逐句含 translation/lag_ms →
      空译文句可识别为「未出译」并从延迟分布剔除；
    · 旧（measure_emission_lag）：只有 lags 数组（ms）→ 无法识别空译文，
      全部进分布（detail.lag.neverEmitted=null 并注明）。
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    stats = dict(data.get("stats") or {}) if isinstance(data, dict) else {}
    sentences = data.get("sentences") if isinstance(data, dict) else None
    if isinstance(sentences, list):
        items = []
        for d in sentences:
            lag_ms = d.get("lag_ms")
            if lag_ms is None and d.get("lag_s") is not None:
                lag_ms = round(float(d["lag_s"]) * 1000)
            items.append({"start_ms": int(d.get("start_ms") or 0),
                          "end_ms": int(d.get("end_ms") or 0),
                          "text": (d.get("text") or "").strip(),
                          "translation": (d.get("translation") or "").strip(),
                          "lag_ms": int(lag_ms or 0)})
        return {"kind": "sentences", "items": items, "stats": stats, "raw": data}
    lags = data.get("lags") if isinstance(data, dict) else None
    if isinstance(lags, list):
        return {"kind": "lags",
                "items": [{"lag_ms": int(x)} for x in lags],
                "stats": stats, "raw": data}
    raise ValueError(f"{path} 里既没有 sentences 也没有 lags，无法算延迟")


def derive_window(segs: list, meta: dict, lag_data: dict):
    """评测时间窗（毫秒）。人工句只在窗内参与判定，窗口外单列（不计漏识）。

    来源优先级（取**并集**，宁可多算漏识也不掩掉边缘块的漏识）：
    · 观测窗 = [最早段 start − pad, 最晚段 end + pad]，pad = meta.chunk_sec 秒
      （失败/空块在边缘时，观测窗会缩水，用一个块的时长补回）；
    · lag 声明窗 = [start_s, start_s+lag_sec]（run_combo_eval 两路推流同起点）。
    两者都拿不到（空识别且 lag 无 start_s）→ ValueError，由调用方退出码 2。
    """
    obs = lw = None
    if segs:
        pad = int(float(meta.get("chunk_sec") or 3) * 1000)
        obs = (min(s["start_ms"] for s in segs) - pad,
               max(s["end_ms"] for s in segs) + pad)
    raw = lag_data.get("raw") or {}
    if raw.get("start_s") is not None and raw.get("lag_sec") is not None:
        lw = (int(float(raw["start_s"]) * 1000),
              int((float(raw["start_s"]) + float(raw["lag_sec"])) * 1000))
    if obs and lw:
        return (min(obs[0], lw[0]), max(obs[1], lw[1])), "观测窗∪lag声明窗"
    if obs:
        return obs, "观测窗(±chunk_sec 补边)"
    if lw:
        return lw, "lag声明窗(识别为空)"
    raise ValueError("空识别且 lag 产物没有 start_s/lag_sec：无法确定评测时间窗")


# ---------------------------------------------------------------- 核心计算
def compute_score(eval_segs: list, eval_meta: dict, lag_data: dict, gold: list, *,
                  min_overlap_ms: int = 300,
                  trust_min_cov: float = 0.5,
                  trust_max_ratio: float = 3.0,
                  trust_max_frag: int = 8) -> dict:
    """纯计算，不做 IO、不用随机。→ {"score": {9 指标}, "detail": {...}}。"""
    if not gold:
        raise ValueError("人工字幕（gold）解析结果为空")
    (w0, w1), wsrc = derive_window(eval_segs, eval_meta, lag_data)

    gold_n = [norm(t) for _, _, t in gold]
    inwin_golds = [(rs, re_, rt, gn) for (rs, re_, rt), gn in zip(gold, gold_n)
                   if overlap(rs, re_, w0, w1) > 0]

    # ---- 逐条人工句分类（spec §2 / 复核 §7）
    g_out, g_short, g_shorttime, g_miss = [], [], [], []
    text_hits, time_hits, pair_recs = [], [], []
    for rs, re_, rt, gn in inwin_golds:
        if len(gn) < NORM_SHORT:
            g_short.append((rs, re_, rt))
            continue
        span = max(1, re_ - rs)
        thr = min(min_overlap_ms, max(1, span // 2))
        hit = [i for i, s in enumerate(eval_segs)
               if overlap(rs, re_, s["start_ms"], s["end_ms"]) >= thr]
        if not hit:
            (g_shorttime if span < min_overlap_ms else g_miss).append((rs, re_, rt))
            continue
        clipped = [(max(eval_segs[i]["start_ms"], rs), min(eval_segs[i]["end_ms"], re_))
                   for i in hit if overlap(rs, re_, eval_segs[i]["start_ms"],
                                           eval_segs[i]["end_ms"]) > 0]
        cov = min(1.0, union_ms(clipped) / span)          # 并集重叠 + clamp（复核 §13）
        best = max(hit, key=lambda i: overlap(rs, re_, eval_segs[i]["start_ms"],
                                              eval_segs[i]["end_ms"]))
        span_ratio = max(1, eval_segs[best]["end_ms"] - eval_segs[best]["start_ms"]) / span
        is_text = any(text_hit(gn, eval_segs[i]["n_zh"], denom="a") for i in hit)
        trusted = (cov >= trust_min_cov and span_ratio <= trust_max_ratio
                   and len(hit) <= trust_max_frag)
        rec = {"rs": rs, "re": re_, "ref": rt, "gn": gn, "hit": hit, "cov": cov,
               "span_ratio": span_ratio, "trusted": trusted,
               "text": is_text, "n_zh": "".join(eval_segs[i]["n_zh"] for i in hit)}
        pair_recs.append(rec)
        (text_hits if is_text else time_hits).append(rec)

    sentence_count = len(pair_recs) + len(g_miss)          # 可判定 = 有命中的 + 漏识
    missing_count = len(g_miss)
    coverage = round(len(text_hits) / sentence_count, 4) if sentence_count else 0.0

    # ---- precision（spec §2 / 复核 §3，方向反转：我的段被人工句佐证）
    p_den = p_sup = p_no_gold = 0
    for s in eval_segs:
        if not s["translation"]:
            continue                                        # 空译文 → 缺译指标，不进分母
        cand = [(gn) for rs, re_, rt, gn in inwin_golds
                if overlap(s["start_ms"], s["end_ms"], rs, re_) > 0]
        if not cand:
            p_no_gold += 1
            continue
        p_den += 1
        if any(text_hit(s["n_zh"], gn, denom="b") for gn in cand):
            p_sup += 1
    precision = round(p_sup / p_den, 4) if p_den else 0.0

    # ---- 幻觉段（spec §3 / 复核 §5）
    rep = Counter(s["n_text"] for s in eval_segs if s["n_text"])
    halluc = [s for s in eval_segs
              if s["n_text"] and (rep[s["n_text"]] >= 5 or has_consecutive_repeat(s["n_text"]))]

    # ---- 缺译 / 未翻译（spec §7 / 复核 §12、14）
    d_empty = d_same = d_kana = d_latin = 0
    for s in eval_segs:
        zh, jp = s["translation"], s["text"]
        if not zh:
            d_empty += 1
            continue
        if s["n_text"] and s["n_zh"] == s["n_text"]:
            d_same += 1
        if KANA.search(unicodedata.normalize("NFKC", zh)):
            d_kana += 1
        toks = [t.lower() for t in LATIN.findall(unicodedata.normalize("NFKC", zh))]
        if toks and not re.search(r"[A-Za-z]", unicodedata.normalize("NFKC", jp)) \
                and any(t not in LATIN_WHITELIST for t in toks):
            d_latin += 1
    n_seg = len(eval_segs)
    untranslated = sum(1 for s in eval_segs
                       if not s["translation"]
                       or (s["n_text"] and s["n_zh"] == s["n_text"])
                       or KANA.search(unicodedata.normalize("NFKC", s["translation"])))

    # ---- 时序（spec §4）：覆盖段最早 start / 最晚 end；max 用 max(abs(d))
    def _timing(errs: list):
        if not errs:
            return None
        abs_ = [abs(e) for e in errs]
        n = len(errs)
        return {"n": n, "medSigned": pct(errs, 0.5), "p90abs": pct(abs_, 0.9),
                "maxAbs": max(abs_),                       # 修 abs(dts[-1]) bug
                "le500Pct": round(100 * sum(1 for e in abs_ if e <= 500) / n, 1),
                "le1000Pct": round(100 * sum(1 for e in abs_ if e <= 1000) / n, 1)}

    def _errs(trusted_only: bool):
        starts, ends = [], []
        for rec in pair_recs:
            if trusted_only and not rec["trusted"]:
                continue
            starts.append(min(eval_segs[i]["start_ms"] for i in rec["hit"]) - rec["rs"])
            ends.append(max(eval_segs[i]["end_ms"] for i in rec["hit"]) - rec["re"])
        return starts, ends

    tr_starts, tr_ends = _errs(True)
    all_starts, all_ends = _errs(False)
    abs_tr = sorted(abs(e) for e in tr_starts)
    timing_p50 = pct(abs_tr, 0.5)
    timing_p90 = pct(abs_tr, 0.9)

    # ---- 长度比（spec §6）：可信且该对译文非空；译文按重叠比例裁剪到人工句范围
    ratios, abnormal = [], []
    for rec in pair_recs:
        if not rec["trusted"] or not rec["n_zh"]:
            continue
        num = 0.0
        for i in rec["hit"]:
            s = eval_segs[i]
            ov = overlap(rec["rs"], rec["re"], s["start_ms"], s["end_ms"])
            if ov > 0:
                num += len(s["n_zh"]) * ov / max(1, s["end_ms"] - s["start_ms"])
        ratio = num / max(1, len(rec["gn"]))
        ratios.append(ratio)
        if ratio > LEN_RATIO_ABNORMAL[1] or ratio < LEN_RATIO_ABNORMAL[0]:
            abnormal.append({"refStart": rec["rs"], "ratio": round(ratio, 3),
                             "ref": rec["ref"][:40],
                             "my": rec["n_zh"][:60]})
    zh_per_jp = pct([len(s["n_zh"]) / max(1, len(s["n_text"]))
                     for s in eval_segs if s["n_text"]], 0.5)

    # ---- 延迟（spec §5 / 复核 §1-2）
    lag_items = lag_data["items"]
    if lag_data["kind"] == "sentences":
        vals = [it["lag_ms"] for it in lag_items if it["translation"]]
        never = sum(1 for it in lag_items if not it["translation"])
    else:                                                    # 旧格式：无法识别空译文
        vals = [it["lag_ms"] for it in lag_items]
        never = None
    def _s(v_ms):                                    # ms → 秒（保留 3 位）
        return round(v_ms / 1000, 3) if v_ms is not None else None

    lag_p50 = _s(pct(vals, 0.5))
    lag_p90 = _s(pct(vals, 0.9))
    lag_max = _s(vals[-1]) if vals else None

    score = {
        "coverage": coverage,
        "precision": precision,
        "timingP50ms": timing_p50,
        "timingP90ms": timing_p90,
        "lenRatio": round(pct(ratios, 0.5), 4) if ratios else None,
        "lagP50s": lag_p50,
        "lagP90s": lag_p90,
        "missingCount": missing_count,
        "sentenceCount": sentence_count,
    }
    detail = {
        "window": {"startMs": w0, "endMs": w1, "source": wsrc},
        "gold": {
            "totalSrt": len(gold), "inWindow": len(inwin_golds),
            "outOfWindow": len(gold) - len(inwin_golds),
            "undecidableShortText": len(g_short),
            "undecidableShortTime": len(g_shorttime),
            "textHits": len(text_hits), "timeOnlyHits": len(time_hits),
            "missed": missing_count,
            "shortTextSamples": [{"refStart": rs, "ref": rt} for rs, re_, rt in g_short[:10]],
            "missedSamples": [{"refStart": rs, "ref": rt} for rs, re_, rt in g_miss[:10]],
        },
        "precision": {"denominator": p_den, "supported": p_sup,
                      "noGoldOverlap": p_no_gold},
        "hallucination": {"count": len(halluc),
                          "rate": round(len(halluc) / n_seg, 4) if n_seg else 0.0,
                          "samples": [{"startMs": s["start_ms"], "text": s["text"][:40]}
                                      for s in halluc[:5]]},
        "defects": {"segments": n_seg, "emptyZh": d_empty, "identicalZh": d_same,
                    "kanaZh": d_kana, "latinLeak": d_latin,
                    "untranslatedRate": round(untranslated / n_seg, 4) if n_seg else 0.0},
        "timing": {"pairsTrusted": len(tr_starts), "pairsAll": len(all_starts),
                   "startErrTrusted": _timing(tr_starts),
                   "endErrTrusted": _timing(tr_ends),
                   "startErrAll": _timing(all_starts),
                   "endErrAll": _timing(all_ends)},
        "lenRatio": {"pairs": len(ratios),
                     "p25": round(pct(ratios, 0.25), 3) if ratios else None,
                     "p50": round(pct(ratios, 0.5), 3) if ratios else None,
                     "p75": round(pct(ratios, 0.75), 3) if ratios else None,
                     "p90": round(pct(ratios, 0.9), 3) if ratios else None,
                     "abnormalBand": list(LEN_RATIO_ABNORMAL),
                     "myZhPerJpChar": round(zh_per_jp, 3) if zh_per_jp is not None else None,
                     "abnormal": abnormal[:10]},
        "lag": {"source": lag_data["kind"], "n": len(vals), "neverEmitted": never,
                "maxS": lag_max,
                "le1sPct": round(100 * sum(1 for v in vals if v <= 1000) / len(vals), 1) if vals else None,
                "le2sPct": round(100 * sum(1 for v in vals if v <= 2000) / len(vals), 1) if vals else None,
                "failedChunks": lag_data["stats"].get("failed_chunks"),
                "startS": (lag_data.get("raw") or {}).get("start_s"),
                "lagSec": (lag_data.get("raw") or {}).get("lag_sec"),
                "note": ("译文为空的句进 neverEmitted，不进分布"
                         if lag_data["kind"] == "sentences"
                         else "旧格式 lags 数组：无法识别空译文，全部进分布")},
        "artifact": {"partial": bool(eval_meta.get("partial")),
                     "failedChunks": eval_meta.get("failed_chunks"),
                     "evalMeta": {k: eval_meta.get(k) for k in
                                  ("chunk_sec", "overlap_sec", "chunks", "path",
                                   "failed_chunks", "http_errors", "bad_json",
                                   "server_errors", "partial")},
                     "limits": ["meta 只有失败块计数、无失败块时间区间：漏识分母无法按"
                                "失败块时段剔除（partial 时见报告头告警）",
                                "逐段 error 字段未落盘：空译文无法按漏译/服务端置空拆分",
                                "无日文参考：CER 不可测；lenRatio 正常带未经 ja→zh 自校准"]},
    }
    return {"score": score, "detail": detail}


# ---------------------------------------------------------------- 报告
def build_report(res: dict, eval_name: str, lag_name: str, gold_name: str) -> list:
    s, d = res["score"], res["detail"]
    g, t, lf = d["gold"], d["timing"], d["lag"]
    lines = [
        "=" * 64,
        f"组合评分  {eval_name} + {lag_name}  vs  {gold_name}",
        "=" * 64,
    ]
    if d["artifact"]["partial"]:
        lines.append(f"⚠ partial 产物：eval meta.failed_chunks="
                     f"{d['artifact']['failedChunks']}，指标被低估"
                     "（失败块时段无法从产物定位，漏识分母未剔除）")
    lines.append(f"时间窗 {d['window']['startMs']}–{d['window']['endMs']}ms"
                 f"（{d['window']['source']}）")
    lines.append(f"人工句：窗口内 {g['inWindow']} 条 → 文本命中 {g['textHits']}"
                 f" · 仅时段 {g['timeOnlyHits']} · 漏识 {g['missed']}"
                 f" ｜ 不可判定：norm<4字 {g['undecidableShortText']}"
                 f" · 句长过短 {g['undecidableShortTime']} ｜ 窗口外 {g['outOfWindow']}")
    lines.append("―― ① 覆盖 / 精度 ――")
    lines.append(f"  coverage（识别召回=文本命中/可判定）  {s['coverage']:.4f}"
                 f"  ({g['textHits']}/{s['sentenceCount']})")
    pr = d["precision"]
    lines.append(f"  precision（我的段被人工译文佐证）    {s['precision']:.4f}"
                 f"  ({pr['supported']}/{pr['denominator']}；"
                 f"与人工句零重叠 {pr['noGoldOverlap']} 段不进分母)")
    de = d["defects"]
    lines.append(f"  幻觉段 {d['hallucination']['count']}/{de['segments']}"
                 f"（率 {d['hallucination']['rate']:.3f}）· "
                 f"未翻译率 {de['untranslatedRate']:.3f}"
                 f"（空 {de['emptyZh']} / 等于原文 {de['identicalZh']} / "
                 f"含假名 {de['kanaZh']} / 夹英文 {de['latinLeak']}）")
    lines.append("―― ② 时序（可信配对）――")
    if t["startErrTrusted"]:
        for label, key in (("start_err", "startErrTrusted"), ("end_err", "endErrTrusted")):
            e = t[key]
            lines.append(f"  {label:8s} 中位 {e['medSigned']:+d}ms  |p90 {e['p90abs']}ms"
                         f"  max {e['maxAbs']}ms  ≤500ms {e['le500Pct']}%"
                         f"  ≤1000ms {e['le1000Pct']}%  （{e['n']} 对）")
    else:
        lines.append("  没有可信配对：时序不可给（全部对齐被判不可信）")
    if t["pairsAll"] != t["pairsTrusted"] and t["startErrAll"]:
        e = t["startErrAll"]
        lines.append(f"  start_err 全部配对照：中位 {e['medSigned']:+d}ms"
                     f"  |p90 {e['p90abs']}ms  max {e['maxAbs']}ms（{e['n']} 对，含对齐不可信）")
    lines.append("―― ③ 长度比（可信且译文非空）――")
    lr = d["lenRatio"]
    if lr["pairs"]:
        lines.append(f"  p25 {lr['p25']}  p50 {lr['p50']}  p75 {lr['p75']}  p90 {lr['p90']}"
                     f"  （{lr['pairs']} 对；异常带 {tuple(lr['abnormalBand'])}，"
                     f"异常 {len(lr['abnormal'])} 条；我的段 zh/ja 字数比 {lr['myZhPerJpChar']}）")
    else:
        lines.append("  没有可信且译文非空的配对：长度比不可给")
    lines.append("―― ④ 出字延迟 ――")
    if s["lagP50s"] is not None:
        lines.append(f"  p50 {s['lagP50s']}s  p90 {s['lagP90s']}s  max {lf['maxS']}s"
                     f"  ≤1s {lf['le1sPct']}%  ≤2s {lf['le2sPct']}%"
                     f"  （{lf['n']} 句；未出译 {lf['neverEmitted']}；"
                     f"失败块 {lf['failedChunks']}）")
    else:
        lines.append("  延迟不可给：lag 产物里没有可用的句级数值")
    return lines


# ---------------------------------------------------------------- 自测
def _load_defaults(args):
    eval_p = pathlib.Path(args.eval) if args.eval else EVAL_DIR / "eval_smoke.json"
    lag_p = pathlib.Path(args.lag) if args.lag else EVAL_DIR / "lag_smoke.json"
    gold_p = pathlib.Path(args.gold) if args.gold else DEFAULT_GOLD
    return eval_p, lag_p, gold_p


def run_selftest(args) -> int:
    """冒烟自测（审计要求 ①②③ + 边界口径验证）。全部断言可复现（固定种子）。"""
    eval_p, lag_p, gold_p = _load_defaults(args)
    print(f"[selftest] eval={eval_p}\n[selftest] lag ={lag_p}\n[selftest] gold={gold_p}")
    gold = parse_srt(gold_p)
    segs, meta = load_eval(eval_p)
    lag = load_lag(lag_p)
    checks: list = []

    def check(name: str, ok: bool, msg: str = "") -> None:
        checks.append(ok)
        print(f"[selftest] {'PASS' if ok else 'FAIL'}  {name}" + (f"  {msg}" if msg else ""))

    # ① 指标能算出来：9 个必需键齐全、类型/范围合理
    res = compute_score(segs, meta, lag, gold)
    sc = res["score"]
    need = [k for k in REQUIRED_KEYS if sc.get(k) is None]
    check("① 指标齐全非空", not need, f"缺失: {need}" if need else f"{len(sc)} 项全部可算")
    check("① 取值范围", (0.0 <= sc["coverage"] <= 1.0 and 0.0 <= sc["precision"] <= 1.0
                          and sc["sentenceCount"] > 0 and sc["lagP50s"] > 0
                          and sc["timingP50ms"] is not None),
          f"coverage={sc['coverage']} precision={sc['precision']} "
          f"sentenceCount={sc['sentenceCount']} lagP50s={sc['lagP50s']} "
          f"timingP50ms={sc['timingP50ms']}")

    # ②a 打乱识别文本（整段内容在段间置换，时间不动）→ precision 应显著下降
    rng = random.Random(SELFTEST_SEED)
    contents = [(s["text"], s["translation"]) for s in segs]
    rng.shuffle(contents)
    segs_shuf = [dict(s, text=t, translation=z) for s, (t, z) in zip(segs, contents)]
    for s in segs_shuf:
        s["n_text"] = norm(s["text"])
        s["n_zh"] = norm(s["translation"])
    prec0 = sc["precision"]
    prec1 = compute_score(segs_shuf, meta, lag, gold)["score"]["precision"]
    check("②a 打乱后 precision 显著下降",
          prec0 - prec1 >= SELFTEST_SHUFFLE_DROP,
          f"{prec0:.4f} → {prec1:.4f}（降幅 {prec0 - prec1:.4f}，"
          f"阈值 ≥{SELFTEST_SHUFFLE_DROP}）")

    # ②b 延迟时间戳整体推后 5s → lagP50/lagP90 应各增加约 5s
    lag_shift = {"kind": lag["kind"], "stats": lag["stats"], "raw": lag["raw"],
                 "items": [dict(it, lag_ms=it["lag_ms"] + 5000) for it in lag["items"]]}
    sc2 = compute_score(segs, meta, lag_shift, gold)["score"]
    d50 = sc2["lagP50s"] - sc["lagP50s"]
    d90 = sc2["lagP90s"] - sc["lagP90s"]
    check("②b 延迟+5s 后 p50 增约 5s", abs(d50 - 5.0) <= SELFTEST_LAG_TOL_S,
          f"p50 {sc['lagP50s']} → {sc2['lagP50s']}（Δ={d50:+.3f}s）")
    check("②b 延迟+5s 后 p90 增约 5s", abs(d90 - 5.0) <= SELFTEST_LAG_TOL_S,
          f"p90 {sc['lagP90s']} → {sc2['lagP90s']}（Δ={d90:+.3f}s）")

    # ③ 对齐不到的金标准句按审计口径处理：三桶互斥恒等 + 不可判定不进分母
    g = res["detail"]["gold"]
    check("③ 命中三桶互斥且和=可判定数",
          g["textHits"] + g["timeOnlyHits"] + g["missed"] == sc["sentenceCount"],
          f"{g['textHits']}+{g['timeOnlyHits']}+{g['missed']} = {sc['sentenceCount']}")
    check("③ 不可判定不进分母",
          sc["sentenceCount"] + g["undecidableShortText"] + g["undecidableShortTime"]
          == g["inWindow"],
          f"{sc['sentenceCount']}+{g['undecidableShortText']}"
          f"+{g['undecidableShortTime']} = {g['inWindow']}")
    check("③ 短句单列生效（smoke 里 norm<4 字的人工句 ≥1）",
          g["undecidableShortText"] >= 1,
          f"norm<4字 {g['undecidableShortText']} 条："
          f"{[x['ref'] for x in g['shortTextSamples']]}")

    # 边界：空识别（零段）——不崩，全部可判定人工句算漏识，覆盖率 0
    # （lag 声明窗 80–120s 提供时间窗：空识别时唯一可用的窗口来源）
    res_e = compute_score([], {}, {"kind": "sentences", "items": [], "stats": {},
                                   "raw": {"start_s": 80, "lag_sec": 40}},
                          [(90000, 92000, "这是一句足够长的台词")])
    sc_e = res_e["score"]
    check("边界 空识别不崩且全算漏识",
          sc_e["sentenceCount"] == 1 and sc_e["missingCount"] == 1
          and sc_e["coverage"] == 0.0 and sc_e["precision"] == 0.0,
          f"sentenceCount={sc_e['sentenceCount']} missing={sc_e['missingCount']} "
          f"coverage={sc_e['coverage']} precision={sc_e['precision']}")

    # 边界：空识别且无任何窗口来源 → 必须显式报错（而不是静默给全 0 分）
    try:
        compute_score([], {}, {"kind": "sentences", "items": [], "stats": {}},
                      [(90000, 92000, "这是一句足够长的台词")])
        no_win = False
    except ValueError:
        no_win = True
    check("边界 无窗口来源显式报错", no_win, "空识别+lag 无 start_s → ValueError")

    # 边界：空译文段——进缺译、只算仅时段命中、不进 precision 分母
    seg_et = [{"start_ms": 90000, "end_ms": 91500, "text": "セリフです",
               "translation": "", "n_text": norm("セリフです"), "n_zh": ""}]
    res_z = compute_score(seg_et, {}, {"kind": "sentences", "items": [], "stats": {}},
                          [(90000, 92000, "这是一句足够长的台词")])
    sc_z = res_z["score"]
    check("边界 空译文段进缺译不进精度分母",
          sc_z["coverage"] == 0.0 and sc_z["precision"] == 0.0
          and res_z["detail"]["defects"]["emptyZh"] == 1
          and res_z["detail"]["precision"]["denominator"] == 0
          and res_z["detail"]["gold"]["timeOnlyHits"] == 1,
          f"coverage={sc_z['coverage']} emptyZh=1 precisionDenom=0 timeOnly=1")

    ok = all(checks)
    print(f"[selftest] {'全部通过' if ok else '存在失败'}（{sum(checks)}/{len(checks)}）")
    return 0 if ok else 1


# ---------------------------------------------------------------- 主流程
def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--eval", default="", help="eval_<tag>.json（识别+翻译产物）")
    ap.add_argument("--lag", default="", help="lag_<tag>.json（逐句出字延迟产物）")
    ap.add_argument("--gold", default="", help="人工中文字幕 .srt")
    ap.add_argument("--tag", default="", help="分数文件 tag（缺省从 eval 文件名解析）")
    ap.add_argument("--out", default="", help="分数 JSON 路径（缺省 eval 同目录 score_<tag>.json）")
    ap.add_argument("--min-overlap-ms", type=int, default=300)
    ap.add_argument("--trust-min-cov", type=float, default=0.5)
    ap.add_argument("--trust-max-ratio", type=float, default=3.0)
    ap.add_argument("--trust-max-frag", type=int, default=8)
    ap.add_argument("--selftest", action="store_true",
                    help="用冒烟产物（eval_smoke/lag_smoke）跑内置断言后照常评分")
    args = ap.parse_args()

    if args.selftest:
        rc = run_selftest(args)
        if rc != 0:
            return rc

    # --selftest 不带 --eval/--lag 时，正式评分沿用同一组冒烟产物
    if args.selftest and (not args.eval or not args.lag):
        eval_d, lag_d, _ = _load_defaults(args)
        args.eval = args.eval or str(eval_d)
        args.lag = args.lag or str(lag_d)
    if not args.eval or not args.lag:
        print("--eval 与 --lag 必填（--selftest 也不例外，缺省取冒烟产物）", file=sys.stderr)
        return 2
    eval_p, lag_p, gold_p = _load_defaults(args)
    for p in (eval_p, lag_p, gold_p):
        if not p.exists():
            print(f"找不到输入：{p}", file=sys.stderr)
            return 2
    try:
        gold = parse_srt(gold_p)
        segs, meta = load_eval(eval_p)
        lag = load_lag(lag_p)
        res = compute_score(segs, meta, lag, gold,
                            min_overlap_ms=args.min_overlap_ms,
                            trust_min_cov=args.trust_min_cov,
                            trust_max_ratio=args.trust_max_ratio,
                            trust_max_frag=args.trust_max_frag)
    except (ValueError, json.JSONDecodeError, OSError) as e:
        print(f"评分失败：{type(e).__name__}: {e}", file=sys.stderr)
        return 2

    tag = args.tag
    if not tag:
        m = re.match(r"eval_(.+)", eval_p.stem)
        tag = m.group(1) if m else eval_p.stem
    out = pathlib.Path(args.out) if args.out else eval_p.parent / f"score_{tag}.json"
    payload = {"tag": tag, "eval": str(eval_p), "lag": str(lag_p), "gold": str(gold_p),
               **res}
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    for line in build_report(res, eval_p.name, lag_p.name, gold_p.name):
        print(line)
    print(f"分数已写出：{out}")
    # 最后一行固定 ##SCORE##（机器可读；run_eval/回归门槛按行抓取）
    print("##SCORE## " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
