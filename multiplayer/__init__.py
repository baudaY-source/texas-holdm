"""《酒馆德州》朋友联机的纯逻辑核心（零 pygame / 网络依赖）。"""

from .actor import RoomActor
from .auth import generate_resume_token, generate_room_code
from .projection import PROJECTION_SCHEMA, project_table_state
from .protocol import (
    MAX_WIRE_INTEGER,
    PROTOCOL_VERSION,
    ActionIntent,
    ClientEnvelope,
    ProtocolError,
    encode_server_message,
    parse_action_intent,
    parse_client_message,
)
from .room import RoomConfig, RoomCore, RoomError, RoomPhase, SeatCredential

__all__ = [
    "PROTOCOL_VERSION",
    "ActionIntent",
    "ClientEnvelope",
    "MAX_WIRE_INTEGER",
    "PROJECTION_SCHEMA",
    "ProtocolError",
    "RoomActor",
    "RoomConfig",
    "RoomCore",
    "RoomError",
    "RoomPhase",
    "SeatCredential",
    "encode_server_message",
    "generate_resume_token",
    "generate_room_code",
    "parse_action_intent",
    "parse_client_message",
    "project_table_state",
]
