# R03 宽面审查报告：质量 / 安全 / 并发 / 工程一致性

> **⚠️ 第一轮审查存档**：本文是 2026-10-02 凌晨的第 1 轮审查（当时尚未以手机端为
> 真源），**其中部分结论已被修正或推翻**——修正记录见
> `review/REVIEW-全面审查报告-R2.md` §7 与用户裁定（Q1-Q4）；
> 实施状态以 `review/最终修复清单-R2.md` 为准。


- 审查员：broad-auditor（task-3）
- 范围：`host_server.py`（4139 行）、`vendor/subtitle/*`、`vendor/dlna/*`、`vendor/player/*`、`build/*`、`installer/*`、`.gitignore`、`tests/*`、`tools/check_cross_repo_consistency.py`、`ui/app.js` + `ui/index.html`（抽查）
- 方式：静态阅读 + grep 定点 + 只读命令复现（未启动 GUI/服务器，未改任何产品代码）
- 版本基线：`version.json` = 1.0.62 / versionCode 63
- 说明：视频联动新功能的功能正确性由 backend-reviewer / frontend-reviewer 负责，本报告只在其边界外做宽面覆盖

---

## 0. 总览

| 严重级 | 确认的缺陷 | 可疑待验证 |
|---|---|---|
| 阻断 | 0 | 0 |
| 高 | 4 | 0 |
| 中 | 10 | 2 |
| 低 | 7 | 3 |

**没有发现阻断级缺陷。** 项目在若干高危面上做得相当扎实（见 §3「已验证为干净的面」）：产品代码里没有 `shell=True` / `os.system` / `eval` / `exec` / `pickle.load`；没有关闭 SSL 校验；DLNA 的路径 containment（`key_to_path` + `_inside_root` + `_reparse_free_below`）防住了 `..`、盘符注入、ADS、以及联接/符号链接逃逸；SOAP 用正则解析（不存在 XXE / billion laughs 面）；设置读写有 RLock + 原子替换 + 缓存键值成对；对局域网的字段做了 `scrub_paths` 脱敏。

真正的问题集中在：**UI 渲染未转义（可被文件名触发的 XSS）**、**两个媒体库接口的路径校验形同虚设**、**8756/8791 两个局域网端口默认无鉴权**，以及**打包/构建链上对 libmpv 与版本一致性的缺守卫**。

---

## 1. 确认的缺陷

### 1.1 【高】媒体库文件名未转义 → 存储型 XSS（WebView 内可调用全部本机 API）

- 证据：`ui/app.js:1417-1419`（目录名）、`ui/app.js:1429-1432`（视频名）、`ui/app.js:1435`（写入 DOM）
```js
1417  html += '<div class="vl-folder" data-path="' + encodeURIComponent(d.path) + '">' +
1418    '<svg class="ic"><use href="#i-vk-folder"/></svg><div class="name">' + d.name + '</div></div>';
...
1432    '<div class="lib-title">' + v.name + '</div></div>';
1435  $("#vlBrowseBody").innerHTML = html;
```
- 同一文件里 `esc()` 是存在的（`ui/app.js:85`）且别处都用了（`ui/app.js:403`、`405`、`419`），只有 `renderBrowse()` 漏了。
- 影响：`d.name` / `v.name` 直接来自 `/api/library/browse`（服务端 `host_server.py:2319`、`2327` 原样回传 `e2.name`）。在媒体根（或被 §1.2 的遍历缺口带出来的任意目录）里放一个名为 `<img src=x onerror="pywebview.api.win_close();fetch('/api/quit',{method:'POST'})">.mp4` 的文件，打开「媒体库」页即执行任意 JS。页面运行在 `http://127.0.0.1:8790` 这个源上，`Host`/`Origin` 两道栅栏对同源脚本全部放行 ⇒ 可以调 `/api/settings`（改设置）、`/api/device/*`（操作设备）、`/api/quit`（退出程序），并且 `window.pywebview.api`（`host_server.py:4070` `js_api=JSAPI`）还提供了原生窗口控制与文件选择框。这是一条从"文件名"到"本机 API 全控"的完整链路。
- 最小修复：`ui/app.js:1418` 与 `1432` 改成 `esc(d.name)` / `esc(v.name)`（`data-path`/`data-vpath` 已经用了 `encodeURIComponent`，保持）。建议顺手给 `renderBrowse` 加一条"所有动态字段必须过 `esc()`"的注释。

### 1.2 【高】`/api/library/browse` 目录穿越：`..` 未被折叠，前缀校验通过 → 可枚举机器上任意目录

