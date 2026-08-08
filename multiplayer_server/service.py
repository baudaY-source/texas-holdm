"""朋友联机的单进程权威房间注册表与传输后端。

本模块把严格 WebSocket 传输、``RoomActor`` 和 ``RoomCore`` 接成首个可
联调闭环。服务仍只监听 localhost；公网 TLS/WSS 由后续 tunnel 层提供。
Alpha 默认只允许一个活跃房间，但房间本身支持 2–9 名真人。
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from collections.abc import Mapping
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field
import json
import math
import random
import time

from multiplayer.actor import OutboundChannel, RoomActor
from multiplayer.auth import generate_room_code
from multiplayer.protocol import PROTOCOL_VERSION, ClientEnvelope
from multiplayer.room import RoomConfig, RoomCore, RoomError, RoomPhase

from .ws_app import Close, ConnectionRejected, Emit, HelloInfo

DEFAULT_MAX_ROOMS = 1
DEFAULT_RESPONSE_CACHE = 128
DEFAULT_COMMANDS_PER_SECOND = 30
DEFAULT_RATE_WINDOW_SECONDS = 1.0
DEFAULT_EMPTY_ROOM_TTL_SECONDS = 15 * 60.0
DEFAULT_AI_DELAY_MIN_SECONDS = 0.8
DEFAULT_AI_DELAY_MAX_SECONDS = 1.2
DEFAULT_SETTLEMENT_DELAY_SECONDS = 3.0


@dataclass
class _ConnectionSession:
    connection_id: str
    emit: Emit = field(repr=False)
    close: Close = field(repr=False)
    room_id: str | None = None
    token: str | None = field(default=None, repr=False)
    pump: asyncio.Task[None] | None = field(default=None, repr=False)
    revoked: bool = False
    responses: OrderedDict[str, tuple[str, dict[str, object]]] = field(
        default_factory=OrderedDict,
        repr=False,
    )
    command_times: deque[float] = field(default_factory=deque, repr=False)

    @property
    def bound(self) -> bool:
        return self.room_id is not None and self.token is not None


@dataclass
class _RoomRuntime:
    core: RoomCore
    actor: RoomActor
    lifecycle_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cleanup_task: asyncio.Task[None] | None = field(default=None, repr=False)
    transition_task: asyncio.Task[None] | None = field(default=None, repr=False)
    transition_kind: str | None = field(default=None, repr=False)
    transition_version: int | None = field(default=None, repr=False)
    empty_since: float | None = None


class RoomRegistryBackend:
    """实现 ``TransportBackend`` 的最小权威朋友局服务。"""

    def __init__(
        self,
        *,
        max_rooms: int = DEFAULT_MAX_ROOMS,
        response_cache_size: int = DEFAULT_RESPONSE_CACHE,
        commands_per_second: int = DEFAULT_COMMANDS_PER_SECOND,
        empty_room_ttl_seconds: float | None = DEFAULT_EMPTY_ROOM_TTL_SECONDS,
        ai_delay_min_seconds: float = DEFAULT_AI_DELAY_MIN_SECONDS,
        ai_delay_max_seconds: float = DEFAULT_AI_DELAY_MAX_SECONDS,
        settlement_delay_seconds: float = DEFAULT_SETTLEMENT_DELAY_SECONDS,
    ) -> None:
        if isinstance(max_rooms, bool) or not isinstance(max_rooms, int) or max_rooms <= 0:
            raise ValueError("max_rooms 须为正整数")
        if (
            isinstance(response_cache_size, bool)
            or not isinstance(response_cache_size, int)
            or response_cache_size <= 0
        ):
            raise ValueError("response_cache_size 须为正整数")
        if (
            isinstance(commands_per_second, bool)
            or not isinstance(commands_per_second, int)
            or commands_per_second <= 0
        ):
            raise ValueError("commands_per_second 须为正整数")
        if empty_room_ttl_seconds is not None and (
            isinstance(empty_room_ttl_seconds, bool)
            or not isinstance(empty_room_ttl_seconds, (int, float))
            or not math.isfinite(empty_room_ttl_seconds)
            or empty_room_ttl_seconds <= 0
        ):
            raise ValueError("empty_room_ttl_seconds 须为正有限数或 None")
        for field_name, value in (
            ("ai_delay_min_seconds", ai_delay_min_seconds),
            ("ai_delay_max_seconds", ai_delay_max_seconds),
            ("settlement_delay_seconds", settlement_delay_seconds),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"{field_name} 须为非负有限数")
        if ai_delay_max_seconds < ai_delay_min_seconds:
            raise ValueError(
                "ai_delay_max_seconds 不得小于 ai_delay_min_seconds"
            )
        self._max_rooms = max_rooms
        self._response_cache_size = response_cache_size
        self._commands_per_second = commands_per_second
        self._empty_room_ttl_seconds = (
            None
            if empty_room_ttl_seconds is None
            else float(empty_room_ttl_seconds)
        )
        self._ai_delay_min_seconds = float(ai_delay_min_seconds)
        self._ai_delay_max_seconds = float(ai_delay_max_seconds)
        self._settlement_delay_seconds = float(settlement_delay_seconds)
        self._connections: dict[str, _ConnectionSession] = {}
        self._rooms: dict[str, _RoomRuntime] = {}
        self._connection_for_token: dict[str, str] = {}
        self._registry_lock = asyncio.Lock()
        self._closed = False

    @property
    def active_room_ids(self) -> tuple[str, ...]:
        """当前尚未关闭的房间码，仅供服务监控与测试。"""
        return tuple(
            room_id
            for room_id, runtime in self._rooms.items()
            if runtime.core.phase is not RoomPhase.CLOSED
        )

    @property
    def room_count(self) -> int:
        return len(self.active_room_ids)

    def room(self, room_id: str) -> RoomCore | None:
        """返回可信服务内部核心；不得暴露给网络客户端。"""
        runtime = self._rooms.get(room_id)
        return None if runtime is None else runtime.core

    async def connect(
        self,
        connection_id: str,
        hello: HelloInfo,
        emit: Emit,
        close: Close,
    ) -> None:
        if self._closed:
            raise ConnectionRejected("SERVICE_UNAVAILABLE", "服务正在关闭")
        if connection_id in self._connections:
            raise ConnectionRejected("CONNECTION_EXISTS", "连接已经登记")
        session = _ConnectionSession(connection_id, emit, close)

        if not hello.is_resume:
            self._connections[connection_id] = session
            emit(_welcome_message(connection_id, resumed=False))
            return

        room_id = hello.resume_room_id
        token = hello.resume_token
        if room_id is None or token is None:  # HelloInfo 契约的防御性兜底
            raise ConnectionRejected("AUTH_FAILED", "恢复凭据无效")
        runtime = self._rooms.get(room_id)
        if runtime is None:
            raise ConnectionRejected("AUTH_FAILED", "恢复凭据无效")

        async with runtime.lifecycle_lock:
            # ``runtime`` 在等锁期间可能刚被空房回收任务关闭并移出 registry。
            # 必须在同一生命周期锁内二次确认，不能尝试向已关闭 actor 重新绑定。
            if (
                self._rooms.get(room_id) is not runtime
                or runtime.core.phase is RoomPhase.CLOSED
            ):
                raise ConnectionRejected("AUTH_FAILED", "恢复凭据无效")
            try:
                runtime.core.principal_for_token(token)
                seat = runtime.core.seat_for_token(token)
            except RoomError as exc:
                raise ConnectionRejected("AUTH_FAILED", "恢复凭据无效") from exc
            self._connections[connection_id] = session
            try:
                await self._bind_session(session, runtime, token)
            except Exception:
                self._connections.pop(connection_id, None)
                raise
            emit(
                _welcome_message(
                    connection_id,
                    resumed=True,
                    room_id=room_id,
                    seat=seat,
                    state_version=runtime.core.state_version,
                )
            )
            emit(_state_message(runtime.core.projection_for_token(token)))

    async def submit(
        self,
        connection_id: str,
        envelope: ClientEnvelope,
    ) -> None:
        session = self._connections.get(connection_id)
        if session is None or session.revoked:
            raise ConnectionRejected("AUTH_FAILED", "连接已经失效")
        self._check_rate(session)

        cached = self._cached_response(session, envelope)
        if cached is not None:
            session.emit(cached)
            return

        if not session.bound:
            if envelope.message_type == "room.create":
                await self._create_room(session, envelope)
            elif envelope.message_type == "room.join":
                await self._join_room(session, envelope)
            else:
                self._reply_error(
                    session,
                    envelope,
                    "AUTH_REQUIRED",
                    "请先创建、加入或恢复房间",
                )
            return

        if envelope.message_type in {"room.create", "room.join"}:
            self._reply_error(
                session,
                envelope,
                "ALREADY_IN_ROOM",
                "当前连接已经绑定房间",
            )
            return
        await self._submit_room_command(session, envelope)

    async def disconnect(self, connection_id: str) -> None:
        session = self._connections.pop(connection_id, None)
        if session is None:
            return
        session.revoked = True
        if not session.bound:
            await _cancel(session.pump)
            return
        room_id = session.room_id
        token = session.token
        runtime = self._rooms.get(room_id) if room_id is not None else None
        if runtime is None:
            await _cancel(session.pump)
            return
        async with runtime.lifecycle_lock:
            await self._unbind_session(session, runtime)
            if token is not None and self._connection_for_token.get(token) == connection_id:
                self._connection_for_token.pop(token, None)
            if self._room_has_bound_sessions(runtime.core.room_id):
                self._schedule_automatic_transition(runtime)
            else:
                self._cancel_automatic_transition(runtime)
            self._schedule_room_cleanup(runtime)

    async def close(self) -> None:
        """关闭 actor 与后台 pump；重复调用幂等。"""
        if self._closed:
            return
        self._closed = True
        sessions = tuple(self._connections.values())
        for session in sessions:
            session.revoked = True
            session.close(4004, "SERVICE_SHUTDOWN")
            await _cancel(session.pump)
        self._connections.clear()
        self._connection_for_token.clear()
        for runtime in tuple(self._rooms.values()):
            await _cancel(runtime.cleanup_task)
            runtime.cleanup_task = None
            await _cancel(runtime.transition_task)
            runtime.transition_task = None
            await runtime.actor.close()
        self._rooms.clear()

    # ---------------------------------------------------------- registry

    async def _create_room(
        self,
        session: _ConnectionSession,
        envelope: ClientEnvelope,
    ) -> None:
        try:
            display_name, config = _parse_create_body(envelope.body)
        except (RoomError, ValueError) as exc:
            self._reply_error(session, envelope, "INVALID_BODY", str(exc))
            return

        async with self._registry_lock:
            if self.room_count >= self._max_rooms:
                self._reply_error(session, envelope, "ROOM_LIMIT", "当前已有活跃房间")
                return
            room_id = generate_room_code(self._rooms)
            try:
                core = RoomCore(config, display_name, room_id=room_id)
            except ValueError as exc:
                self._reply_error(session, envelope, "INVALID_BODY", str(exc))
                return
            actor = RoomActor(core)
            await actor.start()
            runtime = _RoomRuntime(core, actor)
            self._rooms[room_id] = runtime

        async with runtime.lifecycle_lock:
            credential = core.host_credential
            await self._bind_session(session, runtime, credential.resume_token)
            response = _credential_ack(
                envelope,
                core,
                command="room.create",
                seat=credential.seat,
                token=credential.resume_token,
                member_id=credential.member_id,
                is_host=credential.is_host,
            )
            self._cache_and_emit(session, envelope, response)
            await actor.publish_per_principal(
                lambda token: _state_message(core.projection_for_token(token))
            )

    async def _join_room(
        self,
        session: _ConnectionSession,
        envelope: ClientEnvelope,
    ) -> None:
        try:
            display_name = _parse_join_body(envelope.body)
        except (RoomError, ValueError) as exc:
            self._reply_error(session, envelope, "INVALID_BODY", str(exc))
            return
        room_id = envelope.room_id
        runtime = self._rooms.get(room_id) if room_id is not None else None
        if runtime is None or runtime.core.phase is RoomPhase.CLOSED:
            self._reply_error(session, envelope, "ROOM_NOT_FOUND", "房间不存在")
            return

        async with runtime.lifecycle_lock:
            # 空房 TTL 可能在初次查询与取得生命周期锁之间到期。
            if (
                self._rooms.get(room_id) is not runtime
                or runtime.core.phase is RoomPhase.CLOSED
            ):
                self._reply_error(session, envelope, "ROOM_NOT_FOUND", "房间不存在")
                return
            before = runtime.core.state_version
            try:
                credential = runtime.core.join(display_name)
            except RoomError as exc:
                self._reply_error(session, envelope, exc.code, exc.message)
                return
            except ValueError as exc:
                self._reply_error(session, envelope, "INVALID_BODY", str(exc))
                return
            await self._bind_session(session, runtime, credential.resume_token)
            response = _credential_ack(
                envelope,
                runtime.core,
                command="room.join",
                seat=credential.seat,
                token=credential.resume_token,
                member_id=credential.member_id,
                is_host=credential.is_host,
            )
            self._cache_and_emit(session, envelope, response)
            if runtime.core.state_version != before:
                await runtime.actor.publish_per_principal(
                    lambda token: _state_message(
                        runtime.core.projection_for_token(token)
                    )
                )
            self._schedule_automatic_transition(runtime)

    # ---------------------------------------------------------- room commands

    async def _submit_room_command(
        self,
        session: _ConnectionSession,
        envelope: ClientEnvelope,
    ) -> None:
        room_id = session.room_id
        token = session.token
        assert room_id is not None and token is not None
        runtime = self._rooms.get(room_id)
        if runtime is None:
            self._reply_error(session, envelope, "ROOM_NOT_FOUND", "房间不存在")
            return
        if self._connection_for_token.get(token) != session.connection_id:
            session.revoked = True
            session.close(4001, "SESSION_REPLACED")
            raise ConnectionRejected("AUTH_FAILED", "连接已经被替换")

        async with runtime.lifecycle_lock:
            # ``_connection_for_token`` 在取得 lifecycle lock 前可能已经被
            # 同一 member 的恢复连接改写。外层的快速检查只是在常见路径尽早
            # 拒绝旧连接，不能作为授权依据；否则旧 WebSocket 可以在等锁期间
            # 被替换后仍成功落下一条权威命令。
            if (
                session.revoked
                or self._connections.get(session.connection_id) is not session
                or self._connection_for_token.get(token) != session.connection_id
            ):
                if not session.revoked:
                    session.revoked = True
                    session.close(4001, "SESSION_REPLACED")
                raise ConnectionRejected("AUTH_FAILED", "连接已经被替换")
            before = runtime.core.state_version
            response = await runtime.actor.submit(token, envelope)
            if not isinstance(response, Mapping):
                raise RuntimeError("RoomCore 返回了非对象响应")
            response_dict = dict(response)
            self._cache_and_emit(session, envelope, response_dict)
            changed = runtime.core.state_version != before
            result = response_dict.get("result")
            left = (
                bool(response_dict.get("ok"))
                and isinstance(result, Mapping)
                and result.get("left_room") is True
            )

            if left:
                # ACK 已先进入该连接的单 writer 队列；保持 WebSocket 本身可用，
                # 仅解除旧座位。随后它可创建或加入别的房间。
                await self._unbind_session(session, runtime)
                self._connection_for_token.pop(token, None)
                self._schedule_room_cleanup(runtime)
            if changed:
                await runtime.actor.publish_per_principal(
                    lambda viewer_token: _state_message(
                        runtime.core.projection_for_token(viewer_token)
                    )
                )
            # 无效/stale/no-op 命令不得重置已在进行的 AI/续手截止时间，
            # 否则任意成员可以持续发送拒绝命令使牌局永不推进。
            self._schedule_automatic_transition(runtime)

    async def _bind_session(
        self,
        session: _ConnectionSession,
        runtime: _RoomRuntime,
        token: str,
    ) -> None:
        self._cancel_room_cleanup(runtime)
        old_connection_id = self._connection_for_token.get(token)
        if old_connection_id is not None and old_connection_id != session.connection_id:
            old = self._connections.get(old_connection_id)
            if old is not None:
                old.revoked = True
                await _cancel(old.pump)
                old.pump = None
                await runtime.actor.disconnect(old_connection_id, reason="replaced")
                old.close(4001, "SESSION_REPLACED")
                old.room_id = None
                old.token = None

        channel = await runtime.actor.register_connection(session.connection_id, token)
        session.room_id = runtime.core.room_id
        session.token = token
        session.revoked = False
        self._connection_for_token[token] = session.connection_id
        session.pump = asyncio.create_task(
            self._pump_actor_channel(session, channel),
            name=f"room-outbound-{session.connection_id}",
        )
        self._schedule_automatic_transition(runtime)

    async def _unbind_session(
        self,
        session: _ConnectionSession,
        runtime: _RoomRuntime,
    ) -> None:
        await _cancel(session.pump)
        session.pump = None
        await runtime.actor.disconnect(session.connection_id, reason="session_unbound")
        session.room_id = None
        session.token = None

    # ------------------------------------------------ automatic transitions

    def _cancel_automatic_transition(self, runtime: _RoomRuntime) -> None:
        """同步撤销尚未到期的 AI/续手任务。

        ``Task.cancel`` 不等待任务退出，因此本方法可安全地在 lifecycle
        lock 内调用。任务到期后还会核对自身身份、状态版本和核心 hook，
        已失效的任务即便晚一步收到取消也不能推进房间。
        """
        task = runtime.transition_task
        runtime.transition_task = None
        runtime.transition_kind = None
        runtime.transition_version = None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    def _schedule_automatic_transition(self, runtime: _RoomRuntime) -> None:
        """为当前版本确保至多一个权威自动转换。

        已存在同版本、同种类任务时保留原 deadline；只有核心版本/
        hook 改变或全房无连接时才撤销重排。
        """
        if (
            self._closed
            or self._rooms.get(runtime.core.room_id) is not runtime
            or not self._room_has_bound_sessions(runtime.core.room_id)
        ):
            self._cancel_automatic_transition(runtime)
            return
        kind = runtime.core.next_automatic_transition()
        if kind is None:
            self._cancel_automatic_transition(runtime)
            return
        version = runtime.core.state_version
        existing = runtime.transition_task
        if (
            existing is not None
            and not existing.done()
            and runtime.transition_kind == kind
            and runtime.transition_version == version
        ):
            return
        self._cancel_automatic_transition(runtime)
        if kind == "AI_ACTION":
            delay = random.uniform(
                self._ai_delay_min_seconds,
                self._ai_delay_max_seconds,
            )
        elif kind == "NEXT_HAND":
            delay = self._settlement_delay_seconds
        else:
            raise RuntimeError(f"RoomCore 返回未知自动转换: {kind!r}")
        runtime.transition_task = asyncio.create_task(
            self._run_automatic_transition(runtime, kind, version, delay),
            name=(
                f"room-transition-{runtime.core.room_id}-"
                f"v{version}-{kind.lower()}"
            ),
        )
        runtime.transition_kind = kind
        runtime.transition_version = version

    async def _run_automatic_transition(
        self,
        runtime: _RoomRuntime,
        kind: str,
        expected_version: int,
        delay: float,
    ) -> None:
        """计时到期后，在锁内重验并仅提交一个 actor 变更。"""
        current = asyncio.current_task()
        try:
            await asyncio.sleep(delay)
            async with runtime.lifecycle_lock:
                if (
                    self._closed
                    or runtime.transition_task is not current
                    or self._rooms.get(runtime.core.room_id) is not runtime
                    or runtime.core.state_version != expected_version
                    or runtime.core.next_automatic_transition() != kind
                    or not self._room_has_bound_sessions(runtime.core.room_id)
                ):
                    return

                # 先清除当前引用；单步完成后才能依据新状态排下一次任务。
                runtime.transition_task = None
                runtime.transition_kind = None
                runtime.transition_version = None
                stepper = (
                    runtime.core.step_server_ai
                    if kind == "AI_ACTION"
                    else runtime.core.step_scheduled_next_hand
                )
                before = runtime.core.state_version
                report = await runtime.actor.step_automatic(
                    stepper,
                    lambda token: _state_message(
                        runtime.core.projection_for_token(token)
                    ),
                )
                after = runtime.core.state_version
                if report.changed:
                    if after != before + 1:
                        raise RuntimeError(
                            "自动转换必须恰好推进一个 state_version"
                        )
                    self._schedule_automatic_transition(runtime)
                elif after != before:
                    raise RuntimeError(
                        "未变更的自动转换不得推进 state_version"
                    )
        finally:
            if runtime.transition_task is current:
                runtime.transition_task = None
                runtime.transition_kind = None
                runtime.transition_version = None

    # ---------------------------------------------------------- room expiry

    def _room_has_bound_sessions(self, room_id: str) -> bool:
        return any(
            not session.revoked
            and session.room_id == room_id
            and session.token is not None
            for session in self._connections.values()
        )

    def _cancel_room_cleanup(self, runtime: _RoomRuntime) -> None:
        task = runtime.cleanup_task
        runtime.cleanup_task = None
        runtime.empty_since = None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    def _schedule_room_cleanup(self, runtime: _RoomRuntime) -> None:
        ttl = self._empty_room_ttl_seconds
        room_id = runtime.core.room_id
        if (
            ttl is None
            or self._closed
            or self._room_has_bound_sessions(room_id)
            or (runtime.cleanup_task is not None and not runtime.cleanup_task.done())
        ):
            return
        runtime.empty_since = time.monotonic()
        self._cancel_automatic_transition(runtime)
        runtime.cleanup_task = asyncio.create_task(
            self._expire_empty_room(room_id, runtime, ttl),
            name=f"room-expiry-{room_id}",
        )

    async def _expire_empty_room(
        self,
        room_id: str,
        runtime: _RoomRuntime,
        ttl: float,
    ) -> None:
        current = asyncio.current_task()
        try:
            await asyncio.sleep(ttl)
            async with runtime.lifecycle_lock:
                if (
                    self._closed
                    or self._rooms.get(room_id) is not runtime
                    or self._room_has_bound_sessions(room_id)
                ):
                    return
                runtime.core.close()
                self._cancel_automatic_transition(runtime)
                await runtime.actor.close()
                async with self._registry_lock:
                    if (
                        self._rooms.get(room_id) is runtime
                        and not self._room_has_bound_sessions(room_id)
                    ):
                        self._rooms.pop(room_id, None)
                        runtime.empty_since = None
        finally:
            if runtime.cleanup_task is current:
                runtime.cleanup_task = None

    async def _pump_actor_channel(
        self,
        session: _ConnectionSession,
        channel: OutboundChannel,
    ) -> None:
        while True:
            receive = asyncio.create_task(channel.queue.get())
            disconnected = asyncio.create_task(channel.disconnected.wait())
            try:
                done, _pending = await asyncio.wait(
                    {receive, disconnected},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if receive in done:
                    message = receive.result()
                    channel.queue.task_done()
                    if not session.emit(message):
                        return
                if disconnected in done and channel.disconnected.is_set():
                    reason = channel.disconnect_reason or "actor_disconnect"
                    code, wire_reason = {
                        "replaced": (4001, "SESSION_REPLACED"),
                        "slow_consumer": (4003, "SLOW_CONSUMER"),
                        "actor_closed": (4004, "SERVICE_SHUTDOWN"),
                    }.get(reason, (4002, "ROOM_CHANNEL_CLOSED"))
                    session.close(code, wire_reason)
                    return
            finally:
                for task in (receive, disconnected):
                    if not task.done():
                        task.cancel()
                # 一次 gather 只吸收两个子任务自身的取消结果；不能用逐项
                # ``suppress(CancelledError)``，否则可能误吞 pump 自己收到的
                # 取消，使 close 永远等不到后台 writer 退出。
                await asyncio.gather(
                    receive,
                    disconnected,
                    return_exceptions=True,
                )

    # ---------------------------------------------------------- reply/cache/rate

    def _reply_error(
        self,
        session: _ConnectionSession,
        envelope: ClientEnvelope,
        code: str,
        message: str,
    ) -> None:
        response: dict[str, object] = {
            "v": PROTOCOL_VERSION,
            "type": "error",
            "id": envelope.request_id,
            "ok": False,
            "error": {"code": code, "message": message},
        }
        if envelope.room_id is not None:
            response["room_id"] = envelope.room_id
        self._cache_and_emit(session, envelope, response)

    def _cache_and_emit(
        self,
        session: _ConnectionSession,
        envelope: ClientEnvelope,
        response: dict[str, object],
    ) -> None:
        request_id = envelope.request_id
        if request_id is not None:
            session.responses[request_id] = (
                _fingerprint(envelope),
                deepcopy(response),
            )
            session.responses.move_to_end(request_id)
            while len(session.responses) > self._response_cache_size:
                session.responses.popitem(last=False)
        session.emit(response)

    def _cached_response(
        self,
        session: _ConnectionSession,
        envelope: ClientEnvelope,
    ) -> dict[str, object] | None:
        request_id = envelope.request_id
        if request_id is None or request_id not in session.responses:
            return None
        fingerprint, response = session.responses[request_id]
        if fingerprint != _fingerprint(envelope):
            conflict: dict[str, object] = {
                "v": PROTOCOL_VERSION,
                "type": "error",
                "id": request_id,
                "ok": False,
                "error": {
                    "code": "IDEMPOTENCY_CONFLICT",
                    "message": "同一请求 id 不能用于不同命令",
                },
            }
            if envelope.room_id is not None:
                conflict["room_id"] = envelope.room_id
            return conflict
        session.responses.move_to_end(request_id)
        return deepcopy(response)

    def _check_rate(self, session: _ConnectionSession) -> None:
        now = time.monotonic()
        threshold = now - DEFAULT_RATE_WINDOW_SECONDS
        while session.command_times and session.command_times[0] <= threshold:
            session.command_times.popleft()
        if len(session.command_times) >= self._commands_per_second:
            raise ConnectionRejected("RATE_LIMITED", "请求过于频繁，请稍后重连")
        session.command_times.append(now)


def _welcome_message(
    connection_id: str,
    *,
    resumed: bool,
    room_id: str | None = None,
    seat: int | None = None,
    state_version: int | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {
        "connection_id": connection_id,
        "protocol": PROTOCOL_VERSION,
        "resumed": resumed,
        "server": "tavern-mp-v2-alpha1",
    }
    if resumed:
        body.update(
            {
                "room_id": room_id,
                "seat": seat,
                "state_version": state_version,
            }
        )
    return {"v": PROTOCOL_VERSION, "type": "welcome", "body": body}


def _credential_ack(
    envelope: ClientEnvelope,
    core: RoomCore,
    *,
    command: str,
    seat: int | None,
    token: str,
    member_id: str,
    is_host: bool,
) -> dict[str, object]:
    return {
        "v": PROTOCOL_VERSION,
        "type": "ack",
        "id": envelope.request_id,
        "room_id": core.room_id,
        "ok": True,
        "state_version": core.state_version,
        "result": {
            "command": command,
            "credential": {
                "room_id": core.room_id,
                "seat": seat,
                "resume_token": token,
                "state_version": core.state_version,
                "member_id": member_id,
                "is_host": is_host,
            },
        },
        "state": core.projection_for_token(token),
    }


def _state_message(state: dict[str, object]) -> dict[str, object]:
    return {
        "v": PROTOCOL_VERSION,
        "type": "room.state",
        "room_id": state["room"],
        "state_version": state["state_version"],
        "body": state,
    }


def _parse_create_body(body: dict[str, object]) -> tuple[str, RoomConfig]:
    expected = {
        "display_name",
        "player_count",
        "small_blind",
        "big_blind",
        "buyin",
    }
    _strict_body(body, expected)
    display_name = body["display_name"]
    if not isinstance(display_name, str):
        raise ValueError("display_name 须为字符串")
    config = RoomConfig(
        player_count=body["player_count"],  # type: ignore[arg-type]
        small_blind=body["small_blind"],  # type: ignore[arg-type]
        big_blind=body["big_blind"],  # type: ignore[arg-type]
        buyin=body["buyin"],  # type: ignore[arg-type]
    )
    return display_name, config


def _parse_join_body(body: dict[str, object]) -> str:
    _strict_body(body, {"display_name"})
    display_name = body["display_name"]
    if not isinstance(display_name, str):
        raise ValueError("display_name 须为字符串")
    return display_name


def _strict_body(body: dict[str, object], expected: set[str]) -> None:
    if set(body) != expected:
        raise ValueError("body 字段须严格为: " + ", ".join(sorted(expected)))


def _fingerprint(envelope: ClientEnvelope) -> str:
    return json.dumps(
        {
            "type": envelope.message_type,
            "room_id": envelope.room_id,
            "expected_state": envelope.expected_state,
            "body": envelope.body,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


async def _cancel(task: asyncio.Task[None] | None) -> None:
    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


__all__ = [
    "DEFAULT_AI_DELAY_MAX_SECONDS",
    "DEFAULT_AI_DELAY_MIN_SECONDS",
    "DEFAULT_COMMANDS_PER_SECOND",
    "DEFAULT_EMPTY_ROOM_TTL_SECONDS",
    "DEFAULT_MAX_ROOMS",
    "DEFAULT_RESPONSE_CACHE",
    "DEFAULT_SETTLEMENT_DELAY_SECONDS",
    "RoomRegistryBackend",
]
