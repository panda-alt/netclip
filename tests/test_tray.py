"""托盘相关的单元测试。

**不创建真实托盘图标** —— 那会在测试机上留下图标、并且依赖桌面会话。
真实托盘由 `python -m netclip.selftest tray` 手测覆盖。

这里测的是两件容易出错的事：
  1. 状态 -> 提示文本/图标颜色的映射（用户看到的东西）；
  2. 菜单命令 -> 会话动作的分发（点了菜单到底做了没做）。
"""

from __future__ import annotations

import sys

import pytest

from netclip.win.tray import (
    COLOR_ACTIVE,
    COLOR_LOCAL,
    COLOR_PAUSED,
    COLOR_REMOTE,
    Command,
    TrayState,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="托盘只支持 Windows")


# --------------------------------------------------------------------- 状态映射


def test_default_state_is_waiting():
    state = TrayState()
    assert "等待对端" in state.tooltip()
    assert state.color() == COLOR_LOCAL


def test_connected_state_is_green():
    state = TrayState(input_up=True, clip_up=True, file_up=True, peer="desk-b")
    text = state.tooltip()
    assert "已连接" in text
    assert "desk-b" in text
    assert "I" in text and "C" in text and "F" in text
    assert state.color() == COLOR_ACTIVE


def test_remote_state_is_highlighted():
    """光标在对端是最需要一眼看出来的状态 —— 用户会疑惑"鼠标怎么不动了"。"""
    state = TrayState(input_up=True, remote=True)
    assert "光标在对端" in state.tooltip()
    assert state.color() == COLOR_REMOTE


def test_paused_state_wins_over_others():
    """暂停的优先级最高：即使通道还连着、光标还在对端，也要显示成暂停。"""
    state = TrayState(enabled=False, input_up=True, remote=True)
    assert "已暂停" in state.tooltip()
    assert state.color() == COLOR_PAUSED


def test_channel_indicators_show_missing_channels():
    state = TrayState(input_up=True, clip_up=False, file_up=True)
    text = state.tooltip()
    assert "I-F" in text


def test_detail_line_is_appended():
    state = TrayState(input_up=True, detail="剪贴板 ↑3 ↓1")
    assert "剪贴板 ↑3 ↓1" in state.tooltip()


def test_tooltip_respects_127_char_limit():
    """系统托盘提示框上限是 127 个字符，超了会被截断；我们自己先截干净，
    避免出现半个汉字或者半个状态词。"""
    state = TrayState(input_up=True, peer="x" * 200, detail="y" * 200)
    assert len(state.tooltip()) <= 127


def test_menu_labels_reflect_state():
    assert TrayState(enabled=True).menu_labels()[1] == "暂停共享"
    assert TrayState(enabled=False).menu_labels()[1] == "恢复共享"
    assert "运行中" in TrayState(enabled=True).menu_labels()[0]
    assert "已暂停" in TrayState(enabled=False).menu_labels()[0]
    assert "对端" in TrayState(enabled=True, remote=True).menu_labels()[0]


# --------------------------------------------------------------------- 命令分发


class FakeRouter:
    def __init__(self):
        self.enabled = True
        self.mode = type("M", (), {"value": "local"})()
        self.toggles = 0
        self.recaptures = 0

    def toggle_enabled(self):
        self.toggles += 1
        self.enabled = not self.enabled
        return self.enabled

    def recapture(self, reason=""):
        self.recaptures += 1


class FakeManager:
    def __init__(self):
        self.state = type("S", (), {"peer_name": "desk-b"})()
        self._up = {"input": True, "clip": True, "file": False}

    def is_up(self, name):
        return self._up.get(name, False)


class FakeStaging:
    root = "C:/nonexistent-staging-root-for-test"
    peer = "peer"


class FakeFiles:
    def __init__(self):
        self.staging = FakeStaging()
        self.stats = {"sent_files": 2, "recv_files": 1}

    def status(self):
        return "files-status"


class FakeClipboardSync:
    def __init__(self):
        self.stats = {"sent": 3, "recv": 1}

    def status(self):
        return "clip-status"


