from __future__ import annotations

import json
import httpx
import pytest
from sqlalchemy import func, select
from starlette.testclient import TestClient

from backend.auth import set_admin
from backend.config import ProjectConfig
from backend.db import Job, Project, ProjectAutomation, Record
from backend.github import GitHub
from backend.main import create_app
from backend.release_prepare import (
    DEFAULT_PREPARE_PROMPT,
    PREPARE_TAG_PATTERN,
    adopt_prepare_release,
    prepare_release_ready,
    queue_prepare_release,
    validate_prepare_tag,
)
from backend.service import upsert_github
from tests.test_agent_tasks import AGENT_ID, TaskAgent, TaskWorkspace, _rule

ADMIN_PASS = "admin-password-123"


def _release_obj(**changes):
    value = {
        "id": 77,
        "tag_name": "v3",
        "name": "v3",
        "body": "",
        "html_url": "https://github.com/acme/mobile/releases/tag/v3",
        "published_at": "2026-09-17T02:00:00Z",
        "created_at": "2026-09-17T02:00:00Z",
        "draft": False,
        "prerelease": False,
        "assets": [],
    }
    value.update(changes)
    return value


def test_validate_prepare_tag_accepts_semver_like_names():
    assert validate_prepare_tag(" v1.2.3 ") == "v1.2.3"
    assert PREPARE_TAG_PATTERN.fullmatch("1.0.0-rc1")
    with pytest.raises(ValueError, match="tag"):
        validate_prepare_tag("v 1")
    with pytest.raises(ValueError, match="tag"):
        validate_prepare_tag("../main")


def test_adopt_prepare_release_moves_legacy_release_event_rule():
    adopted = adopt_prepare_release({
        "agent_id": AGENT_ID,
        "agent_rules": [
            _rule(event="pull_request"),
            _rule(id="c" * 32, event="release", name="总结并发版", prompt="按技能总结并请求推送", skill_ids=["releasing-version-docs"]),
        ],
    })
    assert [rule["event"] for rule in adopted["agent_rules"]] == ["pull_request"]
    assert adopted["prepare_release"] == {
        "enabled": True,
        "agent_id": AGENT_ID,
        "prompt": "按技能总结并请求推送",
        "skill_ids": ["releasing-version-docs"],
    }


def test_adopt_prepare_release_keeps_later_release_event_rules():
    existing = {
        "prepare_release": {"enabled": True, "agent_id": AGENT_ID, "prompt": "固定说明", "skill_ids": []},
        "agent_rules": [_rule(event="release", name="webhook 文档", prompt="写发布说明")],
    }
    adopted = adopt_prepare_release(existing)
    assert adopted["prepare_release"]["prompt"] == "固定说明"
    assert adopted["agent_rules"][0]["name"] == "webhook 文档"


def test_prepare_release_ready_requires_enabled_agent_and_prompt():
    empty = ProjectConfig(repo="acme/mobile")
    assert prepare_release_ready(empty) is False
    config = ProjectConfig(repo="acme/mobile", prepare_release={"enabled": True, "agent_id": AGENT_ID, "prompt": "总结并请求推送"})
    assert prepare_release_ready(config) is True
    assert DEFAULT_PREPARE_PROMPT


def test_upsert_github_merges_prepare_record_by_tag(setup_db):
    settings, sessions, _ = setup_db
    cfg = ProjectConfig(repo="acme/mobile", release_enabled=True)
    with sessions() as db:
        record = Record(
            repo="acme/mobile", kind="release", external_id="prepare:v3", title="v3",
            status="queued", stage="release", details={"tag": "v3", "preparing": True},
        )
        db.add(record)
        db.flush()
        record_id = record.id
        merged = upsert_github(db, cfg, "release", _release_obj(), "published", canonical=True)
        db.commit()
        assert merged.id == record_id
        assert merged.external_id == "77"
        assert merged.details["release_id"] == 77
        assert merged.details["tag"] == "v3"
        assert db.scalar(select(func.count()).select_from(Record).where(Record.kind == "release")) == 1
        jobs = list(db.scalars(select(Job).where(Job.kind == "release")))
        assert len(jobs) == 1
        assert jobs[0].payload["tag"] == "v3"


