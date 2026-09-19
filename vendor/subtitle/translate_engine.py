# -*- coding: utf-8 -*-
"""翻译引擎：批量 JSON + 键校验纠错 + 免费后端兜底。

与旧版（逐条调用）的差别——思路参考 VideoCaptioner（WEIFENG2333/VideoCaptioner）：

  1. **批量**：把 batch_size 条（默认 10）打包成一个 JSON 字典一次请求
     `{"1": "原文", "2": "原文", ...}`，要求模型返回同构字典。
     逐条调用 → 批量调用，LLM 请求数直接降 10 倍。
  2. **键校验 + 纠错循环**：返回的键必须与输入完全一致，缺/多键就把错误
     反馈回去让它重试（最多 max_steps 轮）。这一步能消掉「漏翻整段」。
  3. **免费兜底**：**某一批** LLM 调用失败就立刻用 Bing/Google 补齐这一批
     （无需 Key、显存 0），保证字幕不整段空白；如果累计失败批数达到
     fallback.after_fail_batches，则判定该后端已挂，后续批次直接走免费后端，
     不再每批都白等一个超时。
     熔断**不以"配了免费兜底"为前提**：没兜底时同样停止撞死后端（只是不切换
     后端而已）。而且"后端级故障"（连不上/超时/熔断）必须与"内容不合格"
     （BatchPartial）分开——只有前者要跳过逐句补救，后者是健康后端在正常
     工作，逐句换提示词形态实测能救回大量句子。
  4. **多线程**：批与批之间并行（thread_num）。

缓存与术语表（分层缓存、热词注入、译文修补）已按需求整体移除：
每次翻译都真发请求，改提示词/模型/参数立即全部生效，不存在旧译文命中。

对外接口保持 `translate_segments(segs, lang_key)` 不变。
"""

from __future__ import annotations

import json
import os
import re
import threading
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from free_translators import make_free


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


class BackendDown(RuntimeError):
    """**后端级**故障：连不上 / 超时 / 熔断后跳过。与 BatchPartial 严格对立。

    区分这两者只为一件事：**还要不要对着同一个后端重试**。

      · BatchPartial —— 模型答得不好，但后端是活的。逐句换一种提示词形态实测
        能救回大量句子（R41：云端批量截断 14 句 → 逐句 14/14 全救回），
        这条补救路径必须保留，不能因为"看起来都算失败"就一起掐掉。
      · BackendDown —— 后端没了。逐句补救等于把同一个死连接再撞 N 遍，而且每遍
        都要吃满一个完整 timeout（云端默认 60s、本地 _post 默认 180s）：一块
        10 句最坏 (max_steps 3 + 逐句 10 + 全局 20/4) × 60s ≈ 18 分钟，
        头显 3s 的上屏节奏会被彻底拖垮。

    所以后端级故障必须是一个**能被上层认出来**的类型：熔断抛它、_post 连不上/
    超时抛它，`work()` 见到它就跳过逐句补救、全局补救也跳过对应段，只留空 + 留痕。
    """


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

# ollama 分支的模型默认值。config.json 里发运的就是 `"model": ""`（键存在、值为
# 空串），于是 `c.get("model", "qwen2.5:3b")` 这个默认值**永远拿不到**——空串会原样
# 发进请求体（ollama 报 model not found），诊断信息里显示的模型名也一直是空。
_OLLAMA_DEFAULT_MODEL = "qwen2.5:3b"


def _local_backend(cfg: dict):
    """本地 llama.cpp 后端的进程内单例（首次调用时构造，不加载模型）。"""
    global _LOCAL_BE
    with _LOCAL_BE_LOCK:
        if _LOCAL_BE is None:
            from llama_backend import LlamaBackend
            _LOCAL_BE = LlamaBackend(cfg)
        return _LOCAL_BE


