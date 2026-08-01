"""翻前图表查看器:13×13 范围矩阵,位置/推佊切换。

布局沿用酒馆主题:左侧 13×13 网格(对角线 = 对子,上三角 = 同花,
下三角 = 杂花),单元格琥珀色深浅表示加注(或全下)频率;右侧信息栏
展示悬停手牌的完整混合策略与图例。顶部按钮切换 UTG/MP/CO/BTN/SB
与推佊模式(可选 5/10/15bb)。ESC 返回主菜单。
"""
from __future__ import annotations

import pygame

from gto.charts import RANKS, PreflopCharts

from .. import fx, theme
from ..widgets import Button, Panel
from .manager import Scene, SceneManager

POSITIONS = ("UTG", "MP", "CO", "BTN", "SB")
STACK_CHOICES = (5, 10, 15)

GRID_X, GRID_Y = 120, 200
CELL, GAP = 44, 3
INFO_X = 780


class ChartsViewerScene(Scene):
    """翻前 GTO 图表查看器。"""

    def __init__(self, manager: SceneManager | None = None, seed: int | None = None) -> None:
        super().__init__(manager)
        self.seed = seed
        self.charts = PreflopCharts()
        self.position = "BTN"
        self.pf_mode = False
        self.pf_stack = 10
        self._hover: tuple[int, int] | None = None  # (行, 列)

        self._pos_buttons: list[Button] = []
        for i, pos in enumerate(POSITIONS):
            self._pos_buttons.append(
                Button((120 + i * 108, 128, 96, 40), pos, lambda p=pos: self._select_pos(p), size=20)
            )
        self._pos_buttons.append(
            Button((120 + 5 * 108, 128, 108, 40), "推佊 PF", self._select_pf, size=20)
        )
        self._stack_buttons = [
            Button(
                (800 + i * 84, 128, 76, 40),
                f"{bb}bb",
                lambda b=bb: self._select_stack(b),
                size=18,
            )
            for i, bb in enumerate(STACK_CHOICES)
        ]
        self._backdrop = fx.workspace_backdrop((1600, 900), seed=11)

    # ------------------------------------------------------------ 选择器

    def _select_pos(self, pos: str) -> None:
        self.position = pos
        self.pf_mode = False

    def _select_pf(self) -> None:
        self.pf_mode = True

    def _select_stack(self, bb: int) -> None:
        self.pf_stack = bb
        self.pf_mode = True

    # ------------------------------------------------------------ 数据

    def _cell_hand(self, row: int, col: int) -> str:
        """网格坐标 → 规范牌型键(行=第一张牌,列=第二张)。"""
        r1, r2 = RANKS[row], RANKS[col]
        if row == col:
            return r1 + r2
        return r1 + r2 + "s" if row < col else r2 + r1 + "o"

    def _cell_freq(self, row: int, col: int) -> float:
        """单元格主频率(RFI = 加注频率;推佊 = 全下 0/1)。"""
        hand = self._cell_hand(row, col)
        if self.pf_mode:
            threshold = self.charts.pushfold_tables["shove"][hand]
            return 1.0 if self.pf_stack <= threshold else 0.0
        return self.charts.rfi[self.position][hand].get("raise", 0.0)

    def _grid_rect(self, row: int, col: int) -> pygame.Rect:
        return pygame.Rect(
            GRID_X + col * (CELL + GAP), GRID_Y + row * (CELL + GAP), CELL, CELL
        )

    # ------------------------------------------------------------ 事件

    def handle_event(self, ev: pygame.event.Event) -> None:
        if ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE:
            self._back()
            return
        for b in (*self._pos_buttons, *self._stack_buttons):
            b.handle_event(ev)
        if ev.type == pygame.MOUSEMOTION:
            self._hover = None
            for r in range(13):
                for c in range(13):
                    if self._grid_rect(r, c).collidepoint(ev.pos):
                        self._hover = (r, c)

    def _back(self) -> None:
        if self.manager is not None:
            from .menu import MenuScene

            self.manager.replace(MenuScene(seed=self.seed))

    # ------------------------------------------------------------ 绘制

    def update(self, dt: float) -> None:
        pass

    def draw(self, dst: pygame.Surface) -> None:
        dst.fill(theme.BG)
        dst.blit(self._backdrop, (0, 0))
        theme.text(dst, "翻前 GTO 图表", (120, 40), 44, theme.AMBER_LIGHT, shadow=True)
        theme.text(
            dst, "数据源:hellomate2/gto-poker-overlay(MIT)· 详见 gto/charts/SOURCE.md",
            (124, 96), 15, theme.TEXT_DIM,
        )
        pygame.draw.line(dst, theme.AMBER_DARK, (120, 112), (1538, 112), 1)
        Panel.draw(dst, (88, 180, 650, 642), alpha=155, border=theme.AMBER_DARK)
        for i, b in enumerate(self._pos_buttons):
            b.selected = (
                self.pf_mode if i == len(self._pos_buttons) - 1
                else not self.pf_mode and POSITIONS[i] == self.position
            )
            b.draw(dst)
        for b, bb in zip(self._stack_buttons, STACK_CHOICES):
            b.selected = self.pf_mode and self.pf_stack == bb
            b.draw(dst)
        self._draw_grid(dst)
        self._draw_info(dst)

    def _draw_grid(self, dst: pygame.Surface) -> None:
        # 行列首的牌力标签(A..2)
        for i, r in enumerate(RANKS):
            theme.text(
                dst, r, (GRID_X - 16, GRID_Y + i * (CELL + GAP) + CELL // 2),
                15, theme.TEXT_DIM, "midright",
            )
            theme.text(
                dst, r, (GRID_X + i * (CELL + GAP) + CELL // 2, GRID_Y - 10),
                15, theme.TEXT_DIM, "midbottom",
            )
        for row in range(13):
            for col in range(13):
                rect = self._grid_rect(row, col)
                freq = self._cell_freq(row, col)
                color = tuple(
                    round(a + (b - a) * freq) for a, b in zip(theme.BG_PANEL, theme.AMBER)
                )
                pygame.draw.rect(dst, color, rect, border_radius=5)
                edge = theme.AMBER_LIGHT if (row, col) == self._hover else theme.FELT_EDGE
                pygame.draw.rect(dst, edge, rect, 1, border_radius=5)
                hand = self._cell_hand(row, col)
                tcolor = theme.BG if freq > 0.55 else theme.TEXT
                theme.text(dst, hand, rect.center, 14, tcolor, "center")

    def _draw_info(self, dst: pygame.Surface) -> None:
        Panel.draw(dst, (INFO_X, GRID_Y, 1600 - INFO_X - 60, 430), alpha=215, border=theme.AMBER_DARK)
        x, y = INFO_X + 24, GRID_Y + 20
        title = f"推佊表 · {self.pf_stack}bb(SB/BTN 全下)" if self.pf_mode else f"{self.position} 率先加注(RFI)"
        theme.text(dst, title, (x, y), 24, theme.AMBER_LIGHT)
        y += 44
        if self._hover is not None:
            row, col = self._hover
            hand = self._cell_hand(row, col)
            kind = "对子" if row == col else "同花" if row < col else "杂花"
            theme.text(dst, f"{hand} · {kind}", (x, y), 30, theme.TEXT)
            y += 46
            if self.pf_mode:
                threshold = self.charts.pushfold_tables["shove"][hand]
                desc = "任意深度全下" if threshold >= 999 else f"≤{threshold}bb 全下,否则弃牌"
                theme.text(dst, desc, (x, y), 18, theme.GOLD)
                y += 32
            else:
                cell = self.charts.rfi[self.position][hand]
                for action, label, color in (
                    ("raise", "加注", theme.AMBER),
                    ("call", "跟注", theme.TEAL),
                    ("fold", "弃牌", theme.TEXT_DIM),
                ):
                    freq = cell.get(action, 0.0)
                    theme.text(dst, f"{label} {freq * 100:.0f}%", (x, y), 18, color)
                    y += 30
        else:
            theme.text(dst, "悬停网格查看手牌详情", (x, y), 17, theme.TEXT_DIM)
            y += 34
        # 图例
        y = GRID_Y + 300
        theme.text(dst, "颜色 = 加注(全下)频率", (x, y), 15, theme.TEXT_DIM)
        for i in range(11):
            f = i / 10
            color = tuple(round(a + (b - a) * f) for a, b in zip(theme.BG_PANEL, theme.AMBER))
            pygame.draw.rect(dst, color, (x + i * 22, y + 24, 20, 12))
        theme.text(dst, "0%", (x, y + 40), 13, theme.TEXT_DIM)
        theme.text(dst, "100%", (x + 10 * 22 - 12, y + 40), 13, theme.TEXT_DIM)
        theme.text(dst, "对角线 = 对子 · 上三角 = 同花 · 下三角 = 杂花", (x, y + 70), 15, theme.TEXT_DIM)
        theme.text(dst, "ESC 返回主菜单", (x, y + 96), 15, theme.TEXT_DIM)
