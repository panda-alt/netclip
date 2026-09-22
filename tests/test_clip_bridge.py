"""剪贴板同步桥接层的单元测试。

覆盖：编码/解码往返、压缩、分片切分、CF_HDROP 解析、HTML 引用内联。
除 HTML 内联会临时写文件外，全部是纯逻辑。
"""

from __future__ import annotations

import os
import struct
import sys
import tempfile
import zlib

import pytest

from netclip.clipsync.bridge import (
    K_CAT,
    K_NAME,
    K_SIZE,
    K_ZLIB,
    _decode_items,
    _public_meta,
    _split_blob,
    inline_html_local_refs,
    make_collect_filter,
    parse_hdrop,
    writable_formats,
)
from netclip.clipsync.policy import CAT_IMAGE, CAT_TEXT, SyncPolicy
from netclip.win import winapi as w
from netclip.win import clipboard as cb


# --------------------------------------------------------------------- 采集过滤


#: 真机日志里实际抓到、并被原样发到对端的格式名。
_CROSS_MACHINE_JUNK = [
    "Shell IDList Array",
    "Shell Object Offsets",
    "AsyncFlag",
    "DropDescription",
    "UIDisplayed",
    "DataObjectAttributes",
    "DataObjectAttributesRequiringElevation",
    "FileName",
    "FileNameW",
]


def test_collect_filter_drops_shell_path_and_dragdrop_formats():
    """采集过滤必须挡掉 Shell 路径格式和拖放簿记格式。

    真机故障：从机复制文件、主机粘贴 —— **沙漏转一下，什么都没粘出来**。
    根因是这些规则当时只写在 `clipboard.collect_filter_skip()` 里（而且那个函数
    没有任何调用点），`make_collect_filter` 里另抄的一份漏了这两条，于是
    `AsyncFlag` 被发给对端，对方的资源管理器以为这是一次**异步拖放**，空等。

    这个用例直接把"抄漏"这件事钉死：`collect_filter_skip` 判掉的，采集过滤也必须判掉。
    """
    keep = make_collect_filter(SyncPolicy())
    leaked = [name for name in _CROSS_MACHINE_JUNK if keep(0, name, "other")]
    assert not leaked, "这些格式绝不该跨机转发: %s" % ", ".join(leaked)


def test_collect_filter_agrees_with_collect_filter_skip():
    """两层过滤不允许再各自漂移：硬规则只留 `collect_filter_skip` 一份。"""
    keep = make_collect_filter(SyncPolicy(forward_all=True))
    for name in _CROSS_MACHINE_JUNK + ["CF_HDROP", "CF_UNICODETEXT", "Rich Text Format"]:
        hard_skip = cb.collect_filter_skip(0, name)
        assert keep(0, name, "other") is not hard_skip, name


def test_collect_filter_still_keeps_hdrop_signal():
    """`CF_HDROP` 必须放行 —— 它是接收端判断"这是一次文件复制"的唯一信号。"""
    keep = make_collect_filter(SyncPolicy())
    assert keep(w.CF_HDROP, "CF_HDROP", "files") is True


def test_collect_filter_still_honours_user_exclude():
    """合并之后配置层的开关不能失效。"""
    keep = make_collect_filter(SyncPolicy(exclude=["^MathType"]))
    assert keep(0, "MathType 5.0 Equations", "other") is False
    assert keep(0, "PowerPoint 12.0 Shape", "other") is True


pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="parse_hdrop 需要 DROPFILES 布局")


# --------------------------------------------------------------------- 元数据 / 切分


def test_wire_metadata_includes_wire_length():
    item = {K_NAME: "CF_DIB", K_SIZE: 1000, K_CAT: CAT_IMAGE, K_ZLIB: True, "wl": 240}
    meta = _public_meta(item)
    assert meta["wl"] == 240
    assert meta[K_SIZE] == 1000


def test_wire_metadata_falls_back_to_size():
    """没有 wl 的老版本对端/条目，退化成用 size。"""
    item = {K_NAME: "x", K_SIZE: 7, K_CAT: CAT_TEXT, K_ZLIB: False}
    assert _public_meta(item)["wl"] == 7


def test_split_blob_uses_wire_length_not_original_size():
    """这是压缩场景下最容易错的地方：切分必须按压缩后的长度。"""
    a = b"AAA"  # 原始 1000，压缩后 3
    b = b"BBBBB"  # 原始 2000，压缩后 5
    meta = [
        {K_NAME: "a", K_SIZE: 1000, K_CAT: CAT_TEXT, K_ZLIB: True, "wl": 3},
        {K_NAME: "b", K_SIZE: 2000, K_CAT: CAT_TEXT, K_ZLIB: True, "wl": 5},
    ]
    parts = _split_blob(a + b, meta)
    assert parts == [b"AAA", b"BBBBB"]


def test_split_blob_handles_zero_length_entries():
    meta = [
        {K_NAME: "a", K_SIZE: 0, K_CAT: CAT_TEXT, K_ZLIB: False, "wl": 0},
        {K_NAME: "b", K_SIZE: 4, K_CAT: CAT_TEXT, K_ZLIB: False, "wl": 4},
    ]
    assert _split_blob(b"abcd", meta) == [b"", b"abcd"]


