"""《酒馆德州》入口。

默认进入主菜单(pygame 窗口);``--headless-screenshot <dir>`` 则
调用 ``tools/shots.py`` 的无头截图管线(供 CI/视觉走查)。
"""
from __future__ import annotations

import sys

MIN_PYTHON = (3, 11)


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

    import pygame  # noqa: F401  确认 pygame 可用后再进场景

    from ui.scenes.manager import SceneManager
    from ui.scenes.menu import MenuScene

    manager = SceneManager((1600, 900))
    manager.push(MenuScene())
    return manager.run()


if __name__ == "__main__":
    sys.exit(main())
