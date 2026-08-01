"""动效与氛围特效:补间、烟雾粒子、灯光/暗角、发牌与弃牌动画。

所有大面积叠加层(灯光、暗角、颗粒)均一次性预渲染为 Surface,
每帧只做 blit,不做逐像素 Python 循环,保证 1600x900 下 60 FPS。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

import pygame

from . import theme

# ------------------------------------------------------------ 缓动


class Ease:
    """常用缓动函数,输入输出均为 [0,1]。"""

    @staticmethod
    def linear(t: float) -> float:
        return t

    @staticmethod
    def smoothstep(t: float) -> float:
        t = min(1.0, max(0.0, t))
        return t * t * (3 - 2 * t)

    @staticmethod
    def out_cubic(t: float) -> float:
        t = min(1.0, max(0.0, t))
        return 1 - (1 - t) ** 3

    @staticmethod
    def out_back(t: float, s: float = 1.70158) -> float:
        t = min(1.0, max(0.0, t)) - 1.0
        return 1 + (s + 1) * t**3 + s * t**2


# ------------------------------------------------------------ 补间


class Tween:
    """一次性数值补间(update(dt) 驱动,到达终点后精确停在 end)。"""

    def __init__(
        self,
        start: float,
        end: float,
        duration: float,
        ease=Ease.out_cubic,
        delay: float = 0.0,
    ) -> None:
        self.start = start
        self.end = end
        self.duration = max(1e-6, duration)
        self.ease = ease
        self.delay = max(0.0, delay)
        self.elapsed = 0.0
        self.value = start
        self.done = False

    def update(self, dt: float) -> float:
        """推进 ``dt`` 秒并返回当前值;完成后恒等于 ``end``。"""
        if self.done:
            return self.end
        self.elapsed += dt
        t = (self.elapsed - self.delay) / self.duration
        if t >= 1.0:
            self.done = True
            self.value = self.end
            return self.end
        if t <= 0.0:
            self.value = self.start
            return self.start
        self.value = self.start + (self.end - self.start) * self.ease(t)
        return self.value


def lerp(a: float, b: float, t: float) -> float:
    """线性插值。"""
    return a + (b - a) * t


def lerp_pos(a: tuple[float, float], b: tuple[float, float], t: float) -> tuple[float, float]:
    return (lerp(a[0], b[0], t), lerp(a[1], b[1], t))


# ------------------------------------------------------------ 预渲染叠加层


def radial_glow(
    size: int, color: tuple[int, int, int], max_alpha: int = 150, power: float = 2.2
) -> pygame.Surface:
    """暖色径向灯光(中心亮、向外衰减),返回方形 SRCALPHA Surface。"""
    surf = pygame.Surface((size, size), pygame.SRCALPHA)
    steps = 48
    for i in range(steps, 0, -1):
        r = size // 2 * i // steps
        # 中心密度高、边缘快速衰减的近似高斯
        alpha = int(max_alpha * (1 - i / steps) ** power)
        pygame.draw.circle(surf, (*color, alpha), (size // 2, size // 2), r)
    return surf


def vignette(
    size: tuple[int, int], strength: int = 170, center: tuple[int, int] | None = None
) -> pygame.Surface:
    """四角压暗的暗角层(中心可指定,默认画面中心)。"""
    w, h = size
    cx, cy = center if center is not None else (w // 2, h // 2)
    surf = pygame.Surface(size, pygame.SRCALPHA)
    steps = 40
    max_r = math.hypot(max(cx, w - cx), max(cy, h - cy))
    for i in range(steps):
        frac = i / steps
        alpha = int(strength * frac**2.4)
        rx = max_r * (1 - frac * 0.55) * (w / max_r)
        ry = max_r * (1 - frac * 0.55) * (h / max_r)
        rect = pygame.Rect(0, 0, rx * 2, ry * 2)
        rect.center = (cx, cy)
        pygame.draw.ellipse(surf, (0, 0, 0, alpha), rect, width=max(2, int(max_r / steps) + 2))
    return surf


def grain_overlay(
    size: tuple[int, int], seed: int = 7, dots: int = 2600, scratches: int = 14
) -> pygame.Surface:
    """胶片颗粒 + 划痕层(低透明度黑白噪点与斜线)。"""
    rng = random.Random(seed)
    w, h = size
    surf = pygame.Surface(size, pygame.SRCALPHA)
    for _ in range(dots):
        x, y = rng.randrange(w), rng.randrange(h)
        v = rng.choice((255, 255, 0, 0))
        a = rng.randint(3, 10)
        surf.set_at((x, y), (v, v, v, a))
    for _ in range(scratches):
        x = rng.randrange(w)
        y0 = rng.randrange(h)
        length = rng.randint(30, h)
        drift = rng.randint(-6, 6)
        a = rng.randint(5, 12)
        pygame.draw.line(
            surf, (220, 210, 190, a), (x, y0), (x + drift, min(h, y0 + length))
        )
    return surf


def workspace_backdrop(
    size: tuple[int, int],
    seed: int = 7,
    accent: tuple[int, int, int] = theme.AMBER,
) -> pygame.Surface:
    """分析/训练页使用的克制背景纹理。

    与牌桌的强暗角不同,这里只在内容层下方放置很淡的暖光、木板接缝
    和边缘压暗。题干、牌面、图表与控件随后绘制,不会再被全屏蒙版
    二次压暗。给定 ``seed`` 时结果完全确定,可用于截图回归。
    """
    w, h = size
    surf = pygame.Surface(size, pygame.SRCALPHA)

    glow_size = min(1200, max(w, h))
    glow = radial_glow(glow_size, accent, max_alpha=26, power=2.0)
    surf.blit(glow, glow.get_rect(center=(round(w * 0.68), round(h * 0.18))))

    # 很淡的木板接缝:保留酒馆质感,但不穿过随后绘制的正文和牌面。
    rng = random.Random(seed)
    x = rng.randint(80, 150)
    while x < w:
        pygame.draw.line(surf, (*theme.AMBER_DARK, 8), (x, 0), (x, h), 1)
        x += rng.randint(150, 230)
    for y in (76, h - 54):
        pygame.draw.line(surf, (*theme.AMBER_DARK, 12), (0, y), (w, y), 1)

    # 矩形边缘衰减避免旧椭圆暗角跨越内容区。
    for i in range(10):
        inset_x, inset_y = i * 5, i * 4
        rect = pygame.Rect(
            inset_x,
            inset_y,
            max(1, w - inset_x * 2),
            max(1, h - inset_y * 2),
        )
        pygame.draw.rect(
            surf,
            (0, 0, 0, 5 + i),
            rect,
            width=7,
            border_radius=max(4, 24 - i),
        )

    # 颗粒仅属于背景,密度也低于牌桌效果。
    surf.blit(grain_overlay(size, seed=seed, dots=900, scratches=5), (0, 0))
    return surf


# ------------------------------------------------------------ 烟雾粒子


@dataclass
class _SmokeParticle:
    x: float
    y: float
    vx: float
    vy: float
    life: float
    max_life: float
    size: int
    alpha: int


class ParticleSystem:
    """缓慢上升的半透明烟雾(精灵按透明度分级预渲染,每帧仅 blit)。"""

    _ALPHA_LEVELS = 8

    def __init__(
        self,
        area: tuple[int, int, int, int],
        seed: int | None = None,
        max_particles: int = 46,
        spawn_per_sec: float = 5.0,
        color: tuple[int, int, int] = (150, 138, 122),
    ) -> None:
        self.area = pygame.Rect(area)
        self.rng = random.Random(seed)
        self.max_particles = max_particles
        self.spawn_per_sec = spawn_per_sec
        self.color = color
        self.particles: list[_SmokeParticle] = []
        self._spawn_acc = 0.0
        self._sprites: dict[tuple[int, int], pygame.Surface] = {}
        self._sprite_base: dict[int, pygame.Surface] = {}

    def _base_sprite(self, size: int) -> pygame.Surface:
        """柔和椭圆烟团(径向衰减),按尺寸缓存。"""
        if size not in self._sprite_base:
            s = pygame.Surface((size, size), pygame.SRCALPHA)
            steps = 10
            for i in range(steps, 0, -1):
                r = size // 2 * i // steps
                a = int(60 * (1 - i / steps) ** 1.6)
                rect = pygame.Rect(0, 0, r * 2, int(r * 1.5))
                rect.center = (size // 2, size // 2)
                pygame.draw.ellipse(s, (*self.color, a), rect)
            self._sprite_base[size] = s
        return self._sprite_base[size]

    def _sprite(self, size: int, alpha: int) -> pygame.Surface:
        level = min(self._ALPHA_LEVELS - 1, alpha * self._ALPHA_LEVELS // 256)
        key = (size, level)
        if key not in self._sprites:
            s = self._base_sprite(size).copy()
            s.fill((255, 255, 255, 255 * level // (self._ALPHA_LEVELS - 1)),
                   special_flags=pygame.BLEND_RGBA_MULT)
            self._sprites[key] = s
        return self._sprites[key]

    def _spawn(self) -> None:
        r = self.area
        self.particles.append(
            _SmokeParticle(
                x=self.rng.uniform(r.left, r.right),
                y=self.rng.uniform(r.centery, r.bottom),
                vx=self.rng.uniform(-6, 6),
                vy=self.rng.uniform(-22, -10),
                life=0.0,
                max_life=self.rng.uniform(5.0, 9.0),
                size=self.rng.choice((36, 48, 64, 84)),
                alpha=self.rng.randint(46, 92),
            )
        )

    def update(self, dt: float) -> None:
        self._spawn_acc += dt * self.spawn_per_sec
        while self._spawn_acc >= 1.0 and len(self.particles) < self.max_particles:
            self._spawn_acc -= 1.0
            self._spawn()
        alive: list[_SmokeParticle] = []
        for p in self.particles:
            p.life += dt
            if p.life < p.max_life:
                p.x += p.vx * dt
                p.y += p.vy * dt
                alive.append(p)
        self.particles = alive

    def draw(self, dst: pygame.Surface) -> None:
        for p in self.particles:
            frac = p.life / p.max_life
            fade = min(1.0, frac * 6) * (1 - Ease.smoothstep(frac))
            a = int(p.alpha * fade)
            if a <= 2:
                continue
            sprite = self._sprite(p.size, a)
            rect = sprite.get_rect(center=(round(p.x), round(p.y)))
            dst.blit(sprite, rect)


# ------------------------------------------------------------ 发牌动画


@dataclass
class _MovingCard:
    front: pygame.Surface
    back: pygame.Surface
    src: tuple[float, float]
    dst: tuple[float, float]
    delay: float
    duration: float
    rot: float  # 到位后的静止倾角(度)
    face_up: bool  # 到位时是否正面朝上
    flip: bool = False  # 到位后是否做一次翻转
    t: float = 0.0


class CardAnimator:
    """管理移动中的牌:从牌堆滑向座位(带轻微旋转),可选到位翻面。"""

    FLIP_TIME = 0.16

    def __init__(self) -> None:
        self.cards: list[_MovingCard] = []

    @property
    def busy(self) -> bool:
        """仍有牌在飞行或翻转。"""
        return bool(self.cards)

    def add_deal(
        self,
        front: pygame.Surface,
        back: pygame.Surface,
        src: tuple[float, float],
        dst: tuple[float, float],
        delay: float = 0.0,
        duration: float = 0.35,
        rot: float = 0.0,
        face_up: bool = False,
        flip: bool = False,
    ) -> None:
        self.cards.append(
            _MovingCard(front, back, src, dst, delay, duration, rot, face_up, flip)
        )

    def clear(self) -> None:
        self.cards.clear()

    def update(self, dt: float) -> None:
        done: list[_MovingCard] = []
        for c in self.cards:
            c.t += dt
            total = c.delay + c.duration + (self.FLIP_TIME * 2 if c.flip else 0.0)
            if c.t >= total:
                done.append(c)
        for c in done:
            self.cards.remove(c)

    def draw(self, dst: pygame.Surface) -> None:
        for c in self.cards:
            t = c.t - c.delay
            if t < 0:
                continue
            frac = min(1.0, t / c.duration)
            pos = lerp_pos(c.src, c.dst, Ease.out_cubic(frac))
            rot = c.rot * frac
            surf = c.front if c.face_up else c.back
            flip_t = t - c.duration
            if c.flip and flip_t > 0:
                # 到位后翻转:先压扁再换面展开
                half = self.FLIP_TIME
                if flip_t < half:
                    scale_x = 1 - flip_t / half
                    surf = c.back if not c.face_up else c.back
                else:
                    scale_x = (flip_t - half) / half
                    surf = c.front
                w = max(2, int(surf.get_width() * max(0.02, scale_x)))
                surf = pygame.transform.scale(surf, (w, surf.get_height()))
                rot = c.rot
            img = pygame.transform.rotate(surf, rot) if abs(rot) > 0.5 else surf
            rect = img.get_rect(center=(round(pos[0]), round(pos[1])))
            dst.blit(img, rect)


# ------------------------------------------------------------ 弃牌动画


@dataclass
class _MuckCard:
    surface: pygame.Surface
    src: tuple[float, float]
    dst: tuple[float, float]
    start_rot: float
    end_rot: float
    delay: float
    duration: float
    t: float = 0.0


class MuckAnimator:
    """把座位底牌短促地滑入弃牌堆。

    本动画与 :class:`CardAnimator` 分离，因此不会把下一位玩家的行动
    错当成仍在发牌。轨迹只由座位号和牌序决定，不使用随机数，固定
    ``dt`` 的无头截图可以逐像素复现。
    """

    def __init__(self) -> None:
        self.cards: list[_MuckCard] = []

    @property
    def busy(self) -> bool:
        """仍有牌正在滑向弃牌堆。"""
        return bool(self.cards)

    def launch(
        self,
        source_cards: list[tuple[pygame.Surface, tuple[float, float], float]],
        dst: tuple[float, float],
        seat: int,
        duration: float = 0.48,
    ) -> None:
        """加入一组弃牌。

        ``source_cards`` 必须由调用方在引擎应用 FOLD 前捕获，元素为
        ``(牌面 Surface, 座位起点, 起始倾角)``。
        """
        for index, (surface, src, start_rot) in enumerate(source_cards):
            direction = -1.0 if (seat + index) % 2 else 1.0
            end_rot = start_rot + direction * (31.0 + seat * 1.5 + index * 5.0)
            target = (dst[0] + (index - 0.5) * 12.0, dst[1] + index * 5.0)
            self.cards.append(
                _MuckCard(
                    surface=surface,
                    src=src,
                    dst=target,
                    start_rot=start_rot,
                    end_rot=end_rot,
                    delay=index * 0.035,
                    duration=max(0.08, duration),
                )
            )

    def clear(self) -> None:
        self.cards.clear()

    def update(self, dt: float) -> None:
        """推进动画并移除已经完全淡出的牌。"""
        for card in self.cards:
            card.t += dt
        self.cards = [
            card
            for card in self.cards
            if card.t < card.delay + card.duration
        ]

    def draw(self, dst: pygame.Surface) -> None:
        for card in self.cards:
            local_t = card.t - card.delay
            if local_t < 0:
                continue
            frac = min(1.0, local_t / card.duration)
            eased = Ease.out_cubic(frac)
            pos = lerp_pos(card.src, card.dst, eased)
            # 轻微抛物线让牌像被手腕甩出，而不是匀速平移。
            arc = math.sin(frac * math.pi) * 18.0
            angle = card.start_rot + (card.end_rot - card.start_rot) * eased
            scale = 1.0 - 0.42 * Ease.smoothstep(frac)
            width = max(2, round(card.surface.get_width() * scale))
            height = max(2, round(card.surface.get_height() * scale))
            image = pygame.transform.smoothscale(card.surface, (width, height))
            image = pygame.transform.rotate(image, angle)
            fade = 1.0 if frac <= 0.38 else (1.0 - frac) / 0.62
            image.set_alpha(max(0, min(255, round(255 * fade))))
            rect = image.get_rect(center=(round(pos[0]), round(pos[1] - arc)))
            dst.blit(image, rect)


# ------------------------------------------------------------ 筹码飞行动画


@dataclass
class _Chip:
    src: tuple[float, float]
    dst: tuple[float, float]
    delay: float
    duration: float
    t: float = 0.0


class ChipFly:
    """结算时筹码从底池飞向胜者座位的小动画。"""

    def __init__(self) -> None:
        self.chips: list[_Chip] = []
        self._sprite: pygame.Surface | None = None

    def sprite(self) -> pygame.Surface:
        if self._sprite is None:
            s = pygame.Surface((26, 26), pygame.SRCALPHA)
            pygame.draw.circle(s, theme.AMBER, (13, 13), 12)
            pygame.draw.circle(s, theme.AMBER_LIGHT, (13, 13), 12, 2)
            pygame.draw.circle(s, theme.BG_PANEL, (13, 13), 6)
            self._sprite = s
        return self._sprite

    def launch(
        self,
        src: tuple[float, float],
        dst: tuple[float, float],
        count: int = 7,
        rng: random.Random | None = None,
    ) -> None:
        rng = rng or random.Random(0)
        for i in range(count):
            jitter = (rng.uniform(-14, 14), rng.uniform(-10, 10))
            self.chips.append(
                _Chip(
                    (src[0] + jitter[0], src[1] + jitter[1]),
                    (dst[0] + jitter[0], dst[1] + jitter[1]),
                    delay=i * 0.05,
                    duration=0.45,
                )
            )

    @property
    def busy(self) -> bool:
        return bool(self.chips)

    def update(self, dt: float) -> None:
        done = []
        for c in self.chips:
            c.t += dt
            if c.t >= c.delay + c.duration:
                done.append(c)
        for c in done:
            self.chips.remove(c)

    def draw(self, dst: pygame.Surface) -> None:
        s = self.sprite()
        for c in self.chips:
            t = c.t - c.delay
            if t < 0:
                continue
            frac = Ease.out_back(min(1.0, t / c.duration))
            pos = lerp_pos(c.src, c.dst, frac)
            # 抛物线弧度
            arc = math.sin(min(1.0, t / c.duration) * math.pi) * 40
            rect = s.get_rect(center=(round(pos[0]), round(pos[1] - arc)))
            dst.blit(s, rect)
