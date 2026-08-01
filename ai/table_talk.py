"""牌桌人格对白与结算后诈唬识别。

本模块只消费稳定人格标识和已经结算的公开结果，不依赖 pygame，也不会
向机器人暴露任何对手底牌。UI 可直接把历史动作记录的 ``actions`` 列表
交给 :func:`is_successful_bluff`，再按结果选择对应对白。
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Sequence


PERSONA_IDS = (
    "bull",
    "fox",
    "rhino",
    "boar",
    "dog",
    "cat",
    "raven",
    "rabbit",
    "wolf",
)


class TalkEvent(str, Enum):
    """结算后可触发的四类对白。"""

    COLLECT = "collect"
    LOST_POT = "lost_pot"
    BLUFF_SUCCESS = "bluff_success"
    BLUFFED = "bluffed"


@dataclass(frozen=True)
class ActionTrace:
    """用于诈唬识别的最小动作记录。"""

    street: str
    seat: int
    action: str


_LINES: dict[str, dict[TalkEvent, tuple[str, ...]]] = {
    "bull": {
        TalkEvent.COLLECT: (
            "这池筹码，扛得动才配拿。",
            "账算清了，筹码归我。",
            "牛角一低，就没有回头路。",
        ),
        TalkEvent.LOST_POT: (
            "这一趟货，我认赔。",
            "好手。下一把再顶回来。",
            "码头的风，比这点损失硬多了。",
        ),
        TalkEvent.BLUFF_SUCCESS: (
            "你躲的不是牌，是我的牛角。",
            "空车也能撞开一条路。",
            "我押的是你先退。",
        ),
        TalkEvent.BLUFFED: (
            "拿空气顶我？你有胆。",
            "这笔账，我记在角上了。",
            "好骗。可别让我逮到第二次。",
        ),
    },
    "fox": {
        TalkEvent.COLLECT: (
            "别难过，筹码只是换了个更聪明的主人。",
            "谢谢惠顾，本店概不退货。",
            "我只动了一下尾巴，你们就全信了。",
        ),
        TalkEvent.LOST_POT: (
            "有趣，这次轮到我付学费。",
            "先替我保管，晚点我来取。",
            "这枚筹码出去转一圈罢了。",
        ),
        TalkEvent.BLUFF_SUCCESS: (
            "看见了吗？最贵的牌叫想象力。",
            "牌是空的，故事可是真的。",
            "你弃掉的不是牌，是勇气。",
        ),
        TalkEvent.BLUFFED: (
            "哦？有人偷走了狐狸的钱袋。",
            "演得不错，差点像真的一样。",
            "这一课很贵，不过我记性很好。",
        ),
    },
    "rhino": {
        TalkEvent.COLLECT: (
            "等得够久，门自己会开。",
            "我不赶时间，筹码会走过来。",
            "拳头不用多，打中一次就够。",
        ),
        TalkEvent.LOST_POT: (
            "没倒。继续。",
            "这一拳挨得住。",
            "皮厚，账慢慢算。",
        ),
        TalkEvent.BLUFF_SUCCESS: (
            "我没牌，但我站得比你稳。",
            "你先眨眼了。",
            "虚招，也是招。",
        ),
        TalkEvent.BLUFFED: (
            "……记住你了。",
            "假拳？下次别让我抓住。",
            "这一回，我看慢了。",
        ),
    },
    "boar": {
        TalkEvent.COLLECT: (
            "嘿！这一锅全端走！",
            "筹码和排骨一样，多多益善！",
            "跟到底，总有肉吃！",
        ),
        TalkEvent.LOST_POT: (
            "啧，剁到自己蹄子了。",
            "这块肉让你，下一块归我！",
            "输一锅算什么，再开火！",
        ),
        TalkEvent.BLUFF_SUCCESS: (
            "哈哈！砧板底下根本没肉！",
            "我随手一拍，你们就跑啦？",
            "空刀也能吓退人！",
        ),
        TalkEvent.BLUFFED: (
            "什么？盘子里是空的！",
            "敢拿空气糊弄屠夫？",
            "好小子，连猪都骗！",
        ),
    },
    "dog": {
        TalkEvent.COLLECT: (
            "门守住了，底池也一样。",
            "你的下注节奏，我听得很清楚。",
            "规矩没变：犯错的人付钱。",
        ),
        TalkEvent.LOST_POT: (
            "这次漏看了一扇门。",
            "记下了，不会再漏。",
            "你藏得很好，暂时。",
        ),
        TalkEvent.BLUFF_SUCCESS: (
            "我知道你会弃，所以牌不重要。",
            "门后什么也没有，你还是没敢进。",
            "习惯会出卖人，你也不例外。",
        ),
        TalkEvent.BLUFFED: (
            "气味不对，我却放你过去了。",
            "漂亮的假动作。档案里见。",
            "你骗过一次，等于提醒我一次。",
        ),
    },
    "cat": {
        TalkEvent.COLLECT: (
            "喵。今晚的花生钱有了。",
            "筹码滚到爪边，可不能不捡。",
            "我只是趴着，它们自己过来的。",
        ),
        TalkEvent.LOST_POT: (
            "喵呜……这桌不够暖。",
            "先借你玩一会儿。",
            "猫有九条命，筹码也会回来的。",
        ),
        TalkEvent.BLUFF_SUCCESS: (
            "纸袋里没鱼，你还是被吓跑了。",
            "尾巴一摆，故事就成真啦。",
            "空爪也会挠人哦。",
        ),
        TalkEvent.BLUFFED: (
            "嘶——你拿空袋子骗猫！",
            "这味道居然是假的。",
            "好吧，这次是你更会装睡。",
        ),
    },
    "raven": {
        TalkEvent.COLLECT: (
            "亮晶晶的，都归我的巢。",
            "我见过结局，才落在这张桌上。",
            "每枚筹码都在讲你的秘密。",
        ),
        TalkEvent.LOST_POT: (
            "不祥的预言，偶尔也会迟到。",
            "拿去吧，乌鸦会循着光回来。",
            "今夜少一枚亮物而已。",
        ),
        TalkEvent.BLUFF_SUCCESS: (
            "影子飞过，你便以为黑夜来了。",
            "我只叫了一声，你就放下了牌。",
            "空洞的预言，照样有人相信。",
        ),
        TalkEvent.BLUFFED: (
            "连乌鸦也看走了眼。",
            "你的谎言很亮，我收下了。",
            "嘎……这段预言得重写。",
        ),
    },
    "rabbit": {
        TalkEvent.COLLECT: (
            "快一点，底池就追不上我啦。",
            "别眨眼，筹码已经搬完了。",
            "小步试探，大包带走！",
        ),
        TalkEvent.LOST_POT: (
            "耳朵抖了一下，被你看见了。",
            "先跑一圈，马上回来。",
            "这坑踩过，下次就会跳过去。",
        ),
        TalkEvent.BLUFF_SUCCESS: (
            "我跑得快，谎话也一样！",
            "你还在想，我已经把空气卖掉了。",
            "谁说兔子只能逃跑？",
        ),
        TalkEvent.BLUFFED: (
            "诶？原来你也在挖洞！",
            "这一下真的没听出来。",
            "好险，下次耳朵会竖得更高。",
        ),
    },
    "wolf": {
        TalkEvent.COLLECT: (
            "落单的筹码，终归属于狼。",
            "我闻到了犹豫，然后扑了上去。",
            "猎物停步的时候，牌局就结束了。",
        ),
        TalkEvent.LOST_POT: (
            "猎人也会留下伤口。",
            "今晚你跑掉了。",
            "血味还在，下一次我会追上。",
        ),
        TalkEvent.BLUFF_SUCCESS: (
            "我露出牙，你就忘了看牌。",
            "没有猎物？那就猎你的恐惧。",
            "这一声嚎叫，值整池筹码。",
        ),
        TalkEvent.BLUFFED: (
            "披着强牌的羊……有意思。",
            "假足迹也骗过了狼。",
            "记住，戏弄狼要付第二次价钱。",
        ),
    },
}


_GENERIC_LINES: dict[TalkEvent, tuple[str, ...]] = {
    TalkEvent.COLLECT: ("这一池我收下了。", "承让。", "筹码归我。"),
    TalkEvent.LOST_POT: ("这手你赢。", "下一手见。", "牌桌还长。"),
    TalkEvent.BLUFF_SUCCESS: ("你信了我的故事。", "这次，牌并不重要。", "空牌也能收池。"),
    TalkEvent.BLUFFED: ("原来是个诈唬。", "这次被你骗到了。", "演得不错。"),
}


_EVENT_ALIASES = {
    "collect": TalkEvent.COLLECT,
    "win": TalkEvent.COLLECT,
    "won": TalkEvent.COLLECT,
    "lost_pot": TalkEvent.LOST_POT,
    "lost": TalkEvent.LOST_POT,
    "loss": TalkEvent.LOST_POT,
    "bluff_success": TalkEvent.BLUFF_SUCCESS,
    "bluff": TalkEvent.BLUFF_SUCCESS,
    "bluffed": TalkEvent.BLUFFED,
}


_IDENTITY_MARKERS: dict[str, tuple[str, ...]] = {
    "bull": ("bull", "toar", "公牛"),
    "fox": ("fox", "foxy", "狐狸"),
    "rhino": ("rhino", "gerk", "犀牛"),
    "boar": ("boar", "bristle", "屠夫猪", "野猪"),
    "dog": ("dog", "scubby", "看门狗", "看门犬"),
    "cat": ("cat", "stray", "流浪猫"),
    "raven": ("raven", "乌鸦"),
    "rabbit": ("rabbit", "兔"),
    "wolf": ("wolf", "狼"),
}


def normalize_persona_id(identity: object) -> str | None:
    """把稳定 ID 或 UI 显示名归一化；未知身份返回 ``None``。"""

    text = str(identity).strip().casefold()
    for persona_id, markers in _IDENTITY_MARKERS.items():
        if text == persona_id or any(marker.casefold() in text for marker in markers):
            return persona_id
    return None


def choose_table_line(
    persona_id: object,
    event: TalkEvent | str,
    rng: random.Random | None = None,
) -> str:
    """选择一句符合人格和结算事件的对白。

    ``persona_id`` 既可传稳定 ID，也可直接传 ``Persona.display_name``。
    给定相同种子的 :class:`random.Random` 时，选择结果可复现。
    """

    if isinstance(event, TalkEvent):
        normalized_event = event
    else:
        try:
            normalized_event = _EVENT_ALIASES[str(event).strip().casefold()]
        except KeyError as exc:
            raise ValueError(f"未知牌桌对白事件：{event!r}") from exc
    identity = normalize_persona_id(persona_id)
    lines = _LINES.get(identity, _GENERIC_LINES)[normalized_event]
    chooser = rng if rng is not None else random
    return chooser.choice(lines)


_CATEGORY_STRENGTH = {
    "HIGH_CARD": 0,
    "ONE_PAIR": 1,
    "TWO_PAIR": 2,
    "THREE_OF_A_KIND": 3,
    "STRAIGHT": 4,
    "FLUSH": 5,
    "FULL_HOUSE": 6,
    "FOUR_OF_A_KIND": 7,
    "STRAIGHT_FLUSH": 8,
}
_AGGRESSIVE_ACTIONS = frozenset(("BET", "RAISE", "ALLIN"))


def _category_strength(category: object) -> int:
    if isinstance(category, str):
        key = category.strip().upper().replace(" ", "_")
    elif hasattr(category, "name"):
        key = str(getattr(category, "name")).upper()
    else:
        try:
            value = int(category)  # 支持 HandCategory IntEnum
        except (TypeError, ValueError) as exc:
            raise ValueError(f"未知手牌牌型：{category!r}") from exc
        if 0 <= value <= 8:
            return value
        raise ValueError(f"未知手牌牌型：{category!r}")
    try:
        return _CATEGORY_STRENGTH[key]
    except KeyError as exc:
        raise ValueError(f"未知手牌牌型：{category!r}") from exc


def _action_trace(raw: ActionTrace | Mapping[str, object]) -> ActionTrace:
    if isinstance(raw, ActionTrace):
        return raw
    try:
        action = raw["action"]
        action_name = (
            str(getattr(action, "name")) if hasattr(action, "name") else str(action)
        )
        return ActionTrace(
            street=str(raw.get("street", "")),
            seat=int(raw["seat"]),
            action=action_name,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"非法动作记录：{raw!r}") from exc


def successful_bluff_targets(
    *,
    winner_seat: int,
    hand_category: object,
    actions: Sequence[ActionTrace | Mapping[str, object]],
    showdown: bool,
    maximum_category: object = "ONE_PAIR",
) -> tuple[int, ...]:
    """返回被该玩家成功诈唬而弃牌的座位。

    判定刻意偏保守：必须未摊牌、赢家至多持有 ``maximum_category`` 指定
    的弱牌，并且赢家最后一次下注/加注/全下之后确有对手弃牌。调用者应
    只在手牌结算后传入赢家自己的牌型与公开动作线；本函数不会读取或
    返回任何对手底牌。
    """

    if showdown or _category_strength(hand_category) > _category_strength(
        maximum_category
    ):
        return ()
    traces = tuple(_action_trace(raw) for raw in actions)
    last_aggression = -1
    for index, trace in enumerate(traces):
        if trace.seat == winner_seat and trace.action.strip().upper() in _AGGRESSIVE_ACTIONS:
            last_aggression = index
    if last_aggression < 0:
        return ()
    targets: list[int] = []
    for trace in traces[last_aggression + 1 :]:
        if trace.seat != winner_seat and trace.action.strip().upper() == "FOLD":
            if trace.seat not in targets:
                targets.append(trace.seat)
    return tuple(targets)


def is_successful_bluff(
    *,
    winner_seat: int,
    hand_category: object,
    actions: Sequence[ActionTrace | Mapping[str, object]],
    showdown: bool,
    maximum_category: object = "ONE_PAIR",
) -> bool:
    """判断结算结果是否为成功诈唬；参数同 :func:`successful_bluff_targets`。"""

    return bool(
        successful_bluff_targets(
            winner_seat=winner_seat,
            hand_category=hand_category,
            actions=actions,
            showdown=showdown,
            maximum_category=maximum_category,
        )
    )


__all__ = [
    "ActionTrace",
    "PERSONA_IDS",
    "TalkEvent",
    "choose_table_line",
    "is_successful_bluff",
    "normalize_persona_id",
    "successful_bluff_targets",
]
