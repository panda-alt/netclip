"""sendinput.api —— 输入注入。

两个关键设计点
--------------
**1. 用 `KEYEVENTF_SCANCODE` 而不是虚拟键**
注入键盘时传扫描码而非 VK：游戏、输入法、以及一些用 `MapVirtualKey` 反查
扫描码的程序对扫描码更敏感，VK 注入在中文输入法下常出现"按键丢失"。

**2. 注入的事件会被自己的钩子再次捕获**
所有注入的事件都带 `dwExtraInfo = INJECT_TAG`，本地钩子回调据此识别并
**原样放行**（不吞掉），否则会形成死循环或把自己注入的输入又转发一遍。
"""

from __future__ import annotations

import ctypes
import logging
import threading
from typing import Iterable, List, Optional, Sequence, Tuple

from . import winapi as w

log = logging.getLogger("netclip.win.inject")

#: 注入事件的身份标记，放进 INPUT.dwExtraInfo。
#: 钩子回调只要看到这个值就知道"这是我自己注入的，不是我产生的输入"。
INJECT_TAG = 0x4E43_4C50  # 'NCLP'

#: SendInput 一次调用的最大批量。
_MAX_BATCH = 64

#: 注入用的互斥锁。SendInput 本身是线程安全的，但我们要检查返回值并记录
#: 错误码（GetLastError 是线程局部的），加锁可以避免多条注入路径互相干扰诊断。
_inject_lock = threading.Lock()

_denied_logged = False


class InjectError(RuntimeError):
    pass


# --------------------------------------------------------------------- 底层


def _send(inputs: Sequence[w.INPUT]) -> int:
    """调用 SendInput，返回实际插入的事件数。"""
    global _denied_logged

    if not inputs:
        return 0
    count = len(inputs)
    array = (w.INPUT * count)(*inputs)
    with _inject_lock:
        ctypes.set_last_error(0)
        sent = int(w.user32.SendInput(count, array, ctypes.sizeof(w.INPUT)))
        err = ctypes.get_last_error()

    if sent != count:
        # 返回 0 且 ERROR_ACCESS_DENIED，通常意味着当前前台窗口是提权窗口
        # （UIPI 拦截）或安全桌面。这是系统策略，不是我们的 bug，所以只提示一次。
        if err == 5 and not _denied_logged:
            _denied_logged = True
            log.warning(
                "SendInput 被拒绝(ERROR_ACCESS_DENIED)：注入目标是提权进程或安全桌面。"
                "以管理员身份运行 netclip 可以解决（注入到管理员窗口需要同等权限）。"
            )
        else:
            log.debug("SendInput 只插入了 %d/%d 个事件（GetLastError=%d）", sent, count, err)
    return sent


def _mouse_input(dx: int, dy: int, data: int, flags: int) -> w.INPUT:
    item = w.INPUT()
    item.type = w.INPUT_MOUSE
    item.mi = w.MOUSEINPUT(int(dx), int(dy), int(data) & 0xFFFFFFFF, int(flags), 0, INJECT_TAG)
    return item


def _key_input(vk: int, scan: int, flags: int) -> w.INPUT:
    item = w.INPUT()
    item.type = w.INPUT_KEYBOARD
    item.ki = w.KEYBDINPUT(int(vk) & 0xFFFF, int(scan) & 0xFFFF, int(flags), 0, INJECT_TAG)
    return item


# --------------------------------------------------------------------- 鼠标


def move_relative(dx: int, dy: int) -> int:
    """相对移动鼠标。这是远程输入的主路径。

    相对移动的好处：不需要知道对端屏幕分辨率，也不需要来回换算绝对坐标；
    对端只要把增量交给系统即可，跨 DPI 缩放也能自然工作。
    """
    if dx == 0 and dy == 0:
        return 0
    return _send([_mouse_input(dx, dy, 0, w.MOUSEEVENTF_MOVE)])


