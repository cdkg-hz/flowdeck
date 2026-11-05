import copy
import hashlib
import hmac
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from backend.config import ProjectConfig, Settings
from backend.db import InboxEvent, Job, Project, Record, Timeline, utcnow
from backend.main import create_app
from backend.service import upsert_github
from backend.worker import Worker
from tests.conftest import pull_payload


def signed(payload, key, delivery="delivery-1", event="pull_request"):
    raw = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(key.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"x-hub-signature-256": signature, "x-github-delivery": delivery, "x-github-event": event, "content-type": "application/json"}


def test_ingress_signature_allowlist_and_dedup(setup_db):
    settings, sessions, _ = setup_db
    payload = {"repository": {"full_name": "acme/mobile", "id": 123}, "number": 7, "pull_request": pull_payload(), "action": "opened"}
    endpoint = "/api/webhooks/github/" + "a" * 32
    with TestClient(create_app(settings)) as client:
        raw, headers = signed(payload, settings.github_webhook_secret)
        assert client.post(endpoint, content=raw, headers={**headers, "x-hub-signature-256": "sha256=wrong"}).status_code == 401
        assert client.post(endpoint, content=raw, headers=headers).json() == {"accepted": True, "duplicate": False}
        assert client.post(endpoint, content=raw, headers=headers).json()["duplicate"] is True
        bad, bad_headers = signed({**payload, "repository": {"full_name": "other/private", "id": 456}}, settings.github_webhook_secret, "foreign")
        assert client.post(endpoint, content=bad, headers=bad_headers).status_code == 403
        assert client.post(endpoint, content=raw, headers={**headers, "x-github-event": ""}).status_code == 400
        assert client.post(endpoint, content=raw, headers={**headers, "x-github-event": "unknown"}).status_code == 400
        malformed, malformed_headers = signed({"repository": {"full_name": "acme/mobile", "id": 123}}, settings.github_webhook_secret, "malformed")
        assert client.post(endpoint, content=malformed, headers=malformed_headers).status_code == 400
        foreign_ping, foreign_headers = signed({"repository": {"full_name": "other/private", "id": 456}}, settings.github_webhook_secret, "ping", "ping")
        assert client.post(endpoint, content=foreign_ping, headers=foreign_headers).status_code == 403
        assert client.post("/api/webhooks/github", content=raw, headers=headers).status_code == 410
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(InboxEvent)) == 1
        assert db.scalar(select(func.count()).select_from(Job)) == 0  # Request thread never executes jobs.


def test_pr_lifecycle_and_review_generation(setup_db):
    settings, sessions, _ = setup_db
    cfg = settings.project("acme/mobile")
    with sessions() as db:
        r = upsert_github(db, cfg, "pr", pull_payload(draft=True), canonical=True)
        db.commit()
        assert r.stage == "draft"
        assert not list(db.scalars(select(Job)))
        upsert_github(db, cfg, "pr", pull_payload(), "ready_for_review", canonical=True)
        db.commit()
        first = db.scalar(select(Job))
        assert r.stage == "review" and first.status == "queued"
        upsert_github(db, cfg, "pr", pull_payload(draft=True), "converted_to_draft", canonical=True)
        db.commit()
        assert first.status == "superseded" and r.stage == "draft"
        upsert_github(db, cfg, "pr", pull_payload(state="closed"), "closed", canonical=True)
        db.commit()
        upsert_github(db, cfg, "pr", pull_payload(), "reopened", canonical=True)
        db.commit()
        assert r.stage == "review" and r.status == "queued"
        assert len(list(db.scalars(select(Job).where(Job.status == "queued")))) == 1
        assert len(list(db.scalars(select(Job)))) == 2


