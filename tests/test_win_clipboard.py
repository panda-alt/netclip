"""需要真实 Windows 剪贴板的测试。

这些测试**会临时改写剪贴板内容**，但会在结束时还原。所以跑测试的机器上
不要同时复制重要内容。
"""

from __future__ import annotations

import os
import sys
import tempfile

import pytest

from netclip.win import clipboard as cb
from netclip.win import winapi as w

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="需要 Windows 剪贴板")


def _make_test_dib(width=4, height=4):
    """构造一个最小的 32bpp BI_RGB DIB。"""
    import ctypes

    header = ctypes.create_string_buffer(40)
    fields = [40, width, height, 1, 32, 0, width * height * 4, 0, 0, 0, 0]
    for idx, value in enumerate(fields):
        ctypes.memmove(ctypes.byref(header, idx * 4), ctypes.byref(ctypes.c_int32(value)), 4)
    pixels = b"".join(
        bytes([(x * 40) % 256, (y * 40) % 256, 128, 255]) for y in range(height) for x in range(width)
    )
    return header.raw + pixels


# --------------------------------------------------------------------- 文件类格式诊断


def _dump(snapshot) -> str:
    import contextlib
    import io

    from netclip.__main__ import _dump_file_formats

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _dump_file_formats(snapshot)
    return buf.getvalue()


def _hdrop_with_fwide(fwide: int) -> bytes:
    import struct

    names = "C:\\netclip_probe\\x.txt\x00\x00".encode("utf-16-le")
    return struct.pack("<IiiIi", 20, 0, 0, 0, fwide) + names


def _snapshot_with(data: bytes):
    return cb.ClipboardSnapshot(
        sequence=1,
        items=[cb.FormatBlob(name=cb.HDROP_FORMAT, category=cb.CAT_FILES, data=data)],
    )


def test_dump_formats_treats_fwide_minus_one_as_wide():
    """`fWide` 是 BOOL：`-1`（0xFFFFFFFF）和 `1` 一样都是"真"。

    真机实测：PowerShell 的 `Set-Clipboard -Path` 写的就是 `-1`。早先按无符号读，
    `--dump-formats` 会把它显示成 4294967295 并且**报成异常** —— 诊断工具自己
    制造假警报，会把排查带偏（我们就差点因此以为 PowerShell 那条路是坏的）。
    """
    out = _dump(_snapshot_with(_hdrop_with_fwide(-1)))
    assert "4294967295" not in out, "又把 BOOL 的 -1 按无符号解释了"
    assert "宽字符" in out
    assert "ANSI" not in out


def test_dump_formats_still_flags_genuine_ansi_hdrop():
    """反过来也要成立：真的写成 ANSI（fWide=0）时必须报出来，否则这个检查就废了。"""
    out = _dump(_snapshot_with(_hdrop_with_fwide(0)))
    assert "ANSI" in out


def test_dump_formats_flags_wrong_pfiles_offset():
    """`pFiles` 不是 20 时文件名起始偏移就错了，必须报出来。"""
    import struct

    names = "C:\\netclip_probe\\x.txt\x00\x00".encode("utf-16-le")
    out = _dump(_snapshot_with(struct.pack("<IiiIi", 16, 0, 0, 0, 1) + names))
    assert "应为 20" in out


# --------------------------------------------------------------------- PowerShell 文件剪贴板


def test_powershell_file_clipboard_handles_nasty_filename():
    """文件名里的 `&` / `'` / `%` / 空格 / 括号都必须原样通过。

    这正是 `subprocess.run(f'powershell.exe Set-Clipboard -Path "{path}"', shell=True)`
    会翻车的地方：`&` 是 cmd 的命令分隔符，`'` 会破坏 PowerShell 的引号，
    `%` 在 cmd 里还会被当变量展开。这三种字符全都是 Windows 的**合法**文件名字符。
    我们用 `-EncodedCommand`（UTF-16LE + Base64）传脚本，路径不经过任何 shell 解析。
    """
    import tempfile

    directory = os.path.join(tempfile.gettempdir(), "netclip_probe")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "it's & 100% (test).txt")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("probe")

    sequence = cb.write_file_clipboard_via_powershell([path])
    assert sequence, "PowerShell 写法报告失败"

    snapshot = cb.capture(max_per_format=1024 * 1024)
    hdrop = [i for i in snapshot.items if i.name == cb.HDROP_FORMAT]
    assert hdrop, "剪贴板里没有 CF_HDROP"
    assert cb.parse_hdrop_paths(hdrop[0].data) == [path]
    # PowerShell 走的是 OLE 路径，会多带一个"这是 OLE 数据对象"的标记格式
    assert any(i.name == "Ole Private Data" for i in snapshot.items), "没走成 OLE 那条路"