def _login(client: TestClient) -> dict:
    login = client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASS})
    assert login.status_code == 200
    return {"X-CSRF-Token": login.json()["csrf_token"]}


def _install_prepare_release(sessions, **changes):
    from backend.db import ProjectAutomation
    feature = {"enabled": True, "agent_id": AGENT_ID, "prompt": "按技能总结并请求推送", "skill_ids": ["releasing-version-docs"]}
    feature.update(changes)
    with sessions() as db:
        item = db.get(ProjectAutomation, "a" * 32)
        item.config = {**(item.config or {}), "prepare_release": feature}
        db.commit()


def test_prepare_release_api_validates_rules_tag_and_overlap(setup_db, monkeypatch):
    settings, sessions, _ = setup_db
    with sessions() as db:
        set_admin(db, "admin", ADMIN_PASS)
        db.commit()

    async def fake_inspect(github, repo, tag, base_branches):
        return {"default_branch": "main"}

    monkeypatch.setattr("backend.release_prepare.inspect_github_prepare", fake_inspect)
    app = create_app(settings)
    with TestClient(app) as client:
        headers = _login(client)
        missing = client.post(f"/api/projects/{'a' * 32}/prepare-release", json={"tag": "v3"}, headers=headers)
        assert missing.status_code == 422
        assert "总结并发布" in missing.json()["detail"]

        _install_prepare_release(sessions)
        bad = client.post(f"/api/projects/{'a' * 32}/prepare-release", json={"tag": "bad tag"}, headers=headers)
        assert bad.status_code == 422

        created = client.post(f"/api/projects/{'a' * 32}/prepare-release", json={"tag": "v3"}, headers=headers)
        assert created.status_code == 202, created.text
        body = created.json()
        assert body["status"] == "queued"
        assert body["record_id"]
        duplicate = client.post(f"/api/projects/{'a' * 32}/prepare-release", json={"tag": "v4"}, headers=headers)
        assert duplicate.status_code == 409

    with sessions() as db:
        record = db.scalar(select(Record).where(Record.kind == "release"))
        job = db.scalar(select(Job).where(Job.kind == "prepare_release"))
        assert record.external_id == "prepare:v3"
        assert record.details["tag"] == "v3"
        assert record.details["base_ref"] == "main"
        assert job.payload["tag"] == "v3"
        assert job.payload["default_branch"] == "main"
        assert job.payload["agent_id"] == AGENT_ID
        assert job.payload["prompt"] == "按技能总结并请求推送"
        assert job.payload["skill_ids"] == ["releasing-version-docs"]


def test_prepare_release_api_rejects_existing_remote_tag(setup_db, monkeypatch):
    settings, sessions, _ = setup_db
    with sessions() as db:
        set_admin(db, "admin", ADMIN_PASS)
        db.commit()
    _install_prepare_release(sessions)

    async def exists(github, repo, tag, base_branches):
        raise ValueError("远程已存在该 tag")

    monkeypatch.setattr("backend.release_prepare.inspect_github_prepare", exists)
    with TestClient(create_app(settings)) as client:
        headers = _login(client)
        response = client.post(f"/api/projects/{'a' * 32}/prepare-release", json={"tag": "v3"}, headers=headers)
        assert response.status_code == 409
        assert "tag" in response.json()["detail"]


