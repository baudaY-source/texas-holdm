"""新对局设置:选择 2–9 人桌并逐座位配置初始买入。"""
from __future__ import annotations

import pygame

from ai.personas import persona_catalog
from ai.styles import style_by_key
from engine.game import MAX_BUYIN_BB, MIN_BUYIN_BB

from .. import fx, theme
from ..widgets import Button, NumberField, Panel
from .manager import Scene, SceneManager
from .table import BIG_BLIND, SMALL_BLIND

DEFAULT_BUYIN_BB = 100
BUYIN_PRESETS_BB = (50, 100, 200)
MIN_PLAYERS, MAX_PLAYERS, DEFAULT_PLAYERS = 2, 9, 6


class GameSetupScene(Scene):
    """2–9 人局开局设置场景。

    人数切换不会丢弃暂时隐藏座位的输入；每个座位独立保存 BB 数，
    合法范围由引擎常量统一定义。点击
    「开始对局」后才构造 :class:`~ui.scenes.table.TableScene` 并发首手牌。
    """

    def __init__(
        self,
        manager: SceneManager | None = None,
        seed: int | None = None,
        initial_buyins_bb: tuple[int, ...] | None = None,
        player_count: int | None = None,
    ) -> None:
        super().__init__(manager)
        self.seed = seed
        self.personas = persona_catalog(seed)
        if player_count is None:
            player_count = (
                len(initial_buyins_bb)
                if initial_buyins_bb is not None
                else DEFAULT_PLAYERS
            )
        if not MIN_PLAYERS <= player_count <= MAX_PLAYERS:
            raise ValueError(f"游玩人数须在 {MIN_PLAYERS}–{MAX_PLAYERS} 之间")
        if initial_buyins_bb is not None and len(initial_buyins_bb) != player_count:
            raise ValueError("开局买入数量须与游玩人数一致")
        self.player_count = player_count
        values = list(initial_buyins_bb or ())
        values.extend([DEFAULT_BUYIN_BB] * (MAX_PLAYERS - len(values)))

        self.fields: list[NumberField] = []
        self.seat_preset_btns: list[list[Button]] = []
        for seat in range(MAX_PLAYERS):
            card = self._card_rect(seat)
            self.fields.append(
                NumberField(
                    (card.left + 298, card.top + 20, 104, 38),
                    "买入",
                    values[seat],
                    minimum=MIN_BUYIN_BB,
                    maximum=MAX_BUYIN_BB,
                )
            )
            self.seat_preset_btns.append(
                [
                    Button(
                        (card.left + 194 + i * 74, card.top + 106, 66, 30),
                        f"{bb}",
                        lambda s=seat, v=bb: self.set_buyin_bb(s, v),
                        size=14,
                    )
                    for i, bb in enumerate(BUYIN_PRESETS_BB)
                ]
            )

        self.player_count_btns = [
            Button(
                (454 + (count - MIN_PLAYERS) * 86, 104, 74, 38),
                str(count),
                lambda value=count: self.set_player_count(value),
                size=18,
            )
            for count in range(MIN_PLAYERS, MAX_PLAYERS + 1)
        ]

        self.btn_back = Button(
            (72, 826, 190, 50), "返回主菜单", self._back_to_menu, size=21
        )
        self.all_preset_btns = [
            Button(
                (570 + i * 150, 830, 136, 42),
                f"全桌 {bb}BB",
                lambda v=bb: self._set_all(v),
                size=17,
            )
            for i, bb in enumerate(BUYIN_PRESETS_BB)
        ]
        self.btn_start = Button(
            (1312, 826, 216, 50), "开始对局  »", self._start_game, size=22
        )

        self.smoke = fx.ParticleSystem((220, 160, 1380, 800), seed=seed, max_particles=34)
        self._glow = fx.radial_glow(1250, (255, 182, 98), 135, power=1.9)
        self._vignette = fx.vignette((1600, 900), 150)
        self._grain = fx.grain_overlay((1600, 900), seed=17)
        self._t = 0.0
        self._refresh_validity()

    # ------------------------------------------------------------ 数据

    @staticmethod
    def _card_rect(seat: int) -> pygame.Rect:
        """九席固定三列网格；人数切换只决定显示前多少席。"""
        return pygame.Rect(
            74 + (seat % 3) * 492,
            206 + (seat // 3) * 186,
            470,
            164,
        )

    @property
    def _cards(self) -> list[pygame.Rect]:
        return [self._card_rect(seat) for seat in range(self.player_count)]

    @property
    def buyins_bb(self) -> tuple[int, ...] | None:
        """返回当前在用座位的合法买入；任一输入无效时返回 ``None``。"""
        values: list[int] = []
        for field in self.fields[: self.player_count]:
            try:
                value = int(field.text)
            except ValueError:
                return None
            if not MIN_BUYIN_BB <= value <= MAX_BUYIN_BB:
                return None
            values.append(value)
        return tuple(values)

    def set_player_count(self, player_count: int) -> None:
        """切换 2–9 人桌；隐藏座位的金额保留，便于来回比较。"""
        if not MIN_PLAYERS <= player_count <= MAX_PLAYERS:
            raise ValueError(f"游玩人数须在 {MIN_PLAYERS}–{MAX_PLAYERS} 之间")
        self.player_count = player_count
        self._refresh_validity()

    def set_buyin_bb(self, seat: int, value: int) -> None:
        """设置单一座位的 BB 买入，供预设按钮和测试复用。"""
        if not 0 <= seat < len(self.fields):
            raise ValueError(f"座位不存在: {seat}")
        self.fields[seat].set_value(value)
        self._refresh_validity()

    def _set_all(self, value: int) -> None:
        for field in self.fields[: self.player_count]:
            field.set_value(value)
        self._refresh_validity()

    def _refresh_validity(self) -> None:
        """按正在编辑的文本即时刷新红框与开始按钮状态。"""
        all_valid = True
        for index, field in enumerate(self.fields):
            if index >= self.player_count:
                field.valid = True
                continue
            try:
                value = int(field.text)
            except ValueError:
                value = -1
            field.valid = MIN_BUYIN_BB <= value <= MAX_BUYIN_BB
            all_valid = all_valid and field.valid
        self.btn_start.enabled = all_valid

    # ------------------------------------------------------------ 跳转/事件

    def _start_game(self) -> None:
        buyins = self.buyins_bb
        if buyins is None or self.manager is None:
            return
        from .table import TableScene

        self.manager.replace(TableScene(seed=self.seed, buyins_bb=buyins))

    def _back_to_menu(self) -> None:
        if self.manager is not None:
            from .menu import MenuScene

            self.manager.replace(MenuScene(seed=self.seed))

    def handle_event(self, ev: pygame.event.Event) -> None:
        if ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE:
            focused = any(field.focused for field in self.fields)
            if focused:
                for field in self.fields[: self.player_count]:
                    field.handle_event(ev)
                self._refresh_validity()
            else:
                self._back_to_menu()
            return

        # 输入框先处理，使点击「开始」的按下事件能先提交当前编辑值。
        for field in self.fields[: self.player_count]:
            field.handle_event(ev)
        self._refresh_validity()
        for button in self.player_count_btns:
            button.handle_event(ev)
        for row in self.seat_preset_btns[: self.player_count]:
            for button in row:
                button.handle_event(ev)
        for button in self.all_preset_btns:
            button.handle_event(ev)
        self.btn_back.handle_event(ev)
        self.btn_start.handle_event(ev)

    # ------------------------------------------------------------ 帧驱动/绘制

    def update(self, dt: float) -> None:
        self._t += dt
        self.smoke.update(dt)

    def draw(self, dst: pygame.Surface) -> None:
        dst.fill(theme.BG)
        dst.blit(self._glow, self._glow.get_rect(center=(800, 405)))
        self.smoke.draw(dst)
        # 氛围后处理只属于背景层，不能覆盖并压暗正文与输入控件。
        dst.blit(self._vignette, (0, 0))
        dst.blit(self._grain, (0, 0))
        Panel.draw(dst, (54, 178, 1492, 568), alpha=225, border=theme.FELT_EDGE, radius=18)

        theme.text(dst, "筹码入场", (800, 42), 42, theme.AMBER_LIGHT, "center", shadow=True)
        theme.text(
            dst,
            "选择游玩人数，再为每位牌手设定初始买入",
            (800, 78),
            16,
            theme.TEXT_DIM,
            "center",
        )
        theme.text(dst, "人数", (430, 123), 15, theme.TEXT_DIM, "midright")
        for count, button in zip(
            range(MIN_PLAYERS, MAX_PLAYERS + 1), self.player_count_btns
        ):
            button.selected = count == self.player_count
            button.draw(dst)
        straddle = (
            f"    ·    STR {BIG_BLIND * 2}（UTG 强制 2BB）"
            if self.player_count >= 8
            else ""
        )
        theme.text(
            dst,
            f"盲注 {SMALL_BLIND} / {BIG_BLIND}    ·    1BB = {BIG_BLIND} 筹码"
            f"{straddle}    ·    合法范围 {MIN_BUYIN_BB}–{MAX_BUYIN_BB}BB",
            (800, 162),
            15,
            theme.TEAL,
            "center",
        )

        for seat, card in enumerate(self._cards):
            self._draw_seat_card(dst, seat, card)

        values = self.buyins_bb
        if values is None:
            summary = f"每个座位均须输入 {MIN_BUYIN_BB}–{MAX_BUYIN_BB} 的整数 BB"
            summary_color = theme.DANGER
        else:
            total_bb = sum(values)
            summary = (
                f"全桌总买入  {total_bb:,}BB  ·  {total_bb * BIG_BLIND:,} 筹码"
            )
            summary_color = theme.GOLD
        theme.text(dst, summary, (800, 786), 17, summary_color, "center")

        self.btn_back.draw(dst)
        for button in self.all_preset_btns:
            button.draw(dst)
        self.btn_start.draw(dst)
        theme.text(dst, "ESC 返回", (58, 882), 13, theme.TEXT_DIM, "bottomleft")

    def _draw_seat_card(self, dst: pygame.Surface, seat: int, card: pygame.Rect) -> None:
        """绘制单个座位的身份、输入值、筹码换算和快捷档位。"""
        pygame.draw.rect(dst, (35, 24, 15), card, border_radius=12)
        pygame.draw.rect(dst, theme.FELT_EDGE, card, 1, border_radius=12)
        if seat == 0:
            name = "你"
            meta = "座位 1 · 人类牌手"
        else:
            persona = self.personas[seat - 1]
            level = {"fish": "入门", "reg": "常客", "shark": "高手"}.get(
                persona.level, persona.level
            )
            name = persona.display_name
            style = style_by_key(persona.style_key).label
            meta = f"座位 {seat + 1} · {persona.species} · {level} · {style}"
        theme.text(dst, name, (card.left + 18, card.top + 18), 20, theme.TEXT)
        theme.text(dst, meta, (card.left + 18, card.top + 50), 13, theme.TEXT_DIM)
        theme.text(dst, "快捷档位", (card.left + 18, card.top + 112), 13, theme.TEXT_DIM)

        field = self.fields[seat]
        field.draw(dst)
        theme.text(dst, "BB", (field.rect.right + 9, field.rect.centery), 16, theme.GOLD, "midleft")
        try:
            value = int(field.text)
        except ValueError:
            value = 0
        chips = value * BIG_BLIND if field.valid else 0
        detail = f"= {chips:,} 筹码" if field.valid else "输入超出范围"
        theme.text(
            dst,
            detail,
            (card.right - 18, card.top + 70),
            13,
            theme.TEXT_DIM if field.valid else theme.DANGER,
            "topright",
        )
        for button in self.seat_preset_btns[seat]:
            button.draw(dst)
