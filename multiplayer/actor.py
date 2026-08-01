"""单房间的异步串行执行器与有界出站邮箱。

``RoomActor`` 是权威 ``RoomCore`` 与未来 WebSocket 传输层之间的并发
边界。所有会读取或修改房间核心的操作都经过同一个 ``asyncio.Queue``，
因此 ``RoomCore`` 本身无需持锁。传输层只持有不透明 token，座位必须由
核心解析，不能从客户端连接参数注入。

本模块不依赖 pygame 或任何 WebSocket 实现。
"""
from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Protocol, TypeAlias, TypeVar

from .protocol import ClientEnvelope

ServerMessage: TypeAlias = Mapping[str, object]
SeatProjector: TypeAlias = Callable[[int], ServerMessage]
_T = TypeVar("_T")


class RoomCoreLike(Protocol):
    """Actor 所需的最小房间核心接口。"""

    def handle(
        self,
        token: str,
        envelope: ClientEnvelope,
    ) -> object | Awaitable[object]:
        """处理一条已解析意图并返回私有响应。"""

    def seat_for_token(self, token: str) -> int | Awaitable[int]:
        """认证 token，并返回它绑定的座位。"""


class ActorStateError(RuntimeError):
    """Actor 生命周期不允许当前操作。"""


class ActorNotStartedError(ActorStateError):
    """Actor 尚未启动。"""


class ActorClosedError(ActorStateError):
    """Actor 已关闭或正在关闭。"""


class ActorAuthenticationError(ValueError):
    """核心没有把 token 解析为合法座位。"""


@dataclass(frozen=True, slots=True)
class OutboundChannel:
    """一个网络连接专属的有界出站邮箱。

    传输层应只消费 ``queue``。当 ``disconnected`` 被置位时，应关闭对应
    连接；慢消费者不会阻塞其他座位，也不会阻塞房间 actor。
    """

    connection_id: str
    seat: int
    queue: asyncio.Queue[dict[str, object]]
    disconnected: asyncio.Event = field(default_factory=asyncio.Event)
    _disconnect_reason: str | None = field(default=None, init=False, repr=False)

    @property
    def disconnect_reason(self) -> str | None:
        """断开原因；活跃连接为 ``None``。"""
        return self._disconnect_reason

    @property
    def active(self) -> bool:
        """该连接是否仍可接收新消息。"""
        return not self.disconnected.is_set()

    def _mark_disconnected(self, reason: str) -> None:
        if self.disconnected.is_set():
            return
        object.__setattr__(self, "_disconnect_reason", reason)
        self.disconnected.set()


@dataclass(frozen=True, slots=True)
class PublishReport:
    """一次广播的投递结果。"""

    delivered: tuple[str, ...]
    disconnected: tuple[str, ...]

    @property
    def delivered_count(self) -> int:
        return len(self.delivered)


@dataclass(slots=True)
class _SubmitCommand:
    token: str = field(repr=False)
    envelope: ClientEnvelope
    reply: asyncio.Future[object]


@dataclass(slots=True)
class _SubmitAndPublishCommand:
    token: str = field(repr=False)
    envelope: ClientEnvelope
    projector: SeatProjector
    reply: asyncio.Future[tuple[object, PublishReport]]


@dataclass(slots=True)
class _RegisterCommand:
    connection_id: str
    token: str = field(repr=False)
    capacity: int
    reply: asyncio.Future[OutboundChannel]


@dataclass(slots=True)
class _DisconnectCommand:
    connection_id: str
    reason: str
    reply: asyncio.Future[bool]


@dataclass(slots=True)
class _PublishCommand:
    projector: SeatProjector
    reply: asyncio.Future[PublishReport]


@dataclass(slots=True)
class _BroadcastCommand:
    message: dict[str, object]
    reply: asyncio.Future[PublishReport]