def test_prepare_release_api_rejects_demo_project(setup_db, monkeypatch):
    settings, sessions, _ = setup_db
    with sessions() as db:
        set_admin(db, "admin", ADMIN_PASS)
        db.commit()
    _install_prepare_release(sessions)

    async def fake_inspect(github, repo, tag, base_branches):
        return {"default_branch": "main"}

    monkeypatch.setattr("backend.release_prepare.inspect_github_prepare", fake_inspect)
    with TestClient(create_app(settings)) as client:
        headers = _login(client)
        with sessions() as db:
            project = db.get(Project, "a" * 32)
            project.demo = True
            db.commit()
        response = client.post(f"/api/projects/{'a' * 32}/prepare-release", json={"tag": "v3"}, headers=headers)
        assert response.status_code in {400, 404, 409}


class PrepareGitHub:
    def __init__(self, shas, *, created=None):
        self.shas = shas
        self.created = created
        self.create_calls = []
        self.releases = []

    async def pages(self, path, params=None, max_pages=100, list_key=None):
        for item in self.releases:
            yield item

    async def commit(self, repo, ref):
        return {"sha": self.shas[ref]}

    async def tag_exists(self, repo, tag):
        return False

    async def release_by_tag(self, repo, tag):
        if self.created and self.created.get("tag_name") == tag:
            return self.created
        return None

    async def create_release(self, repo, tag, sha, name=""):
        self.create_calls.append((repo, tag, sha, name or tag))
        if self.created:
            return self.created
        self.created = _release_obj(tag_name=tag, name=name or tag, target_commitish=sha)
        return self.created


async def test_prepare_release_pushes_default_branch_then_creates_github_release(setup_db, tmp_path, monkeypatch):
    from backend.worker import Worker
    from backend.projects import project_config

    settings, sessions, config = setup_db
    config.write_text("projects:\n  - repo: acme/mobile\n    release_enabled: true\n    base_branches: [main]\n", encoding="utf-8")
    _install_prepare_release(sessions)
    workspaces = TaskWorkspace(tmp_path / "workspaces")
    github = PrepareGitHub({"main": "a" * 40})
    agent = TaskAgent({"summary": "已总结", "outcome": "completed", "release_docs": {"paths": ["docs/notes.md"], "push_requested": True}})
    pushes = []

    def fake_push(root, env, branch, message, paths):
        pushes.append((str(root), branch, list(paths)))
        return {"pushed": True, "branch": branch, "sha": "d" * 40}

    monkeypatch.setattr("backend.agent_actions.commit_and_push", fake_push)
    original_prepare = workspaces.prepare_worktree

    async def prepare_worktree(task_id, repo, *, sha, branch):
        result = await original_prepare(task_id, repo, sha=sha, branch=branch)
        notes = workspaces.task_dir(task_id) / "work" / "docs"
        notes.mkdir(parents=True, exist_ok=True)
        (notes / "notes.md").write_text("notes\n", encoding="utf-8")
        return result

    workspaces.prepare_worktree = prepare_worktree
    worker = Worker(settings, sessions=sessions, github=github, agent=agent, workspaces=workspaces)
    with sessions() as db:
        cfg = project_config(settings, db, "acme/mobile")
        record, job = queue_prepare_release(db, cfg, tag="v3", default_branch="main")
        db.commit()
        job_id = job.id
        record_id = record.id
    await worker.run_job(worker.claim())
    assert pushes and pushes[0][1] == "main"
    assert github.create_calls == [("acme/mobile", "v3", "d" * 40, "v3")]
    assert agent.last_input["release"]["tag"] == "v3"
    with sessions() as db:
        job = db.get(Job, job_id)
        record = db.get(Record, record_id)
        assert job.status == "completed"
        assert record.external_id == "77"
        assert record.details["release_id"] == 77
        release_jobs = list(db.scalars(select(Job).where(Job.kind == "release")))
        assert len(release_jobs) == 1
        assert release_jobs[0].payload["tag"] == "v3"


class CachingTaskAgent(TaskAgent):
    def __init__(self, payload):
        super().__init__(payload)
        self.seen_states = []

    async def execute(self, task_type, task_id, instructions, input_text, state, save):
        self.seen_states.append(dict(state or {}))
        if state and state.get("output") is not None:
            return state["output"]
        return await super().execute(task_type, task_id, instructions, input_text, state, save)


