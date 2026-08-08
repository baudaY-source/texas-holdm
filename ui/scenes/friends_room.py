"""朋友联机大厅与远端牌桌。

本场景刻意保持“薄客户端”：只消费服务端逐座位投影，所有准备、开局、
下注、亮牌与补码决定都作为意图交给权威服务端。大厅和牌桌共用同一个
场景及同一条会话，避免切换画面时丢失 WebSocket 或恢复凭据。

网络实现不得在工作线程里调用 pygame；它只需实现 :class:`FriendsClient`
并通过线程安全队列把消息交给 ``poll``。恢复 token 只属于客户端实现，
本场景既不读取也不显示它。
"""
from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

import pygame

from ai.hand_rank import HandCategory, HandSummary, describe_holdem_hand
from ai.personas import Persona, persona_by_id, persona_catalog
from ai.styles import StylePreset, style_catalog
from multiplayer.projection import PROJECTION_SCHEMA
from multiplayer.room import ROOM_STATE_SCHEMA

from .. import cards, fx, theme
from ..characters import ACTED, FOLDED, IDLE, THINKING, Bust, draw_chip_pile, draw_name_plate
from ..widgets import Button, NumberField, Panel, Slider, ToastLog
from .manager import Scene, SceneManager

TABLE_C = (615, 460)
FELT_RX, FELT_RY = 480, 270
BOARD_Y = 460
PANEL_X = 1225
POT_POS = (TABLE_C[0], BOARD_Y - 105)
DECK_POS = (1000, 500)
MUCK_POS = (820, 535)

_ROOM_PHASES = frozenset({"LOBBY", "PLAYING", "BETWEEN_HANDS", "CLOSED"})
_STREET_LABEL = {
    "PREFLOP": "翻牌前",
    "FLOP": "翻牌圈",
    "TURN": "转牌圈",
    "RIVER": "河牌圈",
    "SHOWDOWN": "摊牌",
    "HAND_OVER": "本手结束",
}
_CARD_RANKS = frozenset("23456789TJQKA")
_CARD_SUITS = frozenset("cdhs")

# 服务端在结算后保留约 3 秒再自动续手。薄客户端在这段时间内先完成
# 跑牌、摊牌与派彩展示；仅本地延后处置控件，不延后权威服务器续手。
_SETTLEMENT_HOLD_SECONDS = 2.6

_ACTION_COLOR = {
    "fold": (166, 108, 92),
    "check": theme.TEAL,
    "call": theme.TEAL,
    "bet": theme.AMBER_LIGHT,
    "raise": theme.AMBER_LIGHT,
    "allin": theme.DANGER,
}


@dataclass
class _ActionEcho:
    """由相邻权威版本差异推导出的短暂行动提示。"""

    seat: int
    label: str
    color: tuple[int, int, int]
    elapsed: float = 0.0
    duration: float = 0.92


@dataclass
class _HiddenCard:
    """发牌飞行完成前暂不绘制的静态牌。"""

    key: tuple[str, int, int]
    remaining: float


@runtime_checkable
class FriendsClient(Protocol):
    """pygame 场景需要的最小非阻塞客户端接口。

    ``poll`` 可返回完整 wire 消息，也可直接返回已校验的 room-state。
    ``send_command`` 负责补 UUID、房间码并编码 envelope；场景只提供当前
    ``expected_state``。``close`` 必须是幂等、快速且不得等待网络 I/O。
    """

    room_id: str

    def poll(self) -> Sequence[object]:
        """取走当前已经收到的消息；没有消息时立即返回空序列。"""

    def send_command(
        self,
        message_type: str,
        body: Mapping[str, object],
        *,
        expected_state: int,
    ) -> str | None:
        """非阻塞提交一条房内命令。"""

    def close(self) -> None:
        """请求关闭会话；不得把 URL 或恢复凭据写入日志。"""


class FriendsInfoScene(Scene):
    """普通主菜单进入的 Alpha 说明页；绝不从 pygame 启动外部进程。"""

    def __init__(
        self,
        manager: SceneManager | None = None,
        *,
        seed: int | None = None,
    ) -> None:
        super().__init__(manager)
        self.seed = seed
        self._t = 0.0
        self.smoke = fx.ParticleSystem(
            (260, 170, 1340, 820), seed=seed, max_particles=30
        )
        self._glow = fx.radial_glow(1050, (255, 182, 98), 145, power=1.9)
        self._vignette = fx.vignette((1600, 900), 155)
        self._grain = fx.grain_overlay((1600, 900), seed=37)
        self.btn_back = Button(
            (650, 704, 300, 54), "返回主菜单", self._back_to_menu, size=22
        )

    def _back_to_menu(self) -> None:
        if self.manager is not None:
            from .menu import MenuScene

            self.manager.replace(MenuScene(seed=self.seed))

    def handle_event(self, ev: pygame.event.Event) -> None:
        self.btn_back.handle_event(ev)
        if ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE:
            self._back_to_menu()

    def update(self, dt: float) -> None:
        self._t += dt
        self.smoke.update(dt)

    def draw(self, dst: pygame.Surface) -> None:
        dst.fill(theme.BG)
        breathe = 1.0 + 0.018 * math.sin(self._t * 2.1)
        size = round(1050 * breathe)
        glow = pygame.transform.smoothscale(self._glow, (size, size))
        dst.blit(glow, glow.get_rect(center=(800, 420)))
        self.smoke.draw(dst)
        dst.blit(self._vignette, (0, 0))
        dst.blit(self._grain, (0, 0))

        Panel.draw(dst, (300, 126, 1000, 606), alpha=236, border=theme.AMBER_DARK, radius=18)
        theme.text(dst, "朋友联机 Alpha", (800, 183), 46, theme.AMBER_LIGHT, "center", shadow=True)
        theme.text(
            dst,
            "保留单机模式 · 联机由独立安全启动器开启",
            (800, 238),
            17,
            theme.TEAL,
            "center",
        )
        Panel.draw(dst, (410, 292, 780, 90), alpha=220, border=theme.FELT_EDGE)
        theme.text(dst, "请先退出当前游戏，在项目根目录运行：", (800, 310), 17, theme.TEXT_DIM, "center")
        theme.text(
            dst,
            ".\\tools\\run_friends_desktop_public.cmd",
            (800, 354),
            20,
            theme.GOLD,
            "center",
            shadow=True,
        )
        lines = (
            "启动器会建立本机权威服务与临时 WSS，并打开 Windows 房主牌桌。",
            "手机粘贴 WSS、输入房间码后，即可与电脑完成 HU 对局测试。",
            "Quick Tunnel 只用于短时联调；关闭联机窗口或按 Ctrl+C 即可结束。",
            "游戏界面不会自行执行脚本，也不会自动复制或保存邀请地址。",
        )
        for row, line in enumerate(lines):
            theme.text(
                dst,
                line,
                (800, 445 + row * 43),
                16,
                theme.TEXT if row < 2 else theme.TEXT_DIM,
                "center",
            )
        self.btn_back.draw(dst)
        theme.text(dst, "ESC 返回", (320, 710), 13, theme.TEXT_DIM, "bottomleft")


def _seat_layout(player_count: int) -> dict[int, tuple[float, float]]:
    """视觉座位 0 固定在下方，其余位置沿桌外圈排布。"""
    if not 2 <= player_count <= 9:
        raise ValueError("联机牌桌人数须为 2–9")
    rx, ry = 475.0, 322.0
    return {
        visual: (
            TABLE_C[0]
            + rx * math.cos(math.pi / 2 - math.tau * visual / player_count),
            TABLE_C[1]
            + ry * math.sin(math.pi / 2 - math.tau * visual / player_count),
        )
        for visual in range(player_count)
    }


def _unit_to_center(anchor: tuple[float, float]) -> tuple[float, float]:
    vx, vy = TABLE_C[0] - anchor[0], TABLE_C[1] - anchor[1]
    length = math.hypot(vx, vy) or 1.0
    return vx / length, vy / length


def _integer(value: object, default: int = 0) -> int:
    return value if type(value) is int else default


def _mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _sequence(value: object) -> Sequence[object] | None:
    return (
        value
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))
        else None
    )


def _card_list(value: object) -> tuple[str, ...]:
    raw = _sequence(value)
    if raw is None:
        return ()
    result: list[str] = []
    for item in raw:
        if (
            not isinstance(item, str)
            or len(item) != 2
            or item[0] not in _CARD_RANKS
            or item[1] not in _CARD_SUITS
        ):
            return ()
        result.append(item)
    return tuple(result)


