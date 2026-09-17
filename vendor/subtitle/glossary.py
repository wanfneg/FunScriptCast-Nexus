# -*- coding: utf-8 -*-
"""统一术语表：一份表同时服务两个阶段

- ASR 阶段：把术语（原文）拼成 context 提示，减少专有名词识别错误
  （实测：小ギャル→コギャル、おさわりカブ→おさわりキャバ 均被纠正）
- 翻译阶段：只把"当前句真正出现的术语"注入 prompt，避免术语被乱套到别的词上

术语表在 PC 侧管理（JSON 文件），**改完文件即时生效**（按 mtime 热加载，无需重启服务）。
头显端不提供术语表管理界面。
"""

import json
import re
import threading
from pathlib import Path

# ------------------------------------------------------------------ ASR 热词挑选
# 术语表按语义分类排列，最前面是人称代词/亲属称谓（私・ぼく・あなた・パパ・お兄さん…）。
# 这些是**最高频**的词，ASR 本来就不会听错；而真正被识别错的低频领域词
# （乳首→弱、おっぱい→ノーパイ、ノーパン→ノーパイ、悠亜→幽）因为排在后面，
# 原来 `list(keys)[:200]` 的取法**一个都进不了提示词**——780 字符全填了代词，等于白填。
#
# 所以这里改成：按"值得偏置程度"排序 + 按字符数封顶。
_RELATION_WORDS = frozenset("""
    私 わたし あたし わたくし あたくし 僕 ぼく 俺 おれ 俺様 自分 あなた あんた
    君 きみ お前 おまえ てめえ 貴様 私たち 僕たち 俺たち 我々 みんな 誰か 誰
    パパ ママ ダディ マミー お父さん お母さん 父さん 母さん
    おじさん おばさん お爺さん お婆さん おじいさん おばあさん
    お兄さん お姉さん 兄貴 姉貴 アニキ こちら そちら あちら どちら
""".split())
# 敬称/复数后缀：带这些的几乎都是称谓或语法词，不是需要偏置的内容词
_RELATION_SUFFIX = ("様", "さん", "ちゃん", "くん", "君", "たち", "達", "ら")
# 补充词条（人名等）的总字符安全上限：它们无条件全量进热词，这里只是兜底
_EXTRA_MAX_CHARS = 300

_KATAKANA = re.compile(r"[\u30a0-\u30ff]")
_KANJI = re.compile(r"[\u4e00-\u9fff]")
_HIRAGANA_ONLY = re.compile(r"[\u3041-\u309f\u30fc]+")


def asr_term_score(term: str) -> float:
    """术语"值得放进 ASR 热词"的分数；负数表示直接剔除。

    判据都是可解释的，不是拍脑袋：
      · **长度 3~4 字的内容词最像"术语"**。太短（1 字）太泛；太长（>12 字）
        是整句——术语表里确实混着 `もっと気持ちよくしてほしい` 这种整句，
        它们当热词没用（ASR 不会因为一整句而改变听感），还会把字符预算吃光。
      · 片假名外来语容易听错（ノーパン/ギャル），略加成。**但权重不能高**：
        早期给 +3.0 时，评分被片假名彻底垄断，900 字符的列表里塞了约 180 个
        片假名词（バイブ/オナニー…），实测把模型直接喂崩——795-840s 窗口
        13 段里 9 段变成空字符串。降到 +1.0 后各词类才有机会竞争。
      · 含汉字加成（`悠亜`/`乳首` 这类短汉字词正是最容易听错又最需要偏置的）
      · 人称代词/亲属称谓（_RELATION_WORDS、敬称后缀）直接剔除
    """
    if not term:
        return -1.0
    n = len(term)
    if n <= 1 or n > 12:
        return -1.0
    if term in _RELATION_WORDS:
        return -1.0
    # 敬称后缀：`あなた様`/`私たち` 要去掉，但 `悠亜様` 要保留——
    # 判据看**词干**是不是称谓词，而不是"以様结尾就扔"。
    for suf in _RELATION_SUFFIX:
        if term.endswith(suf) and len(term) > len(suf):
            if term[:-len(suf)] in _RELATION_WORDS:
                return -1.0
            break
    s = 6.0 - abs(n - 3.5)          # 3~4 字满分，越短/越长越低
    if _KATAKANA.search(term):
        s += 1.0
    if _KANJI.search(term):
        s += 1.0
    return s


