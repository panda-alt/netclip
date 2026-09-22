"""独立诊断脚本：把当前剪贴板里**到底有什么**打成一份能直接看的报告。

用法（在仓库根目录下跑）::

    python tools/clip_probe.py

为什么是独立脚本而不是软件的一部分
----------------------------------
软件正常运行时**不需要**这个功能 —— 它是排查"粘不了"时才用的一次性工具，
塞进 `netclip/` 里只会让本体变大、让主流程多一堆只服务于诊断的分支。
所以它留在 `tools/` 下，`build_exe.py` 也不会把它打进 exe。

排查"两台机器剪贴板不一样"的用法：两台各跑一次，把两份输出放在一起对比。
输出里每一样都是真机上"看一眼就定位"的东西：

  * **格式号** —— 未注册的格式名可能是空的，这时只有号能对上；
  * **每种格式的字节数 + 内容预览** —— 只看格式名分不清发全了没、内容对不对；
  * **嵌入 OLE 三件套单列** —— WPS 的公式/嵌入对象靠它们粘贴，而 `OwnerLink`
    里装的是**源文档路径**（对端根本没有那个文件）；
  * **CF_UNICODETEXT 的字节级细节** —— 末尾双 NUL 是约定，缺了有些程序当成坏数据；
  * **剪贴板所有者进程** —— 所有者不是 netclip 就说明我们写完又被别人抢走了。
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import sys
import time
from ctypes import wintypes
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from netclip.win import clipboard as cb  # noqa: E402
from netclip.win import winapi as w  # noqa: E402

#: 文本类格式 -> 用什么编码解成可读预览。其它格式只给十六进制：
#: 它们是二进制结构，硬解只会显示乱码。
TEXT_ENCODINGS = {
    "CF_UNICODETEXT": "utf-16-le",
    "CF_TEXT": "mbcs",
    "CF_OEMTEXT": "mbcs",
    "HTML Format": "utf-8",
    "Rich Text Format": "latin-1",
    "FileNameW": "utf-16-le",
    "FileName": "mbcs",
}

#: 嵌入 OLE 对象的"三件套"。WPS 的公式就是靠它们粘贴的 ——
#: "公式粘不了、纯文字能粘"这类现象全落在它们身上，所以单列出来。
OLE_FORMATS = ("Embed Source", "Object Descriptor", "OwnerLink")

# --- 只这个脚本要用的 Win32（所以不往 winapi.py 里加）-----------------------
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

w.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
w.user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
w.kernel32.OpenProcess.restype = wintypes.HANDLE
w.kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
w.kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
w.kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]


def clipboard_owner_text() -> str:
    """谁持有剪贴板。

    **排查"粘不了"最容易被忽略、却最有用的一条**：所有者不是 netclip 而是别的进程
    （WPS、资源管理器…）时，说明我们写完内容之后**又被别人覆盖了** ——
    那"粘贴菜单变灰"就跟我们写的内容无关。反过来所有者就是 netclip，
    内容就是我们写的那一份，问题在内容本身。
    """
    try:
        hwnd = w.user32.GetClipboardOwner()
        if not hwnd:
            #: 无主是正常的：我们这种没有窗口的进程写进去就是这个状态。
            return "无（没有窗口持有 —— 通常是本进程写进去的）"
        pid = wintypes.DWORD(0)
        w.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        pid_value = int(pid.value)
        name = "?"
        handle = w.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid_value)
        if handle:
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(len(buf))
                if w.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                    name = os.path.basename(buf.value)
            finally:
                w.kernel32.CloseHandle(handle)
        return "PID %d (%s)" % (pid_value, name)
    except Exception as exc:  # pragma: no cover - 诊断信息拿不到不该让脚本失败
        return "无法获取: %s" % exc


def looks_like_text(data: bytes) -> Optional[str]:
    """二进制格式里"其实装的是字符串"时把它解出来。

    `OwnerLink` 就是典型：它是嵌入对象"源文档在哪"的记录，内容常是 UTF-16 的路径。
    跨机同步时这是**机器相关的信息**，只给十六进制等于白打。

    **判据必须按字节/Unicode 类别来，不能只看"解出来是不是可打印字符"** ——
    试过那种写法，`Embed Source` 的 OLE 头 `d0 cf 11 e0 …` 会被解成一串很像样的
    日文/韩文汉字，看着像字符串，其实完全是二进制垃圾。最终用的规则：

      * **先判 ANSI**：纯 ASCII 负载同时也是一个"合法的 UTF-16 字符串"
        （`Kingsoft WPS 9.0 Format` 按 UTF-16 解出来是一串汉字），顺序反了就会误判；
        而真正的 UTF-16 文本有一半是 NUL，不可能 95% 都是可打印 ASCII。
      * 再按 UTF-16LE 解，要求里面**一个未分配（Cn）/私有区（Co）/代理项（Cs）
        字符都没有**。中文路径照样全是已分配字符，而 OLE 头会含 Cn/Co，直接否掉。
    """
    if len(data) < 6 or len(data) > 8192:
        return None

    printable = sum(1 for b in data if 0x20 <= b < 0x7F or b in (9, 10, 13))
    if printable / float(len(data)) >= 0.95:
        return data.decode("mbcs", errors="replace")[:120]

    try:
        import unicodedata

        def decode_ok(payload: bytes) -> "Optional[str]":
            text = payload.decode("utf-16-le", errors="replace")
            bad = sum(
                1
                for ch in text
                if ch == "\ufffd"
                or not ch.isprintable()
                or unicodedata.category(ch) in ("Cn", "Co", "Cs")
            )
            return text.replace("\x00", "") if len(text) >= 3 and bad == 0 else None

        #: **先按完整负载解**；解不出来才丢掉末尾多出来的那一个字节再试。
        #: 反过来（总是先丢）会把最后一个字符吃掉 —— `…\Doc.wps` 会变成 `…\Doc.wp`。
        text = decode_ok(data)
        if text is None and len(data) % 2:
            text = decode_ok(data[:-1])
        if text is not None:
            return text[:120]
    except (UnicodeDecodeError, LookupError):  # pragma: no cover
        pass
    return None


def preview_of(item, limit: int = 90) -> str:
    """把一个格式的内容压成一行能看的预览。"""
    encoding = TEXT_ENCODINGS.get(item.name)
    if encoding:
        try:
            text = item.data.decode(encoding, errors="replace")
        except LookupError:  # pragma: no cover
            text = item.data.decode("utf-8", errors="replace")
        text = text.replace("\r\n", "⏎").replace("\n", "⏎").replace("\x00", "␀")
        return text[:limit] + ("…" if len(text) > limit else "")
    if item.name in ("CF_HDROP", "FileDrop"):
        try:
            return "、".join(cb.parse_hdrop_paths(item.data))
        except Exception:  # pragma: no cover
            return "<CF_HDROP 解析失败>"
    head = item.data[:16].hex(" ")
    guessed = looks_like_text(item.data)
    tail = " …" if len(item.data) > 16 else ""
    return "%s%s%s" % (head, tail, ("  ← 看着像字符串: 「%s」" % guessed) if guessed else "")


def _file_format_details(snapshot) -> None:
    """CF_HDROP 的路径到底存不存在 —— 文件粘贴失败时先看这里。"""
    hdrop = next((i for i in snapshot.items if i.name == cb.HDROP_FORMAT), None)
    if hdrop is None:
        return
    print("\n--- CF_HDROP 路径 ---")
    paths = cb.parse_hdrop_paths(hdrop.data)
    if not paths:
        print("  解析不出任何路径（数据可能坏了）")
    for path in paths:
        print("  [%s] %s" % ("存在" if os.path.exists(path) else "★不存在，粘贴必失败★", path))


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover
            pass

    max_bytes = 512 * 1024 * 1024
    print("=== 剪贴板内容探查 ===")
    print("时间: %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    snapshot = cb.capture(max_per_format=max_bytes)
    print("序号: %d  （两台机器的号不通用，只用来判断有没有变过）" % snapshot.sequence)
    print("所有者: %s" % clipboard_owner_text())
    print("共 %d 种格式，合计 %.1f KB" % (len(snapshot.items), snapshot.total_size() / 1024.0))

    categories: dict = {}
    for item in snapshot.items:
        categories[item.category] = categories.get(item.category, 0) + 1
    if categories:
        print("分类小计: %s" % "  ".join("%s=%d" % kv for kv in sorted(categories.items())))

    #: **剪贴板的原始枚举顺序。** 消费者是**按这个顺序**挑格式的，
    #: 而我们发出去之前会按自己的优先级重排（`order_for_paste`）——
    #: 顺序一变，对端可能就挑到别的表示（真机现象：可编辑公式变成图片）。
    #: 现在只能靠这张表看出来，所以必须打出来。
    order: "List[str]" = []
    try:
        with cb.ClipboardSession():
            order = [name for _fmt, name in cb.enumerate_formats()]
    except Exception:  # pragma: no cover
        order = []
    print("枚举顺序: %s" % " > ".join(order) if order else "枚举顺序: <取不到>")

    width = max([len(i.name) for i in snapshot.items] + [10])
    print("")
    print(
        "%-*s  %-4s  %-8s  %-6s  %10s  %-12s  %s"
        % (width, "格式名", "顺位", "格式号", "类别", "字节", "内容指纹", "内容预览")
    )
    print("-" * (width + 92))
    for item in sorted(snapshot.items, key=lambda i: -i.size):
        position = order.index(item.name) if item.name in order else -1
        digest = hashlib.sha256(item.data).hexdigest()[:12]
        print(
            "%-*s  %-4s  %-8s  %-6s  %10d  %-12s  %s"
            % (
                width,
                item.name,
                position if position >= 0 else "-",
                ("0x%04X" % item.fmt) if item.fmt else "-",
                item.category,
                item.size,
                digest,
                preview_of(item),
            )
        )
    print("")
    print("顺位 = 剪贴板里的枚举次序（0 最先）；内容指纹 = 该格式负载的 SHA-256 前 12 位。")
    print("两台对比时重点看：**顺位一样吗**、同名格式的**指纹一样吗**。")

    text_item = next((i for i in snapshot.items if i.name == "CF_UNICODETEXT"), None)
    print("\n--- CF_UNICODETEXT 细节 ---")
    if text_item is None:
        print("  没有这个格式（纯文本都粘不了时先看这里）")
    else:
        data = text_item.data
        print("  %d 字节 = %d 个 UTF-16 码元" % (len(data), len(data) // 2))
        print("  末尾 4 字节: %s  （约定要以双 NUL 收尾）" % data[-4:].hex(" "))
        print("  内容: 「%s」" % preview_of(text_item, limit=300))

    print("\n--- 嵌入 OLE 三件套（WPS 公式 / 嵌入对象靠它们）---")
    present = []
    for name in OLE_FORMATS:
        item = next((i for i in snapshot.items if i.name == name), None)
        if item is None:
            print("  %-20s 缺失" % name)
            continue
        present.append(name)
        print("  %-20s %8d 字节  头 16 字节: %s" % (name, item.size, item.data[:16].hex(" ")))
        guessed = looks_like_text(item.data)
        if guessed:
            print("  %-20s ← 看着像字符串: 「%s」" % ("", guessed))
    missing = sorted(set(OLE_FORMATS) - set(present))
    print("  小结: %s" % ("齐全" if not missing else "缺 %s" % missing))

    _file_format_details(snapshot)

    if snapshot.skipped:
        print("\n跳过的格式（读不出来 / 被过滤）:")
        for name, reason in snapshot.skipped:
            print("  %-*s  %s" % (width, name, reason))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
