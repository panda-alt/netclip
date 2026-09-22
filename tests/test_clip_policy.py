"""剪贴板同步策略的单元测试。纯逻辑，不依赖 Windows。"""

from __future__ import annotations

import pytest

from netclip.clipsync.policy import (
    ACTION_ANNOUNCE,
    ACTION_FULL,
    ACTION_SKIP,
    CAT_FILES,
    CAT_HTML,
    CAT_IMAGE,
    CAT_OLE,
    CAT_OTHER,
    CAT_RTF,
    CAT_TEXT,
    SyncPolicy,
)


class FakeBlob:
    """模拟 `win.clipboard.FormatBlob`（策略层只看这几个属性）。"""

    def __init__(self, name, data=b"x", category=CAT_OTHER, size=None):
        self.name = name
        self.data = data
        self.category = category
        self.size = len(data) if size is None else size


def blob(name, n=10, category=CAT_OTHER):
    return FakeBlob(name, data=b"x" * n, category=category)


# --------------------------------------------------------------------- 开关


def test_disabled_policy_skips_everything():
    policy = SyncPolicy(enabled=False)
    decision = policy.decide([blob("CF_UNICODETEXT", category=CAT_TEXT)])
    assert decision.action == ACTION_SKIP
    assert "禁用" in decision.reason
    assert not decision.syncs


def test_category_toggles_are_honoured():
    policy = SyncPolicy(sync_image=False, sync_html=False)
    decision = policy.decide(
        [
            blob("CF_UNICODETEXT", category=CAT_TEXT),
            blob("CF_DIB", category=CAT_IMAGE),
            blob("HTML Format", category=CAT_HTML),
        ]
    )
    assert decision.action == ACTION_FULL
    names = [s.name for s in decision.summaries]
    assert names == ["CF_UNICODETEXT"]
    dropped = dict(decision.dropped)
    assert "CF_DIB" in dropped and "HTML Format" in dropped


def test_all_categories_can_be_disabled():
    policy = SyncPolicy(sync_text=False, sync_rtf=False, sync_html=False, sync_image=False, sync_files=False)
    decision = policy.decide([blob("CF_UNICODETEXT", category=CAT_TEXT)])
    assert decision.action == ACTION_SKIP
    assert "全部格式被过滤" in decision.reason


# --------------------------------------------------------------------- 格式过滤


def test_exclude_regex_drops_matching_format():
    policy = SyncPolicy(exclude=[r"^Ole Private Data$", r"^Link Source.*"])
    decision = policy.decide(
        [
            blob("Ole Private Data", category=CAT_OLE),
            blob("Link Source Descriptor", category=CAT_OLE),
            blob("PowerPoint 12.0 Shape", category=CAT_IMAGE),
        ]
    )
    assert [s.name for s in decision.summaries] == ["PowerPoint 12.0 Shape"]
    assert len(decision.dropped) == 2


def test_forward_all_false_drops_private_formats():
    """关掉全格式转发时，PPT 形状这类私有格式不会被同步（会对端降级）。"""
    policy = SyncPolicy(forward_all=False)
    decision = policy.decide(
        [
            blob("CF_UNICODETEXT", category=CAT_TEXT),
            blob("MathType 5.0 Equations", category=CAT_OLE),
            blob("Vendor Blob", category=CAT_OTHER),
        ]
    )
    assert [s.name for s in decision.summaries] == ["CF_UNICODETEXT"]
    assert len(decision.dropped) == 2


def test_forward_all_true_keeps_private_formats():
    """默认必须保留私有格式，否则 MathType/PPT 到对端就是死图。"""
    policy = SyncPolicy(forward_all=True)
    decision = policy.decide(
        [
            blob("MathType 5.0 Equations", category=CAT_OLE),
            blob("PowerPoint 12.0 Shape", category=CAT_IMAGE),
        ]
    )
    assert len(decision.summaries) == 2


def test_allow_private_whitelist_narrows():
    policy = SyncPolicy(allow_private=[r"^PowerPoint", r"^MathType"])
    decision = policy.decide(
        [
            blob("PowerPoint 12.0 Shape", category=CAT_IMAGE),
            blob("MathType 5.0 Equations", category=CAT_OLE),
            blob("SomeRandomVendor", category=CAT_OTHER),
        ]
    )
    assert [s.name for s in decision.summaries] == ["PowerPoint 12.0 Shape", "MathType 5.0 Equations"]


def test_allow_private_does_not_filter_generic_formats():
    """白名单只约束私有格式，不能把文本/HTML 也挡掉。"""
    policy = SyncPolicy(allow_private=[r"^PowerPoint"])
    decision = policy.decide(
        [
            blob("CF_UNICODETEXT", category=CAT_TEXT),
            blob("HTML Format", category=CAT_HTML),
            blob("Rich Text Format", category=CAT_RTF),
        ]
    )
    assert decision.action == ACTION_FULL
    assert len(decision.summaries) == 3


