"""剪贴板变更监听（隐藏消息窗口 + `WM_CLIPBOARDUPDATE`）。

为什么用 `AddClipboardFormatListener` 而不是定时轮询或 `SetClipboardViewer`
------------------------------------------------------------------------
* **不用轮询**：轮询要么延迟高（用户复制后半天不同步），要么频率高（白烧 CPU）。
  用 `WM_CLIPBOARDUPDATE` 是系统主动通知，延迟最低且几乎没有开销。
* **不用 `SetClipboardViewer`**：那是 Windows 3.x 时代的链式监听协议，要求你
  在被通知后**必须**以 `WM_RENDERFORMAT` 等方式参与渲染协商。一旦处理不当，
  就会长时间占住剪贴板 —— 这正是"其它程序无法写入剪贴板"的典型成因。

**回声抑制**：我们自己写剪贴板也会触发 `WM_CLIPBOARDUPDATE`。如果不处理，
A→B→A→B 会无限乒乓。这里用剪贴板序号（sequence number）做抑制：
写入前记住序号，写入后把"预期序号"登记下来，收到小于该序号的通知就忽略。
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from ctypes import wintypes
from typing import Callable, Optional

from . import winapi as w
from .hooks import WNDCLASSEX

log = logging.getLogger("netclip.win.msgwin")

#: 自定义消息：请求监听线程退出
WM_LISTENER_QUIT = w.WM_APP + 20

#: 抑制窗口：写入剪贴板后这个时间内收到的高序号通知视为自己的回声。
#: 取值要大于系统派发 WM_CLIPBOARDUPDATE 的最坏延迟，但不能太大，
#: 否则用户在这段时间内的真实复制会被吞掉。
SUPPRESS_WINDOW_SEC = 1.5


class ClipboardListener:
    """在独立线程里跑一个隐藏消息窗口，接收剪贴板变更通知。

    `on_change(sequence)` 在**监听线程**里被调用。实现方应该只做"置位 + 唤醒
    消费者线程"，不要在这里读剪贴板（读剪贴板可能被别的进程占用而需要重试）。
    """

    def __init__(self, on_change: Callable[[int], None], name: str = "netclip-clip") -> None:
        self.on_change = on_change
        self.name = name

        self.thread_id: Optional[int] = None
        self.hwnd: Optional[int] = None
        self.ready = threading.Event()

        self._thread: Optional[threading.Thread] = None
        self._suppress_until = 0.0
        self._suppress_min_seq = 0
        self._suppress_lock = threading.Lock()

        self._wnd_proc = w.WNDPROC(self._callback)
        self.stats = {"wm_clipboardupdate": 0, "suppressed": 0, "started": 0, "start_failed": 0}

    # ------------------------------------------------------------ 生命周期

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, timeout: float = 5.0) -> bool:
        if self._thread is not None:
            return True
        self.ready.clear()
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()
        if not self.ready.wait(timeout):
            log.error("剪贴板监听线程在 %.1fs 内没有就绪", timeout)
            return False
        return self.hwnd is not None

    def stop(self, timeout: float = 2.0) -> None:
        if self.thread_id:
            try:
                w.user32.PostThreadMessageW(self.thread_id, WM_LISTENER_QUIT, 0, 0)
            except Exception:  # pragma: no cover
                pass
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    # ------------------------------------------------------------ 回声抑制

    def suppress_next(self, window: float = SUPPRESS_WINDOW_SEC) -> None:
        """在接下来的 `window` 秒内，忽略剪贴板变更通知（我们即将/刚刚自己写入）。

        调用点必须**在** `SetClipboardData` 之前，因为 WM_CLIPBOARDUPDATE 有可能
        在写入后极短时间内就被派发。
        """
        with self._suppress_lock:
            self._suppress_until = time.monotonic() + max(0.0, window)
            try:
                # 只抑制"序号不比当前大"的通知，避免把用户真实复制也误吞
                self._suppress_min_seq = int(w.user32.GetClipboardSequenceNumber())
            except Exception:  # pragma: no cover
                self._suppress_min_seq = 0

    def expect_sequence(self, sequence: int, window: float = SUPPRESS_WINDOW_SEC) -> None:
        """精确抑制指定序号（含附近的小幅增长）的剪贴板通知。

        用于"刚把远端内容写进剪贴板"的场景。比 `suppress_next()` 更准：
        不用猜当前序号，而是直接用写入后 `GetClipboardSequenceNumber()` 拿到的值。

        容差给 16：一次写入可能触发不止一条 `WM_CLIPBOARDUPDATE`
        （不同格式的延迟渲染各触发一次），但容差不能太大 —— 否则用户在这段
        时间内的真实复制会被误吞，表现为"复制了不同步"。
        """
        with self._suppress_lock:
            self._suppress_until = time.monotonic() + max(0.0, window)
            self._suppress_min_seq = int(sequence)

    def _is_suppressed(self, sequence: int) -> bool:
        with self._suppress_lock:
            if time.monotonic() > self._suppress_until:
                return False
            # 容忍少量序号增长：一次写入会触发不止一次 WM_CLIPBOARDUPDATE
            # （不同格式的延迟渲染各触发一次），所以给一个 32 的宽限。
            if sequence > self._suppress_min_seq + 32:
                return False
            return True

    def clear_suppression(self) -> None:
        with self._suppress_lock:
            self._suppress_until = 0.0

    # ------------------------------------------------------------ 线程主体

    def _run(self) -> None:
        self.thread_id = int(w.kernel32.GetCurrentThreadId())
        try:
            self.hwnd = self._create_window()
        except Exception as exc:
            log.exception("剪贴板监听窗口创建失败: %s", exc)
            self.stats["start_failed"] += 1
            self.ready.set()
            return

        if not w.user32.AddClipboardFormatListener(self.hwnd):
            log.error("AddClipboardFormatListener 失败: GetLastError=%d", ctypes.get_last_error())
            self.stats["start_failed"] += 1
        else:
            self.stats["started"] += 1
            log.info("剪贴板监听已启动 (tid=%s)", self.thread_id)

        self.ready.set()

        msg = w.MSG()
        while True:
            ret = w.user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret == 0:
                break
            if ret == -1:
                log.error("剪贴板监听线程 GetMessageW 出错")
                break
            if msg.message == WM_LISTENER_QUIT:
                break
            w.user32.TranslateMessage(ctypes.byref(msg))
            w.user32.DispatchMessageW(ctypes.byref(msg))

        if self.hwnd:
            w.user32.RemoveClipboardFormatListener(self.hwnd)
        log.info("剪贴板监听已停止")

    def _create_window(self) -> int:
        hinstance = w.kernel32.GetModuleHandleW(None)
        class_name = "netclip_clip_window_%d" % (self.thread_id or 0)

        wc = WNDCLASSEX()
        wc.cbSize = ctypes.sizeof(WNDCLASSEX)
        wc.lpfnWndProc = ctypes.cast(self._wnd_proc, ctypes.c_void_p)
        wc.hInstance = hinstance
        wc.lpszClassName = class_name
        atom = w.user32.RegisterClassExW(ctypes.byref(wc))
        if not atom and ctypes.get_last_error() not in (0, 1410):
            log.warning("RegisterClassExW 失败: %d", ctypes.get_last_error())

        hwnd = w.user32.CreateWindowExW(
            0, class_name, "netclip-clip", 0, 0, 0, 0, 0, w.HWND_MESSAGE, None, hinstance, None
        )
        if not hwnd:
            raise w.WinApiError(ctypes.get_last_error(), "CreateWindowExW 失败")
        return int(hwnd)

    def _callback(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        if msg == w.WM_CLIPBOARDUPDATE:
            self.stats["wm_clipboardupdate"] += 1
            try:
                sequence = int(w.user32.GetClipboardSequenceNumber())
            except Exception:  # pragma: no cover
                sequence = 0
            if self._is_suppressed(sequence):
                self.stats["suppressed"] += 1
                return 0
            try:
                self.on_change(sequence)
            except Exception:  # pragma: no cover - 回调异常不能杀掉消息循环
                log.exception("剪贴板变更回调异常")
            return 0
        if msg == w.WM_DESTROY:
            w.user32.PostQuitMessage(0)
            return 0
        return int(w.user32.DefWindowProcW(hwnd, msg, wparam, lparam))


class ClipboardWatcher:
    """把"监听 + 去抖 + 采集"组合成一个可用的生产者。

    监听线程只负责置位；真正读剪贴板由调用方在**自己的线程**里通过
    `wait_for_change()` 取。这样读剪贴板的重试退避不会阻塞消息循环。
    """

    def __init__(self, debounce_ms: int = 120) -> None:
        self.debounce_sec = max(0.0, debounce_ms / 1000.0)
        self._pending = threading.Event()
        self._listener = ClipboardListener(self._on_change)
        self.stats = {"captured": 0, "superseded": 0}

    @property
    def listener(self) -> ClipboardListener:
        return self._listener

    def _on_change(self, _sequence: int) -> None:
        self._pending.set()

    def start(self) -> bool:
        return self._listener.start()

    def stop(self) -> None:
        self._listener.stop()

    def suppress_next(self, window: float = SUPPRESS_WINDOW_SEC) -> None:
        self._listener.suppress_next(window)

    def expect_sequence(self, sequence: int, window: float = SUPPRESS_WINDOW_SEC) -> None:
        self._listener.expect_sequence(sequence, window)

    def clear_suppression(self) -> None:
        self._listener.clear_suppression()

    def discard_pending(self) -> None:
        """丢掉"已经排队但还没被取走"的那次变更通知。

        用于**马上要自己写剪贴板**的场景。抑制是消息窗口在处理
        `WM_CLIPBOARDUPDATE` 的那一刻生效的，所以如果通知已经先一步处理完
        （例如上一次写入的回声），`_pending` 已经被置位了 —— 光武装抑制拦不住它。
        不清掉的话，同步循环会把刚写进去的内容当成"用户复制了东西"再发一遍。
        """
        self._pending.clear()

    def wait_for_change(self, timeout: Optional[float] = None) -> bool:
        """阻塞等待一次（去抖后的）剪贴板变更。返回是否等到。"""
        if not self._pending.wait(timeout):
            return False
        # 去抖：连续多次变更合并成一次处理
        if self.debounce_sec:
            time.sleep(self.debounce_sec)
        self._pending.clear()
        self.stats["captured"] += 1
        return True


__all__ = ["ClipboardListener", "ClipboardWatcher", "SUPPRESS_WINDOW_SEC"]
