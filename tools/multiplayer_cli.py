"""朋友联机 localhost/WSS 协议参考客户端。

默认只完成 hello + 应用层 ping 冒烟；``--interactive`` 可继续逐行发送
JSON。恢复 token 只能通过无回显提示输入，不能放命令行或 URL。
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit
from uuid import uuid4

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from multiplayer.protocol import PROTOCOL_VERSION

DEFAULT_URL = "ws://127.0.0.1:8765/ws"
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_SECRET_KEYS = frozenset({"token", "resume_token"})


def _hello_body(args: argparse.Namespace) -> dict[str, object]:
    body: dict[str, object] = {
        "client": "cli",
        "client_version": args.client_version,
    }
    if args.resume_room is not None:
        token = getpass.getpass("resume token（不回显）: ")
        body["resume"] = {"room_id": args.resume_room, "token": token}
    return body


def _validate_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme == "wss":
        return
    if parsed.scheme == "ws" and parsed.hostname in _LOCAL_HOSTS:
        return
    raise ValueError("非本机地址必须使用 wss://")


def _display(raw: str | bytes) -> None:
    if not isinstance(raw, str):
        print("<收到非文本消息，已忽略>")
        return
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        print(raw)
        return
    print(json.dumps(_redact(value), ensure_ascii=False, indent=2, sort_keys=True))


def _decode(raw: str | bytes) -> dict[str, object] | None:
    if not isinstance(raw, str):
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


async def _receive_until(websocket, predicate) -> dict[str, object] | None:
    """显示异步状态流，直到收到当前请求的终止消息。"""
    while True:
        raw = await websocket.recv()
        _display(raw)
        decoded = _decode(raw)
        if decoded is not None and predicate(decoded):
            return decoded


def _redact(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: "***" if key in _SECRET_KEYS else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


async def _run(args: argparse.Namespace) -> None:
    _validate_url(args.url)
    hello = {
        "v": PROTOCOL_VERSION,
        "type": "hello",
        "body": _hello_body(args),
    }
    async with connect(
        args.url,
        proxy=None,
        compression=None,
        open_timeout=10,
        ping_interval=20,
        ping_timeout=20,
        max_size=1024 * 1024,
    ) as websocket:
        await websocket.send(json.dumps(hello, ensure_ascii=False, separators=(",", ":")))
        await _receive_until(websocket, lambda message: message.get("type") == "welcome")

        ping = {"v": PROTOCOL_VERSION, "type": "ping", "body": {}}
        await websocket.send(json.dumps(ping, separators=(",", ":")))
        await _receive_until(websocket, lambda message: message.get("type") == "pong")

        if args.create:
            request_id = str(uuid4())
            create = {
                "v": PROTOCOL_VERSION,
                "type": "room.create",
                "id": request_id,
                "body": {
                    "display_name": args.name,
                    "player_count": args.players,
                    "small_blind": args.small_blind,
                    "big_blind": args.big_blind,
                    "buyin": args.buyin,
                },
            }
            await websocket.send(
                json.dumps(create, ensure_ascii=False, separators=(",", ":"))
            )
            await _receive_until(
                websocket,
                lambda message: message.get("id") == request_id,
            )
        elif args.join is not None:
            request_id = str(uuid4())
            join = {
                "v": PROTOCOL_VERSION,
                "type": "room.join",
                "id": request_id,
                "room_id": args.join,
                "body": {"display_name": args.name},
            }
            await websocket.send(
                json.dumps(join, ensure_ascii=False, separators=(",", ":"))
            )
            await _receive_until(
                websocket,
                lambda message: message.get("id") == request_id,
            )

        if not args.interactive:
            return
        print("逐行输入客户端 envelope JSON；输入 /quit 退出。")
        while True:
            try:
                raw = await asyncio.to_thread(input, "json> ")
            except (EOFError, KeyboardInterrupt):
                return
            if raw.strip() == "/quit":
                return
            if not raw.strip():
                continue
            await websocket.send(raw)
            try:
                submitted = _decode(raw)
                request_id = submitted.get("id") if submitted is not None else None
                message_type = submitted.get("type") if submitted is not None else None
                await _receive_until(
                    websocket,
                    lambda message: (
                        request_id is not None and message.get("id") == request_id
                    )
                    or (message_type == "ping" and message.get("type") == "pong")
                    or (
                        message.get("type") == "error"
                        and message.get("id") is None
                    ),
                )
            except ConnectionClosed:
                return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="朋友联机协议参考客户端")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--client-version", default="reference-1")
    parser.add_argument("--resume-room")
    room_action = parser.add_mutually_exclusive_group()
    room_action.add_argument("--create", action="store_true", help="创建一个房间")
    room_action.add_argument("--join", metavar="ROOM_ID", help="加入指定房间")
    parser.add_argument("--name", default="玩家")
    parser.add_argument("--players", type=int, default=2)
    parser.add_argument("--small-blind", type=int, default=5)
    parser.add_argument("--big-blind", type=int, default=10)
    parser.add_argument("--buyin", type=int, default=1000)
    parser.add_argument("--interactive", action="store_true")
    args = parser.parse_args(argv)
    if args.resume_room is not None and (args.create or args.join is not None):
        parser.error("恢复连接不能同时创建或加入房间")
    try:
        asyncio.run(_run(args))
    except (OSError, TimeoutError, ConnectionClosed, ValueError) as exc:
        print(f"连接失败: {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
