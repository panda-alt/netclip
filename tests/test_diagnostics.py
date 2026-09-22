"""诊断相关测试：`tools/clip_probe.py`（独立脚本）+ `--freeze-dump`（软件内）。

诊断分两类，放在哪是刻意的：

  * **剪贴板探查** -> `tools/clip_probe.py`，**独立脚本，不进软件、不进 exe**。
    软件正常运行时根本不需要它。
  * **卡死取证** -> `netclip --freeze-dump`，**必须留在软件里**：它 dump 的是
    本进程的线程栈，进程卡死时外部脚本连不上来。

所以这个文件里有一条测试专门盯着"软件本体不许再长出诊断功能"。
"""

from __future__ import annotations

import importlib.util
import shutil
import tempfile
from pathlib import Path

from netclip import config as config_mod

ROOT = Path(__file__).resolve().parent.parent


def _temp_dir():
    path = Path(tempfile.mkdtemp(prefix="netclip_diag_"))
    return path, lambda: shutil.rmtree(path, ignore_errors=True)


def _probe():
    """把 `tools/clip_probe.py` 当模块加载（`tools/` 不是包，用 importlib 按路径加）。"""
    path = ROOT / "tools" / "clip_probe.py"
    spec = importlib.util.spec_from_file_location("netclip_clip_probe", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------- 剪贴板探查（独立脚本）


def test_probe_stays_out_of_the_application():
    """**诊断不许进软件本体。**

    剪贴板探查只在排查"粘不了"时用一次，软件正常运行时完全不需要它。塞进
    `netclip/` 会让本体变大、主流程多出一堆只服务诊断的分支，还要跟着打进 exe。
    它应该在 `tools/` 下，用 `python tools/clip_probe.py` 跑。

    这条测试就是盯着这件事的 —— 哪天有人（包括我）图省事又把它挪回 `__main__.py`，
    这里会直接红。
    """
    app = (ROOT / "netclip" / "__main__.py").read_text(encoding="utf-8")
    for leaked in ("_clipboard_owner_text", "_looks_like_text", "_preview_of", "_TEXT_FORMAT_ENCODINGS"):
        assert leaked not in app, "%s 又跑回软件本体里了" % leaked
    assert (ROOT / "tools" / "clip_probe.py").is_file()


def test_probe_decodes_ownerlink_paths():
    """`OwnerLink` 装的是**源文档路径**，必须能解出来。

    跨机同步时这是"机器相关的信息" —— 对端根本没有那个路径。只显示十六进制等于白打。
    真机上 `OwnerLink` 的字节就是 `43 00 3a 00 5c 00 ...`（`C:\\` 的 UTF-16LE）。
    """
    probe = _probe()
    for path in (r"C:\Users\zxy\Doc.wps", r"C:\Users\zxy\文档.wps", r"C:\Users\zxy\Doc.wps\1"):
        got = probe.looks_like_text(path.encode("utf-16-le"))
        assert got == path, "解出来是 %r" % got


def test_probe_accepts_odd_length_utf16():
    """UTF-16 负载经常是**奇数长度**（末尾多一个字节）。

    一度写成"总是先丢掉末尾那一字节"，结果最后一个字符被吃掉
    （`…\\Doc.wps` 变成 `…\\Doc.wp`）。正确顺序是**先按完整负载解**，解不出来才丢。
    """
    probe = _probe()
    odd = r"C:\Users\zxy\ab".encode("utf-16-le") + b"\x00"
    assert len(odd) % 2 == 1
    assert probe.looks_like_text(odd) == r"C:\Users\zxy\ab"


def test_probe_rejects_binary_that_decodes_to_printable_cjk():
    """**不能只看"解出来是不是可打印字符"。**

    试过那种写法：OLE 存储头 `d0 cf 11 e0 a1 b1 1a e1` 按 UTF-16 解出来是一串
    很像样的汉字，看着像字符串，其实完全是二进制垃圾。所以判据要用 **Unicode 类别**
    （未分配 Cn / 私有区 Co / 代理项 Cs 一个都不许有）。
    """
    probe = _probe()
    assert probe.looks_like_text(bytes.fromhex("d0cf11e0a1b11ae1") + b"X" * 40) is None
    assert probe.looks_like_text(b"\x00" * 64) is None
    assert probe.looks_like_text(bytes(range(256))) is None


def test_probe_prefers_ansi_for_pure_ascii():
    """纯 ASCII 负载同时也是一个"合法的 UTF-16 字符串"，顺序反了就会误判。

    `Kingsoft WPS 9.0 Format` 按 UTF-16 解出来是一串汉字，但它显然是 ANSI。
    所以要先判 ANSI —— 真正的 UTF-16 文本有一半是 NUL，不可能 95% 都是可打印 ASCII。
    """
    probe = _probe()
    assert probe.looks_like_text(b"Kingsoft WPS 9.0 Format") == "Kingsoft WPS 9.0 Format"


def test_probe_ignores_tiny_or_huge_payloads():
    probe = _probe()
    assert probe.looks_like_text(b"") is None
    assert probe.looks_like_text(b"ab") is None
    assert probe.looks_like_text(b"A" * 9000) is None


def test_probe_preview_is_readable():
    """文本类格式的预览必须是人能读的，不是十六进制。"""
    from netclip.win import clipboard as cb

    probe = _probe()
    item = cb.FormatBlob(
        name="CF_UNICODETEXT", category=cb.CAT_TEXT, data="第一行\r\n第二行".encode("utf-16-le")
    )
    preview = probe.preview_of(item)
    assert "第一行" in preview and "第二行" in preview
    assert "⏎" in preview, "换行要看得见，否则分不清是一行还是两行"
    assert "\\x" not in preview


def test_probe_preview_of_binary_shows_hex():
    from netclip.win import clipboard as cb

    probe = _probe()
    item = cb.FormatBlob(name="SomeVendor Blob", category=cb.CAT_OTHER, data=bytes(range(32)))
    assert probe.preview_of(item).startswith("00 01 02 03")


def test_probe_lists_the_ole_trio():
    """嵌入 OLE 三件套必须单列出来 —— "公式粘不了、纯文字能粘"全落在它们身上。"""
    probe = _probe()
    assert probe.OLE_FORMATS == ("Embed Source", "Object Descriptor", "OwnerLink")


# --------------------------------------------------- 卡死取证（软件内）


def test_freeze_dump_option_parses():
    from netclip.__main__ import build_parser

    parser = build_parser()
    assert parser.parse_args([]).freeze_dump == 0.0
    assert parser.parse_args(["--freeze-dump", "20"]).freeze_dump == 20.0


def test_freeze_dump_is_off_by_default(monkeypatch):
    """默认关闭时**不能**创建文件，也不能改全局 faulthandler 状态。"""
    import faulthandler

    from netclip.__main__ import _arm_freeze_dump

    root, cleanup = _temp_dir()
    try:
        monkeypatch.setattr(config_mod, "app_dir", lambda: root)
        _arm_freeze_dump(0.0, _FakeLog())
        assert not (root / "netclip-freeze.txt").exists()
        assert not faulthandler.is_enabled(), "默认不该动 faulthandler"
    finally:
        cleanup()


def test_freeze_dump_writes_a_header_and_arms_the_timer(monkeypatch):
    """打开时要写说明、并**真的**把定时 dump 挂上。

    真机上出现过"整个进程没声了、日志一个字都不写、鼠标完全不能动，只能杀掉才恢复"。
    那种卡死下 Python 层的日志全部失效，唯一能穿透的是 `faulthandler` —— 它跑在独立
    的 C 线程上，**不需要 GIL**。所以这个开关必须有东西真的落盘。
    """
    import faulthandler

    from netclip.__main__ import _arm_freeze_dump

    root, cleanup = _temp_dir()
    monkeypatch.setattr(config_mod, "app_dir", lambda: root)
    log = _FakeLog()
    try:
        _arm_freeze_dump(5.0, log)
        dump = root / "netclip-freeze.txt"
        assert dump.is_file(), "没有生成 dump 文件"
        assert "卡死取证已开启" in dump.read_text(encoding="utf-8")
        assert faulthandler.is_enabled(), "应该已经启用 faulthandler"
        assert log.infos, "应该在日志里说明去哪个文件看"
    finally:
        #: 一定要取消，否则它会一直往临时文件里写（还会拖慢整套测试）
        faulthandler.cancel_dump_traceback_later()
        cleanup()


class _FakeLog:
    def __init__(self) -> None:
        self.infos = []
        self.warnings = []

    def info(self, message, *args):
        self.infos.append(message % args if args else message)

    def warning(self, message, *args, **kwargs):
        self.warnings.append(message % args if args else message)
