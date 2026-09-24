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
                    "mt_system": "测试系统提示词",
                    "local": {"model": "dummy"}})
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
    t = Translator({"backend": "local", "mode": "batch",
                    "local": {"model": "dummy"}})

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
    t = Translator({"backend": "local",
                    "local": {"model": "dummy"}})
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
    t = Translator({"backend": "local", "target_lang": "zh",
                    "local": {"model": "dummy"}})
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


# 10 ------------------------------ keep_segment 跨块去重（生产共用判据）
def t_keep_segment():
    from text_filters import keep_segment
    assert keep_segment(5000, 6000, 0)          # 无 keep_from：全保
    assert keep_segment(5000, 6000, 4500)       # 起点在保留区之后
    assert keep_segment(4000, 5300, 4500)       # 句尾越过 keep_from+300ms：跨块长句保留
    assert not keep_segment(4000, 4700, 4500)   # 整句基本都在重叠区：丢


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
        def do_HEAD(self):
            # F06 的"复核已存在文件"要靠 HEAD 拿远端长度
            self.send_response(200)
            self.send_header("Content-Length", str(len(src.read_bytes())))
            self.end_headers()

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
            if self.path.endswith("truncated"):
                # 声明全长却只发一半就断（模拟连接中断/代理截断）
                self.wfile.write(body[:len(body) // 2])
                self.wfile.flush()
                self.close_connection = True
                return
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

    # ---- F06：半截包不得扶正为正式文件 ----
    dest.unlink()
    part.unlink(missing_ok=True)
    raised = None
    try:
        hs._download_to_file("http://127.0.0.1:%d/f.bin?truncated" % port, dest)
    except Exception as e:
        raised = e
    assert raised is not None, "声明全长却提前断流时必须报错，不能当成下载完成"
    if dest.exists():
        raise AssertionError(
            f"半截包被扶正成了正式文件（{dest.stat().st_size}/{len(payload)} 字节）"
            "——再叠上\"已存在就跳过\"就是永久损坏的模型还显示已安装（F06）")
    assert part.exists() and part.stat().st_size < len(payload), \
        "半截内容应当留在 .part 里等续传"

    # ---- F06：已存在但大小不符的文件必须被识别出来（而不是 size>0 就跳过）----
    dest.write_bytes(payload[:500])          # 冒充"半截的已完成文件"
    assert hs._remote_size("http://127.0.0.1:%d/f.bin" % port) == len(payload), \
        "HEAD 应当能取到远端长度（复核大小的前提）"
    assert dest.stat().st_size != hs._remote_size("http://127.0.0.1:%d/f.bin" % port), \
        "本地半截文件与远端长度必然不等——这正是旧实现漏掉的判据"
    srv.shutdown()


# 9 --------------------------------- 用户数据迁移（数据落在安装目录 data\，不写 C 盘）
def t_user_data_migration():
    """迁移：历史位置 → `<安装目录>\\data\\`，key 要带过去，且现有数据优先。

    守的是这条设计（见 vendor/subtitle/user_paths.py 模块注释）：用户数据放安装目录的
    `data\\` 子目录 —— 一处、一个寿命、一个清理入口，且**不写 C 盘**。历史位置有两个：
      · `%APPDATA%\\FunScriptCast-Nexus\\`（R48 过渡版，文件名 subtitle_config.json）
      · `<安装目录>\\vendor\\subtitle\\`（最早，配置叫 config.json，也是出厂模板）
    覆盖七种情形：
      ① 首次运行：从安装目录旧位置迁移 key；
      ② 已有数据优先：改 data 那份，不会被历史位置覆盖回去；
      ③ 全新安装：历史位置只有模板 → 由模板建出 data 配置；
      ④ 哪儿都没有：回退到 data 路径且不抛异常；
      ⑤ **删安装目录重装**：新装模板不得覆盖已有数据；
      ⑥ 补救扫描：data 被空 key 模板播过种时，历史位置的真实 key 仍要能进来（只跑一次）；
      ⑦ **R48 的 %APPDATA% 位置优先于安装目录旧位置**（新方案优先的迁移顺序）。
    """
    import json as _json
    import os as _os
    import tempfile as _tf

    import user_paths as up

    old_user = _os.environ.get("NEXUS_USER_DIR")
    old_appdata = _os.environ.get("APPDATA")
    # APPDATA 也要隔离：它是"R48 历史位置"的根，不隔离就会读到真实机器上的目录
    _os.environ["APPDATA"] = _tf.mkdtemp(prefix="nexus-appdata-")
    try:
        legacy = Path(_tf.mkdtemp(prefix="nexus-legacy-"))
        user = Path(_tf.mkdtemp(prefix="nexus-user-"))
        (legacy / "config.json").write_text(
            _json.dumps({"translate": {"openai": {"api_key": "sk-SECRET"}}}), encoding="utf-8")

        _os.environ["NEXUS_USER_DIR"] = str(user)
        # ① 迁移：key 要带过来
        cfg = up.load_config(legacy)
        assert cfg["translate"]["openai"]["api_key"] == "sk-SECRET", "迁移必须保住 key"
        assert (user / "subtitle_config.json").is_file(), "配置应落到数据目录"

        # ② 已有数据优先：改 data 那份，不会被历史位置覆盖
        (user / "subtitle_config.json").write_text(
            _json.dumps({"translate": {"openai": {"api_key": "sk-NEW"}}}), encoding="utf-8")
        assert up.load_config(legacy)["translate"]["openai"]["api_key"] == "sk-NEW"

        # ③ 全新安装：历史位置是模板 → 由它建出 data 配置
        legacy2 = Path(_tf.mkdtemp(prefix="nexus-legacy2-"))
        (legacy2 / "config.json").write_text('{"server": {"port": 8756}}', encoding="utf-8")
        user2 = Path(_tf.mkdtemp(prefix="nexus-user2-"))
        _os.environ["NEXUS_USER_DIR"] = str(user2)
        assert up.load_config(legacy2)["server"]["port"] == 8756
        assert (user2 / "subtitle_config.json").is_file()

        # ④ 哪儿都没有：返回 data 路径，不抛
        empty = Path(_tf.mkdtemp(prefix="nexus-empty-"))
        user3 = Path(_tf.mkdtemp(prefix="nexus-user3-"))
        _os.environ["NEXUS_USER_DIR"] = str(user3)
        assert up.config_path(empty) == user3 / "subtitle_config.json"

        # ⑤ 用户报过的场景：**删掉整个安装目录再重装**（数据在新方案里就在安装目录内，
        #    所以只有"重装前先备份过 data"才谈得上保留；这里守的是"模板不得反向覆盖"）。
        user4 = Path(_tf.mkdtemp(prefix="nexus-user4-"))
        _os.environ["NEXUS_USER_DIR"] = str(user4)
        inst_a = Path(_tf.mkdtemp(prefix="nexus-instA-"))
        (inst_a / "config.json").write_text(
            _json.dumps({"translate": {"openai": {"api_key": "sk-KEEP"}}}), encoding="utf-8")
        assert up.load_config(inst_a)["translate"]["openai"]["api_key"] == "sk-KEEP"

        inst_b = Path(_tf.mkdtemp(prefix="nexus-instB-"))   # 重装后的安装目录
        (inst_b / "config.json").write_text(
            _json.dumps({"translate": {"openai": {"api_key": ""}}}), encoding="utf-8")
        assert up.load_config(inst_b)["translate"]["openai"]["api_key"] == "sk-KEEP", \
            "重装后出厂模板不得覆盖数据目录里的 key"

        # ⑥ 补救扫描（本机真实踩到过的情形）：data 已被**空 key 的模板**播过种，
        #    历史位置里有真实 key。先到先得的迁移永远轮不到它 ⇒ 必须有一次性补救，
        #    且被覆盖的要留备份；补救**只跑一次**，之后回到简单的先到先得。
        user5 = Path(_tf.mkdtemp(prefix="nexus-user5-"))
        _os.environ["NEXUS_USER_DIR"] = str(user5)
        (user5 / "subtitle_config.json").write_text(
            _json.dumps({"translate": {"openai": {"api_key": ""}}}), encoding="utf-8")
        inst_c = Path(_tf.mkdtemp(prefix="nexus-instC-"))
        (inst_c / "config.json").write_text(
            _json.dumps({"translate": {"openai": {"api_key": "sk-REAL"}}}), encoding="utf-8")
        assert up.load_config(inst_c)["translate"]["openai"]["api_key"] == "sk-REAL", \
            "空 key 的现存配置必须被历史位置里的真实 key 补救"
        assert (user5 / ".layout-v3").is_file(), "补救扫描要落标记"
        assert (user5 / "subtitle_config.json.bak-layout-v3").is_file(), "覆盖前必须留备份"

        up._layout_checked.clear()          # 模拟进程重启
        (user5 / "subtitle_config.json").write_text(
            _json.dumps({"translate": {"openai": {"api_key": ""}}}), encoding="utf-8")
        assert up.load_config(inst_c)["translate"]["openai"]["api_key"] == "", \
            "补救只跑一次：有标记之后不再回头覆盖用户的当前配置"

        # ⑦ 迁移顺序：R48 的 %APPDATA% 位置比安装目录旧位置**更新**，必须优先
        app_root = Path(_os.environ["APPDATA"]) / "FunScriptCast-Nexus"
        app_root.mkdir(parents=True, exist_ok=True)
        (app_root / "subtitle_config.json").write_text(
            _json.dumps({"translate": {"openai": {"api_key": "sk-R48"}}}), encoding="utf-8")
        inst_d = Path(_tf.mkdtemp(prefix="nexus-instD-"))
        (inst_d / "config.json").write_text(
            _json.dumps({"translate": {"openai": {"api_key": "sk-OLDEST"}}}), encoding="utf-8")
        user6 = Path(_tf.mkdtemp(prefix="nexus-user6-"))
        _os.environ["NEXUS_USER_DIR"] = str(user6)
        assert up.load_config(inst_d)["translate"]["openai"]["api_key"] == "sk-R48", \
            "%APPDATA%（R48）位置应优先于安装目录旧位置被迁移"
    finally:
        if old_appdata is None:
            _os.environ.pop("APPDATA", None)
        else:
            _os.environ["APPDATA"] = old_appdata
        if old_user is None:
            _os.environ.pop("NEXUS_USER_DIR", None)
        else:
            _os.environ["NEXUS_USER_DIR"] = old_user


# 10 --------------------------- run_server.py 在封闭 sys.path 下必须能 import
def t_run_server_embeddable_import():
    """自包含安装跑的是 **embeddable Python**：它的 `._pth` 是封闭列表、**不含脚本所在
    目录**，所以"脚本目录自动进 sys.path"这个普通 Python 的默认行为在这里**没有**。

    实测事故（R49 引入、1.0.19 打包版踩中）：`run_server.py` 里为了取日志目录写了
    `import user_paths`，而补 sys.path 的那段在它**后面** ⇒ 打包版全新安装后点"启动
    字幕服务"永远失败（宿主事件日志：子进程退出 code 1 / No module named 'user_paths'）。
    仓库里用 .venv 手测**测不出来**，所以必须有这条测试。

    做法：起一个子进程，把**脚本目录**从 sys.path 里摘掉（模拟 ._pth 的封闭性），
    cwd 也设在别处（否则 `python -c` 的 sys.path[0]='' 会把 cwd 带进来蒙对），
    再把 run_server.py 当脚本 exec（`__name__='probe'` ⇒ 不会真的去起 uvicorn）。
    """
    import subprocess
    import tempfile as _tf

    sub = Path(__file__).resolve().parents[1] / "vendor" / "subtitle"
    script = sub / "run_server.py"
    assert script.is_file(), f"找不到 {script}"

    cwd = Path(_tf.mkdtemp(prefix="nexus-runserver-"))
    probe = (
        "import os, sys\n"
        f"target = os.path.abspath(r'{sub}')\n"
        # 摘掉脚本目录（模拟 embeddable 的封闭 sys.path）
        "sys.path[:] = [p for p in sys.path if os.path.abspath(p or '.') != target]\n"
        f"src = open(r'{script}', encoding='utf-8').read()\n"
        f"g = {{'__name__': 'probe', '__file__': r'{script}'}}\n"
        "exec(compile(src, g['__file__'], 'exec'), g)\n"
        # 走到这里说明 import 阶段没炸；再确认真的拿到了 user_paths 模块
        "assert '_user_paths' in g, 'run_server 没有成功 import user_paths'\n"
        "assert g['_log_dir'].name == 'logs', g['_log_dir']\n"
        "print('EMBEDDABLE_IMPORT_OK')\n"
    )
    env = dict(os.environ)
    env["NEXUS_USER_DIR"] = str(cwd)          # 别写到真实用户目录
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run([sys.executable, "-c", probe], cwd=str(cwd), env=env,
                       capture_output=True, text=True, timeout=120)
    out = (r.stdout or "") + (r.stderr or "")
    assert "EMBEDDABLE_IMPORT_OK" in out, (
        "run_server.py 在封闭 sys.path（embeddable Python 的条件）下 import 失败——"
        "打包版会表现为「启动字幕服务」永远失败。原始输出：\n" + out[-1500:])


# 13 ------------------------------------------------ 混合切句：切句规则（R63）
def t_hybrid_cut_rules():
    import numpy as np
    from hybrid_segmenter import HybridBuffer
    tone = (np.sin(np.arange(16000 * 3) / 5.0) * 0.1).astype(np.float32)   # RMS≈0.07
    silence = np.zeros(16000, dtype=np.float32)

    buf = HybridBuffer()
    # 2.4s 语音：超过 2s 最短缓冲，但尾部 1s 还在说话 → 不切
    assert buf.feed(tone[:38400], "ja", 0)[0] is None
    # 尾部 1s 静音 → standard 切，绝对起点=缓冲起点，span=语音+静音尾
    span, start_ms, reason = buf.feed(silence, "ja", 2400)
    assert reason == "standard" and start_ms == 0, (reason, start_ms)
    assert len(span) == 16000 * 3.4, len(span)
    # 硬切：切点后续 6s 连续语音（无静音点可找）→ 在 5.2s 格点硬切
    span, start_ms, reason = buf.feed(np.concatenate([tone, tone]), "ja", 3400)
    assert reason == "hard" and start_ms == 3400, (reason, start_ms)
    assert len(span) == 16000 * 5.2, len(span)
    # 整段全静音 → 攒到切点（2s）时整段 RMS<阈值 → 丢弃（防静音复读循环）
    buf2 = HybridBuffer()
    span, start_ms, reason = buf2.feed(np.zeros(16000 * 3, dtype=np.float32), "ja", 0)
    assert span is None and reason == "silent", (reason,)


# 14 --------------------------------------- 混合切句：重叠去重/补零/跳变重置（R63）
def t_hybrid_overlap_gap_jump():
    import numpy as np
    from hybrid_segmenter import HybridBuffer
    tone = (np.sin(np.arange(16000 * 3) / 5.0) * 0.1).astype(np.float32)
    silence = np.zeros(16000, dtype=np.float32)

    buf = HybridBuffer()
    # 头显 3s/1s 协议：喂的是完整块，缓冲按绝对时间轴自己丢弃重叠区
    assert buf.feed(tone, "ja", 0)[0] is None                # [0,3s) 净增 3s
    assert buf.feed(tone, "ja", 2000)[0] is None             # [2s,5s) 重叠 1s 丢弃
    span, start_ms, reason = buf.feed(silence, "ja", 5000)   # [5s,6s) 静音尾
    assert reason == "hard" and start_ms == 0, (reason, start_ms)
    assert len(span) == 16000 * 5.2, len(span)               # 5s 语音+0.2s 静音，5.2s 格点硬切
    # 前跳 >10s：新会话，重置后从新起点重开（无切句时 feed 的 start_ms 恒 0，
    # 用"下一块切句的绝对起点"来验证重置真的发生了）
    assert buf.feed(tone[:32000], "ja", 20000)[0] is None
    span, start_ms, reason = buf.feed(silence, "ja", 22000)
    assert reason == "standard" and start_ms == 20000, (reason, start_ms)
    assert len(span) == 16000 * 3, len(span)                 # 2s 语音 + 1s 静音尾
    # 回跳 >1s：seek/重启，旧缓冲作废，从回跳点重开
    assert buf.feed(tone, "ja", 5000)[0] is None             # [5s,8s)
    assert buf.feed(tone[:16000], "ja", 5000)[0] is None     # 回跳 → 重置，重积 1s
    span, start_ms, reason = buf.feed(np.zeros(32000, dtype=np.float32), "ja", 6000)
    assert reason == "standard" and start_ms == 5000, (reason, start_ms)
    assert len(span) == 16000 * 2, len(span)
    # 空洞（丢块）：缺的 5s 补零保时间轴 → 扫描在补零区第一个静音点(4s)下刀
    buf2 = HybridBuffer()
    assert buf2.feed(tone, "ja", 0)[0] is None
    span, start_ms, reason = buf2.feed(tone, "ja", 8000)
    assert reason == "standard" and start_ms == 0, (reason, start_ms)
    assert len(span) == 16000 * 4, len(span)                 # 3s 语音 + 1s 补零零区


# 15 ------------------------------------------- 混合切句：段-组对齐器（R63）
def t_hybrid_align():
    from hybrid_segmenter import align_segments_to_groups, merge_regions

    groups = [(1000, 3000), (3500, 5500)]
    segs = [
        {"start_ms": 1000, "end_ms": 2500, "text": "あ"},
        {"start_ms": 2500, "end_ms": 3400, "text": "い"},   # 中点仍在组 0 → 并句
        {"start_ms": 3800, "end_ms": 5000, "text": "う"},
        {"start_ms": 8000, "end_ms": 9000, "text": "え"},   # 谁也不沾 → 保留自报时间
    ]
    out = align_segments_to_groups(segs, groups)
    assert [(s["start_ms"], s["end_ms"], s["text"]) for s in out] == [
        (1000, 3000, "あい"), (3500, 5500, "う"), (8000, 9000, "え")], out
    # 落在组间呼吸间隙的短段：中点距组尾 ≤0.6s 吸附进组
    out2 = align_segments_to_groups(
        [{"start_ms": 3050, "end_ms": 3150, "text": "ん"}], groups)
    assert out2[0]["start_ms"] == 1000 and out2[0]["text"] == "ん", out2
    # 英文同组并句走 join_tokens（R68）：词间补空格——"".join 会粘成 "helloworld"
    out3 = align_segments_to_groups(
        [{"start_ms": 1000, "end_ms": 2000, "text": "hello"},
         {"start_ms": 2000, "end_ms": 2900, "text": "world"}], groups)
    assert out3[0]["text"] == "hello world", out3
    # 组空 / 段空的边界
    assert align_segments_to_groups([], groups) == []
    only = [{"start_ms": 100, "end_ms": 200, "text": "あ"}]
    assert align_segments_to_groups(only, []) == only
    # 语音区合并（秒 → 毫秒，间隙 0.3s 内并组）
    assert merge_regions([(0.0, 1.0), (1.2, 2.0), (3.0, 3.2)]) == [
        (0, 2000), (3000, 3200)]


# 16 --------------------------------------- 混合切句：切点续接（R63 回归锁）
def t_hybrid_cutpoint_carry():
    import numpy as np
    from hybrid_segmenter import HybridBuffer
    tone = (np.sin(np.arange(16000 * 3) / 5.0) * 0.1).astype(np.float32)
    silence = np.zeros(16000, dtype=np.float32)

    buf3 = HybridBuffer()
    assert buf3.feed(tone, "ja", 0)[0] is None
    assert buf3.feed(tone, "ja", 2000)[0] is None
    span, _, reason = buf3.feed(silence, "ja", 5000)     # 5.2s 格点硬切，切点=5200ms
    assert reason == "hard" and len(span) == 16000 * 5.2, (reason, len(span))
    # 切点 5200ms 续着时间轴：下一块 [5s,8s) 头部 0.8s 属上一句已入账
    assert buf3.feed(tone, "ja", 5000)[0] is None        # 净增 [6s,8s)
    span, start_ms, reason = buf3.feed(silence, "ja", 8000)
    assert reason == "standard" and start_ms == 5200, (reason, start_ms)
    assert len(span) == 16000 * 3.8, len(span)           # [5.2s,9.0s)，不是从 5s 起
    # 反证：若切点没续（bug 版），下一句起点会标到 5000ms——重叠区重复入账。


# 17 ----------------------------- 混合切句：VAD 裁决切点（R63.1 BGM 场景）
def t_hybrid_vad_cut():
    import numpy as np
    from hybrid_segmenter import HybridBuffer
    bgm = (np.sin(np.arange(16000 * 4) / 5.0) * 0.05).astype(np.float32)  # RMS 0.035，永远过不了静音线

    calls = {"n": 0}
    def fake_vad(pcm):
        calls["n"] += 1
        return [(0.0, 2.5)]          # 语音到 2.5s 结束，之后是 BGM

    buf = HybridBuffer(vad_fn=fake_vad)
    # 2s：到最短句长了，但语音区尾(2.5s)还没到，停顿不足 → 不切
    assert buf.feed(bgm[:32000], "ja", 0)[0] is None
    # +1.5s：语音尾 2.5s，停顿 = 3.5-2.5 = 1.0s → VAD 裁决切在 2.65s（+0.15s 垫）
    span, start_ms, reason = buf.feed(bgm[32000:56000], "ja", 2000)
    assert reason == "vad" and start_ms == 0, (reason, start_ms)
    assert len(span) == 42400, len(span)
    assert buf.last_cut_regions == [(0.0, 2.5)]          # 语音区供对齐复用
    assert calls["n"] == 2, calls
    # VAD 失败（返回 None）→ 退回 RMS/硬切路径，不炸
    def bad_vad(pcm):
        return None
    buf2 = HybridBuffer(vad_fn=bad_vad)
    assert buf2.feed(bgm[:32000], "ja", 0)[0] is None
    assert buf2.feed(bgm[:32000], "ja", 2000)[0] is None
    span, start_ms, reason = buf2.feed(bgm[:32000], "ja", 4000)
    assert reason == "hard" and start_ms == 0, (reason, start_ms)
    assert len(span) == 83200, len(span)                 # 5.2s 格点


# 11 ------------------- F01：字幕配置深合并 + 流式上游地址与配置同源
def t_subtitle_config_deep_merge():
    """保存字幕配置必须**所有组**都做一层深合并（评审 F01）。

    复现的事故：UI 切换识别模型时发 `{"asr":{"audiocpp":{"model": …}}}`，而保存逻辑
    只对 translate 组深合并、其余组 `cfg[group].update(values)` 整段替换 ⇒
    `asr.audiocpp` 从 7 个键塌成 1 个（port/threads/backend 全丢）并持久化；
    stream_bridge 的上游地址依赖 port，于是"切一次模型 = 流式字幕整条失效"。
    """
    import json as _json
    import sys as _sys

    root = Path(__file__).resolve().parents[1]
    if str(root) not in _sys.path:
        _sys.path.insert(0, str(root))
    import host_server as H

    cfg_file = H.subtitle_cfg_path()
    seed = {
        "asr": {"backend": "audiocpp", "audiocpp": {
            "model": "../../models/OLD", "port": 8083, "host": "127.0.0.1",
            "backend": "cuda", "threads": 12, "language": "Japanese", "pad_sec": 0.25}},
        "translate": {"backend": "local", "openai": {
            "base_url": "https://x/v1", "api_key_env": "OPENAI_API_KEY", "temperature": 0.2}},
    }
    cfg_file.parent.mkdir(parents=True, exist_ok=True)
    cfg_file.write_text(_json.dumps(seed, ensure_ascii=False), encoding="utf-8")

    # UI 切换识别模型的真实载荷
    r = H.save_subtitle_config({"asr": {"audiocpp": {"model": "NEW-MODEL"}}})
    assert r.get("ok"), f"保存失败：{r}"
    got = _json.loads(cfg_file.read_text(encoding="utf-8"))
    ac = got["asr"]["audiocpp"]
    assert ac["model"] == "NEW-MODEL", "新值没写进去"
    for k, v in seed["asr"]["audiocpp"].items():
        if k == "model":
            continue
        assert ac.get(k) == v, f"asr.audiocpp.{k} 被静默抹掉了（深合并没生效）：{ac}"
    # translate 组本来就有的深合并不能被改坏
    assert got["translate"]["openai"]["api_key_env"] == "OPENAI_API_KEY", \
        "translate.openai 的同层键被覆盖了"
    assert got["translate"]["openai"]["base_url"] == "https://x/v1", "base_url 不该被动"


def t_stream_asr_upstream_from_config():
    """stream_bridge 的上游地址必须取 asr.audiocpp 的 host/port，且缺省与
    audiocpp_backend.DEFAULT_PORT 同源（评审 F01 后半）。

    此前硬编码 `http://127.0.0.1:8081`，而 audiocpp 缺省 8083 ⇒ 配置里 port 缺失时
    离线路径去 8083、流式路径去 8081，流式字幕整条失效。
    """
    import importlib
    import json as _json

    import audiocpp_backend
    import user_paths

    cfg_file = Path(user_paths.config_path(Path(__file__).resolve().parents[1]
                                           / "vendor" / "subtitle"))

    def _load(port=None, host=None):
        body = {"asr": {"backend": "audiocpp", "audiocpp": {"model": "m"}}}
        if port is not None:
            body["asr"]["audiocpp"]["port"] = port
        if host is not None:
            body["asr"]["audiocpp"]["host"] = host
        cfg_file.write_text(_json.dumps(body, ensure_ascii=False), encoding="utf-8")
        import stream_bridge
        return importlib.reload(stream_bridge)

    sb = _load(port=8099, host="127.0.0.2")
    assert sb.ASR_BASE == "http://127.0.0.2:8099", f"没按配置取上游：{sb.ASR_BASE}"

    # 配置里**没有** port 时，必须与 audiocpp_backend 的缺省一致（而不是老的 8081）
    sb2 = _load()
    assert sb2.ASR_BASE == f"http://127.0.0.1:{audiocpp_backend.DEFAULT_PORT}", \
        f"缺省端口与 audiocpp_backend 不同源：{sb2.ASR_BASE}"
    assert sb2.ASR_MODEL == audiocpp_backend.STREAM_MODEL_ID, \
        f"流式模型 id 与 audiocpp_backend 不同源：{sb2.ASR_MODEL}"


# 12 ------------- F12：ASR 上游死后请求路径自愈 + /health 如实反映死活
def t_asr_selfheal_and_health():
    """上游 audiocpp_server 崩溃/被杀之后必须能自愈，且 /health 不能继续报 ready。

    评审 F12 的三个点，全部用桩验证（不拉起真进程）：
      ① transcribe 进循环前会确认上游活着，死了就重拉一次；
      ② 连接级失败时重拉并给该段一次机会（重拉成功 → 这一句不该白丢）；
      ③ 整块全失败时如实带 error，而不是装成"这块没有语音"；
      ④ /health 的 asr_ready 查活体探测，不再只看类属性。
    """
    import urllib.error

    import numpy as np
    import audiocpp_backend
    import server_app

    class _Dead(Exception):
        pass

    def _backend(span_ok_first=False):
        be = audiocpp_backend.AudioCppBackend({"model": "x", "backend": "cpu"})
        calls = {"ensure": 0, "probe": 0, "span": 0}
        be.speech_spans = lambda pcm, tmpdir=None: [(0.0, 1.0)]
        be._write_wav = lambda wav, pcm: None
        be._ensure_alive_calls = calls

        def probe(timeout=2.0):
            calls["probe"] += 1
            return False          # 上游已死
        be.probe = probe

        def ensure_server():
            calls["ensure"] += 1  # 重拉"成功"
            return True
        be.ensure_server = ensure_server

        def span(*a, **kw):
            calls["span"] += 1
            if span_ok_first and calls["span"] == 1:
                raise urllib.error.URLError("connection refused")
            if span_ok_first:
                return "复活的这一句"
            raise urllib.error.URLError("connection refused")
        be._transcribe_span = span
        return be, calls

    pcm = np.zeros(16000, dtype=np.float32)

    # ④ /health 的判据
    server_app._HEALTH_PROBE["ok"] = None
    class _Alive:
        backend_kind = "audiocpp"
        def probe(self, timeout=2.0): return True
    class _DeadBe:
        backend_kind = "audiocpp"
        def probe(self, timeout=2.0): return False
    assert server_app._asr_ready(_Alive()) is True, "活着的上游应为 ready"
    server_app._HEALTH_PROBE["ok"] = None          # 清缓存再测下一个
    assert server_app._asr_ready(_DeadBe()) is False, "死了的上游不能报 ready（F12 的核心）"
    server_app._HEALTH_PROBE["ok"] = None
    assert server_app._asr_ready(server_app._AsrUnavailable()) is False, "未就绪兜底仍为 False"

    # ② 连接级失败 → 重拉一次并重试该段，这一句不该丢
    be, calls = _backend(span_ok_first=True)
    out = be.transcribe(pcm, "ja", 0, 0, None, None, "")
    assert calls["ensure"] >= 1, f"上游死了却没有重拉：{calls}"
    assert calls["span"] == 2, f"重拉后没有重试该段：{calls}"
    assert any(s.get("text") == "复活的这一句" for s in out["segments"]), \
        f"自愈后这一句仍丢了：{out['segments']}"
    assert "error" not in out, f"已自愈就不该报错：{out}"

    # ③ 整块全失败 → 如实上报，不装成"没有语音"
    be2, calls2 = _backend(span_ok_first=False)
    out2 = be2.transcribe(pcm, "ja", 0, 0, None, None, "")
    assert calls2["ensure"] >= 1, f"全失败时也没重拉：{calls2}"
    assert out2.get("error") == "backend_unavailable", \
        f"整块全失败必须如实报错（否则与真静音不可区分）：{out2}"
    assert out2.get("skipped") is True


# 13 ------------- F21/F22：熔断要能恢复；内容级失败不得计入熔断
def t_circuit_breaker_recovers():
    """评审 F21 + F22。

    F22：云端 HTTP 200 但 content 为空（推理模型把 max_tokens 花在思考上、
    finish_reason=length）是**内容级**不合格——后端活着，不能累加熔断。旧实现用普通
    RuntimeError，一次 4 段调用（2 批）就能把 streak 推满。
    F21：熔断只熔不恢复——唯一复位点在成功之后，而熔断批次到不了那里，于是三连失败
    之后整场播放全部跳过 LLM 直到服务重启。修法是冷却期后放一批探测（半开）。

    两条都用假后端跑真实调用路径。
    """
    import time as _time

    from translate_engine import BackendDown, ContentBad, Translator

    def _mk():
        t = Translator({"backend": "openai", "batch_size": 2, "cache": False,
                        "fallback": {"after_fail_batches": 2, "cooldown_sec": 5},
                        "openai": {"base_url": "https://x/v1", "model": "m",
                                   "api_key": "sk-test-not-real"}})
        t.disabled = False
        return t

    # ---- F22：空译文属于"后端还活着"，不得累加熔断 ----
    t = _mk()
    def _empty(url, payload, headers=None, timeout=180):
        return {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}
    t._post = _empty
    segs = [{"text": f"句子{i}"} for i in range(4)]
    t.translate_segments(segs, "ja")          # 4 段 = 2 批，两批都拿空译文
    assert t._fail_streak == 0, \
        f"空译文被计入熔断了（一次调用就推满，后续整场跳过 LLM）：streak={t._fail_streak}"
    assert "ContentBad" in t.stats["fail_kinds"], \
        f"空译文没有被归到内容级失败：{t.stats['fail_kinds']}"
    assert t.stats["skipped_batches"] == 0, "内容级失败不该触发熔断跳过"

    # ---- F21：刚熔断 → 跳过；冷却期过后 → 放一批探测并复位 ----
    t2 = _mk()
    t2._fail_streak = 2                        # 达到 fallback_after
    t2._last_fail_ts = _time.monotonic()       # 刚失败过
    before = t2.stats["skipped_batches"]
    try:
        t2.translate_segments([{"text": "a"}, {"text": "b"}], "ja")
    except BackendDown:
        pass
    assert t2.stats["skipped_batches"] > before, "熔断期内应当跳过批次"

    t2._last_fail_ts = _time.monotonic() - 999   # 冷却期已过
    def _good(url, payload, headers=None, timeout=180):
        return {"choices": [{"message": {"content": '{"0":"译文"}'},
                             "finish_reason": "stop"}]}
    t2._post = _good
    probes = t2.stats["half_open_probes"]
    t2.translate_segments([{"text": "a"}], "ja")
    assert t2.stats["half_open_probes"] > probes, \
        "冷却期过后没有放行探测批（F21 未修：熔断永不恢复）"
    assert t2._fail_streak == 0, f"半开探测成功后应复位熔断：{t2._fail_streak}"

    # 冷却期内仍应跳过（不能每批都去撞死后端），且**不得刷新冷却时钟**——
    # 否则持续推流时每批都刷新，冷永远走不完 = 还是"只熔不恢复"。
    t3 = _mk()
    t3._fail_streak = 2
    t3._last_fail_ts = _time.monotonic() - 4.0     # 距冷却结束还有 1s
    sk = t3.stats["skipped_batches"]
    ts_before = t3._last_fail_ts
    t3.translate_segments([{"text": "a"}, {"text": "b"}], "ja")
    assert t3.stats["skipped_batches"] > sk, "冷却期内必须继续跳过"
    assert t3._last_fail_ts == ts_before, \
        "熔断跳过刷新了冷却时钟 ⇒ 持续推流时永远等不到半开探测（F21 没真修好）"
    assert t3._fail_streak == 2, f"跳过不该累加失败计数：{t3._fail_streak}"
    # 冷却真的走完 → 下一次必须放行探测
    t3._last_fail_ts = _time.monotonic() - 5.1
    t3._post = _good
    pr = t3.stats["half_open_probes"]
    t3.translate_segments([{"text": "a"}], "ja")
    assert t3.stats["half_open_probes"] > pr, "冷却走完后仍不放行探测"


# 14 ------------- F15：流式断流必须关掉上游连接（GeneratorExit 兜底）
def t_stream_disconnect_closes_upstream():
    """评审 F15：头显断开/取消时，生成器被 close()，抛进来的是 GeneratorExit
    （py3.8+ 属 BaseException）——`except Exception` 捕不到，而旧实现又没有 finally
    ⇒ 上游连接不关、阻塞在 resp.read1 的线程池线程要等到 600s socket 超时才回来；
    反复断流会堆积死解码、拖慢新字幕，甚至耗尽 anyio 线程池（默认 40）。

    做法：假 HTTPConnection + 假请求体，驱动真实的 transcribe_stream 生成器，
    让它停在**第一个 yield**（此时正常路径的 conn.close() 还没执行），再 aclose()
    模拟断流，断言连接被关掉。
    """
    import asyncio
    import http.client as _http_client

    import stream_bridge as sb

    opened = {"closed": False}

    class FakeConn:
        def __init__(self, *a, **kw):
            self.closed = False

        def request(self, *a, **kw):
            pass

        def getresponse(self):
            class R:
                status = 200
                _sent = False

                def read1(self, n):
                    if self._sent:
                        return b""
                    self._sent = True
                    return b"data: [DONE]\n\n"      # 让生成器走到第一个 yield
                read = read1
            return R()

        def close(self):
            self.closed = True
            opened["closed"] = True

    orig_conn_cls = _http_client.HTTPConnection
    orig_body = sb.read_capped_body

    async def _fake_body(request):
        return b"\x00\x01" * 4000                   # 过 3200 字节门槛

    class FakeReq:
        pass

    async def _run():
        resp = await sb.transcribe_stream(FakeReq(), lang="ja", translate=False,
                                         video_start_ms=0)
        agen = resp.body_iterator
        first = await agen.__anext__()              # 跑到第一个 yield 并挂起
        await agen.aclose()                         # 模拟头显断开
        return first

    try:
        sb.http.client.HTTPConnection = FakeConn
        sb.read_capped_body = _fake_body
        first = asyncio.run(_run())
    finally:
        sb.http.client.HTTPConnection = orig_conn_cls
        sb.read_capped_body = orig_body

    assert "DONE" in first, f"首个产出应当是收尾事件，实际：{first[:80]}"
    assert opened["closed"] is True, (
        "生成器被关闭时没有关掉上游连接（F15）：GeneratorExit/CancelledError 都是 "
        "BaseException，`except Exception` 捕不到，必须靠 finally 收尾")


# 15 ------------- F13：坏配置必须能自恢复；写入必须原子
def t_config_corruption_recovery():
    """评审 F13。

    ① 坏 subtitle_config.json 以前会让 `server_app` / `stream_bridge` 的**模块级**
       `CFG = load_config(...)` 抛异常 ⇒ 服务 import 阶段即崩，宿主只看到 `code 1`，
       界面上没有任何修复入口。现在必须：隔离坏文件 + 从历史位置/出厂模板重建 + 不抛。
    ② 写入必须原子（唯一临时名 + os.replace）：原来是固定 `.migrating` 名 + copy2 直写
       现役文件，两进程并发首启会互相交错写同一个临时文件。
    """
    import json as _json

    import user_paths as up

    cfg = up.config_path(SUB)
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("{ 这显然不是 JSON", encoding="utf-8")

    got = up.load_config(SUB)              # ① 绝不能抛
    assert isinstance(got, dict), f"坏配置必须退化成 dict，实际 {type(got)}"
    bad = list(cfg.parent.glob("subtitle_config.json.bad-*"))
    assert bad, "坏配置必须被改名隔离（保留证据），而不是原地反复读崩"
    # 重建出来的必须是合法 JSON（重建源是历史位置/出厂模板）
    _json.loads(cfg.read_text(encoding="utf-8"))

    # ② 原子复制：内容一致、不留临时文件
    src = cfg.parent / "src_probe.json"
    src.write_text('{"a": 1}', encoding="utf-8")
    dst = cfg.parent / "dst_probe.json"
    dst.unlink(missing_ok=True)
    up._atomic_copy(src, dst)
    assert dst.read_text(encoding="utf-8") == '{"a": 1}', "原子复制的内容不对"
    leftovers = list(cfg.parent.glob("dst_probe.json.*.tmp"))
    assert not leftovers, f"原子复制留下了临时文件：{leftovers}"

    # ③ 源不存在时必须抛（而不是悄悄写出个空文件）
    raised = False
    try:
        up._atomic_copy(cfg.parent / "nope.json", dst)
    except Exception:
        raised = True
    assert raised, "源不存在时应当抛异常"
    assert not list(cfg.parent.glob("dst_probe.json.*.tmp")), "失败路径也必须清掉临时文件"


# 16 ------------- F16：8756 的跨站栅栏与过载快速失败
def t_lan_open_guards():
    """评审 F16（只做不涉及跨端契约的部分：Origin 栅栏 / 并发上限 / 体上限）。

    /transcribe 收裸 PCM，属**免预检的简单请求** ⇒ 用户浏览器里的任意网页都能用
    no-cors 直接打过来；而 8756 原本既没有 Origin 栅栏、也没有并发上限，
    100MB 的体上限还是正常块的 50 倍。三项一起意味着：同网任意设备可以并发打满
    GPU/内存并烧云端翻译额度。

    默认随机 token 那部分**没有做**：那是三端契约（docs/cross-repo-consistency.md
    列为契约但未实现），单方面改默认值会让头显连不上——需要先定下发通道。
    """
    import asyncio

    import server_app as sa

    # ① 先验"策略常量"（不依赖新类，所以对修复前的代码也能给出**行为级**失败）：
    #    25s 块约 800KB，上限应当是"够用但不夸张"的量级；100MB（旧值）是 50 倍余量。
    assert sa.MAX_BODY_BYTES >= 2 * 1024 * 1024, "上限不能小于 2MB（要容得下 25s 块）"
    assert sa.MAX_BODY_BYTES <= 16 * 1024 * 1024, \
        f"体上限仍然过大（{sa.MAX_BODY_BYTES} 字节）：同网设备能一次打满内存"
    assert sa.MAX_INFLIGHT >= 1, "并发上限必须存在且 ≥1"

    class _U:
        def __init__(self, path):
            self.path = path

    class _Cli:
        host = "127.0.0.1"

    class FakeReq:
        def __init__(self, path, origin=None):
            self.url = _U(path)
            self.headers = {"Origin": origin} if origin else {}
            self.client = _Cli()

    async def _next(_req):
        return "PASSED"

    def _call(guard, req):
        return asyncio.run(guard.dispatch(req, _next))

    # BaseHTTPMiddleware 要求传 app；这两个守卫的 dispatch 不用 self.app，传 None 即可
    og = sa._OriginGuard(None)
    # ① 跨站 Origin 必须拒绝（恶意网页走这条路）
    r = _call(og, FakeReq("/transcribe", origin="https://evil.example"))
    assert getattr(r, "status_code", None) == 403, f"跨站请求没被拒绝：{r}"
    # ② 非浏览器客户端（无 Origin）放行——头显 OkHttp / curl / 本机脚本
    assert _call(og, FakeReq("/transcribe")) == "PASSED", "无 Origin 的客户端不该被拦"
    # ③ 本机页面（PC 界面所在 origin）放行
    assert _call(og, FakeReq("/transcribe", origin="http://127.0.0.1:8790")) == "PASSED", \
        "本机 origin 不该被拦"
    assert _call(og, FakeReq("/transcribe", origin="http://localhost:8756")) == "PASSED"

    # ④ 过载快速失败：满了就 503（不排队——排队会让每块都等到超时）
    ov = sa._OverloadGuard(None)
    saved = sa._OVERLOAD["n"]
    try:
        sa._OVERLOAD["n"] = sa.MAX_INFLIGHT
        r = _call(ov, FakeReq("/transcribe"))
        assert getattr(r, "status_code", None) == 503, f"满载时应回 503，实际 {r}"
        # /health 永远不能被限流挡住（宿主靠它判断服务死活）
        assert _call(ov, FakeReq("/health")) == "PASSED", "/health 不该被过载守卫拦住"
        sa._OVERLOAD["n"] = 0
        assert _call(ov, FakeReq("/transcribe")) == "PASSED", "未满载时应当放行"
        # 放行后计数必须回到 0（否则每来一块都会永久占用一个名额）
        assert sa._OVERLOAD["n"] == 0, f"计数没归还：{sa._OVERLOAD['n']}"
    finally:
        sa._OVERLOAD["n"] = saved


# 17 ------------- F02：设置缓存的 key/data 必须成对（不得错配命中）
def t_settings_cache_pair():
    """评审 F02：`_SETTINGS_CACHE` 的 key（文件 stat）与 data（内容）必须**成对**读写。

    错配（key=新 stat、data=旧内容）一旦形成，此后每次轮询都**命中**它 ⇒ UI 永远显示旧
    设置；而 save_settings 又以 load_settings() 的结果作整文件写盘基底 ⇒ 并发保存被静默回滚。
    旧实现是锁外两条独立语句 + 锁外命中判定。

    做法：把 switchinterval 调到极小放大线程切换，一边反复改文件、一边多线程读，并有一个
    检查线程专门找"key 与文件当前 stat 相符、但 data 与文件内容不符"的状态。
    修复后该状态**由构造保证不可能出现**，所以这条断言不会偶发失败。
    """
    import json as _json
    import threading
    import time as _time

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import host_server as H

    f = H.SETTINGS_FILE
    f.parent.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    violations = []

    def _write(v):
        # 走**生产路径**（save_settings）：直接写文件不是生产行为，而且会让缓存键
        # (mtime_ns, size) 因为"同长度 + 同刻度"碰撞，测出的是缓存键的固有粒度而非竞态。
        H.save_settings({"dlna_port": v})

    _write(1000)
    H.load_settings()

    def _checker():
        while not stop.is_set():
            with H._SETTINGS_LOCK:
                k, d = H._SETTINGS_CACHE["key"], H._SETTINGS_CACHE["data"]
            if k is None or d is None:
                continue
            try:
                st = f.stat()
                if k != (st.st_mtime_ns, st.st_size):
                    continue                      # key 与文件不符 = 正常的过期缓存
                real = _json.loads(f.read_text(encoding="utf-8")).get("dlna_port")
            except OSError:
                continue
            if d.get("dlna_port") != real:
                violations.append((k, d.get("dlna_port"), real))

    def _reader():
        while not stop.is_set():
            H.load_settings()

    old_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)          # 放大线程交错概率
    threads = [threading.Thread(target=_checker, daemon=True)] + \
              [threading.Thread(target=_reader, daemon=True) for _ in range(4)]
    for t in threads:
        t.start()
    try:
        for i in range(200):
            _write(2000 + i)
            H.load_settings()
            _time.sleep(0.001)
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=2)
        sys.setswitchinterval(old_interval)

    assert not violations, \
        f"出现 key/data 错配缓存（命中却返回旧内容，UI 会永远显示旧设置）：{violations[:3]}"
    # 收尾一致性：文件与读回值必须一致
    _write(4242)
    assert H.load_settings().get("dlna_port") == 4242, "文件变了以后必须能读到新值"