def move_absolute(x: int, y: int, virtual_desktop: bool = False) -> int:
    """绝对移动鼠标到 (x, y)。

    用于"穿越瞬间把光标放到对端边缘"这种需要落点精确的场景。
    坐标会被归一化到 0..65535，这是 `MOUSEEVENTF_ABSOLUTE` 要求的格式。
    """
    if virtual_desktop:
        vx, vy, vw, vh = w.get_virtual_screen_rect()
        flags = w.MOUSEEVENTF_MOVE | w.MOUSEEVENTF_ABSOLUTE | w.MOUSEEVENTF_VIRTUALDESK
    else:
        vx, vy, vw, vh = w.get_screen_rect()
        flags = w.MOUSEEVENTF_MOVE | w.MOUSEEVENTF_ABSOLUTE

    nx = 0xFFFF if vw <= 1 else int(round((x - vx) * 0xFFFF / (vw - 1)))
    ny = 0xFFFF if vh <= 1 else int(round((y - vy) * 0xFFFF / (vh - 1)))
    nx = min(max(nx, 0), 0xFFFF)
    ny = min(max(ny, 0), 0xFFFF)
    return _send([_mouse_input(nx, ny, 0, flags)])


#: 按钮名 -> (按下标志, 抬起标志, XBUTTON 值)
_BUTTON_FLAGS = {
    "left": (w.MOUSEEVENTF_LEFTDOWN, w.MOUSEEVENTF_LEFTUP, 0),
    "right": (w.MOUSEEVENTF_RIGHTDOWN, w.MOUSEEVENTF_RIGHTUP, 0),
    "middle": (w.MOUSEEVENTF_MIDDLEDOWN, w.MOUSEEVENTF_MIDDLEUP, 0),
    "x1": (w.MOUSEEVENTF_XDOWN, w.MOUSEEVENTF_XUP, w.XBUTTON1),
    "x2": (w.MOUSEEVENTF_XDOWN, w.MOUSEEVENTF_XUP, w.XBUTTON2),
}


def button(name: str, down: bool) -> int:
    flags = _BUTTON_FLAGS.get(name)
    if flags is None:
        log.warning("未知鼠标按钮: %s", name)
        return 0
    down_flag, up_flag, xdata = flags
    return _send([_mouse_input(0, 0, xdata, down_flag if down else up_flag)])


def wheel(delta: int, horizontal: bool = False) -> int:
    """滚轮。delta 以 WHEEL_DELTA(120) 为单位，正数向上/向右。"""
    if delta == 0:
        return 0
    flag = w.MOUSEEVENTF_HWHEEL if horizontal else w.MOUSEEVENTF_WHEEL
    # mouseData 是有符号的 DWORD，负数要按补码传递
    return _send([_mouse_input(0, 0, delta & 0xFFFFFFFF, flag)])


def wheel_raw(windows_delta: int, horizontal: bool = False) -> int:
    """直接传 Windows 原始 delta（高精度滚轮可能不是 120 的整数倍）。"""
    return wheel(windows_delta, horizontal)


# --------------------------------------------------------------------- 键盘


def _to_scancode(vk: int) -> int:
    return int(w.user32.MapVirtualKeyW(int(vk) & 0xFF, 0))  # MAPVK_VK_TO_VSC


#: `MapVirtualKeyW` **不认**的虚拟键 -> 真实扫描码。
#:
#: Windows 键最典型：`MapVirtualKeyW(VK_LWIN, MAPVK_VK_TO_VSC)` 返回 0x5B，
#: 而 Win 键的真实扫描码是 **0x5B / 0x5C 加扩展标志**；0x5B 不带扩展标志时
#: 是另一个键。用错的结果就是"按 Win 在对端没反应"。
#: 数值来自 PC/AT 键盘规范。
_SCANCODE_OVERRIDES = {
    w.VK_LWIN: 0x5B,
    w.VK_RWIN: 0x5C,
    w.VK_RMENU: 0x38,
    w.VK_RCONTROL: 0x1D,
    w.VK_DIVIDE: 0x35,
    w.VK_SNAPSHOT: 0x37,
    w.VK_NUMLOCK: 0x45,
}


