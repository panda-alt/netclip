"""Win32 API ctypes 绑定层（P0：剪贴板 + 输入 + 消息窗口所需部分）。

为什么用 ctypes 而不是 pywin32
------------------------------
pywin32 的 `win32clipboard` 在 `CloseClipboard` 异常路径上依赖 GC 与异常，
在"别的程序正占着剪贴板"这种高频竞争场景里容易留下"看起来锁着"的状态。
本模块直接用 ctypes，把"打开-写入-关闭"写成显式的一小段临界区，
并且**所有失败路径都保证调用到 CloseClipboard**。

约定
----
* 所有函数名保持 Win32 原名（`OpenClipboard`、`SendInput`…），方便对照文档；
* 每个函数都检查返回值，失败抛 `WinApiError`，携带 `GetLastError()`；
* `SetLastError` 只在必须拿到错误码的地方设置，避免无谓开销。
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes
from typing import List, Optional, Tuple

log = logging.getLogger("netclip.win")

IS_WINDOWS = sys.platform == "win32"


class WinApiError(OSError):
    """Win32 调用失败，`winerror` 为 GetLastError()。"""


if not IS_WINDOWS:  # pragma: no cover - 纯逻辑单测可在非 Windows 上跑
    raise ImportError("netclip.win 仅支持 Windows")


# ===================================================================== 常量

# --- 剪贴板格式 ---
CF_TEXT = 1
CF_BITMAP = 2
CF_METAFILEPICT = 3
CF_SYLK = 4
CF_DIF = 5
CF_TIFF = 6
CF_OEMTEXT = 7
CF_DIB = 8
CF_PALETTE = 9
CF_PENDATA = 10
CF_RIFF = 11
CF_WAVE = 12
CF_UNICODETEXT = 13
CF_ENHMETAFILE = 14
CF_HDROP = 15
CF_LOCALE = 16
CF_DIBV5 = 17
CF_OWNERDISPLAY = 0x0080
CF_DSPTEXT = 0x0081
CF_DSPBITMAP = 0x0082
CF_DSPMETAFILEPICT = 0x0083
CF_DSPENHMETAFILE = 0x008E
CF_PRIVATEFIRST = 0x0200
CF_PRIVATELAST = 0x02FF
CF_GDIOBJFIRST = 0x0300
CF_GDIOBJLAST = 0x03FF

# --- 需要显式跳过的"进程内句柄"格式---
# 这些格式的数据是 GDI 句柄或进程私有指针，直接跨机传过去毫无意义，
# 必须由各自的转换函数转成可序列化的形态（见 clip_formats.py）。
HANDLE_ONLY_FORMATS = frozenset({CF_BITMAP, CF_PALETTE, CF_OWNERDISPLAY, CF_DSPBITMAP})

# --- 需要显式转换的"结构 + 数据"格式 ---
STRUCT_FORMATS = frozenset({CF_METAFILEPICT})

# --- 文本类格式 ---
TEXT_FORMATS = frozenset({CF_TEXT, CF_OEMTEXT})

# --- 全局内存 ---
GMEM_MOVEABLE = 0x0002
GMEM_ZEROINIT = 0x0040
GMEM_DDESHARE = 0x2000
GMEM_SHARE = GMEM_DDESHARE

# --- 剪贴板打开 / 关闭 ---
CLIP_LOCK_RETRIES = (20, 40, 80, 160, 320)

# --- 鼠标 / 键盘钩子 ---
WH_MOUSE_LL = 14
WH_KEYBOARD_LL = 13
HC_ACTION = 0
LLMHF_INJECTED = 0x00000001
LLKHF_INJECTED = 0x00000010
LLKHF_EXTENDED = 0x00000001
LLKHF_UP = 0x00000080

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MOUSEWHEEL = 0x020A
WM_XBUTTONDOWN = 0x020B
WM_XBUTTONUP = 0x020C
WM_MOUSEHWHEEL = 0x020E

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

WM_CLIPBOARDUPDATE = 0x031D
WM_DESTROY = 0x0002
WM_QUIT = 0x0012
WM_APP = 0x8000
WM_USER = 0x0400
WM_TRAYICON = WM_APP + 1
WM_NETCLIP_QUIT = WM_APP + 2

# --- SendInput ---
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
INPUT_HARDWARE = 2

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_XDOWN = 0x0080
MOUSEEVENTF_XUP = 0x0100
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

XBUTTON1 = 0x0001
XBUTTON2 = 0x0002
WHEEL_DELTA = 120

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

# --- 虚拟键码 ---
VK_LBUTTON = 0x01
VK_RBUTTON = 0x02
VK_CANCEL = 0x03
VK_MBUTTON = 0x04
VK_XBUTTON1 = 0x05
VK_XBUTTON2 = 0x06
VK_BACK = 0x08
VK_TAB = 0x09
VK_RETURN = 0x0D
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt
VK_PAUSE = 0x13
VK_CAPITAL = 0x14
VK_ESCAPE = 0x1B
VK_SPACE = 0x20
VK_PRIOR = 0x21
VK_NEXT = 0x22
VK_END = 0x23
VK_HOME = 0x24
VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28
VK_SNAPSHOT = 0x2C
VK_INSERT = 0x2D
VK_DELETE = 0x2E
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_NUMPAD0 = 0x60
VK_NUMPAD9 = 0x69
VK_MULTIPLY = 0x6A
VK_ADD = 0x6B
VK_SEPARATOR = 0x6C
VK_SUBTRACT = 0x6D
VK_DECIMAL = 0x6E
VK_DIVIDE = 0x6F
VK_F1 = 0x70
VK_F24 = 0x87
VK_NUMLOCK = 0x90
VK_SCROLL = 0x91
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_LMENU = 0xA4
VK_RMENU = 0xA5
VK_OEM_1 = 0xBA
VK_OEM_PLUS = 0xBB
VK_OEM_COMMA = 0xBC
VK_OEM_MINUS = 0xBD
VK_OEM_PERIOD = 0xBE
VK_OEM_2 = 0xBF
VK_OEM_3 = 0xC0
VK_OEM_4 = 0xDB
VK_OEM_5 = 0xDC
VK_OEM_6 = 0xDD
VK_OEM_7 = 0xDE
VK_OEM_102 = 0xE2
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_STOP = 0xB2
VK_MEDIA_PLAY_PAUSE = 0xB3
VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF

# --- 系统度量 ---
SM_CXSCREEN = 0
SM_CYSCREEN = 1
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
SM_CMONITORS = 80

# --- 消息窗口 ---
HWND_MESSAGE = wintypes.HWND(-3)
INFINITE = 0xFFFFFFFF
PM_REMOVE = 0x0001
PM_NOREMOVE = 0x0000
SW_SHOW = 5


# --- 菜单（托盘右键菜单）---
MF_STRING = 0x0000
MF_POPUP = 0x0010
MF_SEPARATOR = 0x0800
MF_GRAYED = 0x0001
MF_DISABLED = 0x0002
MF_CHECKED = 0x0008
TPM_LEFTALIGN = 0x0000
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
TPM_NONOTIFY = 0x0080

# --- 托盘图标 ---
NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_INFO = 0x00000010
NIIF_INFO = 0x00000001
NIIF_WARNING = 0x00000002
NIIF_ERROR = 0x00000003
NIIF_NOSOUND = 0x00000010

WM_LBUTTONDBLCLK = 0x0203
WM_TIMER = 0x0113
WM_COMMAND = 0x0111
WM_CONTEXTMENU = 0x007B

# --- 与托盘消息窗口通信的自定义消息 ---
WM_TRAY_NOTIFY = WM_APP + 30
WM_TRAY_UPDATE = WM_APP + 31
WM_TRAY_QUIT = WM_APP + 32

# --- 图标加载 ---
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040
LR_SHARED = 0x8000
IDI_APPLICATION = 32512
IDI_INFORMATION = 32516

# --- 逻辑字体（托盘图标上的文字）---
ANSI_CHARSET = 0
OUT_DEFAULT_PRECIS = 0
CLIP_DEFAULT_PRECIS = 0
DEFAULT_QUALITY = 0
FF_DONTCARE = 0
TRANSPARENT = 1
DT_CENTER = 0x0001
DT_VCENTER = 0x0004
DT_SINGLELINE = 0x0020


# ===================================================================== 结构体


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", POINT),
    ]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class METAFILEPICT(ctypes.Structure):
    """CF_METAFILEPICT 的数据结构：后面紧跟 HMETAFILE 的位数据（需单独取）。"""

    _fields_ = [
        ("mm", wintypes.LONG),
        ("xExt", wintypes.LONG),
        ("yExt", wintypes.LONG),
        ("hMF", wintypes.HANDLE),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class DROPFILES(ctypes.Structure):
    _fields_ = [
        ("pFiles", wintypes.DWORD),
        ("pt", POINT),
        ("fNC", wintypes.BOOL),
        ("fWide", wintypes.BOOL),
    ]


class BITMAP(ctypes.Structure):
    _fields_ = [
        ("bmType", wintypes.LONG),
        ("bmWidth", wintypes.LONG),
        ("bmHeight", wintypes.LONG),
        ("bmWidthBytes", wintypes.LONG),
        ("bmPlanes", wintypes.WORD),
        ("bmBitsPixel", wintypes.WORD),
        ("bmBits", ctypes.c_void_p),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    """`Shell_NotifyIconW` 用的结构。

    这是**变长结构**：`szTip`/`szInfo`/`szInfoTitle` 的长度由 Windows 版本
    对应的固定值决定。必须按真实的 Vista+ 布局定义（`szTip[128]`、
    `szInfo[256]`、`szInfoTitle[64]`），否则系统会读越界内存 —— 这是托盘
    代码最经典的崩溃原因。
    """

    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]


class LOGFONTW(ctypes.Structure):
    _fields_ = [
        ("lfHeight", wintypes.LONG),
        ("lfWidth", wintypes.LONG),
        ("lfEscapement", wintypes.LONG),
        ("lfOrientation", wintypes.LONG),
        ("lfWeight", wintypes.LONG),
        ("lfItalic", ctypes.c_byte),
        ("lfUnderline", ctypes.c_byte),
        ("lfStrikeOut", ctypes.c_byte),
        ("lfCharSet", ctypes.c_byte),
        ("lfOutPrecision", ctypes.c_byte),
        ("lfClipPrecision", ctypes.c_byte),
        ("lfQuality", ctypes.c_byte),
        ("lfPitchAndFamily", ctypes.c_byte),
        ("lfFaceName", wintypes.WCHAR * 32),
    ]


# ===================================================================== DLL 绑定

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
ole32 = ctypes.WinDLL("ole32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

LRESULT = ctypes.c_ssize_t

#: COM 的 `HRESULT`：`S_OK == 0`，失败是负值。
HRESULT = ctypes.c_long

# --- 问出"剪贴板是哪个进程放的" -----------------------------------------------
#
# 真机 A/B 的结论：同一个 `Ole Private Data` 在 WPS 演示和 Word 上要求**相反** ——
# PPT 要它不在（否则形状退成图片），Word 要它在（否则文字粘不了）。
# 判据就用**复制来源进程名**：那是直接可观测的，不用猜格式。
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]

# --- 把剪贴板交给 OLE 重新接管 -----------------------------------------------
#
# 裸 `SetClipboardData` 写出来的剪贴板**没有 OLE 数据对象的身份**，而 Office/WPS
# 的粘贴走的是 `OleGetClipboard`。这三步让 OLE 自己接管，并生成属于**本机**的
# `Ole Private Data`，而不是把源机器的封送引用原样搬过去（那是悬空引用）。
# 详见 `clipboard.bless_clipboard_with_ole()` 的说明。
ole32.OleInitialize.restype = HRESULT
ole32.OleInitialize.argtypes = [ctypes.c_void_p]
ole32.OleGetClipboard.restype = HRESULT
ole32.OleGetClipboard.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
ole32.OleSetClipboard.restype = HRESULT
ole32.OleSetClipboard.argtypes = [ctypes.c_void_p]
ole32.OleFlushClipboard.restype = HRESULT
ole32.OleFlushClipboard.argtypes = []

HHOOK = ctypes.c_void_p
HGLOBAL = ctypes.c_void_p
HANDLE = ctypes.c_void_p
HMENU = ctypes.c_void_p
HICON = ctypes.c_void_p
HBRUSH = ctypes.c_void_p

HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

user32.SetWindowsHookExW.restype = HHOOK
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.CallNextHookEx.restype = LRESULT
user32.CallNextHookEx.argtypes = [HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.UnhookWindowsHookEx.argtypes = [HHOOK]
user32.GetMessageW.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.PeekMessageW.restype = wintypes.BOOL
user32.PeekMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.PostThreadMessageW.restype = wintypes.BOOL
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

user32.GetSystemMetrics.restype = ctypes.c_int
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetCursorPos.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
user32.SetCursorPos.restype = wintypes.BOOL
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.GetKeyState.restype = ctypes.c_short
user32.GetKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.MapVirtualKeyW.restype = wintypes.UINT
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.GetKeyboardLayout.restype = wintypes.HANDLE
user32.GetKeyboardLayout.argtypes = [wintypes.DWORD]

user32.OpenClipboard.restype = wintypes.BOOL
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.restype = wintypes.BOOL
user32.GetClipboardOwner.restype = wintypes.HWND
user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
user32.GetClipboardData.restype = HANDLE
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.SetClipboardData.restype = HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, HANDLE]
user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
user32.EnumClipboardFormats.restype = wintypes.UINT
user32.EnumClipboardFormats.argtypes = [wintypes.UINT]
user32.RegisterClipboardFormatW.restype = wintypes.UINT
user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
user32.GetClipboardFormatNameW.restype = ctypes.c_int
user32.GetClipboardFormatNameW.argtypes = [wintypes.UINT, wintypes.LPWSTR, ctypes.c_int]
user32.AddClipboardFormatListener.restype = wintypes.BOOL
user32.AddClipboardFormatListener.argtypes = [wintypes.HWND]
user32.RemoveClipboardFormatListener.restype = wintypes.BOOL
user32.RemoveClipboardFormatListener.argtypes = [wintypes.HWND]

user32.CreateWindowExW.restype = wintypes.HWND
user32.DestroyWindow.restype = wintypes.BOOL
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.RegisterClassExW.restype = wintypes.ATOM
user32.RegisterClassExW.argtypes = [ctypes.c_void_p]

#: 取一个"消息号"，用来接收资源管理器重启后广播的 `TaskbarCreated`。
#: 消息号是运行期分配的，没有固定值 —— 每个进程各注册一次，拿到的号一样。
user32.RegisterWindowMessageW.restype = wintypes.UINT
user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
user32.LoadCursorW.restype = HICON
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
user32.LoadImageW.restype = HANDLE
user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.DestroyIcon.restype = wintypes.BOOL
user32.DestroyIcon.argtypes = [HICON]
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.restype = ctypes.c_int
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]

kernel32.GlobalAlloc.restype = HGLOBAL
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalFree.restype = HGLOBAL
kernel32.GlobalFree.argtypes = [HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [HGLOBAL]
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalUnlock.argtypes = [HGLOBAL]
kernel32.GlobalSize.restype = ctypes.c_size_t
kernel32.GlobalSize.argtypes = [HGLOBAL]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
kernel32.GetTickCount64.restype = ctypes.c_ulonglong
kernel32.SetThreadExecutionState.restype = wintypes.DWORD
kernel32.SetThreadExecutionState.argtypes = [wintypes.DWORD]

# --- GDI ---
#
# **句柄类的返回类型一律声明成 `HANDLE`(=c_void_p)。**
# Windows 的 GDI 句柄可以是高地址值（表现为很大的无符号数，按有符号看是负数）。
# 如果 restype 留成默认的 c_int，ctypes 会在返回时抛
# `OverflowError: int too long to convert`，或者更糟：静默截断成负值，
# 之后拿它去调别的函数就变成"句柄无效"。这类问题只在部分机器上复现，极难查。
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.CreateDIBitmap.restype = wintypes.HBITMAP
gdi32.CreateDIBitmap.argtypes = [wintypes.HDC, ctypes.POINTER(BITMAPINFOHEADER), wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC,
    ctypes.POINTER(BITMAPINFOHEADER),
    wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p),
    wintypes.HANDLE,
    wintypes.DWORD,
]
gdi32.GetDIBits.restype = ctypes.c_int
gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
gdi32.GetObjectW.restype = ctypes.c_int
gdi32.GetObjectW.argtypes = [HANDLE, ctypes.c_int, ctypes.c_void_p]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteObject.argtypes = [HANDLE]
gdi32.GetEnhMetaFileBits.restype = wintypes.UINT
gdi32.GetEnhMetaFileBits.argtypes = [wintypes.HENHMETAFILE, wintypes.UINT, ctypes.c_void_p]
gdi32.SetEnhMetaFileBits.restype = wintypes.HENHMETAFILE
gdi32.SetEnhMetaFileBits.argtypes = [wintypes.UINT, ctypes.c_void_p]
gdi32.GetMetaFileBitsEx.restype = wintypes.UINT
gdi32.GetMetaFileBitsEx.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_void_p]
gdi32.SetMetaFileBitsEx.restype = wintypes.HANDLE
gdi32.SetMetaFileBitsEx.argtypes = [wintypes.UINT, ctypes.c_void_p]
gdi32.DeleteMetaFile.restype = wintypes.BOOL
gdi32.DeleteMetaFile.argtypes = [HANDLE]
gdi32.DeleteEnhMetaFile.restype = wintypes.BOOL
gdi32.DeleteEnhMetaFile.argtypes = [wintypes.HENHMETAFILE]

# --- 托盘 / 菜单 / 简单 GDI 绘制（图标是运行时画出来的）---
shell32.Shell_NotifyIconW.restype = wintypes.BOOL
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]

user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.restype = wintypes.BOOL
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
user32.DestroyMenu.restype = wintypes.BOOL
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.TrackPopupMenu.restype = wintypes.BOOL
user32.TrackPopupMenu.argtypes = [
    wintypes.HMENU,
    wintypes.UINT,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    ctypes.c_void_p,
]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.LoadIconW.restype = wintypes.HICON
user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
user32.GetDC.restype = wintypes.HDC
user32.SetTimer.restype = ctypes.c_size_t  # UINT_PTR
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
user32.KillTimer.restype = wintypes.BOOL
user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]

gdi32.CreateFontIndirectW.restype = wintypes.HANDLE
gdi32.CreateFontIndirectW.argtypes = [ctypes.POINTER(LOGFONTW)]
gdi32.SelectObject.restype = wintypes.HANDLE
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
gdi32.SetTextColor.restype = wintypes.DWORD
gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.DWORD]
gdi32.SetBkMode.restype = ctypes.c_int
gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]

# DrawTextW 在 user32 里，不在 gdi32
user32.DrawTextW.restype = ctypes.c_int
user32.DrawTextW.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, ctypes.c_void_p, wintypes.UINT]
# CreateIconIndirect 也在 user32
user32.CreateIconIndirect.restype = wintypes.HICON
user32.CreateIconIndirect.argtypes = [ctypes.c_void_p]

shell32.DragQueryFileW.restype = wintypes.UINT
shell32.DragQueryFileW.argtypes = [HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
shell32.DragQueryFileA.restype = wintypes.UINT
shell32.DragQueryFileA.argtypes = [HANDLE, wintypes.UINT, ctypes.c_char_p, wintypes.UINT]
shell32.DragFinish.argtypes = [HANDLE]


# ===================================================================== 辅助


def last_error() -> int:
    return ctypes.get_last_error()


def _fail(what: str) -> "WinApiError":
    err = ctypes.get_last_error()
    return WinApiError(err, "%s 失败 (GetLastError=%d)" % (what, err))


def check(result: int, what: str) -> int:
    if not result:
        raise _fail(what)
    return result


# --------------------------------------------------------------- 屏幕 / 光标


def get_cursor_pos() -> Tuple[int, int]:
    pt = POINT()
    check(user32.GetCursorPos(ctypes.byref(pt)), "GetCursorPos")
    return pt.x, pt.y


def set_cursor_pos(x: int, y: int) -> None:
    check(user32.SetCursorPos(int(x), int(y)), "SetCursorPos")


def get_screen_rect() -> Tuple[int, int, int, int]:
    """返回主屏 (x, y, w, h)。"""
    return (0, 0, user32.GetSystemMetrics(SM_CXSCREEN), user32.GetSystemMetrics(SM_CYSCREEN))


# --------------------------------------------------------------- DPI 感知


#: `SetProcessDpiAwarenessContext` 的"每显示器感知 v2"上下文句柄
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4


def current_dpi_awareness() -> str:
    """读回本进程当前的 DPI 感知级别，用于日志。"""
    try:
        value = ctypes.c_int()
        ctypes.windll.shcore.GetProcessDpiAwareness(None, ctypes.byref(value))
        return {
            0: "不感知(UNAWARE)",
            1: "系统感知(SYSTEM)",
            2: "每显示器感知(PER_MONITOR)",
        }.get(value.value, "未知(%d)" % value.value)
    except Exception:  # pragma: no cover - 老系统
        return "读取失败"


def ensure_dpi_awareness() -> str:
    """尽力把本进程设成"每显示器 DPI 感知"，返回一句可写进日志的结果描述。

    **为什么必须在启动时显式设定，而不能假设**：DPI 不感知的进程会被 Windows
    **虚拟化**整个坐标系统 —— 150% 缩放下 `GetSystemMetrics` 会把 1920x1200 报成
    1280x800。而 netclip 需要对端知道真实的屏幕尺寸（几何全靠它算），还要和鼠标
    钩子、`GetCursorPos`/`SetCursorPos` 对齐坐标空间，一旦被虚拟化，屏幕边界就
    落在错误的位置上（真机实测：光标只到屏幕 80% 处就被判成"越界"）。

    进程的感知级别由三样东西决定，且**同一台机器换个启动方式就可能不一样**：

      1. `python.exe` 的清单（现代 CPython 声明了 PerMonitorV2）；
      2. 兼容性标志 `HKCU\\...\\AppCompatFlags\\Layers`（真机上 python.exe 上
         被挂过 `~ HIGHDPIAWARE`）；
      3. 从父进程**继承**（父进程不感知且自己没有清单时）。

    所以这里不猜：先读当前级别，不达标才逐个 API 试，最后读回真实状态。
    """
    before = current_dpi_awareness()
    if before.startswith("每显示器") or before.startswith("系统"):
        return "已是 %s，无需设置" % before

    attempts = []
    try:
        fn = user32.SetProcessDpiAwarenessContext
        fn.restype = wintypes.BOOL
        fn.argtypes = [ctypes.c_void_p]
        if fn(ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)):
            return "原为 %s，已改为 %s（每显示器感知 v2）" % (before, current_dpi_awareness())
        attempts.append("SetProcessDpiAwarenessContext 返回失败")
    except Exception as exc:
        attempts.append("SetProcessDpiAwarenessContext 不可用: %s" % exc)

    try:
        result = int(ctypes.windll.shcore.SetProcessDpiAwareness(2))
        if result == 0:
            return "原为 %s，已改为 %s（每显示器感知）" % (before, current_dpi_awareness())
        attempts.append("SetProcessDpiAwareness(2) 返回 %d" % result)
    except Exception as exc:
        attempts.append("shcore 不可用: %s" % exc)

    try:
        user32.SetProcessDPIAware()
        after = current_dpi_awareness()
        if after.startswith("每显示器") or after.startswith("系统"):
            return "原为 %s，已改为 %s（系统感知，精度低于每显示器）" % (before, after)
        attempts.append("SetProcessDPIAware 后仍是 %s" % after)
    except Exception as exc:
        attempts.append("SetProcessDPIAware 不可用: %s" % exc)

    return "**设置失败**，当前 %s：%s （坐标系可能被虚拟化，屏幕边界会算错）" % (
        current_dpi_awareness(),
        "；".join(attempts),
    )


def get_virtual_screen_rect() -> Tuple[int, int, int, int]:
    """返回整个虚拟桌面 (x, y, w, h)；多显示器时 x/y 可能为负。"""
    return (
        user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_CYVIRTUALSCREEN),
    )


def monitor_count() -> int:
    return user32.GetSystemMetrics(SM_CMONITORS)


# --------------------------------------------------------------- 权限级别

#: `GetTokenInformation` 的 `TokenElevation` 信息类
TOKEN_ELEVATION = 20
TOKEN_QUERY = 0x0008

kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.OpenProcessToken.restype = wintypes.BOOL
kernel32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
advapi32.GetTokenInformation.restype = wintypes.BOOL
advapi32.GetTokenInformation.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]


def is_elevated() -> bool:
    """本进程是否以管理员（高完整性级别）身份运行。

    **这不是可有可无的自检。** Windows 的 UIPI 规定：未提权的进程，其低级钩子
    （`WH_MOUSE_LL` / `WH_KEYBOARD_LL`）和 `SendInput` 对**已提权窗口**（任务管理器、
    注册表编辑器、安装程序……）一律无效。现象是鼠标一移到那个窗口上就"卡住" ——
    其实是钩子被系统静默屏蔽，转发链从源头就断了，我们连事件都收不到。

    这是系统限制而不是程序缺陷：Deskflow 有同样的 issue（#8611），QQ 远程桌面也一样。
    唯一的解是让 netclip 本身提权运行（高完整性 -> 低完整性是允许的）。所以启动时
    把它记进日志，用户遇到"卡住"能立刻对上号，而不是又去翻鼠标算法。
    """
    token = wintypes.HANDLE()
    if not kernel32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        return False
    try:
        elevated = wintypes.DWORD()
        returned = wintypes.DWORD()
        ok = advapi32.GetTokenInformation(
            token,
            TOKEN_ELEVATION,
            ctypes.byref(elevated),
            ctypes.sizeof(elevated),
            ctypes.byref(returned),
        )
        return bool(ok and elevated.value)
    finally:
        kernel32.CloseHandle(token)


# --------------------------------------------------------------- 状态查询


def is_key_down(vk: int) -> bool:
    """用 GetAsyncKeyState 判断物理按键当前是否按下（不受消息队列影响）。"""
    return bool(user32.GetAsyncKeyState(int(vk)) & 0x8000)


def toggled_state(vk: int) -> bool:
    """CapsLock/NumLock/ScrollLock 等开关状态。"""
    return bool(user32.GetKeyState(int(vk)) & 0x0001)


# --------------------------------------------------------------- 全局内存


def global_alloc(size: int, zero_init: bool = True) -> HGLOBAL:
    flags = GMEM_MOVEABLE | (GMEM_ZEROINIT if zero_init else 0)
    handle = kernel32.GlobalAlloc(flags, max(1, int(size)))
    if not handle:
        raise _fail("GlobalAlloc")
    return handle


class GlobalMem:
    """HGLOBAL 的 RAII 包装：自动 GlobalLock/Unlock，可选自动 GlobalFree。

    `release=False` 时所有权交给剪贴板（SetClipboardData 成功后系统接管内存）。
    """

    __slots__ = ("handle", "_ptr", "_released", "_size")

    def __init__(self, size: int, zero_init: bool = True) -> None:
        self.handle = global_alloc(size, zero_init)
        self._ptr = kernel32.GlobalLock(self.handle)
        if not self._ptr:
            kernel32.GlobalFree(self.handle)
            self.handle = None
            raise _fail("GlobalLock")
        self._size = int(size)
        self._released = False

    @classmethod
    def from_bytes(cls, data: bytes, extra_pad: int = 0) -> "GlobalMem":
        mem = cls(len(data) + extra_pad, zero_init=extra_pad > 0)
        ctypes.memmove(mem.ptr, data, len(data))
        if extra_pad:
            # 已经 zero_init，但显式再清一遍尾部，确保 CF_HDROP 的双 \\0 终止正确
            ctypes.memset(mem.ptr + len(data), 0, extra_pad)
        return mem

    @property
    def ptr(self) -> int:
        return self._ptr

    @property
    def size(self) -> int:
        return self._size

    def read(self, length: Optional[int] = None) -> bytes:
        n = self._size if length is None else min(int(length), self._size)
        return ctypes.string_at(self._ptr, n)

    def write(self, offset: int, data: bytes) -> None:
        if offset < 0 or offset + len(data) > self._size:
            raise ValueError("写入越界: offset=%d len=%d size=%d" % (offset, len(data), self._size))
        ctypes.memmove(self._ptr + offset, data, len(data))

    def release_to_clipboard(self) -> HGLOBAL:
        """把所有权交给剪贴板：解锁但不释放。

        调用方必须在 `SetClipboardData` **之前**调用它，并且之后不能再碰这块内存。
        """
        if self._ptr:
            kernel32.GlobalUnlock(self.handle)
            self._ptr = 0
        self._released = True
        return self.handle

    def close(self) -> None:
        if self._released:
            self.handle = None
            return
        if self._ptr:
            kernel32.GlobalUnlock(self.handle)
            self._ptr = 0
        if self.handle:
            kernel32.GlobalFree(self.handle)
            self.handle = None

    def __enter__(self) -> "GlobalMem":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def global_mem_bytes(handle: HGLOBAL) -> bytes:
    """把一个 HGLOBAL 的内容整个读出来（只读，不接管所有权）。

    注意：`GlobalSize` 返回的是**分配大小**，Windows 会向上取整（通常 256 字节），
    所以对于自己 `GlobalAlloc` 出来的块，尾部可能包含非零垃圾。调用方若有精确
    长度信息，请使用 `global_mem_read(handle, length)` 指定长度。
    """
    if not handle:
        return b""
    size = kernel32.GlobalSize(handle)
    if not size:
        return b""
    return global_mem_read(handle, int(size))


def global_mem_read(handle: HGLOBAL, length: int) -> bytes:
    """按指定长度读取 HGLOBAL 内容，避免读到 GlobalAlloc 的取整填充。"""
    if not handle or length <= 0:
        return b""
    ptr = kernel32.GlobalLock(handle)
    if not ptr:
        raise _fail("GlobalLock")
    try:
        return ctypes.string_at(ptr, int(length))
    finally:
        kernel32.GlobalUnlock(handle)


def global_mem_clipboard_size(handle: HGLOBAL) -> int:
    """推断"剪贴板数据"的实际长度，剔除 GlobalAlloc 的尾部取整填充。

    做法：先按 GlobalSize 读一遍，再从尾部去掉零字节。对文本格式这是天然的
    终止符处理；对二进制格式，剪贴板数据的尾部填充必然是 0（我们在写入时
    用 GMEM_ZEROINIT），所以同样成立。**不要**用它处理需要精确保留尾部
    零字节的自描述格式（DIB/EMF），那种情况用格式自身的长度字段。
    """
    raw = global_mem_bytes(handle)
    if not raw:
        return 0
    end = len(raw)
    while end > 0 and raw[end - 1] == 0:
        end -= 1
    return end


def global_mem_size(handle: HGLOBAL) -> int:
    if not handle:
        return 0
    return int(kernel32.GlobalSize(handle))


# --------------------------------------------------------------- 格式名


def clip_format_name(fmt: int) -> str:
    """把剪贴板格式号转成可读名字。

    注册格式（>= 0xC000）返回它注册时用的字符串；标准格式返回 `CF_XXX`；
    未知返回 `0xXXXX`。
    """
    standard = {
        CF_TEXT: "CF_TEXT",
        CF_BITMAP: "CF_BITMAP",
        CF_METAFILEPICT: "CF_METAFILEPICT",
        CF_SYLK: "CF_SYLK",
        CF_DIF: "CF_DIF",
        CF_TIFF: "CF_TIFF",
        CF_OEMTEXT: "CF_OEMTEXT",
        CF_DIB: "CF_DIB",
        CF_PALETTE: "CF_PALETTE",
        CF_PENDATA: "CF_PENDATA",
        CF_RIFF: "CF_RIFF",
        CF_WAVE: "CF_WAVE",
        CF_UNICODETEXT: "CF_UNICODETEXT",
        CF_ENHMETAFILE: "CF_ENHMETAFILE",
        CF_HDROP: "CF_HDROP",
        CF_LOCALE: "CF_LOCALE",
        CF_DIBV5: "CF_DIBV5",
        CF_OWNERDISPLAY: "CF_OWNERDISPLAY",
        CF_DSPTEXT: "CF_DSPTEXT",
        CF_DSPBITMAP: "CF_DSPBITMAP",
        CF_DSPMETAFILEPICT: "CF_DSPMETAFILEPICT",
        CF_DSPENHMETAFILE: "CF_DSPENHMETAFILE",
    }
    if fmt in standard:
        return standard[fmt]
    buf = ctypes.create_unicode_buffer(512)
    n = user32.GetClipboardFormatNameW(fmt, buf, 512)
    if n > 0:
        return buf.value
    return "0x%04X" % fmt


def register_clip_format(name: str) -> int:
    """注册（或查询已注册的）剪贴板格式。

    **按名字注册是跨机传输的关键**：格式号是运行时分配的，两台机器上同一个
    私有格式的数字很可能不同，只有名字是稳定的。
    """
    fmt = user32.RegisterClipboardFormatW(name)
    if not fmt:
        raise _fail("RegisterClipboardFormatW(%r)" % name)
    return int(fmt)


def is_standard_format(fmt: int) -> bool:
    return 1 <= fmt <= 0x0017


__all__ = [name for name in dir() if not name.startswith("_")]
