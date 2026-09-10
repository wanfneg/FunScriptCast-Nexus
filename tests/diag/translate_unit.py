# -*- coding: utf-8 -*-
"""翻译层单元验证：批量、键校验纠错、分层缓存、免费兜底。

这不是"打印看看"的脚本，是带断言的测试——跑完给出 PASS/FAIL 与退出码。
重点回归两件事（都曾经真实坏过）：
  A. 缓存必须跨 Translator 实例存活（每个视频都会新建实例，
     纯实例级内存缓存在真实流程里等于没开）；
  B. 免费兜底必须在**第一批**失败时就生效（旧逻辑要连续失败 2 批，
     而单批视频永远达不到阈值，导致整段字幕空白）。

用法： .venv\\Scripts\\python.exe tests\\diag\\translate_unit.py
"""
from __future__ import annotations

import json
import pathlib
import shutil
import sys
import time
import urllib.request

APP = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(APP / "vendor" / "subtitle"))

from translate_engine import Translator  # noqa: E402
from glossary import Glossary            # noqa: E402

CACHE_DIR = APP / "cache" / "_translate_unit"
LOG = APP / "tests" / "_tr_unit.log"

_segs = [
    "うん。",
    "どっちのが好き？",
    "ゆっくりしてあげるから、我慢するんだよ。",
    "さすがに大丈夫。",
    "もう我慢できる。",
    "ああ気持ち。",
    "私のも舐めて。",
    "おいしい。",
    "何よかった？",
    "じゃあ、そうですね。",
    "ゆっくりして。",
    "もう終わりだと思った。",
]

_fails: list = []