class Translator:
    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        # 后端名归一：UI/旧配置里出现过 "dashscope"（云端）这个别名，而分支判断只认
        # "openai"/"local" —— 不归一的话选"云端"会被静默当成 ollama（走错后端、还不报错）。
        raw = str(self.cfg.get("backend", "ollama")).lower()
        self.backend = _BACKEND_ALIAS.get(raw, raw)
        # UI 的「关闭翻译」= backend "none"，必须**真正短路**：此前全链路只判
        # openai/local，其余一律落到 _chat_ollama —— 选了"关闭翻译"反而逐句去请求
        # 11435：没服务就每句一条失败日志 + error=translate_failed:mt_empty，
        # 而若那个端口真有服务，就会**真的翻译**（与用户的显式选择完全相反）。
        self.disabled = self.backend == "none"
        self._openai_fold_system: bool | None = None   # 见 _chat_openai：qwen-mt 不吃 system 角色

        self.batch_size = int(self.cfg.get("batch_size", 10))
        self.max_steps = int(self.cfg.get("max_steps", 3))      # 纠错循环轮数
        self.thread_num = int(self.cfg.get("thread_num", 4))
        self.target = str(self.cfg.get("target_lang", "zh"))

        # 专攻翻译模型（如 Sakura 系）的逐句模式：mt_system 非空即启用。
        # 这类模型按"单文本 + 专用系统提示词"调优（日中galgame领域微调），
        # 不服从 JSON 批量指令；逐句直翻 + 漏译/退化检查，
        # 失败句标记 error（头显跳过空行）。
        self.mt_system = str(self.cfg.get("mt_system", "") or "").strip()
        self.mt_user_prefix = str(self.cfg.get("mt_user_prefix", "") or "将下面的日文文本翻译成中文：")

        # 翻译模式：auto（默认）/ batch / mt。
        # 逐句 MT 是给 Sakura 这类"单文本 + 专用提示词"的**本地**模型准备的；
        # 云端大模型每次往返好几秒（实测 deepseek-flash 单句 5.4–6.1s），逐句会把
        # 队列拖垮 —— 现象正是"延迟高、字幕不全"（3s 一块的节奏追不上）。
        # 所以 auto 下：云端走批量 JSON（一次 batch_size 句），本地/ollama 保持逐句。
        # 需要强制时写 translate.mode = "mt" | "batch"。
        self.use_mt = self._resolve_use_mt()
        # 少量附加规则（如"正在说话的人用第一人称"）：批量模式下也会带上，
        # 默认复用 mt_system 的正文，避免为云端再维护一份提示词。
        self.system_extra = str(self.cfg.get("system_extra", "") or "").strip()

        # 免费兜底：**默认关闭**（用户明确要求"指定用什么就用什么"）。
        # 失败就如实留空（头显跳过空行），绝不静默换成免费机翻 —— 否则"云端质量反而
        # 更差"这类问题几乎无法察觉（兜底以前连一行日志都不打）。
        # 需要时显式写 translate.fallback.enabled = true 才启用。
        # 关掉它同时切断四处：整批补齐、熔断后接管、漏译条目补齐、后台预热。
        fb = self.cfg.get("fallback") or {}
        fb_enabled = bool(fb.get("enabled", False))
        self.fallback_kind = str(fb.get("backend", "bing")) if fb_enabled else ""
        self.fallback_after = max(1, int(fb.get("after_fail_batches", 2)))
        self._fallback = None
        self._fail_streak = 0
        # 同后端逐句补救的总预算（**每次调用**共享，不是每批）。逐句补救有价值
        # （R41：批量截断 14 句 → 逐句 14/14 全救回），所以额度给得宽（默认 40，
        # 约 2 倍 R41 的规模，正常块根本碰不到）；它的作用是兜住病态情况——
        # 一个块里多批内容持续不合格时，"没有上限"等于把整块再逐句重跑一遍
        # （本地小模型每句 2~13s，3s 的上屏节奏直接崩）。
        self.single_budget = max(1, int(self.cfg.get("single_rescue_max", 40)))

        self._lock = threading.Lock()
        self.stats = {"batches": 0,
                      "fix_rounds": 0, "leak_rounds": 0, "degenerate_rounds": 0,
                      "fail_batches": 0,
                      "fail_kinds": {}, "skipped_batches": 0, "partial_batches": 0,
                      "fallback_batches": 0, "fallback_errors": 0,
                      "fallback_error": "", "degraded": False, "segments": 0,
                      "leak_kept": 0,
                      "fatal_errors": 0, "fatal_error": "",
                      "single_fallbacks": 0, "single_capped": 0,
                      "single_fallback_errors": 0,
                      # 熔断/不可达时被主动跳过的同后端逐句补救（P1-2 的留痕）
                      "single_skipped_down": 0, "single_budget_skipped": 0}
        # 后台预热兜底后端：探测要真发一条请求，代理关闭时单次 30s。放后台做，
        # 真需要兜底时结果（含"全挂"的负缓存）已经就绪，不会在字幕流程中间卡住。
        # 关闭翻译时**不预热**：用户选的是"不发任何请求"，连兜底探测也不该发。
        if self.fallback_kind and not self.disabled:
            threading.Thread(target=self._warmup_fallback, daemon=True,
                             name="fallback-warmup").start()

    def _warmup_fallback(self) -> None:
        try:
            fb = self._get_fallback()
            if fb is not None:
                fb._pick()          # AutoTranslator: 探测并缓存结果
        except Exception:
            pass

    # ------------------------------------------------------------ 配置
    def _backend_cfg(self) -> dict:
        # 后端名 → 配置段：local = 本地 llama.cpp（模型来自安装目录 models\ 下的 GGUF，
        # 不经过 Ollama；见 llama_backend.py）
        key = {"openai": "openai", "local": "local"}.get(self.backend, "ollama")
        return self.cfg.get(key) or {}

    def _model_name(self) -> str:
        m = str(self._backend_cfg().get("model") or "").strip()
        # ollama 段允许 model 留空（空串按 _OLLAMA_DEFAULT_MODEL 发送，见 _chat_ollama），
        # 诊断信息必须显示**实际**发出的模型名，否则"空模型"和"默认模型"分不清。
        if not m and not self.disabled and self.backend not in ("openai", "local"):
            return _OLLAMA_DEFAULT_MODEL
        return m

    # ------------------------------------------------------------ 后端调用
    def _post(self, url: str, payload: dict, headers: dict | None = None,
              timeout: int = 180) -> dict:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError:
            # 后端**有响应**（鉴权/参数/额度错误）：不是"连不上"，上层照原样处理
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            # 唯一一处把"连不上/超时"标成后端级故障的地方：三个后端（ollama /
            # local / openai）都走这里，所以分类只需做一次。若不标，上层只看到
            # "一条普通异常"，会按内容不合格去逐句补救 —— 对着死连接每句再吃满
            # 一个 timeout。
            raise BackendDown(
                f"后端不可达（{url}）：{getattr(e, 'reason', None) or e}") from e

    def _chat(self, system: str, user: str) -> str:
        """一次 LLM 对话（local=本地 llama.cpp / ollama / openai 兼容）。"""
        if self.disabled:
            # 防御性收口：UI「关闭翻译」时不该有任何请求发出去。正常路径已在
            # _translate_inner 短路，但 _chat 还有别的调用方（/translate/selftest、
            # 逐句补救、兜底），任何一条漏判都会静默去请求 11434/8082 ——
            # 没服务就是一堆失败日志，有服务就是**真的翻译了**，与用户的显式选择相反。
            raise RuntimeError("翻译已关闭（translate.backend = none）")
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
                if self.use_mt else
                {"temperature": float(c.get("temperature", 0.2)),
                 "num_predict": int(c.get("num_predict", 2048))})
        # 空串/全空白一律按默认模型：config.json 发运的 `"model": ""` 键存在，
        # dict.get 的默认值拿不到，会把空模型名原样发出去（ollama 报 model not found）。
        model = str(c.get("model") or "").strip() or _OLLAMA_DEFAULT_MODEL
        url = str(c.get("base_url", "http://127.0.0.1:11434")).rstrip("/") + "/api/chat"
        data = self._post(url, {
            "model": model,
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
        # 兼容阶梯（学到就记住，避免每个请求重学一遍）：
        #   fold        —— 服务不收 system 角色（少数兼容实现）
        #   drop_temp   —— 推理类模型只接受默认 temperature
        #   comp_tokens —— 新接口用 max_completion_tokens 取代 max_tokens
        compat = getattr(self, "_openai_compat", None)
        if compat is None:
            compat = {"fold": bool(c.get("fold_system", False)),
                      "drop_temp": bool(c.get("drop_temperature", False)),
                      "comp_tokens": bool(c.get("use_max_completion_tokens", False))}
            self._openai_compat = compat

        def _payload() -> dict:
            msgs = ([{"role": "user", "content": f"{system}\n{user}"}] if compat["fold"]
                    else [{"role": "system", "content": system},
                          {"role": "user", "content": user}])
            body = {"model": model, "messages": msgs}
            if not compat["drop_temp"]:
                body["temperature"] = float(c.get("temperature", 0.2))
            body["max_completion_tokens" if compat["comp_tokens"] else "max_tokens"] = \
                int(c.get("max_tokens", 2048))
            return body

        data = None
        for _ in range(4):
            try:
                # 请求级超时（**不是 sleep**）：给单次 HTTP 请求一个上限，超了就失败留空，
                # 避免云端卡住时整条流水线无限等（实测出现过拖满 30 分钟）。
                # timeout_sec = 0 或负数 = 不设上限（用户要求"指定什么就是什么"时可关掉）。
                _to = int(c.get("timeout_sec", 60))
                data = self._post(url, _payload(), {"Authorization": "Bearer " + key},
                                  timeout=(None if _to <= 0 else _to))
                break
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", "replace")[:300]
                except Exception:
                    pass
                low = body.lower()
                if e.code in (400, 404, 422):
                    if not compat["fold"] and ("system" in low or e.code in (400, 422)):
                        compat["fold"] = True
                        print("[translate] 云端不接收 system 角色 → 已折叠进 user 消息重试", flush=True)
                        continue
                    if not compat["drop_temp"] and "temperature" in low:
                        compat["drop_temp"] = True
                        print("[translate] 云端不接受自定义 temperature → 已省略该字段重试", flush=True)
                        continue
                    if not compat["comp_tokens"] and "max_completion_tokens" in low:
                        compat["comp_tokens"] = True
                        print("[translate] 云端要求 max_completion_tokens → 已改名重试", flush=True)
                        continue
                raise RuntimeError(f"云端翻译失败 HTTP {e.code}：{body or e.reason}") from e
            except urllib.error.URLError as e:
                # 与 _post 的口径一致：连不上 = 后端级故障（不是内容不合格），
                # 否则上层会对着这个死端点逐句再撞一遍。
                raise BackendDown(f"云端不可达（{url}）：{e.reason}") from e
        if data is None:
            raise RuntimeError("云端翻译失败：兼容重试仍不通过")
        text = (data["choices"][0]["message"]["content"] or "").strip()
        if not text:
            fr = data["choices"][0].get("finish_reason") or "?"
            raise RuntimeError(
                f"云端返回空译文（finish_reason={fr}）：推理类模型可能把预算都花在思考上，"
                "请调大 max_tokens 或换非推理模型")
        return text

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
        if self.use_mt:
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

    # ------------------------------------------------------------ 模式
    def _resolve_use_mt(self) -> bool:
        """逐句 MT 模式开关（见 __init__ 的注释）。

        auto：mt_system 非空**且**后端不是云端 → 逐句；云端一律批量。
        显式 translate.mode = "mt" / "batch" 时以配置为准。
        """
        mode = str(self.cfg.get("mode", "auto") or "auto").strip().lower()
        if mode == "mt":
            return bool(self.mt_system)
        if mode == "batch":
            return False
        return bool(self.mt_system) and self.backend != "openai"

    def _build_system(self, texts: list) -> str:
        """批量模式的系统提示词 = 基础 SYSTEM + 附加规则。"""
        base = SYSTEM
        # 附加规则：显式 system_extra 优先；否则在批量模式下沿用 mt_system 的正文
        # （那里面是"说话人用第一人称/如实翻译"这类领域规则，云端同样适用）。
        extra = self.system_extra or (self.mt_system if (self.mt_system and not self.use_mt) else "")
        if extra:
            base = base + "\n\n" + extra
        return base

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
        user 消息、绝不进 system**——system 是稳定指令区，剧情上下文放进去会
        让模型把它当翻译对象（参考 realtime-subtitle 的 context carryover）。
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
                got = dict(zip(keys, vals))
                # 空值也算不合格！`{"0": "", "1": "..."}` 是**键齐全**的合法 JSON，
                # 只看键就会判合格直接返回——实测漏掉过 `だから。→空`。
                # `_has_untranslated("")` 返回 False，所以必须单独挑出来。
                blank = [k for k, v in zip(keys, vals) if not v]
                leak = [k for k, v, t in zip(keys, vals, texts)
                        if v and self._has_untranslated(t, v)]
                # 退化译文（短句被放大成"啊啊啊啊…"）同样算不合格：只判空值和漏译时
                # 它会一路当合格返回。后果是这块要靠回填阶段才丢弃、多花一轮逐句
                # 补救。逐句路径（_single_translate / _mt_once）早就查了
                # _is_degenerate，这里补齐。
                degen = [k for k, v, t in zip(keys, vals, texts)
                         if v and self._is_degenerate(t, v)]
                if not blank and not leak and not degen:
                    return vals
                reasons = []
                if blank:
                    reasons.append("以下键的译文是空的，必须给出译文："
                                   + ",".join(blank[:10]))
                if leak:
                    reasons.append("以下键的译文还是原文/夹着原文，没有真正翻译成中文："
                                   + ",".join(leak[:10]))
                if degen:
                    reasons.append("以下键的译文异常重复放大（疑似退化），必须重新翻译："
                                   + ",".join(degen[:10]))
                err = "；".join(reasons)
                bad_keys = blank + [k for k in leak if k not in set(blank)]
                bad_keys += [k for k in degen if k not in set(bad_keys)]
                leak = bad_keys
                with self._lock:
                    self.stats["leak_rounds"] += 1
                    if degen:
                        self.stats["degenerate_rounds"] += 1
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
    def _single_translate(self, src: str, system: str | None = None) -> str:
        """单条兜底翻译：批量 JSON 模式没救回来的句子再单独试一次。

        **仍然用同一个后端**（不算换家兜底）：批量失败时逐句重问，实测能救回大部分
        —— 尤其云端推理型模型把 max_tokens 花在思考上、正片 JSON 被截断
        （finish_reason=length）的情况：批量 14 句全空，逐句 14/14 全翻出来。

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
            out = str(self._chat(system or SYSTEM, SINGLE_PROMPT.format(text=src)) or "").strip()
        except Exception as e:
            # 失败原因必须可见（限频防刷屏）：此前静默吞掉，"逐句补救也没救回来"
            # 在线上完全无从判断是网络挂了还是模型摆烂。
            with self._lock:
                self.stats["single_fallback_errors"] += 1
                n = self.stats["single_fallback_errors"]
            if n <= 3 or n % 20 == 0:
                print(f"[translate] 逐句补救失败（第 {n} 次）：{type(e).__name__}: {e}",
                      flush=True)
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
        return out

    def _translate_mt(self, todo: list, lang_key: str, context: str = "") -> None:
        """逐句专攻 MT（Sakura 系翻译特化模型）：单文本 + 专用系统提示词直翻。

        与批量 JSON 模式并行不悖：mt_system 配置非空才启用。带漏译/退化检查；
        并发 self.thread_num 路。失败句标记 translate_failed:mt_empty（头显跳过
        空译文行）。

        context：上一块的译文（人称/语境衔接参考，Sakura v0.9 官方支持多行
        上下文拼接）。只作为**参考前缀**拼在输入前（换行分隔）、不进 system
        ——实测能显著减少人称错位（她↔我）。"""
        from concurrent.futures import ThreadPoolExecutor

        def work1(s):
            text = (s.get("text") or "").strip()
            try:
                out = self._mt_once(text, lang_key, context)
            except Exception as e:
                # 单句调用失败只报废这一句。此前异常会从 ex.map 一路炸穿
                # _translate_mt，把同批其他句子**已经翻好的结果一起丢掉**
                # （整批标 fatal），一次瞬时网络抖动 = 一整块字幕全没。
                print(f"[translate] MT 单句失败：{type(e).__name__}: {e}", flush=True)
                out = ""
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
                n = self.stats["fallback_batches"]
            # 必须喊出来：兜底会把译文静默换成免费机翻，用户只会觉得"云端的反而更差"。
            if n == 1 or n % 10 == 0:
                print(f"[translate] ⚠️ 已切换到免费兜底（累计 {n} 批）—— 译文质量会明显下降；"
                      f"上游失败统计 fail_kinds={self.stats['fail_kinds']}", flush=True)
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

        context：上一块的原文/译文参考（可选）。只影响提示词。
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
        if self.disabled:
            # UI 的「关闭翻译」（backend = none）：用户显式选择，**不是失败**，
            # 所以不留 error（留了头显/诊断页会把自己的配置当故障报），也不留旧
            # 译文（同一 seg 对象可能被复用，留旧值等于"关了还在翻"）。
            # 放在 todo 计算之前：连"哪些段有文本"都不需要判断，一次请求都不发。
            for s in segs:
                s["translation"] = ""
                s.pop("error", None)
            return
        todo = [(i, s) for i, s in enumerate(segs) if (s.get("text") or "").strip()]
        for s in segs:
            if not (s.get("text") or "").strip():
                s["translation"] = ""
        if not todo:
            return

        # 逐句专攻 MT 模式（Sakura 等翻译特化模型；云端在 auto 下不走这里，见 __init__）。
        if self.use_mt:
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
        # 本块内被判为"后端级故障"的段索引：全局逐句补救要跳过它们
        # （对着同一个死后端再撞一遍，每次都是一个完整 timeout）。
        # 内容级不合格的段**不进这里**，那部分补救行为原样保留。
        down_at: set = set()
        # 同后端逐句补救的总预算，本块内由批内补救与全局补救共享（见 __init__）。
        budget = self.single_budget
        warned = False

        def _take_budget() -> bool:
            """领一次同后端逐句补救的额度；用尽返回 False（留痕 + 只喊一次）。"""
            nonlocal budget, warned
            with self._lock:
                if budget > 0:
                    budget -= 1
                    return True
                self.stats["single_budget_skipped"] += 1
                fire, warned = not warned, True
            if fire:
                print(f"[translate] 本块逐句补救已达预算 {self.single_budget} 句，"
                      f"剩余缺句不再逐句重试（同一后端反复撞只会拖垮上屏节奏）",
                      flush=True)
            return False

        def work(batch):
            texts = [(s.get("text") or "").strip() for _, s in batch]
            indices = [i for i, _ in batch]
            # system 计算也必须在 try 内：放在 try 外时，一条这样的异常会顺着
            # 下面 ex.map 的消费循环炸穿 _translate_inner —— 整块（含其它批次
            # **已经翻好的结果**）全部作废，而 MT 路径早就做过单句加固，
            # 两边隔离度不对等。
            # system 先置空：异常分支里的逐句补救会退回默认 SYSTEM 提示词。
            system = ""
            try:
                system = self._build_system(texts)
                with self._lock:
                    streak = self._fail_streak
                # 熔断判据**与"是否配了免费兜底"解耦**：兜底关着时同样要停止撞
                # 死后端（只是不切换到免费后端而已）。此前 `self.fallback_kind and`
                # 这个门控让默认配置（fallback.enabled=false）下熔断恒不触发。
                if streak >= self.fallback_after:
                    with self._lock:
                        self.stats["skipped_batches"] += 1
                    # 抛 BackendDown（而非普通 RuntimeError）：下面据此跳过逐句补救
                    raise BackendDown(f"LLM 后端已连续 {streak} 批失败，跳过重试")
                out = self._translate_batch(texts, indices, system, lang_key, context)
                with self._lock:
                    self._fail_streak = 0
                    self.stats["degraded"] = False
                return indices, out
            except Exception as e:
                # 后端还活着？只有"连不上/完全没响应"才计入熔断。
                # 内容层面的不合格（缺键、漏译修不好）说明模型在正常工作，
                # 拿它累加熔断会导致后面几十批全部跳过 LLM。
                # down = 后端级故障（连不上/超时/熔断）：这类失败**不能**再逐句
                # 重试——对着同一个死后端，每句都要白等一个完整 timeout。
                down = isinstance(e, BackendDown)
                alive = isinstance(e, BatchPartial)
                with self._lock:
                    self.stats["fail_batches"] += 1
                    self.stats["fail_kinds"][type(e).__name__] = (
                        self.stats["fail_kinds"].get(type(e).__name__, 0) + 1)
                    if alive:
                        self._fail_streak = 0
                    else:
                        self._fail_streak += 1
                        if self._fail_streak >= self.fallback_after:
                            # 与上面的熔断判据同一口径：没配兜底时"后端已挂"也要可见
                            self.stats["degraded"] = True
                if down:
                    with self._lock:
                        down_at.update(indices)
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
                if missing and not down:
                    # ① 先在同一后端上**逐句**救（不换家）：批量被截断/返回空时逐句往往能翻，
                    #    实测 deepseek-flash 批量截断的 14 句，逐句 14/14 全救回。
                    #    与"指定什么就用什么"一致——仍是同一个模型，只是换了提示词形态。
                    #    这里**只对内容级失败开放**：后端级故障（down）走这条等于把同一个
                    #    死连接按句数再撞一遍。另外受本块逐句预算约束（_take_budget）。
                    for p in list(missing):
                        if not _take_budget():
                            break
                        v = self._single_translate(texts[p], system)
                        if v:
                            out[p] = v
                            missing.remove(p)
                            with self._lock:
                                self.stats["single_fallbacks"] += 1
                if missing and self.fallback_kind:
                    filled = self._free_translate([texts[p] for p in missing])
                    for p, v in zip(missing, filled):
                        out[p] = v
                # 免费后端也没救回来（代理关闭时 Google 不可达）时，宁可保留
                # "夹着原文"的译文，也不要留空白字幕——空白对观众是零信息，
                # 夹着一个假名至少能看懂。会在回填阶段标记成 untranslated_leak。
                for p in missing:
                    if not out[p]:
                        v = str(partial.get(str(indices[p]), "") or "").strip()
                        # 退化译文不要捞回来：回填阶段反正会丢弃它并标
                        # translation_degenerate，捞回来只会让 leak_kept 统计说谎
                        if v and not self._is_degenerate(texts[p], v):
                            out[p] = v
                            with self._lock:
                                self.stats["leak_kept"] += 1
                if any(out):
                    if missing and len(missing) < len(texts):
                        with self._lock:
                            self.stats["partial_batches"] += 1
                    return indices, out
                return indices, ["" for _ in texts], f"{type(e).__name__}: {e}"

        def safe_work(batch):
            """单批兜底：work() 自己已尽量不抛，这里再兜一层。

            为什么必须要有：`ex.map` 是**顺序消费**的，某一批的结果一旦在消费时
            抛异常，整个 for 循环立刻中断——它后面批次的结果、以及**整个回填阶段**
            全部丢掉（现象就是整块空白，日志里还看不出是哪批坏的）。
            """
            try:
                return work(batch)
            except Exception as e:
                # work() 的异常分支已经记过账，能漏到这里的都是意料之外的；
                # 仍然要留痕，否则又变成"整块空白但日志里什么都没有"。
                with self._lock:
                    self.stats["fail_batches"] += 1
                    k = f"work:{type(e).__name__}"
                    self.stats["fail_kinds"][k] = self.stats["fail_kinds"].get(k, 0) + 1
                print(f"[translate] 单批异常，只报废该批（{len(batch)} 段）："
                      f"{type(e).__name__}: {e}", flush=True)
                return ([i for i, _ in batch], ["" for _ in batch],
                        f"{type(e).__name__}: {e}")

        if self.thread_num > 1 and len(batches) > 1:
            with ThreadPoolExecutor(max_workers=min(self.thread_num, len(batches))) as ex:
                for batch_r in ex.map(safe_work, batches):
                    results[batch_r[0][0]] = batch_r
        else:
            for b in batches:
                batch_r = safe_work(b)
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
        need = [(i, s) for i, s in enumerate(segs)
                if (s.get("text") or "").strip()
                and (not (s.get("translation") or "").strip()
                     or self._has_untranslated(s["text"], s.get("translation") or ""))]
        if down_at:
            # 后端级故障的段直接跳过：对着同一个死后端逐句再撞一遍，每次都要吃满
            # 一个完整 timeout —— 这正是"单块最坏 18 分钟"的主要来源。留空即可，
            # 回填阶段已经给它们写了 err 留痕。
            n_down = sum(1 for i, _ in need if i in down_at)
            if n_down:
                need = [(i, s) for i, s in need if i not in down_at]
                with self._lock:
                    self.stats["single_skipped_down"] += n_down
                print(f"[translate] 后端级故障：跳过 {n_down} 段的逐句补救"
                      f"（同一后端逐句重试只会再白等 {n_down} 个超时）", flush=True)
        if need:
            if len(need) > 20:
                with self._lock:
                    self.stats["single_capped"] += 1
                need = need[:20]
            # 预算：与批内逐句补救共享同一份额度（内容级失败给得足够宽，默认 40 句；
            # 这里只是兜住病态情况）
            allowed = []
            for i, s in need:
                if not _take_budget():
                    break
                allowed.append((i, s))
            need = allowed
        if need:
            # 并发兜底：串行最坏 20 次完整 LLM 调用（本地小模型 +30~60s），
            # 全部计入该块的 mt_ms；并发 4 条能把最坏延迟压到约 1/4。
            workers = max(1, min(4, len(need)))
            with ThreadPoolExecutor(max_workers=workers) as ex:
                # 注意：这里的局部变量**不能**再叫 results —— 上面那个 results 字典
                # 才是回填用的，重名会埋雷。
                rescued = list(ex.map(lambda s: self._single_translate(
                    s.get("text") or ""), [s for _, s in need]))
            for (_, s), v in zip(need, rescued):
                if v:
                    s["translation"] = v
                    s.pop("error", None)      # 修好了就把错误标记清掉
                    with self._lock:
                        self.stats["single_fallbacks"] += 1

    # ------------------------------------------------------------ 诊断
    def describe(self) -> str:
        if self.disabled:
            # 不能走下面那行：_backend_cfg() 对未知后端一律回落到 ollama 段，
            # 于是"关闭翻译"会显示成 `none/<ollama 的模型> ... fallback=...`，
            # /health 与诊断页上根本看不出后端是 none（与"选了关闭反而真去翻译"
            # 是同一类"静默走错后端"的坑）。
            return "已关闭（translate.backend = none，不发起任何 LLM 请求）"
        fb = self.fallback_kind or "off"
        used = int(self.stats.get("fallback_batches") or 0)
        if fb != "off" and used:
            fb = f"{fb}(已兜底{used}批)"          # 让 /health 一眼看出有没有被静默降质
        fails = int(self.stats.get("fail_batches") or 0)
        fail_note = f" 失败批次={fails}" if fails else ""   # 无兜底时，失败必须看得见
        # 熔断跳过也要看得见：否则"后面几十批为什么全空"在诊断页上无迹可寻
        skipped = int(self.stats.get("skipped_batches") or 0)
        if skipped:
            fail_note += f" 熔断跳过={skipped}批"
        return (f"{self.backend}/{self._model_name()} batch={self.batch_size} "
                f"threads={self.thread_num} mode={'mt逐句' if self.use_mt else 'batch批量'} "
                f"fallback={fb}{fail_note}")
