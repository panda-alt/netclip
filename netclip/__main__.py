"""netclip 命令行入口。

常用::

    python -m netclip                     # 用程序目录下的 config.toml 启动
    python -m netclip --config a.toml     # 指定配置
    python -m netclip --check             # 只校验配置并打印解析结果
    python -m netclip --dump-formats      # 打印当前剪贴板的全部格式（诊断 Office/MathType）
    python -m netclip --gen-config        # 生成默认 config.toml
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time
from typing import List, Optional

from . import __version__
from .config import Config, ConfigError, find_config, from_dict, load, resource_path
from .log import setup_logging
from .protocol import Frame

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2

#: `main()` 里设置 DPI 感知的结果，等日志系统就绪后再写进日志
_DPI_NOTE = ""


def _log_geometry_facts(log) -> None:
    """把几何相关的"环境事实"一次性打进日志。

    这几行是排查鼠标问题时的第一现场：DPI 感知级别、缩放比、主屏/虚拟桌面尺寸。
    真机上就出现过"进程报 1280x800 而显示器其实是 1920x1200"（差 1.5 倍缩放）
    的情况，只凭现象完全看不出来，必须把这些原始数字留档。
    """
    if sys.platform != "win32":  # pragma: no cover - 仅 Windows
        return
    from .win import winapi as w

    sx, sy, sw, sh = w.get_screen_rect()
    vx, vy, vw, vh = w.get_virtual_screen_rect()
    try:
        dpi = int(w.user32.GetDpiForSystem())
    except Exception:  # pragma: no cover
        dpi = 0
    log.info("DPI 感知: %s", _DPI_NOTE or "未设置")
    log.info(
        "屏幕(进程视角): 主屏 %dx%d @ (%d,%d) | 虚拟桌面 %dx%d @ (%d,%d) | 系统 DPI %d (%.2fx) | 显示器数 %d",
        sw,
        sh,
        sx,
        sy,
        vw,
        vh,
        vx,
        vy,
        dpi,
        dpi / 96.0 if dpi else 0.0,
        w.monitor_count(),
    )


def _log_privilege_fact(log) -> None:
    """记下本进程的权限级别。

    **这是"鼠标碰到任务管理器就卡住"的第一现场证据。** Windows 的 UIPI 规定，
    未提权进程的低级钩子/`SendInput` 对已提权窗口无效 —— 钩子被系统屏蔽后我们
    根本收不到事件，转发链从源头断开，看起来就是"卡住"。这是系统限制（Deskflow
    #8611、QQ 远程桌面同样如此），唯一解法是 netclip 自己提权运行。
    把级别写进日志，下次遇到这个现象就不用再去翻鼠标算法了。
    """
    if sys.platform != "win32":  # pragma: no cover - 仅 Windows
        return
    from .win import winapi as w

    try:
        elevated = w.is_elevated()
    except Exception as exc:  # pragma: no cover
        log.info("权限级别: 读取失败（%s）", exc)
        return
    if elevated:
        log.info("权限级别: 管理员（高完整性）—— 可控制任务管理器等提权窗口")
    else:
        log.info(
            "权限级别: 普通用户 —— 受 Windows UIPI 限制，任务管理器等**提权窗口**收不到"
            "转发的鼠标/键盘（现象：移到那个窗口上就卡住）。需要控制它们时用 start.bat "
            "第 10 项以管理员重启，或用第 9 项安装【最高权限】自启。"
        )


def _files_ready(holder: dict, paths: "list[str]") -> None:
    """文件通道传完后，把落盘的本地文件路径放回剪贴板。

    做成模块级函数而不是闭包，是因为闭包会捕获尚未构造完成的 `clipboard_sync`；
    这里通过可变容器 `holder` 延迟取值，避免"引用还没赋值"的时序问题。
    """
    sync = holder.get("sync")
    if sync is None:
        return
    sync.on_files_ready(paths)


def _setup_console() -> None:
    """Windows 中文控制台默认 GBK，输出里带 ✓ 之类的字符会直接抛异常。"""
    if sys.platform == "win32":
        import ctypes

        try:
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:  # pragma: no cover
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):  # pragma: no cover
            pass


# --------------------------------------------------------------------- 子命令


def cmd_check(cfg: Config) -> int:
    print("配置解析成功")
    print(cfg.describe())
    ports = cfg.network.ports()
    for name, (local, peer) in ports.items():
        print("  %-6s 本地监听 %d  ->  对端 %d" % (name, local, peer))
    print("\n几何预演:")
    from .core.session import Session

    layout = Session._build_layout(cfg, None)  # noqa: SLF001 - 诊断用途
    print("  " + layout.describe())
    return EXIT_OK


def cmd_dump_formats(cfg: Optional[Config], keep: bool) -> int:
    """打印当前剪贴板的所有格式及大小。

    排查 Office / MathType 兼容问题的第一把手：在两台机器上各复制一次同样的内容，
    各跑一次这个命令，对比格式清单就知道差在哪一种格式上。
    """
    from .win import clipboard as cb

    max_bytes = 512 * 1024 * 1024 if cfg is None else cfg.clipboard.formats.per_format_max_mb * 1024 * 1024
    print("=== 当前剪贴板内容 ===")
    snapshot = cb.capture(max_per_format=max_bytes)
    print("序号: %d" % snapshot.sequence)
    print("文字预览: %s" % (snapshot.text_preview or "<无>"))
    print("共 %d 种格式，合计 %.1f KB\n" % (len(snapshot.items), snapshot.total_size() / 1024.0))

    width = max([len(i.name) for i in snapshot.items] + [10])
    print("%-*s  %-6s  %12s" % (width, "格式名", "类别", "大小(字节)"))
    print("-" * (width + 26))
    for item in sorted(snapshot.items, key=lambda i: -i.size):
        print("%-*s  %-6s  %12d" % (width, item.name, item.category, item.size))

    if snapshot.skipped:
        print("\n跳过的格式:")
        for name, reason in snapshot.skipped:
            print("  %-*s  %s" % (width, name, reason))

    _dump_file_formats(snapshot)

    if keep:
        print("\n内容已保留在剪贴板中")
    return EXIT_OK


def _dump_file_formats(snapshot) -> None:
    """把"文件类格式"展开成人能看懂的样子。

    **这是排查"粘贴不了"的关键信息。** 文件剪贴板能不能粘贴，取决于三件事：
    路径在不在、`Shell IDList Array` 结构对不对、`Preferred DropEffect` 是不是"复制"。
    只看格式名和大小完全看不出来，所以这里逐个解开。
    """
    from .win import clipboard as cb

    #: 按**名字**挑，不按类别 —— `Preferred DropEffect` 的类别被判成 other，
    #: 用类别筛会正好漏掉最关键的它。
    wanted = {cb.HDROP_FORMAT, cb.PREFERRED_DROPEFFECT_FORMAT}
    files = [i for i in snapshot.items if i.category == cb.CAT_FILES or i.name in wanted]
    if not files:
        return
    print("\n文件类格式详情:")
    for item in files:
        if item.name == cb.HDROP_FORMAT:
            paths = cb.parse_hdrop_paths(item.data)
            print("  CF_HDROP: %d 个路径" % len(paths))
            for path in paths:
                exists = os.path.isfile(path)
                print("    %s  [%s]" % (path, "存在" if exists else "★不存在，粘贴必然失败★"))
            header = item.data[:20]
            pfiles = int.from_bytes(header[0:4], "little", signed=True) if len(header) >= 4 else -1
            # `fWide` 是 BOOL，真值既可能是 1，也可能是 -1(0xFFFFFFFF)。
            # 真机实测：PowerShell 的 `Set-Clipboard -Path` 写的就是 **-1**。
            # 按无符号打印会变成 4294967295，看着像坏数据，其实完全合法 ——
            # 有意义的只是"零 / 非零"。别让诊断工具自己制造假警报。
            fwide = int.from_bytes(header[16:20], "little", signed=True) if len(header) >= 20 else 0
            print(
                "    DROPFILES: pFiles=%s  fWide=%s"
                % (
                    pfiles if pfiles == 20 else "%d ★应为 20（文件名从偏移 20 字节处开始）★" % pfiles,
                    "宽字符 UTF-16（正常）" if fwide else "★0 = ANSI，中文路径会乱码★",
                )
            )
        elif item.name == cb.PREFERRED_DROPEFFECT_FORMAT:
            value = int.from_bytes(item.data[:4], "little") if len(item.data) >= 4 else -1
            label = {0: "NONE(不粘贴)", 1: "COPY", 2: "MOVE", 4: "LINK"}.get(value, "未知")
            print(
                "  Preferred DropEffect: %d (%s) 共 %d 字节  %s"
                % (
                    value,
                    label,
                    len(item.data),
                    "正常" if value == cb.DROPEFFECT_COPY and len(item.data) >= 4 else "★应为 COPY(1) 且 4 字节★",
                )
            )
        else:
            preview = item.data[:16].hex(" ")
            print("  %s: %d 字节  首字节 %s" % (item.name, len(item.data), preview))
    print(
        "\n提示：粘贴文件至少要 CF_HDROP（路径必须存在）。Preferred DropEffect=COPY 用来"
        "声明【这是复制而不是移动】；实测缺省时 Explorer 仍按复制处理。"
    )


def cmd_gen_config(path: str, force: bool) -> int:
    from pathlib import Path

    from .config import app_dir

    target = Path(path)
    if not target.is_absolute():
        #: 相对路径按**程序目录**解析，而不是当前工作目录 —— 这样无论从哪里调用
        #: （快捷方式、计划任务、别的目录里的命令行），生成的 config.toml 都落在
        #: exe / 仓库旁边，和 `find_config()` 找的位置一致。
        #: 打包成 exe 之后这一条尤其重要：用户是在资源管理器里双击的，
        #: cwd 可能是任何地方。
        target = app_dir() / target
    if target.exists() and not force:
        print("文件已存在: %s（加 --force 覆盖）" % target, file=sys.stderr)
        return EXIT_ERROR
    source = resource_path("config.example.toml")
    if not source.is_file():
        # 打包后 example 可能不在旁边，退化成内置的最小配置
        target.write_text(_MINIMAL_CONFIG, encoding="utf-8")
    else:
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    print("已生成 %s" % target)
    print("请至少修改 network.peer_ip 和 security.psk，然后运行 python -m netclip")
    return EXIT_OK


_MINIMAL_CONFIG = """\
[device]
name = ""

