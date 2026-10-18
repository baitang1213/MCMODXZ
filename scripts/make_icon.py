# -*- coding: utf-8 -*-
"""MCMODXZ 应用图标生成器（纯标准库）。

设计：青→蓝渐变圆角方块 + 白色下载箭头 + 双层波浪
      （波浪 = 鸣潮的"浪"，箭头 = 下载；配色与用界面主色一致 #3bd9c6 → #4c7dff）
实现：SDF（符号距离场）解析抗锯齿，主图 256px，盒式重采样输出
      16/24/32/48/64/128/256 七个尺寸，打包为 .ico；另输出 PNG 预览。

用法：
    python scripts/make_icon.py                # 生成项目根目录的 icon.ico
    python scripts/make_icon.py out.ico        # 指定输出
"""
from __future__ import annotations

import math
import struct
import sys
import zlib
from pathlib import Path

TOP = (59, 217, 198)     # 青绿
BOT = (76, 125, 255)     # 蓝
WHITE = (255, 255, 255)
MASTER = 256
SIZES = (16, 24, 32, 48, 64, 128, 256)


def mix(a, b, t):
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t)


def smoothstep(e0, e1, x):
    t = max(0.0, min(1.0, (x - e0) / (e1 - e0) if e1 != e0 else 0.0))
    return t * t * (3.0 - 2.0 * t)


def sd_round_rect(px, py, cx, cy, hw, hh, r):
    qx = abs(px - cx) - hw + r
    qy = abs(py - cy) - hh + r
    ox, oy = max(qx, 0.0), max(qy, 0.0)
    return math.hypot(ox, oy) - r + min(max(qx, qy), 0.0)


def sd_seg(px, py, x0, y0, x1, y1):
    vx, vy = x1 - x0, y1 - y0
    wx, wy = px - x0, py - y0
    l2 = vx * vx + vy * vy
    t = 0.0 if l2 == 0 else max(0.0, min(1.0, (wx * vx + wy * vy) / l2))
    return math.hypot(wx - t * vx, wy - t * vy)


def sd_poly(p, pts):
    """凸多边形 SDF（负值在内部）：各边有符号距离取最大。"""
    px, py = p
    cx = sum(q[0] for q in pts) / len(pts)
    cy = sum(q[1] for q in pts) / len(pts)
    d = -1e18
    for i in range(len(pts)):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % len(pts)]
        ex, ey = x1 - x0, y1 - y0
        ln = math.hypot(ex, ey)
        dist = abs(ey * (px - x0) - ex * (py - y0)) / ln
        s_c = ey * (cx - x0) - ex * (cy - y0)
        s_p = ey * (px - x0) - ex * (py - y0)
        d = max(d, -dist if s_c * s_p >= 0 else dist)
    return d


def render_master(n: int = MASTER, simple: bool = False) -> bytearray:
    """渲染 256 主图，输出预乘 alpha 的 BGRA（8bit）。

    simple=True 时输出小尺寸专用简化版：箭头放大、单条粗波浪（16px 可读性）。
    """
    buf = bytearray(n * n * 4)
    if simple:
        waves = ((0.780, 0.042, 2.1, 1.0, 0.052, 0.95),)
        zoom, pivot = 1.22, 0.415
    else:
        waves = (
            # (基线y, 振幅, 相位, 频率, 半线宽, 透明度)
            (0.742, 0.050, 1.9, 1.15, 0.028, 0.42),
            (0.800, 0.048, 4.4, 1.15, 0.036, 0.95),
        )
        zoom, pivot = 1.0, 0.0
    for y in range(n):
        v = (y + 0.5) / n
        for x in range(n):
            u = (x + 0.5) / n
            # ---- 背景：圆角方块 + 对角渐变 ----
            d = sd_round_rect(u, v, 0.5, 0.5, 0.5, 0.5, 0.205) * n
            if d >= 1.4:
                continue
            a = 1.0 - smoothstep(-0.8, 0.8, d)
            t = smoothstep(0.12, 0.92, (u + v) * 0.5)
            col = mix(TOP, BOT, t)
            # ---- 波浪 ----
            for base, amp, phase, freq, half, wa in waves:
                th = 2 * math.pi * freq * u + phase
                cy = base + amp * math.sin(th)
                slope = amp * 2 * math.pi * freq * math.cos(th)
                dd = abs(v - cy) / math.hypot(1.0, slope) * n
                w = 1.0 - smoothstep(half * n - 1.3, half * n + 1.3, dd)
                if w > 0.0:
                    col = mix(col, WHITE, w * wa)
            # ---- 下载箭头（实心：胶囊杆 + 三角头，经典下载符号） ----
            if zoom != 1.0:
                zu = 0.5 + (u - 0.5) / zoom
                zv = pivot + (v - pivot) / zoom
            else:
                zu, zv = u, v
            stem = sd_round_rect(zu, zv, 0.500, 0.3275, 0.0525, 0.1375, 0.0525)
            tri = sd_poly((zu, zv), ((0.295, 0.372), (0.705, 0.372), (0.500, 0.578)))
            arrow = min(stem, tri)
            aw = 1.0 - smoothstep(-0.7, 0.7, arrow * n)
            if aw > 0.0:
                col = mix(col, WHITE, aw)
            # 写预乘 BGRA
            i = (y * n + x) * 4
            buf[i] = int(col[2] * a + 0.5)
            buf[i + 1] = int(col[1] * a + 0.5)
            buf[i + 2] = int(col[0] * a + 0.5)
            buf[i + 3] = int(a * 255 + 0.5)
    return buf


