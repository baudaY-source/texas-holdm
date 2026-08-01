"""手牌回顾场景:左侧历史列表(最新在前,懒分页),右侧单手回放。

回放视图:公共牌随光标所在街道逐步发出,hero 与对手底牌全亮
(数据源是历史记录的全知视角),动作列表逐条标注 —— hero 的动作由
``training.review.HandReview`` 给出 √(建议频率≥0.6)/ ~(0.2-0.6)/
×(<0.2)与一句话点评。← → 按钮或方向键逐步,滚轮翻列表,
列表底部自动加载更早的分页(每页 200 手)。ESC 返回主菜单。
"""
from __future__ import annotations

import pygame

from training.review import (
    DEFAULT_HISTORY_PATH,
    HandIndex,
    HandReview,
    action_cn,
)

from .. import cards, fx, theme
from ..widgets import Button, Panel
from .manager import Scene, SceneManager

ROW_H = 58
LIST_RECT = pygame.Rect(24, 110, 470, 740)
_STREET_BOARD_LEN = {"PREFLOP": 0, "FLOP": 3, "TURN": 4, "RIVER": 5}
_STREET_CN = {"PREFLOP": "翻前", "FLOP": "翻牌", "TURN": "转牌", "RIVER": "河牌"}
_MARK_COLOR = {"√": theme.TEAL, "~": theme.AMBER_LIGHT, "×": theme.DANGER}


