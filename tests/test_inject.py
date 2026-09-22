"""键盘注入的扩展标志归一化。

真机故障：**右 Shift 在对端变成了别的东西**（用户的原话是"没被映射"）。

`python -m netclip.selftest keys` 抓到的原始数据：

    左 Shift  vk=0xA0  scan=0x2A  ext=False
    右 Shift  vk=0xA1  scan=0x36  ext=True     ← 系统给右 Shift 置了扩展标志

而 `E0 36` 在 PC/AT 规范里**没有对应的键**（小键盘 `/` 是 `E0 35`，小键盘 Enter
是 `E0 1C`）。照着来源给的标志原样注入，就会打出完全无关的键。

修法不是给 Shift 打补丁，而是一条通用规则：**扩展标志只在"这个扫描码确实有
E0 版本"时才有意义**。见 `inject.normalize_extended`。
"""

from __future__ import annotations

import sys

import pytest

from netclip.win import inject
from netclip.win import winapi as w

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="需要 Windows")


def _capture(monkeypatch) -> list:
    """把 `_send` 换掉，返回一个收集 `INPUT` 结构的列表。"""
    sent: list = []
    monkeypatch.setattr(inject, "_send", lambda items: (sent.extend(items), len(items))[1])
    return sent


# ------------------------------------------------------------------ 归一化规则


def test_extended_flag_is_normalised_against_the_scan_code():
    """三种情况各验一遍。"""
    # 没有 E0 版本的扫描码 → 强制非扩展（这就是右 Shift 那个 bug）
    assert inject.normalize_extended(0x36, True) is False, "右 Shift 的 0x36 没有 E0 版本"
    assert inject.normalize_extended(0x2A, True) is False, "左 Shift 同理"

    # 两种形式都有的扫描码 → 只能信来源给的标志
    assert inject.normalize_extended(0x1D, True) is True, "右 Ctrl"
    assert inject.normalize_extended(0x1D, False) is False, "左 Ctrl"
    assert inject.normalize_extended(0x38, True) is True, "右 Alt"
    assert inject.normalize_extended(0x38, False) is False, "左 Alt"
    assert inject.normalize_extended(0x48, True) is True, "方向键 ↑（编辑键区）"
    assert inject.normalize_extended(0x48, False) is False, "小键盘 8"

    # 只做减法：**绝不主动"补上"扩展标志**。
    #
    # `0x35` 是"主键盘 `/`（非扩展）"和"小键盘 `/`（E0 35）"**共用**的扫描码，
    # 标志必须照来源走 —— 强行加扩展就把 `Shift+/`（问号）打成了小键盘的键。
    assert inject.normalize_extended(0x35, False) is False, "主键盘 / 必须保持非扩展"
    assert inject.normalize_extended(0x35, True) is True, "小键盘 / 才是扩展"
    assert inject.normalize_extended(0x5B, False) is False, "不做加法：Win 键也不强行补"
    assert inject.normalize_extended(0x1C, False) is False, "主键盘 Enter"


# ------------------------------------------------------------------ 实际注入


def test_question_mark_shift_slash_is_not_broken(monkeypatch):
    """**回归测试**：`Shift+/` 必须还能打出问号。

    主键盘 `/` 的扫描码是 `0x35`（非扩展），小键盘 `/` 是 `E0 35` —— 同一个扫描码
    两种含义。曾经加过一条"有些扫描码没有非扩展含义、必须强制扩展"的规则，把
    `0x35` 也列了进去：结果主键盘 `/` 被打成小键盘的键，`Shift+/` 出不来问号。
    """
    sent = _capture(monkeypatch)

    inject.key_event(0xBF, True, scan=0x35, extended=False)  # VK_OEM_2 = / ?
    assert sent[0].ki.wScan == 0x35
    assert not (sent[0].ki.dwFlags & w.KEYEVENTF_EXTENDEDKEY), (
        "主键盘 / 带上扩展标志就变成小键盘 / 了，Shift+/ 打不出问号"
    )

    sent.clear()
    inject.key_event(w.VK_DIVIDE, True, scan=0x35, extended=True)  # 小键盘 /
    assert sent[0].ki.dwFlags & w.KEYEVENTF_EXTENDEDKEY, "小键盘 / 必须保留扩展标志"