class FakeSession:
    def __init__(self):
        self.router = FakeRouter()
        self.manager = FakeManager()
        self.files = FakeFiles()
        self.clipboard_sync = FakeClipboardSync()
        self.stopped = 0

    def toggle(self):
        return self.router.toggle_enabled()

    def recapture(self):
        self.router.recapture("tray")

    def status(self):
        return "session-status"

    def stopping(self):
        return False


class FakeWindow:
    """替掉真实的 TrayWindow：只记录调用了什么，不碰 Win32。"""

    available = True

    def __init__(self, get_state, on_command, title="netclip", letter="N"):
        self.get_state = get_state
        self.on_command = on_command
        self.notifications = []
        self.destroyed = 0
        self.created = 0

    def create(self):
        self.created += 1
        return True

    def destroy(self):
        self.destroyed += 1

    def notify(self, title, text, level="info"):
        self.notifications.append((level, text))
        return True


@pytest.fixture()
def controller(monkeypatch):
    from netclip.core import tray as core_tray

    monkeypatch.setattr(core_tray, "TrayWindow", FakeWindow)
    session = FakeSession()
    quit_calls = []
    ctl = core_tray.TrayController(session, quit_callback=lambda: quit_calls.append(1), log_path="")
    ctl.session = session
    ctl._quit_calls = quit_calls  # type: ignore[attr-defined]
    return ctl


def test_toggle_command_flips_state(controller):
    window = controller.window
    initial = controller.session.router.enabled
    window.on_command(Command.TOGGLE)
    assert controller.session.router.toggles == 1
    assert controller.session.router.enabled is not initial
    assert window.notifications, "切换后应该给用户一个提示"


def test_recapture_command(controller):
    controller.window.on_command(Command.RECAPTURE)
    assert controller.session.router.recaptures == 1


def test_quit_command_calls_callback(controller):
    controller.window.on_command(Command.QUIT)
    assert controller._quit_calls == [1]  # type: ignore[attr-defined]


def test_copy_status_puts_text_on_clipboard(controller):
    """把诊断信息一键复制到剪贴板 —— 出问题时让用户能直接把它贴出来。"""
    from netclip.win import clipboard as cb
    from tests.test_win_clipboard import _snapshot_then_restore

    def body():
        controller.window.on_command(Command.COPY_STATUS)
        snap = cb.capture(max_per_format=1024 * 1024)
        item = snap.by_name("CF_UNICODETEXT")
        assert item is not None
        text = item.data.decode("utf-16-le")
        assert "session-status" in text
        assert "clip-status" in text
        assert "files-status" in text
        assert "通道" in text  # 来自 tooltip 的第一行

    _snapshot_then_restore(body)


def test_unknown_command_does_not_raise(controller):
    controller.window.on_command(9999)  # 不该抛异常
    assert controller.session.router.toggles == 0


def test_status_text_contains_all_sections(controller):
    text = controller.status_text()
    assert "session-status" in text
    assert "clip-status" in text
    assert "files-status" in text


def test_state_reflects_session(controller):
    state = controller._build_state()  # noqa: SLF001
    assert state.enabled is True
    assert state.input_up is True
    assert state.clip_up is True
    assert state.file_up is False
    assert state.peer == "desk-b"
    assert "剪贴板 ↑3 ↓1" in state.detail
    assert "文件 ↑2 ↓1" in state.detail


def test_notify_without_tray_falls_back_to_log(monkeypatch):
    """托盘不可用（比如无桌面会话）时，通知必须退化成日志而不是丢消息或抛异常。"""
    from netclip.core import tray as core_tray

    monkeypatch.setattr(core_tray, "TrayWindow", FakeWindow)
    session = FakeSession()
    ctl = core_tray.TrayController(session, quit_callback=lambda: None)
    ctl.window.available = False
    ctl.notify("warn", "测试消息")  # 不应抛异常
    assert ctl.notifications == 1


def test_controller_create_and_destroy(controller):
    assert controller.create() is True
    assert controller.window.created == 1
    controller.destroy()
    assert controller.window.destroyed == 1


# --------------------------------------------------------------------- 图标生成


