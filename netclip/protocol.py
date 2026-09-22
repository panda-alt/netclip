"""TCP 帧编解码与消息类型定义。

帧结构（大端）::

    +--------+--------+--------+--------+--------+--------+
    | magic  | flags  |  type  | jsonlen| binlen |  rsvd  |
    | 2 字节 | 1 字节 | 1 字节 | 4 字节 | 4 字节 | 2 字节 |
    +--------+--------+--------+--------+--------+--------+
    |   JSON 头 (jsonlen, UTF-8, 通常是 {'v': ...} 或 {...})  |
    +-------------------------------------------------------+
    |   二进制体 (binlen)                                    |
    +-------------------------------------------------------+

设计取舍：
  * 头部定长 14 字节 + magic，便于流式解析和异常时重新同步；
  * 二进制体单独成段而不是塞进 JSON 的 base64 —— 剪贴板 DIB / 文件块动辄几十 MB，
    base64 要多吃 33% 带宽和一次额外拷贝；
  * flags 保留给"压缩/优先级"扩展；压缩对 DIB 收益明显，后续在此位标记。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import struct
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

MAGIC = 0x4E43  # 'NC'
HEADER = struct.Struct(">HBBIIH")
HEADER_SIZE = HEADER.size  # 14

FLAG_NONE = 0x00
FLAG_ZLIB = 0x01
FLAG_URGENT = 0x02  # 输入通道优先插队

# 帧上限：防御性设置，避免对端 bug 导致内存爆炸。文件块本身切成 <=1MB。
MAX_FRAME_BYTES = 64 * 1024 * 1024
MAX_JSON_BYTES = 4 * 1024 * 1024

PROTOCOL_VERSION = 1


# --------------------------------------------------------------------- 消息类型


class MsgType:
    """消息类型常量。0x00-0x0F 控制，0x10-0x2F 输入，0x30-0x4F 剪贴板，0x50-0x6F 文件。"""

    # 控制平面（三类通道共用）
    HELLO = 0x01
    HELLO_ACK = 0x02
    HELLO_REJECT = 0x03
    PING = 0x04
    PONG = 0x05
    BYE = 0x06

    # 输入平面（input 通道）
    MOUSE_MOVE = 0x10
    MOUSE_BUTTON = 0x11
    MOUSE_WHEEL = 0x12
    KEY = 0x13
    ENTER = 0x14  # 光标进入对端
    LEAVE = 0x15  # 光标离开对端
    CLAMP = 0x16  # 对端把自己光标钉在边缘（告知本机，便于同步状态）
    RELEASE_ALL = 0x17  # 断线/切换时释放所有按下的键鼠

    # 剪贴板平面（clip 通道）
    CLIP_ANNOUNCE = 0x30  # 声明一次剪贴板更新的元数据（格式清单 + 各段大小）
    CLIP_BEGIN = 0x31  # 开始传输某次剪贴板内容
    CLIP_CHUNK = 0x32  # 二进制分片（可附 json {k: 键名, off: 偏移}）
    CLIP_END = 0x33
    CLIP_ACK = 0x34
    CLIP_SKIP = 0x35  # 对端拒绝（超大 / 不支持）

    # 文件平面（file 通道）
    FILE_BEGIN = 0x50  # 清单：id + [{name,size,mtime,rel}]
    FILE_CHUNK = 0x51  # {id, idx, off} + 二进制块
    FILE_END = 0x52  # {id, idx, sha256}
    FILE_ACK = 0x53  # {id, skip:[idx...]}
    FILE_DONE = 0x54  # 全部完成，接收端回执


_NAMES: Dict[int, str] = {v: k for k, v in vars(MsgType).items() if not k.startswith("_") and isinstance(v, int)}


def msg_name(value: int) -> str:
    return _NAMES.get(value, "0x%02X" % value)


# --------------------------------------------------------------------- 帧对象


class ProtocolError(Exception):
    pass


@dataclass
class Frame:
    type: int
    body: Dict[str, Any] = field(default_factory=dict)
    blob: bytes = b""
    flags: int = FLAG_NONE

    def encode(self) -> bytes:
        body = self.body or {}
        js = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(js) > MAX_JSON_BYTES:
            raise ProtocolError("JSON 头过大: %d 字节" % len(js))
        blob = self.blob or b""
        total = HEADER_SIZE + len(js) + len(blob)
        if total > MAX_FRAME_BYTES:
            raise ProtocolError("帧过大: %d 字节" % total)
        return HEADER.pack(MAGIC, self.flags & 0xFF, self.type & 0xFF, len(js), len(blob), 0) + js + blob


def decode_header(raw: bytes) -> "tuple[int, int, int, int]":
    """解析 14 字节帧头，返回 (flags, type, jsonlen, binlen)。"""
    if len(raw) != HEADER_SIZE:
        raise ProtocolError("帧头长度错误: %d" % len(raw))
    magic, flags, mtype, jsonlen, binlen, _rsvd = HEADER.unpack(raw)
    if magic != MAGIC:
        raise ProtocolError("magic 不匹配: 0x%04X" % magic)
    if jsonlen > MAX_JSON_BYTES:
        raise ProtocolError("JSON 头超大: %d" % jsonlen)
    if HEADER_SIZE + jsonlen + binlen > MAX_FRAME_BYTES:
        raise ProtocolError("声明帧过大: %d" % (HEADER_SIZE + jsonlen + binlen))
    return flags, mtype, jsonlen, binlen


def decode_body(json_bytes: bytes, blob: bytes = b"") -> Dict[str, Any]:
    if not json_bytes:
        return {}
    try:
        parsed = json.loads(json_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("JSON 解析失败: %s" % exc) from None
    if not isinstance(parsed, dict):
        raise ProtocolError("JSON 头必须是对象，得到 %s" % type(parsed).__name__)
    return parsed


def decode(raw: bytes) -> Frame:
    """解码一个完整帧（单测与内存内使用；流式读取见 net/channel.py）。"""
    if len(raw) < HEADER_SIZE:
        raise ProtocolError("数据不足一个帧头")
    flags, mtype, jsonlen, binlen = decode_header(raw[:HEADER_SIZE])
    end = HEADER_SIZE + jsonlen + binlen
    if len(raw) < end:
        raise ProtocolError("数据不足一个完整帧: 需要 %d，实际 %d" % (end, len(raw)))
    body = decode_body(raw[HEADER_SIZE : HEADER_SIZE + jsonlen], raw[HEADER_SIZE + jsonlen : end])
    return Frame(type=mtype, body=body, blob=raw[HEADER_SIZE + jsonlen : end], flags=flags)


# --------------------------------------------------------------------- 握手


def auth_tag(psk: str, nonce: bytes, device_name: str) -> bytes:
    """握手校验标签。

    nonce 用 os.urandom(16)，每次连接重新生成，因此标签不可重放。
    仅做"防误连/防串台"，不做机密性保护（局域网内明文传输，见 README 的安全说明）。
    """
    mac = hmac.new(psk.encode("utf-8"), digestmod=hashlib.sha256)
    mac.update(nonce)
    mac.update(b"\x00")
    mac.update(device_name.encode("utf-8"))
    mac.update(b"\x00v%d" % PROTOCOL_VERSION)
    return mac.digest()


def new_nonce() -> bytes:
    return os.urandom(16)


def make_hello(device_name: str, psk: str, extra: Optional[Dict[str, Any]] = None) -> Frame:
    nonce = new_nonce()
    body: Dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "name": device_name,
        "nonce": nonce.hex(),
        "tag": auth_tag(psk, nonce, device_name).hex(),
    }
    if extra:
        body.update(extra)
    return Frame(type=MsgType.HELLO, body=body)


def verify_hello(body: Dict[str, Any], psk: str) -> bool:
    try:
        nonce = bytes.fromhex(str(body["nonce"]))
        tag = bytes.fromhex(str(body["tag"]))
        name = str(body["name"])
    except (KeyError, ValueError):
        return False
    if len(nonce) != 16:
        return False
    return hmac.compare_digest(tag, auth_tag(psk, nonce, name))
