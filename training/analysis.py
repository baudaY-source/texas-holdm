"""求解结果的展示模型:把 ``SolveResult`` 转成 UI 可直接绘制的数据。

全部纯逻辑,不依赖 pygame。13×13 网格的行列约定与
``gto.charts.PreflopCharts.range_grid_13x13`` 一致:行/列按 A..2 降序,
对角线 = 对子,上三角 = 同花,下三角 = 杂花。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from gto.charts import RANKS, canonical_hands, hand_key
from gto.solver_bridge import SolveNode


def grid_hand(row: int, col: int) -> str:
    """网格坐标 → 规范牌型键(行=高牌,列=低牌)。"""
    r1, r2 = RANKS[row], RANKS[col]
    if row == col:
        return r1 + r2
    return r1 + r2 + "s" if row < col else r2 + r1 + "o"


# ------------------------------------------------------------ 展示模型


@dataclass
class ActionMix:
    """单个牌型在某节点的动作频率混合。"""

    freqs: dict[str, float] = field(default_factory=dict)

    @property
    def dominant(self) -> str | None:
        """频率最高的动作(全 0 时为 ``None``)。"""
        if not self.freqs or max(self.freqs.values()) <= 0:
            return None
        return max(self.freqs, key=lambda a: self.freqs[a])

    @property
    def in_range(self) -> bool:
        return self.dominant is not None


@dataclass
class ActionSummary:
    """某动作在整个范围上的汇总。"""

    action: str
    range_freq: float  # 该动作在全部组合上的平均频率
    avg_ev: float | None = None  # dump 不含 EV,保留字段


# ------------------------------------------------------------ 转换


def street_matrix(node: SolveNode) -> list[list[ActionMix]]:
    """节点策略 → 13×13 ``ActionMix`` 网格(绘制用)。"""
    matrix = node.strategy_matrix_169()
    grid: list[list[ActionMix]] = []
    for r in range(13):
        row: list[ActionMix] = []
        for c in range(13):
            row.append(ActionMix(freqs=matrix.get(grid_hand(r, c), {})))
        grid.append(row)
    return grid


def action_summary(node: SolveNode) -> list[ActionSummary]:
    """各动作的范围频率(按组合等权平均),按频率降序。"""
    totals = {a: 0.0 for a in node.actions}
    n = 0
    for freqs in node.strategy.values():
        n += 1
        for i, a in enumerate(node.actions):
            if i < len(freqs):
                totals[a] += freqs[i]
    if n:
        for a in totals:
            totals[a] /= n
    return sorted(
        (ActionSummary(action=a, range_freq=f) for a, f in totals.items()),
        key=lambda s: -s.range_freq,
    )


def compare_hero_hand(node: SolveNode, hand: str) -> dict[str, float]:
    """单个规范牌型(如 ``"AKs"``)在当前节点的逐动作频率。"""
    if hand not in set(canonical_hands()):
        raise ValueError(f"非规范牌型键: {hand}")
    return dict(node.strategy_matrix_169().get(hand, {}))


def compare_hero_hole(node: SolveNode, hole: list[str]) -> dict[str, float]:
    """具体两张底牌(如 ``["Ah","Kd"]``)在当前节点的逐动作频率。"""
    return compare_hero_hand(node, hand_key(hole))


def node_range_combo_count(node: SolveNode) -> int:
    """当前节点策略中的组合数(范围宽度展示用)。"""
    return len(node.strategy)
