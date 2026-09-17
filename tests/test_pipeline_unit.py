# -*- coding: utf-8 -*-
"""管线单元冒烟测试（不依赖 GPU / 后端进程，秒级完成）。

  .venv/Scripts/python.exe tests/test_pipeline_unit.py

覆盖历史上真实炸过的点：
  1. llama_backend.ensure_server 超时收尾在持锁状态下调 stop_server ——
     非重入 Lock 必自锁死（挂死所有翻译线程），RLock 后必须能正常返回。
  2. MT 逐句模式下单句调用抛异常 —— 只允许报废该句，不许炸掉同批已翻好的结果。
  3. 逐句补救（_single_translate）失败要留痕（stats.single_fallback_errors），
     不许静默吞掉。
  4. make_free("auto") 必须复用同一实例（探测负缓存/已选后端才有意义）。
  5. 中文式 JSON 归一化与批量校验的基础行为不回归。
"""
import sys
from pathlib import Path

SUB = Path(__file__).resolve().parents[1] / "vendor" / "subtitle"
sys.path.insert(0, str(SUB))

FAILED = []


def check(name, fn):
    try:
        fn()
        print(f"  OK   {name}")
    except Exception as e:
        FAILED.append(name)
        print(f"  FAIL {name}: {type(e).__name__}: {e}")


# 1 ---------------------------------------------------------------- RLock
def t_llama_lock_reentrant():
    from llama_backend import LlamaBackend
    be = LlamaBackend({"port": 59999})
    with be._lock:                # 模拟 ensure_server 持锁
        be.stop_server()          # 旧 Lock 在这里永远挂住


# 2 ------------------------------------------------- MT 单句异常隔离
def t_mt_single_failure_isolated():
    from translate_engine import Translator
    t = Translator({"backend": "local", "mode": "mt",
                    "mt_system": "测试系统提示词", "cache": False,
                    "local": {"model": "dummy"}}, glossary=None)
    calls = {"n": 0}

    def fake_chat(system, user):
        calls["n"] += 1
        if "坏句" in user:
            raise RuntimeError("模拟后端瞬时故障")
        return "正常译文" + str(calls["n"])

    t._chat = fake_chat
    segs = [{"text": "句子一"}, {"text": "坏句"}, {"text": "句子三"}]
    t._translate_mt(list(enumerate(segs)), "ja")
    assert segs[0]["translation"], "正常句必须翻出来"
    assert segs[2]["translation"], "正常句必须翻出来"
    assert not segs[1]["translation"], "故障句应留空"
    assert segs[1].get("error") == "translate_failed:mt_empty", segs[1]


# 3 --------------------------------------- 逐句补救失败留痕
def t_single_translate_error_counted():
    from translate_engine import Translator
    t = Translator({"backend": "local", "mode": "batch", "cache": False,
                    "local": {"model": "dummy"}}, glossary=None)

    def boom(system, user):
        raise RuntimeError("后端挂了")

    t._chat = boom
    assert t._single_translate("测试文本") == ""
    assert t.stats["single_fallback_errors"] == 1, t.stats["single_fallback_errors"]


# 4 --------------------------------------------- make_free 复用
def t_make_free_auto_reused():
    import free_translators
    a = free_translators.make_free("auto")
    b = free_translators.make_free("auto")
    assert a is b, "auto 实例必须复用（否则探测负缓存失效）"
    assert free_translators.make_free("edge") is free_translators.make_free("bing")
    assert free_translators.make_free("nope") is None


# 5 ------------------------------------------- JSON 归一化回归
def t_jsonish_and_validate():
    from translate_engine import Translator, _normalize_jsonish
    d = Translator._parse_json_dict(
        '{"0": “米粒呢”，“1”: “嗯。”，“2”: “哦。”}')
    assert d == {"0": "米粒呢", "1": "嗯。", "2": "哦。"}, d
    assert _normalize_jsonish('{0: "x", 1: "y",}').startswith('{"0": "x"')
    t = Translator({"backend": "local", "cache": False,
                    "local": {"model": "dummy"}}, glossary=None)
    ok, _ = t._validate({"1": "a", "0": "b"}, ["0", "1"])
    assert ok
    ok, err = t._validate({"0": "b"}, ["0", "1"])
    assert not ok and "缺少键" in err
    ok, _ = t._validate(None, ["0"])
    assert not ok


# 6 ------------------------------------------- 退化/漏译判据回归
def t_degenerate_and_leak():
    from translate_engine import Translator
    assert Translator._is_degenerate("あ", "啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊")
    assert not Translator._is_degenerate("あ", "啊。")
    t = Translator({"backend": "local", "target_lang": "zh", "cache": False,
                    "local": {"model": "dummy"}}, glossary=None)
    assert t._has_untranslated("あこれすごい。", "あこれ好厉害。")
    assert not t._has_untranslated("すごい。", "好厉害。")
    assert not t._has_untranslated("", "")


# 7 ---------------------------------- 请求体上限（/transcribe/stream 是局域网开放的）
def t_read_capped_body():
    """超限必须**边读边拒**，不能先整段读进内存再判长度。

    /transcribe/stream 对局域网开放（头显流式模式直连），此前只有 /transcribe
    设了上限，该路由的 `await request.body()` 无上限——一个不带 Content-Length
    的分块请求能把服务读到 OOM。
    """
    import asyncio
    from stream_bridge import read_capped_body

    class FakeReq:
        def __init__(self, chunks):
            self.chunks = chunks
            self.consumed = 0

        async def stream(self):
            for c in self.chunks:
                self.consumed += 1
                yield c

    # 未超限：原样返回
    req = FakeReq([b"abc", b"def"])
    assert asyncio.run(read_capped_body(req, 100)) == b"abcdef"
    assert req.consumed == 2

    # 超限：返回 None，且**提前中断**（没有把后续分块读完）
    chunks = [b"x" * 10] * 50
    req = FakeReq(chunks)
    assert asyncio.run(read_capped_body(req, 25)) is None
    assert req.consumed < len(chunks), f"超限后仍在继续读：{req.consumed}/{len(chunks)}"

    # 恰好等于上限：允许（边界不能差一）
    req = FakeReq([b"y" * 25])
    assert asyncio.run(read_capped_body(req, 25)) == b"y" * 25


if __name__ == "__main__":
    print("== 管线单元冒烟 ==")
    check("llama 锁可重入（超时收尾不再自锁死）", t_llama_lock_reentrant)
    check("MT 单句异常只报废该句", t_mt_single_failure_isolated)
    check("逐句补救失败计入 stats", t_single_translate_error_counted)
    check("make_free 实例复用（含 auto/edge 别名）", t_make_free_auto_reused)
    check("中文式 JSON 归一化与校验", t_jsonish_and_validate)
    check("退化/漏译判据", t_degenerate_and_leak)
    check("请求体上限边读边拒（超限提前中断）", t_read_capped_body)
    if FAILED:
        print(f"\n{len(FAILED)} 项失败：{FAILED}")
        sys.exit(1)
    print("\n全部通过")
