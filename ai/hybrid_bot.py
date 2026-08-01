"""具名启发式机器人与实验性 HU NFSP 策略的安全路由。

本模块不负责加载 checkpoint。调用方应在全桌范围内创建、共享一个实现
``NFSPPolicy`` 协议的策略对象，再把同一对象传给各座位的
``HybridPersonaBot``。这样既不会重复占用模型内存，也能在不适用或失败时
完整保留原具名机器人的风格、等级和降级行为。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Protocol

from engine.state import Action, GameSnapshot

from .bots import Bot, HeuristicBot
from .nfsp_runtime import is_experimental_hu_postflop


class NFSPPolicy(Protocol):
    """混合路由所需的最小 NFSP 推理协议。"""

    meta: dict

    def sample_action(
        self,
        snapshot: GameSnapshot,
        hero_seat: int,
        rng: random.Random | None,
    ) -> Action:
        """按策略分布采样当前行动。"""


@dataclass
class HybridPersonaBot:
    """在适用局面使用 NFSP、其余时间保持原人设的机器人。

    ``policy`` 允许为 ``None``，且应由整张牌桌的所有实例共享。NFSP 只会
    在 ``is_experimental_hu_postflop`` 判定通过时被调用；翻前或仍有三名
    以上未弃牌玩家时绝不会触碰策略对象。

    ``last_source`` 稳定区分实际动作来源：

    - ``"net:nfsp"``：NFSP 返回了合法动作；
    - ``"heuristic:persona"``：原具名 ``HeuristicBot`` 决策或保守兜底。

    ``last_detail`` 记录路由原因，便于 UI 做状态提示和问题诊断。
    """

    heuristic: Bot
    policy: NFSPPolicy | None = None
    last_source: str = field(default="", init=False)
    last_detail: str = field(default="", init=False)

    def decide(
        self,
        snapshot: GameSnapshot,
        rng: random.Random | None = None,
    ) -> Action:
        """返回合法动作；NFSP 的任何故障都会降级到原具名机器人。

        和 ``Bot`` 协议一致，本方法要求传入当前行动者视角且
        ``acting_seat``、``legal_actions`` 均有效的快照。
        """
        self.last_source = ""
        self.last_detail = ""

        seat = snapshot.acting_seat
        legal = snapshot.legal_actions
        if seat is None or legal is None:
            raise ValueError("当前无人行动，HybridPersonaBot 无法决策")

        try:
            eligible = is_experimental_hu_postflop(snapshot)
        except Exception as exc:
            return self._decide_persona(
                snapshot,
                rng,
                f"eligibility_failed:{type(exc).__name__}",
            )

        if not eligible:
            return self._decide_persona(snapshot, rng, "not_eligible")

        if self.policy is None:
            return self._decide_persona(snapshot, rng, "policy_unavailable")

        try:
            action = self.policy.sample_action(snapshot, seat, rng)
        except Exception as exc:
            return self._decide_persona(
                snapshot,
                rng,
                f"policy_inference_failed:{type(exc).__name__}",
            )

        try:
            policy_action_is_legal = HeuristicBot._is_legal(action, legal)
        except Exception:
            policy_action_is_legal = False
        if policy_action_is_legal:
            self.last_source = "net:nfsp"
            self.last_detail = "policy_used"
            return action

        return self._decide_persona(snapshot, rng, "policy_illegal_action")

    def _decide_persona(
        self,
        snapshot: GameSnapshot,
        rng: random.Random | None,
        detail: str,
    ) -> Action:
        """调用原具名机器人；其异常或非法结果再走保守合法兜底。"""
        seat = snapshot.acting_seat
        legal = snapshot.legal_actions
        assert seat is not None and legal is not None

        self.last_source = "heuristic:persona"
        self.last_detail = detail
        try:
            action = self.heuristic.decide(snapshot, rng)
        except Exception as exc:
            action = HeuristicBot._fallback(seat, legal)
            self.last_detail += f";heuristic_failed:{type(exc).__name__}"
            return action

        try:
            persona_action_is_legal = HeuristicBot._is_legal(action, legal)
        except Exception:
            persona_action_is_legal = False
        if persona_action_is_legal:
            return action

        self.last_detail += ";heuristic_illegal_action"
        return HeuristicBot._fallback(seat, legal)
