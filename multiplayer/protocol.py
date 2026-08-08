"""朋友联机协议 v2 的严格 JSON 边界。

本模块只解析客户端意图，不信任客户端提供的行动座位、合法金额或牌局
状态。v2 token 认证的是成员；只有 ``target_seat`` 选择/管理命令可提出
物理座位，真正的投注座位仍由权威房间从成员当前入座状态注入。
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Mapping
from uuid import UUID

PROTOCOL_VERSION = 2
MAX_CLIENT_MESSAGE_BYTES = 64 * 1024
# 与 JSON/JavaScript/Android 常见客户端都能无损互通的整数上限。
MAX_WIRE_INTEGER = 9_007_199_254_740_991

CLIENT_MESSAGE_TYPES = frozenset(
    {
        "hello",
        "ping",
        "room.create",
        "room.join",
        "room.ai.fill",
        "room.ai.clear",
        "room.ai.add",
        "room.ai.remove",
        "room.ai.rebuy",
        "room.ai.style",
        "room.ready",
        "room.start",
        "room.leave",
        "room.pause",
        "room.resume",
        "game.action",
        "game.show",
        "game.next_hand",
        "seat.claim",
        "seat.release",
        "seat.rebuy",
        "seat.leave",
        "seat.topup.request",
        "seat.topup.cancel",
        "seat.topup.decline",
        "seat.topup.approve",
        "seat.topup.reject",
    }
)

# 创建/加入也需要幂等 ID，但尚无可比较的房间 state_version。
REQUEST_ID_TYPES = CLIENT_MESSAGE_TYPES - {"hello", "ping"}
STATE_GUARDED_TYPES = REQUEST_ID_TYPES - {"room.create", "room.join"}
ROOM_REQUIRED_TYPES = CLIENT_MESSAGE_TYPES - {"hello", "ping", "room.create"}

_ENVELOPE_KEYS = frozenset({"v", "type", "id", "room_id", "expected_state", "body"})
_ROOM_ID_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)
_ACTION_KINDS = frozenset({"fold", "check", "call", "bet", "raise", "allin"})


class ProtocolError(ValueError):
    """可安全返回给客户端的稳定协议错误。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class ClientEnvelope:
    """校验后的客户端消息；不含认证成员或行动座位。"""

    version: int
    message_type: str
    request_id: str | None
    room_id: str | None
    expected_state: int | None
    # hello.body 可能包含恢复 token；任何调试 repr 都必须默认脱敏。
    body: dict[str, object] = field(repr=False)

    @property
    def is_mutating(self) -> bool:
        """该消息是否需要幂等去重。"""
        return self.message_type in REQUEST_ID_TYPES

    @property
    def is_state_guarded(self) -> bool:
        """该消息是否必须匹配房间状态版本。"""
        return self.message_type in STATE_GUARDED_TYPES


@dataclass(frozen=True)
class ActionIntent:
    """不带座位的投注意图。

    ``to`` 仅对 BET/RAISE 有值。CALL 与 ALLIN 的真实金额由服务器从
    最新合法动作推导，客户端无权上传。
    """

    kind: str
    to: int | None = None


def parse_client_message(raw: str | bytes) -> ClientEnvelope:
    """严格解析一条客户端 JSON 消息。

    拒绝重复键、非标准 NaN/Infinity、未知 envelope 字段、未知消息类型、
    递归出现的 ``seat`` 以及不符合幂等/状态版本约定的消息。
    """
    text = _decode_message(raw)
    try:
        decoded = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
            parse_int=_parse_json_int,
            parse_float=_parse_json_float,
        )
    except ProtocolError:
        raise
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise ProtocolError("INVALID_JSON", "消息不是合法 UTF-8 JSON") from exc

    if not isinstance(decoded, dict):
        raise ProtocolError("INVALID_ENVELOPE", "消息根节点必须是对象")
    if _contains_key(decoded, "seat"):
        raise ProtocolError("CLIENT_SEAT_FORBIDDEN", "座位只能由认证会话绑定")

    unknown = set(decoded) - _ENVELOPE_KEYS
    if unknown:
        raise ProtocolError(
            "UNKNOWN_FIELD",
            f"未知 envelope 字段: {', '.join(sorted(unknown))}",
        )

    version = decoded.get("v")
    if not _is_int(version) or version != PROTOCOL_VERSION:
        raise ProtocolError(
            "UNSUPPORTED_VERSION",
            f"仅支持协议版本 {PROTOCOL_VERSION}",
        )

    message_type = decoded.get("type")
    if not isinstance(message_type, str) or message_type not in CLIENT_MESSAGE_TYPES:
        raise ProtocolError("UNKNOWN_TYPE", "未知客户端消息类型")

    body = decoded.get("body", {})
    if not isinstance(body, dict):
        raise ProtocolError("INVALID_BODY", "body 必须是对象")

    room_id = decoded.get("room_id")
    if room_id is not None:
        if (
            not isinstance(room_id, str)
            or not 1 <= len(room_id) <= 32
            or any(ch not in _ROOM_ID_CHARS for ch in room_id)
        ):
            raise ProtocolError("INVALID_ROOM", "room_id 格式非法")
    if message_type in ROOM_REQUIRED_TYPES and room_id is None:
        raise ProtocolError("ROOM_REQUIRED", "该消息必须包含 room_id")
    if message_type not in ROOM_REQUIRED_TYPES and room_id is not None:
        raise ProtocolError("UNEXPECTED_ROOM", "该消息不应包含 room_id")

    request_id = decoded.get("id")
    if message_type in REQUEST_ID_TYPES:
        request_id = _canonical_uuid(request_id)
    elif request_id is not None:
        request_id = _canonical_uuid(request_id)

    expected_state = decoded.get("expected_state")
    if message_type in STATE_GUARDED_TYPES:
        if (
            not _is_int(expected_state)
            or expected_state < 0
            or expected_state > MAX_WIRE_INTEGER
        ):
            raise ProtocolError(
                "STATE_REQUIRED",
                "变更命令必须包含非负整数 expected_state",
            )
    elif expected_state is not None:
        raise ProtocolError(
            "UNEXPECTED_STATE",
            "创建/加入/探活消息不应包含 expected_state",
        )

    return ClientEnvelope(
        version=version,
        message_type=message_type,
        request_id=request_id,
        room_id=room_id,
        expected_state=expected_state,
        body=dict(body),
    )


