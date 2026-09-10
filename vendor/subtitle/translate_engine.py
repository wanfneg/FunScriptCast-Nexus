# -*- coding: utf-8 -*-
"""翻译引擎：批量 JSON + 键校验纠错 + 分层缓存 + 免费后端兜底。

与旧版（逐条调用）的差别——思路参考 VideoCaptioner（WEIFENG2333/VideoCaptioner）：

  1. **批量**：把 batch_size 条（默认 10）打包成一个 JSON 字典一次请求
     `{"1": "原文", "2": "原文", ...}`，要求模型返回同构字典。
     逐条调用 → 批量调用，LLM 请求数直接降 10 倍。
  2. **键校验 + 纠错循环**：返回的键必须与输入完全一致，缺/多键就把错误
     反馈回去让它重试（最多 max_steps 轮）。这一步能消掉「漏翻整段」。
  3. **分层缓存**：L1 进程内字典 + L2 磁盘（`cache/translate/`）。
     缓存键 = sha256(后端 + 端点 + 模型 + 目标语言 + 本批原文)。
     之所以必须落盘：每个视频都会新建一个 Translator 实例，纯实例级内存缓存
     在真实流程里永远命中不了，等于没开。
  4. **免费兜底**：**某一批** LLM 调用失败就立刻用 Bing/Google 补齐这一批
     （无需 Key、显存 0），保证字幕不整段空白；如果累计失败批数达到
     fallback.after_fail_batches，则判定该后端已挂，后续批次直接走免费后端，
     不再每批都白等一个超时。
  5. **多线程**：批与批之间并行（thread_num）。

对外接口保持 `translate_segments(segs, lang_key)` 不变。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

from free_translators import make_free


def _default_cache_dir() -> Path:
    """翻译缓存目录：<APP_DIR>/cache/translate（vendor/ 与 EXE 同级，故 parents[2]）。

    可用环境变量 NEXUS_CACHE_DIR 覆盖根目录（与 host_server 的字幕缓存同源）。
    """
    env = os.environ.get("NEXUS_CACHE_DIR")
    if env:
        return Path(env) / "translate"
    return Path(__file__).resolve().parents[2] / "cache" / "translate"

SYSTEM = ("你是专业的字幕翻译。译文要口语自然、简洁，符合中文字幕习惯；"
          "不要解释，不要添加原文没有的内容；保持人称和专有名词前后一致。")

BATCH_PROMPT = """把下面 JSON 里的每条文本翻译成简体中文。

要求：
1. 只输出一个 JSON 对象，键与输入完全一致，值是对应译文；
2. 不要输出任何解释、markdown 代码块或多余文字；
3. 必须包含全部 {n} 个键，一个都不能少；
4. 译文口语自然，不要添加原文没有的内容。

