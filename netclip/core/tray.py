"""托盘与 netclip 会话之间的胶水层。

`win/tray.py` 只负责"画图标、弹菜单"，不知道 netclip 是什么。
这个模块负责把会话状态翻译成 `TrayState`，并把菜单命令翻译成会话动作。

拆开的好处：`win/tray.py` 可以在没有任何会话的情况下单独测试（构造、
状态刷新、菜单标签），而这一层可以在没有真实托盘的情况下用假对象测试。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable, List, Optional

from ..win import clipboard as cb
from ..win import tray as tray_mod
from ..win.tray import Command, TrayState, TrayWindow

log = logging.getLogger("netclip.core.tray")


class TrayController:
    """把 Session 包装成托盘能用的形式。"""

    def __init__(
        self,
        session: Any,
        quit_callback: Callable[[], None],
        log_path: str = "",
        letter: str = "N",
    ) -> None:
        self.session = session
        self.quit_callback = quit_callback
        self.log_path = log_path
        self.window = TrayWindow(self._build_state, self._on_command, letter=letter)
        self.notifications = 0

    # ------------------------------------------------------------ 输出

    def create(self) -> bool:
        return self.window.create()

    def destroy(self) -> None:
        self.window.destroy()

    def notify(self, level: str, text: str) -> None:
        """给会话用的通知出口。托盘不可用时退化成日志（调用方不需要判断）。"""
        self.notifications += 1
        if not self.window.available:
            log.info("[通知] %s", text)
            return
        title = {"warn": "netclip 提示", "warning": "netclip 提示", "error": "netclip 错误"}.get(level, "netclip")
        try:
            self.window.notify(title, text, level)
        except Exception:  # pragma: no cover
            log.debug("气泡通知发送失败", exc_info=True)

    # ------------------------------------------------------------ 状态

    def _build_state(self) -> TrayState:
        session = self.session
        state = TrayState()

        router = getattr(session, "router", None)
        manager = getattr(session, "manager", None)

        if router is not None:
            state.enabled = bool(router.enabled)
            mode = getattr(router, "mode", None)
            state.remote = bool(mode is not None and getattr(mode, "value", "") == "remote")

        if manager is not None:
            state.input_up = manager.is_up("input")
            state.clip_up = manager.is_up("clip")
            state.file_up = manager.is_up("file")
            state.peer = getattr(manager.state, "peer_name", "") or ""

        state.detail = self._detail_line()
        return state

    def _detail_line(self) -> str:
        parts: List[str] = []
        sync = getattr(self.session, "clipboard_sync", None)
        if sync is not None:
            stats = getattr(sync, "stats", {})
            sent = stats.get("sent", 0)
            recv = stats.get("recv", 0)
            if sent or recv:
                parts.append("剪贴板 ↑%d ↓%d" % (sent, recv))
        files = getattr(self.session, "files", None)
        if files is not None:
            stats = getattr(files, "stats", {})
            if stats.get("sent_files") or stats.get("recv_files"):
                parts.append("文件 ↑%d ↓%d" % (stats.get("sent_files", 0), stats.get("recv_files", 0)))
        return " ".join(parts)

    def status_text(self) -> str:
        """给"复制状态到剪贴板"用的多行文本。"""
        state = self._build_state()
        lines = [
            state.tooltip(),
            "",
            "会话: %s" % (self.session.status() if hasattr(self.session, "status") else "?"),
        ]
        sync = getattr(self.session, "clipboard_sync", None)
        if sync is not None and hasattr(sync, "status"):
            lines.append("剪贴板同步: %s" % sync.status())
        files = getattr(self.session, "files", None)
        if files is not None and hasattr(files, "status"):
            lines.append("文件传输: %s" % files.status())
        if self.log_path:
            lines.append("日志: %s" % self.log_path)
        return "\n".join(lines)

    # ------------------------------------------------------------ 命令

    def _on_command(self, command: int) -> None:
        try:
            self._dispatch(command)
        except Exception:  # pragma: no cover - 托盘命令不能把主循环搞崩
            log.exception("处理托盘命令失败 (cmd=%s)", command)

    def _dispatch(self, command: int) -> None:
        if command == Command.TOGGLE:
            enabled = bool(self.session.toggle())
            log.info("托盘: 共享已%s", "启用" if enabled else "暂停")
            self.notify("info", "共享已%s" % ("启用" if enabled else "暂停"))
        elif command == Command.RECAPTURE:
            self.session.recapture()
            log.info("托盘: 已请求把光标拉回本机")
        elif command == Command.OPEN_STAGING:
            self._open_staging()
        elif command == Command.OPEN_LOG:
            self._open_path(self.log_path, "日志")
        elif command == Command.COPY_STATUS:
            self._copy_status()
        elif command == Command.QUIT:
            log.info("托盘: 用户请求退出")
            self.quit_callback()

    def _open_staging(self) -> None:
        files = getattr(self.session, "files", None)
        staging = getattr(files, "staging", None) if files is not None else None
        if staging is None:
            self.notify("info", "文件同步未启用，没有接收目录")
            return
        path = Path(staging.root) / (staging.peer or "")
        path.mkdir(parents=True, exist_ok=True)
        self._open_path(str(path), "接收目录")

    def _open_path(self, path: str, label: str) -> None:
        if not path:
            self.notify("info", "%s路径未配置" % label)
            return
        target = Path(path)
        if not target.exists():
            self.notify("info", "%s还不存在: %s" % (label, target))
            return
        try:
            os.startfile(str(target))  # noqa: S606 - 打开的是我们自己生成的路径
        except OSError as exc:
            log.warning("打开 %s 失败: %s", label, exc)
            self.notify("warn", "打开%s失败: %s" % (label, exc))

    def _copy_status(self) -> None:
        text = self.status_text()
        try:
            result = cb.write_formats(
                [cb.FormatBlob(name="CF_UNICODETEXT", category=cb.CAT_TEXT, data=text.encode("utf-16-le"))]
            )
        except Exception as exc:  # pragma: no cover
            self.notify("warn", "复制状态失败: %s" % exc)
            return
        if result:
            log.info("托盘: 状态文本已复制到剪贴板")
        else:
            self.notify("warn", "复制状态失败: %s" % result.describe())


__all__ = ["TrayController"]
