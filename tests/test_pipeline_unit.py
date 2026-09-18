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
import os
import sys
import tempfile
from pathlib import Path

SUB = Path(__file__).resolve().parents[1] / "vendor" / "subtitle"
sys.path.insert(0, str(SUB))

# 整个套件把用户数据目录指到临时目录 —— 必须在 import 任何服务模块**之前**设。
# 不设的话，凡是被测代码走到 user_paths 的路径都会落到 %APPDATA%\FunScriptCast-Nexus\：
# 轻则在那儿留下 `.layout-v2` 标记（补救扫描被提前用掉），重则用**仓库模板**播种
# subtitle_config.json（空 key），把真实 key 挡在迁移之外——本机已经这样中过一次。
# 单个测试内部可以再改（迁移测试就来回切），但默认值必须是隔离的。
os.environ["NEXUS_USER_DIR"] = tempfile.mkdtemp(prefix="nexus-unittest-user-")

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


# 8 ------------------------------------- 拉丁幻觉判据（R44 整片实测新增）
def t_latin_hallucination():
    """日语音频里"没有假名也没有汉字"的短输出要判为幻觉，正常日语与片假名不得误杀。

    实测来源：Whisper 在日语短块上会吐 `.`/`Thank`/`I`/`you`/`2` 并当台词上屏
    （且只在开了 initial_prompt 回传时出现）。判据见 text_filters.is_latin_hallucination。
    """
    from text_filters import is_latin_hallucination as f
    # 该丢的
    for t in ("I", "you", "Thank", ".", "2", "Thank you."):
        assert f(t, "ja"), f"应判为幻觉：{t!r}"
    # 不该丢的：正常日语 / 汉字 / 片假名外来语（外来语写片假名，不是拉丁字母）
    for t in ("そうですね", "悠亜", "セックス", "オーケー", "こんにちは、いい天気ですね。", ""):
        assert not f(t, "ja"), f"误杀：{t!r}"
    # 长段英文是另一类问题，不在这里一刀切
    assert not f("This is a long English sentence that should not be flagged.", "ja")
    # 只对 ja 生效：英文源语言本来就该是拉丁字母
    assert not f("I", "en") and not f("you", "ko")


# 9 -------------------------------- whisper 后端段级过滤与接口（R45 接入）
def t_whisper_backend_filters():
    """faster_whisper 必须是懒加载（轻量运行时没装也能 import）；过滤与接口对齐 audiocpp。

    三条铁律的代码面：不给 prompt（没有回显判据的输入条件）、只本地缓存、
    skipped=True ⇔ 空结果（server 据此跳过 whisper 二次兜底，不会拿 CPU 把同一段
    音频再跑一遍）。"""
    from whisper_backend import WhisperBackend
    be = WhisperBackend({})
    assert be.model_ref == "kotoba-tech/kotoba-whisper-v2.0-faster"
    assert be.device == "cuda" and be.compute_type == "float16"
    assert be.vad is True and be.use_aligner is False and be.model == be.model_ref
    assert be.stop_server() is None
    assert be.backend_kind == "whisper"
    # 正常日语一律保留（含片假名外来语、汉字人名）
    assert be._keep("こんにちは、三上悠亜です。", "ja", True)
    assert be._keep("オーケー、わかった。", "ja", True)
    # 复读退化丢弃
    assert not be._keep("あ" * 30, "ja", True)
    # 拉丁幻觉丢弃（日语音频里的裸英文短输出）
    assert not be._keep("Thank you.", "ja", True)
    assert not be._keep("I", "ja", True)
    # drop_latin=False 时放行（判据开关生效）
    assert be._keep("Thank you.", "ja", False)
    # 长纯英文不在此判据射程内（另一类问题，宁可放行）
    assert be._keep("thank you very much for watching this video", "ja", True)
    # 空文本：既不过滤保留，也对应 skipped 语义
    assert not be._keep("", "ja", True)