def test_icon_creation_returns_handle():
    """真的用 GDI 画一个图标。这一步在部分机器上会因 HDC 句柄类型问题失败，
    所以值得保留一个直接断言。"""
    from netclip.win.tray import make_icon_hicon
    from netclip.win import winapi as w

    hicon = make_icon_hicon(COLOR_ACTIVE)
    assert hicon, "应该能画出一个图标句柄"
    w.user32.DestroyIcon(hicon)


def test_notify_icon_data_struct_size():
    """`NOTIFYICONDATAW` 是变长结构，cbSize 算错会被系统读越界（经典崩溃点）。"""
    import ctypes

    from netclip.win import winapi as w

    assert ctypes.sizeof(w.NOTIFYICONDATAW) == 976


# --------------------------------------------------------------------- 图标解码


def _pack_ico(pixels, side, alpha=True, mask=None):
    """手工拼一个 32 位 `.ico`。

    故意不走 Pillow —— 那样测的是 Pillow，不是我们的解析器。
    `pixels` 是**自上而下**的 `(b, g, r, a)` 列表，长度 `side*side`。
    """
    import struct

    xor = bytearray()
    for y in range(side - 1, -1, -1):  # BMP 的像素行是自下而上存的
        for x in range(side):
            b, g, r, a = pixels[y * side + x]
            xor += bytes((b, g, r, a if alpha else 0))

    mask_stride = ((side + 31) // 32) * 4
    mask_bytes = bytearray(mask_stride * side)
    if mask:
        for y in range(side):
            for x in range(side):
                if mask[y * side + x]:
                    mask_bytes[(side - 1 - y) * mask_stride + (x >> 3)] |= 0x80 >> (x & 7)

    header_size = 40
    bitmap = struct.pack(
        "<IiiHHIIiiII",
        header_size,
        side,
        side * 2,  # ICO 里的高度是实际高度的两倍（下半是 AND 掩码）
        1,
        32,
        0,
        len(xor) + len(mask_bytes),
        0,
        0,
        0,
        0,
    )
    bitmap = bitmap + bytes(xor) + bytes(mask_bytes)

    icon_dir = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(bitmap), 6 + 16)
    return icon_dir + entry + bitmap


def _px(frame, side, x, y):
    idx = (y * side + x) * 4
    return tuple(frame[idx : idx + 4])


def test_extract_icon_frame_decodes_top_down_bgra():
    """BMP 行是自下而上存的，解析后必须是自上而下 —— 弄反了图标就上下颠倒。"""
    from netclip.win.tray import extract_icon_frame

    red, green, blue, white = (
        (0, 0, 255, 255),
        (0, 255, 0, 255),
        (255, 0, 0, 255),
        (255, 255, 255, 255),
    )
    data = _pack_ico([red, green, blue, white], 2)

    frame = extract_icon_frame(data, 2)
    assert frame is not None and len(frame) == 2 * 2 * 4
    assert _px(frame, 2, 0, 0) == red, "左上角应该是源图的第一行"
    assert _px(frame, 2, 1, 0) == green
    assert _px(frame, 2, 0, 1) == blue
    assert _px(frame, 2, 1, 1) == white


def test_extract_icon_frame_scales_to_the_requested_size():
    """尺寸对不上时必须缩放，否则 DIB 行跨度和宽度不符，画出来是斜的。"""
    from netclip.win.tray import extract_icon_frame

    red, green, blue, white = (
        (0, 0, 255, 255),
        (0, 255, 0, 255),
        (255, 0, 0, 255),
        (255, 255, 255, 255),
    )
    frame = extract_icon_frame(_pack_ico([red, green, blue, white], 2), 4)
    assert frame is not None and len(frame) == 4 * 4 * 4
    # 最近邻：2×2 放大成 4×4，每个源像素铺满 2×2
    assert _px(frame, 4, 0, 0) == red
    assert _px(frame, 4, 3, 0) == green
    assert _px(frame, 4, 0, 3) == blue
    assert _px(frame, 4, 3, 3) == white


