"""牌桌场景:俯视 2–9 人桌,完整手牌流程对接 ``engine.Table``。

流程状态机:``deal``(发牌动画)→ ``action``(人类/AI 轮流行动,
AI 有 0.6-1.2s 思考延迟)→ 街道切换时 ``streetdeal``(公共牌动画)
→ 全下跑码 ``runout``(发完剩余公共牌)→ ``showdown_reveal``(翻开
摊牌底牌)→ ``finish``(逐池结算、筹码推向胜者、下一手)。ESC 呼出
暂停菜单。有玩家出局时，必须完整展示摊牌与派彩后才弹出处置模态框。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from pathlib import Path

import pygame

from ai.bots import build_style_bot
from ai.hand_rank import HandCategory, HandSummary, describe_holdem_hand
from ai.hybrid_bot import HybridPersonaBot
from ai.nfsp_runtime import LitePolicyNet
from ai.personas import Persona, persona_catalog, persona_by_id, with_style
from ai.styles import StylePreset, style_catalog
from ai.table_talk import (
    TalkEvent,
    choose_table_line,
    successful_bluff_targets,
)
from engine.game import (
    MAX_REBUY_BB,
    MIN_REBUY_BB,
    HandResult,
    Table,
    TableConfig,
)
from engine.state import Action, ActionType, GameSnapshot, Position, Street
from gto.advisor import Advisor, Hint

from .. import cards, fx, theme
from ..characters import ACTED, FOLDED, IDLE, THINKING, Bust, draw_chip_pile, draw_name_plate
from ..respath import res_path
from ..widgets import Button, NumberField, Panel, Slider, ToastLog
from .manager import Scene, SceneManager

HUMAN_SEAT = 0
SMALL_BLIND, BIG_BLIND, START_STACK = 5, 10, 1000

TABLE_C = (615, 460)
FELT_RX, FELT_RY = 480, 270
DECK_POS = (1000, 500)
BOARD_Y = 460
POT_CHIP_POS = (TABLE_C[0], BOARD_Y - 105)
PANEL_X = 1225

SHOWDOWN_FLIP_SECONDS = 0.34
SHOWDOWN_REVEAL_SECONDS = 1.15
BUST_RESULT_SECONDS = 2.2

# GTO 面板三条频率条的配色:弃牌灰红、过牌/跟注青、下注/加注琥珀
_GTO_FOLD_C = (150, 84, 72)
_GTO_CALL_C = (62, 148, 136)
_GTO_BET_C = (217, 142, 50)
_FOLD_STATUS_C = (166, 108, 92)
_MUCK_POS = (820, 535)
_NFSP_MODEL = ("assets", "models", "nfsp_hu_1244484.bin")

MIN_PLAYERS, MAX_PLAYERS = 2, 9


def _seat_layout(player_count: int) -> dict[int, tuple[float, float]]:
    """人类固定在正下方，其余座位沿牌桌外圈等角排布。"""
    if not MIN_PLAYERS <= player_count <= MAX_PLAYERS:
        raise ValueError(f"游玩人数须在 {MIN_PLAYERS}–{MAX_PLAYERS} 之间")
    rx, ry = 475.0, 332.0
    return {
        seat: (
            TABLE_C[0] + rx * math.cos(math.pi / 2 - math.tau * seat / player_count),
            TABLE_C[1] + ry * math.sin(math.pi / 2 - math.tau * seat / player_count),
        )
        for seat in range(player_count)
    }

STREET_LABEL = {
    Street.PREFLOP: "翻牌前",
    Street.FLOP: "翻牌圈",
    Street.TURN: "转牌圈",
    Street.RIVER: "河牌圈",
    Street.SHOWDOWN: "摊牌",
    Street.HAND_OVER: "本手结束",
}

_ACTION_VERB = {
    ActionType.FOLD: "弃牌",
    ActionType.CHECK: "过牌",
    ActionType.CALL: "跟注",
    ActionType.BET: "下注",
    ActionType.RAISE: "加注到",
    ActionType.ALLIN: "全下",
}

_ACTION_COLOR = {
    ActionType.FOLD: _FOLD_STATUS_C,
    ActionType.CHECK: theme.TEAL,
    ActionType.CALL: theme.TEAL,
    ActionType.BET: theme.AMBER_LIGHT,
    ActionType.RAISE: theme.AMBER_LIGHT,
    ActionType.ALLIN: theme.DANGER,
}


@dataclass
class _ActionEcho:
    """牌桌中央短暂出现的一次动作确认。"""

    seat: int
    label: str
    color: tuple[int, int, int]
    source: str | None = None
    elapsed: float = 0.0
    duration: float = 0.82


@dataclass(frozen=True)
class _SeatJoinRequest:
    """下一手把一个空位重新启用所需的纯配置。"""

    seat: int
    persona_id: str
    style_key: str
    amount_bb: int


@dataclass
class _SpeechBubble:
    """带延迟的牌桌对白；可让赢家与受骗者依次发言。"""

    seat: int
    text: str
    delay: float = 0.0
    duration: float = 3.2
    elapsed: float = 0.0


def _unit_to_center(anchor: tuple[float, float]) -> tuple[float, float]:
    vx, vy = TABLE_C[0] - anchor[0], TABLE_C[1] - anchor[1]
    n = math.hypot(vx, vy) or 1.0
    return (vx / n, vy / n)


class TableScene(Scene):
    """2–9 人牌局场景。

    :param seed: 全局种子(人设、机器人、粒子、洗牌全由此派生);
        ``None`` 表示完全随机。
    :param headless: 无头模式(截图/测试):不依赖窗口,思考延迟缩短。
    :param auto_human: 自动扮演人类玩家(脚本优先,否则 过牌>跟注>弃牌)。
    :param auto_next: 一手结束后自动进入下一手(测试用)。
    :param action_script: 可选脚本 ``{(hand_id, street名, seat): [Action, ...]}``,
        轮到该座位时优先弹出执行(确定性截图)。
    :param record_history: 是否把每手写入 ``hands/history.jsonl``
        (供「回顾/统计」场景消费);缺省 = 非 headless。
    :param buyins_bb: 各座位开局买入(BB)，元组长度即游玩人数；
        缺省为六人、每席 100BB。
    :param opponent_lineup: 可选的显式 AI 阵容，依座位顺序给出
        ``((persona_id, style_key), ...)``；长度必须等于玩家数减一，身份不可
        重复。缺省保持旧行为，按目录前 N 名装配默认阵容。
    :param use_nfsp: 是否加载发行包内的轻量 NFSP 推理模型。
    :param nfsp_model_path: 自定义轻量模型路径；缺省读取 ``assets/models``。
    :param nfsp_policy: 测试/嵌入用的已加载策略对象，全桌 AI 共享同一实例。
    """

    def __init__(
        self,
        manager: SceneManager | None = None,
        seed: int | None = None,
        headless: bool = False,
        auto_human: bool = False,
        auto_next: bool = False,
        think_range: tuple[float, float] = (0.6, 1.2),
        action_script: dict | None = None,
        hole_script: dict[int, tuple[str, str]] | None = None,
        board_script: list[str] | None = None,
        record_history: bool | None = None,
        buyins_bb: tuple[int, ...] | None = None,
        opponent_lineup: tuple[tuple[str, str], ...] | None = None,
        use_nfsp: bool = True,
        nfsp_model_path: str | Path | None = None,
        nfsp_policy: object | None = None,
    ) -> None:
        super().__init__(manager)
        self.seed = seed
        self.headless = headless
        self.auto_human = auto_human
        self.auto_next = auto_next
        self.think_range = (0.05, 0.1) if headless else think_range
        self.action_script = action_script or {}
        self._hole_script = hole_script
        self._board_script = board_script
        if record_history is None:
            record_history = not headless
        self._history_writer = None
        if record_history:
            from engine.history import HandHistoryWriter
            from training.review import DEFAULT_HISTORY_PATH

            self._history_writer = HandHistoryWriter(DEFAULT_HISTORY_PATH)
        self.rng = random.Random(seed)
        self._bot_rng = random.Random(None if seed is None else seed * 31 + 7)
        self._game_no = 0
        self.buyins_bb = (
            (START_STACK // BIG_BLIND,) * 6 if buyins_bb is None else buyins_bb
        )
        if not MIN_PLAYERS <= len(self.buyins_bb) <= MAX_PLAYERS:
            raise ValueError(f"游玩人数须在 {MIN_PLAYERS}–{MAX_PLAYERS} 之间")
        self.player_count = len(self.buyins_bb)
        self.opponent_lineup = self._validate_opponent_lineup(opponent_lineup)
        self._seat_anchors = _seat_layout(self.player_count)
        self._portrait_scale = (
            0.66 if self.player_count >= 8 else 0.76 if self.player_count == 7 else 0.95
        )

        # 氛围
        self.smoke = fx.ParticleSystem((330, 260, 900, 720), seed=seed, max_particles=42)
        self._glow = fx.radial_glow(1150, (255, 176, 96), 175, power=1.9)
        self._vignette = fx.vignette((1600, 900), 140, center=TABLE_C)
        self._grain = fx.grain_overlay((1600, 900), seed=5)
        self.anim = fx.CardAnimator()
        self.muck = fx.MuckAnimator()
        self.chipfly = fx.ChipFly()
        self.log = ToastLog()
        self._action_echoes: list[_ActionEcho] = []
        self._t = 0.0

        # 行动控件
        self.btn_fold = Button((60, 840, 112, 46), "弃牌 F", lambda: self._human_fold(), danger=True)
        self.btn_call = Button((188, 840, 156, 46), "过牌 C", lambda: self._human_call())
        self.btn_raise = Button((360, 840, 132, 46), "加注 R", lambda: self._human_raise())
        self.slider = Slider((540, 856, 320, 16), 0, 0, on_change=lambda v: None)
        self._chips: list[Button] = []
        for i, (lab, frac) in enumerate(
            (("33%", 0.33), ("50%", 0.5), ("75%", 0.75), ("底池", 1.0), ("全下", -1.0))
        ):
            self._chips.append(
                Button((862 + i * 66, 846, 58, 32), lab, lambda f=frac: self._chip_frac(f), size=16)
            )
        self.btn_next = Button((535, 560, 160, 46), "下一手 R", lambda: self._next_hand(), size=22)
        self.btn_show = Button(
            (710, 560, 160, 46), "SHOW 手牌", lambda: self._show_human_cards(), size=19
        )
        self.btn_rebuy = Button((465, 560, 140, 46), "重新买入", lambda: self.new_game(), size=20)
        self.btn_menu = Button((625, 560, 140, 46), "回主菜单", lambda: self._to_menu(), size=20)
        self.btn_resume = Button((695, 400, 210, 52), "继 续", lambda: self._toggle_pause(), size=24)
        self.btn_quit_menu = Button(
            (695, 470, 210, 52), "回主菜单", lambda: self._to_menu(), size=24, danger=True
        )

        # 状态
        self.paused = False
        self.phase = "idle"
        self._phase_t = 0.0
        self._think_left = 0.0
        self._raise_open = False
        self._visual_board = 0
        self._deal_order: list[int] = []
        self._settled_holes: dict[int, int] = {}
        self._acted: set[int] = set()
        self.reveal = False
        self._showdown_reveal_progress = 1.0
        self.banner: tuple[str, str] | None = None  # (主标题, 副标题)
        self._last_settlement_lines: tuple[str, ...] = ()
        self._final_pot_amount = 0
        self._payout_departed = False
        self.snap: GameSnapshot | None = None
        # 出局处置/空位重入:强制 bust 框优先于可取消的召回框。
        self._removed_seats: set[int] = set()
        self._bust_queue: list[int] = []
        self._bust_dialog: _BustDialog | None = None
        self._seat_dialog: _SeatJoinDialog | None = None
        self._pending_joins: dict[int, _SeatJoinRequest] = {}
        self._seat_generation: dict[int, int] = {}
        self._speech_bubbles: list[_SpeechBubble] = []
        self._menu_toast: str | None = None  # 回主菜单时顺带展示的提示

        # AI 策略路由:发行版只加载约 1.18MB 的平均策略权重，不依赖 torch。
        # 全桌具名 AI 共享同一实例；仅翻后剩两名争池者时实验性接管。
        self._nfsp_policy: object | None = nfsp_policy
        self._nfsp_load_error: str | None = None
        if self._nfsp_policy is None and use_nfsp:
            model_path = (
                Path(nfsp_model_path)
                if nfsp_model_path is not None
                else res_path(*_NFSP_MODEL)
            )
            try:
                self._nfsp_policy = LitePolicyNet(model_path)
            except Exception as exc:
                self._nfsp_load_error = type(exc).__name__
        elif self._nfsp_policy is None:
            self._nfsp_load_error = "disabled"
        meta = getattr(self._nfsp_policy, "meta", {}) or {}
        self._nfsp_episode = meta.get("episode")
        self._last_ai_source = ""
        self._last_ai_detail = ""

        # GTO 辅助(M4):建议引擎 + 按快照 id 缓存(动作/街道变化才重算)
        self.gto_on = True
        self._advisor = Advisor(big_blind=BIG_BLIND, seed=seed)
        self._hint_cache: dict[int, Hint] = {}
        self._villain_styles: dict = {}

        self.new_game()

    # ------------------------------------------------------------ 牌局装配

    def _validate_opponent_lineup(
        self,
        lineup: tuple[tuple[str, str], ...] | None,
    ) -> tuple[tuple[str, str], ...] | None:
        """校验并标准化开局页传入的身份/打法阵容。"""
        if lineup is None:
            return None
        normalized = tuple(
            (str(persona_id).strip().lower(), str(style_key).strip().upper())
            for persona_id, style_key in lineup
        )
        expected = self.player_count - 1
        if len(normalized) != expected:
            raise ValueError(f"AI 阵容须恰好包含 {expected} 名牌手")
        persona_ids = {persona.persona_id for persona in persona_catalog(self.seed)}
        style_keys = {preset.key for preset in style_catalog()}
        chosen_ids = [persona_id for persona_id, _ in normalized]
        if len(set(chosen_ids)) != len(chosen_ids):
            raise ValueError("AI 阵容中的身份不可重复")
        unknown_personas = sorted(set(chosen_ids) - persona_ids)
        if unknown_personas:
            raise ValueError(f"未知 AI 身份: {unknown_personas}")
        unknown_styles = sorted(
            {style_key for _, style_key in normalized} - style_keys
        )
        if unknown_styles:
            raise ValueError(f"未知 AI 打法: {unknown_styles}")
        return normalized

    def new_game(self) -> None:
        """开一桌新局:人类 + 已配置的具名 AI,各就各位。

        种子随局数递增,避免「重新买入」后牌序与上一局完全一致。
        """
        gseed = None if self.seed is None else self.seed + self._game_no * 101
        self._game_no += 1
        self._persona_catalog = persona_catalog(gseed)
        self._style_catalog = style_catalog()
        if self.opponent_lineup is None:
            self.personas = list(self._persona_catalog[: self.player_count - 1])
        else:
            by_id = {
                persona.persona_id: persona for persona in self._persona_catalog
            }
            self.personas = []
            for seat, (persona_id, style_key) in enumerate(
                self.opponent_lineup,
                start=1,
            ):
                base = by_id[persona_id]
                style_seed = (
                    None
                    if gseed is None
                    else gseed * 1009 + seat * 37
                )
                self.personas.append(
                    with_style(
                        base,
                        style_key,
                        seed=style_seed,
                        level=base.level,
                    )
                )
        names = ("你",) + tuple(p.display_name for p in self.personas)
        cfg = TableConfig.from_buyins_bb(
            player_count=self.player_count,
            buyins_bb=self.buyins_bb,
            small_blind=SMALL_BLIND,
            big_blind=BIG_BLIND,
            player_names=names,
        )
        self.table = Table(cfg, seed=gseed, history_writer=self._history_writer)
        self._bot_seed_base = None if gseed is None else gseed * 17 + 3
        self.bots: dict[int, HybridPersonaBot] = {}
        self.busts: dict[int, Bust] = {}
        self._villain_styles = {}
        self._seat_generation = {}
        for seat, persona in enumerate(self.personas, start=1):
            self._install_persona(seat, persona)
        self._hint_cache.clear()
        self.log.clear()
        self.chipfly.clear()
        self._removed_seats = set()
        self._bust_queue = []
        self._bust_dialog = None
        self._seat_dialog = None
        self._pending_joins = {}
        self._speech_bubbles = []
        self._menu_toast = None
        self._last_settlement_lines = ()
        self._final_pot_amount = 0
        self._payout_departed = False
        self._showdown_reveal_progress = 1.0
        buyin_summary = (
            f"统一 {self.buyins_bb[0]}BB"
            if len(set(self.buyins_bb)) == 1
            else "逐座位自定义"
        )
        self.log.add(
            f"酒馆开局 · 盲注 {SMALL_BLIND}/{BIG_BLIND} · {buyin_summary}"
        )
        if self._nfsp_policy is not None:
            self.log.add(
                f"NFSP 已载入 · {self._episode_text()}手 · 实验性 HU 池"
            )
        else:
            self.log.add("NFSP 不可用 · 已保留原人格 AI")
        self.start_next_hand()

    def _install_persona(self, seat: int, persona: Persona) -> None:
        """原子同步身份、头像、打法机器人和 GTO 对手画像。"""
        if not 1 <= seat < self.player_count:
            raise ValueError(f"AI 座位不存在: {seat}")
        self.personas[seat - 1] = persona
        generation = self._seat_generation.get(seat, 0) + 1
        self._seat_generation[seat] = generation
        bot_seed = (
            None
            if self._bot_seed_base is None
            else self._bot_seed_base + seat * 97 + generation * 1009
        )
        self.bots[seat] = HybridPersonaBot(
            heuristic=build_style_bot(
                persona.style_key,
                style=persona.style,
                level=persona.level,
                seed=bot_seed,
                big_blind=BIG_BLIND,
            ),
            policy=self._nfsp_policy,
        )
        self.busts[seat] = Bust(
            persona.species,
            seed=(bot_seed or seat),
            scale=self._portrait_scale,
        )
        self._villain_styles[seat] = persona.style

    def _configured_persona(
        self,
        persona_id: str,
        style_key: str,
        seat: int,
    ) -> Persona:
        """按身份目录取角色并覆写打法；身份自带水平保持不变。"""
        identity = persona_by_id(persona_id, self.seed)
        seed = None if self.seed is None else self.seed * 1009 + seat * 37 + self.table.hand_id
        return with_style(identity, style_key, seed=seed, level=identity.level)

    def start_next_hand(self) -> None:
        """发下一手:重置视觉状态并排队发牌动画。"""
        self.table.start_hand(self._hole_script, self._board_script)
        self._hole_script = None  # 脚本牌仅作用于首发(截图用)
        self._board_script = None
        self.banner = None
        self.reveal = False
        self._showdown_reveal_progress = 1.0
        self._final_pot_amount = 0
        self._payout_departed = False
        self._raise_open = False
        self._visual_board = 0
        self._acted = set()
        self._settled_holes = {}
        self._deal_order = []
        self._last_ai_source = ""
        self._last_ai_detail = ""
        self.anim.clear()
        self.muck.clear()
        self.chipfly.clear()
        self._action_echoes.clear()
        snap = self.table.snapshot(perspective=HUMAN_SEAT)
        self.snap = snap
        self.log.add(f"—— 第 {snap.hand_id} 手 · 翻牌前 ——")
        if self.table.current_straddle_amount:
            straddler = next(
                (
                    p.name
                    for p in snap.players
                    if p.position is Position.UTG
                ),
                "UTG",
            )
            actual = self.table.current_straddle_amount
            nominal = self.table.config.straddle_amount
            suffix = "（2BB）" if actual == nominal else "（短码全下，目标 2BB）"
            self.log.add(
                f"STR · UTG {straddler} 强制投入 "
                f"{actual}{suffix}"
            )
        order = [p.seat for p in snap.players if p.position is not None]
        idx = 0
        for _round in range(2):
            for seat in order:
                dst = self._hole_pos(seat, _round)
                code = "back"
                if seat == HUMAN_SEAT:
                    me = next(p for p in snap.players if p.seat == HUMAN_SEAT)
                    assert me.hole_cards is not None
                    code = me.hole_cards[_round]
                size = cards.SIZE_HOLE if seat == HUMAN_SEAT else cards.SIZE_MINI
                self.anim.add_deal(
                    cards.card_surface(code, size),
                    cards.card_surface("back", size),
                    DECK_POS,
                    dst,
                    delay=0.07 * idx,
                    duration=0.32,
                    rot=(7 if _round == 0 else -7) if seat == HUMAN_SEAT else 0.0,
                    face_up=(seat == HUMAN_SEAT),
                )
                self._deal_order.append(seat)
                idx += 1
        self.phase = "deal"
        self._phase_t = 0.0

    # ------------------------------------------------------------ 布局

    def _hole_pos(self, seat: int, idx: int) -> tuple[float, float]:
        anchor = self._seat_anchors[seat]
        if seat == HUMAN_SEAT:
            return (anchor[0] - 38 + idx * 76, anchor[1] - 138)
        ux, uy = _unit_to_center(anchor)
        reach = 112 if self.player_count >= 8 else 122 if self.player_count == 7 else 136
        cx, cy = anchor[0] + ux * reach, anchor[1] + uy * reach
        # 两张小牌沿垂直于视线的方向排开
        px, py = -uy, ux
        gap = 28 if self.player_count >= 8 else 34
        return (cx + px * (idx - 0.5) * gap, cy + py * (idx - 0.5) * gap)

    def _bet_pos(self, seat: int) -> tuple[float, float]:
        anchor = self._seat_anchors[seat]
        if seat == HUMAN_SEAT:
            return (anchor[0], anchor[1] - 252)
        ux, uy = _unit_to_center(anchor)
        reach = 190 if self.player_count >= 8 else 208
        return (anchor[0] + ux * reach, anchor[1] + uy * reach)

    def _stack_chip_pos(self, seat: int) -> tuple[float, float]:
        """返回角色桌前的静态筹码位，也作为下注/收池动画端点。"""
        anchor = self._seat_anchors[seat]
        if seat == HUMAN_SEAT:
            return (anchor[0] + 146, anchor[1] - 72)
        ux, uy = _unit_to_center(anchor)
        px, py = -uy, ux
        # 放在底牌与下注位之间并向一侧错开；不可贴近头像下沿，九人桌
        # 顶部座位的姓名牌正位于那里。
        reach = 150 if self.player_count >= 8 else 165
        side = (48 if self.player_count >= 8 else 52) * (1 if seat % 2 else -1)
        return (
            anchor[0] + ux * reach + px * side,
            anchor[1] + uy * reach + py * side,
        )

    def _display_stack(self, seat: int, settled_stack: int) -> int:
        """派彩飞行完成前暂缓把奖金计入桌面筹码堆。"""
        result = self.table.last_hand_result
        if result is None or not self.table.hand_over:
            return settled_stack
        staging = self.phase in ("runout", "showdown_reveal") or (
            self.phase == "finish" and self.chipfly.busy_for("payout")
        )
        if not staging:
            return settled_stack
        return max(0, settled_stack - result.winners.get(seat, 0))

    # ------------------------------------------------------------ 事件

    def handle_event(self, ev: pygame.event.Event) -> None:
        if self._bust_dialog is not None:
            # 出局处置模态框:拦截全部输入,ESC 也不能跳过(必须做决定)
            self._bust_dialog.handle_event(ev)
            return
        if self._seat_dialog is not None:
            # 空位召回可取消，但同样拦截桌面输入，避免选人时牌局继续。
            self._seat_dialog.handle_event(ev)
            return
        if ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE:
            self._toggle_pause()
            return
        if ev.type == pygame.KEYDOWN and ev.key == pygame.K_g:
            self.gto_on = not self.gto_on
            self.log.add("GTO 辅助 " + ("开启" if self.gto_on else "关闭"))
            return
        if self.paused:
            self.btn_resume.handle_event(ev)
            self.btn_quit_menu.handle_event(ev)
            return
        if self._handle_empty_seat_event(ev):
            return
        if self.phase == "finish":
            self.btn_next.handle_event(ev)
            if self._can_human_show():
                self.btn_show.handle_event(ev)
            if self.table.game_over:
                self.btn_rebuy.handle_event(ev)
                self.btn_menu.handle_event(ev)
            if ev.type == pygame.KEYDOWN and ev.key in (pygame.K_r, pygame.K_SPACE):
                self._next_hand()
            return
        if not self._human_to_act():
            return
        self.btn_fold.handle_event(ev)
        self.btn_call.handle_event(ev)
        self.btn_raise.handle_event(ev)
        if self._raise_open:
            self.slider.handle_event(ev)
            for c in self._chips:
                c.handle_event(ev)
        if ev.type == pygame.KEYDOWN:
            if ev.key == pygame.K_f:
                self._human_fold()
            elif ev.key == pygame.K_c:
                self._human_call()
            elif ev.key in (pygame.K_r, pygame.K_RETURN):
                self._human_raise()

    def _empty_seat_rect(self, seat: int) -> pygame.Rect:
        """空位加号与提示文字共用的点击热区。"""
        rect = pygame.Rect(0, 0, 176, 156)
        rect.center = self._seat_anchors[seat]
        return rect

    def _handle_empty_seat_event(self, ev: pygame.event.Event) -> bool:
        """点空位或已预约座位时打开召回/编辑面板。"""
        if self._bust_queue:
            return False
        if ev.type != pygame.MOUSEBUTTONUP or ev.button != 1:
            return False
        for seat in sorted(self.table.removed_seats):
            if self._empty_seat_rect(seat).collidepoint(ev.pos):
                self._seat_dialog = _SeatJoinDialog(self, seat)
                return True
        return False

    # ------------------------------------------------------------ 人类动作

    def _legal(self):
        if self.snap is None:
            return None
        return self.snap.legal_actions

    def _human_to_act(self) -> bool:
        return (
            self.phase == "action"
            and self.snap is not None
            and self.snap.acting_seat == HUMAN_SEAT
        )

    def _human_fold(self) -> None:
        legal = self._legal()
        if self._human_to_act() and legal is not None and legal.can_fold:
            self._apply(Action(HUMAN_SEAT, ActionType.FOLD))

    def _human_call(self) -> None:
        legal = self._legal()
        if not self._human_to_act() or legal is None:
            return
        if legal.can_check:
            self._apply(Action(HUMAN_SEAT, ActionType.CHECK))
        elif legal.can_call:
            self._apply(Action(HUMAN_SEAT, ActionType.CALL, legal.call_amount))

    def _human_raise(self) -> None:
        legal = self._legal()
        if not self._human_to_act() or legal is None:
            return
        lo, hi, kind = self._wager_bounds(legal)
        if lo is None:
            return
        if not self._raise_open:
            self._raise_open = True
            self.slider.set_range(lo, hi)  # type: ignore[arg-type]
            self.slider.set_value(max(lo, min(hi, self._pot_target(legal, 0.66))))
            return
        self._apply(Action(HUMAN_SEAT, kind, self.slider.value))

    def _chip_frac(self, frac: float) -> None:
        """底池分数快捷键:设置滑杆目标额。"""
        legal = self._legal()
        if not self._raise_open or legal is None:
            return
        if frac < 0:  # 全下
            self.slider.set_value(self.slider.hi)
        else:
            self.slider.set_value(self._pot_target(legal, frac))

    def _wager_bounds(self, legal) -> tuple[int | None, int | None, ActionType]:
        if legal.min_raise_to is not None:
            return legal.min_raise_to, legal.max_raise_to, ActionType.RAISE
        if legal.min_bet_to is not None:
            return legal.min_bet_to, legal.max_bet_to, ActionType.BET
        return None, None, ActionType.BET

    def _pot_target(self, legal, frac: float) -> int:
        """``frac`` 倍底池的“加注到”目标(含需跟注部分)。"""
        pot = self._pot_total()
        faced = max((p.bet for p in self.snap.players), default=0)
        target = round(faced + legal.call_amount + frac * (pot + legal.call_amount))
        lo, hi, _ = self._wager_bounds(legal)
        if lo is None:
            return 0
        return max(lo, min(hi, target))

    def _pot_total(self) -> int:
        assert self.snap is not None
        return self.snap.total_pot + sum(p.bet for p in self.snap.players)

    # ------------------------------------------------------------ 动作执行

    def _apply(
        self,
        action: Action,
        ai_source: str | None = None,
        ai_detail: str = "",
    ) -> None:
        """执行动作、记日志、同步公共牌/结算动画。

        FOLD 的牌面素材必须在 ``table.apply`` 前捕获；动作应用后快照
        可能隐藏底牌，届时再取会只剩状态而没有可供丢牌的图像。
        """
        before = self.snap
        name = before.players[action.seat].name if before else f"seat{action.seat}"
        before_bet = before.players[action.seat].bet if before else 0
        if action.action_type is ActionType.CALL:
            paid = action.amount
        elif action.action_type in (
            ActionType.BET,
            ActionType.RAISE,
            ActionType.ALLIN,
        ):
            paid = max(0, action.amount - before_bet)
        else:
            paid = 0
        collected_bets = (
            {player.seat: player.bet for player in before.players}
            if before is not None
            else {}
        )
        if paid:
            collected_bets[action.seat] = before_bet + paid
        fold_visuals = (
            self._capture_fold_visuals(action.seat)
            if action.action_type is ActionType.FOLD
            else []
        )
        self.table.apply(action)
        if paid:
            self.chipfly.launch_amount(
                self._stack_chip_pos(action.seat),
                self._bet_pos(action.seat),
                paid,
                rng=self.rng,
                group="wager",
                min_chips=2,
                max_chips=14,
            )
        if fold_visuals:
            self.muck.launch(fold_visuals, _MUCK_POS, action.seat)
        self._acted.add(action.seat)
        verb = _ACTION_VERB[action.action_type]
        suffix = f" {action.amount}" if action.amount else ""
        if action.action_type in (ActionType.FOLD, ActionType.CHECK):
            suffix = ""
        if ai_source is not None:
            self._last_ai_source = ai_source
            self._last_ai_detail = ai_detail
        source_suffix = " · NFSP HU池" if ai_source == "net:nfsp" else ""
        self.log.add(f"{name} {verb}{suffix}{source_suffix}")
        self._action_echoes.append(
            _ActionEcho(
                action.seat,
                f"{verb}{suffix}",
                _ACTION_COLOR[action.action_type],
                "NFSP · HU池" if ai_source == "net:nfsp" else None,
            )
        )
        self._raise_open = False
        self._sync_snapshot(
            previous=before,
            collected_bets=collected_bets,
            wager_delay=0.56 if paid else 0.0,
        )

    def _capture_fold_visuals(
        self, seat: int
    ) -> list[tuple[pygame.Surface, tuple[float, float], float]]:
        """在引擎应用弃牌前捕获座位上的两张静态底牌。"""
        if self.snap is None:
            return []
        player = self.snap.players[seat]
        count = min(2, self._settled_holes.get(seat, 0))
        visuals: list[tuple[pygame.Surface, tuple[float, float], float]] = []
        for index in range(count):
            if seat == HUMAN_SEAT:
                if player.hole_cards is None or index >= len(player.hole_cards):
                    continue
                code = player.hole_cards[index]
                size = cards.SIZE_HOLE
                rotation = -7.0 if index == 0 else 7.0
            else:
                # 对手牌在当前视角始终保持牌背，绝不借动画偷看。
                code = "back"
                size = cards.SIZE_MINI
                rotation = -4.0 if index == 0 else 4.0
            visuals.append(
                (cards.card_surface(code, size), self._hole_pos(seat, index), rotation)
            )
        return visuals

    def _sync_snapshot(
        self,
        *,
        previous: GameSnapshot | None = None,
        collected_bets: dict[int, int] | None = None,
        wager_delay: float = 0.0,
    ) -> None:
        """动作后重新取快照,检测街道推进与手牌结束。"""
        snap = self.table.snapshot(perspective=HUMAN_SEAT)
        prev_board = self._visual_board
        self.snap = snap
        street_advanced = previous is not None and previous.street is not snap.street
        if collected_bets and (street_advanced or self.table.hand_over):
            required = sum(collected_bets.values())
            if self.table.hand_over and self.table.last_hand_result is not None:
                final_total = sum(
                    pot.amount for pot in self.table.last_hand_result.pots
                )
                required = max(0, final_total - (previous.total_pot if previous else 0))
            visual_bets = self._collectable_bets(collected_bets, required)
            for seat, amount in visual_bets.items():
                if amount <= 0:
                    continue
                self.chipfly.launch_amount(
                    self._bet_pos(seat),
                    POT_CHIP_POS,
                    amount,
                    rng=self.rng,
                    delay=wager_delay,
                    group="collect",
                    min_chips=2,
                    max_chips=12,
                )
        if len(snap.board) > self._visual_board:
            self._deal_board_anims(
                snap.board[self._visual_board :],
                runout=self.table.hand_over,
            )
            self._visual_board = len(snap.board)
        if self.table.hand_over:
            result = self.table.last_hand_result
            assert result is not None
            self._final_pot_amount = sum(pot.amount for pot in result.pots)
            if len(snap.board) > prev_board and self.anim.busy:
                self.phase = "runout"
            else:
                self._enter_showdown_reveal() if result.showdown else self._enter_finish()
        elif len(snap.board) > prev_board:
            self._acted = set()
            self.log.add(f"—— {STREET_LABEL[snap.street]} ——")
            self.phase = "streetdeal"
            self._phase_t = 0.0
        # 同一街道继续行动则保持 action 阶段

    @staticmethod
    def _collectable_bets(
        bets: dict[int, int],
        required_total: int,
    ) -> dict[int, int]:
        """扣除未跟注返还，得到真正汇入最终底池的本街筹码。"""
        result = {seat: max(0, amount) for seat, amount in bets.items()}
        excess = max(0, sum(result.values()) - max(0, required_total))
        for seat in sorted(result, key=lambda value: result[value], reverse=True):
            if excess <= 0:
                break
            returned = min(excess, result[seat])
            result[seat] -= returned
            excess -= returned
        return result

    def _deal_board_anims(
        self,
        new_codes: tuple[str, ...],
        *,
        runout: bool = False,
    ) -> None:
        start_idx = self._visual_board
        for i, code in enumerate(new_codes):
            x = TABLE_C[0] + (start_idx + i - 2) * 70
            self.anim.add_deal(
                cards.card_surface(code, cards.SIZE_BOARD),
                cards.card_surface("back", cards.SIZE_BOARD),
                DECK_POS,
                (x, BOARD_Y),
                delay=(0.24 if runout else 0.14) * i,
                duration=0.3,
                face_up=True,
                flip=True,
            )

    # ------------------------------------------------------------ 结算

    def _enter_showdown_reveal(self) -> None:
        """公共牌落定后先无遮挡翻开底牌，再进入派彩横幅。"""
        result = self.table.last_hand_result
        assert result is not None and result.showdown
        self.reveal = True
        self.banner = None
        self.phase = "showdown_reveal"
        self._phase_t = 0.0
        self._showdown_reveal_progress = 0.0
        self.log.add("—— 摊牌 · 亮牌 ——")

    def _enter_finish(self) -> None:
        res = self.table.last_hand_result
        assert res is not None
        self.reveal = res.showdown
        self._showdown_reveal_progress = 1.0
        self._final_pot_amount = sum(pot.amount for pot in res.pots)
        self._last_settlement_lines = self._build_settlement_lines(res)
        for line in self._last_settlement_lines:
            self.log.add(f"» {line}")
        self.banner = self._settlement_banner(res)
        self.phase = "finish"
        self._phase_t = 0.0
        self._payout_departed = False
        payout_delay = self.chipfly.remaining + 0.10
        for award_index, award in enumerate(res.pot_awards):
            for payout_index, (seat, amount) in enumerate(award.payouts):
                self.chipfly.launch_amount(
                    POT_CHIP_POS,
                    self._stack_chip_pos(seat),
                    amount,
                    rng=self.rng,
                    delay=payout_delay + award_index * 0.14 + payout_index * 0.06,
                    group="payout",
                    min_chips=3,
                    max_chips=16,
                )
        self._prepare_table_talk(res)
        self._collect_busts()

    def _ending_street_label(self) -> str:
        """返回最后一次真实行动所在街道，供未摊牌结果叙述。"""
        actions = self.table.last_actions
        if not actions:
            return "翻牌前"
        try:
            return STREET_LABEL[Street[str(actions[-1]["street"])]]
        except (KeyError, TypeError):
            return "本手"

    def _winner_hand_label(self, seat: int, res: HandResult) -> str:
        """只在摊牌后从人类可见快照计算赢家牌型。"""
        if not res.showdown or self.snap is None:
            return ""
        hole = self.snap.players[seat].hole_cards
        if hole is None:
            return ""
        return describe_holdem_hand(hole, res.board).label

    def _build_settlement_lines(self, res: HandResult) -> tuple[str, ...]:
        """构造第几手、是否摊牌、逐池赢家/牌型/金额的完整叙述。"""
        total = sum(pot.amount for pot in res.pots)
        mode = "摊牌" if res.showdown else f"{self._ending_street_label()}未摊牌"
        lines = [f"第 {res.hand_id} 手 · {mode} · 总池 ¢{total}"]
        for award in res.pot_awards:
            pool = "主池" if award.pot_index == 0 else f"边池 {award.pot_index}"
            for seat, amount in award.payouts:
                name = self.table_snapshot_name(seat)
                if res.showdown:
                    hand = self._winner_hand_label(seat, res) or "最佳牌"
                    verb = "分得" if len(award.payouts) > 1 else "赢得"
                    lines.append(
                        f"{pool} ¢{award.pot.amount}：{name}以{hand}{verb} ¢{amount}"
                    )
                else:
                    lines.append(
                        f"{pool} ¢{award.pot.amount}：{name}未摊牌收池 ¢{amount}"
                    )
        return tuple(lines)

    def _settlement_banner(self, res: HandResult) -> tuple[str, str]:
        """从逐池明细提炼牌桌中央的一行结果。"""
        total = sum(pot.amount for pot in res.pots)
        names = "、".join(self.table_snapshot_name(seat) for seat in res.winners)
        if len(res.winners) == 1:
            seat = next(iter(res.winners))
            title = f"{names} 赢得 ¢{res.winners[seat]}"
            if res.showdown:
                detail = self._winner_hand_label(seat, res) or "最佳牌"
                sub = f"第 {res.hand_id} 手 · 摊牌 · {detail} · 底池 ¢{total}"
            else:
                sub = (
                    f"第 {res.hand_id} 手 · {self._ending_street_label()}未摊牌"
                    f" · 底池 ¢{total}"
                )
            return title, sub
        return (
            f"{names} 分得总池 ¢{total}",
            f"第 {res.hand_id} 手 · {'摊牌' if res.showdown else '未摊牌'}"
            " · 主池/边池详见右侧",
        )

    def _prepare_table_talk(self, res: HandResult) -> None:
        """按结算与动作线排队人格对白；只在手牌结束后读取完整牌。"""
        self._speech_bubbles = []
        if not res.winners:
            return
        omniscient = self.table.snapshot()
        winner = max(res.winners, key=res.winners.get)
        winner_player = omniscient.players[winner]
        targets: tuple[int, ...] = ()
        if winner_player.hole_cards is not None:
            summary = describe_holdem_hand(winner_player.hole_cards, res.board)
            targets = successful_bluff_targets(
                winner_seat=winner,
                hand_category=summary.category.name,
                actions=self.table.last_actions,
                showdown=res.showdown,
            )

        if winner != HUMAN_SEAT:
            persona = self.personas[winner - 1]
            event = TalkEvent.BLUFF_SUCCESS if targets else TalkEvent.COLLECT
            # 成功诈唬必说；普通收池留出安静牌局，避免每手刷屏。
            if targets or self.rng.random() < 0.62:
                self._speech_bubbles.append(
                    _SpeechBubble(
                        winner,
                        choose_table_line(persona.persona_id, event, self.rng),
                        delay=0.35,
                    )
                )
            if targets and self.rng.random() < 0.72:
                # AI 主动承认诈唬时同步 SHOW，玩家可以当场核对。
                self.table.show_cards(winner)
                self.snap = self.table.snapshot(perspective=HUMAN_SEAT)

        spoken_targets = [seat for seat in targets if seat != HUMAN_SEAT]
        if spoken_targets:
            seat = spoken_targets[0]
            persona = self.personas[seat - 1]
            self._speech_bubbles.append(
                _SpeechBubble(
                    seat,
                    choose_table_line(persona.persona_id, TalkEvent.BLUFFED, self.rng),
                    delay=2.5,
                )
            )
        elif self.rng.random() < 0.38:
            losers = [
                seat
                for seat, delta in res.deltas.items()
                if seat != HUMAN_SEAT and delta < 0
            ]
            if losers:
                seat = min(losers, key=res.deltas.get)
                persona = self.personas[seat - 1]
                self._speech_bubbles.append(
                    _SpeechBubble(
                        seat,
                        choose_table_line(
                            persona.persona_id,
                            TalkEvent.LOST_POT,
                            self.rng,
                        ),
                        delay=2.35,
                    )
                )

    # ------------------------------------------------------------ 出局处置(M8)

    def _collect_busts(self) -> None:
        """只排队新出局座位；派彩演出完成后才真正打开模态框。"""
        busted = self.table.busted_seats
        self._bust_queue = (
            ([HUMAN_SEAT] if HUMAN_SEAT in busted else [])
            + [seat for seat in busted if seat != HUMAN_SEAT]
        )
        self._bust_dialog = None

    def _open_next_bust_dialog(self) -> None:
        """在结果已清晰展示后打开下一项强制出局处置。"""
        if self._bust_dialog is None and self._bust_queue:
            self._bust_dialog = _BustDialog(self, self._bust_queue.pop(0))

    def _advance_bust_queue(self) -> None:
        """当前处置完毕,弹出下一个待处理 AI(若有)。"""
        self._bust_dialog = None
        self._open_next_bust_dialog()

    def _resolve_bust_rebuy(self, dlg: "_BustDialog") -> None:
        """按所选金额买回；AI 同时应用当前选中的打法。"""
        amount = dlg.amount
        if amount is None:
            return
        if not dlg.is_human:
            persona = self.personas[dlg.seat - 1]
            configured = with_style(
                persona,
                dlg.style_key,
                seed=None if self.seed is None else self.seed + self.table.hand_id * 97,
                level=persona.level,
            )
            self._install_persona(dlg.seat, configured)
        self.table.rebuy(dlg.seat, amount)
        suffix = "" if dlg.is_human else f" · {dlg.style_label}"
        self.log.add(
            f"{dlg.name} 重新买入 {dlg.amount_bb}BB（{amount} 筹码）{suffix}"
        )
        self._advance_bust_queue()

    def _resolve_bust_alt(self, dlg: "_BustDialog") -> None:
        """对话框的次按钮:人类回主菜单;AI 移出牌桌(只剩人类则散场)。"""
        if dlg.is_human:
            self._bust_dialog = None
            self._menu_toast = "本局已结束"
            self._to_menu()
            return
        if len(self.table.active_seats) < 2:
            # 移出后将只剩人类一人:牌桌散场
            self._bust_dialog = None
            self._menu_toast = "牌桌已散场"
            self._to_menu()
            return
        self.table.remove_player(dlg.seat)
        self._removed_seats.add(dlg.seat)
        self.log.add(f"{dlg.name} 移出牌桌 · 座位现可点击 ＋ 召回")
        self._advance_bust_queue()

    def _queue_seat_join(self, dlg: "_SeatJoinDialog") -> None:
        """保存空位配置；真正入座统一发生在下一手发牌前。"""
        request = dlg.request
        if request is None:
            return
        self._pending_joins[request.seat] = request
        persona = persona_by_id(request.persona_id, self.seed)
        self.log.add(
            f"座位 {request.seat + 1} 已预约 {persona.display_name} · "
            f"{request.amount_bb}BB · 下一手加入"
        )
        self._seat_dialog = None

    def _cancel_seat_join(self, seat: int) -> None:
        """取消一个尚未生效的召回预约。"""
        self._pending_joins.pop(seat, None)
        self._seat_dialog = None

    def _apply_pending_joins(self) -> None:
        """两手之间把所有预约空位一次性写入引擎并重建 AI。"""
        if not self._pending_joins:
            return
        if not self.table.hand_over:
            raise RuntimeError("空位只能在两手之间重新入座")
        for seat, request in sorted(self._pending_joins.items()):
            persona = self._configured_persona(
                request.persona_id,
                request.style_key,
                seat,
            )
            self.table.seat_player(
                seat,
                request.amount_bb * BIG_BLIND,
                name=persona.display_name,
            )
            self._install_persona(seat, persona)
            self._removed_seats.discard(seat)
            preset = next(p for p in self._style_catalog if p.key == request.style_key)
            self.log.add(
                f"{persona.display_name} 回到座位 {seat + 1} · "
                f"{request.amount_bb}BB · {preset.label}"
            )
        self._pending_joins.clear()
        # 刚结束手牌的快照也要立刻反映新姓名/空位状态。
        self.snap = self.table.snapshot(perspective=HUMAN_SEAT)

    def _occupied_persona_ids(self, except_seat: int) -> set[str]:
        """返回其他在座/已预约身份，召回面板据此防止同名重复。"""
        occupied: set[str] = set()
        for seat, persona in enumerate(self.personas, start=1):
            if seat == except_seat or seat in self.table.removed_seats:
                continue
            occupied.add(persona.persona_id)
        for seat, request in self._pending_joins.items():
            if seat != except_seat:
                occupied.add(request.persona_id)
        return occupied

    def table_snapshot_name(self, seat: int) -> str:
        assert self.snap is not None
        return self.snap.players[seat].name

    def _human_busted(self) -> bool:
        return self.table.stacks[HUMAN_SEAT] <= 0

    def _next_hand(self) -> None:
        if self.phase != "finish":
            return
        if (
            self._bust_dialog is not None
            or self._bust_queue
            or self._seat_dialog is not None
        ):
            return  # 须先处置完所有出局座位
        self._apply_pending_joins()
        if self.table.game_over:
            return  # 整场结束,由横幅上的按钮接管
        self.start_next_hand()

    def _can_human_show(self) -> bool:
        """未摊牌结束且人类参与了本手时，可主动 SHOW（含弃牌手）。"""
        result = self.table.last_hand_result
        return bool(
            self.phase == "finish"
            and result is not None
            and not result.showdown
            and HUMAN_SEAT in result.deltas
            and HUMAN_SEAT not in self.table.shown_seats
        )

    def _show_human_cards(self) -> None:
        """主动亮出人类手牌；这是本地交互也是未来联机消息契约。"""
        if not self._can_human_show():
            return
        self.table.show_cards(HUMAN_SEAT)
        self.snap = self.table.snapshot(perspective=HUMAN_SEAT)
        self.log.add("你选择 SHOW 手牌 · 已记录亮牌事件")

    def _toggle_pause(self) -> None:
        self.paused = not self.paused

    def _to_menu(self) -> None:
        if self.manager is not None:
            from .menu import MenuScene

            self.manager.replace(MenuScene(seed=self.seed, toast=self._menu_toast))

    # ------------------------------------------------------------ 帧驱动

    def update(self, dt: float) -> None:
        self._t += dt
        if self.paused:
            return
        if self._seat_dialog is not None or self._bust_dialog is not None:
            # 选人/处置出局期间冻结牌局与对白计时，只保留环境和头像呼吸。
            # 否则结算对白会在强制 bust 模态框后方悄悄过期。
            self.smoke.update(dt)
            for bust in self.busts.values():
                bust.update(dt)
            return
        self.smoke.update(dt)
        self.anim.update(dt)
        self.muck.update(dt)
        self.chipfly.update(dt)
        if self.phase == "finish" and self.chipfly.started_for("payout"):
            self._payout_departed = True
        for echo in self._action_echoes:
            echo.elapsed += dt
        self._action_echoes = [
            echo for echo in self._action_echoes if echo.elapsed < echo.duration
        ]
        for bubble in self._speech_bubbles:
            bubble.elapsed += dt
        self._speech_bubbles = [
            bubble
            for bubble in self._speech_bubbles
            if bubble.elapsed < bubble.delay + bubble.duration
        ]
        for b in self.busts.values():
            b.update(dt)
        if self.phase == "deal":
            self._track_dealt()
            if not self.anim.busy:
                for seat in self._deal_order:
                    self._settled_holes[seat] = 2
                self.phase = "action"
                self._think_left = self._think_time()
        elif self.phase == "streetdeal":
            if not self.anim.busy:
                self.phase = "action"
                self._think_left = self._think_time()
        elif self.phase == "runout":
            if not self.anim.busy:
                result = self.table.last_hand_result
                assert result is not None
                self._enter_showdown_reveal() if result.showdown else self._enter_finish()
        elif self.phase == "showdown_reveal":
            self._phase_t += dt
            self._showdown_reveal_progress = min(
                1.0,
                self._phase_t / SHOWDOWN_FLIP_SECONDS,
            )
            if self._phase_t >= SHOWDOWN_REVEAL_SECONDS:
                self._enter_finish()
        elif self.phase == "action":
            self._update_action(dt)
        elif self.phase == "finish":
            self._phase_t += dt
            if (
                self._bust_queue
                and self._phase_t >= BUST_RESULT_SECONDS
                and not self.chipfly.busy
            ):
                self._open_next_bust_dialog()
                return
            if (
                self.auto_next
                and self._phase_t > 0.8
                and not self.chipfly.busy
                and self._bust_dialog is None
                and not self._bust_queue
            ):
                self._next_hand()

    def _track_dealt(self) -> None:
        """发牌阶段:按动画完成进度解锁静态底牌绘制。"""
        total = len(self._deal_order)
        arrived = total - len(self.anim.cards)
        counts: dict[int, int] = {}
        for seat in self._deal_order[:arrived]:
            counts[seat] = counts.get(seat, 0) + 1
        self._settled_holes = counts

    def _think_time(self) -> float:
        return self.rng.uniform(*self.think_range)

    def _update_action(self, dt: float) -> None:
        snap = self.snap
        if snap is None or snap.acting_seat is None:
            return
        seat = snap.acting_seat
        self._think_left -= dt
        if self._think_left > 0:
            return
        if seat == HUMAN_SEAT:
            if self.auto_human:
                action = self._scripted_or_fallback(seat)
                self._apply(action)
            else:
                self._think_left = 0.2  # 等待输入
            return
        # AI 行动
        action = self._scripted(seat)
        if action is not None:
            ai_source, ai_detail = "scripted", ""
        else:
            bot = self.bots[seat]
            action = bot.decide(self.table.snapshot(perspective=seat), self._bot_rng)
            ai_source, ai_detail = bot.last_source, bot.last_detail
        self._apply(action, ai_source, ai_detail)
        self._think_left = self._think_time()

    def _scripted(self, seat: int) -> Action | None:
        """取出该座位当前街道的脚本动作(若有)。"""
        assert self.snap is not None
        key = (self.snap.hand_id, self.snap.street.name, seat)
        seq = self.action_script.get(key)
        if seq:
            return seq.pop(0)
        return None

    def _scripted_or_fallback(self, seat: int) -> Action:
        action = self._scripted(seat)
        if action is not None:
            return action
        legal = self.snap.legal_actions
        assert legal is not None
        if legal.can_check:
            return Action(seat, ActionType.CHECK)
        if legal.can_call:
            return Action(seat, ActionType.CALL, legal.call_amount)
        return Action(seat, ActionType.FOLD)

    # ------------------------------------------------------------ 绘制

    def draw(self, dst: pygame.Surface) -> None:
        dst.fill(theme.BG)
        # 灯光(轻微闪烁)
        flick = 1.0 + 0.02 * math.sin(self._t * 2.3) + 0.012 * math.sin(self._t * 5.7)
        gw = int(1150 * flick)
        glow = pygame.transform.smoothscale(self._glow, (gw, gw))
        dst.blit(glow, glow.get_rect(center=(TABLE_C[0], TABLE_C[1] - 30)))
        self._draw_felt(dst)
        self._draw_board_and_pot(dst)
        self._draw_seats(dst)
        self.anim.draw(dst)
        self.muck.draw(dst)
        self.chipfly.draw(dst)
        self.smoke.draw(dst)
        # 暗角/颗粒只统一桌面世界，不再压暗信息栏、横幅和模态框。
        dst.blit(self._vignette, (0, 0))
        dst.blit(self._grain, (0, 0))
        self._draw_hand_strength_overlay(dst)
        self._draw_action_echoes(dst)
        self._draw_speech_bubbles(dst)
        self._draw_info_panel(dst)
        if self._human_to_act():
            self._draw_action_bar(dst)
        if self.phase == "finish":
            self._draw_banner(dst)
        if self._bust_dialog is not None:
            self._bust_dialog.draw(dst, self._t)
        elif self._seat_dialog is not None:
            self._seat_dialog.draw(dst, self._t)
        if self.paused:
            self._draw_pause(dst)

    def _draw_felt(self, dst: pygame.Surface) -> None:
        rim = pygame.Rect(0, 0, (FELT_RX + 26) * 2, (FELT_RY + 26) * 2)
        rim.center = TABLE_C
        pygame.draw.ellipse(dst, (46, 32, 19), rim)
        pygame.draw.ellipse(dst, theme.FELT_EDGE, rim, 3)
        felt = pygame.Rect(0, 0, FELT_RX * 2, FELT_RY * 2)
        felt.center = TABLE_C
        pygame.draw.ellipse(dst, theme.FELT, felt)
        pygame.draw.ellipse(dst, (52, 38, 23), felt, 2)
        # 台面内圈装饰线
        inner = felt.inflate(-56, -56)
        pygame.draw.ellipse(dst, (44, 32, 20), inner, 1)

    def _draw_board_and_pot(self, dst: pygame.Surface) -> None:
        snap = self.snap
        if snap is None:
            return
        # 牌堆
        back = cards.card_surface("back", cards.SIZE_BOARD)
        dst.blit(back, back.get_rect(center=DECK_POS))
        # 公共牌(已落下的;飞行中的由 animator 画)
        settled = self._visual_board - len(self.anim.cards)
        for i, code in enumerate(snap.board[: max(0, settled)]):
            surf = cards.card_surface(code, cards.SIZE_BOARD)
            x = TABLE_C[0] + (i - 2) * 70
            dst.blit(surf, surf.get_rect(center=(x, BOARD_Y)))
        # 当前街下注留在各座位前；已收集部分在中央形成有面值的筹码堆。
        # 引擎虽会即时派彩，UI 在亮牌/派彩动画前持续保留最终底池。
        if self.table.hand_over:
            pot = self._final_pot_amount
            show_pile = self.phase in ("runout", "showdown_reveal") or (
                self.phase == "finish" and not self._payout_departed
            )
            central_amount = pot if show_pile else 0
        else:
            pot = self._pot_total()
            central_amount = snap.total_pot
        if central_amount > 0:
            draw_chip_pile(
                dst,
                POT_CHIP_POS,
                central_amount,
                seed=97,
                show_amount=False,
                scale=0.86,
                min_chips=5,
                max_chips=18,
            )
        if pot > 0:
            theme.text(
                dst,
                f"{'待结算底池' if self.table.hand_over and self.phase != 'finish' else '底池'} {pot}",
                (TABLE_C[0], BOARD_Y - 77),
                22,
                theme.GOLD,
                "center",
                shadow=True,
            )
            pots = (
                self.table.last_hand_result.pots
                if self.table.hand_over and self.table.last_hand_result is not None
                else snap.pots
            )
            if len(pots) > 1:
                side = " / ".join(str(p.amount) for p in pots)
                theme.text(
                    dst,
                    f"边池 {side}",
                    (TABLE_C[0], BOARD_Y - 55),
                    13,
                    theme.TEXT_DIM,
                    "center",
                )

    def _settled_board_cards(self) -> tuple[str, ...]:
        """只返回已完成发牌动画的完整街道，避免提前泄露牌面。"""
        if self.snap is None:
            return ()
        settled = max(0, self._visual_board - len(self.anim.cards))
        if settled >= 5:
            count = 5
        elif settled >= 4:
            count = 4
        elif settled >= 3:
            count = 3
        else:
            count = 0
        return tuple(self.snap.board[:count])

    def _human_hand_summary(self) -> HandSummary | None:
        """当前人类玩家的可见牌力；弃牌、出局或发牌中均不显示。"""
        if self.snap is None or self._settled_holes.get(HUMAN_SEAT, 0) < 2:
            return None
        player = self.snap.players[HUMAN_SEAT]
        if player.position is None or player.folded or player.hole_cards is None:
            return None
        return describe_holdem_hand(player.hole_cards, self._settled_board_cards())

    @staticmethod
    def _draw_card_highlight(
        dst: pygame.Surface,
        center: tuple[float, float],
        size: tuple[int, int],
        rotation: float = 0.0,
    ) -> None:
        """在牌外侧画可随底牌旋转的淡黄色描边，不修改牌面缓存。"""
        pad = 8
        frame = pygame.Surface(
            (size[0] + pad * 2, size[1] + pad * 2), pygame.SRCALPHA
        )
        rect = pygame.Rect(pad - 3, pad - 3, size[0] + 6, size[1] + 6)
        radius = max(7, round(size[0] * 0.13))
        pygame.draw.rect(
            frame,
            (*theme.CARD_HIGHLIGHT, 72),
            rect,
            width=7,
            border_radius=radius + 2,
        )
        pygame.draw.rect(
            frame,
            (*theme.CARD_HIGHLIGHT, 245),
            rect,
            width=3,
            border_radius=radius,
        )
        image = (
            pygame.transform.rotate(frame, rotation)
            if abs(rotation) > 0.01
            else frame
        )
        dst.blit(image, image.get_rect(center=(round(center[0]), round(center[1]))))

    def _draw_hand_strength_overlay(self, dst: pygame.Surface) -> None:
        """绘制当前牌型提示，并从一对起框出构成牌型的核心牌。"""
        summary = self._human_hand_summary()
        if summary is None or self.snap is None:
            return

        highlighted = summary.highlight_cards
        for index, code in enumerate(self._settled_board_cards()):
            if code not in highlighted:
                continue
            center = (TABLE_C[0] + (index - 2) * 70, BOARD_Y)
            self._draw_card_highlight(dst, center, cards.SIZE_BOARD)

        hole = self.snap.players[HUMAN_SEAT].hole_cards
        assert hole is not None
        for index, code in enumerate(hole):
            if code not in highlighted:
                continue
            rotation = -7.0 if index == 0 else 7.0
            self._draw_card_highlight(
                dst,
                self._hole_pos(HUMAN_SEAT, index),
                cards.SIZE_HOLE,
                rotation,
            )

        label = f"当前牌型 · {summary.label}"
        text_color = (
            theme.CARD_HIGHLIGHT
            if summary.category >= HandCategory.ONE_PAIR
            else theme.TEXT_DIM
        )
        width = max(184, theme.text_width(label, 16) + 30)
        pill = pygame.Surface((width, 30), pygame.SRCALPHA)
        pill_rect = pill.get_rect()
        pygame.draw.rect(pill, (22, 15, 10, 224), pill_rect, border_radius=15)
        pygame.draw.rect(
            pill,
            (*text_color, 190),
            pill_rect.inflate(-2, -2),
            width=1,
            border_radius=14,
        )
        theme.text(pill, label, pill_rect.center, 16, text_color, "center")
        dst.blit(pill, pill.get_rect(center=(TABLE_C[0], 735)))

    def _draw_seats(self, dst: pygame.Surface) -> None:
        snap = self.snap
        if snap is None:
            return
        for p in snap.players:
            anchor = self._seat_anchors[p.seat]
            in_hand = p.position is not None
            is_turn = in_hand and self.phase == "action" and p.seat == snap.acting_seat
            if p.seat in self.table.removed_seats:
                self._draw_removed_seat(dst, p, anchor)
                continue
            if is_turn:
                self._draw_turn_halo(dst, p.seat)
            if p.seat == HUMAN_SEAT:
                self._draw_human_seat(dst, p, in_hand)
                continue
            # 对手半身像
            bust = self.busts[p.seat]
            if not in_hand:
                state = FOLDED
            elif p.folded:
                state = FOLDED
            elif is_turn:
                state = THINKING
            elif p.seat in self._acted:
                state = ACTED
            else:
                state = IDLE
            bust.draw(dst, anchor, self._t, state)
            # 名牌压在躯干下缘
            compact = self.player_count >= 8
            plate = pygame.Rect(0, 0, 148 if compact else 176, 52 if compact else 56)
            plate.center = (
                round(anchor[0]),
                round(anchor[1] + (58 if compact else 74)),
            )
            plate_name = (
                p.name.split()[-1]
                if compact and " " in p.name
                else p.name
            )
            display_stack = self._display_stack(p.seat, p.stack)
            draw_name_plate(
                dst,
                plate,
                plate_name,
                display_stack,
                level=self.personas[p.seat - 1].level,
                active=is_turn,
                busted=not in_hand,
                status=(
                    f"¢ {display_stack} · 已弃牌"
                    if in_hand and p.folded
                    else (f"¢ {display_stack} · 思考中…" if is_turn else None)
                ),
                status_color=(
                    _FOLD_STATUS_C
                    if in_hand and p.folded
                    else (theme.AMBER_LIGHT if is_turn else None)
                ),
            )
            self._draw_hole_cards(dst, p)
            self._draw_bet_and_button(dst, p, in_hand)

    def _draw_removed_seat(self, dst: pygame.Surface, p, anchor: tuple[float, float]) -> None:
        """被移出的座位显示程序化 ``＋``；预约后显示下一手入座。"""
        request = self._pending_joins.get(p.seat)
        if request is not None:
            persona = persona_by_id(request.persona_id, self.seed)
            preview = self.busts.get(p.seat)
            if preview is not None and persona.persona_id == self.personas[p.seat - 1].persona_id:
                preview.draw(dst, anchor, self._t, ACTED)
            color = theme.TEAL
            title = f"{persona.display_name.split()[-1]} · 已预约"
            subtitle = f"下一手加入 · {request.amount_bb}BB"
        else:
            color = theme.AMBER_LIGHT
            radius = 43 if self.player_count < 8 else 36
            center = (round(anchor[0]), round(anchor[1]))
            pygame.draw.circle(dst, (32, 23, 15), center, radius)
            pygame.draw.circle(dst, theme.AMBER_DARK, center, radius, 3)
            pygame.draw.line(
                dst,
                color,
                (round(anchor[0] - radius * 0.45), center[1]),
                (round(anchor[0] + radius * 0.45), center[1]),
                5,
            )
            pygame.draw.line(
                dst,
                color,
                (center[0], round(anchor[1] - radius * 0.45)),
                (center[0], round(anchor[1] + radius * 0.45)),
                5,
            )
            title = "空座位"
            subtitle = "点击召回牌手"
        theme.text(
            dst,
            title,
            (anchor[0], anchor[1] + 54),
            16,
            color,
            "midtop",
            shadow=True,
        )
        theme.text(
            dst,
            subtitle,
            (anchor[0], anchor[1] + 78),
            13,
            theme.TEXT_DIM,
            "midtop",
        )

    def _draw_human_seat(self, dst: pygame.Surface, p, in_hand: bool) -> None:
        plate = pygame.Rect(0, 0, 220, 56)
        anchor = self._seat_anchors[HUMAN_SEAT]
        plate.center = (round(anchor[0]), round(anchor[1] - 18))
        display_stack = self._display_stack(p.seat, p.stack)
        draw_name_plate(
            dst,
            plate,
            "你",
            display_stack,
            active=(self.phase == "action" and p.seat == self.snap.acting_seat),
            busted=not in_hand,
            status=(
                f"¢ {display_stack} · 已弃牌"
                if in_hand and p.folded
                else (
                    f"¢ {display_stack} · 轮到你"
                    if self.phase == "action" and p.seat == self.snap.acting_seat
                    else None
                )
            ),
            status_color=(
                _FOLD_STATUS_C
                if in_hand and p.folded
                else (
                    theme.AMBER_LIGHT
                    if self.phase == "action" and p.seat == self.snap.acting_seat
                    else None
                )
            ),
        )
        self._draw_hole_cards(dst, p)
        self._draw_bet_and_button(dst, p, in_hand)

    def _draw_hole_cards(self, dst: pygame.Surface, p) -> None:
        shown = p.seat in self.table.shown_seats
        if p.position is None or (p.folded and not shown):
            return
        count = self._settled_holes.get(p.seat, 0)
        for i in range(min(2, count)):
            pos = self._hole_pos(p.seat, i)
            if p.seat == HUMAN_SEAT:
                assert p.hole_cards is not None
                surf = cards.card_surface(p.hole_cards[i], cards.SIZE_HOLE)
                rot = -7 if i == 0 else 7
                img = pygame.transform.rotate(surf, rot)
            else:
                face_up = ((self.reveal and not p.folded) or shown) and p.hole_cards is not None
                if (
                    face_up
                    and not shown
                    and self.phase == "showdown_reveal"
                ):
                    progress = self._showdown_reveal_progress
                    code = p.hole_cards[i] if progress >= 0.5 else "back"
                    surf = cards.card_surface(code, cards.SIZE_MINI)
                    fold = max(0.035, abs(1.0 - progress * 2.0))
                    surf = pygame.transform.smoothscale(
                        surf,
                        (max(2, round(surf.get_width() * fold)), surf.get_height()),
                    )
                else:
                    code = p.hole_cards[i] if face_up else "back"
                    surf = cards.card_surface(code, cards.SIZE_MINI)
                img = pygame.transform.rotate(surf, -4 if i == 0 else 4)
            dst.blit(img, img.get_rect(center=(round(pos[0]), round(pos[1]))))

    def _draw_turn_halo(self, dst: pygame.Surface, seat: int) -> None:
        """当前行动者周围的低亮度呼吸光圈。"""
        anchor = self._seat_anchors[seat]
        size = (
            (290, 158)
            if seat == HUMAN_SEAT
            else ((174, 196) if self.player_count >= 8 else (244, 270))
        )
        halo = pygame.Surface(size, pygame.SRCALPHA)
        pulse = (math.sin(self._t * 4.2) + 1.0) * 0.5
        alpha = round(72 + pulse * 48)
        outer = halo.get_rect().inflate(-8, -8)
        inner = halo.get_rect().inflate(-18, -18)
        pygame.draw.ellipse(halo, (*theme.AMBER_DARK, alpha // 2), outer, 6)
        pygame.draw.ellipse(halo, (*theme.AMBER, alpha), inner, 2)
        center = (
            round(anchor[0]),
            round(anchor[1] - 86 if seat == HUMAN_SEAT else anchor[1]),
        )
        dst.blit(halo, halo.get_rect(center=center))

    def _draw_action_echoes(self, dst: pygame.Surface) -> None:
        """在行动者靠近底池的一侧显示短暂动作确认。"""
        for echo in self._action_echoes:
            frac = min(1.0, echo.elapsed / echo.duration)
            fade_in = min(1.0, frac / 0.10)
            fade_out = min(1.0, (1.0 - frac) / 0.34)
            alpha = round(230 * fade_in * fade_out)
            if alpha <= 0:
                continue
            x, y = self._bet_pos(echo.seat)
            y -= 20 + 14 * fx.Ease.out_cubic(frac)
            font = theme.get_font(17)
            label = font.render(echo.label, True, echo.color)
            source = (
                theme.get_font(12).render(echo.source, True, theme.TEAL)
                if echo.source
                else None
            )
            content_w = max(label.get_width(), source.get_width() if source else 0)
            content_h = label.get_height() + (source.get_height() + 2 if source else 0)
            bubble = pygame.Surface(
                (content_w + 30, content_h + 16), pygame.SRCALPHA
            )
            pygame.draw.rect(
                bubble,
                (*theme.BG_PANEL, min(205, alpha)),
                bubble.get_rect(),
                border_radius=10,
            )
            pygame.draw.rect(
                bubble,
                (*echo.color, alpha),
                bubble.get_rect(),
                2,
                border_radius=10,
            )
            label.set_alpha(alpha)
            center_x = bubble.get_rect().centerx
            label_y = 8 if source else bubble.get_rect().centery - label.get_height() // 2
            bubble.blit(label, label.get_rect(midtop=(center_x, label_y)))
            if source is not None:
                source.set_alpha(alpha)
                bubble.blit(
                    source,
                    source.get_rect(midtop=(center_x, label_y + label.get_height() + 2)),
                )
            dst.blit(bubble, bubble.get_rect(center=(round(x), round(y))))

    def _draw_speech_bubbles(self, dst: pygame.Surface) -> None:
        """在对应头像靠桌内侧显示结算对白，延迟出现并平滑淡出。"""
        for speech in self._speech_bubbles:
            local = speech.elapsed - speech.delay
            if local < 0:
                continue
            fade_in = min(1.0, local / 0.18)
            fade_out = min(1.0, (speech.duration - local) / 0.55)
            alpha = round(245 * max(0.0, min(fade_in, fade_out)))
            if alpha <= 0:
                continue
            # 人格文案控制在两行，避免九人桌时横跨相邻座位。
            text_lines = (
                (speech.text[:16], speech.text[16:])
                if len(speech.text) > 16
                else (speech.text,)
            )
            text_lines = tuple(line for line in text_lines if line)
            width = min(
                286,
                max(theme.text_width(line, 15) for line in text_lines) + 28,
            )
            height = 22 * len(text_lines) + 18
            panel = pygame.Surface((width, height), pygame.SRCALPHA)
            pygame.draw.rect(
                panel,
                (*theme.BG_PANEL, min(226, alpha)),
                panel.get_rect(),
                border_radius=11,
            )
            pygame.draw.rect(
                panel,
                (*theme.AMBER_LIGHT, alpha),
                panel.get_rect(),
                2,
                border_radius=11,
            )
            for row, line in enumerate(text_lines):
                theme.text(
                    panel,
                    line,
                    (panel.get_width() // 2, 9 + row * 22),
                    15,
                    theme.TEXT,
                    "midtop",
                )
            panel.set_alpha(alpha)
            x, y = self._bet_pos(speech.seat)
            x = max(width / 2 + 12, min(PANEL_X - width / 2 - 12, x))
            y = max(height / 2 + 12, min(788 - height / 2, y - 72))
            dst.blit(panel, panel.get_rect(center=(round(x), round(y))))

    def _draw_bet_and_button(self, dst: pygame.Surface, p, in_hand: bool) -> None:
        display_stack = self._display_stack(p.seat, p.stack)
        if in_hand and display_stack > 0:
            draw_chip_pile(
                dst,
                self._stack_chip_pos(p.seat),
                display_stack,
                seed=p.seat * 19 + 5,
                show_amount=False,
                scale=0.58 if self.player_count >= 8 else 0.68,
                min_chips=4,
                max_chips=14,
            )
        if in_hand and p.bet > 0:
            draw_chip_pile(
                dst,
                self._bet_pos(p.seat),
                p.bet,
                seed=p.seat * 7 + 1,
                scale=0.78,
                min_chips=2,
                max_chips=12,
            )
        if in_hand and self.snap is not None and p.seat == self.snap.button_seat:
            bx, by = self._bet_pos(p.seat)
            ux, uy = _unit_to_center(self._seat_anchors[p.seat])
            cx, cy = bx - uy * 40, by + ux * 40
            pygame.draw.circle(dst, theme.TEXT, (round(cx), round(cy)), 13)
            pygame.draw.circle(dst, theme.AMBER, (round(cx), round(cy)), 13, 2)
            theme.text(dst, "D", (cx, cy), 16, theme.BG, "center")
        if in_hand and p.all_in:
            ax, ay = self._bet_pos(p.seat)
            theme.text(dst, "全下!", (ax, ay + 12), 15, theme.DANGER, "midtop", shadow=True)

    def _draw_action_bar(self, dst: pygame.Surface) -> None:
        legal = self._legal()
        if legal is None:
            return
        Panel.draw(dst, (36, 818, 1180, 76), alpha=215, border=theme.AMBER_DARK)
        theme.text(
            dst,
            "你的回合 · 调整下注后确认" if self._raise_open else "你的回合 · 请选择操作",
            (270 if self._raise_open else 615, 820),
            14,
            theme.AMBER_LIGHT,
            "midtop",
            shadow=True,
        )
        self.btn_fold.enabled = legal.can_fold
        if legal.can_check:
            self.btn_call.label = "过牌 C"
        elif legal.can_call:
            self.btn_call.label = f"跟注 {legal.call_amount} C"
        else:
            self.btn_call.label = "—"
        self.btn_call.enabled = legal.can_check or legal.can_call
        lo, hi, _ = self._wager_bounds(legal)
        self.btn_raise.enabled = lo is not None
        self.btn_raise.label = "确认 R" if self._raise_open else "加注 R"
        self.btn_fold.draw(dst)
        self.btn_call.draw(dst)
        self.btn_raise.draw(dst)
        if self._raise_open:
            self.slider.draw(dst)
            theme.text(
                dst,
                f"加注到 {self.slider.value}",
                (700, 826),
                17,
                theme.AMBER_LIGHT,
                "center",
                shadow=True,
            )
            for c in self._chips:
                c.draw(dst)
        else:
            theme.text(
                dst,
                "F 弃牌 · C 过牌/跟注 · R 加注",
                (700, 856),
                15,
                theme.TEXT_DIM,
                "center",
            )

    def _draw_info_panel(self, dst: pygame.Surface) -> None:
        Panel.draw(dst, (PANEL_X, 0, 1600 - PANEL_X, 900), alpha=235)
        pygame.draw.line(dst, theme.AMBER_DARK, (PANEL_X, 0), (PANEL_X, 900), 2)
        x = PANEL_X + 18
        snap = self.snap
        theme.text(dst, "牌局信息", (x, 18), 20, theme.AMBER_LIGHT)
        if snap is not None:
            pot = self._pot_total()
            if self.table.hand_over and self.table.last_hand_result is not None:
                pot = sum(p.amount for p in self.table.last_hand_result.pots)
            theme.text(dst, f"底池 ¢ {pot}", (x, 50), 24, theme.GOLD)
            theme.text(dst, STREET_LABEL[snap.street], (x + 150, 56), 16, theme.TEAL)
            if self.table.current_straddle_amount:
                theme.text(
                    dst,
                    f"STR {self.table.current_straddle_amount}",
                    (PANEL_X + 358, 57),
                    13,
                    theme.AMBER_LIGHT,
                    "topright",
                )
        # 独立保留上一手结算，不再让它被后续动作迅速顶出日志。
        result_box = pygame.Rect(x - 5, 84, 344, 100)
        pygame.draw.rect(dst, (27, 19, 13), result_box, border_radius=8)
        pygame.draw.rect(dst, theme.AMBER_DARK, result_box, 1, border_radius=8)
        theme.text(dst, "上一手结果", (x + 5, 91), 14, theme.AMBER_LIGHT)
        result_lines = list(self._last_settlement_lines[:4])
        if len(self._last_settlement_lines) > 4:
            result_lines[-1] = f"…另有 {len(self._last_settlement_lines) - 3} 项逐池明细"
        if not result_lines:
            result_lines = ["尚无已结算牌局"]
        for row, line in enumerate(result_lines):
            compact = line if len(line) <= 29 else line[:28] + "…"
            theme.text(
                dst,
                compact,
                (x + 5, 112 + row * 18),
                12,
                theme.GOLD if row == 0 and self._last_settlement_lines else theme.TEXT_DIM,
            )

        # 行动日志
        theme.text(dst, "行动记录", (x, 193), 17, theme.TEXT_DIM)
        strategy_text, strategy_color = self._ai_strategy_status()
        theme.text(
            dst,
            strategy_text,
            (PANEL_X + 358, 196),
            12,
            strategy_color,
            "topright",
        )
        crowded = self.player_count >= 8
        self.log.draw(
            dst,
            (x, 215, 340, 108 if crowded else 205),
            max_lines=4 if crowded else 7,
            size=14 if crowded else 15,
        )
        # 玩家速览
        players_title_y = 326 if crowded else 436
        theme.text(dst, "玩家", (x, players_title_y), 17, theme.TEXT_DIM)
        y = players_title_y + 26
        if snap is not None:
            for p in snap.players:
                if p.seat in self.table.removed_seats:
                    mark, col = (
                        ("待加入", theme.TEAL)
                        if p.seat in self._pending_joins
                        else ("空位", theme.TEXT_DIM)
                    )
                elif p.position is None:
                    mark, col = "出局", theme.TEXT_DIM
                elif p.folded:
                    mark, col = "弃牌", theme.TEXT_DIM
                elif self.phase == "action" and p.seat == snap.acting_seat:
                    mark, col = "行动中", theme.AMBER_LIGHT
                elif p.all_in:
                    mark, col = "全下", theme.DANGER
                else:
                    mark, col = "", theme.TEXT
                name = p.name if len(p.name) <= 15 else p.name[:14] + "…"
                row_size = 14 if crowded else 16
                theme.text(dst, name, (x, y), row_size, col)
                display_stack = self._display_stack(p.seat, p.stack)
                theme.text(dst, f"¢{display_stack}", (x + 150, y), row_size, theme.GOLD if col is theme.TEXT else col)
                if mark:
                    theme.text(dst, mark, (x + 232, y), 13 if crowded else 15, col)
                y += 28 if crowded else 30
        # GTO 辅助面板(M4)
        dock = pygame.Rect(x - 6, 660, 348, 200)
        Panel.draw(dst, dock, alpha=160, border=theme.TEAL_DARK)
        self._draw_gto_panel(dst, dock)

    def _episode_text(self) -> str:
        """供 UI 使用的训练手数文本。"""
        if isinstance(self._nfsp_episode, int):
            return f"{self._nfsp_episode:,}"
        return "未知"

    def _ai_strategy_status(self) -> tuple[str, tuple[int, int, int]]:
        """右侧行动区显示最近一次 AI 的真实策略来源。"""
        if self._last_ai_source == "net:nfsp":
            return (
                f"NFSP · 实验HU池 · {self._episode_text()}手",
                theme.TEAL,
            )
        if self._last_ai_source == "scripted":
            return "脚本动作", theme.TEXT_DIM
        if self._last_ai_source == "heuristic:persona":
            failed = self._last_ai_detail not in ("", "not_eligible")
            if failed:
                return "NFSP 降级 · 人格AI", theme.AMBER_LIGHT
            return "翻前/多人 · 人格AI", theme.TEXT_DIM
        if self._nfsp_policy is not None:
            return (
                f"NFSP 待命 · 实验HU池 · {self._episode_text()}手",
                theme.TEAL,
            )
        return "NFSP 不可用 · 人格AI", theme.AMBER_LIGHT

    # ------------------------------------------------------------ GTO 辅助

    def _current_hint(self) -> tuple[Hint | None, bool]:
        """取当前建议;返回 (建议, 是否过期)。

        只在轮到人类行动时计算(按 ``id(snap)`` 缓存:快照在每次动作后
        重建,因此天然满足"街道/动作变化才重算,不逐帧算");非人类回合
        返回上一条建议并标记过期,供面板淡显。
        """
        if not self.gto_on or self.snap is None:
            return None, False
        me = self.snap.players[HUMAN_SEAT]
        if me.position is None or me.hole_cards is None:
            return None, False  # 人类已出局/不在本手
        if self._human_to_act() and self.snap.legal_actions is not None:
            key = id(self.snap)
            if key not in self._hint_cache:
                self._hint_cache.clear()  # 旧快照不再回访,只留当前
                try:
                    self._hint_cache[key] = self._advisor.hint(
                        self.snap, HUMAN_SEAT, self._villain_styles
                    )
                except (KeyError, ValueError):
                    # 图表/快照边界异常不应让牌桌渲染崩溃；面板本帧显示等待。
                    return None, False
            return self._hint_cache[key], False
        if self._hint_cache:
            return next(iter(self._hint_cache.values())), True
        return None, False

    def _draw_gto_panel(self, dst: pygame.Surface, dock: pygame.Rect) -> None:
        tx = dock.left + 12
        theme.text(dst, "GTO 辅助", (tx, dock.top + 8), 18, theme.TEAL)
        theme.text(dst, "[G] 开关", (dock.right - 10, dock.top + 12), 13, theme.TEXT_DIM, "topright")
        if not self.gto_on:
            theme.text(dst, "已关闭 · 按 G 重新开启", dock.center, 15, theme.TEXT_DIM, "center")
            return
        hint, stale = self._current_hint()
        if hint is None:
            theme.text(dst, "等待你的行动…", dock.center, 15, theme.TEXT_DIM, "center")
            return
        dim = theme.TEXT_DIM if stale else theme.TEXT
        # 三条频率条:弃牌 / 过牌·跟注 / 下注·加注
        f = hint.action_freqs
        bars = (
            ("弃牌", f.get("FOLD", 0.0), _GTO_FOLD_C),
            ("过/跟", f.get("CHECK", 0.0) + f.get("CALL", 0.0), _GTO_CALL_C),
            ("下注/加注", f.get("BET", 0.0) + f.get("RAISE", 0.0), _GTO_BET_C),
        )
        by = dock.top + 36
        for label, frac, color in bars:
            theme.text(dst, label, (tx, by), 13, theme.TEXT_DIM)
            track = pygame.Rect(tx + 64, by + 2, 178, 12)
            pygame.draw.rect(dst, theme.BG, track, border_radius=4)
            fill = pygame.Rect(track)
            fill.width = round(track.width * min(1.0, frac))
            if fill.width > 0:
                pygame.draw.rect(dst, color, fill, border_radius=4)
            pygame.draw.rect(dst, theme.FELT_EDGE, track, 1, border_radius=4)
            theme.text(dst, f"{frac * 100:.0f}%", (track.right + 8, by), 13, dim)
            by += 24
        # 胜率 / 底池赔率
        eq = f"胜率 {hint.equity * 100:.1f}%" if hint.equity is not None else "胜率 —"
        theme.text(dst, eq, (tx, by + 2), 15, theme.GOLD)
        po = (
            f"底池赔率 {hint.pot_odds * 100:.1f}%"
            if hint.pot_odds is not None
            else "底池赔率 —"
        )
        theme.text(dst, po, (tx + 150, by + 2), 15, theme.GOLD)
        # 来源 + 首条说明(过长截断)
        note = hint.notes[0] if hint.notes else ""
        theme.text(dst, note[:22], (tx, by + 26), 12, theme.TEXT_DIM)
        theme.text(dst, hint.source, (dock.right - 10, by + 26), 12, theme.TEAL, "topright")
        if stale:
            theme.text(
                dst, "(上一条建议)", (dock.right - 10, dock.bottom - 22), 12,
                theme.TEXT_DIM, "bottomright",
            )

    def _draw_banner(self, dst: pygame.Surface) -> None:
        if self.banner is None:
            return
        title, sub = self.banner
        # 横幅延迟淡入
        a = min(255, int(255 * self._phase_t / 0.35)) if self._phase_t < 1 else 255
        panel = pygame.Rect(0, 0, 500, 112)
        panel.center = (TABLE_C[0], 285)
        s = pygame.Surface(panel.size, pygame.SRCALPHA)
        pygame.draw.rect(s, (*theme.BG_PANEL, min(230, a)), s.get_rect(), border_radius=14)
        pygame.draw.rect(s, (*theme.AMBER, a), s.get_rect(), 2, border_radius=14)
        dst.blit(s, panel)
        theme.text(dst, title, panel.center, 30, theme.AMBER_LIGHT, "center", shadow=True)
        theme.text(dst, sub, (panel.centerx, panel.centery + 42), 17, theme.TEXT_DIM, "center")
        if self._bust_dialog is not None:
            pass  # 出局处置框接管(绘制于横幅之上)
        elif self._bust_queue:
            theme.text(
                dst,
                "筹码推送完成后处理归零座位…",
                (TABLE_C[0], 570),
                17,
                theme.TEXT_DIM,
                "center",
            )
        elif self.table.game_over and self._pending_joins:
            self.btn_next.label = "召回并继续"
            self.btn_next.draw(dst)
        elif self.table.game_over:
            theme.text(dst, "你赢下了整家酒馆!", (TABLE_C[0], 470), 26, theme.GOLD, "center", shadow=True)
            self.btn_rebuy.label = "再来一局"
            self.btn_rebuy.draw(dst)
            self.btn_menu.draw(dst)
        else:
            self.btn_next.label = "下一手 R"
            self.btn_next.draw(dst)
        if self._can_human_show():
            self.btn_show.draw(dst)
        elif HUMAN_SEAT in self.table.shown_seats:
            theme.text(
                dst,
                "已 SHOW 手牌",
                (790, 582),
                15,
                theme.TEAL,
                "center",
                shadow=True,
            )

    def _draw_pause(self, dst: pygame.Surface) -> None:
        veil = pygame.Surface((1600, 900), pygame.SRCALPHA)
        veil.fill((0, 0, 0, 150))
        dst.blit(veil, (0, 0))
        panel = pygame.Rect(0, 0, 320, 260)
        panel.center = (800, 430)
        Panel.draw(dst, panel, alpha=240, border=theme.AMBER)
        theme.text(dst, "暂 停", (800, 330), 34, theme.AMBER_LIGHT, "center", shadow=True)
        self.btn_resume.draw(dst)
        self.btn_quit_menu.draw(dst)


# ------------------------------------------------------------ 席位/打法配置模态框


class _StylePicker:
    """八类打法的共享选择器：两行矩阵 + 一行参数化说明。"""

    def __init__(
        self,
        scene: TableScene,
        origin: tuple[int, int],
        selected_key: str,
        *,
        columns: int = 4,
        button_width: int = 170,
    ) -> None:
        self.scene = scene
        self.x, self.y = origin
        self.columns = columns
        self.button_width = button_width
        self.selected_key = selected_key
        self.presets = scene._style_catalog
        self.buttons = [
            Button(
                (
                    self.x + (index % columns) * (button_width + 10),
                    self.y + (index // columns) * 48,
                    button_width,
                    39,
                ),
                preset.label,
                lambda key=preset.key: self.select(key),
                size=15,
            )
            for index, preset in enumerate(self.presets)
        ]

    def select(self, key: str) -> None:
        if key not in {preset.key for preset in self.presets}:
            raise ValueError(f"未知打法: {key}")
        self.selected_key = key

    @property
    def selected(self) -> StylePreset:
        return next(p for p in self.presets if p.key == self.selected_key)

    @property
    def width(self) -> int:
        return self.columns * self.button_width + (self.columns - 1) * 10

    def handle_event(self, ev: pygame.event.Event) -> None:
        for button in self.buttons:
            button.handle_event(ev)

    def draw(self, dst: pygame.Surface) -> None:
        for preset, button in zip(self.presets, self.buttons):
            button.selected = preset.key == self.selected_key
            button.draw(dst)
        preset = self.selected
        desc = pygame.Rect(self.x, self.y + 100, self.width, 76)
        pygame.draw.rect(dst, (31, 22, 15), desc, border_radius=9)
        pygame.draw.rect(dst, theme.FELT_EDGE, desc, 1, border_radius=9)
        theme.text(dst, preset.description, (desc.left + 14, desc.top + 12), 15, theme.TEXT)
        theme.text(
            dst,
            f"入池 {preset.style.vpip * 100:.0f}%  ·  PFR {preset.style.pfr * 100:.0f}%"
            f"  ·  激进度 {preset.style.aggression:.1f}",
            (desc.left + 14, desc.top + 43),
            14,
            theme.TEAL,
        )


class _BustDialog:
    """两手间的出局处置框。

    买入统一以 BB 编辑并显示筹码换算；AI 可移出，人类座位只能
    重新买入或结束本局。框内明确展示座位、当前筹码与操作后果。
    """

    PANEL = (150, 50, 1300, 800)

    def __init__(self, scene: TableScene, seat: int) -> None:
        self.scene = scene
        self.seat = seat
        self.is_human = seat == HUMAN_SEAT
        self.bb = scene.table.config.big_blind
        self.lo_bb = MIN_REBUY_BB
        self.hi_bb = MAX_REBUY_BB
        if self.is_human:
            self.name = "你"
            self.title = "你已出局"
            self._subtitle = "人类座位不可移出：重新买入，或结束本局"
            self.icon: Bust | None = None
            self.style_picker: _StylePicker | None = None
        else:
            persona = scene.personas[seat - 1]
            self.name = persona.display_name
            self.title = f"{self.name} 已出局"
            self._subtitle = "补充配额时可同时更换打法；移出后座位会变为可点击空位"
            self.icon = Bust(persona.species, seed=seat, scale=0.58)
            self.style_picker = _StylePicker(
                scene,
                (570, 278),
                persona.style_key,
                button_width=180,
            )
        self.preset_btns = [
            Button(
                (606 + i * 108, 520, 98, 38),
                f"{mult}BB",
                lambda value=mult: self._pick(value),
                size=17,
            )
            for i, mult in enumerate((50, 100, 200))
        ]
        self.field = NumberField(
            (718, 572, 132, 42),
            "自定义",
            100,
            minimum=self.lo_bb,
            maximum=self.hi_bb,
        )
        self.btn_rebuy = Button(
            (490, 752, 270, 52),
            "补充配额并继续",
            lambda: scene._resolve_bust_rebuy(self),
            size=20,
        )
        if self.is_human:
            alt_label = "结束本局"
        elif len(scene.table.active_seats) < 2:
            alt_label = "移出并散场"
        else:
            alt_label = "移出并继续"
        self.btn_alt = Button(
            (790, 752, 270, 52),
            alt_label,
            lambda: scene._resolve_bust_alt(self),
            size=20,
            danger=True,
        )
        self._refresh_validity()

    @property
    def style_key(self) -> str:
        """AI 当前选中的打法 key；人类返回空字符串。"""
        return "" if self.style_picker is None else self.style_picker.selected_key

    @property
    def style_label(self) -> str:
        return "" if self.style_picker is None else self.style_picker.selected.label

    def _pick(self, amount_bb: int) -> None:
        """预设档位:写入输入框并标记合法。"""
        self.field.set_value(amount_bb)
        self._refresh_validity()

    @property
    def amount_bb(self) -> int | None:
        """当前合法的买入 BB 数。"""
        try:
            value = int(self.field.text)
        except ValueError:
            return None
        return value if self.lo_bb <= value <= self.hi_bb else None

    @property
    def amount(self) -> int | None:
        """当前合法的买入筹码；非法时为 ``None``。"""
        value = self.amount_bb
        return None if value is None else value * self.bb

    def _refresh_validity(self) -> None:
        self.field.valid = self.amount_bb is not None
        self.btn_rebuy.enabled = self.field.valid

    def handle_event(self, ev: pygame.event.Event) -> None:
        """分发事件给内部控件(ESC 仅用于输入框失焦,不关闭本框)。"""
        # 先提交输入，确保点击确认时读取到的是刚编辑的值。
        self.field.handle_event(ev)
        self._refresh_validity()
        for b in self.preset_btns:
            b.handle_event(ev)
        if self.style_picker is not None:
            self.style_picker.handle_event(ev)
        self.btn_rebuy.handle_event(ev)
        self.btn_alt.handle_event(ev)

    def draw(self, dst: pygame.Surface, t: float) -> None:
        veil = pygame.Surface((1600, 900), pygame.SRCALPHA)
        veil.fill((0, 0, 0, 178))
        dst.blit(veil, (0, 0))
        Panel.draw(dst, self.PANEL, alpha=245, border=theme.AMBER)
        theme.text(dst, self.title, (800, 88), 31, theme.AMBER_LIGHT, "center", shadow=True)
        theme.text(dst, self._subtitle, (800, 126), 15, theme.TEXT_DIM, "center")

        status = pygame.Rect(190, 156, 1220, 74)
        pygame.draw.rect(dst, (35, 24, 15), status, border_radius=10)
        pygame.draw.rect(dst, theme.FELT_EDGE, status, 1, border_radius=10)
        theme.text(
            dst,
            f"座位 {self.seat + 1} · {self.name}",
            (status.left + 18, status.top + 16),
            18,
            theme.TEXT,
        )
        theme.text(
            dst,
            f"当前筹码  {self.scene.table.stacks[self.seat]}",
            (status.right - 18, status.top + 16),
            18,
            theme.DANGER,
            "topright",
        )
        theme.text(
            dst,
            f"第 {self.scene.table.hand_id} 手已结算 · 下一手尚未发牌",
            (status.centerx, status.bottom - 14),
            14,
            theme.TEAL,
            "midbottom",
        )

        if self.icon is not None:
            self.icon.draw(dst, (350, 422), t, FOLDED)
        else:
            theme.text(dst, "你的座位", (350, 398), 23, theme.GOLD, "center", shadow=True)
            theme.text(dst, "仍会为你保留", (350, 432), 15, theme.TEXT_DIM, "center")

        if self.style_picker is not None:
            theme.text(dst, "更换打法", (570, 250), 16, theme.AMBER_LIGHT)
            self.style_picker.draw(dst)
        else:
            human_note = pygame.Rect(520, 278, 790, 176)
            pygame.draw.rect(dst, (31, 22, 15), human_note, border_radius=10)
            pygame.draw.rect(dst, theme.FELT_EDGE, human_note, 1, border_radius=10)
            theme.text(
                dst,
                "人类座位不配置 AI 打法",
                (human_note.centerx, human_note.top + 48),
                22,
                theme.TEXT,
                "center",
            )
            theme.text(
                dst,
                "补充配额后，你会携所选筹码进入下一手",
                (human_note.centerx, human_note.top + 92),
                16,
                theme.TEXT_DIM,
                "center",
            )

        theme.text(dst, "预设配额", (590, 530), 14, theme.TEXT_DIM, "midright")
        for value, b in zip((50, 100, 200), self.preset_btns):
            b.label = (
                f"» {value}BB"
                if self.amount_bb == value
                else f"{value}BB"
            )
            b.draw(dst)
        self.field.draw(dst)
        theme.text(
            dst, "BB", (self.field.rect.right + 8, self.field.rect.centery), 16, theme.GOLD, "midleft"
        )
        if self.amount is None:
            conversion = f"请输入 {self.lo_bb}–{self.hi_bb} 的整数 BB"
            conversion_color = theme.DANGER
        else:
            conversion = f"= {self.amount:,} 筹码 · 下一手生效"
            conversion_color = theme.GOLD
        theme.text(dst, conversion, (784, 628), 15, conversion_color, "center")

        if self.is_human:
            consequence = "结束本局：返回主菜单；其他座位不会被单独移出"
        elif len(self.scene.table.active_seats) < 2:
            consequence = "移出后在局者不足两人：牌桌散场并返回主菜单"
        else:
            remaining = len(self.scene.table.active_seats)
            consequence = f"移出后：此座显示 ＋，可稍后召回任一 AI；其余 {remaining} 人继续"
        theme.text(
            dst,
            "补充配额：保留牌手身份，并按所选打法与筹码进入下一手",
            (800, 672),
            15,
            theme.TEXT,
            "center",
        )
        theme.text(dst, consequence, (800, 704), 15, theme.TEXT_DIM, "center")

        waiting = len(self.scene._bust_queue)
        if waiting:
            theme.text(
                dst,
                f"完成后还需处置 {waiting} 个归零座位",
                (800, 730),
                14,
                theme.TEAL,
                "center",
            )
        self._refresh_validity()
        self.btn_rebuy.draw(dst)
        self.btn_alt.draw(dst)
        theme.text(
            dst,
            "仅能在两手之间处置 · 必须做出决定（ESC 不可跳过）",
            (800, 828),
            13,
            theme.TEXT_DIM,
            "center",
        )


class _SeatJoinDialog:
    """空位召回面板：身份、打法和买入相互独立配置。"""

    PANEL = (90, 42, 1420, 816)

    def __init__(self, scene: TableScene, seat: int) -> None:
        self.scene = scene
        self.seat = seat
        self.bb = scene.table.config.big_blind
        self.catalog = scene._persona_catalog
        self.occupied = scene._occupied_persona_ids(seat)
        pending = scene._pending_joins.get(seat)
        current = scene.personas[seat - 1]
        self.persona_id = pending.persona_id if pending else current.persona_id
        if self.persona_id in self.occupied:
            # 原身份可能已被另一个空位预约；打开面板时直接落到第一名
            # 可用角色，避免默认选中禁用按钮、还要用户额外排查原因。
            self.persona_id = next(
                persona.persona_id
                for persona in self.catalog
                if persona.persona_id not in self.occupied
            )
        self.style_picker = _StylePicker(
            scene,
            (650, 258),
            pending.style_key if pending else current.style_key,
            button_width=175,
        )
        self.icon = Bust(
            persona_by_id(self.persona_id, scene.seed).species,
            seed=seat * 101,
            scale=0.62,
        )
        self.identity_btns = [
            Button(
                (
                    142 + (index % 5) * 88,
                    210 + (index // 5) * 44,
                    80,
                    36,
                ),
                persona.display_name.split()[-1],
                lambda key=persona.persona_id: self._select_persona(key),
                size=13,
                enabled=persona.persona_id not in self.occupied,
            )
            for index, persona in enumerate(self.catalog)
        ]
        amount_bb = pending.amount_bb if pending else 100
        self.preset_btns = [
            Button(
                (732 + index * 108, 574, 96, 38),
                f"{value}BB",
                lambda bb=value: self._pick(bb),
                size=16,
            )
            for index, value in enumerate((50, 100, 200))
        ]
        self.field = NumberField(
            (844, 628, 132, 42),
            "自定义",
            amount_bb,
            minimum=MIN_REBUY_BB,
            maximum=MAX_REBUY_BB,
        )
        self.btn_ai_mode = Button(
            (142, 136, 190, 42), "AI 牌手", None, size=18, selected=True
        )
        self.btn_friend_mode = Button(
            (346, 136, 238, 42),
            "好友加入 · 后续开放",
            None,
            size=16,
            enabled=False,
        )
        self.btn_confirm = Button(
            (1028, 768, 270, 52),
            "预约下一手入座",
            lambda: scene._queue_seat_join(self),
            size=20,
        )
        self.btn_cancel = Button(
            (724, 768, 270, 52),
            "取消并返回牌桌",
            lambda: setattr(scene, "_seat_dialog", None),
            size=19,
        )
        self.btn_cancel_booking = Button(
            (252, 768, 260, 52),
            "取消当前召回",
            lambda: scene._cancel_seat_join(seat),
            size=18,
            danger=True,
            enabled=pending is not None,
        )
        self._refresh_validity()

    @property
    def persona(self) -> Persona:
        return persona_by_id(self.persona_id, self.scene.seed)

    def _select_persona(self, persona_id: str) -> None:
        if persona_id in self.occupied:
            return
        self.persona_id = persona_id
        self.icon = Bust(
            self.persona.species,
            seed=self.seat * 101 + len(persona_id),
            scale=0.62,
        )

    def _pick(self, amount_bb: int) -> None:
        self.field.set_value(amount_bb)
        self._refresh_validity()

    @property
    def amount_bb(self) -> int | None:
        try:
            value = int(self.field.text)
        except ValueError:
            return None
        return value if MIN_REBUY_BB <= value <= MAX_REBUY_BB else None

    @property
    def request(self) -> _SeatJoinRequest | None:
        amount_bb = self.amount_bb
        if amount_bb is None or self.persona_id in self.occupied:
            return None
        return _SeatJoinRequest(
            seat=self.seat,
            persona_id=self.persona_id,
            style_key=self.style_picker.selected_key,
            amount_bb=amount_bb,
        )

    def _refresh_validity(self) -> None:
        self.field.valid = self.amount_bb is not None
        self.btn_confirm.enabled = self.request is not None

    def handle_event(self, ev: pygame.event.Event) -> None:
        was_focused = self.field.focused
        self.field.handle_event(ev)
        if ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE and not was_focused:
            self.scene._seat_dialog = None
            return
        for button in self.identity_btns:
            button.handle_event(ev)
        self.style_picker.handle_event(ev)
        for button in self.preset_btns:
            button.handle_event(ev)
        self.btn_ai_mode.handle_event(ev)
        self.btn_friend_mode.handle_event(ev)
        self.btn_confirm.handle_event(ev)
        self.btn_cancel.handle_event(ev)
        self.btn_cancel_booking.handle_event(ev)
        self._refresh_validity()

    def draw(self, dst: pygame.Surface, t: float) -> None:
        veil = pygame.Surface((1600, 900), pygame.SRCALPHA)
        veil.fill((0, 0, 0, 184))
        dst.blit(veil, (0, 0))
        Panel.draw(dst, self.PANEL, alpha=247, border=theme.AMBER)
        theme.text(
            dst,
            f"召回牌手 · 座位 {self.seat + 1}",
            (800, 80),
            32,
            theme.AMBER_LIGHT,
            "center",
            shadow=True,
        )
        theme.text(
            dst,
            "本手暂时冻结 · 确认后在下一手发牌前正式入座",
            (800, 116),
            15,
            theme.TEXT_DIM,
            "center",
        )
        self.btn_ai_mode.draw(dst)
        self.btn_friend_mode.draw(dst)

        theme.text(dst, "选择身份", (142, 188), 17, theme.AMBER_LIGHT)
        for persona, button in zip(self.catalog, self.identity_btns):
            button.selected = persona.persona_id == self.persona_id
            button.draw(dst)
        self.icon.draw(dst, (360, 488), t, IDLE)
        theme.text(
            dst,
            self.persona.display_name,
            (360, 605),
            23,
            theme.GOLD,
            "center",
            shadow=True,
        )
        level = {"fish": "入门", "reg": "常客", "shark": "高手"}.get(
            self.persona.level, self.persona.level
        )
        theme.text(
            dst,
            f"{self.persona.species} · {level} AI",
            (360, 636),
            14,
            theme.TEAL,
            "center",
        )
        story = self.persona.backstory
        theme.text(dst, story[:24], (360, 674), 13, theme.TEXT_DIM, "center")
        theme.text(dst, story[24:48], (360, 696), 13, theme.TEXT_DIM, "center")

        theme.text(dst, "选择打法", (650, 230), 17, theme.AMBER_LIGHT)
        self.style_picker.draw(dst)
        theme.text(dst, "初始资金", (650, 574), 16, theme.AMBER_LIGHT)
        for value, button in zip((50, 100, 200), self.preset_btns):
            button.selected = self.amount_bb == value
            button.draw(dst)
        self.field.draw(dst)
        theme.text(
            dst,
            "BB",
            (self.field.rect.right + 8, self.field.rect.centery),
            16,
            theme.GOLD,
            "midleft",
        )
        if self.amount_bb is None:
            conversion = f"请输入 {MIN_REBUY_BB}–{MAX_REBUY_BB} 的整数 BB"
            color = theme.DANGER
        else:
            conversion = f"= {self.amount_bb * self.bb:,} 筹码 · 下一手生效"
            color = theme.GOLD
        # 与左侧自定义输入框留出足够空隙，四位 BB 和五位筹码均不重叠。
        theme.text(dst, conversion, (1190, 650), 15, color, "center")
        theme.text(
            dst,
            "“好友加入”已预留同一空位/资金契约；当前版本仍为离线 AI。",
            (1065, 696),
            14,
            theme.TEXT_DIM,
            "center",
        )

        self._refresh_validity()
        self.btn_cancel_booking.draw(dst)
        self.btn_cancel.draw(dst)
        self.btn_confirm.draw(dst)
        theme.text(
            dst,
            "ESC 可取消 · 已在桌上的身份不可重复选择",
            (800, 838),
            13,
            theme.TEXT_DIM,
            "center",
        )
