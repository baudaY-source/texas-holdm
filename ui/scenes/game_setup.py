"""新对局设置：目标人数、自由选将、打法与逐席买入。"""
from __future__ import annotations

from dataclasses import dataclass

import pygame

from ai.personas import Persona, persona_catalog
from ai.styles import StylePreset, style_catalog
from engine.game import MAX_BUYIN_BB, MIN_BUYIN_BB

from .. import fx, theme
from ..characters import Bust, IDLE
from ..widgets import Button, NumberField, Panel
from .manager import Scene, SceneManager
from .table import BIG_BLIND, SMALL_BLIND

DEFAULT_BUYIN_BB = 100
BUYIN_PRESETS_BB = (50, 100, 200)
MIN_PLAYERS, MAX_PLAYERS, DEFAULT_PLAYERS = 2, 9, 6


@dataclass(frozen=True)
class DraftPick:
    """开局阵容中的一个 AI 身份/打法组合。"""

    persona_id: str
    style_key: str


class GameSetupScene(Scene):
    """2–9 人自由选将开局页。

    人类固定在座位 1；用户从完整动物池中选择身份与打法，依加入顺序填充
    其余座位。只有目标人数已选齐且所有买入合法时才能开始。人数减少会把
    超出目标的尾部角色释放回选将池，逐席金额仍保留供再次扩桌使用。
    """

    def __init__(
        self,
        manager: SceneManager | None = None,
        seed: int | None = None,
        initial_buyins_bb: tuple[int, ...] | None = None,
        player_count: int | None = None,
        initial_lineup: tuple[tuple[str, str], ...] | None = None,
    ) -> None:
        super().__init__(manager)
        self.seed = seed
        self.personas = persona_catalog(seed)
        self.persona_by_id = {
            persona.persona_id: persona for persona in self.personas
        }
        self.styles = style_catalog()
        self.style_by_key = {preset.key: preset for preset in self.styles}

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
        for seat in range(MAX_PLAYERS):
            slot = self._slot_rect(seat)
            self.fields.append(
                NumberField(
                    (slot.right - 78, slot.top + 48, 58, 28),
                    "",
                    values[seat],
                    minimum=MIN_BUYIN_BB,
                    maximum=MAX_BUYIN_BB,
                )
            )

        self.lineup = self._normalize_initial_lineup(initial_lineup or ())
        initial_persona = self._first_available_persona()
        self.selected_persona_id = initial_persona.persona_id
        self.selected_style_key = initial_persona.style_key
        self.preview_bust = Bust(initial_persona.species, seed=seed or 0, scale=0.31)
        self.pool_thumbnails = {
            persona.persona_id: self._make_thumbnail(persona, index)
            for index, persona in enumerate(self.personas)
        }

        self.player_count_btns = [
            Button(
                (454 + (count - MIN_PLAYERS) * 86, 104, 74, 38),
                str(count),
                lambda value=count: self.set_player_count(value),
                size=18,
            )
            for count in range(MIN_PLAYERS, MAX_PLAYERS + 1)
        ]
        self.pool_buttons = [
            Button(
                self._pool_rect(index),
                "",
                lambda key=persona.persona_id: self.select_persona(key),
                size=12,
            )
            for index, persona in enumerate(self.personas)
        ]
        self.style_buttons = [
            Button(
                (
                    1012 + (index % 4) * 128,
                    350 + (index // 4) * 40,
                    120,
                    34,
                ),
                preset.label,
                lambda key=preset.key: self.select_style(key),
                size=13,
            )
            for index, preset in enumerate(self.styles)
        ]
        self.remove_buttons = [
            Button(
                (
                    self._slot_rect(seat).right - 33,
                    self._slot_rect(seat).top + 7,
                    24,
                    24,
                ),
                "×",
                lambda index=seat - 1: self.remove_opponent(index),
                size=14,
                danger=True,
            )
            for seat in range(1, MAX_PLAYERS)
        ]
        self.btn_add = Button(
            (1322, 453, 194, 42),
            "加入当前阵容  »",
            self.add_selected_persona,
            size=16,
        )
        self.btn_back = Button(
            (72, 836, 190, 48), "返回主菜单", self._back_to_menu, size=20
        )
        self.all_preset_btns = [
            Button(
                (520 + i * 144, 840, 132, 40),
                f"全桌 {bb}BB",
                lambda value=bb: self._set_all(value),
                size=15,
            )
            for i, bb in enumerate(BUYIN_PRESETS_BB)
        ]
        self.btn_start = Button(
            (1312, 836, 216, 48), "开始对局  »", self._start_game, size=21
        )

        self.smoke = fx.ParticleSystem(
            (220, 160, 1380, 800), seed=seed, max_particles=34
        )
        self._glow = fx.radial_glow(1250, (255, 182, 98), 135, power=1.9)
        self._vignette = fx.vignette((1600, 900), 150)
        self._grain = fx.grain_overlay((1600, 900), seed=17)
        self._t = 0.0
        self._refresh_validity()

    # ------------------------------------------------------------ 几何/数据

    @staticmethod
    def _slot_rect(seat: int) -> pygame.Rect:
        """人类与最多八名 AI 的 3×3 紧凑阵容槽。"""
        return pygame.Rect(
            72 + (seat % 3) * 296,
            214 + (seat // 3) * 96,
            282,
            86,
        )

    @staticmethod
    def _pool_rect(index: int) -> pygame.Rect:
        """十五名候选角色固定 5×3 同屏展示。"""
        return pygame.Rect(
            72 + (index % 5) * 294,
            558 + (index // 5) * 78,
            280,
            70,
        )

    @staticmethod
    def _copy_field_state(source: NumberField, target: NumberField) -> None:
        """移除中间角色时，让其后角色的买入跟随身份前移。"""
        target.value = source.value
        target.text = source.text
        target.valid = source.valid
        target.focused = False

    def _make_thumbnail(self, persona: Persona, index: int) -> pygame.Surface:
        surface = pygame.Surface((64, 68), pygame.SRCALPHA)
        portrait = Bust(
            persona.species,
            seed=(self.seed or 0) + index * 31,
            scale=0.17,
        )
        portrait.draw(surface, (32, 34), 0.0, IDLE)
        return surface

    def _normalize_initial_lineup(
        self,
        lineup: tuple[tuple[str, str], ...],
    ) -> list[DraftPick]:
        if len(lineup) > self.player_count - 1:
            raise ValueError("初始 AI 阵容超过目标席位数")
        result: list[DraftPick] = []
        occupied: set[str] = set()
        for persona_id, style_key in lineup:
            persona_id = str(persona_id).strip().lower()
            style_key = str(style_key).strip().upper()
            if persona_id not in self.persona_by_id:
                raise ValueError(f"未知 AI 身份: {persona_id}")
            if style_key not in self.style_by_key:
                raise ValueError(f"未知 AI 打法: {style_key}")
            if persona_id in occupied:
                raise ValueError("初始 AI 阵容中的身份不可重复")
            occupied.add(persona_id)
            result.append(DraftPick(persona_id, style_key))
        return result

    def _first_available_persona(self) -> Persona:
        occupied = {pick.persona_id for pick in self.lineup}
        return next(
            (persona for persona in self.personas if persona.persona_id not in occupied),
            self.personas[0],
        )

    @property
    def opponent_lineup(self) -> tuple[tuple[str, str], ...]:
        return tuple((pick.persona_id, pick.style_key) for pick in self.lineup)

    @property
    def buyins_bb(self) -> tuple[int, ...] | None:
        """返回目标座位的合法买入；任一已用输入无效时返回 ``None``。"""
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

    @property
    def roster_ready(self) -> bool:
        return len(self.lineup) == self.player_count - 1

    def set_player_count(self, player_count: int) -> None:
        """切换目标人数；多出的尾部角色自动回到选将池。"""
        if not MIN_PLAYERS <= player_count <= MAX_PLAYERS:
            raise ValueError(f"游玩人数须在 {MIN_PLAYERS}–{MAX_PLAYERS} 之间")
        self.player_count = player_count
        del self.lineup[player_count - 1 :]
        self._refresh_validity()

    def select_persona(self, persona_id: str) -> bool:
        """在候选池选中一名尚未入桌的角色。"""
        persona_id = persona_id.strip().lower()
        if persona_id not in self.persona_by_id:
            raise ValueError(f"未知 AI 身份: {persona_id}")
        if persona_id in {pick.persona_id for pick in self.lineup}:
            return False
        persona = self.persona_by_id[persona_id]
        self.selected_persona_id = persona_id
        self.selected_style_key = persona.style_key
        self.preview_bust = Bust(
            persona.species,
            seed=(self.seed or 0) + len(persona_id) * 41,
            scale=0.31,
        )
        self._refresh_validity()
        return True

    def select_style(self, style_key: str) -> None:
        style_key = style_key.strip().upper()
        if style_key not in self.style_by_key:
            raise ValueError(f"未知 AI 打法: {style_key}")
        self.selected_style_key = style_key
        self._refresh_validity()

    def add_selected_persona(self) -> bool:
        """把当前候选按所选打法追加到下一个空席。"""
        occupied = {pick.persona_id for pick in self.lineup}
        if (
            len(self.lineup) >= self.player_count - 1
            or self.selected_persona_id in occupied
        ):
            return False
        self.lineup.append(
            DraftPick(self.selected_persona_id, self.selected_style_key)
        )
        next_persona = self._first_available_persona()
        self.selected_persona_id = next_persona.persona_id
        self.selected_style_key = next_persona.style_key
        self.preview_bust = Bust(
            next_persona.species,
            seed=(self.seed or 0) + len(self.lineup) * 101,
            scale=0.31,
        )
        self._refresh_validity()
        return True

    def remove_opponent(self, index: int) -> bool:
        """移出阵容中的一名 AI，并把该身份重新放回选将池。"""
        if not 0 <= index < len(self.lineup):
            return False
        removed = self.lineup.pop(index)
        old_ai_count = len(self.lineup) + 1
        for seat in range(index + 1, old_ai_count):
            self._copy_field_state(self.fields[seat + 1], self.fields[seat])
        self.fields[old_ai_count].set_value(DEFAULT_BUYIN_BB)
        persona = self.persona_by_id[removed.persona_id]
        self.selected_persona_id = removed.persona_id
        self.selected_style_key = removed.style_key
        self.preview_bust = Bust(
            persona.species,
            seed=(self.seed or 0) + index * 101 + 17,
            scale=0.31,
        )
        self._refresh_validity()
        return True

    def set_buyin_bb(self, seat: int, value: int) -> None:
        if not 0 <= seat < len(self.fields):
            raise ValueError(f"座位不存在: {seat}")
        self.fields[seat].set_value(value)
        self._refresh_validity()

    def _set_all(self, value: int) -> None:
        for field in self.fields[: self.player_count]:
            field.set_value(value)
        self._refresh_validity()

    def _refresh_validity(self) -> None:
        """刷新金额、选将池、加入按钮与开始门槛。"""
        active_fields = 1 + len(self.lineup)
        all_valid = True
        for index, field in enumerate(self.fields):
            if index >= active_fields:
                field.valid = True
                continue
            try:
                value = int(field.text)
            except ValueError:
                value = -1
            field.valid = MIN_BUYIN_BB <= value <= MAX_BUYIN_BB
            all_valid = all_valid and field.valid

        missing = self.player_count - 1 - len(self.lineup)
        self.btn_start.enabled = missing == 0 and all_valid
        if missing > 0:
            self.btn_start.label = f"还差 {missing} 名牌手"
        elif not all_valid:
            self.btn_start.label = "检查买入金额"
        else:
            self.btn_start.label = "开始对局  »"

        occupied = {pick.persona_id for pick in self.lineup}
        can_add = (
            len(self.lineup) < self.player_count - 1
            and self.selected_persona_id not in occupied
        )
        self.btn_add.enabled = can_add
        if self.selected_persona_id in occupied:
            self.btn_add.label = "该角色已在阵容"
        elif len(self.lineup) >= self.player_count - 1:
            self.btn_add.label = "当前阵容已满"
        else:
            self.btn_add.label = "加入当前阵容  »"

        for persona, button in zip(self.personas, self.pool_buttons):
            button.enabled = (
                persona.persona_id not in occupied
                and len(self.lineup) < self.player_count - 1
            )

    # ------------------------------------------------------------ 跳转/事件

    def _start_game(self) -> None:
        buyins = self.buyins_bb
        if (
            buyins is None
            or not self.roster_ready
            or self.manager is None
        ):
            return
        from .table import TableScene

        self.manager.replace(
            TableScene(
                seed=self.seed,
                buyins_bb=buyins,
                opponent_lineup=self.opponent_lineup,
            )
        )

    def _back_to_menu(self) -> None:
        if self.manager is not None:
            from .menu import MenuScene

            self.manager.replace(MenuScene(seed=self.seed))

    def handle_event(self, ev: pygame.event.Event) -> None:
        active_fields = self.fields[: 1 + len(self.lineup)]
        if ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE:
            if any(field.focused for field in active_fields):
                for field in active_fields:
                    field.handle_event(ev)
                self._refresh_validity()
            else:
                self._back_to_menu()
            return

        for field in active_fields:
            field.handle_event(ev)
        self._refresh_validity()
        for button in self.player_count_btns:
            button.handle_event(ev)
        for button in self.remove_buttons[: len(self.lineup)]:
            button.handle_event(ev)
        for button in self.pool_buttons:
            button.handle_event(ev)
        for button in self.style_buttons:
            button.handle_event(ev)
        self.btn_add.handle_event(ev)
        for button in self.all_preset_btns:
            button.handle_event(ev)
        self.btn_back.handle_event(ev)
        self.btn_start.handle_event(ev)
        self._refresh_validity()

    # ------------------------------------------------------------ 帧驱动/绘制

    def update(self, dt: float) -> None:
        self._t += dt
        self.smoke.update(dt)
        self.preview_bust.update(dt)

    def draw(self, dst: pygame.Surface) -> None:
        dst.fill(theme.BG)
        dst.blit(self._glow, self._glow.get_rect(center=(800, 405)))
        self.smoke.draw(dst)
        dst.blit(self._vignette, (0, 0))
        dst.blit(self._grain, (0, 0))

        theme.text(
            dst,
            "酒馆选将",
            (800, 38),
            40,
            theme.AMBER_LIGHT,
            "center",
            shadow=True,
        )
        theme.text(
            dst,
            "先定人数，再从动物池选择身份与打法；加入顺序就是座位顺序",
            (800, 75),
            15,
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
            f" · STR {BIG_BLIND * 2}（UTG 2BB）"
            if self.player_count >= 8
            else ""
        )
        theme.text(
            dst,
            f"盲注 {SMALL_BLIND}/{BIG_BLIND} · 1BB={BIG_BLIND}筹码"
            f"{straddle} · 买入 {MIN_BUYIN_BB}–{MAX_BUYIN_BB}BB",
            (800, 162),
            14,
            theme.TEAL,
            "center",
        )

        Panel.draw(dst, (54, 178, 925, 330), alpha=232, border=theme.FELT_EDGE)
        Panel.draw(dst, (990, 178, 556, 330), alpha=236, border=theme.FELT_EDGE)
        Panel.draw(dst, (54, 520, 1492, 278), alpha=232, border=theme.FELT_EDGE)
        theme.text(
            dst,
            f"当前阵容  {1 + len(self.lineup)}/{self.player_count}",
            (72, 194),
            17,
            theme.AMBER_LIGHT,
        )
        theme.text(
            dst,
            "人类固定座位 1 · 可移除 AI 后重新选择",
            (950, 194),
            13,
            theme.TEXT_DIM,
            "topright",
        )
        for seat in range(self.player_count):
            self._draw_roster_slot(dst, seat)

        self._draw_candidate_editor(dst)
        self._draw_pool(dst)

        values = self.buyins_bb
        missing = self.player_count - 1 - len(self.lineup)
        if missing > 0:
            summary = f"阵容未满 · 还需从选将池加入 {missing} 名 AI"
            summary_color = theme.AMBER_LIGHT
        elif values is None:
            summary = f"每个在桌座位均须输入 {MIN_BUYIN_BB}–{MAX_BUYIN_BB}BB"
            summary_color = theme.DANGER
        else:
            total_bb = sum(values)
            summary = f"阵容已就绪 · 总买入 {total_bb:,}BB / {total_bb * BIG_BLIND:,} 筹码"
            summary_color = theme.GOLD
        theme.text(dst, summary, (800, 813), 15, summary_color, "center")

        self.btn_back.draw(dst)
        for button in self.all_preset_btns:
            button.draw(dst)
        self.btn_start.draw(dst)
        theme.text(dst, "ESC 返回", (58, 892), 12, theme.TEXT_DIM, "bottomleft")

    def _draw_roster_slot(self, dst: pygame.Surface, seat: int) -> None:
        card = self._slot_rect(seat)
        pygame.draw.rect(dst, (35, 24, 15), card, border_radius=10)
        pygame.draw.rect(dst, theme.FELT_EDGE, card, 1, border_radius=10)
        badge = pygame.Rect(card.left + 9, card.top + 9, 24, 24)
        pygame.draw.circle(dst, theme.AMBER_DARK, badge.center, 12)
        theme.text(dst, str(seat + 1), badge.center, 12, theme.TEXT, "center")

        if seat == 0:
            name = "你"
            meta = "人类牌手 · 固定座位"
        elif seat - 1 < len(self.lineup):
            pick = self.lineup[seat - 1]
            persona = self.persona_by_id[pick.persona_id]
            name = persona.display_name
            meta = self.style_by_key[pick.style_key].label
            self.remove_buttons[seat - 1].draw(dst)
        else:
            theme.text(dst, "＋ 空席", (card.left + 42, card.top + 20), 16, theme.TEXT_DIM)
            theme.text(
                dst,
                "从下方选将池加入牌手",
                (card.left + 42, card.top + 50),
                12,
                theme.TEXT_DIM,
            )
            return

        theme.text(dst, name, (card.left + 42, card.top + 14), 16, theme.TEXT)
        theme.text(dst, meta, (card.left + 42, card.top + 48), 12, theme.TEAL)
        field = self.fields[seat]
        theme.text(
            dst,
            "买入",
            (field.rect.left - 5, field.rect.centery),
            11,
            theme.TEXT_DIM,
            "midright",
        )
        field.draw(dst)
        theme.text(
            dst,
            "BB",
            (field.rect.right + 4, field.rect.centery),
            11,
            theme.GOLD,
            "midleft",
        )

    def _draw_candidate_editor(self, dst: pygame.Surface) -> None:
        persona = self.persona_by_id[self.selected_persona_id]
        preset: StylePreset = self.style_by_key[self.selected_style_key]
        theme.text(dst, "候选详情 / 选择打法", (1012, 194), 17, theme.AMBER_LIGHT)
        self.preview_bust.draw(dst, (1062, 276), self._t, IDLE)
        theme.text(dst, persona.display_name, (1135, 218), 20, theme.GOLD)
        level = {"fish": "入门", "reg": "常客", "shark": "高手"}.get(
            persona.level,
            persona.level,
        )
        theme.text(
            dst,
            f"{persona.species} · {level} AI",
            (1135, 248),
            13,
            theme.TEAL,
        )
        story = persona.backstory
        theme.text(dst, story[:28], (1135, 274), 12, theme.TEXT_DIM)
        theme.text(dst, story[28:56], (1135, 296), 12, theme.TEXT_DIM)
        theme.text(dst, "打法", (1012, 327), 14, theme.TEXT_DIM)
        for style, button in zip(self.styles, self.style_buttons):
            button.selected = style.key == self.selected_style_key
            button.draw(dst)

        desc = pygame.Rect(1012, 431, 298, 66)
        pygame.draw.rect(dst, (30, 21, 14), desc, border_radius=8)
        pygame.draw.rect(dst, theme.FELT_EDGE, desc, 1, border_radius=8)
        theme.text(dst, preset.description[:30], (1024, 442), 11, theme.TEXT_DIM)
        theme.text(
            dst,
            f"入池 {preset.style.vpip:.0%} · PFR {preset.style.pfr:.0%}"
            f" · 激进度 {preset.style.aggression:.1f}",
            (1024, 472),
            12,
            theme.TEAL,
        )
        self.btn_add.draw(dst)

    def _draw_pool(self, dst: pygame.Surface) -> None:
        occupied = {pick.persona_id for pick in self.lineup}
        theme.text(
            dst,
            f"动物选将池  {len(self.personas)} 名",
            (72, 536),
            17,
            theme.AMBER_LIGHT,
        )
        theme.text(
            dst,
            "点击角色 → 右侧挑选打法 → 加入阵容；已入桌身份不可重复",
            (1528, 536),
            13,
            theme.TEXT_DIM,
            "topright",
        )
        for persona, button in zip(self.personas, self.pool_buttons):
            chosen = persona.persona_id in occupied
            button.selected = (
                persona.persona_id == self.selected_persona_id or chosen
            )
            button.draw(dst)
            thumb = self.pool_thumbnails[persona.persona_id]
            if chosen:
                thumb = thumb.copy()
                thumb.set_alpha(105)
            dst.blit(thumb, (button.rect.left + 5, button.rect.top + 1))
            theme.text(
                dst,
                persona.display_name,
                (button.rect.left + 72, button.rect.top + 10),
                14,
                theme.TEXT if not chosen else theme.TEXT_DIM,
            )
            theme.text(
                dst,
                persona.species,
                (button.rect.left + 72, button.rect.top + 34),
                11,
                theme.TEXT_DIM,
            )
            theme.text(
                dst,
                "已入桌" if chosen else "可选择",
                (button.rect.right - 12, button.rect.top + 45),
                11,
                theme.AMBER_LIGHT if chosen else theme.TEAL,
                "topright",
            )
