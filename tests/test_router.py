"""输入路由状态机的单元测试。

这里的每一条都对应一个**真实发生过的故障**，所以注释写清了"不修会怎样"，
避免以后有人觉得某个判断多余就把它删掉。

路由器依赖 Win32（`GetAsyncKeyState` 等），所以只在 Windows 上跑。
`RouterCallbacks` 完全靠注入，因此不需要真实的鼠标键盘就能驱动状态机。
"""

from __future__ import annotations

import sys

import pytest

from netclip.core.router import (
    HotkeyBinding,
    InputRouter,
    Mode,
    RouterCallbacks,
    _normalize_mod,
    _parse_hotkey,
)
from netclip.layout import Rect, ScreenLayout
from netclip.win import winapi as w
from netclip.win.hooks import BUTTON, KEY, MOVE, WHEEL, InputEvent

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="路由器依赖 Win32")


# --------------------------------------------------------------------- 测试装置


class Recorder:
    """记录路由器往外发了什么、做了什么动作。"""

    def __init__(self):
        self.frames = []       # (mtype, body)
        self.moves = []        # (dx, dy)
        #: 每帧随发的**绝对落点**（对端坐标系）。见 router._forward_remote_move：
        #: 相对位移会被系统指针加速曲线改写，绝对落点才是权威位置。
        self.moves_abs = []    # (x, y)
        self.enters = []
        self.leaves = 0
        self.warps = []
        self.releases = 0
        self.recaptures = 0
        self.toggles = 0
        self.peer_ready = True

    def callbacks(self) -> RouterCallbacks:
        return RouterCallbacks(
            send=lambda mtype, body, key=None: self.frames.append((mtype, body)),
            send_move=lambda dx, dy, x, y: (
                self.moves.append((dx, dy)),
                self.moves_abs.append((x, y)),
            ),
            on_enter_peer=lambda ratio: self.enters.append(ratio),
            on_leave_peer=lambda: setattr(self, "leaves", self.leaves + 1),
            warp_cursor=lambda x, y: self.warps.append((x, y)),
            release_all=lambda force: setattr(self, "releases", self.releases + 1),
            request_recapture=lambda: setattr(self, "recaptures", self.recaptures + 1),
            request_toggle=lambda: setattr(self, "toggles", self.toggles + 1),
            is_peer_connected=lambda: self.peer_ready,
        )


def make_router(
    local=(0, 0, 1920, 1080),
    peer=(0, 0, 1920, 1080),
    position="right",
    **kwargs,
):
    rec = Recorder()
    layout = ScreenLayout(Rect(*local), Rect(*peer), position)
    params = dict(
        share_mouse=True,
        share_keyboard=True,
        local_hotkeys=["ctrl+alt+del", "ctrl+shift+esc"],
        hotkey_recapture="ctrl+alt+home",
        hotkey_toggle="ctrl+alt+pause",
        switch_cooldown_ms=0,
        arm_delay_ms=0,
    )
    params.update(kwargs)
    router = InputRouter(layout, rec.callbacks(), **params)
    router.start()
    router.set_peer_ready(True)
    return router, rec


def key_event(vk, down=True):
    return InputEvent(kind=KEY, vk=vk, scan=0, down=down)


def move_event(x, y, dx, dy):
    return InputEvent(kind=MOVE, x=x, y=y, dx=dx, dy=dy)


def enter_remote(router, ratio=0.5):
    """把路由器推到 REMOTE 状态（走真实的 enter_peer 路径）。"""
    router.enter_peer(ratio)


def place_virtual(router, x, y):
    """把**虚拟光标**放到屏幕坐标 (x, y)，用来给测试设定一个已知起点。

    为什么需要它：`InputRouter.start()` 会执行 `_refresh_virtual_cursor()`，
    把虚拟光标初始化成**真实光标**当前的位置（真实机器上可能是任何地方）。
    之后虚拟光标只按事件的增量推进。所以测试不能假设它从 (0,0) 开始 ——
    否则"推到屏幕边缘"这件事就会因为起点不同而随机成立或不成立。
    """
    router._virt_x = int(x)
    router._virt_y = int(y)
    router._virt_have = True


