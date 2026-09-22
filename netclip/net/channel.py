"""单条 TCP 通道：帧收发、移动事件合并、心跳与重连。

线程模型
--------
本模块的内部状态**只在 asyncio 事件循环线程里被修改**。其它线程（钩子线程、
剪贴板线程）通过 `send_threadsafe()` 投递，内部用 `loop.call_soon_threadsafe`
转交给事件循环，因此不需要锁。

为什么不用锁 + 阻塞式 socket：钩子回调里做任何可能阻塞的操作都会被 Windows
判定超时并摘掉钩子，表现为"鼠标突然全部失灵"。所有网络等待必须在别的线程。

发送侧的两种丢包策略
--------------------
1. **合并（coalesce）**：相同 `coalesce_key` 的帧在队列里会被合并而不是排队。
   鼠标移动用 `(dx, dy)` 增量发送，合并时把位移**累加**，所以丢的是中间采样点，
   位移总量不丢——既不堆积延迟也不丢精度。
2. **丢弃最旧（drop-oldest）**：队列超过 `queue_max` 时，从最旧的**可丢弃**帧
   开始丢。键盘、鼠标按键标记为不可丢弃，永不丢。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..protocol import (
    HEADER_SIZE,
    PROTOCOL_VERSION,
    FLAG_NONE,
    Frame,
    MsgType,
    ProtocolError,
    decode_body,
    decode_header,
    make_hello,
    msg_name,
    verify_hello,
)

log = logging.getLogger("netclip.net")


def _brief(body: Any, limit: int = 120) -> str:
    """帧体的一行摘要，只用于链路日志（不进热路径之外的任何地方）。"""
    if not isinstance(body, dict):
        return repr(body)[:limit]
    parts = []
    for key in sorted(body):
        value = body[key]
        if isinstance(value, (bytes, bytearray)):
            value = "<%d 字节>" % len(value)
        elif isinstance(value, (list, tuple)):
            value = "[%d 项]" % len(value)
        elif isinstance(value, str) and len(value) > 40:
            value = value[:40] + "…"
        parts.append("%s=%s" % (key, value))
    text = " ".join(parts)
    return text[:limit]

#: 这些类型在队列满时永不被丢弃。
#:
#: **每一类"状态同步"帧都必须在这里。** 踩过的坑：`ENTER`（光标进入对端）
#: 原先不在这个集合里，队列满时会被当"最旧的可丢弃帧"丢掉。后果是：
#:
#:   * 对端永远收不到 ENTER，光标不会被摆到入口位置；
#:   * 而本机已经在按 REMOTE 转发位移了；
#:   * 于是位移在"原地"累积，光标像被一根弹簧拉住。
#:
#: 输入通道每秒钟几百到上千条移动帧，队列被打满是很正常的事，
#: 所以这不是"极端情况"，而是必然会发生。
UNDROPPABLE = frozenset(
    {
        MsgType.HELLO,
        MsgType.HELLO_ACK,
        MsgType.HELLO_REJECT,
        MsgType.BYE,
        MsgType.KEY,
        MsgType.MOUSE_BUTTON,
        MsgType.MOUSE_WHEEL,
        MsgType.ENTER,
        MsgType.LEAVE,
        MsgType.CLAMP,
        MsgType.RELEASE_ALL,
        MsgType.CLIP_BEGIN,
        MsgType.CLIP_END,
        MsgType.CLIP_ACK,
        MsgType.CLIP_ANNOUNCE,
        MsgType.CLIP_SKIP,
        MsgType.FILE_BEGIN,
        MsgType.FILE_END,
        MsgType.FILE_ACK,
        MsgType.FILE_DONE,
    }
)

#: 这些类型在入站队列满时也允许丢弃 —— 只有鼠标移动可以安全丢。
DROPPABLE_INBOUND = frozenset({MsgType.MOUSE_MOVE})


@dataclass
class ChannelStats:
    sent_frames: int = 0
    recv_frames: int = 0
    coalesced: int = 0
    dropped: int = 0
    bytes_sent: int = 0
    bytes_recv: int = 0
    connects: int = 0
    send_queue_peak: int = 0

    def summary(self) -> str:
        return (
            "↑%d帧/%.1fKB ↓%d帧/%.1fKB 合并%d 丢弃%d 连接%d"
            % (
                self.sent_frames,
                self.bytes_sent / 1024.0,
                self.recv_frames,
                self.bytes_recv / 1024.0,
                self.coalesced,
                self.dropped,
                self.connects,
            )
        )


@dataclass
class ChannelConfig:
    name: str
    listen_host: str
    listen_port: int
    peer_ip: str
    peer_port: int
    device_name: str
    psk: str
    nodelay: bool = True
    queue_max: int = 256
    coalesce_ms: int = 8
    heartbeat_sec: float = 2.0
    timeout_sec: float = 6.0
    reconnect_interval_sec: float = 3.0
    connect_timeout_sec: float = 5.0
    tcp_keepalive: bool = True
    accept_peer: bool = True
    #: 握手时捎带给对端的信息
    local_screen: Optional[List[int]] = None
    peer_position: str = ""
    #: 整条链路的逐条日志（`netclip.debug.chain.ChainLog`）；None 表示不记
    chain: Optional[Any] = None

    def label(self) -> str:
        return "%s@%s:%d" % (self.name, self.peer_ip, self.peer_port)


class Channel:
    """一条方向复用的 TCP 通道。

    入站帧通过 `on_frame(type, body, blob)` 回调交给上层；回调在事件循环线程里执行，
    因此实现必须**只做入队/置位**，不能做耗时工作。
    """

    def __init__(
        self,
        cfg: ChannelConfig,
        loop: asyncio.AbstractEventLoop,
        on_frame: Callable[[int, Dict[str, Any], bytes], None],
        on_up: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_down: Optional[Callable[[Optional[Exception]], None]] = None,
    ) -> None:
        self.cfg = cfg
        self.loop = loop
        self.on_frame = on_frame
        self.on_up = on_up
        self.on_down = on_down

        self.stats = ChannelStats()
        self.peer_info: Dict[str, Any] = {}
        self.connected = False
        #: 最近一次收到对端任何帧的时刻（monotonic）。看门狗跨线程读取 ——
        #: float 赋值在 CPython 里是原子的，读到一个旧值只影响一两个周期的判定。
        self.last_recv_ts: float = 0.0
        self.last_send_ts: float = 0.0

        # 严格遵守配置的上限（只保证至少能放 1 条）。
        # 曾经写成 max(16, queue_max)，结果 queue_max=4 时实际容量是 16 ——
        # 队列比配置的大并不是"更安全"，而是让"队列满时该丢谁"的策略被推迟触发，
        # 也让人调不动这个参数。
        self._out: "asyncio.Queue[Optional[Frame]]" = asyncio.Queue(maxsize=max(1, int(cfg.queue_max)))
        #: coalesce_key -> 同时存在于队列中的帧，合并时直接改它，避免重复入队
        self._pending: Dict[Any, Dict[str, Any]] = {}
        self._server: Optional[asyncio.AbstractServer] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._tasks: List[asyncio.Task] = []
        self._closing = False
        self._connecting = False
        self._send_lock = asyncio.Lock()
        #: 双方同时拨号时，入站连接让位给主动拨号方的等待时间
        self._crossed_wait_sec = 0.35

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        self._closing = False
        if self.cfg.accept_peer:
            try:
                self._server = await asyncio.start_server(
                    self._handle_conn, self.cfg.listen_host, self.cfg.listen_port
                )
                log.info("[%s] 监听 %s:%d", self.cfg.name, self.cfg.listen_host, self.cfg.listen_port)
            except OSError as exc:
                log.error("[%s] 监听 %s:%d 失败: %s", self.cfg.name, self.cfg.listen_host, self.cfg.listen_port, exc)
        self._tasks.append(self.loop.create_task(self._dial_loop(), name="dial-%s" % self.cfg.name))
        self._tasks.append(self.loop.create_task(self._writer_loop(), name="send-%s" % self.cfg.name))

    async def stop(self, timeout: float = 3.0) -> None:
        """停止通道。整体带超时——shutdown 绝不能卡住。

        正常路径会走到 `_stop_inner` 并优雅收尾；万一某一步挂住（例如对端
        处于半开状态、drain 永远不返回），这里会放弃等待并强制把任务取消。
        """
        try:
            await asyncio.wait_for(self._stop_inner(), timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError):
            log.debug("[%s] 优雅停止超时，强制取消任务", self.cfg.name)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover
            log.exception("[%s] 停止时异常", self.cfg.name)

        # 无论走哪条路径，最后都确保任务被取消、socket 被关闭
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        await self._close_writer()

    async def _stop_inner(self) -> None:
        self._closing = True
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:  # pragma: no cover
                pass
            self._server = None
        # 先取消循环任务再关 writer：否则 writer 任务可能正卡在 drain() 上，
        # 等一个永远不会来的对端确认。
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # pragma: no cover
                pass
        await self._close_writer()
        self._tasks.clear()

    # ------------------------------------------------------------ 发送

    def send_threadsafe(self, frame: Frame, coalesce_key: Any = None) -> None:
        """从任意线程投递一帧。永阻塞、永不抛给调用方。"""
        if self._closing:
            return
        try:
            self.loop.call_soon_threadsafe(self._enqueue, frame, coalesce_key)
        except RuntimeError:
            pass  # 事件循环已关闭

    def send(self, frame: Frame, coalesce_key: Any = None) -> None:
        """只能在事件循环线程里调用。"""
        self._enqueue(frame, coalesce_key)

    def _enqueue(self, frame: Frame, coalesce_key: Any) -> None:
        if self._closing:
            return

        if coalesce_key is not None:
            body = self._pending.get(coalesce_key)
            if body is not None:
                self._merge_into(body, frame.body)
                self.stats.coalesced += 1
                return

        # 队列满时先腾位置，再决定是否入队。
        while self._out.full():
            if not self._make_room():
                # 队列里全是不允许丢弃的帧（例如连续按键），只能丢弃新来的可丢弃帧。
                if frame.type not in UNDROPPABLE:
                    self.stats.dropped += 1
                    chain = self.cfg.chain
                    if chain is not None and chain.enabled:
                        chain.log_move(
                            "net-drop",
                            "发送队列满，丢弃新帧 帧=%s 队列=%d（保证不丢的控制帧优先）",
                            msg_name(frame.type),
                            self._out.qsize(),
                        )
                    return
                break

        try:
            self._out.put_nowait(frame)
        except asyncio.QueueFull:  # pragma: no cover - 上面的循环已保证有位置
            self.stats.dropped += 1
            return
        if coalesce_key is not None:
            #: 存**帧体本身**（同一个 dict 对象）。后续同键的帧直接往这个 dict 上累加，
            #: 于是"队列里那个帧"就自动带上了整段窗口的位移，不需要再往队列里塞新帧。
            self._pending[coalesce_key] = frame.body

        peak = self._out.qsize()
        if peak > self.stats.send_queue_peak:
            self.stats.send_queue_peak = peak

    @staticmethod
    def _merge_into(target: Dict[str, Any], incoming: Dict[str, Any]) -> None:
        """把一条新位移并进"还躺在队列里的那一帧"。

        为什么是**累加**而不是丢弃（真机上踩过）：
        `dx/dy` 是可累加的位移，`x/y` 是"现在应该在哪"的绝对落点（与路径无关，
        取最后一条）。早期这里直接 `return` 丢掉新帧，注释说"让 writer 稍后把
        这段时间累积的位移一次性收走"—— 但那些帧**根本没进队列**，
        `_collect_moves` 抽不到任何东西，于是它变成了一个纯限流器：
        每个 8ms 窗口只发出**窗口开头那一个位置**，窗口里的位移全丢。

        表现就是"用户停手的一瞬间，最后一个窗口的位移永远不会被应用"，
        从机光标停在旧位置不动 —— 也就是"被拉回去"。
        """
        target["dx"] = int(target.get("dx", 0)) + int(incoming.get("dx", 0))
        target["dy"] = int(target.get("dy", 0)) + int(incoming.get("dy", 0))
        if incoming.get("x") is not None and incoming.get("y") is not None:
            target["x"] = incoming["x"]
            target["y"] = incoming["y"]

    def _make_room(self) -> bool:
        """腾出至少一个位置，返回是否成功。

        做法：把队列整个抽干，丢掉**最旧的一个可丢弃帧**（通常是过期的鼠标移动），
        再把其余的原样放回去。队列容量很小（默认 256），这一遍的开销可以忽略。

        **注意这里绝不能在回填时递归调用自己**：那会造成"腾出一个位置 -> 回填
        又占满 -> 再递归腾位置"的雪崩，最终队列容量被彻底突破（实测 queue_max=4
        时队列能涨到 16 条），而真正想保护的 `ENTER` 反倒被挤掉。
        抽干时已经知道丢了几个，回填是**纯计数**行为，不可能失败。
        """
        kept: List[Optional[Frame]] = []
        removed = False
        victim: Optional[Frame] = None
        while True:
            try:
                item = self._out.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item is None:
                kept.append(item)
                continue
            # 只丢**最旧的一个**：更旧的已经发走了，越靠前的越可能是过期的位移
            if not removed and item.type not in UNDROPPABLE:
                self.stats.dropped += 1
                self._forget_pending(item)
                removed = True
                victim = item
                continue
            kept.append(item)

        if victim is not None:
            # 防御性检查：真的要丢掉的是可丢弃类型
            assert victim.type not in UNDROPPABLE, "不该丢掉控制帧 0x%02X" % victim.type

        # 回填。抽干前的条数是 len(kept) + (1 if removed else 0)，都不超过 maxsize，
        # 所以 put_nowait 不会失败。
        for item in kept:
            try:
                self._out.put_nowait(item)
            except asyncio.QueueFull:  # pragma: no cover - 数学上不可能
                self.stats.dropped += 1
                break
        return removed

    def _forget_pending(self, frame: Frame) -> None:
        """把某个帧从"待合并"表里摘掉。

        按**帧体对象的身份**匹配，而不是按帧对象 —— 合并之后 writer 拿到的是一个
        新建的帧，但它带着同一个 body，用帧对象匹配会漏掉，条目就会**永远留在表里**，
        导致后续同键的帧全部被并进一个再也发不出去的 dict（位移凭空消失）。
        """
        for key, body in list(self._pending.items()):
            if body is frame.body:
                del self._pending[key]

    # ------------------------------------------------------------ 连接管理

    async def _dial_loop(self) -> None:
        while not self._closing:
            if self.connected or not self.cfg.peer_ip:
                await asyncio.sleep(0.2)
                continue
            try:
                await self._dial_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("[%s] 连接 %s 失败: %s", self.cfg.name, self.cfg.label(), exc)
            if self._closing:
                break
            await asyncio.sleep(max(0.5, self.cfg.reconnect_interval_sec))

    async def _dial_once(self) -> None:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.cfg.peer_ip, self.cfg.peer_port),
            timeout=self.cfg.connect_timeout_sec,
        )
        await self._run_connection(reader, writer, initiated=True)

    async def _handle_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.connected or self._connecting:
            # 双方同时拨号时会出现交叉连接（crossed connections）。
            # 我们统一让"主动拨号方"赢：等一小会儿，如果对方先连上了就拒掉这条，
            # 否则说明我们自己的拨号没成功，就接受这条入站连接。
            await asyncio.sleep(self._crossed_wait_sec)
            if self.connected or self._connecting:
                log.debug("[%s] 已有活跃/正在建立的连接，关闭这条多余的入站连接", self.cfg.name)
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:  # pragma: no cover
                    pass
                return
        try:
            await self._run_connection(reader, writer, initiated=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("[%s] 入站连接异常: %s", self.cfg.name, exc)

    async def _run_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, initiated: bool) -> None:
        self._tune(writer)
        self._connecting = True
        try:
            info = await asyncio.wait_for(self._handshake(reader, writer), timeout=self.cfg.connect_timeout_sec)
        except Exception as exc:
            log.warning("[%s] 与 %s 握手失败: %s", self.cfg.name, self.cfg.label(), exc)
            writer.close()
            return
        finally:
            self._connecting = False

        self._writer = writer
        self.peer_info = info
        self.connected = True
        self.stats.connects += 1
        self.last_recv_ts = time.monotonic()
        log.info(
            "[%s] %s 通道已连通（%s，对端=%s v%s）",
            self.cfg.name,
            "主动连接" if initiated else "接受连接",
            self.cfg.label(),
            info.get("name", "?"),
            info.get("v", "?"),
        )
        if self.on_up:
            self._safe_callback(self.on_up, info)

        reader_task = self.loop.create_task(self._reader_loop(reader), name="recv-%s" % self.cfg.name)
        beat_task = self.loop.create_task(self._heartbeat_loop(), name="beat-%s" % self.cfg.name)
        try:
            await reader_task
        finally:
            beat_task.cancel()
            self.connected = False
            await self._close_writer()
            # 丢掉队列里针对旧连接的残留帧
            self._pending.clear()
            if self.on_down:
                self._safe_callback(self.on_down, None)

    def _tune(self, writer: asyncio.StreamWriter) -> None:
        sock = writer.get_extra_info("socket")
        if sock is None:
            return
        try:
            import socket as _socket

            sock.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1 if self.cfg.nodelay else 0)
            if self.cfg.tcp_keepalive:
                sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_KEEPALIVE, 1)
            sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_SNDBUF, 256 * 1024)
            sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_RCVBUF, 256 * 1024)
        except OSError as exc:  # pragma: no cover
            log.debug("[%s] 设置 socket 选项失败: %s", self.cfg.name, exc)

    async def _close_writer(self) -> None:
        writer, self._writer = self._writer, None
        if writer is None:
            return
        try:
            writer.close()
            # 必须带超时：wait_closed() 在 TCP 半开状态下可能长时间不返回，
            # 而 shutdown 卡住比漏关一个 socket 严重得多（用户会以为程序死了）。
            await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
        except (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError, RuntimeError):
            pass
        except asyncio.CancelledError:
            raise

    # ------------------------------------------------------------ 握手

    async def _handshake(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> Dict[str, Any]:
        extra: Dict[str, Any] = {"channel": self.cfg.name}
        if self.cfg.local_screen:
            extra["screen"] = list(self.cfg.local_screen)
        if self.cfg.peer_position:
            # 告诉对端"我们认为你在我的哪一侧"，便于握手后校验两侧配置是否互为镜像
            extra["peer_position"] = self.cfg.peer_position

        hello = make_hello(self.cfg.device_name, self.cfg.psk, extra)
        writer.write(hello.encode())
        await writer.drain()

        while True:
            mtype, body, _blob = await self._read_frame(reader)
            if mtype == MsgType.HELLO:
                if not verify_hello(body, self.cfg.psk):
                    writer.write(Frame(MsgType.HELLO_REJECT, {"reason": "psk 不匹配"}).encode())
                    await writer.drain()
                    raise ProtocolError("对端 PSK 校验失败（两台机器的 security.psk 必须一致）")
                if int(body.get("v", 0)) != PROTOCOL_VERSION:
                    writer.write(
                        Frame(MsgType.HELLO_REJECT, {"reason": "版本不一致", "peer_v": body.get("v")}).encode()
                    )
                    await writer.drain()
                    raise ProtocolError("协议版本不一致: 对端 v%s 本机 v%d" % (body.get("v"), PROTOCOL_VERSION))
                ack = {"v": PROTOCOL_VERSION, "name": self.cfg.device_name}
                if self.cfg.local_screen:
                    ack["screen"] = list(self.cfg.local_screen)
                if self.cfg.peer_position:
                    ack["peer_position"] = self.cfg.peer_position
                writer.write(Frame(MsgType.HELLO_ACK, ack).encode())
                await writer.drain()
                return body
            if mtype == MsgType.HELLO_ACK:
                return body
            if mtype == MsgType.HELLO_REJECT:
                raise ProtocolError("对端拒绝连接: %s" % body.get("reason", "未知原因"))
            # 对端可能先发别的（比如两条连接交叉时的残留），忽略并继续等
            log.debug("[%s] 握手期间忽略 %s", self.cfg.name, msg_name(mtype))

    # ------------------------------------------------------------ 收发循环

    async def _read_frame(self, reader: asyncio.StreamReader) -> Tuple[int, Dict[str, Any], bytes]:
        header = await reader.readexactly(HEADER_SIZE)
        _flags, mtype, jsonlen, binlen = decode_header(header)
        body: Dict[str, Any] = {}
        if jsonlen:
            body = decode_body(await reader.readexactly(jsonlen))
        blob = await reader.readexactly(binlen) if binlen else b""
        self.stats.recv_frames += 1
        self.stats.bytes_recv += HEADER_SIZE + jsonlen + binlen
        return mtype, body, blob

    async def _reader_loop(self, reader: asyncio.StreamReader) -> None:
        try:
            while not self._closing:
                mtype, body, blob = await self._read_frame(reader)
                self._dispatch(mtype, body, blob)
        except asyncio.IncompleteReadError:
            log.info("[%s] 对端关闭了连接", self.cfg.name)
        except (ConnectionError, OSError) as exc:
            log.info("[%s] 连接中断: %s", self.cfg.name, exc)
        except ProtocolError as exc:
            log.error("[%s] 协议错误，断开: %s", self.cfg.name, exc)
        except asyncio.CancelledError:
            raise

    def _dispatch(self, mtype: int, body: Dict[str, Any], blob: bytes) -> None:
        self.last_recv_ts = time.monotonic()
        if mtype == MsgType.PING:
            self.send(Frame(MsgType.PONG, dict(body or {})))
            return
        if mtype == MsgType.PONG:
            return
        if mtype == MsgType.BYE:
            log.info("[%s] 对端主动断开", self.cfg.name)
            raise ConnectionResetError("对端发送 BYE")
        #: 链路环节：**帧真的从网络到达了**。这条日志回答"到底是没发出来还是没收到"。
        chain = self.cfg.chain
        if chain is not None and chain.enabled:
            if mtype == MsgType.MOUSE_MOVE:
                chain.log_move(
                    "net-in",
                    "通道=%s 帧=MOUSE_MOVE dx=%s dy=%s x=%s y=%s",
                    self.cfg.name,
                    (body or {}).get("dx"),
                    (body or {}).get("dy"),
                    (body or {}).get("x"),
                    (body or {}).get("y"),
                )
            else:
                chain.log("net-in", "通道=%s 帧=%s %s", self.cfg.name, msg_name(mtype), _brief(body))
        if self.on_frame:
            self._safe_callback(self.on_frame, mtype, body, blob)

    def _safe_callback(self, fn: Callable, *args: Any) -> None:
        try:
            fn(*args)
        except Exception:  # pragma: no cover - 回调异常不能杀掉连接
            log.exception("[%s] 帧回调异常", self.cfg.name)

    async def _writer_loop(self) -> None:
        while not self._closing:
            try:
                frame = await self._out.get()
            except asyncio.CancelledError:
                raise
            if frame is None:
                continue
            original = frame
            if frame.type == MsgType.MOUSE_MOVE and self.cfg.coalesce_ms > 0:
                frame = await self._collect_moves(frame)
            #: 必须用**合并前**的那个帧去摘表：合并后是新对象，但 body 是继承来的
            self._forget_pending(original)
            await self._flush(frame)

    async def _collect_moves(self, first: Frame) -> Frame:
        """在 coalesce 窗口内把后续鼠标移动的位移累加进来，只发一帧。

        位移是可累加的，所以这里丢掉的只是"中间采样点"，人手感知不到；
        但位移总量完整保留，不会出现"鼠标变慢/漂移"。

        绝对落点 `x`/`y` **不能累加**，只能取最后一条 —— 它表示"现在应该在哪"，
        与路径无关。重建帧时必须把它带上，否则接收端只能退回相对位移，
        而相对位移会被系统的指针加速曲线改写（见 `InputRouter._forward_remote_move`）。
        """
        await asyncio.sleep(self.cfg.coalesce_ms / 1000.0)

        dx = int(first.body.get("dx", 0))
        dy = int(first.body.get("dy", 0))
        last_x = first.body.get("x")
        last_y = first.body.get("y")
        merged = 0

        while True:
            try:
                nxt = self._out.get_nowait()
            except asyncio.QueueEmpty:
                break
            if nxt is None:
                continue
            if nxt.type != MsgType.MOUSE_MOVE:
                # 非移动帧要插队：放回去，等这帧发完立刻处理它。
                self._out.put_nowait(nxt)
                break
            self._forget_pending(nxt)
            dx += int(nxt.body.get("dx", 0))
            dy += int(nxt.body.get("dy", 0))
            if nxt.body.get("x") is not None and nxt.body.get("y") is not None:
                last_x = nxt.body["x"]
                last_y = nxt.body["y"]
            merged += 1

        if merged:
            self.stats.coalesced += merged
        if merged == 0:
            return first
        body = {"dx": dx, "dy": dy}
        if last_x is not None and last_y is not None:
            body["x"] = last_x
            body["y"] = last_y
        # 返回全新的帧，而不是改 first.body —— 避免任何"对象在前一个任务里还被引用"的隐患。
        return Frame(MsgType.MOUSE_MOVE, body, b"", first.flags)

    async def _flush(self, frame: Frame) -> None:
        writer = self._writer
        if writer is None or writer.is_closing():
            self.stats.dropped += 1
            #: 这一环专门用来抓"帧在队列里排着，但连接已经不可用"的情况
            chain = self.cfg.chain
            if chain is not None and chain.enabled:
                chain.log_move("net-out", "丢弃（连接不可用）帧=%s", msg_name(frame.type))
            return
        data = frame.encode()
        #: 链路环节：**帧真的写进 TCP 了**（合并之后的内容）。
        #: 和 "send"(入队) 对照就能看出合并有没有把内容弄坏。
        chain = self.cfg.chain
        if chain is not None and chain.enabled:
            if frame.type == MsgType.MOUSE_MOVE:
                chain.log_move(
                    "net-out",
                    "实际发出 帧=MOUSE_MOVE dx=%s dy=%s x=%s y=%s 字节=%d 队列=%d",
                    frame.body.get("dx"),
                    frame.body.get("dy"),
                    frame.body.get("x"),
                    frame.body.get("y"),
                    len(data),
                    self._out.qsize(),
                )
            else:
                chain.log("net-out", "实际发出 帧=%s %s", msg_name(frame.type), _brief(frame.body))
        async with self._send_lock:
            try:
                writer.write(data)
                await writer.drain()
            except (ConnectionError, OSError, asyncio.CancelledError) as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                log.debug("[%s] 发送失败: %s", self.cfg.name, exc)
                self.stats.dropped += 1
                return
        self.stats.sent_frames += 1
        self.stats.bytes_sent += len(data)
        self.last_send_ts = time.monotonic()

    # ------------------------------------------------------------ 心跳

    async def _heartbeat_loop(self) -> None:
        interval = max(0.5, self.cfg.heartbeat_sec)
        try:
            while not self._closing:
                await asyncio.sleep(interval)
                if not self.connected:
                    return
                self.send(Frame(MsgType.PING, {"t": _now_ms()}))
                # 应用层超时判定。TCP 自身的 keepalive 在 Windows 上默认要 2 小时
                # 才发第一个探测包，拔网线后根本等不到，所以必须自己判。
                if self.last_recv_ts and time.monotonic() - self.last_recv_ts > self.cfg.timeout_sec:
                    log.warning(
                        "[%s] 心跳超时（%.1fs 没有收到任何帧），主动断开以触发重连",
                        self.cfg.name,
                        time.monotonic() - self.last_recv_ts,
                    )
                    writer = self._writer
                    if writer is not None:
                        writer.close()
                    return
        except asyncio.CancelledError:
            raise


def _now_ms() -> int:
    import time

    return int(time.monotonic() * 1000)


__all__ = [
    "Channel",
    "ChannelConfig",
    "ChannelStats",
    "DROPPABLE_INBOUND",
    "FLAG_NONE",
    "UNDROPPABLE",
]
