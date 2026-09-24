# -*- coding: utf-8 -*-
"""用户数据目录：**所有"会变的"数据都放在安装目录里，不写 C 盘**。

## 规则

安装目录 = 用户安装时自己选的那个文件夹（`vendor\subtitle` 的上两级；打包后就是 exe 所在
目录，源码模式就是仓库根）。用户数据一律落 `<安装目录>\\data\\`：

    <安装目录>\\data\\subtitle_config.json     云端 key / ASR / 分段 / 翻译配置
    <安装目录>\\data\\integrated_settings.json 宿主设置（DLNA 共享目录等）
    <安装目录>\\data\\vr_dlna_settings.json    DLNA 设置
    <安装目录>\\models\\                       模型（ASR / 翻译 / HF 缓存）
    <安装目录>\\logs\\                         日志

装到 D 盘就全在 D 盘，装到移动硬盘就跟着走（便携）。**C 盘一个字节都不落**
（`%APPDATA%` / `%USERPROFILE%\\.cache` 都不用）。

环境变量 `NEXUS_USER_DIR` 可整体覆盖数据目录（评测/测试**必须**设它，否则会写到真实安装目录）。

## 为什么是安装目录，而不是 %APPDATA%

早期两种做法都试过，各有各的病，这里是第三种、也是最终形态：

  · 数据全塞 `{app}\\vendor\\subtitle\\`（和 .py 混着）→ 升级安装的 `[Files]` 整树覆盖会
    把用户的 key 换成打包机副本；工具链还要写"摘出→回填"特例。
  · 数据搬 `%APPDATA%\\FunScriptCast-Nexus\\` → 不再被覆盖，但**装到别的盘也没用**，
    大文件（模型缓存）照样压 C 盘；而且"程序在 D 盘、数据在 C 盘"两处寿命，用户删了
    安装目录却发现设置还在，看起来像没删干净。

现在：**一处、一个寿命、一个清理入口** —— 同一个安装目录下。
  · 升级安装：`data\\` 是运行期产物，安装器不安装它、也不删它 ⇒ 自然保留（见 setup.iss）。
  · 删掉安装目录：程序与数据一起走，干净且**符合预期**（要留就整个文件夹留着）。
  · `data\\` 与代码分离 ⇒ 工具链（sync_distapp / build_exe）不必再为运行数据写特例。

## 首次运行迁移

老版本的数据在别处，首次运行时**按"新方案优先"的顺序找一个存在的迁过来**：

  1. `<安装目录>\\data\\…`        当前方案（已迁移过就什么都不做）
  2. `%APPDATA%\\FunScriptCast-Nexus\\…`   R48 过渡版位置（subtitle_config.json 等）
  3. `<安装目录>\\vendor\\subtitle\\…`     最早的位置（config.json，也是出厂模板）

**保存永远写 `data\\`**，第 3 条那份从此只是出厂模板（只读兜底）。

## 一条给未来的约定

用户数据文件是**首次运行时的快照**，之后不随包更新。所以**新加的配置键必须在代码里带默认值**
（`cfg.get("新键", 默认)`），不能指望模板里那份能传到老用户手里——`asr.whisper`、
`drop_latin_hallucination` 都是这么处理的。
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path

CFG_NAME = "subtitle_config.json"

# 安装目录：本文件在 <安装目录>\vendor\subtitle\ 下，所以上两级就是安装目录
_ROOT = Path(__file__).resolve().parents[2]

# R48 过渡版把用户数据放在这里；现在要迁回安装目录（见模块注释"首次运行迁移"）。
_OLD_APPDATA = "FunScriptCast-Nexus"

_migrate_lock = threading.Lock()
_migrated: set = set()


def user_dir() -> Path:
    """用户数据目录：`<安装目录>\\data`（`NEXUS_USER_DIR` 可整体覆盖）。"""
    env = os.environ.get("NEXUS_USER_DIR")
    if env:
        return Path(env)
    return _ROOT / "data"


def models_dir() -> Path:
    """模型目录：`<安装目录>\\models`（ASR / 翻译 GGUF / HF 缓存都在这下面）。"""
    return _ROOT / "models"


def logs_dir() -> Path:
    """日志目录：`<安装目录>\\logs`（宿主与字幕服务共用一处，排查时只看一个地方）。"""
    return _ROOT / "logs"


def run_dir() -> Path:
    """运行时临时目录：`<安装目录>\\run`（子进程配置、音频转储、同步临时包）。

    **为什么不放 %TEMP%**：%TEMP% 在系统盘上。装到 D 盘的用户往往正是因为 C 盘紧张，
    而这些临时文件可能很大（VAD 转储的 wav、设备同步的 zip）。放安装目录里还有个好处：
    同一卷上 rename 是瞬时的，跨卷搬运要真拷一遍。
    内容随时可删（进程停掉后没有需要保留的东西）。
    """
    d = _ROOT / "run"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def download_dir() -> Path:
    """下载暂存目录：`<安装目录>\\models\\_download`。

    模型与 llama 运行时的压缩包**先落这里再解压**。此前落 `%TEMP%`：一个 627MB 的
    llama 运行时包、几个 GB 的模型包都要先在系统盘上占位——C 盘小的机器会直接下载失败，
    而 %TEMP% 的清理工具还可能删掉半截包、白下。放目标盘上还与解压目标同卷。
    支持断点续传：文件名固定（见 host_server 的 `id_--rel`），重启后接着下。
    """
    d = models_dir() / "_download"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def hf_cache_dir() -> Path:
    """HF 缓存根：`<安装目录>\\models\\hf-cache`。

    **为什么必须指过来**：whisper 兜底模型约 1.4 GB，HF 默认缓存是
    `%USERPROFILE%\\.cache\\huggingface` —— 装到 D 盘也照样压在 C 盘上。
    """
    return models_dir() / "hf-cache"


def apply_hf_env() -> Path:
    """把 `HF_HOME` 指到安装目录，返回最终生效的缓存根。

    **必须在 import huggingface_hub 之前调用**：hub 的缓存路径在 import 时读成常量，
    运行期再改环境变量不生效（同 whisper_backend 里 HF_ENDPOINT 那条注释的坑）。
    用户若自己设了 `HF_HOME` 就尊重他（不覆盖），只记下来。
    """
    cur = os.environ.get("HF_HOME")
    if not cur:
        os.environ["HF_HOME"] = str(hf_cache_dir())
        cur = os.environ["HF_HOME"]
    else:
        print(f"[paths] 沿用外部设置的 HF_HOME={cur}（模型缓存不落安装目录，请自行确认盘符）",
              flush=True)
    return Path(cur)


# 旧位置（HF 默认缓存）——在 C 盘，1.4GB 的 whisper 模型默认落这儿
def legacy_hf_hub() -> Path:
    """HF 旧缓存位置：`%USERPROFILE%\\.cache\\huggingface\\hub`。"""
    return Path(os.environ.get("USERPROFILE") or Path.home()) / ".cache" / "huggingface" / "hub"


def adopt_legacy_hf_model(repo_dirname: str) -> bool:
    """把**指定的那一个** HF 模型仓从旧缓存（C 盘）搬进安装目录，成功返回 True。

    为什么只搬一个：旧缓存是**全局共享**的。实测本机 `.cache\\huggingface` 有 1.8GB，
    其中 374MB 是 CLIP 与 WD-tagger —— 那是别的项目在用的，整目录搬走等于砸别人的东西。
    所以只搬本项目自己要的那个仓（`models--kotoba-tech--kotoba-whisper-v2.0-faster`）。

    先 copytree 到临时目录、成功了再改名就位：copytree 中途失败（断电/磁盘满/用户手滑）
    只会在原地留一个 `.adopting` 临时目录，**目标路径始终不出现**——否则下次运行看到
    "目标已存在"就跳过搬迁，whisper 会去读一份残模型（比搬不动更糟）。
    搬完删源，C 盘真正腾出来。调用方还要有"读旧位置"的兜底（见 whisper_backend），
    这里失败也不能让识别挂掉。
    """
    dst_root = Path(os.environ.get("HF_HOME") or hf_cache_dir()) / "hub"
    dst, src = dst_root / repo_dirname, legacy_hf_hub() / repo_dirname
    tmp = dst_root / (repo_dirname + ".adopting")
    try:
        if dst.exists() or not src.is_dir():
            return dst.exists()
        dst_root.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(tmp, ignore_errors=True)   # 清掉上次中断留下的半成品
        print(f"[paths] 正在把模型缓存搬进安装目录（约 1.4GB，只此一次）：{src} → {dst}",
              flush=True)
        shutil.copytree(src, tmp)
        os.replace(tmp, dst)
        shutil.rmtree(src, ignore_errors=True)
        print(f"[paths] 模型缓存已搬入安装目录，C 盘那份已释放：{dst}", flush=True)
        return True
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"[paths] 模型缓存搬迁失败（忽略，将回退读旧位置）：{type(e).__name__}: {e}",
              flush=True)
        return False


def _ensure_dir() -> Path:
    d = user_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def _legacy_sources(name: str, base_dir: Path) -> "list[Path]":
    """历史位置候选**文件**列表，按"新方案优先"排（只含真正存在的）。

    同一份数据在各处的文件名不一样：配置在新方案里叫 `subtitle_config.json`，
    而最早的安装目录那份叫 `config.json`（出厂模板的名字，不改）。
    """
    appdata = Path(os.environ.get("APPDATA") or Path.home()) / _OLD_APPDATA
    if name == CFG_NAME:
        cands = [appdata / CFG_NAME, Path(base_dir) / "config.json"]
    else:
        cands = [appdata / name, Path(base_dir) / name]
    return [p for p in cands if p.exists()]


def _legacy_source(name: str, base_dir: Path) -> "Path | None":
    """该数据在历史位置里**最新方案的那一份**（都不存在则 None）。"""
    found = _legacy_sources(name, base_dir)
    return found[0] if found else None


def _migrate_once(name: str, base_dir: Path) -> bool:
    """把该数据从最近的历史位置迁到 `data\\`（目标已存在则跳过）。

    失败只记不抛——启动不能因为迁移挂掉（回退读历史位置，功能不受影响）。
    **写临时文件再 os.replace**（评审 F13）：两个进程同时迁（宿主与服务可能并发首启）时，
    最坏是各写一份同样内容，不会留下半截文件。临时名带 pid/随机后缀：此前是固定的
    `.migrating`，两进程会互相交错写**同一个**临时文件，先完成者的 replace 可能把
    对方写坏的半截内容扶正。
    """
    src, dst = _legacy_source(name, base_dir), user_dir() / name
    key = str(dst).lower()
    with _migrate_lock:
        if key in _migrated:
            return dst.exists()
        _migrated.add(key)
    try:
        if dst.exists() or src is None:
            return dst.exists()
        _ensure_dir()
        _atomic_copy(src, dst)
        print(f"[paths] 已迁移用户数据：{src} → {dst}", flush=True)
        return True
    except Exception as e:
        print(f"[paths] 迁移 {name} 失败（忽略，回退读历史位置）：{type(e).__name__}: {e}",
              flush=True)
        return False


def _atomic_copy(src: Path, dst: Path) -> None:
    """原子复制：写唯一临时名 → os.replace。

    唯一后缀（pid + 纳秒）解决"固定 .migrating 互相交错"（评审 F13）；失败时清掉自己的
    临时文件，绝不在目标位置留下半截内容。
    """
    tmp = dst.with_name(f"{dst.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    finally:
        try:
            tmp.unlink(missing_ok=True)      # replace 成功后它已不存在
        except OSError:
            pass


# ---- 一次性补救扫描（布局 v3 = 用户数据落在安装目录 data\ 的那一版）-------------------
#
# 迁移是**先到先得**的：目标文件一旦存在，后来的源就不再被采纳。多实例/多版本混跑时
# 谁先启动谁定音，用一份"更差"的源播了种，好数据就永远进不来（R48 实测踩中过：仓库侧
# 先用空 key 模板播种，装好的程序里那份真实 key 再也迁不过来）。
#
# 所以补一次**只跑一次**的补救：在数据目录里落个标记，标记不存在时用
# 「key 以有值者为准」把所有历史源兜一遍（被覆盖的先备份）。
# 标记写完就不再回头——把这点"聪明"限制在升级过渡这一次。
_LAYOUT_MARKER = ".layout-v3"
_layout_checked: set = set()


def _openai_key(p: Path) -> "tuple[str, str]":
    """(api_key, api_key_env)；读不动就是两个空串。"""
    try:
        t = (json.loads(p.read_text(encoding="utf-8")).get("translate") or {}).get("openai") or {}
        return str(t.get("api_key") or "").strip(), str(t.get("api_key_env") or "").strip()
    except Exception:
        return "", ""


def _backup(p: Path) -> None:
    """被补救覆盖前先留一份后悔药（已有备份就不再叠）。"""
    bak = p.with_suffix(p.suffix + ".bak-layout-v3")
    try:
        if p.exists() and not bak.exists():
            shutil.copy2(p, bak)
    except OSError:
        pass


def _recover_layout(base_dir: Path) -> None:
    d = _ensure_dir()
    stamp = d / _LAYOUT_MARKER
    try:
        if stamp.exists():
            return
    except OSError:
        return
    try:
        dst = d / CFG_NAME
        if dst.exists():
            k_new, env_new = _openai_key(dst)
            # 只在"用户这份没有任何 key 来源"时让历史源说话：用户若显式用
            # api_key_env（key 放环境变量里），空 api_key 是他有意为之。
            # 遍历**所有**历史源而不是只看最新那个——最新那个可能恰好是空模板。
            if not k_new and not env_new:
                for src in _legacy_sources(CFG_NAME, base_dir):
                    if _openai_key(src)[0]:
                        _backup(dst)
                        # **原子写**（评审 F13）：此处原来是 shutil.copy2 直写现役配置，
                        # 与 _migrate_once 的 tmp+os.replace 不一致——中途失败就留下半截
                        # JSON，而坏 subtitle_config.json 会让 server_app 在 import 阶段
                        # 就崩（模块级 CFG = load_config(...)），界面还没有修复入口。
                        _atomic_copy(src, dst)
                        print(f"[paths] 补救：配置的 api_key 为空而 {src.parent} 里有值 "
                              f"→ 采纳它（原文件已备份 {dst.name}.bak-layout-v3）", flush=True)
                        break
        stamp.write_text("用户数据布局 v3（安装目录 data\\）：一次性补救扫描已完成\n",
                         encoding="utf-8")
    except Exception as e:
        print(f"[paths] 布局补救扫描失败（忽略，继续用现有文件）：{type(e).__name__}: {e}",
              flush=True)


def _check_layout(base_dir: Path) -> None:
    key = str(base_dir).lower()
    if key in _layout_checked:
        return
    _layout_checked.add(key)
    _recover_layout(base_dir)


def config_path(base_dir: Path) -> Path:
    """字幕服务配置文件路径（`data\\subtitle_config.json`）。

    调用方只读它；保存也写它。历史位置仅作为**只读兜底**（连出厂模板都没有时）。
    """
    base = Path(base_dir)
    _check_layout(base)
    dst = _ensure_dir() / CFG_NAME
    if dst.exists():
        return dst
    if _migrate_once(CFG_NAME, base):
        return dst
    legacy = _legacy_source(CFG_NAME, base)
    return legacy if legacy is not None else dst


def ensure_user_data(base_dir: Path) -> dict:
    """把该迁的都迁一遍，返回解析结果（宿主启动时调一次即可）。

    返回 {"dir", "config", "migrated": [...]}。
    """
    base = Path(base_dir)
    return {"dir": _ensure_dir(), "config": config_path(base), "migrated": []}


def load_config(base_dir: Path) -> dict:
    """读配置（`data\\` 优先，缺失时迁/读历史位置）。

    **坏配置自恢复**（评审 F13）：解析失败时把坏文件改名隔离、再从历史位置/出厂模板
    重建，实在没有就返回 `{}`（各消费方都有代码内默认值）。此前是直接把异常抛给调用方，
    而 `server_app` / `stream_bridge` 都是**模块级** `CFG = load_config(...)` ⇒ 一个坏
    subtitle_config.json 让服务 import 阶段就崩，宿主只看到 `code 1`，界面上没有任何
    修复入口（对照 integrated_settings 早有 refuse+backup 防护）。

    顺带把该迁的都迁一遍：把迁移收进这一个入口，任何读配置的路径都拿到完整状态，
    不会出现"迁移只做了一半"的静默半迁移。
    """
    base = Path(base_dir)
    ensure_user_data(base)
    p = config_path(base)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        bad = _quarantine(p, e)
        # 隔离后按"首次迁移"的逻辑重建：历史位置里那份通常还是好的（出厂模板也有）。
        _migrated.discard(str(p).lower())
        _layout_checked.discard(str(base).lower())
        ensure_user_data(base)
        try:
            cfg = json.loads(config_path(base).read_text(encoding="utf-8"))
            print(f"[paths] 已从历史位置/出厂模板重建配置（坏的留作 {bad.name}）", flush=True)
            return cfg
        except Exception:
            print("[paths] 配置重建失败，本次按代码内默认值运行（各消费方都有默认值）",
                  flush=True)
            return {}


def _quarantine(p: Path, err: Exception) -> Path:
    """把解析失败的配置文件改名隔离（保留证据、不阻断启动）。返回隔离后的路径。"""
    bad = p.with_name(f"{p.name}.bad-{time.strftime('%Y%m%d-%H%M%S')}")
    try:
        os.replace(p, bad)
        print(f"[paths] ⚠️ 配置文件解析失败（{type(err).__name__}: {err}）→ 已隔离为 "
              f"{bad.name}，将从历史位置/出厂模板重建", flush=True)
    except OSError as e:
        print(f"[paths] ⚠️ 配置文件解析失败且隔离失败（{e}）：{type(err).__name__}: {err}",
              flush=True)
    return bad