- 证据：`host_server.py:2308-2314`
```python
2308  bp_norm = str(Path(bp))
2309  if not any(str(Path(r)) == bp_norm or bp_norm.startswith(str(Path(r)) + os.sep)
2310             for r in roots):
2311      self._json({"ok": False, "error": "路径不在媒体库目录内"}, 403)
2313  try:
2314      for e2 in os.scandir(bp_norm):
```
- `pathlib.Path` 不折叠 `..`，所以 `bp_norm` 仍是 `E:\Media\..\..\Windows`，`startswith("E:\Media" + "\")` 成立，而 `os.scandir` 会按真实语义进入 `E:\Windows`。已用只读命令复现：
```
bp_norm = E:\Media\..\..\Windows
startswith(root+sep) = True
scandir would list -> E:\Windows
```
- 影响：`/api/library/browse?path=<媒体根>\..\..\<任意目录>` 返回该目录的子目录清单与其中的视频/字幕/脚本存在性、时长、缩略图名。局域网不可达（仅 127.0.0.1），但它把"看得到 / 看不到"的边界抹掉了，并且与 §1.1 组合后可被**任意目录名**触发 XSS。同族缺口的根因正是同一文件 `2122-2124` 注释里已经点明的那条教训（"目录遍历防护必须按路径组件比"），此处没有落地。
- 最小修复：把 `bp_norm = str(Path(bp))` 换成 `bp_norm = str(Path(bp).resolve())`，并把 `startswith(...)` 换成 `Path(bp_norm).is_relative_to(Path(r).resolve())`。

### 1.3 【高】`/api/library/stream` 白名单用裸前缀比较，且源码运行（非 frozen）时白名单被整体关闭 → 任意文件读取

- 证据 A（前缀比较缺分隔符）：`host_server.py:2217-2222`
```python
2217  _abs = str(_fp.resolve()).lower()
2218  _allowed = any(_abs.startswith(str(_P(r).resolve()).lower()) for r in _roots if r)
2219  if not _allowed and not getattr(sys, "frozen", False):
2220      _allowed = True          # 源码/开发运行放宽，便于调试
2221  if not _allowed:
2222      self._json({"ok": False, "error": "这个文件不在媒体库或 DLNA 目录里"}, 403)
```
  已复现 `E:\MediaEvil\x.mp4` 对根 `E:\Media` 判定为放行（`startswith=True`，而 `is_relative_to=False`）。
