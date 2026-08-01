"""朋友联机的可选 WebSocket 服务端传输层。

此包是 ``multiplayer`` 纯核心之外的部署边界；桌面单机和 Android APK
均不应导入它。运行服务端时单独安装 ``requirements-server.txt``。
"""

from .ws_app import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    HEALTH_PATH,
    WS_PATH,
    Close,
    ConnectionRejected,
    Emit,
    HelloInfo,
    TransportBackend,
    TransportConfig,
    create_server,
    make_error_message,
)
from .service import RoomRegistryBackend

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "HEALTH_PATH",
    "WS_PATH",
    "Close",
    "ConnectionRejected",
    "Emit",
    "HelloInfo",
    "RoomRegistryBackend",
    "TransportBackend",
    "TransportConfig",
    "create_server",
    "make_error_message",
]