class FriendsRoomScene(Scene):
    """一条已创建房主会话对应的联机大厅/牌桌场景。

    ``client`` 已完成 hello + room.create，且自身持有恢复凭据。``public_wss``
    只用于把手机邀请信息画在大厅，不会打印或写入磁盘。
    """

    def __init__(
        self,
        client: FriendsClient,
        public_wss: str,
        manager: SceneManager | None = None,
        *,
        seed: int | None = None,
        initial_state: Mapping[str, object] | None = None,
        exit_on_back: bool = False,
    ) -> None:
        super().__init__(manager)
        room_id = getattr(client, "room_id", None)
        if not isinstance(room_id, str) or not room_id:
            raise ValueError("联机客户端缺少房间码")
        try:
            parsed_invite = urlsplit(public_wss) if isinstance(public_wss, str) else None
            invite_host = None if parsed_invite is None else parsed_invite.hostname
        except ValueError:
            parsed_invite = None
            invite_host = None
        if (
            parsed_invite is None
            or parsed_invite.scheme != "wss"
            or not invite_host
            or not parsed_invite.path
            or parsed_invite.username is not None
            or parsed_invite.password is not None
            or bool(parsed_invite.query)
            or bool(parsed_invite.fragment)
        ):
            raise ValueError("手机邀请地址必须使用 wss://")
        self.client = client
        self.room_id = room_id
        self.public_wss = public_wss
        self.seed = seed
        self.exit_on_back = exit_on_back
        self.state: Mapping[str, object] | None = None
        self.state_version = -1
        self._closed = False
        self._pending_command: str | None = None
        self._pending_version = -1
        self._connection_error: str | None = None
        self._raise_open = False
        self._raise_bounds: tuple[int, int, str] | None = None
        self._rebuy_signature: tuple[int, int] | None = None
        self._copy_notice: tuple[str, tuple[int, int, int]] | None = None
        self._copy_notice_left = 0.0
        self._t = 0.0
        self._rng = random.Random(seed)

        # 联机视觉只消费安全投影。相邻版本才推导演出；断线恢复若跨过版本，
        # 直接吸附到最新状态，绝不根据缺失中间态猜测动作或牌面。
        self.anim = fx.CardAnimator()
        self.muck = fx.MuckAnimator()
        self.chipfly = fx.ChipFly()
        self._action_echoes: list[_ActionEcho] = []
        self._hidden_cards: dict[tuple[str, int, int], float] = {}
        self._payout_holds: dict[int, int] = {}
        self._last_transition_version = -1
        self._settlement_hand_id: int | None = None
        self._settlement_hold_until = 0.0
        self._ai_busts: dict[tuple[int, str], Bust] = {}
        self._seat_hitboxes: dict[int, pygame.Rect] = {}
        self._decision_hitboxes: dict[tuple[str, int], pygame.Rect] = {}
        self._seat_dialog: _OnlineSeatDialog | None = None
        self._topup_dialog: _OnlineTopUpDialog | None = None
        self._ai_style_dialog: _OnlineAiStyleDialog | None = None
        self._overlay_view: str | None = None
        self._seen_low_stack_prompts: set[tuple[int, int]] = set()
        self._pending_low_stack_decline: tuple[int, int] | None = None

        self.smoke = fx.ParticleSystem(
            (240, 140, 1120, 820), seed=seed, max_particles=34
        )
        self._glow = fx.radial_glow(1120, (255, 180, 96), 155, power=1.9)
        self._vignette = fx.vignette((1600, 900), 145, center=TABLE_C)
        self._grain = fx.grain_overlay((1600, 900), seed=29)
        self.log = ToastLog(capacity=18)
        self.log.add("已连接朋友局 · 等待所有玩家准备")

        # 大厅控件
        self.btn_ready = Button(
            (330, 790, 260, 54), "准备", self._toggle_ready, size=22
        )
        self.btn_start = Button(
            (620, 790, 260, 54), "开始牌局", self._start_game, size=22
        )
        self.btn_copy_wss = Button(
            (1040, 273, 160, 34), "复制 WSS", self._copy_wss, size=15
        )
        self.btn_back = Button(
            (910, 790, 260, 54),
            "断开并退出" if exit_on_back else "断开并返回",
            self._back_to_menu,
            size=21,
            danger=True,
        )

        # 牌桌行动控件
        self.btn_fold = Button(
            (52, 840, 126, 46), "弃牌 F", lambda: self._send_action("fold"), danger=True
        )
        self.btn_call = Button((194, 840, 166, 46), "过牌 C", self._call_or_check)
        self.btn_raise = Button((376, 840, 140, 46), "加注 R", self._raise)
        self.slider = Slider((548, 856, 310, 16), 0, 0)
        self.btn_allin = Button(
            (878, 840, 118, 46), "全下", lambda: self._send_action("allin"), danger=True
        )
        self._chips: list[Button] = []
        for index, (label, fraction) in enumerate(
            (("33%", 0.33), ("50%", 0.5), ("75%", 0.75), ("底池", 1.0), ("全下", -1.0))
        ):
            self._chips.append(
                Button(
                    (862 + index * 66, 846, 58, 32),
                    label,
                    lambda value=fraction: self._set_wager_fraction(value),
                    size=16,
                )
            )
        self.btn_show = Button((420, 760, 170, 44), "SHOW 手牌", self._show_cards, size=18)
        self.btn_next = Button((610, 760, 170, 44), "下一手", self._next_hand, size=19)
        self.btn_table_back = Button(
            (1420, 14, 154, 38),
            "断开并退出" if exit_on_back else "断开并返回",
            self._back_to_menu,
            size=18,
            danger=True,
        )

        # 爆仓处置控件；范围会随房间盲注同步。
        self.rebuy_field = NumberField(
            (610, 706, 120, 38), "重新买入", 1000, minimum=100, maximum=10_000
        )
        self.btn_rebuy = Button((760, 700, 150, 46), "补回买入", self._rebuy, size=18)
        self.btn_leave_seat = Button(
            (930, 700, 150, 46), "离开座位", self._leave_seat, size=18, danger=True
        )
        self.btn_release = Button(
            (1042, 790, 164, 42), "离开座位", self._release_seat, size=16, danger=True
        )
        self.btn_topup = Button(
            (1412, 654, 142, 38), "申请补码", self._open_topup, size=15
        )
        self.btn_pause = Button(
            (1260, 654, 142, 38), "暂停对局", self._toggle_remote_pause, size=15
        )
        self.btn_history = Button(
            (1260, 702, 142, 38), "过往牌局", lambda: self._set_overlay("history"), size=15
        )
        self.btn_stacks = Button(
            (1412, 702, 142, 38), "筹码总览", lambda: self._set_overlay("stacks"), size=15
        )
        self.btn_overlay_close = Button(
            (1015, 752, 150, 40), "返回牌桌", lambda: self._set_overlay(None), size=16
        )

        if initial_state is not None:
            self._accept_state(initial_state)

    # ---------------------------------------------------------- 生命周期/消息

    def on_exit(self) -> None:
        self._close_client()

    def _close_client(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.client.close()
        except Exception:
            # 网络异常中退出仍必须能回到单机菜单；异常文本可能含完整 URL。
            pass

    def _back_to_menu(self) -> None:
        self._close_client()
        if self.manager is not None:
            if self.exit_on_back:
                self.manager.quit()
                return
            from .menu import MenuScene

            self.manager.replace(MenuScene(seed=self.seed, toast="朋友局连接已关闭"))

    def _poll_client(self) -> None:
        if self._closed:
            return
        try:
            messages = self.client.poll()
        except Exception:
            self._connection_error = "连接状态读取失败"
            self._reject_pending_command()
            return
        for message in messages:
            if not isinstance(message, Mapping):
                # 正式客户端使用冻结 ClientEvent；pygame 层只消费其公开字段，
                # 最新完整状态统一从 ``latest_state`` 读取。
                kind_value = getattr(message, "kind", "")
                kind_value = getattr(kind_value, "value", kind_value)
                event_type = getattr(message, "type", "")
                event_type = getattr(event_type, "value", event_type)
                label = f"{kind_value} {event_type}".casefold()
                if "disconnected" in label:
                    self._connection_error = "连接中断，正在尝试恢复"
                elif "reconnected" in label or "connected" in label:
                    self._connection_error = None
                elif "closed" in label:
                    self._connection_error = "连接已经关闭"
                elif "error" in label:
                    code = getattr(message, "code", None)
                    safe_code = code if isinstance(code, str) else "UNKNOWN"
                    self.log.add(f"服务器拒绝操作 · {safe_code}")
                    self._reject_pending_command()
                elif "ack" in label:
                    self._pending_command = None
                if "state" in label:
                    event_state = getattr(message, "state", None)
                    if isinstance(event_state, Mapping):
                        self._accept_state(event_state)
                continue
            message_type = message.get("type")
            if message_type == "room.state":
                state = _mapping(message.get("body"))
                if state is not None:
                    self._accept_state(state)
                continue
            if message_type == "ack":
                state = _mapping(message.get("state"))
                if state is not None:
                    self._accept_state(state)
                self._pending_command = None
                continue
            if message_type == "error":
                detail = _mapping(message.get("error"))
                code = detail.get("code") if detail is not None else None
                safe_code = code if isinstance(code, str) else "UNKNOWN"
                self.log.add(f"服务器拒绝操作 · {safe_code}")
                self._reject_pending_command()
                continue
            # 测试/嵌入客户端可直接交付 room-state，减少一次 envelope 包装。
            if message.get("schema") == ROOM_STATE_SCHEMA:
                self._accept_state(message)
        latest_state = getattr(self.client, "latest_state", None)
        if isinstance(latest_state, Mapping):
            self._accept_state(latest_state)

    def _reject_pending_command(self) -> None:
        """释放被服务器拒绝的本地意图，并允许低码决定重新提交。"""

        if (
            self._pending_command == "seat.topup.decline"
            and self._pending_low_stack_decline is not None
        ):
            self._seen_low_stack_prompts.discard(self._pending_low_stack_decline)
            self._pending_low_stack_decline = None
        self._pending_command = None

    def _accept_state(self, state: Mapping[str, object]) -> bool:
        """只接受本房间、本人视角、单调版本的完整房间投影。"""
        version = state.get("state_version")
        viewer = state.get("viewer_seat")
        phase = state.get("phase")
        seats = state.get("seats")
        config = _mapping(state.get("config"))
        if (
            state.get("schema") != ROOM_STATE_SCHEMA
            or state.get("room") != self.room_id
            or type(version) is not int
            or version < 0
            or version < self.state_version
            or (viewer is not None and type(viewer) is not int)
            or not isinstance(phase, str)
            or phase not in _ROOM_PHASES
            or _sequence(seats) is None
            or config is None
        ):
            return False
        player_count = config.get("player_count")
        if type(player_count) is not int or not 2 <= player_count <= 9:
            return False
        raw_seats = _sequence(seats)
        assert raw_seats is not None
        if (
            (viewer is not None and not 0 <= viewer < player_count)
            or len(raw_seats) != player_count
        ):
            return False
        seat_ids: list[int] = []
        for seat in raw_seats:
            if not isinstance(seat, Mapping):
                return False
            seat_id = seat.get("seat")
            if type(seat_id) is not int or type(seat.get("occupied")) is not bool:
                return False
            seat_ids.append(seat_id)
        if sorted(seat_ids) != list(range(player_count)):
            return False
        viewer_waiting = False
        if viewer is not None:
            viewer_member_id = state.get("viewer_member_id")
            members = _sequence(state.get("members"))
            viewer_member = next(
                (
                    member
                    for member in members or ()
                    if isinstance(member, Mapping)
                    and member.get("member_id") == viewer_member_id
                ),
                None,
            )
            viewer_room_seat = next(
                (
                    seat
                    for seat in raw_seats
                    if isinstance(seat, Mapping)
                    and seat.get("seat") == viewer
                    and seat.get("member_id") == viewer_member_id
                ),
                None,
            )
            if viewer_member is not None and viewer_room_seat is not None:
                member_waiting = viewer_member.get("waiting_next_hand") is True
                seat_waiting = viewer_room_seat.get("waiting_next_hand") is True
                if member_waiting != seat_waiting:
                    return False
                viewer_waiting = member_waiting
        table = state.get("table")
        if table is not None:
            table_map = _mapping(table)
            expected_table_viewer = None if viewer_waiting else viewer
            if (
                table_map is None
                or table_map.get("schema") != PROJECTION_SCHEMA
                or table_map.get("room") != self.room_id
                or table_map.get("viewer_seat") != expected_table_viewer
                or table_map.get("state_version") != version
                or (viewer_waiting and table_map.get("legal_actions") is not None)
            ):
                return False

        previous_state = self.state
        previous_phase = self.phase
        previous_version = self.state_version
        advanced = self.state is None or version > self.state_version
        self.state = state
        self.state_version = version
        self._register_settlement_hold()
        if self._paused():
            self._seat_dialog = None
            self._topup_dialog = None
            self._ai_style_dialog = None
            self._raise_open = False
        if advanced:
            if previous_state is not None and version == previous_version + 1:
                self._queue_state_transition(previous_state, state)
            elif previous_state is not None:
                self._absorb_visual_state()
            else:
                self._prime_visual_state(state)
            self._last_transition_version = version
        # 正式客户端在断线时仍保留最后一份冻结状态；重复读取同版本不得
        # 把“正在恢复”提示误清掉，只有新权威版本或 reconnected 事件可清除。
        if advanced:
            self._connection_error = None
        if self._pending_command is not None and version > self._pending_version:
            if self._pending_low_stack_decline is not None:
                active_signature = self._active_low_stack_signature()
                if active_signature == self._pending_low_stack_decline:
                    # 权威版本已前进但提示仍存在，不能把一次失败/无效决定
                    # 永久当作已处理。
                    self._seen_low_stack_prompts.discard(
                        self._pending_low_stack_decline
                    )
                self._pending_low_stack_decline = None
            self._pending_command = None
        if previous_phase != phase:
            if phase == "PLAYING":
                self.log.add("牌局开始 · 所有动作由服务器确认")
            elif phase == "BETWEEN_HANDS":
                self.log.add("本手已经结算")
            elif phase == "CLOSED":
                self.log.add("房间已关闭")
        self._sync_rebuy_field()
        self._refresh_low_stack_tracking()
        self._maybe_prompt_low_stack()
        return True

    def _absorb_visual_state(self) -> None:
        """跨版本恢复时清空推导演出并直接显示权威最新态。"""
        self.anim.clear()
        self.muck.clear()
        self.chipfly.clear()
        self._action_echoes.clear()
        self._hidden_cards.clear()
        self._payout_holds.clear()

    def _prime_visual_state(self, state: Mapping[str, object]) -> None:
        """第一份状态没有可比较前态，只建立静态画面。"""
        del state
        self._absorb_visual_state()

    def _register_settlement_hold(self) -> None:
        """新结算态先留给跑牌/派彩演出，之后才开放处置控件。"""

        if self.phase != "BETWEEN_HANDS":
            self._settlement_hold_until = 0.0
            return
        table = self._table()
        result = _mapping(table.get("result")) if table is not None else None
        if result is None:
            return
        hand_id = _integer(
            result.get("hand_id"),
            _integer(table.get("hand_id"), -1) if table is not None else -1,
        )
        if hand_id < 0 or hand_id == self._settlement_hand_id:
            return
        self._settlement_hand_id = hand_id
        self._settlement_hold_until = self._t + _SETTLEMENT_HOLD_SECONDS
        self._seat_dialog = None
        self._topup_dialog = None
        self._ai_style_dialog = None
        self._raise_open = False

    def _settlement_locked(self) -> bool:
        """结算演出期间仅允许查看公开结果，不开放牌桌业务决定。"""

        if self.phase != "BETWEEN_HANDS":
            return False
        table = self._table()
        result = _mapping(table.get("result")) if table is not None else None
        if result is None:
            return False
        hand_id = _integer(
            result.get("hand_id"),
            _integer(table.get("hand_id"), -1) if table is not None else -1,
        )
        if hand_id != self._settlement_hand_id:
            return False
        return (
            self._t < self._settlement_hold_until
            or self.anim.busy
            or self.chipfly.busy_for("payout")
            or any(key[0] == "board" for key in self._hidden_cards)
        )

    def _queue_state_transition(
        self,
        previous: Mapping[str, object],
        current: Mapping[str, object],
    ) -> None:
        """只用两个相邻安全投影生成演出，不读取全知历史。"""
        old_table = _mapping(previous.get("table"))
        new_table = _mapping(current.get("table"))
        if new_table is None:
            return
        if old_table is None or old_table.get("hand_id") != new_table.get("hand_id"):
            self._queue_new_hand_deal(new_table)
            return

        old_seats = {
            _integer(item.get("seat"), -1): item
            for item in (_sequence(old_table.get("seats")) or ())
            if isinstance(item, Mapping)
        }
        new_seats = {
            _integer(item.get("seat"), -1): item
            for item in (_sequence(new_table.get("seats")) or ())
            if isinstance(item, Mapping)
        }
        actor = _integer(old_table.get("acting_seat"), -1)
        transition = _mapping(current.get("transition"))
        action_transition = (
            transition
            if transition is not None
            and transition.get("kind") == "ACTION"
            and transition.get("state_version") == current.get("state_version")
            else None
        )
        if action_transition is not None:
            actor = _integer(action_transition.get("target_seat"), actor)
        old_board = _card_list(old_table.get("board"))
        new_board = _card_list(new_table.get("board"))

        # 弃牌先从旧投影捕获牌背/本人牌面，再让新投影隐藏静态底牌。
        for seat_id, old_seat in old_seats.items():
            new_seat = new_seats.get(seat_id)
            if new_seat is None:
                continue
            if (
                old_seat.get("folded") is not True
                and new_seat.get("folded") is True
                and action_transition is None
            ):
                visuals: list[tuple[pygame.Surface, tuple[float, float], float]] = []
                visible = _card_list(old_seat.get("cards"))
                for index in range(2):
                    is_viewer = seat_id == self.viewer_seat
                    code = visible[index] if is_viewer and index < len(visible) else "back"
                    size = cards.SIZE_HOLE if is_viewer else cards.SIZE_MINI
                    rotation = (-7.0 if index == 0 else 7.0) if is_viewer else (-4.0 if index == 0 else 4.0)
                    visuals.append(
                        (
                            cards.card_surface(code, size),
                            self._hole_pos_for(seat_id, index),
                            rotation,
                        )
                    )
                self.muck.launch(visuals, MUCK_POS, seat_id)
                self._add_action_echo(seat_id, "弃牌", "fold")

        # 当前街投入增加时，筹码由桌前码量飞到下注位。若没有筹码变化，
        # 但行动权已经越过旧行动者，则该动作只能安全描述为过牌。
        actor_changed = old_table.get("acting_seat") != new_table.get("acting_seat")
        if action_transition is not None and actor in new_seats and actor in old_seats:
            self._queue_authoritative_action(action_transition, old_seats[actor])
        elif actor in new_seats and actor in old_seats:
            old_actor = old_seats[actor]
            new_actor = new_seats[actor]
            paid = max(
                0,
                _integer(new_actor.get("bet")) - _integer(old_actor.get("bet")),
                _integer(old_actor.get("stack")) - _integer(new_actor.get("stack")),
            )
            if paid:
                self.chipfly.launch_amount(
                    self._stack_pos_for(actor),
                    self._bet_pos_for(actor),
                    paid,
                    self._rng,
                    group=f"wager:{current.get('state_version')}:{actor}",
                    max_chips=12,
                )
                if new_actor.get("all_in") is True:
                    self._add_action_echo(actor, f"全下 · {paid}", "allin")
                else:
                    old_high = max(
                        (_integer(item.get("bet")) for item in old_seats.values()),
                        default=0,
                    )
                    # 恰好结束街道时服务端的新 bet 已归零；old bet + paid
                    # 仍是该动作发生瞬间的真实“加到”额度。
                    new_bet = max(
                        _integer(new_actor.get("bet")),
                        _integer(old_actor.get("bet")) + paid,
                    )
                    if new_bet > old_high:
                        kind = "bet" if old_high == 0 else "raise"
                        verb = "下注" if kind == "bet" else "加注到"
                        self._add_action_echo(actor, f"{verb} {new_bet}", kind)
                    else:
                        self._add_action_echo(actor, f"跟注 {paid}", "call")
            elif actor_changed and new_actor.get("folded") is not True:
                self._add_action_echo(actor, "过牌", "check")

        payout_delay = 0.18
        if len(new_board) > len(old_board):
            # 街道推进时旧街下注进入中央底池；公共牌从牌堆逐张落下。
            for seat_id, old_seat in old_seats.items():
                amount = max(0, _integer(old_seat.get("bet")))
                if seat_id == actor:
                    amount += max(
                        0,
                        _integer(old_seat.get("stack"))
                        - _integer(new_seats.get(seat_id, {}).get("stack")),
                    )
                if amount:
                    self.chipfly.launch_amount(
                        self._bet_pos_for(seat_id),
                        POT_POS,
                        amount,
                        self._rng,
                        group=f"collect:{current.get('state_version')}",
                        max_chips=10,
                    )
            self._queue_board_deal(new_board, len(old_board))
            runout_count = len(new_board) - len(old_board)
            payout_delay = (
                (runout_count - 1) * 0.12
                + 0.34
                + fx.CardAnimator.FLIP_TIME * 2
                + 0.20
            )

        if old_table.get("result") is None and _mapping(new_table.get("result")) is not None:
            self._queue_payouts(new_table, delay=payout_delay)

    def _queue_authoritative_action(
        self,
        transition: Mapping[str, object],
        old_seat: Mapping[str, object],
    ) -> None:
        seat = _integer(transition.get("target_seat"), -1)
        action = transition.get("action")
        if seat < 0 or not isinstance(action, str):
            return
        kind = action.lower()
        amount = max(0, _integer(transition.get("amount")))
        paid = max(0, _integer(transition.get("paid")))
        if kind == "fold":
            visuals: list[tuple[pygame.Surface, tuple[float, float], float]] = []
            visible = _card_list(old_seat.get("cards"))
            for index in range(2):
                is_viewer = seat == self.viewer_seat
                code = visible[index] if is_viewer and index < len(visible) else "back"
                size = cards.SIZE_HOLE if is_viewer else cards.SIZE_MINI
                rotation = (-7.0 if index == 0 else 7.0) if is_viewer else (-4.0 if index == 0 else 4.0)
                visuals.append((cards.card_surface(code, size), self._hole_pos_for(seat, index), rotation))
            self.muck.launch(visuals, MUCK_POS, seat)
        if paid:
            self.chipfly.launch_amount(
                self._stack_pos_for(seat),
                self._bet_pos_for(seat),
                paid,
                self._rng,
                group=f"wager:{transition.get('state_version')}:{seat}",
                max_chips=12,
            )
        verb = {
            "fold": "弃牌",
            "check": "过牌",
            "call": f"跟注 {paid or amount}",
            "bet": f"下注 {amount}",
            "raise": f"加注到 {amount}",
            "allin": f"全下 · {paid or amount}",
        }.get(kind, action)
        self._add_action_echo(seat, verb, kind)

    def _queue_new_hand_deal(self, table: Mapping[str, object]) -> None:
        self.anim.clear()
        self.muck.clear()
        self.chipfly.clear()
        self._hidden_cards.clear()
        seats = [
            item
            for item in (_sequence(table.get("seats")) or ())
            if isinstance(item, Mapping) and item.get("in_hand") is True
        ]
        delay = 0.0
        for round_index in range(2):
            for seat in seats:
                seat_id = _integer(seat.get("seat"), -1)
                if seat_id < 0:
                    continue
                visible = _card_list(seat.get("cards"))
                is_viewer = seat_id == self.viewer_seat
                code = visible[round_index] if is_viewer and round_index < len(visible) else "back"
                size = cards.SIZE_HOLE if is_viewer else cards.SIZE_MINI
                finish = delay + 0.34
                key = ("hole", seat_id, round_index)
                self._hidden_cards[key] = finish
                self.anim.add_deal(
                    cards.card_surface(code, size),
                    cards.card_surface("back", size),
                    DECK_POS,
                    self._hole_pos_for(seat_id, round_index),
                    delay=delay,
                    duration=0.34,
                    rot=(-7 if round_index == 0 else 7) if is_viewer else (-4 if round_index == 0 else 4),
                    face_up=is_viewer,
                )
                delay += 0.045

    def _queue_board_deal(self, board: tuple[str, ...], start: int) -> None:
        for offset, index in enumerate(range(start, len(board))):
            delay = offset * 0.12
            finish = delay + 0.34 + fx.CardAnimator.FLIP_TIME * 2
            self._hidden_cards[("board", 0, index)] = finish
            front = cards.card_surface(board[index], cards.SIZE_BOARD)
            back = cards.card_surface("back", cards.SIZE_BOARD)
            self.anim.add_deal(
                front,
                back,
                DECK_POS,
                (TABLE_C[0] + (index - 2) * 70, BOARD_Y),
                delay=delay,
                duration=0.34,
                face_up=False,
                flip=True,
            )

    def _queue_payouts(
        self,
        table: Mapping[str, object],
        *,
        delay: float = 0.18,
    ) -> None:
        result = _mapping(table.get("result"))
        if result is None:
            return
        awards = _sequence(result.get("pot_awards")) or ()
        self._payout_holds.clear()
        for award in awards:
            if not isinstance(award, Mapping):
                continue
            for payout in _sequence(award.get("payouts")) or ():
                if not isinstance(payout, Mapping):
                    continue
                seat_id = _integer(
                    payout.get("target_seat"),
                    _integer(payout.get("seat"), -1),
                )
                amount = max(0, _integer(payout.get("amount")))
                if seat_id < 0 or amount <= 0:
                    continue
                self._payout_holds[seat_id] = self._payout_holds.get(seat_id, 0) + amount
                self.chipfly.launch_amount(
                    POT_POS,
                    self._stack_pos_for(seat_id),
                    amount,
                    self._rng,
                    delay=delay,
                    group="payout",
                    max_chips=18,
                )
                delay += 0.12

    def _add_action_echo(self, seat: int, label: str, kind: str) -> None:
        self._action_echoes.append(
            _ActionEcho(seat, label, _ACTION_COLOR.get(kind, theme.TEXT))
        )
        table_seat = next(
            (item for item in self._table_seats() if item.get("seat") == seat),
            None,
        )
        name = (
            table_seat.get("name")
            if table_seat is not None and isinstance(table_seat.get("name"), str)
            else f"座位 {seat + 1}"
        )
        self.log.add(f"{name} · {label}")

    def _send(self, message_type: str, body: Mapping[str, object] | None = None) -> bool:
        if (
            self._closed
            or self.state is None
            or not self._can_submit()
        ):
            return False
        try:
            self.client.send_command(
                message_type,
                {} if body is None else body,
                expected_state=self.state_version,
            )
        except Exception:
            self._connection_error = "操作发送失败"
            return False
        self._pending_command = message_type
        self._pending_version = self.state_version
        return True

    # ---------------------------------------------------------- 状态助手

    @property
    def phase(self) -> str:
        if self.state is None:
            return "CONNECTING"
        value = self.state.get("phase")
        return value if isinstance(value, str) else "CONNECTING"

    @property
    def viewer_seat(self) -> int | None:
        value = self.state.get("viewer_seat") if self.state else None
        return value if type(value) is int else None

    @property
    def host_seat(self) -> int | None:
        value = self.state.get("host_seat") if self.state else None
        return value if type(value) is int else None

    @property
    def viewer_is_host(self) -> bool:
        if self.state is None:
            return False
        explicit = self.state.get("viewer_is_host")
        if isinstance(explicit, bool):
            return explicit
        # v1 兼容路径；v2 不再把房主身份绑定到固定座位。
        return self.viewer_seat is not None and self.viewer_seat == self.host_seat

    def _room_seats(self) -> list[Mapping[str, object]]:
        if self.state is None:
            return []
        raw = _sequence(self.state.get("seats"))
        return [] if raw is None else [seat for seat in raw if isinstance(seat, Mapping)]

    def _room_members(self) -> list[Mapping[str, object]]:
        if self.state is None:
            return []
        raw = _sequence(self.state.get("members"))
        return [] if raw is None else [member for member in raw if isinstance(member, Mapping)]

    def _viewer_member(self) -> Mapping[str, object] | None:
        if self.state is None:
            return None
        direct = _mapping(self.state.get("viewer_member"))
        if direct is not None:
            return direct
        for member in self._room_members():
            if member.get("is_viewer") is True:
                return member
        if self.viewer_seat is None:
            return None
        return next(
            (seat for seat in self._room_seats() if seat.get("seat") == self.viewer_seat),
            None,
        )

    def _viewer_room_seat(self) -> Mapping[str, object] | None:
        if self.viewer_seat is None:
            return None
        return next(
            (seat for seat in self._room_seats() if seat.get("seat") == self.viewer_seat),
            None,
        )

    def _table(self) -> Mapping[str, object] | None:
        return _mapping(self.state.get("table")) if self.state is not None else None

    def _table_seats(self) -> list[Mapping[str, object]]:
        table = self._table()
        raw = table.get("seats") if table is not None else None
        sequence = _sequence(raw)
        if sequence is None:
            return []
        return [seat for seat in sequence if isinstance(seat, Mapping)]

    def _legal(self) -> Mapping[str, object] | None:
        table = self._table()
        if table is None or table.get("acting_seat") != self.viewer_seat:
            return None
        return _mapping(table.get("legal_actions"))

    def _client_status(self) -> str | None:
        snapshot_fn = getattr(self.client, "snapshot", None)
        if not callable(snapshot_fn):
            return None
        try:
            snapshot = snapshot_fn()
        except Exception:
            return "failed"
        status = getattr(snapshot, "status", None)
        return status if isinstance(status, str) else None

    def _can_submit(self) -> bool:
        status = self._client_status()
        return self._pending_command is None and status in {None, "connected"}

    def _sync_rebuy_field(self) -> None:
        if self.state is None:
            return
        config = _mapping(self.state.get("config"))
        if config is None:
            return
        big_blind = max(1, _integer(config.get("big_blind"), 10))
        buyin = max(big_blind * 10, _integer(config.get("buyin"), big_blind * 100))
        signature = big_blind, buyin
        if signature == self._rebuy_signature:
            return
        self._rebuy_signature = signature
        self.rebuy_field.minimum = big_blind * 10
        self.rebuy_field.maximum = big_blind * 1000
        self.rebuy_field.set_value(buyin)

    def _maybe_prompt_low_stack(self) -> None:
        if (
            self.state is None
            or self.viewer_seat is None
            or self._topup_dialog is not None
            or self._paused()
            or self._settlement_locked()
            or self._pending_command is not None
        ):
            return
        pending = _sequence(self.state.get("top_up_requests")) or ()
        if any(
            isinstance(item, Mapping)
            and item.get("target_seat") == self.viewer_seat
            and str(item.get("status", "")).upper()
            in {"PENDING_APPROVAL", "APPROVED"}
            for item in pending
        ):
            return
        signature = self._active_low_stack_signature()
        if signature is None:
            return
        if signature in self._seen_low_stack_prompts:
            return
        self._topup_dialog = _OnlineTopUpDialog(self, automatic=True)

    def _active_low_stack_signature(self) -> tuple[int, int] | None:
        """返回本人当前仍待答复的低码提示签名。"""

        if self.state is None or self.viewer_seat is None:
            return None
        prompts = _sequence(self.state.get("low_stack_prompts")) or ()
        if not any(
            isinstance(prompt, Mapping)
            and prompt.get("target_seat") == self.viewer_seat
            and prompt.get("visible_to_viewer") is True
            for prompt in prompts
        ):
            return None
        table = self._table()
        hand_id = _integer(table.get("hand_id")) if table is not None else 0
        return hand_id, self.viewer_seat

    def _refresh_low_stack_tracking(self) -> None:
        """权威提示消失或换手后清理本地去重标记。"""

        active = self._active_low_stack_signature()
        if active is None:
            self._seen_low_stack_prompts.clear()
            self._pending_low_stack_decline = None
            return
        self._seen_low_stack_prompts.intersection_update({active})
        if (
            self._pending_low_stack_decline is not None
            and self._pending_low_stack_decline != active
        ):
            self._pending_low_stack_decline = None

    def _decline_low_stack(self, signature: tuple[int, int]) -> bool:
        """可靠提交低码跳过；发送失败时保留对话框供重试。"""

        if signature != self._active_low_stack_signature():
            return False
        if not self._send("seat.topup.decline"):
            return False
        self._seen_low_stack_prompts.add(signature)
        self._pending_low_stack_decline = signature
        return True

    def _viewer_busted(self) -> bool:
        if self.state is None:
            return False
        pending = _sequence(self.state.get("busted_pending"))
        if pending is not None and self.viewer_seat in pending:
            return True
        decisions = _sequence(self.state.get("bust_decisions"))
        for decision in decisions or ():
            if not isinstance(decision, Mapping):
                continue
            seat = _integer(decision.get("target_seat"), _integer(decision.get("seat"), -1))
            if seat == self.viewer_seat and decision.get("occupant_type") != "AI":
                return True
        return False

    # ---------------------------------------------------------- 大厅命令

    def _toggle_ready(self) -> None:
        seat = self._viewer_room_seat()
        if self.phase != "LOBBY" or seat is None:
            return
        self._send("room.ready", {"ready": seat.get("ready") is not True})

    def _start_game(self) -> None:
        if self._can_start():
            self._send("room.start")

    def _copy_wss(self) -> None:
        """只在用户明确点击后写系统剪贴板；失败信息不包含邀请地址。"""
        try:
            if not pygame.scrap.get_init():
                pygame.scrap.init()
            pygame.scrap.put(pygame.SCRAP_TEXT, self.public_wss.encode("utf-8"))
        except (pygame.error, RuntimeError, TypeError, ValueError):
            self._copy_notice = ("复制失败，请从启动窗口复制", theme.DANGER)
        else:
            self._copy_notice = ("WSS 已复制", theme.TEAL)
        self._copy_notice_left = 2.8

    def _can_start(self) -> bool:
        seats = self._room_seats()
        occupied = [seat for seat in seats if seat.get("occupied") is True]
        human = [
            seat
            for seat in occupied
            if seat.get("occupant_type") in {None, "HUMAN"}
        ]
        return (
            self.phase == "LOBBY"
            and self.viewer_is_host
            and len(occupied) >= 2
            and all(seat.get("ready") is True for seat in human)
            and self._can_submit()
        )

    def _open_seat_dialog(self, seat: int) -> None:
        if (
            self.state is None
            or self.phase not in {"LOBBY", "BETWEEN_HANDS", "PLAYING"}
            or self._paused()
            or self._settlement_locked()
        ):
            return
        room_seat = next(
            (item for item in self._room_seats() if item.get("seat") == seat),
            None,
        )
        if room_seat is None or room_seat.get("occupied") is True:
            return
        # 已准备时服务器也会拒绝换位；客户端先消除无意义点击。
        mine = self._viewer_room_seat()
        if mine is not None and mine.get("ready") is True:
            self.log.add("请先取消准备，再更换座位")
            return
        self._seat_dialog = _OnlineSeatDialog(self, seat)

    def _claim_seat(self, seat: int, buyin: int) -> None:
        if self._paused() or self._settlement_locked():
            return
        if self._send("seat.claim", {"target_seat": seat, "buyin": buyin}):
            self._seat_dialog = None

    def _add_ai(
        self,
        seat: int,
        persona_id: str,
        style_key: str,
        buyin: int,
    ) -> None:
        if not self.viewer_is_host or self._paused() or self._settlement_locked():
            return
        if self._send(
            "room.ai.add",
            {
                "target_seat": seat,
                "persona_id": persona_id,
                "style_key": style_key,
                "buyin": buyin,
            },
        ):
            self._seat_dialog = None

    def _release_seat(self) -> None:
        seat = self._viewer_room_seat()
        if (
            seat is None
            or seat.get("ready") is True
            or self._paused()
            or self._settlement_locked()
        ):
            return
        self._send("seat.release")

    def _toggle_remote_pause(self) -> None:
        if not self.viewer_is_host or self.phase not in {"PLAYING", "BETWEEN_HANDS"}:
            return
        self._send("room.resume" if self._paused() else "room.pause")

    def _paused(self) -> bool:
        return self.state is not None and self.state.get("paused") is True

    def _set_overlay(self, view: str | None) -> None:
        if view not in {None, "history", "stacks"}:
            return
        self._overlay_view = view

    def _open_topup(self) -> None:
        viewer_room = self._viewer_room_seat()
        if (
            self.viewer_seat is None
            or viewer_room is None
            or viewer_room.get("waiting_next_hand") is True
            or self._paused()
            or self._settlement_locked()
        ):
            return
        if self._viewer_topup_request() is not None:
            self._cancel_topup()
            return
        self._topup_dialog = _OnlineTopUpDialog(self)

    def _viewer_topup_request(self) -> Mapping[str, object] | None:
        if self.state is None or self.viewer_seat is None:
            return None
        requests = _sequence(self.state.get("top_up_requests")) or ()
        return next(
            (
                request
                for request in requests
                if isinstance(request, Mapping)
                and request.get("target_seat") == self.viewer_seat
                and str(request.get("status", "")).upper()
                in {"PENDING_APPROVAL", "APPROVED"}
            ),
            None,
        )

    def _request_topup(self, target_stack: int) -> None:
        if self._paused() or self._settlement_locked():
            return
        if self._send("seat.topup.request", {"target_stack": target_stack}):
            self._topup_dialog = None

    def _cancel_topup(self) -> None:
        self._send("seat.topup.cancel")

    def _decide_topup(self, target_seat: int, approve: bool) -> None:
        if self.viewer_is_host and not self._paused() and not self._settlement_locked():
            self._send(
                "seat.topup.approve" if approve else "seat.topup.reject",
                {"target_seat": target_seat},
            )

    def _resolve_ai_bust(self, target_seat: int, *, rebuy: bool) -> None:
        if not self.viewer_is_host or self._paused() or self._settlement_locked():
            return
        if rebuy:
            config = _mapping(self.state.get("config")) if self.state else None
            bb = max(1, _integer(config.get("big_blind"), 10)) if config else 10
            self._send(
                "room.ai.rebuy",
                {"target_seat": target_seat, "target_stack": bb * 100},
            )
        else:
            self._send("room.ai.remove", {"target_seat": target_seat})

    def _change_ai_style(self, target_seat: int, style_key: str) -> None:
        if (
            self.viewer_is_host
            and not self._paused()
            and not self._settlement_locked()
            and self._send(
                "room.ai.style",
                {"target_seat": target_seat, "style_key": style_key},
            )
        ):
            self._ai_style_dialog = None

    # ---------------------------------------------------------- 牌桌命令

    def _available_actions(self) -> set[str]:
        legal = self._legal()
        raw = legal.get("available") if legal is not None else None
        sequence = _sequence(raw)
        if sequence is None:
            return set()
        return {item.upper() for item in sequence if isinstance(item, str)}

    def _send_action(self, kind: str, to: int | None = None) -> None:
        if self._paused():
            return
        available = self._available_actions()
        if kind.upper() not in available:
            return
        body: dict[str, object] = {"kind": kind}
        if kind in {"bet", "raise"}:
            if to is None:
                return
            body["to"] = int(to)
        if self._send("game.action", body):
            self._raise_open = False

    def _call_or_check(self) -> None:
        available = self._available_actions()
        if "CHECK" in available:
            self._send_action("check")
        elif "CALL" in available:
            self._send_action("call")

    def _wager_bounds(self) -> tuple[int, int, str] | None:
        legal = self._legal()
        if legal is None:
            return None
        min_raise = legal.get("min_raise_to")
        max_raise = legal.get("max_raise_to")
        min_bet = legal.get("min_bet_to")
        max_bet = legal.get("max_bet_to")
        if type(min_raise) is int and type(max_raise) is int and min_raise <= max_raise:
            return min_raise, max_raise, "raise"
        if type(min_bet) is int and type(max_bet) is int and min_bet <= max_bet:
            return min_bet, max_bet, "bet"
        return None

    def _raise(self) -> None:
        bounds = self._wager_bounds()
        if bounds is None:
            return
        lo, hi, kind = bounds
        if not self._raise_open or self._raise_bounds != bounds:
            self._raise_open = True
            self._raise_bounds = bounds
            self.slider.set_range(lo, hi)
            self.slider.set_value(lo)
            return
        self._send_action(kind, self.slider.value)

    def _set_wager_fraction(self, fraction: float) -> None:
        bounds = self._wager_bounds()
        table = self._table()
        legal = self._legal()
        if bounds is None or table is None or legal is None:
            return
        lo, hi, kind = bounds
        if fraction < 0:
            target = hi
        else:
            current_bet = 0
            for seat in self._table_seats():
                if seat.get("seat") == self.viewer_seat:
                    current_bet = max(0, _integer(seat.get("bet")))
                    break
            call = max(0, _integer(legal.get("call_amount")))
            pot_after_call = self._pot_total(table) + call
            if kind == "bet":
                target = round(max(1, self._pot_total(table)) * fraction)
            else:
                target = current_bet + call + round(max(1, pot_after_call) * fraction)
            target = max(lo, min(hi, target))
        self._raise_open = True
        self._raise_bounds = bounds
        self.slider.set_range(lo, hi)
        self.slider.set_value(target)

    def _show_cards(self) -> None:
        viewer = next(
            (seat for seat in self._table_seats() if seat.get("seat") == self.viewer_seat),
            None,
        )
        room_seat = self._viewer_room_seat()
        if (
            self.phase == "BETWEEN_HANDS"
            and self.viewer_seat is not None
            and viewer is not None
            and viewer.get("in_hand") is True
            and len(_card_list(viewer.get("cards"))) == 2
            and room_seat is not None
            and room_seat.get("waiting_next_hand") is not True
        ):
            self._send("game.show")

    def _next_hand(self) -> None:
        if (
            self.phase == "BETWEEN_HANDS"
            and self.viewer_is_host
            and not self._busted_pending()
            and not self._settlement_locked()
        ):
            self._send("game.next_hand")

    def _busted_pending(self) -> bool:
        if self.state is None:
            return False
        pending = _sequence(self.state.get("busted_pending"))
        decisions = _sequence(self.state.get("bust_decisions"))
        return bool(pending) or bool(decisions)

    def _rebuy(self) -> None:
        self.rebuy_field._commit()
        if (
            self._viewer_busted()
            and self.rebuy_field.valid
            and not self._settlement_locked()
        ):
            self._send("seat.topup.request", {"target_stack": int(self.rebuy_field.value)})

    def _leave_seat(self) -> None:
        if (
            self._viewer_busted()
            and self.viewer_seat is not None
            and not self._settlement_locked()
        ):
            self._send("seat.leave")

    # ---------------------------------------------------------- pygame 事件/帧

    def handle_event(self, ev: pygame.event.Event) -> None:
        if self._paused():
            # 暂停状态只保留历史、筹码、继续和断开入口；旧模态不得提交业务命令。
            self._seat_dialog = None
            self._topup_dialog = None
            self._ai_style_dialog = None
            self._raise_open = False
        if self._settlement_locked():
            self._seat_dialog = None
            self._topup_dialog = None
            self._ai_style_dialog = None
            self._raise_open = False
        if self._seat_dialog is not None:
            self._seat_dialog.handle_event(ev)
            return
        if self._topup_dialog is not None:
            self._topup_dialog.handle_event(ev)
            return
        if self._ai_style_dialog is not None:
            self._ai_style_dialog.handle_event(ev)
            return
        if ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE:
            if self._overlay_view is not None:
                self._overlay_view = None
                return
            self._back_to_menu()
            return
        if self.phase in {"CONNECTING", "CLOSED"}:
            self.btn_back.handle_event(ev)
            return
        if self.phase == "LOBBY":
            self.btn_copy_wss.handle_event(ev)
            self.btn_ready.handle_event(ev)
            self.btn_start.handle_event(ev)
            self.btn_release.handle_event(ev)
            self.btn_back.handle_event(ev)
            if ev.type == pygame.MOUSEBUTTONUP and ev.button == 1:
                for seat, rect in self._seat_hitboxes.items():
                    if rect.collidepoint(ev.pos):
                        self._open_seat_dialog(seat)
                        break
            return
        self.btn_history.handle_event(ev)
        self.btn_stacks.handle_event(ev)
        if self._overlay_view is not None:
            self.btn_overlay_close.handle_event(ev)
            return
        self.btn_pause.handle_event(ev)
        self.btn_table_back.handle_event(ev)
        if self._paused():
            return
        if self._settlement_locked():
            # 结算阶段只保留公开结果、SHOW、历史/筹码和退出入口；补码、
            # 爆仓与 AI 决策必须等跑牌及派彩演出结束。
            if self.phase == "BETWEEN_HANDS":
                self.btn_show.handle_event(ev)
            return
        self.btn_topup.handle_event(ev)
        if ev.type == pygame.MOUSEBUTTONUP and ev.button == 1:
            for (kind, seat), rect in self._decision_hitboxes.items():
                if not rect.collidepoint(ev.pos):
                    continue
                if kind == "topup_yes":
                    self._decide_topup(seat, True)
                elif kind == "topup_no":
                    self._decide_topup(seat, False)
                elif kind == "ai_rebuy":
                    self._resolve_ai_bust(seat, rebuy=True)
                elif kind == "ai_remove":
                    self._resolve_ai_bust(seat, rebuy=False)
                elif kind == "ai_style":
                    self._ai_style_dialog = _OnlineAiStyleDialog(self, seat)
                return
        if ev.type == pygame.MOUSEBUTTONUP and ev.button == 1:
            for seat, rect in self._seat_hitboxes.items():
                if rect.collidepoint(ev.pos):
                    self._open_seat_dialog(seat)
                    return
        if self._viewer_busted():
            self.rebuy_field.handle_event(ev)
            self.btn_rebuy.handle_event(ev)
            self.btn_leave_seat.handle_event(ev)
            return
        if self.phase == "BETWEEN_HANDS":
            self.btn_show.handle_event(ev)
            self.btn_next.handle_event(ev)
            if ev.type == pygame.MOUSEBUTTONUP and ev.button == 1:
                for seat, rect in self._seat_hitboxes.items():
                    if rect.collidepoint(ev.pos):
                        self._open_seat_dialog(seat)
                        break
            return
        if self.phase != "PLAYING" or self._legal() is None:
            return
        self.btn_fold.handle_event(ev)
        self.btn_call.handle_event(ev)
        self.btn_raise.handle_event(ev)
        if self._raise_open:
            self.slider.handle_event(ev)
            for button in self._chips:
                button.handle_event(ev)
        else:
            self.btn_allin.handle_event(ev)
        if ev.type == pygame.KEYDOWN:
            if ev.key == pygame.K_f:
                self._send_action("fold")
            elif ev.key == pygame.K_c:
                self._call_or_check()
            elif ev.key == pygame.K_r:
                self._raise()

    def update(self, dt: float) -> None:
        self._t += dt
        self.smoke.update(dt)
        self.anim.update(dt)
        self.muck.update(dt)
        self.chipfly.update(dt)
        for echo in self._action_echoes:
            echo.elapsed += dt
        self._action_echoes = [
            echo for echo in self._action_echoes if echo.elapsed < echo.duration
        ]
        expired: list[tuple[str, int, int]] = []
        for key in self._hidden_cards:
            self._hidden_cards[key] -= dt
            if self._hidden_cards[key] <= 0:
                expired.append(key)
        for key in expired:
            self._hidden_cards.pop(key, None)
        if not self.chipfly.busy_for("payout"):
            self._payout_holds.clear()
        self._copy_notice_left = max(0.0, self._copy_notice_left - dt)
        self._poll_client()
        self._maybe_prompt_low_stack()
        bounds = self._wager_bounds()
        if self._raise_open and bounds != self._raise_bounds:
            self._raise_open = False

    # ---------------------------------------------------------- 绘制

    def draw(self, dst: pygame.Surface) -> None:
        dst.fill(theme.BG)
        flicker = 1.0 + 0.018 * math.sin(self._t * 2.2)
        glow_size = round(1120 * flicker)
        glow = pygame.transform.smoothscale(self._glow, (glow_size, glow_size))
        dst.blit(glow, glow.get_rect(center=(TABLE_C[0], TABLE_C[1] - 30)))
        # 氛围层先完成，牌面、座位与正文随后绘制，避免被蒙版压暗。
        self.smoke.draw(dst)
        dst.blit(self._vignette, (0, 0))
        dst.blit(self._grain, (0, 0))
        if self.phase in {"LOBBY", "CONNECTING", "CLOSED"}:
            self._draw_lobby(dst)
        else:
            self._draw_table(dst)
        if self.phase in {"LOBBY", "CONNECTING", "CLOSED"}:
            self._draw_lobby_foreground(dst)
        else:
            self.anim.draw(dst)
            self.muck.draw(dst)
            self.chipfly.draw(dst)
            self._draw_action_echoes(dst)
            self._draw_hand_strength_overlay(dst)
            self._draw_table_foreground(dst)
            if self._paused():
                self._draw_pause_card(dst)
            if self._overlay_view is not None:
                self._draw_overlay(dst)
        if self._seat_dialog is not None and not self._paused():
            self._seat_dialog.draw(dst, self._t)
        if self._topup_dialog is not None and not self._paused():
            self._topup_dialog.draw(dst)
        if self._ai_style_dialog is not None and not self._paused():
            self._ai_style_dialog.draw(dst)

    def _draw_lobby(self, dst: pygame.Surface) -> None:
        Panel.draw(dst, (75, 92, 1450, 650), alpha=224, border=theme.AMBER_DARK, radius=18)
        Panel.draw(dst, (115, 118, 1370, 92), alpha=236, border=theme.TEAL_DARK)
        rim = pygame.Rect(230, 258, 1140, 410)
        pygame.draw.ellipse(dst, (46, 32, 19), rim)
        pygame.draw.ellipse(dst, theme.FELT_EDGE, rim, 3)
        felt = rim.inflate(-30, -28)
        pygame.draw.ellipse(dst, theme.FELT, felt)
        pygame.draw.ellipse(dst, (52, 38, 23), felt, 2)

    def _draw_lobby_foreground(self, dst: pygame.Surface) -> None:
        theme.text(dst, "朋友联机", (800, 34), 42, theme.AMBER_LIGHT, "center", shadow=True)
        theme.text(
            dst,
            "任意成员可建房 · 电脑与手机通过同一 WSS 会合",
            (800, 72),
            16,
            theme.TEAL,
            "center",
        )
        theme.text(dst, "手机连接地址（私密路径已隐藏）", (138, 135), 15, theme.TEXT_DIM)
        for row, line in enumerate(self._wrapped_invite()):
            theme.text(dst, line, (138, 164 + row * 22), 14, theme.TEXT)
        theme.text(dst, "房间码", (1260, 132), 14, theme.TEXT_DIM, "center")
        theme.text(dst, self.room_id, (1260, 167), 30, theme.GOLD, "center", shadow=True)
        self.btn_copy_wss.rect = pygame.Rect(1038, 156, 150, 34)
        self.btn_copy_wss.draw(dst)
        if self._copy_notice is not None and self._copy_notice_left > 0:
            notice, color = self._copy_notice
            theme.text(dst, notice, (1112, 207), 13, color, "center")

        self._seat_hitboxes.clear()
        if self.state is None:
            theme.text(dst, "正在取得房间状态…", (800, 490), 24, theme.TEXT_DIM, "center")
        else:
            seats = self._room_seats()
            count = len(seats)
            for index, seat in enumerate(seats):
                angle = math.pi / 2 - math.tau * index / max(2, count)
                center = (
                    round(800 + 500 * math.cos(angle)),
                    round(468 + 190 * math.sin(angle)),
                )
                rect = pygame.Rect(0, 0, 184 if count < 8 else 158, 72 if count < 8 else 64)
                rect.center = center
                self._draw_lobby_seat(dst, rect, seat)
                if seat.get("occupied") is not True:
                    self._seat_hitboxes[_integer(seat.get("seat"), index)] = rect
            occupied = sum(1 for seat in seats if seat.get("occupied") is True)
            waiting = max(0, len(self._room_members()) - sum(
                1 for member in self._room_members() if type(member.get("seat")) is int
            ))
            theme.text(
                dst,
                f"已入座 {occupied}/{len(seats)} · 未入座 {waiting} · 至少两席准备后由房主开始",
                (800, 701),
                17,
                theme.TEXT_DIM,
                "center",
            )
        if self.phase == "CLOSED":
            theme.text(dst, "房间已经关闭", (800, 715), 20, theme.DANGER, "center")
        elif self._connection_error:
            theme.text(dst, self._connection_error, (800, 715), 18, theme.DANGER, "center")
        elif self._pending_command:
            theme.text(dst, "等待服务器确认…", (800, 715), 16, theme.AMBER_LIGHT, "center")

        member = self._viewer_room_seat()
        ready = member is not None and member.get("ready") is True
        self.btn_ready.label = "取消准备" if ready else "准备"
        self.btn_ready.selected = ready
        self.btn_ready.enabled = (
            self.phase == "LOBBY" and member is not None and self._can_submit()
        )
        self.btn_start.enabled = self._can_start()
        self.btn_start.label = "开始牌局" if self.viewer_is_host else "等待房主开始"
        self.btn_back.enabled = True
        self.btn_release.enabled = member is not None and not ready and self._can_submit()
        self.btn_ready.draw(dst)
        self.btn_start.draw(dst)
        self.btn_back.draw(dst)
        if member is not None:
            self.btn_release.rect = pygame.Rect(1182, 796, 150, 42)
            self.btn_release.draw(dst)

    def _draw_lobby_seat(
        self, dst: pygame.Surface, rect: pygame.Rect, seat: Mapping[str, object]
    ) -> None:
        occupied = seat.get("occupied") is True
        ready = seat.get("ready") is True
        mine = seat.get("seat") == self.viewer_seat
        compact = rect.width < 170
        border = theme.AMBER_LIGHT if mine else theme.TEAL if ready else theme.FELT_EDGE
        pygame.draw.rect(dst, (34, 24, 16), rect, border_radius=12)
        pygame.draw.rect(dst, border, rect, 2 if mine else 1, border_radius=12)
        radius = 24 if compact else 29
        badge = pygame.Rect(0, 0, radius * 2, radius * 2)
        badge.center = (
            rect.left + 32,
            rect.centery,
        )
        pygame.draw.circle(dst, (52, 36, 22), badge.center, radius)
        pygame.draw.circle(dst, border, badge.center, radius, 2)
        if occupied:
            theme.text(dst, str(_integer(seat.get("seat")) + 1), badge.center, 19 if compact else 25, border, "center")
        else:
            arm = 12 if compact else 15
            pygame.draw.line(dst, theme.AMBER_LIGHT, (badge.centerx - arm, badge.centery), (badge.centerx + arm, badge.centery), 4)
            pygame.draw.line(dst, theme.AMBER_LIGHT, (badge.centerx, badge.centery - arm), (badge.centerx, badge.centery + arm), 4)
        name = seat.get("display_name") if occupied else "点击选择座位"
        if not isinstance(name, str):
            name = "玩家"
        if mine:
            name += "（你）"
        name_pos = (rect.left + 66, rect.top + 12)
        theme.text(
            dst,
            name[:18],
            name_pos,
            16 if compact else 18,
            theme.TEXT,
            "topleft",
        )
        if occupied:
            status = "已准备" if ready else "未准备"
            if seat.get("is_host") is True:
                status += " · 房主"
            if seat.get("occupant_type") == "AI":
                style_key = seat.get("style_key")
                status = f"AI · {style_key}" if isinstance(style_key, str) else "AI 牌手"
            color = theme.TEAL if ready else theme.TEXT_DIM
        else:
            status = "你可入座" if not self.viewer_is_host else "入座 / 添加 AI"
            color = theme.AMBER_LIGHT
        status_pos = (rect.left + 66, rect.top + 39)
        theme.text(
            dst,
            status,
            status_pos,
            13 if compact else 15,
            color,
            "topleft",
        )

    def _wrapped_invite(self) -> tuple[str, ...]:
        """只绘制域名；完整私密路径仅在显式复制时进入剪贴板。"""
        parsed = urlsplit(self.public_wss)
        host = parsed.netloc if parsed.scheme == "wss" and parsed.netloc else "临时地址"
        return (f"wss://{host}/…",)

    def _draw_table(self, dst: pygame.Surface) -> None:
        self._draw_felt(dst)
        table = self._table()
        if table is None:
            return
        self._draw_board_and_pot(dst, table)
        self._draw_remote_seats(dst, table)

    def _draw_table_foreground(self, dst: pygame.Surface) -> None:
        table = self._table()
        self._draw_info_panel(dst, table)
        if table is None:
            theme.text(dst, "等待牌桌状态…", TABLE_C, 26, theme.TEXT_DIM, "center")
            self.btn_table_back.draw(dst)
            return
        self.btn_pause.label = "继续对局" if self._paused() else "暂停对局"
        self.btn_pause.enabled = self.viewer_is_host and self._can_submit()
        if self.viewer_is_host:
            self.btn_pause.draw(dst)
        viewer_table = next(
            (seat for seat in self._table_seats() if seat.get("seat") == self.viewer_seat),
            None,
        )
        viewer_room = self._viewer_room_seat()
        viewer_active = (
            viewer_table is not None
            and viewer_room is not None
            and viewer_room.get("waiting_next_hand") is not True
            and table.get("viewer_seat") == self.viewer_seat
        )
        config = _mapping(self.state.get("config")) if self.state is not None else None
        bb = max(1, _integer(config.get("big_blind"), 10)) if config else 10
        can_topup = (
            viewer_active
            and viewer_table is not None
            and _integer(viewer_table.get("stack")) < bb * 1000
            and self.phase in {"PLAYING", "BETWEEN_HANDS"}
            and not self._paused()
            and not self._settlement_locked()
        )
        topup_request = self._viewer_topup_request()
        self.btn_topup.label = "取消补码申请" if topup_request is not None else "申请补码"
        self.btn_topup.enabled = (can_topup or topup_request is not None) and self._can_submit()
        if viewer_active:
            self.btn_topup.draw(dst)
        if self._paused():
            theme.text(dst, "牌局暂停中 · 投注操作已锁定", (TABLE_C[0], 804), 16, theme.TEXT_DIM, "center")
            self.btn_table_back.draw(dst)
        elif self.phase == "BETWEEN_HANDS" and self._settlement_locked():
            self._draw_between_hands(dst, table)
        elif self._viewer_busted():
            self._draw_bust_controls(dst)
        elif self.phase == "PLAYING" and self._legal() is not None:
            self._draw_action_bar(dst)
        elif self.phase == "BETWEEN_HANDS":
            self._draw_between_hands(dst, table)
        else:
            theme.text(dst, "等待其他玩家行动…", (TABLE_C[0], 792), 17, theme.TEXT_DIM, "center")
            self.btn_table_back.draw(dst)
        if self._connection_error:
            theme.text(dst, self._connection_error, (TABLE_C[0], 816), 16, theme.DANGER, "center")
        elif self._pending_command:
            theme.text(dst, "等待服务器确认操作…", (TABLE_C[0], 816), 14, theme.AMBER_LIGHT, "center")

    def _draw_felt(self, dst: pygame.Surface) -> None:
        rim = pygame.Rect(0, 0, (FELT_RX + 26) * 2, (FELT_RY + 26) * 2)
        rim.center = TABLE_C
        pygame.draw.ellipse(dst, (46, 32, 19), rim)
        pygame.draw.ellipse(dst, theme.FELT_EDGE, rim, 3)
        felt = pygame.Rect(0, 0, FELT_RX * 2, FELT_RY * 2)
        felt.center = TABLE_C
        pygame.draw.ellipse(dst, theme.FELT, felt)
        pygame.draw.ellipse(dst, (52, 38, 23), felt, 2)
        pygame.draw.ellipse(dst, (44, 32, 20), felt.inflate(-56, -56), 1)

    def _draw_board_and_pot(
        self, dst: pygame.Surface, table: Mapping[str, object]
    ) -> None:
        board = _card_list(table.get("board"))
        back = cards.card_surface("back", cards.SIZE_BOARD)
        dst.blit(back, back.get_rect(center=DECK_POS))
        for index, code in enumerate(board):
            if ("board", 0, index) in self._hidden_cards:
                continue
            surf = cards.card_surface(code, cards.SIZE_BOARD)
            x = TABLE_C[0] + (index - 2) * 70
            dst.blit(surf, surf.get_rect(center=(x, BOARD_Y)))
        pot = self._pot_total(table)
        if pot > 0:
            draw_chip_pile(
                dst,
                POT_POS,
                pot,
                seed=197,
                show_amount=False,
                scale=0.84,
                min_chips=4,
                max_chips=18,
            )
            theme.text(dst, f"底池 {pot}", (TABLE_C[0], BOARD_Y - 76), 22, theme.GOLD, "center", shadow=True)

    def _pot_total(self, table: Mapping[str, object]) -> int:
        pots = _sequence(table.get("pots"))
        seats = _sequence(table.get("seats"))
        settled = sum(
            max(0, _integer(pot.get("amount")))
            for pot in pots
            if isinstance(pot, Mapping)
        ) if pots is not None else 0
        street = sum(
            max(0, _integer(seat.get("bet")))
            for seat in seats
            if isinstance(seat, Mapping)
        ) if seats is not None else 0
        return settled + street

    def _player_count(self) -> int:
        config = _mapping(self.state.get("config")) if self.state is not None else None
        return max(2, min(9, _integer(config.get("player_count"), 2))) if config else 2

    def _visual_index(self, server_seat: int) -> int:
        count = self._player_count()
        return server_seat % count if self.viewer_seat is None else (server_seat - self.viewer_seat) % count

    def _anchor_for(self, server_seat: int) -> tuple[float, float]:
        return _seat_layout(self._player_count())[self._visual_index(server_seat)]

    def _hole_pos_for(self, server_seat: int, index: int) -> tuple[float, float]:
        anchor = self._anchor_for(server_seat)
        return self._hole_pos(anchor, index, server_seat == self.viewer_seat)

    def _bet_pos_for(self, server_seat: int) -> tuple[float, float]:
        anchor = self._anchor_for(server_seat)
        return self._bet_pos(anchor, server_seat == self.viewer_seat)

    def _stack_pos_for(self, server_seat: int) -> tuple[float, float]:
        anchor = self._anchor_for(server_seat)
        if server_seat == self.viewer_seat:
            return anchor[0] + 146, anchor[1] - 72
        ux, uy = _unit_to_center(anchor)
        px, py = -uy, ux
        compact = self._player_count() >= 8
        reach = 150 if compact else 165
        side = (48 if compact else 52) * (1 if server_seat % 2 else -1)
        return anchor[0] + ux * reach + px * side, anchor[1] + uy * reach + py * side

    def _draw_remote_seats(
        self, dst: pygame.Surface, table: Mapping[str, object]
    ) -> None:
        table_seats = {
            _integer(seat.get("seat"), -1): seat for seat in self._table_seats()
        }
        room_seats = {
            _integer(seat.get("seat"), -1): seat for seat in self._room_seats()
        }
        count = self._player_count()
        layout = _seat_layout(count)
        compact = count >= 8
        shown_raw = _sequence(table.get("shown"))
        shown_seats = set(shown_raw or ())
        self._seat_hitboxes.clear()
        for server_seat in range(count):
            room_seat = room_seats.get(server_seat)
            seat = table_seats.get(server_seat)
            occupied = room_seat is not None and room_seat.get("occupied") is True
            waiting_next_hand = bool(
                occupied and room_seat.get("waiting_next_hand") is True
            )
            visual = self._visual_index(server_seat)
            anchor = layout[visual]
            is_viewer = server_seat == self.viewer_seat
            if not occupied:
                self._draw_empty_table_seat(dst, server_seat, anchor, compact)
                continue
            if seat is None or waiting_next_hand:
                # 已入座但预约到下一手的成员仍显示在圆桌上，不伪造筹码/底牌。
                placeholder: dict[str, object] = {
                    "seat": server_seat,
                    "name": room_seat.get("display_name", f"玩家{server_seat + 1}"),
                    "stack": _integer(room_seat.get("stack")),
                    "bet": 0,
                    "folded": False,
                    "all_in": False,
                    "in_hand": False,
                }
                seat = placeholder
            acting = table.get("acting_seat") == server_seat and self.phase == "PLAYING"
            folded = seat.get("folded") is True
            in_hand = seat.get("in_hand") is True
            if acting:
                self._draw_turn_halo(dst, anchor, is_viewer)
            if not is_viewer:
                self._draw_remote_avatar(
                    dst, anchor, seat, room_seat, acting, folded, compact
                )
            plate = pygame.Rect(
                0,
                0,
                218 if is_viewer else 148 if compact else 176,
                54 if compact and not is_viewer else 58,
            )
            plate.center = (
                round(anchor[0]),
                round(anchor[1] - 18 if is_viewer else anchor[1] + (58 if compact else 72)),
            )
            name = seat.get("name") if isinstance(seat.get("name"), str) else f"玩家{server_seat + 1}"
            if compact and len(name) > 9:
                name = name[:8] + "…"
            status = None
            status_color = None
            if folded:
                status, status_color = "已弃牌", (166, 108, 92)
            elif waiting_next_hand:
                status, status_color = "等待下一手", theme.TEAL
            elif seat.get("all_in") is True:
                status, status_color = "全下", theme.DANGER
            elif acting:
                status, status_color = "轮到你" if is_viewer else "行动中…", theme.AMBER_LIGHT
            display_stack = max(
                0,
                _integer(seat.get("stack")) - self._payout_holds.get(server_seat, 0),
            )
            draw_name_plate(
                dst,
                plate,
                "你 · " + name if is_viewer else name,
                display_stack,
                active=acting,
                busted=not in_hand,
                status=status,
                status_color=status_color,
            )
            self._draw_hole_cards(
                dst,
                seat,
                anchor,
                is_viewer,
                folded,
                server_seat in shown_seats,
            )
            self._draw_bet(dst, seat, anchor, is_viewer, visual)
            if display_stack > 0:
                draw_chip_pile(
                    dst,
                    self._stack_pos_for(server_seat),
                    display_stack,
                    seed=server_seat * 19 + 5,
                    show_amount=False,
                    scale=0.58 if compact else 0.68,
                    min_chips=4,
                    max_chips=14,
                )
            if not waiting_next_hand and table.get("button_seat") == server_seat:
                bx, by = self._bet_pos(anchor, is_viewer)
                pygame.draw.circle(dst, theme.TEXT, (round(bx + 36), round(by)), 13)
                pygame.draw.circle(dst, theme.AMBER, (round(bx + 36), round(by)), 13, 2)
                theme.text(dst, "D", (bx + 36, by), 16, theme.BG, "center")

    def _draw_empty_table_seat(
        self,
        dst: pygame.Surface,
        server_seat: int,
        anchor: tuple[float, float],
        compact: bool,
    ) -> None:
        radius = 34 if compact else 42
        center = round(anchor[0]), round(anchor[1])
        pygame.draw.circle(dst, (32, 23, 15), center, radius)
        pygame.draw.circle(dst, theme.AMBER_DARK, center, radius, 3)
        pygame.draw.line(dst, theme.AMBER_LIGHT, (center[0] - 18, center[1]), (center[0] + 18, center[1]), 5)
        pygame.draw.line(dst, theme.AMBER_LIGHT, (center[0], center[1] - 18), (center[0], center[1] + 18), 5)
        theme.text(dst, "空座位", (center[0], center[1] + radius + 10), 14, theme.TEXT_DIM, "midtop")
        self._seat_hitboxes[server_seat] = pygame.Rect(0, 0, radius * 2 + 28, radius * 2 + 38)
        self._seat_hitboxes[server_seat].center = (center[0], center[1] + 12)

    def _draw_remote_avatar(
        self,
        dst: pygame.Surface,
        anchor: tuple[float, float],
        seat: Mapping[str, object],
        room_seat: Mapping[str, object],
        acting: bool,
        folded: bool,
        compact: bool,
    ) -> None:
        if room_seat.get("occupant_type") == "AI":
            persona_id = room_seat.get("persona_id")
            if isinstance(persona_id, str):
                try:
                    persona = persona_by_id(persona_id, self.seed)
                except KeyError:
                    persona = None
                if persona is not None:
                    cache_key = _integer(room_seat.get("seat"), -1), persona.persona_id
                    bust = self._ai_busts.get(cache_key)
                    if bust is None:
                        bust = Bust(
                            persona.species,
                            seed=max(0, cache_key[0]) * 101 + len(persona.persona_id),
                            scale=0.66 if compact else 0.88,
                        )
                        self._ai_busts[cache_key] = bust
                    state = FOLDED if folded or seat.get("in_hand") is not True else THINKING if acting else IDLE
                    bust.draw(dst, anchor, self._t, state)
                    return
        center = round(anchor[0]), round(anchor[1] - 14)
        radius = 37 if compact else 47
        fill = (40, 31, 24) if folded else (67, 46, 28)
        border = theme.AMBER_LIGHT if acting else theme.AMBER_DARK
        pygame.draw.circle(dst, fill, center, radius)
        pygame.draw.circle(dst, border, center, radius, 3)
        # 无网络头像时使用中性酒馆客人徽章，不把真人伪装成动物 AI。
        pygame.draw.circle(dst, theme.TEXT_DIM, (center[0], center[1] - 12), 15)
        pygame.draw.ellipse(dst, theme.TEXT_DIM, (center[0] - 28, center[1] + 7, 56, 29))
        if folded:
            veil = pygame.Surface((radius * 2, radius * 2), pygame.SRCALPHA)
            pygame.draw.circle(veil, (10, 8, 6, 118), (radius, radius), radius)
            dst.blit(veil, veil.get_rect(center=center))

    def _draw_turn_halo(
        self, dst: pygame.Surface, anchor: tuple[float, float], is_viewer: bool
    ) -> None:
        size = (290, 150) if is_viewer else (220, 220)
        halo = pygame.Surface(size, pygame.SRCALPHA)
        alpha = round(80 + 42 * ((math.sin(self._t * 4.2) + 1.0) * 0.5))
        pygame.draw.ellipse(halo, (*theme.AMBER, alpha), halo.get_rect().inflate(-14, -14), 3)
        center = (round(anchor[0]), round(anchor[1] - 82 if is_viewer else anchor[1] - 12))
        dst.blit(halo, halo.get_rect(center=center))

    def _draw_hole_cards(
        self,
        dst: pygame.Surface,
        seat: Mapping[str, object],
        anchor: tuple[float, float],
        is_viewer: bool,
        folded: bool,
        shown: bool,
    ) -> None:
        if seat.get("in_hand") is not True or (folded and not shown):
            return
        visible = _card_list(seat.get("cards"))
        seat_id = _integer(seat.get("seat"), -1)
        for index in range(2):
            if ("hole", seat_id, index) in self._hidden_cards:
                continue
            code = visible[index] if index < len(visible) else "back"
            size = cards.SIZE_HOLE if is_viewer else cards.SIZE_MINI
            surf = cards.card_surface(code, size)
            pos = self._hole_pos(anchor, index, is_viewer)
            image = pygame.transform.rotate(surf, -6 if index == 0 else 6)
            dst.blit(image, image.get_rect(center=(round(pos[0]), round(pos[1]))))

    def _hole_pos(
        self, anchor: tuple[float, float], index: int, is_viewer: bool
    ) -> tuple[float, float]:
        if is_viewer:
            return anchor[0] - 38 + index * 76, anchor[1] - 136
        ux, uy = _unit_to_center(anchor)
        count = self._player_count()
        reach = 112 if count >= 8 else 122 if count == 7 else 136
        cx, cy = anchor[0] + ux * reach, anchor[1] + uy * reach
        px, py = -uy, ux
        gap = 28 if count >= 8 else 34
        return cx + px * (index - 0.5) * gap, cy + py * (index - 0.5) * gap

    def _bet_pos(
        self, anchor: tuple[float, float], is_viewer: bool
    ) -> tuple[float, float]:
        if is_viewer:
            return anchor[0], anchor[1] - 242
        ux, uy = _unit_to_center(anchor)
        reach = 190 if self._player_count() >= 8 else 208
        return anchor[0] + ux * reach, anchor[1] + uy * reach

    def _draw_bet(
        self,
        dst: pygame.Surface,
        seat: Mapping[str, object],
        anchor: tuple[float, float],
        is_viewer: bool,
        visual: int,
    ) -> None:
        bet = max(0, _integer(seat.get("bet")))
        if bet:
            draw_chip_pile(
                dst,
                self._bet_pos(anchor, is_viewer),
                bet,
                seed=visual * 13 + 3,
                scale=0.72,
                min_chips=2,
                max_chips=12,
            )

    def _draw_action_echoes(self, dst: pygame.Surface) -> None:
        for echo in self._action_echoes:
            frac = min(1.0, echo.elapsed / echo.duration)
            alpha = round(
                230
                * min(1.0, frac / 0.10)
                * min(1.0, max(0.0, 1.0 - frac) / 0.34)
            )
            if alpha <= 0:
                continue
            x, y = self._bet_pos_for(echo.seat)
            y -= 22 + 14 * fx.Ease.out_cubic(frac)
            label = theme.get_font(17).render(echo.label, True, echo.color)
            bubble = pygame.Surface(
                (label.get_width() + 30, label.get_height() + 16), pygame.SRCALPHA
            )
            pygame.draw.rect(
                bubble,
                (*theme.BG_PANEL, min(205, alpha)),
                bubble.get_rect(),
                border_radius=10,
            )
            pygame.draw.rect(
                bubble,
                (*echo.color, alpha),
                bubble.get_rect(),
                2,
                border_radius=10,
            )
            label.set_alpha(alpha)
            bubble.blit(label, label.get_rect(center=bubble.get_rect().center))
            dst.blit(bubble, bubble.get_rect(center=(round(x), round(y))))

    def _viewer_hand_summary(self) -> HandSummary | None:
        table = self._table()
        if table is None or self.viewer_seat is None:
            return None
        viewer = next(
            (seat for seat in self._table_seats() if seat.get("seat") == self.viewer_seat),
            None,
        )
        if viewer is None or viewer.get("folded") is True or viewer.get("in_hand") is not True:
            return None
        hole = _card_list(viewer.get("cards"))
        if len(hole) != 2 or any(("hole", self.viewer_seat, i) in self._hidden_cards for i in range(2)):
            return None
        board = _card_list(table.get("board"))
        settled = 0
        for index in range(len(board)):
            if ("board", 0, index) in self._hidden_cards:
                break
            settled += 1
        settled = 5 if settled >= 5 else 4 if settled >= 4 else 3 if settled >= 3 else 0
        try:
            return describe_holdem_hand(hole, board[:settled])
        except ValueError:
            return None

    @staticmethod
    def _draw_card_highlight(
        dst: pygame.Surface,
        center: tuple[float, float],
        size: tuple[int, int],
        rotation: float = 0.0,
    ) -> None:
        pad = 8
        frame = pygame.Surface((size[0] + pad * 2, size[1] + pad * 2), pygame.SRCALPHA)
        rect = pygame.Rect(pad - 3, pad - 3, size[0] + 6, size[1] + 6)
        radius = max(7, round(size[0] * 0.13))
        pygame.draw.rect(frame, (*theme.CARD_HIGHLIGHT, 72), rect, 7, border_radius=radius + 2)
        pygame.draw.rect(frame, (*theme.CARD_HIGHLIGHT, 245), rect, 3, border_radius=radius)
        image = pygame.transform.rotate(frame, rotation) if abs(rotation) > 0.01 else frame
        dst.blit(image, image.get_rect(center=(round(center[0]), round(center[1]))))

    def _draw_hand_strength_overlay(self, dst: pygame.Surface) -> None:
        summary = self._viewer_hand_summary()
        table = self._table()
        if summary is None or table is None or self.viewer_seat is None:
            return
        board = _card_list(table.get("board"))
        highlighted = summary.highlight_cards
        for index, code in enumerate(board):
            if code in highlighted and ("board", 0, index) not in self._hidden_cards:
                self._draw_card_highlight(
                    dst,
                    (TABLE_C[0] + (index - 2) * 70, BOARD_Y),
                    cards.SIZE_BOARD,
                )
        viewer = next(
            (seat for seat in self._table_seats() if seat.get("seat") == self.viewer_seat),
            None,
        )
        hole = _card_list(viewer.get("cards")) if viewer is not None else ()
        for index, code in enumerate(hole):
            if code in highlighted:
                self._draw_card_highlight(
                    dst,
                    self._hole_pos_for(self.viewer_seat, index),
                    cards.SIZE_HOLE,
                    -6.0 if index == 0 else 6.0,
                )
        label = f"当前牌型 · {summary.label}"
        color = theme.CARD_HIGHLIGHT if summary.category >= HandCategory.ONE_PAIR else theme.TEXT_DIM
        width = max(184, theme.text_width(label, 16) + 30)
        pill = pygame.Surface((width, 30), pygame.SRCALPHA)
        pygame.draw.rect(pill, (22, 15, 10, 224), pill.get_rect(), border_radius=15)
        pygame.draw.rect(pill, (*color, 190), pill.get_rect().inflate(-2, -2), 1, border_radius=14)
        theme.text(pill, label, pill.get_rect().center, 16, color, "center")
        dst.blit(pill, pill.get_rect(center=(TABLE_C[0], 735)))

    def _draw_pause_card(self, dst: pygame.Surface) -> None:
        panel = pygame.Rect(0, 0, 520, 190)
        panel.center = (TABLE_C[0], 370)
        Panel.draw(dst, panel, alpha=246, border=theme.AMBER)
        theme.text(dst, "牌局已暂停", (panel.centerx, panel.top + 35), 32, theme.AMBER_LIGHT, "center", shadow=True)
        paused_by = self.state.get("paused_by") if self.state is not None else None
        paused_by_name = self.state.get("paused_by_name") if self.state is not None else None
        subtitle = "房主暂停了牌局 · 所有投注操作暂时锁定"
        if isinstance(paused_by_name, str) and paused_by_name:
            subtitle = f"{paused_by_name} 暂停了牌局 · 所有投注操作暂时锁定"
        elif isinstance(paused_by, str) and paused_by:
            paused_name = next(
                (
                    member.get("display_name")
                    for member in self._room_members()
                    if member.get("member_id") == paused_by
                    and isinstance(member.get("display_name"), str)
                ),
                "房主",
            )
            subtitle = f"{paused_name} 暂停了牌局 · 所有投注操作暂时锁定"
        theme.text(dst, subtitle, (panel.centerx, panel.top + 86), 16, theme.TEXT, "center")
        theme.text(dst, "仍可从右侧查看过往牌局与所有席位筹码", (panel.centerx, panel.top + 126), 15, theme.TEAL, "center")

    def _draw_overlay(self, dst: pygame.Surface) -> None:
        veil = pygame.Surface((PANEL_X, 900), pygame.SRCALPHA)
        veil.fill((0, 0, 0, 150))
        dst.blit(veil, (0, 0))
        panel = pygame.Rect(105, 92, 1060, 700)
        Panel.draw(dst, panel, alpha=248, border=theme.AMBER)
        if self._overlay_view == "stacks":
            self._draw_stack_overlay(dst, panel)
        else:
            self._draw_history_overlay(dst, panel)
        self.btn_overlay_close.draw(dst)

    def _draw_stack_overlay(self, dst: pygame.Surface, panel: pygame.Rect) -> None:
        theme.text(dst, "全桌筹码量", (panel.centerx, panel.top + 42), 30, theme.AMBER_LIGHT, "center", shadow=True)
        table_by_seat = {
            _integer(seat.get("seat"), -1): seat for seat in self._table_seats()
        }
        for index, room_seat in enumerate(self._room_seats()):
            seat_id = _integer(room_seat.get("seat"), index)
            row = index // 2
            col = index % 2
            rect = pygame.Rect(panel.left + 72 + col * 475, panel.top + 105 + row * 94, 430, 72)
            occupied = room_seat.get("occupied") is True
            border = theme.AMBER_LIGHT if seat_id == self.viewer_seat else theme.FELT_EDGE
            Panel.draw(dst, rect, alpha=210, border=border)
            if not occupied:
                theme.text(dst, f"#{seat_id + 1} 空座位", (rect.left + 18, rect.centery), 17, theme.TEXT_DIM, "midleft")
                continue
            name = room_seat.get("display_name") if isinstance(room_seat.get("display_name"), str) else "玩家"
            kind = "AI" if room_seat.get("occupant_type") == "AI" else "真人"
            table_seat = table_by_seat.get(seat_id)
            stack = _integer(table_seat.get("stack")) if table_seat is not None else _integer(room_seat.get("stack"))
            theme.text(dst, f"#{seat_id + 1} {name}", (rect.left + 18, rect.top + 14), 17, theme.TEXT)
            theme.text(dst, kind, (rect.right - 18, rect.top + 14), 14, theme.TEAL, "topright")
            theme.text(dst, f"¢ {max(0, stack)}", (rect.left + 18, rect.bottom - 15), 18, theme.GOLD, "bottomleft")

    def _draw_history_overlay(self, dst: pygame.Surface, panel: pygame.Rect) -> None:
        theme.text(dst, "过往牌局", (panel.centerx, panel.top + 42), 30, theme.AMBER_LIGHT, "center", shadow=True)
        summaries = _sequence(self.state.get("public_hand_summaries")) if self.state is not None else None
        if not summaries:
            theme.text(dst, "本房间尚无已结算牌局", panel.center, 20, theme.TEXT_DIM, "center")
            return
        y = panel.top + 102
        for summary in list(summaries)[-8:]:
            if not isinstance(summary, Mapping):
                continue
            hand_id = _integer(summary.get("hand_id"))
            text = summary.get("result_text") or summary.get("summary") or summary.get("text")
            if not isinstance(text, str):
                winners = _sequence(summary.get("winners")) or ()
                labels = []
                for winner in winners:
                    if isinstance(winner, Mapping):
                        target_seat = _integer(winner.get("target_seat"), _integer(winner.get("seat")))
                        name = winner.get("display_name") or winner.get("name") or f"座位{target_seat + 1}"
                        labels.append(f"{name} +{_integer(winner.get('amount'))}")
                text = " · ".join(labels) if labels else "已结算"
            pot = _integer(summary.get("pot"), _integer(summary.get("total_pot")))
            theme.text(dst, f"第 {hand_id} 手", (panel.left + 66, y), 16, theme.AMBER_LIGHT)
            theme.text(dst, text[:72], (panel.left + 190, y), 16, theme.TEXT)
            if pot:
                theme.text(dst, f"底池 {pot}", (panel.right - 54, y), 15, theme.GOLD, "topright")
            pygame.draw.line(dst, theme.FELT_EDGE, (panel.left + 60, y + 31), (panel.right - 50, y + 31), 1)
            y += 62

    def _draw_action_bar(self, dst: pygame.Surface) -> None:
        legal = self._legal()
        if legal is None:
            return
        available = self._available_actions()
        Panel.draw(dst, (36, 818, 1180, 76), alpha=228, border=theme.AMBER_DARK)
        theme.text(
            dst,
            "你的回合 · 调整下注后确认" if self._raise_open else "你的回合 · 请选择操作",
            (270 if self._raise_open else 615, 820),
            14,
            theme.AMBER_LIGHT,
            "midtop",
            shadow=True,
        )
        self.btn_fold.enabled = "FOLD" in available and self._can_submit()
        if "CHECK" in available:
            self.btn_call.label = "过牌 C"
        elif "CALL" in available:
            self.btn_call.label = f"跟注 {_integer(legal.get('call_amount'))} C"
        else:
            self.btn_call.label = "—"
        self.btn_call.enabled = bool({"CHECK", "CALL"} & available) and self._can_submit()
        bounds = self._wager_bounds()
        self.btn_raise.enabled = bounds is not None and self._can_submit()
        self.btn_raise.label = "确认 R" if self._raise_open else ("下注 R" if bounds and bounds[2] == "bet" else "加注 R")
        self.btn_allin.enabled = "ALLIN" in available and self._can_submit()
        self.btn_fold.draw(dst)
        self.btn_call.draw(dst)
        self.btn_raise.draw(dst)
        if self._raise_open:
            self.slider.draw(dst)
            theme.text(dst, f"下注到 {self.slider.value}", (703, 830), 16, theme.AMBER_LIGHT, "center")
            for button in self._chips:
                button.enabled = self._can_submit()
                button.draw(dst)
        else:
            self.btn_allin.draw(dst)
            theme.text(dst, "服务器权威确认 · F 弃牌 · C 过牌/跟注 · R 下注", (700, 856), 14, theme.TEXT_DIM, "center")

    def _draw_between_hands(
        self, dst: pygame.Surface, table: Mapping[str, object]
    ) -> None:
        result = _mapping(table.get("result"))
        title, subtitle = "本手结束", "结算展示后将自动开始下一手"
        if result is not None:
            title = "摊牌结算" if result.get("showdown") is True else "未摊牌收池"
            summaries = _sequence(self.state.get("public_hand_summaries")) if self.state is not None else None
            matching = None
            for item in summaries or ():
                if isinstance(item, Mapping) and item.get("hand_id") == result.get("hand_id"):
                    matching = item
            if matching is not None:
                safe_text = matching.get("summary") or matching.get("text") or matching.get("result_text")
                if isinstance(safe_text, str) and safe_text:
                    subtitle = safe_text[:66]
            winners = result.get("winners")
            names = {
                _integer(seat.get("seat"), -1): (
                    seat.get("name") if isinstance(seat.get("name"), str) else "玩家"
                )
                for seat in self._table_seats()
            }
            winner_items = _sequence(winners)
            if winner_items:
                parts = []
                for item in winner_items:
                    if isinstance(item, Mapping):
                        seat_id = _integer(
                            item.get("target_seat"),
                            _integer(item.get("seat"), -1),
                        )
                        parts.append(f"{names.get(seat_id, '玩家')} +{_integer(item.get('amount'))}")
                if parts and matching is None:
                    subtitle = " · ".join(parts)
        panel = pygame.Rect(340, 230, 550, 106)
        Panel.draw(dst, panel, alpha=238, border=theme.AMBER)
        theme.text(dst, title, (panel.centerx, panel.top + 27), 28, theme.AMBER_LIGHT, "center", shadow=True)
        theme.text(dst, subtitle, (panel.centerx, panel.top + 72), 17, theme.GOLD, "center")
        shown = table.get("shown")
        shown_seats = _sequence(shown)
        already_shown = shown_seats is not None and self.viewer_seat in shown_seats
        viewer_table = next(
            (seat for seat in self._table_seats() if seat.get("seat") == self.viewer_seat),
            None,
        )
        viewer_room = self._viewer_room_seat()
        can_show = (
            self.viewer_seat is not None
            and viewer_table is not None
            and viewer_table.get("in_hand") is True
            and len(_card_list(viewer_table.get("cards"))) == 2
            and viewer_room is not None
            and viewer_room.get("occupant_type") in {None, "HUMAN"}
            and viewer_room.get("waiting_next_hand") is not True
        )
        self.btn_show.enabled = can_show and not already_shown and self._can_submit()
        features = _mapping(self.state.get("features")) if self.state is not None else None
        auto_next = features is not None and features.get("automatic_next_hand") is True
        self.btn_next.enabled = (
            not auto_next
            and self.viewer_is_host
            and not self._busted_pending()
            and not self._settlement_locked()
            and self._can_submit()
        )
        if self._settlement_locked():
            self.btn_next.label = "正在展示结算"
        else:
            self.btn_next.label = "自动续局中" if auto_next else "下一手"
        self.btn_show.draw(dst)
        self.btn_next.draw(dst)
        self.btn_table_back.draw(dst)

    def _draw_bust_controls(self, dst: pygame.Surface) -> None:
        panel = pygame.Rect(390, 620, 720, 150)
        Panel.draw(dst, panel, alpha=242, border=theme.DANGER)
        theme.text(dst, "你的筹码已经清空", (panel.centerx, panel.top + 22), 22, theme.DANGER, "center")
        theme.text(dst, "可补回筹码，或离座旁观；房主离座后仍保留管理权", (panel.centerx, panel.top + 51), 14, theme.TEXT_DIM, "center")
        self.rebuy_field.draw(dst)
        self.btn_rebuy.enabled = self._can_submit() and self.rebuy_field.valid
        self.btn_leave_seat.enabled = self.viewer_seat is not None and self._can_submit()
        self.btn_rebuy.draw(dst)
        self.btn_leave_seat.draw(dst)
        self.btn_table_back.draw(dst)

    def _draw_info_panel(
        self, dst: pygame.Surface, table: Mapping[str, object] | None
    ) -> None:
        Panel.draw(dst, (PANEL_X, 0, 1600 - PANEL_X, 900), alpha=238)
        pygame.draw.line(dst, theme.AMBER_DARK, (PANEL_X, 0), (PANEL_X, 900), 2)
        x = PANEL_X + 18
        theme.text(dst, "朋友局", (x, 18), 20, theme.AMBER_LIGHT)
        theme.text(dst, f"房间 {self.room_id}", (x, 50), 16, theme.TEAL)
        theme.text(dst, f"状态 · {self.phase}", (x, 77), 14, theme.TEXT_DIM)
        if table is not None:
            street = table.get("street")
            street_text = _STREET_LABEL.get(street, str(street)) if isinstance(street, str) else "同步中"
            theme.text(dst, street_text, (PANEL_X + 355, 76), 15, theme.GOLD, "topright")
            theme.text(dst, f"底池 ¢ {self._pot_total(table)}", (x, 108), 22, theme.GOLD)

        theme.text(dst, "玩家", (x, 160), 17, theme.TEXT_DIM)
        y = 192
        for seat in self._table_seats():
            seat_id = _integer(seat.get("seat"), -1)
            name = seat.get("name") if isinstance(seat.get("name"), str) else f"玩家{seat_id + 1}"
            mark = "你" if seat_id == self.viewer_seat else ""
            if seat.get("folded") is True:
                mark = "弃牌"
            elif seat.get("all_in") is True:
                mark = "全下"
            elif table is not None and table.get("acting_seat") == seat_id:
                mark = "行动中"
            theme.text(dst, name[:15], (x, y), 15, theme.TEXT)
            theme.text(dst, f"¢{max(0, _integer(seat.get('stack')))}", (x + 160, y), 15, theme.GOLD)
            if mark:
                color = theme.DANGER if mark in {"弃牌", "全下"} else theme.AMBER_LIGHT
                theme.text(dst, mark, (x + 272, y), 13, color)
            y += 30

        # 房主待处理项只显示公开申请，不泄漏牌面或私有凭据。
        self._decision_hitboxes.clear()
        pending_y = min(462, y + 18)
        if self.viewer_is_host and not self._settlement_locked():
            pending_y = self._draw_host_decisions(dst, x, pending_y)

        theme.text(dst, "牌局记录", (x, max(492, pending_y)), 17, theme.TEXT_DIM)
        log_top = max(518, pending_y + 26)
        self.log.draw(dst, (x, log_top, 338, max(70, 688 - log_top)), max_lines=6, size=13)

        self.btn_history.draw(dst)
        self.btn_stacks.draw(dst)
        Panel.draw(dst, (x - 4, 754, 342, 111), alpha=170, border=theme.TEAL_DARK)
        theme.text(dst, "权威服务器模式", (x + 8, 766), 15, theme.TEAL)
        theme.text(dst, "只显示本人可见或已公开底牌", (x + 8, 794), 13, theme.TEXT_DIM)
        theme.text(dst, "连续版本播放演出 · 跳版直接同步", (x + 8, 820), 13, theme.TEXT_DIM)
        if self._paused():
            theme.text(dst, "暂停中仍可查看记录与筹码", (x + 8, 844), 13, theme.AMBER_LIGHT)

    def _draw_host_decisions(self, dst: pygame.Surface, x: int, y: int) -> int:
        requests = _sequence(self.state.get("top_up_requests")) if self.state is not None else None
        busts = _sequence(self.state.get("bust_decisions")) if self.state is not None else None
        rows: list[tuple[str, int, str]] = []
        for request in requests or ():
            if not isinstance(request, Mapping):
                continue
            status = request.get("status")
            if not isinstance(status, str) or status.upper() != "PENDING_APPROVAL":
                continue
            seat = _integer(request.get("target_seat"), _integer(request.get("seat"), -1))
            if seat < 0:
                continue
            target = _integer(request.get("target_stack"))
            name = request.get("display_name") if isinstance(request.get("display_name"), str) else f"座位{seat + 1}"
            rows.append(("topup", seat, f"{name} 申请补至 {target}"))
        for decision in busts or ():
            if not isinstance(decision, Mapping):
                continue
            kind = decision.get("decision_by") or decision.get("owner")
            occupant = decision.get("occupant_type")
            if occupant != "AI" and kind not in {"HOST", "host"}:
                continue
            seat = _integer(decision.get("target_seat"), _integer(decision.get("seat"), -1))
            if seat < 0:
                continue
            name = decision.get("display_name") if isinstance(decision.get("display_name"), str) else f"AI 座位{seat + 1}"
            rows.append(("ai", seat, f"{name} 筹码清空"))
        if not rows:
            return y
        theme.text(dst, "房主待处理", (x, y), 15, theme.AMBER_LIGHT)
        y += 25
        for kind, seat, label in rows[:2]:
            theme.text(dst, label[:22], (x, y + 6), 13, theme.TEXT)
            yes = pygame.Rect(x + (190 if kind == "topup" else 158), y, 66 if kind == "topup" else 54, 28)
            no = pygame.Rect(x + (264 if kind == "topup" else 276), y, 66 if kind == "topup" else 54, 28)
            pygame.draw.rect(dst, (40, 29, 18), yes, border_radius=6)
            pygame.draw.rect(dst, theme.TEAL, yes, 1, border_radius=6)
            pygame.draw.rect(dst, (40, 29, 18), no, border_radius=6)
            pygame.draw.rect(dst, theme.DANGER, no, 1, border_radius=6)
            theme.text(dst, "批准" if kind == "topup" else "补回", yes.center, 13, theme.TEXT, "center")
            theme.text(dst, "拒绝" if kind == "topup" else "移出", no.center, 13, theme.TEXT, "center")
            self._decision_hitboxes[("topup_yes" if kind == "topup" else "ai_rebuy", seat)] = yes
            self._decision_hitboxes[("topup_no" if kind == "topup" else "ai_remove", seat)] = no
            if kind == "ai":
                style = pygame.Rect(x + 216, y, 56, 28)
                pygame.draw.rect(dst, (40, 29, 18), style, border_radius=6)
                pygame.draw.rect(dst, theme.AMBER, style, 1, border_radius=6)
                theme.text(dst, "打法", style.center, 13, theme.TEXT, "center")
                self._decision_hitboxes[("ai_style", seat)] = style
            y += 34
        return y + 2


class _OnlineSeatDialog:
    """联机空位配置：真人认领或房主添加指定 AI。"""

    PANEL = pygame.Rect(92, 46, 1416, 808)

    def __init__(self, scene: FriendsRoomScene, seat: int) -> None:
        self.scene = scene
        self.seat = seat
        config = _mapping(scene.state.get("config")) if scene.state is not None else None
        self.bb = max(1, _integer(config.get("big_blind"), 10)) if config else 10
        default_buyin = _integer(config.get("buyin"), self.bb * 100) if config else self.bb * 100
        self.mode = "AI" if scene.viewer_is_host and scene.viewer_seat is not None else "SELF"
        self.catalog = persona_catalog(scene.seed)
        self.styles = style_catalog()
        used = {
            item.get("persona_id")
            for item in scene._room_seats()
            if item.get("occupant_type") == "AI" and isinstance(item.get("persona_id"), str)
        }
        self.persona_id = next(
            (persona.persona_id for persona in self.catalog if persona.persona_id not in used),
            self.catalog[0].persona_id,
        )
        self.style_key = persona_by_id(self.persona_id, scene.seed).style_key
        self.field = NumberField(
            (830, 650, 150, 42),
            "买入 BB",
            max(10, min(1000, default_buyin // self.bb)),
            minimum=10,
            maximum=1000,
        )
        self.btn_self = Button((170, 124, 210, 44), "我坐这个位置", lambda: self._set_mode("SELF"), size=18)
        self.btn_ai = Button(
            (398, 124, 210, 44),
            "添加 AI 牌手",
            lambda: self._set_mode("AI"),
            size=18,
            enabled=scene.viewer_is_host,
        )
        self.persona_buttons = [
            Button(
                (150 + (index % 5) * 92, 236 + (index // 5) * 46, 84, 36),
                persona.display_name.split()[-1],
                lambda key=persona.persona_id: self._select_persona(key),
                size=13,
                enabled=persona.persona_id not in used,
            )
            for index, persona in enumerate(self.catalog)
        ]
        self.style_buttons = [
            Button(
                (690 + (index % 4) * 176, 270 + (index // 4) * 50, 166, 40),
                preset.label,
                lambda key=preset.key: setattr(self, "style_key", key),
                size=14,
            )
            for index, preset in enumerate(self.styles)
        ]
        self.preset_buttons = [
            Button(
                (718 + index * 112, 586, 100, 38),
                f"{value}BB",
                lambda amount=value: self.field.set_value(amount),
                size=15,
            )
            for index, value in enumerate((50, 100, 200, 500))
        ]
        self.btn_confirm = Button((1030, 770, 250, 52), "确认入座", self._confirm, size=20)
        self.btn_cancel = Button((730, 770, 250, 52), "取消", self._cancel, size=20, danger=True)
        self.preview = self._make_preview()

    def _make_preview(self) -> Bust:
        persona = persona_by_id(self.persona_id, self.scene.seed)
        return Bust(persona.species, seed=self.seat * 101 + len(self.persona_id), scale=0.72)

    def _set_mode(self, mode: str) -> None:
        if mode == "AI" and not self.scene.viewer_is_host:
            return
        self.mode = mode

    def _select_persona(self, persona_id: str) -> None:
        button = next(
            (item for item, persona in zip(self.persona_buttons, self.catalog) if persona.persona_id == persona_id),
            None,
        )
        if button is None or not button.enabled:
            return
        self.persona_id = persona_id
        self.preview = self._make_preview()

    def _cancel(self) -> None:
        self.scene._seat_dialog = None

    def _confirm(self) -> None:
        self.field._commit()
        if not self.field.valid:
            return
        buyin = int(self.field.value) * self.bb
        if self.mode == "AI":
            self.scene._add_ai(self.seat, self.persona_id, self.style_key, buyin)
        else:
            self.scene._claim_seat(self.seat, buyin)

    def handle_event(self, ev: pygame.event.Event) -> None:
        was_focused = self.field.focused
        self.field.handle_event(ev)
        if ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE and not was_focused:
            self._cancel()
            return
        self.btn_self.handle_event(ev)
        self.btn_ai.handle_event(ev)
        if self.mode == "AI":
            for button in self.persona_buttons:
                button.handle_event(ev)
            for button in self.style_buttons:
                button.handle_event(ev)
        for button in self.preset_buttons:
            button.handle_event(ev)
        self.btn_confirm.handle_event(ev)
        self.btn_cancel.handle_event(ev)

    def draw(self, dst: pygame.Surface, t: float) -> None:
        veil = pygame.Surface((1600, 900), pygame.SRCALPHA)
        veil.fill((0, 0, 0, 182))
        dst.blit(veil, (0, 0))
        Panel.draw(dst, self.PANEL, alpha=248, border=theme.AMBER)
        theme.text(dst, f"选择座位 · #{self.seat + 1}", (800, 78), 31, theme.AMBER_LIGHT, "center", shadow=True)
        self.btn_self.selected = self.mode == "SELF"
        self.btn_ai.selected = self.mode == "AI"
        self.btn_self.draw(dst)
        self.btn_ai.draw(dst)
        if self.mode == "AI":
            theme.text(dst, "动物身份", (150, 198), 18, theme.TEXT_DIM)
            for persona, button in zip(self.catalog, self.persona_buttons):
                button.selected = persona.persona_id == self.persona_id
                button.draw(dst)
            persona = persona_by_id(self.persona_id, self.scene.seed)
            self.preview.draw(dst, (390, 510), t, IDLE)
            theme.text(dst, persona.display_name, (390, 642), 19, theme.AMBER_LIGHT, "center")
            theme.text(dst, "打法", (690, 232), 18, theme.TEXT_DIM)
            for preset, button in zip(self.styles, self.style_buttons):
                button.selected = preset.key == self.style_key
                button.draw(dst)
            selected = next(preset for preset in self.styles if preset.key == self.style_key)
            Panel.draw(dst, (690, 384, 694, 112), alpha=210, border=theme.FELT_EDGE)
            theme.text(dst, selected.description, (710, 404), 15, theme.TEXT)
            theme.text(
                dst,
                f"VPIP {selected.style.vpip * 100:.0f}% · PFR {selected.style.pfr * 100:.0f}% · 激进度 {selected.style.aggression:.1f}",
                (710, 452),
                14,
                theme.TEAL,
            )
        else:
            Panel.draw(dst, (250, 236, 1100, 252), alpha=214, border=theme.TEAL_DARK)
            theme.text(dst, "确认后你会坐到这个位置", (800, 306), 28, theme.AMBER_LIGHT, "center")
            note = "牌局进行中认领为空位预约，将从下一手加入。" if self.scene.phase == "PLAYING" else "入座后才可准备；取消准备后可以换到其他空位。"
            theme.text(dst, note, (800, 370), 17, theme.TEXT, "center")
            theme.text(dst, "房主身份与座位独立，换座不会转移房主权限。", (800, 420), 15, theme.TEAL, "center")
        theme.text(dst, "初始资金", (690, 548), 18, theme.TEXT_DIM)
        for button in self.preset_buttons:
            button.draw(dst)
        self.field.draw(dst)
        theme.text(dst, f"= ¢ {int(self.field.value) * self.bb if self.field.valid else 0}", (1000, 671), 17, theme.GOLD, "midleft")
        self.btn_confirm.label = "添加到该座位" if self.mode == "AI" else ("预约下一手" if self.scene.phase == "PLAYING" else "确认入座")
        self.btn_confirm.enabled = self.field.valid and self.scene._can_submit()
        self.btn_confirm.draw(dst)
        self.btn_cancel.draw(dst)


class _OnlineTopUpDialog:
    """真人主动/低码补码申请；服务端决定是否需要房主批准。"""

    def __init__(self, scene: FriendsRoomScene, *, automatic: bool = False) -> None:
        self.scene = scene
        self.automatic = automatic
        config = _mapping(scene.state.get("config")) if scene.state is not None else None
        self.bb = max(1, _integer(config.get("big_blind"), 10)) if config else 10
        viewer = next(
            (seat for seat in scene._table_seats() if seat.get("seat") == scene.viewer_seat),
            None,
        )
        self.current = max(0, _integer(viewer.get("stack"))) if viewer is not None else 0
        current_bb = self.current / self.bb
        default_bb = max(100, min(1000, math.ceil(current_bb / 50) * 50 + 50))
        minimum_bb = max(10, math.floor(current_bb) + 1)
        self.field = NumberField(
            (704, 480, 160, 44),
            "补至 BB",
            default_bb,
            minimum=minimum_bb,
            maximum=1000,
        )
        self.presets = [
            Button(
                (476 + index * 142, 408, 126, 40),
                f"{value}BB",
                lambda amount=value: self.field.set_value(amount),
                size=16,
                enabled=value > current_bb,
            )
            for index, value in enumerate((100, 200, 500, 1000))
        ]
        self.btn_confirm = Button((812, 604, 240, 50), "提交补码", self._confirm, size=20)
        self.btn_cancel = Button((548, 604, 240, 50), "暂不补码", self._cancel, size=19, danger=True)

    def _confirm(self) -> None:
        self.field._commit()
        if self.field.valid:
            self.scene._request_topup(int(self.field.value) * self.bb)

    def _cancel(self) -> None:
        signature = self.scene._active_low_stack_signature()
        if signature is not None:
            # 不论是自动弹窗还是用户从“申请补码”重新打开，只要权威端
            # 仍在等待低码决定，关闭就必须明确 decline。发送失败则保留
            # 对话框，避免把自动续手永久卡住。
            if self.scene._decline_low_stack(signature):
                self.scene._topup_dialog = None
            return
        self.scene._topup_dialog = None

    def handle_event(self, ev: pygame.event.Event) -> None:
        was_focused = self.field.focused
        self.field.handle_event(ev)
        if ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE and not was_focused:
            self._cancel()
            return
        for button in self.presets:
            button.handle_event(ev)
        self.btn_confirm.handle_event(ev)
        self.btn_cancel.handle_event(ev)

    def draw(self, dst: pygame.Surface) -> None:
        veil = pygame.Surface((1600, 900), pygame.SRCALPHA)
        veil.fill((0, 0, 0, 180))
        dst.blit(veil, (0, 0))
        panel = pygame.Rect(350, 210, 900, 500)
        Panel.draw(dst, panel, alpha=248, border=theme.DANGER if self.automatic else theme.AMBER)
        title = "筹码低于 100BB" if self.automatic else "申请补码"
        theme.text(dst, title, (panel.centerx, panel.top + 48), 31, theme.AMBER_LIGHT, "center", shadow=True)
        theme.text(
            dst,
            f"当前 ¢ {self.current}（{self.current / self.bb:.1f}BB） · 补码在下一手生效",
            (panel.centerx, panel.top + 104),
            17,
            theme.TEXT,
            "center",
        )
        approval = self.current > self.bb * 400
        note = "当前超过 400BB，本次申请需由房主批准。" if approval else "提交后将排队，在下一手开始前补至目标码量。"
        theme.text(dst, note, (panel.centerx, panel.top + 150), 15, theme.TEAL if not approval else theme.AMBER_LIGHT, "center")
        for button in self.presets:
            button.draw(dst)
        self.field.draw(dst)
        theme.text(dst, f"= ¢ {int(self.field.value) * self.bb if self.field.valid else 0}", (886, 502), 17, theme.GOLD, "midleft")
        self.btn_confirm.enabled = self.field.valid and self.scene._can_submit()
        self.btn_confirm.draw(dst)
        self.btn_cancel.draw(dst)


class _OnlineAiStyleDialog:
    """AI 爆仓时由房主更换打法；补回/移出仍是独立权威决定。"""

    def __init__(self, scene: FriendsRoomScene, seat: int) -> None:
        self.scene = scene
        self.seat = seat
        room_seat = next(
            (item for item in scene._room_seats() if item.get("seat") == seat),
            None,
        )
        current = room_seat.get("style_key") if room_seat is not None else "BAL"
        self.selected_key = current if isinstance(current, str) else "BAL"
        self.presets = style_catalog()
        self.buttons = [
            Button(
                (460 + (index % 4) * 178, 360 + (index // 4) * 52, 168, 42),
                preset.label,
                lambda key=preset.key: setattr(self, "selected_key", key),
                size=15,
            )
            for index, preset in enumerate(self.presets)
        ]
        self.btn_confirm = Button((812, 620, 240, 50), "确认更换", self._confirm, size=20)
        self.btn_cancel = Button((548, 620, 240, 50), "取消", self._cancel, size=19, danger=True)

    def _confirm(self) -> None:
        self.scene._change_ai_style(self.seat, self.selected_key)

    def _cancel(self) -> None:
        self.scene._ai_style_dialog = None

    def handle_event(self, ev: pygame.event.Event) -> None:
        if ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE:
            self._cancel()
            return
        for button in self.buttons:
            button.handle_event(ev)
        self.btn_confirm.handle_event(ev)
        self.btn_cancel.handle_event(ev)

    def draw(self, dst: pygame.Surface) -> None:
        veil = pygame.Surface((1600, 900), pygame.SRCALPHA)
        veil.fill((0, 0, 0, 180))
        dst.blit(veil, (0, 0))
        panel = pygame.Rect(350, 220, 900, 500)
        Panel.draw(dst, panel, alpha=248, border=theme.AMBER)
        theme.text(dst, f"更换 AI 打法 · 座位 {self.seat + 1}", (panel.centerx, panel.top + 52), 29, theme.AMBER_LIGHT, "center", shadow=True)
        theme.text(dst, "打法变更后，再选择补回筹码或将其移出。", (panel.centerx, panel.top + 104), 16, theme.TEXT_DIM, "center")
        for preset, button in zip(self.presets, self.buttons):
            button.selected = preset.key == self.selected_key
            button.draw(dst)
        selected = next(preset for preset in self.presets if preset.key == self.selected_key)
        theme.text(dst, selected.description, (panel.centerx, 500), 15, theme.TEXT, "center")
        theme.text(dst, f"VPIP {selected.style.vpip * 100:.0f}% · PFR {selected.style.pfr * 100:.0f}% · 激进度 {selected.style.aggression:.1f}", (panel.centerx, 542), 14, theme.TEAL, "center")
        self.btn_confirm.enabled = self.scene._can_submit()
        self.btn_confirm.draw(dst)
        self.btn_cancel.draw(dst)


__all__ = ["FriendsClient", "FriendsInfoScene", "FriendsRoomScene"]