输入：
{payload}"""

FIX_PROMPT = ("上一次的输出有问题：{err}\n"
              "请修正后重新输出**完整的** JSON 对象（必须包含全部 {n} 个键）。")

SINGLE_PROMPT = "把下面的文本翻译成简体中文，只输出译文，不要解释：\n{text}"

# 漏译检测用：假名（平假名 + 片假名）、连续拉丁字母
_KANA = re.compile(r"[\u3041-\u309f\u30a0-\u30ff]")
_LATIN = re.compile(r"[A-Za-z]{2,}")

# 缓存版本：流程/判据改动后 +1，让旧的（可能不合格的）译文自动失效。
# 只按「后端+模型+原文」做缓存键是不够的——提示词或判据一变，旧译文就是错的，
# 而它看起来"命中了"，属于最难查的一类 bug。
_CACHE_VERSION = 2

# 提示词指纹：改了 SYSTEM/BATCH/FIX 任何一段，指纹就变，缓存自动失效。
_PROMPT_FP = hashlib.sha256(
    (SYSTEM + BATCH_PROMPT + FIX_PROMPT).encode("utf-8")).hexdigest()[:10]


class BatchPartial(Exception):
    """模型**有响应**，但这一批没完全达标（缺键 / 漏译没修好）。

    两件事必须分开：

      - `partial`：最后一轮里能用的键 → 别扔，直接用。
      - `bad`：明确不合格的键（漏译）→ 只有这几条送去免费兜底，
        不要因为 10 条里有 1 条没翻好就把另外 9 条也退回 Google。

    另外这个异常同时是「后端还活着」的信号：模型既然回了 JSON，就说明
    Ollama/OpenAI 没挂，因此**不能**拿它去累加熔断计数——否则几次内容层面的
    不合格就会误触发熔断，把后面几十批全部推给免费后端（实测掉到 37/65 批）。
    """

    def __init__(self, msg: str, partial: dict = None, bad=None):
        super().__init__(msg)
        self.partial = partial or {}
        self.bad = set(bad or ())


class Translator:
    def __init__(self, cfg: dict, glossary):
        self.cfg = cfg or {}
        self.glossary = glossary
        self.backend = str(self.cfg.get("backend", "ollama")).lower()

        self.batch_size = int(self.cfg.get("batch_size", 10))
        self.max_steps = int(self.cfg.get("max_steps", 3))      # 纠错循环轮数
        self.thread_num = int(self.cfg.get("thread_num", 4))
        self.cache_enabled = bool(self.cfg.get("cache", True))
        self.cache_dir = str(self.cfg.get("cache_dir") or _default_cache_dir())
        self.target = str(self.cfg.get("target_lang", "zh"))

        # 免费兜底
        fb = self.cfg.get("fallback") or {}
        self.fallback_kind = str(fb.get("backend", "bing")) if fb.get("enabled", True) else ""
        self.fallback_after = max(1, int(fb.get("after_fail_batches", 2)))
        self._fallback = None
        self._fail_streak = 0

        self._lock = threading.Lock()
        self._mem: dict = {}
        self.stats = {"batches": 0, "cache_hits": 0, "cache_disk_hits": 0,
                      "fix_rounds": 0, "leak_rounds": 0, "fail_batches": 0,
                      "fail_kinds": {}, "skipped_batches": 0, "partial_batches": 0,
                      "fallback_batches": 0, "fallback_errors": 0,
                      "fallback_error": "", "degraded": False, "segments": 0}

    # ------------------------------------------------------------ 缓存
    def _backend_cfg(self) -> dict:
        return self.cfg.get("openai" if self.backend == "openai" else "ollama") or {}

    def _model_name(self) -> str:
        return str(self._backend_cfg().get("model", ""))

    def _cache_ns(self) -> str:
        """命名空间：缓存版本 + 提示词指纹 + 后端 + 端点 + 模型 + 目标语言。"""
        base = str(self._backend_cfg().get("base_url", "")).rstrip("/")
        return (f"v{_CACHE_VERSION}|{_PROMPT_FP}|{self.backend}|{base}|"
                f"{self._model_name()}|{self.target}")

    def _key(self, texts: list, system: str = "") -> str:
        """缓存键。system 里含着命中到的术语表条目，所以术语表变了键也变。"""
        gl = hashlib.sha256((system or "").encode("utf-8")).hexdigest()[:12]
        raw = (self._cache_ns() + "|" + gl + "\n"
               + json.dumps(texts, ensure_ascii=False))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _cache_path(self, key: str) -> Path:
        # 按前两位分桶，避免单目录堆几万个小文件
        return Path(self.cache_dir) / key[:2] / (key + ".json")

    def _cache_get(self, key: str, n: int):
        """L1 内存 → L2 磁盘。返回与 texts 等长的译文列表，未命中返回 None。"""
        if not self.cache_enabled:
            return None
        with self._lock:
            hit = self._mem.get(key)
        if hit is not None:
            return hit
        try:
            with open(self._cache_path(key), "r", encoding="utf-8") as f:
                val = json.load(f)
        except Exception:
            return None
        # 长度/类型不符说明文件损坏或被截断，当作未命中
        if (not isinstance(val, list) or len(val) != n
                or not all(isinstance(x, str) for x in val)):
            return None
        with self._lock:
            self._mem[key] = val
            self.stats["cache_disk_hits"] += 1
        return val

    def _cache_put(self, key: str, value: list) -> None:
        if not self.cache_enabled:
            return
        with self._lock:
            if len(self._mem) > 5000:
                self._mem.clear()
            self._mem[key] = value
        try:
            p = self._cache_path(key)
            p.parent.mkdir(parents=True, exist_ok=True)
            # 原子写：多线程/多进程同时写同一批时不会读到半个文件
            tmp = p.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(value, f, ensure_ascii=False)
            os.replace(tmp, p)
        except Exception:
            pass  # 缓存写失败不影响翻译结果

    # ------------------------------------------------------------ 后端调用
    def _post(self, url: str, payload: dict, headers: dict | None = None,
              timeout: int = 180) -> dict:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **(headers or {})})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _chat(self, system: str, user: str) -> str:
        """一次 LLM 对话（ollama 或 openai 兼容）。"""
        if self.backend == "openai":
            return self._chat_openai(system, user)
        return self._chat_ollama(system, user)

    def _chat_ollama(self, system: str, user: str) -> str:
        c = self.cfg.get("ollama") or {}
        url = str(c.get("base_url", "http://127.0.0.1:11434")).rstrip("/") + "/api/chat"
        data = self._post(url, {
            "model": c.get("model", "qwen2.5:3b"),
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "stream": False,
            "options": {"temperature": float(c.get("temperature", 0.2)),
                        "num_predict": int(c.get("num_predict", 2048))},
        })
        return (data.get("message", {}).get("content", "") or "").strip()

    def _chat_openai(self, system: str, user: str) -> str:
        c = self.cfg.get("openai") or {}
        key_env = c.get("api_key_env", "")
        key = os.environ.get(key_env, "") if key_env else ""
        if not key:
            raise RuntimeError(f"未设置 API Key（环境变量 {key_env or '?'}）")
        url = str(c.get("base_url", "")).rstrip("/") + "/chat/completions"
        data = self._post(url, {
            "model": c.get("model", "qwen-mt-turbo"),
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": 0.2,
        }, {"Authorization": "Bearer " + key})
        return (data["choices"][0]["message"]["content"] or "").strip()

    # ------------------------------------------------------------ 解析
    @staticmethod
    def _parse_json_dict(raw: str) -> dict | None:
        """从模型输出里抠出 JSON 字典（容忍 markdown 围栏与前后废话）。"""
        if not raw:
            return None
        s = raw.strip()
        # 去掉 ```json ... ``` 围栏
        if s.startswith("```"):
            s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
            s = re.sub(r"\s*```$", "", s)
        try:
            d = json.loads(s)
            return d if isinstance(d, dict) else None
        except Exception:
            pass
        # 退一步：截取第一个 { 到最后一个 }
        i, j = s.find("{"), s.rfind("}")
        if 0 <= i < j:
            try:
                d = json.loads(s[i:j + 1])
                return d if isinstance(d, dict) else None
            except Exception:
                return None
        return None

    def _validate(self, got: dict | None, want_keys: list) -> tuple:
        if not isinstance(got, dict):
            return False, f"输出不是 JSON 对象（实际 {type(got).__name__}）"
        gk = {str(k) for k in got.keys()}
        wk = set(want_keys)
        if gk != wk:
            miss = sorted(wk - gk)
            extra = sorted(gk - wk)
            parts = []
            if miss:
                parts.append("缺少键 " + ",".join(miss[:10]))
            if extra:
                parts.append("多余键 " + ",".join(extra[:10]))
            return False, "；".join(parts)
        return True, ""

    # ------------------------------------------------------------ 术语表
    def _system_with_glossary(self, lang_key: str, texts: list) -> str:
        joined = " ".join(texts)
        hit = self.glossary.match(lang_key, joined) if self.glossary else {}
        if not hit:
            return SYSTEM
        return (SYSTEM + "\n\n术语表（原文→译文，必须严格遵守；未出现的词不要套用）：\n"
                + "\n".join(f"{k}→{v}" for k, v in hit.items()))

    def _has_untranslated(self, src: str, tr: str) -> bool:
        """译文里残留原文 = 模型抄了原文没翻。

        ja→zh 特别容易出这个：模型遇到语气词/短句时直接把日文原样返回
        （实测 `あこれすごい。→ あこれ好厉害。`、`奥まであと。→ 奥まで啊啊。`）。
        中文译文里出现假名一定是漏译，判据很硬，所以能安全地丢回纠错循环。

        另外中文译文里冒出英文单词、而原文没有英文，同样是没翻译干净
        （实测 `ああ、そう。→ 啊啊、 yeah。`）。
        """
        tr = (tr or "").strip()
        if not tr:
            return False
        if str(self.target).lower().startswith("zh") and _KANA.search(tr):
            return True
        return bool(_LATIN.search(tr)) and not _LATIN.search(src or "")

    # ------------------------------------------------------------ 单批翻译
    def _translate_batch(self, texts: list, indices: list, system: str) -> list:
        """翻译一批（含纠错循环）；返回与 texts 等长的译文列表。"""
        keys = [str(i) for i in indices]
        payload = json.dumps({str(i): t for i, t in zip(indices, texts)},
                             ensure_ascii=False)
        user = BATCH_PROMPT.format(n=len(texts), payload=payload)

        last = None
        last_raw = ""
        leak: list = []
        for step in range(max(1, self.max_steps)):
            if step > 0:
                with self._lock:
                    self.stats["fix_rounds"] += 1
            raw = self._chat(system, user)
            last_raw = raw
            got = self._parse_json_dict(raw)
            ok, err = self._validate(got, keys)
            leak = []
            if ok:
                vals = [str(got[k]).strip() for k in keys]
                leak = [k for k, v, t in zip(keys, vals, texts)
                        if self._has_untranslated(t, v)]
                if not leak:
                    return vals
                with self._lock:
                    self.stats["leak_rounds"] += 1
                err = ("以下键的译文还是原文/夹着原文，没有真正翻译成中文："
                       + ",".join(leak[:10]))
            last = (got, err)
            user = user + "\n\n" + FIX_PROMPT.format(err=err, n=len(texts))
        msg = f"批量翻译校验失败：{last[1] if last else '空响应'}"
        if last and isinstance(last[0], dict) and last[0]:
            # 有响应 → 保住能用的，只把 leak 的那几条标为不合格
            raise BatchPartial(msg, last[0], leak)
        # 区分「完全没响应」和「响应不是 JSON」——后者通常是 batch 太大被
        # num_predict 截断，前者才是后端/网络问题，排查方向完全不同。
        if not (last_raw or "").strip():
            raise RuntimeError("批量翻译失败：模型返回空响应")
        raise RuntimeError("批量翻译失败：响应不是合法 JSON（可能被 num_predict 截断）"
                           f"，前 120 字：{(last_raw or '')[:120]!r}")

    # ------------------------------------------------------------ 兜底
    def _get_fallback(self):
        if self._fallback is None and self.fallback_kind:
            self._fallback = make_free(self.fallback_kind)
        return self._fallback

    def _free_translate(self, texts: list) -> list:
        fb = self._get_fallback()
        if fb is None:
            self._note_fallback_error(f"未知的兜底后端 {self.fallback_kind!r}")
            return [""] * len(texts)
        try:
            with self._lock:
                self.stats["fallback_batches"] += 1
            out = fb.translate(texts, self.target)
            if not any(out):
                self._note_fallback_error("兜底返回空译文")
            return out
        except Exception as e:
            self._note_fallback_error(f"{type(e).__name__}: {e}")
            return [""] * len(texts)

    def _note_fallback_error(self, msg: str) -> None:
        """记下兜底失败原因——否则这个异常会被静默吞掉，线上完全查不出。"""
        with self._lock:
            self.stats["fallback_error"] = msg
            self.stats["fallback_errors"] += 1

    # ------------------------------------------------------------ 退化检测
    @staticmethod
    def _is_degenerate(text: str, translation: str) -> bool:
        """译文异常放大：译文 > 原文×3 且同一字符占比 > 60%。"""
        tr = (translation or "").strip()
        if not tr or len(tr) <= max(6, len(text) * 3):
            return False
        top = Counter(tr).most_common(1)[0][1]
        return top / len(tr) > 0.6

    # ------------------------------------------------------------ 主入口
    def translate_segments(self, segs: list, lang_key: str) -> None:
        """就地写入 seg['translation']；失败时留空并记录 seg['error']。"""
        todo = [(i, s) for i, s in enumerate(segs) if (s.get("text") or "").strip()]
        for s in segs:
            if not (s.get("text") or "").strip():
                s["translation"] = ""
        if not todo:
            return

        # 分批
        batches = [todo[i:i + self.batch_size]
                   for i in range(0, len(todo), self.batch_size)]
        with self._lock:
            self.stats["segments"] += len(todo)
            self.stats["batches"] += len(batches)

        results: dict = {}

        def work(batch):
            texts = [(s.get("text") or "").strip() for _, s in batch]
            indices = [i for i, _ in batch]
            # 先算 system：它包含命中到的术语表条目，要一起并进缓存键
            system = self._system_with_glossary(lang_key, texts)
            key = self._key(texts, system)
            cached = self._cache_get(key, len(texts))
            if cached is not None:
                with self._lock:
                    self.stats["cache_hits"] += 1
                return indices, cached
            try:
                with self._lock:
                    streak = self._fail_streak
                if self.fallback_kind and streak >= self.fallback_after:
                    with self._lock:
                        self.stats["skipped_batches"] += 1
                    raise RuntimeError(f"LLM 后端已连续 {streak} 批失败，跳过重试")
                out = self._translate_batch(texts, indices, system)
                self._cache_put(key, out)
                with self._lock:
                    self._fail_streak = 0
                    self.stats["degraded"] = False
                return indices, out
            except Exception as e:
                # 后端还活着？只有"连不上/完全没响应"才计入熔断。
                # 内容层面的不合格（缺键、漏译修不好）说明模型在正常工作，
                # 拿它累加熔断会导致后面几十批全部跳过 LLM。
                alive = isinstance(e, BatchPartial)
                with self._lock:
                    self.stats["fail_batches"] += 1
                    self.stats["fail_kinds"][type(e).__name__] = (
                        self.stats["fail_kinds"].get(type(e).__name__, 0) + 1)
                    if alive:
                        self._fail_streak = 0
                    else:
                        self._fail_streak += 1
                        if self.fallback_kind and self._fail_streak >= self.fallback_after:
                            self.stats["degraded"] = True
                # 保住最后一轮里已经翻好的键，只对缺的/不合格的走免费兜底
                partial = getattr(e, "partial", None) or {}
                bad = getattr(e, "bad", None) or set()
                out = [""] * len(texts)
                missing = []
                for pos, k in enumerate(str(i) for i in indices):
                    v = str(partial.get(k, "") or "").strip()
                    if v and k not in bad:
                        out[pos] = v
                    else:
                        missing.append(pos)
                if missing:
                    filled = self._free_translate([texts[p] for p in missing])
                    for p, v in zip(missing, filled):
                        out[p] = v
                if any(out):
                    if missing and len(missing) < len(texts):
                        with self._lock:
                            self.stats["partial_batches"] += 1
                    return indices, out
                return indices, ["" for _ in texts], f"{type(e).__name__}: {e}"

        if self.thread_num > 1 and len(batches) > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(self.thread_num, len(batches))) as ex:
                for batch_r in ex.map(work, batches):
                    results[batch_r[0][0]] = batch_r
        else:
            for b in batches:
                batch_r = work(b)
                results[batch_r[0][0]] = batch_r

        # 回填
        for first_index, r in results.items():
            indices, out = r[0], r[1]
            err = r[2] if len(r) > 2 else None
            for pos, seg_i in enumerate(indices):
                seg = segs[seg_i]
                tr = out[pos] if pos < len(out) else ""
                src = (seg.get("text") or "").strip()
                seg.pop("error", None)        # 清掉上一次跑留下的陈旧错误
                if tr and self._is_degenerate(src, tr):
                    tr = ""
                    seg["error"] = "translation_degenerate"
                elif not tr and err:
                    seg["error"] = err
                elif tr and self._has_untranslated(src, tr):
                    # 纠错循环没救回来：保留（总比空白强），但标记出来便于排查
                    seg["error"] = "untranslated_leak"
                seg["translation"] = tr

    # ------------------------------------------------------------ 诊断
    def describe(self) -> str:
        cache = f"cache={'disk+mem' if self.cache_enabled else 'off'}"
        return (f"{self.backend}/{self._model_name()} batch={self.batch_size} "
                f"threads={self.thread_num} {cache} fallback={self.fallback_kind or 'off'}")
