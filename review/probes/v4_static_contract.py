"""R04 只读探针 1：静态契约核对（task-4 verifier 独立编写）。

覆盖 R01/R02/R03 的高危结论里可以**离线证伪或证实**的部分：
  A) ui/presets.json 的 segments/keyframes 分布（R01-A2 的"22/24 只有 keyframes"）
  B) a10_mode ↔ 设备档案映射（R01-A6）+ channel.connect 的候选重试兜底
  C) 关键 settings 键的写点/读点闭环（R01-A8/A9/A10/A11、R02-D5/D7）
  D) QuickMoves.is_stop 是否存在（R01-A1）
  E) 运行配置 data/integrated_settings.json 的实际键（R01-A9）
  F) 行尾体检：哪些源码里混进了孤立 CR（会让 python/Select-String 的行号比
     git/ripgrep/read 口径多出若干行 —— 复核行号时必须先排除这个坑）

只读：不启动 GUI / 服务器；不写任何文件（含 data/integrated_settings.json）。
注意：本探针**按 LF 分行**（与 git diff / ripgrep / 编辑器一致），
      不使用 str.splitlines()（它会把孤立 CR 也当换行，行号会漂）。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "vendor"))


def split_lf(raw: bytes) -> list[str]:
    return raw.decode("utf-8").split("\n")     # ← 只认 LF，和 git/ripgrep 同口径


FILES = {
    "host_server.py": ROOT / "host_server.py",
    "app.js": ROOT / "ui" / "app.js",
    "index.html": ROOT / "ui" / "index.html",
    "channel.py": ROOT / "vendor" / "device" / "channel.py",
    "preset_player.py": ROOT / "vendor" / "device" / "preset_player.py",
    "quick_moves.py": ROOT / "vendor" / "device" / "quick_moves.py",
    "sync_engine.py": ROOT / "vendor" / "device" / "sync_engine.py",
    "protocols.py": ROOT / "vendor" / "device" / "protocols.py",
    "server_app.py": ROOT / "vendor" / "subtitle" / "server_app.py",
}
TEXT = {k: split_lf(v.read_bytes()) for k, v in FILES.items()}

OUT: list[str] = []


def p(line: str = "") -> None:
    OUT.append(line)


def hits(fname: str, needle: str, regex: bool = False) -> list[tuple[int, str]]:
    out = []
    for i, ln in enumerate(TEXT[fname], 1):
        if (re.search(needle, ln) if regex else (needle in ln)):
            out.append((i, ln.strip()))
    return out


def show(fname: str, hs: list[tuple[int, str]], indent: str = "    ") -> None:
    for i, ln in hs:
        p(f"{indent}{fname}:{i}: {ln[:120]}")


# ---------------------------------------------------------------- F) 行尾体检
p("=" * 78)
p("F) 行尾体检：孤立 CR（能让 python 行号相对 git/ripgrep 漂移）")
p("=" * 78)
CRLF = b"\r\n"
for name, path in FILES.items():
    raw = path.read_bytes()
    lone = raw.count(b"\r") - raw.count(CRLF)
    nlf = raw.count(b"\n")
    p(f"{name:18s} LF={nlf:5d} CRLF={raw.count(CRLF):5d} 孤立CR={lone}")
p("（app.js 有 2 个孤立 CR：LF 行 134 与 1616 的**行尾多一个 CR**。"
  "按 LF 分行时它们不额外占行；用 python splitlines()/PowerShell Get-Content 会把"
  "后续所有行号 +1/+2 —— 复核 R01/R02 行号时以 LF 口径为准。）")
for name, path in FILES.items():
    raw = path.read_bytes()
    pos, i = [], 0
    while True:
        i = raw.find(b"\r", i)
        if i < 0:
            break
        if raw[i:i + 2] != b"\r\n":
            pos.append(raw[:i].count(b"\n") + 1)
        i += 1
    if pos:
        p(f"  {name}: 孤立 CR 所在 LF 行号 = {pos}")

# ---------------------------------------------------------------- A) 预设
p("")
p("=" * 78)
p("A) ui/presets.json：segments / keyframes 分布（R01-A2）")
p("=" * 78)
presets = json.loads((ROOT / "ui" / "presets.json").read_text(encoding="utf-8"))
total = len(presets)
empty_seg = [pr for pr in presets if not (pr.get("segments") or [])]
nonempty = [pr for pr in presets if (pr.get("segments") or [])]
only_kf = [pr for pr in empty_seg if (pr.get("keyframes") or [])]
p(f"预设总数 = {total}；segments 为空 = {len(empty_seg)}；segments 非空 = {len(nonempty)} -> "
  f"{[pr['id'] for pr in nonempty]}")
p(f"空 segments 但有 keyframes = {len(only_kf)}；两者皆空 = {len(empty_seg) - len(only_kf)}")
p(f"空 segments 的 id = {[pr['id'] for pr in empty_seg]}")
p(f"[断言] 22/24 只有 keyframes: "
  f"{'PASS' if (total, len(empty_seg), len(only_kf)) == (24, 22, 22) else 'FAIL'}")
p("旁证 preset_player.py:_play_one_loop（segments 为空 ⇒ 循环体一次都不 await）：")
show("preset_player.py", hits("preset_player.py",
     r'for sg in \(pr\.get\("segments"\)|if dist > 0:|await asyncio\.sleep\(TICK_MS', regex=True))
show("preset_player.py", hits("preset_player.py", r'while self\.playing and gen == self\._gen'))
show("preset_player.py", hits("preset_player.py", r'await self\._play_one_loop'))

# ---------------------------------------------------------------- B) 档案映射
p("")
p("=" * 78)
p("B) a10_mode ↔ 设备档案（R01-A6）")
p("=" * 78)
from device.protocols import TOYS  # noqa: E402

auth = {t.a10_mode: t.id for t in TOYS}
p(f"权威映射（protocols.py:41-42）-> {auth}   # 0=ServeU, 1=VorzePiston")
show("protocols.py", hits("protocols.py", "a10_mode: int"))
show("protocols.py", hits("protocols.py", r'^SERVEU = ToyDevice|^VORZE = ToyDevice', regex=True))
p("host_server.py 里实际用的表达式：")
show("host_server.py", hits("host_server.py", '("vorze", "serveu")'))
for mode in (0, 1):
    got = ("vorze", "serveu")[mode]
    p(f"  a10_mode={mode} -> 代码选 [{got}]，权威应为 [{auth[mode]}]  "
      f"{'一致' if got == auth[mode] else '**倒置**'}")
p(f"[断言] 映射与权威表相反: "
  f"{'PASS' if ('vorze', 'serveu')[0] == auth[1] and ('vorze', 'serveu')[1] == auth[0] else 'FAIL'}")
p("影响链旁证（R01 未提及的兜底：connect 会按候选档案重试，所以最终往往还能连上）：")
show("channel.py", hits("channel.py", "self._forced_toy or"))
show("channel.py", hits("channel.py", "cands = [t for t in"))
show("channel.py", hits("channel.py", "for t in cands:"))
show("channel.py", hits("channel.py", "cmd_mode(self.mode_override"))
show("host_server.py", hits("host_server.py", "mode_override = int"))

# ---------------------------------------------------------------- C) 键闭环
p("")
p("=" * 78)
p("C) settings 键写点 / 读点闭环（写侧分流：queueVl/saveVl/bindLink→video_link，saveDev→device）")
p("=" * 78)


def vl_write(key: str) -> list[tuple[int, str]]:
    pat = (r"queueVl\(\{[^}]*\b" + key + r"\b|saveVl\(\{[^}]*\b" + key + r"\b|"
           r"bindLink\([^)]*[\"']" + key + r"[\"']")
    return hits("app.js", pat, regex=True)


def dev_write(key: str) -> list[tuple[int, str]]:
    return hits("app.js", r"saveDev\(\{[^}]*\b" + key + r"\b", regex=True)


def host_read(fname: str, var: str, key: str) -> list[tuple[int, str]]:
    return hits(fname, var + r'\.get\("' + key + r'"', regex=True)


rows = [
    ("video_link.reversed", vl_write("reversed"), host_read("host_server.py", "vl", "reversed"), 0),
    ("device.reversed", dev_write("reversed"), host_read("host_server.py", "dev", "reversed"), 1),
    ("device.script_sync", dev_write("script_sync"), host_read("host_server.py", "dev", "script_sync"), 1),
    ("device.preset_speed", dev_write("preset_speed"), host_read("host_server.py", "dev", "preset_speed"), 0),
    ("device.skip_idle", dev_write("skip_idle"), host_read("host_server.py", "dev", "skip_idle"), 1),
    ("device.idle_threshold", dev_write("idle_threshold"), host_read("host_server.py", "dev", "idle_threshold"), 1),
    ("video_link.range_min", vl_write("range_min"), host_read("host_server.py", "vl", "range_min"), 1),
    ("video_link.max_speed", vl_write("max_speed"), host_read("host_server.py", "vl", "max_speed"), 1),
    ("video_link.idle_min", vl_write("idle_min"), host_read("host_server.py", "vl", "idle_min"), 1),
    ("video_link.idle_speed", vl_write("idle_speed"), host_read("host_server.py", "vl", "idle_speed"), 1),
    ("video_link.idle_link", vl_write("idle_link"), host_read("host_server.py", "vl", "idle_link"), 1),
    ("video_link.burst_min", vl_write("burst_min"), host_read("host_server.py", "vl", "burst_min"), 1),
    ("video_link.burst_speed", vl_write("burst_speed"), host_read("host_server.py", "vl", "burst_speed"), 1),
    ("video_link.burst_speed_link", vl_write("burst_speed_link"), host_read("host_server.py", "vl", "burst_speed_link"), 1),
]
ok_closure = True
for name, w, r, expect_w in rows:
    flag = "" if len(w) == expect_w else f"  <== UI 写点应为 {expect_w}"
    p(f"{name:26s} UI写点={len(w)}  host读点={len(r)}{flag}")
    show("app.js", w)
    show("host_server.py", r)
    if len(w) != expect_w:
        ok_closure = False
p(f"[断言] A9（video_link.reversed 零 UI 写点、host 单点读）: "
  f"{'PASS' if not vl_write('reversed') and len(host_read('host_server.py', 'vl', 'reversed')) == 1 else 'FAIL'}")
p(f"[断言] A8/A10（device.script_sync 在 host 零读点）: "
  f"{'PASS' if not host_read('host_server.py', 'dev', 'script_sync') else 'FAIL'}")
p(f"[断言] A10（device.orgasm 在 host 零读点）: "
  f"{'PASS' if not host_read('host_server.py', 'dev', 'orgasm') else 'FAIL'}")
p(f"[断言] A11（device.preset_speed 在 UI 零写点）: "
  f"{'PASS' if not dev_write('preset_speed') else 'FAIL'}")
p(f"[断言] 所有键的 UI 写点数与预期一致: {'PASS' if ok_closure else 'FAIL'}")

# ---------------------------------------------------------------- D) is_stop
p("")
p("=" * 78)
p("D) QuickMoves.is_stop 是否存在（R01-A1）")
p("=" * 78)
from device.quick_moves import QuickMoves  # noqa: E402

p(f"hasattr(QuickMoves, 'is_stop') = {hasattr(QuickMoves, 'is_stop')}")
p("QuickMoves 公开的停止接口：")
show("quick_moves.py", hits("quick_moves.py", r"def (set_stop|stop_orgasm|stop_slow|state)", regex=True))
p("状态字段（QuickMoves.__init__ 里没有 is_stop）：")
show("quick_moves.py", hits("quick_moves.py", r"self\.(is_orgasm|is_slow) =", regex=True))
p("host_server.py 里 is_stop 的出现位置：")
show("host_server.py", hits("host_server.py", "is_stop"))
p(f"[断言] is_stop 属性不存在: {'PASS' if not hasattr(QuickMoves, 'is_stop') else 'FAIL'}")

# ---------------------------------------------------------------- E) 运行配置
p("")
p("=" * 78)
p("E) data/integrated_settings.json 实际键（只读）")
p("=" * 78)
sf = ROOT / "data" / "integrated_settings.json"
if sf.is_file():
    s = json.loads(sf.read_text(encoding="utf-8"))
    p(f"video_link = {json.dumps(s.get('video_link'), ensure_ascii=False)}")
    p(f"video_link 含 reversed 键 = {'reversed' in (s.get('video_link') or {})}")
    dev = s.get("device") or {}
    p(f"device.reversed = {dev.get('reversed', '<缺席>')}；device.script_sync = {dev.get('script_sync', '<缺席>')}；"
      f"device.preset_speed = {dev.get('preset_speed', '<缺席>')}")
    p(f"device.orgasm = {json.dumps(dev.get('orgasm'), ensure_ascii=False)}")
    p(f"device.slow = {json.dumps(dev.get('slow'), ensure_ascii=False)}")

print("\n".join(OUT))
print("\n[probe done]")
