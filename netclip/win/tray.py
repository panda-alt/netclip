"""系统托盘图标（纯 ctypes，**不依赖 pystray**）。

为什么自己写而不用 pystray
--------------------------
1. 这个项目的卖点之一就是"零依赖、复制过去就能跑"。为一个托盘图标引入
   pystray + Pillow 不划算（Pillow 是几十 MB）。
2. pystray 要求自己的 `run()` 跑在占据主线程的消息循环里，而我们**已经**有
   一个主线程消息循环（`__main__._main_loop`）。自己写可以直接把托盘窗口
   挂进那个循环，少一层线程和一个消息泵。
3. 图标是自己解码 `.ico` + GDI 拼出来的，不需要 Pillow。默认用
   `assets/netclip.ico`（见 `extract_icon_frame`）外面套一圈状态色描边；
   文件不在就退回"圆角方块 + 状态色 + 字母"。

状态怎么看出来
--------------
原先整个图标是一个状态色方块（绿=已连通 / 灰=等待 / 橙=光标在对端 / 红=已暂停）。
换成真图标之后，状态色挪到了**外沿那一圈描边**上 —— 一眼看颜色，同时还能认出
程序。用圈而不是角标圆点，是因为右下角正好是脸，16×16 下盖住就认不出来了。
鼠标悬停的 tooltip 和右键菜单里也都有文字版状态，不用靠猜。

线程模型
--------
`Shell_NotifyIcon` 要求回调消息被某个窗口接收，而窗口必须有消息泵。
这里在**主线程**创建一个 message-only 窗口，并在主线程的消息循环里
（`__main__._main_loop`）把消息转给 `TrayWindow.handle_message()`。

所以：**tray 的所有公开方法都必须在主线程调用**。`Session` 的运行状态由
会话内部的线程更新，托盘只做只读轮询（通过配置的定时器）。
"""

from __future__ import annotations

import ctypes
import logging
import struct
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import winapi as w
from .hooks import WNDCLASSEX

log = logging.getLogger("netclip.win.tray")

#: 随程序分发的托盘图标（`tools/make_icon.py` 生成的七尺寸 `.ico`）。
ICON_RESOURCE = "assets/netclip.ico"

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# --------------------------------------------------------------------- 菜单命令 ID


class Command:
    TOGGLE = 1
    RECAPTURE = 2
    OPEN_STAGING = 3
    OPEN_LOG = 4
    COPY_STATUS = 5
    QUIT = 6


#: 图标颜色（BGR）与形状。用颜色表达状态，不用去猜一个 16x16 的小字母。
COLOR_ACTIVE = (0x4C, 0xC7, 0x3F)  # 绿：已连通
COLOR_LOCAL = (0xB0, 0xB0, 0xB0)  # 灰：只监听，未连通
COLOR_REMOTE = (0x2C, 0xC7, 0xF0)  # 橙黄：光标在对端
COLOR_PAUSED = (0x3C, 0x3C, 0xE8)  # 红：已暂停


@dataclass
class TrayState:
    """托盘要展示的状态（由调用方填充）。"""

    enabled: bool = True
    remote: bool = False
    input_up: bool = False
    clip_up: bool = False
    file_up: bool = False
    peer: str = ""
    detail: str = ""

    def color(self) -> "tuple[int, int, int]":
        if not self.enabled:
            return COLOR_PAUSED
        if self.remote:
            return COLOR_REMOTE
        return COLOR_ACTIVE if self.input_up else COLOR_LOCAL

    def tooltip(self) -> str:
        if not self.enabled:
            head = "netclip：已暂停"
        elif self.remote:
            head = "netclip：光标在对端"
        elif self.input_up:
            head = "netclip：已连接"
        else:
            head = "netclip：等待对端"
        if self.peer:
            head += "（%s）" % self.peer
        channels = "".join(
            [
                "I" if self.input_up else "-",
                "C" if self.clip_up else "-",
                "F" if self.file_up else "-",
            ]
        )
        line2 = "通道 %s" % channels
        if self.detail:
            line2 += " | " + self.detail
        # 托盘 tooltip 有 127 字符上限，超了会被系统截断；自己先截干净
        return ("%s\n%s" % (head, line2))[:127]

    def menu_labels(self) -> "List[str]":
        return [
            "netclip %s" % ("（已暂停）" if not self.enabled else ("（光标在对端）" if self.remote else "（运行中）")),
            "暂停共享" if self.enabled else "恢复共享",
            "把光标拉回本机",
        ]


