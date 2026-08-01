"""NFSP HU 平均策略的轻量推理运行时。

本模块只依赖 Python 标准库与项目自身的 ``engine.state`` 数据契约，
不导入 torch、numpy 或 rlcard。训练 checkpoint 先由
``tools/export_nfsp_runtime.py`` 导出为固定二进制格式；运行时只加载
折叠 BatchNorm 后的三层全连接网络::

    54 → 512 (ReLU) → 512 (ReLU) → 5 (softmax)

模型仅用于实验性 HU 翻后决策。这里的 HU 指当前仍未弃牌且拥有位置标签的
竞争者恰好两人；多人牌局中已经弃牌者投入的死钱仍计入底池与下注尺度。
"""
from __future__ import annotations

from array import array
import hashlib
import math
from pathlib import Path
import random
import struct
import sys
from typing import Sequence

from engine.state import (
    Action,
    ActionType,
    GameSnapshot,
    LegalActions,
    PlayerState,
    Street,
)

OBS_DIM = 54
NUM_ACTIONS = 5

# rlcard 动作 id 与训练时保持完全一致。
RL_ACTION_NAMES = (
    "FOLD",
    "CHECK_CALL",
    "RAISE_HALF_POT",
    "RAISE_POT",
    "ALL_IN",
)
RL_FOLD, RL_CHECK_CALL, RL_RAISE_HALF_POT, RL_RAISE_POT, RL_ALL_IN = range(
    NUM_ACTIONS
)

# 固定模型文件格式。所有整数与 float32 payload 均为 little-endian。
MODEL_MAGIC = b"THNFSP\0\0"
MODEL_VERSION = 1
MODEL_SHAPE = (OBS_DIM, 512, 512, NUM_ACTIONS)
MODEL_HEADER_STRUCT = struct.Struct("<8sIQ4IQ32s")
MODEL_PAYLOAD_FLOATS = (
    MODEL_SHAPE[0] * MODEL_SHAPE[1]
    + MODEL_SHAPE[1]
    + MODEL_SHAPE[1] * MODEL_SHAPE[2]
    + MODEL_SHAPE[2]
    + MODEL_SHAPE[2] * MODEL_SHAPE[3]
    + MODEL_SHAPE[3]
)
MODEL_PAYLOAD_BYTES = MODEL_PAYLOAD_FLOATS * 4

# 版本化资源名把发行包精确钉在这次 1,244,484 局的训练节点；继续训练后
# 须重新导出并显式更新文件名，避免无意间把可变 checkpoint 混进发行版。
DEFAULT_MODEL_ASSET = ("assets", "models", "nfsp_hu_1244484.bin")

_SUIT_BASE = {"s": 0, "h": 13, "d": 26, "c": 39}
_RANK_OFFSET = {rank: i for i, rank in enumerate("A23456789TJQK")}


def default_model_path() -> Path:
    """返回源码/PyInstaller 双模式下的默认模型资源路径。"""
    # 延迟导入，避免 AI 逻辑层在模块加载时接触 pygame；respath 本身也不
    # 导入 pygame，只负责统一处理开发目录与 sys._MEIPASS。
    from ui.respath import res_path

    return res_path(*DEFAULT_MODEL_ASSET)


def card_index(card: str) -> int:
    """把引擎牌面（如 ``"Kh"``）转换为 rlcard 的 0..51 索引。"""
    if len(card) != 2:
        raise ValueError(f"非法牌面: {card!r}")
    rank, suit = card[0], card[1]
    if rank not in _RANK_OFFSET or suit not in _SUIT_BASE:
        raise ValueError(f"非法牌面: {card!r}")
    return _SUIT_BASE[suit] + _RANK_OFFSET[rank]


def _contenders(snapshot: GameSnapshot) -> list[PlayerState]:
    """返回本手仍有资格争夺底池的玩家（不含已弃牌者）。"""
    return [
        player
        for player in snapshot.players
        if player.position is not None and not player.folded
    ]


def _player(snapshot: GameSnapshot, seat: int) -> PlayerState:
    """按座位号取得玩家，找不到时给出明确错误。"""
    for player in snapshot.players:
        if player.seat == seat:
            return player
    raise ValueError(f"快照中不存在座位 {seat}")