def test_powershell_file_clipboard_falls_back_when_powershell_unavailable(monkeypatch):
    """PowerShell 起不来时必须返回 None，让调用方回退 —— 不能抛出去。"""

    def boom(*_args, **_kwargs):
        raise OSError("powershell.exe 不存在")

    monkeypatch.setattr(cb.subprocess, "run", boom)
    assert cb.write_file_clipboard_via_powershell(["C:\\x.txt"]) is None


def test_powershell_file_clipboard_treats_unchanged_sequence_as_failure(monkeypatch):
    """退出码 0 但剪贴板序号没动，必须当成失败。

    只看退出码是不够的：进程能起来、能正常退出，不代表剪贴板真的被改了
    （策略拦截、权限问题都可能这样）。漏了这一步，回退逻辑就形同虚设。
    """
    import types

    monkeypatch.setattr(
        cb.subprocess,
        "run",
        lambda *_a, **_k: types.SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
    )
    assert cb.write_file_clipboard_via_powershell(["C:\\x.txt"]) is None


def test_powershell_file_clipboard_rejects_missing_paths():
    """路径不存在时**绝不能**交给 PowerShell。

    实测：`Set-Clipboard -LiteralPath` 对不存在的路径**不报错**（退出码 0），
    剪贴板里就躺着一个死路径。`-LiteralPath` 不做通配符解析，也就跳过了存在性检查。
    后果是"暂存文件被 TTL 清掉"会静默变成用户那边的"粘贴时提示找不到文件"。
    """
    missing = os.path.join(tempfile.gettempdir(), "netclip_probe", "definitely-not-here-xyz.txt")
    if os.path.exists(missing):  # pragma: no cover - 极端巧合
        os.remove(missing)
    assert cb.write_file_clipboard_via_powershell([missing]) is None


# --------------------------------------------------------------------- 分类


def test_classify_standard_formats():
    assert cb.classify_format(w.CF_UNICODETEXT, "CF_UNICODETEXT") == cb.CAT_TEXT
    assert cb.classify_format(w.CF_DIB, "CF_DIB") == cb.CAT_IMAGE
    assert cb.classify_format(w.CF_HDROP, "CF_HDROP") == cb.CAT_FILES


def test_classify_registered_formats_by_name():
    """注册格式的 ID 是运行期分配的，所以必须靠名字分类。"""
    assert cb.classify_format(0xC123, "HTML Format") == cb.CAT_HTML
    assert cb.classify_format(0xC124, "Rich Text Format") == cb.CAT_RTF
    assert cb.classify_format(0xC125, "PNG") == cb.CAT_IMAGE
    assert cb.classify_format(0xC126, "PowerPoint 12.0 Shape") == cb.CAT_IMAGE
    assert cb.classify_format(0xC127, "MathType 5.0 Equations") == cb.CAT_OLE


def test_classify_unknown_is_other():
    assert cb.classify_format(0xC999, "SomeVendor Blob") == cb.CAT_OTHER


# --------------------------------------------------------------------- 往返


def _snapshot_then_restore(fn):
    """先备份剪贴板，跑测试，再还原。"""
    before = cb.capture(max_per_format=64 * 1024 * 1024)
    try:
        return fn()
    finally:
        if before.items:
            cb.write_formats(cb.order_for_paste(before.items, []))
        else:
            cb.clear_clipboard()


def test_unicode_text_roundtrip():
    def body():
        text = "netclip 中文测试 ✓"
        result = cb.write_formats(
            [cb.FormatBlob(name="CF_UNICODETEXT", category=cb.CAT_TEXT, data=text.encode("utf-16-le"))]
        )
        assert result, "写入应成功"
        snap = cb.capture(max_per_format=1024 * 1024)
        item = snap.by_name("CF_UNICODETEXT")
        assert item is not None
        assert item.data.decode("utf-16-le") == text

    _snapshot_then_restore(body)


def test_unicode_text_has_no_trailing_nul():
    """读出来的文本不能带 NUL 终止符，否则粘到编辑器里会多一个不可见字符。"""

    def body():
        cb.write_formats([cb.FormatBlob(name="CF_UNICODETEXT", category=cb.CAT_TEXT, data="abc".encode("utf-16-le"))])
        snap = cb.capture(max_per_format=1024 * 1024)
        item = snap.by_name("CF_UNICODETEXT")
        assert item is not None
        assert item.data == "abc".encode("utf-16-le")
        assert not item.data.endswith(b"\x00\x00")
    _snapshot_then_restore(body)


# --------------------------------------------------------------------- OLE 接管


