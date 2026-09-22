"""剪贴板读写的自检工具（不依赖 pytest，直接跑）。

用法::

    python -m netclip.selftest clipboard          # 剪贴板往返
    python -m netclip.selftest clipboard --keep    # 往返后保留测试内容
    python -m netclip.selftest env                 # 打印环境与依赖状态
    python -m netclip.selftest all

这个工具的存在意义：剪贴板 / Office / MathType 的兼容问题只能靠"复制一次、
看看两端格式表长什么样"来定位。把它做成零依赖的一行命令。
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
import threading
import zlib
from typing import List, Tuple

from . import __version__


def _ok(condition: bool, text: str) -> bool:
    print(("  [OK]   " if condition else "  [FAIL] ") + text)
    return condition


def _other_instance_running(cfg=None) -> "List[int]":
    """本机上有没有**别的 netclip 实例**在跑（按三个端口试连）。

    为什么自检要关心这个：`clipboard` / `clip-loop` / `files-loop` 都会往**系统
    剪贴板**里写东西，而正在运行的 netclip 会把它当成一次真实复制**同步到对端**。
    真机上撞过一次：`files-loop` 把自己的临时文件路径放上剪贴板，旁边那个 netclip
    立刻开始往对端传这几个文件，然后自检清理了临时目录 —— 对端日志里就是一句
    莫名其妙的 `FileNotFoundError`，而那是自检自己造成的。
    """
    import socket

    ports = []
    try:
        if cfg is None:
            from .config import find_config, load

            cfg = load(find_config(None))
        ports = [cfg.network.listen_port, cfg.network.clip_port, cfg.network.file_port]
    except Exception:  # pragma: no cover - 配置读不出来时用默认端口兜底
        ports = [24800, 24801, 24802]

    busy = []
    for port in ports:
        probe = socket.socket()
        probe.settimeout(0.2)
        try:
            if probe.connect_ex(("127.0.0.1", int(port))) == 0:
                busy.append(int(port))
        finally:
            probe.close()
    return busy


def _warn_if_other_instance(busy: "List[int]") -> None:
    if not busy:
        return
    print("  [注意] 检测到本机已有 netclip 在监听 %s —— 正在运行的实例会把这个自检" % busy)
    print("         写进剪贴板的内容当成一次真实复制同步给对端（自检放在剪贴板上的临时")
    print("         路径会被真的传过去，然后因为临时目录被清掉而报 FileNotFoundError）。")
    print("         要干净地跑这个自检，先退出那个实例；或者只当它是个提示。")


def cmd_warpguard(args: argparse.Namespace) -> int:
    """验证"我们自己移动光标"不会产生被转发的移动事件。

    针对的是一类**必须**避免的设计缺陷：`SetCursorPos` 让系统发出的是
    **不带注入标志**的 `WM_MOUSEMOVE`。如果钩子把它当成用户输入上报，
    两台机器就会互相把对方的位移转发回去：

        A 让 B 挪光标 -> B 的钩子产生真实移动事件 -> B 转发给 A
          -> A 注入位移 -> A 的钩子产生真实移动事件 -> A 又转发给 B ...

    现象就是"鼠标被吸在一个位置高频抖动、完全推不动"。

    这里直接验证修复：移动光标前记录上报的移动事件数，移动后确认没有增加。
    """
    import time

    from .win import winapi as w
    from .win.hooks import MOVE, HookThread, InputEvent

    print("=== 自身移动光标 / 回声防护自检 ===")
    passed = True
    events = {"moves": 0}

    def on_event(event: InputEvent) -> None:
        if event.kind == MOVE:
            events["moves"] += 1

    hook = HookThread(on_event, lambda _e: False, name="netclip-warpguard")
    if not hook.start():
        _ok(False, "钩子启动失败")
        return 1

    try:
        time.sleep(0.3)
        try:
            cx, cy = w.get_cursor_pos()
        except w.WinApiError:
            cx, cy = 100, 100

        for label, (tx, ty) in (
            ("第一次", (max(1, cx - 120), max(1, cy))),
            ("第二次", (min(3000, cx + 90), max(1, cy))),
            ("第三次", (max(1, cx), max(1, cy - 70))),
        ):
            before = events["moves"]
            # 完全模拟 Session._warp_cursor 的动作
            w.check(w.user32.SetCursorPos(int(tx), int(ty)), "SetCursorPos")
            hook.reset_mouse_tracking()
            hook.ignore_next_move()
            time.sleep(0.25)
            after = events["moves"]
            passed &= _ok(
                after == before,
                "%s SetCursorPos 之后没有上报移动事件（%d -> %d）" % (label, before, after),
            )

        if hook.stats.warp_settle_skipped >= 1:
            passed &= _ok(True, "忽略窗口确实拦下了事件（%d 条）" % hook.stats.warp_settle_skipped)
        else:
            # 不是失败：部分环境（无桌面会话、RDP、光标本来就在目标位置）下
            # `SetCursorPos` 根本不会产生移动事件，自然也没东西可拦。
            # 真正要保证的是上面三条"没有多余事件被上报"，那已经验证过了。
            print("  [说明] 本次没有产生可拦的移动事件，跳过计数断言")
        print("  钩子统计: %s" % hook.stats.summary())
    finally:
        hook.stop()

    print("\n结果:", "全部通过" if passed else "存在失败项")
    return 0 if passed else 1


def _setup_console() -> None:
    """把控制台切到 UTF-8。

    Windows 中文控制台默认是 GBK(936)，而我们的输出里有 ✓ 🎯 这类字符，
    不切编码会直接抛 UnicodeEncodeError 把自检搞崩。
    """
    if sys.platform == "win32":
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


# --------------------------------------------------------------------- env


def cmd_hooks(args: argparse.Namespace) -> int:
    """安装真实钩子，观察事件是否到达、回调是否够快。

    这个测试**会短暂接管鼠标和键盘**（默认 3 秒），随后立刻卸载。
    它验证的是最容易出问题的那一环：钩子有没有装成功、注入事件有没有被误判、
    回调耗时会不会触发 Windows 的 LowLevelHooksTimeout。
    """
    import time

    from .win.hooks import BUTTON, KEY, MOVE, WHEEL, HookThread, InputEvent

    duration = float(args.seconds)
    counts = {MOVE: 0, BUTTON: 0, WHEEL: 0, KEY: 0}

    def on_event(event: InputEvent) -> None:
        counts[event.kind] = counts.get(event.kind, 0) + 1

    def should_swallow(_event: InputEvent) -> bool:
        # 自检阶段不吞事件，避免用户在测试期间"失去鼠标"
        return False

    hook = HookThread(on_event, should_swallow, name="netclip-selftest")
    print("=== 钩子自检（%g 秒，请在此期间移动鼠标 / 敲键盘）===" % duration)
    if not hook.start():
        _ok(False, "钩子线程启动失败")
        return 1

    passed = _ok(getattr(hook, "_mouse_hook", None) is not None, "鼠标钩子已安装")
    passed &= _ok(getattr(hook, "_key_hook", None) is not None, "键盘钩子已安装")

    print("  请移动鼠标、点一下、滚一下、敲几个键……")
    deadline = time.time() + duration
    while time.time() < deadline:
        time.sleep(0.2)

    hook.stop()

    print("\n  统计: %s" % hook.stats.summary())
    passed &= _ok(counts[MOVE] > 0, "收到鼠标移动事件（%d 个）" % counts[MOVE])
    passed &= _ok(counts[KEY] > 0, "收到键盘事件（%d 个）" % counts[KEY])
    passed &= _ok(hook.stats.mouse_events > 0, "鼠标事件计数正常")
    peak_us = hook.stats.callback_ns_peak / 1000.0
    passed &= _ok(peak_us < 5000, "回调耗时未超过 5ms（实测峰值 %.0f us，Windows 超时阈值约 300ms）" % peak_us)

    if counts[BUTTON] == 0:
        print("  [提示] 没有捕获到鼠标按键（如果你刚才没点，忽略这条）")
    if counts[WHEEL] == 0:
        print("  [提示] 没有捕获到滚轮（如果你刚才没滚，忽略这条）")

    print("\n结果:", "全部通过" if passed else "存在失败项")
    return 0 if passed else 1


def cmd_inject(args: argparse.Namespace) -> int:
    """注入自检：移动鼠标并观察是否被自己的钩子正确识别为"已注入"。

    之所以要测这个：如果注入事件的识别失败，netclip 会把自己的注入当成真实输入
    再转发一次，形成死循环 —— 这是这类工具最经典的自伤 bug。
    """
    import time

    from .win import inject
    from .win.hooks import BUTTON, KEY, MOVE, WHEEL, HookThread, InputEvent

    seen_move = [0]
    seen_injected = [0]
    count_holder = {"n": 0}

    def on_event(_event: InputEvent) -> None:
        count_holder["n"] += 1
        if _event.kind == MOVE:
            seen_move[0] += 1

    hook = HookThread(on_event, lambda _e: False, name="netclip-inject-test")
    if not hook.start():
        return 1

    time.sleep(0.3)
    before = hook.stats.mouse_events
    inject.move_relative(7, 7)
    inject.move_relative(-7, -7)
    time.sleep(0.4)
    hook.stop()

    passed = _ok(True, "注入调用未抛异常")
    passed &= _ok(
        hook.stats.injected_skipped >= 2,
        "注入事件被识别为本机注入（跳过 %d 个，期望 >=2）" % hook.stats.injected_skipped,
    )
    print("  统计: %s" % hook.stats.summary())
    print("\n结果:", "全部通过" if passed else "存在失败项")
    return 0 if passed else 1


def cmd_loop(args: argparse.Namespace) -> int:
    """起完整的网络 + 钩子，但**不实际转发输入**，只统计。

    用途：在真正打开"共享输入"之前，先确认两端能连上、钩子能收到事件、
    帧能双向流动。这是最安全的端到端连通性验证。
    """
    import asyncio
    import time

    from .config import ConfigError, find_config, load
    from .log import setup_logging
    from .net.manager import NetManager
    from .protocol import Frame, MsgType
    from .win import inject
    from .win.hooks import BUTTON, KEY, MOVE, WHEEL, HookThread, InputEvent

    try:
        cfg = load(find_config(args.config))
    except ConfigError as exc:
        print("配置错误: %s" % exc, file=sys.stderr)
        return 2

    setup_logging(args.log_level or cfg.logging.level, None)
    print("=== netclip 连通性自检 ===")
    print(cfg.describe())
    print()

    counts = {MOVE: 0, BUTTON: 0, WHEEL: 0, KEY: 0}
    counters = {"sent": 0, "recv": 0}

    def on_event(event: InputEvent) -> None:
        counts[event.kind] = counts.get(event.kind, 0) + 1

    # --- 网络 ---
    loop = asyncio.new_event_loop()
    manager = NetManager(cfg)

    def on_frame(mtype: int, body: dict, _blob: bytes) -> None:
        counters["recv"] += 1
        if mtype == MsgType.MOUSE_MOVE:
            inject.move_relative(int(body.get("dx", 0)), int(body.get("dy", 0)))
        elif mtype == MsgType.PING:
            manager.send("input", Frame(MsgType.PONG, {}))

    for mtype in (MsgType.MOUSE_MOVE, MsgType.PING, MsgType.PONG, MsgType.ENTER, MsgType.LEAVE):
        manager.bind(mtype, on_frame)

    def run_net() -> None:
        asyncio.set_event_loop(loop)

        async def main() -> None:
            await manager.start()
            stop = asyncio.Event()

            async def ticker() -> None:
                seq = 0
                while not stop.is_set():
                    await asyncio.sleep(1.0)
                    seq += 1
                    if manager.is_up("input"):
                        manager.send("input", Frame(MsgType.MOUSE_MOVE, {"dx": 0, "dy": 0}))
                        counters["sent"] += 1
                    if seq % 5 == 0:
                        print("  [%s] %s | 输入事件 %s" % (time.strftime("%H:%M:%S"), manager.status_line(), counts))

            task = loop.create_task(ticker())
            await asyncio.sleep(float(args.seconds))
            stop.set()
            task.cancel()
            await manager.stop()

        try:
            loop.run_until_complete(main())
        except KeyboardInterrupt:
            pass
        finally:
            loop.close()

    net_thread = threading.Thread(target=run_net, name="netclip-selftest-net", daemon=True)
    net_thread.start()
    time.sleep(0.5)

    # --- 钩子 ---
    hook = HookThread(on_event, lambda _e: False, name="netclip-selftest")
    hook_ok = hook.start()
    print("钩子: %s" % ("已安装" if hook_ok else "安装失败"))
    print("等待 %g 秒……" % float(args.seconds))

    deadline = time.time() + float(args.seconds)
    while time.time() < deadline:
        time.sleep(0.5)

    hook.stop()
    net_thread.join(5.0)

    print()
    print("  %s" % manager.status_line())
    print("  钩子统计: %s" % hook.stats.summary())
    print("  本机输入事件: 移动=%d 按键=%d 按钮=%d 滚轮=%d" % (counts[MOVE], counts[KEY], counts[BUTTON], counts[WHEEL]))

    passed = _ok(hook_ok, "钩子安装成功")
    passed &= _ok(manager.is_up("input"), "input 通道已连通")
    passed &= _ok(manager.is_up("clip"), "clip 通道已连通")
    passed &= _ok(manager.is_up("file"), "file 通道已连通")
    passed &= _ok(counters["recv"] > 0, "收到对端帧（%d 个）" % counters["recv"])
    print("\n结果:", "全部通过" if passed else "存在失败项")
    return 0 if passed else 1


def cmd_keys(args: argparse.Namespace) -> int:
    """打印每一次按键的**原始** vk / 扫描码 / 扩展标志。

    这是"某个键在对端不对"这类问题的第一把手，和剪贴板问题用 `--dump-formats`
    是同一个思路：先看清**本机钩子上报的是什么**，再去对端看注入进去的是什么。

    为什么必须看这两个值而不是只看键名：

      * `vk` 只用来查表（归一化修饰键、判断是不是扩展键）；
      * **真正注入到对端的是 `scan`**（见 `win/inject.py`：用
        `KEYEVENTF_SCANCODE` 注入物理扫描码，这样不受目标机键盘布局影响）。

    所以典型故障长这样：钩子把左/右 Shift 都报成 `vk=0x10`，而 `scan` 才是
    `0x2A` / `0x36` 的区别。只要 `scan` 对，注入就对；`scan` 丢了才会退化成
    "右 Shift 变成了左 Shift"。
    """
    import time

    from .win.hooks import KEY, HookThread, InputEvent

    duration = float(args.seconds)
    rows: "list[InputEvent]" = []

    def on_event(event: InputEvent) -> None:
        if event.kind == KEY:
            rows.append(event.copy())

    def should_swallow(_event: InputEvent) -> bool:
        return False

    hook = HookThread(on_event, should_swallow, name="netclip-selftest-keys")
    print("=== 按键诊断（%g 秒）===" % duration)
    print("  请依次按：左 Shift、右 Shift、左 Ctrl、右 Ctrl、左 Alt、右 Alt")
    if not hook.start():
        _ok(False, "钩子线程启动失败")
        return 1

    deadline = time.time() + duration
    while time.time() < deadline:
        time.sleep(0.05)
    hook.stop()

    if not rows:
        _ok(False, "一个按键都没收到")
        return 1

    print("")
    print("  %-6s %-6s %-6s %-5s  %s" % ("vk", "scan", "状态", "ext", "识别为"))
    print("  " + "-" * 52)
    for event in rows:
        print(
            "  0x%02X   0x%02X   %-6s %-5s  %s"
            % (
                event.vk,
                event.scan,
                "按下" if event.down else "抬起",
                event.extended,
                _vk_hint(event.vk, event.scan),
            )
        )
    print("")
    print("  说明: 注入对端用的是 scan（物理扫描码，与键盘布局无关）。")
    print("        左/右 Shift 的 scan 分别是 0x2A / 0x36；两边都出现才说明都拿到了。")
    return 0


def _vk_hint(vk: int, scan: int) -> str:
    """把一个按键事件翻译成人看得懂的名字（只为诊断输出，不参与逻辑）。"""
    #: 左右成对的键**优先按扫描码区分**：钩子经常把两者报成同一个通用 vk，
    #: 光看 vk 会得出"左右不分"的错误结论。
    by_scan = {
        0x2A: "左 Shift",
        0x36: "右 Shift",
        0x1D: "左 Ctrl" if vk in (0x11, 0xA2) else "右 Ctrl",
        0x38: "左 Alt" if vk in (0x12, 0xA4) else "右 Alt",
        0x5B: "左 Win",
        0x5C: "右 Win",
    }
    if scan in by_scan:
        return by_scan[scan]
    if scan:
        return "vk=0x%02X scan=0x%02X" % (vk, scan)
    return "（无扫描码）vk=0x%02X" % vk


def cmd_clip_loop(args: argparse.Namespace) -> int:
    """剪贴板同步的本地环回自检（不需要第二台机器）。

    流程：往剪贴板写一组内容 -> 采集 -> 策略决策 -> 编码 -> **解码** -> 写回剪贴板
    -> 回读比对。就是把网络那一段换成内存，其余全是真实代码路径。

    这能验证：策略放行了哪些格式、压缩是否正确、分片切分是否对齐、
    私有格式的名字与字节有没有变形、写回时格式顺序对不对。
    不能验证的只有"网络传输"本身，那个用 `selftest loop` 覆盖。
    """
    from .clipsync.bridge import _decode_items, _public_meta, _split_blob, make_collect_filter
    from .clipsync.policy import ACTION_FULL, ACTION_SKIP, SyncPolicy

    from .win import clipboard as cb

    print("=== 剪贴板同步环回自检 ===")
    _warn_if_other_instance(_other_instance_running())
    passed = True

    policy = SyncPolicy(
        enabled=True,
        max_payload_mb=8,
        per_format_max_mb=16,
        forward_all=True,
        #: 不排除任何格式 —— 和默认配置保持一致。曾经这里排除了
        #: `DataObject` / `Ole Private Data` / `Link Source.*`，那会让 WPS 粘不了。
    )

    created: List[str] = []
    tmpdir = None
    try:
        # 造几个文件给 CF_HDROP 用
        import struct
        import tempfile

        tmpdir = tempfile.mkdtemp(prefix="netclip-loop-")
        f1 = os.path.join(tmpdir, "hello.txt")
        f2 = os.path.join(tmpdir, "中文文件.txt")
        with open(f1, "w", encoding="utf-8") as fh:
            fh.write("netclip 文件同步测试")
        with open(f2, "w", encoding="utf-8") as fh:
            fh.write("second file")
        created = [f1, f2]

        hdrop_payload = b"".join(p.encode("utf-16-le") + b"\x00\x00" for p in created) + b"\x00\x00"
        hdrop = struct.pack("<IiiII", 20, 0, 0, 0, 1) + hdrop_payload

        text = "netclip 环回测试 ✓ 中文 🎯"
        html = (
            "Version:0.9\r\nStartHTML:0000000097\r\nEndHTML:0000000240\r\n"
            "StartFragment:0000000131\r\nEndFragment:0000000204\r\n"
            "<html><body><!--StartFragment--><b>netclip</b> 环回<!--EndFragment--></body></html>"
        ).encode("utf-8")
        # 一段高压缩比的"假 DIB"，用于验证压缩与切分
        dib = b"BM" + b"\x00" * 64 + bytes(range(256)) * 8
        private = b"\x01\x02\x03" + os.urandom(64)

        seed = [
            cb.FormatBlob(name="CF_UNICODETEXT", category=cb.CAT_TEXT, data=text.encode("utf-16-le")),
            cb.FormatBlob(name="HTML Format", category=cb.CAT_HTML, data=html),
            cb.FormatBlob(name="CF_DIB", category=cb.CAT_IMAGE, data=dib),
            cb.FormatBlob(name="netclip.loop.private", category=cb.CAT_OTHER, data=private),
            cb.FormatBlob(name="CF_HDROP", category=cb.CAT_FILES, data=hdrop),
        ]

        print("\n[1] 构造测试内容")
        seed_result = cb.write_formats(seed)
        passed &= _ok(len(seed_result.written) == len(seed), "写入测试内容: %s" % seed_result.describe())

        print("\n[2] 采集 + 策略决策")
        # 用与真实同步完全相同的过滤逻辑，否则自检结论不可信
        snapshot = cb.capture(
            max_per_format=policy.per_format_max_bytes,
            filter_fn=make_collect_filter(policy),
        )
        print("  采集到: %s" % snapshot.describe())
        decision = policy.decide(snapshot.items, skipped=snapshot.skipped)
        print("  决策: %s" % decision.describe())
        passed &= _ok(decision.action == ACTION_FULL, "决策为完整同步（实际 %s: %s）" % (decision.action, decision.reason))
        # 等价表示去重：CF_TEXT/CF_OEMTEXT/CF_LOCALE 不该出现在传输列表里
        sent_names = {s.name for s in decision.summaries}
        passed &= _ok("CF_UNICODETEXT" in sent_names, "CF_UNICODETEXT 已包含")
        passed &= _ok("CF_TEXT" not in sent_names, "冗余的 CF_TEXT 已去重")
        passed &= _ok("CF_OEMTEXT" not in sent_names, "冗余的 CF_OEMTEXT 已去重")
        passed &= _ok("CF_LOCALE" not in sent_names, "与机器绑定的 CF_LOCALE 已排除")

        print("\n[3] 编码（含 zlib 压缩）")
        encoded = []
        for item in decision.items:
            data = bytes(item.data)
            raw_len = len(data)
            compressed = False
            if raw_len >= 512:
                packed = zlib.compress(data, 6)
                if len(packed) < raw_len - 64:
                    data, compressed = packed, True
            encoded.append(
                {
                    "n": item.name,
                    "s": raw_len,
                    "c": item.category,
                    "z": compressed,
                    "wl": len(data),
                    "data": data,
                }
            )
        meta = [_public_meta(e) for e in encoded]
        blob = b"".join(e["data"] for e in encoded)
        total_raw = sum(e["s"] for e in encoded)
        print("  编码后 %d 段，原始 %.1f KB -> 上线 %.1f KB" % (len(meta), total_raw / 1024.0, len(blob) / 1024.0))
        passed &= _ok(len(meta) == len(decision.items), "每段都有元数据")

        print("\n[4] 解码 + 切分")
        items = _decode_items(meta, _split_blob(blob, meta))
        by_name = {i.name: i.data for i in items}
        passed &= _ok(
            by_name.get("CF_UNICODETEXT") == text.encode("utf-16-le"),
            "文本解码一致",
        )
        passed &= _ok(by_name.get("HTML Format") == html, "HTML 解码一致")
        passed &= _ok(by_name.get("CF_DIB") == dib, "二进制（走压缩路径）解码一致")
        passed &= _ok(by_name.get("netclip.loop.private") == private, "私有格式字节完全一致")
        passed &= _ok(by_name.get("CF_HDROP") == hdrop, "CF_HDROP 末尾终止符未丢失")
        passed &= _ok("CF_DIB" in by_name, "CF_DIB 在传输列表里")

        print("\n[5] 写回剪贴板并回读")
        restored = cb.write_formats(cb.order_for_paste(items, []))
        passed &= _ok(bool(restored), "写回成功: %s" % restored.describe())

        after = cb.capture(max_per_format=64 * 1024 * 1024)
        got_text = after.by_name("CF_UNICODETEXT")
        passed &= _ok(
            got_text is not None and got_text.data.decode("utf-16-le") == text,
            "回读文本一致",
        )
        got_private = after.by_name("netclip.loop.private")
        passed &= _ok(
            got_private is not None and got_private.data == private,
            "回读私有格式一致",
        )
        got_hdrop = after.by_name("CF_HDROP")
        passed &= _ok(
            got_hdrop is not None and got_hdrop.data == hdrop,
            "回读 CF_HDROP 一致",
        )

        print("\n[6] 超大内容应该只通知不传")
        big_policy = SyncPolicy(max_payload_mb=1, per_format_max_mb=16)
        big_decision = big_policy.decide(
            [cb.FormatBlob(name="CF_DIB", category=cb.CAT_IMAGE, data=b"\x00" * (2 * 1024 * 1024))]
        )
        passed &= _ok(big_decision.action == "announce", "2MB 内容在 1MB 上限下变成仅通知")

    finally:
        # 还原剪贴板（重新写回原来采集到的内容，或清空）
        try:
            cb.clear_clipboard()
        except Exception:
            pass
        if tmpdir:
            import shutil

            shutil.rmtree(tmpdir, ignore_errors=True)

    print("\n结果:", "全部通过" if passed else "存在失败项")
    return 0 if passed else 1


def cmd_files_loop(args: argparse.Namespace) -> int:
    """文件同步的本地环回自检（不需要第二台机器）。

    走的是真实代码路径，只把网络换成内存直连：

      剪贴板里放 CF_HDROP -> 采集解析出文件清单 -> 阈值决策 -> 分块发送
      -> 对端落盘 + SHA-256 校验 -> 用本地路径重建 CF_HDROP 放回剪贴板
      -> 再采集一次验证解析出来的正是落盘路径
    """
    import asyncio
    import shutil
    import struct
    import tempfile
    import zlib as _zlib  # noqa: F401 - 保持与其它自检一致的导入风格

    from .files.transfer import FilesTransfer, Staging
    from .win import clipboard as cb

    print("=== 文件同步环回自检 ===")
    _warn_if_other_instance(_other_instance_running())
    passed = True
    tmp = tempfile.mkdtemp(prefix="netclip-files-")
    original = cb.capture(max_per_format=64 * 1024 * 1024)

    try:
        # ---------- 造测试文件 ----------
        source_dir = os.path.join(tmp, "source")
        os.makedirs(source_dir, exist_ok=True)
        sizes = (1, 4096, 4097, 200_000, 1_500_000)
        sources = []
        for idx, size in enumerate(sizes):
            path = os.path.join(source_dir, "f%d.bin" % idx)
            with open(path, "wb") as fh:
                fh.write(os.urandom(size))
            sources.append(path)
        # 一个中文名文件，验证名字清洗与编码
        cn = os.path.join(source_dir, "中文 文件.txt")
        with open(cn, "w", encoding="utf-8") as fh:
            fh.write("netclip 文件同步 ✓")
        sources.append(cn)

        print("\n[1] 放进剪贴板并解析")
        hdrop = cb.build_hdrop(sources)
        cb.write_formats([cb.make_hdrop_blob(sources)])
        snap = cb.capture(max_per_format=1024 * 1024)
        item = snap.by_name("CF_HDROP")
        parsed = cb.parse_hdrop_paths(item.data) if item else []
        passed &= _ok(parsed == sources, "CF_HDROP 解析出 %d 个路径" % len(parsed))

        # ---------- 双端装配（内存链路） ----------
        recv_root = os.path.join(tmp, "recv")
        sender_staging = Staging(root=os.path.join(tmp, "send-stage"), peer="selftest")
        recv_staging = Staging(root=recv_root, peer="selftest")
        link = {"receiver": None, "handler": None}
        ready: List[str] = []

        def recv_send(mtype, body, blob):
            pass

        def send_from_sender(mtype, body, blob):
            if link["receiver"] is not None:
                link["receiver"].on_frame(mtype, body, blob)

        def send_from_receiver(mtype, body, blob):
            if sender is not None:
                sender.on_frame(mtype, body, blob)

        sender = FilesTransfer(
            send=send_from_sender,
            staging=sender_staging,
            chunk_size=64 * 1024,
            auto_transfer_max_bytes=50 * 1024 * 1024,
            over_threshold_action="transfer",
        )
        receiver = FilesTransfer(
            send=send_from_receiver,
            staging=recv_staging,
            chunk_size=64 * 1024,
            auto_transfer_max_bytes=50 * 1024 * 1024,
            over_threshold_action="transfer",
            on_files_ready=lambda paths: ready.extend(paths),
        )
        link["receiver"] = receiver

        print("\n[2] 阈值决策")
        plan = sender.plan(parsed, total_bytes=sum(os.path.getsize(p) for p in parsed))
        print("  %s" % plan.describe())
        passed &= _ok(plan.send, "小文件直接传")
        passed &= _ok(plan.count == len(sources), "清单包含全部 %d 个文件" % len(sources))

        print("\n[3] 分块传输 + 落盘 + 校验")
        sender.send_file_begin(plan)
        asyncio.run(sender.stream_files(plan))
        print("  发送端: %s" % sender.status())
        print("  接收端: %s" % receiver.status())
        passed &= _ok(len(ready) == len(sources), "全部 %d 个文件都已就位（实际 %d）" % (len(sources), len(ready)))
        passed &= _ok(receiver.stats["checksum_failed"] == 0, "没有校验失败")
        passed &= _ok(receiver.stats["verified"] == len(sources), "全部文件校验通过")

        print("\n[4] 逐字节比对")
        mismatch = []
        for src, dst in zip(sources, ready):
            if not os.path.isfile(dst):
                mismatch.append((src, dst, "目标不存在"))
                continue
            if os.path.getsize(src) != os.path.getsize(dst):
                mismatch.append((src, dst, "大小不同"))
                continue
            with open(src, "rb") as a, open(dst, "rb") as b:
                if a.read() != b.read():
                    mismatch.append((src, dst, "内容不同"))
        passed &= _ok(not mismatch, "内容逐字节一致（%d 个文件）" % len(sources))
        for src, dst, why in mismatch[:5]:
            print("         %s -> %s: %s" % (os.path.basename(src), dst, why))

        passed &= _ok(
            all(os.path.dirname(d).startswith(os.path.abspath(recv_root)) for d in ready),
            "所有文件都落在预期暂存目录内（没有路径穿越）",
        )
        passed &= _ok(
            any(os.path.basename(d) == "中文 文件.txt" for d in ready),
            "中文文件名被正确保留",
        )

        print("\n[5] 用落盘路径重建剪贴板（走生产同一条路径）")
        #: **必须调生产代码里的 `build_file_clipboard_items`**，不能图省事自己拼一个
        #: `make_hdrop_blob`。真机上就是这条路抛了
        #: `NameError: SHELL_IDLIST_FORMAT`（那个函数被同名旧版本覆盖了），而自检
        #: 因为绕开了它，一路绿灯 —— 自检不覆盖真正会崩的那段，等于没检。
        try:
            items = cb.build_file_clipboard_items(ready)
            passed &= _ok(True, "构造文件剪贴板格式成功（%s）" % "、".join(i.name for i in items))
        except Exception as exc:
            passed &= _ok(False, "构造文件剪贴板格式失败: %r" % (exc,))
            items = [cb.make_hdrop_blob(ready)]
        cb.write_formats(items)
        after = cb.capture(max_per_format=1024 * 1024)
        after_item = after.by_name("CF_HDROP")
        after_paths = cb.parse_hdrop_paths(after_item.data) if after_item else []
        passed &= _ok(after_paths == ready, "剪贴板里的路径就是落盘路径")
        passed &= _ok(all(os.path.isfile(p) for p in after_paths), "这些路径都真实存在（可粘贴）")

        #: `Preferred DropEffect` 缺了它（或值不对）Shell 会拒绝执行粘贴，
        #: 现象是"按钮能点、按 Ctrl+V 毫无反应"。所以这里连值一起验。
        effect = after.by_name(cb.PREFERRED_DROPEFFECT_FORMAT)
        effect_value = int.from_bytes(effect.data[:4], "little") if effect and len(effect.data) >= 4 else -1
        passed &= _ok(
            effect_value == cb.DROPEFFECT_COPY,
            "Preferred DropEffect = COPY(%d)，实际 %d" % (cb.DROPEFFECT_COPY, effect_value),
        )
        #: 落盘重建时**不该**出现 `Shell IDList Array`：自己造 CIDA 在真机上把
        #: 资源管理器搞崩过（README ⑥），只写 CF_HDROP 才是官方背书的做法。
        passed &= _ok(
            after.by_name("Shell IDList Array") is None,
            "没有自己造 Shell IDList Array",
        )

        print("\n[6] 超大文件按阈值策略被拦下")
        strict = FilesTransfer(
            send=lambda *a, **k: None,
            staging=Staging(root=os.path.join(tmp, "strict"), peer="selftest"),
            auto_transfer_max_bytes=1024,
            over_threshold_action="skip",
        )
        big_plan = strict.plan(parsed)
        passed &= _ok(not big_plan.send, "超过阈值时被拦下（%s）" % big_plan.reason)

    finally:
        if original.items:
            cb.write_formats(cb.order_for_paste(original.items, []))
        else:
            cb.clear_clipboard()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n结果:", "全部通过" if passed else "存在失败项")
    return 0 if passed else 1


def cmd_tray(args: argparse.Namespace) -> int:
    """托盘图标自检：创建图标、跑消息循环、销毁。

    验证的是最容易出问题的一环：`NOTIFYICONDATAW` 的结构布局。
    这个结构是变长的，`cbSize` 算错会被系统读越界（经典崩溃点），
    所以先断言大小，再真的把图标加进托盘。
    """
    import time

    from .win import tray as tray_mod
    from .win import winapi as w

    print("=== 托盘自检 ===")
    passed = True

    expected = 976  # Vista+ 的 NOTIFYICONDATAW 大小（x64）
    actual = ctypes.sizeof(w.NOTIFYICONDATAW)
    passed &= _ok(actual == expected, "NOTIFYICONDATAW 大小为 %d（期望 %d）" % (actual, expected))

    color = tray_mod.COLOR_ACTIVE

    #: 真图标这条路最容易在**打包之后**静默失效：`--add-data` 少了一条，
    #: 运行时就读不到 `.ico`，托盘悄悄退回手绘方块，界面上看不出是"坏了"。
    icon_path = tray_mod.default_icon_path()
    passed &= _ok(bool(icon_path), "找到图标文件 %s" % (tray_mod.ICON_RESOURCE,))
    if icon_path:
        print("  图标文件: %s" % icon_path)
        base = tray_mod.load_icon_frame(icon_path, tray_mod.TrayWindow.ICON_SIZE)
        passed &= _ok(
            base is not None and len(base) == tray_mod.TrayWindow.ICON_SIZE ** 2 * 4,
            "解出 %d×%d 的图标位图" % ((tray_mod.TrayWindow.ICON_SIZE,) * 2),
        )
    else:
        base = None

    hicon = tray_mod.make_icon_hicon(color, letter="N")
    passed &= _ok(bool(hicon), "用 GDI 画出了图标句柄")
    if base is not None:
        hicon_base = tray_mod.make_icon_hicon(color, base=base)
        passed &= _ok(bool(hicon_base), "带真图标画出了图标句柄")
        if hicon_base:
            w.user32.DestroyIcon(hicon_base)

    state = tray_mod.TrayState(enabled=True, input_up=True, clip_up=True, peer="selftest")
    print("  提示文本: %r" % state.tooltip())

    window = tray_mod.TrayWindow(lambda: state, on_command=lambda cmd: print("  命令: %s" % cmd), letter="N")
    created = window.create()
    passed &= _ok(created, "托盘窗口 + Shell_NotifyIcon 成功")
    if not created:
        print("  （如果你在无桌面会话/服务里运行，这一项失败是正常的）")
        return 1

    print("  图标已加入托盘，%g 秒后移除……" % float(args.seconds))
    deadline = time.time() + float(args.seconds)
    msg = w.MSG()
    while time.time() < deadline:
        while w.user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, w.PM_REMOVE):
            if not window.handle_message(msg):
                w.user32.TranslateMessage(ctypes.byref(msg))
                w.user32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.05)

    # 改一次状态，验证图标会跟着换色（这是个真实的功能点，不是装饰）
    state.remote = True
    window._refresh(force=True)  # noqa: SLF001 - 自检需要直接触发一次刷新
    passed &= _ok(window._last_icon_color == tray_mod.COLOR_REMOTE, "状态变化后图标颜色已切换")  # noqa: SLF001

    window.destroy()
    passed &= _ok(window.hwnd is None, "托盘已干净移除")

    print("\n结果:", "全部通过" if passed else "存在失败项")
    return 0 if passed else 1


def cmd_env(_args: argparse.Namespace) -> int:
    from ._compat import toml_backend

    from .win import winapi as w

    print("netclip %s" % __version__)
    print("Python      :", sys.version.replace("\n", " "))
    print("TOML 后端   :", toml_backend())
    print("屏幕(主屏)  :", w.get_screen_rect())
    print("虚拟桌面    :", w.get_virtual_screen_rect())
    print("显示器数量  :", w.monitor_count())
    print("剪贴板序号  :", w.user32.GetClipboardSequenceNumber())
    print("光标位置    :", w.get_cursor_pos())
    try:
        import PIL  # noqa: F401

        print("Pillow      : 可用")
    except ImportError:
        print("Pillow      : 不可用（图片格式转换会退化，但 DIB 直传不受影响）")
    return 0


# --------------------------------------------------------------------- clipboard


def _make_test_dib(width: int = 4, height: int = 4) -> bytes:
    """构造一个最小的 32bpp BI_RGB DIB（不依赖 Pillow）。"""
    header = ctypes.create_string_buffer(40)
    fields = [40, width, height, 1, 32, 0, width * height * 4, 0, 0, 0, 0]
    for idx, value in enumerate(fields):
        ctypes.memmove(ctypes.byref(header, idx * 4), ctypes.byref(ctypes.c_int32(value)), 4)
    pixels = b"".join(bytes([(x * 40) % 256, (y * 40) % 256, 128, 255]) for y in range(height) for x in range(width))
    return header.raw + pixels


def cmd_clipboard(args: argparse.Namespace) -> int:
    from .win import clipboard as cb

    print("=== netclip 剪贴板自检 ===")
    _warn_if_other_instance(_other_instance_running())
    passed = True

    # 1) 读取当前剪贴板（不改动它）
    print("\n[1] 读取当前剪贴板")
    before = cb.capture(max_per_format=8 * 1024 * 1024)
    print("  当前内容:", before.describe())
    if before.skipped:
        for name, reason in before.skipped:
            print("  跳过: %s -> %s" % (name, reason))

    # 2) 写入一组可控的测试数据
    print("\n[2] 写入测试内容")
    text = "netclip 自检 ✓ 中文与 emoji 🎯"
    html = (
        "Version:0.9\r\nStartHTML:0000000097\r\nEndHTML:0000000240\r\n"
        "StartFragment:0000000131\r\nEndFragment:0000000204\r\n"
        "<html><body><!--StartFragment--><b>netclip</b> 加粗文本<!--EndFragment--></body></html>"
    )
    items = [
        cb.FormatBlob(name="CF_UNICODETEXT", category=cb.CAT_TEXT, data=text.encode("utf-16-le")),
        cb.FormatBlob(name="HTML Format", category=cb.CAT_HTML, data=html.encode("utf-8")),
        cb.FormatBlob(name="netclip.selftest.blob", category=cb.CAT_OTHER, data=b"\x00\x01\x02netclip-test"),
    ]
    result = cb.write_formats(items)
    passed &= _ok(bool(result), "写入成功: %s" % result.describe())
    if result.failed:
        for name, reason in result.failed:
            print("         %s: %s" % (name, reason))

    # 3) 回读并逐项对比
    print("\n[3] 回读校验")
    after = cb.capture(max_per_format=8 * 1024 * 1024)
    print("  回读内容:", after.describe())

    got_text = after.by_name("CF_UNICODETEXT")
    if got_text:
        decoded = got_text.data.decode("utf-16-le", errors="replace")
        passed &= _ok(decoded == text, "Unicode 文本往返一致（得到 %r）" % decoded)
    else:
        passed &= _ok(False, "CF_UNICODETEXT 未回读成功")

    got_html = after.by_name("HTML Format")
    passed &= _ok(got_html is not None and got_html.data == html.encode("utf-8"), "HTML Format 往返一致")

    got_blob = after.by_name("netclip.selftest.blob")
    passed &= _ok(
        got_blob is not None and got_blob.data == b"\x00\x01\x02netclip-test",
        "自定义注册格式往返一致（验证跨机私有格式方案可行）",
    )

    # 4) DIB 往返（用构造的 4x4 图，不依赖剪贴板里原有内容）
    print("\n[4] DIB 图片往返")
    dib = _make_test_dib()
    dib_result = cb.write_formats([cb.FormatBlob(name="CF_DIB", category=cb.CAT_IMAGE, data=dib)])
    if dib_result:
        after_dib = cb.capture(max_per_format=8 * 1024 * 1024)
        got_dib = after_dib.by_name("CF_DIB")
        if got_dib:
            passed &= _ok(len(got_dib.data) == len(dib), "DIB 长度一致（%d 字节）" % len(dib))
            passed &= _ok(got_dib.data[:40] == dib[:40], "DIB 头一致")
        else:
            passed &= _ok(False, "CF_DIB 未回读成功")
    else:
        print("  [SKIP] 无法写入 CF_DIB（可能被占用）")

    # 5) 还原
    if not args.keep:
        print("\n[5] 还原原始剪贴板")
        if before.items:
            restored = cb.write_formats(cb.order_for_paste(before.items, []))
            passed &= _ok(bool(restored), "还原: %s" % restored.describe())
        else:
            passed &= _ok(cb.clear_clipboard(), "原剪贴板为空，已清空")
    else:
        print("\n[5] --keep 指定，保留测试内容在剪贴板里")

    print("\n结果:", "全部通过" if passed else "存在失败项")
    return 0 if passed else 1


# --------------------------------------------------------------------- 入口


def build_parser() -> argparse.ArgumentParser:
    """构造自检命令行解析器。

    单独抽出来是为了能被测试直接检查 —— 之前 `clip-loop` 子命令忘了
    `set_defaults(func=...)`，于是启动器里的"剪贴板同步环回"一选就只打印帮助并以
    退出码 2 结束，看起来像"自检失败"。有个能遍历子命令的测试就不会再漏。
    """
    parser = argparse.ArgumentParser(prog="python -m netclip.selftest", description="netclip 自检工具")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("env", help="打印环境与依赖状态").set_defaults(func=cmd_env)

    p_keys = sub.add_parser("keys", help="打印每次按键的原始 vk / 扫描码（排查某个键不对）")
    p_keys.add_argument("--seconds", type=float, default=8.0, help="观察时长，默认 8 秒")
    p_keys.set_defaults(func=cmd_keys)

    p_hooks = sub.add_parser("hooks", help="安装真实钩子并观察事件（会短暂接管鼠标键盘）")
    p_hooks.add_argument("--seconds", type=float, default=3.0, help="观察时长，默认 3 秒")
    p_hooks.set_defaults(func=cmd_hooks)

    p_clip = sub.add_parser("clipboard", help="剪贴板读写往返自检")
    p_clip.add_argument("--keep", action="store_true", help="测试后不还原剪贴板")
    p_clip.set_defaults(func=cmd_clipboard)

    p_loop = sub.add_parser("clip-loop", help="剪贴板同步的本地环回自检（不需要第二台机器）")
    p_loop.set_defaults(func=cmd_clip_loop)

    p_files = sub.add_parser("files-loop", help="文件同步的本地环回自检（不需要第二台机器）")
    p_files.set_defaults(func=cmd_files_loop)

    p_tray = sub.add_parser("tray", help="托盘图标自检（会短暂出现一个托盘图标）")
    p_tray.add_argument("--seconds", type=float, default=3.0, help="图标停留时长")
    p_tray.set_defaults(func=cmd_tray)

    #: 下面三个是"已经写好、但一直没挂进菜单"的排查工具。它们不依赖网络之外
    #: 的任何东西，也不需要第二台机器的配合就能暴露具体的坏点，所以挂回菜单。
    p_warp = sub.add_parser(
        "warpguard",
        help="验证自己 SetCursorPos 不会产生被转发的移动事件（查'鼠标被吸住抖动'）",
    )
    p_warp.set_defaults(func=cmd_warpguard)

    p_inject = sub.add_parser(
        "inject",
        help="验证注入的输入被识别为'本机注入'而不是真实输入（查自伤死循环）",
    )
    p_inject.set_defaults(func=cmd_inject)

    p_conn = sub.add_parser(
        "loop", help="连通性自检：连上对端、收发帧，但**不转发**输入（最安全）"
    )
    p_conn.add_argument("--config", "-c", default=None, help="配置文件路径（默认找程序目录下的 config.toml）")
    p_conn.add_argument("--log-level", default=None, help="覆盖日志级别: DEBUG/INFO/WARNING/ERROR")
    p_conn.add_argument("--seconds", type=float, default=10.0, help="观察时长，默认 10 秒")
    p_conn.set_defaults(func=cmd_loop)

    return parser

def main(argv: "List[str] | None" = None) -> int:
    _setup_console()
    args = build_parser().parse_args(argv)
    if not getattr(args, "func", None):
        build_parser().print_help()
        return 2

    if sys.platform != "win32":
        print("netclip 仅支持 Windows", file=sys.stderr)
        return 2
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