# --------------------------------------------------------------------- 编解码


def test_encode_decode_roundtrip_without_compression():
    originals = {
        "CF_UNICODETEXT": "hello 中文".encode("utf-16-le"),
        "HTML Format": b"<html></html>",
    }
    meta = []
    blob = b""
    for name, data in originals.items():
        item = {K_NAME: name, K_SIZE: len(data), K_CAT: CAT_TEXT, K_ZLIB: False, "wl": len(data), "data": data}
        meta.append(_public_meta(item))
        blob += data

    items = _decode_items(meta, _split_blob(blob, meta))
    got = {i.name: i.data for i in items}
    assert got == originals


def test_encode_decode_roundtrip_with_compression():
    original = b"A" * 5000
    packed = zlib.compress(original, 6)
    item = {
        K_NAME: "CF_DIB",
        K_SIZE: len(original),
        K_CAT: CAT_IMAGE,
        K_ZLIB: True,
        "wl": len(packed),
        "data": packed,
    }
    items = _decode_items([_public_meta(item)], _split_blob(packed, [_public_meta(item)]))
    assert len(items) == 1
    assert items[0].data == original
    assert items[0].category == CAT_IMAGE


def test_decode_skips_corrupt_compressed_entry():
    """单个格式解压失败不能拖垮整次同步。"""
    meta = [
        {K_NAME: "good", K_SIZE: 3, K_CAT: CAT_TEXT, K_ZLIB: False, "wl": 3},
        {K_NAME: "bad", K_SIZE: 100, K_CAT: CAT_TEXT, K_ZLIB: True, "wl": 4},
    ]
    blob = b"abc" + b"\x00\x01\x02\x03"
    items = _decode_items(meta, _split_blob(blob, meta))
    assert [i.name for i in items] == ["good"]


def test_decode_tolerates_length_mismatch():
    """长度对不上时按实际长度使用，而不是直接丢弃。"""
    meta = [{K_NAME: "x", K_SIZE: 999, K_CAT: CAT_TEXT, K_ZLIB: False, "wl": 3}]
    items = _decode_items(meta, [b"abc"])
    assert items[0].data == b"abc"


def test_decode_skips_empty_names_and_data():
    meta = [
        {K_NAME: "", K_SIZE: 2, K_CAT: CAT_TEXT, K_ZLIB: False, "wl": 2},
        {K_NAME: "empty", K_SIZE: 0, K_CAT: CAT_TEXT, K_ZLIB: False, "wl": 0},
    ]
    assert _decode_items(meta, [b"ab", b""]) == []


# --------------------------------------------------------------------- CF_HDROP


def make_hdrop(paths, wide=True):
    """构造一份真实的 CF_HDROP 字节流，用于测试解析。"""
    header_size = 20  # DROPFILES: DWORD + POINT(8) + BOOL + BOOL
    if wide:
        payload = b"".join(p.encode("utf-16-le") + b"\x00\x00" for p in paths) + b"\x00\x00"
    else:
        payload = b"".join(p.encode("mbcs") + b"\x00" for p in paths) + b"\x00"
    header = struct.pack("<IiiII", header_size, 0, 0, 0, 1 if wide else 0)
    return header + payload


def test_parse_hdrop_wide():
    paths = [r"C:\tmp\a.txt", r"C:\tmp\中文 名字.txt"]
    assert parse_hdrop(make_hdrop(paths, wide=True)) == paths


def test_parse_hdrop_ansi():
    paths = [r"C:\tmp\a.txt"]
    assert parse_hdrop(make_hdrop(paths, wide=False)) == paths


def test_parse_hdrop_single_file():
    assert parse_hdrop(make_hdrop([r"C:\x.bin"])) == [r"C:\x.bin"]


def test_parse_hdrop_ignores_garbage():
    assert parse_hdrop(b"") == []
    assert parse_hdrop(b"\x00" * 4) == []
    # 偏移指向结构内部，应该被纠正为默认值而不是崩溃
    bad = struct.pack("<IiiII", 4, 0, 0, 0, 1) + "x".encode("utf-16-le") + b"\x00\x00"
    assert parse_hdrop(bad) == ["x"]


def test_parse_hdrop_real_roundtrip_via_clipboard():
    """用真实的剪贴板写一份 CF_HDROP 再解析回来（需要 Windows）。"""
    from tests.test_win_clipboard import _snapshot_then_restore

    from netclip.win import clipboard as cb

    def body():
        paths = [r"C:\Windows\notepad.exe", r"C:\Windows\win.ini"]
        existing = [p for p in paths if os.path.isfile(p)]
        if not existing:
            pytest.skip("测试机上找不到用于构造 CF_HDROP 的文件")
        cb.write_formats(
            [cb.FormatBlob(name="CF_HDROP", category="files", data=make_hdrop(existing))]
        )
        snap = cb.capture(max_per_format=1024 * 1024)
        item = snap.by_name("CF_HDROP")
        assert item is not None
        assert parse_hdrop(item.data) == existing

    _snapshot_then_restore(body)


# --------------------------------------------------------------------- HTML 内联


