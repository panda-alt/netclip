"""文件传输的单元测试。

覆盖：路径安全、阈值策略、分块切分、端到端收发、续传跳过、SHA-256 校验、
暂存目录 TTL 清理、CF_HDROP 往返。

端到端部分用**内存链路**把发送端和接收端接起来（`FakeLink`），
所以不需要网络、也不需要第二台机器。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import struct
import sys
import tempfile
import time

import pytest

from netclip.files.transfer import (
    FileItem,
    FilesTransfer,
    Staging,
    TransferPlan,
    _safe_filename,
    _safe_segment,
    quick_fingerprint,
    sha256_file,
)
from netclip.protocol import MsgType
from netclip.win import clipboard as cb

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="CF_HDROP 相关断言需要 Windows")


# --------------------------------------------------------------------- 路径安全


def test_safe_filename_strips_directories():
    """对端发来的名字不可信：不能带目录成分，否则就是路径穿越。"""
    assert _safe_filename("..\\..\\windows\\system32\\evil.dll") == "evil.dll"
    assert _safe_filename("../../etc/passwd") == "passwd"
    assert _safe_filename("C:\\abs\\path\\file.txt") == "file.txt"
    assert _safe_filename("/etc/shadow") == "shadow"


def test_safe_filename_removes_illegal_chars():
    assert _safe_filename('a<b>c:d"e|f?g*h.txt') == "abcdefgh.txt"
    assert _safe_filename("a\x00b\x1fc.txt") == "abc.txt"


def test_safe_filename_rejects_reserved_device_names():
    """CON/PRN/NUL/COM1 这些是 Windows 保留设备名，直接创建会出错或指向设备。"""
    assert _safe_filename("CON") == "_CON"
    assert _safe_filename("nul.txt") == "_nul.txt"
    assert _safe_filename("COM1") == "_COM1"
    assert _safe_filename("LPT9.log") == "_LPT9.log"


def test_safe_filename_handles_empty_and_dots():
    assert _safe_filename("") == ""
    assert _safe_filename("...") == ""
    assert _safe_filename("   ") == ""


def test_safe_filename_truncates_long_names():
    assert len(_safe_filename("x" * 500)) <= 180


def test_safe_segment_for_directory_names():
    assert _safe_segment("desktop-a") == "desktop-a"
    assert _safe_segment("a/b\\c") == "a_b_c"
    assert _safe_segment("") == ""
    assert len(_safe_segment("y" * 200)) <= 64


# --------------------------------------------------------------------- 指纹


def test_quick_fingerprint_is_stable_and_size_bound():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "a.bin")
        with open(path, "wb") as fh:
            fh.write(b"hello world" * 100)
        size = os.path.getsize(path)
        first = quick_fingerprint(path, size)
        assert first and len(first) == 64
        assert quick_fingerprint(path, size) == first
        # 大小不同 -> 指纹必须不同（避免误判"已有"而跳过传输）
        assert quick_fingerprint(path, size + 1) != first


def test_quick_fingerprint_detects_content_change():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "a.bin")
        with open(path, "wb") as fh:
            fh.write(b"A" * 1024)
        before = quick_fingerprint(path, 1024)
        with open(path, "wb") as fh:
            fh.write(b"B" * 1024)
        assert quick_fingerprint(path, 1024) != before


def test_quick_fingerprint_missing_file_is_empty():
    assert quick_fingerprint("C:/definitely/not/here.bin", 10) == ""


# --------------------------------------------------------------------- 阈值策略


def _make_files(tmp, count=2, size=1024):
    paths = []
    for idx in range(count):
        path = os.path.join(tmp, "f%d.bin" % idx)
        with open(path, "wb") as fh:
            fh.write(b"x" * size)
        paths.append(path)
    return paths


def _transfer(staging, **kwargs):
    params = dict(
        send=lambda *a, **k: None,
        staging=staging,
        chunk_size=4096,
        auto_transfer_max_bytes=200 * 1024 * 1024,
        over_threshold_action="ask",
    )
    params.update(kwargs)
    return FilesTransfer(**params)


def test_plan_sends_small_files():
    with tempfile.TemporaryDirectory() as tmp:
        staging = Staging(root=os.path.join(tmp, "stage"), peer="p")
        paths = _make_files(tmp)
        plan = _transfer(staging).plan(paths)
        assert plan.send
        assert plan.count == 2
        assert plan.total_bytes == 2048
        assert all(i.name.startswith("f") for i in plan.items)


def test_plan_skips_over_threshold_with_skip_action():
    with tempfile.TemporaryDirectory() as tmp:
        staging = Staging(root=os.path.join(tmp, "stage"), peer="p")
        paths = _make_files(tmp, size=4096)
        plan = _transfer(staging, auto_transfer_max_bytes=1024, over_threshold_action="skip").plan(paths)
        assert not plan.send
        assert "超过阈值" in plan.reason


def test_plan_asks_but_does_not_send_by_default():
    """`ask` 的默认行为是"通知 + 不传"。

    后台服务没法同步地问用户，所以宁可默认不传，也不要偷偷占满带宽。
    想直接传就把 over_threshold_action 设成 transfer。
    """
    with tempfile.TemporaryDirectory() as tmp:
        staging = Staging(root=os.path.join(tmp, "stage"), peer="p")
        paths = _make_files(tmp, size=4096)
        notes = []
        plan = _transfer(
            staging,
            auto_transfer_max_bytes=1024,
            over_threshold_action="ask",
            notify=lambda level, text: notes.append((level, text)),
        ).plan(paths)
        assert not plan.send
        assert notes and notes[0][0] == "warn"
        assert "transfer" in notes[0][1]


def test_plan_transfer_action_sends_regardless():
    with tempfile.TemporaryDirectory() as tmp:
        staging = Staging(root=os.path.join(tmp, "stage"), peer="p")
        paths = _make_files(tmp, size=4096)
        plan = _transfer(staging, auto_transfer_max_bytes=1024, over_threshold_action="transfer").plan(paths)
        assert plan.send


def test_plan_ignores_directories():
    with tempfile.TemporaryDirectory() as tmp:
        staging = Staging(root=os.path.join(tmp, "stage"), peer="p")
        sub = os.path.join(tmp, "sub")
        os.makedirs(sub)
        with open(os.path.join(sub, "inner.txt"), "w") as fh:
            fh.write("x")
        plan = _transfer(staging).plan([sub])
        assert not plan.send
        assert "没有可传输的普通文件" in plan.reason


def test_plan_uses_caller_total_for_threshold():
    """调用方给的总大小可能包含目录；阈值判断要用较大的那个，否则大目录会被误放行。"""
    with tempfile.TemporaryDirectory() as tmp:
        staging = Staging(root=os.path.join(tmp, "stage"), peer="p")
        paths = _make_files(tmp, count=1, size=100)
        plan = _transfer(staging, auto_transfer_max_bytes=1024, over_threshold_action="skip").plan(
            paths, total_bytes=10 * 1024 * 1024
        )
        assert not plan.send


def test_plan_ignores_missing_files():
    with tempfile.TemporaryDirectory() as tmp:
        staging = Staging(root=os.path.join(tmp, "stage"), peer="p")
        plan = _transfer(staging).plan([os.path.join(tmp, "nope.bin")])
        assert not plan.send


# --------------------------------------------------------------------- 分块


def test_file_sender_chunks_and_ends_with_hash():
    from netclip.files.transfer import _FileSender

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "a.bin")
        data = bytes(range(256)) * 40  # 10240 字节
        with open(path, "wb") as fh:
            fh.write(data)

        item = FileItem(name="a.bin", size=len(data), mtime=1, local_path=path)
        sender = _FileSender("tid", 0, item, chunk_size=4096)

        async def collect():
            out = []
            async for frame in sender.frames():
                out.append(frame)
            return out

        frames = asyncio.run(collect())
        chunks = [f for f in frames if f.type == MsgType.FILE_CHUNK]
        ends = [f for f in frames if f.type == MsgType.FILE_END]

        assert len(chunks) == 3  # 4096 + 4096 + 2048
        assert b"".join(f.blob for f in chunks) == data
        assert [f.body["o"] for f in chunks] == [0, 4096, 8192]
        assert len(ends) == 1
        assert ends[0].body["sha256"] == hashlib.sha256(data).hexdigest()
        assert ends[0].body["bytes"] == len(data)


# --------------------------------------------------------------------- 端到端


class FakeLink:
    """把两个 FilesTransfer 用内存链路接起来（双向）。

    之所以要双向：接收端在 FILE_BEGIN 之后会回 **FILE_ACK** 告知"哪几个文件
    我已经有了"，发送端据此跳过重复传输。如果测试里丢掉这个回包，
    续传逻辑就永远测不到。

    用 `handler` 这个可变槽位而不是直接绑定方法，是为了让测试能**替换**
    发送端的处理（例如篡改校验和）。`FilesTransfer` 在构造时就捕获了 `send`
    回调，所以事后改 `link.send_from_sender` 是没用的，必须走这层间接。
    """

    def __init__(self) -> None:
        self.sender: FilesTransfer | None = None
        self.receiver: FilesTransfer | None = None
        self.sender_frames = 0
        self.receiver_frames = 0
        #: 发送端 -> 接收端 的实际处理逻辑（可被测试替换）
        self.handler: Any = self._pass_to_receiver

    def _pass_to_receiver(self, mtype: int, body: dict, blob: bytes) -> None:
        if self.receiver is not None:
            self.receiver.on_frame(mtype, body, blob)

    def send_from_sender(self, mtype: int, body: dict, blob: bytes) -> None:
        self.sender_frames += 1
        self.handler(mtype, body, blob)

    def send_from_receiver(self, mtype: int, body: dict, blob: bytes) -> None:
        self.receiver_frames += 1
        if self.sender is not None:
            self.sender.on_frame(mtype, body, blob)


def _make_pair(tmp, chunk_size=8192, share_staging=False):
    """造一对收发端。

    `share_staging=False`（默认）时给接收端一个**独一无二**的 peer 名，
    也就是一套独立的暂存目录。这很重要：接收端的续传优化会在暂存区里按
    "名字 + 大小 + mtime"找已有文件；如果多个测试共用一个 peer 目录，
    先跑的测试留下的文件会让后跑的测试在协商阶段就被整个跳过 ——
    于是"测了个寂寞"，还查不出原因。

    `share_staging=True` 时 peer 名固定，专门用于测续传。
    """
    link = FakeLink()
    sender_staging = Staging(root=os.path.join(tmp, "sender-stage"), peer="s")
    peer = "shared" if share_staging else ("r-" + os.urandom(4).hex())
    recv_staging = Staging(root=os.path.join(tmp, "recv-stage"), peer=peer)
    ready: list = []

    sender = FilesTransfer(
        send=link.send_from_sender,
        staging=sender_staging,
        chunk_size=chunk_size,
        auto_transfer_max_bytes=10 * 1024 * 1024,
        over_threshold_action="transfer",
    )
    receiver = FilesTransfer(
        send=link.send_from_receiver,
        staging=recv_staging,
        chunk_size=chunk_size,
        auto_transfer_max_bytes=10 * 1024 * 1024,
        over_threshold_action="transfer",
        on_files_ready=lambda paths: ready.append(paths),
    )
    link.sender = sender
    link.receiver = receiver
    return link, sender, receiver, recv_staging, ready


def test_end_to_end_file_transfer():
    """完整跑一遍：清单 -> 分块 -> 落盘 -> 校验 -> 回调放入剪贴板。"""
    with tempfile.TemporaryDirectory() as tmp:
        _link, sender, _receiver, _staging, ready = _make_pair(tmp)
        src = os.path.join(tmp, "hello.txt")
        payload = "文件同步测试内容 ✓".encode("utf-8") * 500
        with open(src, "wb") as fh:
            fh.write(payload)

        plan = sender.plan([src])
        assert plan.send

        sender.send_file_begin(plan)
        asyncio.run(sender.stream_files(plan))

        assert len(ready) == 1, "接收端应该回调一次并把文件放回剪贴板"
        paths = ready[0]
        assert len(paths) == 1
        assert os.path.isfile(paths[0])
        with open(paths[0], "rb") as fh:
            assert fh.read() == payload
        assert os.path.basename(paths[0]) == "hello.txt"
        assert sender.stats["sent_files"] == 1
        assert sender.stats["sent_bytes"] == len(payload)
        #: 校验发生在接收侧，所以 verified 记在接收端
        assert _receiver.stats["verified"] == 1
        assert _receiver.stats["recv_files"] == 1
        assert _receiver.stats["clipboard_updated"] == 1


def test_end_to_end_multiple_files_and_chunks():
    with tempfile.TemporaryDirectory() as tmp:
        _link, sender, _receiver, _staging, ready = _make_pair(tmp, chunk_size=4096)

        sources = []
        for idx, size in enumerate((1, 4096, 4097, 100_000)):
            path = os.path.join(tmp, "f%d.bin" % idx)
            with open(path, "wb") as fh:
                fh.write(os.urandom(size))
            sources.append((path, size))

        plan = sender.plan([p for p, _ in sources])
        assert plan.count == 4
        sender.send_file_begin(plan)
        asyncio.run(sender.stream_files(plan))

        assert len(ready) == 1
        assert len(ready[0]) == 4
        for (src, size), dst in zip(sources, ready[0]):
            assert os.path.getsize(dst) == size
            with open(src, "rb") as a, open(dst, "rb") as b:
                assert a.read() == b.read()


def test_end_to_end_resume_skips_existing_files():
    """第二次传同样的文件时，接收端应该识别出"已经有了"并要求跳过。

    用 `share_staging=True` 让两次传输共用同一个暂存区 —— 这正是续传优化的
    作用范围（跨传输、同一对端）。
    """
    with tempfile.TemporaryDirectory() as tmp:
        _link, sender, _receiver, _staging, ready = _make_pair(tmp, share_staging=True)

        src = os.path.join(tmp, "same.bin")
        with open(src, "wb") as fh:
            fh.write(b"Z" * 20000)

        # 第一次
        plan1 = sender.plan([src])
        sender.send_file_begin(plan1)
        asyncio.run(sender.stream_files(plan1))
        assert sender.stats["sent_files"] == 1
        assert len(ready) == 1

        # 第二次：接收端的暂存目录里已经有同名同 size+mtime 的文件
        sent_before = sender.stats["sent_files"]
        plan2 = sender.plan([src])
        sender.send_file_begin(plan2)
        asyncio.run(sender.stream_files(plan2))
        assert sender.stats["sent_files"] == sent_before, "不应该重复传已经在的文件"
        assert sender.stats["resumed"] >= 1, "应该收到对端的跳过清单"
        assert len(ready) == 2, "仍然要把路径放回剪贴板（内容没变）"


def test_end_to_end_checksum_failure_is_rejected():
    """校验失败的文件必须被丢弃，不能留在暂存目录里让用户粘贴到坏文件。"""
    with tempfile.TemporaryDirectory() as tmp:
        link, sender, receiver, staging, ready = _make_pair(tmp)

        # 用独一无二的文件名：接收端默认的暂存根目录在两次测试之间是共用的，
        # 同名文件会被判为"已有"从而跳过传输 —— 那样就测不到校验路径了。
        unique = "corrupt-%s.bin" % os.urandom(6).hex()
        src = os.path.join(tmp, unique)
        with open(src, "wb") as fh:
            fh.write(b"real content" * 100)

        # 拦掉 FILE_END 并篡改校验和
        inner = link.handler
        tampered = {"done": False}

        def evil_send(mtype, body, blob):
            if mtype == MsgType.FILE_END and not tampered["done"]:
                tampered["done"] = True
                body = dict(body)
                body["sha256"] = "0" * 64
            inner(mtype, body, blob)

        link.handler = evil_send

        plan = sender.plan([src])
        sender.send_file_begin(plan)
        asyncio.run(sender.stream_files(plan))

        assert tampered["done"], "这次传输必须真的走到了 FILE_END（否则说明被误判为已存在）"
        assert receiver.stats["checksum_failed"] == 1
        assert receiver.stats["recv_files"] == 0
        # 坏文件被删掉 -> 没有任何路径可放入剪贴板
        assert not ready
        assert not staging.paths_for(plan.transfer_id)


def test_chunk_for_unknown_transfer_is_ignored():
    """收到未知传输的数据块不能崩，只记一次失败。"""
    with tempfile.TemporaryDirectory() as tmp:
        _link, _sender, receiver, _staging, ready = _make_pair(tmp)
        receiver.on_frame(MsgType.FILE_CHUNK, {"id": "nope", "i": 0, "o": 0}, b"junk")
        assert receiver.stats["failed"] == 1
        assert not ready


# --------------------------------------------------------------------- 暂存 TTL


def test_staging_knows_which_files_came_from_the_peer():
    """`Staging.is_received()` 必须按**位置**判断文件来源。

    用途是挡住"收到文件 -> 放回剪贴板 -> 又被当成一次新复制发回对端"的**无限互传**。
    判据刻意用位置而不是"我记得收过它"：`_paths` 只活在当前进程、还会被 TTL 清理，
    而"在暂存区里"这个事实跨进程、跨重启都成立。
    """
    import tempfile
    from pathlib import Path

    staging = Staging(root=tempfile.mkdtemp(prefix="netclip_staging_"), peer="desktop-master")

    received = Path(staging.path_for("abc123", 0, "a.csv"))
    received.parent.mkdir(parents=True, exist_ok=True)
    received.write_bytes(b"x")

    assert staging.is_received(str(received)), "暂存区里的文件应该被认出来"
    # 大小写不敏感（Windows 路径）也要认出来
    assert staging.is_received(str(received).upper())

    assert not staging.is_received(r"C:\Users\someone\Documents\my-own-file.txt")
    assert not staging.is_received("")
    # 清理掉之后"来源"这个事实依然成立 —— 判据是位置，不是存在性
    received.unlink()
    assert staging.is_received(str(received))
def test_cleanup_removes_expired_only():
    with tempfile.TemporaryDirectory() as tmp:
        staging = Staging(root=tmp, peer="p", ttl_minutes=10)
        fresh = staging.prepare("fresh")
        old = staging.prepare("old")
        (fresh / "a.txt").write_text("new")
        (old / "b.txt").write_text("old")

        # 把 old 的 mtime 拨回到 1 小时前
        past = time.time() - 3600
        os.utime(old, (past, past))

        removed = staging.cleanup_expired()
        assert removed == 1
        assert fresh.is_dir() and (fresh / "a.txt").is_file()
        assert not old.exists()


def test_staging_cleanup_disabled_with_zero_ttl():
    with tempfile.TemporaryDirectory() as tmp:
        staging = Staging(root=tmp, peer="p", ttl_minutes=0)
        old = staging.prepare("old")
        past = time.time() - 3600
        os.utime(old, (past, past))
        assert staging.cleanup_expired() == 0
        assert old.exists()


def test_staging_path_for_avoids_overwriting():
    with tempfile.TemporaryDirectory() as tmp:
        staging = Staging(root=tmp, peer="p", ttl_minutes=60)
        first = staging.path_for("t1", 0, "a.txt")
        with open(first, "wb") as fh:
            fh.write(b"1")
        second = staging.path_for("t1", 1, "a.txt")
        assert first != second
        assert os.path.basename(second).startswith("a")
        assert second.endswith(".txt")


def test_staging_isolates_transfers():
    with tempfile.TemporaryDirectory() as tmp:
        staging = Staging(root=tmp, peer="p", ttl_minutes=60)
        a = staging.path_for("t1", 0, "a.txt")
        b = staging.path_for("t2", 0, "a.txt")
        assert os.path.dirname(a) != os.path.dirname(b)


# --------------------------------------------------------------------- CF_HDROP


def test_build_hdrop_roundtrip_with_unicode():
    paths = [r"C:\tmp\普通 文件.txt", r"C:\tmp\has space.bin"]
    data = cb.build_hdrop(paths)
    assert cb.parse_hdrop_paths(data) == paths


def test_build_hdrop_ansi_variant():
    paths = [r"C:\tmp\a.txt"]
    assert cb.parse_hdrop_paths(cb.build_hdrop(paths, wide=False)) == paths


def test_build_hdrop_rejects_empty():
    with pytest.raises(ValueError):
        cb.build_hdrop([])


def test_hdrop_clipboard_roundtrip_real():
    """把落盘文件的真实路径写进剪贴板，回读后能被解析出来。

    这是"接收端把文件变成可粘贴内容"的最后一步，必须真的对。
    """
    from tests.test_win_clipboard import _snapshot_then_restore

    def body():
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for name in ("one.txt", "两 个.txt"):
                p = os.path.join(tmp, name)
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write("data")
                paths.append(p)
            cb.write_formats([cb.make_hdrop_blob(paths)])
            snap = cb.capture(max_per_format=1024 * 1024)
            item = snap.by_name("CF_HDROP")
            assert item is not None
            assert cb.parse_hdrop_paths(item.data) == paths

    _snapshot_then_restore(body)


# --------------------------------------------------------------------- 统计


def test_status_is_readable():
    with tempfile.TemporaryDirectory() as tmp:
        staging = Staging(root=tmp, peer="p")
        transfer = _transfer(staging)
        assert "暂存" in transfer.status()
        plan = transfer.plan(_make_files(tmp))
        assert plan.send
        transfer.take_pending()  # 消费掉，确认不会重复
        assert transfer.take_pending() is None


# --------------------------------------------------------------- 直接投递模式


def test_folder_delivery_writes_flat_into_the_receive_dir():
    """**回归测试**：直接投递模式下文件平铺写进交付目录，不进暂存子目录。

    这是默认的交付方式：收到的文件直接摆在用户能找到的目录里（默认
    `下载\\netclip`），**完全不经过剪贴板**。真机上"放回剪贴板"那条路两次把
    资源管理器的粘贴搞崩，而"文件就在这个目录里"本身根本不需要剪贴板。
    """
    with tempfile.TemporaryDirectory() as tmp:
        receive = os.path.join(tmp, "received")
        staging = Staging(root=os.path.join(tmp, "stage"), peer="p", deliver_dir=receive)
        path = staging.path_for("tid1", 0, "a.txt")
        assert os.path.dirname(path) == receive, "应当平铺在交付目录，而不是 tid1 子目录里"
        with open(path, "wb") as fh:
            fh.write(b"hi")
        #: 同名不覆盖：第二次应当自动改名
        again = staging.path_for("tid2", 0, "a.txt")
        assert again != path and " (1)" in again


def test_folder_delivery_never_touches_the_clipboard():
    """**回归测试**：直接投递模式下绝不调用 `on_files_ready`（也就是不碰剪贴板）。

    剪贴板那条路要同时凑齐 `CF_HDROP` + `Preferred DropEffect=COPY`，还要当心
    拖放簿记格式和定长结构被截断 —— 任何一处不对，资源管理器就转沙漏空等甚至崩溃。
    直接投递把这一整类问题**从根上消掉**。
    """
    with tempfile.TemporaryDirectory() as tmp:
        receive = os.path.join(tmp, "received")
        staging = Staging(root=os.path.join(tmp, "stage"), peer="p", deliver_dir=receive)
        touched = []

        def _on_ready(paths):
            touched.append(list(paths))

        transfer = _transfer(staging, on_files_ready=_on_ready)

        path = staging.path_for("tid1", 0, "a.txt")
        with open(path, "wb") as fh:
            fh.write(b"hi")
        staging.register("tid1", 0, path)
        transfer._incoming["tid1"] = {
            "items": [FileItem(name="a.txt", size=2, mtime=0.0)],
            "handles": {},
            "hashers": {},
            "received": 2,
        }

        transfer._recv_done({"id": "tid1"})

        assert touched == [], "直接投递模式下绝不能去写剪贴板"
        assert os.path.isfile(path), "文件应当已经落在交付目录里"
        assert transfer.stats["delivered"] == 1


def test_clipboard_delivery_still_uses_the_staging_dir():
    """没设 `deliver_dir` 时保持老行为：落进暂存子目录并交给剪贴板回调。"""
    with tempfile.TemporaryDirectory() as tmp:
        staging = Staging(root=os.path.join(tmp, "stage"), peer="p")
        assert staging.deliver_dir is None
        path = staging.path_for("tid1", 0, "a.txt")
        assert os.path.join("stage", "p", "tid1") in path


# --------------------------------------------------------------------- 文件来源


def test_staging_knows_which_files_came_from_the_peer():
    """`Staging.is_received()` 必须按**位置**判断文件来源。

    用途是挡住"收到文件 -> 放回剪贴板 -> 又被当成一次新复制发回对端"的
    **无限互传**。判据刻意用位置而不是"我记得收过它"：`_paths` 只活在当前进程、
    还会被 TTL 清理掉，而"在暂存区里"这个事实跨进程、跨重启都成立。
    """
    with tempfile.TemporaryDirectory() as tmp:
        staging = Staging(root=os.path.join(tmp, "stage"), peer="desktop-master")
        received = staging.path_for("abc123", 0, "a.csv")
        os.makedirs(os.path.dirname(received), exist_ok=True)
        with open(received, "wb") as handle:
            handle.write(b"x")

        assert staging.is_received(received), "暂存区里的文件应该被认出来"
        assert staging.is_received(received.upper()), "Windows 路径大小写不敏感"
        assert not staging.is_received(os.path.join(tmp, "my-own-file.txt"))
        assert not staging.is_received("")

        # 文件被 TTL 清掉之后，"来源"这个事实依然成立 —— 判据是位置，不是存在性
        os.remove(received)
        assert staging.is_received(received)


def test_all_from_peer_requires_every_path():
    """`all_from_peer` 要求**全部**路径都来自对端。

    混着本机文件的情况（用户同时选了暂存区一个、本地一个）仍然按用户的新复制
    处理 —— 宁可多传一次，也不要漏掉用户的真实意图。
    """
    with tempfile.TemporaryDirectory() as tmp:
        staging = Staging(root=os.path.join(tmp, "stage"), peer="p")
        from_peer = staging.path_for("tid", 0, "a.txt")
        os.makedirs(os.path.dirname(from_peer), exist_ok=True)
        with open(from_peer, "wb") as handle:
            handle.write(b"x")
        local = os.path.join(tmp, "mine.txt")
        with open(local, "wb") as handle:
            handle.write(b"y")

        transfer = FilesTransfer(send=lambda *a: None, staging=staging)
        assert transfer.all_from_peer([from_peer]) is True
        assert transfer.all_from_peer([from_peer, local]) is False
        assert transfer.all_from_peer([local]) is False
        assert transfer.all_from_peer([]) is False