def push_to_edge(router, y, overshoot=4):
    """模拟用户从屏幕内把光标推出右边界。

    分两步发事件是刻意的：`detect_crossing` 判的是 **prev -> cur 的跨越**，
    必须有一次"上一帧在里面、这一帧在外面"的转移，光把光标摆在边缘不算。
    """
    place_virtual(router, router.layout.local.right - 2, y)
    router._handle_move(move_event(router.layout.local.right + overshoot, y, 2 + overshoot, 0))


# --------------------------------------------------------------------- 热键解析


def test_parse_hotkey_splits_modifiers():
    mods, main = _parse_hotkey("ctrl+alt+home")
    assert mods == frozenset({w.VK_CONTROL, w.VK_MENU})
    assert main == w.VK_HOME


def test_parse_hotkey_modifier_only():
    mods, main = _parse_hotkey("win")
    assert main is None
    assert mods == frozenset({w.VK_LWIN})


def test_parse_hotkey_function_keys():
    _mods, main = _parse_hotkey("ctrl+f12")
    assert main == w.VK_F1 + 11


def test_parse_hotkey_rejects_garbage():
    assert _parse_hotkey("ctrl+notakey") is None
    assert _parse_hotkey("") is None


# --------------------------------------------------------------------- 修饰键归一化


def test_normalize_mod_maps_left_right_variants():
    """左右修饰键必须归一到同一个虚拟键，否则热键判定会漏。

    这是"Win+L / Alt+Tab 这类本机热键完全不起作用"的根因：
    配置里写的是 VK_LWIN(0x5B)，钩子上报的可能是 VK_RWIN(0x5C)；
    `alt` 解析成 VK_MENU(0x12)，而钩子上报的是 VK_LMENU(0xA4)。
    """
    for vk in (w.VK_LWIN, w.VK_RWIN):
        assert _normalize_mod(vk) == {w.VK_LWIN}
    for vk in (w.VK_LMENU, w.VK_RMENU, w.VK_MENU):
        assert _normalize_mod(vk) == {w.VK_MENU}
    for vk in (w.VK_LCONTROL, w.VK_RCONTROL, w.VK_CONTROL):
        assert _normalize_mod(vk) == {w.VK_CONTROL}
    for vk in (w.VK_LSHIFT, w.VK_RSHIFT, w.VK_SHIFT):
        assert _normalize_mod(vk) == {w.VK_SHIFT}


def test_normalize_mod_returns_empty_for_regular_keys():
    assert _normalize_mod(ord("A")) == set()
    assert _normalize_mod(w.VK_TAB) == set()


# --------------------------------------------------------------------- 光标穿越


def test_crossing_switches_to_remote():
    router, rec = make_router()
    assert router.mode is Mode.LOCAL

    push_to_edge(router, 500)

    assert router.mode is Mode.REMOTE
    assert rec.enters, "应该通知上层光标进入对端"
    assert any(f[0] == 0x14 for f in rec.frames), "应该发出 ENTER"


def test_entering_remote_enables_swallowing():
    """进入 REMOTE 之后钩子必须开始吞事件，否则本机还会同时响应输入。"""
    router, _rec = make_router()
    assert router.should_swallow(move_event(500, 500, 1, 1)) is False
    enter_remote(router)
    assert router.should_swallow(move_event(500, 500, 1, 1)) is True


def test_touching_edge_without_crossing_does_not_switch():
    router, _rec = make_router()
    place_virtual(router, 1916, 500)
    router._handle_move(move_event(1918, 500, 2, 0))
    router._handle_move(move_event(1919, 500, 1, 0))
    assert router.mode is Mode.LOCAL


