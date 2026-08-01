"""随机行动模拟器。

用法::

    python -m engine.simulate --hands 200 --players 6 --seed 1

以均匀随机的合法动作打完若干手牌,逐手断言筹码守恒
(总和恒等于 人数 x 起始筹码),并把每手写入 JSONL 历史。
游戏提前结束(仅剩一人有筹码)时自动开新局,直到凑满手数。
"""
from __future__ import annotations

import argparse
import random
import sys
from dataclasses import dataclass, field

from .game import Table, TableConfig
from .history import HandHistoryWriter
from .state import Action, ActionType, GameSnapshot


@dataclass
class GameStats:
    """单局统计。"""

    hands: int = 0
    showdowns: int = 0
    final_stacks: tuple[int, ...] = field(default_factory=tuple)


def random_action(rng: random.Random, snap: GameSnapshot) -> Action:
    """从合法动作中均匀随机选择一个,额度在上下界内均匀取。"""
    legal = snap.legal_actions
    assert legal is not None and snap.acting_seat is not None
    seat = snap.acting_seat
    choices: list[ActionType] = []
    if legal.can_fold:
        choices.append(ActionType.FOLD)
    if legal.can_check:
        choices.append(ActionType.CHECK)
    if legal.can_call:
        choices.append(ActionType.CALL)
    if legal.min_bet_to is not None:
        choices.append(ActionType.BET)
    if legal.min_raise_to is not None:
        choices.append(ActionType.RAISE)
    kind = rng.choice(choices)
    if kind is ActionType.BET:
        assert legal.min_bet_to is not None and legal.max_bet_to is not None
        amount = rng.randint(legal.min_bet_to, legal.max_bet_to)
    elif kind is ActionType.RAISE:
        assert legal.min_raise_to is not None and legal.max_raise_to is not None
        amount = rng.randint(legal.min_raise_to, legal.max_raise_to)
    elif kind is ActionType.CALL:
        amount = legal.call_amount
    else:
        amount = 0
    return Action(seat=seat, action_type=kind, amount=amount)


def main(argv: list[str] | None = None) -> int:
    """CLI 入口;返回进程退出码。"""
    parser = argparse.ArgumentParser(description="随机行动模拟器")
    parser.add_argument("--hands", type=int, default=200, help="总手数")
    parser.add_argument("--players", type=int, default=6, help="人数(2-6)")
    parser.add_argument("--seed", type=int, default=1, help="随机种子")
    parser.add_argument("--stack", type=int, default=200, help="起始筹码")
    parser.add_argument("--small-blind", type=int, default=5)
    parser.add_argument("--big-blind", type=int, default=10)
    parser.add_argument("--history", default="hands/history.jsonl", help="JSONL 输出路径")
    args = parser.parse_args(argv)

    cfg = TableConfig(
        player_count=args.players,
        starting_stack=args.stack,
        small_blind=args.small_blind,
        big_blind=args.big_blind,
    )
    total_chips = cfg.player_count * cfg.starting_stack
    rng = random.Random(args.seed)
    writer = HandHistoryWriter(args.history)

    game_no = 0
    hands_done = 0
    stats = GameStats()
    table: Table | None = None
    while hands_done < args.hands:
        if table is None or table.game_over:
            if table is not None:
                stats.final_stacks = table.stacks
                _print_game_summary(game_no, stats)
            game_no += 1
            stats = GameStats()
            table = Table(cfg, seed=args.seed + game_no, history_writer=writer)
        table.start_hand()
        while not table.hand_over:
            table.apply(random_action(rng, table.snapshot()))
        stats.hands += 1
        result = table.last_hand_result
        if result is not None and result.showdown:
            stats.showdowns += 1
        if sum(table.stacks) != total_chips + table.chips_added:
            print(
                f"筹码守恒被破坏! 第 {game_no} 局第 {stats.hands} 手: "
                f"总和 {sum(table.stacks)} != {total_chips + table.chips_added}"
                f"(含买入 {table.chips_added})",
                file=sys.stderr,
            )
            return 1
        hands_done += 1
    stats.final_stacks = table.stacks
    _print_game_summary(game_no, stats)
    print(f"共完成 {hands_done} 手牌,筹码守恒校验通过(总额恒为 {total_chips})。")
    print(f"历史已写入 {writer.path}")
    return 0


def _print_game_summary(game_no: int, stats: GameStats) -> None:
    """打印单局小结。"""
    print(
        f"第 {game_no} 局: 手数 {stats.hands}, 摊牌 {stats.showdowns} 次, "
        f"终局筹码 {list(stats.final_stacks)}"
    )


if __name__ == "__main__":
    sys.exit(main())
