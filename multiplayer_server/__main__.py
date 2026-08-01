"""启动 localhost 权威朋友局服务；可选退回纯传输 probe。"""
from __future__ import annotations

import argparse
import asyncio

from multiplayer.protocol import PROTOCOL_VERSION, ClientEnvelope

from .service import RoomRegistryBackend
from .ws_app import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    Close,
    ConnectionRejected,
    Emit,
    HelloInfo,
    TransportConfig,
    create_server,
    make_error_message,
)


class ProbeBackend:
    """联调占位后端；不会创建房间或运行牌局。"""

    def __init__(self) -> None:
        self._emitters: dict[str, Emit] = {}

    async def connect(
        self,
        connection_id: str,
        hello: HelloInfo,
        emit: Emit,
        close: Close,
    ) -> None:
        if hello.is_resume:
            raise ConnectionRejected("AUTH_UNAVAILABLE", "探针不提供座位恢复")
        self._emitters[connection_id] = emit
        emit(
            {
                "v": PROTOCOL_VERSION,
                "type": "welcome",
                "body": {
                    "connection_id": connection_id,
                    "protocol": PROTOCOL_VERSION,
                    "resumed": hello.is_resume,
                    "transport": "probe",
                },
            }
        )

    async def submit(
        self,
        connection_id: str,
        envelope: ClientEnvelope,
    ) -> None:
        emit = self._emitters.get(connection_id)
        if emit is not None:
            emit(make_error_message("SERVER_NOT_READY", "牌局后端尚未接入"))

    async def disconnect(self, connection_id: str) -> None:
        self._emitters.pop(connection_id, None)


async def _serve(port: int, *, probe: bool = False) -> None:
    backend = ProbeBackend() if probe else RoomRegistryBackend()
    server = await create_server(backend, config=TransportConfig(port=port))
    assert server.sockets
    actual_port = int(server.sockets[0].getsockname()[1])
    mode = "传输探针" if probe else "权威朋友局 Alpha"
    print(f"{mode}已启动: http://{DEFAULT_HOST}:{actual_port}/health")
    print(f"WebSocket: ws://{DEFAULT_HOST}:{actual_port}/ws")
    if probe:
        print("当前只支持 hello / ping，不提供牌局。")
    else:
        print("默认限制一个活跃房间；公网接入必须在外层提供 TLS/WSS。")
    try:
        await server.serve_forever()
    finally:
        server.close(code=1012, reason="SERVICE_RESTART")
        await server.wait_closed()
        close_backend = getattr(backend, "close", None)
        if close_backend is not None:
            await close_backend()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="朋友联机 localhost 权威服务")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--probe", action="store_true", help="只运行 hello/ping 探针")
    args = parser.parse_args(argv)
    try:
        asyncio.run(_serve(args.port, probe=args.probe))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
