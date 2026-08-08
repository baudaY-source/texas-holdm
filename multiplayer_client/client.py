"""供 pygame 主线程安全轮询的朋友局 WebSocket 客户端。

网络连接与 ``asyncio`` 事件循环始终归后台线程独占。pygame 主线程只提交
不含座位的意图，并读取深度冻结的最新状态或不可变事件；后台线程绝不调用
pygame，也不直接修改 scene。

恢复 token 只保存在进程内的 ``_SeatCredential``，不会进入公开快照、事件、
异常文本或对象 repr。意外断线后客户端会在服务端空房 TTL 内自动携带该凭据
恢复，并以原 UUID 重发尚未确认的房内命令。
"""
from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field, replace
import json
import math
import ssl
from threading import Event as ThreadEvent
from threading import Lock, Thread, current_thread
from types import MappingProxyType
from typing import Any, Final, TypeAlias
from urllib.parse import urlsplit
from uuid import uuid4

try:  # 离线单机安装不强制携带可选 WebSocket 客户端依赖。
    from websockets.asyncio.client import ClientConnection, connect as _ws_connect
    from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus
except ImportError:  # pragma: no cover - 在无联机依赖的离线发行环境触发
    ClientConnection = Any  # type: ignore[misc,assignment]
    _ws_connect = None

    class ConnectionClosed(Exception):
        """缺少 websockets 时仅用于保持异常分支类型完整。"""

    class InvalidHandshake(Exception):
        """缺少 websockets 时的握手异常占位。"""

    class InvalidStatus(InvalidHandshake):
        """缺少 websockets 时的 HTTP 状态异常占位。"""

from multiplayer.protocol import (
    MAX_CLIENT_MESSAGE_BYTES,
    MAX_WIRE_INTEGER,
    PROTOCOL_VERSION,
)
from multiplayer.projection import PROJECTION_SCHEMA
from multiplayer.room import ROOM_STATE_SCHEMA

