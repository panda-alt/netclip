"""鼠标位移基准的测试 —— 本项目最反直觉的那个坑。

低级鼠标钩子拿到的 `pt` **不是**"光标现在在哪"，而是"如果允许这次移动，光标会到哪"：

    pt = 光标当前位置 + 本次原始增量

所以位移的基准必须取「光标当前位置」：

* **LOCAL（不吞事件）**：移动会被提交，"当前位置"就是上一条 `pt`，两种基准等价；
* **REMOTE（吞掉事件）**：移动不提交、光标冻在穿越点，`pt - 上一条pt` 算出来的是
  `delta(n) - delta(n-1)`（位移的**差**）。人手平滑移动时相邻增量几乎相等，
  于是转发出去的是 **0** —— 真机上每秒 900 条事件、`pt` 跨度 227x57，
  转发位移却只有 ±几十，累计几万条事件的净位移不到 10 像素。
"""

from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="钩子层依赖 Win32")


def make_hook(cursor=(-1, -1)):
    from netclip.win.hooks import HookThread

    hook = HookThread(on_event=lambda ev: None, should_swallow=lambda ev: False)
    hook.cursor_reader = lambda: cursor
    return hook


def test_base_is_the_real_cursor_position():
    """基准必须是系统光标的当前位置，而不是上一条 pt。"""
    hook = make_hook(cursor=(1000, 500))
    assert hook.motion_base(1200, 620) == (1000, 500)


def test_frozen_cursor_recovers_the_true_delta():
    """**核心断言**：光标冻住时，`pt - 光标位置` 恢复出的正是本次位移。

    这正是真机的故障形态：REMOTE 下光标被冻在 (1665,1439)，钩子的 pt 在
    [1556,1783]x[1409,1466] 之间摆动。用"上一条 pt"当基准得到的是位移的差
    （平滑移动时约等于 0），用"光标位置"当基准得到的才是真实位移。
    """
    frozen = (1665, 1439)
    hook = make_hook(cursor=frozen)

    #: 关键：`pt` **不累积**。系统每次都拿"光标当前位置 + 本次增量"重算，
    #: 而光标冻住不动，所以每个事件的 pt 都是 `冻结位置 + 本次增量`。
    #: （真机数据佐证：pt 始终在冻结位置附近 ±100 摆动，而不是一路增下去。）
    deltas = [30, 30, 30, 30]
    pts = [(frozen[0] + d, frozen[1]) for d in deltas]
    assert len(set(pts)) == 1, "光标冻住 + 匀速移动时，每个事件的 pt 是同一个值"

    #: 用"光标位置"当基准 -> 每次都恢复出真实的 30
    recovered = []
    for pt in pts:
        base = hook.motion_base(*pt)
        recovered.append((pt[0] - base[0], pt[1] - base[1]))
    assert all(got == (30, 0) for got in recovered), "按光标位置算才能拿到真实位移"

    #: 用"上一条 pt"当基准 -> 匀速移动时退化成 0（这就是修复前的 bug）
    naive = [(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]) for i in range(1, len(pts))]
    assert all(step == (0, 0) for step in naive), "相邻差在匀速移动时必然退化成 0"


def test_falls_back_when_cursor_is_unreadable():
    """读不到光标位置时退回上一条 pt，不能让钩子崩掉。"""
    hook = make_hook(cursor=(-1, -1))
    hook._last_x, hook._last_y = 700, 300
    hook._have_last = True
    assert hook.motion_base(750, 320) == (700, 300)


def test_falls_back_to_pt_when_nothing_is_known():
    """连上一条都没有时以 pt 为基准（位移视作 0），避免第一次移动就飞出去。"""
    hook = make_hook(cursor=(-1, -1))
    assert hook.motion_base(400, 500) == (400, 500)
