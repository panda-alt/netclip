"""屏幕摆放 / 边缘检测 / 坐标映射的单元测试。

这些是整个鼠标共享里最容易出错、又最容易测的部分：纯几何、不需要任何
Windows API，所以放在测试里把边界情况钉死。
"""

from __future__ import annotations

import pytest

from netclip.layout import (
    DOWN,
    LEFT,
    RIGHT,
    UP,
    Rect,
    ScreenLayout,
    _opposite,
    is_horizontal,
)


def make(position: str, local=(0, 0, 1920, 1080), peer=(0, 0, 1920, 1080), band=2, inset=8):
    return ScreenLayout(Rect(*local), Rect(*peer), position, edge_band_px=band, warp_inset_px=inset)


# --------------------------------------------------------------------- 基础


def test_rect_geometry():
    rect = Rect(10, 20, 100, 50)
    assert (rect.right, rect.bottom) == (110, 70)
    assert rect.contains(10, 20) and rect.contains(109, 69)
    assert not rect.contains(110, 20) and not rect.contains(10, 70)
    assert rect.clamp(-5, 999) == (10, 69), "钳位右/下边界要取 -1，避免落在矩形外"


def test_is_horizontal_and_opposite():
    assert is_horizontal(RIGHT) and is_horizontal(LEFT)
    assert not is_horizontal(UP) and not is_horizontal(DOWN)
    for position in (RIGHT, LEFT, UP, DOWN):
        assert _opposite(_opposite(position)) == position
        assert _opposite(position) != position


def test_invalid_position_is_rejected():
    with pytest.raises(ValueError):
        make("diagonal")


def test_zero_sized_screen_is_rejected():
    with pytest.raises(ValueError):
        make(RIGHT, local=(0, 0, 0, 100))


# --------------------------------------------------------------------- 穿越判定


def test_detect_crossing_right():
    layout = make(RIGHT)
    event = layout.detect_crossing((1918, 500), (1921, 500))
    assert event is not None and event.direction == "to_peer"


def test_detect_crossing_left():
    layout = make(LEFT)
    event = layout.detect_crossing((2, 500), (-1, 500))
    assert event is not None and event.direction == "to_peer"


def test_detect_crossing_up():
    layout = make(UP)
    event = layout.detect_crossing((500, 2), (500, -1))
    assert event is not None and event.direction == "to_peer"


def test_detect_crossing_down():
    layout = make(DOWN)
    event = layout.detect_crossing((500, 1079), (500, 1082))
    assert event is not None and event.direction == "to_peer"


def test_touching_edge_without_crossing_is_not_an_event():
    """贴着边停住不算穿越 —— 必须真的"跨过去"。"""
    layout = make(RIGHT)
    assert layout.detect_crossing((1919, 500), (1919, 500)) is None
    assert layout.detect_crossing((1900, 500), (1919, 500)) is None


def test_only_the_shared_side_triggers():
    """只有朝向对端的那条边能穿越，其它三边完全不管。"""
    layout = make(RIGHT)
    assert layout.detect_crossing((2, 500), (-1, 500)) is None, "左边缘不该触发"
    assert layout.detect_crossing((500, 2), (500, -1)) is None, "上边缘不该触发"
    assert layout.detect_crossing((500, 1079), (500, 1082)) is None, "下边缘不该触发"


def test_armed_flag_blocks_crossing():
    """穿越锁定期内不应再次判定穿越（防共享边弹跳）。"""
    layout = make(RIGHT)
    layout.armed = False
    assert layout.detect_crossing((1918, 500), (1921, 500)) is None
    layout.armed = True
    assert layout.detect_crossing((1918, 500), (1921, 500)) is not None


def test_crossing_only_inside_the_shared_interval():
    """两边屏幕沿共享边的尺寸不同时，只有重叠的那一段能穿过去。

    本机 1920 高、对端 1080 高、居中对齐 -> 对端占据本机 y 的 [420, 1500)，
    与本机 [0, 1080) 的交集是 [420, 1080)。所以在 y=100 处推出去不该穿越。
    """
    layout = make(RIGHT, local=(0, 0, 1920, 1080), peer=(0, 0, 1920, 800))
    assert layout.detect_crossing((1918, 100), (1921, 100)) is None, "在共享区间之外不该穿越"
    assert layout.detect_crossing((1918, 700), (1921, 700)) is not None


# --------------------------------------------------------------------- 入口点