# 10 ------------------------------ keep_segment 跨块去重（生产共用判据）
def t_keep_segment():
    from text_filters import keep_segment
    assert keep_segment(5000, 6000, 0)          # 无 keep_from：全保
    assert keep_segment(5000, 6000, 4500)       # 起点在保留区之后
    assert keep_segment(4000, 5300, 4500)       # 句尾越过 keep_from+300ms：跨块长句保留
    assert not keep_segment(4000, 4700, 4500)   # 整句基本都在重叠区：丢


# 11 ------------------------------ 术语表总开关（glossary.enabled）
def t_glossary_enabled_switch():
    """关闭后：热词/翻译注入/键查询全部走空，但词表数据保留、重开即恢复。"""
    import json, tempfile
    from glossary import Glossary
    d = Path(tempfile.mkdtemp())
    f = d / "g.json"
    f.write_text(json.dumps({"悠亜": "悠亚", "乳首": "乳首"}, ensure_ascii=False), encoding="utf-8")

    def mk(enabled):
        return Glossary({"ja": str(f), "enabled": enabled}, base_dir=d,
                        extra={"ja": {"ゆあ": "悠亚"}})

    g = mk(True)
    assert g.match("ja", "悠亜は…"), "开启时必须命中术语"
    assert g.asr_context("ja") != "", "开启时热词非空"
    assert "ゆあ" in g.keys("ja"), "extra 词条在 keys 里"
    g.set_enabled(False)
    assert g.match("ja", "悠亜は…") == {}, "关闭后翻译注入必须为空"
    assert g.asr_context("ja") == "" and g.asr_context_with_keys("ja") == ("", [])
    assert g.keys("ja") == [], "关闭后键查询为空（复读判据自然无判据可用）"
    assert g.size("ja") == 3, "关闭只是不注入，数据必须原样保留（2 条词表 + 1 条 extra）"
    g.set_enabled(True)
    assert g.match("ja", "悠亜は…"), "重开即恢复"
    # 缺省 enabled = True：旧配置文件没有这个键也不能变成关
    g2 = Glossary({"ja": str(f)}, base_dir=d)
    assert g2.enabled is True and g2.match("ja", "悠亜"), "缺省必须视为开启"


# 12 ------------------------------ 模型下载器（进度/断点续传/原子替换）
def t_model_downloader():
    import tempfile, threading, http.server
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import host_server as hs
    d = Path(tempfile.mkdtemp())
    payload = b"A" * 300000 + b"B" * 12345
    src = d / "src.bin"
    src.write_bytes(payload)

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            data = src.read_bytes()
            rng = None if self.path.endswith("full") else self.headers.get("Range")
            if rng:
                start = int(rng.split("=")[1].split("-")[0])
                body = data[start:]
                self.send_response(206)
                self.send_header("Content-Range",
                                 "bytes %d-%d/%d" % (start, len(data) - 1, len(data)))
            else:
                body = data
                self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    dest = d / "out" / "model.bin"
    seen = []
    hs._download_to_file("http://127.0.0.1:%d/f.bin" % port, dest,
                         prog=lambda done, total: seen.append((done, total)))
    assert dest.read_bytes() == payload, "下载内容必须完整"
    assert not list(dest.parent.glob("*.part")), ".part 必须被原子替换掉"
    assert seen and seen[-1][0] == len(payload), "进度必须走到收尾"

    # 断点续传：预置半个 .part → 只补剩余（Range 206）
    part = dest.with_suffix(".bin.part")
    part.write_bytes(payload[:1000])
    hs._download_to_file("http://127.0.0.1:%d/f.bin" % port, dest)
    assert dest.read_bytes() == payload, "续传后内容必须完整"

    # 坏例（审查 P0-2 实测复现）：已有 .part，但服务器忽略 Range 回 200 全量
    # ——旧实现会把它追加成"两份拼接"的静默损坏且无任何报错
    part.write_bytes(payload[:1000])
    dest.unlink()
    hs._download_to_file("http://127.0.0.1:%d/full.bin" % port, dest)
    assert dest.read_bytes() == payload, "服务器回 200 时必须推倒重下而不是追加"
    assert not list(dest.parent.glob("*.part"))
    srv.shutdown()


