# -*- coding: utf-8 -*-
"""生成 FunScriptCast-Nexus 应用图标（多尺寸 .ico + 预览 PNG）。

设计与 UI 的 brand-mark 一致：深色圆角底 + 青紫渐变圆环 + 中心亮点。
纯 PIL 绘制，无外部素材依赖。
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT_DIR = Path(__file__).resolve().parent
S = 512                     # 主尺寸（下采样出小图标）
BG = (11, 14, 22, 255)      # --bg-base
CYAN = (76, 201, 240)       # --accent
VIOLET = (123, 92, 255)     # --accent-2
INK = (232, 236, 244)


def lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def build_master() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 圆角方底
    r = int(S * 0.22)
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=r, fill=BG)

    # 渐变圆环（青 → 紫，沿 135° 方向）
    ring = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    rd = ImageDraw.Draw(ring)
    cx = cy = S / 2
    outer, inner = S * 0.36, S * 0.235
    steps = 720
    for i in range(steps):
        a0 = (i / steps) * math.pi * 2
        a1 = ((i + 1.6) / steps) * math.pi * 2
        # 用角度映射颜色：起点 135°
        t = ((a0 / (math.pi * 2)) + 0.375) % 1.0
        col = lerp(CYAN, VIOLET, t)
        pts = [
            (cx + math.cos(a0) * inner, cy + math.sin(a0) * inner),
            (cx + math.cos(a0) * outer, cy + math.sin(a0) * outer),
            (cx + math.cos(a1) * outer, cy + math.sin(a1) * outer),
            (cx + math.cos(a1) * inner, cy + math.sin(a1) * inner),
        ]
        rd.polygon(pts, fill=col + (255,))
    ring = ring.filter(ImageFilter.GaussianBlur(1.2))
    img.alpha_composite(ring)

    # 中心亮点（发光球）
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gr = S * 0.115
    gd.ellipse([cx - gr, cy - gr, cx + gr, cy + gr], fill=CYAN + (210,))
    glow = glow.filter(ImageFilter.GaussianBlur(S * 0.045))
    img.alpha_composite(glow)

    core = ImageDraw.Draw(img)
    cr = S * 0.072
    core.ellipse([cx - cr, cy - cr, cx + cr, cy + cr], fill=(240, 250, 255, 255))

    # 外环细描边（提升小尺寸下的可辨识度）
    stroke = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    sd = ImageDraw.Draw(stroke)
    sd.ellipse([cx - outer, cy - outer, cx + outer, cy + outer],
               outline=(255, 255, 255, 38), width=max(2, int(S * 0.008)))
    img.alpha_composite(stroke)
    return img


def main() -> None:
    master = build_master()
    master.save(OUT_DIR / "icon.png")
    sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
    master.save(OUT_DIR / "icon.ico", format="ICO", sizes=sizes)
    for w in (64, 32, 16):
        master.resize((w, w), Image.LANCZOS).save(OUT_DIR / f"icon_{w}.png")
    print(f"已生成：{OUT_DIR / 'icon.ico'}（{', '.join(f'{w}x{h}' for w, h in sizes)}）")
    print(f"预览：{OUT_DIR / 'icon.png'}")


if __name__ == "__main__":
    main()
