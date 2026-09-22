"""剪贴板读写与占用冲突处理。

这个模块的存在理由只有一个：**在别的程序正占着剪贴板的时候，既不失败也不卡住**。

Mouse Without Borders 有过"导致其它应用无法写入剪贴板"的反馈，根因通常是：
  1. 用 `SetClipboardViewer` 链式监听，处理不当会长时间持有剪贴板；
  2. `OpenClipboard` 失败时直接放弃或无限重试；
  3. 在消息循环线程里做延迟渲染（WM_RENDERFORMAT）时被阻塞。

这里的对策：
  * 用 `AddClipboardFormatListener` 接收 `WM_CLIPBOARDUPDATE` 通知，不参与 view 链；
  * 打开剪贴板失败时按 `open_retry_ms` 退避重试（默认约 0.6s 窗口），失败就**放弃这一次**；
  * 把握剪贴板的时间压到毫秒级：打开 -> 枚举/读取 -> 立刻关闭；
  * 所有路径都用 try/finally 保证 `CloseClipboard` 一定被调用。
"""

from __future__ import annotations

import base64
import ctypes
import logging
import os
import struct
import subprocess
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from . import winapi as w

log = logging.getLogger("netclip.win.clipboard")

#: 默认的 OpenClipboard 重试退避（毫秒），总窗口约 0.62s
DEFAULT_OPEN_RETRY_MS: Tuple[int, ...] = (20, 40, 80, 160, 320)

#: 采集重试的退避（毫秒）。
#:
#: 实测（120 次写-读往返，同一台机器）：约 **2.5%** 的次数会出现
#: `OpenClipboard` 成功、`EnumClipboardFormats` 也列出了格式，但紧接着每个
#: `GetClipboardData` 都返回空句柄 —— 重开一次会话就正常了（3 次全部恢复，
#: 其中一次甚至不需要等待）。
#:
#: **不能把它当成"剪贴板没东西"直接放弃**：剪贴板序号此时已经推进，监听器不会
#: 再看第二眼，那一次复制就永远同步不过去了。这正是要替掉 Mouse Without Borders
#: 的那类毛病，所以这里宁可多开几次会话。
CAPTURE_RETRY_MS: Tuple[int, ...] = (0, 20, 50)

#: "格式在表里，但拿不到数据" —— 采集时用它判断这次是不是**瞬时**故障。
_EMPTY_HANDLE = "GetClipboardData 返回空"

#: 这些读不出来的原因意味着"就是不该读/读不了"，重试多少次都一样。
#: 不在这个列表里的原因（最常见的就是 `_EMPTY_HANDLE`）才值得换个会话重读。
_DEFINITIVE_SKIP_REASONS: Tuple[str, ...] = (
    "被配置过滤",
    "超过单格式上限",
    "进程内句柄格式",
)

#: EmptyClipboard 的重试退避（毫秒）。比 OpenClipboard 短得多 ——
#: 一旦 Open 成功我们就已经持有剪贴板，不该长时间占着。
_EMPTY_RETRY_MS: Tuple[int, ...] = (0, 1, 2, 5, 10)

#: 清空+写入**整段重来**的退避（毫秒）。
#:
#: 实测在剪贴板竞争激烈时会出现 `OpenClipboard` 成功、紧接着 `EmptyClipboard`
#: 返回 `ERROR_CLIPBOARD_NOT_OPEN(1418)` 的情况（剪贴板状态被别的进程在中间动过）。
#: 这种情况下继续拿着那个"打开"的句柄毫无意义，只能把 Open+Empty 当作一个整体
#: 重试。总窗口约 0.25s，够用来躲过瞬时竞争，又不会让写剪贴板变得很慢。
_WRITE_RETRY_MS: Tuple[int, ...] = (0, 10, 25, 50, 80, 120)

#: 语义分类：用于按 clipboard.sync_* 开关过滤
CAT_TEXT = "text"
CAT_HTML = "html"
CAT_RTF = "rtf"
CAT_IMAGE = "image"
CAT_FILES = "files"
CAT_OLE = "ole"
CAT_OTHER = "other"

#: 解析后的格式名 -> 类别。`clip_format_name()` 对标准格式返回 "CF_XXX"，
#: 对注册格式返回注册名，两者都要能归类。
_NAME_TO_CATEGORY = {
    "cf_text": CAT_TEXT,
    "cf_oemtext": CAT_TEXT,
    "cf_unicodetext": CAT_TEXT,
    "cf_dib": CAT_IMAGE,
    "cf_dibv5": CAT_IMAGE,
    "cf_bitmap": CAT_IMAGE,
    "cf_tiff": CAT_IMAGE,
    "cf_dspbitmap": CAT_IMAGE,
    "cf_enhmetafile": CAT_IMAGE,
    "cf_metafilepict": CAT_IMAGE,
    "cf_dspenhmetafile": CAT_IMAGE,
    "cf_hdrop": CAT_FILES,
    "cf_palette": CAT_IMAGE,
    "html format": CAT_HTML,
    "html": CAT_HTML,
    "rich text format": CAT_RTF,
    "rtf": CAT_RTF,
    "png": CAT_IMAGE,
    "image/png": CAT_IMAGE,
    "image/bmp": CAT_IMAGE,
    "image/tiff": CAT_IMAGE,
    "image/jpeg": CAT_IMAGE,
    "image/gif": CAT_IMAGE,
    "jfif": CAT_IMAGE,
    "gif": CAT_IMAGE,
    "deviceindependentbitmap": CAT_IMAGE,
    "embed source": CAT_OLE,
    "embedded object": CAT_OLE,
    "object descriptor": CAT_OLE,
    "ole private data": CAT_OLE,
    "link source": CAT_OLE,
    "link source descriptor": CAT_OLE,
    "cf_embeddedobject": CAT_OLE,
    "cf_objectdescriptor": CAT_OLE,
}

_STANDARD_CATEGORIES = {
    w.CF_UNICODETEXT: CAT_TEXT,
    w.CF_TEXT: CAT_TEXT,
    w.CF_OEMTEXT: CAT_TEXT,
    w.CF_DIB: CAT_IMAGE,
    w.CF_DIBV5: CAT_IMAGE,
    w.CF_BITMAP: CAT_IMAGE,
    w.CF_TIFF: CAT_IMAGE,
    w.CF_HDROP: CAT_FILES,
    w.CF_ENHMETAFILE: CAT_IMAGE,
    w.CF_METAFILEPICT: CAT_IMAGE,
    w.CF_DSPENHMETAFILE: CAT_IMAGE,
}

#: 有明确的"保真优先"语义，全格式转发时优先放入
_PRIORITY_HINTS = (
    "powerpoint",
    "mathtype",
    "equation",
    "excel",
    "word",
    "html format",
    "rich text format",
)


def classify_format(fmt: int, name: str) -> str:
    """把一个剪贴板格式归类到语义类别。

    先按数字 ID（标准格式）判断，再按名字判断 —— 因为注册格式的 ID 是运行时
    分配的，两台机器上可能不同，但名字一定相同。
    """
    if fmt in _STANDARD_CATEGORIES:
        return _STANDARD_CATEGORIES[fmt]
    lowered = name.strip().lower()
    if lowered in _NAME_TO_CATEGORY:
        return _NAME_TO_CATEGORY[lowered]
    if "powerpoint" in lowered or "pbrush" in lowered:
        return CAT_IMAGE
    if "mathtype" in lowered or "equation" in lowered or "ole" in lowered:
        return CAT_OLE
    return CAT_OTHER


# --------------------------------------------------------------------- 数据类


@dataclass
class FormatBlob:
    """一个剪贴板格式的内容。

    `name` 是**格式名字符串**而不是数字 ID —— 数字 ID 是运行期分配的，
    两台机器上同一个私有格式的数字通常不同，只有名字稳定。
    """

    name: str
    fmt: int = 0  # 本机采集时的格式号，仅用于日志
    category: str = CAT_OTHER
    data: bytes = b""
    error: str = ""

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass
class ClipboardSnapshot:
    """一次剪贴板读取的完整结果。"""

    sequence: int = 0
    captured_at: float = 0.0
    items: List[FormatBlob] = field(default_factory=list)
    skipped: List[Tuple[str, str]] = field(default_factory=list)  # (格式名, 原因)
    text_preview: str = ""

    def by_name(self, name: str) -> Optional[FormatBlob]:
        lowered = name.lower()
        for item in self.items:
            if item.name.lower() == lowered:
                return item
        return None

    def has(self, category: str) -> bool:
        return any(item.category == category for item in self.items)

    def total_size(self) -> int:
        return sum(item.size for item in self.items)

    @property
    def is_empty(self) -> bool:
        return not self.items

    def describe(self) -> str:
        parts = ["%s(%s,%.1fKB)" % (i.name, i.category, i.size / 1024.0) for i in self.items]
        return "seq=%d 共%d种格式: %s" % (self.sequence, len(self.items), ", ".join(parts) or "空")


