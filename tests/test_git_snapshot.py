import json
from pathlib import Path
import pytest
from backend import workspace as module
from backend.config import Settings
from backend.workspace import Workspaces, command, minimal_environment


def _git_env():
    null = "NUL" if __import__("os").name == "nt" else "/dev/null"
    return minimal_environment(extra={
        "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.test",
        "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.test",
        "GIT_CONFIG_GLOBAL": null,
    })


async def _bare_cache(tmp_path):
    source = tmp_path / "source"
    env = _git_env()
    await command(["git", "init", str(source)], env=env)
    (source / "file.txt").write_text("head\n", encoding="utf-8")
    await command(["git", "add", "."], cwd=source, env=env)
    await command(["git", "commit", "-m", "head"], cwd=source, env=env)
    head = (await command(["git", "rev-parse", "HEAD"], cwd=source, env=env)).decode().strip()
    root = tmp_path / "data"
    cache = root / "repos" / "acme--mobile.git"
    cache.parent.mkdir(parents=True)
    await command(["git", "clone", "--bare", str(source), str(cache)], env=env)
    return root, cache, head, env


async def test_real_git_exports_pinned_snapshots_without_export_ignore(tmp_path, monkeypatch):
    source = tmp_path / "source"
    env = minimal_environment(extra={"GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.test", "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.test", "GIT_CONFIG_GLOBAL": "NUL" if __import__('os').name == 'nt' else '/dev/null'})
    await command(["git", "init", str(source)], env=env)
    (source / "file.txt").write_text("first\n")
    await command(["git", "add", "."], cwd=source, env=env)
    await command(["git", "commit", "-m", "base"], cwd=source, env=env)
    base = (await command(["git", "rev-parse", "HEAD"], cwd=source, env=env)).decode().strip()
    (source / "file.txt").write_text("second\n")
    (source / "hidden.txt").write_text("must be reviewed\n")
    (source / ".gitattributes").write_text("hidden.txt export-ignore\n")
    await command(["git", "add", "."], cwd=source, env=env)
    await command(["git", "commit", "-m", "feature"], cwd=source, env=env)
    head = (await command(["git", "rev-parse", "HEAD"], cwd=source, env=env)).decode().strip()
    root = tmp_path / "data"
    cache = root / "repos" / "acme--mobile.git"; cache.parent.mkdir(parents=True)
    await command(["git", "clone", "--bare", str(source), str(cache)], env=env)
    original = module.command
    async def local_objects_only(argv, **kwargs):
        if "fetch" in argv:
            return b""  # Objects already fetched from the disposable local fixture.
        return await original(argv, **kwargs)
    monkeypatch.setattr(module, "command", local_objects_only)
    manager = Workspaces(Settings(_env_file=None, data_root=root))
    task = "a" * 32
    result = await manager.prepare(task, "acme/mobile", base, head, 7)
    output = manager.task_dir(task)
    assert (output / "code" / "file.txt").read_text() == "second\n"
    assert (output / "base" / "file.txt").read_text() == "first\n"
    assert (output / "code" / "hidden.txt").is_file()
    assert not (output / "code" / ".git").exists()
    assert result["head_sha"] == head and result["merge_base"] == base
    assert "hidden.txt" in result["changed_files"] and "+second" in (output / "changes.diff").read_text()
    again = await manager.prepare(task, "acme/mobile", base, head, 7)
    assert again == result


async def test_prepare_worktree_adds_local_worktree_despite_file_protocol_block(tmp_path):
    root, cache, head, env = await _bare_cache(tmp_path)
    manager = Workspaces(Settings(_env_file=None, data_root=root))
    task = "b" * 32
    directory = manager.task_dir(task)
    directory.mkdir(parents=True)
    leftover = directory / "work"
    leftover.mkdir()
    seen = []
    original = module.command

    async def record(argv, **kwargs):
        seen.append(list(argv))
        return await original(argv, **kwargs)

    module.command = record
    try:
        result = await manager.prepare_worktree(task, "acme/mobile", sha=head, branch="flowdeck/docs-v1")
    finally:
        module.command = original
    work = directory / "work"
    assert result == {"branch": "flowdeck/docs-v1", "sha": head}
    assert (work / "file.txt").read_text(encoding="utf-8") == "head\n"
    assert (work / ".git").is_file()
    assert any("worktree" in argv and "add" in argv for argv in seen)
    assert not any("clone" in argv for argv in seen)
    blocked = tmp_path / "blocked-clone"
    with pytest.raises(RuntimeError, match="transport 'file' not allowed"):
        await command(["git", "clone", "--no-checkout", str(cache), str(blocked)], env=manager.env())
    again = await manager.prepare_worktree(task, "acme/mobile", sha=head, branch="flowdeck/docs-v1")
    assert again == result