def is_experimental_hu_postflop(snapshot: GameSnapshot) -> bool:
    """当前快照是否适合调用实验性 HU 翻后策略网。

    六人手牌在其余玩家都弃牌后，也可形成仅剩两名竞争者的 HU 子局面；
    但只有仍有合法行动者的 flop/turn/river 决策点才返回 ``True``。
    """
    if snapshot.street not in (Street.FLOP, Street.TURN, Street.RIVER):
        return False
    if snapshot.acting_seat is None or snapshot.legal_actions is None:
        return False
    contenders = _contenders(snapshot)
    return (
        len(contenders) == 2
        and snapshot.acting_seat in {player.seat for player in contenders}
    )


def encode_observation(
    snapshot: GameSnapshot, hero_seat: int
) -> tuple[float, ...]:
    """把 hero 视角快照编码为训练时使用的 54 维观测。

    竞争者按 ``position is not None and not folded`` 判定，因此多人手牌
    收缩到两名未弃牌者后也可编码。已弃牌者的累计投入不写入双方投入
    标量，但 :func:`pot_now` 仍会保留其死钱。调用方必须提供能看到 hero
    底牌的视角快照。
    """
    contenders = _contenders(snapshot)
    if len(contenders) != 2:
        raise ValueError(f"观测编码仅支持两名竞争者,当前 {len(contenders)} 人")
    hero = _player(snapshot, hero_seat)
    if hero not in contenders:
        raise ValueError(f"座位 {hero_seat} 不是当前竞争者")
    if hero.hole_cards is None:
        raise ValueError(f"座位 {hero_seat} 的底牌在当前视角不可见")

    observation = [0.0] * OBS_DIM
    for card in (*snapshot.board, *hero.hole_cards):
        observation[card_index(card)] = 1.0
    observation[52] = float(hero.contribution)
    observation[53] = float(
        max(player.contribution for player in contenders)
    )
    return tuple(observation)


def pot_now(snapshot: GameSnapshot) -> int:
    """返回含本街全部下注及已弃牌死钱的当前底池。"""
    return snapshot.total_pot + sum(player.bet for player in snapshot.players)


def legal_rlcard_ids(snapshot: GameSnapshot, hero_seat: int) -> list[int]:
    """把引擎合法动作约束映射到 NFSP 的五档抽象动作空间。"""
    legal = snapshot.legal_actions
    if legal is None:
        raise ValueError("当前无人行动,无合法动作集")
    hero = _player(snapshot, hero_seat)
    pot = pot_now(snapshot)
    remained = hero.stack
    diff = legal.call_amount if legal.can_call else 0
    action_ids = [
        RL_FOLD,
        RL_CHECK_CALL,
        RL_RAISE_HALF_POT,
        RL_RAISE_POT,
        RL_ALL_IN,
    ]

    if diff > 0 and diff >= remained:
        action_ids = [RL_FOLD, RL_CHECK_CALL]
    else:
        if pot > remained:
            action_ids.remove(RL_RAISE_POT)
        if int(pot / 2) > remained:
            action_ids.remove(RL_RAISE_HALF_POT)
        max_bet = max((player.bet for player in snapshot.players), default=0)
        if (
            RL_RAISE_HALF_POT in action_ids
            and int(pot / 2) + hero.bet <= max_bet
        ):
            action_ids.remove(RL_RAISE_HALF_POT)

    engine_can_wager = (
        legal.min_bet_to is not None or legal.min_raise_to is not None
    )
    if not engine_can_wager:
        action_ids = [
            action_id
            for action_id in action_ids
            if action_id in (RL_FOLD, RL_CHECK_CALL)
        ]
    if not legal.can_fold and RL_FOLD in action_ids:
        action_ids.remove(RL_FOLD)
    return sorted(action_ids)


def _fallback(seat: int, legal: LegalActions) -> Action:
    """返回最保守的合法动作。"""
    if legal.can_check:
        return Action(seat, ActionType.CHECK)
    if legal.can_call:
        return Action(seat, ActionType.CALL, legal.call_amount)
    return Action(seat, ActionType.FOLD)


def _clamp_wager(
    hero: PlayerState, legal: LegalActions, desired_to: int
) -> Action | None:
    """把抽象下注额度夹取到引擎的合法“加注到”区间。"""
    if legal.min_raise_to is not None:
        action_type = ActionType.RAISE
        lower, upper = legal.min_raise_to, legal.max_raise_to
    elif legal.min_bet_to is not None:
        action_type = ActionType.BET
        lower, upper = legal.min_bet_to, legal.max_bet_to
    else:
        return None
    if upper is None:
        return None
    return Action(
        hero.seat,
        action_type,
        max(lower, min(upper, desired_to)),
    )


