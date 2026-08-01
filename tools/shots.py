"""无头截图/视觉走查管线。

设置 ``TAVERN_HEADLESS=1`` 与 dummy 视频驱动后,在离屏 Surface 上
渲染确定性画面:(a) 主菜单、(a2) 九席开局设置、(b) 翻牌前新发牌、(c) 翻牌圈
下注中 + 加注滑杆、(d) 摊牌横幅、(e) GTO 辅助面板(翻前 RFI 建议，
同时验收弃牌名牌、muck 动画与当前行动反馈)、
(o) 六人桌弃到两人的翻后 NFSP 实验 HU 池来源提示、
(f) 翻前图表查看器、(g-i) 训练场(编辑器/快速分析/求解矩阵)、
(j-k) 训练 drills、(l-m) 回顾/统计、(n) 出局处置框(买入/移出)、
(p) 九人 straddle 桌、(q) 空位召回配置。
全程种子固定,可复现。

用法: ``.venv/Scripts/python.exe tools/shots.py out_shots/``
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("TAVERN_HEADLESS", "1")
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pygame  # noqa: E402

from engine.state import Action, ActionType  # noqa: E402
from ui.scenes.game_setup import GameSetupScene  # noqa: E402
from ui.scenes.menu import MenuScene  # noqa: E402
from ui.scenes.table import TableScene  # noqa: E402

SIZE = (1600, 900)
SEED = 42

# ------------------------------------------------------------ 脚本手牌

# (c) 翻牌圈:六人跟到 30,翻牌后 seat1 下注 50,两人跟注,轮到人类
HOLE_C = {
    0: ("As", "Kd"), 1: ("Qh", "Jc"), 2: ("Td", "9c"),
    3: ("8s", "7d"), 4: ("6h", "5c"), 5: ("4d", "3s"),
}
BOARD_C = ["2c", "7h", "9d", "Kh", "4c"]
SCRIPT_C = {
    (1, "PREFLOP", 3): [Action(3, ActionType.CALL, 10)],
    (1, "PREFLOP", 4): [Action(4, ActionType.RAISE, 30)],
    (1, "PREFLOP", 5): [Action(5, ActionType.CALL, 30)],
    (1, "PREFLOP", 0): [Action(0, ActionType.CALL, 30)],
    (1, "PREFLOP", 1): [Action(1, ActionType.CALL, 25)],
    (1, "PREFLOP", 2): [Action(2, ActionType.CALL, 20)],
    (1, "FLOP", 1): [Action(1, ActionType.BET, 50)],
    (1, "FLOP", 2): [Action(2, ActionType.FOLD)],
    (1, "FLOP", 3): [Action(3, ActionType.CALL, 50)],
    (1, "FLOP", 4): [Action(4, ActionType.FOLD)],
    (1, "FLOP", 5): [Action(5, ActionType.CALL, 50)],
}

# (d) 人类口袋 A 一路过牌到摊牌
HOLE_D = {
    0: ("Ah", "Ad"), 1: ("Ks", "Qd"), 2: ("Js", "Tc"),
    3: ("7c", "6d"), 4: ("9h", "8d"), 5: ("5h", "4s"),
}
BOARD_D = ["2h", "3d", "6c", "9s", "Kh"]
SCRIPT_D: dict = {
    (1, "PREFLOP", 3): [Action(3, ActionType.FOLD)],
    (1, "PREFLOP", 4): [Action(4, ActionType.CALL, 10)],
    (1, "PREFLOP", 5): [Action(5, ActionType.FOLD)],
    (1, "PREFLOP", 0): [Action(0, ActionType.CALL, 10)],
    (1, "PREFLOP", 1): [Action(1, ActionType.CALL, 5)],
    (1, "PREFLOP", 2): [Action(2, ActionType.CHECK)],
}
for street in ("FLOP", "TURN", "RIVER"):
    for seat in (1, 2, 4, 0):
        SCRIPT_D[(1, street, seat)] = [Action(seat, ActionType.CHECK)]

# (e) 翻牌前三人弃牌,人类(BTN)未开池决策 → GTO 面板显示 RFI 建议
SCRIPT_E = {
    (1, "PREFLOP", 3): [Action(3, ActionType.FOLD)],
    (1, "PREFLOP", 4): [Action(4, ActionType.FOLD)],
    (1, "PREFLOP", 5): [Action(5, ActionType.FOLD)],
}

# (o) 六人桌翻前弃到人类与 seat1；翻牌后由真实 NFSP 先行动。
SCRIPT_O = {
    (1, "PREFLOP", 3): [Action(3, ActionType.FOLD)],
    (1, "PREFLOP", 4): [Action(4, ActionType.FOLD)],
    (1, "PREFLOP", 5): [Action(5, ActionType.FOLD)],
    (1, "PREFLOP", 0): [Action(0, ActionType.RAISE, 25)],
    (1, "PREFLOP", 1): [Action(1, ActionType.CALL, 20)],
    (1, "PREFLOP", 2): [Action(2, ActionType.FOLD)],
}

# (n) 狐狸 Foxy(seat 2)口袋 K 全下输给人类口袋 A → 出局处置框
HOLE_N = {
    0: ("Ah", "Ad"), 1: ("7c", "2d"), 2: ("Ks", "Kh"),
    3: ("8c", "6d"), 4: ("9h", "5s"), 5: ("Qd", "Jc"),
}
BOARD_N = ["3h", "7d", "9c", "Jh", "4s"]
SCRIPT_N = {
    (1, "PREFLOP", 3): [Action(3, ActionType.FOLD)],
    (1, "PREFLOP", 4): [Action(4, ActionType.FOLD)],
    (1, "PREFLOP", 5): [Action(5, ActionType.FOLD)],
    (1, "PREFLOP", 0): [Action(0, ActionType.ALLIN, 1000)],
    (1, "PREFLOP", 1): [Action(1, ActionType.FOLD)],
    (1, "PREFLOP", 2): [Action(2, ActionType.ALLIN, 1000)],
}


# ------------------------------------------------------------ 驱动助手


def _init() -> None:
    pygame.init()
    pygame.display.set_mode((1, 1))  # dummy 驱动下的迷你显示,启用 convert


def _step(scene, seconds: float, dt: float = 1 / 60) -> None:
    for _ in range(int(seconds / dt)):
        scene.update(dt)


def _step_until(scene, pred, max_seconds: float = 30.0, dt: float = 1 / 60) -> bool:
    """按固定步长推进场景直到谓词为真。"""
    for _ in range(int(max_seconds / dt)):
        if pred():
            return True
        scene.update(dt)
    return pred()


def _render(scene, path: Path) -> None:
    surf = pygame.Surface(SIZE)
    scene.draw(surf)
    pygame.image.save(surf, str(path))


# ------------------------------------------------------------ 四个画面


def shot_menu(path: Path) -> None:
    scene = MenuScene(seed=SEED)
    _step(scene, 3.0)  # 让烟雾聚起来
    _render(scene, path)


def shot_game_setup(path: Path) -> None:
    """九人桌设置：人数、逐席买入和 UTG straddle 提示同屏。"""
    scene = GameSetupScene(
        seed=SEED,
        initial_buyins_bb=(100, 80, 150, 50, 200, 120, 75, 125, 180),
        player_count=9,
    )
    _step(scene, 2.0)
    _render(scene, path)


def shot_preflop(path: Path) -> None:
    scene = TableScene(seed=SEED, headless=True)
    assert _step_until(scene, lambda: scene.phase == "action", 10.0)
    _step(scene, 0.3)
    _render(scene, path)


def shot_flop_raise(path: Path) -> None:
    scene = TableScene(
        seed=SEED,
        headless=True,
        auto_human=True,  # 翻牌前由脚本代打,翻牌后停下等输入
        action_script={k: list(v) for k, v in SCRIPT_C.items()},
        hole_script=dict(HOLE_C),
        board_script=list(BOARD_C),
    )
    from engine.state import Street

    ok = _step_until(
        scene,
        lambda: scene.snap is not None and scene.snap.street is Street.FLOP,
        30.0,
    )
    assert ok, "未能推进到翻牌圈"
    scene.auto_human = False  # 冻结在人类决策点
    ok = _step_until(scene, lambda: scene._human_to_act(), 10.0)
    assert ok, "未能等到人类行动"
    scene._human_raise()  # 打开加注滑杆
    _step(scene, 0.1)
    _render(scene, path)


def shot_showdown(path: Path) -> None:
    scene = TableScene(
        seed=SEED,
        headless=True,
        auto_human=True,
        action_script={k: list(v) for k, v in SCRIPT_D.items()},
        hole_script=dict(HOLE_D),
        board_script=list(BOARD_D),
    )
    ok = _step_until(scene, lambda: scene.phase == "finish", 40.0)
    assert ok, "未能推进到摊牌结算"
    _step_until(scene, lambda: not scene.chipfly.busy, 5.0)
    _step(scene, 0.4)  # 横幅淡入
    _render(scene, path)


def shot_gto_panel(path: Path) -> None:
    """翻前 RFI 建议，并冻结在末位弃牌滑向 muck 的中段。"""
    scene = TableScene(
        seed=SEED,
        headless=True,
        auto_human=False,
        action_script={k: list(v) for k, v in SCRIPT_E.items()},
        hole_script=dict(HOLE_C),
    )
    ok = _step_until(scene, lambda: scene._human_to_act(), 30.0)
    assert ok, "未能等到人类行动"
    _step(scene, 0.2)
    _render(scene, path)


def shot_nfsp_hu(path: Path) -> None:
    """冻结在真实 NFSP 完成一次实验性 HU 池动作之后。"""
    from engine.state import Street

    scene = TableScene(
        seed=SEED,
        headless=True,
        auto_human=True,
        action_script={k: list(v) for k, v in SCRIPT_O.items()},
        hole_script=dict(HOLE_C),
        board_script=list(BOARD_C),
    )
    ok = _step_until(
        scene,
        lambda: scene.snap is not None and scene.snap.street is Street.FLOP,
        30.0,
    )
    assert ok, "未能推进到实验性 HU 翻牌圈"
    scene.auto_human = False
    ok = _step_until(scene, lambda: scene._human_to_act(), 10.0)
    assert ok, "NFSP 行动后未能等到人类决策"
    assert scene._last_ai_source == "net:nfsp", "截图未实际命中 NFSP"
    _step(scene, 0.1)
    _render(scene, path)


def shot_charts_viewer(path: Path) -> None:
    """M4:翻前图表查看器(悬停 AKs 展示混合策略)。"""
    from ui.scenes.charts_viewer import ChartsViewerScene

    scene = ChartsViewerScene(seed=SEED)
    scene._hover = (0, 1)  # AKs
    _render(scene, path)


# ------------------------------------------------------------ M5:训练场


def _training_scene():
    from ui.scenes.training import TrainingScene

    scene = TrainingScene(seed=SEED)
    scene._apply_template(scene.templates[0])  # BTN vs BB 单加底池
    scene.hero_picker.set_cards(["As", "Kd"])
    scene.hero_cards = ["As", "Kd"]
    scene.board_picker.set_cards(["Qs", "Jh", "2h"])
    scene.board = ["Qs", "Jh", "2h"]
    return scene


def shot_training_editor(path: Path) -> None:
    """M5 (a):场景编辑器套用模板(villain 范围矩阵 + 公共牌 + 参数)。"""
    scene = _training_scene()
    scene.edit_tab = "villain_range"
    scene.update(1 / 60)
    _render(scene, path)


def shot_training_quick(path: Path) -> None:
    """M5 (b):快速分析(hero AKo vs BB 跟注范围胜率 + 赔率点评)。"""
    scene = _training_scene()
    scene.tab = "quick"
    scene.update(1 / 60)
    assert scene._quick is not None, "快速分析未产出"
    _render(scene, path)


def shot_training_gto(path: Path) -> None:
    """M5 (c):GTO 求解分析视图(夹具解,确定性;检视 AA 格)。"""
    scene = _training_scene()
    scene._load_fixture()
    scene._inspect = (0, 0)  # AA
    scene.update(1 / 60)
    assert scene.result is not None, "示例解未载入"
    _render(scene, path)


# ------------------------------------------------------------ M7:训练/回顾/统计


def _fixture_history(anchor: Path) -> str:
    """生成带"你"的确定性历史夹具(供回顾/统计截图),返回文件路径。"""
    import json as _json
    import random as _random

    from engine.game import Table, TableConfig
    from engine.history import HandHistoryWriter
    from engine.simulate import random_action

    def normalize_timestamps(history_path: Path) -> None:
        """固定截图夹具时间，避免源码/冻结版因生成时刻不同而像素漂移。"""
        rows: list[str] = []
        hand_index = 0
        for line in history_path.read_text(encoding="utf-8").splitlines():
            record = _json.loads(line)
            if "hand_id" in record:
                record["timestamp"] = (
                    f"2026-01-01T00:{hand_index:02d}:00.000000+00:00"
                )
                hand_index += 1
            rows.append(_json.dumps(record, ensure_ascii=False))
        history_path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    out = anchor.parent / "_fixture"
    out.mkdir(parents=True, exist_ok=True)
    path = out / "history.jsonl"
    if path.exists():
        normalize_timestamps(path)
        return str(path)
    cfg = TableConfig(
        player_count=6,
        starting_stack=400,
        small_blind=5,
        big_blind=10,
        player_names=("你", "公牛 Toar", "狐狸 Foxy", "犀牛 Gerk", "屠夫猪 Bristle", "看门狗 Scubby"),
    )
    rng = _random.Random(SEED)
    writer = HandHistoryWriter(path)
    table = None
    done = 0
    while done < 60:
        if table is None or table.game_over:
            table = Table(cfg, seed=SEED * 100 + done, history_writer=writer)
        table.start_hand()
        while not table.hand_over:
            table.apply(random_action(rng, table.snapshot()))
        done += 1
    normalize_timestamps(path)
    return str(path)


def shot_drills_question(path: Path) -> None:
    """M7 (a):drills 题目页(翻前 RFI 题)。"""
    from training.drills import CATEGORY_RFI
    from ui.scenes.drills import DrillsScene

    scene = DrillsScene(
        seed=SEED, profile_path=str(path.parent / "_fixture" / "profile.json")
    )
    scene._start(CATEGORY_RFI)
    assert scene.drill is not None
    _render(scene, path)


def shot_drills_feedback(path: Path) -> None:
    """M7 (b):drills 反馈页(作答后频率条 + 解析)。"""
    from training.drills import CATEGORY_RFI
    from ui.scenes.drills import DrillsScene

    scene = DrillsScene(
        seed=SEED, profile_path=str(path.parent / "_fixture" / "profile.json")
    )
    scene._start(CATEGORY_RFI)
    assert scene.drill is not None
    scene._answer_action(scene.drill.options[-1])  # 选"加注",制造反馈画面
    assert scene.feedback is not None
    _render(scene, path)


def _review_scene_loaded(path: Path):
    from ui.scenes.review import ReviewScene

    scene = ReviewScene(seed=SEED, history_path=_fixture_history(path))
    # 页面右上角会展示文件路径；截图夹具只显示稳定的逻辑路径，
    # 避免源码与冻结版因各自临时目录不同而产生无意义像素差异。
    scene._history_path = "_fixture/history.jsonl"
    # 挑一手有公共牌且 hero 有翻后动作的记录,光标停在 hero 动作上
    for row, (_, review) in enumerate(scene.rows):
        anns = review.annotations()
        hero_post = [a for a in anns if a.is_hero and a.street != "PREFLOP"]
        if len(review.board) >= 3 and hero_post:
            scene._select(row)
            scene.cursor = hero_post[0].index
            return scene
    return scene


def shot_review(path: Path) -> None:
    """M7 (c):手牌回顾(载入一手,光标停在 hero 翻后动作)。"""
    scene = _review_scene_loaded(path)
    assert scene.current is not None
    _render(scene, path)


def shot_stats(path: Path) -> None:
    """M7 (d):数据统计(夹具历史的聚合 + 走势图)。"""
    from ui.scenes.stats import StatsScene

    scene = StatsScene(seed=SEED, history_path=_fixture_history(path))
    assert scene.stats.hands > 0
    _render(scene, path)


def shot_bust_rebuy(path: Path) -> None:
    """M8 (n):狐狸 Foxy 全下出局,弹买入/移出处置框。"""
    scene = TableScene(
        seed=SEED,
        headless=True,
        auto_human=True,
        action_script={k: list(v) for k, v in SCRIPT_N.items()},
        hole_script=dict(HOLE_N),
        board_script=list(BOARD_N),
    )
    ok = _step_until(scene, lambda: scene._bust_dialog is not None, 40.0)
    assert ok, "未能等到出局处置框"
    _step_until(scene, lambda: not scene.chipfly.busy, 5.0)
    _step(scene, 0.3)
    _render(scene, path)


def shot_nine_seat_straddle(path: Path) -> None:
    """九名牌手完整入座，冻结在带 2BB STR 的翻前行动点。"""
    scene = TableScene(
        seed=SEED,
        headless=True,
        auto_human=False,
        buyins_bb=(100,) * 9,
    )
    ok = _step_until(scene, lambda: scene.phase == "action", 12.0)
    assert ok, "九人桌未能推进到翻前行动"
    _render(scene, path)


def shot_empty_seat_join(path: Path) -> None:
    """狐狸被移出后点击空位，展示身份/打法/买入和好友预留入口。"""
    scene = TableScene(
        seed=SEED,
        headless=True,
        auto_human=True,
        action_script={k: list(v) for k, v in SCRIPT_N.items()},
        hole_script=dict(HOLE_N),
        board_script=list(BOARD_N),
    )
    ok = _step_until(scene, lambda: scene._bust_dialog is not None, 40.0)
    assert ok and scene._bust_dialog is not None, "未能等到出局处置框"
    scene._resolve_bust_alt(scene._bust_dialog)
    scene.handle_event(
        pygame.event.Event(
            pygame.MOUSEBUTTONUP,
            button=1,
            pos=scene._empty_seat_rect(2).center,
        )
    )
    assert scene._seat_dialog is not None, "点击空位未打开召回面板"
    scene._seat_dialog._select_persona("raven")
    scene._seat_dialog.style_picker.select("MIX")
    _render(scene, path)


# ------------------------------------------------------------ 入口


def render_all(out_dir: str | os.PathLike) -> list[Path]:
    """渲染全部十八张截图,返回路径列表。"""
    _init()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    jobs = (
        ("a_menu.png", shot_menu),
        ("a2_game_setup.png", shot_game_setup),
        ("b_preflop.png", shot_preflop),
        ("c_flop_raise.png", shot_flop_raise),
        ("d_showdown.png", shot_showdown),
        ("e_gto_panel.png", shot_gto_panel),
        ("o_nfsp_hu.png", shot_nfsp_hu),
        ("f_charts_viewer.png", shot_charts_viewer),
        ("g_training_editor.png", shot_training_editor),
        ("h_training_quick.png", shot_training_quick),
        ("i_training_gto.png", shot_training_gto),
        ("j_drills_question.png", shot_drills_question),
        ("k_drills_feedback.png", shot_drills_feedback),
        ("l_review.png", shot_review),
        ("m_stats.png", shot_stats),
        ("n_bust_rebuy.png", shot_bust_rebuy),
        ("p_nine_seat_straddle.png", shot_nine_seat_straddle),
        ("q_empty_seat_join.png", shot_empty_seat_join),
    )
    paths: list[Path] = []
    for name, fn in jobs:
        path = out / name
        fn(path)
        paths.append(path)
    return paths


def main() -> int:
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "out_shots"
    for p in render_all(out_dir):
        print(f"已生成 {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
