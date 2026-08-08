"""朋友联机 v2 的服务端权威房间核心。

房间成员、房主权限与物理座位彼此独立。创建和加入只签发成员凭据，
成员必须显式选择空位后才能准备；所有私人牌仍只经认证座位视角投影。
本模块不负责 WebSocket、计时或锁，上层 actor 必须串行调用。
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from uuid import uuid4

from ai.hand_rank import describe_holdem_hand
from ai.personas import persona_catalog
from engine.game import (
    MAX_BUYIN_BB,
    MIN_BUYIN_BB,
    IllegalActionError,
    Table,
    TableConfig,
)
from engine.state import Action, ActionType, GameSnapshot

from .auth import generate_resume_token, generate_room_code
from .projection import project_table_state
from .protocol import (
    MAX_WIRE_INTEGER,
    PROTOCOL_VERSION,
    ClientEnvelope,
    ProtocolError,
    encode_server_message,
    parse_action_intent,
)
from .server_ai import (
    ServerAiController,
    build_server_ai_controller,
    choose_server_ai_action,
    default_server_ai_specs,
)

ROOM_STATE_SCHEMA = "tavern.room-state.v2"
DEFAULT_REQUEST_CACHE_SIZE = 128
MAX_RETIRED_TOKEN_TOMBSTONES = 128
MAX_DISPLAY_NAME_CHARS = 32
MAX_PUBLIC_HAND_SUMMARIES = 20
LOW_STACK_PROMPT_BB = 100
SELF_TOP_UP_LIMIT_BB = 400


class RoomPhase(str, Enum):
    """房间生命周期。"""

    LOBBY = "LOBBY"
    PLAYING = "PLAYING"
    BETWEEN_HANDS = "BETWEEN_HANDS"
    CLOSED = "CLOSED"


class RoomError(RuntimeError):
    """可安全映射为稳定服务端错误码的房间错误。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class RoomConfig:
    """朋友房间的固定容量与默认牌桌配置。"""

    player_count: int
    small_blind: int
    big_blind: int
    buyin: int

    def __post_init__(self) -> None:
        if not _is_int(self.player_count) or not 2 <= self.player_count <= 9:
            raise ValueError("player_count 须为 2-9 的整数")
        if not _is_int(self.small_blind) or not _is_int(self.big_blind):
            raise ValueError("盲注须为整数筹码")
        if self.small_blind <= 0 or self.big_blind <= self.small_blind:
            raise ValueError("盲注须满足 0 < small_blind < big_blind")
        if not _is_int(self.buyin):
            raise ValueError("buyin 须为整数筹码")
        if any(
            value > MAX_WIRE_INTEGER
            for value in (self.small_blind, self.big_blind, self.buyin)
        ):
            raise ValueError("盲注与买入超过协议安全整数范围")
        low = MIN_BUYIN_BB * self.big_blind
        high = MAX_BUYIN_BB * self.big_blind
        if not low <= self.buyin <= high:
            raise ValueError(f"buyin 须在 {low}-{high} 筹码之间")
        if self.buyin * self.player_count > MAX_WIRE_INTEGER:
            raise ValueError("全桌初始筹码超过协议安全整数范围")


@dataclass(frozen=True)
class SeatCredential:
    """创建/加入后私发给成员的恢复凭据。

    ``seat`` 只是签发瞬间的座位快照；v2 创建/加入时固定为 ``None``，
    后续动态座位始终以 room.state 为准，token 永不与座位绑定。
    """

    room_id: str
    seat: int | None
    resume_token: str = field(repr=False)
    state_version: int = 0
    member_id: str = ""
    is_host: bool = False


@dataclass
class _Member:
    member_id: str
    display_name: str
    token: str = field(repr=False)
    is_host: bool = False
    seat: int | None = None
    ready: bool = False
    waiting_next_hand: bool = False
    active: bool = True
    responses: OrderedDict[str, tuple[str, dict[str, object]]] = field(
        default_factory=OrderedDict
    )


@dataclass
class _AiMember:
    """无 token/连接能力的服务器权威 AI 座位。"""

    seat: int
    display_name: str
    persona_id: str
    style_key: str
    controller: ServerAiController = field(repr=False)
    ready: bool = True
    waiting_next_hand: bool = False
    active: bool = True


_Occupant = _Member | _AiMember


@dataclass
class _TopUpRequest:
    member_id: str
    target_seat: int
    target_stack: int
    status: str  # APPROVED | PENDING_APPROVAL


@dataclass(frozen=True)
class _CommandResult:
    payload: dict[str, object]
    changed: bool = True


