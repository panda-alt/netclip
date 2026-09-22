"""剪贴板同步：把本机剪贴板变化发到对端，并把对端的变化还原到本机。

数据流
------
本机复制::

    WM_CLIPBOARDUPDATE
      -> ClipboardWatcher 去抖
      -> clipboard.capture()          采集全部格式（含私有格式）
      -> SyncPolicy.decide()          决定传什么 / 只通知 / 不同步
      -> _send_*()                    编码 + zlib 压缩 + 分块
      -> clip 通道（<1MB 一帧发完）

对端粘贴::

    CLIP_ANNOUNCE / CLIP_BEGIN / CLIP_CHUNK / CLIP_END
      -> _recv_*()                    收齐、解压、还原为 FormatBlob
      -> clipboard.order_for_paste()  排好顺序（CF_EMBEDDEDOBJECT 必须最后）
      -> clipboard.write_formats()    写入
      -> expect_sequence()            抑制这次写入触发的回声通知

**回声抑制是这里最容易出错的地方**。我们自己写剪贴板也会触发
`WM_CLIPBOARDUPDATE`，如果不抑制，A→B→A→B 会无限乒乓。做法是写入后用
`write_formats()` 返回的序号去精确抑制（见 `win/msgwin.py: expect_sequence`），
而不是"写入后 N 秒内全部忽略"——后者会把用户在这段时间里的真实复制也吞掉。

文件
----
`CF_HDROP` 的路径会交给文件通道（`files/transfer.py`）真正传输，
剪贴板这边只负责"让对方知道有文件来了"。这部分在 P5 接通。
"""

from __future__ import annotations

import logging
import os
import re
import struct
import threading
import time
import uuid
import zlib
from typing import Any, Callable, Dict, List, Optional

from ..protocol import MAX_FRAME_BYTES, Frame, MsgType
from ..win import clipboard as cb
from ..win import winapi as w
from ..win.msgwin import SUPPRESS_WINDOW_SEC, ClipboardWatcher
from .policy import (
    ACTION_ANNOUNCE,
    ACTION_FULL,
    ACTION_SKIP,
    CAT_FILES,
    SyncDecision,
    SyncPolicy,
)

log = logging.getLogger("netclip.clipsync")

#: 单帧上限留出的余量：帧头 + JSON 头 + 压缩后的数据要一起塞进一帧。
_FRAME_PAYLOAD_BUDGET = MAX_FRAME_BYTES - 8 * 1024

#: 写"文件剪贴板"时，抑制窗口要开多大。
#:
#: 必须**覆盖整个 PowerShell 子进程的运行时间**（冷启动 0.5~1.5 秒，慢机器更久），
#: 因为抑制是在消息窗口处理 `WM_CLIPBOARDUPDATE` 的那一刻判定的 —— 通知可能在
#: 我们还没写完的时候就到了。写完立刻用 `expect_sequence()` 收窄回默认窗口，
#: 所以这个"大窗口"只覆盖写入过程本身，不会长期吞掉用户的真实复制。
_FILE_WRITE_SUPPRESS_SEC = SUPPRESS_WINDOW_SEC + 10.0

#: 同内容的多种等价表示，交由 `win.clipboard.capture(dedupe_groups=True)` 处理。
#:
#: 为什么不在这一层做：这里的 filter 只能看到"格式名字"，看不到"能不能读出来"。
#: 于是会出现这种情况 —— Windows 报告 CF_DIBV5 存在但句柄为空（写入方没真提供），
#: 我们按优先级把 CF_DIB 跳过了，结果**整张图都没同步**。
#: 正确做法是"按优先级依次尝试读取，只保留第一个真正读出来的"，那需要拿到数据，
#: 所以逻辑放在 `clipboard.capture()` 里。


def writable_formats(items: List[Any]) -> List[Any]:
    """从对端发来的格式里挑出**本机可以安全写入**的那些。

    丢掉所有"内容里固化了文件绝对路径"的格式（见 `clipboard.PATH_BEARING_FORMATS`）：
    它们的路径指向**对端**的文件系统，写进本机剪贴板只会误导粘贴方。

    真机上踩到的就是这个（用户报"复制文件到主机粘贴，提示在 temp 文件夹中找不到文件"）：
    从机复制了一个文件，剪贴板里同时带上了 `Shell IDList Array`（内部是对端的绝对 PIDL）
    和 `CF_HDROP`。Explorer 粘贴时按保真度挑格式，**`Shell IDList Array` 的优先级
    高于 `CF_HDROP`**，于是它照着对端路径去找文件 —— 那个文件夹正好叫 Temp。
    文件其实已经传过来、就在暂存区里，只是没人去看 `CF_HDROP`。

    文件类内容必须走文件通道、并由接收端用**本地**路径重建 `CF_HDROP`，
    所以这些格式一律不进剪贴板。

    拖放**簿记**格式（`AsyncFlag` / `DropDescription` / `DataObjectAttributes`…）
    同样挡掉：它们只对"源和目标在同一次拖放会话里"有意义。真机上：
    `AsyncFlag` 被截断后按 DWORD 读，Shell 以为这是异步传递，**转沙漏空等**；
    `DropDescription` 是定长结构，截断后按完整长度读会**越界崩溃**。
    接收端落地时自己写一份干净的 `Preferred DropEffect=COPY`。

    OLE「虚拟文件」格式（`FileGroupDescriptorW` 等）也挡掉：它们只**描述**文件，
    真正的字节要在粘贴时由**源数据对象**现场渲染，而对端根本没有那个对象。
    真机证据：主机 `--dump-formats` 里，失败的那次比成功的那次**只多一个**
    `FileGroupDescriptorW`。

    **这里用的 `NEVER_WRITE_FORMATS` 和发送端 `collect_filter_skip` 是同一份集合。**
    这两处一度各写各的，规则漂移之后 `AsyncFlag` 和 `FileGroupDescriptorW`
    分别从两条缝里漏了过去 —— 所以现在只留一份定义。
    """
    return [i for i in items if i.name not in cb.NEVER_WRITE_FORMATS]


