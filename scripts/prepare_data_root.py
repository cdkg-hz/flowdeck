from __future__ import annotations

import os
from pathlib import Path
import sys

POSTGRES_FS_UID = 999
POSTGRES_FS_GID = 999
ROOT_MISSING = "项目存储根目录不存在；请先创建或挂载该目录"
ROOT_UNSAFE = "项目存储根目录不能是符号链接或目录联接"
CHILD_UNSAFE = "数据子目录必须是普通目录，不能是符号链接"


class PrepareFailed(Exception):
    pass


def _linklike(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _on_unix() -> bool:
    return os.name != "nt"


def _owner_uid(path: Path) -> int:
    return path.stat().st_uid


def _chmod(path: Path, mode: int) -> None:
    os.chmod(path, mode)


def _chown(path: Path, uid: int, gid: int) -> None:
    os.chown(path, uid, gid)


def _chown_tree(path: Path, uid: int, gid: int) -> None:
    for current, _dirnames, filenames in os.walk(path, followlinks=False):
        _chown(Path(current), uid, gid)
        for name in filenames:
            child = Path(current) / name
            if not child.is_symlink():
                _chown(child, uid, gid)


def prepare_data_root(root: Path, *, workflow_uid: int = 1000, workflow_gid: int = 1000) -> None:
    if _linklike(root):
        raise PrepareFailed(ROOT_UNSAFE)
    if not root.exists() or not root.is_dir():
        raise PrepareFailed(ROOT_MISSING)
    postgres = root / "postgres"
    projects = root / "projects"
    for child in (postgres, projects):
        if child.exists() or child.is_symlink():
            if _linklike(child) or not child.is_dir():
                raise PrepareFailed(CHILD_UNSAFE)
        else:
            child.mkdir()
    if not _on_unix():
        return
    _chmod(root, 0o755)
    if _owner_uid(postgres) != POSTGRES_FS_UID:
        _chown_tree(postgres, POSTGRES_FS_UID, POSTGRES_FS_GID)
    else:
        _chown(postgres, POSTGRES_FS_UID, POSTGRES_FS_GID)
    _chmod(postgres, 0o700)
    _chown(projects, workflow_uid, workflow_gid)


def main() -> int:
    raw = (os.environ.get("HOST_DATA_ROOT") or "").strip()
    root = Path(raw) if raw else Path()
    if not raw or not root.is_absolute():
        print("HOST_DATA_ROOT 必须是绝对路径", file=sys.stderr)
        return 1
    try:
        uid = int(os.environ.get("WORKFLOW_UID") or "1000")
        gid = int(os.environ.get("WORKFLOW_GID") or "1000")
    except ValueError:
        print("WORKFLOW_UID 和 WORKFLOW_GID 必须是整数", file=sys.stderr)
        return 1
    try:
        prepare_data_root(root, workflow_uid=uid, workflow_gid=gid)
    except PrepareFailed as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
