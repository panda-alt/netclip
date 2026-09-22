"""网络通道的发送队列策略测试。

重点覆盖一条**真实踩过的 bug**：队列满时的丢弃策略必须保住"状态同步"帧，
否则会出现"鼠标像被弹簧拉住"这种极难从日志看出来的故障。

不需要真实网络：`Channel._enqueue` 只操作内存里的队列。
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from netclip.net.channel import UNDROPPABLE, Channel, ChannelConfig
from netclip.protocol import Frame, MsgType

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="协议模块本身跨平台，但整套测试按 Windows 跑")


def make_channel(queue_max=8):
    cfg = ChannelConfig(
        name="input",
        listen_host="127.0.0.1",
        listen_port=0,
        peer_ip="",
        peer_port=0,
        device_name="test",
        psk="test",
        queue_max=queue_max,
        coalesce_ms=0,
    )
    loop = asyncio.new_event_loop()
    return Channel(cfg, loop, on_frame=lambda *a: None), loop


def drain(channel):
    """把队列里的帧取出来（不发送）。"""
    out = []
    while True:
        try:
            item = channel._out.get_nowait()
        except asyncio.QueueEmpty:
            break
        if item is not None:
            out.append(item.type)
    return out


def test_control_frames_are_marked_undroppable():
    """每一类状态同步帧都必须在 UNDROPPABLE 里。

    这条断言的价值：以后新增一种控制帧时，如果忘了登记，测试会直接失败，
    而不是等到用户报告"鼠标被弹簧拉住"。
    """
    required = [
        MsgType.ENTER,
        MsgType.LEAVE,
        MsgType.RELEASE_ALL,
        MsgType.CLAMP,
        MsgType.KEY,
        MsgType.MOUSE_BUTTON,
        MsgType.MOUSE_WHEEL,
        MsgType.HELLO,
        MsgType.HELLO_ACK,
        MsgType.BYE,
        MsgType.CLIP_BEGIN,
        MsgType.CLIP_END,
        MsgType.CLIP_ACK,
        MsgType.FILE_BEGIN,
        MsgType.FILE_END,
        MsgType.FILE_ACK,
    ]
    missing = [hex(t) for t in required if t not in UNDROPPABLE]
    assert not missing, "这些控制帧没有被标记为不可丢弃: %s" % missing


def test_move_frames_are_droppable():
    """鼠标移动是唯一被允许丢弃的类型 —— 它是可合并、可补偿的。"""
    assert MsgType.MOUSE_MOVE not in UNDROPPABLE


def fill_queue(channel, count, key_factory=None):
    """把发送队列填满。

    注意**不能**用同一个 coalesce_key 反复入队：相同 key 的帧会被去重
    （这正是"移动帧只保留一条待发"的机制），队列根本填不满。
    所以这里给每条帧一个不同的 key，模拟"多种帧同时排队"的真实压力。
    """
    for i in range(count):
        key = None if key_factory is None else key_factory(i)
        channel._enqueue(Frame(MsgType.MOUSE_MOVE, {"dx": 1, "dy": 0}), key)


def test_enter_survives_a_full_queue():
    """**回归测试**：队列被打满时，ENTER 不能被丢掉。

    原先 ENTER 不在不可丢弃集合里，队列满时它会被当成"最旧的帧"扔掉。
    后果是对端永远收不到"把光标摆到入口"的指令，而本机已经在按 REMOTE
    转发位移，位移就在原地累积 —— 现象是鼠标被弹簧拉住。
    """
    channel, loop = make_channel(queue_max=4)
    try:
        # 用**不同 coalesce_key** 的移动帧把队列灌满，模拟真实压力
        fill_queue(channel, 20, key_factory=lambda i: ("move", i))
        assert channel._out.qsize() == 4, "队列应该已经满了"

        channel._enqueue(Frame(MsgType.ENTER, {"ratio": 0.5}), None)

        kinds = drain(channel)
        assert MsgType.ENTER in kinds, "ENTER 必须活下来（实际队列里是 %s）" % [hex(k) for k in kinds]
    finally:
        loop.close()


def test_key_survives_a_full_queue():
    """按键同理：队列满时丢一个按键，用户就会觉得"少打了一个字"。"""
    channel, loop = make_channel(queue_max=4)
    try:
        fill_queue(channel, 20, key_factory=lambda i: ("move", i))
        channel._enqueue(Frame(MsgType.KEY, {"vk": 0x41, "down": True}), None)

        kinds = drain(channel)
        assert MsgType.KEY in kinds
    finally:
        loop.close()


def test_release_all_survives_a_full_queue():
    """RELEASE_ALL 丢了会导致"对端还按着 Ctrl"，本机点击全变成 Ctrl+点击。"""
    channel, loop = make_channel(queue_max=4)
    try:
        fill_queue(channel, 20, key_factory=lambda i: ("move", i))
        channel._enqueue(Frame(MsgType.RELEASE_ALL, {"reason": "test"}), None)
        assert MsgType.RELEASE_ALL in drain(channel)
    finally:
        loop.close()


def test_droppable_frame_is_evicted_when_queue_is_full():
    """队列满时腾位置：被牺牲的应该是最旧的可丢弃帧。"""
    channel, loop = make_channel(queue_max=3)
    try:
        fill_queue(channel, 10, key_factory=lambda i: ("move", i))
        assert channel.stats.dropped >= 1, "队列满时必须丢掉了东西"
        assert channel._out.qsize() <= 3
    finally:
        loop.close()


def test_coalesce_key_dedupes_moves():
    """同一个 coalesce_key 的移动帧只会留一条待发 —— 这是位移合帧的基础。"""
    channel, loop = make_channel(queue_max=8)
    try:
        for _ in range(50):
            channel._enqueue(Frame(MsgType.MOUSE_MOVE, {"dx": 1, "dy": 1}), "move")
        assert channel.stats.coalesced >= 40
        # 队列里只应该有一条移动帧（其余都被去重了）
        assert channel._out.qsize() == 1
    finally:
        loop.close()


def test_pending_is_cleared_when_frame_dropped():
    """被丢弃的帧必须从 pending 表里摘掉。

    否则同一个 coalesce_key 会一直被去重，后续的移动全部被静默丢掉 ——
    光标会"卡住不动"。
    """
    channel, loop = make_channel(queue_max=2)
    try:
        first = Frame(MsgType.MOUSE_MOVE, {"dx": 1, "dy": 0})
        channel._enqueue(first, "move")
        assert "move" in channel._pending

        channel._forget_pending(first)
        assert "move" not in channel._pending

        # 摘干净之后，新的移动应该能正常入队
        before = channel._out.qsize()
        channel._enqueue(Frame(MsgType.MOUSE_MOVE, {"dx": 5, "dy": 5}), "move")
        assert channel._out.qsize() >= before
    finally:
        loop.close()


def test_pending_entry_survives_a_merge_by_body_identity():
    """**回归测试**：合并出新帧之后，pending 表也必须能被正确摘掉。

    合并时 `_collect_moves` 返回的是一个**新帧对象**（带同一个 body）。
    早期 `_forget_pending` 按帧对象身份匹配，于是这一条永远摘不掉 ——
    之后所有同键的帧都会被并进一个再也发不出去的 dict，位移凭空消失。
    """
    channel, loop = make_channel()
    try:
        first = Frame(MsgType.MOUSE_MOVE, {"dx": 1, "dy": 0, "x": 10, "y": 20})
        channel._enqueue(first, "move")
        #: 模拟 writer：先把帧从队列里取走，再去收合并
        taken = channel._out.get_nowait()
        assert taken is first
        channel._out.put_nowait(Frame(MsgType.MOUSE_MOVE, {"dx": 2, "dy": 3, "x": 30, "y": 40}))
        merged = loop.run_until_complete(channel._collect_moves(taken))
        assert merged is not taken, "有合并时必须返回新帧"
        assert "move" in channel._pending, "合并期间表项应当还在"
        channel._forget_pending(taken)
        assert "move" not in channel._pending, "合并后必须能摘掉（按 body 身份匹配）"
    finally:
        loop.close()


def test_coalesce_accumulates_instead_of_dropping():
    """**回归测试**：合并窗口内的位移必须**累加**，不能被丢掉。

    真机上踩到的 bug：`_enqueue` 在发现同键帧已排队时直接 `return` 丢弃新帧，
    注释说"让 writer 稍后把这段时间累积的位移一次性收走"—— 但那些帧根本没进队列，
    `_collect_moves` 抽不到任何东西，于是它退化成一个纯限流器：
    每个 8ms 窗口只发出**窗口开头那一个位置**，窗口里的位移全丢。
    用户停手时最后一个窗口的位移永远不会被应用，从机光标就停在旧位置 ——
    也就是"被拉回去"。
    """
    channel, loop = make_channel()
    try:
        channel._enqueue(Frame(MsgType.MOUSE_MOVE, {"dx": 5, "dy": 1, "x": 100, "y": 200}), "move")
        # 窗口内再来三条：位移要累加，绝对落点取最后一条
        channel._enqueue(Frame(MsgType.MOUSE_MOVE, {"dx": 5, "dy": 1, "x": 105, "y": 201}), "move")
        channel._enqueue(Frame(MsgType.MOUSE_MOVE, {"dx": 5, "dy": 1, "x": 110, "y": 202}), "move")
        channel._enqueue(Frame(MsgType.MOUSE_MOVE, {"dx": 5, "dy": 1, "x": 115, "y": 203}), "move")

        assert channel._out.qsize() == 1, "同一个 key 只应有一个帧在队列里"
        queued = channel._out.get_nowait()
        assert (queued.body["dx"], queued.body["dy"]) == (20, 4), "位移必须累加，不能丢"
        assert (queued.body["x"], queued.body["y"]) == (115, 203), "绝对落点必须取最后一条"
    finally:
        loop.close()


def test_stats_are_counted():
    channel, loop = make_channel(queue_max=2)
    try:
        channel._enqueue(Frame(MsgType.MOUSE_MOVE, {"dx": 1, "dy": 0}), "move")
        channel._enqueue(Frame(MsgType.MOUSE_MOVE, {"dx": 1, "dy": 0}), "move")
        assert channel.stats.coalesced >= 1
        assert "帧" in channel.stats.summary()
    finally:
        loop.close()


def test_coalesced_move_keeps_last_absolute_position():
    """**回归测试**：合并鼠标帧时必须保留绝对落点，而且取**最后**一条。

    `x`/`y` 表示"现在应该在哪"，与路径无关，所以**不能累加**。
    合并时如果把它丢掉，接收端就只能退回相对位移 —— 而相对位移会被系统的
    "提高指针精确度"加速曲线改写（真机实测 80px 变 290px），等于把已经修好的
    问题又放回来。
    """
    channel, loop = make_channel()
    try:
        first = Frame(MsgType.MOUSE_MOVE, {"dx": 1, "dy": 0, "x": 100, "y": 200})
        channel._out.put_nowait(Frame(MsgType.MOUSE_MOVE, {"dx": 2, "dy": 3, "x": 250, "y": 260}))
        merged = loop.run_until_complete(channel._collect_moves(first))
        assert (merged.body["dx"], merged.body["dy"]) == (3, 3), "位移应当累加"
        assert (merged.body["x"], merged.body["y"]) == (250, 260), "绝对落点应当取最后一条"
    finally:
        loop.close()


def test_coalesced_move_without_absolute_stays_relative():
    """老版本（只有相对位移）的帧不能被凭空补上绝对落点。

    补一个假的落点会让接收端把光标瞬移到错误的位置，比不补更糟。
    """
    channel, loop = make_channel()
    try:
        first = Frame(MsgType.MOUSE_MOVE, {"dx": 1, "dy": 0})
        channel._out.put_nowait(Frame(MsgType.MOUSE_MOVE, {"dx": 2, "dy": 3}))
        merged = loop.run_until_complete(channel._collect_moves(first))
        assert (merged.body["dx"], merged.body["dy"]) == (3, 3)
        assert "x" not in merged.body and "y" not in merged.body
    finally:
        loop.close()