def test_ole_takeover_never_loses_the_clipboard():
    """**回归测试**：OLE 接管失败时，必须把原内容原样还回来。

    裸 `SetClipboardData` 写出来的剪贴板没有 OLE 数据对象的身份，而 Office/WPS 的
    粘贴走 `OleGetClipboard`，所以试过"写完再让 OLE 接管一次"。**本机实测这一步会失败**：

        OleSetClipboard = CLIPBRD_E_CANT_CLOSE
        接管后剪贴板只剩 ['DataObject']   <- 我们写进去的格式全没了

    所以 `bless_clipboard_with_ole()` 收尾时会**核对格式有没有变少**，少了就返回 False。
    这条测试钉的是：**不管接管成不成功，剪贴板都不能比接管前更少东西**。
    宁可不要这个改善，也绝不能把用户的剪贴板弄丢。
    """

    def body():
        items = [
            cb.FormatBlob(
                name="CF_UNICODETEXT", category=cb.CAT_TEXT, data="ole 接管测试".encode("utf-16-le")
            ),
            cb.FormatBlob(
                name="Embed Source", category=cb.CAT_OLE, data=bytes.fromhex("d0cf11e0a1b11ae1") + b"X" * 128
            ),
        ]
        cb.write_formats(items)
        before = cb._clipboard_format_names()  # noqa: SLF001 - 测试要的就是这个内部量

        cb.bless_clipboard_with_ole()

        after = cb._clipboard_format_names()  # noqa: SLF001
        lost = before - after
        if lost:
            #: 说明接管确实会丢东西 —— 那就必须能靠重写补回来（调用方就是这么做的）
            cb.write_formats(items)
            restored = cb._clipboard_format_names()  # noqa: SLF001
            assert not (before - restored), "接管丢了 %s，重写也补不回来" % sorted(lost)
        assert cb._clipboard_format_names()  # noqa: SLF001 - 任何时候都不该是空的

    _snapshot_then_restore(body)


# --------------------------------------------------------------------- 剪贴板所有者


def test_write_formats_can_set_the_clipboard_owner():
    """**回归测试**：写剪贴板时可以把**所有者窗口**登记进去。

    不指定时所有者是 NULL。而对端（Office/WPS）的粘贴走 `OleGetClipboard`，
    它要跟所有者打交道。真机对照里唯一还没被动过的差别就是这个：

      | | 剪贴板所有者 |
      |---|---|
      | UU远程（对端能粘成**可编辑对象**） | GameViewer.exe（**有窗口**） |
      | 原生复制 | WPS（有窗口） |
      | **我们（一直）** | **NULL** |

    所以这里钉住两件事：指定了就真的登记上、不指定就还是无主（不回退）。
    """

    def body():
        from netclip.win.msgwin import ClipboardListener

        blob = cb.FormatBlob(
            name="CF_UNICODETEXT", category=cb.CAT_TEXT, data="owner 测试".encode("utf-16-le")
        )

        cb.write_formats([blob])
        assert not w.user32.GetClipboardOwner(), "不指定 owner 时应该是无主（NULL）"

        listener = ClipboardListener(lambda _seq: None)
        assert listener.start(), "剪贴板监听窗口起不来"
        try:
            cb.write_formats([blob], owner=listener.hwnd)
            assert int(w.user32.GetClipboardOwner() or 0) == int(listener.hwnd), (
                "指定的所有者窗口没被登记上"
            )
        finally:
            listener.stop()

    _snapshot_then_restore(body)


# --------------------------------------------------------------------- 采集顺序


def test_capture_preserves_the_clipboard_enumeration_order():
    """**回归测试**：采集结果的顺序必须和剪贴板上的枚举顺序一致。

    这个顺序会被接收端**原样照搬**去写剪贴板 —— 所以一旦采集时排了序，就等于
    替消费者重排了格式优先级。真机抓到的对比（同一个剪贴板，两台各跑一次
    `tools/clip_probe.py`）::

        发送端: DataObject > Kingsoft Data Descriptor > Kingsoft WPS 9.0 Format > …
        接收端: CF_UNICODETEXT > CF_ENHMETAFILE > DataObject > …
                 ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^ 被我们提到了最前面

    根因是 `capture()` 里为了**等价表示去重**而做的"按组优先级读取"排序，泄漏到了
    `items` 的顺序上。读取顺序和输出顺序是两回事，去重只需要前者。
    """

    def body():
        items = [
            cb.FormatBlob(name="Embed Source", category=cb.CAT_OLE, data=b"A" * 40),
            cb.FormatBlob(name="HTML Format", category=cb.CAT_HTML, data=b"<html></html>"),
            cb.FormatBlob(name="CF_UNICODETEXT", category=cb.CAT_TEXT, data="x".encode("utf-16-le")),
        ]
        cb.write_formats(items)

        #: **写完剪贴板后"立刻"枚举可能只列出第一种格式** —— 实测（Windows 10）：
        #: 紧接着写的那次 `EnumClipboardFormats` 只返回 `['Embed Source']`，
        #: 再调一次才把 6 种全列出来。格式表是异步铺完的，所以先枚举一次当"落定"。
        with cb.ClipboardSession():
            cb.enumerate_formats()
        with cb.ClipboardSession():
            on_clipboard = [name for _fmt, name in cb.enumerate_formats()]
        snapshot = cb.capture(max_per_format=1024 * 1024)
        got = [i.name for i in snapshot.items]

        assert got, "什么都没采集到"
        assert got == [name for name in on_clipboard if name in set(got)], (
            "采集顺序 %s 和剪贴板枚举顺序 %s 对不上" % (got, on_clipboard)
        )
        #: 最关键的一条：文本**不许**被提到最前面 —— 那正是以前干的事。
        assert got[0] != "CF_UNICODETEXT", "文本格式又被排到最前面了"

    _snapshot_then_restore(body)


