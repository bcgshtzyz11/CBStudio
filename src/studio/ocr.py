"""ocr.py — 从仪表截图里读出数字（电压等）。

不依赖第三方库：自带 PNG 解码 + 字形模板匹配。
字形模板来自 `Res/font/NimbusSans-Bold.otf`（底部状态栏数值字体 `Style_HelvBold`，30px），
由 `_dev/build_glyph_templates.py` 生成并写入 `data/glyph_templates.json`。

识别流程：
    截图 → 裁"读数区域" → 按局部背景二值化（明暗主题都吃）→ 按列切字形
         → 归一化到 16×24 → 与模板逐一比对 → 置信度达标者拼成字符串 → 解析成数值

对主题（白天/黑夜）、背景图都不敏感；对"半渲染帧"会因置信度低而拒绝识别。
"""

import json
import os
import struct
import zlib

from . import paths

DATA_DIR = paths.DATA_DIR
TEMPLATE_PATH = os.path.join(DATA_DIR, "glyph_templates.json")
BOX_W, BOX_H = 16, 24
MAX_DIST = 0.25          # 单字形置信度门槛（越小越像）
MAX_GLYPH_W = 12         # 超过这个宽度的块多半是图标/字母，不当数字

_templates = None


def read_png(path):
    """解 8 位 RGB/RGBA 的 PNG，返回 (width, height, bgra_bytes)。"""
    with open(path, "rb") as handle:
        blob = handle.read()
    if blob[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("不是 PNG")
    pos = 8
    width = height = depth = color = None
    idat = bytearray()
    while pos < len(blob):
        length = struct.unpack(">I", blob[pos:pos + 4])[0]
        kind = blob[pos + 4:pos + 8]
        body = blob[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, depth, color = struct.unpack(">IIBB", body[:10])
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
    if depth != 8 or color not in (2, 6):
        raise ValueError("只支持 8 位 RGB/RGBA")
    channels = 4 if color == 6 else 3
    stride = width * channels
    raw = zlib.decompress(bytes(idat))
    rows = []
    prev = bytearray(stride)
    offset = 0
    for _y in range(height):
        filter_type = raw[offset]
        offset += 1
        line = bytearray(raw[offset:offset + stride])
        offset += stride
        if filter_type == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif filter_type == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif filter_type == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif filter_type == 4:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        rows.append(line)
        prev = line
    bgra = bytearray(width * height * 4)
    for y, line in enumerate(rows):
        base = y * width * 4
        for x in range(width):
            src = x * channels
            dst = base + x * 4
            bgra[dst] = line[src + 2]
            bgra[dst + 1] = line[src + 1]
            bgra[dst + 2] = line[src]
            bgra[dst + 3] = line[src + 3] if channels == 4 else 255
    return width, height, bytes(bgra)


def _luma(bgra, width, x, y):
    pos = (y * width + x) * 4
    return (bgra[pos + 2] * 299 + bgra[pos + 1] * 587 + bgra[pos] * 114) // 1000


def _crop(bgra, width, height, x0, x1, y0, y1):
    rows = []
    for y in range(max(0, y0), min(height, y1)):
        rows.append([_luma(bgra, width, x, y) for x in range(max(0, x0), min(width, x1))])
    return rows


def _binarize(rows, threshold=18):
    flat = sorted(v for row in rows for v in row)
    if not flat:
        return []
    med = flat[len(flat) // 2]
    return [[1 if abs(v - med) > threshold else 0 for v in row] for row in rows]


def _segment(mask, min_ink=2):
    height = len(mask)
    width = len(mask[0]) if height else 0
    cols = [sum(mask[y][x] for y in range(height)) for x in range(width)]
    spans = []
    start = None
    for x, ink in enumerate(cols):
        if ink >= min_ink and start is None:
            start = x
        elif ink < min_ink and start is not None:
            if x - start >= 2:
                spans.append((start, x))
            start = None
    if start is not None and width - start >= 2:
        spans.append((start, width))
    return spans


def _normalize(mask, x0, x1):
    ys = [y for y in range(len(mask)) if any(mask[y][x0:x1])]
    if not ys:
        return None
    y0, y1 = ys[0], ys[-1] + 1
    xs = [x for x in range(x0, x1) if any(mask[y][x] for y in range(y0, y1))]
    if not xs:
        return None
    gx0, gx1 = xs[0], xs[-1] + 1
    gh, gw = y1 - y0, gx1 - gx0
    grid = []
    for by in range(BOX_H):
        sy0 = y0 + (by * gh) // BOX_H
        sy1 = max(sy0 + 1, y0 + ((by + 1) * gh) // BOX_H)
        row = []
        for bx in range(BOX_W):
            sx0 = gx0 + (bx * gw) // BOX_W
            sx1 = max(sx0 + 1, gx0 + ((bx + 1) * gw) // BOX_W)
            total = 0
            count = 0
            for y in range(sy0, min(sy1, y1)):
                for x in range(sx0, min(sx1, gx1)):
                    total += mask[y][x]
                    count += 1
            row.append(total / count if count else 0.0)
        grid.append(row)
    return grid


def _distance(a, b):
    total = 0.0
    for row_a, row_b in zip(a, b):
        for va, vb in zip(row_a, row_b):
            total += abs(va - vb)
    return total / (BOX_W * BOX_H)


def load_templates(path=None):
    """读取字形模板（首次调用后缓存）。"""
    global _templates
    if _templates is not None and path is None:
        return _templates
    with open(path or TEMPLATE_PATH, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    _templates = {char: grid for char, grid in data.get("glyphs", data).items()}
    return _templates


def read_number(png_path, region, max_dist=MAX_DIST):
    """读截图里的数字。

    ``region`` 是按整帧比例的区域（``x0/x1/y0/y1``，0~1）。
    返回 ``{'text': '24.0', 'value': 24.0, 'glyphs': [...], 'confidence': 0.09}``；
    认不出来时 ``value`` 为 ``None``，``text`` 可能是空串。
    """
    templates = load_templates()
    width, height, bgra = read_png(png_path)
    rows = _crop(bgra, width, height,
                 int(region["x0"] * width), int(region["x1"] * width),
                 int(region["y0"] * height), int(region["y1"] * height))
    mask = _binarize(rows)
    spans = _segment(mask)
    chars = []
    scores = []
    detail = []
    for x0, x1 in spans:
        grid = _normalize(mask, x0, x1)
        if not grid:
            continue
        best_char, best_dist = None, 9.9
        for char, tpl in templates.items():
            dist = _distance(grid, tpl)
            if dist < best_dist:
                best_char, best_dist = char, dist
        detail.append({"x": x0, "w": x1 - x0, "char": best_char, "dist": round(best_dist, 3)})
        if best_dist <= max_dist and (x1 - x0) <= MAX_GLYPH_W:
            chars.append(best_char)
            scores.append(best_dist)
    text = "".join(chars)
    # 只保留数字与小数点：旁边就算混进字母也不会让解析失败
    cleaned = "".join(c for c in text if c.isdigit() or c == ".")
    while ".." in cleaned:
        cleaned = cleaned.replace("..", ".")
    cleaned = cleaned.strip(".")
    value = None
    try:
        if cleaned:
            value = float(cleaned)
    except ValueError:
        value = None
    confidence = round(sum(scores) / len(scores), 3) if scores else None
    return {"text": text, "value": value, "glyphs": detail, "confidence": confidence}