async def test_equal_timestamp_events_use_canonical_sha(setup_db):
    settings, sessions, _ = setup_db
    canonical = pull_payload("c" * 40)
    class GH:
        async def pull(self, repo, number): return copy.deepcopy(canonical)
    worker = Worker(settings, sessions=sessions, github=GH(), agent=object())
    with sessions() as db:
        upsert_github(db, settings.project("acme/mobile"), "pr", pull_payload(), canonical=True)
        event = InboxEvent(source="github", delivery_id="late", event_type="pull_request", payload={"repository": {"full_name": "acme/mobile"}, "number": 7, "pull_request": pull_payload(), "action": "synchronize"})
        db.add(event); db.commit(); event_id = event.id
    await worker.process_event(event_id)
    with sessions() as db:
        r = db.scalar(select(Record))
        assert r.details["head_sha"] == "c" * 40
        queued = list(db.scalars(select(Job).where(Job.status == "queued")))
        assert len(queued) == 1 and queued[0].payload["head_sha"] == "c" * 40


def test_priority_and_release_snapshots(setup_db):
    settings, sessions, _ = setup_db
    cfg = ProjectConfig(repo="acme/mobile", release_enabled=True)
    value = {"id": 55, "tag_name": "v2", "draft": True, "prerelease": False, "created_at": "2026-09-13T10:00:00Z", "assets": []}
    with sessions() as db:
        r = upsert_github(db, cfg, "release", value, snapshot=True, trigger=False)
        db.commit()
        assert r.status == "waiting" and not list(db.scalars(select(Job)))
        upsert_github(db, cfg, "release", {**value, "draft": False}, action="published", canonical=True)
        db.commit()
        assert r.status == "queued" and len(list(db.scalars(select(Job)))) == 1
        upsert_github(db, cfg, "release", {**value, "draft": False, "prerelease": True}, action="edited", canonical=True)
        db.commit()
        assert r.status == "waiting" and db.scalar(select(Job)).status == "superseded"
        issue = upsert_github(db, cfg, "issue", {"id": 60, "number": 3, "title": "issue", "labels": [{"name": "普通"}, {"name": "紧急"}], "assignees": []})
        assert issue.priority == "紧急" and issue.details["priority_conflict"]


def test_executor_cannot_claim_unrelated_steps(setup_db):
    settings, sessions, _ = setup_db
    with sessions() as db:
        upsert_github(db, settings.project("acme/mobile"), "pr", pull_payload(), canonical=True)
        db.commit(); job_id = db.scalar(select(Job)).id
    event = {"version": 1, "event_id": "e1", "producer": "hermes", "repo": "acme/mobile", "execution_id": job_id, "step": "upload", "status": "success", "sequence": 1, "occurred_at": "2026-09-13T11:00:00Z", "message": "test", "result": {}}
    with TestClient(create_app(settings)) as client:
        raw, headers = signed(event, settings.executor_webhook_secret)
        headers["x-executor-signature-256"] = headers.pop("x-hub-signature-256")
        assert client.post("/api/events/executor", content=raw, headers=headers).status_code == 422
        event["step"] = "review"
        raw, headers = signed(event, settings.executor_webhook_secret)
        headers["x-executor-signature-256"] = headers.pop("x-hub-signature-256")
        assert client.post("/api/events/executor", content=raw, headers=headers).status_code == 202


def test_merge_policy_and_demo_boundaries(setup_db, tmp_path):
    with pytest.raises(ValueError): ProjectConfig(repo="acme/mobile", merge_enabled=True)
    with pytest.raises(ValueError): ProjectConfig(repo="acme/mobile", merge_enabled=True, review_enabled=True, required_checks=["flowdeck/agent-review"])
    allowed = ProjectConfig(repo="acme/mobile", merge_enabled=True, review_enabled=True)
    assert allowed.required_checks == []
    settings, sessions, _ = setup_db
    demo = settings.model_copy(update={"app_mode": "demo", "database_url": f"sqlite:///{(tmp_path / 'demo.db').as_posix()}"})
    with TestClient(create_app(demo)) as client:
        assert client.get("/api/overview").json()["mode"] == "demo"
        assert len(client.get("/api/records").json()["items"]) == 10
        assert client.post("/api/webhooks/github", json={}).status_code == 409
        assert client.get("/api/records?status=attention").json()["total"] == 3
        bugs = client.get("/api/records?kind=bug").json()
        assert bugs["total"] == 2 and {item["number"] for item in bugs["items"]} == {118, 131}
        assert client.get("/api/records?assignee=none").json()["total"] == 1
        assert client.get("/api/records?priority=none").json()["total"] == 3
    with pytest.raises(RuntimeError, match="without demo data"):
        with TestClient(create_app(demo.model_copy(update={"app_mode": "live"}))): pass