def _to_scancode_for(event_vk: int, event_scan: int) -> int:
    """优先使用钩子**上报的原始扫描码**。

    这是"原样搬运同一份事件流"最可靠的做法：钩子的 `KBDLLHOOKSTRUCT.scanCode`
    是键盘控制器直接给的值，**与键盘布局无关**；而
    `MapVirtualKey(VK, MAPVK_VK_TO_VSC)` 会受当前键盘布局/输入法影响，
    在某些布局下会给出不同的结果 —— 表现就是"我按 A，对端出来 B"。

    只有在事件里没有扫描码（少数合成事件）时才回退去查表。
    """
    override = _SCANCODE_OVERRIDES.get(int(event_vk))
    if override is not None:
        return override
    if event_scan:
        return int(event_scan) & 0xFF
    return _to_scancode(event_vk)


_EXTENDED_VKS = frozenset(
    {
        w.VK_RMENU,
        w.VK_RCONTROL,
        w.VK_INSERT,
        w.VK_DELETE,
        w.VK_HOME,
        w.VK_END,
        w.VK_PRIOR,
        w.VK_NEXT,
        w.VK_LEFT,
        w.VK_RIGHT,
        w.VK_UP,
        w.VK_DOWN,
        w.VK_NUMLOCK,
        w.VK_SNAPSHOT,
        w.VK_DIVIDE,
        w.VK_LWIN,
        w.VK_RWIN,
    }
)


#: **有 E0（扩展）版本的扫描码。**
#:
#: Windows 的"扩展标志"就是"这个键按了 E0 前缀"。只有下面这些扫描码有 E0 版本，
#: 其余的带上扩展标志就是**无效组合**。
#:
#: 真机实测（`python -m netclip.selftest keys`）：右 Shift 被钩子报成
#: `vk=0xA1 scan=0x36 ext=True`，而左 Shift 是 `ext=False`。可 `E0 36` 在 PC/AT
#: 规范里**根本没有对应的键**（小键盘 `/` 是 `E0 35`，小键盘 Enter 是 `E0 1C`）——
#: 照原样注入，用户按右 Shift、对端出来的是别的东西，现象就是"右 Shift 没被映射"。
#:
#: **注意这里面很多扫描码是"左右成对"的歧义键**，例如 `0x35`：非扩展是主键盘的
#: `/`（也就是 `Shift+/` = `?`），扩展才是小键盘的 `/`。所以这张表只用来**否定**
#: 非法组合，绝不能反过来拿它去**强制**加扩展标志 —— 那样会把主键盘 `/` 变成
#: 小键盘 `/`（真机上就是这么把问号打没的）。
_EXTENDED_SCANCODES = frozenset(
    {
        0x1C,  # 主键盘 Enter / 小键盘 Enter
        0x1D,  # 左 Ctrl / 右 Ctrl
        0x35,  # 主键盘 / / 小键盘 /
        0x37,  # 小键盘 * / PrintScreen
        0x38,  # 左 Alt / 右 Alt
        0x47,  # 小键盘 7 / Home
        0x48,  # 小键盘 8 / ↑
        0x49,  # 小键盘 9 / PageUp
        0x4B,  # 小键盘 4 / ←
        0x4D,  # 小键盘 6 / →
        0x4F,  # 小键盘 1 / End
        0x50,  # 小键盘 2 / ↓
        0x51,  # 小键盘 3 / PageDown
        0x52,  # 小键盘 0 / Insert
        0x53,  # 小键盘 . / Delete
        0x5B,  # 左 Win
        0x5C,  # 右 Win
        0x5D,  # Menu
    }
)


