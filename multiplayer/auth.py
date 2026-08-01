"""朋友房间的无账号认证原语。"""
from __future__ import annotations

import secrets
from collections.abc import Collection

ROOM_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
ROOM_CODE_LENGTH = 6
RESUME_TOKEN_BYTES = 32  # 256 bit 熵


def generate_room_code(
    existing: Collection[str] = (),
    *,
    length: int = ROOM_CODE_LENGTH,
    max_attempts: int = 128,
) -> str:
    """生成便于口述且排除 ``0/O/1/I`` 的房间码。"""
    if isinstance(length, bool) or not isinstance(length, int) or not 4 <= length <= 12:
        raise ValueError("房间码长度须在 4-12 之间")
    if (
        isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or max_attempts <= 0
    ):
        raise ValueError("max_attempts 须为正整数")
    taken = {item.upper() for item in existing}
    for _ in range(max_attempts):
        candidate = "".join(secrets.choice(ROOM_CODE_ALPHABET) for _ in range(length))
        if candidate not in taken:
            return candidate
    raise RuntimeError("无法生成未占用的房间码")


def generate_resume_token() -> str:
    """生成仅用于恢复同一座位的高熵 bearer token。"""
    return secrets.token_urlsafe(RESUME_TOKEN_BYTES)


__all__ = [
    "RESUME_TOKEN_BYTES",
    "ROOM_CODE_ALPHABET",
    "ROOM_CODE_LENGTH",
    "generate_resume_token",
    "generate_room_code",
]
