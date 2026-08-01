"""Adrian Kennard CC0 高清牌面加载与缓存。

牌面由 Super Index 矢量原稿离线栅格化为 240×336 PNG；运行时只做
高质量缩小，因此底牌、公共牌和对手迷你亮牌都能保持清晰角标。
"""
from __future__ import annotations

import pygame

from .respath import res_path

CARD_DIR = res_path("assets", "cards", "clarity")

# 标准显示尺寸(宽 x 高)
SIZE_HOLE = (66, 92)  # 人类玩家底牌
SIZE_BOARD = (61, 86)  # 公共牌
SIZE_MINI = (41, 58)  # 对手亮牌/小牌

_cache: dict[tuple[str, int, int], pygame.Surface] = {}
_trimmed: dict[str, pygame.Surface] = {}


def _load_trimmed(name: str) -> pygame.Surface:
    """加载原图并裁掉透明边距。"""
    if name not in _trimmed:
        surf = pygame.image.load(str(CARD_DIR / name))
        try:
            surf = surf.convert_alpha()
        except pygame.error:
            pass  # 无显示模式(无头)时 PNG 加载结果自带透明通道
        rect = surf.get_bounding_rect(min_alpha=10)
        _trimmed[name] = surf.subsurface(rect).copy()
    return _trimmed[name]


def card_filename(code: str) -> str:
    """``"As"``/``"Td"`` 等短码 -> 高清牌面文件名。"""
    rank, suit = code[0], code[1].lower()
    return f"{rank}{suit.upper()}.png"


def card_surface(code: str, size: tuple[int, int]) -> pygame.Surface:
    """指定短码与尺寸的牌面(带缓存);``code="back"`` 为牌背。"""
    key = (code, size[0], size[1])
    if key not in _cache:
        name = "back.png" if code == "back" else card_filename(code)
        base = _load_trimmed(name)
        _cache[key] = pygame.transform.smoothscale(base, size)
    return _cache[key]