def make_collect_filter(policy: SyncPolicy, exclude_patterns: "Optional[Sequence[object]]" = None):
    """生成采集阶段的过滤回调。**必须在剪贴板已打开的状态下调用**。

    分两层，顺序固定：

      1. `clipboard.collect_filter_skip()` —— **"这份数据在本机之外根本没有意义"
         的硬规则**（与配置无关）：进程内句柄格式、与具体机器绑定的格式、
         固化了绝对路径的 Shell 格式（`Shell IDList Array` / `FileName(W)` …）、
         以及拖放簿记格式（`AsyncFlag` / `DropDescription` …）。
      2. `SyncPolicy.format_allowed()` —— 用户配置层的开关（类别开关、`exclude`
         正则、`forward_all` / `allow_private`）。

    等价表示去重不在这里做（见上面注释）。

    **这里踩过一个非常难查的坑，值得记住。** 第 1 层的规则原本在 `win/clipboard.py`
    的 `collect_filter_skip()` 里写了一份，**又在下面抄了第二份，而且抄漏了 Shell
    格式和拖放簿记格式那两条**。真正被 `ClipboardSync._collect_filter()` 调用的是
    抄漏的这一份，于是：

      * `AsyncFlag` 被原样发给对端 → 对端资源管理器以为这是一次**异步拖放**，
        粘贴时**转沙漏空等、什么都不粘**；
      * `DropDescription`、`Shell IDList Array` 也跟着过去了。

    而 `collect_filter_skip()` 本身**没有任何调用点**（死代码），日志上也看不出
    异常 —— 格式清单里那一长串名字没人会逐个数。现在两层合并成一次调用，
    规则只留一份，不存在再抄漏的可能。
    """

    def filter_fn(fmt: int, name: str, category: str) -> bool:
        if cb.collect_filter_skip(fmt, name):
            return False
        return policy.format_allowed(name, category, exclude_patterns)[0]

    return filter_fn

#: 元数据里的字段名（短名，省一点带宽）
K_NAME = "n"
K_SIZE = "s"
K_CAT = "c"
K_ZLIB = "z"


