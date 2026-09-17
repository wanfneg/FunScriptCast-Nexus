# -*- coding: utf-8 -*-
"""ASR 文本判据的**唯一实现**，PyTorch 与 audiocpp 两条后端共用。

这两个判据此前在 asr_engine.py / audiocpp_backend.py 各写一份，已经出现过
单向漂移（一个修了覆盖率重复计数、另一个还留着旧算法；复读阈值一边 4 连
一边 8 连；跨块去重容差只修了 audiocpp 一侧）。凡是"两边必须一致"的判据都
放到这里，改一处两侧生效。

另含字幕显示策略的共用小判据（引号清洗 / 假名计数 / 热词回显 / token 拼接）。
"""

import re

# 合法长音/省略符号：字幕里连排是正常表达（「えーーーっと」「あーーー」），
# 不能按"死循环退化"误杀；要连到 lenient_limit 次才判退化。
_LENIENT_REPEAT = "ーｰ〜〰…‥·"

_KANA = re.compile(r"[\u3041-\u309f\u30a0-\u30ff]")

# 日语识别里"有没有日文字符"的判据用（假名 + 汉字），见 is_latin_hallucination
_JA_CHARS = re.compile(r"[\u3041-\u309f\u30a0-\u30ff\u4e00-\u9fff]")
# 判为"拉丁幻觉"的输出长度上限：长段纯英文属于另一类问题，不在这里一刀切
_LATIN_HALLUCINATION_MAX_CHARS = 16

# 字幕行首尾的引号类符号：ASR 会带出（实测 …想让你看呢。"），字幕不需要
_WRAP_QUOTES = "「」『』“”\"“”‘’'"

# 复读判据里"算一个命中"的键最短长度。术语表里 1~2 字的键（乳/首/手/私/男、
# 悠亜/ゆあ…）在几乎所有日文句里都能"命中"，并且键之间互相包含（乳首 vs 乳、
# 手を繋いで vs 手），靠它们几乎必然凑够"≥3 个命中"的前置条件，判据随即失效。
_ECHO_KEY_MIN_LEN = 3

# 判定为热词复读所需的覆盖率：输出必须**近乎整段**就是热词表本身。
# 旧阈值 0.6 的本意是"含不少热词"，但在领域词密集的台词上必然饱和。
_ECHO_COVERAGE = 0.95

# is_prompt_echo 的长度下限：短输出与 prompt 同尾是**正常对话**（"そうですね"
# 之后的"ね"、"そうだね悠亜"之后的"悠亜"），不是回显。
_ECHO_MIN_CHARS = 4

# 标点/分隔符：只用来算"正文覆盖率"（整段是热词表时，多余的字只有顿号）
_NON_BODY = re.compile(r"[\s、。，,.!！?？…‥・「」『』（）()\[\]【】ー〜~\-—:：;；]+")


def count_kana(text: str) -> int:
    """统计假名字数（判断"译文里混了多少日文"用）。"""
    return len(_KANA.findall(text or ""))


def strip_wrap_quotes(text: str) -> str:
    """剥掉字幕行首尾的引号类符号（中间的不动）。"""
    return (text or "").strip().strip(_WRAP_QUOTES).strip()


def is_latin_hallucination(text: str, lang_key: str = "ja") -> bool:
    """日语音频里输出"一个日文字符都没有"的**短**文本 → 判为 Whisper 系幻觉。

    实测来源（R44 整片 A/B，SIVR-001，`tests/whisper_scheme.py`）：Whisper 在日语
    短块/噪声上会退化成吐"字幕腔"的常见英文词，本片实测到 `.` / `Thank`×2 /
    `I` / `you` / `2`×2 共 7 段**直接当台词上屏**；而**关掉 initial_prompt 回传后
    这 7 段一个都不出现**（同一音频、同一模型）⇒ 是 prompt 把解码器往字幕腔上带。

    为什么"没有日文字符"就足以判：日语里的外来语会被写成片假名（オーケー），
    **不会写成拉丁字母**，所以整段既无假名也无汉字的短输出，在日语识别里不可能是
    真实台词。三条限制避免误杀：
      · 只对 ja 生效（en/ko 源语言本来就该是拉丁字母）
      · 只要含任意假名/汉字就放行 —— 正常日语一律放行
      · 只对**短**输出下手（≤16 字符）：长段纯英文是"模型串到英文"的另一类问题，
        宁可放行也不要在这里一刀切
    """
    t = (text or "").strip()
    if not t or not str(lang_key or "").startswith("ja"):
        return False
    if _JA_CHARS.search(t):
        return False
    return len(t) <= _LATIN_HALLUCINATION_MAX_CHARS


def join_tokens(parts):
    """拼接 ASR token：中日文直接相连，英文/数字之间补空格。

    从 asr_engine 下沉到这里（两条后端都要用：PyTorch 分句、audiocpp 合并短句）。
    直接相加在英文上会粘成一坨（"hello"+"world" → "helloworld"），
    而中日文之间**不能**加空格（加了字幕会多出空格）。
    """
    out = ""
    for t in parts:
        if not t:
            continue
        if out and out[-1].isascii() and out[-1].isalnum() and t[0].isascii() and t[0].isalnum():
            out += " "
        out += t
    return out