class Glossary:
    def __init__(self, paths: dict, base_dir: Path = Path("."), extra: dict | None = None):
        self._paths = {}
        self._maps = {}
        self._mtimes = {}
        # 翻译线程池（thread_num>1）会并发调用 match/keys，热加载在另一线程
        # 换表——所有换表操作走这把锁；查询侧只做引用快照（见各方法）。
        self._lock = threading.RLock()
        # extra：不写进用户的术语表文件、但优先级最高的补充词条。
        # 用途是**人名/作品名**这类"每部片都不一样、模型又必然听错"的词：
        # 实测 `悠亜`（女主名）在全片被错识别成 `幽`/`弱`/`岩`/`いわ`/`みんか`
        # 五种写法，占全部转录错误的 60%；而 2091 条术语表里一个演员名都没有。
        # 走单独通道，既能立刻生效，也不会污染用户维护的词表。
        self._extra: dict = {}
        for lang, rel in (paths or {}).items():
            if lang == "extra":        # 同名键是补充词条，不是词表文件路径
                continue
            fp = Path(rel)
            if not fp.is_absolute():
                fp = base_dir / fp
            self._paths[lang] = fp
            self._maps[lang] = {}
            self._mtimes[lang] = None
        for lang, m in (extra or {}).items():
            self._extra[lang] = dict(m or {})
            if lang not in self._maps:
                self._paths[lang] = Path(f"<extra:{lang}>")
                self._maps[lang] = {}
                self._mtimes[lang] = None
        with self._lock:
            self.reload(force=True)

    def _merge_extra(self) -> None:
        """把补充词条并进各语言表。**换引用而不是就地 update**：就地改会让
        另一线程正在迭代的 dict 抛 "dictionary changed size during iteration"
        （触发点在翻译主流程，一炸整批译文全空）；换引用后旧 dict 对快照
        持有者永远只读。"""
        for lang, m in self._extra.items():
            base = self._maps.get(lang) or {}
            self._maps[lang] = {**base, **m}

    # ------------------------------------------------------------ 热加载
    def reload(self, force: bool = False) -> bool:
        # 调用方持锁（_ensure）；本方法不再自行加锁以保证可重入
        changed = False
        for lang, fp in self._paths.items():
            try:
                mtime = fp.stat().st_mtime_ns if fp.exists() else 0
            except OSError:
                mtime = 0
            if not force and mtime == self._mtimes.get(lang):
                continue
            self._mtimes[lang] = mtime
            if fp.exists():
                try:
                    loaded = json.loads(fp.read_text(encoding="utf-8"))
                    if not isinstance(loaded, dict):
                        loaded = {}
                    print(f"[glossary] {lang}: 加载 {len(loaded)} 条 ← {fp}")
                    self._maps[lang] = loaded
                except Exception as e:
                    print(f"[glossary] {lang}: 解析失败，沿用旧表 ← {fp}: {e}")
            else:
                self._maps[lang] = {}
                print(f"[glossary] {lang}: 文件不存在，术语表为空 ← {fp}")
            changed = True
        # 热加载会把 _maps 整个换掉，所以补充分词条必须在这里重新并回去，
        # 否则"改一次术语表文件，人名偏置就静默失效"——这种 bug 极难发现。
        self._merge_extra()
        return changed

    def _ensure(self):
        with self._lock:
            self.reload(force=False)

    # ------------------------------------------------------------ 查询
    def langs(self):
        return list(self._paths.keys())

    def size(self, lang: str) -> int:
        self._ensure()
        with self._lock:
            return len(self._maps.get(lang, {}))

    def raw(self, lang: str) -> dict:
        self._ensure()
        with self._lock:
            return dict(self._maps.get(lang, {}))

    def asr_context(self, lang: str, max_chars: int = 0) -> str:
        """拼 ASR 热词提示（只返回提示词本身；需要键的用 asr_context_with_keys）。

        **`extra`（人名）无条件全量带上**，`max_chars` 只约束"额外补充的领域词"，
        默认 **0 = 不补**。这个默认值是被实测逼出来的，不是拍脑袋：

          实测 6 个窗口（含 3 个已知被热词搞崩的），同一段音频：

            热词                ASR 耗时   段数  字符   人名正确  复读重试
            关（无热词）         28196ms    39   258     0/6      0
            仅人名 21 字符       38319ms    38   252     2/6      4     ← 甜点
            人名+领域 199 字符   99089ms    38   257     2/6      4

          领域词**一点额外收益都没有**（人名正确率都是 2/6、段数字符基本相同），
          却把耗时从 1.36× 抬到 3.51×。原因见 audiocpp_backend._transcribe_span：
          热词越长，模型越容易整段复读热词表，复读出来的长文本纯属白烧 CPU
          （130 个热词 ≈ 130 token 的无效生成），然后才被重试逻辑纠正。

          另外旧的 `limit=200` 取的是**文件顺序前 200 条**——2091 条里前 200 条
          全是 `私・ぼく・あなた・パパ・ママ` 这类代词（ASR 本来就不会错的词），
          780 字符里一个真正会被听错的领域词都没有，等于白填。
        """
        return self.asr_context_with_keys(lang, max_chars)[0]

    def asr_context_with_keys(self, lang: str, max_chars: int = 0) -> tuple:
        """返回 (热词提示, 真正进入提示词的键列表)。

        **为什么必须能拿到键**：复读判据（text_filters.is_glossary_echo）此前
        拿 `keys()`（整张表 2096 条）当"热词"，而实际进提示词的默认只有 5 个人名
        共 19 字符（context_max_chars=0）——判据与提示词完全对不上，正常台词被
        大面积误判成复读丢弃。判据必须只用这里返回的键。
        """
        self._ensure()
        with self._lock:
            extra_keys = list(self._extra.get(lang, {}).keys())
            maps = self._maps.get(lang, {})
        picked: list = []
        used = 0
        # 人名等补充词条：全量带上，用宽裕的安全上限兜底，不受 max_chars 影响
        for t in extra_keys:
            if t and used + len(t) + 1 <= _EXTRA_MAX_CHARS:
                picked.append(t)
                used += len(t) + 1
        if max_chars <= 0:
            return "、".join(picked), picked
        base = used
        # 分数只算一次：此前排序 key 和循环判断各调一遍 asr_term_score
        scored = sorted(((t, asr_term_score(t)) for t in maps if t),
                        key=lambda kv: -kv[1])
        for t, score in scored:
            if score < 0:
                break                      # 已按分降序，后面全该剔除
            if t in picked:
                continue
            if used + len(t) + 1 - base > max_chars:
                # continue 而不是 break：按分数降序，某个长词放不下不代表后面
                # 的短词也放不下 —— break 会白扔预算（实测 max_chars=100 时
                # 少装 1 个词 / 4 字符）。
                continue
            picked.append(t)
            used += len(t) + 1
        return "、".join(picked), picked

    def keys(self, lang: str) -> list:
        self._ensure()
        # _maps 里的 dict 只被整体换引用、从不就地改，快照迭代是安全的
        with self._lock:
            return list(self._maps.get(lang, {}).keys())

    def match(self, lang: str, text: str) -> dict:
        """只返回当前文本里真正出现的术语。"""
        self._ensure()
        with self._lock:
            snapshot = dict(self._maps.get(lang, {}))
        return {k: v for k, v in snapshot.items() if k in text}


def context_with_keys(glossary, lang: str, max_chars: int = 0) -> tuple:
    """从任意术语表对象取 (热词提示, 真正进了提示词的键)。两条 ASR 后端共用。

    复读判据要求"键 = 实际进提示词的那批"，取法写两份迟早漂移（同一类 bug 已经
    在两个后端之间出现过）。替身/旧版术语表对象只有 asr_context 时降级成空键
    列表——**宁可漏判复读，也不能退回"拿整张术语表当判据"**：那会把正常台词
    成片误杀（见 text_filters.is_glossary_echo）。
    """
    if glossary is None:
        return "", []
    fn = getattr(glossary, "asr_context_with_keys", None)
    if fn is not None:
        ctx, keys = fn(lang, max_chars)
        return (ctx or ""), list(keys or ())
    return (glossary.asr_context(lang, max_chars) or ""), []
