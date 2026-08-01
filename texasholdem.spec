# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 构建配方:《酒馆德州》one-folder 打包。

用法(项目根目录,详见 tools/build_dist.py)::

    .venv/Scripts/python.exe -m PyInstaller texasholdem.spec --noconfirm --clean

- 数据:assets（含约 1.18MB 的 NFSP 轻量推理权重）/ gto 图表与策略库 /
  TexasSolver 二进制 / 训练场示例解夹具;
- 排除 torch / rlcard / nets / numpy 以及独立的 WebSocket 服务端：这些均不
  属于离线桌面 EXE;
  游戏改由 ``ai.nfsp_runtime`` 的纯标准库运行时读取轻量权重;
- 可写数据(手牌历史、训练档案、用户场景、求解器 scratch)由
  ``ui.respath.user_data_root()`` 落到 exe 同级目录,不进 _MEIPASS。
"""
import os
import fnmatch

block_cipher = None

# (源, 目标)数据对;源不存在则跳过(如未下载求解器)
_datas = [
    ("assets", "assets"),
    ("gto/charts", "gto/charts"),
    ("gto/strategies", "gto/strategies"),
    ("third_party/texassolver/SOURCE.md", "third_party/texassolver"),
    ("tests/fixtures", "tests/fixtures"),
]
datas = [(src, dst) for src, dst in _datas if os.path.exists(src)]

# TexasSolver release 目录本身是可执行工作目录；每次求解会在里面留下
# output_result/tmp_log 等本机牌局产物。不能把整个目录无筛选复制进稳定版。
_solver_root = os.path.join(
    "third_party", "texassolver", "TexasSolver-v0.2.0-Windows"
)
_solver_scratch = (
    "output_result*.json",
    "result_*.json",
    "cfg_*.txt",
    "tmp_log*.txt",
    "*.log",
)
if os.path.isdir(_solver_root):
    for current, dirs, files in os.walk(_solver_root):
        relative_dir = os.path.relpath(current, _solver_root)
        relative_posix = relative_dir.replace("\\", "/")
        if relative_posix == "resources/outputs" or relative_posix.startswith(
            "resources/outputs/"
        ):
            dirs[:] = []
            continue
        for filename in files:
            if filename == ".DS_Store" or any(
                fnmatch.fnmatch(filename, pattern) for pattern in _solver_scratch
            ):
                continue
            source = os.path.join(current, filename)
            destination = os.path.join(
                "third_party",
                "texassolver",
                "TexasSolver-v0.2.0-Windows",
                "" if relative_dir == "." else relative_dir,
            )
            datas.append((source, destination))

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "pokerkit",
        # phevaluator 用 PEP 562 __getattr__ 懒加载子模块,静态分析看不到
        "phevaluator.evaluator",
        "phevaluator.evaluator_omaha",
        "phevaluator.card",
        "phevaluator.utils",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "torch",
        "rlcard",
        "nets",
        "numpy",
        "matplotlib",
        "pandas",
        "scipy",
        "PIL",
        "pytest",
        "websockets",
        "multiplayer_server",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="酒馆德州",
    version="tools/windows_version_info.txt",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # 窗口应用;--headless-screenshot 走 SDL dummy 驱动,无需控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="酒馆德州",
)