def rlcard_to_engine(
    action_id: int, snapshot: GameSnapshot, hero_seat: int
) -> Action:
    """把 NFSP 抽象动作映射为恒合法的引擎动作。"""
    if not 0 <= action_id < NUM_ACTIONS:
        raise ValueError(f"未知 NFSP 动作 id: {action_id}")
    legal = snapshot.legal_actions
    if legal is None:
        raise ValueError("当前无人行动,无法映射动作")
    hero = _player(snapshot, hero_seat)
    pot = pot_now(snapshot)

    if action_id == RL_FOLD:
        if legal.can_fold:
            return Action(hero_seat, ActionType.FOLD)
        return _fallback(hero_seat, legal)
    if action_id == RL_CHECK_CALL:
        if legal.can_call and legal.call_amount > 0:
            return Action(hero_seat, ActionType.CALL, legal.call_amount)
        if legal.can_check:
            return Action(hero_seat, ActionType.CHECK)
        return _fallback(hero_seat, legal)
    if action_id == RL_ALL_IN:
        if legal.min_raise_to is not None or legal.min_bet_to is not None:
            return Action(
                hero_seat,
                ActionType.ALLIN,
                hero.stack + hero.bet,
            )
        return _fallback(hero_seat, legal)

    added = int(pot / 2) if action_id == RL_RAISE_HALF_POT else pot
    action = _clamp_wager(hero, legal, hero.bet + added)
    return action if action is not None else _fallback(hero_seat, legal)


def _linear(
    inputs: Sequence[float],
    weights: Sequence[Sequence[float]],
    biases: Sequence[float],
    *,
    relu: bool,
) -> list[float]:
    """执行一个行主序全连接层，可选 ReLU。"""
    outputs: list[float] = []
    for row, bias in zip(weights, biases):
        value = sum(weight * item for weight, item in zip(row, inputs)) + bias
        outputs.append(max(0.0, value) if relu else value)
    return outputs