class ClipboardSync:
    """剪贴板同步器。

    它不直接持有 socket，而是通过注入的 `send` 回调发帧、通过 `on_state` 观察
    网络状态。这样它可以在没有网络的情况下单独测试。
    """

    def __init__(
        self,
        *,
        policy: SyncPolicy,
        send: Callable[[int, Dict, bytes, Optional[str]], None],
        compress: bool = True,
        inline_html_refs: bool = True,
        ole_finish: bool = False,
        exclude_by_process: "Optional[Sequence[Dict[str, Any]]]" = None,
        notify: Optional[Callable[[str, str], None]] = None,
        files_handler: Optional[Any] = None,
    ) -> None:
        """
        `send(msg_type, body, blob, coalesce_key)` 由会话注入，负责把帧投到 clip 通道。
        `notify(level, text)` 用于向用户提示（超大内容、格式被丢弃等）。
        `files_handler` 是 P5 的文件传输模块，提供 `plan(...)`，可为 None。
        """
        self.policy = policy
        self.send = send
        self.compress = compress
        self.inline_html_refs = inline_html_refs
        #: 写完对端剪贴板后是否让 OLE 正式接管（见 `clipboard.bless_clipboard_with_ole`）。
        #: 默认关 —— 它在**本机实测里会把剪贴板清空**，虽然接管后有自检和回滚，
        #: 但在真机上证明有用之前不该默认打开。
        self.ole_finish = ole_finish
        #: **按复制来源进程切换的丢弃列表。** 正则**在这里就编译好**，
        #: 免得每次采集都重编译一遍。见 `config.ClipboardFormatsConfig.exclude_by_process`。
        self._exclude_by_process: "List[Tuple[str, List[Any]]]" = [
            (
                str(rule.get("process", "")).strip().lower(),
                self.policy.compile_exclude(rule.get("exclude") or []),
            )
            for rule in (exclude_by_process or [])
            if str(rule.get("process", "")).strip()
        ]
        self.notify = notify or (lambda level, text: None)
        self.files_handler = files_handler

        self._watcher: Optional[ClipboardWatcher] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._running = False

        #: 本机上一次成功发出的剪贴板序号，用来跳过重复通知
        self._last_sent_seq = -1
        #: 对端剪贴板序号，收到比它旧的就忽略（乱序/重发保护）
        self._last_remote_seq = -1
        #: 最近一次对端内容里"除文件路径外"的格式，等文件传完后一起写剪贴板
        self._pending_clipboard: List[cb.FormatBlob] = []
        #: 本机已落盘、可粘贴的文件路径。文件传完后任何一次剪贴板重写都要带上它，
        #: 否则"先收到文件、后收到剪贴板帧"的时序会把文件剪贴板抹掉。
        self._local_files: List[str] = []
        #: 最近一次失败的原因（供托盘状态/自检展示；日志之外还要能就地看到）
        self.last_error = ""
        #: `publish_local` 里用来启动物理文件传输的回调（由会话注入）
        self.start_transfer: Optional[Callable[[Any], bool]] = None
        #: 正在接收的分片缓冲
        self._incoming_id: Optional[str] = None
        self._incoming_meta: List[Dict] = []
        self._incoming_blobs: List[bytes] = []
        self._incoming_total = 0
        self._incoming_started = 0.0
        self._lock = threading.Lock()
        #: **"我正在写剪贴板"标记。**
        #:
        #: 光靠 `expect_sequence()` 是**赌时间**：抑制是在消息窗口线程处理
        #: `WM_CLIPBOARDUPDATE` 的那一刻判定的，而写剪贴板的可能是另一个线程 ——
        #: `SetClipboardData` 一调用，通知就投递出去了，完全可能在 `write_formats()`
        #: 还没返回时就被处理掉。写入耗时越长（PowerShell 是子进程，约 1 秒）
        #: 越必然。
        #:
        #: 这个标记让判定**与时间无关**：整个写入期间，同步循环一律不发布。
        #: 代价是"写入期间用户的真实复制会被吞掉一次" —— 但那本来也正确，
        #: 因为这次写入马上就会把剪贴板覆盖掉。
        self._writing = threading.Event()

        #: **对端剪贴板帧的写入队列 —— 只保留最新一帧。**
        #:
        #: 系统剪贴板是**全局独占**资源，`EmptyClipboard` + `SetClipboardData` 必须
        #: 由一个写入者从头做到尾。原先这里每收到一帧就开一个线程去写，实测
        #: （`tests/test_clip_bridge.py`）连着送 8 帧能让 **5 个线程同时抢剪贴板** ——
        #: 它们互相把对方刚写进去的格式清掉，结果是"**粘贴菜单是灰的**"：用户粘贴的
        #: 那一刻，剪贴板正好被另一个线程清空、或者只写了一半。
        #:
        #: 而且中间态本来就没有价值：WPS 复制一次会连发十几帧（真机日志里同一份内容
        #: 2.5 秒内发了 3 次，还有 13/12/7 种格式的不同阶段），只有最后一帧是完整的。
        #: 所以这里不是排队，是**后到的顶掉前面的**。
        self._apply_pending: Optional["Tuple[int, List[cb.FormatBlob]]"] = None
        self._apply_wake = threading.Event()
        self._apply_thread: Optional[threading.Thread] = None

        self.stats = {
            "sent": 0,
            "recv": 0,
            "skipped_local": 0,
            "skipped_remote": 0,
            "announced_only": 0,
            "echo_suppressed": 0,
            "write_failed": 0,
            "partial": 0,
        }

        #: 网络是否可用（由会话更新）
        self._peer_ready = False
        self._enabled = True

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        if self._running:
            return
        self._watcher = ClipboardWatcher(debounce_ms=0)  # 去抖由本模块的等待逻辑做
        if not self._watcher.start():
            raise RuntimeError("剪贴板监听启动失败（AddClipboardFormatListener 失败）")
        self._running = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="netclip-clip-sync", daemon=True)
        self._thread.start()
        self._ensure_apply_thread()
        log.info("剪贴板同步已启动")

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._apply_wake.set()  # 叫醒写入线程，让它看到 _stop
        if self._watcher is not None:
            self._watcher.stop()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        if self._apply_thread is not None:
            self._apply_thread.join(timeout)
            self._apply_thread = None
        self._running = False
        log.info("剪贴板同步已停止")

    def _ensure_apply_thread(self) -> None:
        """按需启动**唯一的**剪贴板写入线程（幂等）。

        见 `_apply_pending` 的说明：剪贴板是全局独占资源，只能有一个写入者。
        做成按需启动是为了让 `_commit` 不依赖"`start()` 一定先被调用过"——
        否则 `start()` 之前到的帧会被静默丢掉。
        """
        if self._apply_thread is not None and self._apply_thread.is_alive():
            return
        self._apply_thread = threading.Thread(
            target=self._apply_loop, name="netclip-clip-apply", daemon=True
        )
        self._apply_thread.start()

    def on_net_state(self, state: Any) -> None:
        """会话在网络状态变化时调用（可能在事件循环线程里）。"""
        self._peer_ready = bool(getattr(state, "clip_up", False))

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    # ------------------------------------------------------------ 本机 -> 对端

    def _loop(self) -> None:
        assert self._watcher is not None
        while not self._stop.is_set():
            try:
                if not self._watcher.wait_for_change(0.25):
                    continue
                self._on_local_change()
            except Exception:  # pragma: no cover - 单次失败不能拖垮整个同步
                log.exception("处理剪贴板变化失败")

    def _on_local_change(self) -> None:
        seq = int(w.user32.GetClipboardSequenceNumber())
        if self._writing.is_set():
            #: 我们自己正在写剪贴板（见 `_writing`）。**这条判定与时间无关**，
            #: 所以不管写的是进程内的 `SetClipboardData` 还是那个要跑一秒的
            #: PowerShell 子进程，都不会把这次写入的回声当成"用户复制了东西"
            #: 发回对端 —— 那正是文件复制无限循环的来源。
            self.stats["echo_suppressed"] += 1
            return
        if seq == self._last_sent_seq:
            # 我们自己刚写进去的那次（理论上已被 suppress 拦掉，这里再兜一层）
            self.stats["echo_suppressed"] += 1
            return
        if not self._enabled or not self._peer_ready:
            return

        decision = self.publish_local()
        if decision.action == ACTION_SKIP:
            self.stats["skipped_local"] += 1
            log.debug("本机剪贴板变化但不同步: %s", decision.reason)
        else:
            self._last_sent_seq = seq

    def publish_local(self) -> SyncDecision:
        """采集本机剪贴板并发送。返回决策结果（也供手动触发/测试使用）。"""
        #: **先问"这份内容是谁放的"**，再决定这次要丢哪些格式。
        #: 同一个 `Ole Private Data` 在 WPS 演示和 Word 上要求相反，静态配置无解 ——
        #: 判据只能是来源进程（见 `config.ClipboardFormatsConfig.exclude_by_process`）。
        owner = cb.clipboard_owner_process()
        exclude_patterns = self._exclude_for_owner(owner)
        if exclude_patterns is not None:
            log.debug("按来源进程 %s 切换丢弃列表：%d 条规则", owner, len(exclude_patterns))

        snapshot = cb.capture(
            max_per_format=self.policy.per_format_max_bytes,
            filter_fn=make_collect_filter(self.policy, exclude_patterns),
        )

        files = _extract_files(snapshot)

        #: **"这些文件本来就是对端发过来的，不能再传回去。"**
        #:
        #: 上面那几道防线（`_writing` 标记、回声抑制）治的都是**时序** ——
        #: 别把自己写入剪贴板造成的回声发出去。这一条治的是**语义**：
        #: 收到对端的文件会落在暂存区，把它们放回剪贴板之后触发的那次"复制"
        #: 根本不是用户的新动作。即使时序防线哪天漏了，这里也能兜住。
        #:
        #: 为什么值得两道独立防线：无限互传的后果是灾难性的 —— 一次复制就能
        #: 让两台机器不停对传同一个文件，灌满磁盘、打满网络，而且**两边日志全正常**。
        #:
        #: 判据用**位置**（在暂存区里）而不是"我记得发过这几个路径"：位置判断
        #: 跨进程、跨重启都成立，而 `_paths` 会被 TTL 清理掉。
        if files and self.files_handler is not None:
            try:
                if self.files_handler.all_from_peer([str(path) for path, _size in files]):
                    self.stats["echo_suppressed"] += 1
                    log.info(
                        "剪贴板里的文件是本机刚从对端收到的，不再发回去（防两端互传）: %s",
                        ", ".join(os.path.basename(str(path)) for path, _size in files[:4]),
                    )
                    return SyncDecision(action=ACTION_SKIP, reason="文件来自对端，不回传")
            except Exception:  # pragma: no cover - 判断失败不能拖垮同步
                log.exception("判断文件来源失败")

        decision = self.policy.decide(
            snapshot.items, files=files, skipped=snapshot.skipped, exclude_patterns=exclude_patterns
        )

        if decision.action == ACTION_SKIP:
            return decision

        if decision.has_files and self.files_handler is not None:
            try:
                plan = self.files_handler.plan(decision.file_paths, decision.files_total_bytes)
                if plan is not None and plan.send:
                    # 真正的传输是异步的（要 await 让路给输入通道），所以交给会话去调度。
                    # 这里只负责"决定要传"，不负责"怎么传"。
                    if self.start_transfer is not None:
                        if not self.start_transfer(plan):
                            log.warning("文件传输未能启动（网络不可用？）")
                            decision.reason = (decision.reason + "；文件传输未启动").strip("；")
                    else:
                        log.debug("没有注入传输调度回调，文件只做了决策未传输")
                elif plan is not None and plan.reason:
                    decision.reason = (decision.reason + "；" + plan.reason).strip("；")
            except Exception:  # pragma: no cover
                log.exception("文件传输计划失败")
        # 无论最终是"完整同步"还是"仅通知"，都要先把元数据算出来 ——
        # 元数据来自决策里的 summaries，不是来自 items（仅通知时 items 是空的）。
        # 这里提前构造 wire 元数据，避免"仅通知"分支发出空的格式清单。
        announce_only = decision.action == ACTION_ANNOUNCE

        wire_meta: List[Dict] = []
        total = 0
        blob = b""
        if announce_only:
            # 只发清单：不编码、不压缩，长度取采集时的大小（近似即可，仅用于告知用户）
            for summary in decision.summaries:
                wire_meta.append(
                    {
                        K_NAME: summary.name,
                        K_SIZE: summary.size,
                        K_CAT: summary.category,
                        K_ZLIB: False,
                        "wl": 0,
                    }
                )
                total += summary.size
        else:
            encoded = self._encode_items(decision)
            if encoded is None:
                decision.action = ACTION_SKIP
                decision.reason = "编码后超过单帧上限"
                return decision
            for item in encoded:
                item["wl"] = len(item["data"])
            blob = b"".join(item["data"] for item in encoded)
            total = len(blob)
            wire_meta = [_public_meta(item) for item in encoded]
            if total > _FRAME_PAYLOAD_BUDGET:
                # 压缩后仍然塞不进一帧：退化成"只通知"。
                # 这里用**原始**大小来汇报，因为用户关心的是"我复制的东西有多大"，
                # 而不是"压缩后有多大"。
                announce_only = True
                blob = b""
                total = sum(int(entry[K_SIZE]) for entry in wire_meta)
                for entry in wire_meta:
                    entry["wl"] = 0

        clip_id = uuid.uuid4().hex
        body: Dict = {
            "id": clip_id,
            "seq": snapshot.sequence,
            "n": len(wire_meta),
            "bytes": total,
            "items": wire_meta,
            "preview": snapshot.text_preview[:200],
            "announce_only": bool(announce_only),
        }

        if announce_only:
            self.send(MsgType.CLIP_ANNOUNCE, body, b"", None)
            self.stats["announced_only"] += 1
            log.info(
                "剪贴板内容过大（%.1f MB），只通知对端不传内容: %s",
                total / 1024.0 / 1024.0,
                decision.reason,
            )
            self.notify("info", "剪贴板内容 %.1f MB，超过上限，未同步内容" % (total / 1024.0 / 1024.0))
            return decision

        self.send(MsgType.CLIP_BEGIN, body, blob, None)
        self.stats["sent"] += 1
        log.info(
            "已发送剪贴板: %d 种格式 %.1f KB -> %s",
            len(wire_meta),
            total / 1024.0,
            ", ".join(str(m[K_NAME]) for m in wire_meta[:8]) + ("…" if len(wire_meta) > 8 else ""),
        )
        if decision.dropped:
            self.stats["partial"] += 1
        return decision

    # ---- 采集过滤 ----

    def _collect_filter(self, fmt: int, name: str, category: str) -> bool:
        return make_collect_filter(self.policy)(fmt, name, category)

    def _exclude_for_owner(self, owner: str) -> "Optional[List[Any]]":
        """按剪贴板所有者的进程名挑一组丢弃正则。

        匹配不到返回 `None` —— 那表示"用配置里那套全局 `exclude`"，**不是**"什么都不丢"。
        这样没配 `exclude_by_process` 的人行为完全不变。
        """
        if not owner:
            return None
        name = owner.strip().lower()
        for process, patterns in self._exclude_by_process:
            if process == name:
                return patterns
        return None

    def _encode_items(self, decision: SyncDecision) -> Optional[List[Dict]]:
        """把决策里的格式编码成待发送的段（含可选压缩）。"""
        out: List[Dict] = []
        for item in decision.items:
            name = str(getattr(item, "name", ""))
            data = bytes(getattr(item, "data", b"") or b"")
            if not data:
                continue
            if name == "HTML Format" and self.inline_html_refs:
                data = inline_html_local_refs(data)
            raw_len = len(data)
            compressed = False
            if self.compress and raw_len >= 512:
                packed = zlib.compress(data, 6)
                # 压缩收益不足就不压：省下的带宽还不够多一次解压的开销
                if len(packed) < raw_len - 64:
                    data = packed
                    compressed = True
            if len(data) > _FRAME_PAYLOAD_BUDGET:
                log.warning("格式 %s 压缩后仍有 %.1f MB，超过单帧上限，本次不传", name, len(data) / 1024.0 / 1024.0)
                return None
            out.append(
                {
                    K_NAME: name,
                    K_SIZE: raw_len,
                    K_CAT: str(getattr(item, "category", "other")),
                    K_ZLIB: compressed,
                    "data": data,
                }
            )
        return out

    # ------------------------------------------------------------ 对端 -> 本机

    def on_frame(self, mtype: int, body: Dict, blob: bytes) -> None:
        """处理对端发来的剪贴板帧（在 asyncio 事件循环线程里被调用）。

        注意：这里会读/写剪贴板，而写剪贴板可能需要退避重试（最长约 0.6s）。
        为此实际的写入被丢到一个短命线程里执行，绝不阻塞事件循环 ——
        否则一次剪贴板竞争就会让鼠标输入卡半秒。
        """
        if mtype == MsgType.CLIP_ANNOUNCE:
            self._handle_announce(body)
        elif mtype == MsgType.CLIP_BEGIN:
            self._handle_begin(body, blob)
        elif mtype == MsgType.CLIP_CHUNK:
            self._handle_chunk(body, blob)
        elif mtype == MsgType.CLIP_END:
            self._handle_end(body)
        elif mtype == MsgType.CLIP_SKIP:
            log.info("对端拒绝接收剪贴板: %s", body.get("reason", ""))

    def _handle_announce(self, body: Dict) -> None:
        """对端只发了元数据（内容太大）。"""
        names = ", ".join(str(i.get(K_NAME, "?")) for i in _as_list(body.get("items"))[:8])
        preview = str(body.get("preview", ""))
        size = int(body.get("bytes", 0))
        log.info("对端剪贴板过大（%.1f MB），未传内容: %s", size / 1024.0 / 1024.0, names)
        text = "对端复制了 %.1f MB 的内容，超过上限未同步" % (size / 1024.0 / 1024.0)
        if preview:
            text += "（%s）" % preview[:60]
        self.notify("info", text)

    def _handle_begin(self, body: Dict, blob: bytes) -> None:
        """对端的完整剪贴板内容到了一帧里。"""
        clip_id = str(body.get("id", ""))
        seq = int(body.get("seq", -1))
        meta = _as_list(body.get("items"))
        blobs = _split_blob(blob, meta)
        self._commit(clip_id, seq, meta, blobs, source="begin")

    def _handle_chunk(self, body: Dict, blob: bytes) -> None:
        """分片（当前发送端不会用，但协议保留。接收端实现好以便向前兼容）。"""
        clip_id = str(body.get("id", ""))
        with self._lock:
            if self._incoming_id != clip_id:
                self._incoming_id = clip_id
                self._incoming_meta = _as_list(body.get("items"))
                self._incoming_blobs = []
                self._incoming_total = 0
                self._incoming_started = time.monotonic()
            self._incoming_blobs.append(blob)
            self._incoming_total += len(blob)

    def _handle_end(self, body: Dict) -> None:
        clip_id = str(body.get("id", ""))
        with self._lock:
            if self._incoming_id != clip_id:
                log.warning("收到不匹配的 CLIP_END (id=%s)", clip_id)
                return
            meta = list(self._incoming_meta)
            blob = b"".join(self._incoming_blobs)
            self._incoming_id = None
            self._incoming_meta = []
            self._incoming_blobs = []
        self._commit(clip_id, int(body.get("seq", -1)), meta, _split_blob(blob, meta), source="end")

    def _commit(self, clip_id: str, seq: int, meta: List[Dict], blobs: List[bytes], source: str) -> None:
        if seq >= 0 and seq < self._last_remote_seq:
            log.debug("忽略过期的对端剪贴板 (seq=%d < %d)", seq, self._last_remote_seq)
            self.stats["skipped_remote"] += 1
            return
        items = _decode_items(meta, blobs)
        if not items:
            log.debug("对端剪贴板解码后为空 (id=%s)", clip_id)
            return

        # 缓存一份"除文件路径之外"的内容。
        #
        # 为什么：对端剪贴板里的 CF_HDROP 装的是**它的**本地路径，在本机毫无意义。
        # 文件要等文件通道把内容传完之后，再用本机的暂存路径重新构造 CF_HDROP。
        # 但那时剪贴板上的其它格式（文本、图片、私有格式）应该被保留 ——
        # 所以这里把它们缓存下来，等文件到位后一起写。
        cache = [i for i in items if i.name != "CF_HDROP"]
        with self._lock:
            self._pending_clipboard = cache

        # 写剪贴板可能退避重试（最坏几秒），放**唯一的**写入线程里做：既不阻塞
        # 事件循环，也不会出现多个线程同时抢系统剪贴板。
        self._queue_apply(seq, items)

    def _queue_apply(self, seq: int, items: List[cb.FormatBlob]) -> None:
        """把一帧排进写入队列。**后到的顶掉前面的** —— 中间态没有价值。"""
        with self._lock:
            self._apply_pending = (seq, items)
        self._ensure_apply_thread()
        self._apply_wake.set()

    def _apply_loop(self) -> None:
        """唯一的剪贴板写入者。"""
        while not self._stop.is_set():
            if not self._apply_wake.wait(0.25):
                continue
            self._apply_wake.clear()
            #: 内层循环把"写入期间又到的帧"一并吃掉：写一帧可能要好几秒，这期间
            #: WPS 可能又发了三帧，全都只保留最后那一帧。
            while not self._stop.is_set():
                with self._lock:
                    pending = self._apply_pending
                    self._apply_pending = None
                if pending is None:
                    break
                try:
                    self._apply_remote(pending[0], pending[1])
                except Exception:  # pragma: no cover - 单帧失败不能拖垮写入线程
                    log.exception("写对端剪贴板内容失败")

    def on_files_ready(self, paths: List[str]) -> None:
        """文件通道把文件落盘并校验通过后调用（在 asyncio 线程里）。

        这一步才真正把"文件"变成"可粘贴的剪贴板内容"：用本机的暂存路径
        构造 `CF_HDROP`，和之前缓存的其它格式一起写进剪贴板。

        **落盘的路径要记在 `_local_files` 里。** 因为发送端的顺序是
        "先发文件帧、再发剪贴板帧"（真机日志：`开始发送 1 个文件` 在前，
        `已发送剪贴板` 在后），所以 `_apply_remote` 很可能在文件落地之后
        才跑完 —— 它重写剪贴板时如果不带上这里的 CF_HDROP，就会把刚写好的
        文件剪贴板**整个抹掉**，用户看到的就是"文件传过来了但粘贴不了"。
        """
        if not paths:
            return
        try:
            file_items = cb.build_file_clipboard_items(paths)
        except Exception:  # pragma: no cover
            log.exception("构造文件剪贴板格式失败")
            return

        with self._lock:
            self._local_files = list(paths)

        #: **投递文件时，剪贴板里只放文件本身 —— 不合并任何对端内容。**
        #:
        #: 之前这里是 `kept = writable_formats(cached) - PreferredDropEffect`，
        #: 再把 `kept + file_items` 一起写进去。想法是"顺便保留别的格式"，
        #: 但真机证明这条路是**污染源**：对端剪贴板里任何我们没预料到的格式
        #: （真机上就是 `FileGroupDescriptorW`）都会跟着写进去，而它正是
        #: "转沙漏空等、什么都不粘"的元凶。
        #:
        #: 而且这本来就该是行为定义的一部分：**在 Explorer 里复制一个文件，
        #: 剪贴板里就是一份文件列表**，没有别的。要保留文本/图片那种"混合剪贴板"
        #: 在这里并不存在 —— 一次复制不可能既是文件又是文本。
        #:
        #: 顺带解决两件事：结果变成确定的（永远只有文件），以及 PowerShell/OLE
        #: 那条更稳的路**总能被用上**（它整体替换剪贴板，之前因为 `kept` 非空被跳过）。
        #:
        #: **优先用 PowerShell 的 OLE 写法**（`Set-Clipboard` = .NET `DataObject` +
        #: `OleSetClipboard`，资源管理器自己那条路，写出来的剪贴板带
        #: `Ole Private Data` 等 Shell 认得的标记）。失败必须回退 ——
        #: 宁可回到"用我们自己拼的 CF_HDROP"，也不能变成"什么都没有"。
        #: 先武装抑制，再写。两件事一起做才够：
        #:
        #:   * `suppress_next()` —— 让消息窗口在处理通知的那一刻就丢掉它，
        #:     这样 `_pending` 根本不会被置位；
        #:   * `_writing` 标记 —— **与时间无关**的那道保险：写入期间同步循环
        #:     一律不发布，不管写的是进程内的 `SetClipboardData` 还是那个要跑
        #:     一秒的 PowerShell 子进程。
        #:
        #: 只靠第一条是在赌时间：`SetClipboardData` 一调用通知就投递出去了，
        #: 消息窗口线程完全可能在 `write_formats()` 还没返回时就把它处理掉。
        #: 真机上就是这样 —— 文件在两端**无限循环**：传过去、写剪贴板、又传回来。
        #: 两边日志全是"正常"的，因为每一步单看都没错。
        if self._watcher is not None:
            self._watcher.suppress_next(window=_FILE_WRITE_SUPPRESS_SEC)
            self._watcher.discard_pending()

        sequence: Optional[int] = None
        how = ""
        self._writing.set()
        try:
            sequence = cb.write_file_clipboard_via_powershell(paths)
            how = "PowerShell Set-Clipboard"

            if sequence is None:
                result = None
                try:
                    result = cb.write_formats(
                        cb.order_for_paste(file_items, []),
                        open_retry_ms=self.policy_open_retry(),
                        owner=self.clipboard_owner_hwnd(),
                    )
                except cb.ClipboardBusy as exc:
                    log.warning("把文件放回剪贴板失败（剪贴板被占用）: %s", exc)
                    return
                except Exception:  # pragma: no cover
                    log.exception("把文件放回剪贴板失败")
                    return

                if not result:
                    log.warning("文件已落盘，但剪贴板写入失败: %s", result.describe())
                    return
                sequence = result.sequence
                how = "内置 CF_HDROP（PowerShell 写法不可用或失败，已回退）"

            #: 序号要在**清除 `_writing` 之前**记下来，否则这里到 `finally` 之间
            #: 还有一条缝：标记已经清了，但 `_last_sent_seq` 还是旧值。
            with self._lock:
                self._last_sent_seq = sequence
        finally:
            self._writing.clear()
            #: 写失败时把那个"覆盖整个子进程运行时间"的大抑制窗口收掉，
            #: 否则它会白白挂 10 多秒，把用户在这期间的正常复制全吞掉。
            if sequence is None and self._watcher is not None:
                self._watcher.clear_suppression()

        if sequence is None:
            return

        if self._watcher is not None:
            #: 写入结束了，把之前那个"覆盖整个子进程运行时间"的大窗口收窄：
            #: 只抑制这次写入产生的那个序号（`_is_suppressed` 仍留了 +32 的宽限，
            #: 因为一次 OLE 落盘会写好几个格式、序号会连着涨），
            #: 时间窗口回到默认的 1.5 秒，不会长期吞掉用户的真实复制。
            #:
            #: 这之后真正的兜底是 `_writing` 标记和 `_last_sent_seq`，不是这个窗口。
            self._watcher.expect_sequence(sequence)
            self._watcher.discard_pending()
        log.info(
            "已把 %d 个本地文件放入剪贴板（%s）: %s",
            len(paths),
            how,
            ", ".join(os.path.basename(p) for p in paths[:4]) + ("…" if len(paths) > 4 else ""),
        )
        #: 把**完整路径**也打出来、并标明是否真实存在。CF_HDROP 是**引用式**的：
        #: 剪贴板里放的是路径，文件必须真的在那儿。排查看不出问题时，先看这一行。
        log.info(
            "剪贴板里的本地路径: %s%s",
            paths[0],
            "" if os.path.exists(paths[0]) else "  ★文件不存在，粘贴必然失败★",
        )

    def _apply_remote(self, seq: int, items: List[cb.FormatBlob]) -> None:
        """把对端发来的剪贴板内容写进本机。

        两条关键规则：

        1. **远端路径在本机无效**：所有"内容里固化了绝对路径"的格式都不写
           （见 `writable_formats`），文件类内容等文件通道传完再用**本地路径**
           重建 `CF_HDROP`。
        2. **不能把已经落盘的文件剪贴板抹掉**：发送端是先发文件帧、再发剪贴板帧，
           所以这里很可能在 `on_files_ready` 之后才跑完。只要这一帧是文件复制
           （帧里有 `CF_HDROP`）且本地文件已经就位，就把本地 `CF_HDROP` 一起写回去。
           真机现象就是漏了这一步 —— "文件传过来了但粘贴不了"。
        """
        has_files = any(i.name == "CF_HDROP" for i in items)

        #: **把"收到了什么"和"写了什么"都记下来。** 排查"粘贴没反应"时，只看
        #: "写入 N 种格式"是分不清下面三种情况的：对端根本没发全 / 被我们过滤掉了 /
        #: 发的是对端的路径而不是本机路径。格式名一并打出来，一眼就能对出来。
        received_names = [i.name for i in items]
        log.info(
            "收到对端剪贴板(%s): %d 种格式 -> %s",
            source_label(seq),
            len(received_names),
            ", ".join(received_names[:8]) + ("…" if len(received_names) > 8 else ""),
        )

        with self._lock:
            if not has_files:
                #: 不是文件复制：本地缓存的文件路径作废，否则下一次重写会凭空塞进
                #: 一个过期的 CF_HDROP
                self._local_files = []
            local_files = list(self._local_files)

        writable = [
            i for i in writable_formats(items) if i.name != cb.PREFERRED_DROPEFFECT_FORMAT
        ]

        if has_files and local_files and not writable:
            #: **纯文件帧：本机什么都不写。**
            #:
            #: 这一帧带来的只有"对端的文件路径"，本机要的是**自己的**路径。
            #: 而那份剪贴板 `on_files_ready` 已经写好了 —— 而且是走 PowerShell/OLE
            #: 那条更稳的路。这里再写一遍等于把它**降级**成我们自己拼的两格式版本。
            #:
            #: 这是真机上最后一块拼图。主机 `--dump-formats` 里失败的那次是：
            #:     CF_HDROP(本机路径) + FileGroupDescriptorW + Preferred DropEffect=COPY
            #: 那个 `Preferred DropEffect` 就是我们内置写法留下的指纹 —— 说明
            #: `on_files_ready` 之后 `_apply_remote` 又写了一遍，把干净的 OLE 剪贴板
            #: 覆盖掉了，还顺手带进了对端的 `FileGroupDescriptorW`（"有描述没内容"
            #: 的虚拟文件格式，正是转沙漏空等的元凶）。
            log.info("对端剪贴板只含文件路径，本机不写（on_files_ready 已用本地路径写好）")
            return

        if has_files and local_files:
            #: 帧里既有文件又有别的格式（例如文本）：把本地文件一起带上，
            #: 否则这一写会把刚建立的文件剪贴板抹掉。
            try:
                writable = writable + cb.build_file_clipboard_items(local_files)
            except Exception:  # pragma: no cover
                log.exception("重建本地文件剪贴板格式失败")
        if not writable:
            #: 这一条**曾经是 log.debug**，结果真机上"文件传过来了但粘贴不了"时
            #: 日志里干干净净，什么都看不出来。这是文件复制最常见的一条路径，必须可见。
            log.info("对端剪贴板没有本机可写的格式（文件路径无效、其余被过滤）")
            return

        #: 和 `on_files_ready` 同一个道理：**抑制必须在写之前武装**，
        #: 而且要靠 `_writing` 标记做到与时间无关。这里的写是进程内的、快得多，
        #: 但 `write_formats` 抢不到剪贴板锁时会退避重试（总窗口约 0.6 秒），
        #: 通知完全可能在那期间被消息线程处理掉。
        if self._watcher is not None:
            self._watcher.suppress_next()
            self._watcher.discard_pending()
        result = None
        ordered: List[cb.FormatBlob] = []
        self._writing.set()
        try:
            try:
                ordered = cb.order_for_paste(writable, [])
                result = cb.write_formats(
                    ordered,
                    open_retry_ms=self.policy_open_retry(),
                    owner=self.clipboard_owner_hwnd(),
                )
            except cb.ClipboardBusy as exc:
                self.stats["write_failed"] += 1
                self.last_error = "剪贴板被占用: %s" % exc
                log.warning("写对端剪贴板内容失败（剪贴板被占用）: %s", exc)
                return
            except Exception as exc:
                self.stats["write_failed"] += 1
                self.last_error = "%s: %s" % (type(exc).__name__, exc)
                log.exception("写对端剪贴板内容失败")
                return

            if not result:
                self.stats["write_failed"] += 1
                self.last_error = "全部格式写入失败: %s" % result.describe()
                log.warning("对端剪贴板内容全部写入失败: %s", result.describe())
                return

            #: 序号在**清除 `_writing` 之前**记下来，别留缝。
            self._last_remote_seq = seq
            self._last_sent_seq = result.sequence  # 也记一份，双保险

            #: **让 OLE 正式接管**（见 `clipboard.bless_clipboard_with_ole`）。
            #: 它会自检"格式有没有变少"；少了就返回 False，这里立刻把原内容写回去 ——
            #: 实测 OLE 接管失败时会把剪贴板清空，那比不接管糟得多。
            if self.ole_finish:
                if cb.bless_clipboard_with_ole():
                    self.stats["ole_finished"] = self.stats.get("ole_finished", 0) + 1
                else:
                    log.info("OLE 接管未成功，把原内容重写回去")
                    retry = cb.write_formats(
                        ordered,
                        open_retry_ms=self.policy_open_retry(),
                        owner=self.clipboard_owner_hwnd(),
                    )
                    if retry:
                        result = retry
                        self._last_sent_seq = retry.sequence
        finally:
            self._writing.clear()
            if result is None and self._watcher is not None:
                self._watcher.clear_suppression()

        # 关键：精确抑制这次写入触发的 WM_CLIPBOARDUPDATE，否则会乒乓。
        # 这是**收窄** `suppress_next()` 开的那个大窗口，不是唯一防线 ——
        # 真正的兜底是 `_writing` 标记和 `_last_sent_seq`。
        if self._watcher is not None:
            self._watcher.expect_sequence(result.sequence)
        self.stats["recv"] += 1
        log.info(
            "已应用对端剪贴板(%s): %s -> %s",
            source_label(seq),
            result.describe(),
            ", ".join(i.name for i in ordered[:8]) + ("…" if len(ordered) > 8 else ""),
        )

    def policy_open_retry(self):
        """写剪贴板的重试序列，可由会话注入真实配置；默认用库内默认值。"""
        retry = getattr(self, "_open_retry_ms", None)
        return retry if retry else cb.DEFAULT_OPEN_RETRY_MS

    def clipboard_owner_hwnd(self) -> int:
        """要用哪个窗口当**剪贴板所有者**。

        用剪贴板监听线程那个 message-only 窗口：那个线程本来就在抽消息，
        正好满足"所有者窗口要能收消息"的要求。取不到就返回 0（无主，和以前一样）。

        为什么要指定：无主剪贴板（`OpenClipboard(None)`）和对端 OLE 的粘贴有关系 ——
        真机对照里 UU远程 送来的内容对端能粘成**可编辑对象**，而它的剪贴板是有主窗口的。
        """
        try:
            hwnd = getattr(self._watcher.listener, "hwnd", None)
            return int(hwnd or 0)
        except Exception:  # pragma: no cover - 拿不到就当无主
            return 0

    # ------------------------------------------------------------ 诊断

    def status(self) -> str:
        active = {k: v for k, v in self.stats.items() if v}
        return " ".join("%s=%d" % (k, v) for k, v in active.items()) or "空闲"


