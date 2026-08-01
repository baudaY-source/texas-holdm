"""Drills 训练:纯逻辑题目生成、评分与训练档案持久化(不依赖 pygame)。

三类题目:

- **翻前 RFI**:随机位置 + 随机手牌,正确答案 = ``gto/charts/rfi_6max.json``
  该位置率先加注表的混合频率;
- **翻前推佊**:随机 5-15bb 短筹码,正确答案 = Nash 推佊阈值表的全下/弃牌;
- **翻后**:从 ``gto/strategies/`` 预计算库随机取 spot,从 hero(OOP/BB)
  范围抽一手牌,正确答案 = 根节点 169 矩阵中该牌型的动作频率;
  库为空时 ``postflop_available()`` 返回 False,界面跳过该类。

难度分档:**新手** 只出"清晰局"(最高频动作 ≥0.8;推佊类因答案恒为 0/1,
改用"决策边界余量 ≥3bb"作为清晰标准);**进阶** 只出混合局(最高频 <0.8,
推佊为边界 ±2bb 内的边缘局)。

评分(GTO 偏差概念):所选动作在正确答案中的频率 ≥0.6 得满分 1.0,
0.2-0.6 得半分 0.5,<0.2 得 0 分。连对 = 连续满分次数。

CLI 自检::

    .venv/Scripts/python.exe -m training.drills --n 5
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from gto.charts import PreflopCharts, canonical_hands, combos_for
from gto.library import StrategyLibrary
from ui.respath import user_data_path

# ------------------------------------------------------------ 常量

CATEGORY_RFI = "rfi"
CATEGORY_PUSHFOLD = "pushfold"
CATEGORY_POSTFLOP = "postflop"
CATEGORIES = (CATEGORY_RFI, CATEGORY_PUSHFOLD, CATEGORY_POSTFLOP)
CATEGORY_LABEL = {
    CATEGORY_RFI: "翻前 RFI",
    CATEGORY_PUSHFOLD: "翻前推佊",
    CATEGORY_POSTFLOP: "翻后求解",
}

TIER_NEWBIE = "newbie"
TIER_ADVANCED = "advanced"
TIERS = (TIER_NEWBIE, TIER_ADVANCED)
TIER_LABEL = {TIER_NEWBIE: "新手", TIER_ADVANCED: "进阶"}

RFI_POSITIONS = ("UTG", "MP", "CO", "BTN", "SB")
PUSHFOLD_POSITIONS = ("BTN", "SB")  # 阈值表本身是 HU SB/BTN 的 Nash 解
PUSHFOLD_MIN_BB, PUSHFOLD_MAX_BB = 5, 15

# 评分阈值:≥0.6 满分,0.2-0.6 半分,<0.2 零分
FULL_CREDIT = 0.6
HALF_CREDIT = 0.2
# 清晰局判定:最高频动作 ≥ 0.8
CLEAR_CUT = 0.8

DEFAULT_PROFILE_PATH = user_data_path("training", "profile.json")

_ACTION_LABEL = {
    "FOLD": "弃牌",
    "CHECK": "过牌",
    "CALL": "跟注",
    "BET": "下注",
    "RAISE": "加注",
    "ALLIN": "全下",
}

# 求解器动作串首词 → 面板动作键(与 gto.advisor 同一映射)
_SOLVER_ACTION = {
    "CHECK": "CHECK", "FOLD": "FOLD", "CALL": "CALL",
    "BET": "BET", "RAISE": "RAISE", "ALLIN": "ALLIN",
}

_RFICHART_ACTION = {"raise": "RAISE", "call": "CALL", "fold": "FOLD", "allin": "ALLIN"}


def action_label(action: str) -> str:
    """动作键 → 中文标签。"""
    return _ACTION_LABEL.get(action, action)


def score_answer(correct_freqs: dict[str, float], action: str) -> float:
    """按 GTO 偏差评分:所选动作频率 ≥0.6 → 1.0;0.2-0.6 → 0.5;否则 0。"""
    f = correct_freqs.get(action, 0.0)
    if f >= FULL_CREDIT:
        return 1.0
    if f >= HALF_CREDIT:
        return 0.5
    return 0.0


# ------------------------------------------------------------ 题目


@dataclass(frozen=True)
class Drill:
    """一道训练题。

    :param category: 类目(``rfi`` / ``pushfold`` / ``postflop``)。
    :param prompt: 局面一句话描述(中文)。
    :param context: 局面上下文(position / stack_bb / pot / board / spot_id 等,
        按类目取用,均为可 JSON 序列化的标量)。
    :param hole_cards: hero 的两张具体底牌。
    :param options: 可选动作键(2-3 个,展示为按钮)。
    :param correct_freqs: 正确答案(动作键 → 频率,和 ≈ 1)。
    :param explanation: 中文解析(引用图表/求解来源)。
    """

    category: str
    prompt: str
    context: dict
    hole_cards: tuple[str, str]
    options: tuple[str, ...]
    correct_freqs: dict[str, float]
    explanation: str

    @property
    def best_action(self) -> str:
        """频率最高的动作。"""
        return max(self.correct_freqs, key=self.correct_freqs.get)  # type: ignore[arg-type]


class DrillGenerator:
    """题目生成器。

    :param charts: 翻前图表门面;缺省加载 ``gto/charts/``。
    :param library: 预计算策略库;缺省加载 ``gto/strategies/``。
    :param seed: 随机种子(测试可复现)。
    """

    def __init__(
        self,
        charts: PreflopCharts | None = None,
        library: StrategyLibrary | None = None,
        seed: int | None = None,
    ) -> None:
        self.charts = charts or PreflopCharts()
        self.library = library if library is not None else StrategyLibrary()
        self._rng = random.Random(seed)

    # ------------------------------------------------------------ 入口

    def postflop_available(self) -> bool:
        """翻后题库是否可用(策略库非空)。"""
        return len(self.library) > 0

    def next(self, category: str, tier: str = TIER_ADVANCED) -> Drill | None:
        """生成一道指定类目/难度的题;类目不可用时返回 ``None``。"""
        if category == CATEGORY_RFI:
            return self.rfi_drill(tier)
        if category == CATEGORY_PUSHFOLD:
            return self.pushfold_drill(tier)
        if category == CATEGORY_POSTFLOP:
            return self.postflop_drill(tier)
        raise ValueError(f"未知类目: {category}")

    # ------------------------------------------------------------ 翻前 RFI

    def rfi_drill(self, tier: str = TIER_ADVANCED, max_tries: int = 60) -> Drill:
        """随机 RFI 题:按难度筛选清晰/混合局,重试超限后兜底返回最后一题。"""
        fallback: Drill | None = None
        for _ in range(max_tries):
            pos = self._rng.choice(RFI_POSITIONS)
            hand = self._rng.choice(canonical_hands())
            drill = self.rfi_drill_for(pos, hand, rng=self._rng)
            if _tier_match(_top_freq(drill.correct_freqs) >= CLEAR_CUT, tier):
                return drill
            fallback = drill
        assert fallback is not None
        return fallback

    def rfi_drill_for(
        self, position: str, hand: str, rng: random.Random | None = None
    ) -> Drill:
        """指定位置 + 规范牌型的 RFI 题(正确答案 = 图表混合频率)。"""
        rng = rng or self._rng
        cell = dict(self.charts.rfi[position][hand])
        freqs: dict[str, float] = {}
        for chart_key, f in cell.items():
            if f <= 0:
                continue
            key = _RFICHART_ACTION[chart_key]
            freqs[key] = freqs.get(key, 0.0) + f
        freqs = _normalize(freqs)
        combo = _pick_combo(hand, rng, blocked=())
        raise_pct = round(cell.get("raise", 0.0) * 100)
        call_pct = round(cell.get("call", 0.0) * 100)
        fold_pct = round(cell.get("fold", 0.0) * 100)
        prompt = f"你在 {position},筹码 100bb,前面无人加注(未开池),怎么办?"
        explanation = (
            f"翻前 {position} 率先加注表:{hand} 加注 {raise_pct}% · "
            f"跟注 {call_pct}% · 弃牌 {fold_pct}%"
            f"(数据源 hellomate2/gto-poker-overlay,见 gto/charts/SOURCE.md)。"
            f"频率 ≥60% 的动作视为标准打法,20-60% 为可接受的混合。"
        )
        return Drill(
            category=CATEGORY_RFI,
            prompt=prompt,
            context={"position": position, "stack_bb": 100.0, "hand": hand},
            hole_cards=combo,
            options=("FOLD", "CALL", "RAISE"),
            correct_freqs=freqs,
            explanation=explanation,
        )

    # ------------------------------------------------------------ 翻前推佊

    def pushfold_drill(self, tier: str = TIER_ADVANCED, max_tries: int = 60) -> Drill:
        """随机推佊题(5-15bb):新手 = 决策边界余量大的清晰局,进阶 = 边缘局。"""
        fallback: Drill | None = None
        for _ in range(max_tries):
            pos = self._rng.choice(PUSHFOLD_POSITIONS)
            hand = self._rng.choice(canonical_hands())
            stack_bb = self._rng.randint(PUSHFOLD_MIN_BB, PUSHFOLD_MAX_BB)
            drill = self.pushfold_drill_for(pos, hand, stack_bb, rng=self._rng)
            margin = abs(self.charts.pushfold_tables["shove"][hand] - stack_bb)
            # 答案恒为 0/1,用"距阈值边界"定义清晰度:≥3bb 清晰,≤2bb 边缘
            if _tier_match(margin >= 3, tier):
                return drill
            fallback = drill
        assert fallback is not None
        return fallback

    def pushfold_drill_for(
        self,
        position: str,
        hand: str,
        stack_bb: float,
        rng: random.Random | None = None,
    ) -> Drill:
        """指定位置/牌型/筹码的推佊题(正确答案 = Nash 阈值表)。"""
        rng = rng or self._rng
        threshold = self.charts.pushfold_tables["shove"][hand]
        shove = stack_bb <= threshold
        freqs = {"ALLIN": 1.0} if shove else {"FOLD": 1.0}
        combo = _pick_combo(hand, rng, blocked=())
        threshold_txt = "任意深度" if threshold >= 999 else f"≤{threshold}bb"
        prompt = f"你在 {position},只剩 {stack_bb:.0f}bb,未开池,推还是丢?"
        explanation = (
            f"Nash 推佊表:{hand} {threshold_txt}可全下;当前 {stack_bb:.0f}bb → "
            f"{'全下' if shove else '弃牌'}。"
            f"短筹码不要小额加注:全下能最大化弃牌收益并避免被反加为难。"
        )
        return Drill(
            category=CATEGORY_PUSHFOLD,
            prompt=prompt,
            context={"position": position, "stack_bb": float(stack_bb), "hand": hand},
            hole_cards=combo,
            options=("FOLD", "ALLIN"),
            correct_freqs=freqs,
            explanation=explanation,
        )

    # ------------------------------------------------------------ 翻后

    def postflop_drill(self, tier: str = TIER_ADVANCED, max_tries: int = 60) -> Drill | None:
        """随机翻后题(预计算库根节点 = hero OOP);库为空返回 ``None``。"""
        spots = self.library.spots
        if not spots:
            return None
        fallback: Drill | None = None
        for _ in range(max_tries):
            spot = self._rng.choice(spots)
            drill = self._postflop_from_spot(spot)
            if drill is None:
                continue
            if _tier_match(_top_freq(drill.correct_freqs) >= CLEAR_CUT, tier):
                return drill
            fallback = drill
        return fallback

    def _postflop_from_spot(self, spot: dict) -> Drill | None:
        """从一条库 spot 出题:hero = OOP(BB),正确答案 = 根节点矩阵频率。"""
        meta = spot.get("config_meta", {})
        root = spot.get("root") or {}
        matrix: dict = root.get("strategy_169") or {}
        board = list(meta.get("board", []))
        if not matrix or len(board) < 3:
            return None
        candidates = [
            h for h, cell in matrix.items() if sum(cell.values()) > 0 and not _blocked(h, board)
        ]
        if not candidates:
            return None
        hand = self._rng.choice(candidates)
        raw = matrix[hand]
        freqs: dict[str, float] = {}
        for action_str, f in raw.items():
            if f <= 0:
                continue
            key = _SOLVER_ACTION.get(str(action_str).split()[0].upper())
            if key is not None:
                freqs[key] = freqs.get(key, 0.0) + f
        if not freqs:
            return None
        freqs = _normalize(freqs)
        options = tuple(sorted(freqs, key=lambda a: -freqs[a]))
        if len(options) == 1:
            # 单动作题补一个干扰项,保持 2-3 个按钮
            options = options + (("BET",) if options[0] == "CHECK" else ("CHECK",))
        combo = _pick_combo(hand, self._rng, blocked=board)
        spot_id = spot.get("spot_id", "?")
        pot, stack = float(meta.get("pot", 0)), float(meta.get("effective_stack", 0))
        street = str(meta.get("street", "flop"))
        board_txt = " ".join(board)
        freq_txt = " · ".join(f"{action_label(a)} {f:.0%}" for a, f in freqs.items())
        prompt = (
            f"单挑:你在 BB(先手),底池 {pot:.0f},有效筹码 {stack:.0f}。"
            f"{_street_label(street)} {board_txt},轮到你先行动,怎么办?"
        )
        explanation = (
            f"离线 TexasSolver 求解 {spot_id}(根节点/OOP 首行动):"
            f"{hand} 的策略为 {freq_txt}。"
            f"翻后 GTO 是混合策略游戏:频率即答案,选高频动作即为标准打法。"
        )
        return Drill(
            category=CATEGORY_POSTFLOP,
            prompt=prompt,
            context={
                "position": "BB",
                "pot": pot,
                "stack": stack,
                "street": street,
                "board": board,
                "spot_id": spot_id,
                "hand": hand,
            },
            hole_cards=combo,
            options=options,
            correct_freqs=freqs,
            explanation=explanation,
        )


# ------------------------------------------------------------ 评分会话


@dataclass
class AnswerRecord:
    """一次作答结果。"""

    drill: Drill
    action: str
    score: float
    streak: int  # 作答后的连对数


class DrillSession:
    """一次训练会话:出题、判分、连对与准确率统计,并把结果写入档案。

    :param category / tier: 类目与难度。
    :param profile_path: 档案路径;``None`` 用默认 ``training/profile.json``。
    :param seed / generator: 复现与注入用。
    """

    def __init__(
        self,
        category: str,
        tier: str = TIER_ADVANCED,
        profile_path: str | Path | None = None,
        seed: int | None = None,
        generator: DrillGenerator | None = None,
    ) -> None:
        if category not in CATEGORIES:
            raise ValueError(f"未知类目: {category}")
        if tier not in TIERS:
            raise ValueError(f"未知难度: {tier}")
        self.category = category
        self.tier = tier
        self.generator = generator or DrillGenerator(seed=seed)
        self.profile = DrillProfile(profile_path or DEFAULT_PROFILE_PATH)
        self.asked = 0
        self.total_score = 0.0
        self.streak = 0
        self.best_streak = 0
        self._finished = False

    @property
    def accuracy(self) -> float:
        """本会话平均得分(0..1);未作答为 0。"""
        return self.total_score / self.asked if self.asked else 0.0

    def next_drill(self) -> Drill | None:
        """出下一题(类目不可用时返回 ``None``)。"""
        return self.generator.next(self.category, self.tier)

    def answer(self, drill: Drill, action: str) -> AnswerRecord:
        """判分并记录:更新连对/准确率,写入档案(每次作答即落盘)。"""
        if action not in drill.options:
            raise ValueError(f"动作 {action} 不在本题选项 {drill.options} 中")
        score = score_answer(drill.correct_freqs, action)
        self.asked += 1
        self.total_score += score
        self.streak = self.streak + 1 if score >= 1.0 else 0
        self.best_streak = max(self.best_streak, self.streak)
        self.profile.record_answer(self.category, score, self.streak)
        return AnswerRecord(drill=drill, action=action, score=score, streak=self.streak)

    def finish(self) -> None:
        """结束会话:把本次小结追加进档案历史(幂等,重复调用只记一次)。"""
        if self._finished:
            return
        self._finished = True
        if self.asked:
            self.profile.record_session(
                self.category, self.tier, self.asked, self.accuracy
            )


class DrillProfile:
    """训练档案(JSON 持久化,线程安全"够用级":锁 + 原子替换写)。

    结构::

        {"sessions": int, "answers": int, "total_score": float,
         "best_streak": int,
         "per_category": {类目: {"asked": int, "score": float, "best_streak": int}},
         "accuracy_history": [{ts, category, tier, asked, accuracy}, ...]}
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._data = self._load()

    # ------------------------------------------------------------ 读

    @property
    def data(self) -> dict:
        """当前档案内容(浅拷贝)。"""
        with self._lock:
            return json.loads(json.dumps(self._data))

    def _load(self) -> dict:
        try:
            with self._path.open(encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                base = _empty_profile()
                base.update(d)
                return base
        except (OSError, json.JSONDecodeError):
            pass
        return _empty_profile()

    # ------------------------------------------------------------ 写

    def record_answer(self, category: str, score: float, streak: int) -> None:
        """累计一次作答(每次落盘)。"""
        with self._lock:
            d = self._data
            d["answers"] += 1
            d["total_score"] += score
            d["best_streak"] = max(d["best_streak"], streak)
            cat = d["per_category"].setdefault(
                category, {"asked": 0, "score": 0.0, "best_streak": 0}
            )
            cat["asked"] += 1
            cat["score"] += score
            cat["best_streak"] = max(cat["best_streak"], streak)
            self._save_locked()

    def record_session(self, category: str, tier: str, asked: int, accuracy: float) -> None:
        """追加一次会话小结。"""
        with self._lock:
            self._data["sessions"] += 1
            self._data["accuracy_history"].append(
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "category": category,
                    "tier": tier,
                    "asked": asked,
                    "accuracy": round(accuracy, 4),
                }
            )
            self._save_locked()

    def _save_locked(self) -> None:
        """原子写:先写临时文件再替换(调用方须已持锁)。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self._path)


def _empty_profile() -> dict:
    return {
        "sessions": 0,
        "answers": 0,
        "total_score": 0.0,
        "best_streak": 0,
        "per_category": {},
        "accuracy_history": [],
    }


# ------------------------------------------------------------ 内部工具


def _tier_match(clear_cut: bool, tier: str) -> bool:
    """难度匹配:新手要清晰局,进阶要混合局。"""
    return clear_cut if tier == TIER_NEWBIE else not clear_cut


def _top_freq(freqs: dict[str, float]) -> float:
    return max(freqs.values(), default=0.0)


def _normalize(freqs: dict[str, float]) -> dict[str, float]:
    total = sum(freqs.values())
    if total <= 0:
        return dict(freqs)
    return {k: v / total for k, v in freqs.items()}


def _blocked(hand: str, board: list[str]) -> bool:
    """该规范牌型的所有组合是否都与公共牌冲突(冲突则不可出题)。"""
    return all(c[0] in board or c[1] in board for c in combos_for(hand))


def _pick_combo(
    hand: str, rng: random.Random, blocked: tuple[str, ...] | list[str]
) -> tuple[str, str]:
    """从规范牌型中随机挑一组不与 ``blocked`` 冲突的具体两牌。"""
    blocked_set = set(blocked)
    ok = [c for c in combos_for(hand) if c[0] not in blocked_set and c[1] not in blocked_set]
    if not ok:
        raise ValueError(f"{hand} 的全部组合都与 {blocked} 冲突")
    return rng.choice(ok)


def _street_label(street: str) -> str:
    return {"flop": "翻牌", "turn": "转牌", "river": "河牌"}.get(street, street)


# ------------------------------------------------------------ CLI 自检


def main(argv: list[str] | None = None) -> int:
    """打印若干样题与答案,供人工校验生成器。"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="drills 样题自检")
    parser.add_argument("--n", type=int, default=5, help="每类题目数量")
    parser.add_argument("--seed", type=int, default=7, help="随机种子")
    parser.add_argument(
        "--tier", choices=TIERS, default=TIER_ADVANCED, help="难度(默认进阶)"
    )
    args = parser.parse_args(argv)
    gen = DrillGenerator(seed=args.seed)
    for category in CATEGORIES:
        if category == CATEGORY_POSTFLOP and not gen.postflop_available():
            print(f"== {CATEGORY_LABEL[category]}:策略库为空,跳过 ==")
            continue
        print(f"== {CATEGORY_LABEL[category]}({TIER_LABEL[args.tier]})==")
        for i in range(args.n):
            drill = gen.next(category, args.tier)
            assert drill is not None
            answer = " / ".join(
                f"{action_label(a)} {f:.0%}" for a, f in drill.correct_freqs.items()
            )
            hole = " ".join(drill.hole_cards)
            print(f"[{i + 1}] {drill.prompt}")
            print(f"    手牌 {hole} | 选项 {'/'.join(action_label(a) for a in drill.options)}")
            print(f"    答案 {answer}")
            print(f"    解析 {drill.explanation}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
