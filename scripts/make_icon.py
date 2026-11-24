# -*- coding: utf-8 -*-
"""make_icon.py — 生成 CBStudio 的 exe / 窗口图标（assets/cbstudio.ico）

设计（与界面配色一致，取自 web/css/app.css）：

* 底色：界面深色面板（``--bg`` #0a0e13 → ``--panel`` #121a23）的圆角方块；
* 主色：强调青 ``--accent`` #2fd6c0；
* 主体：``CB`` 字标（Segoe UI Bold），细节版走青→深青渐变。

小尺寸（≤48）用简化版：字标更大、纯色填充，避免 16px 糊成一团。

用法::

    python scripts/make_icon.py            # 重新生成 assets/cbstudio.ico
"""

import os
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT_DIR = os.path.join(ROOT, "assets")
OUT_ICO = os.path.join(OUT_DIR, "cbstudio.ico")

FONT_PATH = r"C:\Windows\Fonts\segoeuib.ttf"

BG_TOP = (22, 33, 46)         # 略亮于 --panel，给一点纵深
BG_BOTTOM = (8, 12, 18)       # 深于 --bg
RIM = (47, 214, 192)          # --accent
ACCENT_HI = (137, 243, 230)
ACCENT_LO = (35, 168, 156)
TEXT = (232, 244, 250)

DETAIL_SIZES = (256, 128, 96, 64)      # 细节版
SIMPLE_SIZES = (48, 40, 32, 24, 20, 16)  # 简化版
MASTER = 1024


def _vgradient(width, height, top, bottom):
    """竖直渐变。"""
    img = Image.new("RGB", (1, height))
    for y in range(height):
        k = y / max(height - 1, 1)
        img.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * k) for i in range(3)))
    return img.resize((width, height), Image.BILINEAR)


def _rounded_mask(size, radius):
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    return mask


def _draw_base(size, detail):
    """圆角深色底 + 强调色描边，返回 RGBA 图层。"""
    radius = int(size * (0.22 if detail else 0.24))
    base = _vgradient(size, size, BG_TOP, BG_BOTTOM).convert("RGBA")
    base.putalpha(_rounded_mask(size, radius))

    ring = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(ring)
    stroke = max(2, int(size * (0.022 if detail else 0.03)))
    draw.rounded_rectangle(
        (stroke // 2, stroke // 2, size - 1 - stroke // 2, size - 1 - stroke // 2),
        radius=radius, outline=RIM + (110,), width=stroke)
    return Image.alpha_composite(base, ring)


def _draw_letters(canvas_size, detail):
    """字标层：CB，细节版走青→深青渐变，简化版用纯色保证小尺寸清晰。"""
    font_size = int(canvas_size * (0.50 if detail else 0.58))
    font = ImageFont.truetype(FONT_PATH, font_size)
    text = "CB"

    probe = ImageDraw.Draw(Image.new("L", (canvas_size, canvas_size), 0))
    left, top, right, bottom = probe.textbbox((0, 0), text, font=font)
    width, height = right - left, bottom - top

    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).text((-left, -top), text, font=font, fill=255)

    if detail:
        fill = _vgradient(width, height, ACCENT_HI, ACCENT_LO).convert("RGBA")
    else:
        fill = Image.new("RGBA", (width, height), TEXT + (255,))
    fill.putalpha(mask)

    layer = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
    x = (canvas_size - width) // 2
    # 字标按视觉重心略上移一点点（字母本身的下沉空档），并垂直居中
    y = int((canvas_size - height) / 2) - int(canvas_size * 0.012)
    layer.paste(fill, (x, y))
    return layer


def build(size, detail):
    canvas = _draw_base(size, detail)
    canvas = Image.alpha_composite(canvas, _draw_letters(size, detail))
    return canvas


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    detail_master = build(MASTER, True)
    simple_master = build(MASTER, False)

    frames = []
    for size in DETAIL_SIZES:
        frames.append((size, detail_master.resize((size, size), Image.LANCZOS)))
    for size in SIMPLE_SIZES:
        frames.append((size, simple_master.resize((size, size), Image.LANCZOS)))

    frames.sort(key=lambda item: -item[0])
    largest = frames[0][1]
    largest.save(OUT_ICO, format="ICO",
                 sizes=[(size, size) for size, _ in frames],
                 append_images=[img for _, img in frames[1:]])

    preview = os.path.join(OUT_DIR, "icon_preview.png")
    sheet = Image.new("RGBA", (sum(size for size, _ in frames) + 20 * (len(frames) + 1), 290),
                      (10, 14, 19, 255))
    x = 20
    for size, img in frames:
        sheet.paste(img, (x, 20 + (256 - size) // 2), img)
        x += size + 20
    sheet.save(preview)

    print("ok ->", OUT_ICO)
    print("frames:", ", ".join(str(size) for size, _ in frames))
    print("preview ->", preview)
    return 0


if __name__ == "__main__":
    sys.exit(main())