# --------------------------------------------------------------------- 工具


def _as_list(value: Any) -> List[Dict]:
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    return []


def _public_meta(item: Dict) -> Dict:
    """上线用的元数据。`wl` 是压缩后的实际长度，接收端靠它切分 blob。"""
    return {
        K_NAME: item[K_NAME],
        K_SIZE: item[K_SIZE],
        K_CAT: item[K_CAT],
        K_ZLIB: item[K_ZLIB],
        "wl": int(item.get("wl", item[K_SIZE])),
    }


def _split_blob(blob: bytes, meta: List[Dict]) -> List[bytes]:
    """按元数据里的上线长度 `wl` 把一帧二进制体切成各格式的数据。

    `size` 是**压缩前**的原始长度，不能用来切分；`wl` 才是 blob 里的实际长度。
    老版本对端不带 `wl` 时退回用 `size`（那时没有压缩，两者相等）。
    """
    out: List[bytes] = []
    offset = 0
    for item in meta:
        wire_len = int(item.get("wl", item.get(K_SIZE, 0)))
        out.append(blob[offset : offset + wire_len])
        offset += wire_len
    return out


def _decode_items(meta: List[Dict], blobs: List[bytes]) -> List[cb.FormatBlob]:
    items: List[cb.FormatBlob] = []
    for entry, blob in zip(meta, blobs):
        name = str(entry.get(K_NAME, ""))
        if not name or not blob:
            continue
        compressed = bool(entry.get(K_ZLIB, False))
        raw_len = int(entry.get(K_SIZE, len(blob)))
        if compressed:
            try:
                blob = zlib.decompress(blob)
            except zlib.error as exc:
                log.warning("解压格式 %s 失败，跳过: %s", name, exc)
                continue
        if raw_len and len(blob) != raw_len:
            log.debug("格式 %s 长度不符（期望 %d，实际 %d），按实际长度使用", name, raw_len, len(blob))
        category = str(entry.get(K_CAT, "other"))
        items.append(cb.FormatBlob(name=name, category=category, data=blob))
    return items