def test_drag_lock_prevents_switching():
    """按住鼠标键拖拽时不能切屏，否则窗口会被"甩"到对端。"""
    router, _rec = make_router(lock_mouse_on_drag=True)
    place_virtual(router, router.layout.local.right - 2, 500)
    dragging = move_event(router.layout.local.right + 4, 500, 6, 0)
    dragging.any_button_down = True
    router._handle_move(dragging)
    assert router.mode is Mode.LOCAL


def test_switch_cooldown_blocks_rapid_reswitch():
    router, _rec = make_router(switch_cooldown_ms=5000)
    push_to_edge(router, 500)
    assert router.mode is Mode.REMOTE
    router.recapture("测试")
    # 冷却期内不能马上又切过去
    push_to_edge(router, 600)
    assert router.mode is Mode.LOCAL


def test_arm_delay_blocks_immediate_recross():
    router, _rec = make_router(arm_delay_ms=5000)
    router.recapture("测试")
    push_to_edge(router, 500)
    assert router.mode is Mode.LOCAL, "回拉后的锁定期内不应再次穿越"


def test_branch_interval_blocks_crossing_outside_overlap():
    """本机比对端高时，居中对齐之外的高度不能穿过去。"""
    router, _rec = make_router(local=(0, 0, 1920, 1440), peer=(0, 0, 1920, 1080))

    push_to_edge(router, 100)  # 本机 y=100 低于对端叠加上边界（180）
    assert router.mode is Mode.LOCAL, "不在重叠区间内不应触发穿越"

    push_to_edge(router, 700)  # 落在重叠区间（180..1260）内
    assert router.mode is Mode.REMOTE


def test_peer_cursor_is_clamped_to_peer_screen():
    """**回归测试**：转发位移必须以**对端屏幕**为界钳位。

    这是从真实追踪数据里查出来的 bug：主机只维护"本机虚拟光标"，转发钩子的
    原始未钳位位移。于是主机内存里"对端光标在哪"会一路漂到对端屏幕之外
    （实测漂到 (-873, -1092)，而对端只有 1920x1080），之后主机对位置的认知
    和从机实际情况永久错位 —— 表现就是光标被拽住 / 乱跳 / 推到某个位置不动。
    """
    # 用一块很宽、且**与本机等高**的对端屏幕：
    #   * 足够宽 -> 推几万像素也撞不到外边界，不会触发交接；
    #   * 等高且居中对齐 -> 统一坐标系里 y 范围正好是 [0, 800)，边界值好断言。
    router, rec = make_router(local=(0, 0, 1280, 800), peer=(0, 0, 100000, 800))
    enter_remote(router)

    peer_rect = router.layout.peer_rect_global()
    entry_x = router._peer_x  # noqa: SLF001
    assert 0 <= entry_x < 100000, "初始模拟位置必须在对端屏幕内"

    for _ in range(5):
        router._handle_move(move_event(0, 0, 50, 0))
    assert sum(dx for dx, _dy in rec.moves) == 250
    assert router.layout.rect_containing(router._gx, router._gy) == "peer"  # noqa: SLF001

    # 推很多但不到边：转发总量应等于位移总量
    for _ in range(200):
        router._handle_move(move_event(0, 0, 100, 0))
    assert router.mode is Mode.REMOTE, "还没到外边界，不该交接"
    assert router.stats["move_clamped"] == 0, "没顶到边就不该有钳位"
    assert sum(dx for dx, _dy in rec.moves) == 250 + 200 * 100