def normalize_extended(scan: int, extended: bool) -> bool:
    """**只做减法**：把来源给的扩展标志收敛成这台键盘上合法的值。

    规则只有一条 —— 扫描码**没有** E0 版本时，扩展标志一定是错的，清掉：

      * `0x36`（右 Shift）：没有 E0 版本 → 清掉。**这是真机修过的问题**：
        系统给了 `ext=True`，照原样注入就成了 `E0 36`，那不是一个有效的键。
      * `0x35`（主键盘 `/`）：**有** E0 版本（小键盘 `/`）→ 照来源给的标志走。
        `Shift+0x35` 就是 `?`。

    **刻意不做加法**（不去"补上"扩展标志）。曾经试过一条"有些扫描码没有非扩展
    含义，必须强制扩展"的规则，把 `0x35` 也列了进去 —— 结果主键盘 `/` 被当成
    小键盘 `/`，`Shift+/` 打不出问号了。扫描码的"非扩展含义"是不是存在，
    不能靠猜。

    为什么不改钩子那一侧：钩子上报的是**系统给的原样值**，留着它有利于诊断
    （`selftest keys` 要能看到那个反常的标志）。

    这是一条**通用规则**，不是为 Shift 打的补丁：任何来源（钩子、对端转发、
    合成事件）给出的非法组合都会被纠正。
    """
    scan = int(scan) & 0xFF
    if scan and scan not in _EXTENDED_SCANCODES:
        return False
    return bool(extended)


def key_event(vk: int, down: bool, scan: Optional[int] = None, extended: Optional[bool] = None) -> int:
    """按下或抬起一个键。

    `vk`/`scan`/`extended` 都应该直接用钩子上报的原始值（见 `_to_scancode_for`）。
    用扫描码注入而不是虚拟键，是因为扫描码是"物理按键"的编号，
    不受目标机键盘布局影响。
    """
    scancode = _to_scancode_for(vk, scan or 0)
    if extended is None:
        extended = vk in _EXTENDED_VKS
    #: 收敛扩展标志。**这一步不能省** —— 真机上系统会给右 Shift 置上这个标志，
    #: 直接照抄就会把 `E0 36` 打在线上，那不是一个有效的键。
    extended = normalize_extended(scancode, bool(extended))

    if scancode:
        flags = w.KEYEVENTF_SCANCODE
        if extended:
            flags |= w.KEYEVENTF_EXTENDEDKEY
    else:
        # 少数虚拟键没有扫描码（如某些媒体键），退回 VK 注入
        flags = 0
        scancode = 0
    if not down:
        flags |= w.KEYEVENTF_KEYUP
    return _send([_key_input(vk, scancode, flags)])


def key_press(vk: int, scan: Optional[int] = None) -> None:
    key_event(vk, True, scan)
    key_event(vk, False, scan)


def key_press_batch(events: Iterable[Tuple[int, bool]]) -> int:
    """批量注入多个按键事件，减少 SendInput 调用次数。

    `events` 是 (vk, down) 序列。用于"粘贴"这类需要按下+抬起多个键的场景。
    """
    items: List[w.INPUT] = []
    for vk, down in events:
        scancode = _to_scancode(vk)
        flags = w.KEYEVENTF_SCANCODE if scancode else 0
        if normalize_extended(scancode, vk in _EXTENDED_VKS):
            flags |= w.KEYEVENTF_EXTENDEDKEY
        if not down:
            flags |= w.KEYEVENTF_KEYUP
        items.append(_key_input(vk, scancode, flags))
        if len(items) >= _MAX_BATCH:
            _send(items)
            items = []
    if items:
        _send(items)
    return 0