def test_priority_maps_chinese_labels_and_title_prefix(setup_db):
    settings, sessions, _ = setup_db
    cfg = settings.project("acme/mobile")
    with sessions() as db:
        normal = upsert_github(db, cfg, "issue", {"id": 201, "number": 201, "title": "卡顿", "labels": [{"name": "bug"}, {"name": "普通"}], "assignees": []})
        urgent = upsert_github(db, cfg, "issue", {"id": 202, "number": 202, "title": "crash", "labels": [{"name": "紧急"}], "assignees": []})
        low = upsert_github(db, cfg, "issue", {"id": 203, "number": 203, "title": "文案", "labels": [{"name": "较低"}], "assignees": []})
        titled = upsert_github(db, cfg, "issue", {"id": 204, "number": 204, "title": "[P1] 登录失败", "labels": [], "assignees": []})
        mixed = upsert_github(db, cfg, "issue", {"id": 205, "number": 205, "title": "issue", "labels": [{"name": "p0"}], "assignees": []})
        db.commit()
        assert normal.priority == "普通"
        assert urgent.priority == "紧急"
        assert low.priority == "较低"
        assert titled.priority == "普通"
        assert mixed.priority == "紧急"


def test_records_bug_priority_and_assignee_filters(setup_db):

    from backend.service import is_bug_record, work_type
    assert is_bug_record(["Bug", "P0"]) and is_bug_record(["缺陷"])
    assert not is_bug_record(["P1"]) and work_type("issue", ["bug"]) == "bug" and work_type("pr", ["bug"]) == "pr"
    settings, sessions, _ = setup_db
    cfg = settings.project("acme/mobile")
    with sessions() as db:
        upsert_github(db, cfg, "issue", {"id": 101, "number": 101, "title": "crash", "state": "open", "html_url": "https://github.com/acme/mobile/issues/101", "labels": [{"name": "Bug"}, {"name": "P0"}], "assignees": []})
        upsert_github(db, cfg, "issue", {"id": 102, "number": 102, "title": "feature", "state": "open", "html_url": "https://github.com/acme/mobile/issues/102", "labels": [{"name": "P2"}], "assignees": [{"login": "dev"}]})
        upsert_github(db, cfg, "issue", {"id": 103, "number": 103, "title": "缺陷页", "state": "open", "html_url": "https://github.com/acme/mobile/issues/103", "labels": [{"name": "缺陷"}], "assignees": [{"login": "dev"}]})
        db.commit()
    with TestClient(create_app(settings)) as client:
        bugs = client.get("/api/records?kind=bug").json()
        assert bugs["total"] == 2
        assert {item["number"] for item in bugs["items"]} == {101, 103}
        assert all(item["kind"] == "issue" and item["work_type"] == "bug" for item in bugs["items"])
        assert client.get("/api/records?kind=issue").json()["total"] == 3
        none_priority = client.get("/api/records?priority=none&kind=issue").json()
        assert none_priority["total"] == 1 and none_priority["items"][0]["number"] == 103
        unassigned = client.get("/api/records?assignee=none&kind=issue").json()
        assert unassigned["total"] == 1 and unassigned["items"][0]["number"] == 101
        assert client.get("/api/records?assignee=dev&kind=issue").json()["total"] == 2
        detail = client.get(f"/api/records/{bugs['items'][0]['id']}").json()
        assert detail["work_type"] == "bug" and detail["details"]["bug"] is True