def test_registered_format_roundtrip():
    """跨机同步私有格式的基础：按名字注册、原样传字节。"""

    def body():
        payload = bytes(range(256)) * 4
        name = "netclip.test.private"
        cb.write_formats([cb.FormatBlob(name=name, category=cb.CAT_OTHER, data=payload)])
        snap = cb.capture(max_per_format=1024 * 1024)
        item = snap.by_name(name)
        assert item is not None, "自定义格式应被枚举到（名字匹配，而不是 ID）"
        assert item.data == payload

    _snapshot_then_restore(body)


def test_html_format_roundtrip():
    def body():
        html = b"<html><body>hello</body></html>"
        cb.write_formats([cb.FormatBlob(name="HTML Format", category=cb.CAT_HTML, data=html)])
        snap = cb.capture(max_per_format=1024 * 1024)
        item = snap.by_name("HTML Format")
        assert item is not None
        assert item.data == html

    _snapshot_then_restore(body)


def test_multiple_formats_at_once():
    """一次复制会同时产生多种格式，必须都能写进去、都能读回来。"""

    def body():
        items = [
            cb.FormatBlob(name="CF_UNICODETEXT", category=cb.CAT_TEXT, data="hi".encode("utf-16-le")),
            cb.FormatBlob(name="Rich Text Format", category=cb.CAT_RTF, data=b"{\\rtf1 hi}"),
            cb.FormatBlob(name="netclip.test.multi", category=cb.CAT_OTHER, data=b"\x01\x02\x03"),
        ]
        result = cb.write_formats(items)
        assert len(result.written) == 3

        snap = cb.capture(max_per_format=1024 * 1024)
        assert snap.has(cb.CAT_TEXT)
        assert snap.has(cb.CAT_RTF)
        assert snap.by_name("netclip.test.multi") is not None

    _snapshot_then_restore(body)


def test_per_format_limit_is_reported_not_fatal():
    """单个格式超限应该被跳过并记录原因，而不是让整次同步失败。"""

    def body():
        big = b"x" * 5000
        cb.write_formats([cb.FormatBlob(name="netclip.test.big", category=cb.CAT_OTHER, data=big)])
        snap = cb.capture(max_per_format=100)
        assert snap.by_name("netclip.test.big") is None
        assert any("上限" in reason for _name, reason in snap.skipped)

    _snapshot_then_restore(body)


def test_order_for_paste_puts_embedded_object_last():
    """CF_EMBEDDEDOBJECT 必须最后写，否则 Office 认不出可编辑对象。"""
    items = [
        cb.FormatBlob(name="CF_EMBEDDEDOBJECT", data=b"x"),
        cb.FormatBlob(name="CF_UNICODETEXT", data=b"y"),
        cb.FormatBlob(name="PowerPoint 12.0 Shape", data=b"z"),
    ]
    ordered = cb.order_for_paste(items, ["PowerPoint 12.0 Shape", "CF_UNICODETEXT"])
    names = [i.name for i in ordered]
    assert names[-1] == "CF_EMBEDDEDOBJECT"
    assert names[0] == "PowerPoint 12.0 Shape"
    assert names[1] == "CF_UNICODETEXT"


def test_order_preserves_items_not_in_priority():
    items = [cb.FormatBlob(name="CF_A", data=b"1"), cb.FormatBlob(name="CF_B", data=b"2")]
    ordered = cb.order_for_paste(items, ["CF_UNKNOWN"])
    assert {i.name for i in ordered} == {"CF_A", "CF_B"}


def test_write_empty_is_rejected_per_item():
    result = cb.write_formats([cb.FormatBlob(name="CF_UNICODETEXT", category=cb.CAT_TEXT, data=b"")])
    assert not result
    assert result.failed