def source_label(seq: int) -> str:
    return "seq=%d" % seq if seq >= 0 else "无序号"


# --------------------------------------------------------------------- HTML 内联


_FILE_URL_RE = re.compile(rb"(src|href)\s*=\s*[\"']file:///([^\"']+)[\"']", re.IGNORECASE)

#: `CF_HTML` 头里的偏移项。每个值都是**从数据开头算起的字节位置**。
#: 消费者（资源管理器、WPS、Word…）是**照着这些数字去切片段**的。
_CF_HTML_OFFSET_RE = re.compile(
    rb"(StartHTML|EndHTML|StartFragment|EndFragment|StartSelection|EndSelection):(\d+)"
)


def fixup_cf_html_offsets(data: bytes) -> bytes:
    """重算 `CF_HTML`（"HTML Format"）头里的偏移量。

    **为什么必须做。** 这个格式的头部是一段 ASCII 的 `名字:十进制偏移`，
    所有偏移都从数据开头算起。我们内联 `file:///` 图片会把正文改长，**只要正文一变，
    这些数字就全错了** —— 而消费者按偏移去切片段，切到断的片段就当作坏数据，
    于是**放弃 HTML、回退到图片**（真机现象：WPS 公式粘过来变成了图片）。

    真机上的直接证据（同一个剪贴板，两台机器各 `clip_probe.py` 一次）::

        发送端 HTML Format: 33411 字节   头里 EndHTML:0000033411   <- 自洽
        接收端 HTML Format: 96570 字节   头里 EndHTML:0000033411   <- 早就不是这个数了

    片段边界靠 `<!--StartFragment-->` / `<!--EndFragment-->` 这对注释**就地找回**，
    不依赖原来那些（已经错了的）数字。`StartHTML` 不用动：用同样的宽度写回去，
    头部长度不变，正文起点自然不变。
    """
    if not data.startswith(b"Version:"):
        return data
    matches = list(_CF_HTML_OFFSET_RE.finditer(data))
    if not matches:
        return data

    #: 所有字段用同一个宽度；重写后头部长度必须**一模一样**，否则正文起点会移动，
    #: 反而把 StartHTML 也搞错。
    width = max(len(m.group(2)) for m in matches)

    def marker_offset(marker: bytes, after: bool) -> "Optional[int]":
        index = data.find(marker)
        if index < 0:
            return None
        return index + len(marker) if after else index

    new_values = {
        "EndHTML": len(data),
        "StartFragment": marker_offset(b"<!--StartFragment-->", after=True),
        "EndFragment": marker_offset(b"<!--EndFragment-->", after=False),
        "StartSelection": marker_offset(b"<!--StartSelection-->", after=True),
        "EndSelection": marker_offset(b"<!--EndSelection-->", after=False),
    }

    for name, value in new_values.items():
        if value is not None and len(str(value)) > width:
            #: 宽度不够（10 位十进制 = 10GB 的负载）。宁可不改，也不要把头改坏。
            log.debug("CF_HTML 偏移 %s=%d 超出原有宽度 %d，放弃重算", name, value, width)
            return data

    def repl(match: "re.Match[bytes]") -> bytes:
        name = match.group(1).decode("ascii")
        value = new_values.get(name)
        if value is None:
            return match.group(0)  #: StartHTML 不动；找不到标记的项也不动
        return b"%s:%0*d" % (match.group(1), width, value)

    return _CF_HTML_OFFSET_RE.sub(repl, data)