def test_extract_icon_frame_uses_and_mask_when_alpha_is_missing():
    """整帧 alpha 全 0 时要用 1 位掩码补不透明度，否则图标整个看不见。"""
    from netclip.win.tray import extract_icon_frame

    opaque = (10, 20, 30, 255)
    side = 2
    #: 掩码里 1 = 透明；只让右下角（最后一个像素）不透明
    frame = extract_icon_frame(
        _pack_ico([opaque] * (side * side), side, alpha=False, mask=[1, 1, 1, 0]),
        side,
    )
    assert frame is not None
    assert _px(frame, side, 1, 1)[3] == 255, "掩码为 0 的像素应该不透明"
    assert _px(frame, side, 0, 0)[3] == 0, "掩码为 1 的像素应该透明"


def test_extract_icon_frame_rejects_garbage():
    """坏文件不能让托盘挂掉 —— 返回 None，调用方退回手绘图标。"""
    from netclip.win.tray import extract_icon_frame

    assert extract_icon_frame(b"", 32) is None
    assert extract_icon_frame(b"not an icon at all", 32) is None
    #: 类型字段不是 1（不是图标）
    assert extract_icon_frame(b"\x00\x00\x02\x00\x01\x00", 32) is None
    #: 目录说有 1 项，但文件到此为止
    assert extract_icon_frame(b"\x00\x00\x01\x00\x01\x00", 32) is None


def test_extract_icon_frame_skips_png_frames():
    """256×256 那一档通常是 PNG 压的。我们要的是 32，不该被它带偏。"""
    import struct

    from netclip.win.tray import extract_icon_frame

    bmp = _pack_ico([(1, 2, 3, 255)] * 4, 2)[22:]  # 去掉 6+16 的头，只留 BMP 数据
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64

    bmp_offset = 6 + 32
    png_offset = bmp_offset + len(bmp)
    entries = struct.pack("<BBBBHHII", 2, 2, 0, 0, 1, 32, len(bmp), bmp_offset)
    entries += struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(png), png_offset)

    frame = extract_icon_frame(struct.pack("<HHH", 0, 1, 2) + entries + bmp + png, 2)
    assert frame is not None, "PNG 那一帧应该被跳过，用 BMP 那帧"
    assert _px(frame, 2, 0, 0) == (1, 2, 3, 255)


def test_shipped_icon_has_a_tray_frame():
    """仓库里那份 `.ico` 必须能解出托盘用的那一档。"""
    from pathlib import Path

    from netclip.win.tray import extract_icon_frame

    root = Path(__file__).resolve().parent.parent
    data = (root / "assets" / "netclip.ico").read_bytes()
    for size in (16, 32, 48):
        frame = extract_icon_frame(data, size)
        assert frame is not None, "解不出 %d 这一档" % size
        assert len(frame) == size * size * 4
        assert any(frame[3::4]), "%d 这一档全透明，等于看不见" % size


def test_default_icon_path_points_at_a_real_file():
    from netclip.win.tray import default_icon_path

    path = default_icon_path()
    assert path and path.endswith("netclip.ico")


