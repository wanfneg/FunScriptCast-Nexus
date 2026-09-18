# -*- coding: utf-8 -*-
"""CompareVer / VerPart 的 Python 镜像：验证 setup.iss 里那段 Pascal 的**判定符号**。

为什么要有这个：ISCC 只能证明 Pascal 语法与类型正确，证明不了"旧版本判定成升级"这种
语义。把同一套算法逐行抄成 Python 跑一遍用例，能把符号搞反、段落取错这类错误挡住。
（Pascal 原文见 installer/setup.iss 的 [Code] 段，两边逻辑必须一致。）

    .venv/Scripts/python.exe tests/verify_setup_version.py
"""


def ver_part(s: str, index: int) -> int:
    """取版本号第 index 段（从 1 起）。只吃数字与点；遇到 '-' 之类后缀就停。"""
    result = 0
    part = 1
    cur = ""
    for ch in s:
        if ch == ".":
            if part == index:
                return int(cur) if cur.isdigit() else 0
            part += 1
            cur = ""
        elif ch.isdigit():
            cur += ch
        else:
            break
    if part == index:
        return int(cur) if cur.isdigit() else 0
    return result


def compare_ver(a: str, b: str) -> int:
    """A<B → -1，A=B → 0，A>B → 1。"""
    for i in range(1, 5):
        x, y = ver_part(a, i), ver_part(b, i)
        if x < y:
            return -1
        if x > y:
            return 1
    return 0


CASES = [
    # (已装版本, 本安装包版本, 期望, 说明)
    ("", "1.0.19", None, "没装过 → 走全新安装分支，不比较"),
    ("1.0.18", "1.0.19", -1, "旧 → 新：升级（提示数据保留）"),
    ("1.0.18", "1.0.18", 0, "同版本：提示重装（可修复）"),
    ("1.0.18", "1.0.17", 1, "新 → 旧：降级，默认按钮为否、应拦下"),
    ("1.0.18", "1.0.18.0", 0, "补零等价：1.0.18 == 1.0.18.0"),
    ("1.0.18", "1.0.19.1", -1, "第四段参与比较"),
    ("1.9.0", "1.10.0", -1, "数字比较而非字符串（'9' > '10' 的字符串陷阱）"),
    ("2.0.0", "1.99.99", 1, "主版本号优先"),
    ("1.0.18 ", "1.0.19", -1, "尾部空格不影响"),
    ("1.0.18-beta", "1.0.18", 0, "非数字后缀被截断"),
]


def main() -> int:
    bad = 0
    for installed, pkg, want, why in CASES:
        if want is None:
            print(f"  SKIP （不比较）        {why}")
            continue
        got = compare_ver(installed, pkg)
        ok = got == want
        bad += 0 if ok else 1
        print(f"  {'OK  ' if ok else 'FAIL'} 已装{installed!r:>13} vs 本包{pkg!r:<11} "
              f"→ {got:+d}（期望 {want:+d}）  {why}")
    print()
    if bad:
        print(f"{bad} 项失败")
        return 1
    print(f"全部通过（{len(CASES) - 1} 项）—— 与 setup.iss 的 Pascal 逐行对应")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
