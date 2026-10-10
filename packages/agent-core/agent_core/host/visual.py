"""浏览器预览与网页自检（O / P1-11）的纯 stdlib 图像工具。

## 为什么自己解 PNG

agent-core 有一条硬约束：核心链路**零第三方依赖**（`dependencies = []`）。
而「截图自检」需要把两张 PNG 比出差异 —— Electron 的 `capturePage()` 与
落盘的基线都是 PNG，PNG 又只是「zlib 流 + 逐行过滤」，标准库的 `zlib`
足够解开。于是这里手写一个**只解码、不编码**的最小 PNG 解码器，
换来「为这个功能不加任何一个依赖」。

## 支持的子集刻意收窄

只认真实截图会用到的那部分：8 位色深、非隔行、色型 0/2/3/4/6
（灰度 / RGB / 调色板 / 灰度+alpha / RGBA）。遇到不支持的形态抛 `ValueError`，
由调用方降级为一行说明 —— 不假装成功。

## 差异口径是**粗粒度**的

按 16×16 网格把整幅图降成「块平均色」，再数有多少块的平均色跨过了阈值。
这不是像素级/感知哈希的精确回归，故意如此：目标是回答「整块变了没有」
（布局塌了、主题色换了、白屏了），而不是「第 37 行第 5 个像素差了几级」。
"""

from __future__ import annotations

import zlib

#: PNG 文件签名。
_PNG_SIG = b"\x89PNG\r\n\x1a\n"

#: 默认单张截图字节上限（8 MiB）—— 1280×800 的网页截图远小于此。
DEFAULT_MAX_SCREENSHOT_BYTES = 8 << 20

#: 色型 → 通道数（仅 8 位色深）。0=灰度 2=RGB 3=调色板 4=灰度+alpha 6=RGBA
_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def is_png(data: bytes) -> bool:
    """按签名判断是不是 PNG 数据。"""
    return len(data) >= 8 and data[:8] == _PNG_SIG


def _unfilter(ftype: int, line: bytearray, prev: bytearray, bpp: int) -> None:
    """就地还原一行像素（PNG 的五种行过滤器）。"""
    n = len(line)
    if ftype == 0:
        return
    if ftype == 1:  # Sub
        for i in range(bpp, n):
            line[i] = (line[i] + line[i - bpp]) & 0xFF
    elif ftype == 2:  # Up
        for i in range(n):
            line[i] = (line[i] + prev[i]) & 0xFF
    elif ftype == 3:  # Average
        for i in range(n):
            a = line[i - bpp] if i >= bpp else 0
            line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
    elif ftype == 4:  # Paeth
        for i in range(n):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            pa = abs(b - c)
            pb = abs(a - c)
            pc = abs(a + b - 2 * c)
            if pa <= pb and pa <= pc:
                pr = a
            elif pb <= pc:
                pr = b
            else:
                pr = c
            line[i] = (line[i] + pr) & 0xFF
    else:
        raise ValueError(f"未知的 PNG 行过滤类型 {ftype}")


def _over(channel: int, alpha: int) -> int:
    """把一个通道按 alpha 合成到白底（截图基色多为白，比丢 alpha 更接近肉眼）。"""
    return (channel * alpha + 255 * (255 - alpha)) // 255


def _row_to_rgb(
    color: int, line: bytearray, palette: bytes, width: int, out: bytearray, y: int
) -> None:
    """把已还原的一行像素转成 RGB，写进 `out`。"""
    base = y * width * 3
    if color == 2:  # RGB
        out[base : base + width * 3] = line[: width * 3]
    elif color == 6:  # RGBA
        for x in range(width):
            i = x * 4
            a = line[i + 3]
            out[base + x * 3] = _over(line[i], a)
            out[base + x * 3 + 1] = _over(line[i + 1], a)
            out[base + x * 3 + 2] = _over(line[i + 2], a)
    elif color == 0:  # 灰度
        for x in range(width):
            g = line[x]
            j = base + x * 3
            out[j] = out[j + 1] = out[j + 2] = g
    elif color == 4:  # 灰度 + alpha
        for x in range(width):
            v = _over(line[x * 2], line[x * 2 + 1])
            j = base + x * 3
            out[j] = out[j + 1] = out[j + 2] = v
    else:  # color == 3，调色板
        for x in range(width):
            idx = line[x] * 3
            j = base + x * 3
            if idx + 2 < len(palette):
                out[j] = palette[idx]
                out[j + 1] = palette[idx + 1]
                out[j + 2] = palette[idx + 2]


