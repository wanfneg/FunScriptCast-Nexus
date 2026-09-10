# -*- coding: utf-8 -*-
"""把 Lucide 图标内置成 SVG sprite（Symbol），供 ui/index.html 直接 <use>。

为什么内置而不是引 CDN：
  1. 这是桌面应用，WebView2 断网时 CDN 图标会整片消失；
  2. 外部 sprite 的 <use href="x.svg#id"> 会有一帧图标未加载的闪烁；
  3. 只需二十来个图标，内联后体积可忽略。

生成结果是**逐字取自 Lucide 官方 svg**（ISC 许可），不是手抄的近似路径——
手抄路径在细节上永远对不齐，视觉上就是"差点意思"。

用法：
    .venv\\Scripts\\python.exe tools\\gen_lucide_sprite.py            # 写入 ui/index.html
    .venv\\Scripts\\python.exe tools\\gen_lucide_sprite.py --print     # 只打印，不改文件

index.html 里用下面这对标记包住生成区域，脚本反复运行是幂等的：
    <!-- LUCIDE:START -->
    <!-- LUCIDE:END -->
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys
import urllib.request

APP = pathlib.Path(__file__).resolve().parent.parent
UI = APP / "ui" / "index.html"
CACHE = pathlib.Path(__file__).resolve().parent / ".lucide-cache"
CDN = "https://unpkg.com/lucide-static@1.43.0/icons/{}.svg"
UA = {"User-Agent": "Mozilla/5.0 (funscriptcast-nexus icon generator)"}

START = "<!-- LUCIDE:START -->"
END = "<!-- LUCIDE:END -->"

# 界面实际用到的图标。新增图标时只在这里加名字，然后重跑脚本。
ICONS = [
    # 导航
    "layout-dashboard", "radio-tower", "captions", "smartphone", "book-a", "settings",
    # 状态 / 指标
    "activity", "cpu", "gauge", "zap", "circle-dot", "signal", "waves", "thermometer",
    # 操作
    "play", "square", "refresh-cw", "refresh-ccw", "plus", "trash-2", "copy", "check",
    "x", "minus", "save", "upload", "download", "scan-line", "eye", "search",
    "square-play", "square", "power",
    # 文件 / 目录
    "folder", "folder-open", "file-text", "file-json", "hard-drive", "database",
    "package", "layers", "box", "table-2", "clipboard-list", "pencil-line",
    # 网络 / 设备
    "server", "network", "wifi", "link", "unplug", "plug", "monitor", "cast",
    "radio", "route", "git-branch", "waypoints", "arrow-right-left",
    # 媒体
    "film", "disc-3", "audio-lines", "mic-vocal", "subtitles", "headphones",
    # 反馈 / 提示
    "triangle-alert", "circle-check", "circle-alert", "info", "loader-circle",
    "circle-slash", "shield-check", "key-round", "clock", "flag",
    # 排版 / 主题
    "sun", "moon", "palette", "type", "sparkles", "wand-sparkles", "compass",
    "orbit", "crosshair", "corner-down-right", "arrow-up-right", "arrow-right",
    "chevron-right", "chevron-down", "terminal", "languages", "hash", "tag",
    "sliders-horizontal", "filter", "list", "grid-2x2", "maximize-2", "minimize-2",
]


def _download(name: str) -> tuple:
    """下载单个图标到本地缓存。返回 (名字, 错误信息或 None)。"""
    url = CDN.format(name)
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8")
    except Exception as e:
        return name, f"{type(e).__name__}: {e}"
    CACHE.mkdir(parents=True, exist_ok=True)
    (CACHE / f"{name}.svg").write_text(body, encoding="utf-8")
    return name, None


def prefetch(names: list, workers: int = 12) -> None:
    """并发把缺的图标抓到本地缓存。

    必须并发：走代理时单个图标要 5~20s，九十个串行得十几分钟，
    而这里纯粹是等网络，并发到十几个连接能压到一分钟以内。
    """
    from concurrent.futures import ThreadPoolExecutor

    todo = [n for n in dict.fromkeys(names) if not (CACHE / f"{n}.svg").exists()]
    if not todo:
        return
    print(f"下载 {len(todo)} 个图标（{workers} 并发）…", flush=True)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for name, err in ex.map(_download, todo):
            if err:
                print(f"  下载失败 {name}: {err}", flush=True)


def fetch(name: str) -> str:
    f = CACHE / f"{name}.svg"
    if f.exists():
        return f.read_text(encoding="utf-8")
    _, err = _download(name)
    if err:
        raise RuntimeError(err)
    return f.read_text(encoding="utf-8")


def inner(svg: str) -> str:
    """取出 <svg> 内部的内容（各元素原样保留）。"""
    body = re.sub(r"<!--.*?-->", "", svg, flags=re.S)
    m = re.search(r"<svg[^>]*>(.*)</svg>", body, re.S)
    if not m:
        raise ValueError("不是合法的 svg")
    got = m.group(1).strip()
    # 统一成单行，省掉生成文件里的无谓空白
    got = re.sub(r"\s*\n\s*", "", got)
    return got


def build(names: list) -> tuple:
    ok, bad, out = [], [], []
    for n in dict.fromkeys(names):          # 去重且保序
        try:
            body = inner(fetch(n))
        except Exception as e:
            bad.append(f"{n} ({type(e).__name__})")
            continue
        ok.append(n)
        out.append(
            f'  <symbol id="i-{n}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">{body}</symbol>'
        )
    head = (f'<!-- Lucide v1.43.0 · ISC License · 由 tools/gen_lucide_sprite.py 生成，'
            f'请勿手改；要增删图标改脚本里的 ICONS 再重跑。共 {len(ok)} 个。 -->')
    return "\n".join([head] + out), ok, bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", action="store_true", dest="show", help="只打印，不改文件")
    args = ap.parse_args()

    prefetch(ICONS)
    sprite, ok, bad = build(ICONS)
    print(f"成功 {len(ok)} 个，失败 {len(bad)} 个")
    if bad:
        print("  失败: " + ", ".join(bad))
    if args.show:
        print(sprite)
        return 0

    html = UI.read_text(encoding="utf-8")
    if START not in html or END not in html:
        print(f"index.html 里找不到 {START} / {END} 标记，无法写入")
        return 1
    new = re.sub(re.escape(START) + r".*?" + re.escape(END),
                 START + "\n" + sprite + "\n" + END, html, flags=re.S)
    if new == html:
        print("内容无变化")
        return 0
    UI.write_text(new, encoding="utf-8")
    print(f"已写入 {UI}（{len(html)} → {len(new)} 字符）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