def issue_payload(**changes):
    value = {
        "id": 60, "number": 3, "title": "crash on launch", "state": "open", "body": "repro",
        "html_url": "https://github.com/acme/mobile/issues/3",
        "labels": [{"name": "Bug"}, {"name": "P0"}], "assignees": [{"login": "dev"}],
        "created_at": "2026-09-13T10:00:00Z", "updated_at": "2026-09-13T10:00:00Z",
    }
    value.update(changes)
    return value


def test_issue_close_and_delete_sync_status(setup_db):
    settings, sessions, _ = setup_db
    cfg = settings.project("acme/mobile")
    with sessions() as db:
        record = upsert_github(db, cfg, "issue", issue_payload(), "opened")
        job = Job(record_id=record.id, key="agent-task:issue:3", kind="agent_task", status="running", payload={})
        db.add(job)
        db.commit()
        upsert_github(db, cfg, "issue", issue_payload(state="closed", updated_at="2026-09-13T11:00:00Z"), "closed")
        db.commit()
        assert record.status == "closed" and record.stage == "closed"
        assert record.details["bug"] is True and record.details["deleted"] is False
        assert job.status == "superseded"
        assert db.scalars(select(Timeline).where(Timeline.record_id == record.id).order_by(Timeline.occurred_at)).all()[-1].title == "Issue 已关闭"

        upsert_github(db, cfg, "issue", issue_payload(state="closed", updated_at="2026-09-13T11:30:00Z"), "deleted")
        db.commit()
        assert record.status == "closed" and record.details["deleted"] is True
        assert db.scalars(select(Timeline).where(Timeline.record_id == record.id).order_by(Timeline.occurred_at)).all()[-1].title == "GitHub 已删除"

        other = upsert_github(db, cfg, "issue", issue_payload(id=62, number=5, title="reopen then delete"), "opened")
        db.commit()
        upsert_github(db, cfg, "issue", issue_payload(id=62, number=5, title="reopen then delete", state="closed", updated_at="2026-09-13T11:00:00Z"), "closed")
        db.commit()
        upsert_github(db, cfg, "issue", issue_payload(id=62, number=5, title="reopen then delete", state="open", updated_at="2026-09-13T12:00:00Z"), "reopened")
        db.commit()
        assert other.status == "waiting" and other.stage == "issue"

        later = Job(record_id=other.id, key="agent-task:issue:5:reopen", kind="agent_task", status="queued", payload={})
        db.add(later)
        db.commit()
        upsert_github(db, cfg, "issue", issue_payload(id=62, number=5, title="reopen then delete", state="open", updated_at="2026-09-13T10:00:00Z"), "deleted")
        db.commit()
        assert other.status == "closed" and other.details["deleted"] is True
        assert later.status == "superseded"
        assert db.scalars(select(Timeline).where(Timeline.record_id == other.id).order_by(Timeline.occurred_at)).all()[-1].title == "GitHub 已删除"

        upsert_github(db, cfg, "issue", issue_payload(title="should not revive", state="open", updated_at="2026-09-13T13:00:00Z"), "edited")
        db.commit()
        assert record.title == "crash on launch" and record.details["deleted"] is True and record.status == "closed"


