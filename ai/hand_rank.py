"""德州扑克当前牌型、最佳五张与视觉高亮语义。

牌型比较仍交给 ``phevaluator``；本模块枚举 5–7 张牌中的五张组合，
找出最强组合后再用公开的扑克牌规则解释牌型。这样无需依赖第三方库
未公开的 rank 区间，也能准确指出真正构成牌型的牌（不把踢脚高亮）。
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import IntEnum
from itertools import combinations
from typing import Sequence

from phevaluator import evaluate_cards


class HandCategory(IntEnum):
    """按从弱到强排列的标准五张牌类别。"""

    HIGH_CARD = 0
    ONE_PAIR = 1
    TWO_PAIR = 2
    THREE_OF_A_KIND = 3
    STRAIGHT = 4
    FLUSH = 5
    FULL_HOUSE = 6
    FOUR_OF_A_KIND = 7
    STRAIGHT_FLUSH = 8


@dataclass(frozen=True)
class HandSummary:
    """给 UI 使用的当前牌力摘要。"""

    label: str
    category: HandCategory
    best_five: tuple[str, ...]
    highlight_cards: frozenset[str]


_RANK_VALUE = {
    "2": 2,
    "3": 3,
    "4": 4,
    "5": 5,
    "6": 6,
    "7": 7,
    "8": 8,
    "9": 9,
    "T": 10,
    "J": 11,
    "Q": 12,
    "K": 13,
    "A": 14,
}
_VALUE_LABEL = {value: rank for rank, value in _RANK_VALUE.items()}
_VALID_SUITS = frozenset("shdc")
_VALID_BOARD_COUNTS = frozenset((0, 3, 4, 5))


def _validate(hole: tuple[str, ...], board: tuple[str, ...]) -> None:
    if len(hole) != 2:
        raise ValueError("德州底牌必须恰好两张")
    if len(board) not in _VALID_BOARD_COUNTS:
        raise ValueError("公共牌必须为 0、3、4 或 5 张")
    cards = hole + board
    for card in cards:
        if (
            len(card) != 2
            or card[0] not in _RANK_VALUE
            or card[1] not in _VALID_SUITS
        ):
            raise ValueError(f"非法牌码：{card!r}")
    if len(set(cards)) != len(cards):
        raise ValueError("牌组中出现重复牌")


def _straight_high(values: Sequence[int]) -> int | None:
    unique = set(values)
    if unique == {14, 2, 3, 4, 5}:
        return 5
    if len(unique) == 5 and max(unique) - min(unique) == 4:
        return max(unique)
    return None


def _classify_five(cards: tuple[str, ...]) -> HandCategory:
    values = [_RANK_VALUE[card[0]] for card in cards]
    counts = Counter(values)
    groups = sorted(counts.values(), reverse=True)
    flush = len({card[1] for card in cards}) == 1
    straight = _straight_high(values) is not None
    if flush and straight:
        return HandCategory.STRAIGHT_FLUSH
    if groups == [4, 1]:
        return HandCategory.FOUR_OF_A_KIND
    if groups == [3, 2]:
        return HandCategory.FULL_HOUSE
    if flush:
        return HandCategory.FLUSH
    if straight:
        return HandCategory.STRAIGHT
    if groups == [3, 1, 1]:
        return HandCategory.THREE_OF_A_KIND
    if groups == [2, 2, 1]:
        return HandCategory.TWO_PAIR
    if groups == [2, 1, 1, 1]:
        return HandCategory.ONE_PAIR
    return HandCategory.HIGH_CARD


def _highlight_core(
    cards: tuple[str, ...], category: HandCategory
) -> frozenset[str]:
    if category is HandCategory.HIGH_CARD:
        return frozenset()
    if category in (
        HandCategory.STRAIGHT,
        HandCategory.FLUSH,
        HandCategory.FULL_HOUSE,
        HandCategory.STRAIGHT_FLUSH,
    ):
        return frozenset(cards)
    counts = Counter(card[0] for card in cards)
    if category is HandCategory.ONE_PAIR:
        ranks = {rank for rank, count in counts.items() if count == 2}
    elif category is HandCategory.TWO_PAIR:
        ranks = {rank for rank, count in counts.items() if count == 2}
    elif category is HandCategory.THREE_OF_A_KIND:
        ranks = {rank for rank, count in counts.items() if count == 3}
    else:  # FOUR_OF_A_KIND
        ranks = {rank for rank, count in counts.items() if count == 4}
    return frozenset(card for card in cards if card[0] in ranks)


def _label(cards: tuple[str, ...], category: HandCategory) -> str:
    values = [_RANK_VALUE[card[0]] for card in cards]
    counts = Counter(values)
    if category is HandCategory.HIGH_CARD:
        return f"高牌 · {_VALUE_LABEL[max(values)]}"
    if category is HandCategory.ONE_PAIR:
        pair = max(value for value, count in counts.items() if count == 2)
        return f"一对 · {_VALUE_LABEL[pair]}"
    if category is HandCategory.TWO_PAIR:
        pairs = sorted(
            (value for value, count in counts.items() if count == 2), reverse=True
        )
        return f"两对 · {_VALUE_LABEL[pairs[0]]} / {_VALUE_LABEL[pairs[1]]}"
    if category is HandCategory.THREE_OF_A_KIND:
        trips = max(value for value, count in counts.items() if count == 3)
        return f"三条 · {_VALUE_LABEL[trips]}"
    if category is HandCategory.STRAIGHT:
        high = _straight_high(values)
        assert high is not None
        return f"顺子 · {_VALUE_LABEL[high]} 高"
    if category is HandCategory.FLUSH:
        return f"同花 · {_VALUE_LABEL[max(values)]} 高"
    if category is HandCategory.FULL_HOUSE:
        trips = next(value for value, count in counts.items() if count == 3)
        pair = next(value for value, count in counts.items() if count == 2)
        return f"葫芦 · {_VALUE_LABEL[trips]} 带 {_VALUE_LABEL[pair]}"
    if category is HandCategory.FOUR_OF_A_KIND:
        quads = next(value for value, count in counts.items() if count == 4)
        return f"四条 · {_VALUE_LABEL[quads]}"
    high = _straight_high(values)
    assert high is not None
    if high == 14:
        return "皇家同花顺"
    return f"同花顺 · {_VALUE_LABEL[high]} 高"


def _preflop_summary(hole: tuple[str, str]) -> HandSummary:
    first, second = hole
    if first[0] == second[0]:
        return HandSummary(
            label=f"口袋对子 · {first[0]}",
            category=HandCategory.ONE_PAIR,
            best_five=(),
            highlight_cards=frozenset(hole),
        )
    ordered = sorted(
        (first[0], second[0]), key=lambda rank: _RANK_VALUE[rank], reverse=True
    )
    suited = "同花" if first[1] == second[1] else "非同花"
    return HandSummary(
        label=f"{ordered[0]}{ordered[1]} · {suited}",
        category=HandCategory.HIGH_CARD,
        best_five=(),
        highlight_cards=frozenset(),
    )


def describe_holdem_hand(
    hole_cards: Sequence[str], board_cards: Sequence[str]
) -> HandSummary:
    """解释玩家当前可见的德州牌力。

    同一最终 rank 有多套五张组合时，优先选择使用更少底牌的组合。
    因而公共牌已经独立构成最佳牌时，视觉上只会高亮公共牌，不会误导
    玩家以为同点数的底牌参与了成牌。
    """

    hole = tuple(hole_cards)
    board = tuple(board_cards)
    _validate(hole, board)
    if not board:
        return _preflop_summary((hole[0], hole[1]))

    all_cards = hole + board
    best_key: tuple[int, int, tuple[int, ...]] | None = None
    best_five: tuple[str, ...] | None = None
    for indices in combinations(range(len(all_cards)), 5):
        candidate = tuple(all_cards[index] for index in indices)
        rank = evaluate_cards(*candidate)
        hole_count = sum(index < 2 for index in indices)
        key = (rank, hole_count, indices)
        if best_key is None or key < best_key:
            best_key = key
            best_five = candidate

    assert best_five is not None
    category = _classify_five(best_five)
    return HandSummary(
        label=_label(best_five, category),
        category=category,
        best_five=best_five,
        highlight_cards=_highlight_core(best_five, category),
    )
