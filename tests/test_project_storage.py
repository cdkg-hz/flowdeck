from __future__ import annotations

import hashlib
import io
import json
import tarfile

import httpx
import pytest
from cryptography.fernet import Fernet

from backend.config import Settings
from backend.credentials import seal
from backend.db import Base, Job, Project, ProjectConnection, Record, database
from backend.errors import ConfigurationNeeded
from backend.github import GitHub
from backend.storage import (
    PROJECT_MARKER,
    normalize_storage_path,
    prepare_project_storage,
    storage_roots,
    validate_project_marker,
)
from backend import workspace as workspace_module
from backend.workspace import Workspaces, command


def configured(tmp_path, *roots):
    return Settings(_env_file=None, data_root=tmp_path / "data", storage_roots=[str(root) for root in roots])


def test_storage_path_requires_existing_allowlisted_root(tmp_path):
    root = tmp_path / "mounted"
    settings = configured(tmp_path, root)

    with pytest.raises(ConfigurationNeeded, match="根目录不存在"):
        prepare_project_storage(settings, "project-one", root / "one")
    assert not root.exists()

    root.mkdir()
    result = prepare_project_storage(settings, "project-one", root / "teams" / "one")
    assert result == str((root / "teams" / "one").resolve())
    assert json.loads((root / "teams" / "one" / PROJECT_MARKER).read_text())["project_id"] == "project-one"
    assert validate_project_marker(settings, "project-one", result) == (root / "teams" / "one").resolve()


def test_storage_rejects_reserved_postgres_directory(tmp_path):
    data_root = tmp_path / "data"
    data_root.mkdir()
    (data_root / "postgres").mkdir()
    settings = Settings(_env_file=None, data_root=data_root, storage_roots=[str(data_root)])
    with pytest.raises(ConfigurationNeeded, match="数据库目录"):
        normalize_storage_path(settings, data_root / "postgres")
    with pytest.raises(ConfigurationNeeded, match="数据库目录"):
        normalize_storage_path(settings, data_root / "postgres" / "team")
    allowed = data_root / "projects" / "app"
    assert normalize_storage_path(settings, allowed) == str(allowed.resolve())


def test_storage_rejects_duplicate_roots_and_overlapping_project_paths(tmp_path):
    root = tmp_path / "mounted"
    root.mkdir()
    with pytest.raises(ConfigurationNeeded, match="不能重复"):
        storage_roots(configured(tmp_path, root, root / "."))

    settings = configured(tmp_path, root)
    first = root / "one"
    assert normalize_storage_path(settings, first) == str(first.resolve())
    with pytest.raises(ConfigurationNeeded, match="重复或互相嵌套"):
        normalize_storage_path(settings, first, [first])
    with pytest.raises(ConfigurationNeeded, match="重复或互相嵌套"):
        normalize_storage_path(settings, first / "nested", [first])
    with pytest.raises(ConfigurationNeeded, match="重复或互相嵌套"):
        normalize_storage_path(settings, first, [first / "nested"])


def test_storage_rejects_root_escape_unowned_content_and_wrong_marker(tmp_path):
    root = tmp_path / "mounted"
    root.mkdir()
    settings = configured(tmp_path, root)

    with pytest.raises(ConfigurationNeeded, match="允许的存储根目录"):
        normalize_storage_path(settings, root / ".." / "escaped")
    with pytest.raises(ConfigurationNeeded, match="不能等于根目录"):
        normalize_storage_path(settings, root)

    occupied = root / "occupied"
    occupied.mkdir()
    (occupied / "keep.txt").write_text("belongs to someone else")
    with pytest.raises(ConfigurationNeeded, match="非空"):
        prepare_project_storage(settings, "project-one", occupied)
    assert (occupied / "keep.txt").read_text() == "belongs to someone else"

    owned = root / "owned"
    prepare_project_storage(settings, "project-one", owned)
    with pytest.raises(ConfigurationNeeded, match="其他项目"):
        validate_project_marker(settings, "project-two", owned)