# --------------------------------------------------------------------- 打开/关闭


class ClipboardBusy(RuntimeError):
    """重试窗口内始终抢不到剪贴板。"""


class ClipboardSession:
    """`with ClipboardSession(): ...` —— 保证一定 CloseClipboard。

    打开失败按退避重试；用尽仍失败抛 `ClipboardBusy`。

    `owner` 是要登记成**剪贴板所有者**的窗口句柄（0 = 不指定）。

    **为什么值得指定。** 不指定时剪贴板所有者是 NULL，而对端（Office/WPS）的粘贴走
    `OleGetClipboard`，它要跟所有者打交道。真机对照：UU远程 送过来的同一份内容对端
    粘出来是**可编辑对象**，而它的剪贴板**有一个所有者窗口**（GameViewer.exe），
    我们的一直是**无主**。所以这里允许把剪贴板监听线程那个 message-only 窗口登记进去
    ——那个线程本来就在抽消息，正好满足"所有者窗口要能收消息"的要求。
    """

    __slots__ = ("retry_ms", "_opened", "_owner_handle", "owner")

    def __init__(
        self, retry_ms: Tuple[int, ...] = DEFAULT_OPEN_RETRY_MS, owner: int = 0
    ) -> None:
        self.retry_ms = tuple(retry_ms) if retry_ms else DEFAULT_OPEN_RETRY_MS
        self.owner = int(owner or 0)
        self._opened = False
        self._owner_handle = None

    def __enter__(self) -> "ClipboardSession":
        first = True
        for delay in (0,) + self.retry_ms:
            if not first:
                time.sleep(delay / 1000.0)
            first = False
            if w.user32.OpenClipboard(self.owner or None):
                self._opened = True
                # 记住当前 owner：判断"这次写入到底有没有生效"时有用
                self._owner_handle = w.user32.GetClipboardOwner()
                return self
        raise ClipboardBusy("OpenClipboard 重试 %d 次仍被占用" % len(self.retry_ms))

    def __exit__(self, *exc: object) -> None:
        if self._opened:
            w.user32.CloseClipboard()
            self._opened = False


# --------------------------------------------------------------------- 读取


def _read_text(handle: int) -> bytes:
    """读取 CF_UNICODETEXT，返回不含终止符的 UTF-16LE 字节。

    有两个坑要一起处理：

    1. **终止符**：按约定文本以 `\\0\\0` 结尾。我们只去掉第一个终止符，
       而不是从尾部一路砍零 —— 因为 `GlobalAlloc` 会把分配向上取整（通常 256 字节），
       `GlobalSize` 里包含这段填充，从尾部砍会显得"也对"，但逻辑上不严谨。
    2. **NUL 必须成对处理**：绝不做单字节截断，否则会将一个 UTF-16 码元劈成两半。
    """
    ptr = w.kernel32.GlobalLock(handle)
    if not ptr:
        return b""
    try:
        raw = w.ctypes.string_at(ptr, w.global_mem_size(handle))
    finally:
        w.kernel32.GlobalUnlock(handle)

    # 找第一个 (偶对齐的) 双零 = 终止符
    for idx in range(0, len(raw) - 1, 2):
        if raw[idx] == 0 and raw[idx + 1] == 0:
            return raw[:idx]
    return raw


def _read_ansi_text(handle: int) -> bytes:
    """读取 CF_TEXT / CF_OEMTEXT，返回不含终止符的字节。"""
    raw = w.global_mem_bytes(handle)
    end = raw.find(b"\x00")
    return raw if end < 0 else raw[:end]


def _read_hglobal(handle: int, name: str) -> bytes:
    """按格式的长度规则读出一个 HGLOBAL 格式的数据。

    三种规则见 `_LENGTH_RULE` 的说明。把这段逻辑集中在一个函数里，
    是为了让"哪些格式会被裁掉尾部零"这件事可以被审计 —— 分散在三处
    if/else 里时，漏掉一个格式就是一次静默的数据损坏。
    """
    rule = _length_rule(name)
    raw = w.global_mem_bytes(handle)
    if rule == LEN_SELF_DESCRIBING or not raw:
        return raw
    # LEN_TERMINATED 和 LEN_TRAILING_ZEROS 的实际裁剪动作相同（去尾部零），
    # 区别只在于语义：前者是协议的终止符，后者是分配填充。
    return _trim_trailing_zeros(raw)


def _trim_trailing_zeros(raw: bytes) -> bytes:
    """去掉尾部零字节。

    `GlobalSize()` 返回的是**分配大小**（Windows 按 256 字节向上取整），
    所以自己 `GlobalAlloc` 出来的剪贴板数据回读时会多出尾部填充。剪贴板数据的
    尾部填充必然是 0（写入时用的是 GMEM_ZEROINIT），所以去掉尾部零即可。

    例外：DIB / EMF / 图元文件是**自描述**格式，结尾可能真的是零像素，
    必须按各自的长度字段精确切分（见 `_read_bitmap_as_dib` 等），不能走这里。
    """
    end = len(raw)
    while end > 0 and raw[end - 1] == 0:
        end -= 1
    return raw[:end]


