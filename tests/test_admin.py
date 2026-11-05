from sqlalchemy import inspect, select, text
from starlette.testclient import TestClient
from backend import cli
from backend.auth import set_admin
from backend.db import InboxEvent, Job, _add_column, database, initialize_schema
from backend.main import create_app
from tests.test_worker import make_worker


def test_retry_uncertain_keeps_intent_without_acknowledgement(setup_db, tmp_path, monkeypatch):
    worker, _, sessions = make_worker(setup_db, tmp_path)
    with sessions() as db:
        job = db.scalar(select(Job)); job.status = "uncertain"; job.result = {"upload_intent": True}; db.commit(); job_id = job.id
    monkeypatch.setattr(cli, "get_settings", lambda: worker.settings)
    monkeypatch.setattr("sys.argv", ["cli", "retry", job_id])
    cli.main()
    with sessions() as db:
        job = db.get(Job, job_id)
        assert job.status == "queued" and job.result["upload_intent"] is True


def test_explicit_acknowledgement_allows_new_submission(setup_db, tmp_path, monkeypatch):
    worker, _, sessions = make_worker(setup_db, tmp_path)
    with sessions() as db:
        job = db.scalar(select(Job)); job.status = "uncertain"; job.result = {"upload": {"published": True}, "upload_intent": True, "deploy_intent": True}; db.commit(); job_id = job.id
    monkeypatch.setattr(cli, "get_settings", lambda: worker.settings)
    monkeypatch.setattr("sys.argv", ["cli", "retry", job_id, "--acknowledge-uncertain"])
    cli.main()
    with sessions() as db:
        job = db.get(Job, job_id)
        assert job.result["upload"]["published"] and "deploy_intent" not in job.result


def test_reusable_agent_checkpoint_drops_failed_output():
    from backend.service import reusable_agent_checkpoint
    cached = {"thread_id": "t1", "output": '{"push_requested": false}'}
    assert reusable_agent_checkpoint({"agent_agent_task": cached}, "agent_agent_task") == {}
    kept = reusable_agent_checkpoint({"agent_agent_task": cached, "push_intent": True}, "agent_agent_task")
    assert kept["output"] == cached["output"]
    running = reusable_agent_checkpoint({"agent_agent_task": {"thread_id": "t1"}}, "agent_agent_task")
    assert running == {"thread_id": "t1"}


def test_retry_prepare_release_clears_cached_agent_output(setup_db, tmp_path):
    from backend.service import retry_job
    worker, _, sessions = make_worker(setup_db, tmp_path)
    with sessions() as db:
        job = db.scalar(select(Job))
        job.kind, job.status, job.error = "prepare_release", "failed", "总结并发版必须请求推送变更"
        job.result = {"workspace": {"head_sha": "a" * 40}, "agent_agent_task": {"thread_id": "t1", "output": '{"outcome":"failed"}'}}
        db.commit()
        retry_job(db, job)
        db.commit()
        job = db.get(Job, job.id)
        assert job.status == "queued" and job.error is None
        assert "agent_agent_task" not in job.result
        assert job.result["analysis_generation"] == 1
        assert job.result["workspace"]["head_sha"] == "a" * 40


def test_retry_job_api_requeues_needs_configuration(setup_db, tmp_path):
    worker, _, sessions = make_worker(setup_db, tmp_path)
    with sessions() as db:
        job = db.scalar(select(Job))
        job.status, job.error = "needs_configuration", "尚未配置可执行的 Release 步骤"
        set_admin(db, "admin", "admin-password-123")
        db.commit()
        job_id = job.id
    with TestClient(create_app(worker.settings)) as client:
        login = client.post("/api/auth/login", json={"username": "admin", "password": "admin-password-123"})
        assert login.status_code == 200
        res = client.post(f"/api/jobs/{job_id}/retry", headers={"X-CSRF-Token": login.json()["csrf_token"]})
        assert res.status_code == 202
        assert res.json()["status"] == "queued"
    with sessions() as db:
        job = db.get(Job, job_id)
        assert job.status == "queued" and job.error is None


def test_initialize_schema_is_idempotent_when_model_exists(tmp_path):
    engine, _ = database(f"sqlite:///{(tmp_path / 'schema.db').as_posix()}")
    initialize_schema(engine)
    initialize_schema(engine)
    assert "model" in {column["name"] for column in inspect(engine).get_columns("agent_connections")}
    with engine.begin() as connection:
        _add_column(connection, "ALTER TABLE agent_connections ADD COLUMN model VARCHAR(80) NOT NULL DEFAULT ''")