class RoomCore:
    """一张固定 2-9 个物理座位、成员可自由选座的权威牌桌。"""

    _SUPPORTED_COMMANDS = frozenset(
        {
            "room.ai.fill",
            "room.ai.clear",
            "room.ai.add",
            "room.ai.remove",
            "room.ai.rebuy",
            "room.ai.style",
            "room.ready",
            "room.start",
            "room.leave",
            "room.pause",
            "room.resume",
            "game.action",
            "game.show",
            "game.next_hand",
            "seat.claim",
            "seat.release",
            "seat.rebuy",
            "seat.leave",
            "seat.topup.request",
            "seat.topup.cancel",
            "seat.topup.decline",
            "seat.topup.approve",
            "seat.topup.reject",
        }
    )

    def __init__(
        self,
        config: RoomConfig,
        host_name: str,
        *,
        room_id: str | None = None,
        request_cache_size: int = DEFAULT_REQUEST_CACHE_SIZE,
        seed: int | None = None,
    ) -> None:
        if not isinstance(config, RoomConfig):
            raise TypeError("config 须为 RoomConfig")
        if not _is_int(request_cache_size) or request_cache_size <= 0:
            raise ValueError("request_cache_size 须为正整数")
        if seed is not None and not _is_int(seed):
            raise ValueError("seed 须为整数或 None")

        self._room_id = _validate_room_id(room_id or generate_room_code())
        self._config = config
        self._phase = RoomPhase.LOBBY
        self._state_version = 0
        self._request_cache_size = request_cache_size
        self._seed = seed
        self._table: Table | None = None
        self._members_by_seat: list[_Occupant | None] = [None] * config.player_count
        self._members_by_token: dict[str, _Member] = {}
        self._retired_by_token: OrderedDict[str, _Member] = OrderedDict()
        self._pending_buyins: dict[int, int] = {}
        self._top_up_requests: dict[str, _TopUpRequest] = {}
        self._ai_top_ups: dict[int, int] = {}
        self._low_stack_pending: set[str] = set()
        self._paused = False
        self._paused_by: str | None = None
        self._public_hand_summaries: list[dict[str, object]] = []
        self._last_summarized_hand_id = 0
        self._transition: dict[str, object] | None = None

        host = self._new_member(host_name, is_host=True)
        self._host_member_id = host.member_id
        self._host_credential = self._credential(host)

    # ---------------------------------------------------------- properties

    @property
    def room_id(self) -> str:
        return self._room_id

    @property
    def config(self) -> RoomConfig:
        return self._config

    @property
    def phase(self) -> RoomPhase:
        return self._phase

    @property
    def state_version(self) -> int:
        return self._state_version

    @property
    def table(self) -> Table | None:
        return self._table

    @property
    def host_credential(self) -> SeatCredential:
        return self._host_credential

    @property
    def occupied_seats(self) -> tuple[int, ...]:
        return tuple(
            seat
            for seat, occupant in enumerate(self._members_by_seat)
            if occupant is not None and occupant.active
        )

    @property
    def busted_pending(self) -> tuple[int, ...]:
        """仍须本人/房主处理的爆仓座位。"""
        table = self._table
        if table is None:
            return ()
        pending: list[int] = []
        for seat in table.busted_seats:
            occupant = self._members_by_seat[seat]
            if occupant is None or not occupant.active:
                continue
            if isinstance(occupant, _AiMember):
                if seat not in self._ai_top_ups:
                    pending.append(seat)
            else:
                request = self._top_up_requests.get(occupant.member_id)
                if request is None or request.status != "APPROVED":
                    pending.append(seat)
        return tuple(pending)

    @property
    def paused(self) -> bool:
        return self._paused

    # ---------------------------------------------------------- membership

    def join(self, display_name: str) -> SeatCredential:
        """加入为未入座成员；选择物理座位须另发 ``seat.claim``。"""
        if self._phase is RoomPhase.CLOSED:
            raise RoomError("ROOM_CLOSED", "房间已经关闭")
        # 房主不消耗玩家名额；最多允许 N 位非房主成员同时候选/旁观。
        non_host_count = sum(
            1
            for member in self._members_by_token.values()
            if member.active and not member.is_host
        )
        if non_host_count >= self._config.player_count:
            raise RoomError("ROOM_FULL", "房间候选成员人数已满")
        member = self._new_member(display_name, is_host=False)
        self._state_version += 1
        self._set_transition(
            "MEMBER_JOIN",
            member_id=member.member_id,
            display_name=member.display_name,
        )
        return self._credential(member)

    def principal_for_token(self, token: str) -> str:
        """返回与座位无关的稳定成员 ID，供 actor 管理重连连接。"""
        return self._member_for_token(token).member_id

    def seat_for_token(self, token: str) -> int | None:
        """返回成员当前座位；未入座/旁观时为 ``None``。"""
        return self._member_for_token(token).seat

    def projection_for_token(self, token: str) -> dict[str, object]:
        return self._project_member(self._member_for_token(token))

    def projection_for_seat(self, seat: int) -> dict[str, object]:
        """兼容可信调用方的座位投影；新 actor 应统一按 token 投影。"""
        target = _validate_target_seat(seat, self._config.player_count)
        occupant = self._members_by_seat[target]
        if not isinstance(occupant, _Member) or not occupant.active:
            raise RoomError("AUTH_FAILED", "座位不存在或不是在线真人")
        return self._project_member(occupant)

    def projection_for(self, token: str) -> dict[str, object]:
        return self.projection_for_token(token)

    # ---------------------------------------------------------- scheduling

    def next_automatic_transition(self) -> str | None:
        """返回 actor 可调度的下一种单步变化，不在此处执行计时。"""
        if self._paused or self._phase is RoomPhase.CLOSED:
            return None
        table = self._table
        if table is None:
            return None
        if self._phase is RoomPhase.PLAYING:
            acting = table.public_snapshot().acting_seat
            if acting is not None and isinstance(
                self._members_by_seat[acting], _AiMember
            ):
                return "AI_ACTION"
            return None
        if self._phase is not RoomPhase.BETWEEN_HANDS:
            return None
        if self.busted_pending or self._low_stack_pending:
            return None
        if any(
            request.status == "PENDING_APPROVAL"
            for request in self._top_up_requests.values()
        ):
            return None
        if self._projected_next_hand_count() < 2:
            return None
        return "NEXT_HAND"

    def step_server_ai(self) -> bool:
        """在 mailbox 内执行至多一个 AI 动作并推进一个版本。"""
        if self.next_automatic_transition() != "AI_ACTION":
            return False
        table = self._require_table()
        acting = table.public_snapshot().acting_seat
        assert acting is not None
        occupant = self._members_by_seat[acting]
        assert isinstance(occupant, _AiMember)
        before = table.snapshot(perspective=acting)
        street = before.street.name
        action = choose_server_ai_action(occupant.controller, before)
        paid = _action_paid_increment(before, action)
        table.apply(action)
        if not 0 <= paid <= before.players[acting].stack:
            raise AssertionError("已接受 AI 动作的 paid 增量越界")
        if table.hand_over:
            self._on_hand_finished()
        self._state_version += 1
        self._set_transition(
            "ACTION",
            hand_id=table.hand_id,
            street=street,
            target_seat=acting,
            action=action.action_type.name,
            amount=action.amount,
            paid=paid,
        )
        return True

    def step_scheduled_next_hand(self) -> bool:
        """应用已批准的下手变更并发牌，整体只推进一个房间版本。"""
        if self.next_automatic_transition() != "NEXT_HAND":
            return False
        detail = self._deal_next_hand()
        self._state_version += 1
        self._set_transition("HAND_STARTED", **detail)
        return True

    # ---------------------------------------------------------- lifecycle

    def close(self) -> bool:
        if self._phase is RoomPhase.CLOSED:
            return False
        self._phase = RoomPhase.CLOSED
        self._paused = False
        self._paused_by = None
        self._state_version += 1
        self._set_transition("ROOM_CLOSED")
        return True

    def handle(self, token: str, envelope: ClientEnvelope) -> dict[str, object]:
        """处理已解析房内命令，提供每成员 UUID 幂等与状态版本门禁。"""
        if not isinstance(envelope, ClientEnvelope):
            raise TypeError("envelope 须为 ClientEnvelope")

        member: _Member | None = None
        if isinstance(token, str):
            member = self._members_by_token.get(token)
            if member is None:
                member = self._retired_by_token.get(token)
        if member is None:
            return self._error_response(envelope, "AUTH_FAILED", "恢复 token 无效")
        try:
            cached = self._cached_response(member, envelope)
        except RoomError as exc:
            return self._error_response(envelope, exc.code, exc.message)
        if cached is not None:
            return cached
        if envelope.room_id != self._room_id:
            return self._finish_response(
                member,
                envelope,
                self._error_response(envelope, "ROOM_NOT_FOUND", "房间不匹配"),
            )
        if not member.active:
            return self._finish_response(
                member,
                envelope,
                self._error_response(envelope, "AUTH_FAILED", "成员已离开房间"),
            )
        if envelope.message_type not in self._SUPPORTED_COMMANDS:
            return self._finish_response(
                member,
                envelope,
                self._error_response(
                    envelope,
                    "UNSUPPORTED_COMMAND",
                    "该消息不由 RoomCore 处理",
                ),
            )
        if envelope.expected_state != self._state_version:
            return self._finish_response(
                member,
                envelope,
                self._error_response(
                    envelope,
                    "STALE_STATE",
                    "客户端状态版本已过期",
                    state=self._project_member(member),
                ),
            )
        if self._phase is RoomPhase.CLOSED:
            return self._finish_response(
                member,
                envelope,
                self._error_response(envelope, "ROOM_CLOSED", "房间已经关闭"),
            )
        if self._paused and envelope.message_type not in {
            "room.resume",
            "room.leave",
        }:
            return self._finish_response(
                member,
                envelope,
                self._error_response(envelope, "ROOM_PAUSED", "房主已暂停牌局"),
            )

        before = self._state_version
        try:
            result = self._dispatch(member, envelope)
        except (RoomError, ProtocolError) as exc:
            response = self._error_response(envelope, exc.code, exc.message)
        except IllegalActionError as exc:
            response = self._error_response(envelope, "ILLEGAL_ACTION", str(exc))
        else:
            if result.changed:
                self._state_version = before + 1
            response = self._success_response(
                envelope,
                result.payload,
                self._project_member(member),
            )
        if self._state_version not in {before, before + 1}:
            raise AssertionError("一次房间命令只能推进一个 state_version")
        return self._finish_response(member, envelope, response)

    # ---------------------------------------------------------- dispatch

    def _dispatch(self, member: _Member, envelope: ClientEnvelope) -> _CommandResult:
        kind = envelope.message_type
        body = envelope.body
        handlers = {
            "room.ai.fill": self._fill_ai,
            "room.ai.clear": self._clear_ai,
            "room.ai.add": self._add_ai,
            "room.ai.remove": self._remove_ai,
            "room.ai.rebuy": self._rebuy_ai,
            "room.ai.style": self._style_ai,
            "room.ready": self._set_ready,
            "room.start": self._start,
            "room.leave": self._leave_room,
            "room.pause": self._pause,
            "room.resume": self._resume,
            "game.action": self._apply_action,
            "game.show": self._show,
            "game.next_hand": self._next_hand,
            "seat.claim": self._claim_seat,
            "seat.release": self._release_seat,
            "seat.rebuy": self._legacy_rebuy,
            "seat.leave": self._leave_seat,
            "seat.topup.request": self._request_top_up,
            "seat.topup.cancel": self._cancel_top_up,
            "seat.topup.decline": self._decline_low_stack,
            "seat.topup.approve": self._approve_top_up,
            "seat.topup.reject": self._reject_top_up,
        }
        return handlers[kind](member, body)

    # ---------------------------------------------------------- seats/lobby

    def _claim_seat(self, member: _Member, body: dict[str, object]) -> _CommandResult:
        self._ensure_not_paused()
        _require_body_keys(body, {"target_seat", "buyin"})
        target = _validate_target_seat(body["target_seat"], self._config.player_count)
        buyin = self._validate_stack_target(body["buyin"], label="buyin")
        if member.ready:
            raise RoomError("CANCEL_READY_FIRST", "取消准备后才能更换座位")
        occupant = self._members_by_seat[target]
        if occupant is not None and occupant is not member:
            raise RoomError("SEAT_OCCUPIED", "目标座位已经有人或 AI")
        if self._phase not in {
            RoomPhase.LOBBY,
            RoomPhase.PLAYING,
            RoomPhase.BETWEEN_HANDS,
        }:
            raise RoomError("PHASE_MISMATCH", "当前不能选择座位")
        if self._table is not None:
            # 已经参与本桌的真人不能在进行中搬座；爆仓后先 seat.leave。
            if member.seat is not None and not member.waiting_next_hand:
                raise RoomError("SEAT_ACTIVE", "当前座位仍在牌桌中，不能换座")
            if target not in self._table.removed_seats:
                raise RoomError("SEAT_NOT_AVAILABLE", "目标座位须等下一手才可用")

        old = member.seat
        previous_buyin = self._pending_buyins.get(target)
        if old is not None and old != target:
            if self._members_by_seat[old] is member:
                self._members_by_seat[old] = None
            self._pending_buyins.pop(old, None)
        self._members_by_seat[target] = member
        member.seat = target
        member.waiting_next_hand = self._table is not None
        self._pending_buyins[target] = buyin
        changed = old != target or previous_buyin != buyin
        if changed:
            self._set_transition(
                "SEAT_MOVE" if old is not None and old != target else "SEAT_JOIN",
                member_id=member.member_id,
                target_seat=target,
                previous_seat=old,
                buyin=buyin,
                waiting_next_hand=member.waiting_next_hand,
            )
        return _CommandResult(
            {
                "command": "seat.claim",
                "target_seat": target,
                "buyin": buyin,
                "waiting_next_hand": member.waiting_next_hand,
            },
            changed=changed,
        )

    def _release_seat(self, member: _Member, body: dict[str, object]) -> _CommandResult:
        self._ensure_not_paused()
        _require_body_keys(body, set())
        if member.ready:
            raise RoomError("CANCEL_READY_FIRST", "取消准备后才能离开座位")
        if member.seat is None:
            return _CommandResult(
                {"command": "seat.release", "left_seat": False},
                changed=False,
            )
        if self._table is not None and not member.waiting_next_hand:
            raise RoomError("SEAT_ACTIVE", "牌局中的座位只能在爆仓后离开")
        old = member.seat
        self._members_by_seat[old] = None
        self._pending_buyins.pop(old, None)
        member.seat = None
        member.waiting_next_hand = False
        self._set_transition("SEAT_LEAVE", target_seat=old, member_id=member.member_id)
        return _CommandResult(
            {"command": "seat.release", "left_seat": True, "previous_seat": old}
        )

    def _set_ready(self, member: _Member, body: dict[str, object]) -> _CommandResult:
        self._require_phase(RoomPhase.LOBBY)
        _require_body_keys(body, {"ready"})
        ready = body["ready"]
        if not isinstance(ready, bool):
            raise RoomError("INVALID_BODY", "ready 须为布尔值")
        if ready and member.seat is None:
            raise RoomError("SEAT_REQUIRED", "选择座位后才能准备")
        changed = member.ready is not ready
        member.ready = ready
        if changed:
            self._set_transition(
                "READY_CHANGED",
                member_id=member.member_id,
                target_seat=member.seat,
                ready=ready,
            )
        return _CommandResult(
            {"command": "room.ready", "ready": ready},
            changed=changed,
        )

    def _start(self, member: _Member, body: dict[str, object]) -> _CommandResult:
        self._require_phase(RoomPhase.LOBBY)
        self._require_host(member)
        _require_body_keys(body, set())
        if member.seat is None or not member.ready:
            raise RoomError("HOST_NOT_READY", "房主须先入座并准备")
        occupants = [item for item in self._members_by_seat if item is not None]
        if len(occupants) < 2:
            raise RoomError("NOT_ENOUGH_PLAYERS", "至少两名玩家或 AI 才能开局")
        humans = [item for item in occupants if isinstance(item, _Member)]
        if not all(item.ready for item in humans):
            raise RoomError("PLAYERS_NOT_READY", "仍有已入座玩家没有准备")

        stacks: list[int] = []
        names: list[str] = []
        removed: set[int] = set()
        for seat, occupant in enumerate(self._members_by_seat):
            if occupant is None:
                stacks.append(0)
                names.append(f"空位 {seat + 1}")
                removed.add(seat)
                continue
            stacks.append(self._pending_buyins.get(seat, self._config.buyin))
            names.append(occupant.display_name)
            occupant.waiting_next_hand = False
        if sum(stacks) > MAX_WIRE_INTEGER:
            raise RoomError("INVALID_BUYIN", "全桌初始筹码超过协议安全整数范围")
        table_config = TableConfig(
            player_count=self._config.player_count,
            starting_stack=tuple(stacks),
            small_blind=self._config.small_blind,
            big_blind=self._config.big_blind,
            player_names=tuple(names),
            initially_removed=frozenset(removed),
            protected_seats=frozenset(),
        )
        self._table = Table(table_config, seed=self._seed)
        self._pending_buyins.clear()
        self._table.start_hand()
        self._phase = (
            RoomPhase.BETWEEN_HANDS if self._table.hand_over else RoomPhase.PLAYING
        )
        if self._table.hand_over:
            self._on_hand_finished()
        detail = {"command": "room.start", "hand_id": self._table.hand_id}
        self._set_transition("HAND_STARTED", hand_id=self._table.hand_id, initial=True)
        return _CommandResult(detail)

    # ---------------------------------------------------------- server AI

    def _add_ai(self, member: _Member, body: dict[str, object]) -> _CommandResult:
        self._require_host(member)
        self._ensure_not_paused()
        _require_body_keys(
            body,
            {"target_seat", "persona_id", "style_key", "buyin"},
        )
        target = _validate_target_seat(body["target_seat"], self._config.player_count)
        if self._members_by_seat[target] is not None:
            raise RoomError("SEAT_OCCUPIED", "目标座位已经有人或 AI")
        buyin = self._validate_stack_target(body["buyin"], label="buyin")
        if self._table is not None and target not in self._table.removed_seats:
            raise RoomError("SEAT_NOT_AVAILABLE", "目标座位不是可预约空位")
        self._ensure_persona_available(body["persona_id"])
        controller = self._build_ai(target, body["persona_id"], body["style_key"])
        ai = _AiMember(
            seat=target,
            display_name=controller.display_name,
            persona_id=controller.persona_id,
            style_key=controller.style_key,
            controller=controller,
            waiting_next_hand=self._table is not None,
        )
        self._members_by_seat[target] = ai
        self._pending_buyins[target] = buyin
        self._set_transition(
            "AI_ADD",
            target_seat=target,
            persona_id=ai.persona_id,
            style_key=ai.style_key,
            buyin=buyin,
            waiting_next_hand=ai.waiting_next_hand,
        )
        return _CommandResult(
            {
                "command": "room.ai.add",
                "target_seat": target,
                "persona_id": ai.persona_id,
                "style_key": ai.style_key,
                "buyin": buyin,
                "waiting_next_hand": ai.waiting_next_hand,
            }
        )

    def _remove_ai(self, member: _Member, body: dict[str, object]) -> _CommandResult:
        self._require_host(member)
        self._ensure_not_paused()
        _require_body_keys(body, {"target_seat"})
        target = _validate_target_seat(body["target_seat"], self._config.player_count)
        occupant = self._members_by_seat[target]
        if not isinstance(occupant, _AiMember):
            raise RoomError("AI_REQUIRED", "目标座位不是 AI")
        if self._table is not None:
            stack = self._table.stacks[target]
            if stack > 0 and not occupant.waiting_next_hand:
                raise RoomError("AI_ACTIVE", "有筹码的 AI 不能在牌局中移出")
            if target not in self._table.removed_seats:
                self._table.remove_player(target, allow_game_over=True)
        self._members_by_seat[target] = None
        self._pending_buyins.pop(target, None)
        self._ai_top_ups.pop(target, None)
        self._set_transition("AI_REMOVE", target_seat=target)
        return _CommandResult(
            {"command": "room.ai.remove", "target_seat": target}
        )

    def _rebuy_ai(self, member: _Member, body: dict[str, object]) -> _CommandResult:
        self._require_host(member)
        self._require_phase(RoomPhase.BETWEEN_HANDS)
        self._ensure_not_paused()
        _require_body_keys(body, {"target_seat", "target_stack"})
        target = _validate_target_seat(body["target_seat"], self._config.player_count)
        target_stack = self._validate_stack_target(
            body["target_stack"], label="target_stack"
        )
        occupant = self._members_by_seat[target]
        if not isinstance(occupant, _AiMember):
            raise RoomError("AI_REQUIRED", "目标座位不是 AI")
        if self._require_table().stacks[target] > 0:
            raise RoomError("AI_NOT_BUSTED", "只有爆仓 AI 需要房主处理")
        self._ensure_projected_total_safe(ai_override=(target, target_stack))
        self._ai_top_ups[target] = target_stack
        self._set_transition(
            "TOPUP_QUEUED",
            target_seat=target,
            target_stack=target_stack,
            occupant_type="AI",
        )
        return _CommandResult(
            {
                "command": "room.ai.rebuy",
                "target_seat": target,
                "target_stack": target_stack,
            }
        )

    def _style_ai(self, member: _Member, body: dict[str, object]) -> _CommandResult:
        self._require_host(member)
        self._ensure_not_paused()
        _require_body_keys(body, {"target_seat", "style_key"})
        if self._phase is RoomPhase.PLAYING:
            raise RoomError("PHASE_MISMATCH", "只能在开局前或两手之间更换打法")
        target = _validate_target_seat(body["target_seat"], self._config.player_count)
        occupant = self._members_by_seat[target]
        if not isinstance(occupant, _AiMember):
            raise RoomError("AI_REQUIRED", "目标座位不是 AI")
        controller = self._build_ai(target, occupant.persona_id, body["style_key"])
        changed = controller.style_key != occupant.style_key
        occupant.controller = controller
        occupant.style_key = controller.style_key
        occupant.display_name = controller.display_name
        if changed:
            self._set_transition(
                "AI_STYLE",
                target_seat=target,
                style_key=controller.style_key,
            )
        return _CommandResult(
            {
                "command": "room.ai.style",
                "target_seat": target,
                "style_key": controller.style_key,
            },
            changed=changed,
        )

    def _fill_ai(self, member: _Member, body: dict[str, object]) -> _CommandResult:
        """保留旧按钮语义的 v2 便利命令；新 UI 应使用指定座位 add。"""
        self._require_phase(RoomPhase.LOBBY)
        self._require_host(member)
        _require_body_keys(body, set())
        defaults = {spec.seat: spec for spec in default_server_ai_specs()}
        fallback = ("bear", "BAL")
        used_personas = {
            occupant.persona_id
            for occupant in self._members_by_seat
            if isinstance(occupant, _AiMember)
        }
        catalog_ids = [persona.persona_id for persona in persona_catalog()]
        added: list[int] = []
        for target, occupant in enumerate(self._members_by_seat):
            if occupant is not None:
                continue
            spec = defaults.get(target)
            persona_id, style_key = (
                (spec.persona_id, spec.style_key) if spec is not None else fallback
            )
            if persona_id in used_personas:
                persona_id = next(
                    candidate for candidate in catalog_ids if candidate not in used_personas
                )
                style_key = "BAL"
            controller = self._build_ai(target, persona_id, style_key)
            self._members_by_seat[target] = _AiMember(
                seat=target,
                display_name=controller.display_name,
                persona_id=controller.persona_id,
                style_key=controller.style_key,
                controller=controller,
            )
            self._pending_buyins[target] = self._config.buyin
            used_personas.add(controller.persona_id)
            added.append(target)
        if added:
            self._set_transition("AI_ADD", target_seats=added, fill=True)
        return _CommandResult(
            {"command": "room.ai.fill", "added_count": len(added)},
            changed=bool(added),
        )

    def _clear_ai(self, member: _Member, body: dict[str, object]) -> _CommandResult:
        self._require_phase(RoomPhase.LOBBY)
        self._require_host(member)
        _require_body_keys(body, set())
        removed: list[int] = []
        for target, occupant in enumerate(self._members_by_seat):
            if isinstance(occupant, _AiMember):
                self._members_by_seat[target] = None
                self._pending_buyins.pop(target, None)
                removed.append(target)
        if removed:
            self._set_transition("AI_REMOVE", target_seats=removed, clear=True)
        return _CommandResult(
            {"command": "room.ai.clear", "removed_count": len(removed)},
            changed=bool(removed),
        )

    # ---------------------------------------------------------- play

    def _apply_action(self, member: _Member, body: dict[str, object]) -> _CommandResult:
        self._require_phase(RoomPhase.PLAYING)
        self._ensure_not_paused()
        seat = self._require_member_seat(member)
        table = self._require_table()
        snapshot = table.snapshot(perspective=seat)
        if snapshot.acting_seat != seat:
            raise RoomError("NOT_YOUR_TURN", "当前不是该座位行动")
        legal = snapshot.legal_actions
        assert legal is not None
        intent = parse_action_intent(body)
        action_type = {
            "fold": ActionType.FOLD,
            "check": ActionType.CHECK,
            "call": ActionType.CALL,
            "bet": ActionType.BET,
            "raise": ActionType.RAISE,
            "allin": ActionType.ALLIN,
        }[intent.kind]
        if action_type is ActionType.CALL:
            amount = legal.call_amount
        elif action_type is ActionType.ALLIN:
            player = snapshot.players[seat]
            amount = player.stack + player.bet
        elif action_type in {ActionType.BET, ActionType.RAISE}:
            assert intent.to is not None
            amount = intent.to
        else:
            amount = 0
        street = snapshot.street.name
        action = Action(seat, action_type, amount)
        paid = _action_paid_increment(snapshot, action)
        table.apply(action)
        if not 0 <= paid <= snapshot.players[seat].stack:
            raise AssertionError("已接受真人动作的 paid 增量越界")
        if table.hand_over:
            self._on_hand_finished()
        self._set_transition(
            "ACTION",
            hand_id=table.hand_id,
            street=street,
            target_seat=seat,
            action=action_type.name,
            amount=amount,
            paid=paid,
        )
        return _CommandResult(
            {
                "command": "game.action",
                "kind": intent.kind,
                "amount": amount,
                "paid": paid,
                "hand_over": table.hand_over,
            }
        )

    def _show(self, member: _Member, body: dict[str, object]) -> _CommandResult:
        self._require_phase(RoomPhase.BETWEEN_HANDS)
        self._ensure_not_paused()
        _require_body_keys(body, set())
        seat = self._require_member_seat(member)
        table = self._require_table()
        already = seat in table.shown_seats
        try:
            table.show_cards(seat)
        except (RuntimeError, ValueError) as exc:
            raise RoomError("SHOW_FORBIDDEN", str(exc)) from exc
        if not already:
            self._record_public_summary(force=True)
            self._set_transition("SHOW", target_seat=seat, hand_id=table.hand_id)
        return _CommandResult(
            {"command": "game.show", "target_seat": seat},
            changed=not already,
        )

    def _next_hand(self, member: _Member, body: dict[str, object]) -> _CommandResult:
        self._require_host(member)
        self._require_phase(RoomPhase.BETWEEN_HANDS)
        self._ensure_not_paused()
        _require_body_keys(body, set())
        if self.busted_pending:
            raise RoomError("BUSTED_PENDING", "爆仓座位须先完成补码或离座处理")
        if self._low_stack_pending:
            raise RoomError("LOW_STACK_PENDING", "低码玩家须先选择补码或本手跳过")
        if any(
            request.status == "PENDING_APPROVAL"
            for request in self._top_up_requests.values()
        ):
            raise RoomError("TOP_UP_APPROVAL_PENDING", "仍有补码申请等待房主审批")
        if self.next_automatic_transition() != "NEXT_HAND":
            raise RoomError("NEXT_HAND_BLOCKED", "有效玩家不足两人")
        detail = self._deal_next_hand()
        self._set_transition("HAND_STARTED", **detail)
        return _CommandResult({"command": "game.next_hand", **detail})

    # ---------------------------------------------------------- bust/top-up

    def _request_top_up(self, member: _Member, body: dict[str, object]) -> _CommandResult:
        self._ensure_not_paused()
        if self._phase not in {RoomPhase.PLAYING, RoomPhase.BETWEEN_HANDS}:
            raise RoomError("PHASE_MISMATCH", "开局后才能申请补码")
        _require_body_keys(body, {"target_stack"})
        seat = self._require_member_seat(member)
        table = self._require_table()
        current = self._current_stack(seat)
        target = self._validate_stack_target(body["target_stack"], label="target_stack")
        if target <= current:
            raise RoomError("INVALID_TOP_UP", "目标码量必须高于当前码量")
        status = (
            "APPROVED"
            if current <= SELF_TOP_UP_LIMIT_BB * self._config.big_blind
            or member.is_host
            else "PENDING_APPROVAL"
        )
        previous = self._top_up_requests.get(member.member_id)
        request = _TopUpRequest(member.member_id, seat, target, status)
        self._ensure_projected_total_safe(human_override=request)
        self._top_up_requests[member.member_id] = request
        self._low_stack_pending.discard(member.member_id)
        changed = previous != request
        if changed:
            self._set_transition(
                "TOPUP_REQUEST",
                target_seat=seat,
                target_stack=target,
                status=status,
            )
        return _CommandResult(
            {
                "command": "seat.topup.request",
                "target_seat": seat,
                "target_stack": target,
                "status": status,
            },
            changed=changed,
        )

    def _cancel_top_up(self, member: _Member, body: dict[str, object]) -> _CommandResult:
        self._ensure_not_paused()
        _require_body_keys(body, set())
        request = self._top_up_requests.pop(member.member_id, None)
        if request is None:
            return _CommandResult(
                {"command": "seat.topup.cancel", "cancelled": False},
                changed=False,
            )
        self._restore_low_stack_prompt(member)
        self._set_transition("TOPUP_CANCELLED", target_seat=request.target_seat)
        return _CommandResult(
            {"command": "seat.topup.cancel", "cancelled": True}
        )

    def _decline_low_stack(self, member: _Member, body: dict[str, object]) -> _CommandResult:
        self._require_phase(RoomPhase.BETWEEN_HANDS)
        self._ensure_not_paused()
        _require_body_keys(body, set())
        if member.member_id not in self._low_stack_pending:
            raise RoomError("LOW_STACK_PROMPT_REQUIRED", "当前没有待确认的低码提示")
        self._low_stack_pending.remove(member.member_id)
        self._set_transition(
            "LOW_STACK_DECLINED",
            target_seat=member.seat,
            member_id=member.member_id,
        )
        return _CommandResult(
            {"command": "seat.topup.decline", "declined": True}
        )

    def _approve_top_up(self, member: _Member, body: dict[str, object]) -> _CommandResult:
        self._require_host(member)
        self._ensure_not_paused()
        _require_body_keys(body, {"target_seat"})
        request = self._top_up_for_target(body["target_seat"])
        if request.status != "PENDING_APPROVAL":
            raise RoomError("TOP_UP_NOT_PENDING", "该补码申请不需要审批")
        request.status = "APPROVED"
        self._set_transition(
            "TOPUP_APPROVED",
            target_seat=request.target_seat,
            target_stack=request.target_stack,
        )
        return _CommandResult(
            {
                "command": "seat.topup.approve",
                "target_seat": request.target_seat,
                "target_stack": request.target_stack,
            }
        )

    def _reject_top_up(self, member: _Member, body: dict[str, object]) -> _CommandResult:
        self._require_host(member)
        self._ensure_not_paused()
        _require_body_keys(body, {"target_seat"})
        request = self._top_up_for_target(body["target_seat"])
        if request.status != "PENDING_APPROVAL":
            raise RoomError("TOP_UP_NOT_PENDING", "该补码申请不需要审批")
        target_member = self._member_by_id(request.member_id)
        self._top_up_requests.pop(request.member_id, None)
        self._restore_low_stack_prompt(target_member)
        self._set_transition("TOPUP_REJECTED", target_seat=request.target_seat)
        return _CommandResult(
            {"command": "seat.topup.reject", "target_seat": request.target_seat}
        )

    def _legacy_rebuy(self, member: _Member, body: dict[str, object]) -> _CommandResult:
        """把旧 seat.rebuy 明确映射到 v2 目标栈语义，避免静默误加。"""
        _require_body_keys(body, {"amount"})
        return self._request_top_up(member, {"target_stack": body["amount"]})

    def _leave_seat(self, member: _Member, body: dict[str, object]) -> _CommandResult:
        self._require_phase(RoomPhase.BETWEEN_HANDS)
        self._ensure_not_paused()
        _require_body_keys(body, set())
        seat = self._require_member_seat(member)
        table = self._require_table()
        if table.stacks[seat] > 0:
            raise RoomError("PLAYER_HAS_CHIPS", "只有爆仓玩家可以离座旁观")
        table.remove_player(seat, allow_game_over=True)
        self._members_by_seat[seat] = None
        self._top_up_requests.pop(member.member_id, None)
        self._low_stack_pending.discard(member.member_id)
        member.seat = None
        member.ready = False
        member.waiting_next_hand = False
        self._set_transition(
            "SEAT_LEAVE",
            target_seat=seat,
            member_id=member.member_id,
            busted=True,
        )
        return _CommandResult(
            {"command": "seat.leave", "left_seat": True, "previous_seat": seat}
        )

    # ---------------------------------------------------------- pause/leave

    def _pause(self, member: _Member, body: dict[str, object]) -> _CommandResult:
        self._require_host(member)
        _require_body_keys(body, set())
        if self._phase not in {RoomPhase.PLAYING, RoomPhase.BETWEEN_HANDS}:
            raise RoomError("PHASE_MISMATCH", "牌局开始后才能暂停")
        if self._paused:
            return _CommandResult({"command": "room.pause", "paused": True}, False)
        self._paused = True
        self._paused_by = member.member_id
        self._set_transition("PAUSE", paused_by=member.member_id)
        return _CommandResult({"command": "room.pause", "paused": True})

    def _resume(self, member: _Member, body: dict[str, object]) -> _CommandResult:
        self._require_host(member)
        _require_body_keys(body, set())
        if not self._paused:
            return _CommandResult({"command": "room.resume", "paused": False}, False)
        self._paused = False
        self._paused_by = None
        self._set_transition("RESUME")
        return _CommandResult({"command": "room.resume", "paused": False})

    def _leave_room(self, member: _Member, body: dict[str, object]) -> _CommandResult:
        _require_body_keys(body, set())
        if member.is_host:
            raise RoomError("HOST_LEAVE_FORBIDDEN", "房主须保留管理连接")
        if member.seat is not None:
            if self._phase is RoomPhase.PLAYING and not member.waiting_next_hand:
                raise RoomError("PLAYER_ACTIVE", "进行中的手牌不能退出房间")
            if self._table is not None and not member.waiting_next_hand:
                if self._table.stacks[member.seat] > 0:
                    raise RoomError("PLAYER_HAS_CHIPS", "有筹码的玩家不能直接退出")
                self._table.remove_player(member.seat, allow_game_over=True)
            self._members_by_seat[member.seat] = None
            self._pending_buyins.pop(member.seat, None)
        old = member.seat
        self._retire_member(member)
        self._set_transition(
            "MEMBER_LEAVE",
            member_id=member.member_id,
            previous_seat=old,
        )
        return _CommandResult(
            {
                "command": "room.leave",
                "left_room": True,
                "previous_seat": old,
            }
        )

    # ---------------------------------------------------------- internals

    def _deal_next_hand(self) -> dict[str, object]:
        table = self._require_table()
        self._ensure_projected_total_safe()
        topups: list[dict[str, int]] = []
        for request in tuple(self._top_up_requests.values()):
            if request.status != "APPROVED":
                continue
            if request.target_seat in table.removed_seats:
                continue
            delta = table.top_up_to(request.target_seat, request.target_stack)
            if delta:
                topups.append(
                    {
                        "target_seat": request.target_seat,
                        "amount": delta,
                        "target_stack": request.target_stack,
                    }
                )
        self._top_up_requests.clear()
        for target, target_stack in tuple(self._ai_top_ups.items()):
            if target in table.removed_seats:
                continue
            delta = table.top_up_to(target, target_stack)
            if delta:
                topups.append(
                    {
                        "target_seat": target,
                        "amount": delta,
                        "target_stack": target_stack,
                    }
                )
        self._ai_top_ups.clear()

        joined: list[int] = []
        for target, buyin in tuple(sorted(self._pending_buyins.items())):
            occupant = self._members_by_seat[target]
            if occupant is None or target not in table.removed_seats:
                continue
            table.seat_player(target, buyin, occupant.display_name)
            occupant.waiting_next_hand = False
            joined.append(target)
        self._pending_buyins.clear()
        self._low_stack_pending.clear()
        if len(table.active_seats) < 2:
            raise RoomError("NOT_ENOUGH_PLAYERS", "有效玩家不足两人")
        table.rotate_and_deal_next()
        self._phase = RoomPhase.BETWEEN_HANDS if table.hand_over else RoomPhase.PLAYING
        if table.hand_over:
            self._on_hand_finished()
        return {
            "hand_id": table.hand_id,
            "topups_applied": topups,
            "seats_joined": joined,
        }

    def _on_hand_finished(self) -> None:
        self._phase = RoomPhase.BETWEEN_HANDS
        self._record_public_summary()
        table = self._require_table()
        # 进行中申请使用“下一手目标码量”语义。若本手结算后
        # 已自然达到该目标，申请已失效，不应继续卡住房主审批或自动续手。
        for member_id, request in tuple(self._top_up_requests.items()):
            if table.stacks[request.target_seat] >= request.target_stack:
                self._top_up_requests.pop(member_id, None)
        threshold = LOW_STACK_PROMPT_BB * self._config.big_blind
        self._low_stack_pending.clear()
        for seat, occupant in enumerate(self._members_by_seat):
            if not isinstance(occupant, _Member) or occupant.waiting_next_hand:
                continue
            stack = table.stacks[seat]
            if 0 < stack < threshold and occupant.member_id not in self._top_up_requests:
                self._low_stack_pending.add(occupant.member_id)

    def _record_public_summary(self, *, force: bool = False) -> None:
        table = self._require_table()
        result = table.last_hand_result
        if result is None:
            return
        if result.hand_id < self._last_summarized_hand_id:
            return
        if result.hand_id == self._last_summarized_hand_id:
            if not force:
                return
            if (
                self._public_hand_summaries
                and self._public_hand_summaries[-1].get("hand_id") == result.hand_id
            ):
                self._public_hand_summaries.pop()
        public = table.public_snapshot()
        names = {player.seat: player.name for player in public.players}
        visible_cards = {
            player.seat: player.hole_cards for player in public.players
        }
        winner_detail: dict[int, dict[str, object]] = {}
        for target, amount in sorted(result.winners.items()):
            cards = visible_cards.get(target)
            reveal = cards is not None
            hand_label: str | None = None
            hand_category: str | None = None
            if reveal and cards is not None:
                described = describe_holdem_hand(cards, result.board)
                hand_label = described.label
                hand_category = described.category.name
            winner_detail[target] = {
                "target_seat": target,
                "display_name": names.get(target, f"座位 {target + 1}"),
                "amount": amount,
                "reveal": reveal,
                "hand_label": hand_label,
                "hand_category": hand_category,
            }

        projected_awards: list[dict[str, object]] = []
        result_lines: list[str] = []
        for award in result.pot_awards:
            payouts: list[dict[str, object]] = []
            pot_name = "主池" if award.pot_index == 0 else f"边池 {award.pot_index}"
            for target, amount in award.payouts:
                detail = dict(winner_detail[target])
                detail["amount"] = amount
                payouts.append(detail)
                if detail["reveal"]:
                    result_lines.append(
                        f"{detail['display_name']}以{detail['hand_label']}赢得{pot_name} {amount}"
                    )
                else:
                    result_lines.append(
                        f"{detail['display_name']}未摊牌收下{pot_name} {amount}"
                    )
            projected_awards.append(
                {
                    "pot_index": award.pot_index,
                    "amount": award.pot.amount,
                    "eligible_seats": list(award.pot.eligible_seats),
                    "payouts": payouts,
                }
            )

        summary = {
            "hand_id": result.hand_id,
            "ending_street": public.street.name,
            "showdown": result.showdown,
            "board": list(result.board),
            "total_pot": sum(award.pot.amount for award in result.pot_awards),
            "pot_awards": projected_awards,
            "winners": list(winner_detail.values()),
            "result_text": "；".join(result_lines),
        }
        self._public_hand_summaries.append(summary)
        del self._public_hand_summaries[:-MAX_PUBLIC_HAND_SUMMARIES]
        self._last_summarized_hand_id = result.hand_id

    def _projected_next_hand_count(self) -> int:
        table = self._require_table()
        active = set(table.active_seats)
        active.update(self._pending_buyins)
        for request in self._top_up_requests.values():
            if request.status == "APPROVED":
                active.add(request.target_seat)
        active.update(self._ai_top_ups)
        return len(active)

    def _current_stack(self, target: int) -> int:
        table = self._require_table()
        if self._phase is RoomPhase.PLAYING:
            return table.public_snapshot().players[target].stack
        return table.stacks[target]

    def _ensure_projected_total_safe(
        self,
        *,
        human_override: _TopUpRequest | None = None,
        ai_override: tuple[int, int] | None = None,
    ) -> None:
        """在排队阶段按所有目标栈复核协议安全整数总量。"""
        table = self._require_table()
        current = [player.stack for player in table.public_snapshot().players]
        human_requests = dict(self._top_up_requests)
        if human_override is not None:
            human_requests[human_override.member_id] = human_override
        ai_requests = dict(self._ai_top_ups)
        if ai_override is not None:
            ai_requests[ai_override[0]] = ai_override[1]
        projected = sum(current)
        for request in human_requests.values():
            projected += max(0, request.target_stack - current[request.target_seat])
        for target, target_stack in ai_requests.items():
            projected += max(0, target_stack - current[target])
        if projected > MAX_WIRE_INTEGER:
            raise RoomError("INVALID_TOP_UP", "补码后全桌筹码超过协议安全整数范围")

    def _restore_low_stack_prompt(self, member: _Member) -> None:
        if self._phase is not RoomPhase.BETWEEN_HANDS or member.seat is None:
            return
        stack = self._require_table().stacks[member.seat]
        if 0 < stack < LOW_STACK_PROMPT_BB * self._config.big_blind:
            self._low_stack_pending.add(member.member_id)

    def _top_up_for_target(self, value: object) -> _TopUpRequest:
        target = _validate_target_seat(value, self._config.player_count)
        for request in self._top_up_requests.values():
            if request.target_seat == target:
                return request
        raise RoomError("TOP_UP_NOT_FOUND", "目标座位没有补码申请")

    def _build_ai(
        self,
        target: int,
        persona_id: object,
        style_key: object,
    ) -> ServerAiController:
        if not isinstance(persona_id, str) or not isinstance(style_key, str):
            raise RoomError("INVALID_AI", "persona_id/style_key 须为字符串")
        try:
            return build_server_ai_controller(
                room_id=self._room_id,
                seat=target,
                persona_id=persona_id,
                style_key=style_key,
                big_blind=self._config.big_blind,
            )
        except (ValueError, KeyError) as exc:
            raise RoomError("INVALID_AI", str(exc)) from exc

    def _ensure_persona_available(self, persona_id: object) -> None:
        if not isinstance(persona_id, str) or not persona_id.strip():
            raise RoomError("INVALID_AI", "persona_id 须为非空字符串")
        normalized = persona_id.strip().lower()
        if any(
            isinstance(occupant, _AiMember)
            and occupant.active
            and occupant.persona_id == normalized
            for occupant in self._members_by_seat
        ):
            raise RoomError("AI_PERSONA_OCCUPIED", "该动物身份已在本房间入座")

    def _new_member(self, display_name: str, *, is_host: bool) -> _Member:
        token = self._new_unique_token()
        member = _Member(
            member_id=uuid4().hex,
            display_name=_validate_display_name(display_name),
            token=token,
            is_host=is_host,
        )
        self._members_by_token[token] = member
        return member

    def _credential(self, member: _Member) -> SeatCredential:
        return SeatCredential(
            room_id=self._room_id,
            seat=member.seat,
            resume_token=member.token,
            state_version=self._state_version,
            member_id=member.member_id,
            is_host=member.is_host,
        )

    def _retire_member(self, member: _Member) -> None:
        member.active = False
        member.ready = False
        member.seat = None
        member.waiting_next_hand = False
        self._top_up_requests.pop(member.member_id, None)
        self._low_stack_pending.discard(member.member_id)
        self._members_by_token.pop(member.token, None)
        self._retired_by_token[member.token] = member
        self._retired_by_token.move_to_end(member.token)
        while len(self._retired_by_token) > MAX_RETIRED_TOKEN_TOMBSTONES:
            self._retired_by_token.popitem(last=False)

    def _member_for_token(self, token: str) -> _Member:
        if not isinstance(token, str):
            raise RoomError("AUTH_FAILED", "恢复 token 无效")
        member = self._members_by_token.get(token)
        if member is None or not member.active:
            raise RoomError("AUTH_FAILED", "恢复 token 无效或成员已离开")
        return member

    def _member_by_id(self, member_id: str) -> _Member:
        for member in self._members_by_token.values():
            if member.member_id == member_id and member.active:
                return member
        raise RoomError("AUTH_FAILED", "成员不存在")

    def _new_unique_token(self) -> str:
        while True:
            token = generate_resume_token()
            if token not in self._members_by_token and token not in self._retired_by_token:
                return token

    def _require_member_seat(self, member: _Member) -> int:
        if member.seat is None or self._members_by_seat[member.seat] is not member:
            raise RoomError("SEAT_REQUIRED", "该成员尚未入座")
        if member.waiting_next_hand:
            raise RoomError("WAITING_NEXT_HAND", "该座位将在下一手生效")
        return member.seat

    def _require_host(self, member: _Member) -> None:
        if not member.is_host or member.member_id != self._host_member_id:
            raise RoomError("HOST_ONLY", "只有房主可以执行该命令")

    def _require_phase(self, expected: RoomPhase) -> None:
        if self._phase is not expected:
            raise RoomError(
                "PHASE_MISMATCH",
                f"命令要求 {expected.value}，当前为 {self._phase.value}",
            )

    def _ensure_not_paused(self) -> None:
        if self._paused:
            raise RoomError("ROOM_PAUSED", "房主已暂停牌局")

    def _require_table(self) -> Table:
        if self._table is None:
            raise RoomError("PHASE_MISMATCH", "牌桌尚未开始")
        return self._table

    def _validate_stack_target(self, value: object, *, label: str) -> int:
        if not _is_int(value):
            raise RoomError("INVALID_BUYIN", f"{label} 须为整数筹码")
        low = MIN_BUYIN_BB * self._config.big_blind
        high = MAX_BUYIN_BB * self._config.big_blind
        if not low <= value <= high:
            raise RoomError("INVALID_BUYIN", f"{label} 须在 {low}-{high} 之间")
        return value

    def _set_transition(self, kind: str, **detail: object) -> None:
        self._transition = {"kind": kind, **detail}

    # ---------------------------------------------------------- projection

    def _project_member(self, viewer: _Member) -> dict[str, object]:
        table = self._table
        stack_by_seat = (
            tuple(player.stack for player in table.public_snapshot().players)
            if table is not None
            else None
        )
        seats: list[dict[str, object]] = []
        for target, occupant in enumerate(self._members_by_seat):
            if occupant is None or not occupant.active:
                payload: dict[str, object] = {
                    "seat": target,
                    "occupied": False,
                    "occupant_type": None,
                }
                if stack_by_seat is not None:
                    payload["stack"] = stack_by_seat[target]
                seats.append(payload)
                continue
            payload = {
                "seat": target,
                "occupied": True,
                "occupant_type": "AI" if isinstance(occupant, _AiMember) else "HUMAN",
                "display_name": occupant.display_name,
                "ready": occupant.ready,
                "is_host": isinstance(occupant, _Member) and occupant.is_host,
                "waiting_next_hand": occupant.waiting_next_hand,
            }
            if stack_by_seat is not None:
                payload["stack"] = stack_by_seat[target]
            else:
                payload["buyin"] = self._pending_buyins.get(target, self._config.buyin)
            if isinstance(occupant, _Member):
                payload["member_id"] = occupant.member_id
            else:
                payload.update(
                    {
                        "persona_id": occupant.persona_id,
                        "style_key": occupant.style_key,
                    }
                )
            seats.append(payload)

        members = [
            {
                "member_id": member.member_id,
                "display_name": member.display_name,
                "seat": member.seat,
                "ready": member.ready,
                "is_host": member.is_host,
                "waiting_next_hand": member.waiting_next_hand,
            }
            for member in self._members_by_token.values()
            if member.active
        ]
        members.sort(key=lambda item: (not bool(item["is_host"]), str(item["member_id"])))

        table_state = (
            project_table_state(
                table,
                viewer_seat=(
                    viewer.seat
                    if viewer.seat is not None and not viewer.waiting_next_hand
                    else None
                ),
                room=self._room_id,
                state_version=self._state_version,
            )
            if table is not None
            else None
        )
        low_stack_prompts = [
            {
                "member_id": member_id,
                "target_seat": self._member_by_id(member_id).seat,
                "decision_by": "SELF",
                "visible_to_viewer": member_id == viewer.member_id,
            }
            for member_id in sorted(self._low_stack_pending)
        ]
        topups = [
            {
                "member_id": request.member_id,
                "display_name": self._member_by_id(request.member_id).display_name,
                "target_seat": request.target_seat,
                "target_stack": request.target_stack,
                "status": request.status,
                "requires_host_approval": request.status == "PENDING_APPROVAL",
            }
            for request in sorted(
                self._top_up_requests.values(), key=lambda item: item.target_seat
            )
        ]
        bust_decisions: list[dict[str, object]] = []
        if table is not None:
            for target in self.busted_pending:
                occupant = self._members_by_seat[target]
                assert occupant is not None
                bust_decisions.append(
                    {
                        "target_seat": target,
                        "occupant_type": (
                            "AI" if isinstance(occupant, _AiMember) else "HUMAN"
                        ),
                        "display_name": occupant.display_name,
                        "decision_by": (
                            "HOST" if isinstance(occupant, _AiMember) else "SELF"
                        ),
                    }
                )

        transition = None
        if self._transition is not None:
            transition = {"state_version": self._state_version, **self._transition}
        return {
            "schema": ROOM_STATE_SCHEMA,
            "protocol": PROTOCOL_VERSION,
            "room": self._room_id,
            "state_version": self._state_version,
            "phase": self._phase.value,
            "viewer_member_id": viewer.member_id,
            "viewer_seat": viewer.seat,
            "viewer_is_host": viewer.is_host,
            "host_member_id": self._host_member_id,
            "host_seat": next(
                (
                    member.seat
                    for member in self._members_by_token.values()
                    if member.active and member.is_host
                ),
                None,
            ),
            "paused": self._paused,
            "paused_by": self._paused_by,
            "paused_by_name": (
                self._member_by_id(self._paused_by).display_name
                if self._paused_by is not None
                else None
            ),
            "features": {
                "free_seating": True,
                "server_ai_by_seat": True,
                "top_up_approval": True,
                "pause": True,
                "automatic_next_hand": True,
            },
            "config": {
                "player_count": self._config.player_count,
                "small_blind": self._config.small_blind,
                "big_blind": self._config.big_blind,
                "buyin": self._config.buyin,
                "min_buyin": MIN_BUYIN_BB * self._config.big_blind,
                "max_buyin": MAX_BUYIN_BB * self._config.big_blind,
                "low_stack_prompt_below": LOW_STACK_PROMPT_BB
                * self._config.big_blind,
                "self_top_up_at_or_below": SELF_TOP_UP_LIMIT_BB
                * self._config.big_blind,
            },
            "members": members,
            "seats": seats,
            "busted_pending": list(self.busted_pending),
            "bust_decisions": bust_decisions,
            "low_stack_prompts": low_stack_prompts,
            "top_up_requests": topups,
            "public_hand_summaries": deepcopy(self._public_hand_summaries),
            "transition": transition,
            "table": table_state,
        }

    # ---------------------------------------------------------- responses

    def _success_response(
        self,
        envelope: ClientEnvelope,
        detail: dict[str, object],
        state: dict[str, object],
    ) -> dict[str, object]:
        return {
            "v": PROTOCOL_VERSION,
            "type": "ack",
            "id": envelope.request_id,
            "room_id": self._room_id,
            "ok": True,
            "state_version": self._state_version,
            "result": detail,
            "state": state,
        }

    def _error_response(
        self,
        envelope: ClientEnvelope,
        code: str,
        message: str,
        *,
        state: dict[str, object] | None = None,
    ) -> dict[str, object]:
        response: dict[str, object] = {
            "v": PROTOCOL_VERSION,
            "type": "error",
            "id": envelope.request_id,
            "room_id": self._room_id,
            "ok": False,
            "state_version": self._state_version,
            "error": {"code": code, "message": message},
        }
        if state is not None:
            response["state"] = state
        return response

    def _cached_response(
        self,
        member: _Member,
        envelope: ClientEnvelope,
    ) -> dict[str, object] | None:
        request_id = envelope.request_id
        if request_id is None or request_id not in member.responses:
            return None
        fingerprint, response = member.responses.pop(request_id)
        member.responses[request_id] = (fingerprint, response)
        if fingerprint != _request_fingerprint(envelope):
            raise RoomError("IDEMPOTENCY_CONFLICT", "同一请求 id 不能用于不同命令")
        return deepcopy(response)

    def _finish_response(
        self,
        member: _Member,
        envelope: ClientEnvelope,
        response: dict[str, object],
    ) -> dict[str, object]:
        request_id = envelope.request_id
        if request_id is not None:
            member.responses[request_id] = (
                _request_fingerprint(envelope),
                deepcopy(response),
            )
            member.responses.move_to_end(request_id)
            while len(member.responses) > self._request_cache_size:
                member.responses.popitem(last=False)
        return deepcopy(response)