def test_peer_cursor_clamps_in_all_directions():
    """四个方向都要钳位。

    用一块**不高**但很宽的对端屏幕：先把 y 顶到边上（x 还远没到边界，
    所以不会触发交接），再验证 x 方向也钳得住。
    """
    router, _rec = make_router(local=(0, 0, 1280, 800), peer=(0, 0, 100000, 800))
    peer_rect = router.layout.peer_rect_global()
    assert peer_rect.y == 0

    # 往左上推：两个方向都应该钳住
    enter_remote(router)
    for _ in range(200):
        router._handle_move(move_event(0, 0, -60, -60))
    assert router.mode is Mode.REMOTE, "撞到对端左边界不该交接（那是朝向本机的边）"
    assert router._gx == peer_rect.x, router._gx  # noqa: SLF001
    assert router._gy == peer_rect.y, router._gy  # noqa: SLF001
    assert router.stats["move_clamped"] > 0

    # 往右下推：y 先顶到 799，然后一直被钳住；x 继续向右走
    enter_remote(router)
    for _ in range(200):
        router._handle_move(move_event(0, 0, 60, 60))
    assert router._gy == peer_rect.bottom - 1, router._gy  # noqa: SLF001
    assert router.layout.peer_rect_global().contains(router._gx, router._gy)  # noqa: SLF001

    # 继续往右下推很多步：y 必须一直贴在边界上，绝不能跑到屏幕外
    for _ in range(300):
        router._handle_move(move_event(0, 0, 60, 60))
    assert router._gy == peer_rect.bottom - 1, router._gy  # noqa: SLF001
    assert peer_rect.x <= router._gx < peer_rect.right  # noqa: SLF001


def test_clamped_move_never_leaves_peer_screen():
    """模拟位置必须**始终**落在对端屏幕内 —— 这是坐标一致性的底线。"""
    router, _rec = make_router(local=(0, 0, 1280, 800), peer=(0, 0, 1920, 1080))
    enter_remote(router)
    peer_rect = router.layout.peer_rect_global()
    for step in range(1, 600):
        router._handle_move(move_event(0, 0, 7 * (1 if step % 2 else -1), 3 * (1 if step % 3 else -1)))
        assert peer_rect.contains(router._gx, router._gy), (  # noqa: SLF001
            "第 %d 步跑出对端屏幕: (%d, %d)" % (step, router._gx, router._gy)  # noqa: SLF001
        )


def test_peer_cursor_resets_on_each_entry():
    """每次进入都要按新的进入比例重算基准位置。

    对端在右边时进入的是对端的**左边缘**，所以变化的是 y（沿边比例）。
    """
    router, _rec = make_router(local=(0, 0, 1280, 800), peer=(0, 0, 1920, 1080))

    enter_remote(router, ratio=0.2)
    first = (router._gx, router._gy)  # noqa: SLF001
    for _ in range(50):
        router._handle_move(move_event(0, 0, 50, 0))
    router.recapture("测试")

    enter_remote(router, ratio=0.8)
    second = (router._gx, router._gy)  # noqa: SLF001
    assert second != first, "应该重新按入口点设置基准"
    assert second[1] > first[1], "比例变大 -> 入口点应该更靠下"


def test_peer_outer_edge_is_just_a_screen_edge():
    """**回归测试**：对端屏幕的**外**边界只钳位，不交回控制权。

    真机现象（用户原话）："从机的物理鼠标移动到主机顶端会跳回从机，
    但实际主机顶端应该是无法穿越的边界。"

    早期这里有一条"推到外边界就交回控制权"的权宜逻辑，那是在"原路返回"还没实现
    时的绕路方案。现在共享边已经能正常返回（见
    `test_pushing_back_to_the_shared_edge_returns_control`），这条逻辑只剩副作用。
    """
    router, _rec = make_router(local=(0, 0, 1280, 800), peer=(0, 0, 1920, 1080))
    enter_remote(router)

    for _ in range(300):
        router._handle_move(move_event(0, 0, 100, 0))

    assert router.mode is Mode.REMOTE, "推到对端外边界不该交回控制权，只能钳位"
    assert router.stats["move_clamped"] > 0, "应当有钳位发生"


