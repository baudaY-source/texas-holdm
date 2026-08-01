"""手牌历史记录(JSON Lines)。

仅负责序列化:``Table`` 在每手结束后把整理好的 dict 交给本类,
本类不含任何游戏逻辑。每行一个 JSON 对象,便于流式读取与训练取样。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_HISTORY_PATH = Path("hands") / "history.jsonl"


class HandHistoryWriter:
    """把每手牌记录追加写入 JSONL 文件。"""

    def __init__(self, path: str | Path = DEFAULT_HISTORY_PATH) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        """历史文件路径。"""
        return self._path

    def write_hand(self, record: dict[str, Any]) -> None:
        """追加一手牌记录。

        :param record: 可 JSON 序列化的字典,键由 ``Table`` 约定:
            hand_id / timestamp / config / seats / button_seat /
            actions / board / pots / winners / end_stacks。
        """
        self._append(record)

    def write_event(self, event: dict[str, Any]) -> None:
        """追加一条牌局事件(如 rebuy/remove/seat_join/show)。

        事件行带 ``"type"`` 键,与手牌记录区分;回顾/统计端据此跳过。
        """
        self._append(event)

    def _append(self, record: dict[str, Any]) -> None:
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False))
            fh.write("\n")