# 18 ------------- F10 + F18：在飞计数口径与空闲时钟
def t_inflight_and_idle_clock():
    """评审 F10（在飞计数/时钟顺序）+ F18（空闲判定要用单调钟）。

    ① `/transcribe` 进 handler 就要自增在飞计数：旧顺序是"先读体再自增"，首个请求正在
       上传时 _INFLIGHT 仍是 0，空闲回收的临界点会在这时把服务 os._exit 掉（丢一块 +
       重启数十秒）；而 /transcribe/stream 本来就是先自增，两个入口口径还不一致。
    ② finally 里必须**先刷时钟再减计数**：反过来时超长请求在"已减计数、时钟未刷"的
       间隙里会被判成空闲而遭 os._exit。
    ③ 空闲判定必须用单调钟（墙钟回拨 ⇒ 永不回收、显存常驻；前跳 ⇒ 误杀在用服务），
       而对外的 last_req_ts 仍须是墙钟（界面按 Date.now()/1000 - last_req_ts 算）。
    """
    import asyncio
    import time as _time

    import server_app as sa
    import stream_bridge as sb

    orig_read, orig_impl = sb.read_capped_body, sa._transcribe_impl
    orig_lock = sa._INFLIGHT_LOCK
    during_read = []

    async def _fake_read(req, cap=None):
        with sa._INFLIGHT_LOCK:                 # 读体**期间**观察在飞计数
            during_read.append(sa._INFLIGHT)
        return b"\x00\x01" * 4000

    async def _fake_impl(body, lang, vs, kf, tr, want_partial=False):
        return {"language": lang, "segments": [], "asr_ms": 0.0, "skipped": True}

    class SpyLock:
        """记录"每次释放 _INFLIGHT_LOCK 那一刻"的空闲时钟值。"""

        def __init__(self, real):
            self._real = real
            self.snapshots = []

        def __enter__(self):
            self._real.acquire()
            return self

        def __exit__(self, *a):
            self.snapshots.append(sa._LAST_REQ_MONO)
            self._real.release()
            return False

    class FakeReq:
        headers = {"content-length": "8000"}

    spy = SpyLock(orig_lock)
    try:
        sb.read_capped_body = _fake_read
        sa._transcribe_impl = _fake_impl
        sa._INFLIGHT_LOCK = spy
        # 把空闲时钟推老：如果 finally 里"先减计数后刷时钟"，最后一次快照就会看到老值
        sa._LAST_REQ_MONO = _time.monotonic() - 600
        before = sa._INFLIGHT
        out = asyncio.run(sa.transcribe(FakeReq(), lang="ja", video_start_ms=0,
                                        keep_from_ms=0, translate=False, partial=0))
    finally:
        sb.read_capped_body, sa._transcribe_impl = orig_read, orig_impl
        sa._INFLIGHT_LOCK = orig_lock

    assert out.get("skipped") is True, f"假实现应当返回结果：{out}"
    assert during_read and during_read[0] >= 1, \
        f"读体期间在飞计数应当已经 >0（旧实现是读完之后才自增）：{during_read}"
    assert sa._INFLIGHT == before, f"计数必须归还：{sa._INFLIGHT} vs {before}"
    last_snapshot = spy.snapshots[-1]
    assert last_snapshot > _time.monotonic() - 5, (
        "减计数那一刻空闲时钟还是旧的 ⇒ finally 里把顺序写反了（F10：超长请求会被"
        f"误判空闲而 os._exit）；快照={last_snapshot:.1f}")
    # ③ 两个时钟都在请求收尾时刷新：内部单调钟 + 对外墙钟
    assert abs(sa._LAST_REQ_WALL - _time.time()) < 5, "对外墙钟没有刷新（界面会显示错的活动时间）"
    assert sa._LAST_REQ_MONO <= _time.monotonic(), "单调钟不应超前"
    # reaper 的判据必须建立在单调钟上（源码级检查：墙钟减法一旦回来就是 F18 复发）
    import inspect
    src = inspect.getsource(sa._idle_reaper)
    assert "time.monotonic() - _LAST_REQ_MONO" in src, \
        "空闲判定又用回墙钟了（时钟回拨会永不回收、前跳会误杀在用服务）"


