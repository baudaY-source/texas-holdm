"""AI 玩家风格:连续参数 + 可供开局/换人选择的打法目录。

风格参数均为连续值(频率类 0..1,``aggression`` 约 0..3),
工厂函数可注入 ``random.Random`` 以对参数施加小幅高斯抖动
(同一会话内每个 AI 略有差异,且可由种子复现)。打法目录使用稳定 key,
UI 与存档不得依赖中文显示名做判断。
"""
from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class PlayerStyle:
    """一名 AI 玩家的连续风格参数。

    频率类参数均在 [0,1];``aggression`` 为激进度系数(约 0..3,
    1 为中性);``sizing_*`` 为各街道偏好的底池分数下注尺度样本集。
    """

    vpip: float  # 自愿入池率
    pfr: float  # 翻牌前加注率
    threebet: float  # 再加注(3bet)率
    aggression: float  # 激进度系数(0..~3)
    cbet_freq: float  # 持续下注频率
    bluff_freq: float  # 诈唬倾向
    fold_to_raise: float  # 面对加注弃牌倾向
    donk_freq: float  # 反主动下注(donk)频率
    sizing_flop: tuple[float, ...] = (0.5, 0.66)
    sizing_turn: tuple[float, ...] = (0.5, 0.75)
    sizing_river: tuple[float, ...] = (0.66, 1.0)


@dataclass(frozen=True)
class StylePreset:
    """一项可选择打法及其简要说明。

    ``MIX`` 的 ``style`` 是供统计/提示使用的综合参数；实际决策必须由
    :class:`ai.bots.StyleMixerBot` 在 ``mix_components`` 之间按整手切换。
    """

    key: str
    label: str
    description: str
    style: PlayerStyle
    default_level: str
    mix_components: tuple[str, ...] = ()
    mix_weights: tuple[float, ...] = ()


def _jitter(style_kwargs: dict, rng: random.Random | None, rel: float = 0.05) -> dict:
    """对标量参数施加相对高斯抖动(频率类截断到 [0,1])。"""
    if rng is None:
        return style_kwargs
    out = {}
    freq_keys = {
        "vpip", "pfr", "threebet", "cbet_freq", "bluff_freq",
        "fold_to_raise", "donk_freq",
    }
    for k, v in style_kwargs.items():
        if k in freq_keys:
            out[k] = max(0.0, min(1.0, v * (1 + rng.gauss(0, rel))))
        elif k == "aggression":
            out[k] = max(0.1, v * (1 + rng.gauss(0, rel)))
        else:
            out[k] = v
    return out


def tight_aggressive(rng: random.Random | None = None) -> PlayerStyle:
    """紧凶(TAG):公牛 Toar。VPIP≈24%,PFR≈18%。"""
    return PlayerStyle(**_jitter(dict(
        vpip=0.24, pfr=0.18, threebet=0.07, aggression=2.2,
        cbet_freq=0.65, bluff_freq=0.35, fold_to_raise=0.55, donk_freq=0.05,
        sizing_flop=(0.5, 0.66), sizing_turn=(0.66, 0.75), sizing_river=(0.66, 1.0),
    ), rng))


def loose_aggressive(rng: random.Random | None = None) -> PlayerStyle:
    """松凶(LAG):狐狸 Foxy。VPIP≈38%,PFR≈30%。"""
    return PlayerStyle(**_jitter(dict(
        vpip=0.38, pfr=0.30, threebet=0.12, aggression=2.8,
        cbet_freq=0.72, bluff_freq=0.50, fold_to_raise=0.45, donk_freq=0.12,
        sizing_flop=(0.33, 0.5, 0.75), sizing_turn=(0.5, 0.75, 1.0),
        sizing_river=(0.75, 1.0, 1.5),
    ), rng))


def tight_passive(rng: random.Random | None = None) -> PlayerStyle:
    """紧弱(岩石):犀牛 Gerk。VPIP≈18%,PFR≈8%。"""
    return PlayerStyle(**_jitter(dict(
        vpip=0.18, pfr=0.08, threebet=0.03, aggression=0.8,
        cbet_freq=0.40, bluff_freq=0.15, fold_to_raise=0.60, donk_freq=0.03,
        sizing_flop=(0.5,), sizing_turn=(0.5,), sizing_river=(0.5,),
    ), rng))


def loose_passive(rng: random.Random | None = None) -> PlayerStyle:
    """松弱(跟注站):屠夫猪 Bristle。VPIP≈42%,PFR≈8%。"""
    return PlayerStyle(**_jitter(dict(
        vpip=0.42, pfr=0.08, threebet=0.02, aggression=0.6,
        cbet_freq=0.35, bluff_freq=0.20, fold_to_raise=0.35, donk_freq=0.08,
        sizing_flop=(0.33, 0.5), sizing_turn=(0.33, 0.5), sizing_river=(0.5,),
    ), rng))


