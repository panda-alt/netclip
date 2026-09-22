"""三条 TCP 通道的编排：input / clip / file。

为什么要拆三条连接
------------------
鼠标移动是"低延迟、可丢弃"的，文件是"高带宽、不可丢弃"的。如果共用一条 TCP
流，传大文件时文件字节会排在鼠标事件前面，鼠标就会明显卡顿（head-of-line
blocking）。拆成三条独立连接后，文件传输完全不影响输入延迟。

三条连接都是**双向复用**的：握手后谁都能往对端发帧。因此不再需要"哪边是服务端"
的概念，双方都既能拨号也能接受连接。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..config import Config
from ..protocol import Frame, MsgType
from .channel import Channel, ChannelConfig

log = logging.getLogger("netclip.net")

CHANNELS = ("input", "clip", "file")

FrameHandler = Callable[[int, Dict[str, Any], bytes], None]
StateHandler = Callable[[], None]

#: 方位 -> 相反方位。用于把"peer_position 必须相反"这条规则说清楚，
#: 以及握手时提示用户该改成什么。见 `NetManager._check_position_agreement`。
_OPPOSITE_NAME = {"right": "left", "left": "right", "up": "down", "down": "up"}


@dataclass
class PeerState:
    input_up: bool = False
    clip_up: bool = False
    file_up: bool = False
    peer_name: str = ""
    peer_version: int = 0
    #: 对端屏幕 [x, y, w, h]；接入后由 layout 使用
    peer_screen: Optional[List[int]] = None
    #: 对端的摆放认知（对端认为我们在它的哪一侧），仅用于握手一致性校验
    peer_says_position: str = ""
    ready_screen: bool = False

    def any_up(self) -> bool:
        return self.input_up or self.clip_up or self.file_up

    def all_up(self) -> bool:
        return self.input_up and self.clip_up and self.file_up


class NetManager:
    """管理三条通道的生命周期，并把帧分发给上层处理器。

    `on_frame` 会在**事件循环线程**里被调用，实现必须只做入队/置位，不能阻塞。
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.state = PeerState()
        self.channels: Dict[str, Channel] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._handlers: Dict[int, FrameHandler] = {}
        self._on_state: List[StateHandler] = []
        self._started = False
        #: 方位矛盾只报一次，避免每次状态变化刷屏
        self._position_warned = False
        #: 整条链路的逐条日志（`netclip.debug.chain.ChainLog`）；由 Session 注入
        self.chain: Optional[Any] = None

    # ------------------------------------------------------------ 回调注册

    def bind(self, msg_type: int, handler: FrameHandler) -> None:
        """注册某个消息类型的处理器。同一类型后注册的会覆盖先注册的。"""
        self._handlers[msg_type] = handler

    def on_state_change(self, handler: StateHandler) -> None:
        self._on_state.append(handler)

    # ------------------------------------------------------------ 生命周期

    def _effective_local_screen(self) -> Optional[List[int]]:
        """要告诉对端的本机屏幕矩形 `[x, y, w, h]`（虚拟桌面坐标）。

        **必须总是发，不能只在用户显式配了 `layout.local_screen` 时才发。**
        踩过的坑：自动探测的那台什么都不发，于是对端一直用配置里的兜底值
        `peer_screen`。真机上两台实际是 2048x1152 和 1920x1200，却互相以为
        对方是 1920x1080 —— 对端矩形算错，钳位边界和"推到对端外边界就交接"
        的判定点跟着一起错，而且错得完全没有日志痕迹。

        `local_screen` 配了就以示数为准（多显示器/虚拟屏场景可能需要手工指定），
        没配就用系统报的虚拟桌面矩形。
        """
        rect = self.cfg.layout.local_screen
        if rect is not None:
            return list(rect.as_tuple())
        try:
            from ..win import winapi as w

            return list(w.get_virtual_screen_rect())
        except Exception:  # pragma: no cover - 非 Windows / API 异常
            log.debug("无法探测本机屏幕，握手将不携带分辨率", exc_info=True)
            return None

    async def start(self) -> None:
        if self._started:
            return
        self._loop = asyncio.get_running_loop()
        self._started = True

        ports = self.cfg.network.ports()
        local_screen = self._effective_local_screen()
        for name in CHANNELS:
            local_port, peer_port = ports[name]
            ch_cfg = ChannelConfig(
                name=name,
                listen_host=self.cfg.network.listen_host,
                listen_port=local_port,
                peer_ip=self.cfg.network.peer_ip,
                peer_port=peer_port,
                device_name=self.cfg.device.resolved_name(),
                psk=self.cfg.security.psk,
                nodelay=self.cfg.network.nodelay,
                queue_max=self.cfg.network.input_queue_max,
                coalesce_ms=self.cfg.network.move_coalesce_ms if name == "input" else 0,
                heartbeat_sec=self.cfg.network.heartbeat_sec,
                timeout_sec=self.cfg.network.timeout_sec,
                reconnect_interval_sec=self.cfg.network.reconnect_interval_sec,
                connect_timeout_sec=self.cfg.network.connect_timeout_sec,
                tcp_keepalive=self.cfg.network.tcp_keepalive,
                local_screen=local_screen,
                peer_position=self.cfg.layout.peer_position,
                chain=self.chain,
            )
            channel = Channel(
                ch_cfg,
                self._loop,
                on_frame=self._make_dispatcher(name),
                on_up=lambda info, n=name: self._channel_up(n, info),
                on_down=lambda exc, n=name: self._channel_down(n, exc),
            )
            self.channels[name] = channel
            await channel.start()

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        # 礼貌地通知对端（尽力而为，失败也无所谓）
        for channel in self.channels.values():
            try:
                channel.send(Frame(MsgType.BYE, {}))
            except Exception:  # pragma: no cover
                pass
        # 给 BYE 一点时间真的发出去，但绝不能久等：shutdown 卡住比丢一个 BYE 严重得多。
        try:
            await asyncio.wait_for(asyncio.sleep(0.05), timeout=0.2)
        except (asyncio.TimeoutError, TimeoutError):  # pragma: no cover
            pass
        results = await asyncio.gather(*(c.stop() for c in self.channels.values()), return_exceptions=True)
        for name, result in zip(list(self.channels), results):
            if isinstance(result, BaseException):
                log.debug("[%s] 关闭时出现异常: %s", name, result)
        self.channels.clear()

    async def wait_closed(self) -> None:
        """等所有通道的读循环结束（Ctrl+C 后用于优雅退出）。"""
        tasks = []
        for channel in self.channels.values():
            tasks.extend(channel._tasks)  # noqa: SLF001 - 同一包内的内部协作
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------ 发送

    def send(self, channel_name: str, frame: Frame, coalesce_key: Any = None) -> None:
        """从事件循环线程发送。"""
        channel = self.channels.get(channel_name)
        if channel is None:
            return
        channel.send(frame, coalesce_key)

    def send_threadsafe(self, channel_name: str, frame: Frame, coalesce_key: Any = None) -> None:
        """从任意线程发送。钩子线程、剪贴板线程都走这里。"""
        channel = self.channels.get(channel_name)
        if channel is None:
            return
        channel.send_threadsafe(frame, coalesce_key)

    def is_up(self, channel_name: str) -> bool:
        channel = self.channels.get(channel_name)
        return bool(channel and channel.connected)

    # ------------------------------------------------------------ 内部

    def _make_dispatcher(self, channel_name: str) -> FrameHandler:
        def dispatch(mtype: int, body: Dict[str, Any], blob: bytes) -> None:
            handler = self._handlers.get(mtype)
            if handler is None:
                log.debug("[%s] 未处理的帧类型 0x%02X", channel_name, mtype)
                return
            handler(mtype, body, blob)

        return dispatch

    def _channel_up(self, name: str, info: Dict[str, Any]) -> None:
        if name == "input":
            self.state.input_up = True
        elif name == "clip":
            self.state.clip_up = True
        else:
            self.state.file_up = True

        if info.get("name"):
            self.state.peer_name = str(info["name"])
        if info.get("v"):
            self.state.peer_version = int(info["v"])
        screen = info.get("screen")
        if isinstance(screen, (list, tuple)) and len(screen) == 4:
            try:
                self.state.peer_screen = [int(v) for v in screen]
                self.state.ready_screen = True
            except (TypeError, ValueError):
                pass
        if info.get("peer_position"):
            self.state.peer_says_position = str(info["peer_position"])
            self._check_position_agreement()

        self._notify_state()

    def _check_position_agreement(self) -> None:
        """检查两台机器的 `layout.peer_position` 是否互相矛盾。

        `peer_position` 的语义是"**对端**在我这一侧"。两台机器互相是对方的对端，
        所以这两个值**必须相反**（right<->left、up<->down）。合法的组合只有四种：

            本机 right <-> 对端 left        本机 up   <-> 对端 down
            本机 left  <-> 对端 right       本机 down <-> 对端 up

        其他任何组合（两边相同，或者一边 right 一边 down）都描述不出一个
        单轴相邻的几何，会让屏幕矩形互相重叠或方向反掉。

        这是配置里最容易踩的坑 —— 用户很自然会两边填一样的值（"两台电脑的摆放
        关系"听起来像是一个共享设置）。真机上就踩到了：两边都填 `right`，
        于是对端把光标摆到了自己**反方向**的那条边上，离主机模型差了一整个屏宽，
        对端光标从第一帧起就贴在错误的边缘、只能单向挪几像素然后被拉回来。

        只报警不擅自纠正：组合非法时我们无从判断哪一边填错了。
        """
        theirs = self.state.peer_says_position
        mine = self.cfg.layout.peer_position
        if not theirs or self._position_warned:
            return
        if theirs == _OPPOSITE_NAME.get(mine):
            return
        self._position_warned = True
        log.error(
            "配置矛盾：本机 layout.peer_position=%r，但握手里对端说它那边也是 %r。"
            "这两个值必须**相反**（本机 %r 才配得上对端 %r）。"
            "非法组合会让光标被摆到对端错误的那条边上，表现为鼠标被钉在边缘、"
            "推不动或来回弹。请把本机改成 %r，或在对端改。",
            mine,
            theirs,
            mine,
            _OPPOSITE_NAME.get(mine, "?"),
            _OPPOSITE_NAME.get(mine, "?"),
        )

    def _channel_down(self, name: str, _exc: Optional[BaseException]) -> None:
        if name == "input":
            self.state.input_up = False
        elif name == "clip":
            self.state.clip_up = False
        else:
            self.state.file_up = False
        log.info("[%s] 通道断开", name)
        self._notify_state()

    def _notify_state(self) -> None:
        for handler in self._on_state:
            try:
                handler()
            except Exception:  # pragma: no cover
                log.exception("状态回调异常")

    def status_line(self) -> str:
        up = "/".join(name for name in CHANNELS if self.is_up(name))
        peer = self.state.peer_name or self.cfg.network.peer_ip or "?"
        return "对端=%s 已连通通道=[%s]" % (peer, up or "无")
