"""Drills 训练场景:类目/难度选择 → 出题 → 即时反馈循环。

布局沿用酒馆主题:顶部标题与页脚统计,中央左侧为题卡(局面描述 +
hero 底牌 + 公共牌),题卡下方为 2-3 个动作按钮;作答后右侧展开
反馈面板(所选高亮、各动作频率条、解析、得分与连对)。数字键 1-3
作答,空格/回车下一题,ESC 逐级返回。
"""
from __future__ import annotations

import pygame

from training.drills import (
    CATEGORIES,
    CATEGORY_LABEL,
    CATEGORY_POSTFLOP,
    CATEGORY_PUSHFOLD,
    CATEGORY_RFI,
    Drill,
    DrillGenerator,
    DrillSession,
    TIERS,
    TIER_ADVANCED,
    TIER_LABEL,
    action_label,
)

from .. import cards, fx, theme
from ..widgets import Button, Panel
from .manager import Scene, SceneManager

# 频率条配色(与牌桌 GTO 面板一致)
_FREQ_COLOR = {
    "FOLD": (150, 84, 72),
    "CHECK": theme.TEAL,
    "CALL": theme.TEAL,
    "BET": theme.AMBER,
    "RAISE": theme.AMBER,
    "ALLIN": theme.DANGER,
}


def wrap_text(s: str, size: int, max_width: int) -> list[str]:
    """按像素宽度折行(CJK 逐字断行,空格优先)。"""
    lines: list[str] = []
    cur = ""
    for ch in s:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        if theme.text_width(cur + ch, size) > max_width and cur:
            lines.append(cur)
            cur = ch.lstrip()
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines


