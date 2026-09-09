# -*- coding: utf-8 -*-
"""统一术语表：一份表同时服务两个阶段

- ASR 阶段：把术语（原文）拼成 context 提示，减少专有名词识别错误
  （实测：小ギャル→コギャル、おさわりカブ→おさわりキャバ 均被纠正）
- 翻译阶段：只把"当前句真正出现的术语"注入 prompt，避免术语被乱套到别的词上

术语表在 PC 侧管理（JSON 文件），**改完文件即时生效**（按 mtime 热加载，无需重启服务）。
头显端不提供术语表管理界面。
"""

import json
from pathlib import Path


class Glossary:
    def __init__(self, paths: dict, base_dir: Path = Path(".")):
        self._paths = {}
        self._maps = {}
        self._mtimes = {}
        for lang, rel in (paths or {}).items():
            fp = Path(rel)
            if not fp.is_absolute():
                fp = base_dir / fp
            self._paths[lang] = fp
            self._maps[lang] = {}
            self._mtimes[lang] = None
        self.reload(force=True)

    # ------------------------------------------------------------ 热加载
    def reload(self, force: bool = False) -> bool:
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
                    self._maps[lang] = json.loads(fp.read_text(encoding="utf-8"))
                    print(f"[glossary] {lang}: 加载 {len(self._maps[lang])} 条 ← {fp}")
                except Exception as e:
                    print(f"[glossary] {lang}: 解析失败，沿用旧表 ← {fp}: {e}")
            else:
                self._maps[lang] = {}
                print(f"[glossary] {lang}: 文件不存在，术语表为空 ← {fp}")
            changed = True
        return changed

    def _ensure(self):
        self.reload(force=False)

    # ------------------------------------------------------------ 查询
    def langs(self):
        return list(self._paths.keys())

    def size(self, lang: str) -> int:
        self._ensure()
        return len(self._maps.get(lang, {}))

    def raw(self, lang: str) -> dict:
        self._ensure()
        return dict(self._maps.get(lang, {}))

    def asr_context(self, lang: str, limit: int = 200) -> str:
        """术语原文拼成 ASR 热词提示。"""
        self._ensure()
        return "、".join(list(self._maps.get(lang, {}).keys())[:limit])

    def keys(self, lang: str) -> list:
        self._ensure()
        return list(self._maps.get(lang, {}).keys())

    def match(self, lang: str, text: str) -> dict:
        """只返回当前文本里真正出现的术语。"""
        self._ensure()
        return {k: v for k, v in self._maps.get(lang, {}).items() if k in text}
