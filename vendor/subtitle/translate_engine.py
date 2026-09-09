# -*- coding: utf-8 -*-
"""翻译引擎：本地 Ollama / 云端 OpenAI 兼容（qwen-mt-turbo、DeepSeek…）双后端

- 只翻译 ASR 已确定的句子（我们的分块架构里全部是最终结果）
- 术语表按"当前句命中"注入
- 每句带上文（上一句原文+译文）作为上下文，保持人称/术语一致
"""

import json
import os
import urllib.request

SYSTEM = ("你是专业的字幕翻译。译文要口语自然、简洁，符合中文字幕习惯；"
          "不要解释，不要添加原文没有的内容；保持人称和专有名词前后一致。只输出译文。")

USER_CTX = ("上文（仅供理解，不要翻译）：{prev_orig}\n上文译文：{prev_tr}\n\n"
            "把下面的文本翻译成简体中文，只输出译文，不要解释：\n{text}")
USER = "把下面的文本翻译成简体中文，只输出译文，不要解释：\n{text}"


class Translator:
    def __init__(self, cfg: dict, glossary):
        self.cfg = cfg or {}
        self.glossary = glossary
        self.backend = self.cfg.get("backend", "ollama")

    # ------------------------------------------------------------ 后端调用
    def _post(self, url: str, payload: dict, headers: dict, timeout=120) -> dict:
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json", **headers})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _translate_ollama(self, text: str, system: str) -> str:
        c = self.cfg.get("ollama", {})
        url = c.get("base_url", "http://127.0.0.1:11434").rstrip("/") + "/api/chat"
        data = self._post(url, {
            "model": c.get("model", "qwen2.5:3b"),
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": text}],
            "stream": False,
            "options": {"temperature": 0.2, "num_predict": 512},
        }, {})
        return (data.get("message", {}).get("content", "") or "").strip()

    def _translate_openai(self, text: str, lang_key: str) -> str:
        c = self.cfg.get("openai", {})
        key = os.environ.get(c.get("api_key_env", ""), "") if c.get("api_key_env") else ""
        if not key:
            raise RuntimeError("未设置 API Key（环境变量 %s）" % c.get("api_key_env", "?"))
        url = c.get("base_url", "").rstrip("/") + "/chat/completions"
        model = c.get("model", "qwen-mt-turbo")

        if model.startswith("qwen-mt"):
            # qwen-mt 专用：不支持 system message，术语走 translation_options
            terms = [{"source": k, "target": v} for k, v in self.glossary.match(lang_key, text).items()]
            opts = {"source_lang": "auto", "target_lang": c.get("target_lang", "Chinese")}
            if terms:
                opts["terms"] = terms
            payload = {"model": model, "messages": [{"role": "user", "content": text}],
                       "translation_options": opts}
        else:
            payload = {"model": model,
                       "messages": [{"role": "system", "content": SYSTEM},
                                    {"role": "user", "content": text}],
                       "temperature": 0.2}
        data = self._post(url, payload, {"Authorization": "Bearer " + key})
        return (data["choices"][0]["message"]["content"] or "").strip()

    # ------------------------------------------------------------ 主入口
    @staticmethod
    def _is_degenerate(text: str, translation: str) -> bool:
        """译文异常放大检测：小模型偶发把短文本翻成几十个重复字。

        例：原文「ああああ気持ちああ」(8 字) → 「啊啊啊…啊」(31 字)。
        判定：译文长度 > 原文 × 3 且译文里同一字符占比 > 60%。
        """
        tr = (translation or "").strip()
        if not tr or len(tr) <= max(6, len(text) * 3):
            return False
        from collections import Counter
        top = Counter(tr).most_common(1)[0][1]
        return top / len(tr) > 0.6

    def translate_segments(self, segs: list, lang_key: str) -> None:
        """就地写入 seg['translation']；失败时留空并记录 seg['error']。"""
        prev_orig = prev_tr = ""
        for seg in segs:
            text = seg.get("text", "").strip()
            if not text:
                seg["translation"] = ""
                continue
            try:
                if self.backend == "openai":
                    seg["translation"] = self._translate_openai(text, lang_key)
                else:
                    system = SYSTEM
                    hit = self.glossary.match(lang_key, text)
                    if hit:
                        system += "\n\n术语表（原文→译文，必须严格遵守；未出现的词不要套用）：\n" + \
                                  "\n".join(f"{k}→{v}" for k, v in hit.items())
                    user = (USER_CTX.format(prev_orig=prev_orig, prev_tr=prev_tr, text=text)
                            if prev_tr else USER.format(text=text))
                    seg["translation"] = self._translate_ollama(user, system)
                if self._is_degenerate(text, seg.get("translation", "")):
                    seg["translation"] = ""
                    seg["error"] = "translation_degenerate"
            except Exception as e:
                seg["translation"] = ""
                seg["error"] = str(e)
            prev_orig, prev_tr = text, seg.get("translation", "")
