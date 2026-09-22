"""`NetManager` 的握手一致性检查测试。

覆盖两条真机上踩过的坑：

1. 两台机器的 `layout.peer_position` 必须**相反**，否则几何互相矛盾；
2. 握手**必须**带上本机真实分辨率，否则对端一直用配置里的兜底值。
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from netclip.net import manager as manager_mod
from netclip.net.manager import NetManager

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="需要本机屏幕探测")


def make_manager(position="right", local_screen=None):
    cfg = SimpleNamespace(layout=SimpleNamespace(peer_position=position, local_screen=local_screen))
    return NetManager(cfg)


@contextmanager
def capture_errors():
    """收集 `manager.log.error(...)` 的调用。

    不用 `monkeypatch` 夹具：内置 runner 的 `parametrize` 只传参数值，
    不会同时注入夹具，两者不能组合。
    """
    calls = []
    original = manager_mod.log
    manager_mod.log = SimpleNamespace(error=lambda *a, **k: calls.append(a))
    try:
        yield calls
    finally:
        manager_mod.log = original


def test_opposite_positions_are_accepted():
    """`right` 配 `left` 是合法组合，不该报警。"""
    manager = make_manager("right")
    with capture_errors() as errors:
        manager.state.peer_says_position = "left"
        manager._check_position_agreement()
    assert errors == []


@pytest.mark.parametrize(
    "mine,theirs",
    [
        ("right", "right"),   # 两边填一样
        ("right", "down"),    # 非相反（真机上用户把从机改成了 down）
        ("up", "right"),
        ("down", "down"),
    ],
)
def test_illegal_position_combinations_are_reported(mine, theirs):
    """非法组合必须报 ERROR —— 它描述不出一个单轴相邻的几何。

    不报警的后果：对端把光标摆到自己反方向的那条边上，离主机模型差整整一个
    屏宽，从机光标从第一帧起就贴在错误边缘、只能单向挪几像素然后被拉回来。
    """
    manager = make_manager(mine)
    with capture_errors() as errors:
        manager.state.peer_says_position = theirs
        manager._check_position_agreement()
    assert len(errors) == 1, "应当报一次错"
    assert "相反" in errors[0][0]


def test_position_warning_is_logged_only_once():
    """状态回调会反复触发，方位矛盾不能刷屏。"""
    manager = make_manager("right")
    with capture_errors() as errors:
        manager.state.peer_says_position = "right"
        for _ in range(5):
            manager._check_position_agreement()
    assert len(errors) == 1


def test_handshake_always_carries_real_screen_size():
    """**回归测试**：没配 `layout.local_screen` 时也要发真实分辨率。

    踩过的坑：早期只在用户显式配了 `local_screen` 时才发，自动探测的那台什么都不发。
    真机上两台实际是 2048x1152 和 1920x1200，却互相以为对方是 1920x1080 ——
    对端矩形算错，钳位边界和"推到对端外边界就交接"的判定点跟着一起错，
    而且错得没有任何日志痕迹。
    """
    manager = make_manager("right", local_screen=None)
    screen = manager._effective_local_screen()
    assert screen is not None and len(screen) == 4, "必须总是能报出一个屏幕矩形"
    assert screen[2] > 0 and screen[3] > 0

    from netclip.layout import Rect
    from netclip.win import winapi as w

    assert screen == list(w.get_virtual_screen_rect())
    # 显式配置时以示数为准
    configured = make_manager("right", local_screen=Rect(0, 0, 800, 600))
    assert configured._effective_local_screen() == [0, 0, 800, 600]
