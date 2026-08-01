"""酒馆动物半身像:高清肖像优先，程序化动物作为缺失资源后备。

每张高清源图与椭圆酒馆框只加载/烘焙一次；程序化路径同样一次性离屏
渲染躯干与头部(BustData)。每帧只做:
呼吸缩放、眨眼(眼睑遮盖)、思考时的头部微倾与眼神游移、
偶尔的耳朵抖动,以及灯侧的琥珀色轮廓光(构建时烘焙)。
还包含座位名牌与下注筹码堆的绘制助手。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass

import pygame

from . import theme
from .respath import res_path

# 半身像画布尺寸
BUST_W, BUST_H = 240, 280
CENTER = (BUST_W // 2, BUST_H // 2)

# 人设物种 -> 构建器键
SPECIES_KEY = {
    "公牛": "bull",
    "狐狸": "fox",
    "犀牛": "rhino",
    "野猪": "boar",
    "猪": "boar",
    "看门犬": "dog",
    "狗": "dog",
    "猫": "cat",
    "流浪猫": "cat",
    "渡鸦": "raven",
    "兔": "rabbit",
    "兔子": "rabbit",
    "狼": "wolf",
    "灰狼": "wolf",
    "熊": "bear",
    "棕熊": "bear",
    "狮子": "lion",
    "雄狮": "lion",
    "老虎": "tiger",
    "虎": "tiger",
    "乌龟": "turtle",
    "龟": "turtle",
    "猫头鹰": "owl",
    "夜枭": "owl",
    "黑豹": "panther",
    "豹": "panther",
}

PORTRAIT_KEYS = frozenset(
    (
        "bull", "fox", "rhino", "boar", "dog", "cat", "raven", "rabbit", "wolf",
        "bear", "lion", "tiger", "turtle", "owl", "panther",
    )
)

LEVEL_LABEL = {"fish": "新手", "reg": "常客", "shark": "鲨鱼"}

# 半身像状态
IDLE = "idle"
THINKING = "thinking"
ACTED = "acted"
FOLDED = "folded"


@dataclass
class BustData:
    """一个物种的离屏渲染结果。"""

    body: pygame.Surface  # 躯干+头部(无眼珠,含眼眶)
    ear_l: pygame.Surface | None
    ear_r: pygame.Surface | None
    ear_l_mount: tuple[int, int]  # 相对画布中心的挂载点
    ear_r_mount: tuple[int, int]
    ear_pivot: tuple[int, int]  # 耳朵图内绕转点
    eyes: tuple[tuple[int, int], ...]  # 眼珠中心(相对画布中心)
    eye_r: int  # 眼珠半径
    skin: tuple[int, int, int]  # 眨眼眼睑色


# ------------------------------------------------------------ 绘制助手


def _surf() -> pygame.Surface:
    return pygame.Surface((BUST_W, BUST_H), pygame.SRCALPHA)


def _rim(surf: pygame.Surface, rect: pygame.Rect, start: float = 1.9, stop: float = 3.6) -> None:
    """在椭圆轮廓的灯侧(左上)画琥珀轮廓光弧。"""
    pygame.draw.arc(surf, (*theme.AMBER_LIGHT, 230), rect, start, stop, 4)
    pygame.draw.arc(surf, (*theme.AMBER, 110), rect.inflate(7, 7), start + 0.2, stop - 0.2, 2)


def _torso(surf: pygame.Surface, skin: tuple[int, int, int], dark: tuple[int, int, int]) -> None:
    """共通躯干:宽肩 + 收颈,底部顶到画布下沿。"""
    shoulders = pygame.Rect(28, 168, 184, 130)
    pygame.draw.ellipse(surf, dark, shoulders)  # 阴影打底
    pygame.draw.ellipse(surf, skin, shoulders.inflate(-8, -6))
    chest = [(66, 300), (70, 208), (96, 184), (144, 184), (170, 208), (174, 300)]
    pygame.draw.polygon(surf, skin, chest)
    # 领口 V 影
    pygame.draw.polygon(surf, dark, [(104, 190), (136, 190), (120, 226)])
    _rim(surf, shoulders, 3.4, 4.6)


def _head_base(
    surf: pygame.Surface, rect: pygame.Rect, skin: tuple[int, int, int]
) -> None:
    pygame.draw.ellipse(surf, skin, rect)
    _rim(surf, rect)


def _make_ear(points: list[tuple[int, int]], skin, inner, size=(64, 72)) -> pygame.Surface:
    """生成一只耳朵的小画布(pivot 在底部中心)。"""
    s = pygame.Surface(size, pygame.SRCALPHA)
    pygame.draw.polygon(s, skin, points)
    inner_pts = [(x * 0.62 + size[0] * 0.19, y * 0.62 + size[1] * 0.28) for x, y in points]
    pygame.draw.polygon(s, inner, inner_pts)
    return s


def _eye_sockets(surf: pygame.Surface, eyes, r: int, dark) -> None:
    for ex, ey in eyes:
        pygame.draw.ellipse(surf, dark, (ex - r - 3, ey - r - 2, (r + 3) * 2, (r + 2) * 2 + 4))
        pygame.draw.ellipse(surf, (232, 222, 200), (ex - r, ey - r, r * 2, r * 2))


# ------------------------------------------------------------ 各物种构建


def _build_bull() -> BustData:
    skin, dark, muzzle = (122, 86, 58), (72, 50, 34), (168, 124, 88)
    s = _surf()
    _torso(s, skin, dark)
    head = pygame.Rect(64, 44, 112, 128)
    # 牛角(先画,压在头后):弯曲多边形
    horn = (206, 184, 148)
    pygame.draw.polygon(s, horn, [(72, 78), (18, 40), (30, 30), (84, 62)])
    pygame.draw.polygon(s, horn, [(168, 78), (222, 40), (210, 30), (156, 62)])
    pygame.draw.polygon(s, (232, 214, 178), [(30, 34), (18, 40), (26, 26)])  # 角尖
    pygame.draw.polygon(s, (232, 214, 178), [(210, 34), (222, 40), (214, 26)])
    _head_base(s, head, skin)
    # 宽阔鼻吻部
    mz = pygame.Rect(78, 118, 84, 52)
    pygame.draw.ellipse(s, muzzle, mz)
    pygame.draw.circle(s, dark, (98, 142), 5)
    pygame.draw.circle(s, dark, (142, 142), 5)
    pygame.draw.arc(s, theme.GOLD, (106, 140, 28, 26), 0.4, 2.7, 3)  # 鼻环
    # 皱眉
    pygame.draw.line(s, dark, (88, 96), (110, 104), 5)
    pygame.draw.line(s, dark, (152, 96), (130, 104), 5)
    eyes = ((98, 114), (142, 114))
    _eye_sockets(s, eyes, 7, dark)
    ear_l = _make_ear([(8, 60), (30, 6), (56, 56)], skin, dark)
    ear_r = pygame.transform.flip(ear_l, True, False)
    return BustData(s, ear_l, ear_r, (-64, -48), (64, -48), (32, 66), eyes, 4, skin)


def _build_fox() -> BustData:
    skin, dark, cream = (198, 112, 52), (110, 60, 30), (230, 202, 162)
    s = _surf()
    _torso(s, skin, dark)
    head = pygame.Rect(70, 48, 100, 116)
    _head_base(s, head, skin)
    # 尖吻
    pygame.draw.polygon(s, skin, [(92, 128), (148, 128), (120, 168)])
    pygame.draw.polygon(s, cream, [(98, 132), (142, 132), (120, 162)])
    pygame.draw.circle(s, (30, 22, 16), (120, 160), 7)  # 鼻尖
    # 脸颊白毛
    pygame.draw.polygon(s, cream, [(70, 120), (96, 126), (78, 150)])
    pygame.draw.polygon(s, cream, [(170, 120), (144, 126), (162, 150)])
    # 胡须
    for dy in (-2, 4):
        pygame.draw.line(s, (220, 210, 190), (66, 138 + dy), (102, 132 + dy), 1)
        pygame.draw.line(s, (220, 210, 190), (174, 138 + dy), (138, 132 + dy), 1)
    # 狡黠眉眼
    pygame.draw.line(s, dark, (88, 96), (110, 92), 4)
    pygame.draw.line(s, dark, (152, 96), (130, 92), 4)
    eyes = ((98, 110), (142, 110))
    _eye_sockets(s, eyes, 7, dark)
    ear_l = _make_ear([(10, 66), (28, 2), (52, 62)], skin, (40, 24, 14))
    # 耳尖深色
    pygame.draw.polygon(ear_l, (40, 24, 14), [(22, 20), (28, 2), (36, 22)])
    ear_r = pygame.transform.flip(ear_l, True, False)
    return BustData(s, ear_l, ear_r, (-56, -56), (56, -56), (32, 68), eyes, 4, skin)


def _build_rhino() -> BustData:
    skin, dark = (142, 144, 154), (88, 90, 100)
    s = _surf()
    _torso(s, skin, dark)
    head = pygame.Rect(66, 52, 108, 122)
    _head_base(s, head, skin)
    # 厚重下颚
    pygame.draw.ellipse(s, dark, (84, 130, 72, 42))
    pygame.draw.ellipse(s, skin, (88, 128, 64, 40))
    # 鼻端大角(宽底,从两眼之间伸出,浅色带描边)
    horn = (230, 220, 200)
    pygame.draw.polygon(s, horn, [(104, 152), (136, 152), (120, 78)])
    pygame.draw.polygon(s, dark, [(104, 152), (136, 152), (120, 78)], 2)
    pygame.draw.polygon(s, horn, [(112, 82), (128, 82), (120, 58)])
    pygame.draw.polygon(s, dark, [(92, 146), (108, 150), (92, 158)])  # 鼻孔示意
    pygame.draw.polygon(s, dark, [(148, 146), (132, 150), (148, 158)])
    # 沉重眉脊
    pygame.draw.line(s, dark, (84, 98), (156, 98), 7)
    eyes = ((98, 114), (142, 114))
    _eye_sockets(s, eyes, 6, dark)
    ear_l = _make_ear([(14, 58), (32, 10), (52, 58)], skin, dark, size=(60, 64))
    ear_r = pygame.transform.flip(ear_l, True, False)
    return BustData(s, ear_l, ear_r, (-62, -52), (62, -52), (30, 60), eyes, 4, skin)


def _build_pig() -> BustData:
    skin, dark, snout = (160, 106, 86), (96, 62, 48), (202, 144, 118)
    s = _surf()
    _torso(s, skin, dark)
    head = pygame.Rect(62, 50, 116, 124)
    _head_base(s, head, skin)
    # 鬃毛刺
    for i, x in enumerate(range(96, 150, 12)):
        pygame.draw.polygon(s, dark, [(x, 52), (x + 6, 34), (x + 12, 52)])
    # 大拱鼻
    pygame.draw.ellipse(s, snout, (88, 118, 64, 44))
    pygame.draw.ellipse(s, dark, (98, 130, 12, 18))
    pygame.draw.ellipse(s, dark, (130, 130, 12, 18))
    # 獠牙
    pygame.draw.polygon(s, (228, 220, 200), [(84, 150), (94, 150), (88, 132)])
    pygame.draw.polygon(s, (228, 220, 200), [(156, 150), (146, 150), (152, 132)])
    # 眯缝小眼 + 眼袋
    pygame.draw.line(s, dark, (86, 100), (108, 102), 4)
    pygame.draw.line(s, dark, (154, 100), (132, 102), 4)
    eyes = ((96, 112), (144, 112))
    _eye_sockets(s, eyes, 6, dark)
    ear_l = _make_ear([(12, 62), (26, 8), (54, 58)], skin, snout, size=(62, 68))
    ear_r = pygame.transform.flip(ear_l, True, False)
    return BustData(s, ear_l, ear_r, (-60, -50), (60, -50), (31, 64), eyes, 3, skin)


def _build_dog() -> BustData:
    skin, dark, muzzle = (148, 112, 80), (88, 64, 46), (184, 144, 106)
    s = _surf()
    _torso(s, skin, dark)
    head = pygame.Rect(68, 50, 104, 118)
    _head_base(s, head, skin)
    # 下垂的宽吻
    pygame.draw.ellipse(s, muzzle, (92, 120, 56, 48))
    pygame.draw.circle(s, (26, 20, 14), (120, 132), 9)
    pygame.draw.line(s, dark, (120, 140), (120, 152), 3)
    pygame.draw.arc(s, dark, (102, 142, 18, 16), 3.4, 6.0, 3)
    pygame.draw.arc(s, dark, (120, 142, 18, 16), 3.4, 6.0, 3)
    # 忧郁眉
    pygame.draw.line(s, dark, (86, 96), (108, 92), 4)
    pygame.draw.line(s, dark, (154, 96), (132, 92), 4)
    eyes = ((98, 110), (142, 110))
    _eye_sockets(s, eyes, 7, dark)
    # 标志性垂耳(长椭圆多边形,垂到肩)
    ear_l = _make_ear([(14, 6), (48, 6), (54, 70), (30, 76), (8, 64)], skin, dark, size=(62, 82))
    ear_r = pygame.transform.flip(ear_l, True, False)
    return BustData(s, ear_l, ear_r, (-62, -52), (62, -52), (31, 12), eyes, 4, skin)


def _build_cat() -> BustData:
    skin, dark = (132, 132, 144), (80, 80, 92)
    s = _surf()
    _torso(s, skin, dark)
    head = pygame.Rect(72, 52, 96, 108)
    _head_base(s, head, skin)
    # 小吻部与嘴
    pygame.draw.ellipse(s, (164, 164, 176), (104, 124, 32, 24))
    pygame.draw.circle(s, (216, 162, 152), (120, 126), 4)
    for dy in (-2, 4):
        pygame.draw.line(s, (210, 205, 195), (70, 132 + dy), (104, 128 + dy), 1)
        pygame.draw.line(s, (210, 205, 195), (170, 132 + dy), (136, 128 + dy), 1)
    eyes = ((98, 108), (142, 108))
    _eye_sockets(s, eyes, 7, dark)
    ear_l = _make_ear([(12, 62), (30, 4), (50, 60)], skin, (150, 120, 116), size=(60, 66))
    ear_r = pygame.transform.flip(ear_l, True, False)
    return BustData(s, ear_l, ear_r, (-54, -54), (54, -54), (30, 62), eyes, 3, skin)


_BUILDERS = {
    "bull": _build_bull,
    "fox": _build_fox,
    "rhino": _build_rhino,
    "boar": _build_pig,
    "pig": _build_pig,
    "dog": _build_dog,
    "cat": _build_cat,
}

_data_cache: dict[str, BustData] = {}
_portrait_source_cache: dict[str, pygame.Surface | None] = {}
_portrait_frame_cache: dict[str, pygame.Surface | None] = {}


def _species_key(species: str) -> str:
    """把中文物种、旧 ``pig`` 键和资源键归一为肖像资源键。"""
    key = SPECIES_KEY.get(species, species)
    if key == "pig":
        return "boar"
    if key in PORTRAIT_KEYS:
        return key
    return "dog"


def _load_portrait_source(key: str) -> pygame.Surface | None:
    """加载并缓存高分辨率肖像；缺失或损坏时返回 ``None``。"""
    if key in _portrait_source_cache:
        return _portrait_source_cache[key]
    path = res_path("assets", "portraits", "v2", f"{key}.png")
    source: pygame.Surface | None = None
    try:
        loaded = pygame.image.load(str(path))
        # convert_alpha 依赖已初始化的显示模式；工具脚本的纯离屏阶段也要可用。
        source = loaded.convert_alpha() if pygame.display.get_surface() is not None else loaded.copy()
    except (FileNotFoundError, OSError, pygame.error):
        source = None
    _portrait_source_cache[key] = source
    return source


def _fit_cover(source: pygame.Surface, size: tuple[int, int]) -> pygame.Surface:
    """保持比例覆盖目标区域，居中裁掉溢出部分。"""
    sw, sh = source.get_size()
    tw, th = size
    ratio = max(tw / max(1, sw), th / max(1, sh))
    scaled_size = (max(tw, round(sw * ratio)), max(th, round(sh * ratio)))
    scaled = pygame.transform.smoothscale(source, scaled_size)
    result = pygame.Surface(size, pygame.SRCALPHA)
    result.blit(scaled, ((tw - scaled_size[0]) // 2, (th - scaled_size[1]) // 2))
    return result


def _portrait_frame(key: str) -> pygame.Surface | None:
    """把高分辨率源图烘焙进椭圆酒馆框，并缓存标准半身像画布。"""
    if key in _portrait_frame_cache:
        return _portrait_frame_cache[key]
    source = _load_portrait_source(key)
    if source is None:
        _portrait_frame_cache[key] = None
        return None

    framed = _surf()
    outer = pygame.Rect(16, 8, BUST_W - 32, BUST_H - 18)
    inner = outer.inflate(-10, -10)
    pygame.draw.ellipse(framed, (24, 19, 16, 246), outer)

    portrait = _fit_cover(source, inner.size)
    mask = pygame.Surface(inner.size, pygame.SRCALPHA)
    pygame.draw.ellipse(mask, (255, 255, 255, 255), mask.get_rect())
    portrait.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
    framed.blit(portrait, inner)

    pygame.draw.ellipse(framed, (*theme.AMBER_DARK, 245), outer, 4)
    pygame.draw.arc(framed, (*theme.AMBER_LIGHT, 230), outer, 1.9, 3.7, 4)
    pygame.draw.arc(framed, (*theme.AMBER, 125), outer.inflate(-7, -7), 2.05, 3.55, 2)
    _portrait_frame_cache[key] = framed
    return framed


def get_bust_data(species: str) -> BustData:
    """按物种中文名/键获取(并缓存)离屏渲染数据。"""
    key = _species_key(species)
    if key not in _data_cache:
        _data_cache[key] = _BUILDERS.get(key, _build_dog)()
    return _data_cache[key]


# ------------------------------------------------------------ 半身像实例


class Bust:
    """一名牌手的动态半身像(呼吸/眨眼/耳抖/思考/弃牌变暗)。"""

    def __init__(self, species: str, seed: int = 0, scale: float = 1.0) -> None:
        self.species_key = _species_key(species)
        self.portrait = _portrait_frame(self.species_key)
        self.data = get_bust_data(species)
        self.rng = random.Random(seed)
        self.scale = scale
        self.phase = self.rng.uniform(0, math.tau)
        self._blink_at = self.rng.uniform(1.5, 5.0)
        self._blink_t = -1.0
        self._twitch_at = self.rng.uniform(4.0, 9.0)
        self._twitch_t = -1.0
        self._twitch_ear = 1
        self._glance = (0, 0)
        self._glance_at = 0.0

    # -------------------------------------------------------- 每帧

    def _blit_pivot(
        self, dst: pygame.Surface, surf: pygame.Surface, mount, pivot, angle: float
    ) -> None:
        rot = pygame.transform.rotate(surf, angle)
        vec = pygame.Vector2(pivot) - pygame.Vector2(surf.get_rect().center)
        rv = vec.rotate(-angle)
        rect = rot.get_rect(center=(CENTER[0] + mount[0] - rv.x, CENTER[1] + mount[1] - rv.y))
        dst.blit(rot, rect)

    def draw(
        self,
        dst: pygame.Surface,
        center: tuple[float, float],
        t: float,
        state: str = IDLE,
    ) -> None:
        d = self.data
        # 背景托底圆盘:把半身像从近黑背景中托出来(弃牌/出局时收敛)
        back_r = int(108 * self.scale)
        back_a = 210 if state != FOLDED else 110
        back = pygame.Surface((back_r * 2, back_r * 2), pygame.SRCALPHA)
        pygame.draw.circle(back, (92, 66, 40, back_a), (back_r, back_r), back_r)
        pygame.draw.circle(back, (*theme.AMBER_DARK, back_a), (back_r, back_r), back_r, 2)
        brect = back.get_rect(center=(round(center[0]), round(center[1] + 8)))
        dst.blit(back, brect)
        if self.portrait is not None:
            # 新肖像已在标准画布中完成裁切与边框烘焙；状态动画仍走下方统一路径。
            comp = self.portrait.copy()
        else:
            comp = _surf()
            # 资源缺失时保留原程序化动物与眨眼/耳抖作为可靠后备。
            twitch = 0.0
            if 0 <= self._twitch_t < 0.28:
                twitch = math.sin(self._twitch_t / 0.28 * math.pi * 3) * 10
            if d.ear_l is not None:
                a_l = -twitch if self._twitch_ear < 0 else 0.0
                a_r = twitch if self._twitch_ear > 0 else 0.0
                self._blit_pivot(comp, d.ear_l, d.ear_l_mount, d.ear_pivot, a_l - 6)
                self._blit_pivot(comp, d.ear_r, d.ear_r_mount, d.ear_pivot, a_r + 6)
            comp.blit(d.body, (0, 0))
            # 眼珠(思考时游移)
            gx, gy = self._glance if state == THINKING else (0, 0)
            pupil_col = (24, 18, 12)
            for ex, ey in d.eyes:
                pygame.draw.circle(
                    comp, pupil_col, (CENTER[0] + ex + gx, CENTER[1] + ey + gy), d.eye_r
                )
                pygame.draw.circle(
                    comp, (*theme.AMBER_LIGHT,), (CENTER[0] + ex + gx - 1, CENTER[1] + ey + gy - 1), 1
                )
            # 眨眼:肤色眼睑盖住眼睛；高清肖像不要求逐像素对位眨眼。
            if 0 <= self._blink_t < 0.12:
                for ex, ey in d.eyes:
                    rect = pygame.Rect(0, 0, d.eye_r * 2 + 12, d.eye_r * 2 + 8)
                    rect.center = (CENTER[0] + ex, CENTER[1] + ey)
                    pygame.draw.ellipse(comp, d.skin, rect)
                    pygame.draw.line(
                        comp, (30, 22, 16), rect.midleft, rect.midright, 2
                    )
        # 呼吸缩放
        breathe = 1.0 + 0.018 * math.sin(t * 1.35 + self.phase)
        w = int(BUST_W * self.scale)
        h = int(BUST_H * self.scale * breathe)
        img = pygame.transform.smoothscale(comp, (w, h))
        # 思考:头部微倾
        if state == THINKING:
            img = pygame.transform.rotate(img, 2.5 + math.sin(t * 0.9 + self.phase) * 1.2)
        # 弃牌/已行动变暗(仍保持可辨认)
        if state == FOLDED:
            img = img.copy()
            img.fill((108, 108, 116, 255), special_flags=pygame.BLEND_RGB_MULT)
            img.set_alpha(185)
        elif state == ACTED:
            img = img.copy()
            img.fill((168, 168, 176, 255), special_flags=pygame.BLEND_RGB_MULT)
        rect = img.get_rect(center=(round(center[0]), round(center[1])))
        dst.blit(img, rect)

    def update(self, dt: float) -> None:
        if self._blink_t >= 0:
            self._blink_t += dt
            if self._blink_t > 0.12:
                self._blink_t = -1.0
                self._blink_at = self.rng.uniform(2.0, 5.5)
        else:
            self._blink_at -= dt
            if self._blink_at <= 0:
                self._blink_t = 0.0
        if self._twitch_t >= 0:
            self._twitch_t += dt
            if self._twitch_t > 0.28:
                self._twitch_t = -1.0
                self._twitch_at = self.rng.uniform(4.0, 10.0)
                self._twitch_ear = self.rng.choice((-1, 1))
        else:
            self._twitch_at -= dt
            if self._twitch_at <= 0:
                self._twitch_t = 0.0
        self._glance_at -= dt
        if self._glance_at <= 0:
            self._glance = self.rng.choice(((0, 0), (-2, 0), (2, 0), (1, -1), (-1, 1)))
            self._glance_at = self.rng.uniform(0.3, 0.7)


# ------------------------------------------------------------ 名牌与筹码堆


def draw_name_plate(
    dst: pygame.Surface,
    rect: pygame.Rect,
    name: str,
    stack: int,
    level: str | None = None,
    active: bool = False,
    busted: bool = False,
    status: str | None = None,
    status_color: tuple[int, int, int] | None = None,
) -> None:
    """座位名牌:半透深色圆角底 + 名字 + 筹码 + 等级标签。

    :param status: 可选的状态文字(如「已弃牌」),覆盖筹码/出局显示。
    :param status_color: 状态文字颜色;未指定时沿用筹码/出局配色。
    """
    panel = pygame.Surface(rect.size, pygame.SRCALPHA)
    pygame.draw.rect(panel, (*theme.BG_PANEL, 215), panel.get_rect(), border_radius=10)
    border = theme.AMBER if active else (*theme.AMBER_DARK,)
    pygame.draw.rect(panel, border, panel.get_rect(), 2, border_radius=10)
    dst.blit(panel, rect)
    label = f"{name} · {LEVEL_LABEL.get(level, '')}" if level else name
    theme.text(dst, label, (rect.centerx, rect.top + 8), 17,
               theme.TEXT if not busted else theme.TEXT_DIM, "midtop", shadow=True)
    stack_str = status if status is not None else ("出局" if busted else f"¢ {stack}")
    stack_color = status_color or (theme.GOLD if not busted else theme.TEXT_DIM)
    theme.text(dst, stack_str, (rect.centerx, rect.top + 30), 17,
               stack_color, "midtop", shadow=True)


def draw_chip_pile(
    dst: pygame.Surface,
    center: tuple[float, float],
    amount: int,
    seed: int = 0,
) -> None:
    """当前下注筹码堆:几枚错落筹码 + 金额。"""
    rng = random.Random(seed)
    n = min(6, 1 + amount // 40)
    for i in range(n):
        cx = center[0] + rng.uniform(-18, 18)
        cy = center[1] + rng.uniform(-6, 6) - i * 3
        col = theme.AMBER if i % 2 == 0 else theme.TEAL
        rect = pygame.Rect(0, 0, 34, 15)
        rect.center = (round(cx), round(cy))
        pygame.draw.ellipse(dst, col, rect)
        pygame.draw.ellipse(dst, theme.AMBER_LIGHT if i % 2 == 0 else theme.TEAL, rect, 2)
        pygame.draw.ellipse(dst, theme.BG_PANEL, rect.inflate(-14, -6), 1)
    theme.text(dst, str(amount), (center[0], center[1] - 24), 17,
               theme.GOLD, "midbottom", shadow=True)
