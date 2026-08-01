"""可选择的具名 AI 牌手目录:身份与打法相互独立。

背景故事为原创文本(粗粝酒馆风格,灵感致敬但不照搬 Liar's Bar 的设定)。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, replace

from . import styles
from .styles import PlayerStyle


@dataclass(frozen=True)
class Persona:
    """一名具名 AI 牌手。"""

    display_name: str  # 中文显示名
    species: str  # 物种
    backstory: str  # 一句话背景
    style: PlayerStyle
    level: str  # fish / reg / shark
    persona_id: str = ""  # 稳定身份 key,供 UI/存档/头像索引
    style_key: str = "BAL"  # 稳定打法 key,与身份可独立替换


def persona_catalog(seed: int | None = None) -> tuple[Persona, ...]:
    """返回十五名可选牌手；原九名身份及顺序保持不变。"""
    rng = random.Random(seed)
    return (
        Persona(
            display_name="公牛 Toar",
            species="公牛",
            backstory="码头扛了二十年货,牛角上挂着旧账本——他只打值得打的牌,一出手就要顶翻整张桌子。",
            style=styles.tight_aggressive(rng),
            level="shark",
            persona_id="bull",
            style_key="TAG",
        ),
        Persona(
            display_name="狐狸 Foxy",
            species="狐狸",
            backstory="城里三间当铺的幕后老板,笑着把你的表、你的车、最后你的自尊都收进袖口。",
            style=styles.loose_aggressive(rng),
            level="shark",
            persona_id="fox",
            style_key="LAG",
        ),
        Persona(
            display_name="犀牛 Gerk",
            species="犀牛",
            backstory="退役的地下拳手,皮厚、话少、步子慢;他等的不是好牌,是别人先沉不住气。",
            style=styles.tight_passive(rng),
            level="reg",
            persona_id="rhino",
            style_key="ROCK",
        ),
        Persona(
            display_name="屠夫猪 Bristle",
            species="野猪",
            backstory="肉铺的砧板剁了三十年,如今他把筹码当排骨——什么牌都想跟一刀,从不舍得扔。",
            style=styles.loose_passive(rng),
            level="fish",
            persona_id="boar",
            style_key="CALLER",
        ),
        Persona(
            display_name="看门狗 Scubby",
            species="看门犬",
            backstory="酒馆后巷的老门卫,谁出千、谁手抖、谁深夜哭过,他全记在心里,牌桌上滴水不漏。",
            style=styles.balanced(rng),
            level="shark",
            persona_id="dog",
            style_key="BAL",
        ),
        Persona(
            display_name="流浪猫 Stray",
            species="猫",
            backstory="没人知道她从哪张牌桌流浪而来;有免费花生的地方,就有她趴着数筹码。",
            style=styles.balanced(rng),
            level="reg",
            persona_id="cat",
            style_key="BAL",
        ),
        Persona(
            display_name="渡鸦 Corvin",
            species="渡鸦",
            backstory="旧钟楼的账房先生,能复述三天前每次下注;他每手换一种节奏,却从不忘别人露出的破绽。",
            style=styles.mixed_baseline(rng),
            level="shark",
            persona_id="raven",
            style_key="MIX",
        ),
        Persona(
            display_name="兔子 Mallow",
            species="兔",
            backstory="曾替巡回赌局修牌桌,手脚轻得像没碰过筹码;她不抢大锅,只一勺勺舀走你的耐心。",
            style=styles.small_ball(rng),
            level="reg",
            persona_id="rabbit",
            style_key="SMALL",
        ),
        Persona(
            display_name="灰狼 Varg",
            species="狼",
            backstory="雪线外来的赏金客,一嗅到迟疑便追过三条街;他把大底池当篝火,烧着自己也要逼你后退。",
            style=styles.maniac(rng),
            level="reg",
            persona_id="wolf",
            style_key="MANIAC",
        ),
        Persona(
            display_name="棕熊 Borin",
            species="棕熊",
            backstory="北岭伐木场的旧领班，掌心能盖住一摞筹码；他不急着亮爪，只等底池长到值得抱走。",
            style=styles.loose_passive(rng),
            level="reg",
            persona_id="bear",
            style_key="CALLER",
        ),
        Persona(
            display_name="雄狮 Aurelio",
            species="狮子",
            backstory="没落马戏团的前领班，鬃毛里还别着一枚铜徽章；他把每次加注都当作巡视自己的领地。",
            style=styles.tight_aggressive(rng),
            level="shark",
            persona_id="lion",
            style_key="TAG",
        ),
        Persona(
            display_name="猛虎 Raka",
            species="老虎",
            backstory="从雨林河港一路赌到这里的独行客，指节上的旧伤比筹码更多；一闻到软弱就会连扑三街。",
            style=styles.loose_aggressive(rng),
            level="shark",
            persona_id="tiger",
            style_key="LAG",
        ),
        Persona(
            display_name="老龟 Moss",
            species="乌龟",
            backstory="地下酒窖的看守，见过的牌局比酒桶年轮还多；他把筹码缩在壳边，只从最安全的缝隙伸手。",
            style=styles.tight_passive(rng),
            level="reg",
            persona_id="turtle",
            style_key="ROCK",
        ),
        Persona(
            display_name="夜枭 Orin",
            species="猫头鹰",
            backstory="旧法院的夜班书记员，灯熄之后仍能听出谁在撒谎；他安静记录节奏，再在最深的夜里翻开答案。",
            style=styles.mixed_baseline(rng),
            level="shark",
            persona_id="owl",
            style_key="MIX",
        ),
        Persona(
            display_name="黑豹 Nyx",
            species="黑豹",
            backstory="无灯巷里的情报贩子，从没人听见她靠近牌桌；小底池里只留一道影子，大底池里才露出獠牙。",
            style=styles.small_ball(rng),
            level="shark",
            persona_id="panther",
            style_key="SMALL",
        ),
    )


def default_personas(seed: int | None = None) -> list[Persona]:
    """返回原有五名默认牌手(兼容旧六人桌装配)。"""
    return list(persona_catalog(seed)[:5])


def persona_by_id(persona_id: str, seed: int | None = None) -> Persona:
    """按稳定身份 key 取一名牌手。"""
    normalized = persona_id.strip().lower()
    for persona in persona_catalog(seed):
        if persona.persona_id == normalized:
            return persona
    raise KeyError(f"未知 AI 身份 {persona_id!r}")


def with_style(
    persona: Persona,
    style_key: str,
    seed: int | None = None,
    level: str | None = None,
) -> Persona:
    """保留身份文案并换打法；等级默认跟随所选打法。"""
    preset = styles.style_by_key(style_key, random.Random(seed))
    return replace(
        persona,
        style=preset.style,
        level=level or preset.default_level,
        style_key=preset.key,
    )


def filler_persona(seed: int | None = None) -> Persona:
    """凑桌用的路人牌手(均衡风格、常客水平)。"""
    shifted = None if seed is None else seed + 999
    return persona_by_id("cat", shifted)