_MailboxCommand: TypeAlias = (
    _SubmitCommand
    | _SubmitAndPublishCommand
    | _RegisterCommand
    | _DisconnectCommand
    | _PublishCommand
    | _BroadcastCommand
)
_STOP = object()


class _Lifecycle(Enum):
    NEW = auto()
    RUNNING = auto()
    CLOSING = auto()
    CLOSED = auto()


class RoomActor:
    """用单一 mailbox 串行驱动一个 ``RoomCoreLike``。"""

    def __init__(self, core: RoomCoreLike, *, outbound_capacity: int = 32) -> None:
        if isinstance(outbound_capacity, bool) or not isinstance(
            outbound_capacity, int
        ):
            raise TypeError("outbound_capacity 必须是正整数")
        if outbound_capacity <= 0:
            raise ValueError("outbound_capacity 必须是正整数")
        self._core = core
        self._outbound_capacity = outbound_capacity
        self._mailbox: asyncio.Queue[_MailboxCommand | object] = asyncio.Queue()
        self._lifecycle = _Lifecycle.NEW
        self._worker: asyncio.Task[None] | None = None
        self._connections: dict[str, OutboundChannel] = {}
        self._connection_for_seat: dict[int, str] = {}

    @property
    def running(self) -> bool:
        """Actor 是否正在接受新工作。"""
        return self._lifecycle is _Lifecycle.RUNNING

    @property
    def active_connection_ids(self) -> tuple[str, ...]:
        """当前活跃连接 ID 的稳定快照。"""
        return tuple(self._connections)

    async def start(self) -> None:
        """启动 worker；重复调用是幂等的，关闭后不可重启。"""
        if self._lifecycle is _Lifecycle.RUNNING:
            return
        if self._lifecycle in {_Lifecycle.CLOSING, _Lifecycle.CLOSED}:
            raise ActorClosedError("RoomActor 已关闭，不能重新启动")
        self._lifecycle = _Lifecycle.RUNNING
        self._worker = asyncio.create_task(
            self._run(),
            name="tavern-room-actor",
        )

    async def close(self) -> None:
        """有序处理已接受命令，然后关闭全部连接与 worker。"""
        if self._lifecycle is _Lifecycle.CLOSED:
            return
        if self._lifecycle is _Lifecycle.NEW:
            self._lifecycle = _Lifecycle.CLOSED
            self._disconnect_all("actor_closed")
            return
        if self._lifecycle is _Lifecycle.CLOSING:
            worker = self._worker
            if worker is not None:
                await worker
            return

        self._lifecycle = _Lifecycle.CLOSING
        self._mailbox.put_nowait(_STOP)
        worker = self._worker
        if worker is not None:
            await worker

    async def submit(self, token: str, envelope: ClientEnvelope) -> object:
        """串行调用 ``core.handle(token, envelope)``。"""
        if not isinstance(token, str) or not token:
            raise ValueError("token 不能为空")
        reply: asyncio.Future[object] = self._new_reply()
        self._enqueue(_SubmitCommand(token, envelope, reply))
        return await reply

    async def submit_and_publish(
        self,
        token: str,
        envelope: ClientEnvelope,
        projector: SeatProjector,
    ) -> tuple[object, PublishReport]:
        """原子处理意图并广播变更后的逐座位投影。

        ``handle`` 与 ``projector`` 在同一个 mailbox 命令中连续执行，其他
        submit 不可能插到二者之间，因此不会把下一次变更误标成上一次变更
        的广播状态。
        """
        if not isinstance(token, str) or not token:
            raise ValueError("token 不能为空")
        if not callable(projector):
            raise TypeError("projector 必须可调用")
        reply: asyncio.Future[tuple[object, PublishReport]] = self._new_reply()
        self._enqueue(
            _SubmitAndPublishCommand(token, envelope, projector, reply)
        )
        return await reply

    async def register_connection(
        self,
        connection_id: str,
        token: str,
        *,
        capacity: int | None = None,
    ) -> OutboundChannel:
        """认证 token 并注册连接，不允许调用方直接提供座位。

        同一座位的新连接会把旧连接标记为 ``replaced``，符合恢复 token
        重连替换旧连接的约定。
        """
        if not isinstance(connection_id, str) or not connection_id:
            raise ValueError("connection_id 不能为空")
        if not isinstance(token, str) or not token:
            raise ValueError("token 不能为空")
        actual_capacity = self._outbound_capacity if capacity is None else capacity
        if isinstance(actual_capacity, bool) or not isinstance(actual_capacity, int):
            raise TypeError("capacity 必须是正整数")
        if actual_capacity <= 0:
            raise ValueError("capacity 必须是正整数")

        reply: asyncio.Future[OutboundChannel] = self._new_reply()
        self._enqueue(
            _RegisterCommand(connection_id, token, actual_capacity, reply)
        )
        return await reply

    async def disconnect(
        self,
        connection_id: str,
        *,
        reason: str = "client_closed",
    ) -> bool:
        """移除并标记一个连接；不存在时返回 ``False``。"""
        if not isinstance(connection_id, str) or not connection_id:
            raise ValueError("connection_id 不能为空")
        if not isinstance(reason, str) or not reason:
            raise ValueError("reason 不能为空")
        reply: asyncio.Future[bool] = self._new_reply()
        self._enqueue(_DisconnectCommand(connection_id, reason, reply))
        return await reply

    async def publish_per_seat(self, projector: SeatProjector) -> PublishReport:
        """在 actor 内为每个认证座位生成并投递独立安全投影。

        投影先全部生成，任何 projector 异常都不会造成半次广播。投递使用
        ``put_nowait``；满队列会断开慢连接并继续服务其他座位。
        """
        if not callable(projector):
            raise TypeError("projector 必须可调用")
        reply: asyncio.Future[PublishReport] = self._new_reply()
        self._enqueue(_PublishCommand(projector, reply))
        return await reply

    async def broadcast(self, message: ServerMessage) -> PublishReport:
        """向所有活跃连接投递相同的公开消息。"""
        if not isinstance(message, Mapping):
            raise TypeError("message 必须是 Mapping")
        reply: asyncio.Future[PublishReport] = self._new_reply()
        self._enqueue(_BroadcastCommand(dict(message), reply))
        return await reply

    def connection(self, connection_id: str) -> OutboundChannel | None:
        """返回活跃连接；已断开的连接不再出现在注册表。"""
        return self._connections.get(connection_id)

    def _new_reply(self) -> asyncio.Future[_T]:
        self._ensure_running()
        return asyncio.get_running_loop().create_future()

    def _enqueue(self, command: _MailboxCommand) -> None:
        # _new_reply 与 put_nowait 之间没有 await，同一事件循环内不会被 close
        # 插入，因而所有已接受命令都会排在停止哨兵之前。
        self._ensure_running()
        self._mailbox.put_nowait(command)

    def _ensure_running(self) -> None:
        if self._lifecycle is _Lifecycle.NEW:
            raise ActorNotStartedError("RoomActor 尚未启动")
        if self._lifecycle is not _Lifecycle.RUNNING:
            raise ActorClosedError("RoomActor 已关闭或正在关闭")

    async def _run(self) -> None:
        try:
            while True:
                command = await self._mailbox.get()
                try:
                    if command is _STOP:
                        return
                    if not isinstance(
                        command,
                        (
                            _SubmitCommand,
                            _SubmitAndPublishCommand,
                            _RegisterCommand,
                            _DisconnectCommand,
                            _PublishCommand,
                            _BroadcastCommand,
                        ),
                    ):
                        raise RuntimeError("RoomActor mailbox 收到未知命令")
                    await self._dispatch(command)
                finally:
                    self._mailbox.task_done()
        finally:
            self._disconnect_all("actor_closed")
            self._lifecycle = _Lifecycle.CLOSED

    async def _dispatch(self, command: _MailboxCommand) -> None:
        try:
            if isinstance(command, _SubmitCommand):
                result = await _resolve(
                    self._core.handle(command.token, command.envelope)
                )
            elif isinstance(command, _SubmitAndPublishCommand):
                handled = await _resolve(
                    self._core.handle(command.token, command.envelope)
                )
                result = (handled, self._publish_projected(command.projector))
            elif isinstance(command, _RegisterCommand):
                result = await self._register(command)
            elif isinstance(command, _DisconnectCommand):
                result = self._disconnect_now(command.connection_id, command.reason)
            elif isinstance(command, _PublishCommand):
                result = self._publish_projected(command.projector)
            elif isinstance(command, _BroadcastCommand):
                result = self._deliver(lambda _channel: command.message)
            else:  # pragma: no cover - _MailboxCommand 已穷尽
                raise RuntimeError("RoomActor 收到未知命令")
        except Exception as exc:
            if not command.reply.done():
                command.reply.set_exception(exc)
        else:
            if not command.reply.done():
                command.reply.set_result(result)

    async def _register(self, command: _RegisterCommand) -> OutboundChannel:
        seat = await _resolve(self._core.seat_for_token(command.token))
        if isinstance(seat, bool) or not isinstance(seat, int) or seat < 0:
            raise ActorAuthenticationError("token 未绑定到合法座位")

        # connection_id 重用与同座位重连都先原子断开旧连接。
        self._disconnect_now(command.connection_id, "replaced")
        previous_id = self._connection_for_seat.get(seat)
        if previous_id is not None:
            self._disconnect_now(previous_id, "replaced")

        channel = OutboundChannel(
            connection_id=command.connection_id,
            seat=seat,
            queue=asyncio.Queue(maxsize=command.capacity),
        )
        self._connections[command.connection_id] = channel
        self._connection_for_seat[seat] = command.connection_id
        return channel

    def _publish_projected(self, projector: SeatProjector) -> PublishReport:
        projections: dict[int, dict[str, object]] = {}
        for channel in self._connections.values():
            if channel.seat in projections:
                continue
            projected = projector(channel.seat)
            if not isinstance(projected, Mapping):
                raise TypeError("projector 必须返回 Mapping")
            projections[channel.seat] = dict(projected)
        return self._deliver(lambda channel: projections[channel.seat])

    def _deliver(
        self,
        message_for: Callable[[OutboundChannel], ServerMessage],
    ) -> PublishReport:
        delivered: list[str] = []
        disconnected: list[str] = []
        # 先快照，慢连接断开时可以安全修改注册表。
        channels = tuple(self._connections.values())
        for channel in channels:
            message = message_for(channel)
            try:
                channel.queue.put_nowait(dict(message))
            except asyncio.QueueFull:
                disconnected.append(channel.connection_id)
                self._disconnect_now(channel.connection_id, "slow_consumer")
            else:
                delivered.append(channel.connection_id)
        return PublishReport(tuple(delivered), tuple(disconnected))

    def _disconnect_now(self, connection_id: str, reason: str) -> bool:
        channel = self._connections.pop(connection_id, None)
        if channel is None:
            return False
        if self._connection_for_seat.get(channel.seat) == connection_id:
            del self._connection_for_seat[channel.seat]
        channel._mark_disconnected(reason)
        return True

    def _disconnect_all(self, reason: str) -> None:
        for connection_id in tuple(self._connections):
            self._disconnect_now(connection_id, reason)


async def _resolve(value: _T | Awaitable[_T]) -> _T:
    """同时兼容同步 RoomCore 与异步测试替身。"""
    if inspect.isawaitable(value):
        return await value
    return value


__all__ = [
    "ActorAuthenticationError",
    "ActorClosedError",
    "ActorNotStartedError",
    "ActorStateError",
    "OutboundChannel",
    "PublishReport",
    "RoomActor",
    "RoomCoreLike",
    "SeatProjector",
    "ServerMessage",
]