def test_pushing_back_to_the_shared_edge_returns_control():
    """**回归测试**：沿原路推回共享边必须交回控制权。

    真机现象（用户原话）："从机的物理鼠标从顶端进入主机，没问题；但需要从主机
    顶端才能回到从机。" —— 也就是**只有**推到对端屏幕的**外**边界才能回来，
    必须绕到对端屏幕的另一头碰一下。

    原因：模型被钳在对端矩形内，推到近边就停住了，既越不出去、也没有任何判定
    去识别"用户想原路返回"。`layout.detect_return` 写了却从来没被调用过。
    """
    router, _rec = make_router(local=(0, 0, 1920, 1080), peer=(0, 0, 1920, 1080), position="right")
    enter_remote(router, ratio=0.5)

    #: 先深入对端，但别顶到外边界（否则会走"外边界交回"那条路）
    for _ in range(20):
        router._handle_move(move_event(0, 0, 40, 0))
    assert router.mode is Mode.REMOTE, "深入过程中不该交回"
    assert router.stats["shared_edge_returned"] == 0

    #: 再原路推回来
    for _ in range(40):
        router._handle_move(move_event(0, 0, -40, 0))
        if router.stats["shared_edge_returned"]:
            break

    assert router.stats["shared_edge_returned"] >= 1, "沿原路推回共享边应当交回控制权"
    assert router.mode is Mode.LOCAL


def test_immediate_jitter_after_entry_does_not_bounce_back():
    """刚进入对端时的一个抖动不能立刻把控制权拿回来。

    入口点本来就贴在共享边上（`warp_inset_px`），所以"原路返回"的判定必须等
    用户真的深入对端屏幕之后才生效，否则会在共享边上反复弹跳。
    """
    router, _rec = make_router(local=(0, 0, 1920, 1080), peer=(0, 0, 1920, 1080), position="right")
    enter_remote(router, ratio=0.5)
    for _ in range(5):
        router._handle_move(move_event(0, 0, -5, 0))
    assert router.stats["shared_edge_returned"] == 0
    assert router.mode is Mode.REMOTE


# --------------------------------------------------------------------- 键盘转发


def test_keys_forward_after_entering_remote():
    """最基本的一条：光标在对端时，普通按键必须被转发。

    这是用户报的"键盘没被映射到被控端"的直接回归测试。
    """
    router, rec = make_router()
    enter_remote(router)

    router._handle_key(key_event(ord("A")))
    router._handle_key(key_event(ord("A"), down=False))

    forwarded = [body for mtype, body in rec.frames if mtype == 0x13]
    assert len(forwarded) == 2
    assert forwarded[0]["vk"] == ord("A") and forwarded[0]["down"] is True
    assert forwarded[1]["down"] is False


def test_keys_not_forwarded_in_local_mode():
    router, rec = make_router()
    router._handle_key(key_event(ord("A")))
    assert not [f for f in rec.frames if f[0] == 0x13]
    assert router.stats["key_blocked_local"] == 1


def test_shift_letter_is_forwarded():
    """按下 Shift 之后字母仍要转发 —— 热键判定不能把 Shift+A 误判成本机热键。"""
    router, rec = make_router(local_hotkeys=["ctrl+shift+esc"])
    enter_remote(router)
    router._handle_key(key_event(w.VK_LSHIFT))
    router._handle_key(key_event(ord("A")))
    vks = [body["vk"] for mtype, body in rec.frames if mtype == 0x13]
    assert w.VK_LSHIFT in vks
    assert ord("A") in vks, "Shift+A 必须被转发"


def test_local_hotkey_blocks_its_own_segment():
    """本机热键（ctrl+shift+esc）整段不转发，直到修饰键松开。"""
    router, rec = make_router(local_hotkeys=["ctrl+shift+esc"])
    enter_remote(router)

    def forwarded_vks():
        return [body["vk"] for mtype, body in rec.frames if mtype == 0x13]

    # 只按下 Ctrl 时还应该转发（否则用户按 Ctrl 就没反应了）
    router._handle_key(key_event(w.VK_LCONTROL))
    assert w.VK_LCONTROL in forwarded_vks()

    # 再按 Shift，ctrl+shift+esc 的组合成立 -> 进入拦截段
    router._handle_key(key_event(w.VK_LSHIFT))
    router._handle_key(key_event(w.VK_ESCAPE))
    router._handle_key(key_event(ord("Q")))  # 这段里的其它键也不该过去

    vks = forwarded_vks()
    assert w.VK_ESCAPE not in vks, "本机热键的主键不能被转发"
    assert ord("Q") not in vks, "热键段内的其它键也不能被转发"