def _read_bitmap_as_dib(handle: int) -> bytes:
    """把 HBITMAP 转成 CF_DIB 字节流（BITMAPINFOHEADER + 像素）。

    HBITMAP 是进程内 GDI 句柄，无法直接序列化；而 CF_DIB 的字节布局是可以
    原样跨机传的。所以这里统一转成 DIB。
    """
    bmp = w.BITMAP()
    if not w.gdi32.GetObjectW(handle, w.ctypes.sizeof(bmp), w.ctypes.byref(bmp)):
        raise w.WinApiError(0, "GetObjectW(HBITMAP) 失败")

    if bmp.bmBitsPixel not in (1, 4, 8, 15, 16, 24, 32):
        raise ValueError("不支持的位深: %d" % bmp.bmBitsPixel)

    hdc = w.user32.GetDC(None)
    if not hdc:
        raise w.WinApiError(0, "GetDC 失败")
    try:
        header = w.BITMAPINFOHEADER()
        header.biSize = w.ctypes.sizeof(w.BITMAPINFOHEADER)
        header.biWidth = bmp.bmWidth
        header.biHeight = bmp.bmHeight
        header.biPlanes = 1
        header.biBitCount = bmp.bmBitsPixel
        header.biCompression = 0  # BI_RGB

        # biSizeImage = 0，让 GetDIBits 用 BI_RGB 的默认行对齐算出大小
        w.check(
            w.gdi32.GetDIBits(hdc, handle, 0, bmp.bmHeight, None, w.ctypes.byref(header), 0),
            "GetDIBits(询问大小)",
        )

        stride = ((bmp.bmWidth * bmp.bmBitsPixel + 31) // 32) * 4
        image_size = stride * bmp.bmHeight
        header.biSizeImage = image_size

        palette_bytes = _palette_size(bmp.bmBitsPixel)
        buffer = w.ctypes.create_string_buffer(w.ctypes.sizeof(header) + palette_bytes + image_size)

        got = w.gdi32.GetDIBits(
            hdc,
            handle,
            0,
            bmp.bmHeight,
            w.ctypes.cast(w.ctypes.byref(buffer, w.ctypes.sizeof(header) + palette_bytes), w.ctypes.c_void_p),
            w.ctypes.byref(header),
            0,
        )
        if got == 0:
            raise w.WinApiError(0, "GetDIBits 取像素失败")

        w.ctypes.memmove(buffer, w.ctypes.byref(header), w.ctypes.sizeof(header))
        return buffer.raw
    finally:
        w.user32.ReleaseDC(None, hdc)


def _palette_size(bit_count: int) -> int:
    """BI_RGB 下 DIB 调色板占用的字节数。"""
    if bit_count <= 8:
        return (1 << bit_count) * 4
    return 0


def _read_enhmetafile(handle: int) -> bytes:
    size = w.gdi32.GetEnhMetaFileBits(handle, 0, None)
    if not size:
        raise w.WinApiError(0, "GetEnhMetaFileBits 询问大小失败")
    buf = w.ctypes.create_string_buffer(size)
    got = w.gdi32.GetEnhMetaFileBits(handle, size, buf)
    if not got:
        raise w.WinApiError(0, "GetEnhMetaFileBits 取数据失败")
    return buf.raw[:got]


def _read_metafilepict(handle: int) -> bytes:
    """读取 CF_METAFILEPICT。

    这个格式的数据是 `METAFILEPICT` 结构 + 其 hMF 指向的图元文件。
    我们把结构原样保留（hMF 字段会被对端覆盖重建），后面追加图元文件字节。
    布局: [METAFILEPICT(16 字节)][图元文件字节]
    """
    raw = w.global_mem_bytes(handle)
    if len(raw) < w.ctypes.sizeof(w.METAFILEPICT):
        raise ValueError("CF_METAFILEPICT 数据过短: %d 字节" % len(raw))

    mfp = w.METAFILEPICT.from_buffer_copy(raw)
    if not mfp.hMF:
        # 没有图元文件句柄，结构本身就没意义
        return raw

    size = w.gdi32.GetMetaFileBitsEx(mfp.hMF, 0, None)
    if not size:
        return raw
    buf = w.ctypes.create_string_buffer(size)
    got = w.gdi32.GetMetaFileBitsEx(mfp.hMF, size, buf)
    if not got:
        return raw
    return raw[: w.ctypes.sizeof(w.METAFILEPICT)] + buf.raw[:got]


def read_format(fmt: int, name: str, max_bytes: int) -> FormatBlob:
    """读取一个格式的数据。必须在已打开的剪贴板会话里调用。

    读不出来不抛异常，而是把原因写进 `FormatBlob.error` ——
    单个格式失败不应该让整次同步失败。
    """
    category = classify_format(fmt, name)
    try:
        handle = w.user32.GetClipboardData(fmt)
        if not handle:
            return FormatBlob(name=name, fmt=fmt, category=category, error=_EMPTY_HANDLE)

        if fmt == w.CF_UNICODETEXT:
            data = _read_text(handle)
        elif fmt in (w.CF_TEXT, w.CF_OEMTEXT):
            data = _read_ansi_text(handle)
        elif fmt == w.CF_BITMAP:
            data = _read_bitmap_as_dib(handle)
        elif fmt == w.CF_ENHMETAFILE:
            data = _read_enhmetafile(handle)
        elif fmt == w.CF_METAFILEPICT:
            data = _read_metafilepict(handle)
        elif fmt in (w.CF_OWNERDISPLAY, w.CF_PALETTE):
            return FormatBlob(name=name, fmt=fmt, category=category, error="进程内句柄格式，跳过")
        else:
            data = _read_hglobal(handle, name)

        if len(data) > max_bytes:
            return FormatBlob(
                name=name,
                fmt=fmt,
                category=category,
                error="超过单格式上限 %d 字节（实际 %d）" % (max_bytes, len(data)),
            )
        return FormatBlob(name=name, fmt=fmt, category=category, data=data)
    except (w.WinApiError, OSError, ValueError) as exc:
        return FormatBlob(name=name, fmt=fmt, category=category, error=str(exc))


def enumerate_formats() -> List[Tuple[int, str]]:
    """枚举剪贴板上的所有格式，返回 [(格式号, 格式名)]。必须在已打开的会话里调用。"""
    result: List[Tuple[int, str]] = []
    fmt = 0
    while True:
        fmt = w.user32.EnumClipboardFormats(fmt)
        if not fmt:
            break
        result.append((fmt, w.clip_format_name(fmt)))
    return result


def capture(
    max_per_format: int,
    filter_fn: Optional[Callable[[int, str, str], bool]] = None,
    dedupe_groups: bool = True,
) -> ClipboardSnapshot:
    """读取整个剪贴板。

    `filter_fn(fmt, name, category)` 返回 False 则跳过该格式（记入 skipped）。

    `dedupe_groups=True` 时启用**等价表示去重**：
    对每个等价组（文本 / 位图 / 图元文件）按优先级依次尝试读取，
    只保留**第一个真正读出来的**表示。

    为什么不能简单地"优先用第一个"，而要"读到为止"：
    Windows 上经常出现 `CF_DIBV5` 在格式表里但句柄为空（写入方没真的提供），
    这时如果已经按优先级把 `CF_DIB` 跳过了，结果就是**整张图都没同步**。
    先尝试、再决定，才不会有这个坑。

    **整次采集会重试**（见 `CAPTURE_RETRY_MS`）：剪贴板看起来是空的时候，换个会话
    再读一次。实测（300 次「写-读」往返）约 1% 的次数里，刚写完的剪贴板会被读成
    "一个格式都没有"，紧接着单读一次却有内容 —— 只有"确实读不了"（被过滤、超限、
    进程内句柄）才算确定结果。

    为什么值得为这个小概率多花最多 70ms：空结果是要被丢掉的（序号已经推进，
    监听器不会再看第二眼），那一次复制就永远同步不过去了。而 `capture()`
    只在**剪贴板真的变了**的时候才被调用，不是定时轮询，代价可控。
    """
    snapshot = ClipboardSnapshot(captured_at=time.time())
    for attempt, delay in enumerate(CAPTURE_RETRY_MS):
        if delay:
            time.sleep(delay / 1000.0)
        snapshot, saw_formats = _capture_once(max_per_format, filter_fn, dedupe_groups)

        if snapshot.items:
            break
        if not _worth_retrying(snapshot):
            break  # 是"被过滤 / 超限 / 进程内句柄"这类**确定**的结果，重试没用
        if attempt == len(CAPTURE_RETRY_MS) - 1:
            log.warning(
                "剪贴板读了 %d 次仍是空的（列出格式 %s），按空处理",
                len(CAPTURE_RETRY_MS),
                "有" if saw_formats else "无",
            )
            break

    snapshot.text_preview = _preview(snapshot)
    return snapshot


def _worth_retrying(snapshot: ClipboardSnapshot) -> bool:
    """这次"读不出东西"值得换个会话再试吗。

    只有"每一种格式都因为确定的原因读不了"才不值得 —— 那重试多少次都一样，
    只会白白拖慢每一次采集。
    """
    if not snapshot.skipped:
        return True  # 连格式都没列出来：可能正是瞬时状态
    return not all(
        reason.startswith(_DEFINITIVE_SKIP_REASONS) for _, reason in snapshot.skipped
    )


def _capture_once(
    max_per_format: int,
    filter_fn: Optional[Callable[[int, str, str], bool]],
    dedupe_groups: bool,
) -> "Tuple[ClipboardSnapshot, bool]":
    """读一次剪贴板。返回 `(快照, 剪贴板上是否列出了任何格式)`。"""
    snapshot = ClipboardSnapshot(captured_at=time.time())
    try:
        with ClipboardSession() as session:
            snapshot.sequence = int(w.user32.GetClipboardSequenceNumber())
            available = enumerate_formats()

            # 按等价组优先级排序：高优先级的格式先读，这样 Dedupe 时
            # "先读到的"就是"质量最高的"。
            ordered = sorted(available, key=lambda pair: group_rank(pair[1])) if dedupe_groups else available
            satisfied_groups: Dict[int, str] = {}

            #: **读取顺序和输出顺序是两回事。**
            #:
            #: 上面那个排序只是为了决定"同一组等价表示里留哪一个"；而 `items` 的**顺序**
            #: 会被接收端原样照搬去写剪贴板 —— 也就是说，**排序会泄漏成接收端的格式
            #: 枚举顺序**。消费者是照枚举顺序挑格式的，我们没理由替它重排。
            #:
            #: 真机证据（同一个剪贴板，两台各跑一次 `tools/clip_probe.py`）::
            #:
            #:     发送端: DataObject > Kingsoft Data Descriptor > Kingsoft WPS 9.0 Format > …
            #:     接收端: CF_UNICODETEXT > CF_ENHMETAFILE > DataObject > Kingsoft Data Descriptor > …
            #:             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^ 被我们提到了最前面
            #:
            #: 所以这里按**原始枚举位置**收结果，读完再摆回去。
            position = {fmt: index for index, (fmt, _name) in enumerate(available)}
            picked: "Dict[int, FormatBlob]" = {}

            for fmt, name in ordered:
                category = classify_format(fmt, name)
                group_index: Optional[int] = None

                if dedupe_groups:
                    rank = _GROUP_RANK.get(name)
                    if rank is not None:
                        group_index = rank[0]
                        winner = satisfied_groups.get(group_index)
                        if winner is not None:
                            snapshot.skipped.append((name, "等价表示去重（已有 %s）" % winner))
                            continue

                if filter_fn is not None and not filter_fn(fmt, name, category):
                    snapshot.skipped.append((name, "被配置过滤"))
                    continue

                blob = read_format(fmt, name, max_per_format)
                if blob.error:
                    # 读不出来：不记入 items，但也不阻塞组内其它表示
                    snapshot.skipped.append((name, blob.error))
                    continue

                if dedupe_groups and group_index is not None:
                    satisfied_groups[group_index] = name
                picked[position.get(fmt, len(picked))] = blob

            #: 按剪贴板上的**原始枚举顺序**输出（顺序本身就是被转发的一部分）。
            snapshot.items = [picked[key] for key in sorted(picked)]
    except ClipboardBusy as exc:
        snapshot.skipped.append(("<剪贴板>", str(exc)))
        return snapshot, False

    return snapshot, bool(available)


def _preview(snapshot: ClipboardSnapshot, limit: int = 80) -> str:
    for item in snapshot.items:
        if item.name == "CF_UNICODETEXT" and item.data:
            text = item.data.decode("utf-16-le", errors="replace")
            text = text.replace("\r\n", "⏎").replace("\n", "⏎")
            return text[:limit] + ("…" if len(text) > limit else "")
    if snapshot.has(CAT_FILES):
        return "<文件>"
    if snapshot.has(CAT_IMAGE):
        return "<图片>"
    return ""


# --------------------------------------------------------------------- 写入


class ClipboardWriteResult:
    __slots__ = ("written", "failed", "sequence")

    def __init__(self, written: List[str], failed: List[Tuple[str, str]], sequence: int) -> None:
        self.written = written
        self.failed = failed
        self.sequence = sequence

    def __bool__(self) -> bool:
        return bool(self.written)

    def describe(self) -> str:
        text = "写入 %d 种格式" % len(self.written)
        if self.failed:
            text += "，失败 %d 种: %s" % (len(self.failed), "; ".join("%s(%s)" % f for f in self.failed))
        return text


def write_formats(
    items: List[FormatBlob],
    open_retry_ms: Tuple[int, ...] = DEFAULT_OPEN_RETRY_MS,
    owner: int = 0,
) -> ClipboardWriteResult:
    """把一组格式写入剪贴板。

    `owner` 是登记成**剪贴板所有者**的窗口句柄（0 = 不指定）——
    见 `ClipboardSession` 的说明：无主剪贴板和对端 OLE 的粘贴有关系。

    顺序很重要：`CF_EMBEDDEDOBJECT` 必须在最后写，否则 Office 认不出嵌入对象。
    调用方负责排好序（见 `order_for_paste`）。

    **为什么要"整段重来"而不是"只重试 EmptyClipboard"**：
    实测在剪贴板竞争激烈时会出现 `OpenClipboard` 成功、紧接着 `EmptyClipboard`
    返回 `ERROR_CLIPBOARD_NOT_OPEN(1418)` 的情况（Windows 的剪贴板状态被别的
    进程在中间动过）。这种情况下继续拿着那个"打开"的句柄没有意义 ——
    必须把 Open+Empty 作为一个整体重试，才能拿到一次干净的一致状态。

    同理，`EmptyClipboard` 成功、但每一种格式的 `SetClipboardData` 都被抢掉时，
    也要整段重来：那时候剪贴板**已经被我们清空了**，就此收手等于把用户原来的
    内容删掉还什么都没换上。只有"数据本身有问题"（`ValueError`）才算确定性失败。
    """
    written: List[str] = []
    failed: List[Tuple[str, str]] = []
    sequence = 0
    last_err = 0

    # 关键：**清空和写入必须在同一个剪贴板会话里完成**。
    # 如果清空后先把剪贴板关掉再重新打开，中间那一瞬间别的进程可以插进来，
    # 结果就是我们清空的成果被抢走、写进去的东西又混着别人的内容。
    for attempt, delay_ms in enumerate(_WRITE_RETRY_MS):
        if delay_ms:
            time.sleep(delay_ms / 1000.0)

        written = []
        failed = []
        try:
            with ClipboardSession(open_retry_ms, owner=owner):
                if not w.user32.EmptyClipboard():
                    last_err = w.last_error()
                    if _DIAG_EMPTY:
                        import threading

                        log.warning(
                            "DIAG EmptyClipboard 失败 attempt=%d thread=%s err=%d",
                            attempt,
                            threading.current_thread().name,
                            last_err,
                        )
                    # 拿到了锁却清不掉：整段重来，拿一次干净的一致状态
                    continue

                deterministic = False
                for item in items:
                    try:
                        _set_one(item)
                        written.append(item.name)
                    except ValueError as exc:
                        # 数据本身有问题（空数据之类）：重试多少次都一样
                        failed.append((item.name, str(exc)))
                        deterministic = True
                    except (w.WinApiError, OSError) as exc:
                        # 单个格式失败不影响其它格式：丢那一个就好。
                        # 但如果**一个都没写进去**，下面会整段重来。
                        failed.append((item.name, str(exc)))
                sequence = int(w.user32.GetClipboardSequenceNumber())

                if written or deterministic or not items:
                    break
                # 清空成功却颗粒无收 —— 剪贴板现在是空的，必须再试，
                # 不能就这么把用户原来的内容丢掉
                continue
        except ClipboardBusy:
            last_err = 0
            continue  # 抢不到锁，下一轮再来
    else:
        raise w.WinApiError(
            last_err,
            "写入剪贴板连续失败 %d 次（剪贴板被抢占或 OpenClipboard 拿不到锁）" % len(_WRITE_RETRY_MS),
        )

    if not written:
        log.warning("剪贴板写入全部失败: %s", failed)
    elif failed:
        log.info("剪贴板部分写入成功: %s；失败: %s", written, failed)

    return ClipboardWriteResult(written, failed, sequence)


#: 同内容的多种等价表示，按**质量/保真度**从高到低排列。
#:
#: Windows 经常同时提供多种等价表示（写入 CF_UNICODETEXT 时会顺带生成
#: CF_TEXT / CF_OEMTEXT）。全部同步过去没有意义，还会让对端程序挑到有损的那个
#: —— ANSI 版本会丢非 ASCII 字符。
#:
#: 但"优先用第一个"不能写死：有时高优先级的那个其实读不出来（返回空句柄），
#: 这时必须回退到下一个，否则会出现"明明有 DIB 却因为 DIBV5 空而整张图都没同步"。
EQUIVALENT_GROUPS: "tuple[tuple[str, ...], ...]" = (
    ("CF_UNICODETEXT", "CF_TEXT", "CF_OEMTEXT"),
    ("CF_DIBV5", "CF_DIB", "CF_BITMAP"),
    ("CF_ENHMETAFILE", "CF_METAFILEPICT"),
)

#: 与具体机器绑定的格式，跨机传过去没有意义（CF_LOCALE 里的 LCID 只在本机有效）
MACHINE_LOCAL_FORMATS = frozenset({"CF_LOCALE", "CF_PALETTE", "CF_OWNERDISPLAY", "CF_DSPBITMAP"})

#: 内容里**固化了文件绝对路径 / Shell 项目标识**的格式。
#:
#: 这些格式在跨机复制文件时是**有害**的，必须在对端落地时丢掉：
#: Explorer 粘贴时按保真度挑格式，`Shell IDList Array` 的优先级**高于** `CF_HDROP`。
#: 它内部装的是源机器上的绝对 PIDL，于是对端会照着**源机器的路径**去找文件。
#:
#: 真机上就踩到了：从机复制 `C:\Users\zxy\Documents\Python\Temp\netclip\start.ps1`，
#: 主机粘贴时提示"在 temp 文件夹中找不到文件" —— 那个文件夹正好叫 Temp，
#: 而主机上根本没有它。文件其实已经传过来并放在暂存区了，只是没人去看 CF_HDROP。
PATH_BEARING_FORMATS = frozenset(
    {
        "CF_HDROP",
        "FileDrop",
        "Shell IDList Array",
        "Shell Object Offsets",
        "Shell Objects Offsets",
        "FileName",
        "FileNameW",
    }
)

#: 采集阶段就**不发**的那些"带路径"格式。
#:
#: 比 `PATH_BEARING_FORMATS` 少了 `CF_HDROP` / `FileDrop`，这是刻意的：
#: 帧里带上 `CF_HDROP` 是接收端判断"这是一次文件复制"的**唯一信号**
#: （它自己会把内容丢掉，只用本地路径重建）。真机上漏了这个信号之后，
#: 剪贴板帧到达时接收端不知道要保留文件，把刚写好的 `CF_HDROP` 抹掉了 ——
#: 现象就是"文件传过来了但粘贴不了"。
SHELL_PATH_FORMATS = PATH_BEARING_FORMATS - {"CF_HDROP", "FileDrop"}

#: OLE「**虚拟文件**」协议的格式族。
#:
#: `FileGroupDescriptor` 只**描述**文件，真正的字节要靠 `FileContents` 在
#: **粘贴的那一刻**由源数据对象现场渲染。所以它对跨机转发毫无意义 ——
#: 源数据对象在对端根本不存在。留下的是一份"有描述、没内容"的剪贴板：
#: 资源管理器照着描述去要文件流，什么都拿不到，于是**转沙漏空等、什么都不粘**。
#:
#: 真机证据（主机 `--dump-formats`，同一个文件、同一次传输）：
#:
#:   失败（软件复制）:
#:     CF_HDROP(路径存在) + **FileGroupDescriptorW** + Preferred DropEffect=COPY
#:   成功（主机上手动复制同一个文件）:
#:     CF_HDROP(路径存在) + DataObject + Ole Private Data + FileName + FileNameW
#:
#: 两边只差这一个格式 —— 这就是"沙漏一转啥也没粘出来"的来源。
VIRTUAL_FILE_FORMATS = frozenset({"FileGroupDescriptor", "FileGroupDescriptorW", "FileContents"})

#: **发送端**绝不出本机的 Shell 格式：带对端绝对路径的 + 虚拟文件协议族的。
#: `CF_HDROP` 刻意不在里面 —— 它是"这是一次文件复制"的信号，必须发。
UNSENDABLE_SHELL_FORMATS = SHELL_PATH_FORMATS | VIRTUAL_FILE_FORMATS

#: 格式名 -> (组序号, 组内优先级)。用于把枚举结果排出读取顺序。
_GROUP_RANK: "Dict[str, tuple]" = {}
for _gi, _group in enumerate(EQUIVALENT_GROUPS):
    for _pi, _nm in enumerate(_group):
        _GROUP_RANK[_nm] = (_gi, _pi)
del _gi, _group, _pi, _nm


def group_members(name: str) -> "tuple[str, ...]":
    """返回该格式所在等价组的成员；不在任何组里就返回只含自己的元组。"""
    rank = _GROUP_RANK.get(name)
    return EQUIVALENT_GROUPS[rank[0]] if rank is not None else (name,)


def group_rank(name: str) -> "tuple":
    """排序键：组内优先级越高越先被读取。不在组里的格式排最后。"""
    return _GROUP_RANK.get(name, (len(EQUIVALENT_GROUPS), 0))


def collect_filter_skip(fmt: int, name: str) -> bool:
    """采集阶段的"必然不要"判定。

    只拦两类，不承担完整策略（策略要等知道每个格式多大才能判断）：

      * 进程内句柄格式（HBITMAP / HPALETTE / CF_OWNERDISPLAY 之类）；
      * 与具体机器绑定的格式。

    **等价组的高优先级格式"优先"不在这里判断** —— 那需要先知道哪个真的能读出来，
    见 `capture(..., dedupe_groups=True)`。
    """
    if fmt in (w.CF_OWNERDISPLAY, w.CF_PALETTE):
        return True
    if name in MACHINE_LOCAL_FORMATS:
        return True
    #: 固化了对端绝对路径的 Shell 格式一律不发：发过去只会让对端照着无效路径找文件。
    #: 虚拟文件协议族（`FileGroupDescriptorW` 等）同样不发 —— 它描述的"文件内容"
    #: 只存在于本机的数据对象里，对端拿到的是一个空壳。
    #: **但 `CF_HDROP` 要发** —— 它是"这是一次文件复制"的信号，接收端靠它决定
    #: 要不要保留本地文件剪贴板（见 `SHELL_PATH_FORMATS` 与 `clipsync.bridge._apply_remote`）。
    if name in UNSENDABLE_SHELL_FORMATS:
        return True
    #: 拖放簿记格式只对"源和目标在同一次拖放会话里"有意义，跨机转发会让对端的
    #: 资源管理器误判状态：`AsyncFlag` 会让它转沙漏空等，`DropDescription`
    #: 被截断后按完整长度读会越界崩溃。接收端落地时自己写 `DropEffect=COPY`。
    if name in SHELL_BOOKKEEPING_FORMATS:
        return True
    return False


#: 长度判定规则。决定一个 HGLOBAL 格式回读时该怎么确定"数据到哪里结束"。
#:
#: 这是剪贴板里最容易出错的一环：`GlobalSize()` 返回的是**分配大小**
#: （Windows 会向上取整），所以回读时总会多出一些填充字节。怎么剔除它，
#: 取决于格式本身是不是自描述的、以及尾部零是不是有意义的数据。
LEN_TERMINATED = "terminated"  # 按终止符截断（文本类）
LEN_SELF_DESCRIBING = "self"  # 自身带长度字段，绝不裁剪尾部零
LEN_TRAILING_ZEROS = "zeros"  # 无长度信息，只能去掉尾部零字节

_LENGTH_RULE: Dict[str, str] = {
    "CF_UNICODETEXT": LEN_TERMINATED,
    "CF_TEXT": LEN_TERMINATED,
    "CF_OEMTEXT": LEN_TERMINATED,
    # 下面这些都自带结构/长度信息，尾部零可能是**真实数据**（例如 DIB 的最后一个
    # 黑色像素）。对它们做"去尾部零"会静默损坏数据 —— CF_HDROP 尤其明显：
    # 去掉末尾的 \0\0 会让最后一个文件名少一个字符。
    "CF_HDROP": LEN_SELF_DESCRIBING,
    "CF_DIB": LEN_SELF_DESCRIBING,
    "CF_DIBV5": LEN_SELF_DESCRIBING,
    "CF_ENHMETAFILE": LEN_SELF_DESCRIBING,
    "CF_METAFILEPICT": LEN_SELF_DESCRIBING,
    "CF_WAVE": LEN_SELF_DESCRIBING,
    "CF_RIFF": LEN_SELF_DESCRIBING,
    "CF_SYLK": LEN_SELF_DESCRIBING,
    "CF_LOCALE": LEN_SELF_DESCRIBING,
    "CF_TIFF": LEN_SELF_DESCRIBING,
    #: Shell 的文件类格式。**这一条是真机上"文件能传过来但粘贴不了"的根因。**
    #:
    #: `Preferred DropEffect` 是个 **4 字节 DWORD**（DROPEFFECT_COPY=1 →
    #: `01 00 00 00`）。默认规则会"去掉尾部零字节"，于是三个零被当成填充删掉，
    #: 转发出去只剩 `01` 一个字节。Explorer 是按 4 字节 DWORD 读的 ——
    #: 多读的 3 字节取决于堆内存内容，只要拼出来的值不是 1，Shell 就认为
    #: "这不是一次复制"，按 Ctrl+V **既不报错也不粘贴**（用户看到的就是
    #: "按钮能点、转一下沙漏、什么都没发生"）。
    #:
    #: 凡是**定长结构**都要列在这里，尾部零是真实数据。
    "Preferred DropEffect": LEN_SELF_DESCRIBING,
    "Performed DropEffect": LEN_SELF_DESCRIBING,
    "Logical Performed DropEffect": LEN_SELF_DESCRIBING,
    "Paste Succeeded": LEN_SELF_DESCRIBING,
    "AsyncFlag": LEN_SELF_DESCRIBING,
    "DataObjectAttributes": LEN_SELF_DESCRIBING,
    "DataObjectAttributesRequiringElevation": LEN_SELF_DESCRIBING,
    "InShellDragLoop": LEN_SELF_DESCRIBING,
    "UntrustedDragDrop": LEN_SELF_DESCRIBING,
}

#: 默认规则：对于不认识的注册格式，去掉尾部零是最好的猜测 ——
#: 现代应用的私有格式多数是「文本 / UTF-8 / JSON / 自带长度的二进制」，
#: 尾部零通常是 `GlobalAlloc` 的填充。但这个猜测并不总是成立，
#: 所以 `clipboard.formats.exclude` 里可以把这个格式排除掉。
_LENGTH_RULE_DEFAULT = LEN_TRAILING_ZEROS


def _length_rule(name: str) -> str:
    return _LENGTH_RULE.get(name, _LENGTH_RULE_DEFAULT)


#: 标准格式名 -> 格式常量。
#:
#: **这是一个必须显式映射的表，不能靠 `RegisterClipboardFormatW` 反推。**
#: 后者对 "CF_UNICODETEXT" 这样的字符串会返回一个运行期分配的 ID（如 49273），
#: 而不是 13。用错 ID 的后果很隐蔽：数据确实写进了剪贴板，但别的程序按
#: CF_UNICODETEXT(13) 去找时找不到，表现为"复制了但粘贴是空的"。
_STANDARD_BY_NAME: Dict[str, int] = {
    "CF_TEXT": w.CF_TEXT,
    "CF_BITMAP": w.CF_BITMAP,
    "CF_METAFILEPICT": w.CF_METAFILEPICT,
    "CF_SYLK": w.CF_SYLK,
    "CF_DIF": w.CF_DIF,
    "CF_TIFF": w.CF_TIFF,
    "CF_OEMTEXT": w.CF_OEMTEXT,
    "CF_DIB": w.CF_DIB,
    "CF_PALETTE": w.CF_PALETTE,
    "CF_PENDATA": w.CF_PENDATA,
    "CF_RIFF": w.CF_RIFF,
    "CF_WAVE": w.CF_WAVE,
    "CF_UNICODETEXT": w.CF_UNICODETEXT,
    "CF_ENHMETAFILE": w.CF_ENHMETAFILE,
    "CF_HDROP": w.CF_HDROP,
    "CF_LOCALE": w.CF_LOCALE,
    "CF_DIBV5": w.CF_DIBV5,
    "CF_DSPTEXT": w.CF_DSPTEXT,
    "CF_DSPBITMAP": w.CF_DSPBITMAP,
    "CF_DSPMETAFILEPICT": w.CF_DSPMETAFILEPICT,
    "CF_DSPENHMETAFILE": w.CF_DSPENHMETAFILE,
}

_TEXT_FORMAT_NAMES = frozenset({"CF_UNICODETEXT", "CF_TEXT", "CF_OEMTEXT"})

#: 诊断开关：置 True 时会打印 EmptyClipboard 每次失败的线程/错误码。
#: 默认关闭（热路径不该刷日志），排查剪贴板竞争问题时在调试器里打开。
_DIAG_EMPTY = False


def format_id_for(name: str) -> int:
    """把格式名解析成本机可用的格式号。

    标准格式查表；私有/注册格式交给 `RegisterClipboardFormatW` 按名字注册 ——
    这一步是跨机私有格式能工作的关键：格式号是运行期分配的，两台机器上可能不同，
    但**名字是稳定的**，所以协议里一律传名字。
    """
    standard = _STANDARD_BY_NAME.get(name)
    if standard is not None:
        return standard
    return w.register_clip_format(name)


def _set_one(item: FormatBlob) -> None:
    """写入单个格式。数据为空则视为失败。"""
    fmt = format_id_for(item.name)
    data = item.data

    if item.name == "CF_BITMAP":
        # 以 CF_BITMAP 名字传输时，data 里装的是 DIB 字节，重建 HBITMAP
        hbmp = _dib_to_bitmap(data)
        try:
            if not w.user32.SetClipboardData(fmt, hbmp):
                raise w.WinApiError(w.last_error(), "SetClipboardData(CF_BITMAP)")
        except Exception:
            w.gdi32.DeleteObject(hbmp)
            raise
        return

    if item.name == "CF_ENHMETAFILE":
        handle = _bytes_to_enhmetafile(data)
        try:
            if not w.user32.SetClipboardData(fmt, handle):
                raise w.WinApiError(w.last_error(), "SetClipboardData(CF_ENHMETAFILE)")
        except Exception:
            w.gdi32.DeleteEnhMetaFile(handle)
            raise
        return

    if item.name == "CF_METAFILEPICT":
        _set_metafilepict(fmt, data)
        return

    if not data:
        raise ValueError("数据为空")

    # 文本类格式按约定需要以 NUL 结尾（Unicode 是 2 字节 0，ANSI 是 1 字节 0）。
    # 采集时我们已经把终止符去掉了，这里补回来。
    pad = 0
    if item.name in _TEXT_FORMAT_NAMES:
        pad = 2 if item.name == "CF_UNICODETEXT" else 1
        data = data + b"\x00" * pad

    # extra_pad 必须是 0：`GlobalAlloc` 会把块大小向上取整（通常到 8 的倍数），
    # 而 `GlobalSize()` 返回的是取整后的大小，所以自描述格式回读时一定带填充。
    # 再额外补字节只会让填充更难判断，没有好处。
    mem = w.GlobalMem.from_bytes(data, extra_pad=0)
    handle = mem.release_to_clipboard()
    if not w.user32.SetClipboardData(fmt, handle):
        err = w.last_error()
        # 失败时所有权仍在我们手里，必须释放
        w.kernel32.GlobalFree(handle)
        raise w.WinApiError(err, "SetClipboardData(%s)" % item.name)


def _dib_to_bitmap(dib: bytes) -> int:
    """CF_DIB 字节 -> HBITMAP。"""
    if len(dib) < w.ctypes.sizeof(w.BITMAPINFOHEADER):
        raise ValueError("DIB 数据过短")
    header = w.BITMAPINFOHEADER.from_buffer_copy(dib[: w.ctypes.sizeof(w.BITMAPINFOHEADER)])
    header_size = int(header.biSize) or w.ctypes.sizeof(w.BITMAPINFOHEADER)
    if header_size > len(dib):
        raise ValueError("DIB 头长度非法: %d" % header_size)

    hdc = w.user32.GetDC(None)
    if not hdc:
        raise w.WinApiError(0, "GetDC 失败")
    try:
        palette = _palette_size(header.biBitCount)
        offset = header_size + palette
        if offset > len(dib):
            raise ValueError("DIB 调色板越界")
        bits = dib[offset:]
        hbmp = w.gdi32.CreateDIBitmap(
            hdc,
            w.ctypes.byref(header),
            4,  # CBM_INIT
            w.ctypes.c_char_p(bits) if bits else None,
            w.ctypes.byref(header),
            0,  # DIB_RGB_COLORS
        )
        if not hbmp:
            raise w.WinApiError(0, "CreateDIBitmap 失败")
        return hbmp
    finally:
        w.user32.ReleaseDC(None, hdc)


def _bytes_to_enhmetafile(data: bytes) -> int:
    if not data:
        raise ValueError("EMF 数据为空")
    handle = w.gdi32.SetEnhMetaFileBits(len(data), w.ctypes.c_char_p(data))
    if not handle:
        raise w.WinApiError(0, "SetEnhMetaFileBits 失败")
    return handle


def _set_metafilepict(fmt: int, data: bytes) -> None:
    """还原 CF_METAFILEPICT：把结构里的 hMF 换成新重建的图元文件句柄。"""
    size = w.ctypes.sizeof(w.METAFILEPICT)
    if len(data) < size:
        raise ValueError("CF_METAFILEPICT 数据过短")
    body = data[size:]
    if not body:
        raise ValueError("CF_METAFILEPICT 缺少图元文件数据")

    hmf = w.gdi32.SetMetaFileBitsEx(len(body), w.ctypes.c_char_p(body))
    if not hmf:
        raise w.WinApiError(0, "SetMetaFileBitsEx 失败")

    mem = w.GlobalMem(size, zero_init=True)
    mem.write(0, data[:size])
    # 覆盖 hMF 字段为重建出来的句柄
    offset = w.METAFILEPICT.hMF.offset
    w.ctypes.memmove(mem.ptr + offset, w.ctypes.byref(w.ctypes.c_void_p(hmf)), w.ctypes.sizeof(w.ctypes.c_void_p))

    handle = mem.release_to_clipboard()
    if not w.user32.SetClipboardData(fmt, handle):
        err = w.last_error()
        w.kernel32.GlobalFree(handle)
        w.gdi32.DeleteMetaFile(hmf)
        raise w.WinApiError(err, "SetClipboardData(CF_METAFILEPICT)")


def order_for_paste(items: List[FormatBlob], priority: List[str]) -> List[FormatBlob]:
    """按配置的优先级排序，保证 CF_EMBEDDEDOBJECT 最后写入。

    Office 对剪贴板的解析依赖写入顺序：先有数据表示（EMF/DIB）和
    CF_OBJECTDESCRIPTOR，最后才是 CF_EMBEDDEDOBJECT。
    """
    def rank(item: FormatBlob) -> Tuple[int, int]:
        lowered = item.name.lower()
        for idx, name in enumerate(priority):
            if name.lower() == lowered:
                return (0, idx)
        return (1, 0)

    ordered = sorted(items, key=rank)
    embedded = [i for i in ordered if i.name.lower() == "cf_embeddedobject"]
    rest = [i for i in ordered if i.name.lower() != "cf_embeddedobject"]
    return rest + embedded


def clear_clipboard(retry_ms: Tuple[int, ...] = DEFAULT_OPEN_RETRY_MS) -> bool:
    """清空剪贴板。失败返回 False（不抛异常，供看门狗等非关键路径使用）。"""
    try:
        with ClipboardSession(retry_ms):
            return bool(w.user32.EmptyClipboard())
    except ClipboardBusy:
        return False


def clipboard_owner_process() -> str:
    """剪贴板上这份内容**是哪个进程放的**（可执行文件名，小写）。取不到返回空串。

    必须在**剪贴板已打开**的会话里意义才准确，所以调用方一般在 `capture()` 之前、
    或紧跟着读一次。真机用途见 `config.example.toml` 的 `exclude_by_process`：

    同一个 `Ole Private Data` 在 WPS 演示（`wpp.exe`）和 Word（`winword.exe`）上
    要求**相反**，而"谁放的"是直接可观测的，不需要猜格式。
    """
    try:
        hwnd = w.user32.GetClipboardOwner()
        if not hwnd:
            #: 无主时退回"正持有剪贴板的那个窗口"——我们自己写完之后就是这种状态，
            #: 而那时我们要知道的恰恰是"我们没抢到谁的内容"。
            return ""
        pid = wintypes.DWORD(0)
        w.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = w.kernel32.OpenProcess(w.PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if not w.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return ""
            return os.path.basename(buf.value).lower()
        finally:
            w.kernel32.CloseHandle(handle)
    except Exception:  # pragma: no cover - 拿不到就当"不知道来源"，退回默认排除表
        log.debug("取剪贴板所有者进程失败", exc_info=True)
        return ""


# --------------------------------------------------------------------- OLE 收尾


#: 已经 `OleInitialize` 过的线程（每个线程只需一次）。
_OLE_READY: "set" = set()
_OLE_LOCK = threading.Lock()


def _ole_initialize() -> bool:
    """在当前线程上初始化 OLE（幂等）。`OleInitialize` 必须在用任何 OLE 函数之前调。"""
    key = threading.get_ident()
    with _OLE_LOCK:
        if key in _OLE_READY:
            return True
    #: S_OK=0、S_FALSE=1（已经初始化过）都算成功
    if w.ole32.OleInitialize(None) in (0, 1):
        with _OLE_LOCK:
            _OLE_READY.add(key)
        return True
    return False


def _release_com(ptr) -> None:
    """`IUnknown::Release`（vtable 第 3 个槽）。不实现任何接口，只是把引用还回去。"""
    try:
        vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])(ptr)
    except Exception:  # pragma: no cover - 释放失败只是泄漏一个引用，不该影响功能
        pass


