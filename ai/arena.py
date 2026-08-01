"""AI 竞技场:让具名 AI 在真引擎上对战并统计风格指标。

用法::

    python -m ai.arena --hands 10000 --players 6 --seed 1 [--fast]

统计口径:
- VPIP%:翻牌前自愿投入筹码(跟注/下注/加注,盲注除外)的手牌比例;
- PFR%:翻牌前加注过的手牌比例;
- 3bet%:面对加注机会时再加注的比例;
- AF:翻牌后 (下注+加注) / 跟注;
- WTSD%:看到翻牌后打到摊牌的比例;W$SD%:摊牌赢钱比例;
- bb/100:每百手净盈亏(大盲为单位)。

游戏结束(只剩一人有筹码)时自动重开新局,各 AI 的统计持续累计。
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from engine.game import Table, TableConfig
from engine.state import ActionType, Street

from .bots import build_style_bot
from .personas import Persona, default_personas, filler_persona, persona_catalog

# --fast 时的 MC 试验次数(覆盖各等级默认值)
_FAST_TRIALS = {"fish": 0, "reg": 40, "shark": 60}


@dataclass
class SeatStats:
    """单个座位(对应一名 AI)的累计统计。"""

    hands: int = 0
    vpip: int = 0
    pfr: int = 0
    threebet: int = 0
    threebet_opp: int = 0
    agg: int = 0  # 翻牌后下注+加注次数
    calls: int = 0  # 翻牌后跟注次数
    saw_flop: int = 0
    wtsd: int = 0  # 打到摊牌次数
    wsd: int = 0  # 摊牌赢钱次数
    profit: int = 0  # 累计净盈亏(筹码)


@dataclass
class _HandContext:
    """单手牌内的临时追踪状态。"""

    preflop_raises: int = 0
    flop_seen: bool = False
    vpip_recorded: set[int] = field(default_factory=set)
    pfr_recorded: set[int] = field(default_factory=set)


def build_lineup(players: int, seed: int) -> list[Persona]:
    """按人数组建阵容，兼容旧六人顺序并扩展到九名稳定身份。

    前五名和第六席流浪猫沿用旧竞技场的生成路径，保证原有 2–6 人
    固定种子实验不漂移；满桌新增渡鸦、兔子与灰狼来自统一身份目录。
    """
    legacy = default_personas(seed) + [filler_persona(seed)]
    if players <= len(legacy):
        return legacy[:players]
    catalog = persona_catalog(seed)
    return legacy + list(catalog[len(legacy):players])


def run_arena(
    hands: int = 10_000,
    players: int = 6,
    seed: int = 1,
    fast: bool = False,
    stack: int = 200,
    small_blind: int = 5,
    big_blind: int = 10,
) -> dict:
    """跑 ``hands`` 手牌,返回统计字典(可 JSON 序列化、无时间戳)。"""
    if not 2 <= players <= 9:
        raise ValueError("players 须在 2-9 之间")
    lineup = build_lineup(players, seed)
    cfg = TableConfig(
        player_count=players,
        starting_stack=stack,
        small_blind=small_blind,
        big_blind=big_blind,
        player_names=tuple(p.display_name for p in lineup),
    )
    trials = _FAST_TRIALS if fast else {}
    bots = [
        build_style_bot(
            p.style_key,
            style=p.style,
            level=p.level,
            seed=seed * 100 + i,
            big_blind=big_blind,
            trials=trials.get(p.level) if fast else None,
        )
        for i, p in enumerate(lineup)
    ]
    stats = [SeatStats() for _ in lineup]

    table: Table | None = None
    game_no = 0
    done = 0
    straddled_hands = 0
    while done < hands:
        if table is None or table.game_over:
            game_no += 1
            table = Table(cfg, seed=seed * 10_000 + game_no)
        table.start_hand()
        if table.current_straddle_amount:
            straddled_hands += 1
        ctx = _HandContext()
        snap = table.snapshot()
        for p in snap.players:
            if p.position is not None:  # 本手在局
                stats[p.seat].hands += 1
        while not table.hand_over:
            snap = table.snapshot()
            if snap.acting_seat is None:
                break
            if snap.street is Street.FLOP and not ctx.flop_seen:
                ctx.flop_seen = True
                for p in snap.players:
                    if not p.folded:
                        stats[p.seat].saw_flop += 1
            seat = snap.acting_seat
            pov = table.snapshot(perspective=seat)
            action = bots[seat].decide(pov)
            _record_action(stats[seat], action.action_type, snap.street, ctx, seat)
            table.apply(action)
        result = table.last_hand_result
        assert sum(table.stacks) == players * stack + table.chips_added, "筹码守恒被破坏"
        if result is not None:
            for seat_, delta in result.deltas.items():
                stats[seat_].profit += delta
            if len(result.board) >= 3:
                end_snap = table.snapshot()
                if ctx.flop_seen:
                    # 正常行动手牌:行动循环中已按当时未弃牌者记录
                    pass
                else:
                    # 翻牌前全下跑码:行动循环看不到翻牌街,补记
                    for p in end_snap.players:
                        if p.position is not None and not p.folded:
                            stats[p.seat].saw_flop += 1
                if result.showdown:
                    for p in end_snap.players:
                        if p.position is not None and not p.folded:
                            stats[p.seat].wtsd += 1
                            if result.deltas.get(p.seat, 0) > 0:
                                stats[p.seat].wsd += 1
        done += 1

    return _assemble(
        lineup,
        stats,
        hands,
        players,
        seed,
        fast,
        big_blind,
        straddled_hands,
    )


def _record_action(
    st: SeatStats,
    kind: ActionType,
    street: Street,
    ctx: _HandContext,
    seat: int,
) -> None:
    """按街道把一个动作计入统计。"""
    if street is Street.PREFLOP:
        if ctx.preflop_raises >= 1:
            st.threebet_opp += 1
        if (
            kind in (ActionType.CALL, ActionType.BET, ActionType.RAISE, ActionType.ALLIN)
            and seat not in ctx.vpip_recorded
        ):
            st.vpip += 1
            ctx.vpip_recorded.add(seat)
        if kind in (ActionType.BET, ActionType.RAISE, ActionType.ALLIN):
            if seat not in ctx.pfr_recorded:
                st.pfr += 1
                ctx.pfr_recorded.add(seat)
            if ctx.preflop_raises >= 1:
                st.threebet += 1
            ctx.preflop_raises += 1
    else:
        if kind in (ActionType.BET, ActionType.RAISE, ActionType.ALLIN):
            st.agg += 1
        elif kind is ActionType.CALL:
            st.calls += 1


def _pct(num: int, den: int) -> float:
    return round(100.0 * num / den, 1) if den else 0.0


def _assemble(
    lineup: list[Persona], stats: list[SeatStats],
    hands: int, players: int, seed: int, fast: bool, big_blind: int,
    straddled_hands: int,
) -> dict:
    rows = []
    for persona, s in zip(lineup, stats):
        af = round(s.agg / s.calls, 2) if s.calls else (float(s.agg) if s.agg else 0.0)
        bb100 = round(s.profit / big_blind / s.hands * 100, 1) if s.hands else 0.0
        rows.append({
            "name": persona.display_name,
            "persona_id": persona.persona_id,
            "species": persona.species,
            "style_key": persona.style_key,
            "level": persona.level,
            "hands": s.hands,
            "vpip": _pct(s.vpip, s.hands),
            "pfr": _pct(s.pfr, s.hands),
            "threebet": _pct(s.threebet, s.threebet_opp),
            "af": af,
            "wtsd": _pct(s.wtsd, s.saw_flop),
            "wsd": _pct(s.wsd, s.wtsd),
            "bb100": bb100,
            "profit": s.profit,
        })
    return {
        "seed": seed,
        "hands": hands,
        "players": players,
        "fast": fast,
        "straddled_hands": straddled_hands,
        "table": rows,
    }


def _print_table(result: dict) -> None:
    """打印对齐的统计表。"""
    header = (
        f"{'玩家':<14}{'等级':<8}{'手数':>6}{'VPIP%':>8}{'PFR%':>8}"
        f"{'3bet%':>8}{'AF':>7}{'WTSD%':>8}{'W$SD%':>8}{'bb/100':>9}"
    )
    print(header)
    print("-" * len(header))
    for r in result["table"]:
        name = r["name"]
        pad = 14 - sum(2 if ord(ch) > 0x2E80 else 1 for ch in name) + len(name)
        print(
            f"{name:<{pad}}{r['level']:<8}{r['hands']:>6}{r['vpip']:>8.1f}"
            f"{r['pfr']:>8.1f}{r['threebet']:>8.1f}{r['af']:>7.2f}"
            f"{r['wtsd']:>8.1f}{r['wsd']:>8.1f}{r['bb100']:>9.1f}"
        )


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="AI 竞技场")
    parser.add_argument("--hands", type=int, default=10_000, help="总手数")
    parser.add_argument("--players", type=int, default=6, help="人数(2-9)")
    parser.add_argument("--seed", type=int, default=1, help="随机种子")
    parser.add_argument("--fast", action="store_true", help="削减 MC 试验次数以加速")
    parser.add_argument("--stack", type=int, default=200, help="起始筹码")
    parser.add_argument("--small-blind", type=int, default=5)
    parser.add_argument("--big-blind", type=int, default=10)
    parser.add_argument("--out", default="hands/arena_stats.json", help="统计输出路径")
    args = parser.parse_args(argv)

    result = run_arena(
        hands=args.hands,
        players=args.players,
        seed=args.seed,
        fast=args.fast,
        stack=args.stack,
        small_blind=args.small_blind,
        big_blind=args.big_blind,
    )
    print(f"竞技场: {result['hands']} 手 / {result['players']} 人 / seed={result['seed']}"
          f"{' (fast)' if result['fast'] else ''}")
    _print_table(result)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"统计已写入 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