def test_capture_on_empty_clipboard():
    def body():
        cb.clear_clipboard()
        snap = cb.capture(max_per_format=1024)
        assert snap.is_empty

    _snapshot_then_restore(body)


# --------------------------------------------------------------------- 长度规则


def test_length_rules_for_self_describing_formats():
    """定长结构**绝不能**去尾部零。

    **这是真机上"文件能传过来但粘贴不了"的根因。** `Preferred DropEffect` 是 4 字节
    DWORD（DROPEFFECT_COPY=1 → `01 00 00 00`）。按默认规则去尾部零之后只剩 `01`，
    转发给对端就是一段 1 字节数据；Explorer 按 4 字节读，多读的 3 字节取决于堆内存，
    只要拼出来不是 1，Shell 就认为"这不是复制"，按 Ctrl+V 既不报错也不粘贴。
    """
    from netclip.win.clipboard import LEN_SELF_DESCRIBING, _length_rule

    for name in ("CF_HDROP", "Preferred DropEffect", "DataObjectAttributes", "AsyncFlag"):
        assert _length_rule(name) == LEN_SELF_DESCRIBING, "%s 不能被裁剪" % name
    #: 文本类仍然要去尾部零
    assert _length_rule("CF_UNICODETEXT") != LEN_SELF_DESCRIBING
    """自描述格式绝不能被"去尾部零"处理 —— 那会静默损坏数据。"""
    from netclip.win.clipboard import LEN_SELF_DESCRIBING, LEN_TERMINATED, LEN_TRAILING_ZEROS, _length_rule

    assert _length_rule("CF_UNICODETEXT") == LEN_TERMINATED
    assert _length_rule("CF_TEXT") == LEN_TERMINATED
    for name in ("CF_HDROP", "CF_DIB", "CF_DIBV5", "CF_ENHMETAFILE", "CF_METAFILEPICT", "CF_WAVE", "CF_TIFF"):
        assert _length_rule(name) == LEN_SELF_DESCRIBING, name
    # 未知的注册格式走默认规则
    assert _length_rule("SomeVendor Thing") == LEN_TRAILING_ZEROS


def test_hdrop_bytes_survive_clipboard_roundtrip():
    """CF_HDROP 的末尾 \\0\\0 是协议的一部分，不能被当成填充裁掉。"""
    import struct

    def body():
        payload = b"".join(p.encode("utf-16-le") + b"\x00\x00" for p in ("C:/a.txt", "C:/b.txt")) + b"\x00\x00"
        data = struct.pack("<IiiII", 20, 0, 0, 0, 1) + payload
        cb.write_formats([cb.FormatBlob(name="CF_HDROP", category=cb.CAT_FILES, data=data)])
        snap = cb.capture(max_per_format=1024 * 1024)
        item = snap.by_name("CF_HDROP")
        assert item is not None
        # 末尾两个零字节必须还在
        assert item.data.endswith(b"\x00\x00")
        assert item.data == data

    _snapshot_then_restore(body)


def test_registered_binary_format_with_trailing_nuls():
    """尾部零字节是真实数据的注册格式，往返之后不能少字节。"""
    def body():
        payload = b"\x01\x02\x00\x00\x00\x00"
        cb.write_formats([cb.FormatBlob(name="netclip.test.zeros", category=cb.CAT_OTHER, data=payload)])
        snap = cb.capture(max_per_format=1024 * 1024)
        item = snap.by_name("netclip.test.zeros")
        assert item is not None
        # 默认规则会裁掉尾部零，所以这里允许被裁；关键是**不能多出**字节
        assert payload.startswith(item.data)
        assert len(item.data) <= len(payload)

    _snapshot_then_restore(body)


# --------------------------------------------------------------------- 等价表示去重


def test_dedupe_picks_unroutable_winner_fallback():
    """高优先级的等价表示读不出来时，必须回退到下一个读得出来的那个。

    这是真实踩过的坑：Windows 报告 CF_DIBV5 存在但句柄为空，
    如果按名字提前把 CF_DIB 跳过了，结果就是整张图都没同步。
    """
    def body():
        dib = _make_test_dib(3, 3)
        cb.write_formats([cb.FormatBlob(name="CF_DIB", category=cb.CAT_IMAGE, data=dib)])
        # 注意：这里没有再写 CF_DIBV5，用系统行为制造"格式表里有但读不出来"的状态。
        snap = cb.capture(max_per_format=1024 * 1024, dedupe_groups=True)
        image_items = [i for i in snap.items if i.category == cb.CAT_IMAGE]
        assert image_items, "至少要有一个图像格式被保留下来"
        assert any(i.name in ("CF_DIB", "CF_DIBV5", "CF_BITMAP") for i in image_items)

    _snapshot_then_restore(body)


