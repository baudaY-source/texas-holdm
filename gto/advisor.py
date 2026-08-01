"""GTO 助手 v1:为真人玩家生成实时行动建议(纯逻辑,不依赖 pygame)。

数据来源(M5 求解器桥落地前的取舍):

- 翻前未开池(本手尚无人加注)→ ``rfi_6max.json`` 的位置 RFI 混合策略
  (``chart:rfi``);有效筹码 ≤15bb 时短筹码 Nash 推佊表覆盖(``chart:pushfold``)。
- 翻前面对加注 → v1 近似:以**开池者位置**的 RFI 表作为其范围基准,
  手牌落在开池表顶端的做 3bet,中段跟注,表外弃牌。这明显是近似
  (真实 GTO 应对表见上游 ``*-vs-open-*``,v1 未接入),notes 中会标明。
- 翻后(HU 单挑)→ 先查**预计算策略库**(``gto/strategies/`` 的离线求解
  结果,``solver:precomputed``);未命中再退回胜率启发式。
- 翻后(多人池或未命中)→ 胜率启发式(``heuristic:equity``):按对手风格
  (VPIP)构建其继续范围,蒙特卡洛估算胜率后与底池赔率比较给出频率化建议。

性能预算:单次 ``hint`` < 50ms(equity 试验次数默认 200)。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from ai.equity import equity_vs_random, equity_vs_range, preflop_strength
from ai.styles import PlayerStyle
from engine.state import GameSnapshot, LegalActions, PlayerState, Position, Street

from .charts import PreflopCharts, canonical_hands, combos_for, hand_key
from .library import StrategyLibrary

# 动作频率字典的键:与 engine.state.ActionType 同名("FOLD"/"CHECK"/...)。
A_FOLD, A_CHECK, A_CALL, A_BET, A_RAISE, A_ALLIN = (
    "FOLD", "CHECK", "CALL", "BET", "RAISE", "ALLIN",
)

PUSHFOLD_MAX_BB = 15.0  # ≤ 该有效筹码时推佊表覆盖 RFI 表
EQUITY_MARGIN = 0.04  # 胜率超出底池赔率的安全边际
VALUE_EQUITY = 0.65  # 高于此胜率视为价值下注/加注区

# 现有 RFI 资产只有 6-max 的 UTG/MP/CO/BTN/SB。全环桌新位置按
# “身后尚未行动的后位人数”压缩到最接近且不更松的 6-max 参考：
# HJ 等价于 6-max MP，LJ 与更早位统一参考最紧的 UTG。
_RFI_6MAX_POSITION: dict[Position, str] = {
    Position.BTN: "BTN",
    Position.SB: "SB",
    Position.BB: "SB",  # 跛入池时无 BB RFI 表，沿用原有 SB 近似
    Position.UTG: "UTG",
    Position.UTG1: "UTG",
    Position.UTG2: "UTG",
    Position.LJ: "UTG",
    Position.HJ: "MP",
    Position.MP: "MP",
    Position.CO: "CO",
}

_FULL_RING_APPROX_POSITIONS = {
    Position.UTG1,
    Position.UTG2,
    Position.LJ,
    Position.HJ,
}

_ACTION_LEGAL = {
    A_FOLD: lambda l: l.can_fold,
    A_CHECK: lambda l: l.can_check,
    A_CALL: lambda l: l.can_call and l.call_amount > 0,
    A_BET: lambda l: l.min_bet_to is not None,
    A_RAISE: lambda l: l.min_raise_to is not None,
}


@dataclass(frozen=True)
class Hint:
    """一次建议结果。

    :param action_freqs: 合法动作上的推荐频率(和 ≈ 1)。
    :param equity: 对对手(范围)的胜率估计;无法估计时为 ``None``。
    :param pot_odds: 跟注所需胜率 ``call/(pot+call)``;无需跟注为 ``None``。
    :param source: 数据来源标记(``chart:rfi`` / ``chart:pushfold`` /
        ``solver:precomputed`` / ``heuristic:equity``)。
    :param notes: 中文说明(首行供面板直接展示)。
    """

    action_freqs: dict[str, float]
    equity: float | None
    pot_odds: float | None
    source: str
    notes: list[str] = field(default_factory=list)


class Advisor:
    """GTO 助手门面。

    :param charts: 图表门面;缺省加载 ``gto/charts/``。
    :param library: 预计算策略库;缺省加载 ``gto/strategies/``(无文件时
        查询恒 miss,自动退回启发式)。
    :param big_blind: 大盲额度(用于筹码 bb 换算与"未开池"判定);
        ``None`` 时从快照下注额近似推断。
    :param equity_trials: 蒙特卡洛试验次数(≤ 几百,保证 <50ms)。
    :param seed: 随机种子(测试可复现)。
    """

    def __init__(
        self,
        charts: PreflopCharts | None = None,
        big_blind: int | None = None,
        equity_trials: int = 200,
        seed: int | None = None,
        library: StrategyLibrary | None = None,
    ) -> None:
        self.charts = charts or PreflopCharts()
        self.library = library if library is not None else StrategyLibrary()
        self.big_blind = big_blind
        self.equity_trials = equity_trials
        self._rng = random.Random(seed)

    # ------------------------------------------------------------ 入口

    def hint(
        self,
        snapshot: GameSnapshot,
        human_seat: int,
        villain_styles: dict[int, PlayerStyle] | None = None,
    ) -> Hint:
        """为 ``human_seat`` 在当前快照下生成建议。"""
        hero = snapshot.players[human_seat]
        if hero.hole_cards is None:
            raise ValueError("无法建议:该座位底牌不可见")
        legal = snapshot.legal_actions
        if legal is None or snapshot.acting_seat is None:
            raise ValueError("无法建议:当前无人行动")
        styles = villain_styles or {}
        pot = snapshot.total_pot + sum(p.bet for p in snapshot.players)
        call_amt = legal.call_amount if legal.can_call else 0
        pot_odds = call_amt / (pot + call_amt) if call_amt > 0 else None
        if snapshot.street is Street.PREFLOP:
            return self._preflop(snapshot, hero, legal, styles, pot_odds)
        return self._postflop(snapshot, hero, legal, styles, pot_odds)

    # ------------------------------------------------------------ 翻前

    def _preflop(
        self,
        snap: GameSnapshot,
        hero: PlayerState,
        legal: LegalActions,
        styles: dict[int, PlayerStyle],
        pot_odds: float | None,
    ) -> Hint:
        hole = list(hero.hole_cards or ())
        bb = self._bb(snap)
        stack_bb = (hero.stack + hero.bet) / bb
        max_bet = max((p.bet for p in snap.players), default=0)
        straddle_post = self._live_straddle_post(snap, bb)
        live_straddle = straddle_post > 0
        forced_bet = max(bb, straddle_post) if live_straddle else bb
        # live straddle 是强制底注，不是 UTG 主动开池；只有超过 2BB
        # 的投入才视为真正加注。其他玩家平跟到 2BB 仍属未开池。
        unopened = max_bet <= forced_bet
        context_notes = (
            [
                f"UTG live straddle 实付 {straddle_post:g}"
                f"（目标 {2 * bb:g}）按强制底注处理"
            ]
            if live_straddle
            else []
        )
        n_villains = sum(
            1
            for p in snap.players
            if p.seat != hero.seat and p.position is not None and not p.folded
        )
        equity = equity_vs_random(
            hole, [], max(1, n_villains), self.equity_trials, self._rng
        )
        if unopened:
            if stack_bb <= PUSHFOLD_MAX_BB:
                return self._hint_pushfold(
                    hero, legal, stack_bb, equity, pot_odds, context_notes
                )
            return self._hint_rfi(hero, legal, equity, pot_odds, context_notes)
        return self._hint_vs_raise(snap, hero, legal, equity, pot_odds)

    def _hint_rfi(
        self, hero, legal, equity, pot_odds, context_notes: list[str] | None = None
    ) -> Hint:
        pos = hero.position
        if pos is None:
            raise ValueError("无法建议:该座位本手不在局中")
        notes: list[str] = []
        chart_pos = _RFI_6MAX_POSITION[pos]
        if pos is Position.BB:
            # 上游无 BB 率先表(跛入池),暂以最相近的 SB 表近似
            notes.append("大盲跛入池:暂参考小盲开池表(近似)")
        elif pos in _FULL_RING_APPROX_POSITIONS:
            notes.append(
                f"全环位置近似:{pos.name} → 6-max {chart_pos}(保守映射)"
            )
        cell = self.charts.rfi_action(chart_pos, list(hero.hole_cards or ()))
        freqs = self._chart_to_actions(cell)
        raise_pct = round(cell.get("raise", 0.0) * 100)
        notes.insert(0, f"翻前表:{pos.name} 开池 {raise_pct}% 加注")
        notes.extend(context_notes or ())
        return Hint(
            action_freqs=self._to_legal(freqs, legal),
            equity=equity,
            pot_odds=pot_odds,
            source="chart:rfi",
            notes=notes,
        )

    def _hint_pushfold(
        self,
        hero,
        legal,
        stack_bb,
        equity,
        pot_odds,
        context_notes: list[str] | None = None,
    ) -> Hint:
        pos_name = hero.position.name if hero.position else "?"
        cell = self.charts.pushfold(
            hero.position or Position.BTN, list(hero.hole_cards or ()), stack_bb
        )
        shove = cell.get("allin", 0.0) > 0
        freqs = {A_ALLIN: 1.0} if shove else {A_FOLD: 1.0}
        note = f"推佊表:{stack_bb:.0f}bb {'全下' if shove else '弃牌'}({pos_name})"
        return Hint(
            action_freqs=self._to_legal(freqs, legal),
            equity=equity,
            pot_odds=pot_odds,
            source="chart:pushfold",
            notes=[note, *(context_notes or ())],
        )

    def _hint_vs_raise(self, snap, hero, legal, equity, pot_odds) -> Hint:
        """面对加注的 v1 近似应对(见模块 docstring)。"""
        hole = list(hero.hole_cards or ())
        opener = max(snap.players, key=lambda p: p.bet)  # 近似:当前最大下注者
        opener_pos = opener.position
        # 开池者是 BB 时(罕见)取其最紧参考 UTG
        chart_pos = (
            _RFI_6MAX_POSITION[opener_pos]
            if opener_pos is not None and opener_pos is not Position.BB
            else "UTG"
        )
        f = self.charts.rfi_raise_freq(chart_pos, hand_key(hole))
        if f >= 0.9:
            freqs, tier = {A_RAISE: 0.85, A_CALL: 0.15}, "顶端 3bet"
        elif f > 0:
            freqs, tier = {A_CALL: 0.8, A_RAISE: 0.05, A_FOLD: 0.15}, "中段跟注"
        else:
            freqs, tier = {A_FOLD: 1.0}, "范围外弃牌"
        note = f"{tier}(近似 · 基准 {chart_pos} 开池表,M5 前启发式)"
        notes = [note]
        if opener_pos in _FULL_RING_APPROX_POSITIONS:
            notes.append(
                f"开池者全环位置近似:{opener_pos.name} → "
                f"6-max {chart_pos}(保守映射)"
            )
        return Hint(
            action_freqs=self._to_legal(freqs, legal),
            equity=equity,
            pot_odds=pot_odds,
            source="chart:rfi",
            notes=notes,
        )

    # ------------------------------------------------------------ 翻后

    def _postflop(
        self,
        snap: GameSnapshot,
        hero: PlayerState,
        legal: LegalActions,
        styles: dict[int, PlayerStyle],
        pot_odds: float | None,
    ) -> Hint:
        hole = list(hero.hole_cards or ())
        board = list(snap.board)
        villains = [
            p
            for p in snap.players
            if p.seat != hero.seat and p.position is not None and not p.folded
        ]
        lib = self._library_hint(snap, hero, legal, villains, pot_odds)
        if lib is not None:
            return lib
        # 参考对手:本街最大下注者(进攻方),否则第一个仍在局者
        ref = max(villains, key=lambda p: p.bet, default=None)
        style = styles.get(ref.seat) if ref is not None else None
        vpip = style.vpip if style else 0.25
        aggression = style.aggression if style else 1.5

        equity: float | None = None
        if ref is not None:
            combos = self._range_combos(vpip, hole, board)
            if combos:
                equity = equity_vs_range(
                    hole, board, combos, self.equity_trials, self._rng
                )
        notes: list[str] = []
        if equity is None:
            freqs = {A_CHECK: 1.0} if legal.can_check else {A_CALL: 0.5, A_FOLD: 0.5}
            notes.append("启发式:无法估计胜率,保守处理")
            return Hint(self._to_legal(freqs, legal), None, pot_odds,
                        "heuristic:equity", notes)

        call_amt = legal.call_amount if legal.can_call else 0
        facing_bet = call_amt > 0
        # 对手越凶,价值下注频率适当下调(留出诱捕空间)
        value_freq = 0.9 if aggression < 1.0 else 0.8 if aggression < 2.0 else 0.65
        if facing_bet:
            if equity >= VALUE_EQUITY:
                freqs = {A_RAISE: value_freq, A_CALL: 1.0 - value_freq}
                verdict = "强牌价值加注"
            elif pot_odds is not None and equity >= pot_odds + EQUITY_MARGIN:
                freqs = {A_CALL: 0.85, A_RAISE: 0.15}
                verdict = "胜率覆盖赔率,跟注"
            elif pot_odds is not None and equity >= pot_odds - EQUITY_MARGIN:
                freqs = {A_CALL: 0.5, A_FOLD: 0.5}
                verdict = "边缘牌,跟弃参半"
            else:
                freqs = {A_FOLD: 0.9, A_CALL: 0.1}
                verdict = "胜率不抵赔率,弃牌"
        else:
            if equity >= VALUE_EQUITY:
                freqs = {A_BET: value_freq, A_CHECK: 1.0 - value_freq}
                verdict = "强牌价值下注"
            elif equity >= 0.45:
                freqs = {A_CHECK: 0.75, A_BET: 0.25}
                verdict = "中等牌力,以过牌为主"
            else:
                freqs = {A_CHECK: 0.9, A_BET: 0.1}
                verdict = "弱牌过牌,偶发诈唬"
        notes.append(f"{verdict} · 胜率 {equity:.0%} vs 范围前 {vpip:.0%}(启发式占位)")
        return Hint(
            action_freqs=self._to_legal(freqs, legal),
            equity=equity,
            pot_odds=pot_odds,
            source="heuristic:equity",
            notes=notes,
        )

    # ------------------------------------------------------------ 翻后:预计算库

    # 求解器动作串首词 → 面板动作键
    _SOLVER_ACTION = {
        "CHECK": A_CHECK, "FOLD": A_FOLD, "CALL": A_CALL,
        "BET": A_BET, "RAISE": A_RAISE, "ALLIN": A_ALLIN,
    }

    def _library_hint(
        self,
        snap: GameSnapshot,
        hero: PlayerState,
        legal: LegalActions,
        villains: list[PlayerState],
        pot_odds: float | None,
    ) -> Hint | None:
        """HU 单挑翻后:查预计算策略库;命中返回 Hint,否则 ``None``。

        v1 只服务"未面对下注"的节点(hero OOP → 根节点;hero IP → OOP
        过牌后的 CHECK 子节点);面对下注退回启发式。
        """
        if len(villains) != 1 or len(snap.board) < 3:
            return None
        if legal.can_call and legal.call_amount > 0:
            return None
        hole = list(hero.hole_cards or ())
        if len(hole) != 2:
            return None
        pot = snap.total_pot + sum(p.bet for p in snap.players)
        eff_stack = min(hero.stack, villains[0].stack)
        hit = self.library.lookup(
            snap.board, pot, eff_stack, hero_is_ip=hero.position is Position.BTN
        )
        if hit is None:
            return None
        raw = hit["strategy_169"].get(hand_key(hole)) or {}
        freqs: dict[str, float] = {}
        for action_str, f in raw.items():
            if f <= 0:
                continue
            key = self._SOLVER_ACTION.get(action_str.split()[0].upper())
            if key is not None:
                freqs[key] = freqs.get(key, 0.0) + f
        if not freqs:
            return None  # 该牌型不在求解范围内,退回启发式
        notes = [
            f"预计算求解:{hit['spot_id']}({hit['node']},额度×{hit['scale']:.2f})",
            "离线 TexasSolver 批量结果(±20% 底池/筹码容差匹配)",
        ]
        return Hint(
            self._to_legal(freqs, legal), None, pot_odds, "solver:precomputed", notes
        )

    @staticmethod
    def _range_combos(
        vpip: float, hole: list[str], board: list[str]
    ) -> list[tuple[str, str]]:
        """按 VPIP 取解析强度前 X% 的牌型,展开为具体组合并剔除冲突。"""
        ranked = sorted(
            canonical_hands(),
            key=lambda h: preflop_strength(*combos_for(h)[0]),
            reverse=True,
        )
        top_n = max(8, min(169, round(169 * vpip)))
        known = set(hole) | set(board)
        return [
            c
            for h in ranked[:top_n]
            for c in combos_for(h)
            if c[0] not in known and c[1] not in known
        ]

    # ------------------------------------------------------------ 工具

    @staticmethod
    def _live_straddle_post(snap: GameSnapshot, bb: float) -> float:
        """返回引擎 8/9 人 UTG live straddle 的实际投入；未启用为零。

        快照没有额外的 straddle 字段，因此同时核对本手实际入座人数、UTG
        位置和不超过 2BB 的正投入；这也覆盖只能交出部分名义金额的短码
        straddler。配置为九人但本手仅七人在座时不会误判。
        """
        active = [p for p in snap.players if p.position is not None]
        if len(active) < 8:
            return 0.0
        return next(
            (
                float(p.bet)
                for p in active
                if p.position is Position.UTG and 0 < p.bet <= 2 * bb
            ),
            0.0,
        )

    @staticmethod
    def _chart_to_actions(cell: dict[str, float]) -> dict[str, float]:
        """图表单元格(raise/call/fold/allin)→ 面板动作键。"""
        out: dict[str, float] = {}
        for action, freq in cell.items():
            key = {"raise": A_RAISE, "call": A_CALL, "fold": A_FOLD, "allin": A_ALLIN}[
                action
            ]
            out[key] = out.get(key, 0.0) + freq
        return out

    @staticmethod
    def _to_legal(freqs: dict[str, float], legal: LegalActions) -> dict[str, float]:
        """把期望频率投影到合法动作集并归一化(和 = 1)。

        降级规则:ALLIN→RAISE;BET 不可用时→CHECK/CALL;RAISE 不可用
        →CALL/CHECK;免费过牌时 FOLD→CHECK(无人会白送弃牌)。
        """
        f: dict[str, float] = {}
        for k, v in freqs.items():
            f[k] = f.get(k, 0.0) + v
        if A_ALLIN in f:
            f[A_RAISE] = f.get(A_RAISE, 0.0) + f.pop(A_ALLIN)
        if legal.can_check and legal.call_amount == 0 and A_FOLD in f:
            f[A_CHECK] = f.get(A_CHECK, 0.0) + f.pop(A_FOLD)
        out: dict[str, float] = {}
        for k, v in f.items():
            if v <= 0:
                continue
            if _ACTION_LEGAL[k](legal):
                out[k] = out.get(k, 0.0) + v
            elif k == A_BET and legal.can_check:
                out[A_CHECK] = out.get(A_CHECK, 0.0) + v
            elif k in (A_BET, A_RAISE) and legal.can_call and legal.call_amount > 0:
                out[A_CALL] = out.get(A_CALL, 0.0) + v
            elif k == A_CALL and legal.can_check:
                out[A_CHECK] = out.get(A_CHECK, 0.0) + v
            # 其余(如 FOLD 不可用)直接丢弃,归一化时摊平
        if not out:  # 兜底:静态不会全丢,防御而已
            fallback = A_CHECK if legal.can_check else A_CALL if legal.can_call else A_FOLD
            return {fallback: 1.0}
        total = sum(out.values())
        return {k: v / total for k, v in out.items()}

    def _bb(self, snap: GameSnapshot) -> float:
        """大盲额度:显式配置优先,否则从本街下注额近似推断。"""
        if self.big_blind:
            return float(self.big_blind)
        bets = sorted({p.bet for p in snap.players if p.bet > 0})
        if not bets:
            return 1.0
        if len(bets) >= 2 and bets[1] == bets[0] * 2:
            return float(bets[1])  # 形如 5/10 的盲注对
        return float(bets[0])