async def test_issue_webhook_closed_and_deleted(setup_db):
    settings, sessions, _ = setup_db
    worker = Worker(settings, sessions=sessions, github=object(), agent=object())
    opened = InboxEvent(source="github", delivery_id="open-issue", event_type="issues",
                        payload={"repository": {"full_name": "acme/mobile"}, "action": "opened", "issue": issue_payload(), "sender": {"login": "dev"}})
    closed = InboxEvent(source="github", delivery_id="close-issue", event_type="issues",
                        payload={"repository": {"full_name": "acme/mobile"}, "action": "closed", "issue": issue_payload(state="closed", updated_at="2026-09-13T11:00:00Z")})
    deleted = InboxEvent(source="github", delivery_id="delete-bug", event_type="issues",
                         payload={"repository": {"full_name": "acme/mobile"}, "action": "deleted", "issue": issue_payload(id=61, number=4, title="old bug", state="open", updated_at="2026-09-13T09:00:00Z")})
    with sessions() as db:
        db.add_all([opened, closed, deleted])
        upsert_github(db, settings.project("acme/mobile"), "issue", issue_payload(id=61, number=4, title="old bug"), "opened")
        db.commit()
        ids = opened.id, closed.id, deleted.id
    await worker.process_event(ids[0])
    await worker.process_event(ids[1])
    await worker.process_event(ids[2])
    with sessions() as db:
        closed_record = db.scalar(select(Record).where(Record.external_id == "60"))
        deleted_record = db.scalar(select(Record).where(Record.external_id == "61"))
        assert closed_record.status == "closed" and closed_record.details["deleted"] is False
        assert deleted_record.status == "closed" and deleted_record.details["deleted"] is True
        assert deleted_record.title == "old bug"


async def test_sync_reconciles_closed_and_deleted_issues(setup_db):
    settings, sessions, _ = setup_db
    keep = issue_payload(id=1, number=1, title="still open", labels=[{"name": "P2"}])
    missed = issue_payload(id=2, number=2, title="closed remotely", labels=[{"name": "缺陷"}])
    vanished = issue_payload(id=3, number=3, title="deleted remotely", labels=[{"name": "Bug"}])
    later_deleted = issue_payload(id=4, number=4, title="closed then deleted", labels=[{"name": "bug"}])
    class GH:
        async def pages(self, path, params=None, max_pages=100, list_key=None):
            params = params or {}
            if path.endswith("/issues") and params.get("state") == "open" and "since" not in params:
                yield keep
                return
            if path.endswith("/issues"):
                yield keep
        async def request(self, method, path, **kwargs):
            if path.endswith("/issues/2"):
                return {**missed, "state": "closed", "updated_at": "2026-09-13T11:00:00Z"}
            if path.endswith("/issues/3") or path.endswith("/issues/4"):
                request = httpx.Request("GET", "https://api.github.test" + path)
                raise httpx.HTTPStatusError("Not Found", request=request, response=httpx.Response(404, request=request))
            raise AssertionError(path)
    with sessions() as db:
        cfg = settings.project("acme/mobile")
        upsert_github(db, cfg, "issue", keep, "opened")
        upsert_github(db, cfg, "issue", missed, "opened")
        upsert_github(db, cfg, "issue", vanished, "opened")
        closed = upsert_github(db, cfg, "issue", later_deleted, "opened")
        upsert_github(db, cfg, "issue", {**later_deleted, "state": "closed", "updated_at": "2026-09-13T11:00:00Z"}, "closed")
        db.scalar(select(Project)).last_synced_at = utcnow()
        db.commit()
        assert closed.status == "closed" and closed.details["deleted"] is False
    worker = Worker(settings, sessions=sessions, github=GH(), agent=object())
    await worker.sync()
    with sessions() as db:
        by_number = {row.number: row for row in db.scalars(select(Record).where(Record.kind == "issue"))}
        assert by_number[1].status == "waiting"
        assert by_number[2].status == "closed" and by_number[2].details["deleted"] is False and by_number[2].details["bug"] is True
        assert by_number[3].status == "closed" and by_number[3].details["deleted"] is True and by_number[3].title == "deleted remotely"
        assert by_number[4].status == "closed" and by_number[4].details["deleted"] is True and by_number[4].title == "closed then deleted"
        assert db.scalars(select(Timeline).where(Timeline.record_id == by_number[4].id).order_by(Timeline.occurred_at)).all()[-1].title == "GitHub 已删除"