def inline_html_local_refs(html: bytes) -> bytes:
    """把 HTML Format 里的 `file:///` 本地图片引用内联成 data URI。

    为什么必须做：Excel/PPT 复制到 HTML 剪贴板格式时，图片经常是
    `<img src="file:///C:/Users/.../tmp.png">` 这种**本机绝对路径**。
    跨机后这个路径不存在，粘贴出来就是空占位符 —— 用户看到"图片没同步过来"。

    实现上刻意保持保守：
      * 只处理 `src`/`href` 且值是 `file:///` 开头的属性；
      * 文件不存在或读取失败就原样保留（不做任何破坏性改动）；
      * 超过 8MB 的单个文件不内联（避免 HTML 体积失控）。
    """
    if b"file:///" not in html.lower():
        return html

    import base64
    import mimetypes
    import os

    def repl(match: "re.Match[bytes]") -> bytes:
        attr = match.group(1)
        raw_path = match.group(2)
        try:
            path = raw_path.decode("utf-8", errors="replace").replace("/", os.sep)
        except Exception:  # pragma: no cover
            return match.group(0)
        try:
            if not os.path.isfile(path) or os.path.getsize(path) > 8 * 1024 * 1024:
                return match.group(0)
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            return match.group(0)

        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        encoded = base64.b64encode(data)
        quote = match.group(0).split(b"=")[0][-1:] or b'"'
        return b"%s=%sdata:%s;base64,%s%s" % (attr, quote, mime.encode("ascii"), encoded, quote)

    try:
        rewritten = _FILE_URL_RE.sub(repl, html)
    except Exception:  # pragma: no cover - 内联失败不能影响同步
        log.debug("HTML 引用内联失败，原样发送", exc_info=True)
        return html
    if rewritten == html:
        return html
    #: **改完正文必须重算头里的偏移**，否则消费者按旧偏移切到断片段，
    #: 直接放弃 HTML —— 真机上就是"公式粘过来变成图片"。
    return fixup_cf_html_offsets(rewritten)