class DrillsScene(Scene):
    """训练场景:选类目/难度,答题,看反馈,累计档案。"""

    def __init__(
        self,
        manager: SceneManager | None = None,
        seed: int | None = None,
        generator: DrillGenerator | None = None,
        profile_path: str | None = None,
    ) -> None:
        super().__init__(manager)
        self.seed = seed
        self.generator = generator or DrillGenerator(seed=seed)
        self.profile_path = profile_path
        self.tier = TIER_ADVANCED
        self.phase = "picker"  # picker / quiz / empty
        self.session: DrillSession | None = None
        self.drill: Drill | None = None
        self.feedback = None  # AnswerRecord | None
        self._qno = 1  # 当前题号(作答后不变)

        cx = 800
        self._tier_buttons = [
            Button((cx - 220 + i * 230, 330, 210, 52), TIER_LABEL[t], lambda t=t: self._set_tier(t), size=24)
            for i, t in enumerate(TIERS)
        ]
        self._cat_buttons: list[Button] = []
        labels = {
            CATEGORY_RFI: "翻前 RFI",
            CATEGORY_PUSHFOLD: "翻前推佊",
            CATEGORY_POSTFLOP: "翻后求解",
        }
        for i, cat in enumerate(CATEGORIES):
            enabled = cat != CATEGORY_POSTFLOP or self.generator.postflop_available()
            label = labels[cat] if enabled else "翻后(库空)"
            self._cat_buttons.append(
                Button(
                    (cx - 330 + i * 340, 440, 300, 84),
                    label,
                    lambda c=cat: self._start(c),
                    size=26,
                    enabled=enabled,
                )
            )
        self._action_buttons: list[Button] = []
        self.btn_next = Button((1210, 770, 220, 56), "下一题 »", self._next, size=24)
        self.btn_back = Button((30, 24, 130, 40), "← 返回", self._back, size=18)

        self._backdrop = fx.workspace_backdrop((1600, 900), seed=23)

    # ------------------------------------------------------------ 流程

    def _set_tier(self, tier: str) -> None:
        self.tier = tier

    def _start(self, category: str) -> None:
        self.session = DrillSession(
            category,
            tier=self.tier,
            profile_path=self.profile_path,
            generator=self.generator,
        )
        self.feedback = None
        self._next()
        if self.drill is None:
            self.phase = "empty"  # 类目不可用(库空)
        else:
            self.phase = "quiz"

    def _next(self) -> None:
        if self.session is None:
            return
        self.drill = self.session.next_drill()
        self.feedback = None
        self._qno = self.session.asked + 1  # 题号在作答后保持不变
        self._action_buttons = []
        if self.drill is not None:
            n = len(self.drill.options)
            w, gap = 200, 24
            x0 = 470 - (n * w + (n - 1) * gap) // 2
            for i, a in enumerate(self.drill.options):
                self._action_buttons.append(
                    Button(
                        (x0 + i * (w + gap), 700, w, 60),
                        f"{action_label(a)} [{i + 1}]",
                        lambda a=a: self._answer_action(a),
                        size=24,
                        danger=(a in ("FOLD", "ALLIN")),
                    )
                )

    def _answer_action(self, action: str) -> None:
        if self.session is None or self.drill is None or self.feedback is not None:
            return
        self.feedback = self.session.answer(self.drill, action)

    def _back(self) -> None:
        if self.phase in ("quiz", "empty"):
            if self.session is not None:
                self.session.finish()
            self.session = None
            self.drill = None
            self.feedback = None
            self.phase = "picker"
            return
        if self.manager is not None:
            from .menu import MenuScene

            self.manager.replace(MenuScene(seed=self.seed))

    # ------------------------------------------------------------ 事件

    def handle_event(self, ev: pygame.event.Event) -> None:
        if ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE:
            self._back()
            return
        self.btn_back.handle_event(ev)
        if self.phase == "picker":
            for b in (*self._tier_buttons, *self._cat_buttons):
                b.handle_event(ev)
        elif self.phase == "quiz":
            if self.feedback is None:
                for b in self._action_buttons:
                    b.handle_event(ev)
                if ev.type == pygame.KEYDOWN and self.drill is not None:
                    idx = {pygame.K_1: 0, pygame.K_2: 1, pygame.K_3: 2}.get(ev.key)
                    if idx is not None and idx < len(self.drill.options):
                        self._answer_action(self.drill.options[idx])
            else:
                self.btn_next.handle_event(ev)
                if ev.type == pygame.KEYDOWN and ev.key in (pygame.K_SPACE, pygame.K_RETURN):
                    self._next()

    def update(self, dt: float) -> None:
        pass

    # ------------------------------------------------------------ 绘制

    def draw(self, dst: pygame.Surface) -> None:
        dst.fill(theme.BG)
        dst.blit(self._backdrop, (0, 0))
        self.btn_back.draw(dst)
        if self.phase == "picker":
            self._draw_picker(dst)
        elif self.phase == "empty":
            theme.text(dst, "翻后题库为空", (800, 380), 40, theme.AMBER_LIGHT, "center", shadow=True)
            theme.text(
                dst,
                "先运行 python -m gto.precompute 生成策略库(见 README)",
                (800, 450), 20, theme.TEXT_DIM, "center",
            )
        else:
            self._draw_quiz(dst)

    def _draw_picker(self, dst: pygame.Surface) -> None:
        theme.text(dst, "牌手训练", (800, 170), 64, theme.AMBER_LIGHT, "center", shadow=True)
        theme.text(dst, "DRILLS · 像职业选手一样练基本功", (800, 236), 18, theme.TEAL, "center")
        theme.text(dst, "难度", (800, 300), 18, theme.TEXT_DIM, "center")
        for b, t in zip(self._tier_buttons, TIERS):
            b.selected = t == self.tier
            b.draw(dst)
        theme.text(dst, "选择类目开始(新手=清晰局 · 进阶=混合局)", (800, 412), 17, theme.TEXT_DIM, "center")
        for b in self._cat_buttons:
            b.draw(dst)
        # 档案速览
        prof = self.generator and self._profile_summary()
        if prof:
            theme.text(dst, prof, (800, 580), 16, theme.TEXT_DIM, "center")
        theme.text(dst, "作答后即时给出 GTO 频率与解析 · 评分:频率≥60% 满分,20-60% 半分", (800, 800), 16, theme.TEXT_DIM, "center")
        theme.text(dst, "ESC 返回主菜单", (800, 830), 15, theme.TEXT_DIM, "center")

    def _profile_summary(self) -> str:
        try:
            from training.drills import DrillProfile, DEFAULT_PROFILE_PATH

            path = self.profile_path or DEFAULT_PROFILE_PATH
            d = DrillProfile(path).data
        except Exception:
            return ""
        if not d.get("answers"):
            return "还没有训练记录,先来一组吧"
        acc = d["total_score"] / d["answers"]
        return (
            f"历史:{d['sessions']} 组训练 · {d['answers']} 题 · "
            f"得分率 {acc:.0%} · 最佳连对 {d['best_streak']}"
        )

    # ------------------------------------------------------------ 答题视图

    def _draw_quiz(self, dst: pygame.Surface) -> None:
        drill = self.drill
        session = self.session
        if drill is None or session is None:
            return
        cat = CATEGORY_LABEL[drill.category]
        theme.text(dst, f"{cat} · {TIER_LABEL[session.tier]}", (800, 36), 30, theme.AMBER_LIGHT, "center", shadow=True)
        theme.text(dst, f"第 {self._qno} 题", (1460, 40), 18, theme.TEXT_DIM, "topright")

        # 题卡
        Panel.draw(dst, (60, 90, 820, 580), alpha=215, border=theme.AMBER_DARK)
        x, y = 92, 120
        for line in wrap_text(drill.prompt, 24, 760):
            theme.text(dst, line, (x, y), 24, theme.TEXT)
            y += 34
        y += 16
        # hero 底牌
        theme.text(dst, "你的手牌", (x, y), 17, theme.TEXT_DIM)
        for i, code in enumerate(drill.hole_cards):
            surf = cards.card_surface(code, cards.SIZE_HOLE)
            dst.blit(surf, (x + i * (cards.SIZE_HOLE[0] + 14), y + 26))
        y += 26 + cards.SIZE_HOLE[1] + 26
        # 公共牌(翻后题)
        board = drill.context.get("board")
        if board:
            theme.text(dst, "公共牌", (x, y), 17, theme.TEXT_DIM)
            for i, code in enumerate(board):
                surf = cards.card_surface(code, cards.SIZE_MINI)
                dst.blit(surf, (x + i * (cards.SIZE_MINI[0] + 10), y + 24))
            y += 24 + cards.SIZE_MINI[1] + 18
        # 上下文行
        ctx_parts = []
        if drill.context.get("position"):
            ctx_parts.append(f"位置 {drill.context['position']}")
        if drill.context.get("stack_bb"):
            ctx_parts.append(f"筹码 {drill.context['stack_bb']:g}bb")
        if drill.context.get("pot"):
            ctx_parts.append(f"底池 {drill.context['pot']:g}")
        if drill.context.get("stack"):
            ctx_parts.append(f"有效筹码 {drill.context['stack']:g}")
        if ctx_parts:
            theme.text(dst, " · ".join(ctx_parts), (x, y), 17, theme.GOLD)
            y += 30

        # 动作按钮(反馈后禁用并高亮所选)
        for b, a in zip(self._action_buttons, drill.options):
            b.enabled = self.feedback is None
            b.selected = self.feedback is not None and a == self.feedback.action
            b.draw(dst)
        if self.feedback is not None:
            chosen = self.feedback.action
            for b, a in zip(self._action_buttons, drill.options):
                if a == chosen:
                    color = theme.TEAL if self.feedback.score > 0 else theme.DANGER
                    pygame.draw.rect(dst, color, b.rect.inflate(10, 10), 4, border_radius=10)
                    freq = drill.correct_freqs.get(a, 0.0)
                    theme.text(dst, f"{freq:.0%}", (b.rect.centerx, b.rect.bottom + 12), 16, color, "midtop")

        self._draw_feedback(dst)
        self._draw_footer(dst)

    def _draw_feedback(self, dst: pygame.Surface) -> None:
        Panel.draw(dst, (920, 90, 620, 760), alpha=215, border=theme.TEAL_DARK)
        x, y = 952, 120
        drill = self.drill
        if drill is None:
            return
        if self.feedback is None:
            theme.text(dst, "选择你的行动…", (x, y), 20, theme.TEXT_DIM)
            theme.text(dst, "数字键 1-3 快捷作答", (x, y + 36), 15, theme.TEXT_DIM)
            return
        rec = self.feedback
        # 得分与判定
        if rec.score >= 1.0:
            verdict, color = "标准打法!+1.0", theme.TEAL
        elif rec.score >= 0.5:
            verdict, color = "可接受的混合 +0.5", theme.AMBER_LIGHT
        else:
            verdict, color = "GTO 偏差 +0", theme.DANGER
        theme.text(dst, verdict, (x, y), 30, color, shadow=True)
        y += 52
        if rec.streak >= 2:
            theme.text(dst, f"连对 ×{rec.streak}", (x, y), 20, theme.GOLD)
            y += 36
        # 正确频率条
        theme.text(dst, "GTO 频率", (x, y), 17, theme.TEXT_DIM)
        y += 28
        for a in drill.options:
            f = drill.correct_freqs.get(a, 0.0)
            theme.text(dst, action_label(a), (x, y), 16, theme.TEXT)
            track = pygame.Rect(x + 90, y + 3, 300, 14)
            pygame.draw.rect(dst, theme.BG, track, border_radius=4)
            fill = pygame.Rect(track)
            fill.width = round(track.width * min(1.0, f))
            if fill.width > 0:
                pygame.draw.rect(dst, _FREQ_COLOR.get(a, theme.AMBER), fill, border_radius=4)
            pygame.draw.rect(dst, theme.FELT_EDGE, track, 1, border_radius=4)
            mark = " ← 你的选择" if a == rec.action else ""
            theme.text(dst, f"{f:.0%}{mark}", (track.right + 10, y), 15, theme.TEXT_DIM)
            y += 32
        y += 12
        # 解析
        theme.text(dst, "解析", (x, y), 17, theme.TEXT_DIM)
        y += 28
        for line in wrap_text(drill.explanation, 16, 560):
            theme.text(dst, line, (x, y), 16, theme.TEXT)
            y += 24
        self.btn_next.draw(dst)

    def _draw_footer(self, dst: pygame.Surface) -> None:
        session = self.session
        if session is None:
            return
        acc = f"{session.accuracy:.0%}" if session.asked else "—"
        best = max(session.best_streak, session.profile.data.get("best_streak", 0))
        theme.text(
            dst,
            f"本组 {session.asked} 题 · 得分率 {acc} · 当前连对 {session.streak} · 历史最佳连对 {best}",
            (800, 868), 16, theme.TEXT_DIM, "center",
        )
