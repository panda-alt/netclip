"""权限级别 / 启动器一致性测试。

背景（真机上遇到的问题）：鼠标移到**任务管理器**这类提权窗口上就"卡住"。
根因不是鼠标算法，是 Windows 的 UIPI —— 未提权进程的低级钩子和 `SendInput`
对已提权窗口一律无效，钩子被系统屏蔽后 netclip 连事件都收不到。这是系统限制
（Deskflow #8611、QQ 远程桌面同样如此），唯一解法是让 netclip 自己提权。

所以这里测三件事：
  1. `is_elevated()` 真的能读到权限级别（参数声明写错会**静默永远返回 False**，
     那样日志会一直骗人，必须专门盯住）；
  2. 自检的每个子命令都绑定了处理函数（`clip-loop` 就漏过一次）；
  3. 启动器菜单里列的自检名和真实子命令集合一致，防止两边各自漂移。
"""

from __future__ import annotations

import ctypes
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from netclip import selftest
from netclip.win import winapi as w

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="需要 Windows 令牌 API")

#: 仓库根目录（tests/ 的上一级）
ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ 权限读取


def test_token_query_actually_succeeds():
    """`is_elevated()` 不能是因为 API 调用失败才返回 False。

    这是最阴的失败模式：`GetTokenInformation` 的参数一旦声明错（比如 HANDLE 用
    c_void_p 以外的类型），调用**不会抛异常**，只会返回 False。于是"普通用户"和
    "读不出来"在日志里长得一模一样，排查时会被带偏。这里直接照着 `is_elevated()`
    的写法调一遍原始 API，确认它真的成功了。
    """
    token = w.wintypes.HANDLE()
    opened = w.kernel32.OpenProcessToken(w.kernel32.GetCurrentProcess(), w.TOKEN_QUERY, ctypes.byref(token))
    assert opened, "OpenProcessToken 失败，ctypes 声明有问题"
    try:
        info = w.wintypes.DWORD()
        returned = w.wintypes.DWORD()
        ok = w.advapi32.GetTokenInformation(
            token,
            w.TOKEN_ELEVATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
            ctypes.byref(returned),
        )
        assert ok, "GetTokenInformation 失败 —— is_elevated() 会永远返回 False"
        assert returned.value == ctypes.sizeof(info) == 4
        assert bool(info.value) == w.is_elevated()
    finally:
        w.kernel32.CloseHandle(token)


def test_is_elevated_is_a_stable_bool():
    first = w.is_elevated()
    assert isinstance(first, bool)
    # 权限级别在一次进程生命周期内不可能变，重复调用必须一致
    assert w.is_elevated() == first


def test_token_elevation_info_class_is_a_plain_dword():
    """`TokenElevation` 返回的是一个 DWORD，不是结构体。

    写错成结构体时 `GetTokenInformation` 会因为缓冲区大小不符而失败 —— 又一个
    静默返回 False 的途径。
    """
    assert w.TOKEN_ELEVATION == 20
    assert ctypes.sizeof(w.wintypes.DWORD) == 4


# ------------------------------------------------------------------ 自检命令行


def _subcommands() -> "dict[str, object]":
    parser = selftest.build_parser()
    for action in parser._actions:  # noqa: SLF001 - argparse 没有公开的取子命令方式
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict) and choices:
            return choices
    raise AssertionError("自检解析器里没有子命令")


def test_every_subcommand_has_a_handler():
    """每个子命令都必须 `set_defaults(func=...)`。

    漏掉的那个不会报错，只会打印帮助并以退出码 2 结束 —— 在启动器里看起来
    就像"自检失败"，而真正的原因是这个开关根本没接上。
    """
    missing = [name for name, sub in _subcommands().items() if sub.get_default("func") is None]
    assert not missing, "这些子命令没有绑定处理函数: %s" % ", ".join(sorted(missing))


def _launcher_menu_checks() -> "set[str]":
    """从 start.ps1 的自检菜单里抠出被引用的子命令名。

    只认 `$map = @{ ... }` 那个块，避免误抓到脚本里别的 `'1' = @(...)` 映射。
    """
    text = (ROOT / "start.ps1").read_text(encoding="utf-8")
    block = re.search(r"\$map\s*=\s*@\{(.*?)\}", text, re.S)
    assert block, "start.ps1 里找不到自检菜单的 $map 定义"
    return {name for name in re.findall(r"@\(\s*'([a-z][a-z0-9-]*)'", block.group(1))}


