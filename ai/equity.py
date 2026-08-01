"""基于 phevaluator 的快速胜率(equity)工具。

phevaluator 0.6.0 关键事实(均已实测):
- ``evaluate_cards(*cards)`` 接受 5-7 张牌,字符串格式 ``"As"``/``"Td"``;
- 返回整数等级,**越小越强**(1 = 皇家同花顺,最差约 7462);
- 单次评估约 1.5µs,河牌圈对随机对手精确枚举(≤990 组)仅需约 3ms。

本模块只做纯逻辑,不依赖 pygame/pokerkit。
"""
from __future__ import annotations

import random
from itertools import combinations

from phevaluator import evaluate_cards

RANKS = "23456789TJQKA"
SUITS = "cdhs"
DECK: tuple[str, ...] = tuple(r + s for r in RANKS for s in SUITS)

# phevaluator 五张牌等级表的范围:1(最强)..7462(最弱)
_MAX_RANK = 7462

# 默认蒙特卡洛试验次数(单次调用目标:低个位数毫秒)
DEFAULT_TRIALS = 400


def _remaining_deck(known: set[str]) -> list[str]:
    return [c for c in DECK if c not in known]


def _showdown_score(hero_rank: int, villain_ranks: list[int]) -> float:
    """单局结算得分:胜 1.0,并列均分,负 0.0。"""
    best = min([hero_rank, *villain_ranks])
    winners = 1 + sum(1 for r in villain_ranks if r == best)
    if hero_rank > best:
        return 0.0
    return 1.0 / winners


def equity_vs_random(
    hole: list[str] | tuple[str, ...],
    board: list[str] | tuple[str, ...],
    n_opponents: int = 1,
    n_trials: int = DEFAULT_TRIALS,
    rng: random.Random | None = None,
) -> float:
    """估计 hero 对 ``n_opponents`` 个随机对手的胜率(0..1)。

    河牌(5 张公共牌)且单挑时做精确枚举;其余情况蒙特卡洛抽样。
    """
    hole = list(hole)
    board = list(board)
    if len(hole) != 2:
        raise ValueError("hole 须为两张牌")
    if len(board) > 5:
        raise ValueError("board 至多 5 张牌")
    known = set(hole) | set(board)
    if len(known) != len(hole) + len(board):
        raise ValueError("hole 与 board 存在重复牌")
    rng = rng or random

    if len(board) == 5 and n_opponents == 1:
        # 精确枚举对手全部两牌组合
        hero_rank = evaluate_cards(*hole, *board)
        rest = _remaining_deck(known)
        total = 0.0
        n = 0
        for v1, v2 in combinations(rest, 2):
            villain_rank = evaluate_cards(v1, v2, *board)
            total += _showdown_score(hero_rank, [villain_rank])
            n += 1
        return total / n if n else 0.0

    need_board = 5 - len(board)
    rest = _remaining_deck(known)
    total = 0.0
    valid = 0
    for _ in range(n_trials):
        draw = rng.sample(rest, need_board + 2 * n_opponents)
        runout = board + draw[:need_board]
        villains = draw[need_board:]
        hero_rank = evaluate_cards(*hole, *runout)
        villain_ranks = [
            evaluate_cards(villains[2 * i], villains[2 * i + 1], *runout)
            for i in range(n_opponents)
        ]
        total += _showdown_score(hero_rank, villain_ranks)
        valid += 1
    return total / valid if valid else 0.0


def equity_vs_range(
    hole: list[str] | tuple[str, ...],
    board: list[str] | tuple[str, ...],
    range_combos: list[tuple[str, str]],
    n_trials: int = DEFAULT_TRIALS,
    rng: random.Random | None = None,
) -> float:
    """估计 hero 对一个范围(对手两牌组合列表)的胜率。

    与已知牌冲突的组合会被剔除;每次试验均匀抽取一个剩余组合,
    再随机补全公共牌。
    """
    hole = list(hole)
    board = list(board)
    known = set(hole) | set(board)
    combos = [c for c in range_combos if c[0] not in known and c[1] not in known]
    if not combos:
        raise ValueError("范围内无可用组合(全部与已知牌冲突)")
    rng = rng or random
    need_board = 5 - len(board)
    rest = _remaining_deck(known)
    total = 0.0
    for _ in range(n_trials):
        v1, v2 = combos[rng.randrange(len(combos))]
        live = [c for c in rest if c != v1 and c != v2]
        if need_board:
            runout = board + rng.sample(live, need_board)
        else:
            runout = board
        hero_rank = evaluate_cards(*hole, *runout)
        villain_rank = evaluate_cards(v1, v2, *runout)
        total += _showdown_score(hero_rank, [villain_rank])
    return total / n_trials if n_trials else 0.0


def equity_vs_hands(
    hole: list[str] | tuple[str, ...],
    board: list[str] | tuple[str, ...],
    villain_holes: list[tuple[str, str]],
    n_trials: int = DEFAULT_TRIALS,
    rng: random.Random | None = None,
) -> float:
    """估计 hero 对若干**固定底牌**对手的胜率(只随机补全公共牌)。

    河牌(无未知牌)时结果精确;多方胜率之和应 ≈ 1。
    """
    hole = list(hole)
    board = list(board)
    known = set(hole) | set(board)
    for v in villain_holes:
        if v[0] in known or v[1] in known:
            raise ValueError(f"对手底牌 {v} 与已知牌冲突")
        known |= set(v)
    rng = rng or random
    need_board = 5 - len(board)
    rest = _remaining_deck(known)
    total = 0.0
    for _ in range(n_trials if need_board else 1):
        runout = board + rng.sample(rest, need_board) if need_board else board
        hero_rank = evaluate_cards(*hole, *runout)
        villain_ranks = [evaluate_cards(v[0], v[1], *runout) for v in villain_holes]
        total += _showdown_score(hero_rank, villain_ranks)
    trials = n_trials if need_board else 1
    return total / trials


def hand_strength(
    hole: list[str] | tuple[str, ...],
    board: list[str] | tuple[str, ...],
) -> float:
    """当前成手强度,归一化到 [0,1](1 = 坚果)。

    翻牌前(无法构成 5 张牌)使用简单的解析启发式;
    翻牌后直接用 phevaluator 等级归一化。
    """
    hole = list(hole)
    board = list(board)
    cards = hole + board
    if len(cards) >= 5:
        rank = evaluate_cards(*cards)
        return (_MAX_RANK - rank) / (_MAX_RANK - 1)
    if len(hole) == 2 and not board:
        return preflop_strength(hole[0], hole[1])
    raise ValueError("牌数不足,无法评估")


_RANK_VALUE = {r: i + 2 for i, r in enumerate(RANKS)}


def preflop_strength(c1: str, c2: str) -> float:
    """两张底牌的解析强度估计(0..1),仅供快速启发式使用。"""
    v = sorted((_RANK_VALUE[c1[0]], _RANK_VALUE[c2[0]]), reverse=True)
    hi, lo = v
    if hi == lo:  # 对子
        return 0.55 + (hi - 2) / 12 * 0.45
    s = 0.15 + (hi - 2) / 12 * 0.35 + (lo - 2) / 12 * 0.25
    if c1[1] == c2[1]:  # 同花
        s += 0.06
    gap = hi - lo - 1
    if gap == 0:
        s += 0.04
    elif gap == 1:
        s += 0.02
    elif gap >= 4:
        s -= 0.04
    return max(0.0, min(1.0, s))
