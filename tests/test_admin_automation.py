from starlette.testclient import TestClient
from sqlalchemy import select
from backend.auth import set_admin
from backend.db import Job, Record
from backend.main import create_app
from backend.projects import project_config
from backend.service import upsert_github
from tests.conftest import pull_payload

ADMIN_PASS = "admin-password-123"


def test_admin_agent_api(setup_db, monkeypatch):
    settings, sessions, _ = setup_db
    app = create_app(settings)
    with sessions() as db:
        set_admin(db, "admin", ADMIN_PASS)
        db.commit()

    with TestClient(app) as client:
        # Login as admin
        login = client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASS})
        assert login.status_code == 200
        csrf = login.json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf}

        # 1. List admin integrations
        res = client.get("/api/admin/integrations", headers=headers)
        assert res.status_code == 200
        data = res.json()
        assert len(data["agents"]) >= 1

        # 2. Create new agent
        agent_body = {
            "name": "DeepSeek-V3",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "sk-test-key",
            "model": "deepseek-chat",
            "reasoning_effort": "medium",
            "timeout_seconds": 120,
            "enabled": True,
        }
        create_agent = client.post("/api/admin/agents", json=agent_body, headers=headers)
        assert create_agent.status_code == 201
        created_agent = create_agent.json()
        assert created_agent["name"] == "DeepSeek-V3"
        assert created_agent["api_key_configured"] is True
        assert created_agent["model"] == "deepseek-chat"
        agent_id = created_agent["id"]

        async def fake_probe(settings_, sessions_, runtime_, client_factory=None):
            return f"已验证模型接口、凭据与模型（{runtime_.model}）"

        monkeypatch.setattr("backend.copilot_runtime.probe_agent", fake_probe)
        missing_model = client.post(f"/api/admin/agents/{'f' * 32}/test", headers=headers)
        assert missing_model.status_code == 200
        assert missing_model.json()["ok"] is True

        # 3. Update agent
        agent_body["name"] = "DeepSeek-V3 (Updated)"
        agent_body["api_key"] = ""  # Keep existing key
        update_agent = client.put(f"/api/admin/agents/{agent_id}", json=agent_body, headers=headers)
        assert update_agent.status_code == 200
        assert update_agent.json()["name"] == "DeepSeek-V3 (Updated)"
        assert update_agent.json()["api_key_configured"] is True

        # 4. Test agent connection endpoint
        test_agent = client.post(f"/api/admin/agents/{agent_id}/test", headers=headers)
        assert test_agent.status_code == 200
        assert test_agent.json()["ok"] is True
        assert "deepseek-chat" in test_agent.json()["message"]

        async def failing_probe(settings_, sessions_, runtime_, client_factory=None):
            raise ValueError("连不上模型接口 api.deepseek.com")

        monkeypatch.setattr("backend.copilot_runtime.probe_agent", failing_probe)
        failed = client.post(f"/api/admin/agents/{agent_id}/test", headers=headers)
        assert failed.status_code == 422
        assert "连不上" in failed.json()["detail"]

        listed = client.get("/api/admin/integrations", headers=headers)
        assert listed.status_code == 200
        assert "dingtalk_channels" in listed.json()
        assert "registries" not in listed.json()
        assert client.post("/api/admin/registries", json={"name": "ACR", "registry": "example.com", "username": "u", "password": "p"}, headers=headers).status_code in {404, 405}

        project_id = "a" * 32
        auto_body = {"agent_id": agent_id, "build": {"enabled": True, "registry_id": "x", "repository": "myteam/backend"}}
        update_auto = client.put(f"/api/projects/{project_id}/automation", json=auto_body, headers=headers)
        assert update_auto.status_code == 200
        auto_resp = update_auto.json()
        assert auto_resp["automation_config"]["agent_id"] == agent_id
        assert "build" not in auto_resp["automation_config"]
        assert "build" not in auto_resp["automation"]
        assert "deploy" not in auto_resp["automation"]
        assert "script_publisher" not in auto_resp["automation_config"]
        assert "apk_pattern" not in auto_resp["automation_config"]
        assert "publisher_username_configured" not in auto_resp

        from backend.projects import project_config
        with sessions() as db:
            merged = project_config(settings, db, "acme/mobile", active_only=False)
        assert merged.agent_id == agent_id
        assert not hasattr(merged, "build")
        assert not hasattr(merged, "deploy")


