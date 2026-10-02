# -*- coding: utf-8 -*-
"""安装后一键验证（R118.2 起常驻工具）：对 D 盘安装版做文件层面 + 抽帧冒烟。

用法（必须用 D 盘自带 runtime 跑，才是在验证"安装环境自身"）：
    D:\FunScriptCast-Nexusuntime\python.exe toolserify_install.py
检查项：版本号 / libmpv 为 lgpl 版 / 许可文本随包 / 无 zhconv / 无旧播放器残留 / 抽帧冒烟。
"""
import json
import pathlib
import sys
import tempfile

D = pathlib.Path(r"D:\FunScriptCast-Nexus")
ok, fail = [], []


def check(name, cond, detail=""):
    (ok if cond else fail).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + ("  (" + detail + ")" if detail else ""))


# 1) 版本
v = json.loads((D / "version.json").read_text(encoding="utf-8"))
check("version.json", v.get("versionName") == "1.0.84", str(v.get("versionName")))

# 2) libmpv 为 lgpl 版（95.8MB，GPL 版是 121MB）
dll = D / "vendor" / "mpv" / "libmpv-2.dll"
size_mb = dll.stat().st_size / 1048576 if dll.is_file() else 0
check("libmpv-2.dll 为 lgpl 版（90-110MB）", 90 < size_mb < 110, f"{size_mb:.1f} MB")

# 3) libmpv 许可文本随包
check("licenses 随包（LGPL-2.1 + GPL-2.0 + Copyright）",
      all((D / "vendor" / "mpv" / f).is_file()
          for f in ("LICENSE.LGPL-2.1.txt", "LICENSE.GPL-2.0.txt", "mpv-Copyright.txt")))

# 4) runtime 无 zhconv（R118 清理）
sp = D / "runtime" / "Lib" / "site-packages"
check("runtime 无 zhconv", not (sp / "zhconv").exists())

# 5) 旧外挂播放器文件（升级 InstallDelete 应已清）
check("player 目录无 mpv_player.py 残留", not (D / "vendor" / "player" / "mpv_player.py").exists())

# 6) 抽帧冒烟（D 盘环境 + D 盘 dll + 真实视频）
sys.path.insert(0, str(D / "vendor" / "player"))
import library  # noqa: E402

td = pathlib.Path(tempfile.mkdtemp(prefix="verif-"))
lib = library.Library(td / "idx.json", td)
dur, thumb = lib._probe_local(pathlib.Path(r"E:\testvideo\K1cztm.mp4"))
check("抽帧冒烟（时长 + 缩略图）", dur > 1000 and bool(thumb), f"dur={dur}s thumb={thumb}")

print()
print("==== 汇总：%d 通过 / %d 失败 ====" % (len(ok), len(fail)))
if fail:
    print("失败项：", " / ".join(fail))
    sys.exit(1)
