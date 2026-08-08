"""Windows/pygame 可轮询的朋友局 WebSocket 客户端。"""

from .client import (
    ClientBusyError,
    ClientErrorInfo,
    ClientEvent,
    ClientSnapshot,
    ClientUsageError,
    DesktopMultiplayerClient,
)

__all__ = [
    "ClientBusyError",
    "ClientErrorInfo",
    "ClientEvent",
    "ClientSnapshot",
    "ClientUsageError",
    "DesktopMultiplayerClient",
]