# 9 --------------------------------- 用户数据迁移（根治"删目录重装丢 key"）
def t_user_data_migration():
    """legacy 安装目录 → 用户数据目录的迁移：key 与词表都要带过去，且用户数据优先。

    这条守的是本次根治的根因（见 vendor/subtitle/user_paths.py 模块注释）：
    用户数据放安装目录会同时造成"删目录重装丢 key / 升级安装覆盖 key 与词表 /
    工具链一堆摘出回填特例"。测试覆盖四种情形：
      ① 首次运行：legacy 的 key 与词表被迁移到用户目录；
      ② 迁移后用户数据优先：改用户那份，不会被 legacy 覆盖回去；
      ③ 全新安装：legacy 有模板 → 由模板建出用户配置；
      ④ legacy 什么都没有：回退读 legacy 路径（不抛异常）；
      ⑤ **删安装目录重装**：新装的出厂模板不得覆盖已有用户数据（用户实际报的场景）；
      ⑥ 补救扫描：用户目录被空 key 模板播过种时，安装目录里的真实 key 仍要能进来（只跑一次）。
    """
    import json as _json
    import os as _os
    import tempfile as _tf

    import user_paths as up

    legacy = Path(_tf.mkdtemp(prefix="nexus-legacy-"))
    user = Path(_tf.mkdtemp(prefix="nexus-user-"))
    (legacy / "config.json").write_text(
        _json.dumps({"translate": {"openai": {"api_key": "sk-SECRET"}}}), encoding="utf-8")
    (legacy / "glossary_ja_zh.json").write_text(
        _json.dumps({"悠亜": "悠亚"}), encoding="utf-8")

    old = _os.environ.get("NEXUS_USER_DIR")
    _os.environ["NEXUS_USER_DIR"] = str(user)
    try:
        # ① 迁移：key 与词表都带过来
        cfg = up.load_config(legacy)
        assert cfg["translate"]["openai"]["api_key"] == "sk-SECRET", "迁移必须保住 key"
        assert (user / "subtitle_config.json").is_file(), "配置应落到用户数据目录"
        g = up.glossary_path(legacy, "glossary_ja_zh.json")
        assert g.parent == user and g.is_file(), "词表也要迁到用户数据目录"

        # ② 用户数据优先：改用户那份，不会被 legacy 覆盖
        (user / "subtitle_config.json").write_text(
            _json.dumps({"translate": {"openai": {"api_key": "sk-NEW"}}}), encoding="utf-8")
        assert up.load_config(legacy)["translate"]["openai"]["api_key"] == "sk-NEW"

        # ③ 全新安装：legacy 是模板 → 由它建出用户配置
        legacy2 = Path(_tf.mkdtemp(prefix="nexus-legacy2-"))
        (legacy2 / "config.json").write_text('{"server": {"port": 8756}}', encoding="utf-8")
        user2 = Path(_tf.mkdtemp(prefix="nexus-user2-"))
        _os.environ["NEXUS_USER_DIR"] = str(user2)
        assert up.load_config(legacy2)["server"]["port"] == 8756
        assert (user2 / "subtitle_config.json").is_file()

        # ④ legacy 什么都没有：回退到 legacy 路径，不抛
        empty = Path(_tf.mkdtemp(prefix="nexus-empty-"))
        _os.environ["NEXUS_USER_DIR"] = str(Path(_tf.mkdtemp(prefix="nexus-user3-")))
        assert up.config_path(empty) == empty / "config.json"

        # ⑤ 用户报的那个场景：**删掉整个安装目录再重装**。新装的 legacy 里只有出厂
        #    模板（key 为空、词表为空），已有用户数据绝不能被这份模板覆盖回去——
        #    这正是根治要保证的性质，也是「重装后数据还在」从"意外"变成"设计"的那一步。
        user4 = Path(_tf.mkdtemp(prefix="nexus-user4-"))
        _os.environ["NEXUS_USER_DIR"] = str(user4)
        inst_a = Path(_tf.mkdtemp(prefix="nexus-instA-"))
        (inst_a / "config.json").write_text(
            _json.dumps({"translate": {"openai": {"api_key": "sk-KEEP"}}}), encoding="utf-8")
        (inst_a / "glossary_ja_zh.json").write_text(
            _json.dumps({"悠亜": "悠亚"}), encoding="utf-8")
        assert up.load_config(inst_a)["translate"]["openai"]["api_key"] == "sk-KEEP"

        inst_b = Path(_tf.mkdtemp(prefix="nexus-instB-"))   # 重装后的安装目录
        (inst_b / "config.json").write_text(
            _json.dumps({"translate": {"openai": {"api_key": ""}}}), encoding="utf-8")
        (inst_b / "glossary_ja_zh.json").write_text("{}", encoding="utf-8")
        assert up.load_config(inst_b)["translate"]["openai"]["api_key"] == "sk-KEEP", \
            "重装后出厂模板不得覆盖用户数据目录里的 key"
        g2 = up.glossary_path(inst_b, "glossary_ja_zh.json")
        assert g2.parent == user4 and _json.loads(g2.read_text(encoding="utf-8")) == {"悠亜": "悠亚"}, \
            "重装后出厂空词表不得覆盖用户已攒的词表"

        # ⑥ 补救扫描（本机真实踩到过的情形）：用户目录已被**空 key 的模板**播过种，
        #    安装目录里那份有真实 key。先到先得的迁移永远轮不到它 ⇒ 必须有一次性补救，
        #    且被覆盖的那份要留备份；补救**只跑一次**，之后回到简单的先到先得。
        user5 = Path(_tf.mkdtemp(prefix="nexus-user5-"))
        _os.environ["NEXUS_USER_DIR"] = str(user5)
        (user5 / "subtitle_config.json").write_text(
            _json.dumps({"translate": {"openai": {"api_key": ""}}}), encoding="utf-8")
        inst_c = Path(_tf.mkdtemp(prefix="nexus-instC-"))
        (inst_c / "config.json").write_text(
            _json.dumps({"translate": {"openai": {"api_key": "sk-REAL"}}}), encoding="utf-8")
        assert up.load_config(inst_c)["translate"]["openai"]["api_key"] == "sk-REAL", \
            "空 key 的用户配置必须被安装目录里的真实 key 补救"
        assert (user5 / ".layout-v2").is_file(), "补救扫描要落标记"
        assert (user5 / "subtitle_config.json.bak-layout-v2").is_file(), "覆盖前必须留备份"

        up._layout_checked.clear()          # 模拟进程重启
        (user5 / "subtitle_config.json").write_text(
            _json.dumps({"translate": {"openai": {"api_key": ""}}}), encoding="utf-8")
        assert up.load_config(inst_c)["translate"]["openai"]["api_key"] == "", \
            "补救只跑一次：有标记之后不再回头覆盖用户的当前配置"
    finally:
        if old is None:
            _os.environ.pop("NEXUS_USER_DIR", None)
        else:
            _os.environ["NEXUS_USER_DIR"] = old


