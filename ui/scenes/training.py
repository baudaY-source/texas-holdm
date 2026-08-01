"""训练场:HU 局面的快速分析 + GTO 求解分析视图。

左栏为场景编辑器(模板、hero 底牌/范围、villain 范围、公共牌、街、
底池/筹码、下注尺度);右栏两个标签页:

- **快速分析**:hero 手牌 vs villain 范围的蒙特卡洛胜率(``ai.equity``,
  <100ms),附底池赔率点评与翻前图表交叉参考;
- **GTO 求解**:后台线程跑 TexasSolver(``gto.solver_bridge.SolverRunner``),
  进度条显示 Iter/可利用度,完成后展示 13×13 策略矩阵(弃牌灰 /
  过牌跟注青 / 下注琥珀 / 加注红,混合策略按比例色带),可沿动作树
  下钻,点击格子查看单牌型的逐动作频率。

**注意**:TexasSolver 仅解两人(HU)局面;6-max 多人池请折算底池后
拆分为单挑子局面(界面顶部有一行提示)。
"""
from __future__ import annotations

import random
import queue

import pygame

from ai.equity import equity_vs_range
from gto.charts import RANKS, PreflopCharts, combos_for, hand_key
from gto.solver_bridge import BetSizes, SolverRunner, SolveResult
from training import analysis
from training.scenario import Scenario, builtin_templates

from .. import fx, theme
from ..widgets import Button, CardPicker, NumberField, Panel, RangePainter, grid_hand
from .manager import Scene, SceneManager

# ------------------------------------------------------------ 布局

LEFT_X = 24
GRID_Y = 224
BOARD_Y = 646
RIGHT_X = 648
TAB_Y = 26
MGRID_X, MGRID_Y, MCELL, MGAP = RIGHT_X, 254, 34, 2
INSPECT_X = 1130

ACTION_COLORS = {
    "FOLD": (110, 104, 96),
    "CHECK": theme.TEAL,
    "CALL": theme.TEAL,
    "BET": theme.AMBER,
    "RAISE": theme.DANGER,
    "ALLIN": theme.DANGER,
    "DEAL": theme.TEXT_DIM,
}
ACTION_LABEL = {
    "FOLD": "弃牌",
    "CHECK": "过牌",
    "CALL": "跟注",
    "BET": "下注",
    "RAISE": "加注",
    "ALLIN": "全下",
    "DEAL": "发牌",
}


def action_color(action: str) -> tuple[int, int, int]:
    """动作名(``"BET 25.000000"``)→ 展示颜色。"""
    return ACTION_COLORS.get(action.split()[0], theme.TEXT_DIM)


def fmt_action(action: str) -> str:
    """动作名 → 短标签(``"BET 25.000000"`` → ``"下注 25"``)。"""
    parts = action.split()
    verb = ACTION_LABEL.get(parts[0], parts[0])
    if len(parts) > 1:
        try:
            return f"{verb} {float(parts[1]):g}"
        except ValueError:
            return f"{verb} {parts[1]}"
    return verb


