"""安全启动朋友局 Alpha；默认只开放 localhost。

临时公网模式只负责启动一个明确的 ``cloudflared`` Quick Tunnel 子进程。
它不会下载、安装、更新或写入任何 Tunnel 配置，也不会把私密 WS 路径放到
子进程参数、磁盘文件或普通对象 ``repr`` 中。
"""
from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sys
import time
from typing import NoReturn
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from websockets.asyncio.client import connect

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from multiplayer.protocol import MAX_CLIENT_MESSAGE_BYTES, PROTOCOL_VERSION
from multiplayer_server.service import RoomRegistryBackend
from multiplayer_server.ws_app import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    HEALTH_PATH,
    WS_PATH,
    TransportConfig,
    create_server,
    ws_path_from_token,
)
from tools.friends_lobby_host import LobbyHost, LobbyHostError

PUBLIC_CONFIRMATION = "ALPHA-TEMPORARY-PUBLIC"
QUICK_TUNNEL_SUFFIX = ".trycloudflare.com"
DEFAULT_TUNNEL_URL_TIMEOUT = 30.0
DEFAULT_PUBLIC_SMOKE_TIMEOUT = 45.0
DEFAULT_CHILD_STOP_TIMEOUT = 5.0
DESKTOP_PUBLIC_WSS_ENV = "TAVERN_FRIENDS_PUBLIC_WSS"
DESKTOP_LOCAL_WS_ENV = "TAVERN_FRIENDS_LOCAL_WS"
DESKTOP_PLAYER_COUNT_ENV = "TAVERN_FRIENDS_PLAYER_COUNT"
_URL_SETTLE_SECONDS = 0.35
_CLOUDFLARED_CONFIG_NAMES = ("config.yml", "config.yaml")
_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_OUTPUT_URL = re.compile(r"https?://[^\s<>\x00-\x1f]+", re.IGNORECASE)
_QUICK_TUNNEL_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_DIRECT_HTTP_OPENER = build_opener(ProxyHandler({}))
_HEALTH_USER_AGENT = (
    "Mozilla/5.0 (compatible; Tavern-Friends-Alpha/1.0; +local-health-check)"
)


class LauncherError(RuntimeError):
    """可安全显示且不携带私密路径的启动失败。"""


@dataclass
class _TunnelChild:
    """启动器拥有的唯一 Tunnel 子进程及其输出排水任务。"""

    process: asyncio.subprocess.Process = field(repr=False)
    signals: asyncio.Queue[str | LauncherError | None] = field(repr=False)
    readers: tuple[asyncio.Task[None], ...] = field(repr=False)


@dataclass
class _DesktopChild:
    """启动器拥有的 Windows 联机牌桌进程；环境中的地址不进入 repr。"""

    process: asyncio.subprocess.Process = field(repr=False)


def cloudflared_config_path(home: Path | None = None) -> Path | None:
    """返回会干扰 Quick Tunnel 的现有用户配置；不修改该文件。"""
    base = (home if home is not None else Path.home()) / ".cloudflared"
    for name in _CLOUDFLARED_CONFIG_NAMES:
        candidate = base / name
        if candidate.is_file():
            return candidate
    return None


def find_cloudflared(
    explicit: str | os.PathLike[str] | None,
    *,
    which: Callable[[str], str | None] = shutil.which,
) -> Path:
    """只解析明确路径或 PATH；绝不下载、安装或更新程序。"""
    if explicit is not None:
        candidate = Path(explicit).expanduser()
        if not candidate.is_absolute():
            candidate = candidate.resolve()
    else:
        located = which("cloudflared")
        if located is None:
            raise LauncherError(
                "未找到 cloudflared；请自行安装后加入 PATH，或用 "
                "--cloudflared 指定明确路径"
            )
        candidate = Path(located).expanduser()
        if not candidate.is_absolute():
            candidate = candidate.resolve()

    if not candidate.is_file():
        raise LauncherError("指定的 cloudflared 不是现有文件")
    if os.name != "nt" and not os.access(candidate, os.X_OK):
        raise LauncherError("指定的 cloudflared 不可执行")
    return candidate.resolve()