def keep_segment(start_ms: int, end_ms: int, keep_from_ms: int) -> bool:
    """重叠区去重：该段是否保留（PyTorch / audiocpp 共用同一判据）。

    判据：只丢"整句基本都落在重叠区"的段 —— 起点在保留区之后，**或者**
    句尾已经越过保留区 300ms 以上。

    旧判据 `start_ms < keep_from_ms 即丢` 会让跨块长句在两个块里都被扔掉：
    句子横跨块 A 尾/块 B 头时，两边的 start 都早于各自的 keep_from
    （audiocpp 侧实测 192.8s 整句消失）。容差 300ms 是留给 VAD 边界的抖动。
    """
    if not keep_from_ms:
        return True
    return start_ms >= keep_from_ms or end_ms > keep_from_ms + 300


def is_prompt_echo(text: str, prompt: str) -> bool:
    """识别输出是否只是把热词/上下文（prompt）复读了出来。

    ASR 带热词偏置时有概率把热词整段当结果吐出（静音/音乐段尤甚）。
    判据：规范化（去标点、小写）后与 prompt 全等，或是 prompt 的尾部
    子串（热词="悠亚的秘密"，输出="的秘密"）。参考 realtime-subtitle
    的 _is_prompt_echo。prompt 为空恒 False。

    **必须有长度下限**：热词里带着上一句原文（每块最多 60 字），而
    `p.endswith(t)` 对任意长度成立，"そうですね" → "ね"、"そうだね悠亜" →
    "悠亜"（项目最在意的女主名）这类**正常对话**全被判成回显；调用方随即
    改用无热词请求重试——那次请求根本没看到这段 prompt，重试救不回来，
    整句直接消失。只有"近乎整段复读"（≥4 字且不短于 prompt 的一半）才算回显。
    """
    _sym = re.compile(r"[^\w\s]", re.UNICODE)

    def norm(s: str) -> str:
        return _sym.sub("", (s or "").lower()).strip()

    t, p = norm(text), norm(prompt)
    if not t or not p:
        return False
    if len(t) < _ECHO_MIN_CHARS or len(t) < 0.5 * len(p):
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


def is_glossary_echo(text: str, keys: list, threshold: float = _ECHO_COVERAGE) -> bool:
    """判断一段 ASR 输出是否是"把热词表当台词复读"了。

    ⚠️ `keys` 必须是**真正进了提示词的那批键**（glossary.asr_context_with_keys）。
    旧实现传的是整张术语表：默认配置（context_max_chars=0）只有 5 个人名共
    19 字符进提示词，判据却拿 2096 个键去衡量，键之间还互相包含（乳首/乳、
    手を繋いで/手、整句键），并集覆盖率必然饱和到 0.9 —— 于是
    「おっぱいが気持ちいい」0.90、「乳首が痛い、やめて」0.78、
    「もっと気持ちよくしてほしい」1.00、「手を繋いでいい」1.00 全被判复读丢弃
    （audiocpp 侧还会因此做一次无热词重试，再判一次必然又为真 → 整句消失）。

    判据（三条同时成立才算复读）：
      1. ≥3 个 **≥_ECHO_KEY_MIN_LEN 字**的键命中 —— 1~2 字键无判别力（见常量注释）；
      2. 输出本身不是一条术语 —— 表里混着 `もっと気持ちよくしてほしい` 这类
         整句键，正常台词正好等于它，那时"覆盖率 100%"毫无意义；
      3. 命中字符的**并集**占正文（剔掉标点/顿号）的比例 > threshold。
    覆盖率必须按并集算——把各命中词长度直接相加，遇到互相包含的键
    （`マンコ`/`おマンコ`、`ケツ`/`ケツ穴`）会重复计数，覆盖率能算出 1.38 这种
    超过 1 的值；正文口径则让"整段就是热词表 + 顿号"仍能被识别。

    命中数只看长键，覆盖率却把短键也算进去：前置条件 1 已经确认这是"一串独立
    长术语"，此时 2 字键（`悠亜`/`ゆあ`）是真实证据 —— 复读人名时全靠它们把
    "整段就是热词表"补齐。
    """
    text = (text or "").strip()
    if len(text) < 4:
        return False
    keyset = {k for k in (keys or []) if k}
    if text in keyset:
        return False
    hits = [k for k in keyset if len(k) >= _ECHO_KEY_MIN_LEN and k in text]
    if len(hits) < 3:
        return False
    covered: set = set()
    for k in keyset:
        i = text.find(k)
        while i >= 0:
            covered.update(range(i, i + len(k)))
            i = text.find(k, i + 1)
    body = {i for i, ch in enumerate(text) if not _NON_BODY.match(ch)}
    if not body:
        return False
    return len(covered & body) / len(body) > threshold
