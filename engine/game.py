"""牌桌引擎:对 pokerkit 无限注德州状态机的封装。

负责整手流程:按钮轮转、盲注、发牌(随机或脚本化)、行动校验、
全下跑码、摊牌结算。对外只暴露 ``state.py`` 中定义的纯数据契约,
不依赖 pygame,也不泄漏 pokerkit 类型。

pokerkit 0.7.4 关键事实(均已实测):
- ``create_state`` 开启全部 ``Automation`` 后,盲注/发牌/烧牌/结算自动完成;
- heads-up 时 ``raw_blinds_or_straddles=(sb, bb)`` 由 0 号位下大盲、
  1 号位(按钮)下小盲;三人及以上时 0 号小盲、1 号大盲、末位按钮;
- 统一映射:``pokerkit 下标 p`` ↔ ``active[(p + button_active_index + 1) % m]``;
- 8/9 人手牌的 ``raw_blinds_or_straddles=(sb, bb, 2bb)`` 会令
  pokerkit 下标 2(UTG)强制 straddle、下标 3(UTG+1)翻前先行动；
  pokerkit 不会自动把完整 straddle 当作最后一次完整加注,本封装在首轮
  行动前补齐 ``completion_betting_or_raising_amount``，使最小加注为 4BB；
- ``state.bets`` 为本街道下注,``state.stacks`` 已扣除下注;
- 未跟注部分自动返还,全下后自动跑完公共牌并结算;
- 各方投入由 Table 在 apply 时累加,结算时削去唯一最大投入者的
  未跟注部分后重建主/边池;
- 洗牌使用全局 ``random`` 模块,``random.seed`` 可复现。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from pokerkit import Automation, NoLimitTexasHoldem
from phevaluator import evaluate_cards
from pokerkit.state import State

from .history import HandHistoryWriter
from .state import (
    Action,
    ActionType,
    GameSnapshot,
    LegalActions,
    PlayerState,
    Position,
    PotInfo,
    Street,
    position_labels,
)

_STREETS = (Street.PREFLOP, Street.FLOP, Street.TURN, Street.RIVER)


class IllegalActionError(ValueError):
    """动作违反当前合法动作集时抛出。"""


class GameOverRequiredError(RuntimeError):
    """移出该座位后剩余有筹码的玩家不足两人,整场必须结束(由 UI 接管)。"""


# 开局买入 / 重新买入额度上下限(大盲倍数)
MIN_BUYIN_BB = 10
MAX_BUYIN_BB = 1000
# 兼容既有调用方的名称；两种买入口径共用同一组边界。
MIN_REBUY_BB = MIN_BUYIN_BB
MAX_REBUY_BB = MAX_BUYIN_BB


@dataclass(frozen=True)
class TableConfig:
    """牌桌配置。

    :param starting_stack: 统一起始筹码，或逐物理座位筹码元组；初始空位
        在元组中为 0 并同时列入 ``initially_removed``。
    """

    player_count: int  # 2-9
    starting_stack: int | tuple[int, ...]
    small_blind: int
    big_blind: int
    player_names: tuple[str, ...] | None = None
    # 固定物理座位中的初始空位。空位筹码必须为 0，开局时不会被压缩编号；
    # 朋友局可在两手之间通过 ``seat_player`` 把成员安排回来。
    initially_removed: frozenset[int] = frozenset()
    # 单机默认保护 0 号真人座位；权威朋友局的房主与座位解耦，因此会
    # 显式传空集合。该字段只影响 ``remove_player``，不影响投注规则。
    protected_seats: frozenset[int] = frozenset({0})
    straddle_enabled: bool = field(init=False)
    straddle_amount: int = field(init=False)

    @classmethod
    def from_buyins_bb(
        cls,
        *,
        player_count: int,
        buyins_bb: tuple[int, ...],
        small_blind: int,
        big_blind: int,
        player_names: tuple[str, ...] | None = None,
    ) -> "TableConfig":
        """以逐座位 BB 数创建配置，并换算为引擎使用的筹码数。

        这是面向真实开局设置的受约束入口；原 ``starting_stack``
        构造方式继续保留，供短码模拟与确定性测试使用。
        """
        if not 2 <= player_count <= 9:
            raise ValueError(f"player_count 须在 2-9 之间: {player_count}")
        if len(buyins_bb) != player_count:
            raise ValueError("逐座位买入数量须与 player_count 一致")
        if any(isinstance(v, bool) or not isinstance(v, int) for v in buyins_bb):
            raise ValueError("逐座位买入须为整数 BB")
        if any(not MIN_BUYIN_BB <= v <= MAX_BUYIN_BB for v in buyins_bb):
            raise ValueError(
                f"开局买入须在 {MIN_BUYIN_BB}-{MAX_BUYIN_BB}BB 之间"
            )
        stacks = tuple(v * big_blind for v in buyins_bb)
        # 等额桌仍保存为既有标量形态，保持历史记录与调用方行为兼容。
        starting_stack: int | tuple[int, ...] = (
            stacks[0] if len(set(stacks)) == 1 else stacks
        )
        return cls(
            player_count=player_count,
            starting_stack=starting_stack,
            small_blind=small_blind,
            big_blind=big_blind,
            player_names=player_names,
        )

    def __post_init__(self) -> None:
        if not 2 <= self.player_count <= 9:
            raise ValueError(f"player_count 须在 2-9 之间: {self.player_count}")
        if self.small_blind <= 0 or self.big_blind <= self.small_blind:
            raise ValueError("盲注须满足 0 < small_blind < big_blind")
        stacks = (
            self.starting_stack
            if isinstance(self.starting_stack, tuple)
            else (self.starting_stack,) * self.player_count
        )
        if len(stacks) != self.player_count:
            raise ValueError("逐座位筹码数量须与 player_count 一致")
        if not isinstance(self.initially_removed, frozenset):
            raise ValueError("initially_removed 须为 frozenset")
        if not isinstance(self.protected_seats, frozenset):
            raise ValueError("protected_seats 须为 frozenset")
        all_seats = set(range(self.player_count))
        if not self.initially_removed <= all_seats:
            raise ValueError("initially_removed 含不存在的座位")
        if not self.protected_seats <= all_seats:
            raise ValueError("protected_seats 含不存在的座位")
        if len(all_seats - self.initially_removed) < 2:
            raise ValueError("初始至少须有两个在座玩家")
        for seat, stack in enumerate(stacks):
            if seat in self.initially_removed:
                if stack != 0:
                    raise ValueError("初始空位筹码必须为 0")
            elif stack <= 0:
                raise ValueError("在座玩家起始筹码须为正数")
        if self.player_names is not None and len(self.player_names) != self.player_count:
            raise ValueError("player_names 数量须与 player_count 一致")
        # 8/9 人模式默认采用一枪 UTG live straddle；字段为派生只读配置，
        # 保持既有 TableConfig 构造签名不变。
        object.__setattr__(self, "straddle_enabled", self.player_count >= 8)
        object.__setattr__(
            self,
            "straddle_amount",
            2 * self.big_blind if self.player_count >= 8 else 0,
        )


@dataclass(frozen=True)
class PotAward:
    """一个主池/边池的真实派彩明细。

    :param pot_index: ``0`` 为主池，其后依次为边池。
    :param pot: 对应底池的金额与有资格争夺的座位。
    :param payouts: ``(座位, 金额)``；平分与奇数筹码均保留实际结果。
    """

    pot_index: int
    pot: PotInfo
    payouts: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class HandResult:
    """一手牌结算结果。"""

    hand_id: int
    deltas: dict[int, int]  # 座位 -> 本手净盈亏
    winners: dict[int, int]  # 座位 -> 从全部底池获得的毛派彩
    showdown: bool
    board: tuple[str, ...]
    pots: tuple[PotInfo, ...]
    pot_awards: tuple[PotAward, ...]


class Table:
    """一张牌桌,管理连续多手牌。"""

    def __init__(
        self,
        config: TableConfig,
        seed: int | None = None,
        history_writer: HandHistoryWriter | None = None,
    ) -> None:
        self.config = config
        self._writer = history_writer
        if seed is not None:
            random.seed(seed)  # pokerkit 用全局 random 洗牌
        n = config.player_count
        self._names: list[str] = (
            list(config.player_names)
            if config.player_names is not None
            else [f"P{i}" for i in range(n)]
        )
        self._stacks: list[int] = (
            list(config.starting_stack)
            if isinstance(config.starting_stack, tuple)
            else [config.starting_stack] * n
        )
        self._button_seat: int | None = None
        self._hand_id = 0
        self._state: State | None = None
        self._removed: set[int] = set(config.initially_removed)
        self._chips_added: dict[int, int] = {}  # 座位 -> 累计买入筹码(守恒核算)
        # 以下为本手牌的上下文(start_hand 时重建)
        self._active: list[int] = []
        self._pk_to_seat: list[int] = []
        self._seat_to_pk: dict[int, int] = {}
        self._hand_start_stacks: dict[int, int] = {}
        self._hole: dict[int, tuple[str, ...]] = {}
        self._action_log: list[dict[str, Any]] = []
        self._showdown = False
        self._shown_seats: set[int] = set()
        self._current_straddle_amount = 0
        self._scripted_board: list[str] | None = None
        self._board_ptr = 0
        self._invested: dict[int, int] = {}
        self._folded: set[int] = set()
        self._last_result: HandResult | None = None

    # ---------------------------------------------------------------- 属性

    @property
    def hand_id(self) -> int:
        """当前(或最近)手牌编号,从 1 起。"""
        return self._hand_id

    @property
    def stacks(self) -> tuple[int, ...]:
        """各座位当前筹码。"""
        return tuple(self._stacks)

    @property
    def button_seat(self) -> int | None:
        """当前按钮位座位号。"""
        return self._button_seat

    @property
    def hand_over(self) -> bool:
        """当前无进行中的手牌。"""
        return self._state is None or not self._state.status

    @property
    def game_over(self) -> bool:
        """剩余有筹码且未被移出的玩家不足两人,整场结束。"""
        return len(self.active_seats) < 2

    @property
    def removed_seats(self) -> frozenset[int]:
        """已被永久移出牌桌的座位。"""
        return frozenset(self._removed)

    @property
    def shown_seats(self) -> frozenset[int]:
        """当前或刚结束手牌中主动亮牌的座位。"""
        return frozenset(self._shown_seats)

    @property
    def current_straddle_amount(self) -> int:
        """当前或刚结束手牌实际生效的 live straddle；未生效时为零。"""
        return self._current_straddle_amount

    @property
    def last_actions(self) -> tuple[dict[str, Any], ...]:
        """当前或刚结束手牌的动作线副本，供回顾与结算对白消费。"""
        return tuple(dict(action) for action in self._action_log)

    @property
    def busted_seats(self) -> tuple[int, ...]:
        """筹码归零但尚未被移出的座位(两手之间等待处置)。"""
        return tuple(
            s
            for s in range(self.config.player_count)
            if self._stacks[s] == 0 and s not in self._removed
        )

    @property
    def active_seats(self) -> tuple[int, ...]:
        """未被移出且仍有筹码的座位(下一手的在局者)。"""
        return tuple(
            s
            for s in range(self.config.player_count)
            if s not in self._removed and self._stacks[s] > 0
        )

    @property
    def chips_added(self) -> int:
        """历手累计买入的筹码总额(守恒核算:总筹码 = 起始总和 + 本值)。"""
        return sum(self._chips_added.values())

    @property
    def last_hand_result(self) -> HandResult | None:
        """最近一手的结算结果。"""
        return self._last_result

    # ------------------------------------------------------------ 手牌流程

    def start_hand(
        self,
        scripted_hole: dict[int, tuple[str, str]] | None = None,
        scripted_board: Iterable[str] | None = None,
    ) -> None:
        """开始下一手牌(按钮轮转、发牌、盲注自动完成)。

        :param scripted_hole: 可选,座位 -> 两张底牌(如 ``("As", "Kd")``),
            用于确定性测试;须覆盖所有在局玩家。
        :param scripted_board: 可选,5 张公共牌(如 ``"2c 3d 4s 5h 6c"``
            拆成的序列);提供后烧牌与发牌按脚本执行。
        """
        if not self.hand_over:
            raise RuntimeError("上一手牌尚未结束")
        if self.game_over:
            raise RuntimeError("游戏已结束,无法继续发牌")
        board = list(scripted_board) if scripted_board is not None else None
        if board is not None and len(board) != 5:
            raise ValueError("scripted_board 须为 5 张牌")

        self._active = [
            s
            for s in range(self.config.player_count)
            if s not in self._removed and self._stacks[s] > 0
        ]
        m = len(self._active)
        # 按钮轮转:首手为最小座位,之后移到下一个有筹码的座位
        if self._button_seat is None:
            self._button_seat = self._active[0]
        else:
            nxt = [s for s in self._active if s > self._button_seat]
            self._button_seat = nxt[0] if nxt else self._active[0]
        j = self._active.index(self._button_seat)
        self._pk_to_seat = [self._active[(p + j + 1) % m] for p in range(m)]
        self._seat_to_pk = {s: p for p, s in enumerate(self._pk_to_seat)}
        self._hand_start_stacks = {s: self._stacks[s] for s in self._active}
        self._shown_seats.clear()

        excluded: set[Automation] = set()
        if scripted_hole is not None:
            excluded.add(Automation.HOLE_DEALING)
        if board is not None:
            excluded |= {
                Automation.HOLE_DEALING,
                Automation.BOARD_DEALING,
                Automation.CARD_BURNING,
            }
        automations = tuple(a for a in Automation if a not in excluded)
        pk_stacks = [self._stacks[s] for s in self._pk_to_seat]
        nominal_straddle = (
            self.config.straddle_amount
            if self.config.straddle_enabled and m >= 8
            else 0
        )
        blinds_or_straddles = (
            (
                self.config.small_blind,
                self.config.big_blind,
                nominal_straddle,
            )
            if nominal_straddle
            else (self.config.small_blind, self.config.big_blind)
        )
        self._state = NoLimitTexasHoldem.create_state(
            automations,
            True,  # ante_trimming_status(无前注,取默认约定)
            0,
            blinds_or_straddles,
            self.config.big_blind,
            pk_stacks,
            m,
        )
        # 短码 UTG 可能无法交满名义 2BB；对外状态与历史必须记录实际投入，
        # 但只有交满 2BB 才构成一笔完整 live straddle 加注。
        self._current_straddle_amount = (
            self._state.bets[2] if nominal_straddle else 0
        )
        if scripted_hole is not None:
            for seat in self._active:
                cards = scripted_hole.get(seat)
                if cards is None or len(cards) != 2:
                    raise ValueError(f"scripted_hole 缺少座位 {seat} 的两张底牌")
                self._state.deal_hole("".join(cards), self._seat_to_pk[seat])
        if (
            nominal_straddle
            and self._current_straddle_amount == nominal_straddle
        ):
            # pokerkit 0.7.4 以 min_bet(BB)计算第一次加注；完整 live
            # straddle 应视为最后一次完整加注，最小加注因此为 4BB。
            self._state.completion_betting_or_raising_amount = (
                nominal_straddle
            )
        self._scripted_board = board
        self._board_ptr = 0
        self._hole = {
            s: tuple(_card_str(c) for c in self._state.hole_cards[self._seat_to_pk[s]])
            for s in self._active
        }
        self._action_log = []
        self._showdown = False
        # 各方总投入:初始为已下的盲注,之后随 apply 逐步累加
        self._invested = {
            seat: self._state.bets[self._seat_to_pk[seat]] for seat in self._active
        }
        self._folded = set()
        self._hand_id += 1
        self._auto_progress()
        if not self._state.status:
            # 盲注已使在局者全部全下:无需任何行动,直接跑码结算
            self._settle()

    def rotate_and_deal_next(self) -> None:
        """``start_hand()`` 的别名:轮转按钮并发下一手。"""
        self.start_hand()

    # ------------------------------------------------------------ 买入/移出

    def rebuy(self, seat: int, amount: int) -> None:
        """两手之间为某座位补充筹码(重新买入,对后续手牌生效)。

        :param seat: 目标座位(须存在且未被移出)。
        :param amount: 补充的筹码数,须在 ``[MIN_REBUY_BB, MAX_REBUY_BB]``
            倍大盲之间。
        :raises RuntimeError: 当前有进行中的手牌。
        :raises ValueError: 座位非法或额度越界。
        """
        if not self.hand_over:
            raise RuntimeError("只能在两手之间买入")
        self._check_seat(seat)
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise ValueError("买入额度须为整数筹码")
        lo = MIN_REBUY_BB * self.config.big_blind
        hi = MAX_REBUY_BB * self.config.big_blind
        if not lo <= amount <= hi:
            raise ValueError(f"买入额度须在 {lo}-{hi} 之间: {amount}")
        self._stacks[seat] += amount
        self._chips_added[seat] = self._chips_added.get(seat, 0) + amount
        self._write_event({"type": "rebuy", "seat": seat, "amount": amount})

    def remove_player(self, seat: int, *, allow_game_over: bool = False) -> None:
        """两手之间把一名已出局(筹码归零)的玩家永久移出牌桌。

        :raises RuntimeError: 当前有进行中的手牌。
        :raises ValueError: 座位非法 / 是 0 号(人类)座位 / 该座位仍有筹码。
        :raises GameOverRequiredError: 移出后剩余有筹码玩家不足两人。
        """
        if not self.hand_over:
            raise RuntimeError("只能在两手之间移出玩家")
        self._check_seat(seat)
        if seat in self.config.protected_seats:
            raise ValueError(f"座位 {seat} 受保护，不能移出")
        if self._stacks[seat] > 0:
            raise ValueError(f"座位 {seat} 仍有筹码,不能移出")
        if len(self.active_seats) < 2 and not allow_game_over:
            raise GameOverRequiredError("移出后剩余玩家不足两人,应结束整场")
        self._removed.add(seat)
        self._write_event({"type": "remove", "seat": seat})

    def top_up_to(self, seat: int, target_stack: int) -> int:
        """两手之间把座位码量补到指定目标，返回实际增加额。

        与兼容旧界面的 :meth:`rebuy` 不同，目标栈须位于 10-1000BB，
        但实际差额可以小于 10BB；这正是联机“下一手前补到目标码量”
        的金额语义。目标不高于当前栈时不写历史且返回 0。
        """
        if not self.hand_over:
            raise RuntimeError("只能在两手之间补充筹码")
        self._check_seat(seat)
        if isinstance(target_stack, bool) or not isinstance(target_stack, int):
            raise ValueError("目标码量须为整数筹码")
        lo = MIN_REBUY_BB * self.config.big_blind
        hi = MAX_REBUY_BB * self.config.big_blind
        if not lo <= target_stack <= hi:
            raise ValueError(f"目标码量须在 {lo}-{hi} 之间: {target_stack}")
        current = self._stacks[seat]
        if target_stack <= current:
            return 0
        delta = target_stack - current
        self._stacks[seat] = target_stack
        self._chips_added[seat] = self._chips_added.get(seat, 0) + delta
        self._write_event(
            {
                "type": "top_up",
                "seat": seat,
                "amount": delta,
                "target_stack": target_stack,
            }
        )
        return delta

    def seat_player(self, seat: int, amount: int, name: str | None = None) -> None:
        """两手之间把牌手安排进一个已移出的空座位。

        此 API 使用中性的“入座”语义，当前由 AI 召回界面消费，也为将来
        好友加入保留同一席位生命周期。所有参数会先完成校验再原子写入。

        :param seat: 须为已移出且筹码为零的空座位。
        :param amount: 初始资金，须在 10-1000BB 之间。
        :param name: 可选的新显示名；省略时沿用该座位原名。
        """
        if not self.hand_over:
            raise RuntimeError("只能在两手之间安排玩家入座")
        if not 0 <= seat < self.config.player_count:
            raise ValueError(f"座位不存在: {seat}")
        if seat not in self._removed:
            raise ValueError(f"座位 {seat} 不是可加入的空座位")
        if self._stacks[seat] != 0:
            raise ValueError(f"空座位 {seat} 的筹码不为零")
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise ValueError("入座资金须为整数筹码")
        lo = MIN_BUYIN_BB * self.config.big_blind
        hi = MAX_BUYIN_BB * self.config.big_blind
        if not lo <= amount <= hi:
            raise ValueError(f"入座资金须在 {lo}-{hi} 之间: {amount}")
        if name is not None and (not isinstance(name, str) or not name.strip()):
            raise ValueError("玩家名称不能为空")

        display_name = self._names[seat] if name is None else name.strip()
        self._stacks[seat] = amount
        self._names[seat] = display_name
        self._removed.remove(seat)
        self._chips_added[seat] = self._chips_added.get(seat, 0) + amount
        self._write_event(
            {
                "type": "seat_join",
                "seat": seat,
                "amount": amount,
                "name": display_name,
            }
        )

    def show_cards(self, seat: int) -> None:
        """手牌结束后由本手参与者自愿亮牌。

        SHOW 独立于投注动作和 pokerkit 状态机；可由曾弃牌者使用，重复
        调用幂等。亮牌只保持到下一手 ``start_hand``，并会写独立事件行。
        """
        if self._state is None:
            raise RuntimeError("尚未开始任何手牌")
        if not self.hand_over:
            raise RuntimeError("只能在手牌结束后亮牌")
        if not 0 <= seat < self.config.player_count:
            raise ValueError(f"座位不存在: {seat}")
        if seat not in self._active or seat not in self._hole:
            raise ValueError(f"座位 {seat} 未参与刚结束的手牌")
        if seat in self._shown_seats:
            return
        self._shown_seats.add(seat)
        self._write_event({"type": "show", "seat": seat})

    def _check_seat(self, seat: int) -> None:
        """校验座位存在且未被移出。"""
        if not 0 <= seat < self.config.player_count:
            raise ValueError(f"座位不存在: {seat}")
        if seat in self._removed:
            raise ValueError(f"座位 {seat} 已被移出")

    def _write_event(self, event: dict[str, Any]) -> None:
        """把牌局事件写入历史(独立于手牌记录的事件行)。"""
        if self._writer is None:
            return
        self._writer.write_event(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "after_hand": self._hand_id,
                **event,
            }
        )

    # ------------------------------------------------------------ 行动

    def apply(self, action: Action) -> None:
        """执行一个动作,非法时抛 :class:`IllegalActionError`。"""
        st = self._require_state()
        snap = self.snapshot()
        if snap.acting_seat is None or action.seat != snap.acting_seat:
            raise IllegalActionError(
                f"座位 {action.seat} 不可行动(当前行动者: {snap.acting_seat})"
            )
        legal = snap.legal_actions
        assert legal is not None
        t = action.action_type
        if t is ActionType.FOLD:
            if not legal.can_fold:
                raise IllegalActionError("当前不可弃牌")
            self._folded.add(action.seat)
            st.fold()
        elif t is ActionType.CHECK:
            if not legal.can_check:
                raise IllegalActionError("当前不可过牌")
            st.check_or_call()
        elif t is ActionType.CALL:
            if not legal.can_call:
                raise IllegalActionError("当前无可跟注的下注")
            self._invested[action.seat] += legal.call_amount
            st.check_or_call()
        elif t in (ActionType.BET, ActionType.RAISE, ActionType.ALLIN):
            self._apply_wager(st, legal, action)
        else:  # pragma: no cover - 防御未知枚举
            raise IllegalActionError(f"未知动作类型: {t}")
        self._action_log.append(
            {
                "street": snap.street.name,
                "seat": action.seat,
                "action": t.name,
                "amount": action.amount,
            }
        )
        self._auto_progress()
        if not st.status:
            self._settle()

    def _apply_wager(self, st: State, legal: LegalActions, action: Action) -> None:
        """处理 BET/RAISE/ALLIN 的校验、执行与投入累加。"""
        t = action.action_type
        pk = self._seat_to_pk[action.seat]
        if t is ActionType.BET:
            lo, hi = legal.min_bet_to, legal.max_bet_to
            if lo is None or hi is None:
                raise IllegalActionError("本街道已有下注,应使用 RAISE")
        elif t is ActionType.RAISE:
            lo, hi = legal.min_raise_to, legal.max_raise_to
            if lo is None or hi is None:
                raise IllegalActionError("本街道尚无可加注的下注,应使用 BET")
        else:  # ALLIN:额度 = 剩余筹码 + 本街已下注,即 pokerkit 的 max_to
            all_in_to = st.stacks[pk] + st.bets[pk]
            if action.amount != all_in_to:
                raise IllegalActionError(
                    f"ALLIN 额度须为 {all_in_to},收到 {action.amount}"
                )
            if st.can_complete_bet_or_raise_to(all_in_to):
                st.complete_bet_or_raise_to(all_in_to)
                self._invested[action.seat] = self._hand_start_stacks[action.seat]
            elif st.can_check_or_call():
                # 对手已全下(再加注无意义)或筹码不足跟注:跟注即全下
                self._invested[action.seat] += st.checking_or_calling_amount
                st.check_or_call()
            else:
                raise IllegalActionError("当前不可全下")
            return
        if not lo <= action.amount <= hi:
            raise IllegalActionError(
                f"{t.name} 额度 {action.amount} 越界 [{lo}, {hi}]"
            )
        self._invested[action.seat] += action.amount - st.bets[pk]
        st.complete_bet_or_raise_to(action.amount)

    # ------------------------------------------------------------ 快照

    def snapshot(self, perspective: int | None = None) -> GameSnapshot:
        """生成当前快照。

        :param perspective: 可选的观察座位;指定后其他在局玩家的底牌
            将被隐藏(摊牌亮出的除外),``None`` 表示全知视角。
        """
        return self._snapshot_for(perspective=perspective, public_only=False)

    def public_snapshot(self) -> GameSnapshot:
        """生成无私人底牌、无行动权限的安全公共快照。

        仅保留已经 SHOW 或正常摊牌公开的牌；该入口供未入座成员和暂停
        旁观者使用，避免先取得全知快照再删字段的泄密风险。
        """
        return self._snapshot_for(perspective=None, public_only=True)

    def _snapshot_for(
        self,
        *,
        perspective: int | None,
        public_only: bool,
    ) -> GameSnapshot:
        st = self._require_state()
        m = len(self._active)
        labels = position_labels(m)
        j = self._active.index(self._button_seat)  # type: ignore[arg-type]
        acting_seat = (
            self._pk_to_seat[st.actor_index] if st.status and st.actor_index is not None else None
        )
        over = not st.status
        if over:
            street = Street.SHOWDOWN if self._showdown else Street.HAND_OVER
        else:
            street = _STREETS[st.street_index]
        players: list[PlayerState] = []
        for seat in range(self.config.player_count):
            if seat not in self._seat_to_pk:
                players.append(
                    PlayerState(
                        seat=seat,
                        name=self._names[seat],
                        stack=self._stacks[seat],
                        bet=0,
                        contribution=0,
                        hole_cards=None,
                        folded=True,
                        all_in=False,
                        is_acting=False,
                        position=None,
                    )
                )
                continue
            pk = self._seat_to_pk[seat]
            folded = seat in self._folded
            hole = self._visible_hole(
                seat,
                folded,
                perspective,
                over,
                public_only=public_only,
            )
            offset = (self._active.index(seat) - j) % m
            players.append(
                PlayerState(
                    seat=seat,
                    name=self._names[seat],
                    stack=st.stacks[pk],
                    bet=st.bets[pk],
                    contribution=self._hand_start_stacks[seat] - st.stacks[pk],
                    hole_cards=hole,
                    folded=folded,
                    all_in=(not folded) and st.stacks[pk] == 0,
                    is_acting=(seat == acting_seat),
                    position=labels[offset],
                )
            )
        pots = tuple(
            PotInfo(amount=p.amount, eligible_seats=tuple(self._pk_to_seat[i] for i in p.player_indices))
            for p in st.pots
        )
        return GameSnapshot(
            hand_id=self._hand_id,
            street=street,
            board=tuple(_card_str(c) for c in st.get_board_cards(0)),
            pots=pots,
            players=tuple(players),
            acting_seat=acting_seat,
            button_seat=self._button_seat,  # type: ignore[arg-type]
            legal_actions=(
                self._legal_actions()
                if acting_seat is not None and not public_only
                else None
            ),
        )

    def _visible_hole(
        self,
        seat: int,
        folded: bool,
        perspective: int | None,
        over: bool,
        *,
        public_only: bool = False,
    ) -> tuple[str, ...] | None:
        """按视角决定底牌可见性。"""
        if not public_only and (perspective is None or seat == perspective):
            return self._hole.get(seat)
        if seat in self._shown_seats:
            return self._hole.get(seat)
        if over and self._showdown and not folded:
            return self._hole.get(seat)  # 摊牌亮出的牌
        return None

    def _legal_actions(self) -> LegalActions | None:
        st = self._state
        if st is None or not st.status or st.actor_index is None:
            return None
        call_amount = st.checking_or_calling_amount if st.can_check_or_call() else 0
        has_wager = max(st.bets) > 0
        if st.can_complete_bet_or_raise_to():
            lo = st.min_completion_betting_or_raising_to_amount
            hi = st.max_completion_betting_or_raising_to_amount
            min_bet_to, max_bet_to = (None, None) if has_wager else (lo, hi)
            min_raise_to, max_raise_to = (lo, hi) if has_wager else (None, None)
            all_in_only = lo == hi
        else:
            min_bet_to = max_bet_to = min_raise_to = max_raise_to = None
            all_in_only = False
        return LegalActions(
            can_fold=st.can_fold(),
            can_check=st.can_check_or_call() and call_amount == 0,
            can_call=st.can_check_or_call() and call_amount > 0,
            call_amount=call_amount,
            min_bet_to=min_bet_to,
            max_bet_to=max_bet_to,
            min_raise_to=min_raise_to,
            max_raise_to=max_raise_to,
            is_all_in_only=all_in_only,
        )

    # ------------------------------------------------------------ 内部

    def _require_state(self) -> State:
        if self._state is None:
            raise RuntimeError("尚未开始任何手牌")
        return self._state

    def _auto_progress(self) -> None:
        """脚本化牌局下,自动完成烧牌/发公共牌(含全下跑码)。

        其余环节(盲注、收池、摊牌结算等)由 pokerkit 自动化完成。
        """
        st = self._state
        if st is None:
            return
        while st.status and st.actor_index is None:
            if self._scripted_board is not None and st.can_burn_card():
                st.burn_card(self._pick_burn_card(st))
            elif self._scripted_board is not None and st.can_deal_board():
                count = 3 if self._board_ptr == 0 else 1
                chunk = "".join(self._scripted_board[self._board_ptr : self._board_ptr + count])
                self._board_ptr += count
                st.deal_board(chunk)
            else:
                break

    def _pick_burn_card(self, st: State) -> str:
        """选一张烧牌:须避开尚未发出的脚本公共牌。"""
        assert self._scripted_board is not None
        remaining = set(self._scripted_board[self._board_ptr :])
        for card in st.get_dealable_cards(len(remaining) + 1):
            text = _card_str(card)
            if text not in remaining:
                return text
        raise RuntimeError("无可用烧牌")  # pragma: no cover - 不会发生

    def _settle(self) -> None:
        """手牌结束:更新筹码、判定胜者、写历史。

        投入修正:唯一最大投入者超出次大投入的部分为未跟注额,会被
        返还,不计入底池。获胜者的“赢得金额” = 净盈亏 + 修正后投入
        (即从底池中分得的总额,与 pokerkit 推池口径一致)。
        """
        st = self._require_state()
        deltas: dict[int, int] = {}
        for seat in self._active:
            pk = self._seat_to_pk[seat]
            deltas[seat] = st.stacks[pk] - self._hand_start_stacks[seat]
            self._stacks[seat] = st.stacks[pk]
        folded_seats = set(self._folded)
        self._showdown = len(self._active) - len(folded_seats) >= 2
        invested = _trim_uncalled(self._invested)
        pots = tuple(_build_pots(invested, folded_seats))
        board = tuple(_card_str(c) for c in st.get_board_cards(0))
        pot_awards = self._extract_pot_awards(pots, board, self._showdown)
        winners: dict[int, int] = {}
        for award in pot_awards:
            for seat, amount in award.payouts:
                winners[seat] = winners.get(seat, 0) + amount
        self._last_result = HandResult(
            hand_id=self._hand_id,
            deltas=deltas,
            winners=winners,
            showdown=self._showdown,
            board=board,
            pots=pots,
            pot_awards=pot_awards,
        )
        if self._writer is not None:
            self._writer.write_hand(
                self._history_record(
                    board,
                    pots,
                    pot_awards,
                    winners,
                    self._showdown,
                )
            )

    def _extract_pot_awards(
        self,
        pots: tuple[PotInfo, ...],
        board: tuple[str, ...],
        showdown: bool,
    ) -> tuple[PotAward, ...]:
        """按每个重建底池的资格与牌力计算真实派彩。

        PokerKit 会在 kill/push 阶段合并同一赢家可获得的相邻底池，
        因而其 ``pot_index`` 不能稳定映射回本项目保留的主池/边池结构。
        这里逐池比较牌力；平分余筹按 PokerKit 座位顺序发放，既保留
        可解释的逐池明细，也与引擎最终筹码一致。
        """
        awards: list[PotAward] = []
        for index, pot in enumerate(pots):
            eligible = tuple(pot.eligible_seats)
            if not eligible:
                raise RuntimeError(f"底池 {index} 没有可获奖座位")
            if showdown and len(eligible) > 1:
                ranks = {
                    seat: evaluate_cards(*self._hole[seat], *board)
                    for seat in eligible
                }
                best = min(ranks.values())
                winning_seats = [seat for seat in eligible if ranks[seat] == best]
            else:
                # 未摊牌时 PotInfo 已排除弃牌者；正常情况下只剩一名。
                winning_seats = list(eligible)
            ordered = sorted(winning_seats, key=self._seat_to_pk.__getitem__)
            share, odd = divmod(pot.amount, len(ordered))
            payout_by_seat = {seat: share for seat in ordered}
            for seat in ordered[:odd]:
                payout_by_seat[seat] += 1
            payouts = tuple(sorted(payout_by_seat.items()))
            awards.append(PotAward(index, pot, payouts))
        return tuple(awards)

    def _history_record(
        self,
        board: tuple[str, ...],
        pots: tuple[PotInfo, ...],
        pot_awards: tuple[PotAward, ...],
        winners: dict[int, int],
        showdown: bool,
    ) -> dict[str, Any]:
        """组装一手牌的历史记录(纯数据)。"""
        return {
            "hand_id": self._hand_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "config": {
                "player_count": self.config.player_count,
                "starting_stack": self.config.starting_stack,
                "small_blind": self.config.small_blind,
                "big_blind": self.config.big_blind,
                "straddle_enabled": self._current_straddle_amount > 0,
                "straddle_amount": self._current_straddle_amount,
            },
            "seats": [
                {
                    "seat": s,
                    "name": self._names[s],
                    "starting_stack": self._hand_start_stacks[s],
                    "hole_cards": list(self._hole[s]),
                }
                for s in self._active
            ],
            "button_seat": self._button_seat,
            "actions": list(self._action_log),
            "board": list(board),
            "showdown": showdown,
            "pots": [
                {"amount": p.amount, "eligible_seats": list(p.eligible_seats)} for p in pots
            ],
            "pot_awards": [
                {
                    "pot_index": award.pot_index,
                    "amount": award.pot.amount,
                    "eligible_seats": list(award.pot.eligible_seats),
                    "payouts": [
                        {"seat": seat, "amount": amount}
                        for seat, amount in award.payouts
                    ],
                }
                for award in pot_awards
            ],
            "winners": [{"seat": s, "amount": a} for s, a in sorted(winners.items())],
            "end_stacks": {str(s): self._stacks[s] for s in self._active},
        }


def _card_str(card: Any) -> str:
    """pokerkit 牌的短格式,如 ``"As"``、``"Td"``。"""
    return f"{card.rank}{card.suit}"


def _trim_uncalled(invested: dict[int, int]) -> dict[int, int]:
    """削去唯一最大投入者的未跟注部分(超出次大投入的部分)。"""
    result = dict(invested)
    amounts = sorted(result.values(), reverse=True)
    if len(amounts) >= 2 and amounts[0] > amounts[1]:
        for seat, amount in result.items():
            if amount == amounts[0]:
                result[seat] = amounts[1]
                break
    return result


def _build_pots(contributions: dict[int, int], folded: set[int]) -> list[PotInfo]:
    """由各方总投入重建主/边池结构(标准边池算法)。"""
    contrib = {s: c for s, c in contributions.items() if c > 0}
    pots: list[PotInfo] = []
    while contrib:
        level = min(contrib.values())
        participants = sorted(contrib)
        amount = level * len(participants)
        eligible = tuple(s for s in participants if s not in folded)
        for s in participants:
            contrib[s] -= level
            if contrib[s] == 0:
                del contrib[s]
        if not eligible:
            continue
        if pots and pots[-1].eligible_seats == eligible:
            last = pots[-1]
            pots[-1] = PotInfo(amount=last.amount + amount, eligible_seats=eligible)
        else:
            pots.append(PotInfo(amount=amount, eligible_seats=eligible))
    return pots