_LOCAL_HOSTS: Final = frozenset({"127.0.0.1", "localhost", "::1"})
_ROOM_COMMANDS: Final = frozenset(
    {
        "seat.claim",
        "seat.release",
        "seat.topup.request",
        "seat.topup.cancel",
        "seat.topup.decline",
        "seat.topup.approve",
        "seat.topup.reject",
        "room.ai.add",
        "room.ai.remove",
        "room.ai.rebuy",
        "room.ai.style",
        "room.pause",
        "room.resume",
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
_ACTION_KINDS: Final = frozenset(
    {"fold", "check", "call", "bet", "raise", "allin"}
)
_ROOM_PHASES: Final = frozenset({"LOBBY", "PLAYING", "BETWEEN_HANDS", "CLOSED"})
_TOKEN_CHARS: Final = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)
_ROOM_CHARS: Final = _TOKEN_CHARS
_ERROR_CODE_CHARS: Final = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
_EVENT_LIMIT: Final = 256
_THREAD_READY_TIMEOUT: Final = 2.0
_THREAD_ABORT_JOIN_TIMEOUT: Final = 1.0

FrozenJSONScalar: TypeAlias = None | bool | int | float | str
FrozenJSON: TypeAlias = FrozenJSONScalar | tuple["FrozenJSON", ...] | Mapping[str, "FrozenJSON"]


class ClientUsageError(RuntimeError):
    """调用方在不允许的客户端状态下提交了操作。"""


class ClientBusyError(ClientUsageError):
    """已有一条本地变更命令等待服务器确认。"""


class _WireError(RuntimeError):
    """远端消息不符合本客户端可安全消费的协议契约。"""


class _FatalConnection(RuntimeError):
    """已取得稳定错误、且不应自动重连的连接终止。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ClientErrorInfo:
    """可直接显示的稳定错误，不包含 URL、token 或原始异常文本。"""

    code: str
    message: str


@dataclass(frozen=True, slots=True)
class ClientEvent:
    """主线程可轮询的瞬时事件。"""

    sequence: int
    kind: str
    request_id: str | None = None
    command: str | None = None
    code: str | None = None
    message: str | None = None
    state_version: int | None = None
    state: Mapping[str, FrozenJSON] | None = None

    @property
    def type(self) -> str:
        """兼容按 ``type`` 分派事件的 UI 调用方。"""
        return self.kind


@dataclass(frozen=True, slots=True)
class ClientSnapshot:
    """跨线程发布的不可变客户端快照。"""

    revision: int = 0
    status: str = "idle"
    room_id: str | None = None
    seat: int | None = None
    member_id: str | None = None
    is_host: bool = False
    state_version: int | None = None
    state: Mapping[str, FrozenJSON] | None = None
    pending_request_id: str | None = None
    pending_command: str | None = None
    last_error: ClientErrorInfo | None = None


@dataclass(frozen=True, slots=True)
class _SeatCredential:
    room_id: str
    seat: int | None
    resume_token: str = field(repr=False)
    member_id: str = ""
    is_host: bool = False


@dataclass(frozen=True, slots=True)
class _OutboundCommand:
    request_id: str
    message_type: str
    body: dict[str, object] = field(repr=False)
    room_id: str | None = None
    expected_state: int | None = None
    wire: str = field(default="", repr=False)

    def envelope(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "v": PROTOCOL_VERSION,
            "type": self.message_type,
            "id": self.request_id,
            "body": dict(self.body),
        }
        if self.room_id is not None:
            payload["room_id"] = self.room_id
        if self.expected_state is not None:
            payload["expected_state"] = self.expected_state
        return payload


class DesktopMultiplayerClient:
    """一个可由 pygame 主循环无阻塞驱动的朋友局客户端。"""

    def __init__(self) -> None:
        self._lock = Lock()
        self._snapshot = ClientSnapshot()
        self._events: deque[ClientEvent] = deque()
        self._event_sequence = 0
        self._credential: _SeatCredential | None = None

        self._thread: Thread | None = None
        self._thread_ready = ThreadEvent()
        self._abort_requested = ThreadEvent()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._commands: asyncio.Queue[_OutboundCommand] | None = None
        self._stop: asyncio.Event | None = None
        self._runner_task: asyncio.Task[None] | None = None
        self._inflight: _OutboundCommand | None = None
        self._inflight_wire: str | None = None

    # ---------------------------------------------------------- public view

    @property
    def room_id(self) -> str | None:
        return self.snapshot().room_id

    @property
    def seat(self) -> int | None:
        return self.snapshot().seat

    @property
    def member_id(self) -> str | None:
        return self.snapshot().member_id

    @property
    def is_host(self) -> bool:
        return self.snapshot().is_host

    @property
    def latest_state(self) -> Mapping[str, FrozenJSON] | None:
        return self.snapshot().state

    def snapshot(self) -> ClientSnapshot:
        """返回无需复制即可跨线程读取的深度不可变快照。"""
        with self._lock:
            return self._snapshot

    def poll(self) -> tuple[ClientEvent, ...]:
        """取走当前所有瞬时事件；权威房间数据始终从 ``latest_state`` 读取。"""
        with self._lock:
            events = tuple(self._events)
            self._events.clear()
            return events

    # ---------------------------------------------------------- lifecycle

    def start(self, url: str) -> None:
        """启动后台网络线程；该调用不等待网络握手完成。"""
        safe_url = _validate_url(url)
        with self._lock:
            if self._thread is not None:
                raise ClientUsageError("客户端已经启动")
            previous = self._snapshot
            # 这是新的连接世代；旧世代未被 UI 消费的 closed/error/ack
            # 不能在新页面中重新出现。
            self._events.clear()
            self._snapshot = replace(
                previous,
                revision=previous.revision + 1,
                status="connecting",
                pending_request_id=None,
                pending_command=None,
                last_error=None,
            )
            self._thread_ready.clear()
            self._abort_requested.clear()
            thread = Thread(
                target=self._thread_main,
                args=(safe_url,),
                name="tavern-multiplayer-client",
                # 正常路径仍由 close/join 精确收口；daemon 只防解释器被极端
                # OS 级线程启动卡死拖住，不能替代显式关闭。
                daemon=True,
            )
            self._thread = thread
            try:
                thread.start()
            except Exception:
                self._thread = None
                self._snapshot = replace(
                    previous,
                    revision=previous.revision + 1,
                    status="failed",
                    pending_request_id=None,
                    pending_command=None,
                    last_error=ClientErrorInfo(
                        "CLIENT_START_FAILED",
                        "联机线程无法启动",
                    ),
                )
                raise ClientUsageError("联机线程无法启动") from None
        if not self._thread_ready.wait(timeout=_THREAD_READY_TIMEOUT):
            self._publish_error("CLIENT_START_TIMEOUT", "联机线程启动超时")
            self._set_status("failed")
            self._abort_requested.set()
            loop = self._loop
            if loop is not None:
                with suppress(RuntimeError):
                    loop.call_soon_threadsafe(self._stop_runner)
            thread.join(_THREAD_ABORT_JOIN_TIMEOUT)
            raise ClientUsageError("联机线程启动超时")

    def close(self, timeout: float = 3.0) -> bool:
        """幂等安全关闭并有限等待后台线程；不会发送房内离席命令。"""
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("timeout 须为正数")
        with self._lock:
            thread = self._thread
            already_closed = thread is None and self._snapshot.status == "closed"
        if already_closed:
            return True
        if thread is None:
            self._set_status("closed")
            return True
        if thread is current_thread():
            raise ClientUsageError("不能从联机线程等待自身关闭")
        if not thread.is_alive():
            self._set_status("closed")
            return True
        self._set_status("closing")
        loop = self._loop
        stop = self._stop
        if loop is not None and stop is not None:
            try:
                loop.call_soon_threadsafe(self._stop_runner)
            except RuntimeError:
                pass
        thread.join(float(timeout))
        if thread.is_alive():
            self._publish_error("CLIENT_CLOSE_TIMEOUT", "联机连接仍在关闭中")
            return False
        return True

    # ---------------------------------------------------------- commands

    def create_room(
        self,
        display_name: str,
        *,
        player_count: int = 2,
        small_blind: int = 5,
        big_blind: int = 10,
        buyin: int = 1000,
    ) -> str:
        """以当前未绑定连接创建房间；房主先以未入座成员进入。"""
        return self._submit_unbound(
            "room.create",
            {
                "display_name": display_name,
                "player_count": player_count,
                "small_blind": small_blind,
                "big_blind": big_blind,
                "buyin": buyin,
            },
        )

    def join_room(self, room_id: str, display_name: str) -> str:
        """加入既有房间；主要用于桌面间交叉验证。"""
        safe_room = _validate_room_id(room_id)
        return self._submit_unbound(
            "room.join",
            {"display_name": display_name},
            room_id=safe_room,
        )

    def send_command(
        self,
        message_type: str,
        body: Mapping[str, object] | None = None,
        *,
        expected_state: int | None = None,
    ) -> str:
        """提交一条房内命令，自动补 UUID、房间码与状态版本。"""
        if message_type not in _ROOM_COMMANDS:
            raise ClientUsageError("未知或不允许的房内命令")
        if body is not None and not isinstance(body, Mapping):
            raise ClientUsageError("命令 body 必须是对象")
        snapshot = self.snapshot()
        if snapshot.status != "connected" or snapshot.room_id is None:
            raise ClientUsageError("客户端尚未进入房间")
        version = snapshot.state_version if expected_state is None else expected_state
        if type(version) is not int or not 0 <= version <= MAX_WIRE_INTEGER:
            raise ClientUsageError("尚无可用于命令的状态版本")
        return self._enqueue(
            _OutboundCommand(
                request_id=str(uuid4()),
                message_type=message_type,
                body={} if body is None else dict(body),
                room_id=snapshot.room_id,
                expected_state=version,
            )
        )

    def set_ready(self, ready: bool, *, expected_state: int | None = None) -> str:
        if not isinstance(ready, bool):
            raise ValueError("ready 须为布尔值")
        return self.send_command(
            "room.ready", {"ready": ready}, expected_state=expected_state
        )

    def claim_seat(
        self,
        target_seat: int,
        buyin: int,
        *,
        expected_state: int | None = None,
    ) -> str:
        if type(target_seat) is not int or not 0 <= target_seat <= 8:
            raise ValueError("target_seat 须为 0–8")
        if type(buyin) is not int or not 0 < buyin <= MAX_WIRE_INTEGER:
            raise ValueError("buyin 须为正整数筹码")
        return self.send_command(
            "seat.claim",
            {"target_seat": target_seat, "buyin": buyin},
            expected_state=expected_state,
        )

    def release_seat(self, *, expected_state: int | None = None) -> str:
        return self.send_command("seat.release", expected_state=expected_state)

    def request_topup(
        self,
        target_stack: int,
        *,
        expected_state: int | None = None,
    ) -> str:
        if type(target_stack) is not int or not 0 < target_stack <= MAX_WIRE_INTEGER:
            raise ValueError("target_stack 须为正整数筹码")
        return self.send_command(
            "seat.topup.request",
            {"target_stack": target_stack},
            expected_state=expected_state,
        )

    def pause_room(self, paused: bool, *, expected_state: int | None = None) -> str:
        if type(paused) is not bool:
            raise ValueError("paused 须为布尔值")
        return self.send_command(
            "room.pause" if paused else "room.resume",
            expected_state=expected_state,
        )

    def start_game(self, *, expected_state: int | None = None) -> str:
        return self.send_command("room.start", expected_state=expected_state)

    def act(
        self,
        kind: str,
        *,
        to: int | None = None,
        expected_state: int | None = None,
    ) -> str:
        if kind not in _ACTION_KINDS:
            raise ValueError("未知动作 kind")
        body: dict[str, object] = {"kind": kind}
        if kind in {"bet", "raise"}:
            if type(to) is not int or not 0 < to <= MAX_WIRE_INTEGER:
                raise ValueError("bet/raise 必须提供正整数 to")
            body["to"] = to
        elif to is not None:
            raise ValueError("该动作不能提供 to")
        return self.send_command(
            "game.action", body, expected_state=expected_state
        )

    def show_cards(self, *, expected_state: int | None = None) -> str:
        return self.send_command("game.show", expected_state=expected_state)

    def next_hand(self, *, expected_state: int | None = None) -> str:
        return self.send_command("game.next_hand", expected_state=expected_state)

    def rebuy(self, amount: int, *, expected_state: int | None = None) -> str:
        if type(amount) is not int or not 0 < amount <= MAX_WIRE_INTEGER:
            raise ValueError("amount 须为正整数筹码")
        return self.send_command(
            "seat.rebuy", {"amount": amount}, expected_state=expected_state
        )

    def leave_room(self, *, expected_state: int | None = None) -> str:
        return self.send_command("room.leave", expected_state=expected_state)

    def leave_seat(self, *, expected_state: int | None = None) -> str:
        return self.send_command("seat.leave", expected_state=expected_state)

    # ---------------------------------------------------------- thread bridge

    def _submit_unbound(
        self,
        message_type: str,
        body: dict[str, object],
        *,
        room_id: str | None = None,
    ) -> str:
        snapshot = self.snapshot()
        if snapshot.status != "connected" or snapshot.room_id is not None:
            raise ClientUsageError("当前连接不能创建或加入房间")
        return self._enqueue(
            _OutboundCommand(
                request_id=str(uuid4()),
                message_type=message_type,
                body=dict(body),
                room_id=room_id,
            )
        )

    def _enqueue(self, command: _OutboundCommand) -> str:
        # 在占用唯一 pending 槽之前同步验证可编码性；调用方错误不能令
        # 后台线程失败或留下永远无法确认的 pending。
        command = replace(command, wire=_encode(command.envelope()))
        with self._lock:
            snapshot = self._snapshot
            if snapshot.pending_request_id is not None:
                raise ClientBusyError("上一条命令仍在等待服务器确认")
            if snapshot.status != "connected":
                raise ClientUsageError("联机连接当前不可提交命令")
            loop = self._loop
            queue = self._commands
            if loop is None or queue is None or not loop.is_running():
                raise ClientUsageError("联机线程当前不可用")
            self._snapshot = replace(
                snapshot,
                revision=snapshot.revision + 1,
                pending_request_id=command.request_id,
                pending_command=command.message_type,
                last_error=None,
            )
        try:
            loop.call_soon_threadsafe(queue.put_nowait, command)
        except RuntimeError as exc:
            self._finish_pending(command)
            raise ClientUsageError("联机线程已经停止") from exc
        return command.request_id

    def _thread_main(self, url: str) -> None:
        try:
            asyncio.run(self._run(url))
        except asyncio.CancelledError:
            pass
        except Exception:
            self._publish_error("CLIENT_INTERNAL_ERROR", "联机客户端内部错误")
            self._set_status("failed")
        finally:
            self._loop = None
            self._commands = None
            self._stop = None
            self._runner_task = None
            self._inflight = None
            self._inflight_wire = None
            self._thread_ready.set()
            self._clear_any_pending()
            if self.snapshot().status not in {"failed", "closed"}:
                self._set_status("closed")
            self._publish_event("closed")
            with self._lock:
                if self._thread is current_thread():
                    self._thread = None

    async def _run(self, url: str) -> None:
        self._loop = asyncio.get_running_loop()
        self._commands = asyncio.Queue(maxsize=1)
        self._stop = asyncio.Event()
        self._runner_task = asyncio.current_task()
        self._thread_ready.set()
        if self._abort_requested.is_set():
            return
        if _ws_connect is None:
            self._publish_error(
                "CLIENT_DEPENDENCY_MISSING",
                "当前安装未包含朋友联机客户端组件",
            )
            self._set_status("failed")
            return
        delay = 0.25
        ever_connected = False

        while not self._stop.is_set():
            resuming = self._credential is not None
            self._set_status("reconnecting" if ever_connected else "connecting")
            try:
                async with _ws_connect(
                    url,
                    proxy=None,
                    compression=None,
                    open_timeout=8.0,
                    close_timeout=2.0,
                    ping_interval=20.0,
                    ping_timeout=20.0,
                    max_size=MAX_CLIENT_MESSAGE_BYTES,
                ) as websocket:
                    await self._handshake(websocket, resuming=resuming)
                    ever_connected = True
                    delay = 0.25
                    self._set_status("connected", clear_error=True)
                    self._publish_event("reconnected" if resuming else "connected")
                    await self._connected_loop(websocket)
                    if self._stop.is_set():
                        return
            except asyncio.CancelledError:
                raise
            except _FatalConnection as exc:
                self._fail_session(exc.code, exc.message)
                return
            except _WireError:
                self._fail_session(
                    "INVALID_SERVER_MESSAGE",
                    "服务器返回了不符合协议的消息",
                )
                return
            except (InvalidStatus, InvalidHandshake, ssl.SSLError):
                self._fail_session(
                    "HANDSHAKE_REJECTED",
                    "服务器拒绝 WebSocket 握手",
                )
                return
            except ConnectionClosed as exc:
                if self._stop.is_set():
                    return
                fatal = _fatal_close(exc)
                if fatal is not None:
                    code, message = fatal
                    self._fail_session(code, message)
                    return
                if not await self._prepare_reconnect(delay):
                    return
                delay = min(delay * 2.0, 5.0)
            except (OSError, TimeoutError):
                if self._stop.is_set():
                    return
                if not await self._prepare_reconnect(delay):
                    return
                delay = min(delay * 2.0, 5.0)
    def _fail_session(self, code: str, message: str) -> None:
        """发布终止语义并销毁旧凭据，防止显式重启反向抢占会话。"""
        command = self._inflight
        if (
            command is not None
            and command.message_type in {"room.leave", "seat.leave"}
            and code in {"AUTH_FAILED", "POLICY_REJECTED"}
        ):
            code = "LEAVE_OUTCOME_UNKNOWN"
            message = "离席可能已成功，原会话不可恢复；请重新加入房间"
        self._publish_error(code, message, command=command)
        if command is not None:
            self._finish_pending(command)
        self._inflight = None
        self._inflight_wire = None
        self._credential = None
        self._clear_room()
        self._set_status("failed")

    async def _prepare_reconnect(self, delay: float) -> bool:
        """处理可恢复网络中断；未取得凭据的未知结果必须终止。"""
        stop = self._stop
        if stop is None or stop.is_set():
            return False
        if self._credential is None and self._inflight is not None:
            self._publish_error(
                "COMMAND_OUTCOME_UNKNOWN",
                "创建或加入结果未确认，请停止本轮服务后重试",
                command=self._inflight,
            )
            self._finish_pending(self._inflight)
            self._inflight = None
            self._inflight_wire = None
            self._set_status("failed")
            return False
        self._publish_event(
            "disconnected",
            code="CONNECTION_LOST",
            message="连接中断，正在尝试恢复",
        )
        self._set_status("reconnecting")
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except TimeoutError:
            return True
        return False

    async def _handshake(
        self,
        websocket: ClientConnection,
        *,
        resuming: bool,
    ) -> None:
        body: dict[str, object] = {
            "client": "windows-pygame",
            "client_version": "mp-v2-alpha1",
        }
        credential = self._credential
        if resuming:
            if credential is None:  # pragma: no cover - defensive invariant
                raise _WireError("missing credential")
            body["resume"] = {
                "room_id": credential.room_id,
                "token": credential.resume_token,
            }
        await websocket.send(_encode({"v": PROTOCOL_VERSION, "type": "hello", "body": body}))
        welcome = await asyncio.wait_for(_receive(websocket), timeout=8.0)
        if welcome.get("v") == PROTOCOL_VERSION and welcome.get("type") == "error":
            code, message = _transport_error(welcome)
            raise _FatalConnection(code, message)
        if welcome.get("v") != PROTOCOL_VERSION or welcome.get("type") != "welcome":
            raise _WireError("welcome required")
        welcome_body = welcome.get("body")
        if not isinstance(welcome_body, dict):
            raise _WireError("welcome body")
        if (
            welcome_body.get("protocol") != PROTOCOL_VERSION
            or type(welcome_body.get("resumed")) is not bool
            or welcome_body.get("resumed") is not resuming
        ):
            raise _WireError("welcome mismatch")
        if resuming:
            assert credential is not None
            welcome_version = welcome_body.get("state_version")
            if (
                welcome_body.get("room_id") != credential.room_id
                or welcome_body.get("member_id") not in {None, credential.member_id}
                or type(welcome_version) is not int
                or not 0 <= welcome_version <= MAX_WIRE_INTEGER
            ):
                raise _WireError("resume mismatch")
            local_version = self.snapshot().state_version
            if local_version is not None and welcome_version < local_version:
                raise _WireError("resume version rollback")
            state_message = await asyncio.wait_for(_receive(websocket), timeout=8.0)
            self._consume_state_message(
                state_message,
                expected_version=welcome_version,
            )

    async def _connected_loop(self, websocket: ClientConnection) -> None:
        stop = self._stop
        commands = self._commands
        if stop is None or commands is None:  # pragma: no cover
            raise RuntimeError("thread bridge unavailable")

        if self._inflight_wire is not None:
            await websocket.send(self._inflight_wire)

        receive_task = asyncio.create_task(websocket.recv())
        stop_task = asyncio.create_task(stop.wait())
        command_task: asyncio.Task[_OutboundCommand] | None = None
        if self._inflight is None:
            command_task = asyncio.create_task(commands.get())
        try:
            while True:
                watched: set[asyncio.Task] = {receive_task, stop_task}
                if command_task is not None:
                    watched.add(command_task)
                done, _ = await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)
                if stop_task in done and stop.is_set():
                    await websocket.close(code=1000, reason="CLIENT_STOPPED")
                    return
                if command_task is not None and command_task in done:
                    command = command_task.result()
                    commands.task_done()
                    self._inflight = command
                    if not command.wire:  # pragma: no cover - _enqueue 契约
                        raise RuntimeError("command wire missing")
                    self._inflight_wire = command.wire
                    await websocket.send(self._inflight_wire)
                    command_task = None
                if receive_task in done:
                    raw = receive_task.result()
                    message = _decode(raw)
                    self._consume_message(message)
                    receive_task = asyncio.create_task(websocket.recv())
                    if self._inflight is None and command_task is None:
                        command_task = asyncio.create_task(commands.get())
        finally:
            for task in (receive_task, stop_task, command_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (receive_task, stop_task, command_task) if task is not None),
                return_exceptions=True,
            )

    # ---------------------------------------------------------- incoming wire

    def _consume_message(self, message: dict[str, object]) -> None:
        if message.get("v") != PROTOCOL_VERSION or not isinstance(message.get("type"), str):
            raise _WireError("bad envelope")
        message_type = message["type"]
        if message_type == "room.state":
            self._consume_state_message(message)
            return
        if message_type == "pong":
            return
        if message_type not in {"ack", "error"}:
            raise _WireError("unexpected message type")
        inflight = self._inflight
        request_id = message.get("id")
        if inflight is None or request_id != inflight.request_id:
            # id-less transport errors are safe to surface; every request response
            # otherwise must correlate with the sole in-flight command.
            if message_type == "error" and request_id is None:
                code, text = _transport_error(message)
                raise _FatalConnection(code, text)
            raise _WireError("uncorrelated reply")
        if message_type == "error":
            code, text = _business_error(message)
            state = message.get("state")
            if state is not None:
                if not isinstance(state, dict):
                    raise _WireError("error state")
                self._install_state(state)
            self._publish_error(code, text, command=inflight)
            self._finish_pending(inflight)
            self._inflight = None
            self._inflight_wire = None
            return
        self._consume_ack(message, inflight)

    def _consume_ack(
        self,
        message: dict[str, object],
        command: _OutboundCommand,
    ) -> None:
        if message.get("ok") is not True:
            raise _WireError("ack ok")
        result = message.get("result")
        state = message.get("state")
        version = message.get("state_version")
        if (
            not isinstance(result, dict)
            or result.get("command") != command.message_type
            or not isinstance(state, dict)
            or type(version) is not int
            or not 0 <= version <= MAX_WIRE_INTEGER
        ):
            raise _WireError("ack fields")

        if command.message_type in {"room.create", "room.join"}:
            credential_raw = result.get("credential")
            if not isinstance(credential_raw, dict):
                raise _WireError("credential")
            room_id = _validate_room_id_wire(credential_raw.get("room_id"))
            seat = credential_raw.get("seat")
            token = credential_raw.get("resume_token")
            member_id = credential_raw.get("member_id")
            is_host = credential_raw.get("is_host")
            if (
                (seat is not None and (type(seat) is not int or seat < 0))
                or not isinstance(token, str)
                or not 32 <= len(token) <= 128
                or any(char not in _TOKEN_CHARS for char in token)
                or not isinstance(member_id, str)
                or len(member_id) != 32
                or any(char not in "0123456789abcdef" for char in member_id)
                or type(is_host) is not bool
                or message.get("room_id") != room_id
            ):
                raise _WireError("credential fields")
            self._credential = _SeatCredential(
                room_id,
                seat,
                token,
                member_id=member_id,
                is_host=is_host,
            )
        else:
            credential = self._credential
            if credential is None or message.get("room_id") != credential.room_id:
                raise _WireError("ack room")

        if state.get("state_version") != version:
            raise _WireError("ack version")
        if command.message_type == "room.leave":
            credential = self._credential
            if (
                credential is None
                or result.get("left_room") is not True
            ):
                raise _WireError("leave result")
            _validate_departed_room_state(state, credential)
            self._publish_event(
                "ack",
                request_id=command.request_id,
                command=command.message_type,
                state_version=version,
            )
            self._finish_pending(command)
            self._inflight = None
            self._inflight_wire = None
            self._credential = None
            self._clear_room()
            return
        # 恢复后可能先收到比缓存 ACK 更新的完整 room.state。旧 ACK 仍需
        # 通过 envelope/state 自洽与个人视角校验，但绝不能令本地状态倒退。
        self._install_state(state)
        self._publish_event(
            "ack",
            request_id=command.request_id,
            command=command.message_type,
            state_version=version,
        )
        self._finish_pending(command)
        self._inflight = None
        self._inflight_wire = None

    def _consume_state_message(
        self,
        message: dict[str, object],
        *,
        expected_version: int | None = None,
    ) -> int:
        if (
            message.get("v") != PROTOCOL_VERSION
            or message.get("type") != "room.state"
            or type(message.get("state_version")) is not int
            or not isinstance(message.get("body"), dict)
        ):
            raise _WireError("room.state envelope")
        state = message["body"]
        assert isinstance(state, dict)
        if (
            message.get("room_id") != state.get("room")
            or message.get("state_version") != state.get("state_version")
        ):
            raise _WireError("room.state mismatch")
        if expected_version is not None and state.get("state_version") != expected_version:
            raise _WireError("resume welcome/state mismatch")
        self._install_state(state)
        version = state["state_version"]
        assert type(version) is int
        return version

    def _install_state(self, raw: dict[str, object]) -> None:
        credential = self._credential
        if credential is None:
            raise _WireError("state before credential")
        _validate_room_state(raw, credential)
        version = raw["state_version"]
        assert type(version) is int
        frozen = _freeze_mapping(raw)
        with self._lock:
            current = self._snapshot
            if current.state_version is not None and version < current.state_version:
                return
            if (
                current.state_version == version
                and current.state is not None
                and current.state != frozen
            ):
                raise _WireError("same version changed")
            self._snapshot = replace(
                current,
                revision=current.revision + 1,
                room_id=credential.room_id,
                seat=raw.get("viewer_seat") if type(raw.get("viewer_seat")) is int else None,
                member_id=credential.member_id or None,
                is_host=credential.is_host,
                state_version=version,
                state=frozen,
            )
        self._publish_event("state", state_version=version, state=frozen)

    # ---------------------------------------------------------- publication

    def _stop_runner(self) -> None:
        """只在网络事件循环内执行的关闭唤醒。"""
        stop = self._stop
        if stop is not None:
            stop.set()
        runner = self._runner_task
        if runner is not None and not runner.done():
            runner.cancel()

    def _set_status(self, status: str, *, clear_error: bool = False) -> None:
        with self._lock:
            current = self._snapshot
            next_error = None if clear_error else current.last_error
            if current.status == status and current.last_error == next_error:
                return
            self._snapshot = replace(
                current,
                revision=current.revision + 1,
                status=status,
                last_error=next_error,
            )

    def _clear_room(self) -> None:
        with self._lock:
            current = self._snapshot
            self._snapshot = replace(
                current,
                revision=current.revision + 1,
                room_id=None,
                seat=None,
                member_id=None,
                is_host=False,
                state_version=None,
                state=None,
            )

    def _finish_pending(self, command: _OutboundCommand) -> None:
        with self._lock:
            current = self._snapshot
            if current.pending_request_id != command.request_id:
                return
            self._snapshot = replace(
                current,
                revision=current.revision + 1,
                pending_request_id=None,
                pending_command=None,
            )

    def _clear_any_pending(self) -> None:
        with self._lock:
            current = self._snapshot
            if current.pending_request_id is None:
                return
            self._snapshot = replace(
                current,
                revision=current.revision + 1,
                pending_request_id=None,
                pending_command=None,
            )

    def _publish_error(
        self,
        code: str,
        message: str,
        *,
        command: _OutboundCommand | None = None,
    ) -> None:
        credential = self._credential
        token = credential.resume_token if credential is not None else None
        safe = ClientErrorInfo(
            _safe_error_code(code),
            _safe_error_message(message, secret=token),
        )
        with self._lock:
            current = self._snapshot
            self._snapshot = replace(
                current,
                revision=current.revision + 1,
                last_error=safe,
            )
        self._publish_event(
            "error",
            request_id=command.request_id if command is not None else None,
            command=command.message_type if command is not None else None,
            code=safe.code,
            message=safe.message,
            state_version=self.snapshot().state_version,
        )

    def _publish_event(
        self,
        kind: str,
        *,
        request_id: str | None = None,
        command: str | None = None,
        code: str | None = None,
        message: str | None = None,
        state_version: int | None = None,
        state: Mapping[str, FrozenJSON] | None = None,
    ) -> None:
        with self._lock:
            self._event_sequence += 1
            event = ClientEvent(
                sequence=self._event_sequence,
                kind=kind,
                request_id=request_id,
                command=command,
                code=code,
                message=message,
                state_version=state_version,
                state=state,
            )
            if len(self._events) >= _EVENT_LIMIT:
                # 权威状态另存于 snapshot；优先丢弃最旧的瞬时 state 通知。
                drop = next(
                    (index for index, item in enumerate(self._events) if item.kind == "state"),
                    0,
                )
                del self._events[drop]
            self._events.append(event)


def _encode(message: Mapping[str, object]) -> str:
    _validate_outgoing_json(message)
    try:
        encoded = json.dumps(
            dict(message),
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ClientUsageError("命令无法编码为 JSON") from exc
    try:
        size = len(encoded.encode("utf-8", errors="strict"))
    except UnicodeError as exc:
        raise ClientUsageError("命令包含非法 Unicode") from exc
    if size > MAX_CLIENT_MESSAGE_BYTES:
        raise ClientUsageError("命令超过 64 KiB")
    return encoded


async def _receive(websocket: ClientConnection) -> dict[str, object]:
    return _decode(await websocket.recv())


def _decode(raw: str | bytes) -> dict[str, object]:
    if not isinstance(raw, str):
        raise _WireError("binary server message")
    try:
        if len(raw.encode("utf-8", errors="strict")) > MAX_CLIENT_MESSAGE_BYTES:
            raise _WireError("server message too large")
        decoded = json.loads(
            raw,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
            parse_int=_parse_wire_int,
            parse_float=_parse_wire_float,
        )
    except _WireError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise _WireError("invalid server json") from exc
    if not isinstance(decoded, dict):
        raise _WireError("server message root")
    return decoded


def _validate_url(url: object) -> str:
    if not isinstance(url, str):
        raise ValueError("联机地址须为字符串")
    value = url.strip()
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("联机地址格式非法") from exc
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.query
        or "?" in value
    ):
        raise ValueError("联机地址不能包含用户信息、query 或 fragment")
    if parsed.scheme == "wss" and parsed.hostname and parsed.path:
        return value
    if (
        parsed.scheme == "ws"
        and parsed.hostname in _LOCAL_HOSTS
        and parsed.path
    ):
        return value
    raise ValueError("非本机地址必须使用带有效路径的 wss://")


def _validate_room_id(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("room_id 须为字符串")
    room_id = value.strip().upper()
    if not 1 <= len(room_id) <= 32 or any(char not in _ROOM_CHARS for char in room_id):
        raise ValueError("room_id 格式非法")
    return room_id


def _validate_room_id_wire(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 32
        or any(char not in _ROOM_CHARS for char in value)
    ):
        raise _WireError("room id")
    return value


def _validate_room_state(raw: dict[str, object], credential: _SeatCredential) -> None:
    version = raw.get("state_version")
    viewer_seat = raw.get("viewer_seat")
    if (
        raw.get("schema") != ROOM_STATE_SCHEMA
        or raw.get("protocol") != PROTOCOL_VERSION
        or raw.get("room") != credential.room_id
        or type(version) is not int
        or not 0 <= version <= MAX_WIRE_INTEGER
        or (viewer_seat is not None and type(viewer_seat) is not int)
        or raw.get("phase") not in _ROOM_PHASES
    ):
        raise _WireError("room state header")
    config = raw.get("config")
    seats = raw.get("seats")
    if not isinstance(config, dict) or type(config.get("player_count")) is not int:
        raise _WireError("room config")
    player_count = config["player_count"]
    assert type(player_count) is int
    if not 2 <= player_count <= 9 or not isinstance(seats, list) or len(seats) != player_count:
        raise _WireError("room seats")
    seat_ids: list[int] = []
    for seat in seats:
        if (
            not isinstance(seat, dict)
            or type(seat.get("seat")) is not int
            or type(seat.get("occupied")) is not bool
        ):
            raise _WireError("room seat")
        seat_id = seat["seat"]
        assert type(seat_id) is int
        seat_ids.append(seat_id)
    if sorted(seat_ids) != list(range(player_count)):
        raise _WireError("room seat ids")

    # v2 凭据绑定成员身份而非座位；动态认领/换位后只以本人的成员投影
    # 核对 viewer_seat。空 member_id 仅保留给旧单元夹具的内部兼容路径。
    members = raw.get("members")
    viewer_member: dict[str, object] | None = None
    if credential.member_id:
        if (
            raw.get("viewer_member_id") != credential.member_id
            or type(raw.get("viewer_is_host")) is not bool
            or raw.get("viewer_is_host") is not credential.is_host
            or not isinstance(members, list)
        ):
            raise _WireError("room member header")
        viewer_member = next(
            (
                member
                for member in members
                if isinstance(member, dict)
                and member.get("member_id") == credential.member_id
            ),
            None,
        )
        if (
            viewer_member is None
            or viewer_member.get("seat") != viewer_seat
            or viewer_member.get("is_host") is not credential.is_host
        ):
            raise _WireError("viewer member")
    elif viewer_seat != credential.seat:
        raise _WireError("legacy viewer seat")

    if viewer_seat is not None and not 0 <= viewer_seat < player_count:
        raise _WireError("viewer seat")
    viewer_waiting = False
    if viewer_member is not None and viewer_seat is not None:
        viewer_room_seat = next(
            (
                seat
                for seat in seats
                if isinstance(seat, dict)
                and seat.get("seat") == viewer_seat
                and seat.get("member_id") == credential.member_id
            ),
            None,
        )
        member_waiting = viewer_member.get("waiting_next_hand") is True
        seat_waiting = (
            viewer_room_seat is not None
            and viewer_room_seat.get("waiting_next_hand") is True
        )
        if viewer_room_seat is None or member_waiting != seat_waiting:
            raise _WireError("viewer waiting seat")
        viewer_waiting = member_waiting
    for field in (
        "top_up_requests",
        "bust_decisions",
        "low_stack_prompts",
        "public_hand_summaries",
    ):
        value = raw.get(field)
        if value is not None and not isinstance(value, list):
            raise _WireError(field)
    table = raw.get("table")
    if table is None:
        if raw.get("phase") not in {"LOBBY", "CLOSED"}:
            raise _WireError("missing table")
        return
    if not isinstance(table, dict):
        raise _WireError("table state")
    table_viewer = table.get("viewer_seat")
    expected_table_viewer = None if viewer_waiting else viewer_seat
    if (
        table.get("schema") != PROJECTION_SCHEMA
        or table.get("room") != credential.room_id
        or table.get("state_version") != version
        or table_viewer != expected_table_viewer
        or not isinstance(table.get("seats"), list)
        or not isinstance(table.get("board"), list)
        or not isinstance(table.get("pots"), list)
        or not isinstance(table.get("shown"), list)
    ):
        raise _WireError("table header")
    acting = table.get("acting_seat")
    legal = table.get("legal_actions")
    if legal is not None and (table_viewer is None or acting != table_viewer):
        raise _WireError("legal actions leaked")


def _validate_departed_room_state(
    raw: dict[str, object],
    credential: _SeatCredential,
) -> None:
    """校验 room.leave 的退役成员投影，但不把它安装为当前房间状态。"""

    version = raw.get("state_version")
    members = raw.get("members")
    if (
        raw.get("schema") != ROOM_STATE_SCHEMA
        or raw.get("protocol") != PROTOCOL_VERSION
        or raw.get("room") != credential.room_id
        or type(version) is not int
        or not 0 <= version <= MAX_WIRE_INTEGER
        or raw.get("phase") not in _ROOM_PHASES
        or raw.get("viewer_member_id") != credential.member_id
        or raw.get("viewer_seat") is not None
        or raw.get("viewer_is_host") is not credential.is_host
        or not isinstance(members, list)
        or any(
            isinstance(member, dict)
            and member.get("member_id") == credential.member_id
            for member in members
        )
    ):
        raise _WireError("departed room state")
    table = raw.get("table")
    if table is None:
        return
    if (
        not isinstance(table, dict)
        or table.get("schema") != PROJECTION_SCHEMA
        or table.get("room") != credential.room_id
        or table.get("state_version") != version
        or table.get("viewer_seat") is not None
        or table.get("legal_actions") is not None
    ):
        raise _WireError("departed table state")


def _business_error(message: dict[str, object]) -> tuple[str, str]:
    detail = message.get("error")
    if not isinstance(detail, dict):
        raise _WireError("business error")
    return _safe_error_code(detail.get("code")), _raw_error_message(detail.get("message"))


def _transport_error(message: dict[str, object]) -> tuple[str, str]:
    detail = message.get("body")
    if not isinstance(detail, dict):
        raise _WireError("transport error")
    return _safe_error_code(detail.get("code")), _raw_error_message(detail.get("message"))


def _safe_error_code(value: object) -> str:
    if (
        isinstance(value, str)
        and 1 <= len(value) <= 64
        and all(char in _ERROR_CODE_CHARS for char in value)
    ):
        return value
    return "UNKNOWN_ERROR"


def _raw_error_message(value: object) -> str:
    if isinstance(value, str):
        return value
    return "联机请求失败"


def _safe_error_message(value: object, *, secret: str | None = None) -> str:
    if isinstance(value, str):
        text = "".join(char for char in value if char.isprintable()).strip()
        if secret:
            text = text.replace(secret, "***")
        if text:
            return text[:256]
    return "联机请求失败"


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, FrozenJSON]:
    frozen = _freeze(dict(value))
    if not isinstance(frozen, Mapping):  # pragma: no cover
        raise TypeError("mapping freeze invariant")
    return frozen


def _freeze(value: object) -> FrozenJSON:
    if value is None or isinstance(value, (bool, str)):
        return value
    if type(value) is int:
        if not -MAX_WIRE_INTEGER <= value <= MAX_WIRE_INTEGER:
            raise _WireError("wire integer range")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _WireError("wire float finite")
        return value
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise _WireError("non-string key")
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    raise _WireError("unsupported json value")


def _validate_outgoing_json(value: object, *, depth: int = 0) -> None:
    """同步验证待发送 JSON，防止后台线程才发现调用方输入非法。"""
    if depth > 64:
        raise ClientUsageError("命令 JSON 嵌套过深")
    if value is None or isinstance(value, (bool, str)):
        return
    if type(value) is int:
        if not -MAX_WIRE_INTEGER <= value <= MAX_WIRE_INTEGER:
            raise ClientUsageError("命令整数超出协议安全范围")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ClientUsageError("命令浮点数必须有限")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ClientUsageError("命令 JSON 键必须是字符串")
            if key == "seat":
                raise ClientUsageError("客户端命令不得包含 seat")
            _validate_outgoing_json(item, depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_outgoing_json(item, depth=depth + 1)
        return
    raise ClientUsageError("命令包含不可 JSON 编码的值")


def _object_without_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _WireError("duplicate server field")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise _WireError(f"invalid JSON constant: {value}")


def _parse_wire_int(value: str) -> int:
    digits = value[1:] if value.startswith("-") else value
    if len(digits) > 16:
        raise _WireError("server integer too large")
    parsed = int(value)
    if abs(parsed) > MAX_WIRE_INTEGER:
        raise _WireError("server integer too large")
    return parsed


def _parse_wire_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise _WireError("server float not finite")
    return parsed


def _fatal_close(exc: ConnectionClosed) -> tuple[str, str] | None:
    """把认证、策略与会话替换关闭分类为不可自动重连。"""
    received = getattr(exc, "rcvd", None)
    sent = getattr(exc, "sent", None)
    raw_code = getattr(received, "code", None)
    if raw_code is None:
        raw_code = getattr(sent, "code", None)
    try:
        code = int(raw_code) if raw_code is not None else None
    except (TypeError, ValueError):
        code = None
    return {
        1000: ("CONNECTION_CLOSED", "服务器已正常关闭连接"),
        1002: ("PROTOCOL_CLOSED", "服务器因协议错误关闭连接"),
        1003: ("UNSUPPORTED_DATA", "服务器拒绝了不支持的数据"),
        1007: ("INVALID_DATA", "服务器拒绝了非法数据"),
        1008: ("POLICY_REJECTED", "服务器拒绝了当前连接或认证"),
        1009: ("MESSAGE_TOO_LARGE", "服务器因消息过大关闭连接"),
        4001: ("SESSION_REPLACED", "该座位已由另一连接恢复，旧客户端已停止"),
        4002: ("ROOM_CHANNEL_CLOSED", "房间连接已经关闭"),
        4003: ("SLOW_CONSUMER", "客户端消费状态过慢，连接已停止"),
        4004: ("SERVICE_SHUTDOWN", "朋友局服务已经关闭"),
    }.get(code)


__all__ = [
    "ClientBusyError",
    "ClientErrorInfo",
    "ClientEvent",
    "ClientSnapshot",
    "ClientUsageError",
    "DesktopMultiplayerClient",
]
