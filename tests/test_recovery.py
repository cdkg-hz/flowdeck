from datetime import timedelta
import copy
import httpx
import pytest
from sqlalchemy import select
from backend.db import InboxEvent, Job, Record, utcnow
from backend.errors import LostLease
from backend.service import upsert_github
from backend.worker import Context, Worker
from tests.test_worker import FakeAgent, make_worker


async def test_expired_worker_cannot_write_or_publish(setup_db, tmp_path):
    worker, gh, sessions = make_worker(setup_db, tmp_path)
    job_id = worker.claim()
    stale = Context(worker, job_id)
    with sessions() as db:
        db.get(Job, job_id).lease_until = utcnow() - timedelta(seconds=1)
        db.commit()
    replacement = Worker(worker.settings, sessions=sessions, github=gh, agent=worker.agent, workspaces=worker.workspaces)
    assert replacement.claim() == job_id
    for action in [stale.checkpoint("upload_intent", True), stale.step("upload", "running", "stale"), stale.finish("success", "verify")]:
        with pytest.raises(LostLease):
            await action
    with pytest.raises(LostLease):
        stale.guard()
    with sessions() as db:
        job = db.get(Job, job_id)
        assert job.worker_id == replacement.identity and job.attempt == 2
        assert not job.result.get("upload_intent")


async def test_new_base_during_agent_analysis_never_posts_pass(setup_db, tmp_path):
    worker, gh, sessions = make_worker(setup_db, tmp_path)
    class MovingBase(FakeAgent):
        async def execute(self, *args):
            output = await super().execute(*args)
            gh.pr["base"]["sha"] = "e" * 40
            return output
    worker.agent = MovingBase()
    await worker.run_job(worker.claim())
    assert all(state != "success" for _, state in gh.statuses)
    assert not gh.comments and not gh.merges
    with sessions() as db:
        queued = db.scalar(select(Job).where(Job.status == "queued"))
        assert queued.payload["base_sha"] == "e" * 40


async def test_transient_job_retries_after_503(setup_db, tmp_path):
    worker, gh, sessions = make_worker(setup_db, tmp_path)
    original = gh.pull
    async def unavailable(*args):
        httpx.Response(503, request=httpx.Request("GET", "https://api.github.test/pr")).raise_for_status()
    gh.pull = unavailable
    job_id = worker.claim()
    await worker.run_job(job_id)
    with sessions() as db:
        job = db.get(Job, job_id)
        assert job.status == "waiting"
        job.available_at = utcnow()
        db.commit()
    gh.pull = original
    assert worker.claim() == job_id
    await worker.run_job(job_id)
    with sessions() as db:
        assert db.get(Job, job_id).status == "completed"


@pytest.mark.parametrize("status", [404, 503])
async def test_bad_event_does_not_starve_following_events(setup_db, tmp_path, status):
    worker, gh, sessions = make_worker(setup_db, tmp_path)
    async def unavailable(*args):
        httpx.Response(status, request=httpx.Request("GET", "https://api.github.test/pr")).raise_for_status()
    gh.pull = unavailable
    with sessions() as db:
        first = InboxEvent(source="github", delivery_id="bad", event_type="pull_request", payload={"repository": {"full_name": "acme/mobile"}, "number": 7}, received_at=utcnow() - timedelta(seconds=1))
        second = InboxEvent(source="github", delivery_id="good", event_type="issues", payload={"repository": {"full_name": "acme/mobile"}, "action": "opened", "issue": {"id": 32, "number": 8, "title": "Later valid issue", "state": "open"}})
        db.add_all([first, second]); db.commit()
        ids = first.id, second.id
    await worker.process_events()
    with sessions() as db:
        first, second = db.get(InboxEvent, ids[0]), db.get(InboxEvent, ids[1])
        assert first.error and first.attempt == 1
        assert bool(first.processed_at) == (status == 404)
        assert second.processed_at and not second.error
        assert db.scalar(select(Record).where(Record.number == 8)).title == "Later valid issue"


async def test_deleted_release_stops_running_job(setup_db, tmp_path):
    worker, gh, sessions = make_worker(setup_db, tmp_path)
    config = worker.settings.project("acme/mobile").model_copy(update={"release_enabled": True})
    release = {"id": 99, "name": "v2", "tag_name": "v2", "draft": False, "prerelease": False}
    with sessions() as db:
        record = upsert_github(db, config, "release", release, "published", canonical=True)
        job = db.scalar(select(Job).where(Job.record_id == record.id))
        job.status = "running"
        db.commit()
        upsert_github(db, config, "release", release, "deleted", canonical=True)
        db.commit()
        assert job.status == "superseded" and record.status == "closed"


async def test_closed_issue_stops_running_agent_task(setup_db, tmp_path):
    from tests.test_worker import FakeAgent, FakeGitHub, FakeWorkspace
    settings, sessions, _ = setup_db
    worker = Worker(settings, sessions=sessions, github=FakeGitHub(), agent=FakeAgent(), workspaces=FakeWorkspace(tmp_path / "workspaces"))
    config = worker.settings.project("acme/mobile")
    issue = {"id": 44, "number": 9, "title": "bug", "state": "open", "html_url": "https://github.com/acme/mobile/issues/9", "labels": [{"name": "bug"}], "assignees": []}
    with sessions() as db:
        record = upsert_github(db, config, "issue", issue, "opened")
        job = Job(record_id=record.id, key="agent-task:issue:9", kind="agent_task", status="queued", payload={})
        db.add(job)
        db.commit()
        job_id, record_id = job.id, record.id
    assert worker.claim() == job_id
    with sessions() as db:
        upsert_github(db, config, "issue", {**issue, "state": "closed"}, "closed")
        db.commit()
        assert db.get(Job, job_id).status == "superseded"
        assert db.get(Record, record_id).status == "closed"
    await worker.run_job(job_id)
    with sessions() as db:
        assert db.get(Job, job_id).status == "superseded"
        assert db.get(Record, record_id).status == "closed"
