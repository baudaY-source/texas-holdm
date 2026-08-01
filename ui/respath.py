"""资源路径解析:开发模式与 PyInstaller 打包(``sys._MEIPASS``)双兼容。

- 只读资源(assets、gto/charts、gto/strategies、第三方求解器)在打包后
  位于解压临时目录 ``sys._MEIPASS`` 下,开发时位于项目根目录;
- 可写数据(手牌历史、训练档案、用户场景)打包后应写到 exe 同级目录
  (``_MEIPASS`` 是只读临时目录),开发时同样落在项目根目录。

所有资源加载点统一走本模块,不要再各自拼 ``Path(__file__)``。
"""
from __future__ import annotations

import sys
from pathlib import Path


def resource_root() -> Path:
    """只读资源根目录(打包后为 ``sys._MEIPASS``)。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent.parent


def res_path(*parts: str) -> Path:
    """拼接只读资源路径,如 ``res_path("gto", "charts")``。"""
    return resource_root().joinpath(*parts)


def user_data_root() -> Path:
    """可写数据根目录(打包后为 exe 所在目录,开发时为项目根)。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def user_data_path(*parts: str) -> Path:
    """拼接可写数据路径(父目录不存在时创建)。"""
    path = user_data_root().joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
