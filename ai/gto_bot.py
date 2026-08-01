"""GTO 机器人(M6):多数据源降级链 + 频率采样(纯逻辑,不依赖 pygame)。

决策链(任一环节产出即停止,``last_source`` 记录实际来源):

- **翻前**
  - 未开池 → 顾问图表路(``chart:rfi`` / ``chart:pushfold``,频率采样);
  - 单挑面对加注 → HU 求解图表(``hu_solved.json``:BB 遇开池
    ``BB-vs-open-SB``、BTN 遇 3bet ``SB-vs-3bet-BB``、BB 遇 4bet
    ``BB-vs-4bet-SB``;来源 ``chart:hu``);不适用退回顾问启发式。
- **翻后**(依次)
  1. 预计算策略库(``solver:precomputed``,±20% 底池/筹码容差,额度按
     底池比缩放);
  2. NFSP 策略网(``net:nfsp``;仅限翻后恰剩两名未弃竞争者的实验性
     HU 池，且 checkpoint 存在并加载成功；多人池不尝试);
  3. 顾问胜率启发式(``heuristic:equity``,最终兜底)。

所有频率经 seeded rng 采样,动作一律夹取到 ``LegalActions``,任何意外
都有 过/跟/弃 兜底,绝不返回非法动作。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path

from engine.state import (
    Action,
    ActionType,
    GameSnapshot,
    LegalActions,
    PlayerState,
    Position,
    Street,
)
from gto.advisor import Advisor
from gto.charts import hand_key

from .bots import HeuristicBot, _check_perspective
from .nfsp_runtime import is_experimental_hu_postflop

HU_CHART_NAMES = {
    # (hero 位置, 局面) → hu_solved 图表名(HU 里 BTN 兼任小盲)
    (Position.BB, "vs_open"): "BB-vs-open-SB",
    (Position.BTN, "vs_3bet"): "SB-vs-3bet-BB",
    (Position.BB, "vs_4bet"): "BB-vs-4bet-SB",
}


@dataclass
class GTOBot:
    """实现 ``Bot`` 协议的 GTO 机器人。

    :param big_blind: 大盲额度(翻前尺度换算)。
    :param seed: 内部 RNG 种子;``decide`` 传入的 rng 优先。
    :param advisor: 顾问实例(缺省自建;共享其 charts 与策略库)。
    :param policy_checkpoint: NFSP checkpoint 路径;``None`` = 默认
        ``nets/checkpoints/prod/nfsp_latest.pt``。
    :param use_policy_net: 是否启用策略网环节(默认 True;无文件自动跳过)。
    """

    big_blind: int = 10
    seed: int | None = None
    advisor: Advisor | None = None
    policy_checkpoint: str | Path | None = None
    use_policy_net: bool = True
    last_source: str = field(default="", init=False)
    _rng: random.Random = field(init=False, repr=False)
    _policy: object | None = field(default=None, init=False, repr=False)
    _policy_tried: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)
        if self.advisor is None:
            self.advisor = Advisor(big_blind=self.big_blind, seed=self.seed)

    # ------------------------------------------------------------ 策略网(懒加载)

    def _policy_net(self):
        """懒加载 PolicyNet;无 checkpoint / 加载失败时返回 ``None``。"""
        if not self.use_policy_net:
            return None
        if not self._policy_tried:
            self._policy_tried = True
            try:
                from nets.policy import PolicyNet

                self._policy = PolicyNet(self.policy_checkpoint)
            except Exception:
                self._policy = None  # 无 checkpoint 是正常状态,安静降级
        return self._policy

    # ------------------------------------------------------------ 入口

    def decide(self, snapshot: GameSnapshot, rng: random.Random | None = None) -> Action:
        """返回一个合法动作(任何情况下都有兜底)。"""
        rng = rng or self._rng
        seat = snapshot.acting_seat
        legal = snapshot.legal_actions
        assert seat is not None and legal is not None
        me = _check_perspective(snapshot, seat)
        action: Action | None = None
        try:
            if snapshot.street is Street.PREFLOP:
                action = self._decide_preflop(snapshot, me, legal, rng)
            else:
                action = self._decide_postflop(snapshot, me, legal, rng)
        except Exception:
            action = None  # 防御:任何意外都走兜底
        if action is None or not HeuristicBot._is_legal(action, legal):
            action = HeuristicBot._fallback(seat, legal)
            self.last_source = "fallback"
        return action

    # ------------------------------------------------------------ 翻前

    def _decide_preflop(
        self, snapshot: GameSnapshot, me: PlayerState, legal: LegalActions,
        rng: random.Random,
    ) -> Action | None:
        max_bet = max((p.bet for p in snapshot.players), default=0)
        unopened = max_bet <= self.big_blind
        if not unopened:
            cell = self._hu_vs_raise_cell(snapshot, me)
            if cell is not None:
                action = self._sample_freqs(
                    self._cell_to_freqs(cell), snapshot, me, legal, rng
                )
                if action is not None:
                    self.last_source = "chart:hu"
                    return action
        hint = self.advisor.hint(snapshot, me.seat)  # chart:rfi/pushfold 或启发式
        action = self._sample_freqs(hint.action_freqs, snapshot, me, legal, rng)
        if action is not None:
            self.last_source = hint.source
        return action

    def _hu_vs_raise_cell(
        self, snapshot: GameSnapshot, me: PlayerState
    ) -> dict[str, float] | None:
        """单挑面对加注:定位适用的 HU 求解图表单元格(不适用返回 None)。"""
        in_hand = [p for p in snapshot.players if p.position is not None]
        if len(in_hand) != 2 or me.position not in (Position.BTN, Position.BB):
            return None
        hero_raised = me.bet > self.big_blind  # hero 已主动加过注
        if me.position is Position.BB:
            situation = "vs_4bet" if hero_raised else "vs_open"
        else:  # BTN:小盲开池者,被再加注即遇 3bet
            situation = "vs_3bet" if hero_raised else None
        if situation is None:
            return None
        name = HU_CHART_NAMES.get((me.position, situation))
        if name is None:
            return None
        hole = list(me.hole_cards or ())
        if len(hole) != 2:
            return None
        return dict(self.advisor.charts.hu_solved[name][hand_key(hole)])

    @staticmethod
    def _cell_to_freqs(cell: dict[str, float]) -> dict[str, float]:
        """图表单元格(raise/call/fold/allin)→ ActionType 名频率字典。"""
        mapping = {"raise": "RAISE", "call": "CALL", "fold": "FOLD", "allin": "ALLIN"}
        out: dict[str, float] = {}
        for k, v in cell.items():
            key = mapping.get(k)
            if key is not None and v > 0:
                out[key] = out.get(key, 0.0) + v
        return out

    # ------------------------------------------------------------ 翻后

    def _decide_postflop(
        self, snapshot: GameSnapshot, me: PlayerState, legal: LegalActions,
        rng: random.Random,
    ) -> Action | None:
        action = self._from_library(snapshot, me, legal, rng)
        if action is not None:
            self.last_source = "solver:precomputed"
            return action
        if is_experimental_hu_postflop(snapshot):
            policy = self._policy_net()
            if policy is not None:
                try:
                    action = policy.sample_action(snapshot, me.seat, rng)
                except Exception:
                    action = None
                if action is not None:
                    self.last_source = "net:nfsp"
                    return action
        hint = self.advisor.hint(snapshot, me.seat)
        action = self._sample_freqs(hint.action_freqs, snapshot, me, legal, rng)
        if action is not None:
            self.last_source = hint.source
        return action

    def _from_library(
        self, snapshot: GameSnapshot, me: PlayerState, legal: LegalActions,
        rng: random.Random,
    ) -> Action | None:
        """预计算策略库:命中后按 169 频率采样并按底池比缩放额度。"""
        assert self.advisor is not None
        villains = [
            p for p in snapshot.players
            if p.seat != me.seat and p.position is not None and not p.folded
        ]
        if len(villains) != 1 or len(snapshot.board) < 3:
            return None
        if legal.can_call and legal.call_amount > 0:
            return None  # v1 只服务未面对下注的节点
        hole = list(me.hole_cards or ())
        if len(hole) != 2:
            return None
        pot = snapshot.total_pot + sum(p.bet for p in snapshot.players)
        eff_stack = min(me.stack, villains[0].stack)
        hit = self.advisor.library.lookup(
            snapshot.board, pot, eff_stack, hero_is_ip=me.position is Position.BTN
        )
        if hit is None:
            return None
        raw = {a: f for a, f in (hit["strategy_169"].get(hand_key(hole)) or {}).items()
               if f > 0}
        if not raw:
            return None
        picked = _sample_key(raw, rng)
        kind, _, num = picked.partition(" ")
        kind = kind.upper()
        if kind in ("BET", "RAISE", "ALLIN"):
            if kind == "ALLIN":
                return Action(me.seat, ActionType.ALLIN, me.stack + me.bet)
            try:
                desired = round(float(num) * hit["scale"])
            except ValueError:
                return None
            return self._clamp_wager(me, legal, desired)
        if kind == "CHECK":
            return Action(me.seat, ActionType.CHECK) if legal.can_check else None
        if kind == "CALL":
            return (
                Action(me.seat, ActionType.CALL, legal.call_amount)
                if legal.can_call
                else None
            )
        if kind == "FOLD":
            return Action(me.seat, ActionType.FOLD) if legal.can_fold else None
        return None

    # ------------------------------------------------------------ 采样与额度

    def _sample_freqs(
        self, freqs: dict[str, float], snapshot: GameSnapshot, me: PlayerState,
        legal: LegalActions, rng: random.Random,
    ) -> Action | None:
        """从 {ActionType 名: 频率} 采样并构造动作(额度按标准尺度夹取)。"""
        clean = {k: v for k, v in freqs.items() if v > 0}
        if not clean:
            return None
        picked = _sample_key(clean, rng)
        seat = me.seat
        if picked == "FOLD":
            return Action(seat, ActionType.FOLD) if legal.can_fold else None
        if picked == "CHECK":
            return Action(seat, ActionType.CHECK) if legal.can_check else None
        if picked == "CALL":
            return (
                Action(seat, ActionType.CALL, legal.call_amount)
                if legal.can_call
                else None
            )
        if picked == "ALLIN":
            if legal.min_bet_to is None and legal.min_raise_to is None:
                return None
            return Action(seat, ActionType.ALLIN, me.stack + me.bet)
        if picked in ("BET", "RAISE"):
            pot = snapshot.total_pot + sum(p.bet for p in snapshot.players)
            if picked == "BET":
                desired = round(0.66 * pot)
            else:  # RAISE:在跟注目标之上加 3/4 底池
                target = max((p.bet for p in snapshot.players), default=0)
                desired = target + round(0.75 * (pot + legal.call_amount))
            return self._clamp_wager(me, legal, desired)
        return None

    @staticmethod
    def _clamp_wager(
        me: PlayerState, legal: LegalActions, desired_to: int
    ) -> Action | None:
        """期望"加注到"总额夹取进合法区间,产出 BET/RAISE。"""
        if legal.min_raise_to is not None:
            kind, lo, hi = ActionType.RAISE, legal.min_raise_to, legal.max_raise_to
        elif legal.min_bet_to is not None:
            kind, lo, hi = ActionType.BET, legal.min_bet_to, legal.max_bet_to
        else:
            return None
        assert hi is not None
        return Action(me.seat, kind, max(lo, min(hi, desired_to)))


def _sample_key(freqs: dict[str, float], rng: random.Random) -> str:
    """按频率字典采样一个键(频率和不要求为 1,内部归一)。"""
    total = sum(freqs.values())
    r = rng.random() * total
    acc = 0.0
    last = next(reversed(freqs))
    for k, v in freqs.items():
        acc += v
        if r <= acc:
            return k
    return last