def emit(s: str) -> None:
    print(s, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(s + "\n")
    except Exception:
        pass


def check(ok: bool, label: str, detail: str = "") -> bool:
    emit(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  — {detail}" if detail else ""))
    if not ok:
        _fails.append(label)
    return ok


def ollama_alive() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


def fake_segs(n: int) -> list:
    return [{"text": t} for t in _segs[:n]]


def main() -> int:
    if LOG.exists():
        LOG.unlink()
    g = Glossary({"ja": "glossary_ja_zh.json", "en": "glossary_en_zh.json"},
                 base_dir=APP / "vendor" / "subtitle")
    alive = ollama_alive()
    emit(f"术语表 ja={g.size('ja')} en={g.size('en')}")
    emit(f"Ollama 可达: {alive}")
    emit(f"缓存目录: {CACHE_DIR}")

    cfg = {
        "backend": "ollama",
        "ollama": {"base_url": "http://127.0.0.1:11434", "model": "qwen2.5:3b"},
        "batch_size": 6,
        "max_steps": 3,
        "thread_num": 2,
        "cache": True,
        "cache_dir": str(CACHE_DIR),   # 与生产缓存隔离，测试可重复
        "target_lang": "zh",
        "fallback": {"enabled": True, "backend": "auto", "after_fail_batches": 2},
    }

    # ---------------------------------------------------------------- 缓存
    shutil.rmtree(CACHE_DIR, ignore_errors=True)
    emit("\n===== 1) 冷启动（应全部走 LLM）=====")
    t1 = Translator(cfg, g)
    d1 = fake_segs(12)
    t0 = time.perf_counter()
    t1.translate_segments(d1, "ja")
    dt1 = time.perf_counter() - t0
    emit(f"  {t1.describe()}")
    emit(f"  耗时 {dt1:.2f}s  统计 {json.dumps(t1.stats, ensure_ascii=False)}")
    for s in d1[:4]:
        emit(f"    {s['text']}  →  {s.get('translation', '')}")

    emit("\n===== 2) 再跑一次（新实例，应全部命中磁盘缓存）=====")
    t2 = Translator(cfg, g)
    d2 = fake_segs(12)
    t0 = time.perf_counter()
    t2.translate_segments(d2, "ja")
    dt2 = time.perf_counter() - t0
    emit(f"  耗时 {dt2:.2f}s  统计 {json.dumps(t2.stats, ensure_ascii=False)}")

    if alive:
        check(t1.stats["cache_hits"] == 0, "冷启动无缓存命中",
              f"cache_hits={t1.stats['cache_hits']}")
        check(t2.stats["cache_hits"] >= 1, "二次运行命中缓存（跨实例）",
              f"cache_hits={t2.stats['cache_hits']} disk={t2.stats['cache_disk_hits']}")
        check(t2.stats["cache_disk_hits"] >= 1, "命中来自磁盘而非内存",
              f"cache_disk_hits={t2.stats['cache_disk_hits']}")
        check(t2.stats["batches"] - t2.stats["cache_hits"] == 0, "二次运行零 LLM 请求",
              f"batches={t2.stats['batches']} hits={t2.stats['cache_hits']}")
        check(all((s.get("translation") or "").strip() for s in d1), "冷启动译文非空")
        check([s.get("translation") for s in d1] == [s.get("translation") for s in d2],
              "缓存复现结果完全一致（消除 temperature 抖动）")
        check(dt2 < dt1, "二次运行更快", f"{dt1:.2f}s → {dt2:.2f}s")
    else:
        emit("  [SKIP] Ollama 不可达，跳过 LLM 相关断言（先启动 ollama serve）")

    # ---------------------------------------------------------------- 兜底
    emit("\n===== 3) 免费兜底（ollama 指向不可达端口，4 批 × 3 段）=====")
    bad = dict(cfg)
    bad["ollama"] = {"base_url": "http://127.0.0.1:59999", "model": "nope"}
    bad["batch_size"] = 3
    bad["thread_num"] = 1          # 串行才能确定性验证 degraded 跳过
    t3 = Translator(bad, g)
    d3 = fake_segs(12)
    t0 = time.perf_counter()
    t3.translate_segments(d3, "ja")
    dt3 = time.perf_counter() - t0
    st = t3.stats
    emit(f"  耗时 {dt3:.2f}s  统计 {json.dumps(st, ensure_ascii=False)}")
    for s in d3:
        emit(f"    {s['text']}  →  {s.get('translation', '')}  (err={s.get('error', '')})")

    check(st["batches"] == 4, "共 4 批", f"batches={st['batches']}")
    check(st["fail_batches"] == 4, "4 批全部记为失败", f"fail_batches={st['fail_batches']}")
    check(st["fallback_batches"] == 4, "兜底触发（旧逻辑此处为 0）",
          f"fallback_batches={st['fallback_batches']}")
    check(st["degraded"] is True, "连续失败后判定后端已挂", f"degraded={st['degraded']}")
    check(st["skipped_batches"] == 2, "后 2 批直接跳过 LLM 重试（省超时）",
          f"skipped_batches={st['skipped_batches']}")
    filled = sum(1 for s in d3 if (s.get("translation") or "").strip())
    if filled == 0:
        emit(f"  [WARN] 兜底译文为空（{st.get('fallback_error') or '网络不可达'}），"
             f"本地逻辑已通过；填充 {filled}/12")
    else:
        check(filled == 12, "兜底把 12 段全部补齐", f"filled={filled}/12")
        check(st["fallback_errors"] == 0, "兜底无异常",
              f"errors={st['fallback_errors']} {st.get('fallback_error') or ''}")

    # ---------------------------------------------------------------- 漏译
    emit("\n===== 4) 漏译检测：日文原文混进中文，应被丢回纠错循环 =====")
    t5 = Translator(cfg, g)
    t5.cache_enabled = False
    t5.max_steps = 3
    seq = {"n": 0}

    def leak_then_fix(system, user):
        seq["n"] += 1
        if seq["n"] == 1:
            # 第 2 条把原文抄回来，另两条正常；模拟真实看到的 `あこれすごい。→ あこれ好厉害。`
            return json.dumps({"0": "好的。", "1": "どっちのが好き？", "2": "慢一点。"},
                              ensure_ascii=False)
        return json.dumps({"0": "好的。", "1": "你喜欢哪一个？", "2": "慢一点。"},
                          ensure_ascii=False)

    t5._chat = leak_then_fix
    d5 = fake_segs(3)
    t5.translate_segments(d5, "ja")
    emit(f"  统计 {json.dumps(t5.stats, ensure_ascii=False)}")
    for s in d5:
        emit(f"    {s['text']}  →  {s.get('translation', '')}  (err={s.get('error', '')})")

    check(t5.stats["leak_rounds"] == 1, "检出 1 次漏译",
          f"leak_rounds={t5.stats['leak_rounds']}")
    check(t5.stats["fix_rounds"] == 1, "漏译触发 1 轮纠错",
          f"fix_rounds={t5.stats['fix_rounds']}")
    check(d5[1].get("translation") == "你喜欢哪一个？", "纠错后不再是日文原文",
          repr(d5[1].get("translation")))
    check(not d5[1].get("error"), "无残留错误标记", repr(d5[1].get("error")))
    check(seq["n"] == 2, "只多请求了 1 次", f"调用 {seq['n']} 次")

    # 英文漏出（实测 `ああ、そう。→ 啊啊、 yeah。`）走同一判据
    t6 = Translator(cfg, g)
    t6.cache_enabled = False
    t6._chat = lambda s, u: json.dumps({"0": "啊啊、 yeah。"}, ensure_ascii=False)
    d6 = fake_segs(1)
    t6.translate_segments(d6, "ja")
    check(t6.stats["leak_rounds"] == t6.max_steps,
          "英文漏出也被判为漏译", f"leak_rounds={t6.stats['leak_rounds']}")
    check(t6.stats["fail_kinds"].get("BatchPartial") == 1,
          "记为内容不合格（BatchPartial）", json.dumps(t6.stats["fail_kinds"]))
    check(t6.stats["degraded"] is False, "内容不合格**不**触发熔断（关键回归）",
          f"degraded={t6.stats['degraded']}")
    check(t6.stats["fallback_batches"] == 1, "只把不合格的那条送去兜底",
          f"fallback_batches={t6.stats['fallback_batches']}")
    filled6 = (d6[0].get("translation") or "").strip()
    if filled6 and filled6 != "啊啊、 yeah。":
        check(True, "漏译条被兜底结果替换", repr(filled6))
    else:
        emit(f"  [WARN] 兜底不可用，漏译条未被替换（{t6.stats.get('fallback_error')}）")

    # ---------------------------------------------------------------- 部分键
    emit("\n===== 5) 部分键：纠错耗尽后保住已翻好的（不整批退回）=====")
    t4 = Translator(cfg, g)
    t4.cache_enabled = False          # 直接测路径，不掺缓存
    t4.max_steps = 2
    calls = {"n": 0}

    def fake_chat(system, user):
        calls["n"] += 1
        # 永远只给 3 个键里的 2 个，模拟模型漏键；纠错轮也修不好
        return json.dumps({"0": "好的。", "1": "哪一个？"}, ensure_ascii=False)

    t4._chat = fake_chat
    d4 = fake_segs(3)
    t4.translate_segments(d4, "ja")
    st4 = t4.stats
    emit(f"  统计 {json.dumps(st4, ensure_ascii=False)}")
    for s in d4:
        emit(f"    {s['text']}  →  {s.get('translation', '')}  (err={s.get('error', '')})")

    check(calls["n"] == 2, "纠错循环跑满 max_steps", f"调用 {calls['n']} 次")
    check(st4["fix_rounds"] == 1, "记录 1 轮纠错", f"fix_rounds={st4['fix_rounds']}")
    check(d4[0].get("translation") == "好的。" and d4[1].get("translation") == "哪一个？",
          "LLM 已翻好的 2 条被保住",
          f"{d4[0].get('translation')!r}, {d4[1].get('translation')!r}")
    check(st4["partial_batches"] == 1, "记为部分救回批次",
          f"partial_batches={st4['partial_batches']}")
    if (d4[2].get("translation") or "").strip():
        check(d4[2].get("translation") not in ("好的。", "哪一个？"),
              "缺的那 1 条由兜底补齐", repr(d4[2].get("translation")))
    else:
        emit(f"  [WARN] 缺的 1 条未被兜底补齐（{st4.get('fallback_error') or '网络不可达'}）")

    emit("\n" + "=" * 56)
    if _fails:
        emit(f"FAILED {len(_fails)} 项： " + "; ".join(_fails))
        return 1
    emit("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
