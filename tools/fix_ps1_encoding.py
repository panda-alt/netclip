"""给 .ps1 文件补上 UTF-8 BOM。

背景：Windows PowerShell 5.1（也就是 `powershell.exe`）在没有 BOM 时**不按
UTF-8 读取 .ps1**，而是按系统 ANSI 代码页（中文机器上是 GBK）。脚本里的中文
会被解成乱码，运气不好这些乱码字节还会产生语法错误，报错位置还指向毫不相干
的行 —— 极难排查。

而 PowerShell 7（`pwsh.exe`）默认按 UTF-8 读，两种都兼容的写法就是
**带 BOM 的 UTF-8**。所以每次编辑过 .ps1 之后都该跑一下这个脚本。

用法::

    python tools/fix_ps1_encoding.py start.ps1 [more.ps1 ...]
"""

from __future__ import annotations

import sys
from pathlib import Path

BOM = b"\xef\xbb\xbf"


def fix(path: Path) -> str:
    raw = path.read_bytes()
    had_bom = raw.startswith(BOM)
    if had_bom:
        raw = raw[len(BOM) :]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return "跳过（不是 UTF-8）: %s (%s)" % (path, exc)

    # 统一行尾为 CRLF：PowerShell 两种都能读，但混用会让 diff 很难看
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    path.write_bytes(BOM + text.encode("utf-8"))
    return "已写入 BOM: %s（%d 字节）" % (path, len(text))


def main(argv: "list[str]") -> int:
    targets = [Path(a) for a in argv[1:]]
    if not targets:
        here = Path(__file__).resolve().parent.parent
        targets = sorted(here.glob("*.ps1")) + sorted(here.glob("tools/*.ps1"))
    if not targets:
        print("没有找到 .ps1 文件")
        return 1
    for path in targets:
        if not path.is_file():
            print("不存在: %s" % path)
            continue
        print(fix(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
