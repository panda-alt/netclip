"""用 PyInstaller 把 netclip 打包成 exe。

用法::

    python tools/build_exe.py            # 默认: dist/netclip/ 目录版（推荐）
    python tools/build_exe.py --onefile  # 单文件版（见下方取舍）

**目标机器不需要装 Python** —— PyInstaller 会把整个 CPython 运行时
（`python310.dll`、标准库、`VCRUNTIME140.dll`）一起打进去。
验证方式：把 PATH 砍到只剩 `C:\\Windows\\System32`（`Get-Command python` 找不到），
exe 照跑，`--selftest env` 还会报出内置的那个 Python 版本。

为什么默认是**目录版**而不是单文件
----------------------------------

`--onefile` 每次启动都要把自己解压到 `%TEMP%\\_MEIxxxxxx`（约 11.8 MB）。
正常退出会清掉，但**被强杀不会** —— 而 netclip 恰恰经常被强杀
（`start.bat` 的第 3/4 项就是 `Stop-Process -Force`），一次一个 11.8 MB
的垃圾目录，悄无声息地堆着（真机上实测堆了 6 个，70.9 MB）。

另外它启动还要慢 1~2 秒，而且**正在运行的那个实例依赖它的 `_MEI` 目录** ——
清理临时文件时很容易误删到它（真机上就误删过一次）。

目录版（`--onedir`）没有这些问题：27 个文件、13.8 MB，拖一次就行，
不产生任何临时垃圾。相比之下源码树是几百个文件，还是少得多。

其余的选项
----------

**保留控制台（不加 `--noconsole`）** —— `--check` / `--dump-formats` / 全部自检
都要输出，砍掉控制台这些就全瞎了。"不弹窗"是**启动方式**的事：计划任务里勾
"只在用户登录时运行"不会弹窗，脚本启动用 `Start-Process -WindowStyle Hidden`。

**`config.example.toml` 打进包，`config.toml` 绝不打进包** —— 前者是程序自带的
只读资源（`config.resource_path()`），后者每台机器不一样、用户要手改，
必须在 exe 旁边（`config.app_dir()`）。

**`--noupx`** —— UPX 压过的 exe 更容易被杀毒软件误报，而 netclip 常驻运行，
被拦一次很难查。省那点体积不值得。

**排除 pywin32** —— netclip 一行都不用 pywin32（全是自带 ctypes 绑定），但它还是
会被打进包里，因为**标准库**里有可选导入，而 PyInstaller 的静态分析会跟进去：

  * `logging/handlers.py` 的 `NTEventLogHandler.__init__` 里有
    `import win32evtlogutil, win32evtlog`；
  * `distutils/msvccompiler.py` 里有 `import win32api`。

不排除的话会多出 `win32\win32api.pyd`、`win32\win32evtlog.pyd` 和
`pywin32_system32\pywintypes310.dll`（约 340 KB）。更要紧的是它会让别人以为
netclip 依赖 pywin32 —— 和"零依赖、复制过去就能跑"的定位自相矛盾。

**`--icon assets/netclip.ico`** —— 仓库里那份多尺寸 `.ico`（由
`tools/make_icon.py` 从 PNG 生成，含 16/24/32/48/64/128/256 七档），托盘用 32、
资源管理器用 32/48、大图标视图用 256，各取所需。文件不在就跳过，不让构建失败。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = ROOT / "build"
DIST_DIR = ROOT / "dist"
ENTRY = BUILD_DIR / "netclip_entry.py"

#: PyInstaller 的入口脚本，构建时生成。
#:
#: 不能让 PyInstaller 直接把 `netclip/__main__.py` 当入口：那个文件用的是包内
#: 相对导入（`from . import __init__` 之类），当脚本跑会 ImportError。
ENTRY_CODE = '''"""PyInstaller 的入口脚本 —— 由 tools/build_exe.py 生成，不进版本库。