def test_launcher_menu_matches_selftest_subcommands():
    """启动器菜单和真实子命令必须一一对应。

    `inject` / `warpguard` / `loop` 这三个自检被删掉之后，菜单里还留着它们，
    选了就是 argparse 报错。反过来新加的自检没人挂到菜单上也一样是白写。
    """
    menu = _launcher_menu_checks()
    real = set(_subcommands())
    assert menu - real == set(), "启动器菜单里的自检项在 selftest 里不存在: %s" % sorted(menu - real)
    assert real - menu == set(), "selftest 的这些子命令没挂到启动器菜单上: %s" % sorted(real - menu)


def _documented_subcommands(path) -> "dict[str, int]":
    """把文档里提到的 `netclip.selftest <子命令>` 抠出来，返回 {名字: 行号}。"""
    text = path.read_text(encoding="utf-8")
    found = {}
    for match in re.finditer(r"netclip\.selftest\s+([a-z][a-z0-9-]*)", text):
        found.setdefault(match.group(1), text[: match.start()].count("\n") + 1)
    return found


def test_docs_only_advertise_real_subcommands():
    """README / config.example.toml 里教人跑的 `selftest xxx` 必须真的存在。

    真踩过：`netclip/debug/` 被删掉之后，README 和配置示例里还在教
    `selftest trace-compare`，照做就是一个 argparse 错误 —— 而文档是用户唯一
    会去看的地方，它撒谎比代码有 bug 更浪费时间。
    """
    real = set(_subcommands())
    problems = []
    for name in ("README.md", "config.example.toml"):
        for sub, line in sorted(_documented_subcommands(ROOT / name).items(), key=lambda kv: kv[1]):
            if sub not in real:
                problems.append("%s:%d 提到了不存在的子命令 %r" % (name, line, sub))
    assert not problems, "文档里的自检命令对不上: \n  " + "\n  ".join(problems)


def test_docs_do_not_mention_the_removed_debug_modules():
    """文档里不该再出现已经删掉的 `netclip.debug.*` 功能。

    鼠标追踪 CSV、每秒输入诊断、整条链路日志这三块依赖 `netclip/debug/`，
    那个包已经删了。留着的文档会让人以为打开某个开关就能用。
    """
    banned = ("netclip.debug.", "debug.chain_lines", "debug.mouse", "trace-compare", "mouse-trace")
    problems = []
    for name in ("README.md", "config.example.toml"):
        text = (ROOT / name).read_text(encoding="utf-8")
        for needle in banned:
            if needle in text:
                line = text[: text.index(needle)].count("\n") + 1
                problems.append("%s:%d 还在讲 %r" % (name, line, needle))
    assert not problems, "文档里还有已删功能的描述: \n  " + "\n  ".join(problems)


# ------------------------------------------------------------------ 启动器可执行性


def _run_launcher_expression(expression: str) -> str:
    """把 start.ps1 里的**函数定义**抽出来，在子进程里真正执行一次。

    只取函数定义，不执行脚本末尾的菜单，所以不会有副作用。
    """
    script = "\n".join(
        [
            "$path = '" + str(ROOT / "start.ps1").replace("'", "''") + "'",
            #: **必须开 StrictMode**：start.ps1 自己就开了，而不开的话有一类 bug
            #: 根本复现不出来 —— 比如函数返回集合时 PowerShell 会把单元素数组
            #: **展开**，调用方拿到的是对象，`$x.Count` 在宽松模式下静默返回 $null，
            #: 在 StrictMode 下才会抛错。真机上就是这么炸的。
            "Set-StrictMode -Version Latest",
            "$errs = $null; $tokens = $null",
            "$ast = [System.Management.Automation.Language.Parser]::ParseFile("
            "$path, [ref]$tokens, [ref]$errs)",
            "if ($errs.Count) { Write-Error 'start.ps1 解析失败'; exit 2 }",
            "$defs = $ast.FindAll({ param($n) $n -is "
            "[System.Management.Automation.Language.FunctionDefinitionAst] }, $true)",
            '$src = ($defs | ForEach-Object { $_.Extent.Text }) -join "`n"',
            '$src += "`n" + \'' + expression.replace("'", "''") + "'",
            "Invoke-Expression $src",
        ]
    )
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        #: 必须显式指定 UTF-8：脚本自己会把 `[Console]::OutputEncoding` 设成 UTF-8，
        #: 而 `text=True` 默认按**系统区域编码**（中文机器上是 GBK）解码，
        #: 遇到中文输出会在读取线程里抛 UnicodeDecodeError，断言失败时就看不到
        #: 真正的报错内容了。
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    assert proc.returncode == 0, "启动器函数执行失败：\n%s\n%s" % (proc.stdout, proc.stderr)
    return proc.stdout


