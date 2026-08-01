"""翻前 GTO 图表加载器与牌型规范化工具。

数据文件位于 ``gto/charts/``(由 ``tools/convert_charts.py`` 从 MIT 上游
仓库转换而来,出处见 ``gto/charts/SOURCE.md``):

- ``rfi_6max.json``:6-max 五位置(UTG/MP/CO/BTN/SB)率先加注混合策略;
- ``hu_solved.json``:单挑 CFR+ 求解图表(本模块原样暴露,面板暂用 6-max 表);
- ``pushfold.json``:短筹码 Nash 推佊/跟注阈值表(阈值 = 仍可全下/跟注的
  最大有效筹码 bb,999 = 任意深度)。

纯逻辑,不依赖 pygame;JSON 加载结果按目录缓存。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from engine.state import Position
from ui.respath import res_path

RANKS = "AKQJT98765432"  # 强度降序
CHARTS_DIR = res_path("gto", "charts")

ALWAYS_BB = 999  # 与转换器约定一致:任意深度均在范围内


def canonical_hands() -> list[str]:
    """全部 169 个规范化牌型键:对子("AA")+ 同花("AKs")+ 杂花("AKo")。"""
    out: list[str] = []
    for i, hi in enumerate(RANKS):
        out.append(hi + hi)
        for lo in RANKS[i + 1 :]:
            out.append(hi + lo + "s")
            out.append(hi + lo + "o")
    return out


def hand_key(hole: list[str] | tuple[str, ...]) -> str:
    """两张底牌 → 规范化牌型键。

    ``("Ah","Kd") → "AKo"``,``("Ah","Kh") → "AKs"``,``("Ah","Ad") → "AA"``。
    高牌在前;对子无花色后缀。
    """
    if len(hole) != 2:
        raise ValueError("hole 须为两张牌")
    (r1, s1), (r2, s2) = (hole[0][0], hole[0][1]), (hole[1][0], hole[1][1])
    if r1 not in RANKS or r2 not in RANKS:
        raise ValueError(f"非法牌面: {hole}")
    if r1 == r2:
        return r1 + r2
    hi, lo = (r1, r2) if RANKS.index(r1) < RANKS.index(r2) else (r2, r1)
    return hi + lo + ("s" if s1 == s2 else "o")


_CANONICAL_SET = frozenset(canonical_hands())


def combos_for(hand: str) -> list[tuple[str, str]]:
    """规范化牌型键 → 全部具体两牌组合(对子 6、同花 4、杂花 12)。"""
    if hand not in _CANONICAL_SET:
        raise ValueError(f"非规范牌型键: {hand}")
    suits = "cdhs"
    if len(hand) == 2:  # 对子
        return [(hand[0] + a, hand[0] + b) for i, a in enumerate(suits) for b in suits[i + 1 :]]
    hi, lo, kind = hand[0], hand[1], hand[2]
    if kind == "s":
        return [(hi + s, lo + s) for s in suits]
    return [(hi + a, lo + b) for a in suits for b in suits if a != b]


@lru_cache(maxsize=8)
def _load(charts_dir: str, name: str) -> dict:
    path = Path(charts_dir) / name
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class PreflopCharts:
    """翻前图表查询门面(按目录懒加载并缓存)。"""

    def __init__(self, charts_dir: str | Path | None = None) -> None:
        self._dir = str(charts_dir or CHARTS_DIR)

    # ------------------------------------------------------------ 原始数据

    @property
    def rfi(self) -> dict[str, dict[str, dict[str, float]]]:
        """6-max RFI:``{位置: {牌型: {动作: 频率}}}``。"""
        return _load(self._dir, "rfi_6max.json")["positions"]

    @property
    def hu_solved(self) -> dict[str, dict[str, dict[str, float]]]:
        """单挑求解:``{图表名: {牌型: {动作: 频率}}}``。"""
        return _load(self._dir, "hu_solved.json")["charts"]

    @property
    def pushfold_tables(self) -> dict[str, dict[str, int]]:
        """推佊阈值:``{"shove": {牌型: 最大bb}, "call": {...}}``。"""
        return _load(self._dir, "pushfold.json")

    # ------------------------------------------------------------ 查询

    @staticmethod
    def _pos_name(position: Position | str) -> str:
        return position.name if isinstance(position, Position) else position

    def rfi_action(
        self, position: Position | str, hole: list[str] | tuple[str, ...]
    ) -> dict[str, float]:
        """某位置率先加注表中,该手牌的动作频率(如 ``{"raise": 1.0}``)。

        未知位置抛 ``KeyError``;频率字典和为 1。
        """
        pos = self._pos_name(position)
        return dict(self.rfi[pos][hand_key(hole)])

    def rfi_raise_freq(self, position: Position | str, hand: str) -> float:
        """某位置 RFI 表中某规范牌型的加注频率(范围构建用)。"""
        pos = self._pos_name(position)
        return self.rfi[pos][hand].get("raise", 0.0)

    def pushfold(
        self,
        position: Position | str,
        hole: list[str] | tuple[str, ...],
        stack_bb: float,
    ) -> dict[str, float]:
        """短筹码开池决策:``{"allin": 1.0}`` 或 ``{"fold": 1.0}``。

        阈值表本身是单挑 SB/BTN 的 Nash 解;6-max 其他位置暂复用同一表
        (偏松的近似,M5 求解器桥落地后替换)。``position`` 目前仅用于
        语义标注,不影响结果。
        """
        threshold = self.pushfold_tables["shove"][hand_key(hole)]
        if stack_bb <= threshold:
            return {"allin": 1.0}
        return {"fold": 1.0}

    def pushfold_call(self, hole: list[str] | tuple[str, ...], stack_bb: float) -> dict[str, float]:
        """面对全下时的跟注/弃牌阈值决策。"""
        threshold = self.pushfold_tables["call"][hand_key(hole)]
        if stack_bb <= threshold:
            return {"call": 1.0}
        return {"fold": 1.0}

    # ------------------------------------------------------------ 展示辅助

    def range_grid_13x13(
        self,
        actions: dict[str, dict[str, float]] | None = None,
        *,
        position: Position | str | None = None,
        key: str = "raise",
    ) -> list[list[float]]:
        """13×13 频率矩阵(展示用):行/列按 A..2 降序。

        对角线 = 对子,上三角(行号<列号)= 同花,下三角 = 杂花;
        单元格值 = 该牌型动作字典中 ``key`` 动作的频率(缺省 0)。
        可通过 ``position`` 直接取某位置 RFI 表,或传入自定义 ``actions``。
        """
        if actions is None:
            if position is None:
                raise ValueError("actions 与 position 须二选一")
            actions = self.rfi[self._pos_name(position)]
        grid: list[list[float]] = []
        for i, r1 in enumerate(RANKS):
            row: list[float] = []
            for j, r2 in enumerate(RANKS):
                if i == j:
                    hand = r1 + r2
                elif i < j:
                    hand = r1 + r2 + "s"
                else:
                    hand = r2 + r1 + "o"
                row.append(actions.get(hand, {}).get(key, 0.0))
            grid.append(row)
        return grid