`netclip/__main__.py` 用的是包内相对导入，不能直接当脚本喂给 PyInstaller，
所以这里包一层。
"""

import sys

from netclip.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
'''


def pyinstaller_command(onefile: bool, dist_dir: "Optional[Path]" = None) -> "list[str]":
    """拼出完整的 PyInstaller 命令行。

    单独抽成函数是为了能被测试**直接断言** —— 这里少一个选项都会出问题
    （见模块文档），不该只靠读代码来确认。见 `tests/test_packaging.py`。

    `dist_dir` 默认是 `dist/`；换一个目录主要是给"默认目录正被占用"这种情况留条路
    （托盘里的 netclip.exe 开着 `dist\\netclip` 时，那边的文件删不掉）。
    """
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        #: UPX 压过的 exe 更容易被杀毒软件误报，得不偿失。
        "--noupx",
        "--onefile" if onefile else "--onedir",
        "--name",
        "netclip",
        #: `config.example.toml` 放进包根（"源<分隔符>目标"，Windows 上是 `;`）
        "--add-data",
        "%s%s." % (ROOT / "config.example.toml", os.pathsep),
        "--paths",
        str(ROOT),
        "--distpath",
        str(dist_dir or DIST_DIR),
        "--workpath",
        str(BUILD_DIR / "pyinstaller"),
        "--specpath",
        str(BUILD_DIR),
    ]

    #: 图标用仓库里的 `assets/netclip.ico`（`tools/make_icon.py` 从 PNG 生成，
    #: 多尺寸：托盘取 32、资源管理器取 32/48、大图标视图取 256）。
    #:
    #: 两件事都要做，缺一不可：
    #:   * `--icon` 把它塞进 exe 的**资源**里（资源管理器、任务栏、exe 自身）；
    #:   * `--add-data` 把同一份文件也拷进包里 —— 托盘图标要在**运行时**
    #:     读出字节自己解码、再叠一个状态色描边，光有资源取不到像素。
    #: 文件不在就跳过（图标只是外观，不该让整个构建失败）。
    icon = ROOT / "assets" / "netclip.ico"
    if icon.is_file():
        command += [
            "--add-data",
            "%s%sassets" % (icon, os.pathsep),
            "--icon",
            str(icon),
        ]

    for name in EXCLUDED_MODULES:
        command += ["--exclude-module", name]

    command.append(str(ENTRY))
    return command


#: 标准库里的**可选** pywin32 导入会被 PyInstaller 的静态分析跟进来，见模块文档。
#: 逐条列出来而不是笼统地排 `win32`，是为了让"到底排掉了什么"一眼可见。
EXCLUDED_MODULES: "tuple[str, ...]" = (
    "win32api",  # distutils/msvccompiler.py
    "win32evtlog",  # logging/handlers.py 的 NTEventLogHandler
    "win32evtlogutil",  # 同上
    "pywintypes",  # pywin32 的运行时 DLL 封装
    "pythoncom",
)


def artifact_path(onefile: bool, dist_dir: "Optional[Path]" = None) -> Path:
    """这次构建会产出哪个东西。"""
    return (dist_dir or DIST_DIR) / ("netclip.exe" if onefile else "netclip")


#: 构建会整目录删掉旧产物，而**用户真正的配置就躺在里面**
#: （`config.app_dir()` 在 exe 运行时返回 exe 所在目录）。备份这几个，构建完放回去。
PRESERVED_NAMES: "tuple[str, ...]" = ("config.toml",)


def _read_preserved(target: Path) -> "Dict[str, bytes]":
    """把用户自己的文件读进内存，构建完再写回去。"""
    saved: "Dict[str, bytes]" = {}
    if not target.is_dir():
        return saved
    for name in PRESERVED_NAMES:
        candidate = target / name
        if candidate.is_file():
            saved[name] = candidate.read_bytes()
    return saved


def _write_preserved(target: Path, saved: "Dict[str, bytes]") -> "List[str]":
    for name, data in saved.items():
        target.joinpath(name).write_bytes(data)
    return list(saved)


def remove_old_artifact(path: Path) -> None:
    """删掉旧产物。**删不掉就抛异常**，绝不带着半截目录往下走。

    之前这里是 `shutil.rmtree(..., ignore_errors=True)`，真机上吃过亏：目录被某个
    进程占着（托盘里的 netclip.exe、或者别的程序打开着里面的文件），`ignore_errors`
    会**把能删的先删了**（包括用户的 `config.toml`），然后 PyInstaller 自己再去删，
    报一句没头没脑的 `PermissionError`。结果是配置没了、产物也没了，而报错信息
    完全指不到真正的原因。
    """
    if not path.exists():
        return
    last: "Optional[OSError]" = None
    for _ in range(3):
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            return
        except OSError as exc:
            last = exc
            time.sleep(0.3)
    raise OSError(
        "%s 删不掉：%s\n"
        "  多半是有进程占着里面的东西。常见原因：\n"
        "    * 托盘里还挂着 netclip.exe —— 先从托盘退出它（或 Stop-Process -Name netclip）；\n"
        "    * 资源管理器 / 杀毒软件正开着这个目录；\n"
        "    * 某程序打开着里面**已删除**的文件 —— 这时目录看着是空的，却删不掉。\n"
        "  停掉占用者后重跑即可。本次**一个文件都没动**。" % (path, last)
    )


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description="把 netclip 打包成 exe")
    parser.add_argument(
        "--onefile",
        action="store_true",
        help="打成单个 exe（代价：每次启动自解压到 %%TEMP%%，强杀会残留约 11.8 MB）",
    )
    parser.add_argument("--keep-build", action="store_true", help="保留 build/ 中间产物")
    parser.add_argument(
        "--outdir",
        default="",
        help="换个输出目录（默认 dist/）。默认目录被占着删不掉时用得上。",
    )
    args = parser.parse_args(argv)
    dist_dir = Path(args.outdir).resolve() if args.outdir else DIST_DIR

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("没有 PyInstaller。先装：python -m pip install pyinstaller", file=sys.stderr)
        return 1

    if not (ROOT / "config.example.toml").is_file():
        print("找不到 config.example.toml，无法打进包里。", file=sys.stderr)
        return 1

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    ENTRY.write_text(ENTRY_CODE, encoding="utf-8")

    #: 先删掉旧产物。留着的话构建失败时用户会拿到一个**旧的**产物却以为成功了。
    #: 用户的 `config.toml` 就住在里面，所以先读出来、构建完再放回去。
    target = artifact_path(args.onefile, dist_dir)
    saved = _read_preserved(target)
    try:
        remove_old_artifact(target)
    except OSError as exc:
        print("打包中止：%s" % exc, file=sys.stderr)
        return 1

    cmd = pyinstaller_command(args.onefile, dist_dir)
    print("=== 打包（%s）===" % ("单文件" if args.onefile else "目录版"))
    print("  " + " ".join(cmd[2:]))
    result = subprocess.run(cmd, cwd=str(ROOT))
    if result.returncode != 0:
        print("\n打包失败（PyInstaller 退出码 %d）" % result.returncode, file=sys.stderr)
        return result.returncode

    restored = _write_preserved(target, saved)
    if restored:
        print("\n已把 %s 原样放回 %s" % ("、".join(restored), target))

    if not args.keep_build:
        shutil.rmtree(BUILD_DIR / "pyinstaller", ignore_errors=True)

    print("\n=== 产物 ===")
    if args.onefile:
        print("  %s  （%.1f MB，单文件）" % (target, target.stat().st_size / 1024 / 1024))
    else:
        files = [f for f in target.rglob("*") if f.is_file()]
        total = sum(f.stat().st_size for f in files)
        print("  %s\\  （%d 个文件，共 %.1f MB）" % (target, len(files), total / 1024 / 1024))

    #: 另一次构建的产物留在同一个 dist 里很容易被误运行。
    stale = artifact_path(not args.onefile, dist_dir)
    if stale.exists():
        print("")
        print("  注意: %s 是**另一次**构建的旧产物，这次没有更新它。" % stale)
        print("        确认不用了就删掉；如果它正在运行，先停掉再删。")

    exe = target / "netclip.exe" if not args.onefile else target
    print("")
    print("  部署：把上面那个%s复制到另一台机器，然后：" % ("文件" if args.onefile else "文件夹"))
    print("    netclip.exe --gen-config     # 第一次：生成 config.toml（写在 exe 旁边）")
    print("    改 config.toml 里的 peer_ip / peer_position / psk")
    print("    netclip.exe --check          # 校验配置")
    print("    netclip.exe                  # 启动")
    print("")
    print("  自检: netclip.exe --selftest keys --seconds 8")
    print("  完整路径: %s" % exe)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