def test_ring_marks_the_outer_edge_only():
    """状态色画在外沿那一圈 —— 得保证它**不往中间爬**，否则脸就被糊了。"""
    import ctypes

    from netclip.win.tray import _paint_ring

    size = 32
    width = size // 8
    buf = (ctypes.c_ubyte * (size * size * 4))()
    _paint_ring(buf, size, COLOR_PAUSED)

    def pixel(x, y):
        idx = (y * size + x) * 4
        return tuple(buf[idx : idx + 3]), buf[idx + 3]

    assert pixel(0, 0)[0] == COLOR_PAUSED, "左上角要在圈上"
    assert pixel(size - 1, size // 2)[0] == COLOR_PAUSED, "右边缘要在圈上"
    assert pixel(width, width) == ((0, 0, 0), 0), "圈内第一格必须原样不动"
    assert pixel(size // 2, size // 2) == ((0, 0, 0), 0), "正中（脸）必须原样不动"
    assert pixel(0, 0)[1] == 255, "圈必须不透明"


def test_icon_creation_accepts_a_base_image():
    """给了底图也要能出句柄 —— 真图标 + 角标这条路径不能只有手绘那条能跑。"""
    from netclip.win import winapi as w
    from netclip.win.tray import extract_icon_frame, make_icon_hicon

    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    base = extract_icon_frame((root / "assets" / "netclip.ico").read_bytes(), 32)
    assert base is not None

    hicon = make_icon_hicon(COLOR_REMOTE, base=base)
    assert hicon, "应该能画出带底图的图标句柄"
    w.user32.DestroyIcon(hicon)


# --------------------------------------------------------------------- 资源管理器重启


def _bare_window(color=COLOR_ACTIVE):
    """造一个不做任何 Win32 调用的 TrayWindow。

    `_notify_icon` 换成记账的替身，`_make_icon` 换成返回 0 —— 我们测的是
    "什么时候该调用 Shell_NotifyIcon、调的是什么"，不是 GDI 能不能画。
    """
    from netclip.win import tray as tray_mod

    state = TrayState(enabled=True, input_up=True, clip_up=True, peer="selftest")
    window = tray_mod.TrayWindow(lambda: state, on_command=lambda _cmd: None)
    sent = []
    window.hwnd = 0xC0FFEE  # 假的窗口句柄，_notify_icon 已被替掉，不会真的用
    window._notify_icon = lambda message, flags=None: (sent.append(message), True)[1]  # noqa: SLF001
    window._make_icon = lambda _color: 0  # noqa: SLF001
    return window, sent


def test_taskbar_created_message_is_registered():
    """必须拿到 `TaskbarCreated` 的消息号，否则资源管理器重启后收不到通知。

    这个号是 `RegisterWindowMessageW` 在运行期分配的，没有固定值 —— 拿到 0
    说明注册失败，那条自愈路径就整个失效了。
    """
    window, _sent = _bare_window()
    assert window._taskbar_created, "没注册上 TaskbarCreated"  # noqa: SLF001


def test_taskbar_created_readds_the_icon():
    """收到 `TaskbarCreated` 要把图标重新加回托盘。

    `Shell_NotifyIcon` 加的图标挂在**当前那份**任务栏上，explorer.exe 一重启就全没了，
    而且系统不会替我们补。不处理的话托盘图标永久消失 —— 程序还活着但看不见，
    用户只能重启 netclip。
    """
    from netclip.win import winapi as w

    window, sent = _bare_window()
    window._added = False  # noqa: SLF001

    window._wnd_callback(0xC0FFEE, window._taskbar_created, 0, 0)  # noqa: SLF001

    assert w.NIM_ADD in sent, "收到 TaskbarCreated 后应该重新 NIM_ADD"
    assert window._added is True  # noqa: SLF001
    assert window.available is True


def test_refresh_retries_adding_an_icon_that_never_landed():
    """第一次 NIM_ADD 没成功时，后续刷新要接着补挂。

    `_refresh()` 平时会在"状态没变"时提前返回。图标还没挂上去的时候**绝不能**
    提前返回 —— 否则状态一直不变，就永远走不到补挂那一步，托盘里一直空着。
    """
    from netclip.win import winapi as w

    window, sent = _bare_window()
    window._added = False  # noqa: SLF001
    window.available = False

    #: 模拟"任务栏还没准备好"：第一次 NIM_ADD 失败，之后成功
    results = [False, True]

    def flaky(message, flags=None):
        sent.append(message)
        return results.pop(0) if (message == w.NIM_ADD and results) else True

    window._notify_icon = flaky  # noqa: SLF001

    window._refresh()  # noqa: SLF001 - 第一次：NIM_ADD 失败
    assert window._added is False  # noqa: SLF001
    window._refresh()  # noqa: SLF001 - 第二次：状态没变，但必须再试
    assert window._added is True, "状态没变就不补挂了？"  # noqa: SLF001
    assert window.available is True


def test_refresh_still_dedupes_once_the_icon_is_in_the_tray():
    """图标已经在托盘里之后，状态没变就不该反复发 Shell_NotifyIcon。"""
    from netclip.win import winapi as w

    window, sent = _bare_window()
    window._added = True  # noqa: SLF001
    window._refresh()  # noqa: SLF001
    first = len(sent)
    window._refresh()  # noqa: SLF001
    window._refresh()  # noqa: SLF001
    assert len(sent) == first, "状态没变却又发了 Shell_NotifyIcon"
    assert w.NIM_ADD not in sent, "已经挂好了不该再 NIM_ADD"