# --------------------------------------------------------------------- 图标绘制


def extract_icon_frame(data: bytes, size: int) -> "Optional[bytes]":
    """从 `.ico` 里取出一帧，返回**自上而下**的 BGRA 字节（`size*size*4`）。

    为什么自己解析而不是 `LoadImage(..., LR_LOADFROMFILE)`：那个 API 拿到的是
    一个 HICON，没法把状态色描边叠上去，也没法在源码运行（没有 exe 资源）时用。
    这里只做纯字节处理，可以脱离 Windows 单独测试。

    只认 32 位的帧，两种存法都支持：

      * **BMP**（老式 ICO 的写法）—— `_decode_bmp_frame`；
      * **PNG**（Pillow 11 写出来**每一档**都是 PNG，实测七档全是）——
        `_decode_png_frame`。

    按尺寸接近程度**从近到远试**，第一个解得出来的就用，没有再最近邻缩放。
    不先把所有帧都解一遍再挑：那份 `.ico` 里有 16…256 七档，全都解一遍的话
    光 256 那档就是 6.5 万个像素的 Python 循环，而我们只要 32 那一档。
    """
    if size <= 0 or len(data) < 6:
        return None
    reserved, kind, count = struct.unpack_from("<HHH", data, 0)
    if reserved != 0 or kind != 1 or count == 0:
        return None

    entries: "List[Tuple[int, int, int, int, bool]]" = []
    for index in range(count):
        entry = 6 + 16 * index
        if entry + 16 > len(data):
            break
        width = data[entry] or 256
        height = data[entry + 1] or 256
        bpp = struct.unpack_from("<H", data, entry + 6)[0]
        nbytes = struct.unpack_from("<I", data, entry + 8)[0]
        offset = struct.unpack_from("<I", data, entry + 12)[0]
        if bpp != 32 or offset + nbytes > len(data) or nbytes < 40:
            continue
        is_png = data[offset : offset + 8] == _PNG_MAGIC
        entries.append((width, offset, nbytes, height, is_png))

    entries.sort(key=lambda item: (abs(item[0] - size), item[0]))
    for width, offset, nbytes, height, is_png in entries:
        if is_png:
            decoded = _decode_png_frame(data[offset : offset + nbytes])
        else:
            decoded = _decode_bmp_frame(data, offset, width, height, nbytes)
        if decoded is None:
            continue
        side, bgra = decoded
        return bgra if side == size else _scale_bgra(bgra, side, size)
    return None


def _decode_png_frame(data: bytes) -> "Optional[Tuple[int, bytes]]":
    """解一帧 PNG，返回 `(边长, 自上而下的 BGRA)`。

    为什么用 `zlib` 手写而不是引 Pillow：这一整块（含托盘）的存在意义就是
    "复制过去就能跑"，为一个托盘图标拖进几十 MB 的 Pillow 不划算。
    PNG 的**解码**比编码简单得多 —— 只有 IDAT + 五种行过滤器。

    只处理非隔行、8 位深。真图标本来就是 Pillow 从 PNG 转出来的，够用。
    """
    import zlib

    offset = 8  # 跳过 PNG 签名
    width = height = 0
    depth = color_type = 0
    palette = b""
    alpha_table = b""
    idat = bytearray()

    while offset + 8 <= len(data):
        (length,) = struct.unpack_from(">I", data, offset)
        chunk_type = data[offset + 4 : offset + 8]
        body = offset + 8
        if body + length + 4 > len(data):
            return None
        payload = data[body : body + length]
        offset = body + length + 4  # +4 跳过 CRC

        if chunk_type == b"IHDR":
            width, height, depth, color_type, compression, filter_method, interlace = (
                struct.unpack(">IIBBBBB", payload)
            )
            if compression or filter_method or interlace or depth != 8:
                return None
        elif chunk_type == b"PLTE":
            palette = payload
        elif chunk_type == b"tRNS":
            alpha_table = payload
        elif chunk_type == b"IDAT":
            idat += payload
        elif chunk_type == b"IEND":
            break

    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
    if channels is None or width <= 0 or height <= 0:
        return None

    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error:
        log.debug("PNG 的 IDAT 解压失败", exc_info=True)
        return None

    stride = width * channels
    if len(raw) < (stride + 1) * height:
        return None

    rows = bytearray(stride * height)
    previous = bytearray(stride)
    for y in range(height):
        filter_type = raw[y * (stride + 1)]
        if filter_type > 4:
            log.debug("PNG 行过滤器类型非法: %d", filter_type)
            return None
        line = bytearray(raw[y * (stride + 1) + 1 : (y + 1) * (stride + 1)])
        _unfilter_line(line, previous, filter_type, channels)
        rows[y * stride : (y + 1) * stride] = line
        previous = line

    bgra = _png_rows_to_bgra(rows, width, height, color_type, palette, alpha_table)
    return (width, bgra) if bgra is not None else None


