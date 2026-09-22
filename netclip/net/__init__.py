"""网络层：三通道 TCP（input / clip / file）、帧编解码、握手与重连。"""

from .channel import Channel, ChannelConfig, ChannelStats
from .manager import CHANNELS, NetManager, PeerState

__all__ = ["Channel", "ChannelConfig", "ChannelStats", "NetManager", "PeerState", "CHANNELS"]
