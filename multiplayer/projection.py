"""联机牌桌状态的安全投影。

本模块是引擎与未来网络传输之间的唯一安全边界：每次投影都先
已入座者请求 ``Table.snapshot(perspective=viewer_seat)``，未入座者请求
``Table.public_snapshot()``，再逐字段构造可 JSON 序列化状态。禁止先
取得或序列化全知对象再删敏感字段。
"""
from __future__ import annotations

from engine.game import HandResult, Table
from engine.state import (
    ActionType,
    GameSnapshot,
    LegalActions,
    PlayerState,
    Position,
    PotInfo,
    Street,
)

PROJECTION_SCHEMA = "tavern.table-state.v1"

_STREET_WIRE = {
    Street.PREFLOP: "PREFLOP",
    Street.FLOP: "FLOP",
    Street.TURN: "TURN",
    Street.RIVER: "RIVER",
    Street.SHOWDOWN: "SHOWDOWN",
    Street.HAND_OVER: "HAND_OVER",
}

_POSITION_WIRE = {
    Position.BTN: "BTN",
    Position.SB: "SB",
    Position.BB: "BB",
    Position.UTG: "UTG",
    Position.UTG1: "UTG1",
    Position.UTG2: "UTG2",
    Position.LJ: "LJ",
    Position.HJ: "HJ",
    Position.MP: "MP",
    Position.CO: "CO",
}

_ACTION_WIRE = {
    ActionType.FOLD: "FOLD",
    ActionType.CHECK: "CHECK",
    ActionType.CALL: "CALL",
    ActionType.BET: "BET",
    ActionType.RAISE: "RAISE",
    ActionType.ALLIN: "ALLIN",
}


def project_table_state(
    table: Table,
    *,
    viewer_seat: int | None,
    room: str,
    state_version: int,
) -> dict[str, object]:
    """为一个座位生成可发送的牌桌状态。

    ``legal_actions`` 只在已入座观众正好是当前行动者时出现。
    座位牌只使用 ``cards`` 输出，且仅当视角快照已经允许看见
    时才添加该字段；隐藏牌不使用占位值，以免误传内部契约。
    """
    if not isinstance(room, str) or not room.strip():
        raise ValueError("room 不能为空")
    if isinstance(state_version, bool) or not isinstance(state_version, int):
        raise ValueError("state_version 须为非负整数")
    if state_version < 0:
        raise ValueError("state_version 须为非负整数")
    if viewer_seat is not None and (
        isinstance(viewer_seat, bool) or not isinstance(viewer_seat, int)
    ):
        raise ValueError("viewer_seat 须为整数或 None")

    # 安全性核心：未入座者走引擎专用公共视角，绝不请求全知 snapshot
    # 后删除私牌；已入座者仍只请求其认证座位的 perspective。
    snapshot = (
        table.public_snapshot()
        if viewer_seat is None
        else table.snapshot(perspective=viewer_seat)
    )
    if viewer_seat is not None and not 0 <= viewer_seat < len(snapshot.players):
        raise ValueError(f"座位不存在: {viewer_seat}")

    straddle_amount = table.current_straddle_amount
    straddler_seat = next(
        (
            player.seat
            for player in snapshot.players
            if straddle_amount > 0 and player.position is Position.UTG
        ),
        None,
    )
    result = table.last_hand_result
    projected_result = (
        _project_result(result)
        if table.hand_over
        and result is not None
        and result.hand_id == snapshot.hand_id
        else None
    )

    payload: dict[str, object] = {
        "schema": PROJECTION_SCHEMA,
        "room": room.strip(),
        "state_version": state_version,
        "viewer_seat": viewer_seat,
        "hand_id": snapshot.hand_id,
        "street": _STREET_WIRE[snapshot.street],
        "board": list(snapshot.board),
        "button_seat": snapshot.button_seat,
        "acting_seat": snapshot.acting_seat,
        "pots": [_project_pot(pot) for pot in snapshot.pots],
        "seats": [_project_seat(player) for player in snapshot.players],
        "straddle": {
            "amount": straddle_amount,
            "seat": straddler_seat,
        },
        "shown": sorted(table.shown_seats),
        "result": projected_result,
    }
    if (
        viewer_seat is not None
        and viewer_seat == snapshot.acting_seat
        and snapshot.legal_actions is not None
    ):
        payload["legal_actions"] = _project_legal_actions(snapshot.legal_actions)
    return payload


def _project_seat(player: PlayerState) -> dict[str, object]:
    """逐字段投影座位；不可见的底牌字段完全省略。"""
    payload: dict[str, object] = {
        "seat": player.seat,
        "name": player.name,
        "stack": player.stack,
        "bet": player.bet,
        "contribution": player.contribution,
        "folded": player.folded,
        "all_in": player.all_in,
        "is_acting": player.is_acting,
        "in_hand": player.position is not None,
        "position": (
            _POSITION_WIRE[player.position]
            if player.position is not None
            else None
        ),
    }
    if player.hole_cards is not None:
        payload["cards"] = list(player.hole_cards)
    return payload


def _project_pot(pot: PotInfo) -> dict[str, object]:
    return {
        "amount": pot.amount,
        "eligible_seats": list(pot.eligible_seats),
    }


def _project_legal_actions(legal: LegalActions) -> dict[str, object]:
    available: list[str] = []
    if legal.can_fold:
        available.append(_ACTION_WIRE[ActionType.FOLD])
    if legal.can_check:
        available.append(_ACTION_WIRE[ActionType.CHECK])
    if legal.can_call:
        available.append(_ACTION_WIRE[ActionType.CALL])
    if legal.min_bet_to is not None:
        available.append(_ACTION_WIRE[ActionType.BET])
    if legal.min_raise_to is not None:
        available.append(_ACTION_WIRE[ActionType.RAISE])
    if (
        legal.min_bet_to is not None
        or legal.min_raise_to is not None
        or legal.is_all_in_only
    ):
        available.append(_ACTION_WIRE[ActionType.ALLIN])
    return {
        "available": available,
        "can_fold": legal.can_fold,
        "can_check": legal.can_check,
        "can_call": legal.can_call,
        "call_amount": legal.call_amount,
        "min_bet_to": legal.min_bet_to,
        "max_bet_to": legal.max_bet_to,
        "min_raise_to": legal.min_raise_to,
        "max_raise_to": legal.max_raise_to,
        "is_all_in_only": legal.is_all_in_only,
    }


def _project_result(result: HandResult) -> dict[str, object]:
    return {
        "hand_id": result.hand_id,
        "showdown": result.showdown,
        "board": list(result.board),
        "pots": [_project_pot(pot) for pot in result.pots],
        "pot_awards": [
            {
                "pot_index": award.pot_index,
                "amount": award.pot.amount,
                "eligible_seats": list(award.pot.eligible_seats),
                "payouts": [
                    {"seat": seat, "amount": amount}
                    for seat, amount in award.payouts
                ],
            }
            for award in result.pot_awards
        ],
        "winners": [
            {"seat": seat, "amount": amount}
            for seat, amount in sorted(result.winners.items())
        ],
    }


__all__ = ["PROJECTION_SCHEMA", "project_table_state"]