def _clipboard_format_names() -> "set":
    try:
        with ClipboardSession():
            return {name for _fmt, name in enumerate_formats()}
    except ClipboardBusy:
        return set()


def bless_clipboard_with_ole() -> bool:
    """让 **OLE 正式接管**当前剪贴板。

    为什么可能有用（真机 A/B 的推论，尚未定论）
    -------------------------------------------
    裸 `SetClipboardData` 写出来的剪贴板**没有 OLE 数据对象的身份** —— 用
    `OpenClipboard(None)` 写的话，剪贴板所有者是 NULL，而 Office/WPS 的粘贴走的是
    `OleGetClipboard`。对照组：UU远程（GameViewer.exe）送过来的同一份内容
    **接收端粘出来是可编辑对象**，而它的剪贴板**有所有者窗口**、**没有**
    `Ole Private Data`。

    这里不做任何 COM 实现：`OleGetClipboard` 拿到 OLE 对当前剪贴板的封装，
    再 `OleSetClipboard` 交还给它，由 OLE 生成属于**本机**的私有数据 ——
    而不是把源机器的封送引用（悬空引用）原样搬过来。

    **必须自检，因为它可能把剪贴板清空。** 实测（本机，干净流程）出现过::

        OleSetClipboard = CLIPBRD_E_CANT_CLOSE
        之后剪贴板里只剩下 ['DataObject']  —— 我们写的内容全没了

    所以接管之后要**核对格式有没有变少**；少了就返回 False，由调用方把原内容重写回去。
    宁可不要这个改善，也绝不能把用户的剪贴板弄丢。
    """
    before = _clipboard_format_names()
    if not before:
        return False
    if not _ole_initialize():
        log.debug("OleInitialize 失败，不做 OLE 接管")
        return False

    ptr = ctypes.c_void_p()
    try:
        if w.ole32.OleGetClipboard(ctypes.byref(ptr)) != 0 or not ptr:
            log.debug("OleGetClipboard 失败，不做 OLE 接管")
            return False
        if w.ole32.OleSetClipboard(ptr) != 0:
            log.debug("OleSetClipboard 失败")
            return False
        if w.ole32.OleFlushClipboard() != 0:
            log.debug("OleFlushClipboard 失败")
            return False
    except Exception:  # pragma: no cover - OLE 出问题不能影响同步
        log.debug("OLE 接管异常", exc_info=True)
        return False
    finally:
        _release_com(ptr)

    after = _clipboard_format_names()
    lost = before - after
    if lost:
        log.warning("OLE 接管后少了 %d 种格式（%s）—— 判定失败", len(lost), sorted(lost)[:4])
        return False
    log.debug("OLE 已接管剪贴板（%d 种格式，一种没少）", len(after))
    return True


