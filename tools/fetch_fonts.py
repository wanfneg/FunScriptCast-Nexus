# -*- coding: utf-8 -*-
"""下载并内置界面字体（可变字重 woff2，latin 子集）。

为什么要内置而不是写 font-family 让系统去挑：这是桌面应用，用户机器上未必装了
Inter / Space Grotesk，一旦回退到 Segoe UI，整套排版的性格就没了——而字型恰恰是
"看起来是否够专业"的第一决定因素。三个文件加起来才 108 KB。

中文不在这些子集里，由系统字体（微软雅黑）承担，这是常规做法。
字体许可：Inter / Space Grotesk / JetBrains Mono 均为 SIL OFL 1.1，可随程序分发。

用法： .venv\\Scripts\\python.exe tools\\fetch_fonts.py
"""
from __future__ import annotations

import pathlib
import urllib.request

APP = pathlib.Path(__file__).resolve().parent.parent
FONT_DIR = APP / "ui" / "fonts"
CSS = APP / "ui" / "fonts.css"
UA = {"User-Agent": "Mozilla/5.0 (funscriptcast-nexus font fetcher)"}

BASE = "https://cdn.jsdelivr.net/npm/@fontsource-variable/{pkg}/files/{file}"

# (CSS 里的家族名, fontsource 包名（不含 @fontsource-variable/ 前缀）, 文件名, 用途)
FONTS = [
    ("Inter Variable", "inter",
     "inter-latin-wght-normal.woff2", "界面正文"),
    ("Space Grotesk Variable", "space-grotesk",
     "space-grotesk-latin-wght-normal.woff2", "标题与数字（展示体）"),
    ("JetBrains Mono Variable", "jetbrains-mono",
     "jetbrains-mono-latin-wght-normal.woff2", "技术值 / 路径 / 日志"),
]


def _download(url: str, dst: pathlib.Path, tries: int = 4) -> None:
    """带重试下载。走代理时同一个 URL 会时通时不通（HTTP 400/超时），
    不重试的话一次抖动就得手动重跑。"""
    import time
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=40) as r:
                data = r.read()
            if len(data) < 4096:
                raise RuntimeError(f"响应过小（{len(data)} 字节），不像字体文件")
            dst.write_bytes(data)
            return
        except Exception as e:
            last = e
            if i < tries - 1:
                time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"{type(last).__name__}: {last}")


def main() -> int:
    FONT_DIR.mkdir(parents=True, exist_ok=True)
    faces = []
    for family, pkg, fname, use in FONTS:
        dst = FONT_DIR / fname
        if not dst.exists():
            url = BASE.format(pkg=pkg, file=fname)
            print(f"下载 {fname} …", flush=True)
            try:
                _download(url, dst)
            except Exception as e:
                print(f"  失败 {fname}: {e}")
                return 1
        print(f"  {fname:44s} {dst.stat().st_size / 1024:6.0f} KB   {use}")
        faces.append(
            "@font-face{\n"
            f'  font-family:"{family}";\n'
            "  font-style:normal;\n"
            "  font-weight:100 900;\n"          # 可变字重轴
            "  font-display:swap;\n"
            f'  src:url("fonts/{fname}") format("woff2");\n'
            "}"
        )

    CSS.write_text(
        "/* 由 tools/fetch_fonts.py 生成，请勿手改。\n"
        "   Inter / Space Grotesk / JetBrains Mono · SIL OFL 1.1 · latin 子集可变字重。 */\n"
        + "\n".join(faces) + "\n",
        encoding="utf-8")
    print(f"已写出 {CSS}（{CSS.stat().st_size} 字节）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
