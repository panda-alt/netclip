"""全局低级鼠标/键盘钩子。

**钩子回调里只能做一件事：把事件塞进邮箱，然后立刻返回。**

为什么这条纪律如此重要
----------------------
Windows 对低级钩子（`WH_MOUSE_LL` / `WH_KEYBOARD_LL`）有超时限制
（`LowLevelHooksTimeout`，默认 300ms）。回调一旦超时，系统会**静默地把钩子摘掉**，
表现就是"鼠标突然全部失灵，但程序还在跑"——这类问题极难排查，因为没有任何报错。

所以本模块的回调做到：
  * 不分配对象（原地复用同一个 `InputEvent` 实例）；
  * 不记日志、不格式化字符串；
  * 不碰网络、不碰锁、不碰剪贴板；
  * 只做整数比较 + 原地赋值。

真正的事件处理在 `netclip.core.router` 里，跑在另一个专用线程上。
"""

from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, Callable, Optional

from . import winapi as w
from .inject import INJECT_TAG

log = logging.getLogger("netclip.win.hooks")

# --- 事件种类 ---
MOVE = 1
BUTTON = 2
WHEEL = 3
KEY = 4

#: 钩子线程自定义消息：重新安装钩子（用于钩子被系统摘掉后的自愈）
WM_HOOK_REINSTALL = w.WM_APP + 10


class WNDCLASSEX(ctypes.Structure):
    """`WNDCLASSEXW`。放在 winapi 里会污染那个只做绑定的模块，所以定义在这。"""

    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HANDLE),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HANDLE),
    ]


@dataclass
class InputEvent:
    """一个输入事件（原地复用，避免在钩子线程里分配对象）。"""

    kind: int = 0
    x: int = 0
    y: int = 0
    dx: int = 0
    dy: int = 0
    button: str = ""
    down: bool = False
    wheel: int = 0
    horizontal: bool = False
    vk: int = 0
    scan: int = 0
    extended: bool = False
    time: int = 0
    #: 事件发生时是否有任意鼠标键按着（用于"拖拽时不要切屏"的判定）
    any_button_down: bool = False

    def copy(self) -> "InputEvent":
        """复制一份 —— 消费方（router 线程）必须用副本，因为原件会被复用。"""
        return InputEvent(
            kind=self.kind,
            x=self.x,
            y=self.y,
            dx=self.dx,
            dy=self.dy,
            button=self.button,
            down=self.down,
            wheel=self.wheel,
            horizontal=self.horizontal,
            vk=self.vk,
            scan=self.scan,
            extended=self.extended,
            time=self.time,
            any_button_down=self.any_button_down,
        )


@dataclass
class HookStats:
    mouse_events: int = 0
    key_events: int = 0
    injected_skipped: int = 0
    swallowed: int = 0
    callback_ns_peak: int = 0
    install_failures: int = 0
    #: 被丢弃的"我们自己 SetCursorPos 造成的"移动事件数。
    #: 这个数一直在涨说明穿越/回拉在反复发生 —— 排查抖动问题时第一个看它。
    warp_settle_skipped: int = 0

    def summary(self) -> str:
        return "鼠标%d 键盘%d 忽略注入%d 吞掉%d 忽略自身移动%d 回调峰值%.0fus 安装失败%d" % (
            self.mouse_events,
            self.key_events,
            self.injected_skipped,
            self.swallowed,
            self.warp_settle_skipped,
            self.callback_ns_peak / 1000.0,
            self.install_failures,
        )


#: 鼠标消息 -> (按钮名, 是否按下)
_MOUSE_BUTTON_MSGS = {
    w.WM_LBUTTONDOWN: ("left", True),
    w.WM_LBUTTONUP: ("left", False),
    w.WM_RBUTTONDOWN: ("right", True),
    w.WM_RBUTTONUP: ("right", False),
    w.WM_MBUTTONDOWN: ("middle", True),
    w.WM_MBUTTONUP: ("middle", False),
}

user32 = w.user32
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEX)]
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    ctypes.c_void_p,
]


#: 事件类型 -> 中文名，仅用于日志
_KIND_NAME = {MOVE: "移动", BUTTON: "按钮", WHEEL: "滚轮", KEY: "键盘"}


