"""《酒馆德州》入口。

默认进入主菜单(pygame 窗口);``--headless-screenshot <dir>`` 走无头截图管线；
``--friends-host`` 只接受受控启动器通过一次性环境传入的联机地址。
"""
from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Mapping
from urllib.parse import urlsplit

MIN_PYTHON = (3, 11)
FRIENDS_PUBLIC_WSS_ENV = "TAVERN_FRIENDS_PUBLIC_WSS"
FRIENDS_LOCAL_WS_ENV = "TAVERN_FRIENDS_LOCAL_WS"
FRIENDS_PLAYER_COUNT_ENV = "TAVERN_FRIENDS_PLAYER_COUNT"


def _valid_public_wss(value: object) -> bool:
    """校验仅用于邀请展示的公网 WSS；错误信息绝不回显原地址。"""
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value.strip())
        hostname = parsed.hostname
    except ValueError:
        return False
    return bool(
        parsed.scheme == "wss"
        and hostname
        and parsed.path
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )


def _wait_for_friends_client(
    client: object,
    predicate: Callable[[object], bool],
    *,
    timeout: float,
) -> object:
    """有限等待后台客户端；只根据公开快照判断，不读取网络线程内部。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = client.snapshot()  # type: ignore[attr-defined]
        if predicate(snapshot):
            return snapshot
        if getattr(snapshot, "status", None) in {"failed", "closed"}:
            raise RuntimeError("朋友局客户端未能连接")
        time.sleep(0.02)
    raise TimeoutError("朋友局客户端等待超时")


def _bootstrap_friends_host(
    client: object,
    local_ws_url: str,
    *,
    player_count: int = 2,
    timeout: float = 10.0,
) -> Mapping[str, object]:
    """连接 localhost 权威服务并创建 2–9 座 Windows 房间。"""
    if type(player_count) is not int or not 2 <= player_count <= 9:
        raise ValueError("朋友局房间座位数须为 2–9")
    client.start(local_ws_url)  # type: ignore[attr-defined]
    _wait_for_friends_client(
        client,
        lambda item: getattr(item, "status", None) == "connected",
        timeout=timeout,
    )
    request_id = client.create_room(  # type: ignore[attr-defined]
        "电脑房主",
        player_count=player_count,
        small_blind=5,
        big_blind=10,
        buyin=1000,
    )

    def room_ready(item: object) -> bool:
        room_id = getattr(item, "room_id", None)
        state = getattr(item, "state", None)
        if isinstance(room_id, str) and isinstance(state, Mapping):
            return True
        if getattr(item, "pending_request_id", None) != request_id:
            raise RuntimeError("创建朋友房间失败")
        return False

    snapshot = _wait_for_friends_client(client, room_ready, timeout=timeout)
    state = getattr(snapshot, "state", None)
    if not isinstance(state, Mapping):  # pragma: no cover - predicate invariant
        raise RuntimeError("朋友房间缺少初始状态")
    return state


def _run_friends_host() -> int:
    """由受控启动器进入 Windows 房主大厅；地址只从一次性环境读取。"""
    public_ws_url = os.environ.pop(FRIENDS_PUBLIC_WSS_ENV, "").strip()
    local_ws_url = os.environ.pop(FRIENDS_LOCAL_WS_ENV, "").strip()
    raw_player_count = os.environ.pop(FRIENDS_PLAYER_COUNT_ENV, "2").strip()
    if not _valid_public_wss(public_ws_url) or not local_ws_url:
        print("朋友局启动信息缺失，请使用专用启动脚本重试。")
        return 2
    try:
        player_count = int(raw_player_count)
    except ValueError:
        print("朋友局座位数无效，请选择 2–9 人。")
        return 2
    if not 2 <= player_count <= 9:
        print("朋友局座位数无效，请选择 2–9 人。")
        return 2

    from multiplayer_client import DesktopMultiplayerClient

    client = DesktopMultiplayerClient()
    try:
        initial_state = _bootstrap_friends_host(
            client,
            local_ws_url,
            player_count=player_count,
        )

        import pygame  # noqa: F401  延后到房间创建完成再打开窗口

        from ui.scenes.friends_room import FriendsRoomScene
        from ui.scenes.manager import SceneManager

        manager = SceneManager((1600, 900))
        manager.push(
            FriendsRoomScene(
                client,
                public_ws_url,
                initial_state=initial_state,
                exit_on_back=True,
            )
        )
        return manager.run()
    except Exception:
        # URL、token 与原始网络异常均不得进入终端或截图。
        print("朋友局房主启动失败，请关闭本轮服务后重新启动。")
        return 2
    finally:
        # SceneManager 正常退出会先触发 scene.on_exit；此处仍以幂等 close
        # 兜住窗口创建失败等尚未进入 manager.run() 的路径。
        try:
            client.close()
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < MIN_PYTHON:
        print(f"需要 Python >= {MIN_PYTHON},当前 {sys.version}")
        return 1
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--headless-screenshot":
        if len(argv) < 2:
            print("用法: main.py --headless-screenshot <输出目录>")
            return 2
        from tools.shots import render_all

        paths = render_all(argv[1])
        for p in paths:
            print(f"已生成 {p}")
        return 0
    if argv == ["--friends-host"]:
        return _run_friends_host()

    import pygame  # noqa: F401  确认 pygame 可用后再进场景

    from ui.scenes.manager import SceneManager
    from ui.scenes.menu import MenuScene

    manager = SceneManager((1600, 900))
    manager.push(MenuScene())
    return manager.run()


if __name__ == "__main__":
    sys.exit(main())
