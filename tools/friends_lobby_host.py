"""朋友局真机会合用的精简本机房主会话。

只连接启动器已经验证过的 localhost 私密 WS 路径，自动创建测试房并监视
第二位玩家加入。恢复 token 永不保存、打印或进入对象 ``repr``。
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
import json
from uuid import uuid4

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from multiplayer.auth import ROOM_CODE_ALPHABET, ROOM_CODE_LENGTH
from multiplayer.protocol import MAX_CLIENT_MESSAGE_BYTES, PROTOCOL_VERSION
from multiplayer.room import ROOM_STATE_SCHEMA


class LobbyHostError(RuntimeError):
    """可安全显示、且不包含 WSS 路径或恢复 token 的房主会话错误。"""


@dataclass(frozen=True)
class HostedRoom:
    """可公开给受邀朋友的最小房间信息。"""

    room_id: str
    player_count: int


@dataclass
class LobbyHost:
    """保持房主连接，并在外部手机加入时给出简洁状态。"""

    websocket: ClientConnection = field(repr=False)
    room: HostedRoom
    guest_joined: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _announced_guest: bool = field(default=False, init=False, repr=False)

    @classmethod
    async def create(
        cls,
        local_ws_url: str,
        *,
        display_name: str = "电脑房主",
        player_count: int = 2,
        small_blind: int = 5,
        big_blind: int = 10,
        buyin: int = 1000,
    ) -> LobbyHost:
        """建立本机认证连接并创建房间；失败时保证关闭 socket。"""
        try:
            websocket = await connect(
                local_ws_url,
                proxy=None,
                compression=None,
                open_timeout=8.0,
                close_timeout=3.0,
                ping_interval=20.0,
                ping_timeout=20.0,
                max_size=MAX_CLIENT_MESSAGE_BYTES,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # websockets 的底层异常可能带完整私密 WS 路径，不能越过安全边界。
            raise LobbyHostError("自动房主无法连接本机服务") from None
        try:
            await websocket.send(
                json.dumps(
                    {
                        "v": PROTOCOL_VERSION,
                        "type": "hello",
                        "body": {
                            "client": "alpha-host",
                            "client_version": "w3a-simple-2",
                        },
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            welcome = await _receive_json(websocket)
            if (
                welcome.get("v") != PROTOCOL_VERSION
                or welcome.get("type") != "welcome"
            ):
                raise LobbyHostError("自动房主 hello 失败")

            request_id = str(uuid4())
            await websocket.send(
                json.dumps(
                    {
                        "v": PROTOCOL_VERSION,
                        "type": "room.create",
                        "id": request_id,
                        "body": {
                            "display_name": display_name,
                            "player_count": player_count,
                            "small_blind": small_blind,
                            "big_blind": big_blind,
                            "buyin": buyin,
                        },
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            while True:
                reply = await _receive_json(websocket)
                if reply.get("id") != request_id:
                    continue
                if reply.get("v") != PROTOCOL_VERSION:
                    raise LobbyHostError("自动创建房间收到非法协议版本")
                if reply.get("type") == "error":
                    detail = reply.get("error")
                    code = detail.get("code") if isinstance(detail, dict) else None
                    safe_code = code if isinstance(code, str) else "UNKNOWN"
                    raise LobbyHostError(f"自动创建房间失败（{safe_code}）")
                if reply.get("type") != "ack" or reply.get("ok") is not True:
                    raise LobbyHostError("自动创建房间收到非法响应")
                room_id = reply.get("room_id")
                if not _valid_room_id(room_id):
                    raise LobbyHostError("自动创建房间未返回有效房间码")
                room = HostedRoom(room_id=room_id, player_count=player_count)
                if not _valid_create_ack(reply, room):
                    raise LobbyHostError("自动创建房间收到非法凭据或状态")
                return cls(
                    websocket=websocket,
                    room=room,
                )
        except asyncio.CancelledError:
            with suppress(Exception):
                await websocket.close(code=1000, reason="HOST_SETUP_CANCELLED")
            raise
        except LobbyHostError:
            with suppress(Exception):
                await websocket.close(code=1000, reason="HOST_SETUP_FAILED")
            raise
        except Exception:
            with suppress(Exception):
                await websocket.close(code=1000, reason="HOST_SETUP_FAILED")
            raise LobbyHostError("自动房主与本机服务通信失败") from None

    async def watch(self) -> None:
        """持续消费脱敏状态流；第二位玩家加入时只打印简洁结论。"""
        try:
            while True:
                message = await _receive_json(self.websocket, timeout=None)
                if (
                    message.get("v") != PROTOCOL_VERSION
                    or message.get("type") != "room.state"
                    or message.get("room_id") != self.room.room_id
                ):
                    continue
                state = message.get("body")
                if not isinstance(state, dict):
                    continue
                if (
                    type(message.get("state_version")) is not int
                    or message.get("state_version") != state.get("state_version")
                ):
                    continue
                members = _validated_lobby_members(state, self.room)
                if members is None:
                    continue
                if len(members) >= 2 and not self._announced_guest:
                    self._announced_guest = True
                    self.guest_joined.set()
                    print(
                        "√ 第二位玩家已加入：服务器已收到会合。"
                        "请同时确认手机显示加入成功。",
                        flush=True,
                    )
        except asyncio.CancelledError:
            raise
        except LobbyHostError:
            raise
        except (ConnectionClosed, OSError):
            raise LobbyHostError("自动房主连接意外关闭") from None

    async def close(self) -> None:
        """幂等关闭房主连接。"""
        try:
            await self.websocket.close(code=1000, reason="HOST_STOPPED")
        except asyncio.CancelledError:
            raise
        except Exception:
            raise LobbyHostError("自动房主关闭失败") from None


async def _receive_json(
    websocket: ClientConnection,
    *,
    timeout: float | None = 10.0,
) -> dict[str, object]:
    try:
        raw = (
            await websocket.recv()
            if timeout is None
            else await asyncio.wait_for(websocket.recv(), timeout=timeout)
        )
    except TimeoutError as exc:
        raise LobbyHostError("自动房主等待服务响应超时") from exc
    if not isinstance(raw, str):
        raise LobbyHostError("自动房主收到非文本消息")
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise LobbyHostError("自动房主收到非法 JSON") from exc
    if not isinstance(decoded, dict):
        raise LobbyHostError("自动房主收到非法消息")
    return decoded


def _valid_room_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == ROOM_CODE_LENGTH
        and all(ch in ROOM_CODE_ALPHABET for ch in value)
    )


def _validated_lobby_members(
    state: dict[str, object],
    room: HostedRoom,
) -> list[dict[str, object]] | None:
    """验证房主的 v2 大厅投影，按成员数而非座位数判定会合。"""
    viewer_member_id = state.get("viewer_member_id")
    if (
        state.get("schema") != ROOM_STATE_SCHEMA
        or state.get("protocol") != PROTOCOL_VERSION
        or state.get("room") != room.room_id
        or type(state.get("state_version")) is not int
        or state.get("state_version", -1) < 0
        or not isinstance(viewer_member_id, str)
        or not viewer_member_id
        or state.get("viewer_seat") is not None
        or state.get("viewer_is_host") is not True
        or state.get("host_member_id") != viewer_member_id
        or state.get("host_seat") is not None
        or state.get("phase") != "LOBBY"
        or state.get("table") is not None
    ):
        return None
    config = state.get("config")
    if (
        not isinstance(config, dict)
        or type(config.get("player_count")) is not int
        or config.get("player_count") != room.player_count
    ):
        return None
    raw_seats = state.get("seats")
    if not isinstance(raw_seats, list) or len(raw_seats) != room.player_count:
        return None
    seats: list[dict[str, object]] = []
    seat_ids: list[int] = []
    for raw_seat in raw_seats:
        if not isinstance(raw_seat, dict):
            return None
        seat_id = raw_seat.get("seat")
        if type(seat_id) is not int or type(raw_seat.get("occupied")) is not bool:
            return None
        seats.append(raw_seat)
        seat_ids.append(seat_id)
    if sorted(seat_ids) != list(range(room.player_count)):
        return None
    raw_members = state.get("members")
    if not isinstance(raw_members, list) or not raw_members:
        return None
    members: list[dict[str, object]] = []
    member_ids: set[str] = set()
    host_count = 0
    for raw_member in raw_members:
        if not isinstance(raw_member, dict):
            return None
        member_id = raw_member.get("member_id")
        seat = raw_member.get("seat")
        if (
            not isinstance(member_id, str)
            or not member_id
            or member_id in member_ids
            or (seat is not None and type(seat) is not int)
            or type(raw_member.get("is_host")) is not bool
        ):
            return None
        member_ids.add(member_id)
        host_count += int(raw_member.get("is_host") is True)
        members.append(raw_member)
    if host_count != 1 or not any(
        item.get("member_id") == viewer_member_id and item.get("is_host") is True
        for item in members
    ):
        return None
    return members


def _valid_create_ack(reply: dict[str, object], room: HostedRoom) -> bool:
    """只验证创建凭据结构；高熵恢复 token 不返回、不保存也不打印。"""
    result = reply.get("result")
    state = reply.get("state")
    if not isinstance(result, dict) or not isinstance(state, dict):
        return False
    credential = result.get("credential")
    if (
        result.get("command") != "room.create"
        or not isinstance(credential, dict)
        or credential.get("room_id") != room.room_id
        or credential.get("seat") is not None
        or not isinstance(credential.get("member_id"), str)
        or not credential["member_id"]
        or credential.get("is_host") is not True
        or not isinstance(credential.get("resume_token"), str)
        or not credential["resume_token"]
        or type(reply.get("state_version")) is not int
        or reply.get("state_version") != state.get("state_version")
    ):
        return False
    return (
        state.get("viewer_member_id") == credential.get("member_id")
        and _validated_lobby_members(state, room) is not None
    )


__all__ = ["HostedRoom", "LobbyHost", "LobbyHostError"]