def test_launcher_functions_actually_run():
    """启动器函数必须能**真正执行**，不能只保证"语法看得懂"。

    真机故障：`[ordered]@{}`（OrderedDictionary）用 `$d[$k] = $v` 给**不存在的键**
    赋值 —— 这在 PowerShell 里**语法完全正确**，要跑到那一行才抛
    `ArgumentOutOfRangeException`（参数名 index），于是启动器一选"停止/重启"
    就整个崩掉，报错还只有一行看不懂的 index。

    语法解析器看不见这类问题，只有真跑一遍才行。这也是为什么这个用例存在。
    """
    out = _run_launcher_expression(
        "(Get-NetclipProcesses | Measure-Object).Count; 'elevated=' + (Test-Elevated)"
    )
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    assert lines and lines[0].isdigit(), "Get-NetclipProcesses 没有返回数量: %r" % out
    assert any(line.startswith("elevated=") for line in lines), out


def test_launcher_process_list_is_always_an_array():
    """`Get-NetclipProcesses` 返回的必须**始终是数组**，哪怕只有 0 或 1 个进程。

    PowerShell 的 `return` 会把集合**展开**：只有一个元素时调用方拿到的是单个
    对象，而脚本开头的 `Set-StrictMode -Version Latest` 会让 `$procs.Count`
    直接抛 "The property 'Count' cannot be found on this object"。
    真机上就是这么炸的 —— 而且只在**恰好一个**实例在跑时出现，一停就复现不了。

    `_run_launcher_expression` 里也开了 StrictMode，否则这条路径测不出来。
    """
    out = _run_launcher_expression(
        "'count=' + @(Get-NetclipProcesses).Count; "
        "'index0=' + @(Get-NetclipProcesses)[0].Id"
    )
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    assert any(line.startswith("count=") for line in lines), out


def _braced_block(text: str, start: int) -> str:
    """从 `start`（指向 `{` 之前）开始，按大括号配平截出一整块。"""
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError("大括号不配平，截不出完整的块")


def test_cmd_and_vbs_launchers_are_ascii_only():
    """`.bat` 和 `.vbs` 必须**纯 ASCII**，而且不能带 BOM。

    这两类文件没有"带 BOM 的 UTF-8"这种约定可用：

      * `cmd.exe` 按**系统 ANSI 代码页**（中文机器上是 GBK）逐字节读 `.bat`，
        乱码字节里可能含有被当成**命令分隔符**的字符，于是注释的碎片会被当命令执行；
      * `wscript.exe` 按同样的代码页读没有 BOM 的 `.vbs`，乱码直接变成语法错误。

    所以这两个后缀里的中文一律挪到 `.ps1`（它带 UTF-8 BOM，PowerShell 认得）。
    """
    problems = []
    for pattern in ("*.bat", "*.vbs"):
        for path in sorted(ROOT.glob(pattern)):
            raw = path.read_bytes()
            if raw.startswith(b"\xef\xbb\xbf"):
                problems.append("%s 带了 UTF-8 BOM" % path.name)
            non_ascii = sum(1 for byte in raw if byte > 0x7F)
            if non_ascii:
                problems.append("%s 有 %d 个非 ASCII 字节" % (path.name, non_ascii))
    assert not problems, "; ".join(problems)