# --------------------------------------------------------------------- CF_HDROP


def build_hdrop(paths: Sequence[str], wide: bool = True) -> bytes:
    """构造 `CF_HDROP` 的字节流，让一组本地文件能被粘贴。

    这是"把远端传来的文件放回剪贴板"的关键一步：Windows 的 `CF_HDROP`
    只是一个**路径列表**，资源管理器在粘贴时才去读文件，所以：

      * 路径必须是接收端**真实存在**的本地路径；
      * 这些文件不能在粘贴前被删掉（见 `Staging` 的 TTL 设计）。

    布局::

        DROPFILES{pFiles=20, pt=(0,0), fNC=FALSE, fWide=TRUE}
        "C:\\a.txt\\0C:\\b.txt\\0\\0"    （UTF-16LE，双 \\0 结束）

    `fWide` 必须写成 4 字节的 BOOL；写成 1 字节会让所有解析方（包括
    资源管理器和我们自己的 `parse_hdrop`）算错偏移。
    """
    import struct

    if not paths:
        raise ValueError("paths 不能为空")
    if wide:
        payload = b"".join(str(p).encode("utf-16-le") + b"\x00\x00" for p in paths) + b"\x00\x00"
    else:
        payload = b"".join(str(p).encode("mbcs") + b"\x00" for p in paths) + b"\x00"
    header = struct.pack("<IiiII", _DROPFILES_HEADER, 0, 0, 0, 1 if wide else 0)
    return header + payload


