"""预计算策略库:加载/查询 ``gto/strategies/`` 下的离线求解结果(纯逻辑)。

每个 JSON 文件对应一个 HU 单挑局面(``gto/precompute.py`` 产出),内含
求解树**根节点与第一层子节点**的 169 牌型策略矩阵(不存整棵树,控制体积)。

v1 匹配规则(``lookup``):

- 公共牌**精确**匹配(同街同牌集合);
- 底池/有效筹码与该 spot 的比值在 ±20% 容差内;
- hero 为先手(OOP/BB)→ 取根节点策略(其本就是 OOP 首个行动);
- hero 为后手(IP/BTN)→ 取根节点 "CHECK" 子节点策略(OOP 过牌后 IP 行动);
  无 CHECK 子节点或 hero 正面对下注时返回 ``None``(v1 不服务该情形)。

未来工作:牌面纹理相似度匹配(同花/连张结构泛化到未见牌面)、范围加权
匹配、转/河牌 deeper 节点。下注额度按实际底池与 spot 底池之比缩放。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from ui.respath import res_path

FORMAT_VERSION = 1
STRATEGIES_DIR = res_path("gto", "strategies")
DEFAULT_TOLERANCE = 0.2  # 底池/筹码匹配容差(±20%)


@lru_cache(maxsize=4)
def _load_dir(directory: str) -> tuple[dict, ...]:
    """加载目录下全部 spot JSON(按目录缓存;损坏文件跳过)。"""
    spots: list[dict] = []
    path = Path(directory)
    if path.is_dir():
        for f in sorted(path.glob("*.json")):
            try:
                with open(f, encoding="utf-8") as fh:
                    spot = json.load(fh)
                if spot.get("format_version") == FORMAT_VERSION:
                    spots.append(spot)
            except (json.JSONDecodeError, OSError):
                continue
    return tuple(spots)


class StrategyLibrary:
    """预计算 spot 集合的查询门面。

    :param directory: spot JSON 目录;缺省 ``gto/strategies/``。
    :param tolerance: 底池/筹码匹配容差(默认 ±20%)。
    """

    def __init__(
        self, directory: str | Path | None = None, tolerance: float = DEFAULT_TOLERANCE
    ) -> None:
        self.directory = str(directory or STRATEGIES_DIR)
        self.tolerance = tolerance

    @property
    def spots(self) -> tuple[dict, ...]:
        return _load_dir(self.directory)

    def __len__(self) -> int:
        return len(self.spots)

    # ------------------------------------------------------------ 查询

    def lookup(
        self,
        board: list[str] | tuple[str, ...],
        pot: float,
        stack: float,
        hero_is_ip: bool,
        ranges_approx: dict | None = None,
    ) -> dict | None:
        """最近 spot 匹配;命中返回节点策略包,否则 ``None``。

        :param board: 当前公共牌(须与 spot 完全同牌同街)。
        :param pot: 当前底池(含本街下注)。
        :param stack: 有效筹码(双方较小者的剩余筹码)。
        :param hero_is_ip: hero 是否后手(BTN)。
        :param ranges_approx: 预留(v1 忽略,范围匹配为未来工作)。
        :return: ``{"spot_id", "node", "actions", "strategy_169", "scale"}``;
            ``scale`` = 实际底池 / spot 底池(额度缩放系数)。
        """
        del ranges_approx  # v1 不使用
        want = sorted(board)
        best: tuple[float, dict] | None = None
        for spot in self.spots:
            meta = spot.get("config_meta", {})
            if sorted(meta.get("board", [])) != want:
                continue
            spot_pot = float(meta.get("pot", 0))
            spot_stack = float(meta.get("effective_stack", 0))
            if spot_pot <= 0 or spot_stack <= 0:
                continue
            if not (1 - self.tolerance <= pot / spot_pot <= 1 + self.tolerance):
                continue
            if not (1 - self.tolerance <= stack / spot_stack <= 1 + self.tolerance):
                continue
            node = self._node_for(spot, hero_is_ip)
            if node is None:
                continue
            # 多个候选(同牌面不同深度)时取筹码最接近的
            distance = abs(stack / spot_stack - 1.0)
            hit = {
                "spot_id": spot.get("spot_id", "?"),
                "node": "check_child" if hero_is_ip else "root",
                "actions": node["actions"],
                "strategy_169": node["strategy_169"],
                "scale": pot / spot_pot,
            }
            if best is None or distance < best[0]:
                best = (distance, hit)
        return best[1] if best else None

    @staticmethod
    def _node_for(spot: dict, hero_is_ip: bool) -> dict | None:
        """按 hero 位置取节点:OOP → 根;IP → 根的 CHECK 子节点。"""
        if not hero_is_ip:
            return spot.get("root")
        return (spot.get("children") or {}).get("CHECK")


def solver_action_freqs(node_strategy: dict, hand: str) -> dict[str, float]:
    """从节点 169 矩阵中取某规范牌型的 {求解器动作串: 频率}。"""
    return dict(node_strategy.get(hand, {}))
