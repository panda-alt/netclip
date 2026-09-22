"""`Session` 的输入接收路径测试。

只测"收到帧之后做了什么决定"，不启动网络、钩子或真实光标 ——
把 `Session` 用 `object.__new__` 造出来，只挂上需要的那几个属性。

覆盖的核心决定：**收到鼠标移动帧时优先用绝对落点**。原因是 Windows 会把
注入的**相对**位移再过一遍"提高指针精确度"的加速曲线（真机实测：请求 1px
实际走 0px、请求 80px 实际走 290px），相对位移因此无法和主机的模型对齐。
"""

from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Session 依赖 Win32")


class FakeRouter:
    """只记录"被要求把光标摆到哪"。"""

    def __init__(self):
        self.warps = []
        self.noted = []
        self.mode = type("M", (), {"value": "local"})()

    def warp_local_cursor(self, x, y):
        self.warps.append((x, y))

    def note_absolute_position(self, x, y):
        self.noted.append((x, y))


def make_session(monkeypatch=None):
    from netclip.core.session import Session

    sess = object.__new__(Session)
    sess.tracer = None
    sess.chain = None
    sess.router = FakeRouter()
    return sess


def _patch_absolute(monkeypatch, calls):
    monkeypatch.setattr(
        "netclip.win.inject.move_absolute",
        lambda x, y, virtual_desktop=False: (calls.append((x, y, virtual_desktop)), 1)[1],
    )


def test_absolute_move_frame_uses_injection_not_setcursorpos(monkeypatch):
    """带 `x`/`y` 的帧必须走**注入**式绝对定位，而不是 `SetCursorPos`。

    为什么这条很关键：`SetCursorPos` 产生不带注入标志的事件，需要靠 60ms 的
    "沉降窗"屏蔽；绝对定位是每帧一次，窗口就永远开着，从机自己的物理鼠标
    会被彻底屏蔽（真机实测：从机整个会话 `移动事件 0`），用户没法把光标
    推回主机。`SendInput` 带 `LLMHF_INJECTED`，钩子直接跳过，不需要沉降窗。
    """
    sess = make_session()
    calls = []
    _patch_absolute(monkeypatch, calls)
    sess._recv_mouse_move(0, {"dx": 5, "dy": -3, "x": 100, "y": 200}, b"")
    assert calls == [(100, 200, True)], "应当用绝对注入，且走虚拟桌面坐标"
    assert sess.router.noted == [(100, 200)], "模型要同步更新"
    assert sess.router.warps == [], "绝对定位路径不该再碰 SetCursorPos"


def test_relative_only_frame_falls_back(monkeypatch):
    """没有绝对落点的帧（老版本对端）要退回相对注入，保持向后兼容。"""
    sess = make_session()
    calls = []
    monkeypatch.setattr("netclip.win.inject.move_relative", lambda dx, dy: calls.append((dx, dy)) or 1)
    sess._recv_mouse_move(0, {"dx": 5, "dy": -3}, b"")
    assert calls == [(5, -3)]
    assert sess.router.warps == [], "没有绝对落点时不该去摆光标"


def test_empty_move_frame_is_ignored(monkeypatch):
    """零位移且没有绝对落点的帧不必处理。"""
    sess = make_session()
    calls = []
    monkeypatch.setattr("netclip.win.inject.move_relative", lambda dx, dy: calls.append((dx, dy)) or 1)
    sess._recv_mouse_move(0, {"dx": 0, "dy": 0}, b"")
    assert calls == []
    assert sess.router.warps == []


def test_absolute_move_is_applied_even_when_delta_is_zero(monkeypatch):
    """位移为 0 但有绝对落点时仍要摆光标。

    这是"主机模型没动但从机光标偏了"（被别的程序挪走）的自愈路径：
    主机下一帧的绝对落点会把从机光标拉回来。
    """
    sess = make_session()
    calls = []
    _patch_absolute(monkeypatch, calls)
    sess._recv_mouse_move(0, {"dx": 0, "dy": 0, "x": 7, "y": 9}, b"")
    assert calls == [(7, 9, True)]
    assert sess.router.noted == [(7, 9)]
