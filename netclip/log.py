"""日志初始化。

设计要点：
  * 输入热路径（钩子回调）**不打日志**，否则会被 Windows 判定钩子超时。
  * 网络/剪贴板等后台线程的日志走 QueueHandler，由单独线程落盘，避免持锁阻塞。
  * 控制台输出带毫秒时间戳，便于和 clipdump 的格式序列号对齐排查。
"""

from __future__ import annotations

import logging
import logging.handlers
import queue
import sys
from pathlib import Path
from typing import Optional

_CONFIGURED = False

_FMT = "%(asctime)s.%(msecs)03d %(levelname)-7s [%(threadName)-14s] %(name)-22s %(message)s"
_DATEFMT = "%H:%M:%S"


def setup_logging(
    level: str = "INFO",
    log_file: Optional[str] = None,
    max_bytes: int = 2 * 1024 * 1024,
    backup_count: int = 3,
) -> logging.Logger:
    """配置根 logger，可重复调用（幂等）。"""
    global _CONFIGURED

    root = logging.getLogger()
    numeric = getattr(logging, str(level).upper(), logging.INFO)
    root.setLevel(numeric)

    if _CONFIGURED:
        for handler in root.handlers:
            handler.setLevel(numeric)
        return logging.getLogger("netclip")

    formatter = logging.Formatter(_FMT, datefmt=_DATEFMT)

    # pythonw.exe（无控制台）里 sys.stderr 是 None，直接挂 StreamHandler 会在
    # 每次写日志时抛 AttributeError。这种情况必须跳过控制台 handler，
    # 否则"双击启动"就会看到一堆 traceback（甚至因为日志异常把主流程带偏）。
    if sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        root.addHandler(stream)

    if log_file:
        path = Path(log_file)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                str(path),
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)

            # QueueHandler/QueueListener：让业务线程写日志时永不阻塞在自己身上
            log_queue: "queue.Queue[logging.LogRecord]" = queue.Queue(-1)
            queue_handler = logging.handlers.QueueHandler(log_queue)
            listener = logging.handlers.QueueListener(log_queue, file_handler, respect_handler_level=True)
            listener.start()
            root.addHandler(queue_handler)
        except OSError:
            # 日志文件写不进去（目录只读、盘满…）不该让程序起不来
            if sys.stderr is not None:
                root.warning("无法写日志文件 %s", path)

    _CONFIGURED = True
    return logging.getLogger("netclip")