def test_win_hotkey_does_not_swallow_everything():
    """即使把纯修饰键 "win" 配成本机热键，它也不能把后续所有键都吞掉。

    这里对应的是归一化 bug 的另一面：`_holding_local_hotkey` 被错误置位之后
    会一直拦到修饰键松开，用户感觉就是"键盘完全没映射过去"。
    """
    router, rec = make_router(local_hotkeys=["win"])
    enter_remote(router)

    # 没有按 Win 键时，普通按键必须正常转发
    router._handle_key(key_event(ord("A")))
    vks = [body["vk"] for mtype, body in rec.frames if mtype == 0x13]
    assert ord("A") in vks


def test_single_modifier_combo_swallows_that_modifier():
    """**这是 `local_hotkeys` 里不能放 `win+l` / `alt+tab` 的原因**，写成测试钉住。

    拦截策略是"整段拦下"：修饰键一齐全就进入拦截段。而 `win+l` 的修饰键只有
    Win 一个，所以**按下 Win 的瞬间**条件就成立了 —— Win 键本身被吞，
    之后到松开为止所有键都被吞。真机上用户报的就是"Win 键不能映射"。

    要保住本机组合，必须用两个以上修饰键（如 `ctrl+alt+del`）。
    """
    router, rec = make_router(local_hotkeys=["win+l"])
    enter_remote(router)
    router._handle_key(key_event(w.VK_LWIN))
    vks = [body["vk"] for mtype, body in rec.frames if mtype == 0x13]
    assert w.VK_LWIN not in vks, "单个修饰键的组合会把这个修饰键也吞掉（所以才不能这么配）"

    #: 换成两个修饰键的组合，单独按其中一个修饰键不受影响
    router2, rec2 = make_router(local_hotkeys=["ctrl+alt+del"])
    enter_remote(router2)
    router2._handle_key(key_event(w.VK_LCONTROL))
    vks2 = [body["vk"] for mtype, body in rec2.frames if mtype == 0x13]
    assert w.VK_LCONTROL in vks2, "两个修饰键的组合，单独按 Ctrl 必须照常转发"


def test_media_keys_are_filtered_by_default():
    router, rec = make_router(forward_media_keys=False)
    enter_remote(router)
    router._handle_key(key_event(w.VK_VOLUME_UP))
    assert not [f for f in rec.frames if f[0] == 0x13]
    assert router.stats["key_blocked_media"] == 1


def test_media_keys_forwarded_when_enabled():
    router, rec = make_router(forward_media_keys=True)
    enter_remote(router)
    router._handle_key(key_event(w.VK_VOLUME_UP))
    vks = [body["vk"] for mtype, body in rec.frames if mtype == 0x13]
    assert w.VK_VOLUME_UP in vks


def test_share_keyboard_off_blocks_forwarding():
    router, rec = make_router(share_keyboard=False)
    enter_remote(router)
    router._handle_key(key_event(ord("A")))
    assert not [f for f in rec.frames if f[0] == 0x13]
    assert router.stats["key_blocked_share_off"] == 1


def test_recapture_hotkey_triggers_callback():
    router, rec = make_router()
    router._handle_key(key_event(w.VK_HOME))
    # 单独按 Home 不该触发（需要 Ctrl+Alt 一起按）
    assert rec.recaptures == 0

    router._handle_key(key_event(w.VK_HOME))  # 再按一次仍然不触发
    assert rec.recaptures == 0


def test_toggle_hotkey_binding_is_registered():
    router, _rec = make_router()
    specs = [(b.spec, b.action) for b in router._bindings]
    assert ("ctrl+alt+home", "recapture") in specs
    assert ("ctrl+alt+pause", "toggle") in specs