def decode_png(data: bytes) -> tuple[int, int, bytes]:
    """解码 PNG 为 ``(width, height, rgb_bytes)``（RGB 三通道、逐行排列）。

    识别不出 / 不支持的形态抛 `ValueError`，消息面向用户可读。
    """
    if not is_png(data):
        raise ValueError("不是 PNG 数据")
    pos = 8
    end = len(data)
    width = height = 0
    depth = color = interlace = -1
    palette = b""
    idat = bytearray()

    while pos + 8 <= end:
        length = int.from_bytes(data[pos : pos + 4], "big")
        ctype = data[pos + 4 : pos + 8]
        body_start = pos + 8
        body_end = body_start + length
        if body_end + 4 > end:
            raise ValueError("PNG 数据不完整")
        body = data[body_start:body_end]
        pos = body_end + 4  # 跳过 CRC
        if ctype == b"IHDR":
            width = int.from_bytes(body[0:4], "big")
            height = int.from_bytes(body[4:8], "big")
            depth = body[8]
            color = body[9]
            interlace = body[12]
        elif ctype == b"PLTE":
            palette = body
        elif ctype == b"IDAT":
            idat.extend(body)
        elif ctype == b"IEND":
            break

    if width <= 0 or height <= 0:
        raise ValueError("PNG 头信息不完整")
    if depth != 8:
        raise ValueError(f"暂不支持 {depth} 位色深的 PNG")
    if interlace != 0:
        raise ValueError("暂不支持隔行（Adam7）PNG")
    if color not in _CHANNELS:
        raise ValueError(f"暂不支持色型 {color} 的 PNG")

    channels = _CHANNELS[color]
    stride = width * channels
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as exc:
        raise ValueError(f"PNG 解压失败：{exc}") from exc
    if len(raw) < (stride + 1) * height:
        raise ValueError("PNG 像素数据不完整")

    rgb = bytearray(width * height * 3)
    prev = bytearray(stride)
    p = 0
    for y in range(height):
        ftype = raw[p]
        p += 1
        line = bytearray(raw[p : p + stride])
        p += stride
        _unfilter(ftype, line, prev, channels)
        _row_to_rgb(color, line, palette, width, rgb, y)
        prev = line
    return width, height, bytes(rgb)


def _block_avg(
    rgb: bytes, w: int, h: int, gx: int, gy: int, grid: int
) -> tuple[int, int, int]:
    """网格第 (gx, gy) 块的平均 RGB。块边界按比例映射，故两图尺寸不同也能比。"""
    x0 = (gx * w) // grid
    x1 = ((gx + 1) * w) // grid
    y0 = (gy * h) // grid
    y1 = ((gy + 1) * h) // grid
    if x1 <= x0:
        x1 = min(x0 + 1, w)
    if y1 <= y0:
        y1 = min(y0 + 1, h)
    sr = sg = sb = 0
    n = 0
    for y in range(y0, y1):
        row = y * w * 3
        for x in range(x0, x1):
            i = row + x * 3
            sr += rgb[i]
            sg += rgb[i + 1]
            sb += rgb[i + 2]
            n += 1
    if n == 0:
        return 0, 0, 0
    return sr // n, sg // n, sb // n


def diff_ratio(
    current: bytes, baseline: bytes, *, grid: int = 16, threshold: int = 18
) -> tuple[float, int, int]:
    """粗粒度差异：返回 ``(差异比 0..1, 变化块数, 总块数)``。

    逐块比较平均 RGB，任一通道差超过 `threshold`（0–255）即记为「变了」。
    差异比 = 变化块数 / 总块数 —— 一眼能读成「大约几成区域不一样了」。
    """
    wa, ha, ca = decode_png(current)
    wb, hb, cb = decode_png(baseline)
    cells = max(1, int(grid))
    changed = 0
    total = 0
    for gy in range(cells):
        for gx in range(cells):
            ba = _block_avg(ca, wa, ha, gx, gy, cells)
            bb = _block_avg(cb, wb, hb, gx, gy, cells)
            total += 1
            if (
                abs(ba[0] - bb[0]) > threshold
                or abs(ba[1] - bb[1]) > threshold
                or abs(ba[2] - bb[2]) > threshold
            ):
                changed += 1
    ratio = (changed / total) if total else 0.0
    return ratio, changed, total


__all__ = [
    "DEFAULT_MAX_SCREENSHOT_BYTES",
    "decode_png",
    "diff_ratio",
    "is_png",
]