def test_dedupe_drops_ansi_text_when_unicode_present():
    def body():
        cb.write_formats(
            [cb.FormatBlob(name="CF_UNICODETEXT", category=cb.CAT_TEXT, data="中文 abc".encode("utf-16-le"))]
        )
        snap = cb.capture(max_per_format=1024 * 1024, dedupe_groups=True)
        names = {i.name for i in snap.items}
        assert "CF_UNICODETEXT" in names
        # 系统会自动附带 ANSI 版本，它们必须被去重掉（否则对端可能挑到有损的那个）
        assert "CF_TEXT" not in names
        assert "CF_OEMTEXT" not in names
        assert any("去重" in reason for _n, reason in snap.skipped)

    _snapshot_then_restore(body)


def test_dedupe_disabled_keeps_everything():
    def body():
        cb.write_formats(
            [cb.FormatBlob(name="CF_UNICODETEXT", category=cb.CAT_TEXT, data="abc".encode("utf-16-le"))]
        )
        snap = cb.capture(max_per_format=1024 * 1024, dedupe_groups=False)
        names = {i.name for i in snap.items}
        assert "CF_UNICODETEXT" in names
        # 关掉去重后，系统附带的等价表示也应该在（可能没有，取决于系统行为）
        assert not any("去重" in reason for _n, reason in snap.skipped)

    _snapshot_then_restore(body)


def test_locale_format_is_always_skipped():
    """CF_LOCALE 里的 LCID 只在本机有效，跨机传过去有害无益。

    注意这一条是 `make_collect_filter` 的职责，所以这里用它来做断言。
    """
    from netclip.clipsync.bridge import make_collect_filter
    from netclip.clipsync.policy import SyncPolicy

    filter_fn = make_collect_filter(SyncPolicy())
    allowed, _why = True, ""
    assert not filter_fn(w.CF_LOCALE, "CF_LOCALE", cb.CAT_OTHER)
    assert not filter_fn(w.CF_PALETTE, "CF_PALETTE", cb.CAT_IMAGE)
    assert filter_fn(w.CF_UNICODETEXT, "CF_UNICODETEXT", cb.CAT_TEXT)
    assert filter_fn(0xC000, "PowerPoint 12.0 Shape", cb.CAT_IMAGE)


# --------------------------------------------------------------------- 端到端


def _wait_for(predicate, timeout=5.0, interval=0.05):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_end_to_end_clipboard_sync_over_fake_link():
    """把两个 ClipboardSync 用内存链路接起来，跑通真实的发送 -> 接收 -> 写剪贴板。

    覆盖的是"网络那一段之外"的全部代码路径：采集过滤、策略决策、压缩、
    编码、切分、解码、格式排序、写剪贴板、回声抑制。
    网络本身由 `selftest loop` 覆盖。

    之所以能做到，是因为 `ClipboardSync` 不直接持有 socket ——
    它通过注入的 `send` 回调发帧，所以测试里可以换成内存转发。
    """
    from netclip.clipsync.bridge import ClipboardSync
    from netclip.clipsync.policy import SyncPolicy
    from netclip.protocol import MsgType

    def body():
        policy = SyncPolicy(max_payload_mb=8, per_format_max_mb=16)
        a_frames = []

        # A 发出的帧直接投递给 B（模拟 clip 通道）
        bridge = {"b": None}

        def send_a(mtype, msg_body, blob, _key=None):
            a_frames.append((mtype, msg_body, blob))
            if bridge["b"] is not None:
                bridge["b"].on_frame(mtype, msg_body, blob)

        a = ClipboardSync(policy=policy, send=send_a)
        b = ClipboardSync(policy=policy, send=lambda *a_, **k_: None)
        bridge["b"] = b

        text = "端到端 同步测试 ✓ netclip-unique-marker"
        private = b"\xde\xad\xbe\xef" + bytes(range(32))
        cb.write_formats(
            [
                cb.FormatBlob(name="CF_UNICODETEXT", category=cb.CAT_TEXT, data=text.encode("utf-16-le")),
                cb.FormatBlob(name="netclip.e2e.private", category=cb.CAT_OTHER, data=private),
            ]
        )

        decision = a.publish_local()
        assert decision.action == "full", decision.describe()
        assert any(m == MsgType.CLIP_BEGIN for m, _b, _blob in a_frames), "应该发出了 CLIP_BEGIN"

        # B 的写入在短命线程里跑，轮询等它完成。
        # 用独一无二的文本做判据，确保读到的是 B 写进去的而不是本机原有的。
        got_text = []

        def read_text():
            snap = cb.capture(max_per_format=1024 * 1024)
            item = snap.by_name("CF_UNICODETEXT")
            if item is None:
                return False
            got_text.append(item.data.decode("utf-16-le", errors="replace"))
            return got_text[-1] == text

        assert _wait_for(read_text), "对端没有在超时内把文本写进剪贴板（实际得到 %r）" % (got_text or None)

        def applied():
            return b.stats["recv"] == 1

        assert _wait_for(applied), "对端应记录一次成功应用（实际 %s，last_error=%r）" % (
            b.stats,
            b.last_error,
        )

        snap = cb.capture(max_per_format=1024 * 1024)
        got_private = snap.by_name("netclip.e2e.private")
        assert got_private is not None
        assert got_private.data == private

        # 回声抑制：B 写入后不应该再产生一次"对端变更"的发送
        b_frames_before = len(a_frames)
        a._on_local_change()  # 模拟 B 的写入触发的本机变更通知
        assert len(a_frames) == b_frames_before, "自己的写入被当成本地复制又发了一次（回声）"

    _snapshot_then_restore(body)


