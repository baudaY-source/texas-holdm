"""手牌回顾与数据统计的纯逻辑层(不依赖 pygame)。

三部分:

- ``HandIndex``:JSONL 历史文件的**行偏移索引**。打开时只扫描字节偏移
  (不解析 JSON),10k 行文件打开远低于 1s;记录按需 seek 解析并缓存,
  列表视图按页(200 条)取最新数据。
- ``HandReview``:单手牌回放标注。从记录重建每个决策点的近似快照,
  由 ``gto.advisor.Advisor`` 计算建议,把人类玩家的每个动作标为
  √(建议频率 ≥0.6)/ ~(0.2-0.6)/ ×(<0.2);翻前未开池时 advisor
  内部即 RFI 图表(≤15bb 为推佊表),与 M4 口径一致。按需计算并缓存。
- ``aggregate_stats``:人类玩家累计统计(手数/盈亏/bb/100/VPIP/PFR/AF/
  分位置盈亏/累计盈亏曲线),VPIP/PFR/AF 口径复用 ``ai.arena`` 的
  ``_record_action``。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from ai.arena import SeatStats, _HandContext, _record_action
from engine.state import (
    ActionType,
    GameSnapshot,
    LegalActions,
    PlayerState,
    Position,
    Street,
    position_labels,
)
from gto.advisor import Advisor, Hint
from ui.respath import user_data_path

DEFAULT_HISTORY_PATH = user_data_path("hands", "history.jsonl")
PAGE_SIZE = 200

# 标注阈值(与 drills 评分口径一致)
MARK_FULL = 0.6
MARK_HALF = 0.2

_STREET_BY_NAME = {
    "PREFLOP": Street.PREFLOP,
    "FLOP": Street.FLOP,
    "TURN": Street.TURN,
    "RIVER": Street.RIVER,
}
_STREET_BOARD_LEN = {Street.PREFLOP: 0, Street.FLOP: 3, Street.TURN: 4, Street.RIVER: 5}
_ACTION_BY_NAME = {t.name: t for t in ActionType}


# ------------------------------------------------------------ 索引


class HandIndex:
    """JSONL 历史的行偏移懒加载索引(0 号 = 最旧的一手)。

    打开时只读一遍文件记录每行字节偏移(二进制流,不解析 JSON),
    10k 行规模打开耗时 << 1s;``record(i)`` 按需 seek 解析并缓存。
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._offsets: list[int] = []
        self._cache: dict[int, dict] = {}
        if self._path.is_file():
            with self._path.open("rb") as f:
                while True:
                    pos = f.tell()
                    line = f.readline()
                    if not line:
                        break
                    # 跳过空行与牌局事件行(带 "type" 键,如买入/移出)
                    if line.strip() and b'"type"' not in line:
                        self._offsets.append(pos)

    def __len__(self) -> int:
        return len(self._offsets)

    @property
    def path(self) -> Path:
        return self._path

    def record(self, index: int) -> dict:
        """按序号(0 = 最旧)解析一手记录,带缓存。"""
        if not 0 <= index < len(self._offsets):
            raise IndexError(index)
        if index not in self._cache:
            with self._path.open("rb") as f:
                f.seek(self._offsets[index])
                line = f.readline()
            self._cache[index] = json.loads(line.decode("utf-8"))
        return self._cache[index]

    def newest_page(self, page: int = 0, size: int = PAGE_SIZE) -> list[tuple[int, dict]]:
        """最新一页 ``(序号, 记录)``,新的在前;``page=0`` 为最近 ``size`` 手。"""
        n = len(self._offsets)
        hi = n - page * size
        lo = max(0, hi - size)
        return [(i, self.record(i)) for i in range(hi - 1, lo - 1, -1)]

    def iter_all(self) -> Iterator[tuple[int, dict]]:
        """从最旧到最新遍历全部记录。"""
        for i in range(len(self._offsets)):
            yield i, self.record(i)


# ------------------------------------------------------------ 单手回放标注


