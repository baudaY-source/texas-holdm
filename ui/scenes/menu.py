"""主菜单:离线单机完整保留，并展示独立的朋友联机 Alpha 入口。"""
from __future__ import annotations

import pygame

from .. import fx, theme
from ..widgets import Button, Panel
from .manager import Scene, SceneManager


class MenuScene(Scene):
    """标题画面：单机模式、朋友联机及各分析/训练工具。"""

    def __init__(
        self,
        manager: SceneManager | None = None,
        seed: int | None = None,
        toast: str | None = None,
    ) -> None:
        super().__init__(manager)
        self.seed = seed
        self.toast = toast  # 入场后短暂展示的提示(如「牌桌已散场」)
        self._toast_t = 0.0
        cx = 800
        entries = (
            ("单机开局", self._start_game, 23, False),
            ("朋友联机 Alpha", self._start_friends, 21, False),
            ("牌手训练 Drills", self._start_drills, 22, False),
            ("手牌回顾", self._start_review, 22, False),
            ("数据统计", self._start_stats, 22, False),
            ("翻前图表", self._start_charts, 22, False),
            ("训练场", self._start_training, 22, False),
            ("退 出", self._quit, 24, True),
        )
        self.buttons = [
            Button((cx - 130, 390 + i * 56, 260, 46), label, cb, size=size, danger=danger)
            for i, (label, cb, size, danger) in enumerate(entries)
        ]
        self.smoke = fx.ParticleSystem((300, 380, 1300, 900), seed=seed, max_particles=40)
        self._glow = fx.radial_glow(1000, (255, 190, 110), 130)
        self._vignette = fx.vignette((1600, 900), 190)
        self._grain = fx.grain_overlay((1600, 900), seed=11)
        self._t = 0.0

    def _start_game(self) -> None:
        if self.manager is not None:
            from .game_setup import GameSetupScene

            self.manager.replace(GameSetupScene(seed=self.seed))

    def _start_friends(self) -> None:
        if self.manager is not None:
            from .friends_room import FriendsInfoScene

            self.manager.replace(FriendsInfoScene(seed=self.seed))

    def _start_drills(self) -> None:
        if self.manager is not None:
            from .drills import DrillsScene

            self.manager.replace(DrillsScene(seed=self.seed))

    def _start_review(self) -> None:
        if self.manager is not None:
            from .review import ReviewScene

            self.manager.replace(ReviewScene(seed=self.seed))

    def _start_stats(self) -> None:
        if self.manager is not None:
            from .stats import StatsScene

            self.manager.replace(StatsScene(seed=self.seed))

    def _start_charts(self) -> None:
        if self.manager is not None:
            from .charts_viewer import ChartsViewerScene

            self.manager.replace(ChartsViewerScene(seed=self.seed))

    def _start_training(self) -> None:
        if self.manager is not None:
            from .training import TrainingScene

            self.manager.replace(TrainingScene(seed=self.seed))

    def _quit(self) -> None:
        if self.manager is not None:
            self.manager.quit()

    def handle_event(self, ev: pygame.event.Event) -> None:
        for b in self.buttons:
            b.handle_event(ev)
        if ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE:
            self._quit()

    def update(self, dt: float) -> None:
        self._t += dt
        self._toast_t += dt
        self.smoke.update(dt)

    def draw(self, dst: pygame.Surface) -> None:
        dst.fill(theme.BG)
        # 招牌后的暖光(带轻微呼吸)
        breathe = 1.0 + 0.03 * fx.Ease.smoothstep((self._t % 4.0) / 4.0)
        glow = pygame.transform.smoothscale(
            self._glow, (int(1000 * breathe), int(1000 * breathe))
        )
        dst.blit(glow, glow.get_rect(center=(800, 430)))
        self.smoke.draw(dst)
        # 氛围层属于背景;正文和控件始终最后绘制,保持清晰。
        dst.blit(self._vignette, (0, 0))
        dst.blit(self._grain, (0, 0))
        Panel.draw(dst, (636, 380, 328, 468), alpha=150, border=theme.AMBER_DARK, radius=18)
        # 标题
        theme.text(dst, "酒馆德州", (800, 240), 96, theme.AMBER_LIGHT, "center", shadow=True)
        theme.text(dst, "TEXAS HOLD'EM TAVERN", (800, 312), 22, theme.TEAL, "center")
        theme.text(dst, "离线单机 · 朋友联机 Alpha", (800, 350), 18, theme.TEXT_DIM, "center")
        if self.toast and self._toast_t < 5.0:
            alpha = 255 if self._toast_t < 3.5 else max(0, int(255 * (5.0 - self._toast_t) / 1.5))
            img = theme.get_font(22).render(self.toast, True, theme.GOLD)
            img.set_alpha(alpha)
            dst.blit(img, img.get_rect(center=(800, 384)))
        for b in self.buttons:
            b.draw(dst)
        theme.text(
            dst,
            "单机牌局完整保留 · 联机模式由专用启动器进入",
            (800, 868),
            15,
            theme.TEXT_DIM,
            "center",
        )