def balanced(rng: random.Random | None = None) -> PlayerStyle:
    """均衡:看门狗 Scubby。VPIP≈25%,PFR≈20%。"""
    return PlayerStyle(**_jitter(dict(
        vpip=0.25, pfr=0.20, threebet=0.08, aggression=1.8,
        cbet_freq=0.60, bluff_freq=0.35, fold_to_raise=0.50, donk_freq=0.05,
        sizing_flop=(0.33, 0.5, 0.66), sizing_turn=(0.5, 0.66, 0.75),
        sizing_river=(0.5, 0.75, 1.0),
    ), rng))


def maniac(rng: random.Random | None = None) -> PlayerStyle:
    """疯狗(MANIAC):极宽入池、频繁再加注与超池施压。"""
    return PlayerStyle(**_jitter(dict(
        vpip=0.58, pfr=0.46, threebet=0.21, aggression=3.4,
        cbet_freq=0.80, bluff_freq=0.64, fold_to_raise=0.30, donk_freq=0.22,
        sizing_flop=(0.5, 0.75, 1.0), sizing_turn=(0.75, 1.0, 1.25),
        sizing_river=(0.75, 1.0, 1.5),
    ), rng))


def small_ball(rng: random.Random | None = None) -> PlayerStyle:
    """小球(SMALL):较宽范围配合小尺度下注，靠位置累积小底池。"""
    return PlayerStyle(**_jitter(dict(
        vpip=0.32, pfr=0.25, threebet=0.10, aggression=1.9,
        cbet_freq=0.73, bluff_freq=0.42, fold_to_raise=0.52, donk_freq=0.10,
        sizing_flop=(0.25, 0.33, 0.5), sizing_turn=(0.33, 0.5, 0.66),
        sizing_river=(0.5, 0.66, 0.75),
    ), rng))


def mixed_baseline(rng: random.Random | None = None) -> PlayerStyle:
    """混合(MIX)的综合参数；真正混合由机器人按整手选择分量。"""
    return PlayerStyle(**_jitter(dict(
        vpip=0.31, pfr=0.24, threebet=0.10, aggression=2.1,
        cbet_freq=0.67, bluff_freq=0.42, fold_to_raise=0.49, donk_freq=0.09,
        sizing_flop=(0.25, 0.33, 0.5, 0.66, 0.75),
        sizing_turn=(0.33, 0.5, 0.66, 0.75, 1.0),
        sizing_river=(0.5, 0.66, 0.75, 1.0, 1.5),
    ), rng))


# 稳定顺序既是 UI 默认展示顺序，也是轻量存档使用的公共契约。
STYLE_KEYS = ("TAG", "LAG", "ROCK", "CALLER", "BAL", "MANIAC", "SMALL", "MIX")

_STYLE_META = {
    "TAG": (
        "紧凶 TAG", "精选起手牌，入池后主动争取价值，失误率低。",
        tight_aggressive, "shark", (), (),
    ),
    "LAG": (
        "松凶 LAG", "用更宽范围持续施压，强势但波动和诈唬都更大。",
        loose_aggressive, "shark", (), (),
    ),
    "ROCK": (
        "岩石 ROCK", "等待强牌再投入，容易被偷盲，却很少付出大代价。",
        tight_passive, "reg", (), (),
    ),
    "CALLER": (
        "跟注站 CALLER", "爱看下一张牌、很难被吓退，但主动进攻较少。",
        loose_passive, "fish", (), (),
    ),
    "BAL": (
        "均衡 BAL", "范围与尺度较均衡，会在价值下注和诈唬间切换。",
        balanced, "shark", (), (),
    ),
    "MANIAC": (
        "疯狗 MANIAC", "极宽入池并频繁大尺度施压，危险也容易自爆。",
        maniac, "reg", (), (),
    ),
    "SMALL": (
        "小球 SMALL", "靠位置和小尺度频繁争夺小底池，避免无谓豪赌。",
        small_ball, "reg", (), (),
    ),
    "MIX": (
        "混合 MIX", "每手锁定一种子打法，下手再切换，难以被固定读牌。",
        mixed_baseline, "shark", ("TAG", "LAG", "SMALL"), (0.40, 0.35, 0.25),
    ),
}


def style_catalog(rng: random.Random | None = None) -> tuple[StylePreset, ...]:
    """按稳定顺序返回八类打法；可传 RNG 生成可复现的个体抖动。"""
    presets: list[StylePreset] = []
    for key in STYLE_KEYS:
        label, description, factory, level, components, weights = _STYLE_META[key]
        presets.append(StylePreset(
            key=key,
            label=label,
            description=description,
            style=factory(rng),
            default_level=level,
            mix_components=components,
            mix_weights=weights,
        ))
    return tuple(presets)


def style_by_key(key: str, rng: random.Random | None = None) -> StylePreset:
    """按稳定 key 取得一类打法，不接受含糊的中文名匹配。"""
    normalized = key.strip().upper()
    if normalized not in _STYLE_META:
        raise KeyError(f"未知打法 {key!r}; 可选 {STYLE_KEYS}")
    label, description, factory, level, components, weights = _STYLE_META[normalized]
    return StylePreset(
        key=normalized,
        label=label,
        description=description,
        style=factory(rng),
        default_level=level,
        mix_components=components,
        mix_weights=weights,
    )
