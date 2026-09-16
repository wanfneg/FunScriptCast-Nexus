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

# 最后一轮不累加错误说明，改用干净提示词重来——原因见 _translate_batch 里的注释。
# ⚠️ 不要在这里描述键的取值范围：work() 传进来的键是**全局段索引**（第 2 批是
# "10".."19"），任何写死 "0 到 {last}" 的提示词都会跟 payload 自相矛盾，
# 模型听提示词返回 0..9 键 → _validate 判缺键 → 末轮翻盘机会被亲手毁掉。
RETRY_PROMPT = ("注意：只输出一个 JSON 对象，键与输入 JSON 里的键完全一致，"
                "值是对应的简体中文译文。对象前后不要有任何其他文字。")

SINGLE_PROMPT = "把下面的文本翻译成简体中文，只输出译文，不要解释：\n{text}"

# 漏译检测用：假名（平假名 + 片假名）、连续拉丁字母
_KANA = re.compile(r"[\u3041-\u309f\u30a0-\u30ff]")
_LATIN = re.compile(r"[A-Za-z]{2,}")

# 缓存版本：流程/判据改动后 +1，让旧的（可能不合格的）译文自动失效。
# 只按「后端+模型+原文」做缓存键是不够的——提示词或判据一变，旧译文就是错的，
# 而它看起来"命中了"，属于最难查的一类 bug。
#   v3：加 JSON 归一化 + 术语表修补 + 历史最好一轮保全（v2 缓存里的空白译文必须作废）
#   v4：空译文也算不合格（v3 把 `{"0":""}` 当合格存进了缓存，必须作废）
_CACHE_VERSION = 4

# 提示词指纹：改了 SYSTEM/BATCH/FIX/RETRY 任何一段，指纹就变，缓存自动失效。
_PROMPT_FP = hashlib.sha256(
    (SYSTEM + BATCH_PROMPT + FIX_PROMPT + RETRY_PROMPT).encode("utf-8")).hexdigest()[:10]

# ---------------------------------------------------------------- JSON 归一化
# 小模型（qwen2.5:3b）经常输出"中文式 JSON"：引号是全角/弯引号，分隔符是全角逗号。
# 内容其实完全正确，但 json.loads 直接判死。实测：
#   {"0": "米粒呢”，“1”: “嗯。”，“2”: “嗯。”，“3”: “哦。”，...}
# 7 个键一个不少，归一化后就是合法 JSON；不归一化则整批判失败。
_QUOTE_CH = '"“”„‟「」『』｢｣'
_COMMA_CH = "，、"
_COLON_CH = "：:＝"


def _normalize_jsonish(s: str) -> str:
    """把"中文式 JSON"归一成合法 JSON。

    必须**扫描**而不是全局替换：全角逗号出现在字符串内部时（`"嗯，"`）不能动，
    所以得跟踪引号状态。另外引号在值里出现时按"开关"处理——`“米粒呢”，“1”: “嗯。”`
    里的 `”` 实际充当了收尾引号，开关语义刚好把它补回来。
    """
    out = []
    in_str = False
    for ch in s:
        if ch in _QUOTE_CH:
            out.append('"')
            in_str = not in_str
        elif not in_str and ch in _COMMA_CH:
            out.append(",")
        elif not in_str and ch in _COLON_CH:
            out.append(":")
        else:
            out.append(ch)
    t = "".join(out)
    t = re.sub(r"([{,]\s*)(\d+)(\s*:)", r'\1"\2"\3', t)   # 无引号数字键 {0: "x"}
    t = re.sub(r",\s*([}\]])", r"\1", t)                  # 尾随逗号
    return t


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


_LOCAL_BE = None                     # 进程内单例：4 个翻译线程共用同一个 llama-server
_LOCAL_BE_LOCK = threading.Lock()