def cloudflared_command(executable: Path, origin: str) -> tuple[str, ...]:
    """返回无 shell、无私密 WS 路径的 Quick Tunnel 精确参数。"""
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "http"
        or parsed.hostname != DEFAULT_HOST
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise LauncherError("Tunnel origin 必须是本机 HTTP 服务")
    try:
        port = parsed.port
    except ValueError as exc:
        raise LauncherError("Tunnel origin 端口非法") from exc
    if port is None or not 1 <= port <= 65535:
        raise LauncherError("Tunnel origin 缺少有效端口")
    return (
        str(executable),
        "tunnel",
        "--no-autoupdate",
        "--url",
        f"http://{DEFAULT_HOST}:{port}",
    )


def _quick_tunnel_urls(output: str) -> list[str]:
    """提取并严格校验日志中的 trycloudflare HTTPS URL。"""
    cleaned = _ANSI_ESCAPE.sub("", output)
    matches: list[str] = []
    for raw in _OUTPUT_URL.findall(cleaned):
        candidate = raw.rstrip("\"'.,;:!?)>]}|")
        if "trycloudflare" not in candidate.casefold():
            continue
        try:
            parsed = urlsplit(candidate)
            port = parsed.port
        except ValueError as exc:
            raise LauncherError("cloudflared 输出了非法 Quick Tunnel URL") from exc
        hostname = parsed.hostname.casefold() if parsed.hostname is not None else None
        if (
            parsed.scheme.casefold() != "https"
            or hostname is None
            or not hostname.endswith(QUICK_TUNNEL_SUFFIX)
            or hostname.count(".") != 2
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
            or parsed.path != ""
            or parsed.query != ""
            or parsed.fragment != ""
        ):
            raise LauncherError("cloudflared 输出了非法 Quick Tunnel URL")
        label = hostname[: -len(QUICK_TUNNEL_SUFFIX)]
        if _QUICK_TUNNEL_LABEL.fullmatch(label) is None:
            raise LauncherError("cloudflared 输出了非法 Quick Tunnel URL")
        matches.append(f"https://{hostname}")
    return matches


def parse_quick_tunnel_url(output: str) -> str:
    """日志必须恰好包含一次合法 Quick Tunnel URL。"""
    matches = _quick_tunnel_urls(output)
    if not matches:
        raise LauncherError("cloudflared 输出中没有 Quick Tunnel URL")
    if len(matches) != 1:
        raise LauncherError("cloudflared 输出了多个 Quick Tunnel URL")
    return matches[0]


async def _read_tunnel_output(
    stream: asyncio.StreamReader,
    signals: asyncio.Queue[str | LauncherError | None],
) -> None:
    """持续排空一条子进程管道，只转发 URL 信号，不回显原始日志。"""
    try:
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace")
            try:
                urls = _quick_tunnel_urls(text)
            except LauncherError as exc:
                await signals.put(exc)
                continue
            for url in urls:
                await signals.put(url)
    finally:
        await signals.put(None)


async def _start_tunnel(executable: Path, origin: str) -> _TunnelChild:
    command = cloudflared_command(executable, origin)
    creationflags = 0
    if os.name == "nt":
        # 管道模式下不创建额外控制台；终止时仍只操作这个 Popen PID。
        creationflags = getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0)
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except (OSError, ValueError) as exc:
        raise LauncherError("cloudflared 启动失败") from exc
    assert process.stdout is not None
    assert process.stderr is not None
    signals: asyncio.Queue[str | LauncherError | None] = asyncio.Queue()
    readers = (
        asyncio.create_task(
            _read_tunnel_output(process.stdout, signals),
            name="cloudflared-stdout-drain",
        ),
        asyncio.create_task(
            _read_tunnel_output(process.stderr, signals),
            name="cloudflared-stderr-drain",
        ),
    )
    return _TunnelChild(process=process, signals=signals, readers=readers)


