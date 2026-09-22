"""临时诊断：打印我们写入 CF_UNICODETEXT 后回读到的原始字节。"""

import sys

sys.path.insert(0, ".")

from netclip.win import clipboard as cb
from netclip.win import winapi as w

original = cb.capture(max_per_format=64 * 1024 * 1024)
try:
    text = "abc"
    payload = text.encode("utf-16-le")
    print("写入前 payload =", payload)
    result = cb.write_formats([cb.FormatBlob(name="CF_UNICODETEXT", category=cb.CAT_TEXT, data=payload)])
    print("写入结果:", result.describe())

    with cb.ClipboardSession():
        handle = w.user32.GetClipboardData(w.CF_UNICODETEXT)
        size = w.global_mem_size(handle)
        raw = w.global_mem_bytes(handle)
        print("GlobalSize =", size)
        print("原始字节前 32 =", raw[:32])
        print("末尾 16 =", raw[-16:])

    snap = cb.capture(max_per_format=1024 * 1024)
    item = snap.by_name("CF_UNICODETEXT")
    print("回读 data =", item.data, "len =", len(item.data))
finally:
    if original.items:
        cb.write_formats(cb.order_for_paste(original.items, []))
    else:
        cb.clear_clipboard()