# 后端别名 → 真实分支名。UI 上"云端"这一项历史值是 dashscope，而分支只认 openai；
# 不做归一就会静默落到 ollama 分支（配置里 ollama 段还在，于是"看起来在跑"但不通）。
# 注意：这里只是**名字归一**——云端一律走标准 OpenAI 格式（/chat/completions + Bearer），
# 没有任何厂商专有实现；dashscope 仅作为旧配置里的别名保留。
_BACKEND_ALIAS = {
    "openai": "openai", "cloud": "openai", "dashscope": "openai", "qwen": "openai",
    "local": "local", "llamacpp": "local", "llama": "local",
    "ollama": "ollama", "none": "none",
}


def _local_backend(cfg: dict):
    """本地 llama.cpp 后端的进程内单例（首次调用时构造，不加载模型）。"""
    global _LOCAL_BE
    with _LOCAL_BE_LOCK:
        if _LOCAL_BE is None:
            from llama_backend import LlamaBackend
            _LOCAL_BE = LlamaBackend(cfg)
        return _LOCAL_BE


class Translator:
    def __init__(self, cfg: dict, glossary):
        self.cfg = cfg or {}
        self.glossary = glossary
        # 后端名归一：UI/旧配置里出现过 "dashscope"（云端）这个别名，而分支判断只认
        # "openai"/"local" —— 不归一的话选"云端"会被静默当成 ollama（走错后端、还不报错）。
        raw = str(self.cfg.get("backend", "ollama")).lower()
        self.backend = _BACKEND_ALIAS.get(raw, raw)
        self._openai_fold_system: bool | None = None   # 见 _chat_openai：qwen-mt 不吃 system 角色

        self.batch_size = int(self.cfg.get("batch_size", 10))
        self.max_steps = int(self.cfg.get("max_steps", 3))      # 纠错循环轮数
        self.thread_num = int(self.cfg.get("thread_num", 4))
        self.cache_enabled = bool(self.cfg.get("cache", True))
        self.cache_dir = str(self.cfg.get("cache_dir") or _default_cache_dir())
        self.target = str(self.cfg.get("target_lang", "zh"))

        # 专攻翻译模型（如 Sakura 系）的逐句模式：mt_system 非空即启用。
        # 这类模型按"单文本 + 专用系统提示词"调优（日中galgame领域微调），
        # 不服从 JSON 批量指令；逐句直翻 + 术语表修补 + 漏译/退化检查，
        # 失败句标记 error（头显跳过空行）。
        self.mt_system = str(self.cfg.get("mt_system", "") or "").strip()
        self.mt_user_prefix = str(self.cfg.get("mt_user_prefix", "") or "将下面的日文文本翻译成中文：")

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
                      "fallback_error": "", "degraded": False, "segments": 0,
                      "leak_kept": 0, "glossary_repaired": 0,
                      "fatal_errors": 0, "fatal_error": "",
                      "single_fallbacks": 0, "single_capped": 0}
        # 后台预热兜底后端：探测要真发一条请求，代理关闭时单次 30s。放后台做，
        # 真需要兜底时结果（含"全挂"的负缓存）已经就绪，不会在字幕流程中间卡住。
        if self.fallback_kind:
            threading.Thread(target=self._warmup_fallback, daemon=True,
                             name="fallback-warmup").start()

    def _warmup_fallback(self) -> None:
        try:
            fb = self._get_fallback()
            if fb is not None:
                fb._pick()          # AutoTranslator: 探测并缓存结果
        except Exception:
            pass

    # ------------------------------------------------------------ 缓存
    def _backend_cfg(self) -> dict:
        # 后端名 → 配置段：local = 本地 llama.cpp（模型来自安装目录 models\ 下的 GGUF，
        # 不经过 Ollama；见 llama_backend.py）
        key = {"openai": "openai", "local": "local"}.get(self.backend, "ollama")
        return self.cfg.get(key) or {}

    def _model_name(self) -> str:
        return str(self._backend_cfg().get("model", ""))

    def _cache_ns(self) -> str:
        """命名空间：缓存版本 + 提示词指纹 + 后端 + 端点 + 模型 + 目标语言。"""
        base = str(self._backend_cfg().get("base_url", "")).rstrip("/")
        return (f"v{_CACHE_VERSION}|{_PROMPT_FP}|{self.backend}|{base}|"
                f"{self._model_name()}|{self.target}")

    def _key(self, texts: list, system: str = "", lang_key: str = "") -> str:
        """缓存键。system 里含着命中到的术语表条目，所以术语表变了键也变；
        源语言也要并进去——术语表无命中时 system 相同，ja/en 的同形短文本
        （如 "OK"）否则会串语言复用同一份译文。"""
        gl = hashlib.sha256((system or "").encode("utf-8")).hexdigest()[:12]
        raw = (self._cache_ns() + "|" + lang_key + "|" + gl + "\n"
               + json.dumps(texts, ensure_ascii=False))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _cache_path(self, key: str) -> Path:
        # 按前两位分桶，避免单目录堆几万个小文件
        return Path(self.cache_dir) / key[:2] / (key + ".json")

    def _cache_get(self, key: str, n: int):
        """L1 内存 → L2 磁盘。返回与 texts 等长的译文列表，未命中返回 None。

        命中统计在这里分层计数：cache_hits 只算 L1，cache_disk_hits 只算 L2，
        两者相加才是总命中（调用方不再重复累加）。"""
        if not self.cache_enabled:
            return None
        with self._lock:
            hit = self._mem.get(key)
        if hit is not None:
            with self._lock:
                self.stats["cache_hits"] += 1
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
                # 淘汰一半最旧的（dict 保插入序），整体 clear 会让长视频
                # 周期性全冷、缓存命中率锯齿状抖动
                for k in list(self._mem.keys())[:len(self._mem) // 2]:
                    del self._mem[k]
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
        """一次 LLM 对话（local=本地 llama.cpp / ollama / openai 兼容）。"""
        if self.backend == "openai":
            return self._chat_openai(system, user)
        if self.backend == "local":
            return self._chat_local(system, user)
        return self._chat_ollama(system, user)

    def _chat_ollama(self, system: str, user: str) -> str:
        c = self.cfg.get("ollama") or {}
        # MT 逐句模式用 Sakura 官方采样参数（temp 0.1/top_p 0.3）+ 短输出上限：
        # 提示词回显会进入生成长循环（实测一次 13.7s），num_predict 必须收窄
        opts = ({"temperature": 0.1, "top_p": 0.3, "num_predict": 256}
                if self.mt_system else
                {"temperature": float(c.get("temperature", 0.2)),
                 "num_predict": int(c.get("num_predict", 2048))})
        url = str(c.get("base_url", "http://127.0.0.1:11434")).rstrip("/") + "/api/chat"
        data = self._post(url, {
            "model": c.get("model", "qwen2.5:3b"),
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "stream": False,
            "options": opts,
        })
        return (data.get("message", {}).get("content", "") or "").strip()

    def _chat_openai(self, system: str, user: str) -> str:
        """**标准 OpenAI 格式**的云端后端（任何 OpenAI 兼容服务都能接）。

        只用两个约定，不做任何厂商专有适配：
          · POST {base_url}/chat/completions
          · Authorization: Bearer <api_key>
        请求体也只有 model / messages / temperature / max_tokens 四个通用字段。

        三个实测坑：
        1. Key 来源：优先 config 的 `api_key`（UI 里填的），其次环境变量 `api_key_env`。
           UI 保存的是明文 key（本地应用，config.json 不进库），读回接口会被打码。
        2. 少数兼容服务不收 `system` 角色（回 400）—— 出错时自动把 system 折进 user 重试一次，
           并把结论记住，后续请求直接用折叠形式。
        3. 出错必须把上游响应体带出来 —— 否则"key 错/余额不足/模型名错"在日志里
           全是一句 HTTP 400，用户没法自查。
        """
        c = self.cfg.get("openai") or {}
        key = str(c.get("api_key") or "")
        if not key:
            key_env = c.get("api_key_env", "")
            key = os.environ.get(key_env, "") if key_env else ""
        if not key:
            raise RuntimeError(
                "未设置云端 API Key：请在「翻译后端」里填入（或设环境变量 "
                f"{c.get('api_key_env') or 'OPENAI_API_KEY'}）")

        model = str(c.get("model", ""))
        base = str(c.get("base_url", "")).rstrip("/")
        if not model:
            raise RuntimeError("未填写云端模型名（如 gpt-4o-mini / deepseek-chat 等）")
        if not base:
            raise RuntimeError("未填写云端 base_url（形如 https://api.openai.com/v1）")
        url = base + "/chat/completions"
        if self._openai_fold_system is None:
            self._openai_fold_system = bool(c.get("fold_system", False))

        def _payload(fold: bool) -> dict:
            msgs = ([{"role": "user", "content": f"{system}\n{user}"}] if fold
                    else [{"role": "system", "content": system},
                          {"role": "user", "content": user}])
            return {"model": model, "messages": msgs,
                    "temperature": float(c.get("temperature", 0.2)),
                    "max_tokens": int(c.get("max_tokens", 2048))}

        try:
            data = self._post(url, _payload(self._openai_fold_system),
                              {"Authorization": "Bearer " + key})
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            # 兼容服务拒绝 system 角色时，折叠重试一次（并记住结论）
            if (not self._openai_fold_system
                    and ("system" in body.lower() or e.code in (400, 422))):
                self._openai_fold_system = True
                try:
                    data = self._post(url, _payload(True), {"Authorization": "Bearer " + key})
                except urllib.error.HTTPError as e2:
                    body2 = ""
                    try:
                        body2 = e2.read().decode("utf-8", "replace")[:300]
                    except Exception:
                        pass
                    raise RuntimeError(f"云端翻译失败 HTTP {e2.code}：{body2 or e2.reason}") from e2
            else:
                raise RuntimeError(f"云端翻译失败 HTTP {e.code}：{body or e.reason}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"云端不可达（{url}）：{e.reason}") from e
        return (data["choices"][0]["message"]["content"] or "").strip()

    def _chat_local(self, system: str, user: str) -> str:
        """本地 llama.cpp（OpenAI 兼容 /v1/chat/completions）。

        模型是安装目录 models\\ 下的 GGUF 文件，由 llama_backend 按需拉起常驻
        llama-server —— 不依赖 Ollama、不需要模型库环境变量。
        采样参数与 Ollama 路径对齐（Sakura 官方 temp 0.1 / top_p 0.3；
        MT 模式必须收窄 max_tokens，提示词回显会拖成长循环）。
        """
        c = self.cfg.get("local") or {}
        be = _local_backend(c)
        be.ensure_server()                      # 幂等：已在跑直接复用
        if self.mt_system:
            opts = {"temperature": 0.1, "top_p": 0.3, "max_tokens": 256}
        else:
            opts = {"temperature": float(c.get("temperature", 0.2)),
                    "top_p": float(c.get("top_p", 0.3)),
                    "max_tokens": int(c.get("max_tokens", 2048))}
        url = str(c.get("base_url") or be.base_url).rstrip("/") + "/v1/chat/completions"
        data = self._post(url, {
            "model": c.get("alias", "sakura"),
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            **opts,
        })
        return (data["choices"][0]["message"]["content"] or "").strip()

    # ------------------------------------------------------------ 解析
    @staticmethod
    def _parse_json_dict(raw: str, want_keys: list | None = None) -> dict | None:
        """从模型输出里抠出 JSON 字典（容忍 markdown 围栏、前后废话、中文式 JSON）。

        依次尝试：原文 → 截取 `{...}` → 两者各自归一化后的版本，命中即返回。
        `want_keys` 给定时，模型返回**裸数组**（`["译文1","译文2"]`）也按顺序配对——
        小模型被要求"输出 JSON"时给数组是常见退化，按位配对能直接救回来。
        """
        if not raw:
            return None
        s = str(raw).strip()
        # 去掉 ```json ... ``` 围栏
        if s.startswith("```"):
            s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
            s = re.sub(r"\s*```$", "", s)
        cands = [s]
        i, j = s.find("{"), s.rfind("}")
        if 0 <= i < j:
            cands.append(s[i:j + 1])
        li, lj = s.find("["), s.rfind("]")
        if 0 <= li < lj:
            cands.append(s[li:lj + 1])
        for cand in list(cands):
            cands.append(_normalize_jsonish(cand))
        for cand in cands:
            try:
                d = json.loads(cand)
            except Exception:
                continue
            if isinstance(d, dict):
                return d
            # 裸数组：[{"text":...}] 或 ["译文", ...] → 按 want_keys 顺序配对
            if isinstance(d, list) and want_keys:
                vals = []
                for item in d:
                    if isinstance(item, dict):
                        v = (item.get("translation") or item.get("译文")
                             or item.get("text") or "")
                    else:
                        v = item
                    vals.append(v)
                if len(vals) == len(want_keys):
                    return {k: v for k, v in zip(want_keys, vals)}
        return None

    def _validate(self, got: dict | None, want_keys: list) -> tuple:
        if not isinstance(got, dict):
            # ⚠️ 这句会被拼进重试提示词。**绝对不要**把 Python 类型名（NoneType）
            # 写进去：小模型会照着 echo，实测连错两轮后第 3 轮只回了一个 "None"，
            # 于是"响应不是合法 JSON，前 120 字：'None'"——真凶其实是这句报错。
            return False, "模型没有按要求返回 JSON 对象"
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

        局限：第二判据对**拉丁字母源语言**（en→zh）天然失效——原文必含拉丁
        字母，条件恒 False，"译文夹英文"检测等于关闭。en 源要可靠检测需要
        统计译文中长拉丁词占比，误伤风险高，暂不启用。
        """
        tr = (tr or "").strip()
        if not tr:
            return False
        if str(self.target).lower().startswith("zh") and _KANA.search(tr):
            return True
        return bool(_LATIN.search(tr)) and not _LATIN.search(src or "")

    def _repair_with_glossary(self, lang_key: str, src: str, tr: str) -> str:
        """术语表自动修补：译文里残留的**原文**术语直接换成术语表译文。

        实测 `こんにちは、三上ゆあです。→ 你好，三上ゆあ。`——术语表里明明有
        `三上ゆあ→三上悠亚` 并已注入 system，小模型就是不套用。让模型再改一轮
        既不保证成功又慢，直接替换反而确定。长词优先，避免短词先吃掉长词的一部分。
        """
        tr = (tr or "").strip()
        if not tr or not self.glossary:
            return tr
        try:
            hit = self.glossary.match(lang_key, src or "")
        except Exception:
            return tr
        if not hit:
            return tr
        repaired = False
        for s, d in sorted(hit.items(), key=lambda kv: -len(kv[0])):
            if s and d and s in tr:
                tr = tr.replace(s, d)
                repaired = True
        if repaired:
            with self._lock:
                self.stats["glossary_repaired"] += 1
        return tr

    # ------------------------------------------------------------ 单批翻译
    def _translate_batch(self, texts: list, indices: list, system: str,
                         lang_key: str = "", context: str = "") -> list:
        """翻译一批（含纠错循环）；返回与 texts 等长的译文列表。

        三轮纠错的两条实测教训（都曾造成整批空白译文）：

        1. **不能用最后一轮覆盖前面的结果**。实测第 1 轮译文内容完全正确（只是引号
           是全角），第 3 轮退化成只回了 `None`；原来只保留最后一轮 → 7 条全空。
           所以这里记录"历史最好的一轮"（命中键数 − 漏译数）并优先用它。
        2. **最后一轮要换干净提示词**。把连续两轮的报错拼进 user 消息，小模型会
           照着报错里的词 echo；而且提示词越长，它越倾向于放弃格式。

        context：上一块的原文/译文参考（剧情承接，治代词/场景断裂）。**只拼进
        user 消息、绝不进 system**——缓存键含 system 哈希，上下文进 system 会让
        键随剧情滚动、缓存永久失效（参考 realtime-subtitle 的 context carryover）。
        """
        keys = [str(i) for i in indices]
        payload = json.dumps({str(i): t for i, t in zip(indices, texts)},
                             ensure_ascii=False)
        base_user = BATCH_PROMPT.format(n=len(texts), payload=payload)
        if context:
            base_user = context + "\n\n" + base_user
        user = base_user

        best = None            # (得分, dict, leak) —— 历史最好的一轮
        last_err = ""
        last_raw = ""
        steps = max(1, self.max_steps)
        for step in range(steps):
            if step > 0:
                with self._lock:
                    self.stats["fix_rounds"] += 1
            raw = self._chat(system, user)
            last_raw = raw
            got = self._parse_json_dict(raw, keys)
            ok, err = self._validate(got, keys)
            leak: list = []
            if ok:
                vals = [str(got[k]).strip() for k in keys]
                # 术语表自动修补：模型抄原文没翻时，按术语表直接替换
                vals = [self._repair_with_glossary(lang_key, t, v)
                        for t, v in zip(texts, vals)]
                got = dict(zip(keys, vals))
                # 空值也算不合格！`{"0": "", "1": "..."}` 是**键齐全**的合法 JSON，
                # 只看键就会判合格直接返回——实测漏掉过 `だから。→空`。
                # `_has_untranslated("")` 返回 False，所以必须单独挑出来。
                blank = [k for k, v in zip(keys, vals) if not v]
                leak = [k for k, v, t in zip(keys, vals, texts)
                        if v and self._has_untranslated(t, v)]
                if not blank and not leak:
                    return vals
                reasons = []
                if blank:
                    reasons.append("以下键的译文是空的，必须给出译文："
                                   + ",".join(blank[:10]))
                if leak:
                    reasons.append("以下键的译文还是原文/夹着原文，没有真正翻译成中文："
                                   + ",".join(leak[:10]))
                err = "；".join(reasons)
                leak = blank + [k for k in leak if k not in set(blank)]
                with self._lock:
                    self.stats["leak_rounds"] += 1
            elif isinstance(got, dict):
                # 键不齐：能用的先留着，缺的记成"不合格"（交给免费后端补这几条）
                leak = sorted(set(keys) - {str(k) for k in got})
            last_err = err

            if isinstance(got, dict) and got:
                score = len(set(keys) & {str(k) for k in got}) - len(leak)
                if best is None or score > best[0]:
                    best = (score, got, leak)

            # 末轮换成干净提示词重来（不累加报错），其余轮次才反馈错误
            if step == steps - 2:
                user = base_user + "\n\n" + RETRY_PROMPT
            elif step < steps - 1:
                user = user + "\n\n" + FIX_PROMPT.format(err=err, n=len(texts))

        if best is not None and best[1]:
            partial = {str(k): str(v).strip() for k, v in best[1].items()}
            raise BatchPartial(f"批量翻译校验失败：{last_err or '未完全达标'}",
                               partial, set(best[2] or ()))
        # 区分「完全没响应」和「响应不是 JSON」——排查方向完全不同
        if not (last_raw or "").strip():
            raise RuntimeError("批量翻译失败：模型返回空响应")
        raise RuntimeError("批量翻译失败：多轮都没有返回可用的 JSON"
                           f"（最后一次响应前 120 字：{str(last_raw)[:120]!r}）")

    # ------------------------------------------------------------ 逐条兜底
    def _single_translate(self, src: str) -> str:
        """单条兜底翻译：批量 JSON 模式没救回来的句子再单独试一次。

        批量模式对小模型天然更难——它得同时维持 JSON 结构和逐条译文，短语气词
        （`ふふ。`）和口语缩略（`あなたの家ってすごくおいしい。`）上容易"摆烂"：
        原样返回或中英日混杂。单条模式只要吐一句中文，成功率明显更高。

        测出来残留的硬缺陷正是这两类：`ふふ。→ふふ。`、`…おいしい。→你家って真的
        很香啊。`（夹着日文 `って`）。
        """
        src = (src or "").strip()
        if not src:
            return ""
        try:
            out = str(self._chat(SYSTEM, SINGLE_PROMPT.format(text=src)) or "").strip()
        except Exception:
            return ""
        # 单条模式没有 JSON 结构，模型可能带引号或前后缀，剥掉
        out = out.strip().strip('"“”「」『』').strip()
        if not out or self._has_untranslated(src, out) or self._is_degenerate(src, out):
            return ""
        return out

    # ------------------------------------------------------ 逐句专攻 MT
    def _mt_once(self, text: str, lang_key: str, context: str = "") -> str:
        """单句调用专攻翻译模型；空/漏译/退化一律判失败返回空串。"""
        prefix = ""   # 上下文前缀实测会泄漏进上屏译文（MT 无 JSON 校验兜底），弃用
        raw = str(self._chat(self.mt_system, prefix + self.mt_user_prefix + text) or "").strip()
        out = raw.strip().strip('"“”「」『』').strip()
        if not out or self._has_untranslated(text, out) or self._is_degenerate(text, out):
            return ""
        return self._repair_with_glossary(lang_key, text, out)

    def _translate_mt(self, todo: list, lang_key: str, context: str = "") -> None:
        """逐句专攻 MT（Sakura 系翻译特化模型）：单文本 + 专用系统提示词直翻。

        与批量 JSON 模式并行不悖：mt_system 配置非空才启用。带缓存（逐句键）、
        术语表修补、漏译/退化检查；并发 self.thread_num 路。失败句标记
        translate_failed:mt_empty（头显跳过空译文行）。

        context：上一块的译文（人称/语境衔接参考，Sakura v0.9 官方支持多行
        上下文拼接）。只作为**参考前缀**拼在输入前（换行分隔），不进缓存键、
        不进 system——实测能显著减少人称错位（她↔我）。"""
        from concurrent.futures import ThreadPoolExecutor

        def work1(s):
            text = (s.get("text") or "").strip()
            key = self._key([text], self.mt_system, lang_key)
            cached = self._cache_get(key, 1)
            if cached is not None:
                return cached[0]
            out = self._mt_once(text, lang_key, context)
            if out:
                self._cache_put(key, [out])
            return out

        workers = max(1, min(self.thread_num, len(todo)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(work1, [s for _, s in todo]))
        # 失败句 → 免费后端兜底（与批量模式同一安全网；MT 模式此前漏接）。
        # 免费结果若仍夹假名则保持失败（宁缺不上日文，见 _display_zh 同策略）。
        failed_pos = [n for n, r in enumerate(results) if not r]
        if failed_pos and self.fallback_kind:
            filled = self._free_translate([todo[n][1].get("text") or "" for n in failed_pos])
            for n, v in zip(failed_pos, filled):
                src = todo[n][1].get("text") or ""
                if v and not self._has_untranslated(src, v):
                    results[n] = v
        for (_, s), tr in zip(todo, results):
            s.pop("error", None)
            if not tr:
                s["error"] = "translate_failed:mt_empty"
            s["translation"] = tr
        with self._lock:
            self.stats["segments"] += len(todo)

    # ------------------------------------------------------------ 兜底
    def _get_fallback(self):
        with self._lock:
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
    def translate_segments(self, segs: list, lang_key: str, context: str = "") -> None:
        """就地写入 seg['translation']；**绝不抛异常**。

        翻译是加分项，ASR 文本才是核心产出。这里一旦往外抛，字幕服务的
        /transcribe 会变成 HTTP 500，调用方**整块音频连 ASR 结果一起丢**——
        实测 `@0s` 整块就这么没了（起因只是兜底模块少了一行 import）。
        所以翻译层必须自己兜住所有异常，最坏情况留空译文照常返回。

        context：上一块的原文/译文参考（可选）。只影响提示词，不影响缓存键。
        """
        try:
            self._translate_inner(segs, lang_key, context)
        except Exception as e:
            with self._lock:
                self.stats["fatal_errors"] += 1
                self.stats["fatal_error"] = f"{type(e).__name__}: {e}"
            for s in segs:
                if not (s.get("translation") or "").strip():
                    s["error"] = s.get("error") or f"translate_failed:{type(e).__name__}"

    def _translate_inner(self, segs: list, lang_key: str, context: str = "") -> None:
        """就地写入 seg['translation']；失败时留空并记录 seg['error']。"""
        todo = [(i, s) for i, s in enumerate(segs) if (s.get("text") or "").strip()]
        for s in segs:
            if not (s.get("text") or "").strip():
                s["translation"] = ""
        if not todo:
            return

        # 逐句专攻 MT 模式：mt_system 配置非空即启用（Sakura 等翻译特化模型）。
        # 该模式没有"批"的概念，直接逐句直翻后返回。
        if self.mt_system:
            ctx_note = ""
            if context:
                ctx_note = f"（上一句译文，供人称衔接参考，勿翻译：{context[:60]}）"
            self._translate_mt(todo, lang_key, ctx_note)
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
            key = self._key(texts, system, lang_key)
            cached = self._cache_get(key, len(texts))
            if cached is not None:
                return indices, cached
            try:
                with self._lock:
                    streak = self._fail_streak
                if self.fallback_kind and streak >= self.fallback_after:
                    with self._lock:
                        self.stats["skipped_batches"] += 1
                    raise RuntimeError(f"LLM 后端已连续 {streak} 批失败，跳过重试")
                out = self._translate_batch(texts, indices, system, lang_key, context)
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
                # 免费后端也没救回来（代理关闭时 Google 不可达）时，宁可保留
                # "夹着原文"的译文，也不要留空白字幕——空白对观众是零信息，
                # 夹着一个假名至少能看懂。会在回填阶段标记成 untranslated_leak。
                for p in missing:
                    if not out[p]:
                        v = str(partial.get(str(indices[p]), "") or "").strip()
                        if v:
                            out[p] = v
                            with self._lock:
                                self.stats["leak_kept"] += 1
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

        # ---- 逐条兜底：批量模式没能救回的（空译文 / 仍夹着原文）单独再试
        # 上限 20 条，避免异常情况下把整片都重跑一遍（正常一部片只有个位数）
        need = [s for s in segs
                if (s.get("text") or "").strip()
                and (not (s.get("translation") or "").strip()
                     or self._has_untranslated(s["text"], s.get("translation") or ""))]
        if need:
            if len(need) > 20:
                with self._lock:
                    self.stats["single_capped"] += 1
                need = need[:20]
            # 并发兜底：串行最坏 20 次完整 LLM 调用（本地小模型 +30~60s），
            # 全部计入该块的 mt_ms；并发 4 条能把最坏延迟压到约 1/4。
            workers = max(1, min(4, len(need)))
            with ThreadPoolExecutor(max_workers=workers) as ex:
                results = list(ex.map(lambda s: self._single_translate(
                    s.get("text") or ""), need))
            for s, v in zip(need, results):
                if v:
                    s["translation"] = v
                    s.pop("error", None)      # 修好了就把错误标记清掉
                    with self._lock:
                        self.stats["single_fallbacks"] += 1

    # ------------------------------------------------------------ 诊断
    def describe(self) -> str:
        cache = f"cache={'disk+mem' if self.cache_enabled else 'off'}"
        return (f"{self.backend}/{self._model_name()} batch={self.batch_size} "
                f"threads={self.thread_num} {cache} fallback={self.fallback_kind or 'off'}")
