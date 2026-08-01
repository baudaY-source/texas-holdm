"""训练场场景:HU 局面的描述、内置模板与存取(纯逻辑,不依赖 pygame)。

``Scenario`` 汇总一次求解/分析所需的全部输入:hero 底牌(具体两张或
范围)、villain 范围(169 权重)、公共牌、底池/有效筹码、位置先后手、
下注尺度预设与求解参数。``to_solve_config()`` 转换为求解器桥配置。

**v1 硬约束**:TexasSolver 只解两人(HU)局面,因此模板均为
hero(BTN/IP)vs villain(BB/OOP)的单挑模型;6-max 多人池须由用户
自行折算底池后拆成单挑子局面(UI 中有提示)。

用户自建场景以 JSON 存于 ``training/scenarios/``(已 gitignore);
内置模板由代码生成,不落盘。
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from gto.charts import PreflopCharts, canonical_hands, combos_for, hand_key
from gto.solver_bridge import BetSizes, RangeStr, SolveConfig
from ai.equity import preflop_strength
from ui.respath import user_data_path

# 用户自建场景目录(可写;开发时为 training/scenarios,打包后在 exe 同级)
def _scenarios_dir() -> Path:
    return user_data_path("training", "scenarios")

STREET_BOARD_LEN = {"flop": 3, "turn": 4, "river": 5}


# ------------------------------------------------------------ 范围构造


def ranked_hands() -> list[str]:
    """169 牌型按解析翻前强度降序(与 ``ai.advisor`` 同一近似)。"""
    return sorted(
        canonical_hands(),
        key=lambda h: preflop_strength(*combos_for(h)[0]),
        reverse=True,
    )


def top_range(pct: float) -> dict[str, float]:
    """强度前 ``pct``(0..1)的牌型,权重 1;其余 0。"""
    n = max(0, min(169, round(169 * pct)))
    chosen = set(ranked_hands()[:n])
    return {h: (1.0 if h in chosen else 0.0) for h in canonical_hands()}


def _full() -> dict[str, float]:
    return {h: 0.0 for h in canonical_hands()}


# ------------------------------------------------------------ 场景


@dataclass
class Scenario:
    """一个 HU 训练局面。

    :param hero_cards: hero 具体底牌(0 或 2 张;2 张时快速分析用,
        求解范围仍取 ``hero_range``)。
    :param hero_range / villain_range: 169 键权重字典。
    :param street: 求解街(flop/turn/river),须与 ``len(board)`` 一致。
    :param hero_is_ip: hero 是否后手(BTN);否则 hero 为 BB/OOP。
    """

    name: str = "自定义场景"
    hero_cards: list[str] = field(default_factory=list)
    hero_range: dict[str, float] = field(default_factory=_full)
    villain_range: dict[str, float] = field(default_factory=_full)
    board: list[str] = field(default_factory=list)
    street: str = "flop"
    pot: float = 55.0
    effective_stack: float = 975.0
    hero_is_ip: bool = True
    bet_sizes: dict[str, dict[str, BetSizes]] | None = None
    allin_threshold: float = 0.67
    accuracy: float = 1.0
    max_iteration: int = 200
    dump_rounds: int = 2

    # ------------------------------------------------------------ 转换

    def hero_range_effective(self) -> dict[str, float]:
        """hero 求解范围:显式范围优先;仅有具体底牌时退化为单牌型范围。"""
        if any(w > 0 for w in self.hero_range.values()):
            return dict(self.hero_range)
        if len(self.hero_cards) == 2:
            out = _full()
            out[hand_key(self.hero_cards)] = 1.0
            return out
        return dict(self.hero_range)

    def to_solve_config(self) -> SolveConfig:
        """转换为求解器桥配置(校验范围/公共牌/街一致性)。"""
        if self.street not in STREET_BOARD_LEN:
            raise ValueError(f"非法街: {self.street}")
        if len(self.board) != STREET_BOARD_LEN[self.street]:
            raise ValueError(
                f"{self.street} 需要 {STREET_BOARD_LEN[self.street]} 张公共牌,"
                f"当前 {len(self.board)} 张"
            )
        known = set(self.board) | set(self.hero_cards)
        if len(known) != len(self.board) + len(self.hero_cards):
            raise ValueError("公共牌与 hero 底牌存在重复")
        hero = RangeStr.dump(self.hero_range_effective())
        villain = RangeStr.dump(self.villain_range)
        cfg = SolveConfig(
            pot=self.pot,
            effective_stack=self.effective_stack,
            board=list(self.board),
            range_ip=hero if self.hero_is_ip else villain,
            range_oop=villain if self.hero_is_ip else hero,
            allin_threshold=self.allin_threshold,
            accuracy=self.accuracy,
            max_iteration=self.max_iteration,
            dump_rounds=self.dump_rounds,
        )
        if self.bet_sizes is not None:
            cfg.bet_sizes = self.bet_sizes
        cfg.validate()
        return cfg

    # ------------------------------------------------------------ 存取

    def to_dict(self) -> dict:
        d = asdict(self)
        # BetSizes 嵌套结构已在 asdict 中递归展开为 dict
        return d

    @staticmethod
    def from_dict(d: dict) -> "Scenario":
        d = dict(d)
        bs = d.get("bet_sizes")
        if isinstance(bs, dict):
            d["bet_sizes"] = {
                side: {street: BetSizes(**cell) for street, cell in streets.items()}
                for side, streets in bs.items()
            }
        known = {f for f in Scenario.__dataclass_fields__}
        return Scenario(**{k: v for k, v in d.items() if k in known})

    def save(self, directory: str | Path | None = None) -> Path:
        """保存为 JSON(文件名由场景名 slug 化),返回路径。"""
        directory = Path(directory) if directory else _scenarios_dir()
        directory.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^\w一-鿿-]+", "_", self.name).strip("_") or "scenario"
        path = directory / f"{slug}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=1)
        return path

    @staticmethod
    def load(path: str | Path) -> "Scenario":
        with open(path, encoding="utf-8") as f:
            return Scenario.from_dict(json.load(f))

    @staticmethod
    def list_saved(directory: str | Path | None = None) -> list[Path]:
        directory = Path(directory) if directory else _scenarios_dir()
        if not directory.is_dir():
            return []
        return sorted(directory.glob("*.json"))


# ------------------------------------------------------------ 内置模板


def template_btn_vs_bb() -> Scenario:
    """BTN vs BB 单加底池:BTN 开池 2.5bb,BB 跟注。

    hero(BTN/IP)范围取 6-max RFI 图表 BTN 位的真实混合频率;
    villain(BB)范围近似为强度前 35%(跟注范围,3bet 牌型未剔除)。
    """
    charts = PreflopCharts()
    hero = {h: charts.rfi_raise_freq("BTN", h) for h in canonical_hands()}
    villain = top_range(0.35)
    return Scenario(
        name="BTN vs BB 单加底池",
        hero_range=hero,
        villain_range=villain,
        street="flop",
        pot=55.0,
        effective_stack=975.0,
        hero_is_ip=True,
    )


def template_bb_3bet() -> Scenario:
    """BB vs BTN 3bet 池:BTN 开池 2.5bb,BB 3bet 到 11bb,BTN 跟注。

    hero(BB/OOP)范围近似为强度前 12%(3bet 范围);
    villain(BTN)范围近似为强度前 15%(跟 3bet 范围)。
    """
    return Scenario(
        name="BB vs BTN 3bet 池",
        hero_range=top_range(0.12),
        villain_range=top_range(0.15),
        street="flop",
        pot=225.0,
        effective_stack=890.0,
        hero_is_ip=False,
    )


def template_hu_pushfold() -> Scenario:
    """HU 推佊边缘(10bb):短筹码翻后全下/弃牌结构。

    范围为 10bb 推佊表:hero(BTN/IP)取全下阈值 ≥10bb 的牌型,
    villain(BB)取跟注阈值 ≥10bb 的牌型;下注树只保留全下
    (allin_threshold=0,任意下注即全下)。
    """
    charts = PreflopCharts()
    shove = charts.pushfold_tables["shove"]
    call = charts.pushfold_tables["call"]
    hero = {h: (1.0 if shove[h] >= 10 else 0.0) for h in canonical_hands()}
    villain = {h: (1.0 if call[h] >= 10 else 0.0) for h in canonical_hands()}
    sizes = {
        side: {
            street: BetSizes(bet=[50.0], raises=[50.0], donk=[], allin=True)
            for street in ("flop", "turn", "river")
        }
        for side in ("oop", "ip")
    }
    return Scenario(
        name="HU 推佊边缘(10bb)",
        hero_range=hero,
        villain_range=villain,
        street="flop",
        pot=20.0,
        effective_stack=90.0,
        hero_is_ip=True,
        bet_sizes=sizes,
        allin_threshold=0.0,
        max_iteration=100,
    )


TEMPLATES = (
    ("BTN vs BB 单加底池", template_btn_vs_bb),
    ("BB vs BTN 3bet 池", template_bb_3bet),
    ("HU 推佊边缘(10bb)", template_hu_pushfold),
)


def builtin_templates() -> list[Scenario]:
    """全部内置模板实例(每次新建,调用方可随意修改)。"""
    return [fn() for _, fn in TEMPLATES]
