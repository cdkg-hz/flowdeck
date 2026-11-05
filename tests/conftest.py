import json
import os
from pathlib import Path
import pytest

# Windows pytest temp under the user profile often hits permission errors (pytest-of-*).
if os.name == "nt":
    _pytest_tmp = Path(__file__).resolve().parents[1] / ".tmp-pytest"
    _pytest_tmp.mkdir(exist_ok=True)
    os.environ["TMP"] = str(_pytest_tmp)
    os.environ["TEMP"] = str(_pytest_tmp)
    os.environ["TMPDIR"] = str(_pytest_tmp)
from backend.config import Settings
from backend.db import AgentConnection, Base, Project, ProjectAutomation, ProjectConnection, UploadedSkill, database
from backend.credentials import seal
from backend.projects import DEFAULT_EVENTS
from backend.skills import write_skill_dir
from backend.storage import prepare_project_storage
from cryptography.fernet import Fernet


@pytest.fixture
def setup_db(tmp_path):
    config = tmp_path / "projects.yml"
    config.write_text("projects:\n  - repo: acme/mobile\n    review_enabled: true\n    merge_enabled: true\n    required_checks: [CI]\n", encoding="utf-8")
    settings = Settings(_env_file=None, app_mode="live", database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}", data_root=tmp_path / "data", config_path=str(config), github_token="test-token", github_webhook_secret="test-webhook-secret", executor_webhook_secret="test-executor-secret")
    settings = settings.model_copy(update={"secrets_encryption_key": Fernet.generate_key().decode(), "session_secure": False})
    engine, sessions = database(settings.database_url)
    Base.metadata.create_all(engine)
    root = settings.data_root / "projects"
    root.mkdir(parents=True)
    project_id = "a" * 32
    storage = str(root / "mobile")
    prepare_project_storage(settings, project_id, storage)
    with sessions() as db:
        db.add(Project(id=project_id, repo="acme/mobile", name="Mobile"))
        db.flush()
        db.add(ProjectConnection(project_id=project_id, webhook_id="a" * 32, storage_path=storage,
            events=list(DEFAULT_EVENTS), storage_status="ready", api_status="ready", webhook_verified=True,
            repository_id="123", webhook_repository_id="123", revision=1, verified_revision=1, verify_requested=False,
            secret_ciphertext=seal(settings, project_id, "webhook", settings.github_webhook_secret),
            token_ciphertext=seal(settings, project_id, "github", settings.github_token)))
        agent_id = "f" * 32
        db.add(AgentConnection(id=agent_id, name="Test Agent", base_url="http://agent.test/v1", protocol="copilot",
                               api_key_ciphertext=seal(settings, "agent:" + agent_id, "api_key", "test-agent-key"),
                               model="gpt-5-codex", enabled=True))
        db.add(ProjectAutomation(project_id=project_id, config={"agent_id": agent_id}))
        skill_id = "e" * 32
        write_skill_dir(settings.data_root / "skills" / skill_id, {"SKILL.md": "# 发版统御\n只做输入门。\n".encode("utf-8")})
        db.add(UploadedSkill(id=skill_id, slug="releasing-version-docs", label="发版统御"))
        db.commit()
    yield settings, sessions, config
    engine.dispose()


def pull_payload(sha="a" * 40, **changes):
    value = {"id": 21, "number": 7, "title": "Fix retry", "body": "Fixes #3", "state": "open", "draft": False, "created_at": "2026-09-12T10:00:00Z", "updated_at": "2026-09-13T10:00:00Z", "html_url": "https://github.com/acme/mobile/pull/7", "head": {"sha": sha, "ref": "fix/retry"}, "base": {"sha": "b" * 40, "ref": "main"}, "assignees": [{"login": "dev"}], "labels": [{"name": "P1"}], "mergeable": True, "mergeable_state": "clean"}
    value.update(changes)
    return value