- 证据 B（开发运行等于无白名单）：`2219-2220`，只要不是 PyInstaller 打包运行（源码模式、`python host_server.py`、`--no-window` 调试），任意路径都被放行。
- 证据 C（无扩展名限制）：`2224-2232` 的 `_ctype` 表只是给类型，缺省 `application/octet-stream`，所以任何后缀都能下；还支持 `Range`（`2237-2263`），可分段拖走大文件。
- 影响：`GET /api/library/stream?path=<任意绝对路径>` 在开发/源码运行下是**任意文件读取**（包括 `data\subtitle_config.json` 里的云端 API Key）；打包版下则可读取与已配置媒体根共享前缀的兄弟目录（如 `E:\MediaBackup\`）。
- 最小修复：`_allowed` 改用 `Path(_abs).is_relative_to(Path(r).resolve())`；把 `2219-2220` 的开发放宽改成"环境变量显式开启"（例如 `NEXUS_DEV_ALLOW_ANY_PATH=1`），不要用 `sys.frozen` 当开关。

### 1.4 【高】8756 字幕服务默认绑 `0.0.0.0` 且 `auth_token` 为空，`/transcribe` 默认翻译 → 局域网无鉴权可烧云端额度 / 占 GPU

- 证据 A（默认配置）：`vendor/subtitle/config.json:84-88`
```json
84  "server": {
85    "host": "0.0.0.0",
86    "port": 8756,
87    "idle_release_min": 5,
88    "auth_token": ""
89  }
```
- 证据 B（空 token = 不校验）：`vendor/subtitle/server_app.py:407-425`
```python
407  def _auth_token() -> str:
409      return str((CFG.get("server") or {}).get("auth_token") or "")
416      if _auth_token() and request.url.path.startswith("/transcribe"):
```
- 证据 C（回环栅栏只保护非 `/transcribe*` 与 `/health`）：`vendor/subtitle/server_app.py:378-391`，`_LAN_OPEN_PREFIXES = ("/transcribe", "/health")`。而 `/transcribe` 的签名默认 `translate: bool = True`（`server_app.py:831-833`）。
- 证据 D（GUI 里没有设置项）：`grep auth_token|X-FSC-Subtitle-Token ui/` 无任何命中 ⇒ 用户在界面上无法开启这个令牌，默认部署恒为"无鉴权"。
- 影响：同网任意设备 POST `/transcribe?translate=true` 就能借用用户配置的云端 Key（`translate.openai.api_key`）翻译任意音频、并占用 GPU/显存导致本机字幕不可用（DoS）。设计注释（`server_app.py:398-403`）明确承认这是"向后兼容"取舍，但代价写在用户账单上；`_OverloadGuard` 与单客户端独占（`_claim_session`）只降低并发，不构成鉴权。
- 最小修复：把 `server.host` 默认改为 `127.0.0.1` 并在需要头显时显式切 `0.0.0.0`；或在 UI 的「识别与翻译」里暴露 `server.auth_token` 输入框，并在 token 为空且绑定非回环时于界面显著告警。

### 1.5 【中】就地更新交接脚本按 ASCII 写盘（`errors="replace"`）→ 非 ASCII 安装路径被替换成 `?`，静默升级装错目录或失败

- 证据：`host_server.py:2856`、`2962-2967`
```python
2856  esc = lambda s: str(s).replace("%", "%%")     # 批处理里 % 要写成 %%
...
2965  cmd_path.write_text(
2966      _build_update_waiter(APP_DIR, setup, app_exe, log_path, os.getpid()),
2967      encoding="ascii", errors="replace")
```
  `_build_update_waiter` 把 `APPDIR / SETUP / APPEXE / LOG` 原样写进 `set "VAR=..."`（`2860-2864`），随后用 `"%SETUP%" ... /DIR="%APPDIR%"` 调安装器（`2908`、`2913`）。
- 影响：安装目录含中文（`D:\软件\FunScriptCast-Nexus`，中文 Windows 用户常见）时，`errors="replace"` 把路径静默变成 `D:\??\FunScriptCast-Nexus`，`/DIR` 指向不存在的目录：静默安装可能失败、或在错误位置装出一份新副本，而日志只留一行 ASCII 记录，用户看到的是"更新完没变化"。文件头注释只声明"批处理只追加 ASCII 状态行"，但实际被编码进去的是**用户数据路径**。
- 最小修复：按控制台 OEM 代码页编码写盘（`ctypes.windll.kernel32.GetOEMCP()` → `encoding=f"cp{cp}"`，`errors="replace"` 仅在真失败时）；或对非 ASCII 安装目录直接拒绝自动更新并提示手动运行安装包。

### 1.6 【中】下载"停滞看门狗"位置错误：涓流时 `r.read(1MB)` 长时间阻塞，看门狗永不触发，通道也不切换

- 证据：`host_server.py:1616-1630`
```python
1619  # 停滞看门狗：代理节点"涓流"时数据一直有但极慢，60s 超时永远不触发，通道切不出去。
1621  if time.time() - last_ok > 90:
1622      raise RuntimeError("下载停滞超过 90 秒（通道无有效进展）")
1623  chunk = r.read(1024 * 1024)
```
- 影响：判断在**阻塞读之前**，而 `HTTPResponse.read(n)` 会一直攒到 `n` 字节或 EOF。若代理以 1 B/s 涓流，`read(1MB)` 要等约 12 天，循环体内的看门狗一次都执行不到；socket 的 60s 超时也不会触发（每一小段数据都按时到达）。表现：模型/更新下载永久卡在某个百分比，不报错、不换通道（`_download_to_file` 的双通道兜底 `1682-1690` 因此形同虚设）。注释里描述的正是要防的场景，实现没有防住。
- 最小修复：改成 `r.read1(65536)`（`HTTPResponse` 支持，返回"当前可用"字节）循环，让看门狗每轮都执行；或用独立守护线程在 `time.monotonic()` 上判定无进展后 `conn.close()`。

### 1.7 【中】`/api/library/script` 完全没有根目录校验 → 任意 `.funscript` 文件读取

- 证据：`host_server.py:2277-2286`
```python
2277  elif path == "/api/library/script":
2280      vp = (q.get("path") or [""])[0]
2281      fs = Path(vp).with_suffix(".funscript")
2283      if (not vp or not Path(vp).is_file() or not fs.is_file()):
2286          data = json.loads(fs.read_text(encoding="utf-8"))
```
- 影响：与 `/api/library/stream` 不同，这里连"是否在媒体根内"都不判：`?path=C:\Users\x\secret.funscript` 直接返回其 JSON 内容（含错误文本回显，见 `2296-2297`）。扩展名固定，泄露面比 §1.3 窄，但它属于同一类缺口且修复成本极低。
- 最小修复：抽出 §1.3 的 roots 校验为一个函数，`/api/library/script` 复用。

### 1.8 【中】`/api/player/open` 只校验扩展名，可打开任意路径的视频（外挂 mpv 窗口）

- 证据：`host_server.py:2430-2437`
```python
2431  vp = str(body.get("path") or "")
2433  if not vp or not re.fullmatch(r".*\.(mp4|mkv|wmv|avi|mov|webm|m2ts|ts)",
2434                                vp, re.I) or not Path(vp).is_file():
```
- 影响：任意本地视频（例如私人视频目录）可被一条本机 POST 直接播放；与 §1.1 的 XSS 组合后，恶意页面名称可以驱动它播放/循环打开文件。属于"越权打开"而非"读取内容"，故列中。
- 最小修复：同 §1.3 复用 roots 校验；`/api/sync/start`（`2457-2464`）传入的 `vp` 也应同样校验（脚本驱动设备）。

### 1.9 【中】libmpv 运行时双份 121MB：代码加载的 `vendor/mpv` 不被 git 跟踪、也没有任何构建步骤生成

- 证据 A（代码只认 `vendor/mpv`）：`vendor/player/mpv_player.py:11-12`、`22`、`33-39`
```python
11  DLL：vendor\mpv\libmpv-2.dll 必须存在（tools\fetch_mpv.ps1 拉取）；import mpv 前
22  DLL_DIR = APP_DIR / "vendor" / "mpv"
33  if (DLL_DIR / "libmpv-2.dll").is_file():
35      os.add_dll_directory(d)
```
  注意 `ensure_dll()` 在文件缺失时**静默跳过**，不报错。
- 证据 B（gitignore 排除它）：`.gitignore` 末行 `vendor/mpv/libmpv-2.dll`；构建脚本里无人调用 `tools\fetch_mpv.ps1`（`Select-String build\*.ps1,installer\*.iss,tools\*.ps1 -Pattern 'fetch_mpv|vendor\\mpv'` 只命中 `tools/fetch_mpv.ps1` 自身）。
- 证据 C（实际靠另一份兜底）：`git ls-files vendor/player` 包含 `libmpv-2.dll`（`git cat-file -s HEAD:vendor/player/libmpv-2.dll` = 121089024），`vendor/player/mpv.py:44-50` 会退回"与 mpv.py 同目录"的 DLL：
```python
44  else:
45      for name in names:
46          dll = os.path.join(os.path.dirname(__file__), name)
47          if os.path.isfile(dll):
48              break
```
- 影响：全新克隆构建时 `dist-app\vendor\mpv` 不存在，安装包能跑**只是**因为仓库里多提交了一份 121MB DLL 在错误位置；一旦有人按 `.gitignore` 与 `fetch_mpv.ps1` 的约定清理它，桌面播放器（视频联动主路径）在所有新装机器上直接失效，而 `ensure_dll()` 不会给出可读错误。顺带：安装包为同一份 DLL 背 121MB 冗余。
- 最小修复：删掉 `vendor/player/libmpv-2.dll` 的跟踪，在 `build_exe.ps1`（组装 dist-app 前）调用 `tools/fetch_mpv.ps1` 并加存在性前置校验，缺失即 `throw`。

### 1.10 【中】仓库 `.git` 已膨胀到 2.15GB（121MB DLL 曾入库）

- 证据：`git count-objects -vH` → `size-pack: 1.73 GiB`，`in-pack: 1890`，`garbage: 0`；`Get-ChildItem .git -Recurse -File | Measure-Object Length -Sum` → 2151 MB；`git log -1 -- vendor/player/libmpv-2.dll` → `f23af86`；`.gitignore` 里 `vendor/llama/`、`models/`、`vendor/audiocpp/`、`vendor/mpv/libmpv-2.dll` 都排除了，唯独漏了 `vendor/player/libmpv-2.dll`。另注：仓库内还留着 `build/_mpv-proto/*.7z`、`build/_mpv-proto/libmpv-2.dll`、`build/qq-promo/*.png`（4MB 级）等构建暂存（`build/_mpv-proto/` 未被 `.gitignore` 覆盖）。
- 影响：克隆/CI 每次都要拉 2GB+；后续所有版本的分支切换与 history 操作都变慢。
- 最小修复：把 DLL 从跟踪中移除并加进 `.gitignore`（同上条修复），必要时 `git filter-repo` 清历史；`.gitignore` 增补 `build/_mpv-proto/`。

### 1.11 【中】8791 头显端口无鉴权：任何局域网主机都能反复拉起字幕服务

- 证据：`host_server.py:3064-3070`（路由白名单内，无来源校验）与 `4024`（绑 `0.0.0.0`）
```python
3064  if path == "/api/subtitle/start":
3065      RT.add_log(f"头显（{self.client_address[0]}）请求启动字幕服务", "info")
3066      res = sub_start()
```
- 影响：`/api/subtitle/start` 会以承载 torch/llama 的 python 拉起 8756 服务并加载模型（数 GB 显存/内存，`sub_start` → `895-996`）；攻击者可在服务空闲回收（5 分钟）后反复拉起，形成资源耗尽（用户 PC 卡顿、显存被占）。同端口 `/api/headset/status` 也会回 `lan_ip`、版本与哈希签名（信息量小，可接受）。这与 §1.4 是"局域网面默认无鉴权"的两个不同入口。
- 最小修复：给 8791 也加一个共享令牌（头显侧请求头，空值时不校验但要提示），或对 `/api/subtitle/start` 做"最近 N 分钟内只允许来自已登记头显 IP/限速"的处理。

### 1.12 【中】DLNA（0.0.0.0）500 响应回显异常原文 → 向局域网泄露本机绝对路径

- 证据：`vendor/dlna/vr_dlna.py:1111-1114` 与 `1453-1456`
```python
1111  except Exception as e:
1112      log.warning("%s %s error: %s", "GET" if want_body else "HEAD", path, e)
1114      self._send_error_text(500, f"server error: {e}", want_body=want_body)
```
  `OSError` 文本本身带完整路径（如 `[WinError 3] ... : 'D:\\Media\\...'`）。同项目在宿主侧已经为此写了 `scrub_paths()`（`host_server.py:1334-1351`），DLNA 侧没有对应处理。
- 影响：局域网任意客户端可用构造的 `/media/<key>` 触发 500，拿到安装路径、媒体根、用户名等；与"DLNA 只暴露媒体"的设计意图不符。
- 最小修复：把 `f"server error: {e}"` 换成固定文案（细节只进 `log.warning`），或复用一份脱敏函数。

### 1.13 【中】测试套件没有框架与隔离：`pytest` 只能收集到 2 个用例，主回归套件与 26 个脚本硬编码 `E:\Development\FunScriptCast-Nexus`

- 证据：`tests/` 全树 `import pytest|pytest.` 与 `import unittest` 均为 0 命中；`(?m)^def test` 仅出现在 `tests/test_merge_shards.py`（2 个）。主回归套件 `tests/test_pipeline_unit.py` 用模块级 `sys.exit(1)` 收尾（文件头 1-40 行注释写明"`.venv/Scripts/python.exe tests/test_pipeline_unit.py`"，301 处 `assert`、无 `def test_*`）。硬编码绝对路径命中：`test_pipeline_unit.py`(24)、`run_eval.py`(9)、`diag/headset_resolve_test.py`(10)、`probe_speech.py`(6)、`hybrid_scheme.py`(5)、`run_combo_eval.py`(5)、`score_combo.py`(5)、`run_full_with_cache.py`(5) 等共 26 个文件。
- 影响：CI 或新同事无法用通用方式跑回归（`python -m pytest tests/` 近乎空转），且换机即大面积不可运行；`tests/diag/*`（18 个 .py + `audiocpp_vram.ps1`）是一次性诊断/评测脚本，与回归测试混在同一目录且无标注。
- 最小修复：加 `tests/conftest.py`（把 `vendor/subtitle` 塞 `sys.path`、设 `NEXUS_USER_DIR` 到 tmp——`test_pipeline_unit.py:29-40` 已经有一套可复制的写法）；把 `test_pipeline_unit.py` 的函数改成 `test_*` 或包一层 `main()` 由 pytest 调用；把 `tests/diag/` 明确标为手工诊断工具（README 或 `pytest.ini` 的 `norecursedirs`），硬编码根目录改为 `Path(__file__).resolve().parents[1]`。

### 1.14 【中】`tools/check_cross_repo_consistency.py` 不检查版本与打包清单，"一致性检查"名不副实

- 证据：`tools/check_cross_repo_consistency.py:37-50` 的 `PAIRS` 全是 Kotlin 文件（phone ↔ VR），第 1-16 行自述"信息性检查/非门禁"；全文没有读 `version.json`、`setup.iss`、`*_setup.ps1` 的逻辑。
- 影响：**版本号漂移与打包漏文件恰好是最需要守卫的两件事，而这个唯一的相关工具两件都不查**（本次宽面发现 §1.9、§1.15、§2.3 都属于它本该拦住的范围）。
- 最小修复：新增一组本地一致性断言（`version.json` / `dist-app\version.json` / setup.iss `MyAppVersion` 由 `-D` 注入的一致性、`dist-app\{ui,vendor,tools,runtime,version.json}` 必须存在、`vendor\mpv\libmpv-2.dll` 必须存在），并入 `build_installer.ps1` 的前置检查（失败即中止）。

### 1.15 【中】`/api/settings` 对 `library_roots` 无元素类型校验，字符串会被存成"字符串"并被下游按字符迭代

- 证据 A（只判 list、不判元素）：`host_server.py:561-563`
```python
561  elif k == "library_roots" and isinstance(v, list):
562      v = [norm_path(x) for x in v if norm_path(x)]
563  s[k] = v
```
- 证据 B（下游假设是列表）：`host_server.py:2303`、`2362`、`4039`
```python
2303  roots = [r for r in (load_settings().get("library_roots") or []) if r]
```
- 影响：POST `{"library_roots":"D:\\Media"}` 会原样落盘；此后 `for r in "D:\Media"` 迭代出 `'D'`、`':'`、`'\\'`… —— 媒体库列表变成一堆单字符"根"，`/api/library/browse` 顶层目录为空、`rescan` 不启动（`4039` 同样按字符判断）。这类脏值会持久化，用户只能手改 JSON 才能恢复。
- 最小修复：在 `save_settings` 里对 `library_roots`/`dlna_roots` 强制 `isinstance(v, list)`，否则拒绝并返回 `{"ok": False, "error": ...}`；同时校验元素为 str。

### 1.16 【低】`/api/state`、`do_GET`/`do_POST` 兜底错误回显异常原文

- 证据：`host_server.py:2084`（`"settings": s` 全量返回，含所有媒体根与设备目录）、`2390`、`2412`、`2674`
```python
2389  except Exception as e:
2390      self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
```
- 影响：仅 127.0.0.1 + Host/Origin 栅栏后可达，泄露面小（异常文本里可能带路径，例如 §1.3/§1.7 的错误分支）；属于"纵深防御"缺口而非可用攻击路径。
- 最小修复：500 分支统一回固定文案，细节只写 `RT.add_log`。

### 1.17 【低】`Host` 头缺失即放行（HTTP/1.0 客户端）——与 §1.3 组合时可绕过 rebinding 栅栏

- 证据：`host_server.py:2115-2118`
```python
2115  host = (self.headers.get("Host") or "").strip().lower()
2116  if not host:
2117      return True
```
- 影响：刻意为之（注释说明"rebinding 攻击必然带 Host"），单独看没问题；但它使"不带 Host 的本地脚本"与"浏览器"共享同一条白名单放宽路径（§1.3 的开发放宽）。属结构性提示。
- 最小修复：保持现状可接受；若采纳 §1.3 的 env 开关，此处可顺便要求"无 Host 时仅 127.0.0.1/::1 来源地址"（`self.client_address[0]`）。

### 1.18 【低】"空闲回收"依赖中文特征串匹配子进程 stdout

- 证据：`host_server.py:1060-1071`
```python
1066  if rc == 0 and "释放模型并退出" in detail:
1067      RT.add_log("字幕服务空闲超时已自动回收（显存已释放；下次使用会自动再启动）", "info")
```
- 影响：服务端文案一改（或输出编码/截断落在 tail 之外）就会被判成"运行中退出（code 0）"，用户看到"异常退出"红色告警。`tail` 只保留 40 行（`1015-1016`），长启动日志下特征行可能已被挤出。
- 最小修复：改为结构化信号（退出码约定，如 `code 0` 且 stdout 末行匹配一个稳定的 ASCII 标记 `[idle-reclaim]`，或服务端写一个 `data/reclaim.flag`）。

### 1.19 【低】版本号分散在两处 HTTP Server 头与陈旧注释里

- 证据：`host_server.py:2095` `server_version = "FSHost/1.0"`、`3003` `server_version = "FSHost-Headset/1.0"`，与 `version.json`(1.0.62) 无关联；`installer/setup.iss:3` 注释仍写 `/DMyAppVersion=1.0.12`；`tools/release_notes.md:3` 最新条目是 `## 1.0.42`。
- 影响：无功能影响；排查时容易被 `Server: FSHost/1.0` 误导（以为是 1.0 版）。版本能力判断本身已正确（`app_version()` 每次读 `version.json`，`_ver_tuple` 也剥掉了"（开发副本）"尾巴）。
- 最小修复：`server_version` 由 `app_version()["code"]` 拼出；注释里的示例版本改成 `<version>`；`release_notes.md` 交给 `tools/bump_version.ps1` 追加。

### 1.20 【低】`_autostart_enabled()` 用区分大小写的字符串比较判定注册表值

- 证据：`host_server.py:344-354`
```python
344  if not getattr(sys, "frozen", False):
345      return False                 # 源码运行没有可注册的 exe
...
352      return bool(str(v).strip('" ')) and             str(v).strip('" ') == str(sys.executable)
```
- 影响：Windows 路径不区分大小写，而 `str` 比较区分；若注册表值来自旧版本/手改（大小写或 `\\?\`/短名形式不同），界面会显示"开机自启=关"，用户重新打开开关时才会被修正（`_set_autostart`）。功能可自愈，故低。
- 最小修复：两侧都过 `os.path.normcase(os.path.normpath(...))` 再比。

### 1.21 【低】`host_server.py:2560` 死分支 `elif False: pass`

- 证据：`host_server.py:2556-2566`（`/api/quick` 分发里的空分支）
```python
2560  elif False:
2561      pass
2562  elif kind == "pause":
```
- 影响：无功能影响，但它是"曾经有过一个分支被删掉"的遗迹，读代码时会误以为分支被穷举处理过。
- 最小修复：删除 `2560-2561`。

---

## 2. 可疑待验证

### 2.1 【可疑/低】`user_paths._migrate_once` 失败后仍标记 `_migrated`，同一进程内不再重试
- 证据：`vendor/subtitle/user_paths.py:198-201`
```python
198  with _migrate_lock:
199      if key in _migrated:
200          return dst.exists()
201      _migrated.add(key)
```
  标记发生在真正迁移（`206 _atomic_copy`）之前；若 `_atomic_copy` 因占用/权限失败，本次进程后续调用会直接返回 `dst.exists()`=False，直到重启才重试。
- 需要验证：失败后调用方是否有别的重试路径（`ensure_user_data` 的一次性补救扫描 `232-239` 注释提到"只跑一次"的标记）。
- 建议：把 `_migrated.add(key)` 移到成功后（或失败时 `_migrated.discard(key)`）。

### 2.2 【可疑/低】`_download_once` 的 `.part` 复用与 `Content-Length` 缺失分支
- 证据：`host_server.py:1600-1615`（`done and not ranged` → 归零重下；`cl == 0` → 归零重下）
- 需要验证：hf-mirror / modelscope 在 206 响应里是否总是带 `Content-Length`；若有的 CDN 用 `Transfer-Encoding: chunked` 回 206，每次断点续传都会退化成"全量重下"，大模型（4GB）在弱网下永远下不完（表现为进度反复归零）。
- 建议：真机用 `-Range` 复现一次断点续传，确认 `total` 非 0。

### 2.3 【可疑/低】`save_subtitle_config` 无 schema 校验，任意键/类型可写入 config.json
- 证据：`host_server.py:3167-3176`（组内 `values.items()` 直接写；组不是 dict 时 `cfg[group] = values`）
- 需要验证：前端是否会回传 `server.host` 之类的高危键（若可，则 §1.4 的 `0.0.0.0` 绑定可由 HTTP 改写并持久化）。属于"合法但危险"的接口面。
- 建议：对 `server` 组做白名单/类型校验（host 仅允许回环或显式确认）。

### 2.4 【可疑/低】`free_translators` 会把字幕原文发往第三方免费接口
- 证据：`vendor/subtitle/free_translators.py:74-75`（`translate.google.com`）、`144-145`（`edge.microsoft.com` / `api-edge.cognitive.microsofttranslator.com`）、触发点 `vendor/subtitle/translate_engine.py:956-966`、开关默认关闭 `vendor/subtitle/config.json:52-56`（`fallback.enabled=false`）
- 需要验证：UI 在开启"免费兜底"时是否明确告知"你的字幕文本会发送到 Google/微软的公开翻译接口"（隐私知情）。目前只在服务端 stdout 打印一行 `⚠️ 已切换到免费兜底`（`translate_engine.py:971-974`），用户看不到。
- 建议：在设置页该开关旁写明数据去向；或默认不提供该选项。

### 2.5 【可疑/低】`tests/test_pipeline_unit.py` 是否真能在当前环境跑通
- 未执行（避免引入副作用与耗时）；文件自述"不依赖 GPU / 后端进程，秒级完成"（`tests/test_pipeline_unit.py:1-12`）。建议由 task-4 的 verifier 实跑一次并记录命令与输出。

---

## 3. 已验证为干净的面（避免重复审查）

- **命令注入**：全仓（除 `dist-app/`）`shell=True|os.system(|eval(|exec(|pickle.load` 零命中；`_kill_tree`（`host_server.py:1121`）、adb（`vendor/dlna/funscript_sync.py:108-110`、`video_sync.py:115-117`）、`cmd.exe /c <cmd路径>`（`host_server.py:2972`）全部走列表参数。
- **TLS**：产品代码无 `_create_unverified_context` / `CERT_NONE` / `verify=False`（仅 `tests/diag/ssl_probe.py` 打印默认 context）。
- **DLNA 路径 containment**：`vendor/dlna/vr_dlna.py:519-577`（`key_to_path` 拒绝绝对路径、`: `ADS/盘符、`_inside_root` 逐父目录比对、`_reparse_free_below` 逐段拒 reparse point）、`580-617`、`622-644`（`collect_scripts` 复用同一套判定）；`/scripts` 与 `/media/*` 都过这道门。SOAP 用正则解析（`1460-1473`），无 XML 解析器 ⇒ 无 XXE/billion-laughs；`do_POST` 有 1MB 上限（`1419-1434`）。
- **设置并发**：`_SETTINGS_LOCK`(RLock) 内完成读改写（`host_server.py:523-587`），缓存 key/data 成对读写（`385-386`、`438-439`、`582-586`），`os.replace` 原子落盘，读失败时拒绝覆盖并留 `.bak`（`526-537`）。
- **局域网回显脱敏**：`headset_status()` 的 `error`/`asr` 过 `scrub_paths`（`host_server.py:1397-1400`、`1340-1351`），头显接口只回标识类字段（`1374`）。
- **8756 的三个中间件**：回环栅栏（`server_app.py:382-395`，fail-closed 写法正确）、可选令牌常数时间比较（`414-425`）、Origin 栅栏（`428-448`）、过载快速失败（`456-479`）、流式桥的阻塞调用确实进了 `run_in_threadpool`（`stream_bridge.py:406-417`、`443-445`）。
- **打包密钥哨兵**：`build/build_installer.ps1:17-29`（仓库 config 带 key 即拒绝打包）+ `64-119`（dist-app 全树扫描，占位符白名单窄化到精确值）+ `46-53` + `153-173`（摘除/回填放 try/finally）。
- **安装器数据保护**：`installer/setup.iss:74-98` 的 `[InstallDelete]` 只删代码，注释明确永不动 `data\ logs\ models\ cache\ run\`（`:77-84`）；`data` 种子为 `onlyifdoesntexist`（`:121-122`）；`AppId` 与卸载项一致（`:31`、`:150-153`）。
- **打包一致性（版本号）**：`version.json` 是唯一来源，`build_installer.ps1:125` 从 `dist-app\version.json` 读取并以 `-DMyAppVersion` 注入（`:150`），`setup.iss:26-27` 只在未注入时退 `0.0.0`；`build_exe.ps1:28-35` 把 bump 放在 EXE 产出之后避免空耗版本号。未发现"同一版本号被写成多个不同值"的实例（§1.19 只是注释陈旧）。

---

## 4. 复现命令（只读，未改动任何文件）

```powershell
# 1.3 前缀比较缺分隔符
.\.venv\Scripts\python.exe -c "from pathlib import Path; p=Path(r'E:\MediaEvil\x.mp4'); r=Path(r'E:\Media'); print(p.resolve().is_relative_to(r.resolve()), str(p.resolve()).lower().startswith(str(r.resolve()).lower()))"
# -> False True

# 1.2 `..` 不折叠，startswith 通过
.\.venv\Scripts\python.exe -c "from pathlib import Path; import os; bp=r'E:\Media\..\..\Windows'; print(str(Path(bp)), any(str(Path(bp)).startswith(str(Path(r))+os.sep) for r in [r'E:\Media']), os.path.normpath(str(Path(bp))))"
# -> E:\Media\..\..\Windows True E:\Windows

# 1.9 / 1.10 DLL 跟踪与体积
git ls-files vendor/player; git cat-file -s HEAD:vendor/player/libmpv-2.dll; git count-objects -vH

# 1.13 测试面
Select-String -Path tests\*.py,tests\diag\*.py -Pattern 'import pytest|import unittest'
Select-String -Path tests\*.py,tests\diag\*.py -Pattern '[A-Za-z]:\\'
```

---

## 5. 给 Lead 的优先级建议

1. 先修 §1.1（一行 `esc()`）+ §1.2（一次 `resolve()`）：两者相加是一条"文件名 → 本机 API 全控"的链路，修复成本最低、收益最高。
2. 再修 §1.3（`is_relative_to` + 去掉 `sys.frozen` 放宽）与 §1.4（默认回环 / UI 暴露 token）：这两条是"本机任意文件读取"和"局域网烧钱"。
3. §1.5（非 ASCII 路径更新）与 §1.6（下载卡死）是用户可直接撞到的功能缺陷，建议同批修。
4. §1.9/§1.10 是仓库工程债，修完能同时消掉"打包漏运行时"的隐患；§1.14 的检查项正好可以作为防回归门禁。
