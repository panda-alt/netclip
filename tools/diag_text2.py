"""临时诊断 2：逐步打印 _read_text 的中间结果。"""

import ctypes
import sys

sys.path.insert(0, ".")

from netclip.win import clipboard as cb
from netclip.win import winapi as w

original = cb.capture(max_per_format=64 * 1024 * 1024)
try:
    payload = "abc".encode("utf-16-le")
    cb.write_formats([cb.FormatBlob(name="CF_UNICODETEXT", category=cb.CAT_TEXT, data=payload)])

    with cb.ClipboardSession():
        ctypes.set_last_error(0)
        available = w.user32.IsClipboardFormatAvailable(w.CF_UNICODETEXT)
        print("IsClipboardFormatAvailable =", available, "err =", ctypes.get_last_error())
        fmt = w.user32.EnumClipboardFormats(0)
        names = []
        while fmt:
            names.append((fmt, w.clip_format_name(fmt)))
            fmt = w.user32.EnumClipboardFormats(fmt)
        print("剪贴板格式:", names)
        ctypes.set_last_error(0)
        handle = w.user32.GetClipboardData(w.CF_UNICODETEXT)
        print("handle =", handle, "err =", ctypes.get_last_error())
        if handle:
            print("GlobalSize =", w.global_mem_size(handle))
            ptr = w.kernel32.GlobalLock(handle)
            raw = ctypes.string_at(ptr, w.global_mem_size(handle))
            w.kernel32.GlobalUnlock(handle)
            print("raw len =", len(raw), "raw[:24] =", raw[:24])
            got = cb._read_text(handle)
            print("_read_text len =", len(got), "got =", got)
finally:
    if original.items:
        cb.write_formats(cb.order_for_paste(original.items, []))
    else:
        cb.clear_clipboard()