async def _wait_for_tunnel_url(
    child: _TunnelChild,
    *,
    timeout: float = DEFAULT_TUNNEL_URL_TIMEOUT,
) -> str:
    """等待唯一 URL；短暂稳定窗口可捕获重复或恶意第二输出。"""
    deadline = time.monotonic() + timeout
    settle_deadline: float | None = None
    candidate: str | None = None
    eof_count = 0
    while True:
        now = time.monotonic()
        if settle_deadline is not None and now >= settle_deadline:
            if child.process.returncode is not None:
                raise LauncherError("cloudflared 在联调地址就绪前退出")
            assert candidate is not None
            return candidate
        remaining = deadline - now
        if remaining <= 0:
            raise LauncherError("等待 Quick Tunnel 地址超时")
        wait_for = remaining
        if settle_deadline is not None:
            wait_for = min(wait_for, settle_deadline - now)
        try:
            signal = await asyncio.wait_for(child.signals.get(), wait_for)
        except TimeoutError:
            continue
        if isinstance(signal, LauncherError):
            raise signal
        if signal is None:
            eof_count += 1
            if eof_count == 2 or child.process.returncode is not None:
                raise LauncherError("cloudflared 在联调地址就绪前退出")
            continue
        if candidate is not None:
            raise LauncherError("cloudflared 输出了多个 Quick Tunnel URL")
        candidate = signal
        settle_deadline = time.monotonic() + _URL_SETTLE_SECONDS


async def _stop_tunnel(child: _TunnelChild | None) -> None:
    """只终止启动器记录的精确子进程 PID，并等待其退出。"""
    if child is None:
        return
    process = child.process
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(
                process.wait(),
                timeout=DEFAULT_CHILD_STOP_TIMEOUT,
            )
        except TimeoutError:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            with suppress(Exception):
                await asyncio.wait_for(
                    process.wait(),
                    timeout=DEFAULT_CHILD_STOP_TIMEOUT,
                )
    for task in child.readers:
        task.cancel()
    await asyncio.gather(*child.readers, return_exceptions=True)


async def _start_desktop_host(
    *,
    public_ws_url: str,
    local_ws_url: str,
    player_count: int,
) -> _DesktopChild:
    """用仅存于子进程环境的地址启动 Windows 房主 UI。"""
    environment = os.environ.copy()
    environment[DESKTOP_PUBLIC_WSS_ENV] = public_ws_url
    environment[DESKTOP_LOCAL_WS_ENV] = local_ws_url
    environment[DESKTOP_PLAYER_COUNT_ENV] = str(player_count)
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(ROOT / "main.py"),
            "--friends-host",
            cwd=str(ROOT),
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
        )
    except (OSError, ValueError):
        raise LauncherError("Windows 联机牌桌启动失败") from None
    return _DesktopChild(process=process)


async def _stop_desktop(child: _DesktopChild | None) -> None:
    """只终止本启动器创建的牌桌进程，并等待精确 PID 退出。"""
    if child is None:
        return
    process = child.process
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(
                process.wait(),
                timeout=DEFAULT_CHILD_STOP_TIMEOUT,
            )
        except TimeoutError:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            with suppress(Exception):
                await asyncio.wait_for(
                    process.wait(),
                    timeout=DEFAULT_CHILD_STOP_TIMEOUT,
                )


def _fetch_health(
    url: str,
    *,
    use_system_proxy: bool = False,
) -> tuple[bool, str]:
    """执行严格 health 请求，只返回不含 URL/凭据的诊断。"""
    request = Request(
        url,
        method="GET",
        # Cloudflare 对明显的脚本 UA 可能直接返回 403；保留产品标识，
        # 同时使用兼容浏览器的格式完成真实 origin 冒烟。
        headers={"User-Agent": _HEALTH_USER_AGENT},
    )
    try:
        # localhost 永远直连。公网只有用户显式选择后才允许交给其系统代理；
        # 这可兼容必须经本地代理访问 trycloudflare.com 的网络。
        opener = build_opener() if use_system_proxy else _DIRECT_HTTP_OPENER
        with opener.open(request, timeout=5.0) as response:
            body = response.read(16)
            if response.status != 200:
                return False, f"HTTP {response.status}"
            if response.geturl() != url:
                return False, "发生重定向"
            if body != b"OK\n":
                return False, "响应体不是 OK"
            cache_control = response.headers.get("Cache-Control", "")
            directives = {
                part.strip().casefold().split("=", 1)[0]
                for part in cache_control.split(",")
                if part.strip()
            }
            if "no-store" not in directives:
                return False, "缺少 Cache-Control no-store"
            return True, "OK"
    except HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except URLError as exc:
        return False, f"网络错误 {type(exc.reason).__name__}"
    except Exception as exc:
        return False, f"网络错误 {type(exc).__name__}"


