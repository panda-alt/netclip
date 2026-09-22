"""PyInstaller 的入口脚本 —— 由 tools/build_exe.py 生成，不进版本库。

`netclip/__main__.py` 用的是包内相对导入，不能直接当脚本喂给 PyInstaller，
所以这里包一层。
"""

import sys

from netclip.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
