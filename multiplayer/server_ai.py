"""朋友局服务端 AI 的轻量、安全适配层。

本模块只负责把稳定的动物身份/打法目录装配成服务端机器人，并在机器人
决策与权威引擎之间执行最后一道视角及动作校验。它刻意不知道房间、
WebSocket、token 或 pygame；房间核心须自行从权威牌桌取得
``snapshot(perspective=ai_seat)`` 后再调用 :func:`choose_server_ai_action`。

首版服务端 AI 只使用 :mod:`ai.bots` 的启发式/整手混合机器人，不加载
torch、NFSP 或任何 UI 资源。
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib

from ai.bots import Bot, build_style_bot
from ai.personas import persona_by_id, with_style
from ai.styles import STYLE_KEYS
from engine.state import Action, ActionType, GameSnapshot, LegalActions, PlayerState

DEFAULT_SERVER_AI_TRIALS = 120
MAX_SERVER_AI_TRIALS = 400


class ServerAiStateError(RuntimeError):
    """当前快照无法由指定的服务端 AI 安全行动。"""


@dataclass(frozen=True, slots=True)
class ServerAiSpec:
    """大厅中一个稳定的 AI 座位配置。"""

    seat: int
    persona_id: str
    style_key: str


@dataclass(frozen=True, slots=True)
class ServerAiController:
    """一个已经完成装配、可跨整局复用的服务端 AI。

    ``bot`` 是有内部 RNG 状态的对象，房间应在配置 AI 时构建一次并缓存，
    不得在每次行动前重建，否则会破坏随机序列和 ``MIX`` 的整手一致性。
    """

    seat: int
    persona_id: str
    style_key: str
    display_name: str
    bot: Bot


_DEFAULT_SERVER_AI_SPECS: tuple[ServerAiSpec, ...] = (
    ServerAiSpec(1, "bull", "TAG"),
    ServerAiSpec(2, "fox", "LAG"),
    ServerAiSpec(3, "rhino", "ROCK"),
    ServerAiSpec(4, "boar", "CALLER"),
    ServerAiSpec(5, "dog", "BAL"),
    ServerAiSpec(6, "cat", "BAL"),
    ServerAiSpec(7, "raven", "MIX"),
    ServerAiSpec(8, "rabbit", "SMALL"),
)


def default_server_ai_specs() -> tuple[ServerAiSpec, ...]:
    """返回 9 人桌最多八个 AI 的稳定默认阵容。"""

    return _DEFAULT_SERVER_AI_SPECS


def build_server_ai_controller(
    *,
    room_id: str,
    seat: int,
    persona_id: str,
    style_key: str,
    big_blind: int,
    trials: int = DEFAULT_SERVER_AI_TRIALS,
) -> ServerAiController:
    """从稳定身份/打法 key 构建一个可复现的服务端 AI。

    种子使用 SHA-256 对房间、座位、身份和打法做域分离；禁止使用 Python
    进程级随机化的 ``hash()``。客户端不能指定 seed、level 或 trials，调用
    方应只把这里的固定 ``trials`` 上限作为服务端策略。
    """

    normalized_room = _validate_room_id(room_id)
    normalized_seat = _validate_seat(seat)
    normalized_persona = _normalize_persona_id(persona_id)
    normalized_style = _normalize_style_key(style_key)
    normalized_big_blind = _validate_positive_int(big_blind, "big_blind")
    normalized_trials = _validate_trials(trials)

    seed = _stable_seed(
        normalized_room,
        normalized_seat,
        normalized_persona,
        normalized_style,
    )
    identity = persona_by_id(normalized_persona, seed=seed)
    persona = with_style(identity, normalized_style, seed=seed)
    bot = build_style_bot(
        persona.style_key,
        style=persona.style,
        level=persona.level,
        seed=seed,
        big_blind=normalized_big_blind,
        trials=normalized_trials,
    )
    return ServerAiController(
        seat=normalized_seat,
        persona_id=persona.persona_id,
        style_key=persona.style_key,
        display_name=persona.display_name,
        bot=bot,
    )


def choose_server_ai_action(
    controller: ServerAiController,
    snapshot: GameSnapshot,
) -> Action:
    """让服务端 AI 对自己的安全视角快照做一次决策。

    集成错误（座位并非当前行动者、没有合法动作）会直接抛出
    :class:`ServerAiStateError`，避免替错误座位行动。若快照含私牌泄露、
    bot 自身异常或返回非法动作，则**不会重试随机决策**，而是返回确定性
    的保守合法动作：过牌 → 弃牌 → 跟注 → 最小下注/加注。
    """

    if not isinstance(controller, ServerAiController):
        raise TypeError("controller 须为 ServerAiController")
    if not isinstance(snapshot, GameSnapshot):
        raise TypeError("snapshot 须为 GameSnapshot")
    if snapshot.acting_seat != controller.seat:
        raise ServerAiStateError(
            f"AI seat {controller.seat} 不是当前行动者 {snapshot.acting_seat}"
        )
    legal = snapshot.legal_actions
    if legal is None:
        raise ServerAiStateError("当前快照没有合法动作")
    me = _player_for_seat(snapshot, controller.seat)

    # 先做视角门禁，再调用不受信的决策对象；泄露快照绝不传给 bot。
    if not _is_safe_perspective(snapshot, controller.seat):
        return _deterministic_fallback(me, legal)

    try:
        action = controller.bot.decide(snapshot)
    except Exception:
        return _deterministic_fallback(me, legal)
    if not _is_strictly_legal_action(action, me, snapshot, legal):
        return _deterministic_fallback(me, legal)
    return action


def _is_safe_perspective(snapshot: GameSnapshot, seat: int) -> bool:
    own = [player for player in snapshot.players if player.seat == seat]
    if len(own) != 1 or own[0].hole_cards is None:
        return False
    return all(
        player.seat == seat or player.hole_cards is None
        for player in snapshot.players
    )


def _is_strictly_legal_action(
    action: object,
    me: PlayerState,
    snapshot: GameSnapshot,
    legal: LegalActions,
) -> bool:
    """按引擎金额口径严格复验 bot 动作，尤其校验 CALL 的增量。"""

    if not isinstance(action, Action):
        return False
    if action.seat != me.seat or action.seat != snapshot.acting_seat:
        return False
    if isinstance(action.amount, bool) or not isinstance(action.amount, int):
        return False

    kind = action.action_type
    if kind is ActionType.FOLD:
        return legal.can_fold and action.amount == 0
    if kind is ActionType.CHECK:
        return legal.can_check and action.amount == 0
    if kind is ActionType.CALL:
        return legal.can_call and action.amount == legal.call_amount
    if kind is ActionType.BET:
        return _amount_in_range(
            action.amount,
            legal.min_bet_to,
            legal.max_bet_to,
        )
    if kind is ActionType.RAISE:
        return _amount_in_range(
            action.amount,
            legal.min_raise_to,
            legal.max_raise_to,
        )
    if kind is ActionType.ALLIN:
        all_in_to = me.bet + me.stack
        if action.amount != all_in_to or me.stack <= 0:
            return False
        if _amount_in_range(all_in_to, legal.min_bet_to, legal.max_bet_to):
            return True
        if _amount_in_range(all_in_to, legal.min_raise_to, legal.max_raise_to):
            return True
        # 无加注权时只允许真正耗尽筹码的跟注，不能把普通跟注伪记为 ALLIN。
        return legal.can_call and legal.call_amount == me.stack
    return False


def _deterministic_fallback(me: PlayerState, legal: LegalActions) -> Action:
    """返回无需随机数的最保守合法动作。"""

    if legal.can_check:
        return Action(me.seat, ActionType.CHECK)
    if legal.can_fold:
        return Action(me.seat, ActionType.FOLD)
    if legal.can_call:
        return Action(me.seat, ActionType.CALL, legal.call_amount)
    if legal.min_bet_to is not None:
        return Action(me.seat, ActionType.BET, legal.min_bet_to)
    if legal.min_raise_to is not None:
        return Action(me.seat, ActionType.RAISE, legal.min_raise_to)
    raise ServerAiStateError("当前合法动作集合为空，无法执行 AI 兜底")


def _amount_in_range(amount: int, low: int | None, high: int | None) -> bool:
    return low is not None and high is not None and low <= amount <= high


def _player_for_seat(snapshot: GameSnapshot, seat: int) -> PlayerState:
    players = [player for player in snapshot.players if player.seat == seat]
    if len(players) != 1:
        raise ServerAiStateError(f"快照中 seat {seat} 不存在或重复")
    return players[0]


def _stable_seed(room_id: str, seat: int, persona_id: str, style_key: str) -> int:
    payload = "\0".join(
        ("tavern-server-ai-v1", room_id, str(seat), persona_id, style_key)
    ).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def _validate_room_id(room_id: object) -> str:
    if not isinstance(room_id, str) or not room_id.strip():
        raise ValueError("room_id 须为非空字符串")
    return room_id.strip()


def _validate_seat(seat: object) -> int:
    if isinstance(seat, bool) or not isinstance(seat, int) or not 0 <= seat <= 8:
        raise ValueError("seat 须为 0-8 的整数")
    return seat


def _normalize_persona_id(persona_id: object) -> str:
    if not isinstance(persona_id, str) or not persona_id.strip():
        raise ValueError("persona_id 须为非空字符串")
    return persona_id.strip().lower()


def _normalize_style_key(style_key: object) -> str:
    if not isinstance(style_key, str) or not style_key.strip():
        raise ValueError("style_key 须为非空字符串")
    normalized = style_key.strip().upper()
    if normalized not in STYLE_KEYS:
        raise KeyError(f"未知打法 {style_key!r}; 可选 {STYLE_KEYS}")
    return normalized


def _validate_positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} 须为正整数")
    return value


def _validate_trials(trials: object) -> int:
    value = _validate_positive_int(trials, "trials")
    if value > MAX_SERVER_AI_TRIALS:
        raise ValueError(f"trials 不得超过 {MAX_SERVER_AI_TRIALS}")
    return value


__all__ = [
    "DEFAULT_SERVER_AI_TRIALS",
    "MAX_SERVER_AI_TRIALS",
    "ServerAiController",
    "ServerAiSpec",
    "ServerAiStateError",
    "build_server_ai_controller",
    "choose_server_ai_action",
    "default_server_ai_specs",
]