# --------------------------------------------------------------------- CF_HDROP


def _extract_files(snapshot: cb.ClipboardSnapshot) -> List[tuple]:
    """从剪贴板快照里解析出 CF_HDROP 的文件路径与大小。"""
    item = snapshot.by_name("CF_HDROP")
    if item is None or not item.data:
        return []
    try:
        paths = parse_hdrop(item.data)
    except Exception:  # pragma: no cover
        log.debug("解析 CF_HDROP 失败", exc_info=True)
        return []

    import os

    out: List[tuple] = []
    for path in paths:
        try:
            out.append((path, os.path.getsize(path)))
        except OSError:
            out.append((path, -1))  # -1 = 大小未知（可能是目录或已删除）
    return out


def parse_hdrop(data: bytes) -> List[str]:
    """解析 CF_HDROP 的字节流，返回文件路径列表。

    布局::

        DROPFILES 结构（20 字节）
          pFiles = 文件路径列表相对结构起点的偏移（通常 = 20）
          fWide  = TRUE 表示路径是 UTF-16LE
        路径1\\0路径2\\0\\0          （fWide 时每字符 2 字节）

    两个容易踩的点：

    1. `fWide` 必须按 **BOOL(4 字节)** 读，不能按 1 字节读 —— 否则后面的偏移全错。
    2. `pFiles` 不可信时要**回退**到 20 而不是报错。回退后解出来的第一个"路径"
       可能是 `fWide` 字段的字节。所以再加一道形似性检查：正常情况下第一项
       应该长得像路径（`X:\\` 或 `\\\\server`），否则说明偏移不可信，按默认偏移重解。
    """
    size = _DROPFILES_SIZE
    if len(data) < size:
        return []

    p_files, _pt_x, _pt_y, _f_nc, f_wide = struct_unpack_dropfiles(data)
    offset = int(p_files) if p_files else size
    if offset < size or offset >= len(data):
        offset = size

    paths = _decode_path_block(data[offset:], bool(f_wide))
    if paths and _looks_like_paths(paths):
        return paths

    # 偏移不可信：退回默认偏移再试一次
    if offset != size:
        fallback = _decode_path_block(data[size:], bool(f_wide))
        if _looks_like_paths(fallback):
            return fallback
    return paths


