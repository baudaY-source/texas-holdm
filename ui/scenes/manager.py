"""场景栈管理器:切换/压栈/出栈 + 黑场淡入淡出过渡。"""
from __future__ import annotations

import pygame


class Scene:
    """场景基类:事件、逻辑、绘制三段式。"""

    def __init__(self, manager: "SceneManager | None" = None) -> None:
        self.manager = manager

    def on_enter(self) -> None:
        """进入场景(被压入/换回时)调用。"""

    def handle_event(self, ev: pygame.event.Event) -> None:
        pass

    def update(self, dt: float) -> None:
        pass

    def draw(self, dst: pygame.Surface) -> None:
        pass


class SceneManager:
    """场景栈 + 淡入淡出。``replace`` 触发 0.25s 黑场过渡。"""

    FADE_TIME = 0.25

    def __init__(self, size: tuple[int, int] = (1600, 900)) -> None:
        self.size = size
        self.stack: list[Scene] = []
        self._pending: Scene | None = None
        self._fade_t = -1.0  # <0 表示无过渡
        self._fade_phase = 0  # 0 淡出,1 淡入
        self._fade_surf = pygame.Surface(size)
        self._fade_surf.fill((0, 0, 0))
        self.quit_requested = False

    # ------------------------------------------------------------ 栈操作

    @property
    def current(self) -> Scene | None:
        return self.stack[-1] if self.stack else None

    def replace(self, scene: Scene) -> None:
        """淡出后替换栈顶场景。"""
        self._pending = scene
        self._fade_t = 0.0
        self._fade_phase = 0

    def _swap(self, scene: Scene) -> None:
        if self.stack:
            self.stack.pop()
        self.push(scene)

    def push(self, scene: Scene) -> None:
        scene.manager = self
        self.stack.append(scene)
        scene.on_enter()

    def pop(self) -> None:
        if self.stack:
            self.stack.pop()
        if self.stack:
            self.stack[-1].on_enter()

    def quit(self) -> None:
        self.quit_requested = True

    # ------------------------------------------------------------ 帧驱动

    def handle_event(self, ev: pygame.event.Event) -> None:
        if self._fade_phase == 0 and self._fade_t >= 0:
            return  # 淡出期间吞掉输入
        if self.current is not None:
            self.current.handle_event(ev)

    def update(self, dt: float) -> None:
        if self._fade_t >= 0:
            self._fade_t += dt
            if self._fade_phase == 0 and self._fade_t >= self.FADE_TIME:
                if self._pending is not None:
                    self._swap(self._pending)
                    self._pending = None
                self._fade_phase = 1
                self._fade_t = 0.0
            elif self._fade_phase == 1 and self._fade_t >= self.FADE_TIME:
                self._fade_t = -1.0
        if self.current is not None:
            self.current.update(dt)

    def draw(self, dst: pygame.Surface) -> None:
        if self.current is not None:
            self.current.draw(dst)
        if self._fade_t >= 0:
            frac = min(1.0, self._fade_t / self.FADE_TIME)
            eased = frac * frac * (3.0 - 2.0 * frac)
            alpha = int(255 * (eased if self._fade_phase == 0 else 1 - eased))
            self._fade_surf.set_alpha(alpha)
            dst.blit(self._fade_surf, (0, 0))

    # ------------------------------------------------------------ 主循环

    def run(self) -> int:
        """开窗并运行主循环(真实模式;无头模式不要调用)。

        场景始终绘制到 1600x900 逻辑画布,再整体缩放适配窗口,
        因此窗口可随意拉伸而布局不变。
        """
        pygame.init()
        pygame.display.set_caption("酒馆德州 Texas Hold'em Tavern")
        screen = pygame.display.set_mode(self.size, pygame.RESIZABLE)
        canvas = pygame.Surface(self.size)
        clock = pygame.time.Clock()
        while not self.quit_requested:
            dt = clock.tick(60) / 1000.0
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    self.quit()
                elif ev.type == pygame.VIDEORESIZE:
                    screen = pygame.display.set_mode(
                        (max(960, ev.w), max(600, ev.h)), pygame.RESIZABLE
                    )
                else:
                    self.handle_event(ev)
            self.update(dt)
            canvas.fill((0, 0, 0))
            self.draw(canvas)
            if screen.get_size() != canvas.get_size():
                pygame.transform.smoothscale(canvas, screen.get_size(), screen)
            else:
                screen.blit(canvas, (0, 0))
            pygame.display.flip()
        pygame.quit()
        return 0