def test_right_shift_is_injected_without_the_extended_flag(monkeypatch):
    """**回归测试**：右 Shift 绝不能带扩展标志注入。

    这条直接钉住真机故障：钩子报 `scan=0x36 ext=True`，如果照抄，
    `E0 36` 在 Windows 上不是"右 Shift"。
    """
    sent = _capture(monkeypatch)
    inject.key_event(w.VK_RSHIFT, True, scan=0x36, extended=True)

    assert sent, "没有发出任何输入"
    assert sent[0].ki.wScan == 0x36
    assert sent[0].ki.dwFlags & w.KEYEVENTF_SCANCODE, "必须走扫描码注入"
    assert not (sent[0].ki.dwFlags & w.KEYEVENTF_EXTENDEDKEY), "右 Shift 不该带扩展标志"


def test_left_shift_is_injected_without_the_extended_flag(monkeypatch):
    """左 Shift 本来就没问题，别在修右 Shift 的时候把它弄坏。"""
    sent = _capture(monkeypatch)
    inject.key_event(w.VK_LSHIFT, True, scan=0x2A, extended=False)

    assert sent[0].ki.wScan == 0x2A
    assert not (sent[0].ki.dwFlags & w.KEYEVENTF_EXTENDEDKEY)


def test_right_ctrl_and_alt_keep_the_extended_flag(monkeypatch):
    """右 Ctrl / 右 Alt 的扫描码**确实有** E0 版本，标志必须保留。

    它们是"左 Ctrl 和右 Ctrl 用同一个扫描码 0x1D"的那一对 —— 扩展标志是
    唯一的区分手段，丢了就变成左键了。
    """
    sent = _capture(monkeypatch)
    inject.key_event(w.VK_RCONTROL, True, scan=0x1D, extended=True)
    inject.key_event(w.VK_RMENU, True, scan=0x38, extended=True)

    assert sent[0].ki.dwFlags & w.KEYEVENTF_EXTENDEDKEY, "右 Ctrl 丢了扩展标志就变成左 Ctrl"
    assert sent[1].ki.dwFlags & w.KEYEVENTF_EXTENDEDKEY, "右 Alt 同理"


def test_left_ctrl_stays_non_extended(monkeypatch):
    sent = _capture(monkeypatch)
    inject.key_event(w.VK_LCONTROL, True, scan=0x1D, extended=False)
    assert not (sent[0].ki.dwFlags & w.KEYEVENTF_EXTENDEDKEY)


def test_numberpad_keys_keep_the_extended_flag(monkeypatch):
    """小键盘上的键**确实**要扩展标志 —— 别在修主键盘 `/` 的时候把它们弄坏。"""
    sent = _capture(monkeypatch)
    inject.key_event(w.VK_DIVIDE, True, scan=0x35, extended=True)  # 小键盘 /
    inject.key_event(0x68, True, scan=0x48, extended=False)  # VK_NUMPAD8 = 小键盘 8（非扩展）
    inject.key_event(w.VK_UP, True, scan=0x48, extended=True)  # 方向键 ↑（扩展）

    assert sent[0].ki.dwFlags & w.KEYEVENTF_EXTENDEDKEY, "小键盘 /"
    assert not (sent[1].ki.dwFlags & w.KEYEVENTF_EXTENDEDKEY), "小键盘 8 是非扩展的"
    assert sent[2].ki.dwFlags & w.KEYEVENTF_EXTENDEDKEY, "方向键 ↑ 是扩展的"


def test_keyup_still_carries_the_keyup_flag(monkeypatch):
    """抬起的标志不能被归一化逻辑吃掉。"""
    sent = _capture(monkeypatch)
    inject.key_event(w.VK_RSHIFT, False, scan=0x36, extended=True)
    assert sent[0].ki.dwFlags & w.KEYEVENTF_KEYUP
    assert not (sent[0].ki.dwFlags & w.KEYEVENTF_EXTENDEDKEY)
