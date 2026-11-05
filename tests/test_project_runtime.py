from datetime import timedelta
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from backend.cli import cleanup_tasks
from backend.db import InboxEvent, Job, Project, ProjectConnection, Record, utcnow
from backend.projects import verify_api_access, verify_connections
from backend.errors import ConfigurationNeeded
from backend.schemas import ReviewResult
from backend.worker import Worker


async def test_paused_project_backlog_does_not_starve_active_project(setup_db):
    settings, sessions, _ = setup_db
    with sessions() as db:
        for index in range(60):
            db.add(InboxEvent(source="github", delivery_id=f"pending-{index}", event_type="issues",
                received_at=utcnow() - timedelta(days=1), payload={"repository": {"full_name": "other/pending"}, "issue": {"id": index}}))
        db.add(InboxEvent(source="github", delivery_id="ready", event_type="issues",
            payload={"repository": {"full_name": "acme/mobile"}, "issue": {"id": 987, "number": 9, "title": "Ready project", "state": "open"}}))
        db.commit()
    worker = Worker(settings, sessions=sessions, github=object(), agent=object())
    await worker.process_events()
    with sessions() as db:
        assert db.scalar(select(Record)).title == "Ready project"
        assert db.scalar(select(InboxEvent).where(InboxEvent.delivery_id == "ready")).processed_at
        assert db.scalar(select(InboxEvent).where(InboxEvent.delivery_id == "pending-0")).processed_at is None


async def test_sync_respects_selected_event_categories(setup_db):
    settings, sessions, _ = setup_db
    calls = []
    class GH:
        async def pages(self, path, *args, **kwargs):
            calls.append(path)
            if path.endswith("/releases"):
                yield {"id": 42, "tag_name": "v1", "name": "v1", "created_at": utcnow().isoformat(), "published_at": utcnow().isoformat(), "draft": False, "prerelease": False}
    with sessions() as db:
        db.get(ProjectConnection, "a" * 32).events = ["release"]
        db.commit()
    worker = Worker(settings, sessions=sessions, github=GH(), agent=object())
    await worker.sync()
    assert calls == ["/repos/acme/mobile/releases"]
    with sessions() as db:
        assert db.scalar(select(Record)).kind == "release"
        assert db.scalar(select(Job)) is None


def test_cleanup_uses_project_ownership_even_after_token_expiry(setup_db):
    settings, sessions, _ = setup_db
    task_id = "b" * 32
    with sessions() as db:
        connection = db.get(ProjectConnection, "a" * 32)
        path = Path(connection.storage_path) / "workspaces" / task_id
        path.mkdir(parents=True)
        (path / "task.json").write_text(json.dumps({"repo": "acme/mobile"}), encoding="utf-8")
        sibling = Path(connection.storage_path) / "keep.txt"
        sibling.write_text("keep", encoding="utf-8")
        record = Record(repo="acme/mobile", kind="pr", external_id="cleanup", title="cleanup")
        db.add(record)
        db.flush()
        db.add(Job(id=task_id, record_id=record.id, key="cleanup", kind="review", status="completed", updated_at=utcnow() - timedelta(days=30)))
        connection.api_status = "error"
        db.commit()
    assert cleanup_tasks(settings, sessions, 14) == 1
    assert not path.exists() and sibling.read_text(encoding="utf-8") == "keep"


async def test_api_verification_uses_only_reads_and_repo_identity(setup_db, monkeypatch):
    settings, _, _ = setup_db
    calls = []
    class GH:
        def __init__(self, cfg):
            assert cfg.github_token == "project-verification-token"
        async def request(self, method, path, **kwargs):
            calls.append((method, path))
            if path == "/user": return {"id": 10}
            if path == "/repos/acme/mobile": return {"id": 123, "full_name": "acme/mobile"}
            if path.endswith("/commits"): return [{"sha": "a" * 40}]
            return []
        async def close(self): pass
    monkeypatch.setattr("backend.github.GitHub", GH)
    result = await verify_api_access(settings, "project-verification-token", "acme/mobile", ["issues", "pull_request", "release", "check_run", "status"])
    assert result == "123"
    assert all(method == "GET" for method, _ in calls)
    assert any(path.endswith("/check-runs") for _, path in calls)
    assert settings.github_token == "test-token"


async def test_expired_verification_attempt_cannot_overwrite_new_success(setup_db):
    settings, sessions, _ = setup_db
    started, release = asyncio.Event(), asyncio.Event()
    with sessions() as db:
        connection = db.get(ProjectConnection, "a" * 32)
        connection.verify_requested, connection.next_verify_at = True, utcnow()
        db.commit()
    async def slow(*args):
        started.set()
        await release.wait()
        raise ConfigurationNeeded("old failed request")
    async def success(*args): return "123"
    old = asyncio.create_task(verify_connections(settings, sessions, verifier=slow))
    await started.wait()
    with sessions() as db:
        db.get(ProjectConnection, "a" * 32).next_verify_at = utcnow() - timedelta(seconds=1)
        db.commit()
    await verify_connections(settings, sessions, verifier=success)
    release.set()
    await old
    with sessions() as db:
        connection = db.get(ProjectConnection, "a" * 32)
        assert connection.api_status == "ready" and connection.api_error is None


async def test_agent_result_does_not_publish_server_paths_or_tokens(setup_db):
    settings, sessions, _ = setup_db
    with sessions() as db:
        path = db.get(ProjectConnection, "a" * 32).storage_path
    class Agent:
        async def execute(self, *args):
            return json.dumps({"head_sha": "a" * 40, "verdict": "pass", "summary": f"Read {path}/workspaces/123 using test-token", "findings": []})
    class Context:
        def snapshot(self): return SimpleNamespace(id="a" * 32, result={}), None
        async def checkpoint(self, *args): pass
    worker = Worker(settings, sessions=sessions, github=object(), agent=Agent())
    result = await worker.analyze(Context(), "review", ReviewResult, {})
    assert path not in result.summary and "test-token" not in result.summary
    assert "[REDACTED]" in result.summary


def test_cleanup_rejects_legacy_parent_junction_or_symlink(setup_db, tmp_path):
    settings, sessions, _ = setup_db
    task_id = "c" * 32
    outside = tmp_path / "outside" / "workspaces"
    target = outside / task_id
    target.mkdir(parents=True)
    (target / "task.json").write_text(json.dumps({"repo": "acme/mobile"}), encoding="utf-8")
    sentinel = target / "keep.txt"
    sentinel.write_text("must not be deleted", encoding="utf-8")
    link = settings.data_root / "workspaces"
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(outside), str(link))
    else:
        link.symlink_to(outside, target_is_directory=True)
    with sessions() as db:
        record = Record(repo="acme/mobile", kind="pr", external_id="outside", title="outside")
        db.add(record)
        db.flush()
        db.add(Job(id=task_id, record_id=record.id, key="outside", kind="review", status="completed", updated_at=utcnow() - timedelta(days=30)))
        db.commit()
    with pytest.raises((ValueError, ConfigurationNeeded)):
        cleanup_tasks(settings, sessions, 14)
    assert sentinel.read_text(encoding="utf-8") == "must not be deleted"
