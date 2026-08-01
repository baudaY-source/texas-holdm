"""牌桌快照数据契约。

本模块定义引擎对外的核心数据结构,供 UI、AI 机器人与 GTO 求解器桥
三方消费。全部为不可变 dataclass,仅含纯数据,不依赖 pokerkit 类型。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto


class Street(Enum):
    """牌局所处的街道。"""

    PREFLOP = auto()
    FLOP = auto()
    TURN = auto()
    RIVER = auto()
    SHOWDOWN = auto()  # 进入摊牌(手牌已结束)
    HAND_OVER = auto()  # 未摊牌即结束(对手弃牌)


class Position(Enum):
    """位置标签。heads-up 时按钮位兼任小盲,统一标 BTN。

    ``UTG1`` / ``UTG2`` 分别表示常写作 ``UTG+1`` / ``UTG+2``
    的全环桌前位；``LJ`` / ``HJ`` 为 lowjack / hijack。
    """

    BTN = auto()
    SB = auto()
    BB = auto()
    UTG = auto()
    UTG1 = auto()
    UTG2 = auto()
    LJ = auto()
    HJ = auto()
    MP = auto()
    CO = auto()


# 各人数下,按“从按钮位起顺时针”的偏移给出的位置标签表。
_POSITION_TABLE: dict[int, tuple[Position, ...]] = {
    2: (Position.BTN, Position.BB),
    3: (Position.BTN, Position.SB, Position.BB),
    4: (Position.BTN, Position.SB, Position.BB, Position.UTG),
    5: (Position.BTN, Position.SB, Position.BB, Position.UTG, Position.CO),
    6: (
        Position.BTN,
        Position.SB,
        Position.BB,
        Position.UTG,
        Position.MP,
        Position.CO,
    ),
    7: (
        Position.BTN,
        Position.SB,
        Position.BB,
        Position.UTG,
        Position.MP,
        Position.HJ,
        Position.CO,
    ),
    8: (
        Position.BTN,
        Position.SB,
        Position.BB,
        Position.UTG,
        Position.UTG1,
        Position.LJ,
        Position.HJ,
        Position.CO,
    ),
    9: (
        Position.BTN,
        Position.SB,
        Position.BB,
        Position.UTG,
        Position.UTG1,
        Position.UTG2,
        Position.LJ,
        Position.HJ,
        Position.CO,
    ),
}


def position_labels(player_count: int, button_offset: int = 0) -> tuple[Position, ...]:
    """返回各座位的位置标签。

    :param player_count: 本手牌参与人数(2-9)。
    :param button_offset: 按钮位在返回序列中的下标,默认 0。
    :return: 与座位顺序对应的 Position 元组,``result[i]`` 为
        ``(button_offset + i) % player_count`` 号座位…… 即结果按
        “按钮位开始顺时针”排列,调用方自行映射回真实座位。
    """
    if player_count not in _POSITION_TABLE:
        raise ValueError(f"不支持的人数: {player_count}")
    labels = _POSITION_TABLE[player_count]
    return labels  # 已按“自按钮位起”的顺序定义,无需再旋转


class ActionType(Enum):
    """动作类型。BET/RAISE 的具体额度由 Action.amount 携带。"""

    FOLD = auto()
    CHECK = auto()
    CALL = auto()
    BET = auto()  # 本街道首个下注
    RAISE = auto()  # 在已有下注基础上加注
    ALLIN = auto()  # 全下(额度 = 剩余筹码 + 本街已下注)


@dataclass(frozen=True)
class Action:
    """一次玩家动作。

    :param seat: 行动者座位号。
    :param action_type: 动作类型。
    :param amount: 仅 BET/RAISE/ALLIN 有意义,为“加注到”的总额
        (本街道该玩家投入的总筹码);CALL 时为需补的跟注额;其余为 0。
    """

    seat: int
    action_type: ActionType
    amount: int = 0


@dataclass(frozen=True)
class PlayerState:
    """单个玩家在快照时刻的状态。

    :param hole_cards: 明牌列表(如 ``("As", "Td")``);被隐藏的
        对手为 ``None``。
    :param bet: 本街道已投入筹码。
    :param contribution: 本手牌累计投入筹码。
    """

    seat: int
    name: str
    stack: int
    bet: int
    contribution: int
    hole_cards: tuple[str, ...] | None
    folded: bool
    all_in: bool
    is_acting: bool
    position: Position | None


@dataclass(frozen=True)
class PotInfo:
    """一个(边)池。"""

    amount: int
    eligible_seats: tuple[int, ...]  # 有资格赢取该池的座位


@dataclass(frozen=True)
class LegalActions:
    """当前行动者的合法动作集合。

    ``min/max_bet_to`` 在本街道尚无下注时有效,``min/max_raise_to``
    在已有下注时有效;不适用的一对为 ``None``。额度均为“到”总额。
    """

    can_fold: bool
    can_check: bool
    can_call: bool
    call_amount: int
    min_bet_to: int | None
    max_bet_to: int | None
    min_raise_to: int | None
    max_raise_to: int | None
    is_all_in_only: bool  # 仅能全下(最小加注额即全部筹码)


@dataclass(frozen=True)
class GameSnapshot:
    """一手牌某一时刻的完整快照(引擎层中心数据契约)。"""

    hand_id: int
    street: Street
    board: tuple[str, ...]
    pots: tuple[PotInfo, ...]
    players: tuple[PlayerState, ...]
    acting_seat: int | None  # 手牌结束或等待发牌时为 None
    button_seat: int
    legal_actions: LegalActions | None  # 仅在有行动者时非 None

    @property
    def total_pot(self) -> int:
        """总底池(各池之和,不含本街道尚未收池的下注)。"""
        return sum(p.amount for p in self.pots)

    @property
    def main_pot(self) -> int:
        """主池金额;无池时为 0。"""
        return self.pots[0].amount if self.pots else 0
