"""UI 控件:琥珀描边按钮、下注滑杆、面板、行动日志、范围绘制板、选牌器、数字输入框。"""
from __future__ import annotations

from collections.abc import Callable

import pygame

from gto.charts import RANKS, canonical_hands

from . import theme


class Button:
    """琥珀描边按钮,支持悬停/按下/选中/禁用视觉状态。"""

    def __init__(
        self,
        rect: pygame.Rect | tuple[int, int, int, int],
        label: str,
        on_click: Callable[[], None] | None = None,
        size: int = 20,
        danger: bool = False,
        enabled: bool = True,
        selected: bool = False,
    ) -> None:
        self.rect = pygame.Rect(rect)
        self.label = label
        self.on_click = on_click
        self.size = size
        self.danger = danger
        self.enabled = enabled
        self.selected = selected
        self._hover = False
        self._pressed = False

    def contains(self, pos: tuple[int, int]) -> bool:
        return self.enabled and self.rect.collidepoint(pos)

    def handle_event(self, ev: pygame.event.Event) -> bool:
        """处理事件;触发了点击时返回 True。"""
        if not self.enabled:
            self._hover = False
            self._pressed = False
            return False
        if ev.type == pygame.MOUSEMOTION:
            self._hover = self.rect.collidepoint(ev.pos)
        elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
            self._pressed = self.rect.collidepoint(ev.pos)
        elif ev.type == pygame.MOUSEBUTTONUP and ev.button == 1:
            was = self._pressed
            self._pressed = False
            if was and self.rect.collidepoint(ev.pos):
                if self.on_click is not None:
                    self.on_click()
                return True
        return False

    def draw(self, dst: pygame.Surface) -> None:
        accent = theme.DANGER if self.danger else theme.AMBER
        if not self.enabled:
            self._hover = False
            self._pressed = False
        text_offset_y = 1 if self._pressed else -1 if self._hover else 0
        rect = self.rect
        if not self.enabled:
            bg = (40, 32, 25) if self.selected else (30, 25, 20)
            border = theme.AMBER_DARK if self.selected else (62, 52, 42)
            fg = (132, 118, 100)
        elif self._pressed:
            bg, border, fg = accent, theme.AMBER_LIGHT, theme.BG
        elif self.selected:
            bg, border, fg = (76, 49, 24), theme.AMBER_LIGHT, theme.AMBER_LIGHT
        elif self._hover:
            bg, border, fg = (68, 46, 25), theme.AMBER_LIGHT, theme.AMBER_LIGHT
        else:
            bg, border, fg = (40, 29, 18), accent, theme.TEXT

        # 外框固定在命中矩形内;只移动文字与内部高光,避免视觉/热区错位。
        pygame.draw.rect(dst, bg, rect, border_radius=8)
        pygame.draw.rect(dst, border, rect, 2, border_radius=8)
        if self.selected and rect.width >= 24:
            pygame.draw.line(
                dst,
                theme.AMBER_LIGHT,
                (rect.left + 10, rect.bottom - 4),
                (rect.right - 10, rect.bottom - 4),
                2,
            )
        elif self._hover and self.enabled:
            pygame.draw.rect(dst, theme.AMBER_DARK, rect.inflate(-6, -6), 1, border_radius=6)
        elif self.enabled and not self._pressed:
            pygame.draw.line(
                dst,
                (93, 63, 34),
                (rect.left + 9, rect.top + 4),
                (rect.right - 9, rect.top + 4),
                1,
            )
        theme.text(
            dst,
            self.label,
            (rect.centerx, rect.centery + text_offset_y),
            self.size,
            fg,
            "center",
        )