def struct_unpack_dropfiles(data: bytes):
    """读出 DROPFILES 的字段 `(pFiles, pt.x, pt.y, fNC, fWide)`。

    布局与 `winapi.DROPFILES` 一致：`<I ii I I` = 4 + 4 + 4 + 4 + 4 = 20 字节。
    """
    return struct.unpack_from("<IiiII", data, 0)


def _decode_path_block(payload: bytes, wide: bool) -> List[str]:
    if wide:
        end = len(payload) - (len(payload) % 2)
        text = payload[:end].decode("utf-16-le", errors="replace")
    else:
        text = payload.decode("mbcs", errors="replace")
    return [chunk for chunk in (part.strip() for part in text.split("\x00")) if chunk]


def _looks_like_paths(paths: List[str]) -> bool:
    """判断解出来的是不是"像路径"的东西。

    正常路径以盘符（`C:\\`）或 UNC（`\\\\server\\share`）开头。
    这一步是防御性的：CF_HDROP 被破坏或偏移算错时，我们宁可返回空，
    也不要把一坨二进制当成路径去访问文件系统。
    """
    if not paths:
        return False
    first = paths[0]
    if not first:
        return False
    if len(first) >= 2 and first[1] == ":":
        return True
    return first.startswith("\\\\") or first.startswith("//")


_DROPFILES_SIZE = 20


__all__ = [
    "ClipboardSync",
    "inline_html_local_refs",
    "parse_hdrop",
]
