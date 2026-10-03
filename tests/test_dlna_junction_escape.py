# -*- coding: utf-8 -*-
"""高危①回归验证（R125 全项目审查）：DLNA 单根模式 junction 逃逸必须被拒。

历史：R103 曾声称修复"单根分支同样要过 reparse 检查"，但单根分支的三个提前
return 使检查不可达（全项目审查实测复现 LEAK）。修复后单根与多根统一落检查。
用法：python tests/test_dlna_junction_escape.py（需 Windows，mklink /J）
"""
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "vendor" / "dlna"))

tmp = Path(tempfile.mkdtemp(prefix="dlna-sec-"))
root = tmp / "media"
(root / "sub").mkdir(parents=True)
secret = tmp / "secret.txt"
secret.write_text("LEAK", encoding="utf-8")
(root / "sub" / "ok.mp4").write_text("OK", encoding="utf-8")
# junction: media/link -> tmp（根外）
subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "link"), str(tmp)],
               capture_output=True, check=True)

import vr_dlna as V  # noqa: E402

lib = V.MediaLibrary(roots=[V.MediaRoot(label="Videos", path=root)])


def probe(key):
    p = lib.key_to_path(key)
    if p is None:
        return "BLOCKED"
    try:
        return "LEAK:" + p.read_text(encoding="utf-8")
    except OSError:
        return "LEAK:(unreadable)"


r1 = probe("link/secret.txt")
r2p = lib.key_to_path("sub/ok.mp4")
r2 = "OK-PATH" if r2p and r2p.name == "ok.mp4" else ("BLOCKED" if r2p is None else "WRONG:" + str(r2p))
r3 = probe("../secret.txt")
# 多根对照
lib2 = V.MediaLibrary(roots=[V.MediaRoot(label="Videos", path=root), V.MediaRoot(label="Other", path=tmp / "other")])
r4 = lib2.key_to_path("Videos/link/secret.txt")
r4s = "BLOCKED" if r4 is None else "LEAK"

print("单根 junction 逃逸:", r1, "(期望 BLOCKED)")
print("单根 正常文件    :", r2, "(期望 OK-PATH)")
print("单根 .. 穿越     :", r3, "(期望 BLOCKED)")
print("多根 junction    :", r4s, "(期望 BLOCKED)")
ok = r1 == "BLOCKED" and r2 == "OK-PATH" and r3 == "BLOCKED" and r4s == "BLOCKED"
print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
