"""临时维护脚本：清掉 config.toml 的 peer_ip，用于本机单机冒烟测试。

用一次就删。之所以写成文件而不是命令行内联，是因为 PowerShell 没有 heredoc，
内联的引号转义非常容易出错。
"""

import pathlib
import sys

p = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "config.toml")
raw = p.read_bytes()
if raw.startswith(b"\xef\xbb\xbf"):
    raw = raw[3:]
    print("已去掉 BOM")
text = raw.decode("utf-8")
text = text.replace('peer_ip = "192.168.1.42"', 'peer_ip = ""')
p.write_text(text, encoding="utf-8")
print("peer_ip 已置空:", p)
