"""剪贴板同步决策（纯逻辑）。

把"这份剪贴板内容该怎么同步"从实际的网络/剪贴板操作里拆出来，理由是：

  * 它是**策略**，会随着使用体验反复调整（哪些格式要传、多大算大、什么情况下
    只提示不传），放在独立模块里可以单独测试，改起来不怕碰坏 I/O 代码；
  * 出问题时能一眼看出"为什么这条内容没同步"——`SyncDecision.reason` 里
    带完整的理由，直接能显示给用户。

不依赖 Windows，因此可以直接单测。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Pattern, Sequence, Tuple

ACTION_FULL = "full"  # 完整传输内容
ACTION_ANNOUNCE = "announce"  # 只通知元数据（内容太大，让对方决定）
ACTION_SKIP = "skip"  # 完全不同步

CAT_TEXT = "text"
CAT_HTML = "html"
CAT_RTF = "rtf"
CAT_IMAGE = "image"
CAT_FILES = "files"
CAT_OLE = "ole"
CAT_OTHER = "other"

#: 每个类别对应的配置开关名（见 `SyncPolicy.toggles`）。
#: 文件也在里面 —— 它走独立的文件通道，但"要不要同步"由同一个开关决定。
_CATEGORY_TOGGLE = {
    CAT_TEXT: "sync_text",
    CAT_RTF: "sync_rtf",
    CAT_HTML: "sync_html",
    CAT_IMAGE: "sync_image",
    CAT_FILES: "sync_files",
}


@dataclass(frozen=True)
class FormatSummary:
    """一份格式的元数据（不含数据本身）。"""

    name: str
    size: int
    category: str = CAT_OTHER
    #: 数据是否被压缩过（`announce` 时对方据此判断值不值得要）
    compressed: bool = False

    @property
    def wire_size(self) -> int:
        """线上占用的字节数（压缩后按未压缩算的大小）。"""
        return self.size


@dataclass
class SyncDecision:
    """一次同步的决策结果。"""

    action: str = ACTION_SKIP
    reason: str = ""
    #: 决定要传输的格式（`full` 时才有意义）
    items: List[object] = field(default_factory=list)
    #: 被主动丢弃的格式 -> 原因
    dropped: List[Tuple[str, str]] = field(default_factory=list)
    #: 元数据（`announce` 时只发这些）
    summaries: List[FormatSummary] = field(default_factory=list)
    total_bytes: int = 0
    #: 这份内容里是否含有文件（用于触发文件通道的真传输）
    has_files: bool = False
    #: 文件路径列表（`has_files` 时有效）
    file_paths: List[str] = field(default_factory=list)
    #: 文件总大小
    files_total_bytes: int = 0

    @property
    def syncs(self) -> bool:
        return self.action != ACTION_SKIP

    def describe(self) -> str:
        if self.action == ACTION_SKIP:
            return "不同步: %s" % (self.reason or "无内容")
        head = "完整同步" if self.action == ACTION_FULL else "仅通知（内容过大）"
        text = "%s: %d 种格式 %.1f KB" % (head, len(self.summaries), self.total_bytes / 1024.0)
        if self.has_files:
            text += "，含 %d 个文件共 %.1f KB" % (
                len(self.file_paths),
                self.files_total_bytes / 1024.0,
            )
        if self.dropped:
            text += "，被丢弃 %d 种（%s）" % (
                len(self.dropped),
                "; ".join("%s: %s" % pair for pair in self.dropped[:3]),
            )
        if self.reason:
            text += "；%s" % self.reason
        return text


class SyncPolicy:
    """把配置翻译成"要不要同步、同步哪些格式"的判断。

    参数都是可调的策略值，通过构造器注入，便于测试与热更新。
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        sync_text: bool = True,
        sync_rtf: bool = True,
        sync_html: bool = True,
        sync_image: bool = True,
        sync_files: bool = True,
        max_payload_mb: int = 50,
        per_format_max_mb: int = 100,
        forward_all: bool = True,
        exclude: Sequence[str] = (),
        allow_private: Sequence[str] = (),
        frame_limit: Optional[int] = None,
    ) -> None:
        self.enabled = enabled
        self.toggles = {
            CAT_TEXT: sync_text,
            CAT_RTF: sync_rtf,
            CAT_HTML: sync_html,
            CAT_IMAGE: sync_image,
            CAT_FILES: sync_files,
        }
        self.max_payload_bytes = max(0, int(max_payload_mb)) * 1024 * 1024
        self.per_format_max_bytes = max(1, int(per_format_max_mb)) * 1024 * 1024
        self.forward_all = forward_all
        self._exclude: List[Pattern] = [re.compile(p) for p in exclude if p]
        self._allow_private: List[Pattern] = [re.compile(p) for p in allow_private if p]
        #: 单帧上限（由协议层传入）。超过的话即使压缩后也发不出去，那就干脆不传。
        self.frame_limit = frame_limit

    # ------------------------------------------------------------ 格式过滤

    @staticmethod
    def compile_exclude(patterns: Sequence[str]) -> List[Pattern]:
        """把正则字符串编译好。调用方可以**预编译一次**、反复用，避免每次采集都重编译。"""
        return [re.compile(p) for p in patterns if p]

    def format_allowed(
        self, name: str, category: str, exclude_patterns: Optional[Sequence[Pattern]] = None
    ) -> Tuple[bool, str]:
        """判断单个格式是否允许传输。返回 (是否允许, 不允许的原因)。

        `exclude_patterns` 给定时**替代**配置里的 `exclude` —— 用于"按复制来源进程
        切换丢弃列表"（见 `bridge.ClipboardSync.publish_local`）。
        """
        toggle_key = _CATEGORY_TOGGLE.get(category)
        if toggle_key is not None and not self.toggles.get(category, True):
            return False, "该类别已在配置中关闭"

        for pattern in self._exclude if exclude_patterns is None else exclude_patterns:
            if pattern.search(name):
                return False, "匹配 clipboard.formats.exclude"

        if not self.forward_all and category in (CAT_OTHER, CAT_OLE):
            # forward_all=false 时只同步有明确语义的通用格式。
            # 这会让 PPT 形状 / MathType 公式在对方变成图片或文本（降级），
            # 所以默认是开启全格式转发的。
            return False, "forward_all=false 且不属于通用格式"

        if self._allow_private and category in (CAT_OTHER, CAT_OLE):
            if not any(p.search(name) for p in self._allow_private):
                return False, "不在 clipboard.formats.allow_private 白名单内"

        return True, ""

    # ------------------------------------------------------------ 主判断

    def decide(
        self,
        items: Sequence[object],
        *,
        files: Sequence[Tuple[str, int]] = (),
        skipped: Sequence[Tuple[str, str]] = (),
        exclude_patterns: Optional[Sequence[Pattern]] = None,
    ) -> SyncDecision:
        """给出同步决策。

        `items` 是 `win.clipboard.FormatBlob` 列表（本模块只看 `.name/.data/.size`
        和可选的 `.category`，所以用任何鸭子类型都能测）。
        `files` 是 [(路径, 大小)]，来自 CF_HDROP。
        `skipped` 是采集阶段就失败的格式 [(名字, 原因)]，原样带进决策结果。
        `exclude_patterns` 给定时替代配置里的 `exclude`（按来源进程切换时用）。
        """
        decision = SyncDecision(dropped=list(skipped))
        if not self.enabled:
            decision.reason = "剪贴板同步已禁用"
            return decision

        kept: List[object] = []
        summaries: List[FormatSummary] = []
        total = 0
        for item in items:
            name = str(getattr(item, "name", ""))
            category = str(getattr(item, "category", CAT_OTHER))
            size = int(getattr(item, "size", 0))
            if not size:
                size = len(getattr(item, "data", b"") or b"")

            allowed, why = self.format_allowed(name, category, exclude_patterns)
            if not allowed:
                decision.dropped.append((name, why))
                continue
            if size > self.per_format_max_bytes:
                decision.dropped.append(
                    (name, "超过单格式上限 %.0f MB" % (self.per_format_max_bytes / 1024.0 / 1024.0))
                )
                continue

            kept.append(item)
            summaries.append(FormatSummary(name=name, size=size, category=category))
            total += size

        if files and self.toggles.get(CAT_FILES, True):
            decision.has_files = True
            decision.file_paths = [str(path) for path, _size in files]
            decision.files_total_bytes = int(sum(int(size) for _path, size in files))
        elif files:
            decision.dropped.append(("<文件>", "文件同步已关闭"))

        if not kept and not decision.has_files:
            decision.reason = "全部格式被过滤" if decision.dropped else "没有可同步的格式"
            return decision

        decision.items = kept
        decision.summaries = summaries
        decision.total_bytes = total

        limit = self.max_payload_bytes
        if self.frame_limit is not None:
            limit = min(limit, self.frame_limit)
        if total > limit:
            decision.action = ACTION_ANNOUNCE
            decision.items = []
            decision.reason = "内容 %.1f MB 超过上限 %.1f MB" % (
                total / 1024.0 / 1024.0,
                limit / 1024.0 / 1024.0,
            )
            return decision

        decision.action = ACTION_FULL
        if decision.dropped:
            decision.reason = "部分格式被过滤"
        return decision


__all__ = [
    "ACTION_ANNOUNCE",
    "ACTION_FULL",
    "ACTION_SKIP",
    "CAT_FILES",
    "CAT_HTML",
    "CAT_IMAGE",
    "CAT_OLE",
    "CAT_OTHER",
    "CAT_RTF",
    "CAT_TEXT",
    "FormatSummary",
    "SyncDecision",
    "SyncPolicy",
]