#: DROPFILES 结构的字节长度（DWORD + POINT(8) + BOOL + BOOL）
_DROPFILES_HEADER = 20


def make_hdrop_blob(paths: Sequence[str]) -> "FormatBlob":
    """便捷函数：把路径列表包成可以 `write_formats` 的 FormatBlob。"""
    return FormatBlob(name="CF_HDROP", category=CAT_FILES, data=build_hdrop(paths))


#: 文件剪贴板必备的格式名
HDROP_FORMAT = "CF_HDROP"
PREFERRED_DROPEFFECT_FORMAT = "Preferred DropEffect"
#: `DROPEFFECT_COPY`。文件剪贴板必须**显式**声明是"复制"，
#: 缺了它或值不对时 Shell 会拒绝执行粘贴（表现为按 Ctrl+V 毫无反应）。
DROPEFFECT_COPY = 1

#: 拖放/粘贴的**簿记格式**：它们只对"源和目标的同一次拖放会话"有意义，
#: 跨机转发过去只会让对端的资源管理器误判自己的状态。
#:
#: **绝不能让它们跟着文件走**（真机踩过）：
#:   * `AsyncFlag` 被截断成 1 字节却按 DWORD 读 -> Shell 以为这是一次异步传递，
#:     于是**转沙漏空等**，什么都不粘贴；
#:   * `DropDescription` 是定长结构（含两个 MAX_PATH 宽字符串），截断后
#:     资源管理器按完整长度读会**越界访问，直接崩掉重启**。
#: 文件落地时我们自己写一份干净的 `Preferred DropEffect=COPY` 就够了。
SHELL_BOOKKEEPING_FORMATS = frozenset(
    {
        "Preferred DropEffect",
        "Performed DropEffect",
        "Logical Performed DropEffect",
        "Paste Succeeded",
        "AsyncFlag",
        "InShellDragLoop",
        "UntrustedDragDrop",
        "DataObjectAttributes",
        "DataObjectAttributesRequiringElevation",
        "DropDescription",
        "UIDisplayed",
        "DragWindow",
        "TargetCLSID",
    }
)