def _unfilter_line(line: bytearray, previous: bytearray, filter_type: int, bpp: int) -> None:
    """就地还原一行像素（PNG 的行过滤器，规范第 9.2 节）。"""
    size = len(line)
    if filter_type == 0:  # None
        return
    if filter_type == 1:  # Sub：减左边
        for i in range(bpp, size):
            line[i] = (line[i] + line[i - bpp]) & 0xFF
    elif filter_type == 2:  # Up：减上一行
        for i in range(size):
            line[i] = (line[i] + previous[i]) & 0xFF
    elif filter_type == 3:  # Average：减左边和上行的平均
        for i in range(size):
            left = line[i - bpp] if i >= bpp else 0
            line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
    elif filter_type == 4:  # Paeth
        for i in range(size):
            left = line[i - bpp] if i >= bpp else 0
            up = previous[i]
            up_left = previous[i - bpp] if i >= bpp else 0
            line[i] = (line[i] + _paeth(left, up, up_left)) & 0xFF


def _paeth(a: int, b: int, c: int) -> int:
    """PNG 的 Paeth 预测器：取 a/b/c 里最接近 `a+b-c` 的那个。"""
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _png_rows_to_bgra(
    rows: bytes,
    width: int,
    height: int,
    color_type: int,
    palette: bytes,
    alpha_table: bytes,
) -> "Optional[bytes]":
    """把 PNG 的原始像素行转成 BGRA（和 BMP 那条路径同一个出口）。"""
    out = bytearray(width * height * 4)
    count = width * height
    for index in range(count):
        if color_type == 6:  # RGBA
            r, g, b, a = rows[index * 4 : index * 4 + 4]
        elif color_type == 2:  # RGB
            r, g, b = rows[index * 3 : index * 3 + 3]
            a = 255
        elif color_type == 0:  # 灰度
            r = g = b = rows[index]
            a = 255
        elif color_type == 4:  # 灰度 + alpha
            r = g = b = rows[index * 2]
            a = rows[index * 2 + 1]
        else:  # color_type == 3，调色板
            entry = rows[index] * 3
            if entry + 3 > len(palette):
                return None
            r, g, b = palette[entry : entry + 3]
            a = alpha_table[rows[index]] if rows[index] < len(alpha_table) else 255
        dst = index * 4
        out[dst] = b
        out[dst + 1] = g
        out[dst + 2] = r
        out[dst + 3] = a
    return bytes(out)


def _decode_bmp_frame(
    data: bytes, offset: int, width: int, height: int, nbytes: int
) -> "Optional[Tuple[int, bytes]]":
    """解一帧 ICO 里的 BMP 数据（`BITMAPINFOHEADER` + 自下而上的像素行）。"""
    header_size, bi_width, bi_height = struct.unpack_from("<Iii", data, offset)
    _planes, bit_count, compression = struct.unpack_from("<HHI", data, offset + 12)
    if header_size < 40 or compression != 0 or bit_count != 32:
        return None
    if bi_width <= 0 or bi_height == 0:
        return None

    #: ICO 里的 BMP 高度是实际高度的两倍 —— 下半部分是 1 位 AND 掩码
    rows = abs(bi_height) // 2 if abs(bi_height) == 2 * height else abs(bi_height)
    if rows != height or bi_width != width:
        return None

    stride = width * 4
    start = offset + header_size
    if start + stride * rows > offset + nbytes:
        return None

    buf = bytearray(stride * rows)
    for y in range(rows):
        src = start + (rows - 1 - y) * stride  # BMP 行是自下而上存的
        buf[y * stride : (y + 1) * stride] = data[src : src + stride]

    if not any(buf[3::4]):
        _apply_and_mask(buf, data, start + stride * rows, width, rows, offset + nbytes)
    return (width, bytes(buf))


