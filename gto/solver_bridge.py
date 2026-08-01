"""TexasSolver 控制台求解器桥(纯逻辑,不依赖 pygame)。

负责三件事:

1. ``SolveConfig`` → DSL 配置文本(行序与 ``gto/sample_config.txt`` 一致);
2. ``RangeStr``:TexasSolver 范围字符串(``"AA,KK,99:0.75,AKs"``)与
   ``{规范牌型: 权重}`` 字典(169 键)互转;
3. ``SolverRunner``:子进程包装 —— 把配置写入 ``gto/work/``,以求解器
   自身目录为工作目录启动 ``console_solver.exe -i cfg``(该二进制依赖
   相对路径资源,**必须**以其所在目录为 cwd,否则迭代时崩溃),流式解析
    stdout 进度(``Iter: N`` / ``Total exploitability`` / ``time used``),
   经 ``queue.Queue`` 向 UI 抛事件,支持 ``cancel()`` 与超时。

输出 JSON 结构(v0.2.0 实测):

- 行动节点:``{actions, childrens, node_type:"action_node", player:0|1,
  strategy:{actions:[...], strategy:{combo: [freq, ...]}}}``,
  combo 键为 4 字符(如 ``"AcKd"``,rank 大写 suit 小写);
- 机会节点:``{node_type:"chance_node", deal_number, dealcards?}``;
  ``dump_rounds`` 覆盖时 ``dealcards = {发出的牌: 下一街行动节点}``,
  否则只有 ``deal_number: 0``;
- 终局节点不单独出现(行动节点无 ``childrens`` 即为叶)。

player 约定:0 = IP(后手/BTN),1 = OOP(先手/BB)。
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .charts import canonical_hands, hand_key
from ui.respath import res_path, user_data_path

# ------------------------------------------------------------ 路径

DEFAULT_SOLVER_EXE = res_path(
    "third_party", "texassolver", "TexasSolver-v0.2.0-Windows", "console_solver.exe"
)
# 求解器工作目录为可写 scratch(打包后落到 exe 同级,开发时为 gto/work)
DEFAULT_WORK_DIR = user_data_path("gto", "work")

STREETS = ("flop", "turn", "river")
SIDES = ("oop", "ip")

_CANONICAL_SET = frozenset(canonical_hands())

# 进度日志正则(实测输出格式)
_RE_ITER = re.compile(r"^Iter:\s*(\d+)")
_RE_EXPLOIT = re.compile(r"Total exploitability\s+([\d.eE+-]+)")
_RE_TIME = re.compile(r"time used:\s*([\d.eE+-]+)")
_RE_BAD_CMD = re.compile(r"command not (?:valid|recognized)")


class SolverError(RuntimeError):
    """求解器调用失败(缺 exe / 非法命令 / 非零退出 / 缺输出 JSON)。"""


# ------------------------------------------------------------ 范围字符串


class RangeStr:
    """TexasSolver 范围字符串 ↔ ``{规范牌型: 权重}``(169 键,缺省 0)。"""

    @staticmethod
    def parse(text: str) -> dict[str, float]:
        """解析 ``"AA,KK,99:0.75,AKs"`` 为 169 键权重字典。

        未列出的牌型权重为 0;``:w`` 省略时权重为 1。
        空段、非法牌型、越界权重均抛 ``ValueError``。
        """
        out = {h: 0.0 for h in _CANONICAL_SET}
        text = (text or "").strip()
        if not text:
            return out
        for seg in text.split(","):
            seg = seg.strip()
            if not seg:
                raise ValueError(f"范围串含空段: {text!r}")
            hand, _, w = seg.partition(":")
            hand = hand.strip()
            if hand not in _CANONICAL_SET:
                raise ValueError(f"非法牌型键: {hand!r}(须形如 AA/AKs/AKo)")
            if w:
                try:
                    weight = float(w)
                except ValueError:
                    raise ValueError(f"非法权重: {seg!r}") from None
            else:
                weight = 1.0
            if not 0.0 <= weight <= 1.0:
                raise ValueError(f"权重须在 [0,1]: {seg!r}")
            out[hand] = weight
        return out

    @staticmethod
    def dump(weights: dict[str, float]) -> str:
        """权重字典 → 范围串(权重 1 省略 ``:1``;0 权重牌型不写出)。"""
        parts: list[str] = []
        for h in canonical_hands():
            w = float(weights.get(h, 0.0))
            if w <= 0.0:
                continue
            if not 0.0 <= w <= 1.0:
                raise ValueError(f"权重须在 [0,1]: {h}={w}")
            parts.append(h if w == 1.0 else f"{h}:{w:g}")
        return ",".join(parts)

    @staticmethod
    def zero() -> dict[str, float]:
        """全 0 的 169 键字典。"""
        return {h: 0.0 for h in _CANONICAL_SET}

    @staticmethod
    def combo_count(weights: dict[str, float]) -> float:
        """加权组合数(对子 6 / 同花 4 / 杂花 12,按权重求和)。"""
        total = 0.0
        for h, w in weights.items():
            if w <= 0:
                continue
            n = 6 if len(h) == 2 else 4 if h[2] == "s" else 12
            total += n * w
        return total


# ------------------------------------------------------------ 求解配置


def _g(x: float) -> str:
    """数值 → DSL 文本(50.0 → "50",0.67 → "0.67")。"""
    return f"{x:g}"


@dataclass
class BetSizes:
    """某方某街的下注尺度(底池百分比)+ 是否允许全下。

    ``raises`` 对应 DSL 的 ``raise``(避开 Python 关键字)。
    """

    bet: list[float] = field(default_factory=lambda: [50.0])
    raises: list[float] = field(default_factory=lambda: [60.0])
    donk: list[float] = field(default_factory=list)
    allin: bool = True

    def lines(self, side: str, street: str) -> list[str]:
        """该格的全部 ``set_bet_sizes`` 行(bet → raise → donk → allin)。"""
        out: list[str] = []
        for kind, vals in (("bet", self.bet), ("raise", self.raises), ("donk", self.donk)):
            for v in vals:
                out.append(f"set_bet_sizes {side},{street},{kind},{_g(v)}")
        if self.allin:
            out.append(f"set_bet_sizes {side},{street},allin")
        return out


def _default_bet_sizes() -> dict[str, dict[str, BetSizes]]:
    return {side: {street: BetSizes() for street in STREETS} for side in SIDES}


@dataclass
class SolveConfig:
    """一次求解的全部参数。

    :param board: 3–5 张公共牌(如 ``["Qs","Jh","2h"]``)。
    :param range_ip / range_oop: TexasSolver 范围串。
    :param bet_sizes: ``{side: {street: BetSizes}}``,side ∈ oop/ip。
    :param accuracy: 目标可利用度(底池百分比)。
    """

    pot: float = 50.0
    effective_stack: float = 200.0
    board: list[str] = field(default_factory=lambda: ["Qs", "Jh", "2h"])
    range_ip: str = "AA,KK,QQ,JJ,TT,99,AKs,AKo,AQs"
    range_oop: str = "AA,KK,QQ,JJ,TT,99,AKs,AKo,AQs"
    bet_sizes: dict[str, dict[str, BetSizes]] = field(default_factory=_default_bet_sizes)
    allin_threshold: float = 0.67
    threads: int = field(default_factory=lambda: min(8, os.cpu_count() or 4))
    accuracy: float = 1.0
    max_iteration: int = 200
    print_interval: int = 10
    use_isomorphism: bool = True
    dump_rounds: int = 2

    def validate(self) -> None:
        """基本合法性检查(求解前调用,失败抛 ``ValueError``)。"""
        if self.pot <= 0:
            raise ValueError("pot 须为正数")
        if self.effective_stack <= 0:
            raise ValueError("effective_stack 须为正数")
        if not 3 <= len(self.board) <= 5:
            raise ValueError("board 须为 3–5 张牌")
        if len(set(self.board)) != len(self.board):
            raise ValueError("board 存在重复牌")
        for name, rng in (("range_ip", self.range_ip), ("range_oop", self.range_oop)):
            weights = RangeStr.parse(rng)  # 非法串在此抛出
            if RangeStr.combo_count(weights) <= 0:
                raise ValueError(f"{name} 为空范围")
        if self.max_iteration <= 0:
            raise ValueError("max_iteration 须为正数")
        if self.threads <= 0:
            raise ValueError("threads 须为正数")
        if not 0 <= self.dump_rounds <= 3:
            raise ValueError("dump_rounds 须在 0–3")


def build_config_text(cfg: SolveConfig) -> str:
    """生成 DSL 配置文本(行序与 ``gto/sample_config.txt`` 一致)。

    顺序:底池/筹码/公共牌/范围 → 各街下注尺度(街 → oop → ip,
    bet → raise → donk → allin)→ allin 阈值 → build_tree →
    线程/精度/迭代/打印间隔/同构 → start_solve → dump 轮数 → dump_result。
    """
    lines = [
        f"set_pot {_g(cfg.pot)}",
        f"set_effective_stack {_g(cfg.effective_stack)}",
        f"set_board {','.join(cfg.board)}",
        f"set_range_ip {cfg.range_ip}",
        f"set_range_oop {cfg.range_oop}",
    ]
    for street in STREETS:
        for side in SIDES:
            lines.extend(cfg.bet_sizes[side][street].lines(side, street))
    lines.append(f"set_allin_threshold {_g(cfg.allin_threshold)}")
    lines.append("build_tree")
    lines.append(f"set_thread_num {cfg.threads}")
    lines.append(f"set_accuracy {_g(cfg.accuracy)}")
    lines.append(f"set_max_iteration {cfg.max_iteration}")
    lines.append(f"set_print_interval {cfg.print_interval}")
    lines.append(f"set_use_isomorphism {1 if cfg.use_isomorphism else 0}")
    lines.append("start_solve")
    lines.append(f"set_dump_rounds {cfg.dump_rounds}")
    lines.append("dump_result output_result.json")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------ 结果解析


@dataclass
class SolveNode:
    """动作树节点。

    :param actions: 本节点可选动作(``"CHECK"`` / ``"BET 25.000000"`` …)。
    :param children: 动作 → 子节点;机会节点的 ``dealcards`` 也并入此字典
        (键形如 ``"DEAL:Qh"``,值为其下街行动节点)。
    :param player: 行动方(0=IP,1=OOP);机会节点为 ``None``。
    :param strategy: 原始组合策略 ``{combo: [freq, ...]}``(对齐 actions)。
    """

    node_type: str
    player: int | None = None
    actions: list[str] = field(default_factory=list)
    children: dict[str, "SolveNode"] = field(default_factory=dict)
    strategy: dict[str, list[float]] = field(default_factory=dict)
    deal_number: int = 0

    @property
    def is_chance(self) -> bool:
        return self.node_type == "chance_node"

    def strategy_matrix_169(self) -> dict[str, dict[str, float]]:
        """组合级策略聚合为 169 规范牌型:``{hand: {action: freq}}``。

        频率 = 该牌型全部组合频率的算术平均(每组合等权)。
        不在范围/被公共牌阻挡的牌型保持全 0。
        """
        out: dict[str, dict[str, float]] = {
            h: {a: 0.0 for a in self.actions} for h in _CANONICAL_SET
        }
        sums: dict[str, list[float]] = {}
        counts: dict[str, int] = {}
        for combo, freqs in self.strategy.items():
            if len(combo) != 4:
                continue
            try:
                hand = hand_key([combo[:2], combo[2:]])
            except ValueError:
                continue
            acc = sums.setdefault(hand, [0.0] * len(self.actions))
            for i, f in enumerate(freqs[: len(self.actions)]):
                acc[i] += f
            counts[hand] = counts.get(hand, 0) + 1
        for hand, acc in sums.items():
            n = counts[hand]
            out[hand] = {a: acc[i] / n for i, a in enumerate(self.actions)}
        return out

    def action_evs(self, combo: str) -> list[float] | None:
        """某组合的逐动作 EV(v0.2.0 dump 不含 EV,恒为 ``None``;保留接口)。"""
        return None


@dataclass
class SolveResult:
    """一次求解的产物:根节点 + 收尾日志指标。"""

    root: SolveNode
    exploitability: float | None = None
    iterations: int | None = None
    time_used: float | None = None
    log_tail: list[str] = field(default_factory=list)


def _parse_node(raw: dict) -> SolveNode:
    node_type = raw.get("node_type", "action_node")
    if node_type == "chance_node":
        children: dict[str, SolveNode] = {}
        for card, sub in (raw.get("dealcards") or {}).items():
            children[f"DEAL:{card}"] = _parse_node(sub)
        return SolveNode(
            node_type="chance_node",
            children=children,
            deal_number=int(raw.get("deal_number", 0)),
        )
    strat_raw = raw.get("strategy") or {}
    actions = list(raw.get("actions") or strat_raw.get("actions") or [])
    children = {a: _parse_node(c) for a, c in (raw.get("childrens") or {}).items()}
    return SolveNode(
        node_type="action_node",
        player=raw.get("player"),
        actions=actions,
        children=children,
        strategy={k: list(v) for k, v in (strat_raw.get("strategy") or {}).items()},
    )


def parse_result(path: str | Path) -> SolveResult:
    """读取 ``output_result.json`` 为 ``SolveResult``(不含日志指标)。"""
    path = Path(path)
    if not path.is_file():
        raise SolverError(f"求解输出不存在: {path}")
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        raise SolverError(f"求解输出不是合法 JSON: {path}({e})") from None
    return SolveResult(root=_parse_node(raw))


# ------------------------------------------------------------ 子进程运行器


@dataclass
class ProgressEvent:
    """求解进度事件(UI 经队列消费)。

    kind: ``"log"`` 原始行 / ``"progress"`` 迭代进度 / ``"done"`` 成功 /
    ``"error"`` 失败。
    """

    kind: str
    line: str = ""
    iteration: int = 0
    exploitability: float | None = None
    time_used: float | None = None
    result: SolveResult | None = None
    error: str = ""


class SolverRunner:
    """``console_solver.exe`` 子进程包装(后台线程 + 事件队列)。

    用法::

        runner = SolverRunner()
        runner.start(cfg)            # 非阻塞
        ev = runner.events.get()     # UI 轮询
        runner.cancel()              # 需要时终止

    求解器依赖其目录下的相对资源,故以其所在目录为 cwd;配置文件写入
    ``work_dir`` 并以绝对路径传入;产物 ``output_result.json`` 先落在求解器
    目录,成功后移动到 ``work_dir/result_<时间戳>.json``。
    """

    def __init__(
        self,
        solver_exe: str | Path | None = None,
        work_dir: str | Path | None = None,
        timeout: float = 600.0,
    ) -> None:
        self.solver_exe = Path(solver_exe) if solver_exe else DEFAULT_SOLVER_EXE
        self.work_dir = Path(work_dir) if work_dir else DEFAULT_WORK_DIR
        self.timeout = timeout
        self.events: queue.Queue[ProgressEvent] = queue.Queue()
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._cancelled = False

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------ 控制

    def start(self, cfg: SolveConfig) -> None:
        """后台启动一次求解;已有任务在跑时抛 ``SolverError``。"""
        if self.running:
            raise SolverError("已有求解任务在运行")
        if not self.solver_exe.is_file():
            raise SolverError(f"找不到求解器: {self.solver_exe}(见 third_party/texassolver/SOURCE.md)")
        cfg.validate()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        cfg_path = self.work_dir / f"cfg_{stamp}.txt"
        cfg_path.write_text(build_config_text(cfg), encoding="utf-8")
        self._cancelled = False
        self._thread = threading.Thread(
            target=self._run, args=(cfg_path, stamp), daemon=True, name="solver-runner"
        )
        self._thread.start()

    def cancel(self) -> None:
        """终止正在运行的求解进程(无任务时静默忽略)。"""
        self._cancelled = True
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    def run_blocking(self, cfg: SolveConfig) -> SolveResult:
        """同步求解(测试/脚本用);失败抛 ``SolverError``。"""
        self.start(cfg)
        while True:
            ev = self.events.get()
            if ev.kind == "done":
                assert ev.result is not None
                return ev.result
            if ev.kind == "error":
                raise SolverError(ev.error)

    # ------------------------------------------------------------ 后台

    def _run(self, cfg_path: Path, stamp: str) -> None:
        exe_dir = self.solver_exe.parent
        out_name = "output_result.json"
        out_path = exe_dir / out_name
        log_tail: list[str] = []
        iterations = 0
        exploitability: float | None = None
        time_used: float | None = None
        try:
            # 清理上一次的产物,避免把旧结果当新结果
            try:
                out_path.unlink(missing_ok=True)
            except OSError:
                pass
            self._proc = subprocess.Popen(
                [str(self.solver_exe), "-i", str(cfg_path)],
                cwd=str(exe_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            deadline = time.monotonic() + self.timeout
            assert self._proc.stdout is not None
            for line in self._proc.stdout:
                line = line.rstrip("\n")
                if not line.strip():
                    continue
                log_tail.append(line)
                log_tail = log_tail[-40:]
                m = _RE_ITER.match(line.strip())
                if m:
                    iterations = int(m.group(1))
                m = _RE_EXPLOIT.search(line)
                if m:
                    exploitability = float(m.group(1))
                    self.events.put(
                        ProgressEvent(
                            kind="progress",
                            line=line,
                            iteration=iterations,
                            exploitability=exploitability,
                        )
                    )
                    continue
                m = _RE_TIME.search(line)
                if m:
                    time_used = float(m.group(1))
                self.events.put(ProgressEvent(kind="log", line=line))
                if _RE_BAD_CMD.search(line):
                    raise SolverError(f"求解器报告非法命令: {line.strip()}")
                if time.monotonic() > deadline:
                    self.cancel()
                    raise SolverError(f"求解超时(>{self.timeout:.0f}s),已终止")
            rc = self._proc.wait()
            if self._cancelled:
                raise SolverError("求解已取消")
            if rc != 0:
                tail = " / ".join(log_tail[-5:])
                raise SolverError(f"求解器退出码 {rc};日志尾部: {tail}")
            if not out_path.is_file():
                tail = " / ".join(log_tail[-5:])
                raise SolverError(f"求解器未写出 {out_name};日志尾部: {tail}")
            dest = self.work_dir / f"result_{stamp}.json"
            try:
                os.replace(out_path, dest)
            except OSError:
                dest = out_path  # 跨设备移动失败时原地读取
            result = parse_result(dest)
            result.exploitability = exploitability
            result.iterations = iterations
            result.time_used = time_used
            result.log_tail = log_tail
            self.events.put(
                ProgressEvent(
                    kind="done",
                    iteration=iterations,
                    exploitability=exploitability,
                    time_used=time_used,
                    result=result,
                )
            )
        except SolverError as e:
            self.events.put(ProgressEvent(kind="error", error=str(e)))
        except Exception as e:  # noqa: BLE001 —— 后台线程兜底,错误走队列
            self.events.put(ProgressEvent(kind="error", error=f"求解器异常: {e}"))
        finally:
            self._proc = None
