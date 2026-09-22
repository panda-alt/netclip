"""配置加载与校验。

配置来源优先级：命令行 `--config` > 环境变量 `NETCLIP_CONFIG` > 程序目录下 `config.toml`。

校验失败时抛出 `ConfigError`，其中包含**字段路径**（如 `network.peer_ip`），
便于用户直接定位。所有字段都有默认值，最小可运行配置只需要填 `peer_ip`。
"""

from __future__ import annotations

import os
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ._compat import load_toml
from .layout import DOWN, LEFT, RIGHT, UP, Rect

VALID_POSITIONS = (RIGHT, LEFT, UP, DOWN)


# --------------------------------------------------------------------- 路径
#
# 打包成 exe 之后有两个"根目录"，**必须分开**，否则配置会写到临时目录里去。
# 见 `app_dir()` 和 `resource_path()` 的说明。


def app_dir() -> Path:
    """程序的**根目录** —— 用户看得见、会去改的东西（`config.toml`、日志）在这。

    * 源码运行：仓库根目录；
    * 打包成 exe：**exe 所在目录**。

    为什么不能直接用 `__file__`：PyInstaller 的 `--onefile` 会把包解压到一个
    临时目录（`sys._MEIPASS`），`__file__` 就指向那里 —— 每次启动都换一个地方。
    拿它去找 `config.toml`，用户改的配置永远读不到，日志也会散落在 temp 里。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_path(name: str) -> Path:
    """找一个**随程序分发**的只读资源（例如 `config.example.toml`）。

    和 `app_dir()` 不是一回事：`app_dir()` 是"用户会去改"的目录，
    这里是"程序自带的、用户不该动"的东西。打包之后它在 `sys._MEIPASS` 里。

    优先找解包目录，找不到再退回 `app_dir()` —— 这样源码运行、exe 运行、
    以及用户手工把 example 放在 exe 旁边这几种情况都能工作。
    """
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidate = Path(bundle) / name
        if candidate.exists():
            return candidate
    return app_dir() / name


class ConfigError(ValueError):
    pass


# --------------------------------------------------------------------- 取值助手


def _get(table: Dict[str, Any], key: str, path: str, default: Any) -> Any:
    if key not in table or table[key] is None:
        return default
    return table[key]


def _as_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError("%s 必须是整数，得到 %r" % (path, value))
    return int(value)


def _as_float(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError("%s 必须是数字，得到 %r" % (path, value))
    return float(value)


def _as_exclude_rules(value: Any) -> List[Dict[str, Any]]:
    """解析 `clipboard.formats.exclude_by_process`。

    每项必须是 `{ process = "xxx.exe", exclude = ["正则", ...] }`。
    进程名做小写归一；`exclude` 里的正则**当场编译一次**，非法就报错（别等到采集时才炸）。
    """
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ConfigError("clipboard.formats.exclude_by_process 必须是数组，得到 %r" % (value,))
    rules: List[Dict[str, Any]] = []
    for index, item in enumerate(value):
        where = "clipboard.formats.exclude_by_process[%d]" % index
        if not isinstance(item, dict):
            raise ConfigError("%s 必须是表 { process = ..., exclude = [...] }，得到 %r" % (where, item))
        process = item.get("process")
        if not isinstance(process, str) or not process.strip():
            raise ConfigError("%s.process 必须是非空字符串" % where)
        patterns = item.get("exclude", [])
        if not isinstance(patterns, list) or any(not isinstance(p, str) for p in patterns):
            raise ConfigError("%s.exclude 必须是字符串数组" % where)
        for pattern in patterns:
            try:
                import re

                re.compile(pattern)
            except re.error as exc:
                raise ConfigError("%s.exclude 里的正则非法 %r: %s" % (where, pattern, exc)) from None
        rules.append({"process": process.strip().lower(), "exclude": list(patterns)})
    return rules


def _as_bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError("%s 必须是 true/false，得到 %r" % (path, value))
    return value


def _as_str(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise ConfigError("%s 必须是字符串，得到 %r" % (path, value))
    return value


def _as_str_list(value: Any, path: str) -> List[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigError("%s 必须是字符串数组，得到 %r" % (path, value))
    return list(value)


def _as_int_quad(value: Any, path: str) -> Rect:
    if not isinstance(value, list) or len(value) != 4:
        raise ConfigError("%s 必须是 [x, y, w, h] 四个整数，得到 %r" % (path, value))
    nums = [_as_int(item, "%s[%d]" % (path, idx)) for idx, item in enumerate(value)]
    if nums[2] <= 0 or nums[3] <= 0:
        raise ConfigError("%s 的宽高必须为正，得到 %r" % (path, value))
    return Rect(nums[0], nums[1], nums[2], nums[3])


def _one_of(value: str, options: "tuple[str, ...]", path: str) -> str:
    if value not in options:
        raise ConfigError("%s 必须是 %s 之一，得到 %r" % (path, ", ".join(options), value))
    return value


# --------------------------------------------------------------------- 配置段


@dataclass
class DeviceConfig:
    name: str = ""

    def resolved_name(self) -> str:
        if self.name:
            return self.name
        return socket.gethostname() or "netclip-host"


@dataclass
class NetworkConfig:
    listen_host: str = "0.0.0.0"
    listen_port: int = 24800
    clip_port: int = 24801
    file_port: int = 24802
    peer_ip: str = ""
    peer_port: int = 24800
    nodelay: bool = True
    input_queue_max: int = 256
    move_coalesce_ms: int = 8
    heartbeat_sec: float = 2.0
    timeout_sec: float = 6.0
    reconnect_interval_sec: float = 3.0
    connect_timeout_sec: float = 5.0
    tcp_keepalive: bool = True
    recapture_on_disconnect: bool = True

    def ports(self) -> Dict[str, "tuple[int, int]"]:
        """返回 通道 -> (本地监听端口, 对端端口)。

        三通道在同一条链路上是不同的 TCP 连接，端口按 0/1/2 顺序排列：
        input = peer_port, clip = peer_port + 1, file = peer_port + 2。
        """
        return {
            "input": (self.listen_port, self.peer_port),
            "clip": (self.clip_port, self.peer_port + 1),
            "file": (self.file_port, self.peer_port + 2),
        }


@dataclass
class LayoutConfig:
    peer_position: str = RIGHT
    local_screen: Optional[Rect] = None  # None = 运行时自动探测
    peer_screen: Rect = field(default_factory=lambda: Rect(0, 0, 1920, 1080))
    edge_band_px: int = 2
    warp_inset_px: int = 8
    #: 穿越后多久不判反向穿越，防共享边弹跳
    arm_delay_ms: int = 600
    #: 两次穿越之间的冷却时间。
    #:
    #: 不只是"防抖"：它还是**两端抢控制权的仲裁窗口** —— 刚让出控制权的一方
    #: 在这段时间内不接受对端的接管请求（见 `InputRouter.on_peer_enter`）。
    #: 默认值偏大是刻意的：太小会退化成两端互相把光标拽回来（看起来像被弹簧拉住）。
    switch_cooldown_ms: int = 500
    #: 两边屏幕沿共享边尺寸不同时的对齐方式: center / start / end
    alignment: str = "center"
    #: 拖拽时锁定鼠标（阻止把窗口拖到另一半屏幕时被"拽"走）
    lock_mouse_on_drag: bool = True


@dataclass
class InputConfig:
    share_mouse: bool = True
    share_keyboard: bool = True
    forward_media_keys: bool = False
    backend: str = "auto"  # auto | python | native
    #: 这些组合键**永不转发**，永远在本机生效。
    #:
    #: ★ 拦截策略是"**整段拦下**"：一旦某个组合的修饰键齐全，就进入拦截段，
    #:   直到修饰键全部松开为止，中间所有按键都不转发。这是为了让 `ctrl+alt+del`
    #:   这类"主键在最后"的组合也能被完整挡下。
    #:
    #: ★ 因此**不要放"单个修饰键 + 主键"的组合**（`win+l`、`alt+tab`…）：
    #:   按下那个修饰键的瞬间组合条件就成立了，于是**这个修饰键本身也被吞掉**。
    #:   真机上就踩到了 —— 配了 `win+l` 之后 Win 键完全传不到对端，
    #:   而 Win 键极其常用（Win+E / Win+D / Win+方向）；`alt+tab` 同理会吞掉 Alt。
    #:
    #: ★ 要保住某个本机组合，请用**两个以上修饰键**，例如 `ctrl+alt+del`、
    #:   `ctrl+shift+esc`。逃生键另有 `hotkey_recapture`，不怕被锁在对端。
    local_hotkeys: List[str] = field(
        default_factory=lambda: ["ctrl+alt+del", "ctrl+shift+esc"]
    )
    hotkey_recapture: str = "ctrl+alt+home"
    hotkey_toggle: str = "ctrl+alt+pause"
    #: 启动后打印多少条"按键未转发"的诊断日志（0 = 关闭）。
    #: 排查"键盘没映射到对端"时设成 10，敲几下键盘就能看出原因。
    debug_keys: int = 0
    #: 启动后打印多少条"按键未转发"的诊断日志（0 = 关闭）。
    #: 排查"键盘没映射到对端"时把它设成 10，敲几下键盘就能看出原因。
    debug_keys: int = 0


@dataclass
class ClipboardFilesConfig:
    auto_transfer_max_mb: int = 200
    over_threshold_action: str = "ask"  # ask | skip | transfer
    receive_dir: str = ""
    #: 收到的文件怎么交付：
    #:
    #:   * `folder`（默认）—— **直接落盘到 `receive_dir`**，完全不碰剪贴板。
    #:     收完给个提示"文件已存到 …"，你自己去那个目录拿。
    #:   * `clipboard` —— 落盘到暂存目录后把 `CF_HDROP` 放回剪贴板，对端可以直接
    #:     Ctrl+V 粘贴。**但它依赖资源管理器的粘贴路径，实测两次把 Explorer
    #:     搞崩（卡住重启）**，所以默认不开。
    #:
    #: 为什么默认选 folder：跨机复制文件在剪贴板层面牵扯的东西太多
    #: （`Shell IDList Array` / 拖放簿记格式 / 定长结构截断），而"文件就在那儿了"
    #: 这个语义本身完全不需要剪贴板。稳，而且用户找得到。
    deliver: str = "folder"
    staging_ttl_min: int = 120
    chunk_size_kb: int = 1024
    verify_sha256: bool = True


def default_receive_dir() -> str:
    """收文件的默认目录：`%USERPROFILE%\\Downloads\\netclip`。

    特意放在**用户能找到**的地方（而不是 `%LOCALAPPDATA%` 那种隐藏目录），
    因为默认交付方式就是"文件直接摆在这儿"。
    """
    base = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return os.path.join(base, "Downloads", "netclip")


@dataclass
class ClipboardFormatsConfig:
    forward_all: bool = True
    #: 暂时**一条都不排除** —— 这是"能粘、但退成图片"那一组实测状态。
    #:
    #: 真机 A/B 记录（同一个剪贴板，两台各跑一次 `tools/clip_probe.py`）：
    #:
    #:   | 排除 | 接收端 | 结果 |
    #:   |---|---|---|
    #:   | DataObject + Ole Private Data + Link Source* | 13 种 | 菜单灰，完全粘不了 |
    #:   | 一条不排 | 18 种 | 能粘，但退成图片 |
    #:   | DataObject + Ole Private Data | 16 种 | 能粘，可编辑（**UU远程 的行为**）|
    #:
    #: **但这三组之间不止一个变量不同**：UU远程 除了不转发 `Ole Private Data`，
    #: 也**不改 HTML**（两边 HTML 逐字节一致），而我们会把 `file:///` 图片内联成
    #: data URI。所以"到底是哪一个导致的退成图片"还没定论，正在**逐个隔离**：
    #: 先只关 `inline_html_refs`，其余保持不动。
    #:
    #: `Link Source` 系列现在能确定**不能排除** —— 第一组里它被排掉，结果是菜单灰。
    exclude: List[str] = field(default_factory=list)
    #: **按"复制来源进程"切换丢弃列表。**
    #:
    #: 为什么需要它：同一个 `Ole Private Data` 在不同程序上要求**相反** ——
    #: WPS 演示（`wpp.exe`）要它不在（否则形状/文本框退成图片），
    #: Word（`winword.exe`）要它在（否则文字粘不了）。8 种组合的真机枚举证明
    #: **没有任何一组静态 `exclude` 能两边都满足**。
    #:
    #: 判据用**进程名**（剪贴板所有者），因为那是直接可观测的，不用猜格式。
    #: 每项形如 `{ process = "wpp.exe", exclude = ["^Ole Private Data$"] }`；
    #: 匹配不到就用上面的 `exclude`。进程名按小写、精确匹配文件名。
    exclude_by_process: List[Dict[str, Any]] = field(default_factory=list)
    per_format_max_mb: int = 100
    compress: bool = True
    inline_html_refs: bool = True
    #: 写完对端剪贴板后，让 **OLE 正式接管**（`OleGetClipboard` -> `OleSetClipboard`
    #: -> `OleFlushClipboard`），由 OLE 生成属于**本机**的数据对象身份。
    #:
    #: 背景：裸 `SetClipboardData` 写出来的剪贴板没有 OLE 数据对象的身份，而
    #: Office/WPS 的粘贴走 `OleGetClipboard`。对照组 UU远程 送过来的同一份内容
    #: 对端粘出来是**可编辑对象**，它的剪贴板**有所有者窗口**。
    #:
    #: **默认关**：本机实测里这一步会把剪贴板清空（`CLIPBRD_E_CANT_CLOSE`）。
    #: 接管后有"格式有没有变少"的自检和回滚，但在真机上证明有用之前不开。
    ole_finish: bool = False
    allow_private: List[str] = field(default_factory=list)
    #: 还原时写回剪贴板的格式顺序（先匹配到的先写），未列出的按原始顺序追加
    paste_priority: List[str] = field(
        default_factory=lambda: [
            "PowerPoint 12.0 Shape",
            "MathType 5.0 Equations",
            "Equation Native",
            "PNG",
            "HTML Format",
            "Rich Text Format",
            "image/bmp",
            "CF_ENHMETAFILE",
            "CF_DIBV5",
            "CF_DIB",
            "CF_UNICODETEXT",
        ]
    )


@dataclass
class ClipboardConfig:
    enable: bool = True
    sync_text: bool = True
    sync_rtf: bool = True
    sync_html: bool = True
    sync_image: bool = True
    sync_files: bool = True
    debounce_ms: int = 120
    max_payload_mb: int = 50
    #: 还原剪贴板时 OpenClipboard 的重试退避（毫秒），窗口约 1.5s
    open_retry_ms: List[int] = field(default_factory=lambda: [20, 40, 80, 160, 320])
    files: ClipboardFilesConfig = field(default_factory=ClipboardFilesConfig)
    formats: ClipboardFormatsConfig = field(default_factory=ClipboardFormatsConfig)


@dataclass
class UiConfig:
    tray: bool = True
    notify: bool = True


@dataclass
class SecurityConfig:
    psk: str = "change-me-please"


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = "netclip.log"
    max_mb: int = 2
    backups: int = 3


@dataclass
class DebugConfig:
    """`[debug]` 段的字段。**目前没有任何代码读它。**

    原先这些开关驱动 `netclip/debug/` 里的鼠标追踪器、输入诊断和链路日志；
    那三个模块在精简时被删掉了，字段就只剩"配置还认、但没有任何效果"。
    保留的理由只有一个：`from_dict()` 对**未知的配置段**是直接报错的，把
    `[debug]` 从 `known` 里拿掉会让已经写过这段的旧配置当场启动失败。

    真要排查输入 / 鼠标问题，用那些自检（`selftest loop` / `warpguard` /
    `inject` / `keys`），它们不依赖这两个模块。
    """

    #: 鼠标追踪总开关。开启后按 `mouse_file` 落盘 CSV，两台机器的文件可直接对比。
    mouse: bool = False
    #: 每多少条鼠标事件采样一条（高频事件全采会把文件撑爆）
    mouse_sample_every: int = 5
    #: 追踪文件路径（相对路径基于配置文件所在目录）。留空则自动按侧命名。
    mouse_file: str = ""
    #: 从机侧采样"真实光标位置"的间隔（毫秒）
    client_sample_ms: int = 200
    #: 逐条打印**整条链路**详细日志的条数上限（0 = 关闭）。
    #:
    #: 为什么要一次性记整条链路而不是只记一个汇总数：这类"位移丢在哪一环"的问题，
    #: 任何单点汇总都只是**猜测**，猜错了就要用户再跑一遍。把
    #: 钩子 -> 路由 -> 转发计算 -> 通道发出 -> 对端收到 -> 注入 -> 光标落点
    #: 每一环都打出来，一次测试就能定位，且日志量有硬上限（不会撑爆文件）。
    chain_lines: int = 0
    #: 启动后打印多少条"按键未转发"的诊断日志（沿用旧字段，这里保留别名说明）
    #: 真实字段在 InputConfig.debug_keys


@dataclass
class Config:
    device: DeviceConfig = field(default_factory=DeviceConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    layout: LayoutConfig = field(default_factory=LayoutConfig)
    input: InputConfig = field(default_factory=InputConfig)
    clipboard: ClipboardConfig = field(default_factory=ClipboardConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    debug: DebugConfig = field(default_factory=DebugConfig)
    source_path: Optional[Path] = None

    # ------------------------------------------------------------ 校验

    def validate(self) -> None:
        net = self.network
        for label, port in (("listen_port", net.listen_port), ("clip_port", net.clip_port), ("file_port", net.file_port)):
            if not (1 <= port <= 65535):
                raise ConfigError("network.%s 超出范围: %d" % (label, port))
        if net.peer_ip and net.peer_ip not in ("0.0.0.0",):
            try:
                socket.inet_aton(net.peer_ip)
            except OSError:
                # 允许主机名（内网可用计算机名）
                if not net.peer_ip.replace("-", "").replace(".", "").isalnum():
                    raise ConfigError("network.peer_ip 不是合法 IP 或主机名: %r" % net.peer_ip) from None
        if net.peer_port <= 0:
            raise ConfigError("network.peer_port 必须为正")
        if net.move_coalesce_ms < 0:
            raise ConfigError("network.move_coalesce_ms 不能为负")
        if net.timeout_sec <= net.heartbeat_sec:
            raise ConfigError("network.timeout_sec 必须大于 heartbeat_sec（否则心跳还没发就判超时）")

        lay = self.layout
        _one_of(lay.peer_position, VALID_POSITIONS, "layout.peer_position")
        _one_of(lay.alignment, ("center", "start", "end"), "layout.alignment")
        if lay.edge_band_px < 0:
            raise ConfigError("layout.edge_band_px 不能为负")
        if lay.warp_inset_px < 0:
            raise ConfigError("layout.warp_inset_px 不能为负")
        if lay.switch_cooldown_ms < 0:
            raise ConfigError("layout.switch_cooldown_ms 不能为负")

        inp = self.input
        _one_of(inp.backend, ("auto", "python", "native"), "input.backend")

        clip = self.clipboard
        if clip.max_payload_mb <= 0:
            raise ConfigError("clipboard.max_payload_mb 必须为正")
        if clip.debounce_ms < 0:
            raise ConfigError("clipboard.debounce_ms 不能为负")
        if any(ms < 0 for ms in clip.open_retry_ms):
            raise ConfigError("clipboard.open_retry_ms 不能包含负数")

        cf = clip.files
        if cf.auto_transfer_max_mb < 0:
            raise ConfigError("clipboard.files.auto_transfer_max_mb 不能为负")
        _one_of(cf.over_threshold_action, ("ask", "skip", "transfer"), "clipboard.files.over_threshold_action")
        if cf.chunk_size_kb <= 0 or cf.chunk_size_kb > 16 * 1024:
            raise ConfigError("clipboard.files.chunk_size_kb 应在 1..16384 之间")
        if cf.staging_ttl_min < 0:
            raise ConfigError("clipboard.files.staging_ttl_min 不能为负")

        cff = clip.formats
        if cff.per_format_max_mb <= 0:
            raise ConfigError("clipboard.formats.per_format_max_mb 必须为正")
        import re

        for pattern in cff.exclude:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ConfigError("clipboard.formats.exclude 中的正则非法 %r: %s" % (pattern, exc)) from None

        if not self.security.psk:
            raise ConfigError("security.psk 不能为空（否则会与局域网内其它 netclip 实例串台）")

    def describe(self) -> str:
        lines = [
            "设备名       : %s" % self.device.resolved_name(),
            "对端         : %s:%d" % (self.network.peer_ip or "<未设置>", self.network.peer_port),
            "监听端口     : input=%d clip=%d file=%d"
            % (self.network.listen_port, self.network.clip_port, self.network.file_port),
            "摆放关系     : 对端在%s（对齐 %s）"
            % (
                {"right": "右", "left": "左", "up": "上", "down": "下"}[self.layout.peer_position],
                self.layout.alignment,
            ),
            "本机屏幕     : %s" % (self.layout.local_screen or "自动探测"),
            "对端屏幕     : %s" % self.layout.peer_screen,
            "文件同步阈值 : %d MB（超过时 %s）"
            % (self.clipboard.files.auto_transfer_max_mb, self.clipboard.files.over_threshold_action),
            "剪贴板       : %s | 全格式转发=%s | 单格式上限=%dMB"
            % (
                "启用" if self.clipboard.enable else "禁用",
                self.clipboard.formats.forward_all,
                self.clipboard.formats.per_format_max_mb,
            ),
        ]
        return "\n".join(lines)


# --------------------------------------------------------------------- 解析


def _parse_device(raw: Dict[str, Any]) -> DeviceConfig:
    return DeviceConfig(name=_as_str(_get(raw, "name", "device.name", ""), "device.name"))


def _parse_network(raw: Dict[str, Any]) -> NetworkConfig:
    cfg = NetworkConfig()
    cfg.listen_host = _as_str(_get(raw, "listen_host", "network.listen_host", cfg.listen_host), "network.listen_host")
    cfg.listen_port = _as_int(_get(raw, "listen_port", "network.listen_port", cfg.listen_port), "network.listen_port")
    cfg.clip_port = _as_int(_get(raw, "clip_port", "network.clip_port", cfg.listen_port + 1), "network.clip_port")
    cfg.file_port = _as_int(_get(raw, "file_port", "network.file_port", cfg.listen_port + 2), "network.file_port")
    cfg.peer_ip = _as_str(_get(raw, "peer_ip", "network.peer_ip", cfg.peer_ip), "network.peer_ip")
    cfg.peer_port = _as_int(_get(raw, "peer_port", "network.peer_port", cfg.peer_ip and cfg.listen_port or 0), "network.peer_port")
    if cfg.peer_port == 0:
        # 未填 peer_port 时默认与本地监听一致
        cfg.peer_port = cfg.listen_port
    cfg.nodelay = _as_bool(_get(raw, "nodelay", "network.nodelay", cfg.nodelay), "network.nodelay")
    cfg.input_queue_max = _as_int(
        _get(raw, "input_queue_max", "network.input_queue_max", cfg.input_queue_max), "network.input_queue_max"
    )
    cfg.move_coalesce_ms = _as_int(
        _get(raw, "move_coalesce_ms", "network.move_coalesce_ms", cfg.move_coalesce_ms), "network.move_coalesce_ms"
    )
    cfg.heartbeat_sec = _as_float(
        _get(raw, "heartbeat_sec", "network.heartbeat_sec", cfg.heartbeat_sec), "network.heartbeat_sec"
    )
    cfg.timeout_sec = _as_float(_get(raw, "timeout_sec", "network.timeout_sec", cfg.timeout_sec), "network.timeout_sec")
    cfg.reconnect_interval_sec = _as_float(
        _get(raw, "reconnect_interval_sec", "network.reconnect_interval_sec", cfg.reconnect_interval_sec),
        "network.reconnect_interval_sec",
    )
    cfg.connect_timeout_sec = _as_float(
        _get(raw, "connect_timeout_sec", "network.connect_timeout_sec", cfg.connect_timeout_sec),
        "network.connect_timeout_sec",
    )
    cfg.tcp_keepalive = _as_bool(
        _get(raw, "tcp_keepalive", "network.tcp_keepalive", cfg.tcp_keepalive), "network.tcp_keepalive"
    )
    cfg.recapture_on_disconnect = _as_bool(
        _get(raw, "recapture_on_disconnect", "network.recapture_on_disconnect", cfg.recapture_on_disconnect),
        "network.recapture_on_disconnect",
    )
    return cfg


def _parse_layout(raw: Dict[str, Any]) -> LayoutConfig:
    cfg = LayoutConfig()
    cfg.peer_position = _one_of(
        _as_str(_get(raw, "peer_position", "layout.peer_position", cfg.peer_position), "layout.peer_position"),
        VALID_POSITIONS,
        "layout.peer_position",
    )
    local = _get(raw, "local_screen", "layout.local_screen", None)
    cfg.local_screen = None if local is None else _as_int_quad(local, "layout.local_screen")
    peer = _get(raw, "peer_screen", "layout.peer_screen", None)
    if peer is not None:
        cfg.peer_screen = _as_int_quad(peer, "layout.peer_screen")
    cfg.edge_band_px = _as_int(_get(raw, "edge_band_px", "layout.edge_band_px", cfg.edge_band_px), "layout.edge_band_px")
    cfg.warp_inset_px = _as_int(
        _get(raw, "warp_inset_px", "layout.warp_inset_px", cfg.warp_inset_px), "layout.warp_inset_px"
    )
    cfg.switch_cooldown_ms = _as_int(
        _get(raw, "switch_cooldown_ms", "layout.switch_cooldown_ms", cfg.switch_cooldown_ms),
        "layout.switch_cooldown_ms",
    )
    cfg.arm_delay_ms = _as_int(_get(raw, "arm_delay_ms", "layout.arm_delay_ms", cfg.arm_delay_ms), "layout.arm_delay_ms")
    cfg.alignment = _one_of(
        _as_str(_get(raw, "alignment", "layout.alignment", cfg.alignment), "layout.alignment"),
        ("center", "start", "end"),
        "layout.alignment",
    )
    cfg.lock_mouse_on_drag = _as_bool(
        _get(raw, "lock_mouse_on_drag", "layout.lock_mouse_on_drag", cfg.lock_mouse_on_drag),
        "layout.lock_mouse_on_drag",
    )
    return cfg


def _parse_input(raw: Dict[str, Any]) -> InputConfig:
    cfg = InputConfig()
    cfg.share_mouse = _as_bool(_get(raw, "share_mouse", "input.share_mouse", cfg.share_mouse), "input.share_mouse")
    cfg.share_keyboard = _as_bool(
        _get(raw, "share_keyboard", "input.share_keyboard", cfg.share_keyboard), "input.share_keyboard"
    )
    cfg.forward_media_keys = _as_bool(
        _get(raw, "forward_media_keys", "input.forward_media_keys", cfg.forward_media_keys), "input.forward_media_keys"
    )
    cfg.backend = _one_of(
        _as_str(_get(raw, "backend", "input.backend", cfg.backend), "input.backend"), ("auto", "python", "native"), "input.backend"
    )
    cfg.local_hotkeys = _as_str_list(
        _get(raw, "local_hotkeys", "input.local_hotkeys", cfg.local_hotkeys), "input.local_hotkeys"
    )
    cfg.hotkey_recapture = _as_str(
        _get(raw, "hotkey_recapture", "input.hotkey_recapture", cfg.hotkey_recapture), "input.hotkey_recapture"
    )
    cfg.hotkey_toggle = _as_str(
        _get(raw, "hotkey_toggle", "input.hotkey_toggle", cfg.hotkey_toggle), "input.hotkey_toggle"
    )
    cfg.debug_keys = _as_int(_get(raw, "debug_keys", "input.debug_keys", cfg.debug_keys), "input.debug_keys")
    return cfg


def _parse_clipboard_files(raw: Dict[str, Any]) -> ClipboardFilesConfig:
    cfg = ClipboardFilesConfig()
    cfg.auto_transfer_max_mb = _as_int(
        _get(raw, "auto_transfer_max_mb", "clipboard.files.auto_transfer_max_mb", cfg.auto_transfer_max_mb),
        "clipboard.files.auto_transfer_max_mb",
    )
    cfg.over_threshold_action = _one_of(
        _as_str(
            _get(raw, "over_threshold_action", "clipboard.files.over_threshold_action", cfg.over_threshold_action),
            "clipboard.files.over_threshold_action",
        ),
        ("ask", "skip", "transfer"),
        "clipboard.files.over_threshold_action",
    )
    cfg.receive_dir = _as_str(
        _get(raw, "receive_dir", "clipboard.files.receive_dir", cfg.receive_dir), "clipboard.files.receive_dir"
    )
    cfg.deliver = _one_of(
        _as_str(_get(raw, "deliver", "clipboard.files.deliver", cfg.deliver), "clipboard.files.deliver"),
        ("folder", "clipboard"),
        "clipboard.files.deliver",
    )
    cfg.staging_ttl_min = _as_int(
        _get(raw, "staging_ttl_min", "clipboard.files.staging_ttl_min", cfg.staging_ttl_min),
        "clipboard.files.staging_ttl_min",
    )
    cfg.chunk_size_kb = _as_int(
        _get(raw, "chunk_size_kb", "clipboard.files.chunk_size_kb", cfg.chunk_size_kb), "clipboard.files.chunk_size_kb"
    )
    cfg.verify_sha256 = _as_bool(
        _get(raw, "verify_sha256", "clipboard.files.verify_sha256", cfg.verify_sha256), "clipboard.files.verify_sha256"
    )
    return cfg


def _parse_clipboard_formats(raw: Dict[str, Any]) -> ClipboardFormatsConfig:
    cfg = ClipboardFormatsConfig()
    cfg.forward_all = _as_bool(
        _get(raw, "forward_all", "clipboard.formats.forward_all", cfg.forward_all), "clipboard.formats.forward_all"
    )
    cfg.exclude = _as_str_list(
        _get(raw, "exclude", "clipboard.formats.exclude", cfg.exclude), "clipboard.formats.exclude"
    )
    cfg.per_format_max_mb = _as_int(
        _get(raw, "per_format_max_mb", "clipboard.formats.per_format_max_mb", cfg.per_format_max_mb),
        "clipboard.formats.per_format_max_mb",
    )
    cfg.compress = _as_bool(
        _get(raw, "compress", "clipboard.formats.compress", cfg.compress), "clipboard.formats.compress"
    )
    cfg.inline_html_refs = _as_bool(
        _get(raw, "inline_html_refs", "clipboard.formats.inline_html_refs", cfg.inline_html_refs),
        "clipboard.formats.inline_html_refs",
    )
    cfg.ole_finish = _as_bool(
        _get(raw, "ole_finish", "clipboard.formats.ole_finish", cfg.ole_finish),
        "clipboard.formats.ole_finish",
    )
    cfg.exclude_by_process = _as_exclude_rules(
        _get(
            raw,
            "exclude_by_process",
            "clipboard.formats.exclude_by_process",
            cfg.exclude_by_process,
        )
    )
    cfg.allow_private = _as_str_list(
        _get(raw, "allow_private", "clipboard.formats.allow_private", cfg.allow_private), "clipboard.formats.allow_private"
    )
    cfg.paste_priority = _as_str_list(
        _get(raw, "paste_priority", "clipboard.formats.paste_priority", cfg.paste_priority),
        "clipboard.formats.paste_priority",
    )
    return cfg


def _parse_clipboard(raw: Dict[str, Any]) -> ClipboardConfig:
    cfg = ClipboardConfig()
    cfg.enable = _as_bool(_get(raw, "enable", "clipboard.enable", cfg.enable), "clipboard.enable")
    for name in ("sync_text", "sync_rtf", "sync_html", "sync_image", "sync_files"):
        setattr(cfg, name, _as_bool(_get(raw, name, "clipboard.%s" % name, getattr(cfg, name)), "clipboard.%s" % name))
    cfg.debounce_ms = _as_int(_get(raw, "debounce_ms", "clipboard.debounce_ms", cfg.debounce_ms), "clipboard.debounce_ms")
    cfg.max_payload_mb = _as_int(
        _get(raw, "max_payload_mb", "clipboard.max_payload_mb", cfg.max_payload_mb), "clipboard.max_payload_mb"
    )
    cfg.open_retry_ms = [
        _as_int(item, "clipboard.open_retry_ms[]")
        for item in _get(raw, "open_retry_ms", "clipboard.open_retry_ms", cfg.open_retry_ms)
    ]
    cfg.files = _parse_clipboard_files(raw.get("files", {}) or {})
    cfg.formats = _parse_clipboard_formats(raw.get("formats", {}) or {})
    return cfg


def _parse_ui(raw: Dict[str, Any]) -> UiConfig:
    cfg = UiConfig()
    cfg.tray = _as_bool(_get(raw, "tray", "ui.tray", cfg.tray), "ui.tray")
    cfg.notify = _as_bool(_get(raw, "notify", "ui.notify", cfg.notify), "ui.notify")
    return cfg


def _parse_security(raw: Dict[str, Any]) -> SecurityConfig:
    cfg = SecurityConfig()
    cfg.psk = _as_str(_get(raw, "psk", "security.psk", cfg.psk), "security.psk")
    return cfg


def _parse_logging(raw: Dict[str, Any]) -> LoggingConfig:
    cfg = LoggingConfig()
    cfg.level = _as_str(_get(raw, "level", "logging.level", cfg.level), "logging.level").upper()
    cfg.file = _as_str(_get(raw, "file", "logging.file", cfg.file), "logging.file")
    cfg.max_mb = _as_int(_get(raw, "max_mb", "logging.max_mb", cfg.max_mb), "logging.max_mb")
    cfg.backups = _as_int(_get(raw, "backups", "logging.backups", cfg.backups), "logging.backups")
    return cfg


def _parse_debug(raw: Dict[str, Any]) -> DebugConfig:
    cfg = DebugConfig()
    cfg.mouse = _as_bool(_get(raw, "mouse", "debug.mouse", cfg.mouse), "debug.mouse")
    cfg.mouse_sample_every = _as_int(
        _get(raw, "mouse_sample_every", "debug.mouse_sample_every", cfg.mouse_sample_every),
        "debug.mouse_sample_every",
    )
    cfg.mouse_file = _as_str(_get(raw, "mouse_file", "debug.mouse_file", cfg.mouse_file), "debug.mouse_file")
    cfg.client_sample_ms = _as_int(
        _get(raw, "client_sample_ms", "debug.client_sample_ms", cfg.client_sample_ms),
        "debug.client_sample_ms",
    )
    cfg.chain_lines = _as_int(
        _get(raw, "chain_lines", "debug.chain_lines", cfg.chain_lines),
        "debug.chain_lines",
    )
    return cfg


def from_dict(raw: Dict[str, Any], source_path: Optional[Path] = None) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("配置根节点必须是表")
    known = {"device", "network", "layout", "input", "clipboard", "ui", "security", "logging", "debug"}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ConfigError("未知的配置段: %s（可用: %s）" % (", ".join(unknown), ", ".join(sorted(known))))
    cfg = Config(
        device=_parse_device(raw.get("device", {}) or {}),
        network=_parse_network(raw.get("network", {}) or {}),
        layout=_parse_layout(raw.get("layout", {}) or {}),
        input=_parse_input(raw.get("input", {}) or {}),
        clipboard=_parse_clipboard(raw.get("clipboard", {}) or {}),
        ui=_parse_ui(raw.get("ui", {}) or {}),
        security=_parse_security(raw.get("security", {}) or {}),
        logging=_parse_logging(raw.get("logging", {}) or {}),
        debug=_parse_debug(raw.get("debug", {}) or {}),
        source_path=source_path,
    )
    cfg.validate()
    return cfg


def load(path: "os.PathLike[str] | str") -> Config:  # type: ignore[valid-type]
    p = Path(path)
    if not p.is_file():
        raise ConfigError("配置文件不存在: %s" % p)
    try:
        raw = load_toml(str(p))
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError("解析 %s 失败: %s" % (p, exc)) from None
    return from_dict(raw, source_path=p)


def find_config(explicit: Optional[str] = None) -> Path:
    """按优先级定位配置文件。"""
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            raise ConfigError("--config 指定的文件不存在: %s" % p)
        return p
    env = os.environ.get("NETCLIP_CONFIG")
    if env:
        p = Path(env)
        if not p.is_file():
            raise ConfigError("NETCLIP_CONFIG 指定的文件不存在: %s" % p)
        return p
    here = app_dir()
    for candidate in (here / "config.toml", Path.cwd() / "config.toml"):
        if candidate.is_file():
            return candidate
    raise ConfigError(
        "找不到 config.toml。请复制 config.example.toml 为 config.toml，或用 --config 指定路径。"
    )