# --------------------------------------------------------------------- 自定义光标


def test_recapture_releases_keys_and_notifies():
    """回拉时必须释放对端残留按键，否则本机点击会变成 Ctrl+点击。"""
    router, rec = make_router()
    enter_remote(router)
    router.recapture("测试")
    assert router.mode is Mode.LOCAL
    assert rec.releases >= 1
    assert rec.leaves == 1
    assert any(f[0] == 0x17 for f in rec.frames), "应该发出 RELEASE_ALL"


def test_peer_enter_warps_local_cursor():
    router, rec = make_router()
    router.on_peer_enter({"ratio": 0.5, "from": "right"})
    assert rec.warps, "对端进入本机时要按几何算出的入口点摆放本机光标"
    x, y = rec.warps[-1]
    assert x == 1920 - 1 - 8, "应该落在对端对应的那条边（本机右边）"


def test_disabled_router_does_not_switch():
    """暂停共享之后不能再穿越 —— 否则用户按了暂停，光标却还是会跑过去。"""
    router, _rec = make_router()
    router.set_enabled(False)
    assert router.enabled is False
    push_to_edge(router, 500)
    assert router.mode is not Mode.REMOTE
    assert router.mode is Mode.PAUSED


def test_peer_not_ready_does_not_switch():
    """对端没连上时不能穿越，否则光标会"消失"。"""
    router, _rec = make_router()
    router.set_peer_ready(False)
    router._handle_move(move_event(1917, 500, 2, 0))
    router._handle_move(move_event(1921, 500, 4, 0))
    assert router.mode is Mode.LOCAL


def test_status_is_readable():
    router, _rec = make_router()
    text = router.status()
    assert "模式=" in text
    assert "共享=" in text


# ------------------------------------------------- 绝对落点（指针加速的修复）


def test_enter_tells_peer_the_opposite_side():
    """**回归测试**：ENTER 里的 `from` 必须是**对端**被进入的那条边。

    也就是本机方位的**反方向**：对端在本机右边 -> 本机从自己的右边界出去 ->
    落在对端的**左**边界。

    踩过的坑：早期发的是本机自己的 `peer_position`。真机追踪数据里，
    主机模型期望对端光标落在 (8, 680)（对端左边缘），而对端把光标摆到了
    (1271, 686)（对端**右**边缘，屏宽 1280）—— 差了整整一个屏宽。
    对端光标从第一帧起就贴在错误的那条边上，只能朝一个方向挪 9 像素，
    然后被主机的模型拉回去，用户看到的就是"弹簧"。
    """
    from netclip.protocol import MsgType

    for position, expect_from in (
        ("right", "left"),
        ("left", "right"),
        ("up", "down"),
        ("down", "up"),
    ):
        router, rec = make_router(position=position)
        router.enter_peer(0.5)
        bodies = [body for mtype, body in rec.frames if mtype == MsgType.ENTER]
        assert bodies, "%s: 应当发出 ENTER" % position
        assert bodies[-1]["from"] == expect_from, (
            "对端在 %s 侧时，应当告诉对端『从它的 %s 边进入』，实际发了 %r"
            % (position, expect_from, bodies[-1]["from"])
        )


def test_move_frame_carries_absolute_peer_position():
    """**回归测试**：每帧移动都必须带**对端坐标系**下的绝对落点。

    为什么不能只发相对位移：Windows 会把注入的相对位移再过一遍"提高指针精确度"
    的加速曲线。真机实测（MouseSpeed=1, MouseSensitivity=10）：

        请求  1px -> 实际   0px   （直接丢失）
        请求 20px -> 实际  42px   （2.1x）
        请求 80px -> 实际 290px   （3.6x）

    于是"主机算出来的位移"和"从机光标实际走的距离"永远对不上，两边模型从第一帧
    起就发散，撞到对端屏幕边缘后被钳住。绝对定位不走这条曲线（同机实测误差 0），
    所以它才是权威落点。
    """
    router, rec = make_router(local=(0, 0, 1280, 800), peer=(0, 0, 1920, 1080), position="right")
    enter_remote(router)

    for _ in range(20):
        router._handle_move(move_event(0, 0, 17, -9))

    assert rec.moves, "应当转发了移动"
    assert len(rec.moves_abs) == len(rec.moves), "每一帧位移都必须同时带绝对落点"
    for x, y in rec.moves_abs:
        assert 0 <= x < 1920 and 0 <= y < 1080, "绝对落点必须落在**对端**屏幕坐标系内"
    assert rec.moves_abs[-1] == (router._peer_x, router._peer_y), "绝对落点要和模型完全一致"