async def _wait_for_health(
    url: str,
    *,
    timeout: float,
    tunnel: _TunnelChild | None = None,
    use_system_proxy: bool = False,
) -> None:
    deadline = time.monotonic() + timeout
    last_reason = "尚未收到响应"
    while time.monotonic() < deadline:
        if tunnel is not None and tunnel.process.returncode is not None:
            raise LauncherError("cloudflared 在公网健康检查期间退出")
        healthy, last_reason = await asyncio.to_thread(
            _fetch_health,
            url,
            use_system_proxy=use_system_proxy,
        )
        if healthy:
            return
        await asyncio.sleep(0.35)
    raise LauncherError(f"服务 /health 冒烟失败（{last_reason}）")


def _decode_server_message(raw: str | bytes) -> dict[str, object]:
    if not isinstance(raw, str):
        raise LauncherError("WebSocket 冒烟收到非文本消息")
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise LauncherError("WebSocket 冒烟收到非法 JSON") from exc
    if not isinstance(value, dict):
        raise LauncherError("WebSocket 冒烟收到非法消息")
    return value


async def _websocket_smoke_once(
    url: str,
    *,
    use_system_proxy: bool = False,
) -> None:
    hello = {
        "v": PROTOCOL_VERSION,
        "type": "hello",
        "body": {"client": "alpha-launcher", "client_version": "1"},
    }
    ping = {"v": PROTOCOL_VERSION, "type": "ping", "body": {}}
    async with connect(
        url,
        # localhost 永远直连；公网代理模式必须由用户显式选择，因为代理
        # 会看到完整的临时 WSS 路径。
        proxy=True if use_system_proxy else None,
        compression=None,
        open_timeout=6.0,
        close_timeout=3.0,
        ping_interval=None,
        max_size=MAX_CLIENT_MESSAGE_BYTES,
    ) as websocket:
        await websocket.send(json.dumps(hello, separators=(",", ":")))
        welcome = _decode_server_message(
            await asyncio.wait_for(websocket.recv(), timeout=6.0)
        )
        if welcome.get("v") != PROTOCOL_VERSION or welcome.get("type") != "welcome":
            raise LauncherError("WebSocket hello 冒烟失败")
        await websocket.send(json.dumps(ping, separators=(",", ":")))
        pong = _decode_server_message(
            await asyncio.wait_for(websocket.recv(), timeout=6.0)
        )
        if pong != {"v": PROTOCOL_VERSION, "type": "pong", "body": {}}:
            raise LauncherError("WebSocket ping 冒烟失败")


async def _wait_for_websocket_smoke(
    url: str,
    *,
    timeout: float,
    tunnel: _TunnelChild | None = None,
    use_system_proxy: bool = False,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if tunnel is not None and tunnel.process.returncode is not None:
            raise LauncherError("cloudflared 在公网 WebSocket 冒烟期间退出")
        try:
            await _websocket_smoke_once(
                url,
                use_system_proxy=use_system_proxy,
            )
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(0.4)
    raise LauncherError("WebSocket hello/ping 冒烟失败")


async def _wait_until_stopped(
    stop_event: asyncio.Event,
    tunnel: _TunnelChild | None,
    host_watch: asyncio.Task[None] | None = None,
    desktop: _DesktopChild | None = None,
) -> None:
    """等待用户停止；任一受托子资源退出时同步关闭整个 Alpha。"""
    if tunnel is None and host_watch is None and desktop is None:
        await stop_event.wait()
        return

    stop_task = asyncio.create_task(stop_event.wait(), name="alpha-stop-wait")
    tunnel_task = (
        None
        if tunnel is None
        else asyncio.create_task(
            tunnel.process.wait(),
            name="cloudflared-exit-wait",
        )
    )
    desktop_task = (
        None
        if desktop is None
        else asyncio.create_task(
            desktop.process.wait(),
            name="desktop-host-exit-wait",
        )
    )
    tasks: set[asyncio.Task[object]] = {stop_task}
    if tunnel_task is not None:
        tasks.add(tunnel_task)
    if host_watch is not None:
        tasks.add(host_watch)
    if desktop_task is not None:
        tasks.add(desktop_task)
    try:
        done, _pending = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_task in done:
            return
        if host_watch is not None and host_watch in done:
            try:
                await host_watch
            except LobbyHostError as exc:
                raise LauncherError(str(exc)) from exc
            raise LauncherError("自动房主监视意外停止")
        if desktop_task is not None and desktop_task in done:
            return_code = await desktop_task
            if return_code == 0:
                return
            raise LauncherError("Windows 联机牌桌异常退出")
        raise LauncherError("cloudflared 意外退出；朋友局已安全停止")
    finally:
        for task in tasks:
            if task is not host_watch and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in tasks if task is not host_watch),
            return_exceptions=True,
        )