class ReviewScene(Scene):
    """手牌历史回顾。"""

    def __init__(
        self,
        manager: SceneManager | None = None,
        seed: int | None = None,
        history_path: str | None = None,
        hero_name: str = "你",
    ) -> None:
        super().__init__(manager)
        self.seed = seed
        self.hero_name = hero_name
        self._history_path = history_path or str(DEFAULT_HISTORY_PATH)
        self.index = HandIndex(self._history_path)
        self.rows: list[tuple[int, HandReview]] = []  # (序号, 回放),最新在前
        self._pages_loaded = 0
        self.scroll = 0  # 列表滚动(行)
        self.selected = 0  # rows 下标
        self.cursor = 0  # 动作下标
        self.btn_back = Button((24, 24, 130, 40), "← 返回", self._back, size=18)
        self.btn_prev = Button((560, 812, 120, 44), "← 上一步", self._step_prev, size=18)
        self.btn_next = Button((696, 812, 120, 44), "下一步 →", self._step_next, size=18)
        self._backdrop = fx.workspace_backdrop((1600, 900), seed=31)
        self._load_page(0)
        if self.rows:
            self._select(0)

    # ------------------------------------------------------------ 数据

    def _load_page(self, page: int) -> None:
        for i, rec in self.index.newest_page(page):
            self.rows.append((i, HandReview(rec, hero_name=self.hero_name)))
        self._pages_loaded = max(self._pages_loaded, page + 1)

    def _maybe_extend(self) -> None:
        """滚动接近已加载末尾时加载更早一页。"""
        if self.scroll + self._visible_rows() + 20 >= len(self.rows):
            more = self._pages_loaded * 200 < len(self.index)
            if more:
                self._load_page(self._pages_loaded)

    @property
    def current(self) -> HandReview | None:
        if not self.rows:
            return None
        return self.rows[self.selected][1]

    def _select(self, row: int) -> None:
        self.selected = max(0, min(len(self.rows) - 1, row))
        review = self.current
        n = len(review.annotations()) if review else 0
        self.cursor = 0 if n == 0 else n - 1  # 默认停在最末动作(完整牌面)

    # ------------------------------------------------------------ 事件

    def _back(self) -> None:
        if self.manager is not None:
            from .menu import MenuScene

            self.manager.replace(MenuScene(seed=self.seed))

    def _step_prev(self) -> None:
        self.cursor = max(0, self.cursor - 1)

    def _step_next(self) -> None:
        review = self.current
        n = len(review.annotations()) if review else 0
        self.cursor = min(max(0, n - 1), self.cursor + 1)

    def _visible_rows(self) -> int:
        return LIST_RECT.height // ROW_H

    def handle_event(self, ev: pygame.event.Event) -> None:
        if ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE:
            self._back()
            return
        self.btn_back.handle_event(ev)
        if not self.rows:
            return
        self.btn_prev.handle_event(ev)
        self.btn_next.handle_event(ev)
        if ev.type == pygame.MOUSEBUTTONDOWN:
            if ev.button == 4 and LIST_RECT.collidepoint(ev.pos):
                self.scroll = max(0, self.scroll - 3)
            elif ev.button == 5 and LIST_RECT.collidepoint(ev.pos):
                self.scroll = min(max(0, len(self.rows) - self._visible_rows()), self.scroll + 3)
                self._maybe_extend()
            elif ev.button == 1 and LIST_RECT.collidepoint(ev.pos):
                row = self.scroll + (ev.pos[1] - LIST_RECT.top) // ROW_H
                if 0 <= row < len(self.rows):
                    self._select(row)
        elif ev.type == pygame.KEYDOWN:
            if ev.key == pygame.K_LEFT:
                self._step_prev()
            elif ev.key == pygame.K_RIGHT:
                self._step_next()
            elif ev.key == pygame.K_UP:
                self._select(self.selected - 1)
                if self.selected < self.scroll:
                    self.scroll = self.selected
            elif ev.key == pygame.K_DOWN:
                self._select(self.selected + 1)
                if self.selected >= self.scroll + self._visible_rows():
                    self.scroll = self.selected - self._visible_rows() + 1
                self._maybe_extend()

    def update(self, dt: float) -> None:
        pass

    # ------------------------------------------------------------ 绘制

    def draw(self, dst: pygame.Surface) -> None:
        dst.fill(theme.BG)
        dst.blit(self._backdrop, (0, 0))
        self.btn_back.draw(dst)
        theme.text(dst, "手牌回顾", (180, 28), 34, theme.AMBER_LIGHT, shadow=True)
        theme.text(dst, f"{self._history_path}", (1534, 44), 14, theme.TEXT_DIM, "topright")
        pygame.draw.line(dst, theme.AMBER_DARK, (180, 76), (1538, 76), 1)
        if not self.rows:
            Panel.draw(dst, (24, 110, 1510, 740), alpha=205, border=theme.AMBER_DARK)
            theme.text(dst, "还没有手牌记录", (800, 420), 36, theme.TEXT_DIM, "center")
            theme.text(
                dst, "去「开局」打几手,或运行 python -m engine.simulate 生成模拟数据",
                (800, 480), 18, theme.TEXT_DIM, "center",
            )
        else:
            self._draw_list(dst)
            self._draw_detail(dst)

    # ------------------------------------------------------------ 左:列表

    def _draw_list(self, dst: pygame.Surface) -> None:
        Panel.draw(dst, LIST_RECT, alpha=215, border=theme.AMBER_DARK)
        clip = dst.get_clip()
        dst.set_clip(LIST_RECT.inflate(-8, -8))
        y = LIST_RECT.top + 8 - 0
        visible = self._visible_rows()
        for row in range(self.scroll, min(len(self.rows), self.scroll + visible + 1)):
            _, review = self.rows[row]
            rect = pygame.Rect(LIST_RECT.left + 8, y, LIST_RECT.width - 16, ROW_H - 6)
            if row == self.selected:
                pygame.draw.rect(dst, (66, 46, 26), rect, border_radius=8)
                pygame.draw.rect(dst, theme.AMBER, rect, 1, border_radius=8)
            delta = review.hero_delta()
            d_color = theme.TEAL if delta > 0 else theme.DANGER if delta < 0 else theme.TEXT_DIM
            sign = f"+{delta}" if delta > 0 else str(delta)
            ts = review.timestamp[5:16].replace("T", " ")
            theme.text(dst, f"#{review.hand_id} {ts}", (rect.left + 10, rect.top + 6), 15, theme.TEXT)
            theme.text(dst, sign, (rect.right - 10, rect.top + 6), 16, d_color, "topright")
            board = " ".join(review.board) if review.board else "(翻前结束)"
            theme.text(dst, board, (rect.left + 10, rect.top + 30), 14, theme.TEXT_DIM)
            y += ROW_H
        dst.set_clip(clip)
        total = len(self.index)
        theme.text(
            dst, f"共 {total} 手 · 滚轮翻页(每 200 手自动加载)",
            (LIST_RECT.left + 6, LIST_RECT.bottom + 8), 14, theme.TEXT_DIM,
        )

    # ------------------------------------------------------------ 右:回放

    def _draw_detail(self, dst: pygame.Surface) -> None:
        review = self.current
        if review is None:
            return
        anns = review.annotations()
        cur = anns[self.cursor] if anns else None
        area = pygame.Rect(520, 110, 1040, 740)
        Panel.draw(dst, area, alpha=215, border=theme.AMBER_DARK)
        x = area.left + 28
        # 头部
        theme.text(dst, f"第 {review.hand_id} 手", (x, area.top + 18), 26, theme.AMBER_LIGHT)
        street_txt = _STREET_CN.get(cur.street, "") if cur else "—"
        theme.text(dst, street_txt, (area.right - 28, area.top + 26), 18, theme.TEAL, "topright")
        # 公共牌(随光标所在街道)
        n_board = _STREET_BOARD_LEN.get(cur.street, 5) if cur else 5
        board = review.board[: max(n_board, 0)]
        by = area.top + 64
        if board:
            for i, code in enumerate(board):
                surf = cards.card_surface(code, cards.SIZE_BOARD)
                dst.blit(surf, (x + i * (cards.SIZE_BOARD[0] + 10), by))
        else:
            theme.text(dst, "(尚未发出公共牌)", (x, by + 24), 16, theme.TEXT_DIM)
        # 玩家行:底牌全亮 + 位置 + 盈亏
        py = by + cards.SIZE_BOARD[1] + 22
        end_stacks = review.record.get("end_stacks", {})
        for s in review.seats():
            for i, code in enumerate(s.hole_cards):
                size = cards.SIZE_HOLE if s.seat == review.hero_seat else cards.SIZE_MINI
                if code != "?":
                    surf = cards.card_surface(code, size)
                    dst.blit(surf, (x + i * (size[0] + 6), py))
                else:
                    r = pygame.Rect(x + i * (size[0] + 6), py, *cards.SIZE_MINI)
                    pygame.draw.rect(dst, theme.BG, r, border_radius=4)
                    pygame.draw.rect(dst, theme.FELT_EDGE, r, 1, border_radius=4)
            info_x = x + 2 * cards.SIZE_HOLE[0] + 22
            if s.seat == review.hero_seat and s.name != "你":
                name = f"{s.name}(你)"
            else:
                name = s.name
            pos = s.position.name if s.position else "—"
            start = s.starting_stack
            delta = int(end_stacks.get(str(s.seat), start)) - start
            d_color = theme.TEAL if delta > 0 else theme.DANGER if delta < 0 else theme.TEXT_DIM
            sign = f"+{delta}" if delta > 0 else str(delta)
            theme.text(dst, f"{name} · {pos}", (info_x, py + 4), 16, theme.TEXT)
            theme.text(dst, sign, (info_x + 220, py + 4), 16, d_color)
            py += cards.SIZE_HOLE[1] + 12 if s.seat == review.hero_seat else cards.SIZE_MINI[1] + 12
        # 动作列表(光标高亮,标注着色)
        ly = max(py + 6, area.top + 380)
        theme.text(dst, "行动记录(← → 逐步)", (x, ly), 16, theme.TEXT_DIM)
        ly += 26
        list_h = area.bottom - 90 - ly
        line_h = 24
        max_lines = max(1, list_h // line_h)
        top = max(0, min(self.cursor - max_lines + 2, len(anns) - max_lines))
        clip = dst.get_clip()
        dst.set_clip(pygame.Rect(x - 4, ly, area.width - 48, list_h))
        for a in anns[top : top + max_lines]:
            row_rect = pygame.Rect(x - 4, ly - 2, area.width - 48, line_h)
            if a.index == self.cursor:
                pygame.draw.rect(dst, (66, 46, 26), row_rect, border_radius=6)
            label = action_cn(a.action)
            amt = f" {a.amount}" if a.amount and a.action not in ("FOLD", "CHECK") else ""
            color = theme.TEXT if a.is_hero else theme.TEXT_DIM
            theme.text(
                dst, f"{_STREET_CN.get(a.street, a.street)} {a.name} {label}{amt}",
                (x, ly), 15, color,
            )
            if a.mark:
                theme.text(
                    dst, a.mark, (area.right - 64, ly), 16,
                    _MARK_COLOR.get(a.mark, theme.TEXT_DIM), "topright",
                )
            ly += line_h
        dst.set_clip(clip)
        # 当前动作点评
        if cur is not None and cur.note:
            theme.text(dst, cur.note, (x, area.bottom - 78), 15, theme.GOLD)
        delta = review.hero_delta()
        d_color = theme.TEAL if delta > 0 else theme.DANGER if delta < 0 else theme.TEXT_DIM
        sign = f"+{delta}" if delta > 0 else str(delta)
        theme.text(
            dst, f"底池 {review.total_pot()} · 本手盈亏 {sign}",
            (area.right - 28, area.bottom - 78), 16, d_color, "topright",
        )
        self.btn_prev.enabled = self.cursor > 0
        self.btn_next.enabled = self.cursor < max(0, len(anns) - 1)
        self.btn_prev.draw(dst)
        self.btn_next.draw(dst)