def _action_paid_increment(snapshot: GameSnapshot, action: Action) -> int:
    """返回动作发生当刻新增投入的筹码。

    ``Table.apply`` 可能在终结动作内立即派彩，因此不能用 apply 前后
    stack 差推导 paid；赢家的 stack 已包含底池，会得到负数。
    """
    player = snapshot.players[action.seat]
    if action.action_type in {ActionType.FOLD, ActionType.CHECK}:
        return 0
    if action.action_type is ActionType.CALL:
        paid = action.amount
    else:
        paid = action.amount - player.bet
    return paid


def _validate_room_id(room_id: object) -> str:
    if not isinstance(room_id, str):
        raise ValueError("room_id 须为字符串")
    value = room_id.strip()
    safe = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    if not 1 <= len(value) <= 32 or any(char not in safe for char in value):
        raise ValueError("room_id 格式非法")
    return value


def _validate_display_name(name: object) -> str:
    if not isinstance(name, str):
        raise ValueError("display_name 须为字符串")
    value = name.strip()
    if not value or len(value) > MAX_DISPLAY_NAME_CHARS:
        raise ValueError(f"display_name 长度须为 1-{MAX_DISPLAY_NAME_CHARS}")
    if not all(char.isprintable() for char in value):
        raise ValueError("display_name 不能包含控制或不可见格式字符")
    return value


