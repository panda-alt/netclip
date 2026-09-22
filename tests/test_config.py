"""配置解析与校验的单元测试。全部为纯逻辑，不依赖 Windows。"""

from __future__ import annotations

import pytest

from netclip.config import ConfigError, from_dict
from netclip.layout import Rect

# --------------------------------------------------------------------- 默认值


def test_minimal_config_uses_defaults():
    cfg = from_dict({})
    assert cfg.network.listen_port == 24800
    assert cfg.network.clip_port == 24801
    assert cfg.network.file_port == 24802
    assert cfg.layout.peer_position == "right"
    assert cfg.layout.alignment == "center"
    assert cfg.clipboard.files.auto_transfer_max_mb == 200
    assert cfg.clipboard.files.over_threshold_action == "ask"
    assert cfg.clipboard.formats.forward_all is True
    assert cfg.clipboard.enable is True
    assert cfg.input.backend == "auto"
    assert cfg.ui.tray is True


def test_ports_are_offset_from_listen_port():
    cfg = from_dict({"network": {"listen_port": 25000, "peer_port": 25000}})
    ports = cfg.network.ports()
    assert ports["input"] == (25000, 25000)
    assert ports["clip"] == (25001, 25001)
    assert ports["file"] == (25002, 25002)


def test_peer_port_defaults_to_listen_port():
    cfg = from_dict({"network": {"listen_port": 24800}})
    assert cfg.network.peer_port == 24800


# --------------------------------------------------------------------- 完整配置


FULL = {
    "device": {"name": "desktop-a"},
    "network": {
        "listen_host": "0.0.0.0",
        "listen_port": 24800,
        "peer_ip": "192.168.1.42",
        "peer_port": 24800,
        "nodelay": True,
        "input_queue_max": 128,
        "move_coalesce_ms": 16,
        "heartbeat_sec": 3,
        "timeout_sec": 9,
        "reconnect_interval_sec": 2,
        "tcp_keepalive": False,
        "recapture_on_disconnect": False,
    },
    "layout": {
        "peer_position": "up",
        "local_screen": [0, 0, 2560, 1440],
        "peer_screen": [0, 0, 1920, 1200],
        "edge_band_px": 3,
        "warp_inset_px": 12,
        "switch_cooldown_ms": 300,
        "arm_delay_ms": 400,
        "alignment": "start",
        "lock_mouse_on_drag": False,
    },
    "input": {
        "share_mouse": False,
        "share_keyboard": True,
        "forward_media_keys": True,
        "backend": "python",
        "local_hotkeys": ["ctrl+alt+del"],
        "hotkey_recapture": "ctrl+alt+f12",
        "hotkey_toggle": "ctrl+alt+p",
    },
    "clipboard": {
        "enable": False,
        "sync_text": True,
        "sync_rtf": False,
        "sync_html": False,
        "sync_image": False,
        "sync_files": False,
        "debounce_ms": 300,
        "max_payload_mb": 10,
        "open_retry_ms": [10, 20],
        "files": {
            "auto_transfer_max_mb": 5,
            "over_threshold_action": "skip",
            "receive_dir": "D:/netclip",
            "staging_ttl_min": 30,
            "chunk_size_kb": 256,
            "verify_sha256": False,
        },
        "formats": {
            "forward_all": False,
            "exclude": ["^Foo$"],
            "per_format_max_mb": 7,
            "compress": False,
            "inline_html_refs": False,
            "allow_private": ["^PowerPoint"],
            "paste_priority": ["CF_DIB"],
        },
    },
    "ui": {"tray": False, "notify": False},
    "security": {"psk": "s3cret"},
    "logging": {"level": "debug", "file": "x.log", "max_mb": 5, "backups": 2},
}


def test_full_config_roundtrip():
    cfg = from_dict(FULL)
    assert cfg.device.resolved_name() == "desktop-a"
    assert cfg.network.peer_ip == "192.168.1.42"
    assert cfg.network.move_coalesce_ms == 16
    assert cfg.network.tcp_keepalive is False
    assert cfg.network.recapture_on_disconnect is False
    assert cfg.layout.peer_position == "up"
    assert cfg.layout.local_screen == Rect(0, 0, 2560, 1440)
    assert cfg.layout.peer_screen == Rect(0, 0, 1920, 1200)
    assert cfg.layout.alignment == "start"
    assert cfg.layout.lock_mouse_on_drag is False
    assert cfg.input.share_mouse is False
    assert cfg.input.backend == "python"
    assert cfg.input.local_hotkeys == ["ctrl+alt+del"]
    assert cfg.clipboard.enable is False
    assert cfg.clipboard.open_retry_ms == [10, 20]
    assert cfg.clipboard.files.auto_transfer_max_mb == 5
    assert cfg.clipboard.files.over_threshold_action == "skip"
    assert cfg.clipboard.formats.forward_all is False
    assert cfg.clipboard.formats.allow_private == ["^PowerPoint"]
    assert cfg.clipboard.formats.paste_priority == ["CF_DIB"]
    assert cfg.security.psk == "s3cret"
    assert cfg.logging.level == "DEBUG"  # 自动转大写


# --------------------------------------------------------------------- 错误路径


def _expect_error(payload, needle):
    with pytest.raises(ConfigError) as excinfo:
        from_dict(payload)
    assert needle in str(excinfo.value), "错误信息里应包含 %r，实际: %s" % (needle, excinfo.value)


def test_unknown_section_rejected():
    """拼错的配置段要明确报错，而不是被静默忽略。"""
    _expect_error({"netwrok": {}}, "未知的配置段")


def test_bad_peer_position():
    _expect_error({"layout": {"peer_position": "diagonal"}}, "layout.peer_position")


def test_bad_alignment():
    _expect_error({"layout": {"alignment": "middle"}}, "layout.alignment")


def test_bad_port_range():
    _expect_error({"network": {"listen_port": 70000}}, "network.listen_port")


def test_timeout_must_exceed_heartbeat():
    _expect_error({"network": {"heartbeat_sec": 10, "timeout_sec": 5}}, "timeout_sec")


def test_bad_over_threshold_action():
    _expect_error({"clipboard": {"files": {"over_threshold_action": "explode"}}}, "over_threshold_action")


def test_bad_regex_in_exclude():
    _expect_error({"clipboard": {"formats": {"exclude": ["["]}}}, "exclude")


def test_negative_threshold():
    _expect_error({"clipboard": {"files": {"auto_transfer_max_mb": -1}}}, "auto_transfer_max_mb")


def test_bad_chunk_size():
    _expect_error({"clipboard": {"files": {"chunk_size_kb": 0}}}, "chunk_size_kb")


def test_empty_psk_rejected():
    _expect_error({"security": {"psk": ""}}, "psk")


def test_bad_screen_quad():
    _expect_error({"layout": {"peer_screen": [0, 0, 0, 100]}}, "宽高必须为正")
    _expect_error({"layout": {"peer_screen": [0, 0, 100]}}, "四个整数")


def test_type_errors_are_reported_with_path():
    _expect_error({"network": {"listen_port": "abc"}}, "network.listen_port")
    _expect_error({"clipboard": {"enable": "yes"}}, "clipboard.enable")
    _expect_error({"input": {"local_hotkeys": [1, 2]}}, "input.local_hotkeys")


def test_bad_backend():
    _expect_error({"input": {"backend": "rust"}}, "input.backend")


# --------------------------------------------------------------------- 描述


def test_describe_mentions_key_settings():
    text = from_dict(FULL).describe()
    assert "对端在上" in text
    assert "5 MB" in text
    assert "skip" in text