def parse_action_intent(body: Mapping[str, object]) -> ActionIntent:
    """把 ``game.action`` 的 body 解析为无座位动作意图。"""
    if not isinstance(body, Mapping):
        raise ProtocolError("INVALID_ACTION", "动作 body 必须是对象")
    if "seat" in body:
        raise ProtocolError("CLIENT_SEAT_FORBIDDEN", "动作不得包含 seat")

    kind = body.get("kind")
    if not isinstance(kind, str) or kind not in _ACTION_KINDS:
        raise ProtocolError("INVALID_ACTION", "未知动作 kind")

    expected_keys = {"kind", "to"} if kind in {"bet", "raise"} else {"kind"}
    unknown = set(body) - expected_keys
    missing = expected_keys - set(body)
    if unknown:
        raise ProtocolError(
            "INVALID_ACTION_FIELD",
            f"动作 {kind} 不接受字段: {', '.join(sorted(unknown))}",
        )
    if missing:
        raise ProtocolError(
            "ACTION_TO_REQUIRED",
            f"动作 {kind} 必须包含整数 to",
        )

    to = body.get("to")
    if kind in {"bet", "raise"}:
        if not _is_int(to) or not 0 < to <= MAX_WIRE_INTEGER:
            raise ProtocolError(
                "INVALID_ACTION_TO",
                f"to 必须是 1-{MAX_WIRE_INTEGER} 的整数",
            )
        return ActionIntent(kind=kind, to=to)
    return ActionIntent(kind=kind)


def encode_server_message(message: Mapping[str, object]) -> str:
    """用确定性紧凑 JSON 编码服务端消息。"""
    if not isinstance(message, Mapping):
        raise TypeError("服务端消息必须是 Mapping")
    try:
        return json.dumps(
            dict(message),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ProtocolError("INVALID_SERVER_MESSAGE", "服务端消息不可 JSON 编码") from exc


def _decode_message(raw: str | bytes) -> str:
    if isinstance(raw, bytes):
        if len(raw) > MAX_CLIENT_MESSAGE_BYTES:
            raise ProtocolError("MESSAGE_TOO_LARGE", "消息超过 64 KiB")
        try:
            return raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ProtocolError("INVALID_UTF8", "消息不是合法 UTF-8") from exc
    if not isinstance(raw, str):
        raise ProtocolError("INVALID_MESSAGE", "消息必须是 str 或 bytes")
    try:
        encoded = raw.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ProtocolError("INVALID_UTF8", "消息不是合法 UTF-8") from exc
    if len(encoded) > MAX_CLIENT_MESSAGE_BYTES:
        raise ProtocolError("MESSAGE_TOO_LARGE", "消息超过 64 KiB")
    return raw


def _object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("DUPLICATE_FIELD", f"JSON 字段重复: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ProtocolError("INVALID_JSON_NUMBER", f"JSON 不允许 {value}")


def _parse_json_int(value: str) -> int:
    digits = value[1:] if value.startswith("-") else value
    if len(digits) > 16:
        raise ProtocolError("INVALID_JSON_NUMBER", "整数超出安全范围")
    parsed = int(value)
    if abs(parsed) > MAX_WIRE_INTEGER:
        raise ProtocolError("INVALID_JSON_NUMBER", "整数超出安全范围")
    return parsed


def _parse_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ProtocolError("INVALID_JSON_NUMBER", "浮点数必须有限")
    return parsed


def _contains_key(value: object, forbidden: str) -> bool:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            if forbidden in current:
                return True
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return False


def _canonical_uuid(value: object) -> str:
    if not isinstance(value, str):
        raise ProtocolError("REQUEST_ID_REQUIRED", "该消息必须包含 UUID id")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ProtocolError("INVALID_REQUEST_ID", "id 必须是 UUID") from exc
    canonical = str(parsed)
    if value.lower() != canonical:
        raise ProtocolError("INVALID_REQUEST_ID", "id 必须使用标准 UUID 格式")
    return canonical


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


__all__ = [
    "CLIENT_MESSAGE_TYPES",
    "MAX_CLIENT_MESSAGE_BYTES",
    "MAX_WIRE_INTEGER",
    "PROTOCOL_VERSION",
    "REQUEST_ID_TYPES",
    "ROOM_REQUIRED_TYPES",
    "STATE_GUARDED_TYPES",
    "ActionIntent",
    "ClientEnvelope",
    "ProtocolError",
    "encode_server_message",
    "parse_action_intent",
    "parse_client_message",
]