async def test_prepare_release_ignores_cached_failed_agent_output(setup_db, tmp_path, monkeypatch):
    from backend.worker import Worker
    from backend.projects import project_config

    settings, sessions, config = setup_db
    config.write_text("projects:\n  - repo: acme/mobile\n    release_enabled: true\n    base_branches: [main]\n", encoding="utf-8")
    _install_prepare_release(sessions)
    workspaces = TaskWorkspace(tmp_path / "workspaces")
    github = PrepareGitHub({"main": "a" * 40})
    cached = json.dumps({
        "summary": "当前环境未配置文件读取与写入工具",
        "outcome": "failed",
        "pull_request": None,
        "release_docs": {"push_requested": False, "paths": []},
    }, ensure_ascii=False)
    agent = CachingTaskAgent({"summary": "已总结", "outcome": "completed", "release_docs": {"paths": ["docs/notes.md"], "push_requested": True}})
    pushes = []

    def fake_push(root, env, branch, message, paths):
        pushes.append(list(paths))
        return {"pushed": True, "branch": branch, "sha": "d" * 40}

    monkeypatch.setattr("backend.agent_actions.commit_and_push", fake_push)

    async def prepare_worktree(task_id, repo, *, sha, branch):
        notes = workspaces.task_dir(task_id) / "work" / "docs"
        notes.mkdir(parents=True, exist_ok=True)
        (notes / "notes.md").write_text("notes\n", encoding="utf-8")
        return {"branch": branch, "sha": sha}

    workspaces.prepare_worktree = prepare_worktree
    worker = Worker(settings, sessions=sessions, github=github, agent=agent, workspaces=workspaces)
    with sessions() as db:
        cfg = project_config(settings, db, "acme/mobile")
        record, job = queue_prepare_release(db, cfg, tag="v3", default_branch="main")
        job.result = {**(job.result or {}), "agent_agent_task": {"thread_id": "old", "output": cached}}
        db.commit()
        job_id = job.id
    await worker.run_job(worker.claim())
    assert agent.seen_states and "output" not in agent.seen_states[0]
    assert pushes == [["docs/notes.md"]]
    with sessions() as db:
        assert db.get(Job, job_id).status == "completed"


async def test_prepare_release_retries_without_duplicating_github_release(setup_db, tmp_path, monkeypatch):
    from backend.worker import Worker
    from backend.projects import project_config

    settings, sessions, config = setup_db
    config.write_text("projects:\n  - repo: acme/mobile\n    release_enabled: true\n", encoding="utf-8")
    _install_prepare_release(sessions)
    existing = _release_obj()
    github = PrepareGitHub({"main": "a" * 40}, created=existing)
    workspaces = TaskWorkspace(tmp_path / "workspaces")
    agent = TaskAgent({"summary": "已总结", "outcome": "completed", "release_docs": {"paths": ["docs/notes.md"], "push_requested": True}})
    monkeypatch.setattr("backend.agent_actions.commit_and_push", lambda *args, **kwargs: {"pushed": True, "branch": "main", "sha": "d" * 40})

    async def prepare_worktree(task_id, repo, *, sha, branch):
        notes = workspaces.task_dir(task_id) / "work" / "docs"
        notes.mkdir(parents=True, exist_ok=True)
        (notes / "notes.md").write_text("notes\n", encoding="utf-8")
        return {"branch": branch, "sha": sha}

    workspaces.prepare_worktree = prepare_worktree
    worker = Worker(settings, sessions=sessions, github=github, agent=agent, workspaces=workspaces)
    with sessions() as db:
        cfg = project_config(settings, db, "acme/mobile")
        record, job = queue_prepare_release(db, cfg, tag="v3", default_branch="main")
        job.result = {"github_release": existing, "pushed": {"sha": "d" * 40, "branch": "main"}}
        db.commit()
        job_id = job.id
        record_id = record.id
    await worker.run_job(worker.claim())
    assert github.create_calls == []
    with sessions() as db:
        assert db.get(Job, job_id).status == "completed"
        assert db.get(Record, record_id).external_id == "77"