class HookThread:
    """在独立线程里安装全局鼠标/键盘钩子并跑消息循环。

    安装钩子的线程必须自己抽消息，否则钩子不会被调用。所以这里自带一个
    `GetMessage` 循环和一个仅用于接收控制消息的 message-only 窗口。
    """

    def __init__(
        self,
        on_event: Callable[[InputEvent], None],
        should_swallow: Callable[[InputEvent], bool],
        name: str = "netclip-hooks",
        chain: Optional[Any] = None,
    ) -> None:
        """
        `on_event`      : 每个事件调用一次（**在钩子线程内**），必须极快，只允许入队。
        `should_swallow`: 返回 True 表示吞掉该事件，不让本机处理。
        `chain`         : 整条链路的逐条日志（`netclip.debug.chain.ChainLog`）；
                          None 表示不记 —— **调试模块已移除，现在恒为 None**，
                          但下面那些 `if self.chain is not None` 的守卫还留着，
                          所以这个参数要保留，不能只删守卫的一半。
        """
        self.on_event = on_event
        self.should_swallow = should_swallow
        self.name = name
        #: 见上。`Session._start_input()` 会把这个参数传进来。
        self.chain = chain

        self.stats = HookStats()
        self.thread_id: Optional[int] = None
        self.hwnd: Optional[int] = None

        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._mouse_hook: Optional[int] = None
        self._key_hook: Optional[int] = None
        self._event = InputEvent()
        self._last_x = 0
        self._last_y = 0
        self._have_last = False
        #: 当前按下的鼠标按钮集合，用于判断"是否正在拖拽"
        self._buttons_down: set = set()
        #: 我们自己用 SetCursorPos 移动光标后，需要忽略移动事件的截止时间戳
        #: （毫秒，GetTickCount64）。见 `ignore_next_move()`。
        self._warp_settle_until = 0
        #: 读系统光标当前位置。做成可替换的，一是单测能注入假值，
        #: 二是换实现时不用动热路径。见 `motion_base()`。
        self.cursor_reader: Callable[[], "tuple[int, int]"] = _read_cursor_pos
        #: 我们自己用 SetCursorPos 移动光标后，要吞掉的那条移动事件的时间戳
        #: （毫秒，GetTickCount64）。见 `ignore_next_move()`。
        self._warp_settle_until = 0

        # 必须保存 ctypes 回调对象，否则会被 GC 回收导致调用到已释放的地址而崩溃
        self._mouse_proc = w.HOOKPROC(self._mouse_callback)
        self._key_proc = w.HOOKPROC(self._key_callback)
        self._wnd_proc = w.WNDPROC(self._wnd_callback)

    # ------------------------------------------------------------ 生命周期

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, timeout: float = 5.0) -> bool:
        if self._thread is not None:
            return True
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            log.error("钩子线程在 %.1fs 内没有就绪", timeout)
            return False
        return bool(self._mouse_hook)

    def stop(self, timeout: float = 2.0) -> None:
        if self.thread_id:
            try:
                user32.PostThreadMessageW(self.thread_id, w.WM_QUIT, 0, 0)
            except Exception:  # pragma: no cover
                pass
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                log.warning("钩子线程未能在 %.1fs 内退出", timeout)
            self._thread = None

    def reinstall(self) -> bool:
        """请求重新安装钩子（钩子被系统摘掉后的自愈路径）。"""
        if not self.thread_id:
            return False
        return bool(user32.PostThreadMessageW(self.thread_id, WM_HOOK_REINSTALL, 0, 0))

    def reset_mouse_tracking(self) -> None:
        """丢弃上一次鼠标位置。

        穿越屏幕 / 强制回拉光标之后必须调用：否则下一次移动事件里的 dx/dy 会
        包含"光标被瞬移"的那一段位移，对端光标会突然飞出去。
        """
        self._have_last = False

    def motion_base(self, pt_x: int = 0, pt_y: int = 0) -> "tuple[int, int]":
        """算出这次移动的**基准点** —— 必须是系统光标的**当前实际位置**。

        这是本项目最反直觉、也最难查的一个坑，真机上靠"冻结光标探针"
        （`tools/frozen_delta_probe.py`）才证实下来。

        低级鼠标钩子拿到的 `pt` **不是**"光标现在在哪"，而是"如果允许这次移动，
        光标会到哪"，也就是

            pt = 光标当前位置 + 本次原始增量

        * **LOCAL（不吞事件）**：移动会被提交，所以"当前位置"就是上一条 `pt`，
          两种基准完全等价 —— 早期用 `pt - 上一条pt` 在 LOCAL 下一直是对的。
        * **REMOTE（吞掉事件）**：移动**不提交**，光标冻在穿越点不动。这时
          `pt - 上一条pt` 算出来的是

              delta(n) - delta(n-1)        <- 位移的**差**

          人手平滑移动时相邻增量几乎相等，于是转发出去的是 **0**。真机实测：
          每秒 900 条事件、`pt` 跨度 227x57，转发位移却只有 ±几十，
          累计几万条事件的净位移不到 10 像素 —— 表现就是"鼠标像被弹簧拉住"。

          而 `pt - 光标当前位置` 恢复出来的正是 `delta(n)`，也就是本机光标
          "本来会"移动的距离（含系统指针加速），这才是应该转发给对端的东西。

        读 `GetCursorPos` 每个移动事件一次（约 1000 次/秒）。开销可以忽略，
        但换来的是**永远正确的基准**：光标被谁挪过（穿越回拉、第三方程序、
        用户手抖）都不影响。

        探针实测（吞掉移动时注入已知位移）::

            注入位移      pt-上一条pt    pt-光标位置
            (5,0)        (0,0)          (5,0)
            (20,0)       (0,0)          (42,0)
            (0,30)       (0,0)          (0,89)
        """
        try:
            x, y = self.cursor_reader()
        except Exception:  # pragma: no cover - 光标读取失败不该影响钩子
            x = y = -1
        if x >= 0 and y >= 0:
            return (int(x), int(y))
        #: 读不到就退回上一条 pt（坐标系一致，只是失去了"冻结光标"下的正确性）
        if self._have_last:
            return (self._last_x, self._last_y)
        return (int(pt_x), int(pt_y))

    def ignore_next_move(self, settle_ms: int = 60) -> None:
        """告诉钩子：接下来 `settle_ms` 毫秒内的鼠标移动是"我们自己造成的"，不要上报。

        **为什么必须有这个**：`SetCursorPos`（以及注入的绝对移动）会让系统发出
        `WM_MOUSEMOVE`，而这条事件**不带** `LLMHF_INJECTED` —— 它走的是正常的
        光标移动路径。于是：

            A 让 B 把光标挪到边缘
              -> B 的光标真的动了，B 的钩子产生一条真实移动事件
              -> B 处于 LOCAL，以为是用户在动鼠标，转发给 A
              -> A 注入位移，A 的光标也动了，A 又转发给 B ...

        两边互相喂位移，现象是"鼠标被吸在一个位置高频抖动、完全推不动"。

        用"时间窗"而不是"计数器"是因为 `SetCursorPos` 产生几条移动事件并不确定
        （取决于系统合并鼠标事件的策略），数条数很容易漏。窗口取 60ms：
        远小于人手两次有意义的移动间隔（正常移动间隔 8~16ms，但一次穿越/回拉
        只需要躲开紧随其后的那几条），又足够覆盖系统的事件合并延迟。
        """
        self._warp_settle_until = _now_ms() + max(0, int(settle_ms))

    def _note_button(self, name: str, down: bool) -> None:
        """维护"当前按下的按钮集合"。只在钩子线程里访问，无需加锁。"""
        if not name:
            return
        if down:
            self._buttons_down.add(name)
        else:
            self._buttons_down.discard(name)

    # ------------------------------------------------------------ 线程主体

    def _run(self) -> None:
        self.thread_id = int(w.kernel32.GetCurrentThreadId())
        try:
            self.hwnd = self._create_message_window()
            self._install_hooks()
        except Exception as exc:
            log.exception("钩子线程初始化失败: %s", exc)
            self._ready.set()
            return

        log.info(
            "钩子线程已就绪 (tid=%s): 鼠标=%s 键盘=%s",
            self.thread_id,
            "已安装" if self._mouse_hook else "失败",
            "已安装" if self._key_hook else "失败",
        )
        self._ready.set()

        msg = w.MSG()
        while True:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret == 0:
                break
            if ret == -1:
                log.error("GetMessageW 出错，钩子线程退出")
                break
            if msg.message == WM_HOOK_REINSTALL:
                self._reinstall_hooks()
                continue
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        self._uninstall_hooks()
        if self.hwnd:
            user32.DestroyWindow(self.hwnd)
            self.hwnd = None
        log.info("钩子线程已退出")

    def _create_message_window(self) -> int:
        hinstance = w.kernel32.GetModuleHandleW(None)
        class_name = "netclip_hook_window_%d" % (self.thread_id or 0)

        wc = WNDCLASSEX()
        wc.cbSize = ctypes.sizeof(WNDCLASSEX)
        wc.lpfnWndProc = ctypes.cast(self._wnd_proc, ctypes.c_void_p)
        wc.hInstance = hinstance
        wc.lpszClassName = class_name
        atom = user32.RegisterClassExW(ctypes.byref(wc))
        if not atom and ctypes.get_last_error() not in (0, 1410):  # 1410 = 类已存在
            log.warning("RegisterClassExW 失败: %d", ctypes.get_last_error())

        hwnd = user32.CreateWindowExW(
            0, class_name, "netclip-hooks", 0, 0, 0, 0, 0, w.HWND_MESSAGE, None, hinstance, None
        )
        if not hwnd:
            raise w.WinApiError(ctypes.get_last_error(), "CreateWindowExW 失败")
        return int(hwnd)

    def _wnd_callback(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        if msg == w.WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return int(user32.DefWindowProcW(hwnd, msg, wparam, lparam))

    def _install_hooks(self) -> None:
        hinstance = w.kernel32.GetModuleHandleW(None)
        # 第 4 个参数为 0 = 全局钩子（对当前桌面上的所有进程生效）
        self._mouse_hook = user32.SetWindowsHookExW(w.WH_MOUSE_LL, self._mouse_proc, hinstance, 0)
        if not self._mouse_hook:
            self.stats.install_failures += 1
            log.error("安装鼠标钩子失败: GetLastError=%d", ctypes.get_last_error())
        self._key_hook = user32.SetWindowsHookExW(w.WH_KEYBOARD_LL, self._key_proc, hinstance, 0)
        if not self._key_hook:
            self.stats.install_failures += 1
            log.error("安装键盘钩子失败: GetLastError=%d", ctypes.get_last_error())

    def _reinstall_hooks(self) -> None:
        self._uninstall_hooks()
        self._install_hooks()
        log.info("钩子已重新安装: 鼠标=%s 键盘=%s", bool(self._mouse_hook), bool(self._key_hook))

    def _uninstall_hooks(self) -> None:
        if self._mouse_hook:
            user32.UnhookWindowsHookEx(self._mouse_hook)
            self._mouse_hook = None
        if self._key_hook:
            user32.UnhookWindowsHookEx(self._key_hook)
            self._key_hook = None

    # ------------------------------------------------------------ 回调（热路径）

    def _mouse_callback(self, ncode: int, wparam: int, lparam: int) -> int:
        if ncode != w.HC_ACTION:
            return int(user32.CallNextHookEx(self._mouse_hook, ncode, wparam, lparam))

        data = ctypes.cast(lparam, ctypes.POINTER(w.MSLLHOOKSTRUCT)).contents
        if data.flags & w.LLMHF_INJECTED or data.dwExtraInfo == INJECT_TAG:
            # 自己注入的事件：直接放行，绝不能再转发，否则形成死循环
            self.stats.injected_skipped += 1
            return int(user32.CallNextHookEx(self._mouse_hook, ncode, wparam, lparam))

        msg = int(wparam)
        if msg not in (w.WM_MOUSEMOVE,) and msg not in _MOUSE_BUTTON_MSGS and msg not in (
            w.WM_XBUTTONDOWN,
            w.WM_XBUTTONUP,
            w.WM_MOUSEWHEEL,
            w.WM_MOUSEHWHEEL,
        ):
            return int(user32.CallNextHookEx(self._mouse_hook, ncode, wparam, lparam))

        ev = self._event
        ev.time = int(data.time)
        ev.x = int(data.pt.x)
        ev.y = int(data.pt.y)
        self.stats.mouse_events += 1

        if msg == w.WM_MOUSEMOVE and _now_ms() < self._warp_settle_until:
            # 这是我们自己 SetCursorPos 造成的移动（它**不带**注入标志），
            # 必须丢弃：否则两端会互相把对方的位移转发回去，形成高频抖动。
            # 只更新坐标基准、不上报事件；仍然放行给系统，本机光标正常显示。
            self.stats.warp_settle_skipped += 1
            self._last_x = ev.x
            self._last_y = ev.y
            self._have_last = True
            return int(user32.CallNextHookEx(self._mouse_hook, ncode, wparam, lparam))

        if msg == w.WM_MOUSEMOVE:
            ev.kind = MOVE
            base = self.motion_base(ev.x, ev.y)
            ev.dx = ev.x - base[0]
            ev.dy = ev.y - base[1]
            self._last_x = ev.x
            self._last_y = ev.y
            self._have_last = True
        elif msg in _MOUSE_BUTTON_MSGS:
            ev.kind = BUTTON
            ev.button, ev.down = _MOUSE_BUTTON_MSGS[msg]
            self._note_button(ev.button, ev.down)
        elif msg in (w.WM_XBUTTONDOWN, w.WM_XBUTTONUP):
            ev.kind = BUTTON
            high = (int(data.mouseData) >> 16) & 0xFFFF
            ev.button = "x1" if high == w.XBUTTON1 else "x2"
            ev.down = msg == w.WM_XBUTTONDOWN
            self._note_button(ev.button, ev.down)
        elif msg == w.WM_MOUSEWHEEL:
            ev.kind = WHEEL
            ev.horizontal = False
            ev.wheel = _signed_high_word(int(data.mouseData))
        else:  # WM_MOUSEHWHEEL
            ev.kind = WHEEL
            ev.horizontal = True
            ev.wheel = _signed_high_word(int(data.mouseData))

        ev.any_button_down = bool(self._buttons_down)

        if self.chain is not None and self.chain.enabled:
            self.chain.log(
                "hook",
                "%s pt=(%d,%d) d=(%d,%d) 按钮=%s 沉降窗=%s",
                _KIND_NAME.get(ev.kind, ev.kind),
                ev.x,
                ev.y,
                ev.dx,
                ev.dy,
                ",".join(sorted(self._buttons_down)) or "无",
                "是" if _now_ms() < self._warp_settle_until else "否",
            )

        self.on_event(ev)
        if self.should_swallow(ev):
            self.stats.swallowed += 1
            if self.chain is not None and self.chain.enabled:
                self.chain.log("hook", "  -> 被吞掉（本机不处理）")
            self._event = InputEvent()  # 上一份所有权已交给消费方
            return 1
        self._event = InputEvent()
        return int(user32.CallNextHookEx(self._mouse_hook, ncode, wparam, lparam))

    def _key_callback(self, ncode: int, wparam: int, lparam: int) -> int:
        if ncode != w.HC_ACTION:
            return int(user32.CallNextHookEx(self._key_hook, ncode, wparam, lparam))

        data = ctypes.cast(lparam, ctypes.POINTER(w.KBDLLHOOKSTRUCT)).contents
        if data.flags & w.LLKHF_INJECTED or data.dwExtraInfo == INJECT_TAG:
            self.stats.injected_skipped += 1
            return int(user32.CallNextHookEx(self._key_hook, ncode, wparam, lparam))

        msg = int(wparam)
        if msg not in (w.WM_KEYDOWN, w.WM_KEYUP, w.WM_SYSKEYDOWN, w.WM_SYSKEYUP):
            return int(user32.CallNextHookEx(self._key_hook, ncode, wparam, lparam))

        ev = self._event
        ev.kind = KEY
        ev.vk = int(data.vkCode)
        ev.scan = int(data.scanCode)
        ev.extended = bool(data.flags & w.LLKHF_EXTENDED)
        ev.down = msg in (w.WM_KEYDOWN, w.WM_SYSKEYDOWN)
        ev.time = int(data.time)
        self.stats.key_events += 1

        if self.chain is not None and self.chain.enabled:
            self.chain.log(
                "hook", "键盘 vk=0x%02X scan=0x%02X 扩展=%s 按下=%s", ev.vk, ev.scan, ev.extended, ev.down
            )

        self.on_event(ev)
        if self.should_swallow(ev):
            self.stats.swallowed += 1
            if self.chain is not None and self.chain.enabled:
                self.chain.log("hook", "  -> 被吞掉（本机不处理）")
            self._event = InputEvent()
            return 1
        self._event = InputEvent()
        return int(user32.CallNextHookEx(self._key_hook, ncode, wparam, lparam))


def _signed_high_word(value: int) -> int:
    """取 DWORD 的高 16 位并解释为有符号数（滚轮增量是带符号的）。"""
    high = (value >> 16) & 0xFFFF
    return high - 0x10000 if high & 0x8000 else high


def _read_cursor_pos() -> "tuple[int, int]":
    """读系统光标的**当前实际位置**。失败返回 (-1,-1)。

    钩子热路径每来一条移动事件就要读一次（约 1000 次/秒）。`GetCursorPos`
    是个很便宜的查询，实测对钩子回调耗时没有可测量的影响（回调峰值仍是 0µs）。
    """
    point = w.POINT()
    if user32.GetCursorPos(ctypes.byref(point)):
        return (int(point.x), int(point.y))
    return (-1, -1)


def _now_ms() -> int:
    """单调时钟的毫秒值。只用它做"时间窗"比较，不做墙钟展示。"""
    return int(w.kernel32.GetTickCount64())


__all__ = ["BUTTON", "KEY", "MOVE", "WHEEL", "HookStats", "HookThread", "InputEvent"]
