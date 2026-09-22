"""文件传输：把复制的文件真正搬到对端，并在接收端放回剪贴板。

为什么单独一个模块 + 单独的 TCP 通道
-----------------------------------
剪贴板内容是"小而且必须立刻到"，文件是"大而且可以慢慢来"。共用一条流时，
传 2GB 文件会把鼠标事件堵在后面（head-of-line blocking）。所以文件走独立的
`file` 通道，且**每发一块都让路**给输入通道。

整体流程
--------
发送端::

    ClipboardSync 采集到 CF_HDROP
      -> FilesTransfer.plan()          按阈值决定传不传、算每个文件的 size+mtime
      -> FILE_BEGIN                    清单
      -> FILE_CHUNK x N                逐文件、逐块
      -> FILE_END                      校验和

接收端::

    FILE_BEGIN -> 建暂存目录，对端已有同 size+mtime 的文件就回 FILE_ACK 要求跳过
    FILE_CHUNK -> 落盘
    FILE_END   -> 校验 SHA-256
    FILE_DONE  -> 用 CF_HDROP 把落盘的本地路径放回剪贴板

**关键设计：对端拿到的必须是"本地真实存在"的路径。**
Windows 的 `CF_HDROP` 是引用式的 —— 剪贴板里放的只是路径字符串，粘贴时资源管理器
才去读文件。所以暂存文件不能传完就删，要留够时间（`staging_ttl_min`）。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..protocol import Frame, MsgType

log = logging.getLogger("netclip.files")

#: 大文件算哈希时的采样字节数（首 + 尾各取这么多）。
#: 全量 SHA-256 一个 8GB 文件要几十秒，而"跳过已有文件"这个优化
#: 只需要一个足够强的指纹，不需要密码学强度。
_QUICK_HASH_SAMPLE = 256 * 1024

#: 超过这个大小才按"首尾采样"算快速指纹；小文件直接全量 SHA-256（快且更准）
_QUICK_HASH_THRESHOLD = 8 * 1024 * 1024

#: 等待对端 FILE_ACK 的超时（秒）。
#: 对端回 ACK 只是"跳过重复文件"的优化，等不到也必须继续传 —— 宁可多传，
#: 不能因为对端一个慢响应就把整次传输拖住。
ACK_WAIT_TIMEOUT = 3.0


# --------------------------------------------------------------------- 数据类


@dataclass
class FileItem:
    """传输清单里的一个文件。"""

    name: str  # 相对文件名（不含目录），落盘时用
    size: int
    mtime: int
    quick_hash: str = ""
    #: 发送端的绝对路径（只在发送端有意义，不传输）
    local_path: str = ""
    #: 接收端决定跳过（对端已有同内容）
    skipped: bool = False
    #: 接收端已写入的字节数（用于续传判定）
    received: int = 0

    def to_wire(self) -> Dict[str, Any]:
        return {"n": self.name, "s": self.size, "m": self.mtime, "h": self.quick_hash}

    @staticmethod
    def from_wire(raw: Dict[str, Any]) -> "FileItem":
        return FileItem(
            name=str(raw.get("n", "")),
            size=int(raw.get("s", 0)),
            mtime=int(raw.get("m", 0)),
            quick_hash=str(raw.get("h", "")),
        )


@dataclass
class TransferPlan:
    """一次文件传输的决策结果。"""

    transfer_id: str = ""
    items: List[FileItem] = field(default_factory=list)
    total_bytes: int = 0
    #: 是否要真的传
    send: bool = False
    #: 不传的原因（`send=False` 时有效）
    reason: str = ""
    #: 用户可见的提示级别: info / warn
    level: str = "info"

    @property
    def count(self) -> int:
        return len(self.items)

    def describe(self) -> str:
        if not self.send:
            return "不同步文件（%s）" % (self.reason or "未指定原因")
        return "准备传输 %d 个文件，共 %.2f MB" % (self.count, self.total_bytes / 1024.0 / 1024.0)


# --------------------------------------------------------------------- 快速指纹


def quick_fingerprint(path: str, size: int) -> str:
    """算出用于"这个文件对端是不是已经有了"的指纹。

    对小于 `_QUICK_HASH_THRESHOLD` 的文件做全量 SHA-256；
    更大的文件只取首尾各 256KB 混合，避免为了一个优化把整个文件读一遍。
    """
    hasher = hashlib.sha256()
    hasher.update(b"size=%d;" % size)
    try:
        with open(path, "rb") as fh:
            if size <= _QUICK_HASH_THRESHOLD:
                while True:
                    block = fh.read(1024 * 1024)
                    if not block:
                        break
                    hasher.update(block)
            else:
                head = fh.read(_QUICK_HASH_SAMPLE)
                hasher.update(head)
                fh.seek(max(0, size - _QUICK_HASH_SAMPLE))
                hasher.update(fh.read(_QUICK_HASH_SAMPLE))
    except OSError as exc:
        log.debug("计算指纹失败 %s: %s", path, exc)
        return ""
    return hasher.hexdigest()


def sha256_file(path: str, chunk: int = 1024 * 1024) -> str:
    """完整 SHA-256，用于传输后的完整性校验。"""
    hasher = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            hasher.update(block)
    return hasher.hexdigest()


# --------------------------------------------------------------------- 暂存目录


def default_staging_root() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "netclip" / "staging"


class Staging:
    """接收端的暂存目录管理。

    目录结构::

        <root>/<peer>/<transfer_id>/<文件名>

    按 transfer 分目录的原因：不同次传输的同名文件不会互相覆盖，
    而且一次传输的文件可以整目录清理。
    """

    def __init__(
        self,
        root: Optional[str] = None,
        peer: str = "peer",
        ttl_minutes: int = 120,
        deliver_dir: Optional[str] = None,
    ) -> None:
        self.root = Path(root) if root else default_staging_root()
        self.peer = _safe_segment(peer) or "peer"
        self.ttl_seconds = max(0, int(ttl_minutes)) * 60
        #: 设置之后走"**直接投递**"模式：文件平铺写进这个**用户可见**的目录，
        #: 不参与 TTL 清理（它们是交付物，不是暂存），剪贴板也不会被碰。
        #: 见 `FilesTransfer._recv_done`。
        self.deliver_dir: Optional[Path] = Path(deliver_dir) if deliver_dir else None
        #: transfer_id -> 该次传输落盘的文件路径列表（按索引）
        self._paths: Dict[str, List[str]] = {}
        self._lock = threading.Lock()

    def transfer_dir(self, transfer_id: str) -> Path:
        return self.root / self.peer / _safe_segment(transfer_id)

    def prepare(self, transfer_id: str) -> Path:
        path = self.transfer_dir(transfer_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def path_for(self, transfer_id: str, index: int, name: str) -> str:
        directory = self.deliver_dir if self.deliver_dir is not None else self.prepare(transfer_id)
        directory.mkdir(parents=True, exist_ok=True)
        safe = _safe_filename(name) or ("file-%d" % index)
        target = directory / safe
        # 同名冲突：加序号，绝不覆盖用户已有的文件
        counter = 1
        stem, suffix = target.stem, target.suffix
        while target.exists() and counter < 1000:
            target = directory / ("%s (%d)%s" % (stem, counter, suffix))
            counter += 1
        return str(target)

    def register(self, transfer_id: str, index: int, path: str) -> None:
        with self._lock:
            paths = self._paths.setdefault(transfer_id, [])
            while len(paths) <= index:
                paths.append("")
            paths[index] = path

    def paths_for(self, transfer_id: str) -> List[str]:
        with self._lock:
            return [p for p in self._paths.get(transfer_id, []) if p]

    def forget(self, transfer_id: str) -> None:
        with self._lock:
            self._paths.pop(transfer_id, None)

    # ------------------------------------------------------------ 文件来源

    def is_received(self, path: str) -> bool:
        """这个路径是不是**我们从对端接收下来的**文件（落在暂存区里）。

        用途：挡住"收到文件 -> 放回剪贴板 -> 又被当成一次新复制发回对端"的
        **无限互传**。时序防线（回声抑制）治的是"别把回声发出去"，这一条治的是
        **语义** —— 这份文件本来就是对端的，不该往回传。两道独立防线是值得的：
        互传失控会把磁盘灌满、把网络打满。

        判据刻意用**位置**，而不是"我记不记得收过它"：

          * `_paths` 只活在当前进程里，还会被 TTL 清理掉；而"在暂存区里"
            这个事实跨进程、跨重启都成立；
          * 每次调用都要遍历 `_paths` 里的所有路径，文件一多就是 O(n) 的字符串比较。

        注意 `deliver = "folder"` 模式根本不经过剪贴板（见 `FilesTransfer._recv_done`），
        所以这里只需要管暂存区。
        """
        if not path:
            return False
        try:
            resolved = Path(path).resolve()
            root = self.root.resolve()
        except OSError:  # pragma: no cover - 路径畸形
            return False
        try:
            resolved.relative_to(root)
        except ValueError:
            return False
        return True

    # ------------------------------------------------------------ TTL 清理

    def cleanup_expired(self) -> int:
        """删掉超过 TTL 的暂存传输目录。返回删除的目录数。

        **必须留够时间**：`CF_HDROP` 只是路径引用，用户可能过几分钟才粘贴。
        默认 TTL 120 分钟，配置项 `clipboard.files.staging_ttl_min`。
        """
        if self.ttl_seconds <= 0:
            return 0
        base = self.root / self.peer
        if not base.is_dir():
            return 0
        deadline = time.time() - self.ttl_seconds
        removed = 0
        for entry in base.iterdir():
            if not entry.is_dir():
                continue
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            if mtime >= deadline:
                continue
            try:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
                with self._lock:
                    self._paths.pop(entry.name, None)
            except OSError as exc:  # pragma: no cover
                log.debug("清理暂存目录失败 %s: %s", entry, exc)
        if removed:
            log.info("已清理 %d 个过期暂存目录", removed)
        return removed

    def summary(self) -> str:
        with self._lock:
            transfers = len(self._paths)
            files = sum(len(v) for v in self._paths.values())
        return "暂存: %d 次传输 / %d 个文件 @ %s" % (transfers, files, self.root / self.peer)


# --------------------------------------------------------------------- 传输主体


class FilesTransfer:
    """文件传输的收发两侧。

    它不持有 socket：发帧通过注入的 `send`，让路通过注入的 `yield_fn`，
    这样它可以在没有网络的情况下单独测试。
    """

    def __init__(
        self,
        *,
        send: Callable[[int, Dict, bytes], None],
        staging: Staging,
        chunk_size: int = 1024 * 1024,
        auto_transfer_max_bytes: int = 200 * 1024 * 1024,
        over_threshold_action: str = "ask",
        verify_sha256: bool = True,
        notify: Optional[Callable[[str, str], None]] = None,
        on_files_ready: Optional[Callable[[List[str]], None]] = None,
        yield_fn: Optional[Callable[[], None]] = None,
    ) -> None:
        """
        `on_files_ready(paths)` 在接收端文件全部落盘并校验后被调用，
        用于把 `CF_HDROP` 放回剪贴板（由 ClipboardSync 提供）。
        `yield_fn()` 每发一块文件数据后调用，让输入通道有机会插队 ——
        这是"传大文件时鼠标不卡"的实现点之一。
        """
        self.send = send
        self.staging = staging
        self.chunk_size = max(4096, int(chunk_size))
        self.auto_transfer_max_bytes = max(0, int(auto_transfer_max_bytes))
        self.over_threshold_action = over_threshold_action
        self.verify_sha256 = verify_sha256
        self.notify = notify or (lambda level, text: None)
        self.on_files_ready = on_files_ready
        self.yield_fn = yield_fn or (lambda: None)

        #: 当前待发送的传输（plan() 产生，send_file_begin() 消费）
        self._pending: Optional[TransferPlan] = None
        self._pending_lock = threading.Lock()

        #: FILE_ACK 协商：对端回了哪几个"我已经有"的文件。
        #: 用 asyncio.Event 而不是 Condition，因为发送侧就是协程。
        #: **初始就是 set 状态**：没有正在等 ACK 的时候不该有任何人被阻塞。
        self._ack_event = asyncio.Event()
        self._ack_event.set()
        self._ack_transfer_id = ""
        self._ack_skip: List[int] = []

        #: 接收侧状态: transfer_id -> {...}
        self._incoming: Dict[str, Dict[str, Any]] = {}

        self.stats = {
            "planned": 0,
            "skipped_by_threshold": 0,
            "skipped_by_action": 0,
            "sent_files": 0,
            "sent_bytes": 0,
            "recv_files": 0,
            "recv_bytes": 0,
            "resumed": 0,
            "verified": 0,
            "checksum_failed": 0,
            "failed": 0,
            "clipboard_updated": 0,
            #: 直接投递模式下已交付到接收目录的文件数（不经过剪贴板）
            "delivered": 0,
        }

    # ================================================================ 发送侧

    def plan(self, paths: Sequence[str], total_bytes: int = -1) -> TransferPlan:
        """按阈值决定这些文件要不要传，并生成清单。

        `total_bytes < 0` 表示调用方没算过，这里自己 stat 一遍。
        """
        plan = TransferPlan(transfer_id=uuid.uuid4().hex)
        self.stats["planned"] += 1

        items: List[FileItem] = []
        total = 0
        for raw in paths:
            path = str(raw)
            try:
                stat = os.stat(path)
            except OSError as exc:
                log.warning("跳过无法访问的文件 %s: %s", path, exc)
                continue
            if not os.path.isfile(path):
                # 目录要递归展开；这里先只处理文件，目录同步放到后续版本
                log.info("跳过目录（暂不支持递归同步目录）: %s", path)
                continue
            size = int(stat.st_size)
            item = FileItem(
                name=os.path.basename(path),
                size=size,
                mtime=int(stat.st_mtime),
                local_path=path,
            )
            items.append(item)
            total += size

        if total_bytes >= 0:
            # 调用方给的总大小可能包含目录；以实际可传文件为准，但取较大的那个
            # 用于阈值判断（否则一个大目录会被误判成 0 字节而直接放行）。
            total_for_threshold = max(total, int(total_bytes))
        else:
            total_for_threshold = total

        plan.items = items
        plan.total_bytes = total

        if not items:
            plan.reason = "没有可传输的普通文件"
            return plan

        if total_for_threshold > self.auto_transfer_max_bytes:
            # 阈值可能配得很小（甚至 0），所以用 MB 打印时保留两位小数，
            # 否则 1.63MB 会被显示成"超过阈值 0 MB"，看起来像 bug。
            limit_mb = self.auto_transfer_max_bytes / 1024.0 / 1024.0
            actual_mb = total_for_threshold / 1024.0 / 1024.0
            if self.over_threshold_action == "skip":
                plan.reason = "%.2f MB 超过阈值 %.2f MB，按配置不同步" % (actual_mb, limit_mb)
                self.stats["skipped_by_threshold"] += 1
                self.notify("info", "文件共 %.1f MB，超过同步阈值，已按配置跳过" % actual_mb)
                return plan
            if self.over_threshold_action == "ask":
                # 后台服务无法同步地"问用户"，所以这里的行为是：**通知 + 不传**。
                # 想让它直接传就把 over_threshold_action 改成 "transfer"。
                # 这样默认行为永远是"不会突然占用带宽"，符合"不打扰"的定位。
                plan.reason = (
                    "%.2f MB 超过阈值 %.2f MB，需用户确认（over_threshold_action=ask 时不会自动传）"
                    % (actual_mb, limit_mb)
                )
                self.stats["skipped_by_action"] += 1
                self.notify(
                    "warn",
                    "复制的文件共 %.1f MB，超过阈值 %.1f MB：未自动同步。"
                    "想直接传请把 over_threshold_action 设为 transfer" % (actual_mb, limit_mb),
                )
                return plan
            # "transfer": 照传不误
            log.info("文件 %.2f MB 超过阈值，但配置为 transfer，继续传输", actual_mb)

        plan.send = True
        with self._pending_lock:
            self._pending = plan
        return plan

    def take_pending(self) -> Optional[TransferPlan]:
        with self._pending_lock:
            plan, self._pending = self._pending, None
        return plan

    def send_file_begin(self, plan: TransferPlan) -> None:
        """发出 FILE_BEGIN。

        对端收到后会立刻回 FILE_ACK 声明"哪几个文件我已经有了"（见 `stream_files`
        里的等待逻辑）。对端没回也不会卡住 —— 见 `ACK_WAIT_TIMEOUT`。
        """
        #: 先清掉上一轮可能残留的信号，再发清单，避免把旧的 ACK 当成这一轮的
        self._ack_event.clear()
        self._ack_skip = []

        body = {
            "id": plan.transfer_id,
            "n": plan.count,
            "bytes": plan.total_bytes,
            "items": [item.to_wire() for item in plan.items],
        }
        self.send(MsgType.FILE_BEGIN, body, b"")
        log.info(
            "开始发送 %d 个文件（%.2f MB）",
            plan.count,
            plan.total_bytes / 1024.0 / 1024.0,
        )

    async def _await_ack(self, transfer_id: str, timeout: float = ACK_WAIT_TIMEOUT) -> List[int]:
        """等对端的 FILE_ACK，拿到要跳过的文件索引。

        超时或对端报错就返回空列表（= 全都传），保证传输一定会继续。
        """
        try:
            await asyncio.wait_for(self._ack_event.wait(), timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError):
            log.debug("等待对端 ACK 超时（%s），按全部传输处理", transfer_id[:8])
            return []
        if self._ack_transfer_id != transfer_id:
            log.debug("收到的 ACK 属于另一次传输，忽略")
            return []
        return list(self._ack_skip)

    async def stream_files(self, plan: TransferPlan, skip: Optional[Sequence[int]] = None) -> None:
        """逐个文件、逐块发送。这是一个**协程**，因为它要 await 让路。

        `skip` 是对端要求跳过的文件索引（它已经有同样的文件了）。
        不传时先等对端的 FILE_ACK 自己协商。
        """
        if skip is None:
            skip = await self._await_ack(plan.transfer_id)
        skip_set = set(skip or ())
        for index, item in enumerate(plan.items):
            if index in skip_set:
                log.info("对端已有 %s，跳过", item.name)
                self.stats["resumed"] += 1
                continue

            sender = _FileSender(plan.transfer_id, index, item, self.chunk_size)
            async for frame in sender.frames():
                self.send(frame.type, frame.body, frame.blob)
                self.stats["sent_bytes"] += len(frame.blob)
                # 让路：给输入通道一个插队的机会
                await asyncio.sleep(0)
                self.yield_fn()

            self.stats["sent_files"] += 1
            log.info("已发送 %s（%.2f MB）", item.name, item.size / 1024.0 / 1024.0)

        self.send(MsgType.FILE_DONE, {"id": plan.transfer_id, "n": plan.count}, b"")
        log.info("文件传输完成: %d 个文件", plan.count)

    # ---------------------------------------------------------------- 续传协商

    def handle_ack(self, body: Dict) -> None:
        """接收端的 FILE_ACK：告诉我们它已经有哪几个文件。"""
        transfer_id = str(body.get("id", ""))
        skip = [int(v) for v in body.get("skip", []) if isinstance(v, (int, float))]
        log.info("对端报告已有 %d 个文件（传输 %s）", len(skip), transfer_id[:8])
        self._ack_transfer_id = transfer_id
        self._ack_skip = skip
        self.stats["resumed"] += len(skip)
        self._ack_event.set()

    # ================================================================ 接收侧

    def on_frame(self, mtype: int, body: Dict, blob: bytes) -> None:
        if mtype == MsgType.FILE_BEGIN:
            self._recv_begin(body)
        elif mtype == MsgType.FILE_CHUNK:
            self._recv_chunk(body, blob)
        elif mtype == MsgType.FILE_END:
            self._recv_end(body)
        elif mtype == MsgType.FILE_ACK:
            self.handle_ack(body)
        elif mtype == MsgType.FILE_DONE:
            self._recv_done(body)

    def _recv_begin(self, body: Dict) -> None:
        transfer_id = str(body.get("id", ""))
        raw_items = body.get("items")
        items = [FileItem.from_wire(raw) for raw in raw_items] if isinstance(raw_items, list) else []

        self.staging.prepare(transfer_id)
        skip: List[int] = []
        for index, item in enumerate(items):
            existing = self._find_existing(transfer_id, index, item)
            if existing:
                item.skipped = True
                skip.append(index)

        self._incoming[transfer_id] = {
            "items": items,
            "bytes": int(body.get("bytes", 0)),
            "received": 0,
            "started": time.monotonic(),
            "handles": {},
            "hashers": {},
        }

        self.send(MsgType.FILE_ACK, {"id": transfer_id, "skip": skip}, b"")
        log.info(
            "收到文件传输 %s: %d 个文件 %.2f MB（%d 个已存在将跳过）",
            transfer_id[:8],
            len(items),
            int(body.get("bytes", 0)) / 1024.0 / 1024.0,
            len(skip),
        )

    def _find_existing(self, transfer_id: str, index: int, item: FileItem) -> str:
        """在本机暂存区里找有没有同内容文件；有就链接过来并返回路径（跳过传输）。

        为什么在整个暂存区里找而不是只看当前传输目录：每次传输的 `transfer_id`
        都是新的，如果只看自己那个目录就永远找不到旧文件，续传优化形同虚设。

        **为什么不直接复用旧路径**：暂存目录按 TTL 清理，旧文件可能很快被删。
        所以这里用**硬链接**把它挂到当前传输目录下 —— 不占额外空间、瞬间完成，
        并且它的 mtime 是新的，TTL 从这一刻重新计时，不会"刚放回剪贴板就被清理"。

        只在暂存区里找，绝不去用户目录里猜：猜错会拿别的文件冒充，
        比多传一次严重得多。
        """
        target = self._find_reusable(transfer_id, item)
        if not target:
            return ""

        destination = self.staging.path_for(transfer_id, index, item.name)
        if self._materialize(target, destination):
            self.staging.register(transfer_id, index, destination)
            log.info("暂存区已有 %s，跳过重复传输", item.name)
            return destination
        # 链接失败（跨卷/权限）就退回复用原路径
        self.staging.register(transfer_id, index, target)
        return target

    def _find_reusable(self, transfer_id: str, item: FileItem) -> str:
        """在暂存区里扫出一个 name + size + mtime 都一致的文件。"""
        base = self.staging.root / self.staging.peer
        if not base.is_dir() or not item.name:
            return ""
        safe = _safe_filename(item.name)
        if not safe:
            return ""
        current = self.staging.transfer_dir(transfer_id).resolve()
        try:
            candidates = list(base.glob("*/" + safe))
        except OSError:  # pragma: no cover
            return ""
        for candidate in candidates:
            try:
                if candidate.resolve() == current:
                    continue  # 不看自己这次传输的（可能是个没传完的半截文件）
                if not candidate.is_file():
                    continue
                stat = candidate.stat()
                if int(stat.st_size) == item.size and int(stat.st_mtime) == item.mtime:
                    return str(candidate)
            except OSError:
                continue
        return ""

    @staticmethod
    def _materialize(source: str, destination: str) -> bool:
        """把 source 弄到 destination：优先硬链接，不行就复制。"""
        if os.path.exists(destination):
            return True
        try:
            os.link(source, destination)
            return True
        except OSError:
            pass
        try:
            shutil.copy2(source, destination)
            return True
        except OSError as exc:
            log.debug("无法把暂存文件放到当前传输目录: %s", exc)
            return False

    def _recv_chunk(self, body: Dict, blob: bytes) -> None:
        transfer_id = str(body.get("id", ""))
        index = int(body.get("i", 0))
        offset = int(body.get("o", 0))
        state = self._incoming.get(transfer_id)
        if state is None:
            log.warning("收到未知传输 %s 的数据块，忽略", transfer_id[:8])
            self.stats["failed"] += 1
            return

        items: List[FileItem] = state["items"]
        if not (0 <= index < len(items)):
            log.warning("数据块索引越界: %d", index)
            self.stats["failed"] += 1
            return
        item = items[index]
        if item.skipped:
            return

        handle = None
        try:
            handles: Dict[int, Any] = state["handles"]
            if index not in handles:
                path = self.staging.path_for(transfer_id, index, item.name)
                self.staging.register(transfer_id, index, path)
                handle = open(path, "wb")
                handles[index] = handle
                state.setdefault("hashers", {})[index] = hashlib.sha256()
            else:
                handle = handles[index]
            handle.write(blob)
            state["hashers"][index].update(blob)
            item.received += len(blob)
            state["received"] += len(blob)
            self.stats["recv_bytes"] += len(blob)
        except OSError as exc:
            log.error("写入文件失败（传输 %s 索引 %d）: %s", transfer_id[:8], index, exc)
            self.stats["failed"] += 1

    def _recv_end(self, body: Dict) -> None:
        transfer_id = str(body.get("id", ""))
        index = int(body.get("i", 0))
        digest = str(body.get("sha256", ""))
        state = self._incoming.get(transfer_id)
        if state is None:
            return

        items: List[FileItem] = state["items"]
        if not (0 <= index < len(items)):
            return
        item = items[index]
        if item.skipped:
            # 协商阶段就决定跳过的文件，不会收到它的数据块；这里只是防御
            return

        handle = state.get("handles", {}).pop(index, None)
        if handle is not None:
            try:
                handle.close()
            except OSError:  # pragma: no cover
                pass

        path = self._path_at(transfer_id, index)

        got = ""
        if digest:
            hasher = state.get("hashers", {}).pop(index, None)
            if hasher is not None:
                got = hasher.hexdigest()

        if digest and got and got != digest:
            self.stats["checksum_failed"] += 1
            log.error("文件 %s 校验失败（期望 %s，实际 %s）", item.name, digest[:16], got[:16])
            self.notify("warn", "文件 %s 校验失败，已丢弃" % item.name)
            try:
                if path and os.path.isfile(path):
                    os.remove(path)
                    self.staging.register(transfer_id, index, "")
            except OSError:  # pragma: no cover
                pass
            return

        if digest:
            self.stats["verified"] += 1
        self.stats["recv_files"] += 1
        log.info("已接收 %s（%d 字节）%s", item.name, item.size, "校验通过" if digest else "")

    def _path_at(self, transfer_id: str, index: int) -> str:
        paths = self.staging.paths_for(transfer_id)
        return paths[index] if index < len(paths) else ""

    def _recv_done(self, body: Dict) -> None:
        """全部传完：把落盘的文件路径放回剪贴板。"""
        transfer_id = str(body.get("id", ""))
        state = self._incoming.pop(transfer_id, None)
        if state is None:
            return

        # 关闭可能还开着的句柄（对端异常中断时会有）
        for handle in state.get("handles", {}).values():
            try:
                handle.close()
            except OSError:  # pragma: no cover
                pass

        items: List[FileItem] = state["items"]
        for index, item in enumerate(items):
            if item.received:
                continue  # 真正传过来的，已经注册过路径
            # 协商时判为"跳过"的文件。此刻文件通常已经在暂存区里了，
            # 但仍然走一遍定位逻辑，覆盖几种情况：
            #   * 第一次定位时硬链接失败，只登记了旧路径；
            #   * 文件在传输过程中被 TTL 清理掉了（那就放进不了剪贴板）。
            path = self._find_existing(transfer_id, index, item)
            if not path:
                log.warning("跳过的文件 %s 在暂存区里已经找不到了", item.name)

        paths = [p for p in self.staging.paths_for(transfer_id) if p and os.path.isfile(p)]
        if not paths:
            log.warning("传输 %s 结束后没有任何可用文件", transfer_id[:8])
            return

        total = sum(os.path.getsize(p) for p in paths if os.path.isfile(p))
        where = self.staging.deliver_dir

        if where is not None:
            #: **直接投递**：文件已经在用户可见的目录里了，**完全不碰剪贴板**。
            #:
            #: 为什么默认走这条路：跨机复制文件在剪贴板层面牵扯太多
            #: （`Shell IDList Array` 的 CIDA 结构、拖放簿记格式、定长结构截断），
            #: 真机上两次把资源管理器的粘贴搞崩。而"文件就在这个目录里"这个语义
            #: 根本不需要剪贴板，也就没有任何可崩的地方。
            log.info(
                "已接收 %d 个文件（%.2f MB）-> %s",
                len(paths),
                total / 1024.0 / 1024.0,
                where,
            )
            self.notify("info", "已接收 %d 个文件，存放在 %s" % (len(paths), where))
            self.stats["delivered"] += len(paths)
            return

        log.info("文件已就位: %d 个文件 %.2f MB -> 放入剪贴板", len(paths), total / 1024.0 / 1024.0)
        if self.on_files_ready is not None:
            try:
                self.on_files_ready(paths)
                self.stats["clipboard_updated"] += 1
            except Exception:  # pragma: no cover
                log.exception("把文件放回剪贴板失败")
        else:
            log.warning("没有注册回调，文件已落盘但未放入剪贴板")

    # ---------------------------------------------------------------- 文件来源

    def all_from_peer(self, paths: Sequence[str]) -> bool:
        """这些路径是不是**全都**来自对端（都在暂存区里）。

        由 `clipsync.bridge.publish_local` 在决定"要不要发起一次文件传输"之前问。
        返回 True 就说明这次剪贴板变化不是用户的新动作，而是我们自己刚把收到的
        文件放回剪贴板造成的回声 —— 传回去就会变成两端**无限互传**。

        只要求"全都来自对端"而不是"至少一个"：混着本机文件的情况（例如用户
        同时选了暂存区里一个和本地一个）仍然按用户的新复制处理，宁可多传一次，
        也不要漏掉用户的真实意图。
        """
        if not paths:
            return False
        return all(self.staging.is_received(path) for path in paths)

    # ---------------------------------------------------------------- 诊断

    def status(self) -> str:
        active = {k: v for k, v in self.stats.items() if v}
        text = " ".join("%s=%d" % (k, v) for k, v in active.items()) or "空闲"
        return "%s | %s" % (text, self.staging.summary())

    def cleanup(self) -> int:
        return self.staging.cleanup_expired()


class _FileSender:
    """把单个文件拆成 FILE_CHUNK / FILE_END 帧序列。"""

    def __init__(self, transfer_id: str, index: int, item: FileItem, chunk_size: int) -> None:
        self.transfer_id = transfer_id
        self.index = index
        self.item = item
        self.chunk_size = chunk_size

    async def frames(self):
        hasher = hashlib.sha256()
        offset = 0
        with open(self.item.local_path, "rb") as fh:
            while True:
                block = fh.read(self.chunk_size)
                if not block:
                    break
                hasher.update(block)
                yield Frame(
                    MsgType.FILE_CHUNK,
                    {"id": self.transfer_id, "i": self.index, "o": offset},
                    block,
                )
                offset += len(block)
        yield Frame(
            MsgType.FILE_END,
            {"id": self.transfer_id, "i": self.index, "sha256": hasher.hexdigest(), "bytes": offset},
            b"",
        )


# --------------------------------------------------------------------- 路径安全


def _safe_segment(text: str) -> str:
    """把一段文本变成安全的单层目录名。"""
    cleaned = "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in str(text))
    return cleaned.strip("._")[:64]


def _safe_filename(name: str) -> str:
    """把文件名清洗成本地可用的文件名。

    对端发来的名字**不可信**：可能带路径分隔符、`..`、保留设备名（CON/PRN/NUL…）、
    非法字符。不清洗的话就是经典的路径穿越漏洞（往系统目录写文件）。
    """
    if not name:
        return ""
    # 只取最后一段，扔掉任何目录成分（包括 ..\..\ 这种）
    base = str(name).replace("\\", "/").split("/")[-1]
    base = "".join(ch for ch in base if ch not in '<>:"|?*' and ord(ch) >= 32)
    base = base.strip(" .")
    if not base:
        return ""
    if base.upper().split(".")[0] in _RESERVED_NAMES:
        base = "_" + base
    return base[:180]


_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {"COM%d" % i for i in range(1, 10)}
    | {"LPT%d" % i for i in range(1, 10)}
)


__all__ = [
    "FileItem",
    "FilesTransfer",
    "Staging",
    "TransferPlan",
    "default_staging_root",
    "quick_fingerprint",
    "sha256_file",
]