def test_storage_rejects_symlink_escape(tmp_path):
    root = tmp_path / "mounted"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this host")

    with pytest.raises(ConfigurationNeeded):
        prepare_project_storage(configured(tmp_path, root), "project-one", link / "project")
    assert not (outside / "project").exists()


async def test_github_uses_repository_token_for_rest_graphql_and_download(monkeypatch, tmp_path):
    import backend.projects as projects

    sessions = object()
    tokens = {"acme/one": "token-one", "acme/two": "token-two"}
    calls = []

    def project_token(_settings, actual_sessions, repo, require_active=True):
        assert actual_sessions is sessions
        assert require_active is True
        return tokens[repo]

    monkeypatch.setattr(projects, "project_token", project_token)

    def respond(request: httpx.Request):
        calls.append((str(request.url), request.headers.get("authorization")))
        if request.url.host == "objects.githubusercontent.com":
            return httpx.Response(200, content=b"apk")
        if request.url.path.endswith("/graphql"):
            return httpx.Response(200, json={"data": {"repository": {"pullRequest": {"closingIssuesReferences": {"nodes": [], "pageInfo": {"hasNextPage": False}}}}}})
        if "/releases/assets/" in request.url.path:
            return httpx.Response(302, headers={"location": "https://objects.githubusercontent.com/download.apk"})
        return httpx.Response(200, json={"full_name": request.url.path})

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=False)
    github = GitHub(Settings(_env_file=None), client=client, sessions=sessions)
    await github.request("GET", "/repos/acme/one")
    await github.request("GET", "/repos/acme/two", headers={"Authorization": "Bearer attacker"})
    await github.linked_issues("acme/one", 1)
    target = tmp_path / "app.apk"
    digest = await github.download_asset("acme/two", {"id": 7, "size": 3}, target)
    await github.close()

    assert digest == hashlib.sha256(b"apk").hexdigest()
    assert target.read_bytes() == b"apk"
    assert calls == [
        ("https://api.github.com/repos/acme/one", "Bearer token-one"),
        ("https://api.github.com/repos/acme/two", "Bearer token-two"),
        ("https://api.github.com/graphql", "Bearer token-one"),
        ("https://api.github.com/repos/acme/two/releases/assets/7", "Bearer token-two"),
        ("https://objects.githubusercontent.com/download.apk", None),
    ]


async def test_github_standalone_adapter_keeps_explicit_settings_token():
    seen = []

    def respond(request: httpx.Request):
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    github = GitHub(Settings(_env_file=None, github_token="standalone"), client=client)
    await github.request("GET", "/user")
    await github.close()
    assert seen == ["Bearer standalone"]


