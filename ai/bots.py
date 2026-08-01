"""启发式 AI 机器人:风格 + 等级驱动的决策。

三个等级:
- ``fish``(新手):只用风格原始频率,无视底池赔率,跟注站倾向;
- ``reg``(常客):加入位置意识、底池赔率校验(少量 MC 试验)、标准尺度;
- ``shark``(鲨鱼):再加入价值/诈唬分配、牌面结构感知与简单剥削钩子。

所有机器人只接收 ``snapshot(perspective=自己座位)`` 的视角快照,
``decide`` 会校验快照中没有泄露其他玩家的底牌,防止作弊。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from itertools import combinations
from typing import Protocol

from engine.state import (
    Action,
    ActionType,
    GameSnapshot,
    LegalActions,
    PlayerState,
    Position,
    Street,
)

from .equity import DECK, equity_vs_random, hand_strength, preflop_strength
from .styles import PlayerStyle, style_by_key

LEVELS = ("fish", "reg", "shark")

# 各等级的蒙特卡洛试验次数(竞技场 --fast 可再砍半)
_MC_TRIALS = {"fish": 0, "reg": 120, "shark": 220}

# 位置松紧系数(乘在频率阈值上):越靠后越松。按枚举名装配可自然覆盖
# 7–9 人桌新增的 UTG1/LJ/HJ，同时对未来未知标签保留中性兜底。
_REG_POS_FACTOR = {
    "UTG": 0.65, "UTG1": 0.72, "UTG2": 0.78, "MP": 0.82, "LJ": 0.92, "HJ": 1.05,
    "CO": 1.17, "BTN": 1.35, "SB": 1.05, "BB": 1.0,
}
_SHARK_POS_FACTOR = {
    "UTG": 0.60, "UTG1": 0.68, "UTG2": 0.74, "MP": 0.78, "LJ": 0.90, "HJ": 1.08,
    "CO": 1.22, "BTN": 1.45, "SB": 1.10, "BB": 1.0,
}
_POS_FACTOR = {
    "fish": {position: 1.0 for position in Position},
    "reg": {
        position: _REG_POS_FACTOR.get(position.name, 1.0) for position in Position
    },
    "shark": {
        position: _SHARK_POS_FACTOR.get(position.name, 1.0) for position in Position
    },
}


class Bot(Protocol):
    """AI 机器人协议:给定(己方视角)快照,返回一个合法动作。"""

    def decide(self, snapshot: GameSnapshot, rng: random.Random | None = None) -> Action:
        ...


# ------------------------------------------------------------ 翻牌前阈值标定

_PREFLOP_SCORES: list[float] | None = None


def _preflop_cutoff(freq: float) -> float:
    """返回强度阈值,使随机两牌强度 >= 阈值的频率约为 ``freq``。

    通过对全部 1326 种两牌组合的启发式强度排序标定,首次调用时
    惰性计算并缓存。
    """
    global _PREFLOP_SCORES
    if _PREFLOP_SCORES is None:
        _PREFLOP_SCORES = sorted(
            preflop_strength(a, b) for a, b in combinations(DECK, 2)
        )
    freq = min(1.0, max(0.001, freq))
    idx = min(len(_PREFLOP_SCORES) - 1, int((1 - freq) * len(_PREFLOP_SCORES)))
    return _PREFLOP_SCORES[idx]


# ------------------------------------------------------------ 快照辅助

def _check_perspective(snapshot: GameSnapshot, seat: int) -> PlayerState:
    """校验快照确为 ``seat`` 的视角:自己必须看得到底牌,其他人必须看不到。"""
    me = next(p for p in snapshot.players if p.seat == seat)
    if me.hole_cards is None:
        raise ValueError(f"机器人(seat {seat})看不到自己的底牌")
    for p in snapshot.players:
        if p.seat != seat and p.hole_cards is not None:
            raise ValueError(
                f"快照泄露了 seat {p.seat} 的底牌;机器人只准使用 perspective 快照"
            )
    return me


def _pot_now(snapshot: GameSnapshot) -> int:
    """当前底池(含本街道尚未收池的下注)。"""
    return snapshot.total_pot + sum(p.bet for p in snapshot.players)


def _board_scare_factor(board: tuple[str, ...]) -> float:
    """牌面危险度(0..~0.3):同花/顺子可能性越高越吓人。"""
    if len(board) < 3:
        return 0.0
    suits = [c[1] for c in board]
    flush = max(suits.count(s) for s in set(suits)) >= 3
    vals = sorted({"23456789TJQKA".index(c[0]) for c in board})
    run = max_run = 1
    for a, b in zip(vals, vals[1:]):
        run = run + 1 if b - a <= 2 else 1
        max_run = max(max_run, run)
    straighty = max_run >= 3
    return 0.15 * flush + 0.10 * straighty


# ------------------------------------------------------------ 机器人本体

@dataclass
class HeuristicBot:
    """风格 + 等级驱动的启发式机器人。

    :param style: 连续风格参数。
    :param level: ``"fish"`` / ``"reg"`` / ``"shark"``。
    :param seed: 内部 RNG 种子(决定复现性);``decide`` 传入的 rng 优先。
    :param big_blind: 大盲额度,用于翻牌前加注尺度换算。
    :param opponent_stats: 可选的(桌面均值的)对手统计,供 shark 轻度剥削。
    :param trials: 覆盖 MC 试验次数(默认按等级取值)。
    """

    style: PlayerStyle
    level: str = "reg"
    seed: int | None = None
    big_blind: int = 10
    opponent_stats: dict | None = None
    trials: int | None = None
    _rng: random.Random = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.level not in LEVELS:
            raise ValueError(f"level 须为 {LEVELS} 之一: {self.level}")
        self._rng = random.Random(self.seed)

    # -------------------------------------------------------- 入口

    def decide(self, snapshot: GameSnapshot, rng: random.Random | None = None) -> Action:
        """返回一个合法动作(任何情况下都有兜底,绝不返回非法动作)。"""
        rng = rng or self._rng
        seat = snapshot.acting_seat
        legal = snapshot.legal_actions
        assert seat is not None and legal is not None
        me = _check_perspective(snapshot, seat)
        try:
            if snapshot.street is Street.PREFLOP:
                action = self._decide_preflop(snapshot, me, legal, rng)
            else:
                action = self._decide_postflop(snapshot, me, legal, rng)
        except Exception:
            action = None  # 防御:任何意外都走兜底
        if action is None or not self._is_legal(action, legal):
            action = self._fallback(seat, legal)
        return action

    # -------------------------------------------------------- 翻牌前

    def _decide_preflop(
        self, snapshot: GameSnapshot, me: PlayerState, legal: LegalActions,
        rng: random.Random,
    ) -> Action | None:
        assert me.hole_cards is not None
        s = preflop_strength(me.hole_cards[0], me.hole_cards[1])
        pos = me.position or Position.BB
        pf = _POS_FACTOR[self.level].get(pos, 1.0)
        st = self.style
        seat = me.seat

        # 只要场上最大下注超过大盲即为“面对加注”(对 BB 的最低加注也成立)
        # live straddle 对其他人等价于一次强制加注；轮回 straddler 本人且
        # 无人再加注时仍拥有过牌选项，不能把自己的 2BB 误判成“面对加注”。
        facing_raise = max(p.bet for p in snapshot.players) > max(
            self.big_blind,
            me.bet,
        )

        if not facing_raise:
            # 未被加注(或仅有跛入):按 VPIP/PFR 频率决定 加注/跟注/弃牌
            if legal.min_raise_to is not None and s >= _preflop_cutoff(st.pfr * pf):
                return self._open_raise(snapshot, me, legal, rng)
            if s >= _preflop_cutoff(st.vpip * pf):
                if legal.can_call:
                    return Action(seat, ActionType.CALL, legal.call_amount)
                if legal.can_check:
                    return Action(seat, ActionType.CHECK)
            if legal.can_check:
                return Action(seat, ActionType.CHECK)
            return Action(seat, ActionType.FOLD)

        # 面对加注:3bet / 跟注 / 弃牌
        f2r = st.fold_to_raise * (0.4 if self.level == "fish" else 1.0)
        if legal.min_raise_to is not None and s >= _preflop_cutoff(st.threebet * pf):
            return self._reraise(snapshot, me, legal, rng, mult=3.0)
        call_freq = st.vpip * pf * (1 - f2r)
        if s >= _preflop_cutoff(call_freq):
            return Action(seat, ActionType.CALL, legal.call_amount)
        if self.level == "fish" and rng.random() < 0.12:
            # 新手的跟注站式错误
            return Action(seat, ActionType.CALL, legal.call_amount)
        return Action(seat, ActionType.FOLD)

    def _open_raise(
        self, snapshot: GameSnapshot, me: PlayerState, legal: LegalActions,
        rng: random.Random,
    ) -> Action | None:
        if self.level == "fish":
            mult = rng.uniform(2.0, 5.0)
        elif self.level == "reg":
            mult = 2.5
        else:
            early = {
                position
                for position in Position
                if position.name in {"UTG", "UTG1", "UTG2", "MP"}
            }
            mult = 3.0 if me.position in early else 2.5
        return self._clamp_wager(
            me, legal, round(mult * self.big_blind), rng, preflop=True
        )

    def _reraise(
        self, snapshot: GameSnapshot, me: PlayerState, legal: LegalActions,
        rng: random.Random, mult: float,
    ) -> Action | None:
        target = max(p.bet for p in snapshot.players)
        if self.level == "fish":
            mult = rng.uniform(2.0, 4.5)
        return self._clamp_wager(me, legal, round(target * mult), rng, preflop=True)

    # -------------------------------------------------------- 翻牌后

    def _decide_postflop(
        self, snapshot: GameSnapshot, me: PlayerState, legal: LegalActions,
        rng: random.Random,
    ) -> Action | None:
        assert me.hole_cards is not None
        hole = list(me.hole_cards)
        board = list(snapshot.board)
        hs = hand_strength(hole, board)
        st = self.style
        seat = me.seat
        pot = max(_pot_now(snapshot), self.big_blind)

        eq: float | None = None
        if self.level != "fish":
            n_opp = min(3, sum(1 for p in snapshot.players
                               if p.seat != seat and not p.folded))
            if n_opp > 0:
                trials = self.trials if self.trials is not None else _MC_TRIALS[self.level]
                eq = equity_vs_random(hole, board, n_opp, trials, rng)

        if legal.can_call and legal.call_amount > 0:
            return self._face_bet(snapshot, me, legal, rng, hs, eq, pot)
        return self._no_bet(snapshot, me, legal, rng, hs, eq, pot)

    def _face_bet(
        self, snapshot: GameSnapshot, me: PlayerState, legal: LegalActions,
        rng: random.Random, hs: float, eq: float | None, pot: int,
    ) -> Action | None:
        st = self.style
        seat = me.seat
        call = legal.call_amount
        pot_odds = call / (pot + call)

        if self.level == "fish":
            # 不看赔率:强牌偶尔加注,其余按手感跟注,很少弃牌
            if hs > 0.80 and legal.min_raise_to is not None \
                    and rng.random() < 0.30 * st.aggression:
                return self._value_raise(snapshot, me, legal, rng, pot)
            p_call = min(0.95, 0.25 + hs * 0.55 + (1 - st.fold_to_raise) * 0.35)
            if rng.random() < p_call:
                return Action(seat, ActionType.CALL, call)
            return Action(seat, ActionType.FOLD)

        assert eq is not None
        # shark 的剥削钩子:对手过度弃牌则多诈唬,对手松则打薄价值、少诈唬
        bluff_mult = 1.0
        value_t = 0.68 if self.level == "shark" else 0.72
        if self.level == "shark" and self.opponent_stats:
            if self.opponent_stats.get("fold_to_raise", 0.5) > 0.55:
                bluff_mult *= 1.3
            if self.opponent_stats.get("vpip", 0.25) > 0.35:
                bluff_mult *= 0.6
                value_t = 0.64

        if eq >= value_t and legal.min_raise_to is not None \
                and rng.random() < min(0.9, 0.30 * st.aggression + (eq - value_t) * 2):
            return self._value_raise(snapshot, me, legal, rng, pot)
        if eq < 0.30 and legal.min_raise_to is not None \
                and rng.random() < st.bluff_freq * 0.20 * bluff_mult:
            return self._value_raise(snapshot, me, legal, rng, pot)  # 诈唬加注
        # 底池赔率校验(reg 保守一些,shark 更贴赔率)
        margin = 1.0 + (st.fold_to_raise - 0.5) * 0.3
        if self.level == "shark":
            margin = min(margin, 0.98)
        if eq >= pot_odds * margin:
            return Action(seat, ActionType.CALL, call)
        return Action(seat, ActionType.FOLD)

    def _no_bet(
        self, snapshot: GameSnapshot, me: PlayerState, legal: LegalActions,
        rng: random.Random, hs: float, eq: float | None, pot: int,
    ) -> Action | None:
        st = self.style
        seat = me.seat
        can_bet = legal.min_bet_to is not None

        if self.level == "fish":
            if can_bet and rng.random() < min(0.9, st.cbet_freq * (0.3 + hs)
                                              * st.aggression / 2):
                return self._bet(snapshot, me, legal, rng, pot, frac=rng.uniform(0.25, 1.2))
            if can_bet and rng.random() < st.donk_freq * 0.5:
                return self._bet(snapshot, me, legal, rng, pot, frac=0.3)
            return Action(seat, ActionType.CHECK) if legal.can_check else None

        assert eq is not None
        scare = _board_scare_factor(snapshot.board)
        value_t = 0.55 if self.level == "shark" else 0.60
        # 极强成手偶尔慢打
        if hs >= 0.90 and rng.random() < 0.15:
            return Action(seat, ActionType.CHECK) if legal.can_check else None
        if eq >= value_t and can_bet \
                and rng.random() < min(0.95, st.cbet_freq * st.aggression / 2):
            return self._bet(snapshot, me, legal, rng, pot, frac=None)
        if eq < 0.32 and can_bet \
                and rng.random() < st.bluff_freq * (0.40 + scare):
            return self._bet(snapshot, me, legal, rng, pot, frac=None)
        if can_bet and rng.random() < st.donk_freq * 0.3:
            return self._bet(snapshot, me, legal, rng, pot, frac=0.33)
        return Action(seat, ActionType.CHECK) if legal.can_check else None

    # -------------------------------------------------------- 下注尺度

    def _street_fracs(self, street: Street) -> tuple[float, ...]:
        st = self.style
        if street is Street.FLOP:
            return st.sizing_flop
        if street is Street.TURN:
            return st.sizing_turn
        return st.sizing_river

    def _bet(
        self, snapshot: GameSnapshot, me: PlayerState, legal: LegalActions,
        rng: random.Random, pot: int, frac: float | None,
    ) -> Action | None:
        """开注(BET):额度 = 底池分数,自动夹取到合法区间。"""
        if frac is None:
            if self.level == "fish":
                frac = rng.uniform(0.25, 1.2)
            elif self.level == "reg":
                frac = rng.choice((0.5, 0.66))
            else:
                frac = rng.choice(self._street_fracs(snapshot.street))
        return self._clamp_wager(me, legal, round(frac * pot), rng, preflop=False)

    def _value_raise(
        self, snapshot: GameSnapshot, me: PlayerState, legal: LegalActions,
        rng: random.Random, pot: int,
    ) -> Action | None:
        """加注:在跟注目标之上加底池分数,自动夹取。"""
        if self.level == "fish":
            frac = rng.uniform(0.5, 1.5)
        elif self.level == "reg":
            frac = rng.choice((0.5, 0.75))
        else:
            frac = rng.choice(self._street_fracs(snapshot.street))
        target = max(p.bet for p in snapshot.players)
        desired = target + round(frac * (pot + legal.call_amount))
        return self._clamp_wager(me, legal, desired, rng, preflop=False)

    def _clamp_wager(
        self, me: PlayerState, legal: LegalActions, desired_to: int,
        rng: random.Random, preflop: bool,
    ) -> Action | None:
        """把期望的“加注到”总额夹取进合法区间,产出 BET/RAISE 动作。"""
        if legal.min_raise_to is not None:
            kind, lo, hi = ActionType.RAISE, legal.min_raise_to, legal.max_raise_to
        elif legal.min_bet_to is not None:
            kind, lo, hi = ActionType.BET, legal.min_bet_to, legal.max_bet_to
        else:
            return None
        assert hi is not None
        amount = max(lo, min(hi, desired_to))
        return Action(me.seat, kind, amount)

    # -------------------------------------------------------- 兜底

    @staticmethod
    def _is_legal(action: Action, legal: LegalActions) -> bool:
        """对照 LegalActions 做本地校验(与引擎口径一致)。"""
        t = action.action_type
        if t is ActionType.FOLD:
            return legal.can_fold
        if t is ActionType.CHECK:
            return legal.can_check
        if t is ActionType.CALL:
            return legal.can_call and action.amount == legal.call_amount
        if t is ActionType.BET:
            return (legal.min_bet_to is not None
                    and legal.min_bet_to <= action.amount <= (legal.max_bet_to or 0))
        if t is ActionType.RAISE:
            return (legal.min_raise_to is not None
                    and legal.min_raise_to <= action.amount <= (legal.max_raise_to or 0))
        if t is ActionType.ALLIN:
            hi = legal.max_raise_to or legal.max_bet_to
            return hi is not None and action.amount == hi
        return False

    @staticmethod
    def _fallback(seat: int, legal: LegalActions) -> Action:
        """最保守的合法动作:能过则过,能跟则跟,否则弃牌。"""
        if legal.can_check:
            return Action(seat, ActionType.CHECK)
        if legal.can_call:
            return Action(seat, ActionType.CALL, legal.call_amount)
        return Action(seat, ActionType.FOLD)


@dataclass
class StyleMixerBot:
    """按 ``hand_id`` 整手锁定一个子打法的真正混合机器人。

    混合发生在手与手之间，而非同一手的不同街随机换人格；因此对手可以
    观察到一手内连贯的下注逻辑，却难以用单一长期标签读取它。子机器人
    仍各自保有正常的随机决策频率。
    """

    components: tuple[tuple[str, Bot], ...]
    weights: tuple[float, ...]
    seed: int | None = None
    last_component_key: str = field(default="", init=False)
    _salt: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.components:
            raise ValueError("混合机器人至少需要一个子打法")
        if len(self.components) != len(self.weights):
            raise ValueError("components 与 weights 数量必须一致")
        if any(weight < 0 for weight in self.weights) or sum(self.weights) <= 0:
            raise ValueError("混合权重必须非负且总和大于零")
        keys = [key for key, _ in self.components]
        if len(set(keys)) != len(keys):
            raise ValueError("混合子打法 key 不得重复")
        self._salt = random.Random(self.seed).getrandbits(64)

    def component_key_for_hand(self, hand_id: int) -> str:
        """返回某一手固定使用的子打法 key；调用顺序不影响结果。"""
        chooser = random.Random(self._salt ^ (hand_id * 0x9E3779B97F4A7C15))
        needle = chooser.random() * sum(self.weights)
        running = 0.0
        for (key, _), weight in zip(self.components, self.weights):
            running += weight
            if needle <= running:
                return key
        return self.components[-1][0]

    def decide(
        self,
        snapshot: GameSnapshot,
        rng: random.Random | None = None,
    ) -> Action:
        """交给本手锁定的子机器人，接口与 ``HeuristicBot`` 一致。"""
        selected = self.component_key_for_hand(snapshot.hand_id)
        self.last_component_key = selected
        for key, bot in self.components:
            if key == selected:
                return bot.decide(snapshot, rng)
        raise AssertionError("混合子打法选择结果不在组件目录中")


def build_style_bot(
    style_key: str,
    *,
    style: PlayerStyle | None = None,
    level: str | None = None,
    seed: int | None = None,
    big_blind: int = 10,
    opponent_stats: dict | None = None,
    trials: int | None = None,
) -> Bot:
    """从稳定打法 key 构建机器人，``MIX`` 自动启用整手混合。

    非混合打法可传入已经抖动过的 ``style``（通常来自 ``Persona``），避免
    重复生成参数。``level`` 为空时采用打法目录推荐等级。
    """
    normalized = style_key.strip().upper()
    preset_rng = random.Random(None if seed is None else seed * 1009 + 17)
    preset = style_by_key(normalized, preset_rng)
    selected_level = level or preset.default_level
    if normalized != "MIX":
        return HeuristicBot(
            style=style or preset.style,
            level=selected_level,
            seed=seed,
            big_blind=big_blind,
            opponent_stats=opponent_stats,
            trials=trials,
        )

    components: list[tuple[str, Bot]] = []
    for index, component_key in enumerate(preset.mix_components):
        component_seed = None if seed is None else seed * 1009 + index + 1
        component_preset = style_by_key(
            component_key,
            random.Random(component_seed),
        )
        components.append((
            component_key,
            HeuristicBot(
                style=component_preset.style,
                level=selected_level,
                seed=component_seed,
                big_blind=big_blind,
                opponent_stats=opponent_stats,
                trials=trials,
            ),
        ))
    return StyleMixerBot(
        components=tuple(components),
        weights=preset.mix_weights,
        seed=seed,
    )