def test_inline_html_no_file_refs_is_unchanged():
    html = b"<html><body><b>hi</b></body></html>"
    assert inline_html_local_refs(html) == html


def test_inline_html_replaces_local_file_with_data_uri():
    with tempfile.TemporaryDirectory() as tmp:
        png = os.path.join(tmp, "t.png")
        with open(png, "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
        url = "file:///" + png.replace("\\", "/")
        html = ('<img src="%s"/>' % url).encode("utf-8")
        out = inline_html_local_refs(html)
        assert b"data:image/png;base64," in out
        assert b"file:///" not in out


def test_inline_html_keeps_missing_file_untouched():
    """文件不存在时原样保留 —— 不做事比做错事好。"""
    html = b'<img src="file:///C:/definitely/not/here/nope.png"/>'
    assert inline_html_local_refs(html) == html


def test_inline_html_ignores_http_refs():
    html = b'<img src="https://example.com/a.png"/>'
    assert inline_html_local_refs(html) == html


def test_inline_html_handles_href():
    with tempfile.TemporaryDirectory() as tmp:
        css = os.path.join(tmp, "s.css")
        with open(css, "wb") as fh:
            fh.write(b"body{}")
        url = "file:///" + css.replace("\\", "/")
        html = ("<link href='%s'/>" % url).encode("utf-8")
        out = inline_html_local_refs(html)
        assert b"data:text/css;base64," in out


# --------------------------------------------------- CF_HTML 头里的偏移必须重算

def _make_cf_html(body: bytes) -> bytes:
    """拼一份结构和真实剪贴板一致的 CF_HTML（头 + 正文，偏移 10 位零填充）。"""
    header_size = (
        b"Version:1.0\r\nStartHTML:0000000000\r\nEndHTML:0000000000\r\n"
        b"StartFragment:0000000000\r\nEndFragment:0000000000\r\n"
    )
    start_html = len(header_size)
    start_fragment = start_html + body.index(b"<!--StartFragment-->") + len(b"<!--StartFragment-->")
    end_fragment = start_html + body.index(b"<!--EndFragment-->")
    header = (
        b"Version:1.0\r\nStartHTML:%010d\r\nEndHTML:%010d\r\n"
        b"StartFragment:%010d\r\nEndFragment:%010d\r\n"
        % (start_html, start_html + len(body), start_fragment, end_fragment)
    )
    assert len(header) == len(header_size), "头的长度必须固定，否则偏移会整体平移"
    return header + body


def _cf_html_offset(data: bytes, name: str) -> int:
    import re

    return int(re.search((name + r":(\d+)").encode(), data).group(1))


def test_cf_html_fixture_is_self_consistent():
    """先确认测试用的夹具本身是对的 —— 否则后面的断言没有意义。"""
    body = b"<html><body><!--StartFragment--><p>x</p><!--EndFragment--></body></html>"
    data = _make_cf_html(body)
    assert _cf_html_offset(data, "EndHTML") == len(data)
    assert data[_cf_html_offset(data, "StartFragment") : _cf_html_offset(data, "EndFragment")] == b"<p>x</p>"


def test_inlining_fixes_the_cf_html_offsets():
    """**回归测试**：内联图片改了正文，就必须重算头里的偏移量。

    真机上踩到的现象：WPS 复制一个带公式的段落，对端粘出来**变成了图片**。
    抓到的直接证据是同一个剪贴板在两台机器上的两份 `clip_probe.py` 输出::

        发送端 HTML Format: 33411 字节   头里 EndHTML:0000033411   <- 自洽
        接收端 HTML Format: 96570 字节   头里 EndHTML:0000033411   <- 早就不是这个数了

    原因就是内联图片把正文改长了、偏移量却还停在改写前。消费者（WPS / 资源管理器）
    照着旧偏移去切片段，切到断的片段就当作坏数据 —— 于是**放弃 HTML 回退成图片**。
    """
    with tempfile.TemporaryDirectory() as tmp:
        png = os.path.join(tmp, "p.png")
        with open(png, "wb") as fh:
            fh.write(bytes(range(256)) * 8)
        url = "file:///" + png.replace("\\", "/")
        body = (
            b"<html><body><!--StartFragment--><p>formula</p>"
            b'<img src="' + url.encode("utf-8") + b'">'
            b"<!--EndFragment--></body></html>"
        )
        data = _make_cf_html(body)
        before = len(data)

        out = inline_html_local_refs(data)

        assert len(out) > before, "图片没被内联，这个用例就没意义了"
        assert b"data:image/png;base64," in out
        assert _cf_html_offset(out, "EndHTML") == len(out), "EndHTML 必须等于改写后的总长度"
        assert _cf_html_offset(out, "StartHTML") == _cf_html_offset(data, "StartHTML"), (
            "用同样的宽度写回去，正文起点不该移动"
        )
        #: 片段边界要能就地切出正文 —— 这正是消费者会做的事
        fragment = out[
            _cf_html_offset(out, "StartFragment") : _cf_html_offset(out, "EndFragment")
        ]
        assert fragment.startswith(b"<p>formula</p>")
        assert b"base64," in fragment


def test_fixup_ignores_data_that_is_not_cf_html():
    """不是 CF_HTML 的负载一律不碰 —— 别把别人的格式改坏。"""
    from netclip.clipsync.bridge import fixup_cf_html_offsets

    plain = b"<html><body>StartHTML:0000000042</body></html>"
    assert fixup_cf_html_offsets(plain) == plain


def test_fixup_leaves_offsets_alone_when_the_width_would_grow():
    """偏移位宽不够时宁可不改 —— 改坏头比不改更糟。"""
    from netclip.clipsync.bridge import fixup_cf_html_offsets

    #: `EndHTML` 只有 1 位，而真实长度远不止 9 —— 重写会把头撑长、正文起点平移，
    #: 所以这里必须原样返回。
    data = b"Version:1.0\r\nEndHTML:5\r\n<html><body>x</body></html>"
    assert fixup_cf_html_offsets(data) == data


def test_link_source_is_never_excluded_by_default():
    """默认不排除 `Link Source` 系列 —— 这是**保守默认**，不是因果结论。

    真机 A/B（同一个剪贴板，两台各跑一次 `tools/clip_probe.py`）：

      | 排除 | 接收端 | 结果 |
      |---|---|---|
      | 三条（旧默认，含 `^Link Source.*`） | 13 种 | 菜单灰，**完全粘不了** |
      | 一条不排 | 18 种 | 能粘，但退成图片 |

    **注意这两组之间不止一个变量不同**：第一组同时丢了 `DataObject` /
    `Ole Private Data` / `Link Source` / `Link Source Descriptor`，
    所以"菜单灰"到底该怪谁**没有定论**（字节数 1087 = 90.1-89.0 KB 只说明丢的就是这四个）。

    这里断言"不排除"的理由是**保守**：除非有证据证明某个格式必须排除，
    否则不该替消费者拿掉东西 —— 旧默认那三条就是没证据就排掉的。
    """
    from netclip.config import ClipboardFormatsConfig, Config
    from netclip.clipsync.factory import build_policy

    assert not any("Link Source" in p for p in ClipboardFormatsConfig().exclude), (
        "没有证据表明 Link Source 必须排除 —— 保守起见不要排"
    )
    policy = build_policy(Config())
    for name in ("Link Source", "Link Source Descriptor"):
        ok, reason = policy.format_allowed(name, "ole")
        assert ok, "%s 被默认挡掉了（%s）" % (name, reason)


# ------------------------------------------------------- 携带路径的格式必须挡掉

class _Blob:
    """只需要 name 就够了，`writable_formats` 不碰其它字段。"""

    def __init__(self, name):
        self.name = name


def test_path_bearing_formats_are_never_written():
    """**回归测试**：固化了对端绝对路径的格式一律不许写进本机剪贴板。

    真机上用户报"复制文件到主机粘贴，提示在 temp 文件夹中找不到文件"。
    根因：剪贴板里既有 `Shell IDList Array`（内部是**从机**的绝对 PIDL，
    路径里正好有 `...\\Documents\\Python\\Temp\\netclip\\start.ps1`），
    又有指向本机暂存文件的 `CF_HDROP`。
    **Explorer 粘贴时按保真度挑格式，`Shell IDList Array` 优先级高于 `CF_HDROP`**，
    于是它照着从机的路径去找 —— 当然找不到。
    """
    names = [
        # 用户日志里实际出现的格式，一个都不能漏
        "Shell IDList Array",
        "Shell Object Offsets",
        "DataObjectAttributes",
        "DataObjectAttributesRequiringElevation",
        "Preferred DropEffect",
        "AsyncFlag",
        "CF_HDROP",
        "FileName",
        "FileNameW",
        "UIDisplayed",
        "DropDescription",
    ]
    kept = {b.name for b in writable_formats([_Blob(n) for n in names])}
    for banned in ("Shell IDList Array", "Shell Object Offsets", "CF_HDROP", "FileName", "FileNameW"):
        assert banned not in kept, "%s 携带对端路径，必须挡掉" % banned
    #: 拖放**簿记**格式同样不能写：它们只对"源和目标在同一次拖放会话里"有意义。
    #: 真机上 `AsyncFlag` 被截断后按 DWORD 读会让 Shell 转沙漏空等；
    #: `DropDescription` 是定长结构，截断后按完整长度读会**越界崩溃**。
    #: 接收端落地时自己写一份干净的 `Preferred DropEffect=COPY`。
    for banned in ("Preferred DropEffect", "DataObjectAttributes", "DropDescription", "AsyncFlag", "UIDisplayed"):
        assert banned not in kept, "%s 是拖放簿记格式，不能跨机转发" % banned


def test_writable_formats_keeps_ordinary_content():
    """普通内容格式（文本/图片/HTML）不能被误伤。"""
    names = ["CF_UNICODETEXT", "HTML Format", "CF_DIBV5", "Rich Text Format"]
    kept = [b.name for b in writable_formats([_Blob(n) for n in names])]
    assert kept == names


def test_path_bearing_set_covers_the_real_world_formats():
    """这条名单来自真机日志，不能被随意删减。"""
    from netclip.win.clipboard import PATH_BEARING_FORMATS

    assert "Shell IDList Array" in PATH_BEARING_FORMATS
    assert "Shell Object Offsets" in PATH_BEARING_FORMATS
    assert "CF_HDROP" in PATH_BEARING_FORMATS


def test_hdrop_is_still_sent_but_shell_idlist_is_not():
    """**回归测试**：`CF_HDROP` 必须继续发，`Shell IDList Array` 必须不发。

    发送端靠"帧里有没有 CF_HDROP"来告诉接收端"这是一次文件复制"
    （接收端自己会把内容丢掉，只用本地路径重建）。早期把 CF_HDROP 也一起过滤掉了，
    结果接收端不知道要保留文件，剪贴板帧一到就把刚写好的文件剪贴板抹掉 ——
    现象就是"文件传过来了但粘贴不了"。

    而 `Shell IDList Array` 内部是**源机器**的绝对 PIDL，它对 Explorer 的
    优先级还高于 CF_HDROP，必须留在发送端不发。
    """
    from netclip.win.clipboard import SHELL_PATH_FORMATS, collect_filter_skip

    assert not collect_filter_skip(0, "CF_HDROP"), "CF_HDROP 要发（它是文件复制的信号）"
    assert collect_filter_skip(0, "Shell IDList Array"), "Shell IDList Array 不能发"
    assert collect_filter_skip(0, "FileNameW")
    assert "CF_HDROP" not in SHELL_PATH_FORMATS


def test_remote_clipboard_writes_are_serialized():
    """**回归测试**：写对端剪贴板必须**串行** —— 只能有一个写入者。

    系统剪贴板是全局独占资源：`EmptyClipboard` + `SetClipboardData` 必须由一个
    写入者从头做到尾。原先 `_commit` 每收到一帧就 `threading.Thread(...).start()`，
    实测（这个用例复现的就是它）连着送 8 帧能让 **5 个线程同时抢同一个剪贴板** ——
    互相把对方刚写进去的格式清掉。用户看到的是"**粘贴菜单是灰的**"：粘贴的那一刻，
    剪贴板正好被另一个线程清空、或者只写了一半。

    真机上触发这个的条件很容易满足：WPS 复制一次会连发十几帧（真机日志里同一份内容
    2.5 秒内发了 3 次，还有 13/12/7 种格式的不同阶段）。
    """
    import threading
    import time

    import netclip.clipsync.bridge as bridge_mod
    from netclip.clipsync.bridge import ClipboardSync

    concurrent = 0
    peak = 0
    calls = 0
    workers = set()
    guard = threading.Lock()

    def fake_write(items, **kwargs):
        nonlocal concurrent, peak, calls
        with guard:
            concurrent += 1
            calls += 1
            peak = max(peak, concurrent)
            workers.add(threading.current_thread().name)
        time.sleep(0.15)  # 真实写入要抢锁/退避重试，这里只需制造重叠窗口
        with guard:
            concurrent -= 1
        result = _Blob("(result)")
        result.sequence = calls
        return result

    monkeypatch_original = bridge_mod.cb.write_formats
    bridge_mod.cb.write_formats = fake_write
    sync = _make_sync()
    sync._watcher = None  # 不需要回声抑制，只测写入并发
    try:
        data = "公式".encode("utf-16-le")
        meta = [
            {
                K_NAME: "CF_UNICODETEXT",
                K_SIZE: len(data),
                K_CAT: CAT_TEXT,
                K_ZLIB: False,
            }
        ]
        for index in range(8):
            sync._commit("id%d" % index, index, meta, [data], source="test")
            time.sleep(0.02)

        deadline = time.time() + 5.0
        while time.time() < deadline:
            with guard:
                if concurrent == 0 and calls > 0:
                    break
            time.sleep(0.05)

        assert peak == 1, "有 %d 个线程在同时写剪贴板，它们会互相擦除" % peak
        assert workers == {"netclip-clip-apply"}, "写入者应该是那个唯一的线程: %s" % workers
        assert calls < 8, "中间态应该被后到的帧顶掉，实际写了 %d 次" % calls
    finally:
        bridge_mod.cb.write_formats = monkeypatch_original
        sync.stop(timeout=1.0)


def test_built_file_clipboard_writes_no_shell_idlist():
    """**回归测试**：给本地文件拼剪贴板时**只能**写这两个格式。

    真机上踩过两次，方向相反，所以这里正反都钉住：

      * **不写 `Shell IDList Array`。** 一度怀疑"只写 CF_HDROP 粘贴不了"，于是用
        本地 PIDL 重建 `CIDA` —— 结果**资源管理器的粘贴直接崩掉重启**（对照实验：
        加它之前只是"什么都不发生"，不崩）。所以这条路是明确废弃的。
      * **必须写 `Preferred DropEffect = COPY`（4 字节）** —— 缺了它 Shell 会
        拒绝执行粘贴，表现为按 Ctrl+V 毫无反应。

    这个函数原先**一个用例都没有**，所以它里面引用了一个根本不存在
    （`SHELL_IDLIST_FORMAT`）的名字也没人发现 —— 一直要等用户真的复制一个文件过来，
    才会在"文件已就位、正在放入剪贴板"那一步抛 NameError，文件也就粘贴不了。
    """
    import os
    import tempfile

    from netclip.win import clipboard as cb

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "待粘贴的文件.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("x")

        items = cb.build_file_clipboard_items([path])
        names = [item.name for item in items]

        assert names == [cb.HDROP_FORMAT, cb.PREFERRED_DROPEFFECT_FORMAT], (
            "文件剪贴板只该有这两个格式，实际: %s" % names
        )
        assert "Shell IDList Array" not in names, "不要自己造 Shell IDList Array（会崩资源管理器）"

        hdrop = next(item for item in items if item.name == cb.HDROP_FORMAT)
        assert cb.parse_hdrop_paths(hdrop.data) == [path], "路径要能原样解析回来"

        effect = next(item for item in items if item.name == cb.PREFERRED_DROPEFFECT_FORMAT)
        assert len(effect.data) == 4, "Preferred DropEffect 必须是 4 字节 DWORD"
        assert int.from_bytes(effect.data, "little") == cb.DROPEFFECT_COPY


# ------------------------------------------------- 文件落地与剪贴板帧的时序


class _FakeClipboard:
    """拦住 `write_formats`，把"实际写了哪些格式"记下来。"""

    def __init__(self):
        self.written = []

    def install(self, monkeypatch):
        import netclip.clipsync.bridge as bridge_mod

        def fake_write(items, **kwargs):
            self.written.append([i.name for i in items])
            blob = _Blob("(result)")
            blob.sequence = 1
            blob.describe = lambda: ""
            return blob

        monkeypatch.setattr(bridge_mod.cb, "write_formats", fake_write)
        #: 这一组用例断言的是**内置 CF_HDROP 写法**写出来的内容，所以把 PowerShell
        #: 写法钉成"不可用"，强制走回退分支。"PowerShell 优先"这件事另有专门用例
        #: （`test_powershell_writer_is_preferred_over_builtin`）盯着。
        monkeypatch.setattr(
            bridge_mod.cb, "write_file_clipboard_via_powershell", lambda paths, **kwargs: None
        )


def _make_sync():
    from netclip.clipsync.bridge import ClipboardSync
    from netclip.clipsync.factory import build_policy
    from netclip.config import Config

    return ClipboardSync(policy=build_policy(Config()), send=lambda *a: None)


def test_clip_frame_arriving_after_files_keeps_the_local_hdrop(monkeypatch):
    """**回归测试**：文件先落地、剪贴板帧后到 —— 不能把文件剪贴板抹掉。

    发送端的顺序是"先发文件帧、再发剪贴板帧"（真机日志里 `开始发送 1 个文件`
    在 `已发送剪贴板` 之前）。所以接收端会在文件落地**之后**才跑 `_apply_remote`，
    那时它重写剪贴板时必须把本地 CF_HDROP 一起带上，否则用户看到的就是
    "文件传过来了但粘贴不了"。
    """
    fake = _FakeClipboard()
    fake.install(monkeypatch)
    sync = _make_sync()

    #: 文件通道先落地
    sync.on_files_ready([r"C:\tmp\staged\a.ps1"])
    assert "CF_HDROP" in fake.written[-1]

    #: 紧接着剪贴板帧到达（带 CF_HDROP 表示这是一次文件复制）
    items = [_Blob("CF_UNICODETEXT"), _Blob("CF_HDROP")]
    sync._apply_remote(0, items)

    assert "CF_HDROP" in fake.written[-1], "剪贴板帧到达后仍然必须能粘贴文件"
    assert "CF_UNICODETEXT" in fake.written[-1], "普通格式也不能丢"


def test_clip_frame_without_files_does_not_inject_stale_hdrop(monkeypatch):
    """不是文件复制的剪贴板帧，不能凭空塞进上次留下的文件路径。"""
    fake = _FakeClipboard()
    fake.install(monkeypatch)
    sync = _make_sync()

    sync.on_files_ready([r"C:\tmp\staged\a.ps1"])
    sync._apply_remote(0, [_Blob("CF_UNICODETEXT")])

    assert "CF_HDROP" not in fake.written[-1], "普通文本复制不该带上过期的文件"


def test_pure_file_frame_does_not_rewrite_the_clipboard(monkeypatch):
    """**回归测试**：纯文件帧到达后，本机不能再写一遍剪贴板。

    真机故障：主机粘贴"沙漏一转啥也没粘出来"。主机 `--dump-formats` 显示失败时是：

        CF_HDROP(本机路径) + FileGroupDescriptorW + Preferred DropEffect=COPY

    那个 `Preferred DropEffect` 是**内置**写法的指纹，而 `on_files_ready` 走的是
    PowerShell/OLE 写法（会写 `DataObject` / `Ole Private Data`，没有它）。
    说明 `_apply_remote` 在文件落地之后**又写了一遍**，把干净的 OLE 剪贴板降级了，
    还带进了对端的 `FileGroupDescriptorW`。
    """
    import netclip.clipsync.bridge as bridge_mod

    fake = _FakeClipboard()
    fake.install(monkeypatch)
    monkeypatch.setattr(
        bridge_mod.cb, "write_file_clipboard_via_powershell", lambda paths, **kwargs: 99
    )

    sync = _make_sync()
    sync.on_files_ready([r"C:\tmp\staged\a.ps1"])
    written_after_files = list(fake.written)

    #: 发送端现在发出来的就是这种纯文件帧（日志：`1 种格式 -> CF_HDROP`）
    sync._apply_remote(0, [_Blob("CF_HDROP")])

    assert fake.written == written_after_files, "纯文件帧到达后不该再写剪贴板"


def test_virtual_file_formats_are_blocked_on_both_ends():
    """OLE「虚拟文件」格式族两端都必须挡掉。

    `FileGroupDescriptorW` 只**描述**文件，真正的字节要靠 `FileContents` 在粘贴时
    由源数据对象现场渲染。跨机转发留下的是一份"有描述、没内容"的剪贴板 ——
    资源管理器照着描述去要文件流、什么都拿不到，于是**转沙漏空等**。

    真机证据：主机 `--dump-formats` 里，失败的那次比成功的那次**只多这一个格式**。
    """
    for name in ("FileGroupDescriptorW", "FileGroupDescriptor", "FileContents"):
        assert name in cb.NEVER_WRITE_FORMATS, "接收端没挡: %s" % name
        assert cb.collect_filter_skip(0, name) is True, "发送端没挡: %s" % name
        assert writable_formats([_Blob(name)]) == [], "writable_formats 没挡: %s" % name


class _FakeWatcher:
    """只记录调用顺序的监听器替身，不做真的抑制。"""

    def __init__(self, order):
        self._order = order

    def suppress_next(self, window=None):
        self._order.append("suppress")

    def expect_sequence(self, sequence, window=None):
        self._order.append("expect")

    def discard_pending(self):
        self._order.append("discard")

    def clear_suppression(self):
        self._order.append("clear")


def test_file_clipboard_suppresses_before_writing(monkeypatch):
    """**回归测试**：回声抑制必须在**写剪贴板之前**武装。

    真机故障：从机复制文件，两端**无限循环** —— 传过去、写剪贴板、又传回来……

    根因是时序：`on_files_ready` 原来是**写完才** `expect_sequence()`，而
    PowerShell 是**子进程**（冷启动 0.5~1.5 秒）。这段时间里 Windows 早就把
    `WM_CLIPBOARDUPDATE` 送到消息窗口并处理完了，`_pending` 被置位 ——
    同步循环于是把刚写进去的内容当成"用户复制了新内容"，把文件发回对端，
    对端收下、写剪贴板、再发回来。两边日志都"正常"，但永远停不下来。

    抑制（`_is_suppressed`）是在消息窗口处理通知的**那一刻**判定的，
    所以必须在写之前就武装好，光靠事后 `expect_sequence()` 拦不住。
    """
    import netclip.clipsync.bridge as bridge_mod

    order: list = []
    seen: dict = {}
    fake = _FakeClipboard()
    fake.install(monkeypatch)

    sync = _make_sync()
    sync._watcher = _FakeWatcher(order)  # noqa: SLF001 - 注入替身

    def fake_powershell(paths, **kwargs):
        #: 在**写入进行中**这个时刻取样，才能看出两件事有没有提前做好。
        seen["suppress_armed"] = "suppress" in order
        seen["writing_flag"] = sync._writing.is_set()  # noqa: SLF001
        seen["pending_discarded"] = "discard" in order
        order.append("write")
        return 7

    monkeypatch.setattr(bridge_mod.cb, "write_file_clipboard_via_powershell", fake_powershell)

    sync.on_files_ready([r"C:\tmp\staged\a.ps1"])

    assert "suppress" in order, "没有武装抑制: %s" % order
    assert order.index("suppress") < order.index("write"), (
        "抑制必须在写之前武装，否则通知会先一步到达、被当成用户复制: %s" % order
    )
    assert seen.get("suppress_armed"), "写入开始时抑制还没武装好"
    assert seen.get("pending_discarded"), "写入前没有清掉已排队的变更通知"
    assert seen.get("writing_flag"), "写入期间 _writing 标记必须置位（与时间无关的那道保险）"
    assert not sync._writing.is_set(), "写完之后 _writing 标记必须清掉"  # noqa: SLF001


def test_peer_files_are_never_published_back(monkeypatch):
    """收到的文件绝不能又被当成"用户复制"发回对端。

    真机故障：文件在两台机器之间**无限互传**。前面那几道防线（`_writing` 标记、
    回声抑制）治的都是**时序** —— 别把回声发出去；这一条治的是**语义**：
    收到对端的文件落在暂存区里，把它们放回剪贴板之后触发的那次"复制"根本不是
    用户的新动作。

    两道独立防线是值得的：无限互传会把磁盘灌满、把网络打满，
    而且**两边日志全都是正常的**，很难察觉。
    """
    import netclip.clipsync.bridge as bridge_mod
    from netclip.clipsync.policy import ACTION_SKIP

    staging_path = r"C:\Users\x\AppData\Local\netclip\staging\desktop-master\abc\a.csv"
    snapshot = cb.ClipboardSnapshot(
        sequence=1,
        items=[
            cb.FormatBlob(
                name=cb.HDROP_FORMAT,
                category=cb.CAT_FILES,
                data=cb.build_hdrop([staging_path]),
            )
        ],
    )
    monkeypatch.setattr(bridge_mod.cb, "capture", lambda **kwargs: snapshot)

    class _Handler:
        def all_from_peer(self, paths):
            return True

        def plan(self, *_args, **_kwargs):
            raise AssertionError("来自对端的文件不该进入传输计划")

    sync = _make_sync()
    sync.files_handler = _Handler()

    decision = sync.publish_local()

    assert decision.action == ACTION_SKIP
    assert "对端" in decision.reason


def test_local_files_are_still_published(monkeypatch):
    """反过来也要成立：本机自己的文件必须照常发出去，别把功能一并挡掉。"""
    import netclip.clipsync.bridge as bridge_mod

    local_path = r"C:\Users\x\Documents\mine.csv"
    snapshot = cb.ClipboardSnapshot(
        sequence=1,
        items=[
            cb.FormatBlob(
                name=cb.HDROP_FORMAT,
                category=cb.CAT_FILES,
                data=cb.build_hdrop([local_path]),
            )
        ],
    )
    monkeypatch.setattr(bridge_mod.cb, "capture", lambda **kwargs: snapshot)

    planned: list = []

    class _Handler:
        def all_from_peer(self, paths):
            return False

        def plan(self, paths, total):
            planned.append(list(paths))
            return None  # 不真的传，只看有没有走到这一步

    sync = _make_sync()
    sync.files_handler = _Handler()

    sync.publish_local()

    assert planned == [[local_path]], "本机文件的传输计划没有被触发: %s" % planned


def test_powershell_writer_is_preferred_over_builtin(monkeypatch):
    """PowerShell 写法可用时，**不该**再用内置写法写第二遍。

    两套都写一遍会让剪贴板序号跳两次，而回声抑制只压了一次
    （`expect_sequence` 是按写入后的序号精确抑制的），对端可能收到重复内容。
    """
    import netclip.clipsync.bridge as bridge_mod

    fake = _FakeClipboard()
    fake.install(monkeypatch)
    seen: list = []

    def fake_powershell(paths, **_kwargs):
        seen.append(list(paths))
        return 4242

    monkeypatch.setattr(bridge_mod.cb, "write_file_clipboard_via_powershell", fake_powershell)

    sync = _make_sync()
    sync.on_files_ready([r"C:\tmp\staged\a.ps1"])

    assert seen == [[r"C:\tmp\staged\a.ps1"]], "没调用 PowerShell 写法"
    assert fake.written == [], "PowerShell 成功时不该再走内置写法"
    assert sync._last_sent_seq == 4242  # noqa: SLF001 - 回声抑制依赖这个序号


def test_powershell_failure_falls_back_to_builtin_writer(monkeypatch):
    """PowerShell 失败时必须回退，不能让"粘贴不了"变成"剪贴板里什么都没有"。"""
    import netclip.clipsync.bridge as bridge_mod

    fake = _FakeClipboard()
    fake.install(monkeypatch)
    monkeypatch.setattr(
        bridge_mod.cb, "write_file_clipboard_via_powershell", lambda paths, **kwargs: None
    )

    sync = _make_sync()
    sync.on_files_ready([r"C:\tmp\staged\a.ps1"])

    assert fake.written, "回退分支没有写剪贴板"
    assert "CF_HDROP" in fake.written[-1]

def test_exclude_by_process_selects_the_rule_for_the_copying_process():
    """按"复制来源进程"切换丢弃列表。

    真机枚举了 3 条正则的 8 种组合，结论是**没有任何一组静态 `exclude` 能两边都满足**：

      | Ole Private Data | PPT（wpp.exe） | Word（winword.exe） |
      |---|---|---|
      | 排掉 | 可编辑 ✅ | 粘不了 ❌ |
      | 原样转发 | 退成图片 ❌ | 可以 ✅ |

    所以判据只能用**进程名**（剪贴板所有者）—— 那是直接可观测的，不用猜格式。
    """
    from netclip.config import Config
    from netclip.clipsync.bridge import ClipboardSync
    from netclip.clipsync.factory import build_policy

    sync = ClipboardSync(
        policy=build_policy(Config()),
        send=lambda *a: None,
        exclude_by_process=[
            {"process": "wpp.exe", "exclude": [r"^Ole Private Data$"]},
            {"process": "WinWord.EXE", "exclude": []},
        ],
    )

    assert [p.pattern for p in sync._exclude_for_owner("wpp.exe")] == [  # noqa: SLF001
        r"^Ole Private Data$"
    ]
    #: 空列表 = 显式"什么都不排"（Word 要留着 Ole Private Data）
    assert sync._exclude_for_owner("WINWORD.EXE") == []  # noqa: SLF001
    #: 没匹配到 = None，表示"用全局 exclude"，**不是**"什么都不排"
    assert sync._exclude_for_owner("chrome.exe") is None  # noqa: SLF001
    assert sync._exclude_for_owner("") is None  # noqa: SLF001
