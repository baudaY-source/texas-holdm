"""数据统计场景:人类玩家累计战绩一览。

数据源 ``hands/history.jsonl``(经 ``training.review.aggregate_stats``
聚合):总手数、净盈亏、bb/100、VPIP/PFR/AF,分位置盈亏条形,
以及累计盈亏走势(pygame 折线,零轴参考线)。无数据时给出引导占位。
ESC 返回主菜单。
"""
from __future__ import annotations

from pathlib import Path

import pygame

from training.review import DEFAULT_HISTORY_PATH, HandIndex, aggregate_stats

from .. import fx, theme
from ..widgets import Button, Panel
from .manager import Scene, SceneManager

_POSITION_ORDER = (
    "BTN",
    "SB",
    "BB",
    "UTG",
    "UTG1",
    "UTG2",
    "LJ",
    "MP",
    "HJ",
    "CO",
)
_BASE_POSITIONS = ("BTN", "SB", "BB", "UTG", "MP", "CO")


class StatsScene(Scene):
    """战绩统计。"""

    def __init__(
        self,
        manager: SceneManager | None = None,
        seed: int | None = None,
        history_path: str | None = None,
        hero_name: str = "你",
    ) -> None:
        super().__init__(manager)
        self.seed = seed
        self._history_path = history_path or str(DEFAULT_HISTORY_PATH)
        self.hero_name = hero_name
        self._confirm_clear = False
        self._clear_error = ""
        self._notice = ""
        self.btn_back = Button((24, 24, 130, 40), "← 返回", self._back, size=18)
        self.btn_clear = Button(
            (1374, 24, 160, 40),
            "清除当前记录",
            self._request_clear,
            size=17,
            danger=True,
            enabled=False,
        )
        self.btn_cancel_clear = Button(
            (625, 500, 160, 48), "取消", self._cancel_clear, size=19
        )
        self.btn_confirm_clear = Button(
            (815, 500, 160, 48),
            "确认清除",
            self._clear_records,
            size=18,
            danger=True,
        )
        self._backdrop = fx.workspace_backdrop((1600, 900), seed=47)
        self._reload_stats()

    def _reload_stats(self) -> None:
        """重新读取历史文件，让清除后的统计在当前页面立即归零。"""
        index = HandIndex(self._history_path)
        self.stats = aggregate_stats(
            (rec for _, rec in index.iter_all()), hero_name=self.hero_name
        )
        self.total = len(index)
        path = Path(self._history_path)
        self._stored_bytes = path.stat().st_size if path.is_file() else 0
        self.btn_clear.enabled = self._stored_bytes > 0

    def _request_clear(self) -> None:
        """首次点击只打开确认框，不触碰磁盘。"""
        if self._stored_bytes > 0:
            self._confirm_clear = True
            self._clear_error = ""
            self._notice = ""

    def _cancel_clear(self) -> None:
        self._confirm_clear = False
        self._clear_error = ""

    def _clear_records(self) -> None:
        """确认后删除整份 JSONL；下一手牌会自动创建新文件。"""
        old_total = self.total
        try:
            Path(self._history_path).unlink(missing_ok=True)
        except OSError:
            self._clear_error = "清除失败：文件可能被占用或目录无写入权限"
            return
        self._confirm_clear = False
        self._clear_error = ""
        self._reload_stats()
        self._notice = f"已清除 {old_total} 手牌局记录"

    def _back(self) -> None:
        if self.manager is not None:
            from .menu import MenuScene

            self.manager.replace(MenuScene(seed=self.seed))

    def handle_event(self, ev: pygame.event.Event) -> None:
        if self._confirm_clear:
            if ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE:
                self._cancel_clear()
                return
            self.btn_cancel_clear.handle_event(ev)
            self.btn_confirm_clear.handle_event(ev)
            return
        if ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE:
            self._back()
            return
        self.btn_back.handle_event(ev)
        self.btn_clear.handle_event(ev)

    def update(self, dt: float) -> None:
        pass

    # ------------------------------------------------------------ 绘制

    def draw(self, dst: pygame.Surface) -> None:
        dst.fill(theme.BG)
        dst.blit(self._backdrop, (0, 0))
        self.btn_back.draw(dst)
        self.btn_clear.draw(dst)
        theme.text(dst, "数据统计", (180, 28), 34, theme.AMBER_LIGHT, shadow=True)
        theme.text(dst, f"历史文件共 {self.total} 手(统计仅含你参与的)", (320, 44), 14, theme.TEXT_DIM)
        pygame.draw.line(dst, theme.AMBER_DARK, (180, 76), (1538, 76), 1)
        if self._notice:
            color = theme.DANGER if self._notice.startswith("清除失败") else theme.TEAL
            theme.text(dst, self._notice, (1534, 82), 14, color, "topright")
        if self.stats.hands == 0:
            Panel.draw(dst, (24, 110, 1510, 740), alpha=205, border=theme.AMBER_DARK)
            theme.text(dst, "还没有你的战绩", (800, 420), 36, theme.TEXT_DIM, "center")
            theme.text(dst, "去「开局」打几手再回来看看吧", (800, 480), 18, theme.TEXT_DIM, "center")
        else:
            self._draw_cards(dst)
            self._draw_positions(dst)
            self._draw_sparkline(dst)
        if self._confirm_clear:
            self._draw_clear_confirmation(dst)

    def _draw_clear_confirmation(self, dst: pygame.Surface) -> None:
        veil = pygame.Surface(dst.get_size(), pygame.SRCALPHA)
        veil.fill((7, 5, 3, 190))
        dst.blit(veil, (0, 0))
        panel = pygame.Rect(430, 270, 740, 330)
        Panel.draw(dst, panel, alpha=244, border=theme.DANGER)
        theme.text(
            dst,
            "清除本程序当前牌局记录？",
            (panel.centerx, panel.top + 58),
            30,
            theme.DANGER,
            "center",
            shadow=True,
        )
        theme.text(
            dst,
            f"将删除当前运行版本保存的 {self.total} 手牌局、行动回放及配额/移出事件。",
            (panel.centerx, panel.top + 125),
            18,
            theme.TEXT,
            "center",
        )
        theme.text(
            dst,
            "累计统计会立即归零；训练档案、模型和策略库不会受影响。",
            (panel.centerx, panel.top + 165),
            16,
            theme.TEXT_DIM,
            "center",
        )
        warning = self._clear_error or "当前记录删除后无法在程序内恢复"
        theme.text(
            dst,
            warning,
            (panel.centerx, panel.top + 204),
            16,
            theme.DANGER if self._clear_error else theme.AMBER_LIGHT,
            "center",
        )
        self.btn_cancel_clear.draw(dst)
        self.btn_confirm_clear.draw(dst)

    def _draw_cards(self, dst: pygame.Surface) -> None:
        st = self.stats
        profit_color = theme.TEAL if st.profit > 0 else theme.DANGER if st.profit < 0 else theme.TEXT
        bb_color = theme.TEAL if st.bb100 > 0 else theme.DANGER if st.bb100 < 0 else theme.TEXT
        cards_spec = (
            ("总手数", f"{st.hands}", theme.TEXT),
            ("净盈亏(筹码)", f"{st.profit:+d}", profit_color),
            ("bb/100", f"{st.bb100:+.1f}", bb_color),
            ("VPIP 入池率", f"{st.vpip:.1f}%", theme.TEXT),
            ("PFR 翻前加注率", f"{st.pfr:.1f}%", theme.TEXT),
            ("AF 进攻频率", f"{st.af:.2f}", theme.TEXT),
        )
        for i, (label, value, color) in enumerate(cards_spec):
            col, row = i % 2, i // 2
            rect = pygame.Rect(24 + col * 240, 110 + row * 150, 220, 130)
            Panel.draw(dst, rect, alpha=215, border=theme.AMBER_DARK)
            theme.text(dst, label, (rect.centerx, rect.top + 24), 15, theme.TEXT_DIM, "center")
            theme.text(dst, value, (rect.centerx, rect.top + 66), 34, color, "center", shadow=True)

    def _draw_positions(self, dst: pygame.Surface) -> None:
        rect = pygame.Rect(520, 110, 1040, 300)
        Panel.draw(dst, rect, alpha=215, border=theme.AMBER_DARK)
        x, y = rect.left + 28, rect.top + 20
        theme.text(dst, "分位置盈亏", (x, y), 20, theme.AMBER_LIGHT)
        y += 44
        positions = [p for p in _POSITION_ORDER if p in self.stats.per_position]
        if not positions:
            positions = list(_BASE_POSITIONS)
        values = [self.stats.per_position.get(p, 0) for p in positions]
        scale = max(1, max(abs(v) for v in values))
        zero_x = x + 180
        bar_w = rect.width - 320
        row_height = min(40, max(22, (rect.bottom - y - 12) // len(positions)))
        font_size = 17 if row_height >= 32 else 14
        for pos, v in zip(positions, values):
            theme.text(dst, pos, (x, y), font_size, theme.TEXT)
            color = theme.TEAL if v > 0 else theme.DANGER if v < 0 else theme.TEXT_DIM
            w = round(bar_w / 2 * abs(v) / scale)
            if v > 0:
                bar = pygame.Rect(zero_x + bar_w // 2, y + 3, w, 16)
            else:
                bar = pygame.Rect(zero_x + bar_w // 2 - w, y + 3, w, 16)
            if w > 0:
                pygame.draw.rect(dst, color, bar, border_radius=4)
            sign = f"+{v}" if v > 0 else str(v)
            theme.text(dst, sign, (rect.right - 28, y), font_size, color, "topright")
            y += row_height
        # 零轴
        pygame.draw.line(
            dst, theme.FELT_EDGE,
            (zero_x + bar_w // 2, rect.top + 58), (zero_x + bar_w // 2, y - 16), 1,
        )

    def _draw_sparkline(self, dst: pygame.Surface) -> None:
        rect = pygame.Rect(520, 440, 1040, 410)
        Panel.draw(dst, rect, alpha=215, border=theme.AMBER_DARK)
        theme.text(dst, "累计盈亏走势", (rect.left + 28, rect.top + 20), 20, theme.AMBER_LIGHT)
        curve = self.stats.profit_curve
        plot = pygame.Rect(rect.left + 40, rect.top + 60, rect.width - 80, rect.height - 110)
        pygame.draw.rect(dst, theme.BG, plot)
        pygame.draw.rect(dst, theme.FELT_EDGE, plot, 1)
        if len(curve) < 2:
            theme.text(dst, "数据太少,画不出曲线", plot.center, 16, theme.TEXT_DIM, "center")
            return
        lo, hi = min(curve + [0]), max(curve + [0])
        span = max(1, hi - lo)
        # 零轴
        zero_y = plot.bottom - (0 - lo) / span * plot.height
        pygame.draw.line(dst, theme.FELT_EDGE, (plot.left, zero_y), (plot.right, zero_y), 1)
        pts = []
        for i, v in enumerate(curve):
            px = plot.left + i / (len(curve) - 1) * plot.width
            py = plot.bottom - (v - lo) / span * plot.height
            pts.append((px, py))
        final_color = theme.TEAL if curve[-1] >= 0 else theme.DANGER
        pygame.draw.lines(dst, final_color, False, pts, 2)
        # 首尾标注
        theme.text(dst, f"{curve[0]:+d}", (plot.left + 4, plot.top + 4), 13, theme.TEXT_DIM)
        theme.text(
            dst, f"{curve[-1]:+d}", (plot.right - 6, pts[-1][1] - 8), 15,
            final_color, "bottomright",
        )
        theme.text(dst, f"最高 {hi:+d} · 最低 {lo:+d}", (plot.right, plot.bottom + 8), 13, theme.TEXT_DIM, "topright")