async def test_prepare_worktree_replaces_failed_clone_leftover(tmp_path):
    root, _cache, head, _env = await _bare_cache(tmp_path)
    manager = Workspaces(Settings(_env_file=None, data_root=root))
    task = "e" * 32
    leftover = manager.task_dir(task) / "work"
    leftover.mkdir(parents=True)
    (leftover / ".git").mkdir()
    (leftover / ".git" / "HEAD").write_text("ref: refs/heads/master\n", encoding="utf-8")
    (leftover / "orphan.txt").write_text("partial clone", encoding="utf-8")
    result = await manager.prepare_worktree(task, "acme/mobile", sha=head, branch="flowdeck/docs-v1")
    work = leftover
    assert result == {"branch": "flowdeck/docs-v1", "sha": head}
    assert (work / "file.txt").read_text(encoding="utf-8") == "head\n"
    assert (work / ".git").is_file()
    assert not (work / "orphan.txt").exists()


async def test_docs_baseline_uses_commit_on_latest_manual_date(tmp_path, monkeypatch):
    source = tmp_path / "source"
    env = _git_env()
    await command(["git", "init", str(source)], env=env)
    docs = source / "docs" / "updata_log"
    docs.mkdir(parents=True)
    (docs / "manual_2026-06-30.md").write_text("older\n", encoding="utf-8")
    await command(["git", "add", "."], cwd=source, env=env)
    env_old = {**env, "GIT_AUTHOR_DATE": "2026-06-30T12:00:00+0000", "GIT_COMMITTER_DATE": "2026-06-30T12:00:00+0000"}
    await command(["git", "commit", "-m", "old docs"], cwd=source, env=env_old)
    (docs / "manual_2026-09-10.md").write_text("baseline\n", encoding="utf-8")
    (docs / "architecture_2026-09-10.md").write_text("arch\n", encoding="utf-8")
    await command(["git", "add", "."], cwd=source, env=env)
    env_docs = {**env, "GIT_AUTHOR_DATE": "2026-09-10T12:00:00+0000", "GIT_COMMITTER_DATE": "2026-09-10T12:00:00+0000"}
    await command(["git", "commit", "-m", "latest docs"], cwd=source, env=env_docs)
    docs_sha = (await command(["git", "rev-parse", "HEAD"], cwd=source, env=env)).decode().strip()
    (source / "app.txt").write_text("feature\n", encoding="utf-8")
    await command(["git", "add", "."], cwd=source, env=env)
    env_head = {**env, "GIT_AUTHOR_DATE": "2026-09-13T12:00:00+0000", "GIT_COMMITTER_DATE": "2026-09-13T12:00:00+0000"}
    await command(["git", "commit", "-m", "feature"], cwd=source, env=env_head)
    head = (await command(["git", "rev-parse", "HEAD"], cwd=source, env=env)).decode().strip()
    root = tmp_path / "data"
    cache = root / "repos" / "acme--mobile.git"
    cache.parent.mkdir(parents=True)
    await command(["git", "clone", "--bare", str(source), str(cache)], env=env)
    original = module.command

    async def local_objects_only(argv, **kwargs):
        if "fetch" in argv:
            return b""
        return await original(argv, **kwargs)

    monkeypatch.setattr(module, "command", local_objects_only)
    manager = Workspaces(Settings(_env_file=None, data_root=root))
    result = await manager.docs_baseline("a" * 32, "acme/mobile", head)
    assert result["sha"] == docs_sha
    assert result["docs_date"] == "2026-09-10"
    assert result["files"] == [
        "docs/updata_log/architecture_2026-09-10.md",
        "docs/updata_log/manual_2026-09-10.md",
    ]


async def test_docs_baseline_empty_when_no_dated_manuals(tmp_path, monkeypatch):
    root, _cache, head, _env = await _bare_cache(tmp_path)
    original = module.command

    async def local_objects_only(argv, **kwargs):
        if "fetch" in argv:
            return b""
        return await original(argv, **kwargs)

    monkeypatch.setattr(module, "command", local_objects_only)
    manager = Workspaces(Settings(_env_file=None, data_root=root))
    result = await manager.docs_baseline("a" * 32, "acme/mobile", head)
    assert result == {"sha": "", "docs_date": "", "files": []}


async def test_prepare_worktree_can_run_two_tasks_from_same_cache(tmp_path):
    root, cache, head, _env = await _bare_cache(tmp_path)
    manager = Workspaces(Settings(_env_file=None, data_root=root))
    first = "c" * 32
    second = "d" * 32
    manager.task_dir(first).mkdir(parents=True)
    manager.task_dir(second).mkdir(parents=True)
    await manager.prepare_worktree(first, "acme/mobile", sha=head, branch="flowdeck/docs-v1")
    await manager.prepare_worktree(second, "acme/mobile", sha=head, branch="flowdeck/docs-v1")
    assert (manager.task_dir(first) / "work" / "file.txt").is_file()
    assert (manager.task_dir(second) / "work" / "file.txt").is_file()