def test_absolute_position_stops_at_peer_edge():
    """顶到对端边界时，绝对落点必须停在边界上，不能越界。"""
    router, rec = make_router(local=(0, 0, 1280, 800), peer=(0, 0, 1920, 1080), position="right")
    enter_remote(router)

    for _ in range(400):
        router._handle_move(move_event(0, 0, 50, 0))

    xs = [x for x, _y in rec.moves_abs]
    assert xs, "应当转发过移动"
    assert max(xs) == 1919, "一路右推应当精确停在对端右边界上"
    assert all(0 <= x < 1920 for x in xs), "任何一帧都不能越出对端屏幕"


def test_peer_local_roundtrip_covers_alignment_offset():
    """**回归测试**：对端坐标 <-> 统一坐标 的换算必须精确互逆。

    覆盖所有四种方位，且用**两块不同尺寸**的屏幕 —— 只有这样非穿越轴上才会
    有 `alignment` 偏移，才可能暴露"只处理穿越轴"的 bug。
    """
    local = Rect(0, 0, 2560, 1440)
    peer = Rect(0, 0, 1920, 1200)
    for position in ("right", "left", "up", "down"):
        layout = ScreenLayout(local, peer, position)
        rect = layout.peer_rect_global()
        for px, py in ((0, 0), (1919, 1199), (960, 600)):
            gx, gy = layout.peer_local_to_global(px, py)
            assert rect.contains(gx, gy), "%s: (%d,%d) 应当落在对端矩形内" % (position, gx, gy)
            assert layout.global_to_peer_local(gx, gy) == (px, py), "%s: 换算必须可逆" % position


def test_absolute_position_accounts_for_alignment_on_the_other_axis():
    """**回归测试**：非穿越轴上的 alignment 偏移必须算进发给对端的绝对落点。

    真机场景：主机 2560x1440、从机 1920x1200、上下叠放、`center` 对齐。
    对端矩形整体偏移 `(2560-1920)//2 = 320` 像素。早期只把穿越轴（y）算对了，
    x 少了这 320，于是发给从机的绝对落点整体右移 320 —— 从机光标被推到屏幕
    右侧钳住，表现为"拉走又被慢慢拉回来"的阻尼弹簧。
    """
    router, rec = make_router(local=(0, 0, 2560, 1440), peer=(0, 0, 1920, 1200), position="down")
    assert router.layout.peer_rect_global().x == 320, "居中对齐应当把对端矩形右移 320"

    enter_remote(router, ratio=0.5)
    #: 进入时模型自己算出的对端落点就该在范围内
    assert 0 <= router._peer_x < 1920, "入口点的 x 必须在对端屏幕坐标系内"

    for _ in range(60):
        router._handle_move(move_event(0, 0, 0, 13))

    xs = [x for x, _y in rec.moves_abs]
    ys = [y for _x, y in rec.moves_abs]
    assert xs and ys, "应当转发了移动"
    assert all(0 <= x < 1920 for x in xs), "绝对落点 x 必须在对端屏幕内，实际最大 %d" % max(xs)
    assert all(0 <= y < 1200 for y in ys), "绝对落点 y 必须在对端屏幕内"
    assert rec.moves_abs[-1] == (router._peer_x, router._peer_y), "落点要和模型完全一致"
