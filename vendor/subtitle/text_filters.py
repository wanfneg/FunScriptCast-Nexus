# -*- coding: utf-8 -*-
"""ASR 文本判据的**唯一实现**，PyTorch 与 audiocpp 两条后端共用。

这两个判据此前在 asr_engine.py / audiocpp_backend.py 各写一份，已经出现过
单向漂移（一个修了覆盖率重复计数、另一个还留着旧算法；复读阈值一边 4 连
一边 8 连）。凡是"两边必须一致"的文本判据都放到这里，改一处两侧生效。

另含字幕显示策略的共用小判据（引号清洗 / 假名计数 / 热词回显）。
"""

import re

# 合法长音/省略符号：字幕里连排是正常表达（「えーーーっと」「あーーー」），
# 不能按"死循环退化"误杀；要连到 lenient_limit 次才判退化。
_LENIENT_REPEAT = "ーｰ〜〰…‥·"

_KANA = re.compile(r"[\u3041-\u309f\u30a0-\u30ff]")

# 字幕行首尾的引号类符号：ASR 会带出（实测 …想让你看呢。"），字幕不需要
_WRAP_QUOTES = "「」『』“”\"“”‘’'"


def count_kana(text: str) -> int:
    """统计假名字数（判断"译文里混了多少日文"用）。"""
    return len(_KANA.findall(text or ""))


def strip_wrap_quotes(text: str) -> str:
    """剥掉字幕行首尾的引号类符号（中间的不动）。"""
    return (text or "").strip().strip(_WRAP_QUOTES).strip()


def is_prompt_echo(text: str, prompt: str) -> bool:
    """识别输出是否只是把热词/上下文（prompt）复读了出来。

    ASR 带热词偏置时有概率把热词整段当结果吐出（静音/音乐段尤甚）。
    判据：规范化（去标点、小写）后与 prompt 全等，或是 prompt 的尾部
    子串（热词="悠亚的秘密"，输出="的秘密"）。参考 realtime-subtitle
    的 _is_prompt_echo。prompt 为空恒 False。
    """
    _sym = re.compile(r"[^\w\s]", re.UNICODE)

    def norm(s: str) -> str:
        return _sym.sub("", (s or "").lower()).strip()

    t, p = norm(text), norm(prompt)
    if not t or not p:
        return False
    return t == p or p.endswith(t)


def has_repetition_loop(text: str, max_run: int = 3, lenient_limit: int = 15) -> bool:
    """检测同一字符连续重复过多（小模型的死循环退化）。

    max_run=3：4 个以上同字连排基本可判退化（实测「ああああ気持ちああ」这类
    喘息/拟声退化会污染翻译，译文被放大成几十个「啊」）。但 `ー`/`〜`/`…`
    这类符号的连排是日语的合法拖长音，放宽到 lenient_limit。

    PyTorch 路径旧实现一刀切 4 连即杀，会误杀「えーーーっと」；audiocpp 旧
    实现 8 连才杀、同一个退化两侧宽容度差一倍——现已统一到这里。
    """
    text = text or ""
    run = 1
    for i in range(1, len(text)):
        if text[i] == text[i - 1]:
            run += 1
            limit = lenient_limit if text[i] in _LENIENT_REPEAT else max_run
            if run > limit:
                return True
        else:
            run = 1
    return False


def is_glossary_echo(text: str, keys: list, threshold: float = 0.6) -> bool:
    """判断一段 ASR 输出是否是"把热词表当台词复读"了。

    判据：命中 ≥3 个术语键，且命中字符的**并集**占全文超过 threshold。
    覆盖率必须按并集算——把各命中词长度直接相加，遇到互相包含的键
    （术语表里 `マンコ`/`おマンコ`、`ケツ`/`ケツ穴` 同时存在）会重复计数，
    覆盖率能算出 1.38 这种超过 1 的值，从而误杀正常句子。
    """
    text = text or ""
    if len(text) < 4:
        return False
    hits = [k for k in (keys or []) if k and k in text]
    if len(hits) < 3:
        return False
    covered: set = set()
    for k in hits:
        i = text.find(k)
        while i >= 0:
            covered.update(range(i, i + len(k)))
            i = text.find(k, i + 1)
    return len(covered) / max(1, len(text)) > threshold