class Slider:
    """下注额度滑杆:整数 min..max 吸附,拖动或点击跳转。"""

    def __init__(
        self,
        rect: pygame.Rect | tuple[int, int, int, int],
        lo: int,
        hi: int,
        value: int | None = None,
        on_change: Callable[[int], None] | None = None,
    ) -> None:
        self.rect = pygame.Rect(rect)
        self.lo = lo
        self.hi = max(hi, lo)
        self.value = lo if value is None else max(lo, min(self.hi, value))
        self.on_change = on_change
        self._dragging = False
        self._hover = False

    @property
    def frac(self) -> float:
        return 0.0 if self.hi == self.lo else (self.value - self.lo) / (self.hi - self.lo)

    def set_range(self, lo: int, hi: int) -> None:
        self.lo = lo
        self.hi = max(hi, lo)
        self.set_value(self.value)

    def set_value(self, v: int) -> None:
        v = max(self.lo, min(self.hi, int(round(v))))
        if v != self.value:
            self.value = v
            if self.on_change is not None:
                self.on_change(v)

    def set_frac(self, f: float) -> None:
        self.set_value(self.lo + f * (self.hi - self.lo))

    def _knob_x(self) -> int:
        return round(self.rect.left + 10 + (self.rect.width - 20) * self.frac)

    def _value_at(self, x: int) -> int:
        span = self.rect.width - 20
        f = min(1.0, max(0.0, (x - self.rect.left - 10) / span))
        return round(self.lo + f * (self.hi - self.lo))

    def handle_event(self, ev: pygame.event.Event) -> bool:
        """处理事件;值被拖动改变时返回 True。"""
        if ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
            if self.rect.inflate(0, 12).collidepoint(ev.pos):
                self._dragging = True
                self._hover = True
                self.set_value(self._value_at(ev.pos[0]))
                return True
        elif ev.type == pygame.MOUSEMOTION:
            self._hover = self.rect.inflate(0, 12).collidepoint(ev.pos)
            if self._dragging:
                self.set_value(self._value_at(ev.pos[0]))
                return True
        elif ev.type == pygame.MOUSEBUTTONUP and ev.button == 1:
            self._dragging = False
        return False

    def draw(self, dst: pygame.Surface) -> None:
        track = self.rect
        pygame.draw.rect(dst, (24, 17, 11), track, border_radius=track.height // 2)
        fill = pygame.Rect(track)
        fill.width = max(8, self._knob_x() - track.left)
        pygame.draw.rect(dst, theme.AMBER_DARK, fill, border_radius=track.height // 2)
        edge = theme.AMBER_LIGHT if self._hover or self._dragging else theme.AMBER
        pygame.draw.rect(dst, edge, track, 2 if self._dragging else 1, border_radius=track.height // 2)
        knob = pygame.Rect(0, 0, 20, track.height + 10)
        knob.center = (self._knob_x(), track.centery)
        knob_color = theme.GOLD if self._dragging else theme.AMBER_LIGHT if self._hover else theme.AMBER
        pygame.draw.rect(dst, knob_color, knob, border_radius=6)
        pygame.draw.rect(dst, theme.BG, knob, 2, border_radius=6)


class Panel:
    """半透深色圆角面板(静态助手)。"""

    @staticmethod
    def draw(
        dst: pygame.Surface,
        rect: pygame.Rect | tuple[int, int, int, int],
        alpha: int = 205,
        border: tuple[int, int, int] | None = None,
        radius: int = 12,
    ) -> None:
        r = pygame.Rect(rect)
        s = pygame.Surface(r.size, pygame.SRCALPHA)
        pygame.draw.rect(s, (*theme.BG_PANEL, alpha), s.get_rect(), border_radius=radius)
        if border is not None:
            pygame.draw.rect(s, border, s.get_rect(), 1, border_radius=radius)
        dst.blit(s, r)


class ToastLog:
    """行动叙述日志:保留最近若干条,绘制时旧的逐渐变淡。"""

    def __init__(self, capacity: int = 24) -> None:
        self.capacity = capacity
        self.lines: list[str] = []

    def add(self, msg: str) -> None:
        self.lines.append(msg)
        if len(self.lines) > self.capacity:
            self.lines = self.lines[-self.capacity :]

    def clear(self) -> None:
        self.lines.clear()

    def draw(
        self,
        dst: pygame.Surface,
        rect: pygame.Rect | tuple[int, int, int, int],
        max_lines: int = 8,
        size: int = 16,
    ) -> None:
        r = pygame.Rect(rect)
        shown = self.lines[-max_lines:]
        y = r.bottom - size - 4
        for i, line in enumerate(reversed(shown)):
            # 越旧的行越淡
            frac = i / max(1, len(shown) - 1) if len(shown) > 1 else 0.0
            color = theme.TEXT if i == 0 else _fade(theme.TEXT, theme.TEXT_DIM, 0.35 + frac * 0.6)
            if y < r.top:
                break
            theme.text(dst, line, (r.left + 6, y), size, color, "bottomleft")
            y -= size + 6


def _fade(a, b, t: float):
    t = min(1.0, max(0.0, t))
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


# ------------------------------------------------------------ M5:训练场控件

_BRUSHES = (0.0, 0.25, 0.5, 0.75, 1.0)


def grid_hand(row: int, col: int) -> str:
    """13×13 网格坐标 → 规范牌型键(对角=对子,上三角=同花,下三角=杂花)。"""
    r1, r2 = RANKS[row], RANKS[col]
    if row == col:
        return r1 + r2
    return r1 + r2 + "s" if row < col else r2 + r1 + "o"


class RangePainter:
    """13×13 可编辑范围矩阵。

    左键点击/拖动以当前笔刷权重(0/25/50/75/100%)上色,右键清除(=0)。
    单元格以琥珀色透明度 + 百分比标签展示权重;顶部为笔刷选择行,
    底部为加权组合数与范围占比读数。布局:``rect`` 为网格左上角,
    总高 = 笔刷行(26) + 网格 + 读数行(22)。
    """

    def __init__(
        self,
        pos: tuple[int, int],
        weights: dict[str, float] | None = None,
        cell: int = 26,
        gap: int = 2,
    ) -> None:
        self.x, self.y = pos
        self.cell, self.gap = cell, gap
        self.weights: dict[str, float] = (
            dict(weights) if weights is not None else {h: 0.0 for h in canonical_hands()}
        )
        self.brush = 1.0
        self.hover: tuple[int, int] | None = None
        self._painting = False
        self._clearing = False

    # ------------------------------------------------------------ 几何

    @property
    def grid_w(self) -> int:
        return 13 * (self.cell + self.gap) - self.gap

    @property
    def total_h(self) -> int:
        return 30 + self.grid_w + 24

    def cell_rect(self, row: int, col: int) -> pygame.Rect:
        return pygame.Rect(
            self.x + col * (self.cell + self.gap),
            self.y + 30 + row * (self.cell + self.gap),
            self.cell,
            self.cell,
        )

    def _cell_at(self, pos: tuple[int, int]) -> tuple[int, int] | None:
        for r in range(13):
            for c in range(13):
                if self.cell_rect(r, c).collidepoint(pos):
                    return (r, c)
        return None

    def _brush_rect(self, i: int) -> pygame.Rect:
        return pygame.Rect(self.x + i * 62, self.y, 56, 24)

    # ------------------------------------------------------------ 数据

    def set_weights(self, weights: dict[str, float]) -> None:
        self.weights = {h: float(weights.get(h, 0.0)) for h in canonical_hands()}

    def combo_count(self) -> float:
        """加权组合数(对子 6 / 同花 4 / 杂花 12)。"""
        total = 0.0
        for h, w in self.weights.items():
            if w <= 0:
                continue
            total += (6 if len(h) == 2 else 4 if h[2] == "s" else 12) * w
        return total

    def range_pct(self) -> float:
        """范围占全部 1326 组合的比例。"""
        return self.combo_count() / 1326.0

    def _paint(self, pos: tuple[int, int], value: float) -> None:
        cell = self._cell_at(pos)
        if cell is not None:
            self.weights[grid_hand(*cell)] = value

    # ------------------------------------------------------------ 事件/绘制

    def handle_event(self, ev: pygame.event.Event) -> bool:
        if ev.type == pygame.MOUSEBUTTONDOWN:
            for i, b in enumerate(_BRUSHES):
                if self._brush_rect(i).collidepoint(ev.pos):
                    self.brush = b
                    return True
            if self._cell_at(ev.pos) is not None:
                if ev.button == 1:
                    self._painting = True
                    self._paint(ev.pos, self.brush)
                    return True
                if ev.button == 3:
                    self._clearing = True
                    self._paint(ev.pos, 0.0)
                    return True
        elif ev.type == pygame.MOUSEMOTION:
            self.hover = self._cell_at(ev.pos)
            if self._painting:
                self._paint(ev.pos, self.brush)
                return True
            if self._clearing:
                self._paint(ev.pos, 0.0)
                return True
        elif ev.type == pygame.MOUSEBUTTONUP:
            if ev.button == 1:
                self._painting = False
            elif ev.button == 3:
                self._clearing = False
        return False

    def draw(self, dst: pygame.Surface) -> None:
        for i, b in enumerate(_BRUSHES):
            rect = self._brush_rect(i)
            active = abs(self.brush - b) < 1e-6
            bg = theme.AMBER_DARK if active else (40, 29, 18)
            border = theme.AMBER_LIGHT if active else theme.FELT_EDGE
            pygame.draw.rect(dst, bg, rect, border_radius=6)
            pygame.draw.rect(dst, border, rect, 1, border_radius=6)
            theme.text(dst, f"{b:.0%}", rect.center, 14, theme.TEXT, "center")
        for r in range(13):
            for c in range(13):
                rect = self.cell_rect(r, c)
                w = self.weights.get(grid_hand(r, c), 0.0)
                color = _fade(theme.BG_PANEL, theme.AMBER, w)
                pygame.draw.rect(dst, color, rect, border_radius=4)
                edge = theme.AMBER_LIGHT if (r, c) == self.hover else theme.FELT_EDGE
                pygame.draw.rect(dst, edge, rect, 1, border_radius=4)
                hand = grid_hand(r, c)
                tcolor = theme.BG if w > 0.55 else theme.TEXT if w > 0 else theme.TEXT_DIM
                if 0 < w < 1:
                    theme.text(dst, hand, (rect.centerx, rect.top + 9), 10, tcolor, "center")
                    theme.text(dst, f"{w:.0%}", (rect.centerx, rect.bottom - 9), 10, tcolor, "center")
                else:
                    theme.text(dst, hand, rect.center, 11, tcolor, "center")
        y = self.y + 30 + self.grid_w + 14
        theme.text(
            dst,
            f"组合 {self.combo_count():.1f} / 1326 · 范围 {self.range_pct():.1%}",
            (self.x, y),
            15,
            theme.TEXT_DIM,
        )


class CardPicker:
    """52 选 N 迷你牌池(4 花色行 × 13 点数列)。

    点击选择/再点取消,按选择顺序记录(公共牌翻→转→河);``blocked``
    中的牌置灰且不可选(用于 hero 底牌与公共牌互斥)。总高 = 标题行
    (26) + 4 行格子。
    """

    SUITS = "cdhs"
    SUIT_COLOR = {
        "c": theme.TEAL,
        "d": (122, 164, 210),
        "h": (196, 84, 66),
        "s": theme.TEXT,
    }

    def __init__(
        self,
        pos: tuple[int, int],
        max_cards: int,
        title: str = "",
        cell: int = 26,
        gap: int = 2,
    ) -> None:
        self.x, self.y = pos
        self.max_cards = max_cards
        self.title = title
        self.cell, self.gap = cell, gap
        self.cards: list[str] = []
        self.blocked: set[str] = set()
        self.hover: str | None = None

    @property
    def grid_w(self) -> int:
        return 13 * (self.cell + self.gap) - self.gap

    @property
    def total_h(self) -> int:
        return 26 + 4 * (self.cell + self.gap) - self.gap

    def card_rect(self, suit_i: int, rank_i: int) -> pygame.Rect:
        return pygame.Rect(
            self.x + rank_i * (self.cell + self.gap),
            self.y + 26 + suit_i * (self.cell + self.gap),
            self.cell,
            self.cell,
        )

    def _card_at(self, pos: tuple[int, int]) -> str | None:
        for si, s in enumerate(self.SUITS):
            for ri, r in enumerate(RANKS):
                if self.card_rect(si, ri).collidepoint(pos):
                    return r + s
        return None

    def set_cards(self, cards: list[str]) -> None:
        self.cards = list(cards)[: self.max_cards]

    def _can_interact(self, card: str) -> bool:
        """该牌此刻是否可点击(已选可取消;未满时可新增)。"""
        return card not in self.blocked and (
            card in self.cards or len(self.cards) < self.max_cards
        )

    def handle_event(self, ev: pygame.event.Event) -> bool:
        if ev.type == pygame.MOUSEMOTION:
            card = self._card_at(ev.pos)
            self.hover = card if card is not None and self._can_interact(card) else None
            return False
        if ev.type != pygame.MOUSEBUTTONDOWN or ev.button != 1:
            return False
        card = self._card_at(ev.pos)
        if card is None or not self._can_interact(card):
            return False
        if card in self.cards:
            self.cards.remove(card)
        elif len(self.cards) < self.max_cards:
            self.cards.append(card)
        else:
            return False
        return True

    def draw(self, dst: pygame.Surface) -> None:
        sel = " ".join(self.cards) if self.cards else "未选择"
        theme.text(dst, f"{self.title}{sel}", (self.x, self.y), 16, theme.TEXT_DIM)
        for si, s in enumerate(self.SUITS):
            for ri, r in enumerate(RANKS):
                code = r + s
                rect = self.card_rect(si, ri)
                blocked = code in self.blocked
                selected = code in self.cards
                if selected:
                    bg = theme.AMBER
                elif blocked:
                    bg = (26, 20, 14)
                else:
                    bg = theme.BG_PANEL
                pygame.draw.rect(dst, bg, rect, border_radius=4)
                hovered = code == self.hover and self._can_interact(code)
                edge = (
                    theme.AMBER_LIGHT
                    if selected
                    else theme.TEAL
                    if hovered
                    else theme.FELT_EDGE
                )
                pygame.draw.rect(dst, edge, rect, 2 if selected or hovered else 1, border_radius=4)
                fg = theme.BG if selected else (70, 58, 46) if blocked else self.SUIT_COLOR[s]
                theme.text(dst, r, rect.center, 14, fg, "center")


class NumberField:
    """数字输入框:点击聚焦,键盘输入,Enter/失焦提交。

    非法输入(非数字/超界)以红环提示,``value`` 保持上一次合法值;
    ``text`` 为当前编辑文本。``integer=True`` 时仅接受整数。
    """

    def __init__(
        self,
        rect: pygame.Rect | tuple[int, int, int, int],
        label: str,
        value: float,
        minimum: float = 0.0,
        maximum: float = 1e9,
        integer: bool = True,
    ) -> None:
        self.rect = pygame.Rect(rect)
        self.label = label
        self.value = value
        self.minimum = minimum
        self.maximum = maximum
        self.integer = integer
        self.text = f"{value:g}"
        self.focused = False
        self.valid = True
        self.hovered = False

    def set_value(self, v: float) -> None:
        self.value = v
        self.text = f"{v:g}"
        self.valid = True

    def _commit(self) -> None:
        try:
            v = int(self.text) if self.integer else float(self.text)
        except ValueError:
            self.valid = False
            return
        if not self.minimum <= v <= self.maximum:
            self.valid = False
            return
        self.value = v
        self.text = f"{v:g}"
        self.valid = True

    def handle_event(self, ev: pygame.event.Event) -> bool:
        if ev.type == pygame.MOUSEMOTION:
            self.hovered = self.rect.collidepoint(ev.pos)
            return False
        if ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
            was = self.focused
            self.focused = self.rect.collidepoint(ev.pos)
            if self.focused:
                return True
            if was:
                self._commit()
            return False
        if not self.focused or ev.type != pygame.KEYDOWN:
            return False
        if ev.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_TAB):
            self._commit()
            self.focused = False
        elif ev.key == pygame.K_BACKSPACE:
            self.text = self.text[:-1]
            self.valid = True
        elif ev.key == pygame.K_ESCAPE:
            self.focused = False
        elif ev.unicode and (ev.unicode.isdigit() or (not self.integer and ev.unicode == ".")):
            if len(self.text) < 9:
                self.text += ev.unicode
        return True

    def draw(self, dst: pygame.Surface) -> None:
        theme.text(
            dst, self.label, (self.rect.left - 8, self.rect.centery), 15, theme.TEXT_DIM, "midright"
        )
        border = (
            theme.DANGER
            if not self.valid
            else theme.AMBER_LIGHT
            if self.focused
            else theme.AMBER_DARK
            if self.hovered
            else theme.FELT_EDGE
        )
        pygame.draw.rect(dst, (24, 17, 11), self.rect, border_radius=6)
        pygame.draw.rect(dst, border, self.rect, 2 if self.focused or not self.valid else 1, 6)
        theme.text(dst, self.text, self.rect.center, 16, theme.TEXT, "center")
