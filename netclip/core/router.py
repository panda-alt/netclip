"""输入路由状态机：LOCAL <-> REMOTE。

核心职责
--------
把"本机输入"和"转发给对端的输入"区分开，并在穿过共享边时正确切换。

线程模型
--------
* **钩子线程**：只调用 `note_event()`（写入环形缓冲）和 `should_swallow()`（读一个
  原子布尔量）。两者都是纳秒级的，绝不做判断逻辑。
* **router 线程**（本类的消费循环）：从缓冲里取事件、跑状态机、调用 `send_*` 回调。
  判断逻辑都在这里，慢一点也没关系。

为什么把判断逻辑和"是否吞掉"分开
--------------------------------
低级钩子必须在回调里当场决定吞或不吞，但"这个事件该不该转发"的判断需要读状态机，
而在回调里跑状态机会违反"回调要快"的纪律。解法：**吞不吞只看一个布尔量** ——
只要当前处于 REMOTE 状态，本机的一切鼠标/键盘事件都吞掉（转发由 router 线程做）。
状态切换本身只写这个布尔量，是原子的。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Deque, Dict, List, Optional, Sequence, Set, Tuple

from .. import layout as L
from ..win import hooks as H
from ..win import inject
from ..win import winapi as w

log = logging.getLogger("netclip.core.router")

#: 判定"光标是真的压回共享边了"所需的余量（像素）。
#:
#: 穿越进对端之后光标会被钉在共享边附近（`warp_inset_px`）。如果这时用户只是
#: 抖了一下、或者系统补了个反向小位移，就立刻判成"想回来"，手感会变成
#: "刚过去就被弹回来"。所以要求光标先离共享边超过这个距离（置上
#: `_left_entry_edge`），才允许 `_pegged_at_shared_edge` 触发返回。
_ENTRY_EDGE_SLACK = 32


def _opposite(position: str) -> str:
    """对端方位的相反边。用于把"进入对端"换算成"对端哪条边被进入"。"""
    return {L.RIGHT: L.LEFT, L.LEFT: L.RIGHT, L.UP: L.DOWN, L.DOWN: L.UP}[position]


class Mode(Enum):
    LOCAL = "local"  # 输入留在本机
    REMOTE = "remote"  # 输入发给对端
    PAUSED = "paused"  # 用户手动暂停共享（等价 LOCAL，但记住用户意图）


# --------------------------------------------------------------------- 热键


_VK_ALIASES: Dict[str, int] = {
    "ctrl": w.VK_CONTROL,
    "control": w.VK_CONTROL,
    "alt": w.VK_MENU,
    "shift": w.VK_SHIFT,
    "win": w.VK_LWIN,
    "lwin": w.VK_LWIN,
    "rwin": w.VK_RWIN,
    "home": w.VK_HOME,
    "end": w.VK_END,
    "pause": w.VK_PAUSE,
    "insert": w.VK_INSERT,
    "delete": w.VK_DELETE,
    "esc": w.VK_ESCAPE,
    "escape": w.VK_ESCAPE,
    "space": w.VK_SPACE,
    "tab": w.VK_TAB,
    "del": w.VK_DELETE,
}


def _parse_hotkey(spec: str) -> Optional[Tuple[frozenset, Optional[int]]]:
    """解析 "ctrl+alt+home" 这样的热键描述。

    返回 (修饰键集合, 主键 VK)；主键为 None 表示"只按修饰键"。
    """
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    if not parts:
        return None
    mods: Set[int] = set()
    main: Optional[int] = None
    for idx, part in enumerate(parts):
        vk = _VK_ALIASES.get(part)
        if vk is None:
            if part.startswith("f") and part[1:].isdigit():
                num = int(part[1:])
                if 1 <= num <= 24:
                    vk = w.VK_F1 + num - 1
            elif len(part) == 1 and part.isalnum():
                vk = ord(part.upper())
            elif part.isdigit():
                vk = ord(part)
            if vk is None:
                log.warning("无法解析热键片段: %r（来自 %r）", part, spec)
                return None
        if vk in (w.VK_CONTROL, w.VK_MENU, w.VK_SHIFT, w.VK_LWIN, w.VK_RWIN):
            mods.add(vk)
        elif idx == len(parts) - 1:
            main = vk
        else:
            mods.add(vk)
    return frozenset(mods), main


def _mods_down() -> Set[int]:
    """当前按下的修饰键集合（把左右键归一到通用键）。"""
    down: Set[int] = set()
    if w.is_key_down(w.VK_LCONTROL) or w.is_key_down(w.VK_RCONTROL):
        down.add(w.VK_CONTROL)
    if w.is_key_down(w.VK_LMENU) or w.is_key_down(w.VK_RMENU):
        down.add(w.VK_MENU)
    if w.is_key_down(w.VK_LSHIFT) or w.is_key_down(w.VK_RSHIFT):
        down.add(w.VK_SHIFT)
    if w.is_key_down(w.VK_LWIN) or w.is_key_down(w.VK_RWIN):
        down.add(w.VK_LWIN)
    return down


@dataclass
class HotkeyBinding:
    spec: str
    mods: frozenset
    main: Optional[int]
    action: str

    def matches(self, event: H.InputEvent, current_mods: Set[int]) -> bool:
        """判断一个按键事件是否命中这个热键。

        只在**按下**且修饰键状态匹配时命中。修饰键本身（main is None）不触发动作，
        因为"只按 Ctrl"太容易误触。
        """
        if not event.down:
            return False
        if self.main is not None and event.vk != self.main:
            return False
        wanted = set(self.mods)
        if self.main is None:
            return False
        if not wanted.issubset(current_mods):
            return False
        # 不允许有热键之外的修饰键同时按着，否则 Ctrl+Shift+X 会误触发 Ctrl+X
        return not (current_mods - wanted - _normalize_mod(event.vk))


def _normalize_mod(vk: int) -> Set[int]:
    if vk in (w.VK_LCONTROL, w.VK_RCONTROL, w.VK_CONTROL):
        return {w.VK_CONTROL}
    if vk in (w.VK_LMENU, w.VK_RMENU, w.VK_MENU):
        return {w.VK_MENU}
    if vk in (w.VK_LSHIFT, w.VK_RSHIFT, w.VK_SHIFT):
        return {w.VK_SHIFT}
    if vk in (w.VK_LWIN, w.VK_RWIN):
        return {w.VK_LWIN}
    return set()


# --------------------------------------------------------------------- 回调


@dataclass
class RouterCallbacks:
    """router 线程向外部的出口。全部由使用方注入，便于单测。"""

    #: 发送一个输入帧到对端（channel="input"）
    send: Callable[[int, Dict, Optional[object]], None] = lambda *a, **k: None
    #: 发送鼠标位移（单独出口，便于合并/诊断）。
    #: 参数：(dx, dy, peer_x, peer_y)，后两个是**对端坐标系下的绝对落点**。
    #: 为什么必须同时给绝对落点见 `_forward_remote_move`。
    send_move: Callable[[int, int, int, int], None] = lambda dx, dy, x, y: None
    #: 光标进入对端
    on_enter_peer: Callable[[float], None] = lambda ratio: None
    #: 光标回到本机
    on_leave_peer: Callable[[], None] = lambda: None
    #: 请求把本机光标钉到指定位置（穿越后防抖，或强制回拉）
    warp_cursor: Callable[[int, int], None] = lambda x, y: None
    #: 释放所有残留按键
    release_all: Callable[[bool], None] = lambda force: None
    #: 切换共享开关（热键触发）
    request_toggle: Callable[[], None] = lambda: None
    #: 请求强制回拉（热键触发）
    request_recapture: Callable[[], None] = lambda: None
    #: 网络是否可用
    is_peer_connected: Callable[[], bool] = lambda: False


# --------------------------------------------------------------------- 状态机


#: 事件类型 -> 中文名，仅用于链路日志
_KIND_LABEL = {H.MOVE: "移动", H.BUTTON: "按钮", H.WHEEL: "滚轮", H.KEY: "键盘"}


class InputRouter:
    """输入路由状态机。

    `note_event()` 由钩子线程调用（纳秒级），`run()` 在 router 线程里跑消费循环。
    """

    def __init__(
        self,
        layout: L.ScreenLayout,
        callbacks: Optional[RouterCallbacks] = None,
        *,
        share_mouse: bool = True,
        share_keyboard: bool = True,
        forward_media_keys: bool = False,
        local_hotkeys: Sequence[str] = (),
        hotkey_recapture: str = "",
        hotkey_toggle: str = "",
        switch_cooldown_ms: int = 250,
        arm_delay_ms: int = 300,
        lock_mouse_on_drag: bool = True,
        queue_max: int = 4096,
        key_debug_lines: int = 0,
        tracer: Optional[Any] = None,
        diag: Optional[Any] = None,
        chain: Optional[Any] = None,
    ) -> None:
        self.layout = layout
        self.cb = callbacks or RouterCallbacks()
        #: 鼠标追踪器（`netclip.debug.mouse.MouseTracer`）；None 表示不追踪
        self.tracer = tracer
        #: 每秒输入诊断（`netclip.debug.inputdiag.InputDiag`）；None 表示不诊断
        self.diag = diag
        #: 整条链路的逐条日志（`netclip.debug.chain.ChainLog`）；None 表示不记
        self.chain = chain
        self.share_mouse = share_mouse
        self.share_keyboard = share_keyboard
        self.forward_media_keys = forward_media_keys
        self.lock_mouse_on_drag = lock_mouse_on_drag
        self.switch_cooldown_sec = max(0.0, switch_cooldown_ms / 1000.0)
        self.arm_delay_sec = max(0.0, arm_delay_ms / 1000.0)
        #: 还能打几条"按键被拦下"的诊断日志（0 = 不打）
        self.key_debug_left = max(0, int(key_debug_lines))

        self._mode = Mode.LOCAL
        self._enabled = True
        self._remote_ready = False  # 对端是否报告"可以接收输入"
        self._swallow = False  # 钩子线程直接读的原子布尔量
        self._lock = threading.Lock()
        self._queue: Deque[H.InputEvent] = deque()
        self._queue_max = max(64, queue_max)
        self._wake = threading.Event()
        self._running = False

        # 虚拟光标：用位移增量累加，避免每次移动都调 GetCursorPos
        self._virt_x = 0
        self._virt_y = 0
        self._virt_have = False
        self._last_switch_ts = 0.0
        self._armed_at = 0.0
        #: 统一坐标系下的光标位置。LOCAL 时代表本机光标，REMOTE 时代表对端光标。
        #: 这是 REMOTE 期间**唯一**可信的位置来源，见 `_forward_remote_move`。
        self._gx = 0
        self._gy = 0
        self._cursor_have = False
        #: REMOTE 期间"是否已经离开过进入对端时的那条边"。
        #: 入口点本身就贴在共享边上，没有这个标记的话刚进去的一个抖动就会
        #: 立刻把控制权又拿回来（见 `_pegged_at_shared_edge`）。
        self._left_entry_edge = False
        #: 我们自己模拟的对端光标位置（对端坐标系），便于诊断输出
        self._peer_x = 0
        self._peer_y = 0
        self._peer_have = False
        self._prev_mods: Set[int] = set()
        #: 是否正处在"本机热键段"（本机热键的修饰键还没全松开）
        self._holding_local_hotkey = False
        self._hotkey_mods: frozenset = frozenset()
        #: 从**事件流**推出来的"当前按下的修饰键"（归一化后的集合）。
        #: 比 GetAsyncKeyState 更可靠：它反映的是"我们确实看到的按键序列"。
        self._mods_down_set: Set[int] = set()
        #: 状态切换的环形记录（见 `_trace`）
        self._trace_buf: List[str] = []

        self._local_hotkeys = [k for k in (local_hotkeys or ()) if k]
        self._bindings: List[HotkeyBinding] = []
        self._add_binding(hotkey_recapture, "recapture")
        self._add_binding(hotkey_toggle, "toggle")

        self.stats = {
            "switch_to_peer": 0,
            "switch_to_local": 0,
            "forwarded_move": 0,
            "forwarded_key": 0,
            "forwarded_button": 0,
            "hotkey_recapture": 0,
            "hotkey_toggle": 0,
            "dropped_events": 0,
            # 下面几个是"两端互相抢控制权"以及"按键为什么没过去"的诊断计数
            "key_blocked_local": 0,
            "key_blocked_share_off": 0,
            "key_blocked_media": 0,
            "peer_enter_accepted": 0,
            "peer_enter_ignored": 0,
            #: 转发时被钳位（对端光标已贴边）
            "move_clamped": 0,
        #: 沿原路推回共享边（唯一的"回到本机"路径）
            "shared_edge_returned": 0,
        }

    # ------------------------------------------------------------ 热键绑定

    def _add_binding(self, spec: str, action: str) -> None:
        """解析并登记一个热键。解析失败只警告，不影响程序启动。"""
        if not spec:
            return
        parsed = _parse_hotkey(spec)
        if parsed is None:
            log.warning("热键 %r（动作 %s）无法解析，已忽略", spec, action)
            return
        mods, main = parsed
        if main is None and not mods:
            log.warning("热键 %r 为空，已忽略", spec)
            return
        self._bindings.append(HotkeyBinding(spec=spec, mods=mods, main=main, action=action))
        log.info("热键已登记: %s -> %s", spec, action)

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        self._running = True
        self._refresh_virtual_cursor()

    def stop(self) -> None:
        self._running = False
        self._wake.set()

    @property
    def mode(self) -> Mode:
        return self._mode

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        """启用/暂停共享。暂停时立刻把控制权交回本机。"""
        with self._lock:
            self._enabled = enabled
        if not enabled:
            self.recapture("暂停共享")
        log.info("共享已%s", "启用" if enabled else "暂停")

    def toggle_enabled(self) -> bool:
        self.stats["hotkey_toggle"] += 1
        self.set_enabled(not self._enabled)
        return self._enabled

    def set_peer_ready(self, ready: bool) -> None:
        self._remote_ready = ready
        if not ready and self._mode is Mode.REMOTE:
            self.recapture("对端不可用")

    def update_layout(self, layout: L.ScreenLayout) -> None:
        """对端上报分辨率后更新几何。"""
        self.layout = layout
        self._refresh_virtual_cursor()

    def _refresh_virtual_cursor(self) -> None:
        try:
            x, y = w.get_cursor_pos()
        except w.WinApiError:
            return
        self._virt_x, self._virt_y = x, y
        self._virt_have = True

    # ------------------------------------------------------------ 钩子线程接口

    def note_event(self, event: H.InputEvent) -> None:
        """钩子线程调用。只做入队，不做任何判断。"""
        if len(self._queue) >= self._queue_max:
            self.stats["dropped_events"] += 1
            return
        self._queue.append(event.copy())
        self._wake.set()

    def should_swallow(self, event: H.InputEvent) -> bool:
        """钩子线程调用。只看一个布尔量，纳秒级返回。"""
        if not self._swallow:
            return False
        # 暂停键始终放行，否则用户没法用热键恢复
        if event.kind == H.KEY and self._matches_any_binding(event):
            return False
        return True

    def _matches_any_binding(self, event: H.InputEvent) -> bool:
        if event.kind != H.KEY:
            return False
        for binding in self._bindings:
            if binding.main is not None and event.vk == binding.main:
                return True
        return False

    # ------------------------------------------------------------ 消费循环

    def run(self) -> None:
        """router 线程主循环。"""
        self.start()
        log.info("输入路由已启动: %s", self.layout.describe())
        while self._running:
            if not self._queue:
                self._wake.wait(0.25)
                self._wake.clear()
                continue
            batch = self._drain()
            for event in batch:
                if not self._running:
                    break
                try:
                    self._handle(event)
                except Exception:  # pragma: no cover - 单条事件出错不能拖垮路由
                    log.exception("处理输入事件失败: kind=%s", event.kind)

    def _drain(self, limit: int = 512) -> List[H.InputEvent]:
        out: List[H.InputEvent] = []
        for _ in range(limit):
            if not self._queue:
                break
            out.append(self._queue.popleft())
        return out

    def _handle(self, event: H.InputEvent) -> None:
        #: 链路第 2 环：路由收到事件。把"钩子给的事件"和"当时的模式"记在一起，
        #: 这样"事件其实到了、只是模式判断把它挡住了"这种情况一眼可见。
        if self.chain is not None and self.chain.enabled:
            if event.kind == H.MOVE:
                self.chain.log_move(
                    "route",
                    "移动 模式=%s 对端可用=%s 共享=%s pt=(%d,%d) d=(%d,%d)",
                    self._mode.value,
                    "是" if self._remote_ready else "否",
                    "开" if self._enabled else "关",
                    event.x,
                    event.y,
                    event.dx,
                    event.dy,
                )
            else:
                self.chain.log(
                    "route",
                    "%s 模式=%s 对端可用=%s 共享=%s pt=(%d,%d) d=(%d,%d)",
                    _KIND_LABEL.get(event.kind, str(event.kind)),
                    self._mode.value,
                    "是" if self._remote_ready else "否",
                    "开" if self._enabled else "关",
                    event.x,
                    event.y,
                    event.dx,
                    event.dy,
                )
        if self.diag is not None:
            self.diag.on_event(
                {H.MOVE: "MOVE", H.KEY: "KEY", H.BUTTON: "BUTTON", H.WHEEL: "WHEEL"}.get(event.kind, "?"),
                self._mode.value,
                pt=(event.x, event.y),
            )
        if event.kind == H.MOVE:
            self._handle_move(event)
        elif event.kind == H.KEY:
            self._handle_key(event)
        elif event.kind == H.BUTTON:
            self._handle_button(event)
        elif event.kind == H.WHEEL:
            self._handle_wheel(event)

    # ------------------------------------------------------------ 鼠标移动

    def _why(self, reason: str) -> None:
        """记一条"为什么这条事件没被处理"。

        这类"静默 return"是排查输入问题时最耗时间的地方：事件明明到了路由，
        程序却什么都不做、也不说为什么。每个提前返回都配上原因，日志就能直接
        回答"到底是模式不对、还是冷却期、还是拖拽锁"。
        """
        if self.chain is not None and self.chain.enabled:
            self.chain.log_move("gate", "未处理：%s", reason)

    def _handle_move(self, event: H.InputEvent) -> None:
        if not self.share_mouse:
            self._why("共享鼠标已关闭")
            return
        if not self._virt_have:
            self._virt_x, self._virt_y = event.x, event.y
            self._virt_have = True

        prev = (self._virt_x, self._virt_y)
        # 用增量推进虚拟光标，而不是直接读事件坐标：这样即使本机光标已经被
        # 系统钉在屏幕边上（SetCursorPos 之后），虚拟光标仍能继续往屏幕外走。
        self._virt_x += event.dx
        self._virt_y += event.dy
        cur = (self._virt_x, self._virt_y)

        if self._mode is Mode.REMOTE:
            self._forward_remote_move(event)
            return

        if not self._enabled or not self._remote_ready:
            self._why("暂停中或对端不可用（enabled=%s 对端可用=%s）" % (self._enabled, self._remote_ready))
            return
        if time.monotonic() - self._last_switch_ts < self.switch_cooldown_sec:
            self._why("切换冷却期内（本机刚切换过）")
            return
        if time.monotonic() < self._armed_at:
            self._why("防抖解锁等待中（刚穿越完）")
            return

        # 拖拽锁定：按住鼠标键时不允许切屏。
        # 否则在资源管理器里把窗口往屏幕边上一拖，窗口就被"甩"到对端了；
        # 反过来，文件跨屏拖拽 netclip 也不支持，锁定可以避免半途中断的错觉。
        if self.lock_mouse_on_drag and event.any_button_down:
            self._why("拖拽锁定（有鼠标键按着）")
            return

        if not self._virt_have:
            self._why("虚拟光标未初始化")
            return

        crossing = self.layout.detect_crossing(prev, cur)
        if crossing is not None:
            self.enter_peer(crossing.ratio)
            return
        #: 没穿成（冷却期 / 对端不可用 / 拖拽锁 / 根本没到边）：把虚拟光标拉回屏幕内。
        #: 见 `_pull_virtual_cursor_inside` —— 这是"进去一次就再也进不去"的根因。
        self._pull_virtual_cursor_inside()

    # ------------------------------------------------------------ 光标模型

    def _pull_virtual_cursor_inside(self) -> None:
        """把"虚拟本机光标"拉回本机屏幕范围内。

        **虚拟光标唯一的作用**是：真实光标被屏幕边界钉住之后，让它还能继续累积
        一点位移，好让 `detect_crossing` 能观察到 `prev < 边界 <= cur`。
        它的合法取值范围就是"本机屏幕 + 一小段越界"，除此之外没有意义。

        **为什么必须强制收回来**（真机现象："主机鼠标进入从机、再回到主机，
        就再也无法重新进入从机"）：

        REMOTE 期间虚拟光标累积的是**真实位移**，而用户是一路把光标推到对端屏幕的
        **外边界**才交回控制权的。这段行程原封不动累积进去：

            本机屏高 1440 + 对端屏高 1200 = 2640      <- 回来时 _virt_y 停在这里
            本机底边（共享边）            = 1440

        `detect_crossing` 的判据是 `prev < 边界 <= cur`，而 `prev` 现在是 2640，
        **永远不成立**。用户必须先往上推够 1200 像素把它拉回边界内，才能再穿过去 ——
        但他直觉上会继续往下推，于是它只会越推越远。表现就是"再也进不去"。

        这个 bug 被"位移基准"错误掩盖了很久：以前 `_virt_y` 累积的是约等于 0 的
        垃圾值，所以它一直莫名其妙地贴着真实光标。
        """
        rect = self.layout.local
        self._virt_x = min(max(self._virt_x, rect.x), rect.right - 1)
        self._virt_y = min(max(self._virt_y, rect.y), rect.bottom - 1)

    def _reset_cursor_model(self, ratio: float = 0.5) -> None:
        """重置"统一坐标系里的光标位置"。

        * **REMOTE** 期间：它表示**对端光标**的位置，所以我们按几何把它设到
          对端入口点，之后随转发的位移移动。
        * **LOCAL** 期间：它表示本机光标的位置，由本机虚拟光标换算过来。

        统一坐标系是关键：本机光标会被自己屏幕的边界钳住，而对端光标在某条轴上
        是自由的，用本机坐标当基准会越漂越远（实测漂到过屏幕外）。
        """
        if self._mode is Mode.REMOTE:
            try:
                x, y = self.layout.peer_entry_point(ratio, _opposite(self.layout.peer_position))
            except Exception:  # pragma: no cover - 几何异常不该影响输入
                x, y = self.layout.peer_entry_point(0.5, _opposite(self.layout.peer_position))
            #: `peer_entry_point` 给的是**对端坐标系**里的点，按对端矩形原点**平移**
            #: 就得到统一坐标。这里绝不能按比例缩放 —— 坐标系之间的换算必须是
            #: 纯平移，否则每一帧算出来的位置都带缩放误差，越走越偏。
            #: 平移量包含**两条轴**（非穿越轴上有 alignment 偏移），所以必须走
            #: `peer_local_to_global`，不能只处理穿越轴。
            self._gx, self._gy = self.layout.peer_local_to_global(x, y)
        else:
            if self._virt_have:
                lx, ly = self._virt_x, self._virt_y
            else:
                try:
                    lx, ly = w.get_cursor_pos()
                except w.WinApiError:  # pragma: no cover
                    lx, ly = self.layout.local.cx, self.layout.local.cy
            #: 统一坐标系就是**以本机屏幕左上角为原点**定义的（见 `local_rect_global`），
            #: 所以"本机坐标 -> 统一坐标"是恒等映射。
            self._gx, self._gy = int(lx), int(ly)

        self._cursor_have = True
        self._peer_x, self._peer_y = self.layout.global_to_peer_local(self._gx, self._gy)
        #: 刚进入（或被重新定位到）入口边，先认为"还没离开过"
        self._left_entry_edge = False

    def _forward_remote_move(self, event: H.InputEvent) -> None:
        """REMOTE 状态下转发一次鼠标移动。

        **以"统一坐标系里的光标位置"为唯一事实来源。** 踩过的坑：

        早期版本在 REMOTE 状态下只累加"本机虚拟光标"，并直接转发钩子的原始位移。
        问题是本机光标会被**自己屏幕**的边界钳住，而用户此刻关心的是对端屏幕上的
        位置。两者一旦脱节，主机内存里"对端光标在哪"就会一路漂到屏幕外
        （实测漂到 (-873, -1092)，而对端只有 1920x1080），之后所有判断都基于
        错误基准 —— 表现就是光标被拽住、乱跳、推到某个位置就不动。

        钳位到**对端屏幕**之后，模拟位置永远和从机的真实光标一致；
        到达对端外边界时多出来的位移不再发送，并且触发"把控制权交给对端"
        的判断（对端自己会从它那条边穿回来，那是它本来就有的行为）。
        """
        dx = int(event.dx)
        dy = int(event.dy)
        if dx == 0 and dy == 0:
            return

        if not self._cursor_have:
            self._reset_cursor_model()

        prev_g = (self._gx, self._gy)
        want_g = (prev_g[0] + dx, prev_g[1] + dy)
        peer_rect = self.layout.peer_rect_global()
        #: 钳到对端屏幕内 —— 这是整套坐标一致性的关键一步
        self._gx, self._gy = peer_rect.clamp(*want_g)

        clamped_dx = self._gx - prev_g[0]
        clamped_dy = self._gy - prev_g[1]

        #: 对端坐标系下的绝对落点。**必须随每一帧一起发。**
        #:
        #: 实测（真机，Windows 11）：注入的**相对**位移会被系统的
        #: "提高指针精确度"(HKCU\Control Panel\Mouse\MouseSpeed=1) 加速曲线再放大一遍：
        #:
        #:     请求 1px  -> 实际  0px   （直接丢失）
        #:     请求 5px  -> 实际  6px
        #:     请求 20px -> 实际 42px   (2.1x)
        #:     请求 80px -> 实际 290px  (3.6x)
        #:
        #: 于是"主机算出来的位移"和"从机光标实际走的距离"永远对不上，两边的位置
        #: 模型从第一帧起就发散，撞到对端屏幕边缘后被钳住 —— 用户看到的就是
        #: "光标被弹簧拉住、挪走一点又被拽回去"。
        #:
        #: 绝对定位不走这条曲线（同机实测误差 0），所以把它作为**权威落点**一起发；
        #: 相对位移保留，供老版本或需要原生相对运动的场景回退。
        self._peer_x, self._peer_y = self.layout.global_to_peer_local(self._gx, self._gy)

        if clamped_dx or clamped_dy:
            self.cb.send_move(clamped_dx, clamped_dy, self._peer_x, self._peer_y)
            self.stats["forwarded_move"] += 1
        else:
            self.stats["move_clamped"] += 1

        #: 离开入口边足够远之后，才允许"原路推回去"触发交回控制权
        if not self._left_entry_edge and self._distance_from_shared_edge(peer_rect) > _ENTRY_EDGE_SLACK:
            self._left_entry_edge = True

        #: **沿原路推回共享边 = 用户想把控制权要回去。**
        #:
        #: 模型被钳在对端矩形内，所以推到近边就停住了 —— 既越不出去，也没有任何
        #: 判定去识别"用户想原路返回"。真机现象（用户原话）："从机的物理鼠标从顶端
        #: 进入主机没问题，但要回到从机必须绕到主机屏幕的另一头碰一下。"
        #:
        #: 必须等 `_left_entry_edge` 置上（真的深入过对端）才判定：入口点本来就贴在
        #: 共享边上，否则刚过去时的一个抖动就会把控制权弹回来。
        if self._left_entry_edge and self._pegged_at_shared_edge(peer_rect, dx, dy):
            self.stats["shared_edge_returned"] += 1
            self.recapture("已沿原路推回共享边")
            return

        if self.diag is not None:
            self.diag.on_forward(clamped_dx, clamped_dy, clamped=(not (clamped_dx or clamped_dy)))

        #: 链路第 3 环：转发计算。这一环把"钩子给的原始位移"、"钳位后的位移"、
        #: "模型位置"、"发给对端的绝对落点"四个量放在同一行 —— 它们在真机上
        #: 曾经互相矛盾（原始位移一直有、模型却几乎不动），就是靠这一行看出来的。
        if self.chain is not None and self.chain.enabled:
            self.chain.log_move(
                "forward",
                "模型 (%d,%d)->(%d,%d) 原始位移=(%d,%d) 钳位后=(%d,%d) 钳位=%s 绝对落点=(%d,%d)",
                prev_g[0],
                prev_g[1],
                self._gx,
                self._gy,
                dx,
                dy,
                clamped_dx,
                clamped_dy,
                "是" if (clamped_dx != dx or clamped_dy != dy) else "否",
                self._peer_x,
                self._peer_y,
            )

        if self.tracer is not None:
            self.tracer.host_move(
                dx, dy, self._gx, self._gy,
                forwarded=bool(clamped_dx or clamped_dy), mode=self._mode.value,
                raw=(int(event.x), int(event.y)),
                peer=(self._peer_x, self._peer_y),
            )

    def _pegged_at_shared_edge(self, peer_rect: L.Rect, dx: int, dy: int) -> bool:
        """光标是否**正压在对端朝向本机的那条边上、而且还在往外推**。

        也就是"用户想沿原路回去"。三条缺一不可：

          * 位置**正好贴在那条边上**（模型被钳住了）；
          * 位移方向是**朝外的**；
          * 方向用的是**钩子给的原始位移** `dx/dy`，不是钳位后的 ——
            钳位后位移在边上恒为 0，靠它根本分不出"停在这儿"和"想推出去"。

        "共享边"就是两块屏幕相接的那条：对端在右边时，那是**对端的左边**。
        对端自己的 `warp_inset_px` 会把光标从那条边往里拉一点，所以这里要求
        精确相等是安全的。
        """
        position = self.layout.peer_position
        if L.is_horizontal(position):
            edge = peer_rect.x if position == L.RIGHT else peer_rect.right - 1
            if self._gx != edge:
                return False
            return dx < 0 if position == L.RIGHT else dx > 0
        edge = peer_rect.y if position == L.DOWN else peer_rect.bottom - 1
        if self._gy != edge:
            return False
        return dy < 0 if position == L.DOWN else dy > 0

    def _distance_from_shared_edge(self, peer_rect: L.Rect) -> int:
        """对端光标离"朝向本机的那条边"有多少像素。

        用于判断"用户是不是已经深入对端屏幕了" —— 只有离开入口边一段距离之后，
        `_pegged_at_shared_edge` 才允许触发返回。
        """
        position = self.layout.peer_position
        if L.is_horizontal(position):
            if position == L.RIGHT:
                return self._gx - peer_rect.x
            return peer_rect.right - 1 - self._gx
        if position == L.DOWN:
            return self._gy - peer_rect.y
        return peer_rect.bottom - 1 - self._gy

    def _handle_button(self, event: H.InputEvent) -> None:
        if not self.share_mouse:
            return
        if self._mode is Mode.REMOTE:
            self.cb.send(_MT.MOUSE_BUTTON, {"btn": event.button, "down": event.down}, None)
            self.stats["forwarded_button"] += 1

    def _handle_wheel(self, event: H.InputEvent) -> None:
        if not self.share_mouse:
            return
        if self._mode is Mode.REMOTE:
            self.cb.send(
                _MT.MOUSE_WHEEL,
                {"delta": int(event.wheel), "horizontal": bool(event.horizontal)},
                None,
            )

    # ------------------------------------------------------------ 键盘

    def _handle_key(self, event: H.InputEvent) -> None:
        mods = self._track_mods(event)

        # 热键优先于一切：无论在哪个状态都要能触发
        for binding in self._bindings:
            if binding.matches(event, mods):
                self._trigger(binding.action)
                self._prev_mods = mods
                return

        # 本机保留热键：在这些组合按下时，整段都不转发
        if self._mode is Mode.REMOTE and self._is_local_hotkey(event, mods):
            self._log_key("本机热键，不转发", event, mods)
            self._prev_mods = mods
            return

        self._prev_mods = mods

        if not self.share_keyboard:
            self.stats["key_blocked_share_off"] += 1
            self._log_key("share_keyboard=false", event, mods)
            return
        if self._mode is not Mode.REMOTE:
            self.stats["key_blocked_local"] += 1
            self._log_key("当前是 LOCAL（光标不在对端）", event, mods)
            return
        if not self.forward_media_keys and event.vk in _MEDIA_KEYS:
            self.stats["key_blocked_media"] += 1
            return

        self.cb.send(
            _MT.KEY,
            {
                "vk": event.vk,
                "scan": event.scan,
                "down": event.down,
                "ext": event.extended,
            },
            None,
        )
        self.stats["forwarded_key"] += 1

    def _log_key(self, why: str, event: H.InputEvent, mods: Set[int]) -> None:
        """记一条"按键未转发"。

        这类问题的可能原因太多（模式不对、热键误拦、开关没开、对端没收到），
        没有现场日志基本靠猜，所以这里把判断依据一起打出来。
        """
        log.info(
            "按键未转发: vk=0x%02X scan=0x%02X down=%s | 原因: %s | 模式=%s 修饰键=%s",
            event.vk,
            event.scan,
            event.down,
            why,
            self._mode.value,
            sorted("0x%02X" % m for m in mods) or "无",
        )

    def _track_mods(self, event: H.InputEvent) -> Set[int]:
        """维护修饰键状态，返回**当前按下**的归一化修饰键集合。

        为什么不能直接调用 `_mods_down()`（读 `GetAsyncKeyState`）：
        本方法同时是热键判定的输入，而在单测里我们只能构造"虚拟"按键事件，
        真实键盘状态看不到它们 —— 那样热键逻辑就完全测不了。
        所以以**事件流**为准，只在事件流没给出任何修饰键信息时才去问系统
        （覆盖"程序启动时用户正好按着 Shift"这类情况）。
        """
        norm = _normalize_mod(event.vk)
        if norm:
            if event.down:
                self._mods_down_set |= norm
            else:
                self._mods_down_set -= norm
            if not event.down and not self._mods_down_set:
                # 修饰键全松开：退出"本机热键段"
                self._holding_local_hotkey = False
                self._hotkey_mods = frozenset()
            return set(self._mods_down_set)

        if self._mods_down_set:
            return set(self._mods_down_set)
        # 事件流里还没出现过修饰键：用系统状态兜底（只补不写回）
        return _mods_down()

    def _is_local_hotkey(self, event: H.InputEvent, mods: Set[int]) -> bool:
        """判断当前按键是否属于"永不转发"的本机热键。

        策略是"整段拦下"而不是"只拦那一个键"：一旦检测到某个本机热键的修饰键组合
        已经成立，那么在修饰键全部松开之前，所有按键都不转发。这样才能挡住
        `Ctrl+Alt+Del`（Del 才是主键）、`Win+L`（L 才是主键）这类组合，
        不会把控制权锁死在对端。

        注意 `Ctrl+Alt+Del` 是安全注意序列，系统自己会在更底层截获，
        钩子本来也拿不到；这里只是保证同类组合的行为一致。
        """
        if not self._local_hotkeys:
            return False

        # 已经进入"本机热键段"：修饰键没全松开就继续拦
        if self._holding_local_hotkey:
            if self._hotkey_mods and self._hotkey_mods.issubset(mods):
                return True
            self._holding_local_hotkey = False
            self._hotkey_mods = frozenset()

        pressed = _normalize_mod(event.vk)

        for spec in self._local_hotkeys:
            parsed = _parse_hotkey(spec)
            if not parsed:
                continue
            spec_mods, main = parsed

            if not spec_mods:
                continue

            # 纯修饰键热键（如 "win"）：按下该修饰键就进入拦截状态。
            #
            # **必须用归一化后的键比较**：`_parse_hotkey("win")` 给的是 VK_LWIN(0x5B)，
            # 而用户实际按下的是 VK_LWIN 或 VK_RWIN；`alt` 解析成 VK_MENU(0x12)，而钩子
            # 收到的是 VK_LMENU(0xA4)/VK_RMENU(0xA5)。直接比 VK 会永远不相等 ——
            # 一边是"这些热键完全不起作用"，另一边更糟：`_holding_local_hotkey`
            # 在错误的地方被置位，把随后的正常按键整段吞掉。
            if main is None:
                if pressed & spec_mods:
                    self._holding_local_hotkey = True
                    self._hotkey_mods = spec_mods
                    return True
                continue

            # 组合热键：修饰键齐全且没有多余修饰键时，整段拦下
            if spec_mods.issubset(mods) and not (mods - spec_mods):
                self._holding_local_hotkey = True
                self._hotkey_mods = spec_mods
                return True
        return False

    def _trigger(self, action: str) -> None:
        if action == "recapture":
            self.stats["hotkey_recapture"] += 1
            self.cb.request_recapture()
        elif action == "toggle":
            self.cb.request_toggle()

    # ------------------------------------------------------------ 状态切换

    def _trace(self, text: str) -> None:
        """把一次状态切换记进环形缓冲。

        "鼠标像被弹簧拉住"这类现象的本质是**两端在反复抢控制权**，而单看
        每秒几千条移动日志是看不出来的。这里只记录"谁在什么时候抢了什么"，
        保留最近 200 条，出问题时一次 dump 就能看出循环在哪。
        """
        self._trace_buf.append("%.3f %s" % (time.monotonic(), text))
        if len(self._trace_buf) > 200:
            del self._trace_buf[:100]

    def dump_trace(self, limit: int = 60) -> str:
        if not self._trace_buf:
            return "（没有状态切换记录）"
        return "\n".join(self._trace_buf[-limit:])

    def enter_peer(self, ratio: float) -> None:
        """本机 -> 对端。"""
        if self._mode is Mode.REMOTE or not self._remote_ready or not self._enabled:
            return

        with self._lock:
            self._mode = Mode.REMOTE
            self._swallow = True
        self._last_switch_ts = time.monotonic()

        # 让对端把光标放到对应位置。
        #
        # `from` 必须是**对端**的哪条边被进入，也就是本机方位的**相反**方向：
        # 对端在本机右侧 -> 本机从自己的右边界出去 -> 落在对端的**左**边界。
        #
        # 这里早期发的是 `self.layout.peer_position`（本机自己的方位），于是对端
        # 把光标摆到了它自己那条**反方向**的边上。真机实测：
        #   主机模型期望对端落在 (8, 680)（对端左边缘）
        #   对端实际落在 (1271, 686)（对端右边缘，屏宽 1280）
        # 差了整整一个屏宽，对端光标从第一帧起就贴在错误的那条边上、
        # 只能往一个方向挪 9 像素，然后被主机模型拉回去 —— 又一个"弹簧"来源。
        self.cb.send(_MT.ENTER, {"ratio": float(ratio), "from": _opposite(self.layout.peer_position)}, None)
        #: 重置统一坐标光标到对端入口点，之后所有位移都以它为基准做钳位
        self._reset_cursor_model(ratio)
        self.cb.on_enter_peer(ratio)
        self.stats["switch_to_peer"] += 1
        self._trace("-> REMOTE  ratio=%.2f  (发出 ENTER)" % ratio)
        if self.chain is not None and self.chain.enabled:
            self.chain.log(
                "enter",
                "本机 -> REMOTE 沿边比例=%.3f 对端该落在=(%d,%d) 统一模型起点=(%d,%d) 对端矩形=%s",
                ratio,
                self._peer_x,
                self._peer_y,
                self._gx,
                self._gy,
                self.layout.peer_rect_global(),
            )
        log.info("光标进入对端（沿边比例 %.2f）", ratio)

    def recapture(self, reason: str = "") -> None:
        """对端 -> 本机（或强制收回控制权）。

        两种情况都会走到这里：对端报告光标越出，或本地热键/断线看门狗要求收回。
        """
        was_remote = self._mode is Mode.REMOTE
        with self._lock:
            self._mode = Mode.LOCAL if self._enabled else Mode.PAUSED
            self._swallow = False
        self._last_switch_ts = time.monotonic()
        self._armed_at = self._last_switch_ts + self.arm_delay_sec
        #: 回到 LOCAL：虚拟光标必须**重新锚定到真实光标**。
        #:
        #: REMOTE 期间它累积的是真实位移，而用户是推到对端屏幕的**外边界**才
        #: 交回控制权的，所以它会停在"本机屏 + 对端屏"那么远的地方。不清掉的话
        #: `detect_crossing` 的 `prev < 边界` 永远不成立 —— 用户再也进不去对端。
        #: 详见 `_pull_virtual_cursor_inside`。
        self._refresh_virtual_cursor()
        #: 回到 LOCAL，光标模型切回"本机光标"
        self._reset_cursor_model()

        # 释放对端所有可能残留的按键 —— 否则"对端还按着 Ctrl"会让本机点击全部变成 Ctrl+点击
        self.cb.send(_MT.RELEASE_ALL, {"reason": reason}, None)
        self.cb.release_all(False)

        if was_remote:
            self.cb.on_leave_peer()
            self.stats["switch_to_local"] += 1
        if self.tracer is not None:
            self.tracer.host_leave(reason or "无原因", self._mode.value)
        self._trace("-> LOCAL   %s" % (reason or "无原因"))
        if self.chain is not None and self.chain.enabled:
            self.chain.log(
                "leave",
                "REMOTE -> LOCAL 原因=%s 当时模型=(%d,%d) 本机光标=(%d,%d)",
                reason or "无原因",
                self._gx,
                self._gy,
                self._virt_x,
                self._virt_y,
            )
        if self.diag is not None:
            self.diag.on_recapture()
        log.info("控制权已交回本机%s", ("（%s）" % reason) if reason else "")

    def peer_wants_return(self) -> None:
        """对端报告：它的光标从朝向本机的边缘越出了。"""
        self.recapture("对端光标越出")

    def warp_local_cursor(self, x: int, y: int) -> None:
        """把本机光标放到指定位置（用 `SetCursorPos`）。

        **只在"穿越交接"这种一次性场景使用**（`on_peer_enter`）。连续每帧的绝对
        定位必须走 `note_absolute_position` + 注入，原因见 `Session._apply_absolute`：
        `SetCursorPos` 会产生不带注入标志的事件，需要沉降窗屏蔽，而每帧调用会让
        那个窗口永远开着，把用户自己的鼠标彻底屏蔽掉。

        **关键：SetCursorPos 会产生一条"真实的"鼠标移动事件**（没有
        `LLMHF_INJECTED` 标志，因为移动光标本来就是系统允许的操作）。
        如果不处理它，就会形成死循环：

            A 让 B 把光标放到边缘
              -> B 的光标真的动了 -> B 的钩子产生一条移动事件
              -> B 处于 LOCAL 以为用户在动鼠标 -> B 又发给 A
              -> A 注入位移 -> A 的光标动了 -> A 又发给 B ...

        两边的位移互相咬住，表现就是"鼠标被吸在原地高频抖动"。
        所以每次我们自己移动光标之后，都必须让钩子丢掉紧跟着的那条移动事件。
        """
        self.cb.warp_cursor(int(x), int(y))
        self._virt_x, self._virt_y = int(x), int(y)
        self._virt_have = True

    def note_absolute_position(self, x: int, y: int) -> None:
        """告知"本机光标已经被摆到 (x, y)"，只更新模型，不碰系统光标。

        实际移动由调用方用注入完成（`SendInput`），这样钩子会跳过带注入标志的
        事件，不需要任何沉降窗。
        """
        self._virt_x, self._virt_y = int(x), int(y)
        self._virt_have = True

    # ------------------------------------------------------------ 对端事件

    def on_peer_enter(self, body: Dict) -> None:
        """对端告诉我们：它的光标进入我们这里了（即我们要把输入让给对方）。

        **必须做"我是不是刚让过"的检查。** 这是在两台真机上踩出来的：
        两侧的"退出检测"是各自独立跑的，一个很常见的时序是

            A 推向边界 -> A 发出 ENTER、进入 REMOTE
            B 收到 ENTER -> 摆好光标、回 LEAVE
            B 自己的钩子在**同一瞬间**也检测到"我的光标在朝向 A 的边界上"
                       -> B 也发出 ENTER（想接管）
            A 收到 B 的 ENTER -> A 把自己的光标挪到边界、回 LEAVE
            A 的这次移动又让它的钩子以为刚退出 -> 回到 LOCAL / 再次进入 ...

        表现就是光标被反复"拽回"某个位置（用户形容为弹簧），只有很快地划出去
        才能短暂脱离，然后又会被拉回来。

        拦截方式：刚让出控制权的一方在冷却期内**不接受**对端的接管请求。
        真正的"推回边界"要等冷却期结束（默认几百毫秒，远短于人的操作节奏）。
        """
        if not self._enabled:
            return
        since = time.monotonic() - self._last_switch_ts
        if since < self.switch_cooldown_sec:
            self.stats["peer_enter_ignored"] += 1
            self._trace("忽略接管请求（我方 %.0fms 前刚切换）" % (since * 1000.0))
            log.info(
                "忽略对端的接管请求（我方 %.0fms 前刚切换过，冷却 %.0fms）",
                since * 1000.0,
                self.switch_cooldown_sec * 1000.0,
            )
            return

        ratio = float(body.get("ratio", 0.5))
        from_side = str(body.get("from", self.layout.peer_position))
        # 对端进入本机 -> 本机光标应从与对端方位一致的边进入
        x, y = self.layout.entry_point(ratio, from_side)
        self.stats["peer_enter_accepted"] += 1
        self._trace("接受接管请求 ratio=%.2f -> 光标摆到 (%d,%d)" % (ratio, x, y))
        self.warp_local_cursor(x, y)

    def on_peer_leave(self, _body: Dict) -> None:
        """对端告诉我们：它的光标离开我们这里了。"""
        # 此时本机不需要做几何动作，等对端确认后由对端把控制权交回；
        # 这里只需要丢弃虚拟光标，避免下次穿越时累计出巨大位移。
        self._refresh_virtual_cursor()
        #: 对端光标的模拟位置也失效了，下次进入时会重新按入口点设置
        self._peer_have = False

    def on_peer_release_all(self, _body: Dict) -> None:
        inject.release_all_buttons(force=True)

    def on_peer_mouse_move(self, body: Dict) -> None:
        dx = int(body.get("dx", 0))
        dy = int(body.get("dy", 0))
        if dx or dy:
            inject.move_relative(dx, dy)

    def on_peer_mouse_button(self, body: Dict) -> None:
        inject.button(str(body.get("btn", "")), bool(body.get("down", False)))

    def on_peer_mouse_wheel(self, body: Dict) -> None:
        horizontal = bool(body.get("horizontal", False))
        inject.wheel(int(body.get("delta", 0)), horizontal)

    def on_peer_key(self, body: Dict) -> None:
        inject.key_event(
            int(body.get("vk", 0)),
            bool(body.get("down", False)),
            scan=int(body.get("scan", 0)),
            extended=bool(body.get("ext", False)),
        )

    # ------------------------------------------------------------ 诊断

    def status(self) -> str:
        return "模式=%s 共享=%s 对端可用=%s | %s" % (
            self._mode.value,
            "启用" if self._enabled else "暂停",
            "是" if self._remote_ready else "否",
            " ".join("%s=%d" % (k, v) for k, v in self.stats.items() if v),
        )


#: 媒体键集合（forward_media_keys=False 时不转发）
_MEDIA_KEYS = frozenset(
    {
        w.VK_MEDIA_NEXT_TRACK,
        w.VK_MEDIA_PREV_TRACK,
        w.VK_MEDIA_STOP,
        w.VK_MEDIA_PLAY_PAUSE,
        w.VK_VOLUME_MUTE,
        w.VK_VOLUME_DOWN,
        w.VK_VOLUME_UP,
    }
)


class _MT:
    """消息类型别名，避免在热路径上反复查属性。"""

    from ..protocol import MsgType as _M

    MOUSE_MOVE = _M.MOUSE_MOVE
    MOUSE_BUTTON = _M.MOUSE_BUTTON
    MOUSE_WHEEL = _M.MOUSE_WHEEL
    KEY = _M.KEY
    ENTER = _M.ENTER
    LEAVE = _M.LEAVE
    RELEASE_ALL = _M.RELEASE_ALL


__all__ = ["HotkeyBinding", "InputRouter", "Mode", "RouterCallbacks"]