def test_manual_release_run_endpoint(setup_db):
    settings, sessions, config_path = setup_db
    config_path.write_text("projects:\n  - repo: acme/mobile\n    release_enabled: true\n", encoding="utf-8")
    app = create_app(settings)
    with sessions() as db:
        set_admin(db, "admin", ADMIN_PASS)
        db.commit()

    with TestClient(app) as client:
        # Login
        login = client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASS})
        assert login.status_code == 200
        csrf = login.json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf}

        record_id = "r" * 32
        with sessions() as db:
            db.add(Record(
                id=record_id,
                repo="acme/mobile",
                kind="release",
                external_id="rel_123",
                number=None,
                title="Release v2.0",
                body="Release notes",
                url="https://github.com/acme/mobile/releases/tag/v2.0",
                status="waiting",
                stage="release",
                details={"release_id": 123, "tag": "v2.0", "draft": False, "prerelease": False},
            ))
            db.commit()

        run = client.post(f"/api/releases/{record_id}/run", headers=headers)
        assert run.status_code == 202
        job_id = run.json()["id"]

        with sessions() as db:
            job = db.get(Job, job_id)
            assert job.kind == "release"
            assert job.payload["tag"] == "v2.0"
            assert job.payload["manual"] is True


def test_require_login_enforcement(setup_db):
    settings, sessions, _ = setup_db
    secure_settings = settings.model_copy(update={"require_login": True})
    app = create_app(secure_settings)
    with sessions() as db:
        set_admin(db, "admin", ADMIN_PASS)
        db.commit()

    with TestClient(app) as client:
        # 1. Unauthenticated requests must be blocked with 401
        assert client.get("/api/auth/session").json()["require_login"] is True
        assert client.get("/api/overview").status_code == 401
        assert client.get("/api/records").status_code == 401
        assert client.get("/api/projects").status_code == 401
        assert client.get("/api/integrations").status_code == 401
        assert client.get("/api/stream").status_code == 401

        # 2. Login succeeds
        login = client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASS})
        assert login.status_code == 200

        # 3. Authenticated requests succeed with 200
        assert client.get("/api/overview").status_code == 200
        assert client.get("/api/records").status_code == 200
        assert client.get("/api/projects").status_code == 200
        integ = client.get("/api/integrations")
        assert integ.status_code == 200
        assert integ.json()["agent"]["protocol"] == "copilot"
        assert "registry" not in integ.json()
        assert "build" not in integ.json()["projects"][0]
        assert "deploy" not in integ.json()["projects"][0]