@dataclass(frozen=True)
class AnnotatedAction:
    """一条带标注的动作记录。"""

    index: int  # 在 actions 列表中的下标
    street: str
    seat: int
    name: str
    action: str  # ActionType 名
    amount: int
    is_hero: bool
    mark: str = ""  # "√" / "~" / "×" / ""(非 hero)
    note: str = ""  # 中文点评(建议来源 + 频率)
    freq: float = 0.0  # 所选动作在建议中的频率


@dataclass(frozen=True)
class SeatInfo:
    """记录中一个座位的静态信息。"""

    seat: int
    name: str
    starting_stack: int
    hole_cards: tuple[str, str]
    position: Position | None


def hero_seat_of(record: dict, hero_name: str = "你") -> int | None:
    """按名字找 hero 座位;找不到返回 ``None``。"""
    for s in record.get("seats", []):
        if s.get("name") == hero_name:
            return int(s["seat"])
    return None


def seat_positions(record: dict) -> dict[int, Position]:
    """由按钮位与在局座位重建各座位位置标签(与 engine 同一口径)。"""
    active = [int(s["seat"]) for s in record.get("seats", [])]
    m = len(active)
    if not 2 <= m <= 9:
        return {}
    button = int(record.get("button_seat", active[0]))
    if button not in active:
        button = active[0]
    labels = position_labels(m)
    j = active.index(button)
    return {seat: labels[(active.index(seat) - j) % m] for seat in active}


