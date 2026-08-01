"""酒馆视觉主题:调色板、CJK 字体加载与文字绘制助手。

调色板围绕「骗子酒馆」的氛围:近黑的棕色背景、桌中央暖琥珀色
灯光、青色 UI 点缀与危险红。pygame 默认字体无法渲染中文,
这里从 ``C:/Windows/Fonts`` 依次尝试微软雅黑/黑体/等线/宋体。
"""
from __future__ import annotations

import os

import pygame

# ------------------------------------------------------------ 调色板
BG = (20, 13, 8)  # #140d08 近黑棕背景
BG_PANEL = (28, 19, 12)  # 面板底色
FELT = (31, 22, 14)  # 台面
FELT_EDGE = (58, 40, 24)  # 台面边缘木沿
AMBER = (217, 142, 50)  # #d98e32 主琥珀
AMBER_LIGHT = (240, 179, 92)  # #f0b35c 亮琥珀
AMBER_DARK = (120, 76, 26)  # 暗琥珀(描边/投影)
TEAL = (62, 148, 136)  # 青色 UI 点缀
TEAL_DARK = (34, 88, 80)
DANGER = (184, 62, 48)  # 危险红(弃牌/全下)
TEXT = (236, 226, 208)  # 暖白正文
TEXT_DIM = (158, 140, 118)  # 次级文字
GOLD = (240, 200, 110)  # 筹码/金额高亮
CARD_HIGHLIGHT = (243, 217, 150)  # 最佳牌型涉及牌的淡黄色描边

# ------------------------------------------------------------ 字体
_FONT_DIRS = (os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Fonts"),)
_FONT_CANDIDATES = ("msyh.ttc", "simhei.ttf", "Deng.ttf", "simsun.ttc")

_font_path: str | None = None
_font_cache: dict[int, pygame.font.Font] = {}


def _find_cjk_font() -> str | None:
    """在系统字体目录中寻找可渲染中文的字体文件。"""
    for d in _FONT_DIRS:
        for name in _FONT_CANDIDATES:
            path = os.path.join(d, name)
            if os.path.isfile(path):
                return path
    return None


def get_font(size: int) -> pygame.font.Font:
    """按字号返回缓存的 CJK 字体(找不到系统字体时退回默认字体)。"""
    global _font_path
    if not pygame.font.get_init():
        pygame.font.init()
    if _font_path is None:
        _font_path = _find_cjk_font() or ""
    key = max(8, int(size))
    if key not in _font_cache:
        _font_cache[key] = (
            pygame.font.Font(_font_path, key)
            if _font_path
            else pygame.font.Font(None, key)
        )
    return _font_cache[key]


def text(
    surface: pygame.Surface,
    s: str,
    pos: tuple[float, float],
    size: int = 20,
    color: tuple[int, int, int] = TEXT,
    anchor: str = "topleft",
    shadow: bool = False,
) -> pygame.Rect:
    """在 ``surface`` 上绘制一行文字。

    :param anchor: pygame Rect 定位点(如 ``"center"``、``"midtop"``)。
    :param shadow: 是否先垫一层近黑投影(灯下的文字更易读)。
    :return: 实际绘制的矩形。
    """
    font = get_font(size)
    if shadow:
        dark = font.render(s, True, (0, 0, 0))
        rect = dark.get_rect()
        setattr(rect, anchor, (round(pos[0]) + 2, round(pos[1]) + 2))
        surface.blit(dark, rect)
    img = font.render(s, True, color)
    rect = img.get_rect()
    setattr(rect, anchor, (round(pos[0]), round(pos[1])))
    surface.blit(img, rect)
    return rect


def text_width(s: str, size: int = 20) -> int:
    """测量文字宽度(用于布局)。"""
    return get_font(size).size(s)[0]