# 19 ------------- F11：单客户端独占（会话状态是进程级单份）
def t_single_client_session():
    """评审 F11：`_HYBRID` / `_LAST_CTX` / stream_bridge 的 `_recent_ja`/`_last_ctx`
    都是**进程级单份**，按"同时只有一个客户端推流"设计；而 8756 绑 0.0.0.0，头显与手机
    都会直连 ⇒ 两设备并发时音频交错进同一缓冲、上下文串台（热词/剧情承接用错对白），
    字幕两边全乱；默认无鉴权时第二台还能把任意文本注入下一句。

    做法是把隐含假设变成显式约束：第一个来源独占会话，别的来源在独占期内被拒（503/error），
    最后一个请求过去 30 秒后自动释放（避免客户端崩了以后永久占用）。
    """
    import time as _time

    import server_app as sa

    class _Cli:
        def __init__(self, host):
            self.host = host

    class Req:
        def __init__(self, ip):
            self.client = _Cli(ip) if ip else None

    saved = dict(sa._SESSION)
    saved_on = sa._single_client_enabled
    try:
        sa._single_client_enabled = lambda: True
        with sa._SESSION_LOCK:
            sa._SESSION.update({"ip": "", "ts": 0.0})

        # ① 第一个来源认领成功
        assert sa._claim_session(Req("192.168.2.9")) is None, "第一个客户端应当能认领会话"
        # ② 同一来源继续 → 仍然放行（不能自己把自己挡了）
        assert sa._claim_session(Req("192.168.2.9")) is None, "同一来源不该被自己挡住"
        # ③ 另一台设备 → 拒绝，且原因里要带上占用者与"多久以后能接管"
        deny = sa._claim_session(Req("192.168.2.77"))
        assert deny and "192.168.2.9" in deny, f"第二个来源必须被拒并说明占用者：{deny}"
        # ④ 占用者静默超过接管期 → 允许新来源接管（自愈，避免永久占用）
        with sa._SESSION_LOCK:
            sa._SESSION["ts"] = _time.monotonic() - (sa._SESSION_TAKEOVER_SEC + 1)
        assert sa._claim_session(Req("192.168.2.77")) is None, \
            "占用者静默超过接管期后，新设备必须能接管（否则客户端崩了就永久锁死）"
        # ⑤ 开关关掉时完全恢复旧行为
        sa._single_client_enabled = lambda: False
        assert sa._claim_session(Req("10.0.0.1")) is None
        assert sa._claim_session(Req("10.0.0.2")) is None, "single_client=false 时不该拦任何来源"
    finally:
        sa._single_client_enabled = saved_on
        with sa._SESSION_LOCK:
            sa._SESSION.update(saved)


