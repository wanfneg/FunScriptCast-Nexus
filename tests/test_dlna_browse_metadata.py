# -*- coding: utf-8 -*-
"""验证 vr_dlna 容器 BrowseMetadata 修复：应返回容器自身及其真实上级。

旧行为：对容器请求 BrowseMetadata 会返回**子项列表**，子项的 parentID 是容器自己，
客户端据此"返回上一级"会直接跳到根目录。
新行为：返回该容器自身，parentID 为其真实上级。
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vendor.dlna.vr_dlna import DlnaApp, MediaRoot  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="dlna_meta_"))
folder = tmp / "H-Europe and America VR" / "SLR_Test_alpha-BFR-TeAm"
folder.mkdir(parents=True)
(folder / "movie.mp4").write_bytes(b"\x00" * 16)

app = DlnaApp([MediaRoot(label="Videos", path=tmp)], 8899)

fails = []


def expect(name, got, want):
    ok = got == want
    print(("  OK   " if ok else "  FAIL ") + name)
    print("        got  = " + repr(got))
    if not ok:
        print("        want = " + repr(want))
        fails.append(name)


print("=== 1) 顶层目录 F:H-Europe and America VR ===")
xml, n, t = app.browse("F:H-Europe and America VR", "BrowseMetadata")
expect("NumberReturned", n, 1)
expect("TotalMatches", t, 1)
expect("是自己", 'id="F:H-Europe and America VR"' in xml, True)
expect("上级=0", 'parentID="0"' in xml, True)
expect("不是子项列表", "SLR_Test" not in xml, True)

print("=== 2) 深层目录（用户报的那一级）===")
deep = "F:H-Europe and America VR/SLR_Test_alpha-BFR-TeAm"
xml2, n2, t2 = app.browse(deep, "BrowseMetadata")
expect("NumberReturned", n2, 1)
expect("是自己", ('id="%s"' % deep) in xml2, True)
expect("上级=顶层目录", 'parentID="F:H-Europe and America VR"' in xml2, True)

print("=== 3) 文件 V:... （非本次改动，确认未被破坏）===")
fkey = "H-Europe and America VR/SLR_Test_alpha-BFR-TeAm/movie.mp4"
xml3, n3, t3 = app.browse("V:" + fkey, "BrowseMetadata")
expect("NumberReturned", n3, 1)
expect("是自己", ('id="V:%s"' % fkey) in xml3, True)
expect("上级=所在目录", 'parentID="F:H-Europe and America VR/SLR_Test_alpha-BFR-TeAm"' in xml3, True)

print("=== 5) 列目录：文件项 parentID 必须是所在目录（不能是自己）===")
lst, ln, lt = app.browse("F:H-Europe and America VR/SLR_Test_alpha-BFR-TeAm", "BrowseDirectChildren")
expect("列出 1 个文件", ln, 1)
expect("文件项 parentID=所在目录",
       'id="V:%s" parentID="F:H-Europe and America VR/SLR_Test_alpha-BFR-TeAm"' % fkey in lst, True)
expect("文件项 parentID 不等于自身", ('id="V:%s" parentID="V:%s"' % (fkey, fkey)) in lst, False)

print("=== 5b) 列父目录：子目录项 parentID=本层 ===")
par, pn, pt = app.browse("F:H-Europe and America VR", "BrowseDirectChildren")
expect("列出 1 个子目录", pn, 1)
expect("子目录项 parentID=所在层",
       'id="F:H-Europe and America VR/SLR_Test_alpha-BFR-TeAm" parentID="F:H-Europe and America VR"' in par, True)

print("=== 6) 列根目录：顶层条目 parentID=0 ===")
root_lst, rn, rt = app.browse("0", "BrowseDirectChildren")
expect("根列出 1 项", rn, 1)
expect("顶层目录 parentID=0",
       'id="F:H-Europe and America VR" parentID="0"' in root_lst, True)

print("=== 4) 根 ====")
xml4, n4, t4 = app.browse("0", "BrowseMetadata")
expect("列出根内容", n4 >= 1, True)

print()
if fails:
    print("RESULT: FAIL -> " + ", ".join(fails))
    sys.exit(1)
print("RESULT: ALL PASS")