def test_agent_rules_round_trip_and_later_put_keeps_them(setup_db):
    settings, sessions, _ = setup_db
    app = create_app(settings)
    with sessions() as db:
        set_admin(db, "admin", ADMIN_PASS)
        db.commit()
    agent_id = "f" * 32
    rule = {
        "name": "审查后合并",
        "event": "pull_request",
        "agent_id": agent_id,
        "prompt": "审查这个 PR，通过则合并。",
        "include_repository": False,
        "include_github_credentials": True,
        "skill_ids": ["releasing-version-docs"],
        "enabled": True,
    }
    with TestClient(app) as client:
        headers = {"X-CSRF-Token": client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASS}).json()["csrf_token"]}
        created = client.put(f"/api/projects/{'a' * 32}/automation", json={"agent_rules": [rule]}, headers=headers)
        assert created.status_code == 200, created.text
        saved = created.json()["automation_config"]["agent_rules"]
        assert len(saved) == 1
        assert saved[0]["include_repository"] is True
        assert saved[0]["include_github_credentials"] is True
        assert saved[0]["skill_ids"] == ["releasing-version-docs"]
        assert len(saved[0]["id"]) == 32

        assert client.put(f"/api/projects/{'a' * 32}/automation", json={"agent_rules": [{**rule, "event": "push"}]}, headers=headers).status_code == 422
        assert client.put(f"/api/projects/{'a' * 32}/automation", json={"agent_rules": [{**rule, "agent_id": "0" * 32}]}, headers=headers).status_code == 422
        assert client.put(f"/api/projects/{'a' * 32}/automation", json={"agent_rules": [{**rule, "skill_ids": ["not-uploaded"]}]}, headers=headers).status_code == 422
        overflow = [{**rule, "name": f"规则{i}", "prompt": f"任务 {i}"} for i in range(21)]
        assert client.put(f"/api/projects/{'a' * 32}/automation", json={"agent_rules": overflow}, headers=headers).status_code == 422

        kept = client.put(f"/api/projects/{'a' * 32}/automation", json={"agent_id": agent_id}, headers=headers)
        assert kept.status_code == 200, kept.text
        rules = kept.json()["automation_config"]["agent_rules"]
        assert rules[0]["prompt"] == "审查这个 PR，通过则合并。"
        assert rules[0]["id"] == saved[0]["id"]


def test_web_review_policy_overrides_yaml_and_queues_pr(setup_db):
    settings, sessions, config_path = setup_db
    config_path.write_text("projects:\n  - repo: acme/mobile\n", encoding="utf-8")
    app = create_app(settings)
    with sessions() as db:
        set_admin(db, "admin", ADMIN_PASS)
        db.commit()
    agent_id = "f" * 32
    project_id = "a" * 32
    with TestClient(app) as client:
        headers = {"X-CSRF-Token": client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASS}).json()["csrf_token"]}
        before = client.get(f"/api/projects/{project_id}/connection", headers=headers)
        assert before.status_code == 200
        assert before.json()["automation"]["review"] is False
        assert before.json()["automation_config"]["review_enabled"] is False

        enabled = client.put(f"/api/projects/{project_id}/automation", json={
            "agent_id": agent_id,
            "review_enabled": True,
            "merge_enabled": False,
        }, headers=headers)
        assert enabled.status_code == 200, enabled.text
        body = enabled.json()
        assert body["automation"]["review"] is True
        assert body["automation"]["merge"] is False
        assert body["automation_config"]["review_enabled"] is True
        assert body["automation_config"]["merge_enabled"] is False

        with sessions() as db:
            cfg = project_config(settings, db, "acme/mobile", active_only=False)
            assert cfg.review_enabled is True
            record = upsert_github(db, cfg, "pr", pull_payload(), canonical=True)
            db.commit()
            assert record.status == "queued"
            assert db.scalar(select(Job).where(Job.kind == "review", Job.status == "queued")) is not None

        assert client.put(f"/api/projects/{project_id}/automation", json={
            "agent_id": agent_id,
            "review_enabled": True,
            "merge_enabled": True,
            "required_checks": ["flowdeck/agent-review"],
        }, headers=headers).status_code == 422
        assert client.put(f"/api/projects/{project_id}/automation", json={
            "agent_id": agent_id,
            "review_enabled": False,
            "merge_enabled": True,
            "required_checks": ["CI"],
        }, headers=headers).status_code == 422
        no_ci = client.put(f"/api/projects/{project_id}/automation", json={
            "agent_id": agent_id,
            "review_enabled": True,
            "merge_enabled": True,
            "required_checks": [],
        }, headers=headers)
        assert no_ci.status_code == 200, no_ci.text
        assert no_ci.json()["automation"]["merge"] is True
        assert no_ci.json()["automation_config"]["required_checks"] == []

        kept = client.put(f"/api/projects/{project_id}/automation", json={"agent_id": agent_id}, headers=headers)
        assert kept.status_code == 200, kept.text
        assert kept.json()["automation_config"]["review_enabled"] is True
        assert kept.json()["automation"]["review"] is True