# 20 ------------- F19：对外接口不得泄漏本机绝对路径
def t_lan_endpoints_scrub_paths():
    """评审 F19：8756 `/health` 与 8791 `headset_status` 都对本机/局域网开放，
    却把**本机绝对路径**与**异常全文**原样回出去（便携安装里含 Windows 用户名）。

    这不是推测：R52 那次"启动字幕服务失败"的真实错误文本就是
      File "D:\\FunScriptCast-Nexus\\vendor\\subtitle\\run_server.py", line 22
    它经 headset_status 的 error 字段发给头显 = 发给整个局域网。
    项目自己对错误类别早有脱敏标准（audiocpp_backend._error_kind），漏的是这两个字段。
    """
    import sys as _sys

    root = Path(__file__).resolve().parents[1]
    if str(root) not in _sys.path:
        _sys.path.insert(0, str(root))
    import host_server as H
    import server_app as sa

    # ① 模型标识：绝对路径只留最后两段（不含盘符与用户名）
    abs_path = r"E:\Development\FunScriptCast-Nexus\models\Qwen3-ASR-0.6B"
    assert sa._model_label(abs_path) == "models/Qwen3-ASR-0.6B", sa._model_label(abs_path)
    assert "E:" not in sa._model_label(abs_path) and "Development" not in sa._model_label(abs_path)
    assert sa._model_label("kotoba-tech/kotoba-whisper-v2.0-faster") == \
        "kotoba-tech/kotoba-whisper-v2.0-faster", "HF 仓名不该被动"
    assert sa._model_label("") == "" and sa._model_label(None) == ""

    # ② 8791 的 error/asr/translate 必须脱敏（真实错误文本形态）
    real_err = ('子进程退出（code 1）：  File "D:\\FunScriptCast-Nexus\\vendor\\subtitle'
                '\\run_server.py", line 22, in <module>\n    import user_paths')
    scrubbed = H.scrub_paths(real_err)
    assert "D:\\FunScriptCast-Nexus" not in scrubbed, f"绝对路径没被抹掉：{scrubbed}"
    assert "<路径>" in scrubbed, f"应当留下占位符：{scrubbed}"
    assert "line 22" in scrubbed, "脱敏不能把可诊断信息一起删掉"
    assert H.scrub_paths("/home/pi/models/x.bin").find("/home/pi") < 0
    assert H.scrub_paths("普通文本 without paths") == "普通文本 without paths"
    assert H.scrub_paths(None) is None and H.scrub_paths(123) == 123