if __name__ == "__main__":
    print("== 管线单元冒烟 ==")
    check("llama 锁可重入（超时收尾不再自锁死）", t_llama_lock_reentrant)
    check("MT 单句异常只报废该句", t_mt_single_failure_isolated)
    check("逐句补救失败计入 stats", t_single_translate_error_counted)
    check("make_free 实例复用（含 auto/edge 别名）", t_make_free_auto_reused)
    check("中文式 JSON 归一化与校验", t_jsonish_and_validate)
    check("退化/漏译判据", t_degenerate_and_leak)
    check("请求体上限边读边拒（超限提前中断）", t_read_capped_body)
    check("拉丁幻觉判据（不误杀正常日语/片假名）", t_latin_hallucination)
    check("whisper 后端过滤与接口（懒加载/铁律）", t_whisper_backend_filters)
    check("keep_segment 跨块去重", t_keep_segment)
    check("术语表总开关（enabled 热更新/数据保留）", t_glossary_enabled_switch)
    check("模型下载器（进度/断点续传/原子替换）", t_model_downloader)
    check("用户数据迁移（key/词表带过去，用户数据优先）", t_user_data_migration)
    if FAILED:
        print(f"\n{len(FAILED)} 项失败：{FAILED}")
        sys.exit(1)
    print("\n全部通过")