def test_per_format_size_limit():
    policy = SyncPolicy(per_format_max_mb=1)
    decision = policy.decide(
        [
            blob("small", n=100, category=CAT_TEXT),
            blob("huge", n=2 * 1024 * 1024, category=CAT_IMAGE),
        ]
    )
    assert [s.name for s in decision.summaries] == ["small"]
    assert any("上限" in reason for _n, reason in decision.dropped)


# --------------------------------------------------------------------- 大小策略


def test_small_payload_is_full_sync():
    policy = SyncPolicy(max_payload_mb=10)
    decision = policy.decide([blob("CF_UNICODETEXT", n=1024, category=CAT_TEXT)])
    assert decision.action == ACTION_FULL
    assert decision.total_bytes == 1024
    assert len(decision.items) == 1


def test_oversized_payload_becomes_announce_only():
    """超过上限时只发元数据 —— 不传内容，但要让对方知道。"""
    policy = SyncPolicy(max_payload_mb=1)
    decision = policy.decide([blob("CF_DIB", n=2 * 1024 * 1024, category=CAT_IMAGE)])
    assert decision.action == ACTION_ANNOUNCE
    assert decision.items == []  # 内容不传
    assert len(decision.summaries) == 1  # 元数据还在
    assert "超过上限" in decision.reason
    assert decision.syncs  # 仍然算"要同步"（要发通知）


def test_frame_limit_tightens_the_budget():
    """单帧上限比配置上限更小时，以单帧上限为准。"""
    policy = SyncPolicy(max_payload_mb=100, frame_limit=1024)
    decision = policy.decide([blob("CF_DIB", n=2048, category=CAT_IMAGE)])
    assert decision.action == ACTION_ANNOUNCE


def test_empty_payload_is_skipped():
    policy = SyncPolicy()
    decision = policy.decide([])
    assert decision.action == ACTION_SKIP
    assert "没有可同步的格式" in decision.reason


def test_collection_errors_are_carried_through():
    policy = SyncPolicy()
    decision = policy.decide(
        [blob("CF_UNICODETEXT", category=CAT_TEXT)],
        skipped=[("CF_BITMAP", "进程内句柄格式，跳过")],
    )
    assert decision.action == ACTION_FULL
    assert ("CF_BITMAP", "进程内句柄格式，跳过") in decision.dropped


# --------------------------------------------------------------------- 文件


def test_file_paths_are_reported():
    policy = SyncPolicy()
    decision = policy.decide(
        [blob("CF_HDROP", n=100, category=CAT_FILES)],
        files=[("C:/a.txt", 100), ("C:/b.txt", 200)],
    )
    assert decision.has_files
    assert decision.file_paths == ["C:/a.txt", "C:/b.txt"]
    assert decision.files_total_bytes == 300


def test_files_can_be_disabled_independently():
    policy = SyncPolicy(sync_files=False)
    decision = policy.decide([], files=[("C:/a.txt", 100)])
    assert decision.action == ACTION_SKIP
    assert not decision.has_files
    assert any("文件" in name for name, _r in decision.dropped)


def test_files_alone_still_syncs():
    """只有文件、没有其它格式时，也应该产生一次同步（交给文件通道）。"""
    policy = SyncPolicy()
    decision = policy.decide(
        [blob("CF_HDROP", n=100, category=CAT_FILES)],
        files=[("C:/a.txt", 100)],
    )
    assert decision.syncs
    assert decision.has_files


# --------------------------------------------------------------------- 描述


def test_describe_is_human_readable():
    policy = SyncPolicy(max_payload_mb=1)
    text = policy.decide([blob("CF_DIB", n=2 * 1024 * 1024, category=CAT_IMAGE)]).describe()
    assert "仅通知" in text
    assert "超过上限" in text


def test_format_allowed_reports_reason():
    policy = SyncPolicy(exclude=[r"^Foo$"], forward_all=False)
    allowed, why = policy.format_allowed("Foo", CAT_OTHER)
    assert not allowed and why

    allowed, why = policy.format_allowed("CF_UNICODETEXT", CAT_TEXT)
    assert allowed and why == ""

def test_exclude_patterns_override_replaces_the_configured_exclude():
    """传了 `exclude_patterns` 就**用它**，而不是配置里那一套。

    这是"按复制来源进程切换丢弃列表"的底座：同一个 `Ole Private Data` 在
    WPS 演示上要排掉、在 Word 上要留着，静态配置无论怎么配都只能满足一边。
    """
    from netclip.clipsync.policy import SyncPolicy

    policy = SyncPolicy(exclude=[r"^Ole Private Data$"])
    assert policy.format_allowed("Ole Private Data", "ole")[0] is False

    #: 覆盖成"什么都不排" —— 又允许了
    assert policy.format_allowed("Ole Private Data", "ole", policy.compile_exclude([]))[0] is True
    #: 覆盖成另一套 —— 按新的来
    only_text = policy.compile_exclude([r"^CF_UNICODETEXT$"])
    assert policy.format_allowed("CF_UNICODETEXT", "text", only_text)[0] is False
    assert policy.format_allowed("Ole Private Data", "ole", only_text)[0] is True
    #: 不传 = 老行为，没配这个功能的人完全不受影响
    assert policy.format_allowed("Ole Private Data", "ole")[0] is False
