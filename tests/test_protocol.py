"""协议帧编解码与握手的单元测试。纯逻辑，不依赖 Windows。"""

from __future__ import annotations

import json
import struct

import pytest

from netclip.protocol import (
    HEADER_SIZE,
    MAGIC,
    MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
    Frame,
    MsgType,
    ProtocolError,
    auth_tag,
    decode,
    decode_body,
    decode_header,
    make_hello,
    msg_name,
    new_nonce,
    verify_hello,
)


# --------------------------------------------------------------------- 帧编解码


def test_frame_roundtrip_without_blob():
    frame = Frame(MsgType.MOUSE_MOVE, {"dx": -3, "dy": 7})
    decoded = decode(frame.encode())
    assert decoded.type == MsgType.MOUSE_MOVE
    assert decoded.body == {"dx": -3, "dy": 7}
    assert decoded.blob == b""


def test_frame_roundtrip_with_blob():
    payload = bytes(range(256)) * 8
    frame = Frame(MsgType.CLIP_CHUNK, {"k": "CF_DIB", "off": 4096}, payload)
    decoded = decode(frame.encode())
    assert decoded.body["k"] == "CF_DIB"
    assert decoded.body["off"] == 4096
    assert decoded.blob == payload


def test_empty_body_is_allowed():
    decoded = decode(Frame(MsgType.PING).encode())
    assert decoded.body == {}


def test_unicode_body_survives():
    text = "中文✅emoji🎯"
    decoded = decode(Frame(MsgType.CLIP_ANNOUNCE, {"preview": text}).encode())
    assert decoded.body["preview"] == text


def test_header_layout_is_stable():
    """帧头格式一旦变化，两端新旧版本就不兼容了，所以钉死它。"""
    raw = Frame(MsgType.KEY, {"a": 1}, b"xy").encode()
    magic, flags, mtype, jsonlen, binlen, rsvd = struct.unpack(">HBBIIH", raw[:HEADER_SIZE])
    assert magic == MAGIC
    assert flags == 0
    assert mtype == MsgType.KEY
    assert jsonlen == len(json.dumps({"a": 1}, separators=(",", ":")).encode("utf-8"))
    assert binlen == 2
    assert rsvd == 0
    assert len(raw) == HEADER_SIZE + jsonlen + binlen


def test_decode_rejects_bad_magic():
    raw = bytearray(Frame(MsgType.PING, {}).encode())
    raw[0] = 0xFF
    with pytest.raises(ProtocolError, match="magic"):
        decode(bytes(raw))


def test_decode_rejects_oversized_declaration():
    bad = struct.pack(">HBBIIH", MAGIC, 0, MsgType.FILE_CHUNK, 0, MAX_FRAME_BYTES, 0)
    with pytest.raises(ProtocolError, match="过大"):
        decode_header(bad)


def test_decode_rejects_short_buffer():
    with pytest.raises(ProtocolError):
        decode(b"\x4e\x43\x00")


def test_decode_rejects_non_object_json():
    body = json.dumps([1, 2, 3]).encode("utf-8")
    with pytest.raises(ProtocolError, match="对象"):
        decode_body(body)


def test_decode_rejects_broken_json():
    with pytest.raises(ProtocolError, match="JSON"):
        decode_body(b"{not json")


def test_encode_rejects_too_large_blob():
    with pytest.raises(ProtocolError):
        Frame(MsgType.FILE_CHUNK, {}, b"\x00" * (MAX_FRAME_BYTES + 1)).encode()


def test_msg_name_is_readable():
    assert msg_name(MsgType.MOUSE_MOVE) == "MOUSE_MOVE"
    assert msg_name(MsgType.CLIP_ANNOUNCE) == "CLIP_ANNOUNCE"
    assert msg_name(0x7F).startswith("0x")


def test_message_types_do_not_collide():
    values = [v for k, v in vars(MsgType).items() if not k.startswith("_") and isinstance(v, int)]
    assert len(values) == len(set(values)), "消息类型定义里有重复值"


# --------------------------------------------------------------------- 握手


def test_hello_verifies_with_same_psk():
    frame = make_hello("desktop-a", "shared-secret")
    assert verify_hello(frame.body, "shared-secret")


def test_hello_rejected_with_wrong_psk():
    frame = make_hello("desktop-a", "shared-secret")
    assert not verify_hello(frame.body, "other-secret")


def test_hello_rejected_when_name_tampered():
    """标签绑定设备名，改名后校验必须失败（防止中间人换名字串台）。"""
    frame = make_hello("desktop-a", "shared-secret")
    body = dict(frame.body)
    body["name"] = "desktop-b"
    assert not verify_hello(body, "shared-secret")


def test_hello_rejected_when_nonce_swapped():
    a = make_hello("desktop-a", "shared-secret")
    b = make_hello("desktop-a", "shared-secret")
    assert a.body["nonce"] != b.body["nonce"], "每次握手都应该是新的 nonce"
    body = dict(a.body)
    body["nonce"] = b.body["nonce"]
    assert not verify_hello(body, "shared-secret")


def test_auth_tag_is_deterministic_and_version_bound():
    nonce = b"\x01" * 16
    tag = auth_tag("psk", nonce, "dev")
    assert tag == auth_tag("psk", nonce, "dev")
    assert tag != auth_tag("psk", nonce, "dev2")
    assert tag != auth_tag("psk2", nonce, "dev")
    assert tag != auth_tag("psk", b"\x02" * 16, "dev")


def test_hello_carries_extra_fields():
    frame = make_hello("dev", "psk", {"screen": [0, 0, 1920, 1080], "channel": "input"})
    assert frame.body["screen"] == [0, 0, 1920, 1080]
    assert frame.body["channel"] == "input"
    assert frame.body["v"] == PROTOCOL_VERSION
    assert verify_hello(frame.body, "psk")


def test_verify_hello_tolerates_missing_fields():
    assert not verify_hello({}, "psk")
    assert not verify_hello({"nonce": "zz", "tag": "yy", "name": "x"}, "psk")
    assert not verify_hello({"nonce": "00", "tag": "00", "name": "x"}, "psk")


def test_new_nonce_is_16_bytes():
    assert len(new_nonce()) == 16
    assert new_nonce() != new_nonce()
