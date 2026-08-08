"""启动 localhost 权威朋友局服务；可选退回纯传输 probe。"""
from __future__ import annotations

import argparse
import asyncio
import math

from multiplayer.protocol import PROTOCOL_VERSION, ClientEnvelope

from .service import DEFAULT_EMPTY_ROOM_TTL_SECONDS, RoomRegistryBackend
from .ws_app import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    Close,
    ConnectionRejected,
    Emit,
    HelloInfo,
    TransportConfig,
    WS_PATH,
    create_server,
    make_error_message,
    ws_path_from_token,
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


async def _serve(
    port: int,
    *,
    probe: bool = False,
    ws_path: str = WS_PATH,
    empty_room_ttl_seconds: float = DEFAULT_EMPTY_ROOM_TTL_SECONDS,
) -> None:
    backend = (
        ProbeBackend()
        if probe
        else RoomRegistryBackend(empty_room_ttl_seconds=empty_room_ttl_seconds)
    )
    server = await create_server(
        backend,
        config=TransportConfig(port=port, ws_path=ws_path),
    )
    assert server.sockets
    actual_port = int(server.sockets[0].getsockname()[1])
    mode = "传输探针" if probe else "权威朋友局 Alpha"
    print(f"{mode}已启动: http://{DEFAULT_HOST}:{actual_port}/health")
    if ws_path == WS_PATH:
        print(f"WebSocket: ws://{DEFAULT_HOST}:{actual_port}{WS_PATH}")
    else:
        # 私密路径可能是临时访问秘密，普通启动日志只确认已启用。
        print(f"WebSocket: ws://{DEFAULT_HOST}:{actual_port}/[private-path]")
        print("已启用精确私密 WS 路径；请向联调客户端单独传递原路径。")
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
    parser.add_argument(
        "--empty-room-ttl",
        type=float,
        default=DEFAULT_EMPTY_ROOM_TTL_SECONDS,
        metavar="SECONDS",
        help="全部连接断开后保留恢复令牌的秒数（默认 900）",
    )
    path_group = parser.add_mutually_exclusive_group()
    path_group.add_argument(
        "--ws-path",
        help="临时 Tunnel 使用的精确私密路径（默认 /ws）",
    )
    path_group.add_argument(
        "--path-token",
        help="32-128 位 URL-safe 高熵 token，服务端映射为 /ws/<token>",
    )
    args = parser.parse_args(argv)
    if not math.isfinite(args.empty_room_ttl) or args.empty_room_ttl <= 0:
        parser.error("--empty-room-ttl 必须是正有限数")
    try:
        ws_path = (
            ws_path_from_token(args.path_token)
            if args.path_token is not None
            else args.ws_path or WS_PATH
        )
        # 在进入事件循环前完成同一套路径校验。
        config = TransportConfig(port=args.port, ws_path=ws_path)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        asyncio.run(
            _serve(
                config.port,
                probe=args.probe,
                ws_path=config.ws_path,
                empty_room_ttl_seconds=args.empty_room_ttl,
            )
        )
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