def _apply_and_mask(
    buf: bytearray, data: bytes, mask_start: int, width: int, rows: int, limit: int
) -> None:
    """整帧 alpha 全 0 时，用 1 位 AND 掩码补出不透明度（老式 `.ico` 的写法）。

    掩码里 1 = 透明。不补的话整张图会完全看不见。
    """
    mask_stride = ((width + 31) // 32) * 4
    if mask_start + mask_stride * rows > limit:
        return
    for y in range(rows):
        row = data[mask_start + (rows - 1 - y) * mask_stride :]
        for x in range(width):
            transparent = row[x >> 3] & (0x80 >> (x & 7))
            buf[(y * width + x) * 4 + 3] = 0 if transparent else 255


def _scale_bgra(bgra: bytes, src_side: int, dst_side: int) -> bytes:
    """最近邻缩放。图标尺寸很小，缩放质量不是问题，但**必须**做，
    否则 DIB 的行跨度和实际宽度对不上，画出来是斜的。"""
    out = bytearray(dst_side * dst_side * 4)
    for y in range(dst_side):
        sy = min(src_side - 1, y * src_side // dst_side)
        for x in range(dst_side):
            sx = min(src_side - 1, x * src_side // dst_side)
            src = (sy * src_side + sx) * 4
            dst = (y * dst_side + x) * 4
            out[dst : dst + 4] = bgra[src : src + 4]
    return bytes(out)


#: 同一个尺寸只解码一次 —— 图标每次状态变化都会重建，而文件读取不该跟着跑。
_FRAME_CACHE: "Dict[Tuple[str, int], Optional[bytes]]" = {}


def load_icon_frame(path: "Optional[str]", size: int) -> "Optional[bytes]":
    """读文件 + 解码 + 缓存。任何一步失败都返回 None（调用方退回手绘图标）。"""
    if not path:
        return None
    key = (str(path), size)
    if key in _FRAME_CACHE:
        return _FRAME_CACHE[key]

    frame: "Optional[bytes]" = None
    try:
        frame = extract_icon_frame(Path(path).read_bytes(), size)
    except OSError:
        log.debug("读不到托盘图标文件 %s，改用手绘图标", path, exc_info=True)
    except Exception:  # pragma: no cover - 图标只影响外观
        log.debug("解析托盘图标失败 %s", path, exc_info=True)
    if frame is None:
        log.debug("图标文件 %s 里没有可用的 %d×%d 帧", path, size, size)

    _FRAME_CACHE[key] = frame
    return frame


def default_icon_path() -> "Optional[str]":
    """`assets/netclip.ico` 在哪。源码运行在仓库里，打包后在 `sys._MEIPASS`。"""
    from .. import config

    candidate = config.resource_path(ICON_RESOURCE)
    return str(candidate) if candidate.is_file() else None


class _IconInfo(ctypes.Structure):
    """`ICONINFO`：`CreateIconIndirect` 的入参。"""

    _fields_ = [
        ("fIcon", wintypes.BOOL),
        ("xHotspot", wintypes.DWORD),
        ("yHotspot", wintypes.DWORD),
        ("hbmMask", wintypes.HBITMAP),
        ("hbmColor", wintypes.HBITMAP),
    ]


def make_icon_hicon(
    color: "tuple[int, int, int]",
    size: int = 32,
    letter: str = "N",
    base: "Optional[bytes]" = None,
) -> int:
    """画一个图标并返回 HICON。

    `color` 是 BGR 顺序（GDI 的 COLORREF 约定），也是状态色。
    `base` 是 `load_icon_frame()` 给的底图（BGRA，`size*size*4`）；给了就画真图标
    + 一圈状态色描边，没给就退回"圆角方块 + 字母"。

    失败返回 0（调用方退回系统图标）。
    """
    hdc_screen = 0
    hdc_mem = 0
    hbm = 0
    old_bmp = 0
    font = 0
    info_ptr = ctypes.c_void_p()
    try:
        hdc_screen = w.user32.GetDC(None)
        if not hdc_screen:
            return 0
        hdc_mem = w.gdi32.CreateCompatibleDC(hdc_screen)
        if not hdc_mem:
            return 0

        info = w.BITMAPINFOHEADER()
        info.biSize = ctypes.sizeof(w.BITMAPINFOHEADER)
        info.biWidth = size
        info.biHeight = -size  # 负高度 = 自上而下，方便按行写像素
        info.biPlanes = 1
        info.biBitCount = 32
        info.biCompression = 0  # BI_RGB

        # 注意 HDC 句柄的类型：Windows 会用高地址（表现为很大的无符号数或负数）
        # 返回 GDI 句柄，所以这些函数的 restype 必须声明成 c_void_p，
        # 否则 ctypes 会尝试把 0xFFFF... 塞进 32 位 int 并抛 OverflowError。
        hbm = w.gdi32.CreateDIBSection(
            hdc_screen, ctypes.byref(info), 0, ctypes.byref(info_ptr), None, 0
        )
        if not hbm or not info_ptr:
            return 0
        old_bmp = w.gdi32.SelectObject(hdc_mem, hbm)

        # 直接往 DIB 里写像素比走 GDI 画刷更简单也更可控。
        buf = (ctypes.c_ubyte * (size * size * 4)).from_address(info_ptr.value)
        use_letter = True
        if base is not None and len(base) == size * size * 4:
            ctypes.memmove(buf, base, size * size * 4)
            _paint_ring(buf, size, color)
            use_letter = False
        else:
            _paint_icon(buf, size, color)

        # 字母只在手绘方块上画 —— 真图标上再叠一个字就糊成一团了
        if use_letter:
            lf = w.LOGFONTW()
            lf.lfHeight = -int(size * 0.62)
            lf.lfWeight = 700
            lf.lfCharSet = w.ANSI_CHARSET
            lf.lfQuality = w.DEFAULT_QUALITY
            lf.lfFaceName = "Segoe UI"
            font = w.gdi32.CreateFontIndirectW(ctypes.byref(lf))
            if font:
                old_font = w.gdi32.SelectObject(hdc_mem, font)
                w.gdi32.SetBkMode(hdc_mem, w.TRANSPARENT)
                w.gdi32.SetTextColor(hdc_mem, 0x00FFFFFF)  # 白字（COLORREF 是 BGR）
                rect = (ctypes.c_int * 4)(0, 0, size, size)
                w.user32.DrawTextW(
                    hdc_mem,
                    letter,
                    -1,
                    ctypes.byref(rect),
                    w.DT_CENTER | w.DT_VCENTER | w.DT_SINGLELINE,
                )
                w.gdi32.SelectObject(hdc_mem, old_font)

        # 用 ICONINFO 把 DIB 转成 HICON；掩码位图传同一个 DIB 表示"全用 alpha 通道"
        icon_info = _IconInfo()
        icon_info.fIcon = True
        icon_info.hbmMask = hbm
        icon_info.hbmColor = hbm
        hicon = w.user32.CreateIconIndirect(ctypes.byref(icon_info))
        if not hicon:
            log.debug("CreateIconIndirect 失败，将退回系统图标")
        return int(hicon or 0)
    except Exception:  # pragma: no cover - 图标只是外观，出错不该影响功能
        log.debug("绘制托盘图标失败", exc_info=True)
        return 0
    finally:
        if hdc_mem and old_bmp:
            w.gdi32.SelectObject(hdc_mem, old_bmp)
        if font:
            w.gdi32.DeleteObject(font)
        if hbm:
            w.gdi32.DeleteObject(hbm)
        if hdc_mem:
            w.gdi32.DeleteDC(hdc_mem)
        if hdc_screen:
            w.user32.ReleaseDC(None, hdc_screen)


def _paint_icon(buf, size: int, color: "tuple[int, int, int]") -> None:
    """把 buf 填成一个圆角方块（BGRA，自上而下）。"""
    b, g, r = color
    radius = max(2, size // 6)
    for y in range(size):
        for x in range(size):
            # 圆角：四个角上距离角点超过 radius 的像素透明
            dx = radius - x if x < radius else (x - (size - 1 - radius) if x > size - 1 - radius else 0)
            dy = radius - y if y < radius else (y - (size - 1 - radius) if y > size - 1 - radius else 0)
            inside = (dx * dx + dy * dy) <= radius * radius
            idx = (y * size + x) * 4
            if inside:
                buf[idx] = b
                buf[idx + 1] = g
                buf[idx + 2] = r
                buf[idx + 3] = 255
            else:
                buf[idx] = 0
                buf[idx + 1] = 0
                buf[idx + 2] = 0
                buf[idx + 3] = 0


def _paint_ring(buf, size: int, color: "tuple[int, int, int]", width: int = 0) -> None:
    """沿图标外沿刷一圈状态色。

    为什么是圈而不是角标圆点：**一点主体都不挡**。图标（`assets/netclip.ico`）
    四边都是背景，圈只压在那上面；而右下角圆点会盖到嘴/下巴 —— 托盘只有 16×16，
    盖住那点就认不出是谁了。

    圈的周长也远大于一个圆点，缩到 16×16 时圈是 1 像素粗，颜色仍然一眼可辨。
    """
    if width <= 0:
        width = max(1, size // 8)
    b, g, r = color
    for y in range(size):
        for x in range(size):
            if min(x, y, size - 1 - x, size - 1 - y) >= width:
                continue
            idx = (y * size + x) * 4
            buf[idx] = b
            buf[idx + 1] = g
            buf[idx + 2] = r
            buf[idx + 3] = 255


# --------------------------------------------------------------------- 托盘窗口


class TrayWindow:
    """托盘图标 + 右键菜单。

    必须在主线程创建与销毁；`handle_message()` 由主线程消息循环调用。
    """

    #: 状态轮询间隔（毫秒）
    TIMER_ID = 0xC1
    TIMER_INTERVAL_MS = 800

    #: 图标位图边长。16 是 Windows 托盘的经典尺寸，但系统在做 DPI 缩放和
    #: "大图标"时会取更大的那一档，32 是两边都能兼顾的。
    ICON_SIZE = 32

    def __init__(
        self,
        get_state: Callable[[], TrayState],
        on_command: Callable[[int], None],
        title: str = "netclip",
        letter: str = "N",
        icon_path: "Optional[str]" = None,
    ) -> None:
        self.get_state = get_state
        self.on_command = on_command
        self.title = title
        self.letter = letter
        #: None 表示"自己去仓库/包里找"；空串则强制手绘图标（测试用得上）
        self.icon_path = default_icon_path() if icon_path is None else icon_path

        self.hwnd: Optional[int] = None
        self._hicon = 0
        self._owns_icon = False
        self._last_state: Optional[TrayState] = None
        self._last_icon_color: Optional[tuple] = None
        self._added = False
        self._menu: Optional[int] = None
        self._wnd_proc = w.WNDPROC(self._wnd_callback)
        #: 资源管理器重启后广播的消息号（见 `_readd_after_taskbar_restart`）。
        #: `RegisterWindowMessageW` 只是把一个字符串映射成一个消息号，没有副作用，
        #: 但它在非 Windows 上不存在，所以这里兜一下。
        try:
            self._taskbar_created = int(w.user32.RegisterWindowMessageW("TaskbarCreated"))
        except Exception:  # pragma: no cover - 只影响这一条自愈路径
            self._taskbar_created = 0
        self.available = True
        self.last_error = ""

    # ------------------------------------------------------------ 生命周期

    def create(self) -> bool:
        try:
            self.hwnd = self._create_window()
        except Exception as exc:
            self.available = False
            self.last_error = str(exc)
            log.warning("托盘窗口创建失败，托盘功能不可用: %s", exc)
            return False

        self._hicon = self._make_icon(self.get_state().color())
        self._owns_icon = bool(self._hicon)
        if not self._hicon:
            self._hicon = int(w.user32.LoadIconW(None, ctypes.c_void_p(w.IDI_APPLICATION)) or 0)

        if not self._add_icon():
            self.available = False
            log.warning("Shell_NotifyIcon(NIM_ADD) 失败，托盘功能不可用")
            return False

        w.user32.SetTimer(self.hwnd, self.TIMER_ID, self.TIMER_INTERVAL_MS, None)
        self._refresh(force=True)
        log.info("托盘图标已就绪")
        return True

    def destroy(self) -> None:
        if self.hwnd:
            try:
                w.user32.KillTimer(self.hwnd, self.TIMER_ID)
            except Exception:  # pragma: no cover
                pass
        if self._added:
            self._notify_icon(w.NIM_DELETE, flags=0)
            self._added = False
        if self._hicon and self._owns_icon:
            try:
                w.user32.DestroyIcon(self._hicon)
            except Exception:  # pragma: no cover
                pass
        self._hicon = 0
        if self.hwnd:
            try:
                w.user32.DestroyWindow(self.hwnd)
            except Exception:  # pragma: no cover
                pass
            self.hwnd = None

    # ------------------------------------------------------------ 窗口

    def _create_window(self) -> int:
        hinstance = w.kernel32.GetModuleHandleW(None)
        class_name = "netclip_tray_window"
        wc = WNDCLASSEX()
        wc.cbSize = ctypes.sizeof(WNDCLASSEX)
        wc.lpfnWndProc = ctypes.cast(self._wnd_proc, ctypes.c_void_p)
        wc.hInstance = hinstance
        wc.lpszClassName = class_name
        atom = w.user32.RegisterClassExW(ctypes.byref(wc))
        if not atom and ctypes.get_last_error() not in (0, 1410):  # 1410 = 类已存在
            log.debug("RegisterClassExW(tray) 返回 %d", ctypes.get_last_error())

        hwnd = w.user32.CreateWindowExW(
            0, class_name, "netclip-tray", 0, 0, 0, 0, 0, w.HWND_MESSAGE, None, hinstance, None
        )
        if not hwnd:
            raise w.WinApiError(ctypes.get_last_error(), "CreateWindowExW(tray) 失败")
        return int(hwnd)

    def _wnd_callback(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        if msg == w.WM_TRAY_NOTIFY:
            event = int(lparam) & 0xFFFF
            self._on_tray_event(event)
            return 0
        if msg == w.WM_TIMER and int(wparam) == self.TIMER_ID:
            self._refresh()
            return 0
        if msg == w.WM_TRAY_UPDATE:
            self._refresh(force=True)
            return 0
        if msg == w.WM_TRAY_QUIT:
            self.destroy()
            w.user32.PostQuitMessage(0)
            return 0
        if self._taskbar_created and msg == self._taskbar_created:
            self._readd_after_taskbar_restart()
            return 0
        if msg == w.WM_DESTROY:
            w.user32.PostQuitMessage(0)
            return 0
        return int(w.user32.DefWindowProcW(hwnd, msg, wparam, lparam))

    def handle_message(self, msg: "w.MSG") -> bool:
        """主线程消息循环调用。返回 True 表示这条消息已被托盘的窗口过程处理。"""
        if self.hwnd is None or int(msg.hwnd or 0) != self.hwnd:
            return False
        w.user32.TranslateMessage(ctypes.byref(msg))
        w.user32.DispatchMessageW(ctypes.byref(msg))
        return True

    # ------------------------------------------------------------ 图标与提示

    def _make_icon(self, color: "tuple[int, int, int]") -> int:
        """按当前状态色生成托盘图标：真图标优先，找不到就手绘一个方块。"""
        base = load_icon_frame(self.icon_path, self.ICON_SIZE)
        return make_icon_hicon(
            color, size=self.ICON_SIZE, letter=self.letter, base=base
        )

    def _notify_icon(self, message: int, flags: Optional[int] = None) -> bool:
        data = w.NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(w.NOTIFYICONDATAW)
        data.hWnd = self.hwnd
        data.uID = 1
        if flags is None:
            flags = w.NIF_MESSAGE | w.NIF_ICON | w.NIF_TIP
        data.uFlags = flags
        data.uCallbackMessage = w.WM_TRAY_NOTIFY
        data.hIcon = self._hicon
        data.szTip = self.get_state().tooltip()[:127]
        return bool(w.shell32.Shell_NotifyIconW(message, ctypes.byref(data)))

    def _add_icon(self) -> bool:
        ok = self._notify_icon(w.NIM_ADD)
        self._added = ok
        return ok

    def _readd_after_taskbar_restart(self) -> None:
        """资源管理器重启后，把图标重新加回托盘。

        `Shell_NotifyIcon` 加进去的图标是挂在**当前那一份任务栏**上的。explorer.exe
        一旦重启（崩溃、系统更新、或者手动重启），之前加的图标全部消失，而且**系统
        不会替我们补** —— 唯一的机会是它广播的 `TaskbarCreated` 消息，接到就自己
        再加一次。不处理的话托盘图标会永久消失（程序还活着、只是看不见了），
        用户唯一的办法是重启 netclip。
        """
        if self.hwnd is None:
            return
        self._added = False
        if not self._add_icon():
            #: 任务栏刚起来时可能还没准备好接受 NIM_ADD。不致命 —— 800ms 的定时器
            #: 会走 `_refresh()`，那里有同一条补挂逻辑。
            log.warning("资源管理器重启后重新添加托盘图标失败，定时器会再试")
            return
        self.available = True
        log.info("资源管理器已重启，托盘图标已重新添加")
        self._refresh(force=True)

    def _refresh(self, force: bool = False) -> None:
        state = self.get_state()
        #: 图标还没挂进托盘时**绝不提前返回** —— 否则状态没变就永远走不到下面
        #: 那条补挂逻辑，托盘里会一直空着。
        if not force and self._added and state == self._last_state:
            return
        self._last_state = state

        if state.color() != self._last_icon_color:
            new_icon = self._make_icon(state.color())
            if new_icon:
                if self._hicon and self._owns_icon:
                    try:
                        w.user32.DestroyIcon(self._hicon)
                    except Exception:  # pragma: no cover
                        pass
                self._hicon = new_icon
                self._owns_icon = True
                self._last_icon_color = state.color()
        if self._added:
            self._notify_icon(w.NIM_MODIFY)
        else:
            #: 图标还没挂上去（第一次就没成功，或者资源管理器刚重启还没准备好接受
            #: `NIM_ADD`）—— 借这次刷新再试一次。不然程序活着、托盘里却永远没有它，
            #: 用户只能靠重启程序才能看见图标。
            if self._add_icon():
                self.available = True
                log.info("托盘图标已补挂上去（%s）", self.title)

    def notify(self, title: str, text: str, level: str = "info") -> bool:
        """弹一个气泡通知。托盘不可用时返回 False。

        用于替代"弹模态对话框问用户" —— 后台常驻程序不该抢焦点。
        """
        if not self._added or self.hwnd is None:
            return False

        if level == "error":
            info_flags = w.NIIF_ERROR
        elif level in ("warn", "warning"):
            info_flags = w.NIIF_WARNING
        else:
            info_flags = w.NIIF_INFO

        data = w.NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(w.NOTIFYICONDATAW)
        data.hWnd = self.hwnd
        data.uID = 1
        data.uFlags = w.NIF_INFO
        data.szInfo = str(text)[:255]
        data.szInfoTitle = str(title)[:63]
        data.dwInfoFlags = info_flags
        data.hIcon = self._hicon
        return bool(w.shell32.Shell_NotifyIconW(w.NIM_MODIFY, ctypes.byref(data)))

    # ------------------------------------------------------------ 菜单

    def _on_tray_event(self, event: int) -> None:
        if event in (w.WM_RBUTTONUP, w.WM_CONTEXTMENU):
            self._show_menu()
        elif event == w.WM_LBUTTONDBLCLK:
            self.on_command(Command.TOGGLE)

    def _show_menu(self) -> None:
        state = self.get_state()
        labels = state.menu_labels()

        menu = w.user32.CreatePopupMenu()
        if not menu:
            return
        try:
            # 第一项是纯状态展示，用 ID 0 + 灰色表示不可点
            w.user32.AppendMenuW(menu, w.MF_STRING | w.MF_DISABLED, 0, labels[0])
            w.user32.AppendMenuW(menu, w.MF_SEPARATOR, 0, None)
            w.user32.AppendMenuW(menu, w.MF_STRING, Command.TOGGLE, labels[1])
            w.user32.AppendMenuW(menu, w.MF_STRING, Command.RECAPTURE, labels[2])
            w.user32.AppendMenuW(menu, w.MF_SEPARATOR, 0, None)
            w.user32.AppendMenuW(menu, w.MF_STRING, Command.OPEN_STAGING, "打开接收目录")
            w.user32.AppendMenuW(menu, w.MF_STRING, Command.OPEN_LOG, "打开日志")
            w.user32.AppendMenuW(menu, w.MF_STRING, Command.COPY_STATUS, "复制状态到剪贴板")
            w.user32.AppendMenuW(menu, w.MF_SEPARATOR, 0, None)
            w.user32.AppendMenuW(menu, w.MF_STRING, Command.QUIT, "退出 netclip")

            # 必须先把我们的窗口设为前台，否则点菜单外面菜单不会消失
            w.user32.SetForegroundWindow(self.hwnd)
            pt = w.POINT()
            w.user32.GetCursorPos(ctypes.byref(pt))
            cmd = w.user32.TrackPopupMenu(
                menu,
                w.TPM_RIGHTBUTTON | w.TPM_RETURNCMD | w.TPM_NONOTIFY,
                pt.x,
                pt.y,
                0,
                self.hwnd,
                None,
            )
            if cmd:
                self.on_command(int(cmd))
        finally:
            w.user32.DestroyMenu(menu)


__all__ = [
    "Command",
    "TrayState",
    "TrayWindow",
    "default_icon_path",
    "extract_icon_frame",
    "load_icon_frame",
    "make_icon_hicon",
]