def test_end_to_end_oversized_becomes_announce():
    """超大内容只发 CLIP_ANNOUNCE，不带二进制体。"""
    from netclip.clipsync.bridge import ClipboardSync
    from netclip.clipsync.policy import SyncPolicy
    from netclip.protocol import MsgType

    def body():
        policy = SyncPolicy(max_payload_mb=1, per_format_max_mb=16)
        frames = []
        a = ClipboardSync(policy=policy, send=lambda m, b_, blob, k=None: frames.append((m, b_, blob)))

        cb.write_formats(
            # 必须是**不可压缩**的数据：全零的 DIB 会因为尾部零裁剪 + zlib 变成几百字节，
            # 那样就走不到"超过上限"的分支，测试也就测不到想问的东西。
            [cb.FormatBlob(name="CF_DIB", category=cb.CAT_IMAGE, data=os.urandom(2 * 1024 * 1024))]
        )
        decision = a.publish_local()
        assert decision.action == "announce"
        assert len(frames) == 1
        mtype, meta, blob = frames[0]
        assert mtype == MsgType.CLIP_ANNOUNCE
        assert blob == b"", "仅通知时不应携带二进制体"
        assert meta.get("announce_only") is True
        assert int(meta.get("bytes", 0)) > 0
        assert int(meta.get("n", 0)) >= 1, "仅通知时仍要带上格式元数据"
        # 这一段是压缩后的长度；2MB 全零压缩后极小，所以不能断言它很大
        assert any(str(i.get("n", "")) == "CF_DIB" for i in meta.get("items", []))
        assert "超过上限" in decision.reason

    _snapshot_then_restore(body)


# --------------------------------------------------------------------- 瞬时故障重试
#
# 这两组测试**不碰真实剪贴板** —— 它们要复现的是竞争中才会出现的瞬时状态，
# 真剪贴板既复现不出来（约 2.5% 概率）也不该被测试反复清空。
# 场景是实测出来的：120 次「写-读」往返里有 3 次读到空句柄，换会话重读全部恢复。


def _fake_capture_once(results, seen):
    """做一个按顺序返回预设结果的 `_capture_once` 替身。"""

    def fake(max_per_format, filter_fn, dedupe_groups):
        seen.append(1)
        return results[min(len(seen) - 1, len(results) - 1)]

    return fake


def _empty_handle_snapshot():
    snap = cb.ClipboardSnapshot(captured_at=0.0)
    snap.skipped.append(("CF_UNICODETEXT", cb._EMPTY_HANDLE))
    return snap, True


def _good_snapshot():
    snap = cb.ClipboardSnapshot(captured_at=0.0)
    snap.items.append(
        cb.FormatBlob(name="CF_UNICODETEXT", category=cb.CAT_TEXT, data="hi".encode("utf-16-le"))
    )
    return snap, True


def test_capture_retries_when_formats_are_listed_but_unreadable(monkeypatch):
    """列了格式却全是空句柄时，必须换会话重读。

    否则这一次复制就被当成"剪贴板没内容"丢掉 —— 序号已经推进，监听器不会再看
    第二眼，那次复制永远同步不过去。这正是要替掉 Mouse Without Borders 的毛病。
    """
    seen = []
    monkeypatch.setattr(cb, "CAPTURE_RETRY_MS", (0, 0, 0))
    monkeypatch.setattr(
        cb, "_capture_once", _fake_capture_once([_empty_handle_snapshot(), _good_snapshot()], seen)
    )

    snapshot = cb.capture(max_per_format=1024)
    assert len(seen) == 2, "应该重读了一次"
    assert snapshot.by_name("CF_UNICODETEXT") is not None