[network]
peer_ip = "192.168.1.42"

[layout]
peer_position = "right"

[security]
psk = "change-me-please"
"""


# --------------------------------------------------------------------- 主运行


def cmd_run(args: argparse.Namespace, cfg: Config) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):  # pragma: no cover
            pass

    log_path = None if args.no_log_file else _resolve_log_path(cfg)
    log = setup_logging(
        args.log_level or cfg.logging.level,
        log_path,
        cfg.logging.max_mb * 1024 * 1024,
        cfg.logging.backups,
    )
    log.info("netclip %s 启动", __version__)
    _log_geometry_facts(log)
    _log_privilege_fact(log)
    if args.port_offset:
        log.info(
            "端口已整体偏移 %+d: listen=%d clip=%d file=%d peer=%d",
            args.port_offset,
            cfg.network.listen_port,
            cfg.network.clip_port,
            cfg.network.file_port,
            cfg.network.peer_port,
        )
    log.info("\n%s", cfg.describe())

    if not cfg.network.peer_ip:
        log.warning(
            "未配置 network.peer_ip：本机只会监听、不会主动连接。"
            "对端如果配了本机地址，仍可以连进来。填上对端 IP 才能主动连。"
        )
    if cfg.security.psk == "change-me-please":
        log.warning("security.psk 仍是默认值！请改成两台机器一致的随机字符串，否则可能连到别人的 netclip。")

    from .clipsync.factory import build_clipboard_sync, build_files_transfer
    from .core.session import Session

    session: Optional[Session] = None

    def send_clip(mtype: int, body: dict, blob: bytes, _coalesce: object = None) -> None:
        if session is not None and session.manager is not None:
            session.manager.send_threadsafe("clip", Frame(mtype, body, blob))

    def send_file(mtype: int, body: dict, blob: bytes) -> None:
        if session is not None and session.manager is not None:
            session.manager.send_threadsafe("file", Frame(mtype, body, blob))

    clipboard_holder: dict = {"sync": None}
    tray_holder: dict = {"ctl": None}

    def notify(level: str, text: str) -> None:
        ctl = tray_holder.get("ctl")
        if ctl is not None:
            ctl.notify(level, text)
            return
        (log.warning if level in ("warn", "warning") else log.info)("%s", text)

    # 文件传输先建，因为剪贴板同步要拿它做阈值决策
    files_transfer = build_files_transfer(
        cfg,
        send_file=send_file,
        on_files_ready=lambda paths: _files_ready(clipboard_holder, paths),
        notify=notify,
        peer_name=cfg.device.resolved_name(),
        yield_fn=lambda: session.yield_to_input() if session is not None else None,
    )

    clipboard_sync = build_clipboard_sync(cfg, send_clip, notify=notify, files_handler=files_transfer)
    clipboard_holder["sync"] = clipboard_sync

    session = Session(cfg, clipboard_sync=clipboard_sync, files=files_transfer)
    if clipboard_sync is not None:
        # 让剪贴板同步能把"真的要传文件"这件事交给会话去调度
        clipboard_sync.start_transfer = session.start_file_transfer
    exit_code = EXIT_OK

    def request_stop(_signum: object = None, _frame: object = None) -> None:
        session.stop("收到退出信号")

    try:
        session.start()
    except Exception as exc:
        log.exception("启动失败: %s", exc)
        return EXIT_ERROR

    # Ctrl+C / 控制台关闭时把控制权收回来再退出，避免"鼠标卡在对端"
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, request_stop)
        except (ValueError, OSError):  # pragma: no cover
            pass

    if args.seconds:
        log.info("%.0f 秒后自动退出（--seconds）", args.seconds)

    log.info("运行中。Ctrl+C 退出。热键: 回拉=%s 暂停/恢复=%s", cfg.input.hotkey_recapture, cfg.input.hotkey_toggle)

    # ---- 托盘 ----
    #
    # 必须在**启动会话之前**检查托盘可用性：如果用户配置里开了托盘但系统建不出来
    # （比如被安全软件拦了），应该只告警不影响主功能，而不是启动完再抛异常。
    tray_ctl = None
    if cfg.ui.tray and not args.no_tray:
        from .core.tray import TrayController

        tray_ctl = TrayController(
            session,
            quit_callback=lambda: session.stop("托盘退出"),
            log_path=log_path or "",
            letter=(cfg.device.resolved_name() or "N")[:1].upper(),
        )
        if not tray_ctl.create():
            log.warning("系统托盘不可用，改为纯后台运行（Ctrl+C 退出）")
            tray_ctl = None
        else:
            tray_holder["ctl"] = tray_ctl
    elif args.no_tray:
        log.info("已按 --no-tray 禁用系统托盘")

    try:
        _main_loop(session, log, args, tray=(tray_ctl.window if tray_ctl is not None else None))
    except KeyboardInterrupt:  # pragma: no cover
        pass
    except Exception as exc:
        log.exception("运行期异常: %s", exc)
        exit_code = EXIT_ERROR
    finally:
        if tray_ctl is not None:
            tray_ctl.destroy()
        session.stop("正常退出")
    log.info("已退出")
    return exit_code


def _resolve_log_path(cfg: Config) -> str:
    from pathlib import Path

    configured = cfg.logging.file
    if not configured:
        return ""
    path = Path(configured)
    if path.is_absolute():
        return str(path)
    base = Path(cfg.source_path).resolve().parent if cfg.source_path else Path.cwd()
    return str(base / path)


def _main_loop(
    session: object,
    log: object,
    args: argparse.Namespace,
    tray: object = None,
) -> None:
    """主线程循环。

    主线程**不做输入处理**（钩子和网络都在各自的线程里）。这里保留一个轻量循环的
    目的有两个：一是让 Ctrl+C 能被及时处理，二是承载**托盘图标的窗口消息**
    —— `Shell_NotifyIcon` 的回调消息和右键菜单都必须在这个线程里处理。

    用 `PeekMessage` 而不是 `GetMessage`，是为了让循环能周期性检查退出条件。

    退出条件：`--seconds` 到期，或者会话已经停止（含托盘/信号触发的退出）。
    """
    from .win import winapi as w

    deadline = time.time() + float(args.seconds) if args.seconds else None
    status_interval = float(args.status_interval)
    next_status = time.time() + status_interval

    try:
        import ctypes

        msg = w.MSG()
        while not session.stopping:  # type: ignore[attr-defined]
            if deadline is not None and time.time() >= deadline:
                break
            # 抽干主线程消息队列。
            # 托盘窗口的消息交给它自己的窗口过程；其余消息正常派发。
            while w.user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, w.PM_REMOVE):
                handled = False
                if tray is not None:
                    try:
                        handled = tray.handle_message(msg)
                    except Exception:  # pragma: no cover - 托盘不能搞崩主循环
                        log.exception("托盘消息处理失败")  # type: ignore[attr-defined]
                        handled = False
                if not handled:
                    w.user32.TranslateMessage(ctypes.byref(msg))
                    w.user32.DispatchMessageW(ctypes.byref(msg))
            if status_interval > 0 and time.time() >= next_status:
                next_status = time.time() + status_interval
                log.info("状态: %s", session.status())  # type: ignore[attr-defined]
            time.sleep(0.05)
    except KeyboardInterrupt:  # pragma: no cover
        pass


# --------------------------------------------------------------------- 入口


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="netclip",
        description="在两台 Windows 电脑之间共享鼠标/键盘并同步剪贴板",
    )
    parser.add_argument("--version", action="version", version="netclip %s" % __version__)
    parser.add_argument("--config", "-c", default=None, help="配置文件路径（默认找程序目录下的 config.toml）")
    parser.add_argument("--log-level", default=None, help="覆盖日志级别: DEBUG/INFO/WARNING/ERROR")
    parser.add_argument("--no-log-file", action="store_true", help="不写日志文件，只输出到控制台")
    parser.add_argument("--no-tray", action="store_true", help="禁用系统托盘图标")
    parser.add_argument("--check", action="store_true", help="只校验配置并打印解析结果，然后退出")
    parser.add_argument("--gen-config", nargs="?", const="config.toml", default=None, metavar="PATH", help="生成默认配置文件")
    parser.add_argument("--force", action="store_true", help="--gen-config 时覆盖已存在的文件")
    parser.add_argument("--dump-formats", action="store_true", help="打印当前剪贴板的所有格式（诊断用）")
    parser.add_argument("--keep", action="store_true", help="--dump-formats 时不要动剪贴板内容")
    parser.add_argument(
        "--selftest",
        nargs=argparse.REMAINDER,
        default=None,
        metavar="NAME",
        help="运行自检，后面的参数原样转给 selftest，例如 --selftest keys --seconds 8",
    )
    parser.add_argument("--seconds", type=float, default=0.0, help="运行指定秒数后自动退出（0=一直运行）")
    parser.add_argument("--status-interval", type=float, default=60.0, help="打印状态行的间隔秒数（0=不打印）")
    parser.add_argument(
        "--port-offset",
        type=int,
        default=0,
        help="把监听端口和对端端口整体偏移这么多（同一台机器上跑两个实例做联调时用）",
    )
    return parser


def _apply_port_offset(cfg: Config, offset: int) -> None:
    """把四个端口整体偏移。同一台机器上跑两个实例做联调时用。"""
    if not offset:
        return
    net = cfg.network
    net.listen_port += offset
    net.clip_port += offset
    net.file_port += offset
    net.peer_port += offset
    for label, port in (
        ("listen_port", net.listen_port),
        ("clip_port", net.clip_port),
        ("file_port", net.file_port),
        ("peer_port", net.peer_port),
    ):
        if not (1 <= port <= 65535):
            raise ConfigError("--port-offset 把 network.%s 推到了非法端口 %d" % (label, port))


def main(argv: "Optional[List[str]]" = None) -> int:
    _setup_console()
    #: **必须在任何窗口/DC 创建之前**设定 DPI 感知，否则整个坐标系会被
    #: Windows 虚拟化（150% 缩放下 1920x1200 会报成 1280x800），
    #: 屏幕边界、"穿越"判定和告诉对端的分辨率会一起错。
    #: 详见 `netclip.win.winapi.ensure_dpi_awareness`。
    global _DPI_NOTE
    if sys.platform == "win32":
        from .win import winapi as w

        _DPI_NOTE = w.ensure_dpi_awareness()

    parser = build_parser()
    args = parser.parse_args(argv)

    if sys.platform != "win32":
        print("netclip 目前只支持 Windows。", file=sys.stderr)
        return EXIT_CONFIG

    if args.gen_config is not None:
        return cmd_gen_config(args.gen_config, args.force)

    #: 打包成 exe 之后没法 `python -m netclip.selftest`，所以主程序里也留一个入口。
    #: 用 `REMAINDER` 是为了让 `--selftest keys --seconds 8` 里的参数原样透传，
    #: 不用在这里再抄一份 argparse 定义（抄一份就一定会漂移）。
    if args.selftest is not None:
        from . import selftest

        return selftest.main(list(args.selftest))

    # 加载配置。--dump-formats 允许没有配置文件（它只是读剪贴板）。
    cfg: Optional[Config] = None
    try:
        cfg = load(find_config(args.config))
    except ConfigError as exc:
        if args.dump_formats:
            print("提示: 未加载到配置（%s），--dump-formats 将使用默认上限。" % exc, file=sys.stderr)
        else:
            print("配置错误: %s" % exc, file=sys.stderr)
            return EXIT_CONFIG

    if args.dump_formats:
        return cmd_dump_formats(cfg, args.keep)
    if args.check:
        assert cfg is not None
        return cmd_check(cfg)
    if cfg is None:
        print("没有可用的配置，无法启动。", file=sys.stderr)
        return EXIT_CONFIG
    _apply_port_offset(cfg, args.port_offset)
    return cmd_run(args, cfg)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
