"""从配置构造剪贴板同步器。

把"配置 -> 对象"的装配逻辑单独放在这里，而不是塞进 `core/session.py`：
`session.py` 已经足够长了，而且它关心的是线程与生命周期，不关心某个功能的
参数怎么来。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from ..config import Config
from ..files.transfer import FilesTransfer, Staging
from ..protocol import MAX_FRAME_BYTES
from ..win import clipboard as cb
from .bridge import ClipboardSync
from .policy import SyncPolicy

log = logging.getLogger("netclip.clipsync")

#: 单帧预算：帧头 + JSON 头 + 数据。留 8KB 给头部绰绰有余。
FRAME_PAYLOAD_BUDGET = MAX_FRAME_BYTES - 8 * 1024


def build_policy(cfg: Config) -> SyncPolicy:
    c = cfg.clipboard
    return SyncPolicy(
        enabled=c.enable,
        sync_text=c.sync_text,
        sync_rtf=c.sync_rtf,
        sync_html=c.sync_html,
        sync_image=c.sync_image,
        sync_files=c.sync_files,
        max_payload_mb=c.max_payload_mb,
        per_format_max_mb=c.formats.per_format_max_mb,
        forward_all=c.formats.forward_all,
        exclude=c.formats.exclude,
        allow_private=c.formats.allow_private,
        frame_limit=FRAME_PAYLOAD_BUDGET,
    )


def build_files_transfer(
    cfg: Config,
    send_file: Callable[[int, Dict, bytes], None],
    on_files_ready: Callable[[Any], None],
    notify: Optional[Callable[[str, str], None]] = None,
    peer_name: str = "peer",
    yield_fn: Optional[Callable[[], None]] = None,
) -> Optional[FilesTransfer]:
    """构造文件传输器；配置里没开文件同步就返回 None。"""
    if not (cfg.clipboard.enable and cfg.clipboard.sync_files):
        log.info("文件同步未启用（clipboard.sync_files = false）")
        return None

    files = cfg.clipboard.files
    #: `deliver = "folder"`（默认）：直接投递到用户可见目录，不碰剪贴板。
    #: 只有显式选 `clipboard` 时才走"暂存 + 放回剪贴板"那条路（它依赖资源管理器
    #: 的粘贴实现，真机上把 Explorer 搞崩过两次）。
    from ..config import default_receive_dir

    deliver_dir = None
    if files.deliver == "folder":
        deliver_dir = files.receive_dir or default_receive_dir()
        log.info("文件投递方式: 直接存到目录 %s（不经过剪贴板）", deliver_dir)

    staging = Staging(
        root=None,
        peer=peer_name,
        ttl_minutes=files.staging_ttl_min,
        deliver_dir=deliver_dir,
    )
    return FilesTransfer(
        send=send_file,
        staging=staging,
        chunk_size=files.chunk_size_kb * 1024,
        auto_transfer_max_bytes=files.auto_transfer_max_mb * 1024 * 1024,
        over_threshold_action=files.over_threshold_action,
        verify_sha256=files.verify_sha256,
        notify=notify,
        on_files_ready=on_files_ready,
        yield_fn=yield_fn,
    )


def build_clipboard_sync(
    cfg: Config,
    send: Callable[[int, Dict, bytes, Optional[str]], None],
    notify: Optional[Callable[[str, str], None]] = None,
    files_handler: Optional[Any] = None,
) -> Optional[ClipboardSync]:
    """构造剪贴板同步器；配置里禁用了就返回 None。"""
    if not cfg.clipboard.enable:
        log.info("剪贴板同步已在配置中禁用")
        return None

    policy = build_policy(cfg)
    sync = ClipboardSync(
        policy=policy,
        send=send,
        compress=cfg.clipboard.formats.compress,
        inline_html_refs=cfg.clipboard.formats.inline_html_refs,
        ole_finish=cfg.clipboard.formats.ole_finish,
        exclude_by_process=cfg.clipboard.formats.exclude_by_process,
        notify=notify,
        files_handler=files_handler,
    )
    #: 写剪贴板失败时的重试退避，来自配置
    sync._open_retry_ms = tuple(cfg.clipboard.open_retry_ms)  # noqa: SLF001 - 同包内的装配细节
    return sync


__all__ = [
    "FRAME_PAYLOAD_BUDGET",
    "build_clipboard_sync",
    "build_files_transfer",
    "build_policy",
]