def type_unicode(text: str) -> int:
    """用 `KEYEVENTF_UNICODE` 直接输入文本（不经过键盘布局）。

    用于对方键盘布局与文本不匹配的兜底场景（例如从网页复制了中文路径，
    但需要"打字"到对端的输入框里）。日常粘贴不走这里。
    """
    items: List[w.INPUT] = []
    for ch in text:
        code = ord(ch)
        if code > 0xFFFF:
            # 需要 UTF-16 代理对
            code -= 0x10000
            high = 0xD800 + (code >> 10)
            low = 0xDC00 + (code & 0x3FF)
            for unit in (high, low):
                items.append(_key_input(0, unit, w.KEYEVENTF_UNICODE))
                items.append(_key_input(0, unit, w.KEYEVENTF_UNICODE | w.KEYEVENTF_KEYUP))
        else:
            items.append(_key_input(0, code, w.KEYEVENTF_UNICODE))
            items.append(_key_input(0, code, w.KEYEVENTF_UNICODE | w.KEYEVENTF_KEYUP))
        if len(items) >= _MAX_BATCH:
            _send(items)
            items = []
    if items:
        _send(items)
    return 0


# --------------------------------------------------------------------- 释放


#: 需要在断线/切换时检查并释放的鼠标按钮
_MOUSE_BUTTONS = (
    (w.VK_LBUTTON, "left"),
    (w.VK_RBUTTON, "right"),
    (w.VK_MBUTTON, "middle"),
    (w.VK_XBUTTON1, "x1"),
    (w.VK_XBUTTON2, "x2"),
)

#: 需要检查并释放的键盘修饰键与常见按键
_STICKY_VKS = (
    w.VK_LCONTROL,
    w.VK_RCONTROL,
    w.VK_LSHIFT,
    w.VK_RSHIFT,
    w.VK_LMENU,
    w.VK_RMENU,
    w.VK_LWIN,
    w.VK_RWIN,
)


def release_all_buttons(force: bool = False) -> List[str]:
    """释放所有按下的鼠标按钮和修饰键。

    这是"断线/切回本机"时**必须**做的一件事：如果对端还留着 Ctrl 按下，
    回到本机后所有鼠标点击都会变成 Ctrl+点击，用户体验就是"卡住了"。

    `force=False` 时只释放"本机物理状态显示确实按着"的键（GetAsyncKeyState）。

    **鼠标按钮永远只释放确实按着的那些，`force` 也救不了它。** 真机上踩过：
    交回控制权时会发 `RELEASE_ALL`，对端用 `force=True` 无条件补了一条
    `WM_RBUTTONUP` —— 而资源管理器是**收到 RBUTTONUP 就弹右键菜单**的，
    于是用户看到的是"鼠标离开从机时莫名其妙点了一下右键"。
    单独一条修饰键 UP 没有副作用，所以只有修饰键允许 `force` 无条件补。
    """
    released: List[str] = []

    for vk, name in _MOUSE_BUTTONS:
        if w.is_key_down(vk):
            if button(name, False):
                released.append(name)

    for vk in _STICKY_VKS:
        if force or w.is_key_down(vk):
            if key_event(vk, False):
                released.append(_vk_name(vk))

    if released:
        log.info("已释放残留按键: %s", ", ".join(released))
    return released


_VK_NAMES = {
    w.VK_LCONTROL: "LCtrl",
    w.VK_RCONTROL: "RCtrl",
    w.VK_LSHIFT: "LShift",
    w.VK_RSHIFT: "RShift",
    w.VK_LMENU: "LAlt",
    w.VK_RMENU: "RAlt",
    w.VK_LWIN: "LWin",
    w.VK_RWIN: "RWin",
}


def _vk_name(vk: int) -> str:
    return _VK_NAMES.get(vk, "VK_%02X" % vk)


def reset_tag_logging() -> None:
    """测试用：让 ERROR_ACCESS_DENIED 的提示可以再次打印。"""
    global _denied_logged

    _denied_logged = False


__all__ = [
    "INJECT_TAG",
    "InjectError",
    "button",
    "key_event",
    "key_press",
    "key_press_batch",
    "move_absolute",
    "move_relative",
    "release_all_buttons",
    "turn_off_key_if_down",
    "type_unicode",
    "wheel",
    "wheel_raw",
]


def turn_off_key_if_down(vk: int) -> bool:
    """如果某个键当前按下，就把它释放。返回是否做了动作。"""
    if w.is_key_down(vk):
        return bool(key_event(vk, False))
    return False
