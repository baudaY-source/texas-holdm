"""打包脚本:用 texasholdem.spec 构建 dist/酒馆德州/ 并汇报体积与耗时。

用法(项目根目录)::

    .venv/Scripts/python.exe tools/build_dist.py [--skip-verify]

构建后默认做一次冒烟:``dist/酒馆德州/酒馆德州.exe --headless-screenshot
<临时目录>`` 应产出全部截图(验证打包后的资源路径解析)。
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime
import hashlib
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXE = ROOT / "dist" / "酒馆德州" / "酒馆德州.exe"
DIST = EXE.parent
LOCAL_BACKUPS = ROOT / "local_backups"
WRITABLE_DATA_DIRS = ("hands", "training", "gto")
RELEASE_DOCS = ("VERSION", "CHANGELOG.md", "THIRD_PARTY_NOTICES.md")


def _dir_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def build() -> float:
    """运行 PyInstaller,返回耗时(秒)。"""
    t0 = time.time()
    cmd = [
        sys.executable, "-m", "PyInstaller",
        str(ROOT / "texasholdem.spec"),
        "--noconfirm", "--clean",
    ]
    print("运行:", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)
    return time.time() - t0


@contextmanager
def preserve_user_data():
    """持久备份 EXE 同级可写数据，构建结束后恢复并逐文件校验。

    自动备份保留在 gitignore 的 ``local_backups/``。即使恢复因磁盘、权限
    或文件锁失败，备份也不会被临时目录清理器删除。
    """

    def hashes(path: Path) -> dict[str, str]:
        result: dict[str, str] = {}
        for file_path in sorted(path.rglob("*")):
            if not file_path.is_file():
                continue
            digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
            result[file_path.relative_to(path).as_posix()] = digest
        return result

    preserved = [
        name for name in WRITABLE_DATA_DIRS if (DIST / name).exists()
    ]
    backup_root: Path | None = None
    expected: dict[str, dict[str, str]] = {}
    if preserved:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup_root = LOCAL_BACKUPS / f"build-dist-{stamp}"
        backup_root.mkdir(parents=True)
        for name in preserved:
            shutil.copytree(DIST / name, backup_root / name)
            expected[name] = hashes(backup_root / name)
        print("已持久备份用户数据:", backup_root)

    try:
        yield
    finally:
        if backup_root is not None:
            failures: list[str] = []
            for name in preserved:
                destination = DIST / name
                try:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(
                        backup_root / name,
                        destination,
                        dirs_exist_ok=True,
                    )
                    if hashes(destination) != expected[name]:
                        failures.append(f"{name}:恢复后哈希不一致")
                except OSError as exc:
                    failures.append(f"{name}:{exc}")
            if failures:
                raise RuntimeError(
                    "用户数据恢复失败；永久备份仍保留在 "
                    f"{backup_root}：{' | '.join(failures)}"
                )
            print("已恢复并校验用户数据；安全备份保留在:", backup_root)


def copy_release_docs() -> None:
    """把版本、更新日志和第三方说明放到发行目录根部。"""
    DIST.mkdir(parents=True, exist_ok=True)
    for name in RELEASE_DOCS:
        shutil.copy2(ROOT / name, DIST / name)


def verify() -> int:
    """冒烟:exe 无头截图,返回产出 PNG 数。"""
    with tempfile.TemporaryDirectory(prefix="tavern_shots_") as tmp:
        result = subprocess.run(
            [str(EXE), "--headless-screenshot", tmp],
            cwd=tmp,  # 刻意换个 cwd:验证资源解析不依赖工作目录
            capture_output=True, timeout=600,
        )
        # 冻结 exe 的 stdout 用系统代码页(GBK);按控制台编码解码并容错
        enc = sys.stdout.encoding or "utf-8"
        out_tail = (result.stdout or b"")[-3000:].decode(enc, errors="replace")
        err_tail = (result.stderr or b"")[-3000:].decode(enc, errors="replace")
        if result.returncode != 0:
            print("冒烟失败,退出码", result.returncode, file=sys.stderr)
            print(out_tail, file=sys.stderr)
            print(err_tail, file=sys.stderr)
            raise SystemExit(1)
        pngs = sorted(Path(tmp).glob("*.png"))
        print(f"冒烟通过:{len(pngs)} 张截图 -> {tmp}(临时目录已清理)")
        for p in pngs:
            print("  ", p.name, p.stat().st_size, "bytes")
        return len(pngs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建 dist 发行包")
    parser.add_argument("--skip-verify", action="store_true", help="跳过 exe 冒烟")
    args = parser.parse_args(argv)

    with preserve_user_data():
        for d in ("build", "dist"):
            shutil.rmtree(ROOT / d, ignore_errors=True)
        elapsed = build()
        copy_release_docs()
        size = _dir_size(DIST)
        print(
            f"构建完成:{elapsed / 60:.1f} 分钟,"
            f"{size / 1024 / 1024:.0f} MB -> {DIST}"
        )
        if not EXE.is_file():
            print(f"找不到 {EXE}", file=sys.stderr)
            return 1
        if not args.skip_verify:
            verify()
    return 0


if __name__ == "__main__":
    sys.exit(main())
