"""会话装配：把配置、网络、路由、钩子、看门狗拼成一个可运行的整体。

线程布局（这是全项目最重要的一张图）
------------------------------------

    ┌ 主线程 ─────────────────────────────────────────────┐
    │ 轻量消息循环 (PeekMessage)                          │
    │ 只用来响应 Ctrl+C 与（将来的）托盘消息               │
    └─────────────────────────────────────────────────────┘
    ┌ 网络线程 (asyncio) ─────────────────────────────────┐
    │ 三条 TCP 通道的收发、握手、心跳、重连                │
    │ 收到对端输入帧 -> 直接注入（不经过 router，延迟最低）│
    └─────────────────────────────────────────────────────┘
    ┌ 钩子线程 ───────────────────────────────────────────┐
    │ WH_MOUSE_LL / WH_KEYBOARD_LL + 自己的消息循环        │
    │ 回调只做入队，绝不做判断（否则会被系统摘掉钩子）      │
    └─────────────────────────────────────────────────────┘
    ┌ 路由线程 ───────────────────────────────────────────┐
    │ 消费钩子事件，跑 LOCAL/REMOTE 状态机，决定转发或丢弃 │
    └─────────────────────────────────────────────────────┘
    ┌ 看门狗线程 ─────────────────────────────────────────┐
    │ 断线回拉、松键、钩子自愈                             │
    └─────────────────────────────────────────────────────┘

任何一条线程都不允许阻塞在另一条线程上：它们之间只通过无锁队列和原子标记通信。

**关于关闭顺序**：网络线程是 asyncio 事件循环的 owner，所以它必须**自己**负责
`NetManager.stop()` 并在之后关闭循环。主线程只设置一个"该收尾了"的标志然后 join。
早期版本让主线程用 `run_coroutine_threadsafe` 去停网络，结果和网络线程的
`loop.close()` 抢跑，稳定复现 "coroutine was never awaited / Event loop is closed"。
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from ..config import Config
from ..layout import Rect, ScreenLayout
from ..net.manager import NetManager
from ..protocol import Frame, MsgType
from ..win import inject
from ..win import winapi as w
from ..win.hooks import HookThread, InputEvent
from .router import InputRouter, Mode, RouterCallbacks
from .watchdog import Watchdog

log = logging.getLogger("netclip.core.session")


def _delta_text(before, after) -> str:
    """两点的差，用于日志。任一点缺失就返回 "?"。"""
    if before is None or after is None:
        return "?"
    return "(%+d,%+d)" % (after[0] - before[0], after[1] - before[1])


#: 网络收尾的整体超时。超过就放弃优雅关闭（daemon 线程会随进程一起走）。
NET_SHUTDOWN_TIMEOUT = 5.0


class Session:
    """netclip 运行实例。"""

    def __init__(self, cfg: Config, clipboard_sync: Optional[Any] = None, files: Optional[Any] = None) -> None:
        self.cfg = cfg
        self.clipboard_sync = clipboard_sync
        self.files = files
        #: 鼠标追踪器（`debug.mouse = true` 时创建）
        self.tracer: Optional[Any] = None
        self._tracer_thread: Optional[threading.Thread] = None
        #: 每秒输入诊断（`debug.mouse = true` 时创建）
        self.diag: Optional[Any] = None
        #: 逐条链路日志。**恒定 None** —— 调试设施已整体删除，保留这个属性只是
        #: 因为下面几十处 `if self.chain is not None` 的守卫还在，删掉它们不划算。
        self.chain: Optional[Any] = None

        self.manager: Optional[NetManager] = None
        self.router: Optional[InputRouter] = None
        self.hooks: Optional[HookThread] = None
        self.watchdog: Optional[Watchdog] = None

        self.layout = self._build_layout(cfg, None)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._net_thread: Optional[threading.Thread] = None
        self._router_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        #: 请求网络线程开始收尾（由主线程设置，网络线程读取）
        self._net_stop = threading.Event()
        self._net_done = threading.Event()
        #: NetManager.start() 已完成的信号
        self._net_loop_ready = threading.Event()
        self._ready = threading.Event()

        #: 对端已经确认收到我们的 ENTER（仅用于诊断）
        self._peer_accepted = False

    # ------------------------------------------------------------ 几何

    @staticmethod
    def _build_layout(cfg: Config, peer_screen: Optional[Rect]) -> ScreenLayout:
        if cfg is None:  # pragma: no cover - 仅防御
            raise ValueError("cfg 不能为空")
        local = cfg.layout.local_screen
        if local is None:
            left, top, width, height = w.get_virtual_screen_rect()
            local = Rect(left, top, width, height)
        peer = peer_screen or cfg.layout.peer_screen
        return ScreenLayout(
            local_rect=local,
            peer_rect=peer,
            peer_position=cfg.layout.peer_position,
            edge_band_px=cfg.layout.edge_band_px,
            warp_inset_px=cfg.layout.warp_inset_px,
            alignment=cfg.layout.alignment,
        )

    def _on_peer_screen(self, screen: Any) -> None:
        """对端上报分辨率后重建几何。"""
        if not isinstance(screen, (list, tuple)) or len(screen) != 4:
            return
        try:
            rect = Rect(int(screen[0]), int(screen[1]), int(screen[2]), int(screen[3]))
        except (TypeError, ValueError):
            return
        if rect.w <= 0 or rect.h <= 0:
            return
        if self.router is not None and rect == self.layout.peer:
            return
        self.layout = self._build_layout(self.cfg, rect)
        if self.router is not None:
            self.router.update_layout(self.layout)
        log.info("对端屏幕已更新: %s", self.layout.describe())

    def _get_clipboard_sync(self) -> Optional[Any]:
        """允许外部（如 __main__）在 Session 构造前注册剪贴板同步器。"""
        return self.clipboard_sync

    def set_files(self, files: Any) -> None:
        """注入文件传输模块（在 start() 之前调用）。"""
        self.files = files

    def set_clipboard_sync(self, clipboard_sync: Any) -> None:
        self.clipboard_sync = clipboard_sync

    # ------------------------------------------------------------ 文件暂存清理

    def _start_staging_cleanup(self) -> None:
        """周期性清理过期的文件暂存目录。

        为什么要留 TTL 而不是传完就删：`CF_HDROP` 只是路径引用，
        用户可能过几分钟才去粘贴。删早了粘贴出来就是"文件不存在"。
        """
        if self.files is None:
            return
        ttl_min = max(1, int(self.cfg.clipboard.files.staging_ttl_min))

        def loop() -> None:
            interval = max(60.0, ttl_min * 60.0 / 4.0)
            while not self._stop.wait(interval):
                try:
                    self.files.cleanup()
                except Exception:  # pragma: no cover
                    log.debug("暂存清理出错", exc_info=True)

        threading.Thread(target=loop, name="netclip-staging-cleanup", daemon=True).start()

    # ------------------------------------------------------------ 文件传输调度

    def start_file_transfer(self, plan: Any) -> bool:
        """把一次文件传输的发送排到事件循环上。返回是否成功排入。

        由 ClipboardSync 在剪贴板同步线程里调用（见 `bridge.publish_local`）。
        真正的发送是协程，因为它要在每个数据块之间 await 让路给输入通道 ——
        这是"传大文件时鼠标不卡"的关键。
        """
        if self._loop is None or self.files is None:
            return False
        if not self.is_up("file"):
            log.warning("file 通道未连通，无法传输文件")
            return False

        async def runner() -> None:
            try:
                self.files.send_file_begin(plan)
                await self.files.stream_files(plan)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("文件传输失败")

        try:
            asyncio.run_coroutine_threadsafe(runner(), self._loop)
        except RuntimeError as exc:  # pragma: no cover - 循环已关闭
            log.debug("无法调度文件传输: %s", exc)
            return False
        return True

    def is_up(self, channel: str) -> bool:
        return bool(self.manager and self.manager.is_up(channel))

    def yield_to_input(self) -> None:
        """文件传输每发一块之后调用，用于观测/微调"让路"效果。

        当前实现是**空操作**，理由：

        * 真正的让路靠 `stream_files` 里的 `await asyncio.sleep(0)` ——
          它把控制权交回事件循环，input 通道的 writer 任务就有机会发送；
        * file 和 input 是**两条独立的 TCP 连接**，本来就互不排队，
          不需要额外的"插队帧"（往 input 通道塞空位移只会浪费带宽）。

        保留这个钩子是因为将来若要加"每 N 块限速一次"之类的策略，
        这里是唯一需要改的地方，而且看门狗/日志可以观察它被调用的频率。
        """

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        self._start_network()
        self._start_input()
        self._start_watchdog()
        if self.clipboard_sync is not None:
            try:
                self.clipboard_sync.start()
            except Exception:
                log.exception("剪贴板同步启动失败（其它功能不受影响）")
        self._start_staging_cleanup()
        self._ready.set()
        log.info("netclip 已启动")

    def stop(self, reason: str = "退出") -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        log.info("正在停止 netclip（%s）…", reason)

        # 顺序很重要：先把控制权收回本机，再停钩子/网络，最后松键。
        # 反过来会出现"钩子拆了但状态还认为在 REMOTE"的瞬间，用户会感觉鼠标失灵。
        if self.watchdog is not None:
            self.watchdog.stop()
        if self.tracer is not None:
            try:
                self.tracer.stop()
            except Exception:  # pragma: no cover
                pass
            self.tracer = None
        if self.router is not None:
            try:
                self.router.recapture(reason)
            except Exception:  # pragma: no cover
                pass
            self.router.stop()
        if self.clipboard_sync is not None:
            try:
                self.clipboard_sync.stop()
            except Exception:  # pragma: no cover
                pass
        if self.hooks is not None:
            self.hooks.stop()

        # 交给网络线程自己收尾（见模块文档：它才是事件循环的 owner）
        self._net_stop.set()
        if not self._net_done.wait(NET_SHUTDOWN_TIMEOUT):
            log.debug("网络线程未在 %.0fs 内完成收尾", NET_SHUTDOWN_TIMEOUT)
        if self._net_thread is not None:
            self._net_thread.join(2.0)
        if self._router_thread is not None:
            self._router_thread.join(2.0)
        try:
            inject.release_all_buttons(force=True)
        except Exception:  # pragma: no cover
            pass
        log.info("netclip 已停止")

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    # ------------------------------------------------------------ 鼠标追踪

    def _on_peer_entry_point(self, ratio: float, peer_x: int, peer_y: int) -> None:
        """主机侧：记下"对端应该落在哪"，作为之后所有位移的基准点。"""
        if self.tracer is None or self.router is None:
            return
        self.tracer.host_enter(ratio, peer_x, peer_y, self.router.mode.value)

    # ------------------------------------------------------------ 网络线程

    def _start_network(self) -> None:
        self._loop = asyncio.new_event_loop()
        self.manager = NetManager(self.cfg)
        self.manager.chain = self.chain
        self._bind_handlers(self.manager)
        self.manager.on_state_change(self._on_net_state)

        def run() -> None:
            assert self._loop is not None and self.manager is not None
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(self._net_main())
            except Exception:
                log.exception("网络线程异常退出")
            finally:
                try:
                    # 把还没跑完的任务清干净，再关循环，避免
                    # "Task was destroyed but it is pending" 刷屏
                    pending = [t for t in asyncio.all_tasks(self._loop) if not t.done()]
                    for task in pending:
                        task.cancel()
                    if pending:
                        self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                except Exception:  # pragma: no cover
                    pass
                try:
                    self._loop.close()
                except Exception:  # pragma: no cover
                    pass
                self._net_done.set()

        self._net_thread = threading.Thread(target=run, name="netclip-net", daemon=True)
        self._net_thread.start()

    async def _net_main(self) -> None:
        """网络线程的主协程：启动 -> 等停止信号 -> 收尾。"""
        assert self.manager is not None
        await self.manager.start()
        self._net_loop_ready.set()

        while not self._stop.is_set():
            await asyncio.sleep(0.1)

        try:
            await asyncio.wait_for(self.manager.stop(), timeout=NET_SHUTDOWN_TIMEOUT)
        except (asyncio.TimeoutError, TimeoutError):
            log.debug("网络优雅关闭超时，强制收尾")
        except Exception:  # pragma: no cover
            log.exception("网络收尾异常")

    def wait_net_ready(self, timeout: float = 5.0) -> bool:
        return self._net_loop_ready.wait(timeout)

    def _bind_handlers(self, manager: NetManager) -> None:
        """把对端来的帧接到本机动作上。

        注意：这些处理函数跑在 **asyncio 事件循环线程**里，它们会直接调用
        `SendInput` 注入输入。这是刻意的 —— 注入路径越短延迟越低，而且
        `SendInput` 本身很快（微秒级），不会拖慢事件循环。

        剪贴板帧是个例外：写剪贴板可能退避重试（最长 0.6s），所以
        `ClipboardSync` 内部会把它丢到短命线程里执行。
        """
        manager.bind(MsgType.MOUSE_MOVE, self._recv_mouse_move)
        manager.bind(MsgType.MOUSE_BUTTON, self._recv_mouse_button)
        manager.bind(MsgType.MOUSE_WHEEL, self._recv_mouse_wheel)
        manager.bind(MsgType.KEY, self._recv_key)
        manager.bind(MsgType.ENTER, self._recv_enter)
        manager.bind(MsgType.LEAVE, self._recv_leave)
        manager.bind(MsgType.CLAMP, self._recv_clamp)
        manager.bind(MsgType.RELEASE_ALL, self._recv_release_all)
        for mtype in (MsgType.CLIP_ANNOUNCE, MsgType.CLIP_BEGIN, MsgType.CLIP_CHUNK, MsgType.CLIP_END, MsgType.CLIP_SKIP):
            manager.bind(mtype, self._recv_clipboard)
        for mtype in (MsgType.FILE_BEGIN, MsgType.FILE_CHUNK, MsgType.FILE_END, MsgType.FILE_ACK, MsgType.FILE_DONE):
            manager.bind(mtype, self._recv_file)

    def _recv_clipboard(self, mtype: int, body: Dict, blob: bytes) -> None:
        if self.clipboard_sync is None:
            return
        try:
            self.clipboard_sync.on_frame(mtype, body, blob)
        except Exception:  # pragma: no cover
            log.exception("处理剪贴板帧失败")

    def _recv_file(self, mtype: int, body: Dict, blob: bytes) -> None:
        if self.files is None:
            log.debug("收到文件帧 0x%02X 但文件传输模块未启用", mtype)
            return
        try:
            self.files.on_frame(mtype, body, blob)
        except Exception:  # pragma: no cover
            log.exception("处理文件帧失败")

    # ---- 对端 -> 本机的输入 ----

    def _recv_mouse_move(self, _t: int, body: Dict, _blob: bytes) -> None:
        """对端发来的光标移动。

        **优先使用绝对落点**（`x`/`y`，对端坐标系）。原因见
        `InputRouter._forward_remote_move`：Windows 会把注入的**相对**位移
        再过一遍"提高指针精确度"的加速曲线（实测 80px -> 290px，1px -> 0px），
        相对位移因此无法和主机的模型对齐。绝对定位不走那条曲线。
        """
        dx = int(body.get("dx", 0))
        dy = int(body.get("dy", 0))
        ax = body.get("x")
        ay = body.get("y")
        mode = self.router.mode.value if self.router else ""

        if self.chain is not None and self.chain.enabled:
            if ax is not None and ay is not None:
                self.chain.log_move(
                    "recv", "收到 dx=%d dy=%d 绝对落点=(%d,%d) 方式=绝对定位", dx, dy, int(ax), int(ay)
                )
            else:
                self.chain.log_move("recv", "收到 dx=%d dy=%d 方式=相对注入（无绝对落点）", dx, dy)

        if ax is not None and ay is not None and self.router is not None:
            self._apply_absolute(int(ax), int(ay), dx, dy, mode)
            return

        if not (dx or dy):
            if self.chain is not None and self.chain.enabled:
                self.chain.log_move("recv", "零位移且无绝对落点，忽略")
            return
        sent = inject.move_relative(dx, dy)
        if self.chain is not None and self.chain.enabled:
            before = self._cursor_or_none()
            after = self._cursor_or_none()
            self.chain.log_move(
                "apply",
                "相对注入 SendInput 插入=%d 光标 %s -> %s（差 %s）",
                sent,
                before,
                after,
                _delta_text(before, after),
            )
        if self.tracer is not None:
            self.tracer.client_move(dx, dy, mode)
            if not sent:
                # SendInput 一个事件都没插进去（UIPI 拦截或安全桌面）
                self.tracer.note("inject_failed", "dx=%d dy=%d" % (dx, dy))

    def _cursor_or_none(self):
        try:
            return w.get_cursor_pos()
        except w.WinApiError:  # pragma: no cover
            return None

    def _apply_absolute(self, x: int, y: int, dx: int, dy: int, mode: str) -> None:
        """把本机光标**绝对**摆到对端指定的位置。

        这是链路最后一环："主机说该在哪" vs "实际落在哪"。两者的差就是最终误差，
        它包含了钳位（光标被屏幕边界挡住）、UIPI 拦截、以及第三方程序改光标。

        **为什么用 `SendInput` 而不是 `SetCursorPos`**（真机上踩过）：
        `SetCursorPos` 产生的是**不带注入标志**的移动事件，会被自己的钩子当成
        用户输入。为了让它可以被识别，早期每次调用都会打开一个 60ms 的"沉降窗"
        去丢弃紧随其后的移动事件 —— 但绝对定位是**每帧一次**（每秒上百次），
        这个窗口就**永远开着**，于是从机自己的物理鼠标被彻底屏蔽：

            从机日志: [输入诊断] 移动事件 0 (0/秒)     <- 整个会话一条都没有

        后果是**从机的鼠标没法把光标推回主机**，只能等主机把光标推到外边界
        主动交回控制权。

        `SendInput` 注入的事件带 `LLMHF_INJECTED`，钩子在计算位移**之前**就跳过它，
        所以既不需要沉降窗，也不会来回喂数据。它同时绕过系统的"提高指针精确度"
        加速曲线（同机实测落点误差 0）。
        """
        before = self._cursor_or_none() if (self.chain is not None and self.chain.enabled) else None
        sent = inject.move_absolute(x, y, virtual_desktop=True)
        if self.router is not None:
            #: 只更新"本机光标在哪"的模型，不做任何会惊动钩子的动作
            self.router.note_absolute_position(x, y)
        after = self._cursor_or_none() if (self.chain is not None and self.chain.enabled) else None
        if self.chain is not None and self.chain.enabled:
            err = "?"
            if after is not None:
                err = "(%+d,%+d)" % (after[0] - x, after[1] - y)
            self.chain.log_move(
                "apply",
                "绝对定位 SendInput(%d,%d) 插入=%d 光标 %s -> %s 误差=%s",
                x,
                y,
                sent,
                before,
                after,
                err,
            )
        if self.tracer is not None:
            self.tracer.client_move(dx, dy, mode, target=(x, y))

    def _recv_mouse_button(self, _t: int, body: Dict, _blob: bytes) -> None:
        inject.button(str(body.get("btn", "")), bool(body.get("down", False)))

    def _recv_mouse_wheel(self, _t: int, body: Dict, _blob: bytes) -> None:
        inject.wheel(int(body.get("delta", 0)), bool(body.get("horizontal", False)))

    def _recv_key(self, _t: int, body: Dict, _blob: bytes) -> None:
        inject.key_event(
            int(body.get("vk", 0)),
            bool(body.get("down", False)),
            scan=int(body.get("scan", 0)),
            extended=bool(body.get("ext", False)),
        )

    def _recv_enter(self, _t: int, body: Dict, _blob: bytes) -> None:
        """对端的光标进入我们这边。

        我们要做的：把本机光标放到对应边缘的对应位置，然后**回一个 LEAVE**，
        告诉对端"本机已经让出控制权"。这一来一回构成完整的交接握手。
        """
        if self.router is None:
            return
        self._peer_accepted = True
        # 从机侧：先按几何算出"应该落在哪"，交给 router 摆光标之后再采样实际落点
        expect_x = expect_y = -1
        ratio = float(body.get("ratio", 0.5))
        from_side = str(body.get("from", self.layout.peer_position))
        try:
            expect_x, expect_y = self.layout.entry_point(ratio, from_side)
        except Exception:  # pragma: no cover
            pass

        if self.chain is not None and self.chain.enabled:
            self.chain.log(
                "enter",
                "收到 ENTER 比例=%.3f 对端方位=%s -> 本机该落在=(%d,%d) 本机矩形=%s 对端矩形=%s",
                ratio,
                from_side,
                expect_x,
                expect_y,
                self.layout.local,
                self.layout.peer,
            )

        self.router.on_peer_enter(body)

        if self.tracer is not None and expect_x >= 0:
            try:
                actual_x, actual_y = w.get_cursor_pos()
            except w.WinApiError:  # pragma: no cover
                actual_x, actual_y = -1, -1
            self.tracer.client_enter(
                ratio, expect_x, expect_y, actual_x, actual_y,
                self.router.mode.value,
            )

        if self.manager is not None:
            self.manager.send("input", Frame(MsgType.LEAVE, {"ack": True, "reason": "peer_enter"}))
        log.info("对端光标进入本机（比例 %.2f）", ratio)

    def _recv_leave(self, _t: int, body: Dict, _blob: bytes) -> None:
        if self.router is not None:
            self.router.on_peer_leave(body)

    def _recv_clamp(self, _t: int, _body: Dict, _blob: bytes) -> None:
        """对端把自己光标钉在边缘了。本机不需要动作，保留接口用于诊断。"""

    def _recv_release_all(self, _t: int, body: Dict, _blob: bytes) -> None:
        log.info("对端要求释放所有按键（%s）", body.get("reason", ""))
        inject.release_all_buttons(force=True)

    # ---- 状态回调 ----

    def _on_net_state(self) -> None:
        if self.manager is None:
            return
        state = self.manager.state
        if self.router is not None:
            self.router.set_peer_ready(state.input_up)
        if state.ready_screen and state.peer_screen:
            self._on_peer_screen(state.peer_screen)
        if self.clipboard_sync is not None:
            try:
                self.clipboard_sync.on_net_state(state)
            except Exception:  # pragma: no cover
                log.exception("剪贴板同步状态回调异常")

    # ------------------------------------------------------------ 输入线程

    def _start_input(self) -> None:
        tracer = None  # 调试追踪器已移除

        callbacks = RouterCallbacks(
            send=self._post,
            send_move=self._send_move,
            warp_cursor=self._warp_cursor,
            release_all=lambda force: inject.release_all_buttons(force),
            request_recapture=self._request_recapture,
            request_toggle=self._request_toggle,
            is_peer_connected=lambda: bool(self.manager and self.manager.is_up("input")),
        )

        self.router = InputRouter(
            self.layout,
            callbacks,
            share_mouse=self.cfg.input.share_mouse,
            share_keyboard=self.cfg.input.share_keyboard,
            forward_media_keys=self.cfg.input.forward_media_keys,
            local_hotkeys=self.cfg.input.local_hotkeys,
            hotkey_recapture=self.cfg.input.hotkey_recapture,
            hotkey_toggle=self.cfg.input.hotkey_toggle,
            switch_cooldown_ms=self.cfg.layout.switch_cooldown_ms,
            arm_delay_ms=self.cfg.layout.arm_delay_ms,
            lock_mouse_on_drag=self.cfg.layout.lock_mouse_on_drag,
            key_debug_lines=self.cfg.input.debug_keys,
            tracer=tracer,
            diag=self.diag,
            chain=self.chain,
        )

        router = self.router
        self.hooks = HookThread(
            on_event=router.note_event,
            should_swallow=router.should_swallow,
            name="netclip-hooks",
            chain=self.chain,
        )
        if not self.hooks.start():
            raise RuntimeError("无法安装全局钩子。可能被安全软件拦截；请查看日志。")

        def route_loop() -> None:
            try:
                router.run()
            except Exception:
                log.exception("输入路由线程异常退出")

        self._router_thread = threading.Thread(target=route_loop, name="netclip-router", daemon=True)
        self._router_thread.start()

    def _post(self, mtype: int, body: Dict, coalesce_key: Optional[object] = None) -> None:
        if self.manager is None:
            return
        self.manager.send_threadsafe("input", Frame(mtype, body), coalesce_key)

    def _send_move(self, dx: int, dy: int, x: int, y: int) -> None:
        """发送鼠标移动。

        `coalesce_key="move"` 让通道层把排队中的多个位移合并成一帧。
        合并是**累加位移**，所以丢的只是中间采样点，位移总量不丢；
        绝对落点取最后一条（合并后仍然是最新位置）。

        注意：合并只对 `dx/dy` 生效，`x/y` 会被覆盖成最新值 —— 语义正确，
        因为绝对落点本身就是"当前应该在哪"，与路径无关。
        """
        if self.manager is None or not self.manager.is_up("input"):
            if self.chain is not None and self.chain.enabled:
                self.chain.log_move("send", "未发出：input 通道不可用")
            return
        if self.chain is not None and self.chain.enabled:
            self.chain.log_move(
                "send", "入队 dx=%d dy=%d 绝对落点=(%d,%d) 合并键=move", dx, dy, x, y
            )
        self.manager.send_threadsafe(
            "input",
            Frame(MsgType.MOUSE_MOVE, {"dx": int(dx), "dy": int(dy), "x": int(x), "y": int(y)}),
            "move",
        )

    def _warp_cursor(self, x: int, y: int) -> None:
        """把本机光标移到 (x, y)。

        除了 `SetCursorPos`，还必须让钩子**忽略紧接着产生的移动事件** ——
        `SetCursorPos` 会让系统发出不带注入标志的 `WM_MOUSEMOVE`，
        如果不屏蔽，两端会把对方的位移互相转发，表现为"鼠标被吸住高频抖动"。
        """
        try:
            w.check(w.user32.SetCursorPos(int(x), int(y)), "SetCursorPos")
        except w.WinApiError as exc:
            log.debug("设置光标位置失败: %s", exc)
        if self.hooks is not None:
            self.hooks.reset_mouse_tracking()
            self.hooks.ignore_next_move()

    def _request_recapture(self) -> None:
        if self.router is not None:
            self.router.recapture("本机热键")

    def _request_toggle(self) -> None:
        if self.router is not None:
            enabled = self.router.toggle_enabled()
            log.info("共享已通过热键%s", "启用" if enabled else "暂停")

    # ------------------------------------------------------------ 看门狗

    def _start_watchdog(self) -> None:
        hooks = self.hooks
        router = self.router

        def last_recv_age() -> Optional[float]:
            if self.manager is None:
                return None
            channel = self.manager.channels.get("input")
            if channel is None or not channel.connected:
                return None
            if not channel.last_recv_ts:
                return 0.0
            return max(0.0, time.monotonic() - channel.last_recv_ts)

        self.watchdog = Watchdog(
            is_peer_input_ready=lambda: bool(self.manager and self.manager.is_up("input")),
            is_remote_mode=lambda: bool(router and router.mode is Mode.REMOTE),
            recapture=lambda reason: router.recapture(reason) if router else None,
            release_all=lambda: inject.release_all_buttons(force=True),
            last_recv_age=last_recv_age,
            timeout_sec=self.cfg.network.timeout_sec,
            reinstall_hooks=(hooks.reinstall if hooks else None),
            is_hook_alive=None,  # 事件静默检测暂不启用，避免空闲时误判
            on_unhealthy=lambda msg: log.warning("看门狗: %s", msg),
        )
        self.watchdog.start()

    # ------------------------------------------------------------ 外部控制

    def toggle(self) -> bool:
        return self.router.toggle_enabled() if self.router else False

    def recapture(self) -> None:
        if self.router is not None:
            self.router.recapture("手动请求")

    def status(self) -> str:
        parts = []
        if self.manager is not None:
            parts.append(self.manager.status_line())
        if self.router is not None:
            parts.append(self.router.status())
        if self.hooks is not None:
            parts.append("钩子: %s" % self.hooks.stats.summary())
        if self.watchdog is not None:
            parts.append("看门狗: %s" % self.watchdog.status())
        return " | ".join(parts)

    def status_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.router.mode.value if self.router else "?",
            "enabled": self.router.enabled if self.router else False,
            "input_up": bool(self.manager and self.manager.is_up("input")),
            "clip_up": bool(self.manager and self.manager.is_up("clip")),
            "file_up": bool(self.manager and self.manager.is_up("file")),
            "peer": self.manager.state.peer_name if self.manager else "",
            "layout": self.layout.describe(),
        }


__all__ = ["Session"]
