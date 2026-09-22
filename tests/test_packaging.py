"""打包相关的路径逻辑。

打包成 exe 之后会多出一个"根目录"，必须和源码运行时区分开：

  * **程序目录**（`config.app_dir()`）—— 用户看得见、会去改的东西（`config.toml`、
    日志）在这。exe 运行时是 **exe 所在目录**，源码运行时是仓库根目录。
  * **只读资源**（`config.resource_path()`）—— 随程序分发的、用户不该动的东西
    （`config.example.toml`）。exe 运行时在 `sys._MEIPASS` 解包目录里。

**搞混的后果很隐蔽**：PyInstaller 的 `--onefile` 每次启动都会解压到一个**新的**
临时目录，`__file__` 指向那里。如果拿它去找 `config.toml`，用户改的配置永远读不到
（每次都在别的地方找一个不存在的文件），日志也会散落在 temp 里一路堆积。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from netclip import config as config_mod

ROOT = Path(__file__).resolve().parent.parent

#: 见 `_temp_dir()`：有邻例会把 `shutil.rmtree` 换成必定失败的替身，
#: 清理临时目录时要绕开它。
_REAL_RMTREE = shutil.rmtree


# ------------------------------------------------------------------ 程序目录


def test_app_dir_is_the_repo_root_when_not_frozen(monkeypatch):
    """源码运行时，程序目录 = 仓库根目录。"""
    monkeypatch.setattr(config_mod.sys, "frozen", False, raising=False)
    assert (config_mod.app_dir() / "config.example.toml").is_file()


def test_app_dir_follows_the_executable_when_frozen(monkeypatch):
    """**打包后程序目录必须是 exe 所在目录**，而不是临时解包目录。

    真机上踩过：`--onefile` 把包解压到 `%TEMP%\\_MEIxxxxx`，`__file__` 就在那里。
    拿它去找 `config.toml`，用户改的配置永远读不到。
    """
    monkeypatch.setattr(config_mod.sys, "frozen", True, raising=False)
    monkeypatch.setattr(config_mod.sys, "executable", r"C:\somewhere\netclip.exe", raising=False)
    #: 即使解包目录存在，也不能影响"程序目录"的判定 —— 两者是不同的概念
    monkeypatch.setattr(config_mod.sys, "_MEIPASS", r"C:\Temp\_MEI123456", raising=False)

    assert config_mod.app_dir() == Path(r"C:\somewhere")


# ------------------------------------------------------------------ 只读资源


def test_resource_path_prefers_the_pyinstaller_bundle(monkeypatch):
    """打包后优先从 `sys._MEIPASS` 里找随程序分发的资源。"""
    bundle = Path(tempfile.mkdtemp(prefix="netclip_meipass_"))
    (bundle / "config.example.toml").write_text("[device]\n", encoding="utf-8")
    monkeypatch.setattr(config_mod.sys, "_MEIPASS", str(bundle), raising=False)

    assert config_mod.resource_path("config.example.toml") == bundle / "config.example.toml"


def test_resource_path_falls_back_to_app_dir():
    """没有解包目录时（源码运行）退回程序目录。"""
    assert config_mod.resource_path("config.example.toml") == (
        config_mod.app_dir() / "config.example.toml"
    )


# ------------------------------------------------------------------ 构建脚本


def _build_module():
    """把 `tools/build_exe.py` 当模块加载。

    `tools/` 不是一个包（没有 `__init__.py`，也不该为了测试加一个），
    所以用 importlib 按路径加载。加载它只有函数定义，不会触发构建。
    """
    import importlib.util

    path = ROOT / "tools" / "build_exe.py"
    spec = importlib.util.spec_from_file_location("netclip_build_exe", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pyinstaller_command_defaults_to_onedir():
    """**默认必须是目录版。**

    `--onefile` 每次启动都往 `%TEMP%\\_MEIxxxxxx` 解压约 11.8 MB；正常退出会清，
    **被强杀不会**。而 netclip 经常被强杀（`start.bat` 第 3/4 项就是
    `Stop-Process -Force`）—— 真机上实测堆了 6 个，70.9 MB。
    目录版没有这个问题，而且启动更快。
    """
    cmd = _build_module().pyinstaller_command(onefile=False)
    assert "--onedir" in cmd, "默认应该是目录版"
    assert "--onefile" not in cmd


def test_pyinstaller_command_still_supports_onefile():
    """单文件仍然是可选能力，只是不再默认。"""
    cmd = _build_module().pyinstaller_command(onefile=True)
    assert "--onefile" in cmd
    assert "--onedir" not in cmd


def test_build_script_has_the_flags_that_matter():
    """构建命令必须带上那几个"少一个就出问题"的选项。

    每一条都有具体理由（见 `tools/build_exe.py` 的说明），不是照抄模板：

      * `--add-data` 带上 `config.example.toml` —— 否则 `--gen-config` 在只有
        exe 的机器上用不了；
      * `--noupx` —— UPX 压过的 exe 更容易被杀毒软件误报，而 netclip 常驻运行，
        被拦一次很难查；
      * **不能有 `--noconsole`** —— `--check` / `--dump-formats` / 自检都要输出。
        要"不弹窗"应该由调用方隐藏窗口，不该砍掉控制台。
    """
    module = _build_module()
    for onefile in (False, True):
        cmd = module.pyinstaller_command(onefile=onefile)
        assert "--add-data" in cmd, onefile
        assert "--noupx" in cmd, onefile
        assert "--noconsole" not in cmd and "--windowed" not in cmd, onefile
        assert "--icon" in cmd, "打包没带上图标"
        assert module.artifact_path(onefile).name == (
            "netclip.exe" if onefile else "netclip"
        )


def test_build_excludes_pywin32():
    """netclip 一行都不用 pywin32，包里不该有它。

    它之所以会被打进去，是因为**标准库**里有可选导入，而 PyInstaller 的静态
    分析会跟进去（`logging/handlers.py` 的 `NTEventLogHandler`、
    `distutils/msvccompiler.py`）。不排除就会多出
    `win32\\win32api.pyd`、`win32\\win32evtlog.pyd`、
    `pywin32_system32\\pywintypes310.dll` —— 除了体积，更糟的是让人以为
    netclip 依赖 pywin32，和"零依赖、复制过去就能跑"的定位自相矛盾。
    """
    module = _build_module()
    cmd = module.pyinstaller_command(onefile=False)
    excluded = {
        cmd[i + 1] for i, token in enumerate(cmd) if token == "--exclude-module"
    }
    for name in ("win32api", "win32evtlog", "pywintypes"):
        assert name in excluded, "没有排除 %s" % name


def test_the_built_package_has_no_pywin32_files():
    """构建产物里不该出现那几个文件（构建过才有意义，没构建就跳过）。"""
    if sys.platform != "win32":
        return
    dist = ROOT / "dist" / "netclip"
    if not dist.is_dir():
        return
    leftovers = [
        f
        for f in dist.rglob("*")
        if f.is_file()
        and f.name.lower() in ("win32api.pyd", "win32evtlog.pyd", "pywintypes310.dll")
    ]
    assert not leftovers, "包里还有 pywin32 的文件: %s" % leftovers


# ------------------------------------------------------------------ 旧产物的清理


def _temp_dir():
    """建一个临时目录，返回 `(路径, 清理函数)`。

    运行器只提供 `monkeypatch` 一个 fixture（没有 `tmp_path`），所以这里按仓库里
    既有的写法自己建。

    清理用的是**导入时抓到的**那个 `shutil.rmtree`：有的用例会把
    `shutil.rmtree` 换成一个必定失败的替身，替换后 `shutil.rmtree` 就删不掉东西了。
    """
    path = Path(tempfile.mkdtemp(prefix="netclip_dist_"))
    return path, lambda: _REAL_RMTREE(path, ignore_errors=True)


def test_remove_old_artifact_reports_instead_of_half_deleting(monkeypatch):
    """删不掉旧产物时必须**抛异常**，不能吞掉继续。

    真机上吃过亏：原来这里是 `shutil.rmtree(..., ignore_errors=True)`，目录被占着
    （托盘里的 netclip.exe），它把能删的先删了（**包括用户的 config.toml**），
    然后 PyInstaller 再删一次就报 `PermissionError`。配置没了、产物也没了，
    报错还完全指不到原因。
    """
    module = _build_module()
    monkeypatch.setattr(module.time, "sleep", lambda _s: None)

    def refuse(_path, *args, **kwargs):
        raise PermissionError(5, "拒绝访问")

    root, cleanup = _temp_dir()
    monkeypatch.setattr(module.shutil, "rmtree", refuse)
    try:
        target = root / "netclip"
        target.mkdir()
        try:
            module.remove_old_artifact(target)
        except OSError as exc:
            message = str(exc)
            assert "删不掉" in message
            assert "netclip.exe" in message, "提示里要给出最可能的原因"
        else:
            raise AssertionError("删不掉却没有抛异常")
    finally:
        cleanup()


def test_remove_old_artifact_is_a_noop_when_missing():
    module = _build_module()
    root, cleanup = _temp_dir()
    try:
        module.remove_old_artifact(root / "从来不存在")  # 不应抛异常
    finally:
        cleanup()


def test_user_config_survives_a_rebuild():
    """`config.toml` 必须原样活过一次重新打包。

    `config.app_dir()` 在 exe 运行时返回 **exe 所在目录**，所以用户填的
    peer_ip / peer_position / psk 就在 `dist\\netclip\\config.toml` 里，
    而构建的第一步是把这个目录整个删掉。
    """
    module = _build_module()
    root, cleanup = _temp_dir()
    try:
        target = root / "netclip"
        target.mkdir()
        config = target / "config.toml"
        config.write_bytes(b"[security]\npsk = 'my-secret'\n")
        (target / "netclip.exe").write_bytes(b"old")

        saved = module._read_preserved(target)
        module.remove_old_artifact(target)
        assert not target.exists(), "旧产物应该被删干净"

        target.mkdir()  # 模拟 PyInstaller 重新产出
        assert module._write_preserved(target, saved) == ["config.toml"]
        assert config.read_bytes() == b"[security]\npsk = 'my-secret'\n"
    finally:
        cleanup()


def test_read_preserved_ignores_a_missing_config():
    module = _build_module()
    root, cleanup = _temp_dir()
    try:
        target = root / "netclip"
        target.mkdir()
        assert module._read_preserved(target) == {}
        assert module._read_preserved(root / "不存在") == {}
    finally:
        cleanup()


def test_icon_is_multi_size():
    """仓库里的 `.ico` 必须是多尺寸的。

    只放一张 256 的话，托盘（16）和资源管理器小图标（32/48）就靠系统临时缩放，
    糊得看不清。尺寸由 `tools/make_icon.py` 的 `SIZES` 决定。
    """
    from PIL import Image

    icon = ROOT / "assets" / "netclip.ico"
    assert icon.is_file(), "缺少 assets/netclip.ico"
    sizes = set(Image.open(icon).info.get("sizes", []))
    for size in (16, 24, 32, 48, 64, 128, 256):
        assert (size, size) in sizes, "图标里没有 %d 这一档: %s" % (
            size,
            sorted(sizes),
        )


def test_cli_exposes_selftest_for_the_packaged_exe():
    """打成 exe 之后没法 `python -m netclip.selftest`，所以主程序里要有入口。

    用 `nargs=REMAINDER` 是为了让后面的参数**原样透传**给 selftest，
    而不是在这里再抄一份 argparse 定义（抄一份就一定会漂移）。
    """
    from netclip.__main__ import build_parser

    args = build_parser().parse_args(["--selftest", "keys", "--seconds", "8"])
    assert args.selftest == ["keys", "--seconds", "8"]


def test_selftest_dispatch_does_not_rebuild_argparse():
    """`--selftest` 的处理必须是"转发给 selftest.main"，不是另写一套解析。"""
    text = (ROOT / "netclip" / "__main__.py").read_text(encoding="utf-8")
    assert "selftest.main(" in text


def test_generated_entry_script_is_not_in_the_repo():
    """入口脚本是构建时生成的（`build/`），不该进版本库。

    它存在的唯一理由是 `netclip/__main__.py` 用包内相对导入，不能当脚本喂给
    PyInstaller。放个静态文件在仓库里只会让人以为它是手写的。
    """
    assert not (ROOT / "netclip_entry.py").exists()
    build_script = (ROOT / "tools" / "build_exe.py").read_text(encoding="utf-8")
    assert 'ENTRY = BUILD_DIR / "netclip_entry.py"' in build_script
    assert "ENTRY.write_text(" in build_script
