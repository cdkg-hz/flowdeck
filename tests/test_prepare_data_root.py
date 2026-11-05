from pathlib import Path
import pytest
from scripts.prepare_data_root import (
    POSTGRES_FS_GID,
    POSTGRES_FS_UID,
    PrepareFailed,
    main,
    prepare_data_root,
)


def test_missing_root_fails_without_creating(tmp_path):
    root = tmp_path / "missing"
    with pytest.raises(PrepareFailed, match="根目录不存在"):
        prepare_data_root(root)
    assert not root.exists()


def test_creates_postgres_and_projects_and_keeps_existing_files(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    kept = root / "projects" / "keep.txt"
    kept.parent.mkdir()
    kept.write_text("stay", encoding="utf-8")
    prepare_data_root(root)
    assert (root / "postgres").is_dir()
    assert (root / "projects").is_dir()
    assert kept.read_text(encoding="utf-8") == "stay"
    assert not (root / "copilot-home").exists()
    assert not (root / "skills").exists()


def test_rejects_symlink_root_and_children(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    root = tmp_path / "link"
    try:
        root.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this host")
    with pytest.raises(PrepareFailed, match="符号链接"):
        prepare_data_root(root)

    data = tmp_path / "data"
    data.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = data / "postgres"
    linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(PrepareFailed, match="符号链接"):
        prepare_data_root(data)
    assert list(outside.iterdir()) == []


def test_unix_chowns_root_and_projects_and_recurses_postgres_only_when_needed(tmp_path, monkeypatch):
    from scripts import prepare_data_root as module

    root = tmp_path / "data"
    root.mkdir()
    postgres = root / "postgres"
    postgres.mkdir()
    nested = postgres / "base" / "file"
    nested.parent.mkdir()
    nested.write_text("pg", encoding="utf-8")
    (root / "projects").mkdir()
    chown_calls = []
    chmod_calls = []
    tree_calls = []

    monkeypatch.setattr(module, "_on_unix", lambda: True)
    monkeypatch.setattr(module, "_chown", lambda path, uid, gid: chown_calls.append((Path(path), uid, gid)))
    monkeypatch.setattr(module, "_chmod", lambda path, mode: chmod_calls.append((Path(path), mode)))
    monkeypatch.setattr(module, "_chown_tree", lambda path, uid, gid: tree_calls.append((Path(path), uid, gid)))

    monkeypatch.setattr(module, "_owner_uid", lambda path: 1000 if Path(path) == postgres else 0)
    prepare_data_root(root, workflow_uid=2000, workflow_gid=2001)
    assert (root, 0o755) in chmod_calls
    assert (postgres, 0o700) in chmod_calls
    assert tree_calls == [(postgres, POSTGRES_FS_UID, POSTGRES_FS_GID)]
    assert (root / "projects", 2000, 2001) in chown_calls

    chown_calls.clear()
    chmod_calls.clear()
    tree_calls.clear()
    monkeypatch.setattr(module, "_owner_uid", lambda path: POSTGRES_FS_UID)
    prepare_data_root(root, workflow_uid=2000, workflow_gid=2001)
    assert tree_calls == []
    assert (postgres, POSTGRES_FS_UID, POSTGRES_FS_GID) in chown_calls
    assert (root / "projects", 2000, 2001) in chown_calls


def test_main_requires_absolute_host_data_root(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("HOST_DATA_ROOT", raising=False)
    assert main() == 1
    monkeypatch.setenv("HOST_DATA_ROOT", "relative/path")
    assert main() == 1
    monkeypatch.setenv("HOST_DATA_ROOT", str(tmp_path / "missing"))
    assert main() == 1
    err = capsys.readouterr().err
    assert "HOST_DATA_ROOT" in err or "根目录不存在" in err