async def run_alpha(
    *,
    port: int = DEFAULT_PORT,
    public_quick_tunnel: bool = False,
    public_via_system_proxy: bool = False,
    phone_verifies_public: bool = False,
    auto_create_room: bool = False,
    launch_desktop_host: bool = False,
    desktop_player_count: int = 2,
    cloudflared: Path | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    """同一进程运行权威后端；公网模式额外托管一个 Tunnel PID。"""
    if public_via_system_proxy and not public_quick_tunnel:
        raise LauncherError("系统代理只允许用于临时公网冒烟")
    if phone_verifies_public and not public_quick_tunnel:
        raise LauncherError("手机验证模式只允许用于临时公网")
    if public_via_system_proxy and phone_verifies_public:
        raise LauncherError("系统代理冒烟与手机验证模式不能同时启用")
    if auto_create_room and not public_quick_tunnel:
        raise LauncherError("自动测试房只允许用于临时公网")
    if launch_desktop_host and not public_quick_tunnel:
        raise LauncherError("Windows 联机牌桌只允许用于临时公网")
    if launch_desktop_host and auto_create_room:
        raise LauncherError("Windows 可玩房主不能与自动连接验证房同时启动")
    if type(desktop_player_count) is not int or not 2 <= desktop_player_count <= 9:
        raise LauncherError("Windows 房间座位数须为 2–9")
    path_token = secrets.token_urlsafe(32) if public_quick_tunnel else None
    ws_path = ws_path_from_token(path_token) if path_token is not None else WS_PATH
    config = TransportConfig(port=port, ws_path=ws_path)
    backend = RoomRegistryBackend()
    server = None
    tunnel: _TunnelChild | None = None
    lobby_host: LobbyHost | None = None
    host_watch: asyncio.Task[None] | None = None
    desktop: _DesktopChild | None = None
    try:
        print("[1/4] 正在启动 localhost 权威服务……", flush=True)
        try:
            server = await create_server(backend, config=config)
        except OSError as exc:
            raise LauncherError("localhost 服务启动失败；请检查端口是否被占用") from exc
        assert server.sockets
        actual_port = int(server.sockets[0].getsockname()[1])
        health_url = f"http://{DEFAULT_HOST}:{actual_port}{HEALTH_PATH}"
        local_ws_url = f"ws://{DEFAULT_HOST}:{actual_port}{ws_path}"
        await _wait_for_health(health_url, timeout=5.0)
        await _wait_for_websocket_smoke(local_ws_url, timeout=8.0)
        print("[2/4] 本机 health 与 WebSocket 冒烟通过。", flush=True)

        if public_quick_tunnel:
            if cloudflared is None:
                raise LauncherError("公网模式缺少 cloudflared 明确路径")
            origin = f"http://{DEFAULT_HOST}:{actual_port}"
            print("[3/4] 正在连接 Cloudflare，等待临时域名（最长约 30 秒）……", flush=True)
            tunnel = await _start_tunnel(cloudflared, origin)
            public_base = await _wait_for_tunnel_url(tunnel)
            public_ws_url = "wss" + public_base[5:] + ws_path
            if phone_verifies_public:
                print(
                    "[4/4] 已取得临时域名；按明确选项交由手机验证公网 WSS。",
                    flush=True,
                )
                print("警告：电脑尚未验证该公网地址，请先不要转发给朋友。")
            else:
                print("[4/4] 已取得临时域名，正在验证公网 health 与 WSS……", flush=True)
                await _wait_for_health(
                    f"{public_base}{HEALTH_PATH}",
                    timeout=DEFAULT_PUBLIC_SMOKE_TIMEOUT,
                    tunnel=tunnel,
                    use_system_proxy=public_via_system_proxy,
                )
                await _wait_for_websocket_smoke(
                    public_ws_url,
                    timeout=DEFAULT_PUBLIC_SMOKE_TIMEOUT,
                    tunnel=tunnel,
                    use_system_proxy=public_via_system_proxy,
                )
                print("公网 /health 与 WSS hello/ping 冒烟通过。")
            if auto_create_room:
                print("正在自动创建两人测试房……", flush=True)
                try:
                    lobby_host = await LobbyHost.create(local_ws_url)
                except LobbyHostError as exc:
                    raise LauncherError(str(exc)) from exc
                host_watch = asyncio.create_task(
                    lobby_host.watch(),
                    name="alpha-auto-host-watch",
                )
                print("\n======= 手机测试邀请（连接验证，仅私发） =======")
                print(f"WSS：{public_ws_url}")
                print(f"房间码：{lobby_host.room.room_id}")
                print("自动测试房主：已连接（v2 默认未入座）")
                print("状态：等待手机成员加入……")
                print("============================================")
                print("说明：这是连接验证房；Windows 图形牌桌请改用桌面房主脚本。")
                print("Android 须使用支持协议 v2 的会合版。")
                print("不要截图公开；URL 泄露后请 Ctrl+C 并重新启动。")
            elif launch_desktop_host:
                print("正在启动 Windows 联机房主牌桌……", flush=True)
                desktop = await _start_desktop_host(
                    public_ws_url=public_ws_url,
                    local_ws_url=local_ws_url,
                    player_count=desktop_player_count,
                )
                print(
                    f"Windows 联机窗口已启动（{desktop_player_count} 座）；"
                    "房间码将在窗口内显示。"
                )
                print("完整 WSS 只通过本机进程环境交给牌桌，未写入命令行。")
            else:
                print(
                    "待手机验证 WSS 地址："
                    if phone_verifies_public
                    else "临时可分享 WSS 地址："
                )
                print(public_ws_url)
                print("此 URL 含本次临时私密路径；泄露时请 Ctrl+C 后重新启动。")
        else:
            print("localhost /health 与 WS hello/ping 冒烟通过。")
            print(f"本机 WebSocket：ws://{DEFAULT_HOST}:{actual_port}{WS_PATH}")

        print("权威朋友局 Alpha 正在运行；按 Ctrl+C 安全停止。")
        waiter = stop_event if stop_event is not None else asyncio.Event()
        await _wait_until_stopped(waiter, tunnel, host_watch, desktop)
    finally:
        try:
            await _stop_desktop(desktop)
        finally:
            try:
                await _stop_tunnel(tunnel)
            finally:
                try:
                    if host_watch is not None and not host_watch.done():
                        host_watch.cancel()
                    if host_watch is not None:
                        await asyncio.gather(host_watch, return_exceptions=True)
                    if lobby_host is not None:
                        with suppress(Exception):
                            await lobby_host.close()
                finally:
                    try:
                        if server is not None:
                            server.close(code=1012, reason="SERVICE_RESTART")
                            await server.wait_closed()
                    finally:
                        # 任一子资源清理失败也不能跳过权威后端关闭。
                        await backend.close()


def _confirm_public() -> bool:
    print("警告：Quick Tunnel 仅用于短时朋友联调，无 SLA，也不是正式认证。")
    print("公网地址拿到它的人都可尝试连接；URL 泄露后必须立即重启。")
    try:
        value = input(f"确认临时开放公网，请输入 {PUBLIC_CONFIRMATION}: ")
    except (EOFError, KeyboardInterrupt):
        return False
    return value.strip() == PUBLIC_CONFIRMATION


def _argument_error(parser: argparse.ArgumentParser, message: str) -> NoReturn:
    parser.error(message)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="启动朋友局 Alpha（默认仅 localhost）"
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--public-quick-tunnel",
        action="store_true",
        help="交互确认后启动临时 Cloudflare Quick Tunnel",
    )
    parser.add_argument(
        "--cloudflared",
        help="cloudflared 可执行文件的明确路径；省略时仅查询 PATH",
    )
    verification_group = parser.add_mutually_exclusive_group()
    verification_group.add_argument(
        "--public-via-system-proxy",
        action="store_true",
        help="仅让公网 health/WSS 冒烟使用系统代理（代理可见完整临时 URL）",
    )
    verification_group.add_argument(
        "--phone-verifies-public",
        action="store_true",
        help="电脑 DNS 无法回环验证时，输出地址并改由手机执行首次公网验证",
    )
    parser.add_argument(
        "--auto-create-room",
        action="store_true",
        help="自动创建两人测试房并在同一窗口监视手机加入",
    )
    parser.add_argument(
        "--launch-desktop-host",
        action="store_true",
        help="启动可操作的 Windows 房主牌桌（不能与自动测试房同用）",
    )
    parser.add_argument(
        "--players",
        dest="desktop_player_count",
        type=int,
        default=2,
        help="Windows 房主房间的物理座位数（2–9，默认 2）",
    )
    args = parser.parse_args(argv)

    if args.cloudflared is not None and not args.public_quick_tunnel:
        _argument_error(parser, "--cloudflared 只能与 --public-quick-tunnel 一起使用")
    if args.public_via_system_proxy and not args.public_quick_tunnel:
        _argument_error(
            parser,
            "--public-via-system-proxy 只能与 --public-quick-tunnel 一起使用",
        )
    if args.phone_verifies_public and not args.public_quick_tunnel:
        _argument_error(
            parser,
            "--phone-verifies-public 只能与 --public-quick-tunnel 一起使用",
        )
    if args.auto_create_room and not args.public_quick_tunnel:
        _argument_error(
            parser,
            "--auto-create-room 只能与 --public-quick-tunnel 一起使用",
        )
    if args.launch_desktop_host and not args.public_quick_tunnel:
        _argument_error(
            parser,
            "--launch-desktop-host 只能与 --public-quick-tunnel 一起使用",
        )
    if args.launch_desktop_host and args.auto_create_room:
        _argument_error(
            parser,
            "--launch-desktop-host 不能与 --auto-create-room 同时使用",
        )

    executable: Path | None = None
    if args.public_quick_tunnel:
        if args.public_via_system_proxy:
            print("警告：公网 health/WSS 冒烟将使用当前系统代理。")
            print("该代理能够看到本次完整临时 WSS 地址；只可使用你信任的代理。")
        if args.phone_verifies_public:
            print("警告：电脑将不验证公网 WSS；完整地址必须先由你的手机实测。")
            print("手机连接成功前不得把该地址转发给朋友。")
        # 二次确认必须发生在文件检测、PATH 查询和任何进程启动之前。
        if not _confirm_public():
            print("未确认临时公网模式；未启动任何服务或子进程。")
            return 2
        config = cloudflared_config_path()
        if config is not None:
            print(
                "错误：检测到 ~/.cloudflared/config.yml 或 config.yaml；"
                "Quick Tunnel 与该配置不能安全共用。",
                file=sys.stderr,
            )
            print("启动器不会移动、改名或删除你的配置。", file=sys.stderr)
            return 2
        try:
            executable = find_cloudflared(args.cloudflared)
        except LauncherError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 2

    try:
        asyncio.run(
            run_alpha(
                port=args.port,
                public_quick_tunnel=args.public_quick_tunnel,
                public_via_system_proxy=args.public_via_system_proxy,
                phone_verifies_public=args.phone_verifies_public,
                auto_create_room=args.auto_create_room,
                launch_desktop_host=args.launch_desktop_host,
                desktop_player_count=args.desktop_player_count,
                cloudflared=executable,
            )
        )
    except KeyboardInterrupt:
        print("\n已停止朋友局 Alpha。")
        return 0
    except (LauncherError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