#: **接收端绝不写进本机剪贴板的格式集合。**
#:
#: 三种情况，任何一种写进去都会让 Explorer 的粘贴出问题：
#:
#:   * `PATH_BEARING_FORMATS` —— 里面装着**对端**的绝对路径，本机照着找必然找不到；
#:   * `SHELL_BOOKKEEPING_FORMATS` —— 拖放簿记，跨机会让 Shell 误判状态
#:     （`AsyncFlag` 转沙漏空等、`DropDescription` 截断后越界崩溃）；
#:   * `VIRTUAL_FILE_FORMATS` —— 虚拟文件描述，有描述没内容。
#:
#: 发送端和接收端必须**用同一份集合**。这两处一度各写各的，结果规则漂移，
#: `AsyncFlag` 被原样发到对端、`FileGroupDescriptorW` 被原样写进剪贴板。
NEVER_WRITE_FORMATS = PATH_BEARING_FORMATS | SHELL_BOOKKEEPING_FORMATS | VIRTUAL_FILE_FORMATS


def build_file_clipboard_items(paths: Sequence[str]) -> "List[FormatBlob]":
    """把一组**本机**文件路径包成"可粘贴文件"的格式集合。

    只写两个格式，而且是有意为之：

      * `CF_HDROP` —— 路径列表，也是接收端判断"这是一次文件复制"的信号；
      * `Preferred DropEffect = COPY`（4 字节 DWORD）—— 显式声明这是复制。

    **不写 `Shell IDList Array`。** 我一度以为"只写 CF_HDROP 粘贴不了"而用本地
    PIDL 重建了它，结果**资源管理器的粘贴直接崩掉重启**（对照实验：加它之前
    只是"什么都不发生"，不崩）。`CIDA` 结构出一点偏差就会让 Shell 越界解析，
    已知的崩溃案例不少。而 `CF_HDROP` 单独使用是有官方文档背书的做法：
    `CF_HDROP` 就是"用来传递一组**已存在文件**的位置"的标准格式。

    **也不写 `FileGroupDescriptorW`。** 它是"虚拟文件"协议的一半（另一半
    `FileContents` 要在粘贴时现场渲染），写进去只会得到"转沙漏空等"。
    见 `VIRTUAL_FILE_FORMATS`。
    """
    return [
        FormatBlob(name=HDROP_FORMAT, category=CAT_FILES, data=build_hdrop(paths)),
        FormatBlob(
            name=PREFERRED_DROPEFFECT_FORMAT,
            category=CAT_FILES,
            data=struct.pack("<I", DROPEFFECT_COPY),
        ),
    ]


