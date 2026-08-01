"""localhost WebSocket 传输适配器。

牌局规则、认证座位及房间状态全部由注入的 ``TransportBackend`` 掌管。
本模块仅负责 HTTP 健康检查、WebSocket 生命周期、hello 门禁、消息大小、
保活和异常到稳定 wire error / close code 的映射。
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from functools import partial
from http import HTTPStatus
import logging
import math
from typing import Protocol

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.frames import CloseCode
from websockets.http11 import Request, Response

from multiplayer.protocol import (
    MAX_CLIENT_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    ClientEnvelope,
    ProtocolError,
    encode_server_message,
    parse_client_message,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
HEALTH_PATH = "/health"
WS_PATH = "/ws"

DEFAULT_HELLO_TIMEOUT = 5.0
DEFAULT_OUTBOUND_QUEUE = 64
DEFAULT_PING_INTERVAL = 20.0
DEFAULT_PING_TIMEOUT = 20.0
DEFAULT_CLOSE_TIMEOUT = 10.0

_CLIENT_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"
)
_VERSION_CHARS = _CLIENT_CHARS | frozenset("+")
_ROOM_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)
_TOKEN_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)
_CLOSE_REASON_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"
)

LOGGER = logging.getLogger("tavern.multiplayer.transport")


@dataclass(frozen=True)
class HelloInfo:
    """严格校验后的客户端介绍及可选恢复凭据。"""

    client: str
    client_version: str
    resume_room_id: str | None = None
    resume_token: str | None = field(default=None, repr=False)

    @property
    def is_resume(self) -> bool:
        """该连接是否请求恢复既有座位。"""
        return self.resume_token is not None


Emit = Callable[[Mapping[str, object]], bool]
Close = Callable[[int, str], None]


class TransportBackend(Protocol):
    """房间服务需要实现的最小异步适配接口。

    ``emit`` 只能在创建它的 asyncio 事件循环内调用。它只会把消息加入该
    连接的单 writer 队列；返回 ``False`` 表示连接已进入关闭流程。
    """

    async def connect(
        self,
        connection_id: str,
        hello: HelloInfo,
        emit: Emit,
        close: Close,
    ) -> None:
        """登记连接；失败时可抛 ``ConnectionRejected``。"""
        ...

    async def submit(
        self,
        connection_id: str,
        envelope: ClientEnvelope,
    ) -> None:
        """处理一条已通过传输门禁的客户端意图。"""
        ...

    async def disconnect(self, connection_id: str) -> None:
        """幂等注销连接。"""
        ...


class ConnectionRejected(RuntimeError):
    """后端拒绝 hello 或恢复认证；不会把敏感输入写入异常文本。"""

    def __init__(self, code: str = "AUTH_FAILED", message: str = "认证失败") -> None:
        self.code = code
        self.message = message
        super().__init__(code)


@dataclass(frozen=True)
class TransportConfig:
    """服务端传输参数；网络接口固定为 IPv4 localhost。"""

    port: int = DEFAULT_PORT
    hello_timeout: float = DEFAULT_HELLO_TIMEOUT
    outbound_queue: int = DEFAULT_OUTBOUND_QUEUE
    ping_interval: float = DEFAULT_PING_INTERVAL
    ping_timeout: float = DEFAULT_PING_TIMEOUT
    close_timeout: float = DEFAULT_CLOSE_TIMEOUT

    def __post_init__(self) -> None:
        if isinstance(self.port, bool) or not isinstance(self.port, int):
            raise ValueError("port 必须是整数")
        if not 0 <= self.port <= 65535:
            raise ValueError("port 必须在 0-65535 之间")
        if not _positive_number(self.hello_timeout):
            raise ValueError("hello_timeout 必须为正数")
        if (
            isinstance(self.outbound_queue, bool)
            or not isinstance(self.outbound_queue, int)
            or self.outbound_queue <= 0
        ):
            raise ValueError("outbound_queue 必须为正整数")
        for name in ("ping_interval", "ping_timeout", "close_timeout"):
            if not _positive_number(getattr(self, name)):
                raise ValueError(f"{name} 必须为正数")


@dataclass
class _QueuedMessage:
    text: str
    delivered: asyncio.Future[bool] | None = None


class _Outbox:
    """单连接有界出站队列；WebSocket 只由一个 writer 调用 ``send``。"""

    def __init__(self, limit: int) -> None:
        self._queue: asyncio.Queue[_QueuedMessage] = asyncio.Queue(maxsize=limit)
        self.failed = asyncio.Event()
        self.failure_code = int(CloseCode.TRY_AGAIN_LATER)
        self.failure_reason = "OUTBOUND_BACKPRESSURE"

    def emit(self, message: Mapping[str, object]) -> bool:
        """非阻塞加入可信服务端消息，避免慢客户端阻塞房间 actor。"""
        try:
            text = encode_server_message(message)
        except (ProtocolError, TypeError):
            self._fail(CloseCode.INTERNAL_ERROR, "INVALID_SERVER_MESSAGE")
            return False
        return self._put(_QueuedMessage(text))

    async def send_and_wait(
        self,
        message: Mapping[str, object],
        *,
        timeout: float = 1.0,
    ) -> bool:
        """排队并等待单 writer 发送，供“错误后关闭”保持顺序。"""
        loop = asyncio.get_running_loop()
        delivered: asyncio.Future[bool] = loop.create_future()
        try:
            text = encode_server_message(message)
        except (ProtocolError, TypeError):
            self._fail(CloseCode.INTERNAL_ERROR, "INVALID_SERVER_MESSAGE")
            return False
        if not self._put(_QueuedMessage(text, delivered)):
            return False
        try:
            return await asyncio.wait_for(asyncio.shield(delivered), timeout)
        except TimeoutError:
            return False

    def _put(self, item: _QueuedMessage) -> bool:
        if self.failed.is_set():
            return False
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            self._fail(CloseCode.TRY_AGAIN_LATER, "OUTBOUND_BACKPRESSURE")
            return False
        return True

    def _fail(self, code: int | CloseCode, reason: str) -> None:
        if self.failed.is_set():
            return
        self.failure_code = int(code)
        self.failure_reason = reason
        self.failed.set()

    async def write_forever(self, websocket: ServerConnection) -> None:
        """顺序发送出站消息；连接关闭时唤醒等待发送结果的调用者。"""
        while True:
            item = await self._queue.get()
            try:
                await websocket.send(item.text)
            except ConnectionClosed:
                if item.delivered is not None and not item.delivered.done():
                    item.delivered.set_result(False)
                raise
            except Exception:
                if item.delivered is not None and not item.delivered.done():
                    item.delivered.set_result(False)
                self._fail(CloseCode.INTERNAL_ERROR, "SEND_FAILED")
                raise
            else:
                if item.delivered is not None and not item.delivered.done():
                    item.delivered.set_result(True)
            finally:
                self._queue.task_done()


class _CloseRequest:
    """后端到传输层的幂等关闭信号。"""

    def __init__(self) -> None:
        self.event = asyncio.Event()
        self.code = 4000
        self.reason = "BACKEND_CLOSE"

    def request(self, code: int, reason: str) -> None:
        if self.event.is_set():
            return
        if (
            not _valid_application_close(code)
            or not isinstance(reason, str)
            or not 1 <= len(reason) <= 80
            or any(ch not in _CLOSE_REASON_CHARS for ch in reason)
        ):
            self.code = int(CloseCode.INTERNAL_ERROR)
            self.reason = "INVALID_CLOSE_REQUEST"
        else:
            self.code = code
            # WebSocket close reason上限 123 bytes；后端理由保持短 ASCII wire code。
            encoded = reason.encode("utf-8", errors="replace")
            self.reason = (
                reason if len(encoded) <= 123 else "BACKEND_CLOSE_REASON_TOO_LONG"
            )
        self.event.set()


def make_error_message(code: str, message: str) -> dict[str, object]:
    """构造不含请求或认证秘密的稳定传输错误。"""
    return {
        "v": PROTOCOL_VERSION,
        "type": "error",
        "body": {"code": code, "message": message},
    }


def process_http_request(
    connection: ServerConnection,
    request: Request,
) -> Response | None:
    """在同一端口只提供 ``GET /health`` 与精确 ``/ws`` Upgrade。"""
    path = request.path
    if path == HEALTH_PATH:
        response = connection.respond(HTTPStatus.OK, "OK\n")
        response.headers["Cache-Control"] = "no-store"
        return response
    if path != WS_PATH:
        response = connection.respond(HTTPStatus.NOT_FOUND, "Not Found\n")
        response.headers["Cache-Control"] = "no-store"
        return response
    return None


async def create_server(
    backend: TransportBackend,
    *,
    config: TransportConfig | None = None,
) -> Server:
    """创建仅绑定 ``127.0.0.1`` 的服务端并立即开始监听。"""
    active = config or TransportConfig()
    handler = partial(_connection_handler, backend=backend, config=active)
    return await serve(
        handler,
        DEFAULT_HOST,
        active.port,
        process_request=process_http_request,
        compression=None,
        server_header=None,
        open_timeout=10,
        ping_interval=active.ping_interval,
        ping_timeout=active.ping_timeout,
        close_timeout=active.close_timeout,
        max_size=MAX_CLIENT_MESSAGE_BYTES,
        max_queue=16,
        write_limit=32 * 1024,
    )


async def _connection_handler(
    websocket: ServerConnection,
    *,
    backend: TransportBackend,
    config: TransportConfig,
) -> None:
    connection_id = str(websocket.id)
    try:
        try:
            raw_hello = await asyncio.wait_for(
                websocket.recv(),
                timeout=config.hello_timeout,
            )
        except TimeoutError:
            await _direct_error_close(
                websocket,
                "HELLO_TIMEOUT",
                "连接后必须在 5 秒内发送 hello",
                CloseCode.POLICY_VIOLATION,
            )
            return
        except ConnectionClosed:
            return

        if not isinstance(raw_hello, str):
            await _direct_error_close(
                websocket,
                "UNSUPPORTED_DATA",
                "仅接受文本 JSON 消息",
                CloseCode.UNSUPPORTED_DATA,
            )
            return
        try:
            envelope = parse_client_message(raw_hello)
            hello = _parse_hello(envelope)
        except ProtocolError as exc:
            await _direct_error_close(
                websocket,
                exc.code,
                exc.message,
                _protocol_close_code(exc),
            )
            return

        outbox = _Outbox(config.outbound_queue)
        close_request = _CloseRequest()
        try:
            await backend.connect(
                connection_id,
                hello,
                outbox.emit,
                close_request.request,
            )
        except ConnectionRejected as exc:
            await _direct_error_close(
                websocket,
                exc.code,
                exc.message,
                CloseCode.POLICY_VIOLATION,
            )
            return
        except Exception as exc:
            _log_backend_failure("connect", connection_id, exc)
            await _direct_error_close(
                websocket,
                "INTERNAL_ERROR",
                "服务器内部错误",
                CloseCode.INTERNAL_ERROR,
            )
            return

        writer = asyncio.create_task(
            _writer_guard(websocket, outbox, connection_id),
            name=f"ws-writer-{connection_id}",
        )
        failure_watcher = asyncio.create_task(
            _close_on_outbox_failure(websocket, outbox),
            name=f"ws-backpressure-{connection_id}",
        )
        backend_close_watcher = asyncio.create_task(
            _close_on_backend_request(websocket, close_request),
            name=f"ws-backend-close-{connection_id}",
        )
        try:
            async for raw in websocket:
                if not isinstance(raw, str):
                    await _queued_error_close(
                        websocket,
                        outbox,
                        "UNSUPPORTED_DATA",
                        "仅接受文本 JSON 消息",
                        CloseCode.UNSUPPORTED_DATA,
                    )
                    return
                try:
                    message = parse_client_message(raw)
                except ProtocolError as exc:
                    await _queued_error_close(
                        websocket,
                        outbox,
                        exc.code,
                        exc.message,
                        _protocol_close_code(exc),
                    )
                    return
                if message.message_type == "hello":
                    await _queued_error_close(
                        websocket,
                        outbox,
                        "HELLO_ALREADY_DONE",
                        "同一连接只能发送一次 hello",
                        CloseCode.POLICY_VIOLATION,
                    )
                    return
                if message.message_type == "ping":
                    outbox.emit(
                        {"v": PROTOCOL_VERSION, "type": "pong", "body": {}}
                    )
                    continue
                try:
                    await backend.submit(connection_id, message)
                except ConnectionRejected as exc:
                    await _queued_error_close(
                        websocket,
                        outbox,
                        exc.code,
                        exc.message,
                        CloseCode.POLICY_VIOLATION,
                    )
                    return
                except ProtocolError as exc:
                    # envelope 合法后的动作/房间语义错误可修正重试，不断开。
                    outbox.emit(make_error_message(exc.code, exc.message))
                except Exception as exc:
                    _log_backend_failure("submit", connection_id, exc)
                    await _queued_error_close(
                        websocket,
                        outbox,
                        "INTERNAL_ERROR",
                        "服务器内部错误",
                        CloseCode.INTERNAL_ERROR,
                    )
                    return
        except ConnectionClosed:
            pass
        finally:
            try:
                await backend.disconnect(connection_id)
            except Exception as exc:
                _log_backend_failure("disconnect", connection_id, exc)
            await _cancel_task(backend_close_watcher)
            await _cancel_task(failure_watcher)
            await _cancel_task(writer)
    except ConnectionClosed:
        pass


def _parse_hello(envelope: ClientEnvelope) -> HelloInfo:
    if envelope.message_type != "hello":
        raise ProtocolError("HELLO_REQUIRED", "首条消息必须是 hello")
    body = envelope.body
    unknown = set(body) - {"client", "client_version", "resume"}
    if unknown:
        raise ProtocolError(
            "INVALID_HELLO",
            f"hello 包含未知字段: {', '.join(sorted(unknown))}",
        )
    client = _safe_wire_text(
        body.get("client"),
        field="client",
        maximum=32,
        alphabet=_CLIENT_CHARS,
    )
    client_version = _safe_wire_text(
        body.get("client_version"),
        field="client_version",
        maximum=64,
        alphabet=_VERSION_CHARS,
    )
    resume = body.get("resume")
    if resume is None:
        return HelloInfo(client=client, client_version=client_version)
    if not isinstance(resume, dict) or set(resume) != {"room_id", "token"}:
        raise ProtocolError(
            "INVALID_HELLO",
            "resume 必须且只能包含 room_id 与 token",
        )
    room_id = _safe_wire_text(
        resume.get("room_id"),
        field="resume.room_id",
        maximum=32,
        alphabet=_ROOM_CHARS,
    )
    token = _safe_wire_text(
        resume.get("token"),
        field="resume.token",
        minimum=32,
        maximum=128,
        alphabet=_TOKEN_CHARS,
    )
    return HelloInfo(
        client=client,
        client_version=client_version,
        resume_room_id=room_id,
        resume_token=token,
    )


def _safe_wire_text(
    value: object,
    *,
    field: str,
    maximum: int,
    alphabet: frozenset[str],
    minimum: int = 1,
) -> str:
    if (
        not isinstance(value, str)
        or not minimum <= len(value) <= maximum
        or any(ch not in alphabet for ch in value)
    ):
        raise ProtocolError("INVALID_HELLO", f"{field} 格式非法")
    return value


async def _writer_guard(
    websocket: ServerConnection,
    outbox: _Outbox,
    connection_id: str,
) -> None:
    try:
        await outbox.write_forever(websocket)
    except ConnectionClosed:
        pass
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log_backend_failure("writer", connection_id, exc)
        with suppress(ConnectionClosed):
            await websocket.close(
                code=CloseCode.INTERNAL_ERROR,
                reason="SEND_FAILED",
            )


async def _close_on_outbox_failure(
    websocket: ServerConnection,
    outbox: _Outbox,
) -> None:
    await outbox.failed.wait()
    with suppress(ConnectionClosed):
        await websocket.close(
            code=outbox.failure_code,
            reason=outbox.failure_reason,
        )


async def _close_on_backend_request(
    websocket: ServerConnection,
    request: _CloseRequest,
) -> None:
    await request.event.wait()
    with suppress(ConnectionClosed):
        await websocket.close(code=request.code, reason=request.reason)


async def _direct_error_close(
    websocket: ServerConnection,
    code: str,
    message: str,
    close_code: int | CloseCode,
) -> None:
    with suppress(ConnectionClosed):
        await websocket.send(encode_server_message(make_error_message(code, message)))
    with suppress(ConnectionClosed):
        await websocket.close(code=close_code, reason=code[:80])


async def _queued_error_close(
    websocket: ServerConnection,
    outbox: _Outbox,
    code: str,
    message: str,
    close_code: int | CloseCode,
) -> None:
    await outbox.send_and_wait(make_error_message(code, message))
    with suppress(ConnectionClosed):
        await websocket.close(code=close_code, reason=code[:80])


def _protocol_close_code(exc: ProtocolError) -> CloseCode:
    if exc.code == "MESSAGE_TOO_LARGE":
        return CloseCode.MESSAGE_TOO_BIG
    if exc.code == "INVALID_UTF8":
        return CloseCode.INVALID_DATA
    return CloseCode.POLICY_VIOLATION


async def _cancel_task(task: asyncio.Task[None]) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError, ConnectionClosed):
        await task


def _log_backend_failure(stage: str, connection_id: str, exc: Exception) -> None:
    # 刻意不记录异常文本、原始消息、hello 或 token。
    LOGGER.error(
        "multiplayer backend %s failed for connection %s (%s)",
        stage,
        connection_id,
        type(exc).__name__,
    )


def _positive_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value > 0
    )


def _valid_application_close(value: object) -> bool:
    """后端只使用 RFC 私有范围，避免伪造保留的标准关闭码。"""
    return (
        not isinstance(value, bool)
        and isinstance(value, int)
        and 3000 <= value <= 4999
    )


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "HEALTH_PATH",
    "WS_PATH",
    "Close",
    "ConnectionRejected",
    "Emit",
    "HelloInfo",
    "TransportBackend",
    "TransportConfig",
    "create_server",
    "make_error_message",
    "process_http_request",
]