class HandReview:
    """单手牌的回放与标注。

    :param record: history.jsonl 的一行(已解析的 dict)。
    :param hero_name: 人类玩家名字(默认"你")。
    :param advisor: 建议引擎;缺省按本手大盲新建(低频调用,按需建)。
    """

    def __init__(
        self,
        record: dict,
        hero_name: str = "你",
        advisor: Advisor | None = None,
    ) -> None:
        self.record = record
        self.hero_name = hero_name
        self.hero_seat = hero_seat_of(record, hero_name)
        cfg = record.get("config", {})
        self.big_blind = int(cfg.get("big_blind", 10))
        self._advisor = advisor
        self._annotations: list[AnnotatedAction] | None = None

    # ------------------------------------------------------------ 基础信息

    @property
    def hand_id(self) -> int:
        return int(self.record.get("hand_id", 0))

    @property
    def timestamp(self) -> str:
        return str(self.record.get("timestamp", ""))

    @property
    def board(self) -> list[str]:
        return list(self.record.get("board", []))

    def seats(self) -> list[SeatInfo]:
        positions = seat_positions(self.record)
        out = []
        for s in self.record.get("seats", []):
            seat = int(s["seat"])
            hole = tuple(s.get("hole_cards") or ())
            out.append(
                SeatInfo(
                    seat=seat,
                    name=str(s.get("name", f"P{seat}")),
                    starting_stack=int(s.get("starting_stack", 0)),
                    hole_cards=(hole[0], hole[1]) if len(hole) == 2 else ("?", "?"),
                    position=positions.get(seat),
                )
            )
        return out

    def hero_delta(self) -> int:
        """hero 本手净盈亏(末筹码 - 起始筹码);不在局中为 0。"""
        if self.hero_seat is None:
            return 0
        end = self.record.get("end_stacks", {})
        start = {
            int(s["seat"]): int(s.get("starting_stack", 0))
            for s in self.record.get("seats", [])
        }
        return int(end.get(str(self.hero_seat), start.get(self.hero_seat, 0))) - start.get(
            self.hero_seat, 0
        )

    def total_pot(self) -> int:
        return sum(int(p.get("amount", 0)) for p in self.record.get("pots", []))

    def summary_line(self) -> str:
        """列表行:`#12 翻牌 Ah7d2c · 你 +120`。"""
        board = " ".join(self.board) if self.board else "(翻前结束)"
        delta = self.hero_delta()
        sign = f"+{delta}" if delta > 0 else str(delta)
        return f"#{self.hand_id} {board} · {sign}"

    # ------------------------------------------------------------ 标注

    def annotations(self) -> list[AnnotatedAction]:
        """逐步标注全部动作(懒计算,缓存)。"""
        if self._annotations is None:
            self._annotations = self._build_annotations()
        return self._annotations

    def _advisor_instance(self) -> Advisor:
        if self._advisor is None:
            self._advisor = Advisor(big_blind=self.big_blind, equity_trials=60, seed=1)
        return self._advisor

    def _build_annotations(self) -> list[AnnotatedAction]:
        """沿动作日志重建每个决策点快照,为 hero 动作请求 advisor 建议。"""
        seats = {s.seat: s for s in self.seats()}
        actions = list(self.record.get("actions", []))
        out: list[AnnotatedAction] = []
        if not seats:
            return out
        # 逐步追踪的牌桌状态
        stacks = {s.seat: s.starting_stack for s in seats.values()}
        bets = {s.seat: 0 for s in seats.values()}
        folded: set[int] = set()
        pot = 0
        street = Street.PREFLOP
        # 翻前初始:按位置下盲注(与 engine 口径一致;HU 时 BTN 下小盲)
        config = self.record.get("config", {})
        sb = int(config.get("small_blind", self.big_blind // 2))
        straddle_amount = (
            int(config.get("straddle_amount", 0))
            if config.get("straddle_enabled") and len(seats) >= 8
            else 0
        )
        for s in seats.values():
            blind = 0
            if s.position is Position.SB or (s.position is Position.BTN and len(seats) == 2):
                blind = sb
            elif s.position is Position.BB:
                blind = self.big_blind
            elif s.position is Position.UTG and straddle_amount > 0:
                blind = straddle_amount
            if blind:
                posted = min(blind, stacks[s.seat])
                bets[s.seat] = posted
                stacks[s.seat] -= posted
        # 只有交满 2BB 才构成完整 live straddle；短码部分投入仍沿用 BB
        # 作为下一次完整加注增量。
        posted_straddle = max(
            (
                bets[s.seat]
                for s in seats.values()
                if s.position is Position.UTG and straddle_amount > 0
            ),
            default=0,
        )
        min_raise_increment = (
            posted_straddle
            if posted_straddle >= self.big_blind * 2
            else self.big_blind
        )
        for idx, a in enumerate(actions):
            a_street = _STREET_BY_NAME.get(str(a.get("street", "")))
            if a_street is None:
                out.append(self._plain(idx, a, seats))
                continue
            if a_street is not street:
                # 街道推进:本街下注收池
                pot += sum(bets.values())
                bets = {s: 0 for s in bets}
                street = a_street
                min_raise_increment = self.big_blind
            seat = int(a.get("seat", -1))
            kind = _ACTION_BY_NAME.get(str(a.get("action", "")))
            amount = int(a.get("amount", 0))
            if seat not in seats or kind is None:
                continue
            is_hero = seat == self.hero_seat
            mark, note, freq = "", "", 0.0
            if is_hero:
                mark, note, freq = self._annotate(
                    street,
                    seat,
                    seats,
                    stacks,
                    bets,
                    folded,
                    pot,
                    kind,
                    min_raise_increment,
                )
            out.append(
                AnnotatedAction(
                    index=idx,
                    street=a.get("street", ""),
                    seat=seat,
                    name=seats[seat].name,
                    action=kind.name,
                    amount=amount,
                    is_hero=is_hero,
                    mark=mark,
                    note=note,
                    freq=freq,
                )
            )
            previous_max = max(bets.values(), default=0)
            self._apply(kind, seat, amount, bets, stacks, folded)
            if kind in (ActionType.BET, ActionType.RAISE, ActionType.ALLIN):
                increment = amount - previous_max
                if increment >= min_raise_increment:
                    min_raise_increment = increment
        return out

    @staticmethod
    def _plain(idx: int, a: dict, seats: dict[int, SeatInfo]) -> AnnotatedAction:
        seat = int(a.get("seat", -1))
        return AnnotatedAction(
            index=idx,
            street=str(a.get("street", "")),
            seat=seat,
            name=seats[seat].name if seat in seats else f"P{seat}",
            action=str(a.get("action", "")),
            amount=int(a.get("amount", 0)),
            is_hero=False,
        )

    @staticmethod
    def _apply(
        kind: ActionType,
        seat: int,
        amount: int,
        bets: dict[int, int],
        stacks: dict[int, int],
        folded: set[int],
    ) -> None:
        """把一条动作应用到追踪状态(bets/stacks/folded)。"""
        if kind is ActionType.FOLD:
            folded.add(seat)
        elif kind is ActionType.CALL:
            bets[seat] += amount
            stacks[seat] -= amount
        elif kind in (ActionType.BET, ActionType.RAISE):
            # amount 为"加注到"的本街总额
            stacks[seat] -= amount - bets[seat]
            bets[seat] = amount
        elif kind is ActionType.ALLIN:
            stacks[seat] -= amount - bets[seat]
            bets[seat] = amount

    def _annotate(
        self,
        street: Street,
        seat: int,
        seats: dict[int, SeatInfo],
        stacks: dict[int, int],
        bets: dict[int, int],
        folded: set[int],
        pot: int,
        chosen: ActionType,
        min_raise_increment: int,
    ) -> tuple[str, str, float]:
        """对 hero 的一次决策求建议并打分;失败时给空标注。"""
        try:
            hint = self._hint(
                street,
                seat,
                seats,
                stacks,
                bets,
                folded,
                pot,
                min_raise_increment,
            )
        except Exception:
            return "", "", 0.0
        if hint is None:
            return "", "", 0.0
        freq = _chosen_freq(hint.action_freqs, chosen)
        if freq >= MARK_FULL:
            mark = "√"
        elif freq >= MARK_HALF:
            mark = "~"
        else:
            mark = "×"
        top = max(hint.action_freqs.items(), key=lambda kv: kv[1], default=("", 0.0))
        note = (
            f"建议 {hint.source}:{_fmt_freqs(hint.action_freqs)}"
            f"(首选 {_ACTION_CN.get(top[0], top[0])} {top[1]:.0%})"
        )
        return mark, note, freq

    def _hint(
        self,
        street: Street,
        seat: int,
        seats: dict[int, SeatInfo],
        stacks: dict[int, int],
        bets: dict[int, int],
        folded: set[int],
        pot: int,
        min_raise_increment: int,
    ) -> Hint | None:
        """重建该决策点的近似快照并求建议(多街共用)。"""
        board = tuple(self.board[: _STREET_BOARD_LEN[street]])
        max_bet = max(bets.values(), default=0)
        my_bet = bets[seat]
        call_amount = max(0, max_bet - my_bet)
        stack = stacks[seat]
        can_wager = stack > call_amount
        max_wager_to = my_bet + stack
        min_bet_to = (
            min(self.big_blind, max_wager_to)
            if max_bet == 0 and can_wager
            else None
        )
        min_raise_to = (
            min(max_bet + min_raise_increment, max_wager_to)
            if max_bet > 0 and can_wager
            else None
        )
        players: list[PlayerState] = []
        count = int(self.record.get("config", {}).get("player_count", len(seats)))
        for s in range(count):
            info = seats.get(s)
            if info is None:
                players.append(
                    PlayerState(s, f"P{s}", 0, 0, 0, None, True, False, False, None)
                )
                continue
            players.append(
                PlayerState(
                    seat=s,
                    name=info.name,
                    stack=stacks[s],
                    bet=bets[s],
                    contribution=info.starting_stack - stacks[s],
                    hole_cards=info.hole_cards if s == seat else None,
                    folded=s in folded,
                    all_in=stacks[s] == 0 and s not in folded,
                    is_acting=s == seat,
                    position=info.position,
                )
            )
        legal = LegalActions(
            can_fold=True,
            can_check=call_amount == 0,
            can_call=call_amount > 0 and stack > 0,
            call_amount=min(call_amount, stack),
            min_bet_to=min_bet_to,
            max_bet_to=(max_wager_to if max_bet == 0 and can_wager else None),
            min_raise_to=min_raise_to,
            max_raise_to=(max_wager_to if max_bet > 0 and can_wager else None),
            is_all_in_only=(
                min_raise_to is not None
                and min_raise_to == max_wager_to
                and max_wager_to < max_bet + min_raise_increment
            ),
        )
        snap = GameSnapshot(
            hand_id=self.hand_id,
            street=street,
            board=board,
            pots=(),
            players=tuple(players),
            acting_seat=seat,
            button_seat=int(self.record.get("button_seat", 0)),
            legal_actions=legal,
        )
        # total_pot 为 pots 之和;把收池额以单池形式给出
        if pot > 0:
            from engine.state import PotInfo

            snap = GameSnapshot(
                hand_id=snap.hand_id,
                street=snap.street,
                board=snap.board,
                pots=(PotInfo(pot, tuple()),),
                players=snap.players,
                acting_seat=snap.acting_seat,
                button_seat=snap.button_seat,
                legal_actions=snap.legal_actions,
            )
        return self._advisor_instance().hint(snap, seat)


# ------------------------------------------------------------ 统计聚合


@dataclass
class HeroStats:
    """人类玩家累计统计。"""

    hands: int = 0
    profit: int = 0
    bb100: float = 0.0
    vpip: float = 0.0  # 百分比
    pfr: float = 0.0
    af: float = 0.0
    per_position: dict[str, int] = field(default_factory=dict)  # 位置 -> 盈亏
    profit_curve: list[int] = field(default_factory=list)  # 按时间顺序的累计盈亏


def aggregate_stats(
    records: Iterator[dict] | list[dict],
    hero_name: str = "你",
    big_blind: int | None = None,
) -> HeroStats:
    """从记录流聚合 hero 统计(VPIP/PFR/AF 口径复用 ``ai.arena``)。"""
    st = SeatStats()
    hands = 0
    profit = 0
    per_position: dict[str, int] = {}
    curve: list[int] = []
    bb = big_blind
    for rec in records:
        seat = hero_seat_of(rec, hero_name)
        if seat is None:
            continue
        if bb is None:
            bb = int(rec.get("config", {}).get("big_blind", 10))
        hands += 1
        start = int(
            next(s.get("starting_stack", 0) for s in rec["seats"] if int(s["seat"]) == seat)
        )
        delta = int(rec.get("end_stacks", {}).get(str(seat), start)) - start
        profit += delta
        curve.append(profit)
        pos = seat_positions(rec).get(seat)
        if pos is not None:
            per_position[pos.name] = per_position.get(pos.name, 0) + delta
        ctx = _HandContext()
        for a in rec.get("actions", []):
            # 只喂 hero 的动作:3bet 口径依赖全桌动作序列,本模块不展示 3bet,
            # 因此 VPIP/PFR/AF 与 arena 完全一致,3bet 计数略偏但不消费。
            if int(a.get("seat", -1)) != seat:
                continue
            kind = _ACTION_BY_NAME.get(str(a.get("action", "")))
            street = _STREET_BY_NAME.get(str(a.get("street", "")))
            if kind is None or street is None:
                continue
            _record_action(st, kind, street, ctx, seat)
    out = HeroStats(hands=hands, profit=profit, per_position=per_position, profit_curve=curve)
    if hands:
        out.vpip = round(100.0 * st.vpip / hands, 1)
        out.pfr = round(100.0 * st.pfr / hands, 1)
        out.bb100 = round(profit / (bb or 10) / hands * 100, 1)
    out.af = round(st.agg / st.calls, 2) if st.calls else (float(st.agg) if st.agg else 0.0)
    return out


# ------------------------------------------------------------ 工具

_ACTION_CN = {
    "FOLD": "弃牌",
    "CHECK": "过牌",
    "CALL": "跟注",
    "BET": "下注",
    "RAISE": "加注",
    "ALLIN": "全下",
}


def action_cn(action: str) -> str:
    """动作键 → 中文。"""
    return _ACTION_CN.get(action, action)


def _chosen_freq(freqs: dict[str, float], chosen: ActionType) -> float:
    """所选动作在建议频率中的值。"""
    return freqs.get(chosen.name, 0.0)


def _fmt_freqs(freqs: dict[str, float]) -> str:
    parts = [f"{_ACTION_CN.get(a, a)} {f:.0%}" for a, f in freqs.items() if f > 0.005]
    return " / ".join(parts) if parts else "—"