def test_connection_adopts_legacy_release_rule_and_lists_flag(setup_db):
    settings, sessions, _ = setup_db
    with sessions() as db:
        set_admin(db, "admin", ADMIN_PASS)
        item = db.get(ProjectAutomation, "a" * 32)
        item.config = {
            **(item.config or {}),
            "agent_rules": [_rule(event="release", name="总结并发版", prompt="按技能总结并请求推送", skill_ids=["releasing-version-docs"])],
        }
        db.commit()
    with TestClient(create_app(settings)) as client:
        headers = _login(client)
        listed = client.get("/api/projects", headers=headers)
        assert listed.status_code == 200
        assert listed.json()[0]["prepare_release"] is True
        connection = client.get(f"/api/projects/{'a' * 32}/connection", headers=headers)
        assert connection.status_code == 200
        body = connection.json()
        assert [rule["event"] for rule in body["automation_config"]["agent_rules"]] == []
        assert body["automation_config"]["prepare_release"]["enabled"] is True
        assert body["automation_config"]["prepare_release"]["prompt"] == "按技能总结并请求推送"
        assert body["automation_config"]["prepare_release"]["skill_ids"] == ["releasing-version-docs"]
    with sessions() as db:
        saved = db.get(ProjectAutomation, "a" * 32).config
        assert saved["prepare_release"]["enabled"] is True
        assert saved["agent_rules"] == []


def test_prepare_release_put_round_trip_and_later_put_keeps_it(setup_db):
    settings, sessions, _ = setup_db
    with sessions() as db:
        set_admin(db, "admin", ADMIN_PASS)
        db.commit()
    payload = {
        "enabled": True,
        "agent_id": AGENT_ID,
        "prompt": "按仓库技能总结并请求推送",
        "skill_ids": ["releasing-version-docs"],
    }
    with TestClient(create_app(settings)) as client:
        headers = _login(client)
        saved = client.put(f"/api/projects/{'a' * 32}/automation", json={"prepare_release": payload}, headers=headers)
        assert saved.status_code == 200, saved.text
        feature = saved.json()["automation_config"]["prepare_release"]
        assert feature == payload
        listed = client.get("/api/projects", headers=headers).json()
        assert listed[0]["prepare_release"] is True
        kept = client.put(f"/api/projects/{'a' * 32}/automation", json={"agent_id": AGENT_ID}, headers=headers)
        assert kept.status_code == 200, kept.text
        assert kept.json()["automation_config"]["prepare_release"] == payload
        disabled = client.put(
            f"/api/projects/{'a' * 32}/automation",
            json={"prepare_release": {**payload, "enabled": False}},
            headers=headers,
        )
        assert disabled.status_code == 200
        assert client.get("/api/projects", headers=headers).json()[0]["prepare_release"] is False
        missing_prompt = client.put(
            f"/api/projects/{'a' * 32}/automation",
            json={"prepare_release": {**payload, "prompt": ""}},
            headers=headers,
        )
        assert missing_prompt.status_code == 422


async def test_github_create_release_skips_when_tag_release_exists():
    settings_calls = []

    class FakeClient:
        async def request(self, method, url, **kwargs):
            settings_calls.append((method, url, kwargs.get("json")))
            request = httpx.Request(method, url)
            if method == "GET" and "/releases/tags/" in url:
                return httpx.Response(200, json=_release_obj(), request=request)
            return httpx.Response(201, json=_release_obj(id=1), request=request)

        async def aclose(self):
            return None

    from backend.config import Settings
    github = GitHub(Settings(_env_file=None, github_token="t"), client=FakeClient())
    github._token = lambda repo=None: "t"
    created = await github.create_release("acme/mobile", "v3", "d" * 40)
    assert created["id"] == 77
    assert not any(method == "POST" for method, _url, _body in settings_calls)