def _validate_target_seat(value: object, capacity: int) -> int:
    if not _is_int(value) or not 0 <= value < capacity:
        raise RoomError("INVALID_SEAT", f"target_seat 须为 0-{capacity - 1} 的整数")
    return value


def _require_body_keys(body: dict[str, object], expected: set[str]) -> None:
    if set(body) != expected:
        raise RoomError(
            "INVALID_BODY",
            "body 字段须严格为: " + (", ".join(sorted(expected)) or "空对象"),
        )


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _request_fingerprint(envelope: ClientEnvelope) -> str:
    return encode_server_message(
        {
            "v": envelope.version,
            "type": envelope.message_type,
            "room_id": envelope.room_id,
            "expected_state": envelope.expected_state,
            "body": envelope.body,
        }
    )


__all__ = [
    "DEFAULT_REQUEST_CACHE_SIZE",
    "LOW_STACK_PROMPT_BB",
    "MAX_DISPLAY_NAME_CHARS",
    "MAX_PUBLIC_HAND_SUMMARIES",
    "MAX_RETIRED_TOKEN_TOMBSTONES",
    "ROOM_STATE_SCHEMA",
    "SELF_TOP_UP_LIMIT_BB",
    "RoomConfig",
    "RoomCore",
    "RoomError",
    "RoomPhase",
    "SeatCredential",
]