#: `CreateProcess` 的 `CREATE_NO_WINDOW`：不弹控制台窗口（否则每次投递文件闪一下黑框）
_CREATE_NO_WINDOW = 0x08000000


def powershell_path() -> str:
    """定位 `powershell.exe`（Windows PowerShell 5.1）。"""
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    return os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")


def write_file_clipboard_via_powershell(paths: Sequence[str], timeout: float = 20.0) -> Optional[int]:
    """用 `Set-Clipboard -LiteralPath` 把文件放进剪贴板。成功返回新的剪贴板序号。

    **为什么走 PowerShell 而不是自己 `SetClipboardData`。**
    `Set-Clipboard` 内部是 .NET 的 `DataObject` + OLE `OleSetClipboard`，也就是
    资源管理器自己用的那条路。真机 A/B 对照（逐格式打印 + 手动 `Ctrl+V`）显示，
    它写出来的剪贴板比我们手拼的 `CF_HDROP` **多三样东西**：

      * `Ole Private Data` —— 标记"这个剪贴板来自一个真正的 OLE 数据对象"；
      * `DataObject` / `FileName` / `FileNameW` —— Shell 认的老式文件格式；
      * `fWide` 写成 `-1`（BOOL 的另一种真值写法，同样合法）。

    自己拼字节的写法在真机上一度把资源管理器搞崩过（`Shell IDList Array` 结构
    出一点偏差就崩），所以这里**优先用平台自己的实现**。

    几个必须做对的地方，全都踩过：

      * **绝不能用 `shell=True` 拼命令行。** 文件名里的 `&` `%` `"` 都是合法字符，
        拼字符串轻则出错、重则执行到别的命令。这里用 `-EncodedCommand`
        （UTF-16LE + Base64）传脚本，路径再怎么长、带什么字符都不会被 shell 解释。
      * **加 `CREATE_NO_WINDOW`**，否则每次收到文件都会闪一个黑窗口。
      * **用序号变化判断成功**，而不是只看退出码：进程起来了不代表剪贴板真的变了。
    """
    if not paths:
        return None

    #: **`Set-Clipboard -LiteralPath` 对不存在的路径不报错。** 实测：退出码 0，
    #: 剪贴板里就躺着一个死路径（`-LiteralPath` 不做通配符解析，也就跳过了存在性
    #: 检查）。于是"文件已被暂存区 TTL 清掉"会**静默**变成用户那边的
    #: "粘贴时提示找不到文件"。自己先查一遍，比事后猜粘贴为什么失败便宜得多。
    missing = [p for p in paths if not os.path.exists(p)]
    if missing:
        log.warning(
            "这些路径已不存在，不放进剪贴板（否则 Explorer 会拿着死路径去找文件）: %s",
            ", ".join(missing[:4]) + ("…" if len(missing) > 4 else ""),
        )
        return None

    before = int(w.user32.GetClipboardSequenceNumber())
    #: 单引号在 PowerShell 里靠**写两遍**转义；-EncodedCommand 保证不会被 shell 再解释一次。
    quoted = ", ".join("'" + str(p).replace("'", "''") + "'" for p in paths)
    script = "$ErrorActionPreference = 'Stop'\n$p = @(%s)\nSet-Clipboard -LiteralPath $p\n" % quoted
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")

    try:
        proc = subprocess.run(
            [
                powershell_path(),
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                encoded,
            ],
            capture_output=True,
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("调用 PowerShell 设置文件剪贴板失败: %s", exc)
        return None

    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip()
        log.warning("PowerShell Set-Clipboard 退出码 %d: %s", proc.returncode, detail[:300])
        return None

    after = int(w.user32.GetClipboardSequenceNumber())
    if after == before:
        #: 进程正常退出但剪贴板序号没动 —— 不能当成成功。
        log.warning("PowerShell 已退出但剪贴板序号没变化（%d），视为失败", before)
        return None
    return after


def parse_hdrop_paths(data: bytes) -> List[str]:
    """解析 CF_HDROP 字节流。实现细节见 `clipsync.bridge.parse_hdrop`。

    放在这里是为了让剪贴板模块自包含（`clipsync` 依赖 `win.clipboard`，
    反向依赖会形成环）。两个实现共用同一套规则：只按 20 字节头 +
    `fWide` 解码，并对不可信的 `pFiles` 偏移做回退。
    """
    import struct

    size = _DROPFILES_HEADER
    if len(data) < size:
        return []
    p_files, _px, _py, _nc, f_wide = struct.unpack_from("<IiiII", data, 0)
    offset = int(p_files) if p_files else size
    if offset < size or offset >= len(data):
        offset = size

    def decode(block: bytes) -> List[str]:
        if f_wide:
            end = len(block) - (len(block) % 2)
            text = block[:end].decode("utf-16-le", errors="replace")
        else:
            text = block.decode("mbcs", errors="replace")
        return [part for part in (chunk.strip() for chunk in text.split("\x00")) if part]

    paths = decode(data[offset:])
    if paths and _looks_like_paths(paths):
        return paths
    if offset != size:
        fallback = decode(data[size:])
        if _looks_like_paths(fallback):
            return fallback
    return paths


def _looks_like_paths(paths: List[str]) -> bool:
    """判断解出来的是不是"像路径"的东西。

    正常路径以盘符（`C:\\`）或 UNC（`\\\\server\\share`）开头。
    这是**防御性**检查：`CF_HDROP` 被破坏或偏移算错时，宁可返回空，
    也不要拿一坨二进制当路径去访问文件系统。
    """
    if not paths:
        return False
    first = paths[0]
    if not first:
        return False
    if len(first) >= 2 and first[1] == ":":
        return True
    return first.startswith("\\\\") or first.startswith("//")


__all__ = [
    "CAT_FILES",
    "CAT_HTML",
    "CAT_IMAGE",
    "CAT_OLE",
    "CAT_OTHER",
    "CAT_RTF",
    "CAT_TEXT",
    "ClipboardBusy",
    "ClipboardSession",
    "ClipboardSnapshot",
    "ClipboardWriteResult",
    "FormatBlob",
    "PATH_BEARING_FORMATS",
    "build_hdrop",
    "capture",
    "classify_format",
    "clear_clipboard",
    "enumerate_formats",
    "make_hdrop_blob",
    "order_for_paste",
    "parse_hdrop_paths",
    "read_format",
    "write_formats",
]