# 21 ------------- F20：混合档位下 ASR 热词必须真的传下去
def t_hybrid_passes_asr_extra():
    """评审 F20：`_hybrid_transcribe` 的两处 `transcribe(..., "")` 把热词写死成空串，
    而调用方算了 `asr_extra` 却没传进来 —— 发运默认档位就是 hybrid，模块头注释里
    列为**核心设计**的"上一句原文进 ASR 热词"（跨块人名/专名承接）静默失效，
    且只有 ASR 侧断（翻译侧的剧情承接仍生效，所以更难发现）。

    这里用假 hybrid 缓冲 + 假 ASR 直接驱动，断言"喂进去的 asr_extra 原样到达 ASR"。
    """
    import numpy as np

    import server_app as sa

    captured = []
    orig_get_buf = sa._get_hybrid_buffer
    orig_asr = sa.state.get("asr")

    class FakeBuf:
        # reason="vad" 时上层会复用切句期算好的语音区（span 内相对毫秒），
        # 给一段假数据就不会去现场 spawn VAD CLI（测试不该依赖外部进程）
        last_cut_regions = [(0, 1000)]

        def feed(self, pcm, lang, start_ms):
            return (np.zeros(16000, dtype=np.float32), 1000, "vad")   # 立刻切出一句

        def snapshot(self):
            return (np.zeros(16000, dtype=np.float32), 0)

    class FakeAsr:
        def transcribe(self, pcm, lang, start_ms, keep, vad_cfg, seg_cfg, extra=""):
            captured.append(extra)
            return {"language": lang, "segments": [{"start_ms": start_ms,
                                                    "end_ms": start_ms + 900,
                                                    "text": "句"}],
                    "asr_ms": 1.0, "skipped": False}

    try:
        sa._get_hybrid_buffer = lambda: FakeBuf()
        sa.state["asr"] = FakeAsr()
        pcm = np.zeros(16000, dtype=np.float32)

        sa._hybrid_transcribe(pcm, "ja", 0, {}, False, "上一句原文")
        assert captured == ["上一句原文"], \
            f"asr_extra 没传到 ASR（混合档位热词失效）：{captured}"

        # 默认参数时行为不变（不能因为加参数就把空串路径弄坏）
        captured.clear()
        sa._hybrid_transcribe(pcm, "ja", 0, {}, False)
        assert captured == [""], captured
    finally:
        sa._get_hybrid_buffer = orig_get_buf
        sa.state["asr"] = orig_asr


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
    check("keep_segment 跨块去重", t_keep_segment)
    check("模型下载器（进度/断点续传/原子替换）", t_model_downloader)
    check("用户数据迁移（落在安装目录 data\\，多源迁移与补救）", t_user_data_migration)
    check("run_server 在封闭 sys.path 下可 import（embeddable 条件）", t_run_server_embeddable_import)
    check("混合切句规则（standard/hard/silent，R63）", t_hybrid_cut_rules)
    check("混合切句 重叠去重/补零/跳变重置（R63）", t_hybrid_overlap_gap_jump)
    check("混合切句 段-组对齐器（R63）", t_hybrid_align)
    check("混合切句 切点续接（R63 回归锁）", t_hybrid_cutpoint_carry)
    check("混合切句 VAD 裁决切点（R63.1 BGM 场景）", t_hybrid_vad_cut)
    check("F01 字幕配置深合并（所有组，切模型不毁配置）", t_subtitle_config_deep_merge)
    check("F01 流式上游地址与 asr.audiocpp 同源", t_stream_asr_upstream_from_config)
    check("F12 ASR 上游自愈 + /health 如实反映死活", t_asr_selfheal_and_health)
    check("F21/F22 熔断半开恢复 + 空译文不计熔断", t_circuit_breaker_recovers)
    check("F15 流式断流关掉上游连接（GeneratorExit 兜底）", t_stream_disconnect_closes_upstream)
    check("F13 坏配置自恢复 + 写入原子", t_config_corruption_recovery)
    check("F16 8756 跨站栅栏 + 过载快速失败（不涉及跨端契约部分）", t_lan_open_guards)
    check("F02 设置缓存 key/data 成对（杜绝错配命中）", t_settings_cache_pair)
    check("F10/F18 在飞计数口径 + 空闲判定用单调钟", t_inflight_and_idle_clock)
    check("F11 单客户端独占（会话状态进程级单份）", t_single_client_session)
    check("F19 局域网接口不泄漏本机绝对路径", t_lan_endpoints_scrub_paths)
    check("F20 混合档位 ASR 热词真的传下去", t_hybrid_passes_asr_extra)
    if FAILED:
        print(f"\n{len(FAILED)} 项失败：{FAILED}")
        sys.exit(1)
    print("\n全部通过")