async def test_two_projects_use_separate_storage_caches_tokens_and_locked_paths(monkeypatch, tmp_path):
    storage_root = tmp_path / "mounted"
    storage_root.mkdir()
    key = Fernet.generate_key().decode()
    settings = Settings(
        _env_file=None,
        app_mode="live",
        data_root=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        storage_roots=[str(storage_root)],
        secrets_encryption_key=key,
    )
    engine, sessions = database(settings.database_url)
    Base.metadata.create_all(engine)
    task_ids = ["a" * 32, "b" * 32]
    repos = ["acme/one", "acme/two"]
    tokens = ["project-one-secret", "project-two-secret"]
    paths = [storage_root / "one", storage_root / "two"]
    with sessions() as db:
        for index, (task_id, repo, token, path) in enumerate(zip(task_ids, repos, tokens, paths), 1):
            project_id = str(index) * 32
            prepare_project_storage(settings, project_id, path)
            db.add(Project(id=project_id, repo=repo, name=repo))
            record = Record(id=(str(index + 2) * 32), repo=repo, kind="pull", external_id=str(index), title=repo)
            db.add(record)
            db.flush()
            db.add(ProjectConnection(
                project_id=project_id,
                storage_path=str(path.resolve()),
                storage_key=str(path.resolve()),
                storage_status="ready",
                api_status="ready",
                webhook_verified=True,
                repository_id=str(index),
                webhook_repository_id=str(index),
                revision=1,
                verified_revision=1,
                token_ciphertext=seal(settings, project_id, "github", token),
            ))
            db.add(Job(id=task_id, key=f"job-{index}", record_id=record.id, kind="review"))
        db.commit()

    archive_file = io.BytesIO()
    with tarfile.open(fileobj=archive_file, mode="w") as archive:
        content = b"snapshot\n"
        member = tarfile.TarInfo("source.txt")
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))
    archive_bytes = archive_file.getvalue()
    authorization_headers = []

    async def fake_command(argv, **kwargs):
        env = kwargs.get("env", {})
        if "GIT_CONFIG_VALUE_4" in env:
            authorization_headers.append(env["GIT_CONFIG_VALUE_4"])
        if len(argv) > 2 and argv[1:3] == ["init", "--bare"]:
            __import__("pathlib").Path(argv[-1]).mkdir(parents=True)
        if "archive" in argv:
            return archive_bytes
        if "diff" in argv and "--name-only" in argv:
            return b"source.txt\x00"
        if "diff" in argv:
            return b"diff"
        if "log" in argv:
            return b"commit message\n"
        return b""

    monkeypatch.setattr(workspace_module, "command", fake_command)
    manager = Workspaces(settings, sessions)
    for task_id, repo, path in zip(task_ids, repos, paths):
        result = await manager.prepare(task_id, repo, "1" * 40, "2" * 40)
        assert manager.task_dir(task_id) == path.resolve() / "workspaces" / task_id
        assert (path / "repo.git").is_dir()
        assert (path / "workspaces" / task_id / "code" / "source.txt").read_text() == "snapshot\n"
        assert result["host_workspace"] == str(path.resolve() / "workspaces" / task_id)

    decoded = {
        __import__("base64").b64decode(value.removeprefix("Authorization: Basic ")).decode()
        for value in authorization_headers
    }
    assert decoded == {"x-access-token:project-one-secret", "x-access-token:project-two-secret"}
    with sessions() as db:
        assert all(db.get(ProjectConnection, str(index) * 32).storage_locked for index in (1, 2))
        db.get(ProjectConnection, "1" * 32).api_status = "error"
        db.commit()
    assert manager.task_dir(task_ids[0]) == paths[0].resolve() / "workspaces" / task_ids[0]

    legacy_id = "c" * 32
    legacy = settings.data_root.resolve() / "workspaces" / legacy_id
    legacy.mkdir(parents=True)
    with sessions() as db:
        record = Record(id="5" * 32, repo=repos[1], kind="pull", external_id="legacy", title="legacy")
        db.add(record)
        db.flush()
        db.add(Job(id=legacy_id, key="job-legacy", record_id=record.id, kind="review"))
        db.commit()
    assert manager.task_dir(legacy_id) == legacy
    await manager.prepare(legacy_id, repos[1], "1" * 40, "2" * 40)
    assert (legacy / "code" / "source.txt").read_text() == "snapshot\n"
    assert not (paths[1] / "workspaces" / legacy_id).exists()
    engine.dispose()


async def test_command_masks_raw_project_token_from_subprocess_errors(monkeypatch):
    token = "project-super-secret-token"

    class Process:
        returncode = 1

        async def communicate(self):
            return b"", f"authentication failed for {token}".encode()

    async def create_subprocess_exec(*_argv, **_kwargs):
        return Process()

    monkeypatch.setattr(workspace_module.asyncio, "create_subprocess_exec", create_subprocess_exec)
    env = Workspaces(Settings(_env_file=None), sessions=object()).env(token)
    with pytest.raises(RuntimeError) as failure:
        await command(["git", "fetch"], env=env)
    assert token not in str(failure.value)
    assert "REDACTED" in str(failure.value)