def test_windowless_launcher_calls_run_mode():
    """`netclip_task.vbs` 必须是非交互启动（`-Run`）。

    否则隐藏窗口下的 `Read-Host` 会让计划任务**永远挂住** ——
    任务列表里显示"正在运行"，但 netclip 根本没起来，也没有任何报错。
    """
    text = (ROOT / "netclip_task.vbs").read_text(encoding="ascii")
    assert "start.ps1" in text and "-Run" in text, "没有调用 start.ps1 -Run"
    assert "wscript.exe" in text, "注释里没写清楚计划任务该用 wscript.exe 启动它"


def test_cleanup_script_removes_a_staging_tree():
    """`clean.ps1` 真的能清掉一棵暂存树。

    用 `-Path` 指到临时目录上跑，**不去动真实的暂存区** —— 这也是那个参数存在的
    唯一理由：让这个流程可测。真实的暂存清理由 netclip 自己按 TTL 做，
    这个脚本只是"想现在就清一下"的入口。
    """
    base = Path(tempfile.mkdtemp(prefix="netclip_clean_"))
    staging = base / "staging"
    (staging / "desktop-master" / "aaa").mkdir(parents=True)
    (staging / "desktop-master" / "aaa" / "a.csv").write_bytes(b"x" * 4096)
    assert staging.is_dir()

    proc = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "clean.ps1"),
            "-Path",
            str(staging),
            "-Yes",
        ],
        capture_output=True,
        encoding="utf-8",  # 同上：脚本输出是 UTF-8，别按 GBK 解
        errors="replace",
        timeout=180,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not staging.exists(), "暂存目录没被清掉"


# ------------------------------------------------------------------ 脚本编码


def test_run_mode_asks_nothing():
    """`-Run` 模式下不允许出现任何 Read-Host。

    计划任务里的窗口是隐藏的，`Read-Host` 会**永远等下去**：任务列表里显示"正在运行"，
    但 netclip 根本没起来，而且没有任何报错。所以这个模式下的分支必须全部
    走"失败就退出"，不能问。
    """
    text = (ROOT / "start.ps1").read_text(encoding="utf-8-sig")

    blocks = []
    position = 0
    while True:
        found = text.find("if ($Run) {", position)
        if found < 0:
            break
        blocks.append(_braced_block(text, found))
        position = found + 1

    assert blocks, "start.ps1 里找不到 if ($Run) 分支"
    for block in blocks:
        #: 只剥掉**整行注释**再检查。注释里出现 "Read-Host" 是正常的
        #: （那正是在解释"为什么这里不能 Read-Host"），不能因此误报。
        code = "\n".join(
            line for line in block.splitlines() if not line.lstrip().startswith("#")
        )
        assert "Read-Host" not in code, "-Run 分支里有 Read-Host，隐藏窗口下会挂死"
    assert any(
        "Start-NetclipBackground" in block and "-NoPrompt" in block for block in blocks
    ), "-Run 分支没有真正启动 netclip，或者忘了把 -NoPrompt 传下去"


# ------------------------------------------------------------------ 脚本编码


def test_powershell_scripts_carry_a_utf8_bom():
    """每个 .ps1 都必须带 UTF-8 BOM。

    这不是洁癖，是**真踩过的坑**：Windows PowerShell 5.1（也就是 `powershell.exe`）
    在没有 BOM 时不按 UTF-8 读 .ps1，而按系统 ANSI 代码页（中文机器上是 GBK）。
    脚本里的中文全变乱码，某些乱码字节还会构成语法错误，报错行号指向毫不相干的位置
    —— 一个只改了菜单文字的操作，会表现成"第 150 行有个多余的 }"。

    任何文本编辑工具都可能顺手丢掉 BOM，所以用测试钉死。修复方式：
    `python tools/fix_ps1_encoding.py`
    """
    scripts = sorted(ROOT.glob("*.ps1")) + sorted(ROOT.glob("tools/*.ps1"))
    assert scripts, "没有找到任何 .ps1 脚本"
    bad = [str(p.relative_to(ROOT)) for p in scripts if not p.read_bytes().startswith(b"\xef\xbb\xbf")]
    assert not bad, "这些 .ps1 缺 UTF-8 BOM（跑 python tools/fix_ps1_encoding.py）: %s" % ", ".join(bad)