def resample(master: bytearray, n: int, size: int) -> bytearray:
    """盒式重采样（预乘域）到 size×size。"""
    out = bytearray(size * size * 4)
    ratio = n / size
    for oy in range(size):
        y0 = int(oy * ratio)
        y1 = max(y0 + 1, int((oy + 1) * ratio))
        for ox in range(size):
            x0 = int(ox * ratio)
            x1 = max(x0 + 1, int((ox + 1) * ratio))
            b = g = r = a = cnt = 0
            for sy in range(y0, min(y1, n)):
                row = sy * n
                for sx in range(x0, min(x1, n)):
                    i = (row + sx) * 4
                    b += master[i]
                    g += master[i + 1]
                    r += master[i + 2]
                    a += master[i + 3]
                    cnt += 1
            o = (oy * size + ox) * 4
            out[o] = b // cnt
            out[o + 1] = g // cnt
            out[o + 2] = r // cnt
            out[o + 3] = a // cnt
    return out


def unpremul(pre: bytearray, size: int) -> bytearray:
    """预乘 BGRA → 直通 RGBA（PNG 用）与直通 BGRA（ICO 用）。"""
    rgba = bytearray(size * size * 4)
    bgra = bytearray(size * size * 4)
    for i in range(0, size * size * 4, 4):
        a = pre[i + 3]
        if a == 0:
            continue
        b = min(255, pre[i] * 255 // a)
        g = min(255, pre[i + 1] * 255 // a)
        r = min(255, pre[i + 2] * 255 // a)
        rgba[i], rgba[i + 1], rgba[i + 2], rgba[i + 3] = r, g, b, a
        bgra[i], bgra[i + 1], bgra[i + 2], bgra[i + 3] = b, g, r, a
    return rgba


def write_png(path: Path, size: int, rgba: bytearray) -> None:
    raw = b"".join(b"\x00" + bytes(rgba[y * size * 4:(y + 1) * size * 4]) for y in range(size))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9))
           + chunk(b"IEND", b""))
    path.write_bytes(png)


def write_ico(path: Path, images: list[tuple[int, bytearray]]) -> None:
    """images: [(size, 直通 BGRA 顶行优先)]，写 32bpp DIB 条目（兼容性最好）。"""
    blobs, entries = [], []
    for size, bgra in images:
        hdr = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
        xor = bytearray()
        for y in range(size - 1, -1, -1):          # DIB 自底向上
            xor += bgra[y * size * 4:(y + 1) * size * 4]
        row = ((size + 31) // 32) * 4              # AND 掩码行（按 32 位对齐）
        and_mask = bytes(row * size)
        blobs.append(hdr + bytes(xor) + and_mask)
        entries.append(size)
    out = bytearray(struct.pack("<HHH", 0, 1, len(blobs)))
    offset = 6 + 16 * len(blobs)
    for size, blob in zip(entries, blobs):
        w = 0 if size >= 256 else size
        out += struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
    for blob in blobs:
        out += blob
    path.write_bytes(bytes(out))


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    out_ico = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "icon.ico"
    out_ico.parent.mkdir(parents=True, exist_ok=True)

    master = render_master(MASTER)
    master_simple = render_master(MASTER, simple=True)   # 16px 专用简化版
    images = []
    preview = None
    for size in SIZES:
        src = master_simple if size < 24 else master
        pre = src if size == MASTER else resample(src, MASTER, size)
        rgba = unpremul(pre, size)
        bgra = bytearray(len(rgba))
        for i in range(0, len(rgba), 4):
            bgra[i], bgra[i + 1], bgra[i + 2], bgra[i + 3] = rgba[i + 2], rgba[i + 1], rgba[i], rgba[i + 3]
        images.append((size, bgra))
        if size == MASTER:
            preview = rgba
    write_ico(out_ico, images)
    png_path = out_ico.with_suffix(".png")
    write_png(png_path, MASTER, preview)
    print(f"ICO: {out_ico} | {out_ico.stat().st_size} bytes | 尺寸: {', '.join(map(str, SIZES))}")
    print(f"预览: {png_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