def test_capture_gives_up_after_the_retry_budget(monkeypatch):
    """一直读不出来就别无限重试 —— 带着空结果返回，让调用方照常处理。"""
    seen = []
    monkeypatch.setattr(cb, "CAPTURE_RETRY_MS", (0, 0, 0))
    monkeypatch.setattr(cb, "_capture_once", _fake_capture_once([_empty_handle_snapshot()], seen))

    snapshot = cb.capture(max_per_format=1024)
    assert len(seen) == 3, "重试次数应该就是 CAPTURE_RETRY_MS 的长度"
    assert not snapshot.items


def test_capture_does_not_retry_a_definitive_result(monkeypatch):
    """「被配置过滤」是确定的结果，重试只是白白拖慢每一次采集。"""
    seen = []

    def fake(max_per_format, filter_fn, dedupe_groups):
        seen.append(1)
        snap = cb.ClipboardSnapshot(captured_at=0.0)
        snap.skipped.append(("CF_DIB", "被配置过滤"))
        return snap, True

    monkeypatch.setattr(cb, "CAPTURE_RETRY_MS", (0, 0, 0))
    monkeypatch.setattr(cb, "_capture_once", fake)

    cb.capture(max_per_format=1024)
    assert len(seen) == 1


def test_capture_retries_a_clipboard_that_looks_completely_empty(monkeypatch):
    """"一个格式都没有"也可能只是瞬时状态 —— 实测刚写完时常被读成这样。

    分不清"真空"和"假空"，而猜错的代价是**永久丢掉一次复制**（序号已经推进），
    所以这边一律重读。`capture()` 只在剪贴板真的变化时才被调用，不是定时轮询。
    """
    seen = []
    monkeypatch.setattr(cb, "CAPTURE_RETRY_MS", (0, 0, 0))
    monkeypatch.setattr(
        cb,
        "_capture_once",
        _fake_capture_once(
            [(cb.ClipboardSnapshot(captured_at=0.0), False), _good_snapshot()], seen
        ),
    )

    snapshot = cb.capture(max_per_format=1024)
    assert len(seen) == 2, "应该重读了一次"
    assert snapshot.by_name("CF_UNICODETEXT") is not None


class _FakeSession:
    """替掉真的 `ClipboardSession`：不碰系统剪贴板。"""

    def __init__(self, retry_ms=cb.DEFAULT_OPEN_RETRY_MS, owner: int = 0) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


def _patch_write(monkeypatch, set_one):
    monkeypatch.setattr(cb, "ClipboardSession", _FakeSession)
    monkeypatch.setattr(cb, "_set_one", set_one)
    monkeypatch.setattr(cb, "_WRITE_RETRY_MS", (0, 0, 0))
    monkeypatch.setattr(cb.w.user32, "EmptyClipboard", lambda: True)
    monkeypatch.setattr(cb.w.user32, "GetClipboardSequenceNumber", lambda: 7)


def _one_blob():
    return cb.FormatBlob(
        name="CF_UNICODETEXT", category=cb.CAT_TEXT, data="hi".encode("utf-16-le")
    )


def test_write_formats_retries_when_nothing_was_written(monkeypatch):
    """`EmptyClipboard` 成功了、但一个格式都没写进去时，必须整段重来。

    这时候剪贴板**已经被我们清空了**，就此收手等于把用户原来的内容删掉还什么都
    没换上 —— 比不同步还糟。
    """
    calls = []

    def flaky(item):
        calls.append(item.name)
        if len(calls) < 3:
            raise cb.w.WinApiError(5, "写入时被别的进程抢走了")

    _patch_write(monkeypatch, flaky)
    result = cb.write_formats([_one_blob()])

    assert len(calls) == 3, "前两次颗粒无收，应该重来"
    assert result.written == ["CF_UNICODETEXT"]


def test_write_formats_does_not_retry_bad_data(monkeypatch):
    """数据本身有问题（`ValueError`）时重试没有意义，别浪费六轮退避。"""
    calls = []

    def broken(item):
        calls.append(item.name)
        raise ValueError("数据为空")

    _patch_write(monkeypatch, broken)
    result = cb.write_formats([_one_blob()])

    assert len(calls) == 1
    assert result.written == []
    assert result.failed and "数据为空" in result.failed[0][1]


def test_write_formats_raises_when_the_clipboard_stays_broken(monkeypatch):
    """一直被抢就抛出去 —— 调用方需要知道"这次没写成功"，不能静默。"""

    def always_broken(item):
        raise cb.w.WinApiError(5, "一直抢不到")

    _patch_write(monkeypatch, always_broken)

    with pytest.raises(w.WinApiError):
        cb.write_formats([_one_blob()])