class TrainingScene(Scene):
    """训练场主场景(HU 分析工具)。

    :param seed: 快速分析蒙特卡洛的随机种子(截图/测试可复现)。
    :param solver: 可注入的 ``SolverRunner``(测试替换);缺省新建。
    """

    def __init__(
        self,
        manager: SceneManager | None = None,
        seed: int | None = None,
        solver: SolverRunner | None = None,
    ) -> None:
        super().__init__(manager)
        self.seed = seed
        self.rng = random.Random(seed)
        self.charts = PreflopCharts()
        self.solver = solver or SolverRunner()

        # ---------------- 编辑器状态
        self.templates = builtin_templates()
        self.scenario_name = "自定义场景"
        self.hero_cards: list[str] = []
        self.board: list[str] = []
        self.street = "flop"
        self.hero_is_ip = True
        self.edit_tab = "hero_cards"  # hero_cards / hero_range / villain_range
        self.show_advanced = False
        self.status = ""

        self.hero_painter = RangePainter((LEFT_X, GRID_Y))
        self.villain_painter = RangePainter((LEFT_X, GRID_Y))
        self.hero_picker = CardPicker((LEFT_X, GRID_Y), 2, title="hero 底牌:")
        self.board_picker = CardPicker((LEFT_X, BOARD_Y), 5, title="公共牌:", cell=26)

        self.f_pot = NumberField((352, 792, 76, 30), "底池", 55, 1, 100000)
        self.f_stack = NumberField((520, 792, 90, 30), "有效筹码", 975, 1, 1000000)
        self.f_bet = NumberField((96, 832, 56, 28), "下注%", 50, 1, 400)
        self.f_raise = NumberField((256, 832, 56, 28), "加注%", 60, 1, 400)
        self.f_acc = NumberField((96, 868, 56, 26), "精度", 1.0, 0.1, 100, integer=False)
        self.f_iter = NumberField((256, 868, 70, 26), "迭代", 200, 1, 100000)
        self.allin_allowed = True

        # ---------------- 求解/展示状态
        self.tab = "quick"  # quick / gto
        self.result: SolveResult | None = None
        self.result_label = ""
        self._path: list[tuple[str, object]] = []  # (动作键, 节点);首元素 ("根", root)
        self._inspect: tuple[int, int] | None = None
        self._prog_iter = 0
        self._prog_exploit: float | None = None
        self._solving = False
        self._quick_cache_key: tuple | None = None
        self._quick: dict | None = None

        # ---------------- 按钮
        self.btn_tab_quick = Button((RIGHT_X, TAB_Y, 160, 40), "快速分析", lambda: self._set_tab("quick"), 20)
        self.btn_tab_gto = Button((RIGHT_X + 172, TAB_Y, 160, 40), "GTO 求解", lambda: self._set_tab("gto"), 20)
        self._template_buttons = [
            Button((LEFT_X + i * 196, 92, 188, 36), t.name, lambda t=t: self._apply_template(t), 15)
            for i, t in enumerate(self.templates)
        ]
        self.btn_save = Button((LEFT_X, 140, 88, 32), "保存", self._save_scenario, 16)
        self.btn_load = Button((LEFT_X + 100, 140, 88, 32), "加载", self._load_scenario, 16)
        self._edit_tab_buttons = [
            Button((LEFT_X, 184, 140, 30), "hero 手牌", lambda: self._set_edit_tab("hero_cards"), 15),
            Button((LEFT_X + 150, 184, 140, 30), "hero 范围", lambda: self._set_edit_tab("hero_range"), 15),
            Button((LEFT_X + 300, 184, 140, 30), "villain 范围", lambda: self._set_edit_tab("villain_range"), 15),
        ]
        self.btn_pos = Button((LEFT_X + 452, 184, 158, 30), "hero: BTN(IP)", self._toggle_pos, 15)
        self._street_buttons = [
            Button((LEFT_X + i * 96, 792, 86, 30), label, lambda s=s: self._set_street(s), 15)
            for i, (s, label) in enumerate(
                (("flop", "翻牌"), ("turn", "转牌"), ("river", "河牌"))
            )
        ]
        self.btn_allin = Button((LEFT_X + 340, 832, 96, 28), "全下 开", self._toggle_allin, 14)
        self.btn_adv = Button((LEFT_X + 452, 832, 158, 28), "高级 +", self._toggle_adv, 14)
        self.btn_solve = Button((RIGHT_X, 84, 150, 42), "开始求解", self._start_solve, 20)
        self.btn_cancel = Button(
            (RIGHT_X + 162, 84, 100, 42), "取消", self._cancel_solve, 18, danger=True
        )
        self.btn_solve_fixture = Button(
            (RIGHT_X + 274, 84, 150, 42), "载入示例解", self._load_fixture, 15
        )
        self._path_buttons: list[Button] = []
        self._child_buttons: list[Button] = []
        self._fields = [self.f_pot, self.f_stack, self.f_bet, self.f_raise, self.f_acc, self.f_iter]

        self._backdrop = fx.workspace_backdrop((1600, 900), seed=17)

    # ------------------------------------------------------------ 编辑器操作

    def _set_tab(self, tab: str) -> None:
        self.tab = tab

    def _set_edit_tab(self, tab: str) -> None:
        self.edit_tab = tab

    def _toggle_pos(self) -> None:
        self.hero_is_ip = not self.hero_is_ip
        self.btn_pos.label = "hero: BTN(IP)" if self.hero_is_ip else "hero: BB(OOP)"

    def _set_street(self, street: str) -> None:
        self.street = street
        need = {"flop": 3, "turn": 4, "river": 5}[street]
        self.board = self.board[:need]
        self.board_picker.set_cards(self.board)

    def _toggle_allin(self) -> None:
        self.allin_allowed = not self.allin_allowed
        self.btn_allin.label = "全下 开" if self.allin_allowed else "全下 关"

    def _toggle_adv(self) -> None:
        self.show_advanced = not self.show_advanced
        self.btn_adv.label = "高级 -" if self.show_advanced else "高级 +"

    def _apply_template(self, t: Scenario) -> None:
        self.scenario_name = t.name
        self.hero_cards = list(t.hero_cards)
        self.hero_picker.set_cards(self.hero_cards)
        self.board = list(t.board)
        self.board_picker.set_cards(self.board)
        self.street = t.street
        self.hero_is_ip = t.hero_is_ip
        self.btn_pos.label = "hero: BTN(IP)" if t.hero_is_ip else "hero: BB(OOP)"
        self.hero_painter.set_weights(t.hero_range)
        self.villain_painter.set_weights(t.villain_range)
        self.f_pot.set_value(t.pot)
        self.f_stack.set_value(t.effective_stack)
        bs = (t.bet_sizes or {}).get("oop", {}).get("flop")
        if bs is not None:
            self.f_bet.set_value(bs.bet[0] if bs.bet else 50)
            self.f_raise.set_value(bs.raises[0] if bs.raises else 60)
            self.allin_allowed = bs.allin
            self.btn_allin.label = "全下 开" if self.allin_allowed else "全下 关"
        self.f_acc.set_value(t.accuracy)
        self.f_iter.set_value(t.max_iteration)
        self.status = f"已套用模板:{t.name}"

    def _collect_scenario(self) -> Scenario:
        bet = float(self.f_bet.value)
        raise_pct = float(self.f_raise.value)
        sizes = {
            side: {
                street: BetSizes(
                    bet=[bet], raises=[raise_pct], donk=[], allin=self.allin_allowed
                )
                for street in ("flop", "turn", "river")
            }
            for side in ("oop", "ip")
        }
        return Scenario(
            name=self.scenario_name,
            hero_cards=list(self.hero_cards),
            hero_range=dict(self.hero_painter.weights),
            villain_range=dict(self.villain_painter.weights),
            board=list(self.board),
            street=self.street,
            pot=float(self.f_pot.value),
            effective_stack=float(self.f_stack.value),
            hero_is_ip=self.hero_is_ip,
            bet_sizes=sizes,
            accuracy=float(self.f_acc.value),
            max_iteration=int(self.f_iter.value),
        )

    def _save_scenario(self) -> None:
        sc = self._collect_scenario()
        if sc.name in ("自定义场景", ""):
            import time

            sc.name = f"场景 {time.strftime('%m%d_%H%M%S')}"
            self.scenario_name = sc.name
        path = sc.save()
        self.status = f"已保存:{path.name}"

    def _load_scenario(self) -> None:
        saved = Scenario.list_saved()
        if not saved:
            self.status = "暂无已存场景(先点保存)"
            return
        names = [p.stem for p in saved]
        idx = (names.index(self.scenario_name) + 1) % len(saved) if self.scenario_name in names else 0
        try:
            sc = Scenario.load(saved[idx])
        except Exception as e:  # noqa: BLE001
            self.status = f"加载失败:{e}"
            return
        t = sc
        self._apply_template(t)
        self.status = f"已加载:{t.name}"

    # ------------------------------------------------------------ 求解

    def _start_solve(self) -> None:
        if self._solving:
            return
        try:
            cfg = self._collect_scenario().to_solve_config()
        except ValueError as e:
            self.status = f"无法求解:{e}"
            self.tab = "gto"
            return
        try:
            self.solver.start(cfg)
        except Exception as e:  # noqa: BLE001
            self.status = f"无法启动求解器:{e}"
            self.tab = "gto"
            return
        self._solving = True
        self._prog_iter = 0
        self._prog_exploit = None
        self.status = "求解中…"
        self.tab = "gto"

    def _cancel_solve(self) -> None:
        self.solver.cancel()

    def _load_fixture(self) -> None:
        """载入 tests 夹具作为示例解(无求解器时也能浏览分析视图)。"""
        from gto.solver_bridge import parse_result
        from ui.respath import res_path

        path = res_path("tests", "fixtures", "solver_tiny.json")
        if not path.is_file():
            self.status = "示例解不存在:tests/fixtures/solver_tiny.json"
            return
        self.load_result(parse_result(path), label="示例解(tiny)")

    def load_result(self, result: SolveResult, label: str = "") -> None:
        """把一份求解结果挂到分析视图(截图/测试可直接注入)。"""
        self.result = result
        self.result_label = label
        self._path = [("根", result.root)]
        self._inspect = None
        self._rebuild_path_buttons()
        self.tab = "gto"

    def _drain_solver_events(self) -> None:
        while True:
            try:
                ev = self.solver.events.get_nowait()
            except queue.Empty:
                break
            if ev.kind == "progress":
                self._prog_iter = ev.iteration
                self._prog_exploit = ev.exploitability
                self.status = f"求解中… Iter {ev.iteration} · 可利用度 {ev.exploitability:.2f}"
            elif ev.kind == "done":
                self._solving = False
                assert ev.result is not None
                self.load_result(ev.result, label="本次求解")
                expl = f"{ev.exploitability:.3f}" if ev.exploitability is not None else "?"
                self.status = f"求解完成 · Iter {ev.iteration} · 可利用度 {expl}"
            elif ev.kind == "error":
                self._solving = False
                self.status = f"求解失败:{ev.error}"

    # ------------------------------------------------------------ 动作树导航

    def _current_node(self):
        return self._path[-1][1] if self._path else None

    def _descend(self, key: str) -> None:
        node = self._current_node()
        if node is not None and key in node.children:
            self._path.append((key, node.children[key]))
            self._inspect = None
            self._rebuild_path_buttons()

    def _ascend(self, depth: int) -> None:
        self._path = self._path[: depth + 1]
        self._inspect = None
        self._rebuild_path_buttons()

    def _rebuild_path_buttons(self) -> None:
        self._path_buttons = []
        for i, (key, _node) in enumerate(self._path):
            label = "根" if i == 0 else fmt_action(key)
            self._path_buttons.append(
                Button(
                    (RIGHT_X + i * 96, 168, 90, 28),
                    label,
                    lambda d=i: self._ascend(d),
                    13,
                )
            )
        node = self._current_node()
        self._child_buttons = []
        if node is not None and not node.is_chance:
            keys = [k for k in node.children if not k.startswith("DEAL:")][:5]
            for i, key in enumerate(keys):
                self._child_buttons.append(
                    Button(
                        (RIGHT_X + i * 96, 202, 90, 28),
                        fmt_action(key),
                        lambda k=key: self._descend(k),
                        13,
                    )
                )

    # ------------------------------------------------------------ 快速分析

    def _villain_combos_expanded(self) -> list[tuple[str, str]]:
        """villain 权重范围展开为组合列表(按权重复制,供均匀抽样近似)。"""
        known = set(self.hero_cards) | set(self.board)
        combos: list[tuple[str, str]] = []
        for hand, w in self.villain_painter.weights.items():
            if w <= 0:
                continue
            reps = max(1, round(w * 10))
            for c in combos_for(hand):
                if c[0] in known or c[1] in known:
                    continue
                combos.extend([c] * reps)
        return combos

    def _quick_key(self) -> tuple:
        weights = tuple(
            sorted((h, round(w, 3)) for h, w in self.villain_painter.weights.items() if w > 0)
        )
        return (
            tuple(self.hero_cards),
            tuple(self.board),
            weights,
            float(self.f_pot.value),
            float(self.f_bet.value),
            self.hero_is_ip,
        )

    def _update_quick(self) -> None:
        key = self._quick_key()
        if key == self._quick_cache_key:
            return
        self._quick_cache_key = key
        self._quick = None
        if len(self.hero_cards) != 2:
            return
        combos = self._villain_combos_expanded()
        if not combos:
            return
        equity = equity_vs_range(self.hero_cards, self.board, combos, 400, self.rng)
        n_combos = len(set(combos))
        pot = float(self.f_pot.value)
        bet_pct = float(self.f_bet.value) / 100.0
        bet_amt = pot * bet_pct
        pot_odds = bet_amt / (pot + 2 * bet_amt) if pot > 0 else 0.0
        hand = hand_key(self.hero_cards)
        chart_pos = "BTN" if self.hero_is_ip else "SB"  # BB 暂以 SB 表近似
        rfi = self.charts.rfi_raise_freq(chart_pos, hand)
        self._quick = {
            "equity": equity,
            "bet_amt": bet_amt,
            "pot_odds": pot_odds,
            "hand": hand,
            "rfi": rfi,
            "chart_pos": chart_pos,
            "n_combos": n_combos,
        }

    # ------------------------------------------------------------ 事件

    def handle_event(self, ev: pygame.event.Event) -> None:
        if ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE:
            focused = any(f.focused for f in self._fields)
            if not focused:
                self._back()
                return
        for f in self._fields:
            if f.handle_event(ev):
                return
        # 编辑器
        for b in self._template_buttons:
            b.handle_event(ev)
        self.btn_save.handle_event(ev)
        self.btn_load.handle_event(ev)
        for b in self._edit_tab_buttons:
            b.handle_event(ev)
        self.btn_pos.handle_event(ev)
        for b in self._street_buttons:
            b.handle_event(ev)
        self.btn_allin.handle_event(ev)
        self.btn_adv.handle_event(ev)
        if self.edit_tab == "hero_cards":
            if self.hero_picker.handle_event(ev):
                self.hero_cards = list(self.hero_picker.cards)
        elif self.edit_tab == "hero_range":
            self.hero_painter.handle_event(ev)
        else:
            self.villain_painter.handle_event(ev)
        if self.board_picker.handle_event(ev):
            self.board = list(self.board_picker.cards)
        # 右栏
        self.btn_tab_quick.handle_event(ev)
        self.btn_tab_gto.handle_event(ev)
        if self.tab == "gto":
            self.btn_solve.handle_event(ev)
            self.btn_cancel.handle_event(ev)
            self.btn_solve_fixture.handle_event(ev)
            for b in (*self._path_buttons, *self._child_buttons):
                b.handle_event(ev)
            if ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                cell = self._matrix_cell_at(ev.pos)
                if cell is not None:
                    self._inspect = cell

    def _back(self) -> None:
        self.solver.cancel()
        if self.manager is not None:
            from .menu import MenuScene

            self.manager.replace(MenuScene(seed=self.seed))

    def _matrix_cell_at(self, pos: tuple[int, int]) -> tuple[int, int] | None:
        for r in range(13):
            for c in range(13):
                rect = pygame.Rect(
                    MGRID_X + c * (MCELL + MGAP), MGRID_Y + r * (MCELL + MGAP), MCELL, MCELL
                )
                if rect.collidepoint(pos):
                    return (r, c)
        return None

    # ------------------------------------------------------------ 帧驱动

    def update(self, dt: float) -> None:
        self.hero_picker.blocked = set(self.board)
        self.board_picker.blocked = set(self.hero_cards)
        self._drain_solver_events()
        self._update_quick()

    # ------------------------------------------------------------ 绘制

    def draw(self, dst: pygame.Surface) -> None:
        dst.fill(theme.BG)
        dst.blit(self._backdrop, (0, 0))
        Panel.draw(dst, (12, 84, 620, 806), alpha=178, border=theme.AMBER_DARK)
        pygame.draw.line(dst, theme.AMBER_DARK, (636, 18), (636, 882), 1)
        theme.text(dst, "训练场 · HU 分析", (LEFT_X, 26), 32, theme.AMBER_LIGHT, shadow=True)
        theme.text(
            dst,
            "求解器为两人(HU)模型,多人池请折算底池后拆分为单挑子局面",
            (LEFT_X, 68),
            14,
            theme.TEXT_DIM,
        )
        self._draw_editor(dst)
        self._draw_tabs(dst)
        if self.tab == "quick":
            self._draw_quick(dst)
        else:
            self._draw_gto(dst)

    def _draw_editor(self, dst: pygame.Surface) -> None:
        for b, template in zip(self._template_buttons, self.templates):
            b.selected = template.name == self.scenario_name
            b.draw(dst)
        self.btn_save.draw(dst)
        self.btn_load.draw(dst)
        theme.text(dst, self.scenario_name, (LEFT_X + 200, 148), 15, theme.GOLD)
        for b, tab in zip(
            self._edit_tab_buttons,
            ("hero_cards", "hero_range", "villain_range"),
        ):
            b.selected = tab == self.edit_tab
            b.draw(dst)
        self.btn_pos.selected = self.hero_is_ip
        self.btn_pos.draw(dst)
        if self.edit_tab == "hero_cards":
            self.hero_picker.draw(dst)
            theme.text(
                dst,
                "hero 求解范围(留空则以具体手牌代替):",
                (LEFT_X, GRID_Y + 160),
                14,
                theme.TEXT_DIM,
            )
            theme.text(
                dst,
                f"当前范围组合 {self.hero_painter.combo_count():.1f} · "
                f"{self.hero_painter.range_pct():.1%}(切到「hero 范围」编辑)",
                (LEFT_X, GRID_Y + 182),
                14,
                theme.TEXT_DIM,
            )
        elif self.edit_tab == "hero_range":
            self.hero_painter.draw(dst)
        else:
            self.villain_painter.draw(dst)
        self.board_picker.draw(dst)
        for b, street in zip(self._street_buttons, ("flop", "turn", "river")):
            b.selected = street == self.street
            b.draw(dst)
        for f in (self.f_pot, self.f_stack, self.f_bet, self.f_raise):
            f.draw(dst)
        self.btn_allin.selected = self.allin_allowed
        self.btn_allin.draw(dst)
        self.btn_adv.selected = self.show_advanced
        self.btn_adv.draw(dst)
        if self.show_advanced:
            self.f_acc.draw(dst)
            self.f_iter.draw(dst)
        if self.status:
            theme.text(dst, self.status[:72], (RIGHT_X, 132), 14, theme.GOLD)

    def _draw_tabs(self, dst: pygame.Surface) -> None:
        self.btn_tab_quick.selected = self.tab == "quick"
        self.btn_tab_gto.selected = self.tab == "gto"
        self.btn_tab_quick.draw(dst)
        self.btn_tab_gto.draw(dst)

    # ------------------------------------------------------------ 快速分析绘制

    def _draw_quick(self, dst: pygame.Surface) -> None:
        Panel.draw(dst, (RIGHT_X, 160, 928, 330), alpha=215, border=theme.AMBER_DARK)
        x, y = RIGHT_X + 24, 182
        q = self._quick
        if len(self.hero_cards) != 2:
            theme.text(dst, "请在左侧「hero 手牌」选两张底牌", (x, y), 20, theme.TEXT_DIM)
            return
        if q is None:
            theme.text(dst, "请先编辑 villain 范围(左侧「villain 范围」)", (x, y), 20, theme.TEXT_DIM)
            return
        hero = " ".join(self.hero_cards)
        board = " ".join(self.board) if self.board else "(未发)"
        theme.text(dst, f"hero {hero} · {q['hand']}  vs  villain 范围", (x, y), 20, theme.TEXT)
        theme.text(dst, f"公共牌 {board} · 范围组合 {q['n_combos']}", (x, y + 30), 15, theme.TEXT_DIM)
        eq = q["equity"]
        theme.text(dst, f"{eq:.1%}", (x, y + 66), 64, theme.AMBER_LIGHT, shadow=True)
        theme.text(dst, "胜率(蒙特卡洛 400 次)", (x + 190, y + 104), 15, theme.TEXT_DIM)
        bar = pygame.Rect(x, y + 150, 560, 20)
        pygame.draw.rect(dst, (24, 17, 11), bar, border_radius=10)
        fill = pygame.Rect(bar)
        fill.width = round(bar.width * eq)
        pygame.draw.rect(dst, theme.TEAL, fill, border_radius=10)
        pygame.draw.rect(dst, theme.AMBER, bar, 1, border_radius=10)
        # 赔率点评
        odds = q["pot_odds"]
        verdict = (
            "胜率覆盖赔率,跟注有利"
            if eq >= odds + 0.04
            else "边缘,跟弃两可"
            if eq >= odds - 0.04
            else "胜率不抵赔率,倾向弃牌"
        )
        theme.text(
            dst,
            f"若 villain 下注 {self.f_bet.value:g}% 底池({q['bet_amt']:.0f}),"
            f"所需胜率 {odds:.1%} → {verdict}",
            (x, y + 188),
            16,
            theme.TEXT,
        )
        note = "" if self.hero_is_ip else "(BB 以 SB 表近似)"
        theme.text(
            dst,
            f"翻前表交叉参考:{q['chart_pos']} RFI 加注 {q['rfi']:.0%}{note}",
            (x, y + 220),
            16,
            theme.TEXT,
        )
        theme.text(
            dst,
            "即时启发式,未经求解;精确策略请用「GTO 求解」标签页",
            (x, y + 252),
            14,
            theme.TEXT_DIM,
        )

    # ------------------------------------------------------------ GTO 绘制

    def _draw_gto(self, dst: pygame.Surface) -> None:
        self.btn_solve.enabled = not self._solving
        self.btn_solve.draw(dst)
        self.btn_cancel.enabled = self._solving
        self.btn_cancel.draw(dst)
        self.btn_solve_fixture.enabled = not self._solving
        self.btn_solve_fixture.draw(dst)
        # 进度条
        bar = pygame.Rect(RIGHT_X + 436, 96, 340, 22)
        pygame.draw.rect(dst, (24, 17, 11), bar, border_radius=11)
        if self._solving:
            frac = min(1.0, self._prog_iter / max(1, int(self.f_iter.value)))
            fill = pygame.Rect(bar)
            fill.width = max(10, round(bar.width * frac))
            pygame.draw.rect(dst, theme.AMBER_DARK, fill, border_radius=11)
        pygame.draw.rect(dst, theme.AMBER, bar, 1, border_radius=11)
        prog = (
            f"Iter {self._prog_iter} · 可利用度 {self._prog_exploit:.2f}"
            if self._prog_exploit is not None
            else "待机"
        )
        theme.text(dst, prog, (bar.right + 12, bar.centery), 15, theme.TEXT_DIM, "midleft")
        if self.result is None:
            Panel.draw(dst, (RIGHT_X, 160, 928, 240), alpha=200, border=theme.FELT_EDGE)
            theme.text(
                dst,
                "编辑左侧场景后点「开始求解」;或「载入示例解」先浏览分析视图",
                (RIGHT_X + 24, 190),
                18,
                theme.TEXT_DIM,
            )
            return
        for b in (*self._path_buttons, *self._child_buttons):
            b.draw(dst)
        node = self._current_node()
        if node is None:
            return
        if node.is_chance:
            theme.text(
                dst,
                f"机会节点:{node.deal_number} 种发牌(dump_rounds 限制,v1 不再下钻)",
                (RIGHT_X, MGRID_Y),
                17,
                theme.TEXT_DIM,
            )
            return
        self._draw_matrix(dst, node)
        self._draw_inspector(dst, node)

    def _draw_matrix(self, dst: pygame.Surface, node) -> None:
        grid = analysis.street_matrix(node)
        who = "IP" if node.player == 0 else "OOP"
        hero_side = "IP" if self.hero_is_ip else "OOP"
        tag = "hero" if who == hero_side else "villain"
        theme.text(
            dst,
            f"行动方:{who}({tag})· 组合 {analysis.node_range_combo_count(node)}"
            + (f" · {self.result_label}" if self.result_label else ""),
            (MGRID_X, MGRID_Y - 22),
            15,
            theme.TEXT_DIM,
        )
        for r in range(13):
            for c in range(13):
                rect = pygame.Rect(
                    MGRID_X + c * (MCELL + MGAP), MGRID_Y + r * (MCELL + MGAP), MCELL, MCELL
                )
                mix = grid[r][c]
                pygame.draw.rect(dst, theme.BG_PANEL, rect)
                if mix.in_range:
                    y_off = 0
                    items = [(a, f) for a, f in mix.freqs.items() if f > 0.005]
                    total = sum(f for _, f in items) or 1.0
                    for a, f in items:
                        h = max(1, round(MCELL * f / total))
                        if y_off + h > MCELL:
                            h = MCELL - y_off
                        pygame.draw.rect(
                            dst,
                            action_color(a),
                            (rect.left, rect.top + y_off, MCELL, h),
                        )
                        y_off += h
                        if y_off >= MCELL:
                            break
                edge = theme.AMBER_LIGHT if (r, c) == self._inspect else theme.FELT_EDGE
                pygame.draw.rect(dst, edge, rect, 1)
                hand = grid_hand(r, c)
                tcolor = theme.TEXT if mix.in_range else theme.TEXT_DIM
                theme.text(dst, hand, rect.center, 11, tcolor, "center", shadow=mix.in_range)
        # 图例
        y = MGRID_Y + 13 * (MCELL + MGAP) + 8
        for i, (key, label) in enumerate(
            (("FOLD", "弃牌"), ("CHECK", "过牌/跟注"), ("BET", "下注"), ("RAISE", "加注/全下"))
        ):
            x = MGRID_X + i * 120
            pygame.draw.rect(dst, ACTION_COLORS[key], (x, y, 14, 14))
            theme.text(dst, label, (x + 20, y + 7), 14, theme.TEXT_DIM, "midleft")

    def _draw_inspector(self, dst: pygame.Surface, node) -> None:
        Panel.draw(dst, (INSPECT_X, MGRID_Y - 30, 1600 - INSPECT_X - 24, 560), alpha=215, border=theme.AMBER_DARK)
        x, y = INSPECT_X + 18, MGRID_Y - 12
        theme.text(dst, "动作汇总(范围频率)", (x, y), 18, theme.AMBER_LIGHT)
        y += 34
        for s in analysis.action_summary(node):
            color = action_color(s.action)
            pygame.draw.rect(dst, color, (x, y + 3, 12, 12))
            theme.text(dst, fmt_action(s.action), (x + 18, y), 15, theme.TEXT)
            bar = pygame.Rect(x + 120, y + 4, 200, 12)
            pygame.draw.rect(dst, (24, 17, 11), bar)
            fill = pygame.Rect(bar)
            fill.width = round(bar.width * s.range_freq)
            pygame.draw.rect(dst, color, fill)
            theme.text(dst, f"{s.range_freq:.1%}", (x + 330, y), 15, theme.TEXT_DIM)
            y += 30
        y += 16
        if self._inspect is not None:
            hand = grid_hand(*self._inspect)
            freqs = analysis.compare_hero_hand(node, hand)
            theme.text(dst, f"手牌检视:{hand}", (x, y), 18, theme.AMBER_LIGHT)
            y += 32
            if any(f > 0 for f in freqs.values()):
                for a, f in freqs.items():
                    pygame.draw.rect(dst, action_color(a), (x, y + 3, 12, 12))
                    theme.text(dst, f"{fmt_action(a)}  {f:.1%}", (x + 18, y), 15, theme.TEXT)
                    y += 26
                theme.text(dst, "EV:求解器 dump 不含 EV(v0.2.0)", (x, y + 6), 13, theme.TEXT_DIM)
            else:
                theme.text(dst, "该牌型不在当前节点范围", (x, y), 15, theme.TEXT_DIM)
        else:
            theme.text(dst, "点击矩阵格子检视单牌型", (x, y), 15, theme.TEXT_DIM)