class LitePolicyNet:
    """加载并执行导出的 NFSP HU 平均策略。

    :param model_path: 轻量二进制模型；``None`` 使用
        ``assets/models/nfsp_hu_1244484.bin``。
    """

    def __init__(self, model_path: str | Path | None = None) -> None:
        self.path = Path(model_path) if model_path is not None else default_model_path()
        (
            self.episode,
            self.shape,
            self.payload_sha256,
            values,
        ) = self._load(self.path)
        self._unpack_weights(values)
        self.loaded = True
        self.meta = {
            "episode": self.episode,
            "path": str(self.path),
            "format": f"nfsp-lite-v{MODEL_VERSION}",
            "sha256": self.payload_sha256,
        }

    @staticmethod
    def _load(
        path: Path,
    ) -> tuple[int, tuple[int, int, int, int], str, list[float]]:
        """读取、校验 header、长度及 payload SHA-256。"""
        try:
            with path.open("rb") as stream:
                header = stream.read(MODEL_HEADER_STRUCT.size)
                if len(header) != MODEL_HEADER_STRUCT.size:
                    raise ValueError("模型文件头不完整")
                (
                    magic,
                    version,
                    episode,
                    input_dim,
                    hidden_1,
                    hidden_2,
                    output_dim,
                    payload_size,
                    expected_digest,
                ) = MODEL_HEADER_STRUCT.unpack(header)
                shape = (input_dim, hidden_1, hidden_2, output_dim)
                if magic != MODEL_MAGIC:
                    raise ValueError("模型 magic 不匹配")
                if version != MODEL_VERSION:
                    raise ValueError(
                        f"不支持的模型版本 {version},期望 {MODEL_VERSION}"
                    )
                if shape != MODEL_SHAPE:
                    raise ValueError(
                        f"模型结构不匹配 {shape},期望 {MODEL_SHAPE}"
                    )
                if payload_size != MODEL_PAYLOAD_BYTES:
                    raise ValueError(
                        f"payload 长度 {payload_size} 不符合结构"
                    )
                payload = stream.read(payload_size)
                if len(payload) != payload_size:
                    raise ValueError("模型 payload 不完整")
                if stream.read(1):
                    raise ValueError("模型文件尾含有未声明数据")
        except OSError as exc:
            raise FileNotFoundError(f"无法读取 NFSP 模型: {path}") from exc

        actual_digest = hashlib.sha256(payload).digest()
        if actual_digest != expected_digest:
            raise ValueError("模型 payload SHA-256 校验失败")
        values_array = array("f")
        values_array.frombytes(payload)
        if values_array.itemsize != 4:
            raise RuntimeError("当前平台 float array 不是 32 位")
        if sys.byteorder != "little":
            values_array.byteswap()
        if len(values_array) != MODEL_PAYLOAD_FLOATS:
            raise ValueError("模型 float32 数量与结构不一致")
        return int(episode), shape, actual_digest.hex(), values_array.tolist()

    def _unpack_weights(self, values: list[float]) -> None:
        """按固定 shape 将扁平 payload 拆成三层权重与偏置。"""
        offset = 0

        def take_matrix(rows: int, columns: int) -> list[list[float]]:
            nonlocal offset
            end = offset + rows * columns
            flat = values[offset:end]
            offset = end
            return [
                flat[row * columns : (row + 1) * columns]
                for row in range(rows)
            ]

        def take_vector(length: int) -> list[float]:
            nonlocal offset
            end = offset + length
            vector = values[offset:end]
            offset = end
            return vector

        input_dim, hidden_1, hidden_2, output_dim = self.shape
        self._weight_1 = take_matrix(hidden_1, input_dim)
        self._bias_1 = take_vector(hidden_1)
        self._weight_2 = take_matrix(hidden_2, hidden_1)
        self._bias_2 = take_vector(hidden_2)
        self._weight_3 = take_matrix(output_dim, hidden_2)
        self._bias_3 = take_vector(output_dim)
        if offset != len(values):
            raise ValueError("模型 payload 拆分后仍有剩余数据")

    def predict_observation(
        self, observation: Sequence[float]
    ) -> tuple[float, ...]:
        """对一个 54 维观测返回未做合法动作遮罩的五档概率。"""
        if len(observation) != self.shape[0]:
            raise ValueError(
                f"观测维度 {len(observation)} 不匹配模型 {self.shape[0]}"
            )
        hidden_1 = _linear(
            observation, self._weight_1, self._bias_1, relu=True
        )
        hidden_2 = _linear(
            hidden_1, self._weight_2, self._bias_2, relu=True
        )
        logits = _linear(
            hidden_2, self._weight_3, self._bias_3, relu=False
        )
        maximum = max(logits)
        exponentials = [math.exp(value - maximum) for value in logits]
        total = sum(exponentials)
        if total <= 0.0 or not math.isfinite(total):
            raise ValueError("模型输出无法归一化")
        return tuple(value / total for value in exponentials)

    def action_distribution(
        self, snapshot: GameSnapshot, hero_seat: int
    ) -> dict[str, float]:
        """返回合法动作子集上归一化后的 NFSP 平均策略分布。"""
        probabilities = self.predict_observation(
            encode_observation(snapshot, hero_seat)
        )
        legal_ids = legal_rlcard_ids(snapshot, hero_seat)
        if not legal_ids:
            raise ValueError("当前没有可映射的 NFSP 合法动作")
        masked = {
            action_id: probabilities[action_id] for action_id in legal_ids
        }
        total = sum(masked.values())
        if total <= 0.0:
            uniform = 1.0 / len(masked)
            return {
                RL_ACTION_NAMES[action_id]: uniform for action_id in masked
            }
        return {
            RL_ACTION_NAMES[action_id]: probability / total
            for action_id, probability in masked.items()
        }

    def sample_action(
        self,
        snapshot: GameSnapshot,
        hero_seat: int,
        rng: random.Random | None = None,
    ) -> Action:
        """按合法动作频率采样，并转换为引擎合法动作。"""
        distribution = self.action_distribution(snapshot, hero_seat)
        draw = rng.random() if rng is not None else random.random()
        cumulative = 0.0
        picked = next(reversed(distribution))
        for name, probability in distribution.items():
            cumulative += probability
            if draw <= cumulative:
                picked = name
                break
        return rlcard_to_engine(
            RL_ACTION_NAMES.index(picked), snapshot, hero_seat
        )
