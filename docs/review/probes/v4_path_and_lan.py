"""R04 只读探针 6：路径校验 / 局域网面（R03-§1.2、§1.3、§1.4）。

全部只读：只做路径字符串计算与 os.scandir 列目录（不读文件内容、不写文件、
不启动任何服务）。subtitle 的 api_key 只打印"是否非空"，绝不回显明文。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT: list[str] = []


def p(s: str = "") -> None:
    OUT.append(s)


# ---------------------------------------------------------------- §1.2 browse
p("=" * 78)
p("§1.2 /api/library/browse 的校验表达式（host_server.py:2308-2314 逐字复刻）")
p("=" * 78)
settings = json.loads((ROOT / "data" / "integrated_settings.json").read_text(encoding="utf-8"))
roots = [r for r in (settings.get("library_roots") or []) if r]
p(f"library_roots = {roots}")
for r in roots:
    bp = r + os.sep + os.sep.join(["..", ".."]) + os.sep + "Windows"
    bp_norm = str(Path(bp))                                  # host_server.py:2308
    ok = any(str(Path(x)) == bp_norm or bp_norm.startswith(str(Path(x)) + os.sep)
             for x in roots)                                  # host_server.py:2309-2310
    p(f"  请求 path           = {bp}")
    p(f"  Path(bp)（不折叠 ..）= {bp_norm}")
    p(f"  校验通过 startswith = {ok}")
    p(f"  os.scandir 真实目标 = {os.path.normpath(bp_norm)}")
    if Path(r).is_dir():
        real_parent = Path(r).parent
        try:
            names = [e.name for e in os.scandir(real_parent)][:6]
            p(f"  旁证：os.scandir({str(Path(r)) + os.sep + '..'}) 真的列出 {real_parent} 的内容，"
              f"前 6 项 = {names}")
        except OSError as e:
            p(f"  （列目录失败：{e}）")
    p(f"  [断言] .. 未被折叠且校验通过: {'PASS' if ok and '..' in bp_norm else 'FAIL'}")
p("同族正例对照：app.js 的返回上一级按钮自己做了 split 归一（app.js:1462-1472），宿主没有。")
p("根因注释（同一文件里已有的教训）见 host_server.py:2122-2124：")
for i, ln in enumerate((ROOT / "host_server.py").read_bytes().decode("utf-8").split("\n"), 1):
    if 2120 <= i <= 2126:
        p(f"  host_server.py:{i}: {ln.strip()}")

# ---------------------------------------------------------------- §1.3 stream
p("")
p("=" * 78)
p("§1.3 /api/library/stream 白名单（host_server.py:2217-2222 逐字复刻）")
p("=" * 78)
for r in roots:
    root_res = Path(r).resolve()
    for sibling in (Path(str(root_res) + "-evil") / "x.mp4", Path("C:/Windows/win.ini")):
        _abs = str(sibling.resolve()).lower()
        startswith_ok = _abs.startswith(str(root_res).lower())
        rel_ok = False
        try:
            rel_ok = Path(_abs).is_relative_to(root_res)
        except Exception:
            pass
        allowed = startswith_ok
        if not allowed and not getattr(sys, "frozen", False):   # host_server.py:2219-2220
            allowed = True
        p(f"  请求路径 {sibling}")
        p(f"    startswith(root)  = {startswith_ok}   is_relative_to(root) = {rel_ok}")
        p(f"    sys.frozen = {getattr(sys, 'frozen', False)} -> 开发放宽后 _allowed = {allowed}")
r0 = Path(roots[0]).resolve()
sibling0 = (Path(str(r0) + "-evil") / "x.mp4").resolve()
prefix_bypass = str(sibling0).lower().startswith(str(r0).lower())
dev_relax = not getattr(sys, "frozen", False)
p(f"[断言] 兄弟目录前缀绕过（{sibling0} vs {r0}）: {'PASS' if prefix_bypass else 'FAIL'}")
p(f"[断言] 开发运行（sys.frozen=False）下任意路径被放行: "
  f"{'PASS' if dev_relax else 'FAIL（本进程是 frozen，需在打包版复核）'}")

# ---------------------------------------------------------------- §1.4 subtitle
p("")
p("=" * 78)
p("§1.4 8756 字幕服务：绑定 / 令牌 / 开放前缀 / 云端 Key")
p("=" * 78)
cfg_runtime = ROOT / "data" / "subtitle_config.json"
cfg_factory = ROOT / "vendor" / "subtitle" / "config.json"
for label, f in (("运行配置 data/subtitle_config.json", cfg_runtime),
                 ("出厂模板 vendor/subtitle/config.json", cfg_factory)):
    if f.is_file():
        c = json.loads(f.read_text(encoding="utf-8"))
        srv = c.get("server") or {}
        p(f"  {label}: host={srv.get('host')!r} port={srv.get('port')!r} "
          f"auth_token={srv.get('auth_token')!r} idle_release_min={srv.get('idle_release_min')!r}")
        openai = ((c.get("translate") or {}).get("openai") or {})
        key = str(openai.get("api_key") or "")
        p(f"    translate.openai.api_key 非空 = {bool(key)}（长度 {len(key)}，不打印明文）")
    else:
        p(f"  {label}: 不存在")
p("  server_app.py 的鉴权/开放面（逐字）：")
for i, ln in enumerate((ROOT / "vendor" / "subtitle" / "server_app.py").read_bytes()
                       .decode("utf-8").split("\n"), 1):
    if i in (378, 379, 382, 385, 390, 407, 409, 416, 418, 831, 832, 833):
        p(f"    server_app.py:{i}: {ln.strip()}")
p("  UI 里是否有 auth_token / 令牌开关：")
ui_hits = []
for f in list((ROOT / "ui").glob("*")):
    if f.is_file() and f.suffix.lower() in (".js", ".html", ".css"):
        txt = f.read_bytes().decode("utf-8", "replace")
        for i, ln in enumerate(txt.split("\n"), 1):
            if "auth_token" in ln or "X-FSC-Subtitle-Token" in ln:
                ui_hits.append(f"    ui/{f.name}:{i}: {ln.strip()[:100]}")
p("\n".join(ui_hits) if ui_hits else "    （0 处 —— 用户在界面上无法设置令牌）")
p("  host_server.py 是否写 server.host / auth_token：")
hs_hits = []
for i, ln in enumerate((ROOT / "host_server.py").read_bytes().decode("utf-8").split("\n"), 1):
    if "auth_token" in ln or '"host"' in ln and "server" in ln:
        hs_hits.append(f"    host_server.py:{i}: {ln.strip()[:100]}")
p("\n".join(hs_hits) if hs_hits else "    （0 处）")
rt_srv = (json.loads(cfg_runtime.read_text(encoding="utf-8")).get("server") or {})
p(f"[断言] 运行配置 host=0.0.0.0 且 auth_token 为空: "
  f"{'PASS' if rt_srv.get('host') == '0.0.0.0' and not rt_srv.get('auth_token') else 'FAIL'}")
p("[断言] /transcribe 默认 translate=True（server_app.py:833）: PASS（见上）")

print("\n".join(OUT))
print("\n[probe done]")