def test_entry_point_insets_from_the_edge():
    """入口点必须离边有一点内缩，否则落地就被判成又要出去。"""
    layout = make(RIGHT)
    x, y = layout.entry_point(0.5, RIGHT)
    assert x == 1920 - 1 - 8
    assert y == pytest.approx(539, abs=1)
    left_x, _ = layout.entry_point(0.5, LEFT)
    assert left_x == 8


def test_entry_point_uses_the_peer_resolution():
    """对端入口点用的是**对端**的屏幕尺寸，不是本机的。"""
    layout = make(RIGHT, local=(0, 0, 1920, 1080), peer=(0, 0, 1280, 800))
    x, y = layout.peer_entry_point(0.5, LEFT)
    assert x == 8
    assert y == pytest.approx(399, abs=1)
    x, y = layout.peer_entry_point(0.5, RIGHT)
    assert x == 1280 - 1 - 8


# --------------------------------------------------------------------- 统一坐标系


def test_local_rect_is_the_origin():
    layout = make(RIGHT)
    assert layout.local_rect_global().as_tuple() == (0, 0, 1920, 1080)


@pytest.mark.parametrize(
    "position,expect",
    [
        (RIGHT, (1920, 0, 1920, 1080)),
        (LEFT, (-1920, 0, 1920, 1080)),
        (UP, (0, -1080, 1920, 1080)),
        (DOWN, (0, 1080, 1920, 1080)),
    ],
)
def test_peer_rect_is_adjacent(position, expect):
    assert make(position).peer_rect_global().as_tuple() == expect


def test_screens_never_overlap():
    """**回归测试**：两块屏幕在统一坐标系里绝不能重叠。

    早期版本把 alignment 同时用在两条轴上，结果穿越方向上两块屏幕叠在一起，
    坐标一进去就是负数，光标直接乱跳。
    """
    for position in (RIGHT, LEFT, UP, DOWN):
        layout = make(position, local=(0, 0, 2560, 1440), peer=(0, 0, 1920, 1200))
        local_rect = layout.local_rect_global()
        peer_rect = layout.peer_rect_global()
        assert not (
            local_rect.x < peer_rect.right
            and peer_rect.x < local_rect.right
            and local_rect.y < peer_rect.bottom
            and peer_rect.y < local_rect.bottom
        ), "%s: 两块屏幕重叠了" % position


def test_alignment_offset_applies_on_the_non_crossing_axis():
    """居中对齐时，窄的那块屏沿**非穿越轴**整体偏移。

    真机场景：主机 2560x1440、从机 1920x1200、上下叠放。上下穿越用 y，
    于是 x 上留 (2560-1920)/2 = 320 的偏移。这个偏移**必须**体现在对端坐标
    换算里 —— 漏掉它会让发给对端的绝对落点整体偏 320，从机光标被推到屏幕
    外侧钳住（"拉走又被慢慢拉回来"）。
    """
    layout = make(DOWN, local=(0, 0, 2560, 1440), peer=(0, 0, 1920, 1200))
    rect = layout.peer_rect_global()
    assert rect.x == 320, "居中对齐应当把对端矩形右移 320"
    assert rect.y == 1440, "穿越轴上要紧贴本机下边界"


def test_peer_local_roundtrip_covers_alignment_offset():
    """对端坐标 <-> 统一坐标 必须在四种方位下都精确可逆（含 alignment 偏移）。"""
    local = Rect(0, 0, 2560, 1440)
    peer = Rect(0, 0, 1920, 1200)
    for position in (RIGHT, LEFT, UP, DOWN):
        layout = ScreenLayout(local, peer, position)
        rect = layout.peer_rect_global()
        for point in ((0, 0), (1919, 1199), (960, 600)):
            gx, gy = layout.peer_local_to_global(*point)
            assert rect.contains(gx, gy), "%s: %s 应当落在对端矩形内" % (position, (gx, gy))
            assert layout.global_to_peer_local(gx, gy) == point, "%s: 换算必须可逆" % position


def test_rect_containing_tells_which_screen():
    layout = make(RIGHT, local=(0, 0, 1920, 1080), peer=(0, 0, 1280, 800))
    assert layout.rect_containing(100, 100) == "local"
    #: 对端居中 -> 它在统一坐标系里是 (1920, 140) 起，y=100 落在它上面那片空档
    assert layout.rect_containing(1920 + 100, 100) == "none"
    assert layout.rect_containing(1920 + 100, 500) == "peer"
    assert layout.rect_containing(5000, 5000) == "none"


def test_describe_mentions_axis_and_rects():
    text = make(DOWN).describe()
    assert "垂直" in text and "本机=" in text and "对端=" in text
