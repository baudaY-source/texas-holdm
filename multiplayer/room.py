"""朋友联机的权威房间纯核心。

``RoomCore`` 不负责 WebSocket、连接锁或持久化；上层 actor 必须串行调用
本对象。客户端 token 只在这里绑定座位，所有牌桌状态都经
``project_table_state`` 生成个人视角投影。
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum

from engine.game import (
    MAX_BUYIN_BB,
    MIN_BUYIN_BB,
    GameOverRequiredError,
    IllegalActionError,
    Table,
    TableConfig,
)
from engine.state import Action, ActionType

from .auth import generate_resume_token, generate_room_code
from .projection import project_table_state
from .protocol import (
    MAX_WIRE_INTEGER,
    ClientEnvelope,
    ProtocolError,
    encode_server_message,
    parse_action_intent,
)

ROOM_STATE_SCHEMA = "tavern.room-state.v1"
DEFAULT_REQUEST_CACHE_SIZE = 128
MAX_RETIRED_TOKEN_TOMBSTONES = 128
MAX_DISPLAY_NAME_CHARS = 32


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
    """一个朋友房间的固定牌桌配置，金额单位均为整数筹码。"""

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
    """创建/加入房间后仅私发给该客户端的恢复凭据。"""

    room_id: str
    seat: int
    resume_token: str = field(repr=False)
    state_version: int


@dataclass
class _Member:
    seat: int
    display_name: str
    token: str = field(repr=False)
    ready: bool = False
    active: bool = True
    responses: OrderedDict[str, tuple[str, dict[str, object]]] = field(
        default_factory=OrderedDict
    )


@dataclass(frozen=True)
class _CommandResult:
    """一条已执行命令的公开结果与真实变更标记。"""

    payload: dict[str, object]
    changed: bool = True


class RoomCore:
    """一张由服务端权威驱动的朋友牌桌。

    构造时房主固定占用 seat 0。``join`` 由未来的房间注册表消费；已有
    token 的房内命令统一走同步 ``handle``。并发串行化属于上层 actor 的
    职责，本类自身刻意不持有线程或 asyncio 锁。
    """

    _SUPPORTED_COMMANDS = frozenset(
        {
            "room.ready",
            "room.start",
            "room.leave",
            "game.action",
            "game.show",
            "game.next_hand",
            "seat.rebuy",
            "seat.leave",
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

        resolved_room_id = generate_room_code() if room_id is None else room_id
        self._room_id = _validate_room_id(resolved_room_id)
        self._config = config
        self._phase = RoomPhase.LOBBY
        self._state_version = 0
        self._request_cache_size = request_cache_size
        self._seed = seed
        self._table: Table | None = None
        self._pending_removals: set[int] = set()

        host = _Member(
            seat=0,
            display_name=_validate_display_name(host_name),
            token=generate_resume_token(),
        )
        self._members_by_seat: list[_Member | None] = [None] * config.player_count
        self._members_by_seat[0] = host
        self._members_by_token: dict[str, _Member] = {host.token: host}
        # 退场 token 只保留一个有限 tombstone 窗口，用于重放离席 ACK；
        # 它们不再具备认证能力，也不能因大厅反复进出而无限占用内存。
        self._retired_by_token: OrderedDict[str, _Member] = OrderedDict()
        self._host_credential = SeatCredential(
            room_id=self._room_id,
            seat=0,
            resume_token=host.token,
            state_version=0,
        )

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
        """只读暴露权威引擎实例，供 actor 做事件差分；客户端不得访问。"""
        return self._table

    @property
    def host_credential(self) -> SeatCredential:
        return self._host_credential

    @property
    def occupied_seats(self) -> tuple[int, ...]:
        return tuple(
            member.seat
            for member in self._members_by_seat
            if member is not None and member.active
        )

    @property
    def busted_pending(self) -> tuple[int, ...]:
        """仍需本人选择重新买入或离桌的爆仓座位。"""
        if self._table is None:
            return ()
        return tuple(
            seat
            for seat in self._table.busted_seats
            if seat not in self._pending_removals
            and (member := self._members_by_seat[seat]) is not None
            and member.active
        )

    def join(self, display_name: str) -> SeatCredential:
        """在大厅中占用最低空座位，并签发该座位的恢复 token。"""
        self._require_phase(RoomPhase.LOBBY)
        seat = next(
            (index for index, member in enumerate(self._members_by_seat) if member is None),
            None,
        )
        if seat is None:
            raise RoomError("ROOM_FULL", "房间人数已满")
        token = self._new_unique_token()
        member = _Member(
            seat=seat,
            display_name=_validate_display_name(display_name),
            token=token,
        )
        self._members_by_seat[seat] = member
        self._members_by_token[token] = member
        self._state_version += 1
        return SeatCredential(self._room_id, seat, token, self._state_version)

    def seat_for_token(self, token: str) -> int:
        """只按高熵 token 返回当前有效座位；永不接收客户端 seat。"""
        return self._member_for_token(token).seat

    def projection_for_token(self, token: str) -> dict[str, object]:
        """生成重连所需的最新个人房间/牌桌完整投影。"""
        return self._project_member(self._member_for_token(token))

    def projection_for_seat(self, seat: int) -> dict[str, object]:
        """供可信 actor 广播使用的座位投影。

        actor 必须先用连接 token 完成认证；本方法绝不能直接接收客户端
        body 中的座位值。
        """
        if not _is_int(seat) or not 0 <= seat < self._config.player_count:
            raise RoomError("AUTH_FAILED", "座位不存在")
        member = self._members_by_seat[seat]
        if member is None or not member.active:
            raise RoomError("AUTH_FAILED", "座位不存在或已经离席")
        return self._project_member(member)

    # 兼容早期调用草案；正式 actor 应使用 projection_for_token。
    def projection_for(self, token: str) -> dict[str, object]:
        return self.projection_for_token(token)

    def close(self) -> bool:
        """由服务进程管理者关闭房间；重复关闭不产生新版本。"""
        if self._phase is RoomPhase.CLOSED:
            return False
        self._phase = RoomPhase.CLOSED
        self._state_version += 1
        return True

    def handle(
        self,
        token: str,
        envelope: ClientEnvelope,
    ) -> dict[str, object]:
        """处理一条已解析房内命令并返回可直接 JSON 编码的响应。

        相同 token、相同 request id 的重试会返回原响应的深拷贝，即使
        房间后来已经变化；这样重复包绝不会再次下注或买入。
        """
        if not isinstance(envelope, ClientEnvelope):
            raise TypeError("envelope 须为 ClientEnvelope")

        member = None
        if isinstance(token, str):
            member = self._members_by_token.get(token)
            if member is None:
                member = self._retired_by_token.get(token)
        if member is None:
            return self._error_response(
                envelope,
                "AUTH_FAILED",
                "恢复 token 无效",
            )
        try:
            cached = self._cached_response(member, envelope)
        except RoomError as exc:
            # 冲突响应不能覆盖原 UUID 的幂等记录。
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
                self._error_response(envelope, "AUTH_FAILED", "该座位已离开房间"),
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
            response = self._error_response(
                envelope,
                "STALE_STATE",
                "客户端状态版本已过期",
                state=self._project_member(member),
            )
            return self._finish_response(member, envelope, response)
        if self._phase is RoomPhase.CLOSED:
            return self._finish_response(
                member,
                envelope,
                self._error_response(envelope, "ROOM_CLOSED", "房间已经关闭"),
            )

        before = self._state_version
        try:
            command_result = self._dispatch(member, envelope)
        except RoomError as exc:
            response = self._error_response(envelope, exc.code, exc.message)
        except ProtocolError as exc:
            response = self._error_response(envelope, exc.code, exc.message)
        except IllegalActionError as exc:
            response = self._error_response(envelope, "ILLEGAL_ACTION", str(exc))
        else:
            # 只有真实状态变更才推进版本。重复 SHOW、重复设置相同 ready
            # 等幂等业务 no-op 不得制造版本风暴。
            if command_result.changed:
                self._state_version = before + 1
            response = self._success_response(
                envelope,
                command_result.payload,
                self._project_member(member),
            )
        if self._state_version not in {before, before + 1}:
            raise AssertionError("一次房间命令只能推进一个 state_version")
        return self._finish_response(member, envelope, response)

    # ---------------------------------------------------------- 命令分派

    def _dispatch(
        self,
        member: _Member,
        envelope: ClientEnvelope,
    ) -> _CommandResult:
        message_type = envelope.message_type
        if message_type == "room.ready":
            return self._set_ready(member, envelope.body)
        if message_type == "room.start":
            return _CommandResult(self._start(member, envelope.body))
        if message_type == "room.leave":
            return _CommandResult(self._leave_room(member, envelope.body))
        if message_type == "game.action":
            return _CommandResult(self._apply_action(member, envelope.body))
        if message_type == "game.show":
            return self._show(member, envelope.body)
        if message_type == "game.next_hand":
            return _CommandResult(self._next_hand(member, envelope.body))
        if message_type == "seat.rebuy":
            return _CommandResult(self._rebuy(member, envelope.body))
        if message_type == "seat.leave":
            return _CommandResult(self._leave_seat(member, envelope.body))
        raise RoomError("UNSUPPORTED_COMMAND", "未知房间命令")  # pragma: no cover

    def _set_ready(
        self,
        member: _Member,
        body: dict[str, object],
    ) -> _CommandResult:
        self._require_phase(RoomPhase.LOBBY)
        _require_body_keys(body, {"ready"})
        ready = body["ready"]
        if not isinstance(ready, bool):
            raise RoomError("INVALID_BODY", "ready 须为布尔值")
        changed = member.ready is not ready
        member.ready = ready
        return _CommandResult(
            {"command": "room.ready", "ready": ready},
            changed=changed,
        )

    def _start(
        self,
        member: _Member,
        body: dict[str, object],
    ) -> dict[str, object]:
        self._require_phase(RoomPhase.LOBBY)
        self._require_host(member)
        _require_body_keys(body, set())
        active_members = [
            item
            for item in self._members_by_seat
            if item is not None and item.active
        ]
        if len(active_members) != self._config.player_count:
            raise RoomError("ROOM_NOT_FULL", "须等目标人数全部入座后才能开局")
        if not all(item.ready for item in active_members):
            raise RoomError("PLAYERS_NOT_READY", "仍有玩家没有准备")

        names = tuple(item.display_name for item in active_members)
        table_config = TableConfig(
            player_count=self._config.player_count,
            starting_stack=self._config.buyin,
            small_blind=self._config.small_blind,
            big_blind=self._config.big_blind,
            player_names=names,
        )
        self._table = Table(table_config, seed=self._seed)
        self._table.start_hand()
        self._phase = (
            RoomPhase.BETWEEN_HANDS if self._table.hand_over else RoomPhase.PLAYING
        )
        return {"command": "room.start", "hand_id": self._table.hand_id}

    def _leave_room(
        self,
        member: _Member,
        body: dict[str, object],
    ) -> dict[str, object]:
        _require_body_keys(body, set())
        if member.seat == 0:
            raise RoomError("HOST_LEAVE_FORBIDDEN", "Alpha 房主不能主动离开")
        if self._phase is RoomPhase.LOBBY:
            self._retire_member(member)
            return {"command": "room.leave", "seat": member.seat}
        if self._phase is RoomPhase.PLAYING:
            raise RoomError("PLAYER_ACTIVE", "进行中的手牌不能主动离桌")
        self._require_phase(RoomPhase.BETWEEN_HANDS)
        self._require_busted(member)
        self._retire_busted_member(member)
        return {"command": "room.leave", "seat": member.seat}

    def _apply_action(
        self,
        member: _Member,
        body: dict[str, object],
    ) -> dict[str, object]:
        self._require_phase(RoomPhase.PLAYING)
        table = self._require_table()
        snapshot = table.snapshot(perspective=member.seat)
        if snapshot.acting_seat != member.seat:
            raise RoomError("NOT_YOUR_TURN", "当前不是该座位行动")
        legal = snapshot.legal_actions
        if legal is None:  # pragma: no cover - 与 acting_seat 契约互斥
            raise RoomError("ILLEGAL_ACTION", "当前没有合法动作")
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
            # 客户端不上传 CALL 金额；只信任刚取得的权威合法动作。
            amount = legal.call_amount
        elif action_type is ActionType.ALLIN:
            # ALLIN 的“加到”总额 = 当前剩余筹码 + 本街已下注。
            player = snapshot.players[member.seat]
            amount = player.stack + player.bet
        elif action_type in {ActionType.BET, ActionType.RAISE}:
            assert intent.to is not None
            amount = intent.to
        else:
            amount = 0

        table.apply(Action(member.seat, action_type, amount))
        if table.hand_over:
            self._phase = RoomPhase.BETWEEN_HANDS
        return {
            "command": "game.action",
            "kind": intent.kind,
            "amount": amount,
            "hand_over": table.hand_over,
        }

    def _show(
        self,
        member: _Member,
        body: dict[str, object],
    ) -> _CommandResult:
        self._require_phase(RoomPhase.BETWEEN_HANDS)
        _require_body_keys(body, set())
        table = self._require_table()
        already_shown = member.seat in table.shown_seats
        try:
            table.show_cards(member.seat)
        except (RuntimeError, ValueError) as exc:
            raise RoomError("SHOW_FORBIDDEN", str(exc)) from exc
        return _CommandResult(
            {"command": "game.show", "seat": member.seat},
            changed=not already_shown,
        )

    def _next_hand(
        self,
        member: _Member,
        body: dict[str, object],
    ) -> dict[str, object]:
        self._require_phase(RoomPhase.BETWEEN_HANDS)
        self._require_host(member)
        _require_body_keys(body, set())
        table = self._require_table()
        self._flush_pending_removals()
        pending = self.busted_pending
        if pending:
            raise RoomError(
                "BUSTED_PENDING",
                "爆仓座位须先选择重新买入或离桌: "
                + ",".join(str(seat) for seat in pending),
            )
        if table.game_over:
            raise RoomError("GAME_OVER", "剩余玩家不足两人")
        try:
            table.rotate_and_deal_next()
        except RuntimeError as exc:
            raise RoomError("GAME_OVER", str(exc)) from exc
        self._phase = RoomPhase.BETWEEN_HANDS if table.hand_over else RoomPhase.PLAYING
        return {"command": "game.next_hand", "hand_id": table.hand_id}

    def _rebuy(
        self,
        member: _Member,
        body: dict[str, object],
    ) -> dict[str, object]:
        self._require_phase(RoomPhase.BETWEEN_HANDS)
        _require_body_keys(body, {"amount"})
        amount = body["amount"]
        if not _is_int(amount):
            raise RoomError("INVALID_REBUY", "重新买入额度须为整数筹码")
        self._require_busted(member)
        table = self._require_table()
        if amount > MAX_WIRE_INTEGER or sum(table.stacks) + amount > MAX_WIRE_INTEGER:
            raise RoomError("INVALID_REBUY", "全桌筹码将超过协议安全整数范围")
        try:
            table.rebuy(member.seat, amount)
        except (RuntimeError, ValueError) as exc:
            raise RoomError("INVALID_REBUY", str(exc)) from exc
        self._flush_pending_removals()
        return {"command": "seat.rebuy", "seat": member.seat, "amount": amount}

    def _leave_seat(
        self,
        member: _Member,
        body: dict[str, object],
    ) -> dict[str, object]:
        self._require_phase(RoomPhase.BETWEEN_HANDS)
        _require_body_keys(body, set())
        if member.seat == 0:
            raise RoomError("HOST_LEAVE_FORBIDDEN", "Alpha 房主不能主动离开")
        self._require_busted(member)
        self._retire_busted_member(member)
        return {"command": "seat.leave", "seat": member.seat}

    # ---------------------------------------------------------- 内部状态

    def _retire_busted_member(self, member: _Member) -> None:
        table = self._require_table()
        self._retire_member(member)
        if len(self.occupied_seats) < 2:
            self._phase = RoomPhase.CLOSED
            return
        if len(table.active_seats) >= 2:
            try:
                table.remove_player(member.seat)
            except GameOverRequiredError:
                self._pending_removals.add(member.seat)
        else:
            # 另一位爆仓成员可能稍后重新买入；先把此座位记为已选择
            # 离桌，避免它继续阻塞下一手，待恢复到两名有筹码者后落盘。
            self._pending_removals.add(member.seat)

    def _flush_pending_removals(self) -> None:
        table = self._require_table()
        if len(table.active_seats) < 2:
            return
        for seat in tuple(sorted(self._pending_removals)):
            try:
                table.remove_player(seat)
            except GameOverRequiredError:
                return
            else:
                self._pending_removals.remove(seat)

    def _retire_member(self, member: _Member) -> None:
        member.active = False
        member.ready = False
        if self._members_by_seat[member.seat] is member:
            self._members_by_seat[member.seat] = None
        self._members_by_token.pop(member.token, None)
        self._retired_by_token[member.token] = member
        self._retired_by_token.move_to_end(member.token)
        while len(self._retired_by_token) > MAX_RETIRED_TOKEN_TOMBSTONES:
            self._retired_by_token.popitem(last=False)

    def _require_busted(self, member: _Member) -> None:
        table = self._require_table()
        if table.stacks[member.seat] > 0:
            raise RoomError("PLAYER_HAS_CHIPS", "仍有筹码的玩家不能离桌或重新买入")

    def _require_host(self, member: _Member) -> None:
        if member.seat != 0:
            raise RoomError("HOST_ONLY", "只有房主可以执行该命令")

    def _require_phase(self, expected: RoomPhase) -> None:
        if self._phase is not expected:
            raise RoomError(
                "PHASE_MISMATCH",
                f"命令要求 {expected.value}，当前为 {self._phase.value}",
            )

    def _require_table(self) -> Table:
        if self._table is None:
            raise RoomError("PHASE_MISMATCH", "牌桌尚未开始")
        return self._table

    def _member_for_token(self, token: str) -> _Member:
        if not isinstance(token, str):
            raise RoomError("AUTH_FAILED", "恢复 token 无效")
        member = self._members_by_token.get(token)
        if member is None or not member.active:
            raise RoomError("AUTH_FAILED", "恢复 token 无效或座位已离开")
        return member

    def _new_unique_token(self) -> str:
        while True:  # secrets 碰撞概率可忽略；循环仍保持契约完整。
            token = generate_resume_token()
            if (
                token not in self._members_by_token
                and token not in self._retired_by_token
            ):
                return token

    # ---------------------------------------------------------- 响应/投影

    def _project_member(self, member: _Member) -> dict[str, object]:
        members: list[dict[str, object]] = []
        for seat in range(self._config.player_count):
            occupant = self._members_by_seat[seat]
            if occupant is None or not occupant.active:
                members.append({"seat": seat, "occupied": False})
            else:
                members.append(
                    {
                        "seat": seat,
                        "occupied": True,
                        "display_name": occupant.display_name,
                        "ready": occupant.ready,
                        "is_host": seat == 0,
                    }
                )
        table_state = (
            project_table_state(
                self._table,
                viewer_seat=member.seat,
                room=self._room_id,
                state_version=self._state_version,
            )
            if self._table is not None
            else None
        )
        return {
            "schema": ROOM_STATE_SCHEMA,
            "room": self._room_id,
            "state_version": self._state_version,
            "phase": self._phase.value,
            "viewer_seat": member.seat,
            "host_seat": 0,
            "config": {
                "player_count": self._config.player_count,
                "small_blind": self._config.small_blind,
                "big_blind": self._config.big_blind,
                "buyin": self._config.buyin,
            },
            "seats": members,
            "busted_pending": list(self.busted_pending),
            "table": table_state,
        }

    def _success_response(
        self,
        envelope: ClientEnvelope,
        detail: dict[str, object],
        state: dict[str, object],
    ) -> dict[str, object]:
        return {
            "v": 1,
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
            "v": 1,
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
            raise RoomError(
                "IDEMPOTENCY_CONFLICT",
                "同一请求 id 不能用于不同命令",
            )
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
    "MAX_RETIRED_TOKEN_TOMBSTONES",
    "MAX_DISPLAY_NAME_CHARS",
    "ROOM_STATE_SCHEMA",
    "RoomConfig",
    "RoomCore",
    "RoomError",
    "RoomPhase",
    "SeatCredential",
]
