from pathlib import Path
import pytest
from starlette.testclient import TestClient
from backend.auth import set_admin
from backend.config import Settings
from backend.credentials import Fernet
from backend.db import AgentConnection, Base, Job, Project, ProjectAutomation, ProjectConnection, Record, Timeline, database
from backend.main import create_app
from backend.storage import prepare_project_storage

ADMIN_PASS = "admin-del-pass"


@pytest.fixture
def test_env(tmp_path):
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    settings = Settings(
        _env_file=None,
        app_mode="live",
        database_url=f"sqlite:///{(tmp_path / 'del_test.db').as_posix()}",
        data_root=tmp_path / "data",
        storage_roots=[str(storage_root)],
        secrets_encryption_key=Fernet.generate_key().decode(),
        session_secure=False,
    )
    engine, sessions = database(settings.database_url)
    Base.metadata.create_all(engine)
    app = create_app(settings)
    with sessions() as db:
        set_admin(db, "admin", ADMIN_PASS)
        db.commit()
    yield settings, sessions, app, storage_root
    engine.dispose()


def test_delete_project_lifecycle(test_env):
    settings, sessions, app, storage_root = test_env

    with TestClient(app) as client:
        # 1. Unauthorized delete
        res = client.delete("/api/projects/nonexistent")
        assert res.status_code == 401

        # 2. Login
        login = client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASS})
        assert login.status_code == 200
        csrf = login.json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf}

        # 3. Nonexistent project delete
        res = client.delete("/api/projects/nonexistent", headers=headers)
        assert res.status_code == 404

        # 4. Create a project
        proj_dir = storage_root / "my-org" / "my-repo"
        create_res = client.post("/api/projects", json={
            "name": "测试项目",
            "repo": "my-org/my-repo",
            "storage_path": str(proj_dir),
            "events": ["issues", "pull_request"],
            "github_token": "ghp_mocktoken123"
        }, headers=headers)
        assert create_res.status_code == 201
        proj_id = create_res.json()["id"]

        # Add automation
        client.put(f"/api/projects/{proj_id}/automation", json={"agent_id": ""}, headers=headers)

        # Add record, timeline, job
        with sessions() as db:
            rec = Record(repo="my-org/my-repo", kind="pr", external_id="101", title="PR 101")
            db.add(rec)
            db.flush()
            tl = Timeline(record_id=rec.id, stage="review", status="passed", title="Review passed")
            jb = Job(key="job-1", record_id=rec.id, kind="review", status="completed")
            db.add_all([tl, jb])
            db.commit()

        # Prepare storage on disk
        prepare_project_storage(settings, proj_id, str(proj_dir))
        assert proj_dir.exists()

        # 5. Delete project with delete_files=True
        del_res = client.delete(f"/api/projects/{proj_id}?delete_files=true", headers=headers)
        assert del_res.status_code == 200
        assert del_res.json()["ok"] is True
        assert del_res.json()["deleted_files"] is True

        # Check DB cleanup
        with sessions() as db:
            assert db.get(Project, proj_id) is None
            assert db.get(ProjectConnection, proj_id) is None
            assert db.get(ProjectAutomation, proj_id) is None
            assert db.query(Record).filter(Record.repo == "my-org/my-repo").count() == 0
            assert db.query(Timeline).count() == 0
            assert db.query(Job).count() == 0

        # Check disk cleanup
        assert not proj_dir.exists()

        # 6. Re-create project at same storage path succeeds
        recreate_res = client.post("/api/projects", json={
            "name": "重新测试项目",
            "repo": "my-org/my-repo",
            "storage_path": str(proj_dir),
            "events": ["issues"],
            "github_token": ""
        }, headers=headers)
        assert recreate_res.status_code == 201
        new_proj_id = recreate_res.json()["id"]

        prepare_project_storage(settings, new_proj_id, str(proj_dir))
        assert proj_dir.exists()

        # 7. Delete with delete_files=False keeps the directory
        del_keep = client.delete(f"/api/projects/{new_proj_id}?delete_files=false", headers=headers)
        assert del_keep.status_code == 200
        assert proj_dir.exists()


def test_delete_project_prunes_dingtalk_channel(test_env):
    _settings, _sessions, app, storage_root = test_env

    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASS})
        assert login.status_code == 200
        headers = {"X-CSRF-Token": login.json()["csrf_token"]}

        proj_dir = storage_root / "ding-org" / "ding-repo"
        created = client.post("/api/projects", json={
            "name": "钉钉修剪项目",
            "repo": "ding-org/ding-repo",
            "storage_path": str(proj_dir),
            "events": ["issues"],
            "github_token": "ghp_mocktoken123",
        }, headers=headers)
        assert created.status_code == 201
        proj_id = created.json()["id"]

        channel = client.post("/api/admin/dingtalk-channels", json={
            "name": "开发群",
            "webhook": "https://oapi.dingtalk.com/robot/send?access_token=TOKEN123",
            "sign_secret": "SECabc",
            "enabled": True,
            "events": ["review_needs_attention"],
            "project_ids": [proj_id],
        }, headers=headers)
        assert channel.status_code == 201
        channel_id = channel.json()["id"]
        assert channel.json()["project_ids"] == [proj_id]

        deleted = client.delete(f"/api/projects/{proj_id}?delete_files=false", headers=headers)
        assert deleted.status_code == 200

        listed = client.get("/api/admin/integrations", headers=headers)
        assert listed.status_code == 200
        channels = listed.json()["dingtalk_channels"]
        match = next(item for item in channels if item["id"] == channel_id)
        assert match["project_ids"] == []


def test_delete_agent(test_env):
    settings, sessions, app, storage_root = test_env

    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASS})
        csrf = login.json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf}

        # Create Agent
        ag_res = client.post("/api/admin/agents", json={
            "name": "Agent-To-Delete",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-test",
            "model": "gpt-5-codex",
            "timeout_seconds": 60,
            "enabled": True
        }, headers=headers)
        assert ag_res.status_code == 201
        agent_id = ag_res.json()["id"]

        # Delete Agent
        del_ag = client.delete(f"/api/admin/agents/{agent_id}", headers=headers)
        assert del_ag.status_code == 200
        with sessions() as db:
            assert db.get(AgentConnection, agent_id) is None
