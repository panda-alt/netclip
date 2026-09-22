"""看门狗：断线回拉、松键、钩子自愈、剪贴板监听自愈。

存在的意义
----------
这类工具最糟的失败模式不是"不工作"，而是"**看起来卡住了**"：

  * 光标跑到对端后网断了 —— 本机鼠标完全没反应，用户只能强杀进程；
  * 对端还留着一个按下的 Ctrl —— 回到本机后每次点击都变成 Ctrl+点击；
  * 钩子被 Windows 静默摘掉 —— 鼠标能动，但共享功能再也不生效，且无任何报错。

看门狗就是处理这三种情况的兜底逻辑。它跑在**独立线程**里，只做周期性检查，
所以即使网络层或钩子层出了问题，它依然能工作。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

log = logging.getLogger("netclip.core.watchdog")


class Watchdog:
    """周期性健康检查。

    `check_*` 回调返回 True 表示发现问题并已处理（用于日志统计）。
    """

    def __init__(
        self,
        *,
        is_peer_input_ready: Callable[[], bool],
        is_remote_mode: Callable[[], bool],
        recapture: Callable[[str], None],
        release_all: Callable[[], None],
        last_recv_age: Callable[[], float],
        timeout_sec: float,
        reinstall_hooks: Optional[Callable[[], bool]] = None,
        on_unhealthy: Optional[Callable[[str], None]] = None,
        poll_interval_sec: float = 0.5,
        hooks_silent_sec: float = 60.0,
        is_hook_alive: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._is_peer_input_ready = is_peer_input_ready
        self._is_remote_mode = is_remote_mode
        self._recapture = recapture
        self._release_all = release_all
        self._last_recv_age = last_recv_age
        self.timeout_sec = max(1.0, float(timeout_sec))
        self._reinstall_hooks = reinstall_hooks
        self._on_unhealthy = on_unhealthy
        self.poll_interval_sec = max(0.1, float(poll_interval_sec))
        self.hooks_silent_sec = max(10.0, float(hooks_silent_sec))
        self._is_hook_alive = is_hook_alive

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._hook_reinstall_cooldown = 0.0
        self.stats = {
            "recapture_disconnect": 0,
            "recapture_timeout": 0,
            "hook_reinstall": 0,
            "timeout_detected": 0,
        }

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="netclip-watchdog", daemon=True)
        self._thread.start()
        log.info("看门狗已启动（超时判定 %.1fs）", self.timeout_sec)

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    # ------------------------------------------------------------ 检查循环

    def _run(self) -> None:
        while not self._stop.wait(self.poll_interval_sec):
            try:
                self.tick()
            except Exception:  # pragma: no cover - 看门狗自身绝不能挂
                log.exception("看门狗检查异常")

    def tick(self) -> None:
        """跑一轮检查（也供单测直接调用）。"""
        remote = self._is_remote_mode()
        ready = self._is_peer_input_ready()
        age = self._last_recv_age()

        # 1) 处于 REMOTE 但对端已经不可用 -> 立刻把控制权收回来
        if remote and not ready:
            self.stats["recapture_disconnect"] += 1
            log.warning("对端通道已断，强制收回控制权")
            self._recapture("对端断开")
            self._release_all()
            self._notify("对端断开，控制权已交回本机")
            return

        # 2) 输入通道看起来还连着，但超时没有收到任何帧 -> 判定链路已死
        if age is not None and age > self.timeout_sec:
            self.stats["timeout_detected"] += 1
            log.warning("链路超时（%.1fs 无数据），判定断线", age)
            if remote:
                self.stats["recapture_timeout"] += 1
                self._recapture("链路超时")
                self._release_all()
            self._notify("与对端的链路超时，正在重连")
            # 让检查间隔拉长一点，避免在真的断线期间刷屏
            time.sleep(1.0)
            return

        # 3) 钩子进程还活着但没有事件 —— 可能被系统摘掉了，尝试重装
        if self._is_hook_alive is not None and self._reinstall_hooks is not None:
            if not self._is_hook_alive():
                if time.monotonic() >= self._hook_reinstall_cooldown:
                    self._hook_reinstall_cooldown = time.monotonic() + 30.0
                    self.stats["hook_reinstall"] += 1
                    log.warning("检测到钩子可能已失效，请求重新安装")
                    self._reinstall_hooks()

    def _notify(self, message: str) -> None:
        if self._on_unhealthy:
            try:
                self._on_unhealthy(message)
            except Exception:  # pragma: no cover
                pass

    def status(self) -> str:
        active = {k: v for k, v in self.stats.items() if v}
        return " ".join("%s=%d" % (k, v) for k, v in active.items()) or "无异常"
