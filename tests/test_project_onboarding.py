from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.auth import set_admin
from backend.config import Settings
from backend.credentials import unseal
from backend.db import InboxEvent, LoginAttempt, ProjectConnection
from backend.errors import ConfigurationNeeded
from backend.main import create_app
from backend.projects import verify_connections


ADMIN = "flowdeck-admin"
PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def onboarding(tmp_path, monkeypatch):
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    config = tmp_path / "projects.yml"
    config.write_text("projects: []\n", encoding="utf-8")
    settings = Settings(
        _env_file=None,
        app_mode="live",
        database_url=f"sqlite:///{(tmp_path / 'onboarding.db').as_posix()}",
        config_path=str(config),
        data_root=tmp_path / "data",
        storage_roots=[str(storage_root)],
        secrets_encryption_key=Fernet.generate_key().decode(),
        session_secure=False,
        webhook_public_base_url="https://hooks.example.test",
    )
    hooks = []

    def fake_ensure(**kwargs):
        hooks.append(kwargs)
        return "101"

    monkeypatch.setattr("backend.github.ensure_repository_webhook", fake_ensure)
    app = create_app(settings)
    with TestClient(app) as client:
        with app.state.Session() as db:
            set_admin(db, ADMIN, PASSWORD)
        yield SimpleNamespace(client=client, settings=settings, sessions=app.state.Session, root=storage_root, hooks=hooks)


def login(client: TestClient, username: str = ADMIN, password: str = PASSWORD) -> str:
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["authenticated"] is True
    assert body["username"] == username
    assert body["csrf_token"]
    return body["csrf_token"]


def create_project(
    harness,
    csrf: str,
    repo: str,
    directory: str,
    *,
    events: list[str] | None = None,
    token: str | None = None,
):
    body = {
        "name": repo.split("/")[-1].title(),
        "description": f"Automation for {repo}",
        "repo": repo,
        "storage_path": str(harness.root / directory),
        "events": events or ["issues", "pull_request", "release"],
    }
    if token is not None:
        body["github_token"] = token
    response = harness.client.post("/api/projects", json=body, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 201, response.text
    return response.json()


def signed(payload: dict, secret: str, delivery: str, event: str):
    raw = json.dumps(payload, separators=(",", ":")).encode()
    signature = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {
        "content-type": "application/json",
        "x-hub-signature-256": signature,
        "x-github-delivery": delivery,
        "x-github-event": event,
    }


def reveal_secret(harness, project_id: str, csrf: str) -> str:
    response = harness.client.post(
        f"/api/projects/{project_id}/webhook-secret/reveal",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text
    return response.json()["secret"]


def ping(harness, project: dict, secret: str, repository_id: int, delivery: str):
    raw, headers = signed(
        {"zen": "Keep it logically awesome.", "repository": {"full_name": project["repo"], "id": repository_id}},
        secret,
        delivery,
        "ping",
    )
    return harness.client.post(f"/api/webhooks/github/{project['webhook_url'].rsplit('/', 1)[-1]}", content=raw, headers=headers)


def test_unauthenticated_reads_and_csrf_boundaries(onboarding):
    client = onboarding.client
    assert client.get("/api/projects").status_code == 200
    assert client.get("/api/project-options").status_code == 401
    unauthenticated_body = {
        "name": "Unauthorized",
        "repo": "acme/unauthorized",
        "storage_path": str(onboarding.root / "unauthorized"),
        "events": ["issues"],
    }
    assert client.post("/api/projects", json=unauthenticated_body).status_code == 401

    csrf = login(client)
    options = client.get("/api/project-options")
    assert options.status_code == 200
    assert options.json()["management_enabled"] is True
    assert options.json()["webhook_public_base_url"] == "https://hooks.example.test"

    body = {
        "name": "One",
        "repo": "acme/one",
        "storage_path": str(onboarding.root / "one"),
        "events": ["issues"],
    }
    assert client.post("/api/projects", json=body).status_code == 403
    assert client.post("/api/projects", json=body, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    created = client.post("/api/projects", json=body, headers={"X-CSRF-Token": csrf})
    assert created.status_code == 201

    assert client.post("/api/auth/logout").status_code == 403
    assert client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 200
    assert client.get("/api/auth/session").json() == {
        "authenticated": False,
        "username": None,
        "csrf_token": None,
        "admin_configured": True,
        "require_login": False,
    }
    assert client.get(f"/api/projects/{created.json()['id']}/connection").status_code == 401


def test_login_rate_limit_success_reset_and_admin_reset(onboarding):
    client = onboarding.client
    for _ in range(4):
        assert client.post("/api/auth/login", json={"username": ADMIN, "password": "wrong-password"}).status_code == 401
    csrf = login(client)
    with onboarding.sessions() as db:
        assert db.scalar(select(func.count()).select_from(LoginAttempt)) == 0

    with onboarding.sessions() as db:
        set_admin(db, "replacement-admin", "replacement-password-123")
    assert client.get("/api/auth/session").json()["authenticated"] is False
    assert client.post("/api/auth/login", json={"username": ADMIN, "password": PASSWORD}).status_code == 401
    csrf = login(client, "replacement-admin", "replacement-password-123")
    assert client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 200

    for _ in range(5):
        assert client.post("/api/auth/login", json={"username": "replacement-admin", "password": "wrong-password"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "replacement-admin", "password": "wrong-password"}).status_code == 429
    with onboarding.sessions() as db:
        assert db.scalar(select(func.count()).select_from(LoginAttempt)) == 5


def test_creation_duplicates_storage_overlap_and_secret_disclosure(onboarding):
    csrf = login(onboarding.client)
    first = create_project(onboarding, csrf, "Acme/One", "one", events=["issues"])
    assert first["repo"] == "acme/one"
    assert first["token_configured"] is False
    assert first["api_status"] == "missing"
    assert first["storage_status"] == "pending"
    assert "secret" not in first and "github_token" not in first

    duplicate = onboarding.client.post(
        "/api/projects",
        json={
            "name": "Duplicate",
            "repo": "acme/ONE",
            "storage_path": str(onboarding.root / "other"),
            "events": ["issues"],
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert duplicate.status_code == 409

    overlap = onboarding.client.post(
        "/api/projects",
        json={
            "name": "Nested",
            "repo": "acme/nested",
            "storage_path": str(onboarding.root / "one" / "nested"),
            "events": ["issues"],
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert overlap.status_code == 422

    token = "github_pat_project_specific_secret_value"
    second = create_project(onboarding, csrf, "acme/two", "two", token=token)
    assert second["token_configured"] is True
    assert second["api_status"] == "pending"

    assert onboarding.client.post(f"/api/projects/{first['id']}/webhook-secret/reveal").status_code == 403
    secret = reveal_secret(onboarding, first["id"], csrf)
    assert secret
    public = onboarding.client.get("/api/projects").json()
    sensitive = {"storage_path", "repository_path", "workspaces_path", "secret", "secret_ciphertext", "github_token", "token_ciphertext"}
    assert all(not sensitive.intersection(project) for project in public)
    assert token not in json.dumps(public)
    assert secret not in json.dumps(public)
    assert secret not in onboarding.client.get("/api/integrations").text

    with onboarding.sessions() as db:
        first_connection = db.get(ProjectConnection, first["id"])
        second_connection = db.get(ProjectConnection, second["id"])
        assert secret not in first_connection.secret_ciphertext
        assert token not in second_connection.token_ciphertext
        assert unseal(onboarding.settings, first["id"], "webhook", first_connection.secret_ciphertext) == secret
        assert unseal(onboarding.settings, second["id"], "github", second_connection.token_ciphertext) == token
        with pytest.raises(ConfigurationNeeded):
            unseal(onboarding.settings, second["id"], "webhook", first_connection.secret_ciphertext)
        with pytest.raises(ConfigurationNeeded):
            unseal(onboarding.settings, first["id"], "webhook", second_connection.token_ciphertext)


async def test_events_wait_until_all_three_readiness_checks_pass(onboarding):
    csrf = login(onboarding.client)
    project = create_project(onboarding, csrf, "acme/events", "events", events=["issues"], token="project-events-token")
    secret = reveal_secret(onboarding, project["id"], csrf)
    endpoint = f"/api/webhooks/github/{project['webhook_url'].rsplit('/', 1)[-1]}"

    issue_payload = {"repository": {"full_name": "acme/events", "id": 101}, "issue": {"id": 501}}
    raw, headers = signed(issue_payload, secret, "issue-before-ready", "issues")
    response = onboarding.client.post(endpoint, content=raw, headers=headers)
    assert response.status_code == 202
    assert response.json() == {"accepted": True, "duplicate": False}
    with onboarding.sessions() as db:
        waiting = db.scalar(select(InboxEvent).where(InboxEvent.event_type == "issues"))
        assert waiting.processed_at is None
        assert db.get(ProjectConnection, project["id"]).webhook_verified is False

    release_payload = {"repository": {"full_name": "acme/events", "id": 101}, "release": {"id": 601}}
    raw, headers = signed(release_payload, secret, "ignored-release", "release")
    ignored = onboarding.client.post(endpoint, content=raw, headers=headers)
    assert ignored.status_code == 202
    assert ignored.json() == {"accepted": True, "ignored": True}
    with onboarding.sessions() as db:
        assert db.scalar(select(func.count()).select_from(InboxEvent)) == 1

    assert ping(onboarding, project, secret, 101, "initial-ping").status_code == 202
    before = onboarding.client.get(f"/api/projects/{project['id']}/connection").json()
    assert before["webhook_status"] == "ready"
    assert before["storage_status"] == "pending"
    assert before["api_status"] == "pending"
    assert before["status"] == "pending"

    calls = []

    async def verifier(_settings, token, repo, events):
        calls.append((token, repo, events))
        return "101"

    assert await verify_connections(onboarding.settings, onboarding.sessions, verifier=verifier) == 1
    assert calls == [("project-events-token", "acme/events", ["issues"])]
    ready = onboarding.client.get(f"/api/projects/{project['id']}/connection").json()
    assert {key: ready[key] for key in ("storage_status", "api_status", "webhook_status", "status")} == {
        "storage_status": "ready",
        "api_status": "ready",
        "webhook_status": "ready",
        "status": "active",
    }


async def test_token_update_fences_stale_verification_result(onboarding):
    csrf = login(onboarding.client)
    project = create_project(onboarding, csrf, "acme/fenced", "fenced", events=["issues"], token="old-project-token")
    secret = reveal_secret(onboarding, project["id"], csrf)
    assert ping(onboarding, project, secret, 202, "fenced-ping").status_code == 202

    async def stale_verifier(_settings, token, repo, events):
        assert (token, repo, events) == ("old-project-token", "acme/fenced", ["issues"])
        updated = onboarding.client.put(
            f"/api/projects/{project['id']}/github-token",
            json={"github_token": "new-project-token"},
            headers={"X-CSRF-Token": csrf},
        )
        assert updated.status_code == 200, updated.text
        return "202"

    assert await verify_connections(onboarding.settings, onboarding.sessions, verifier=stale_verifier) == 0
    with onboarding.sessions() as db:
        connection = db.get(ProjectConnection, project["id"])
        assert connection.revision == 2
        assert connection.verified_revision == 0
        assert connection.api_status == "pending"
        assert connection.verify_requested is True
        assert unseal(onboarding.settings, project["id"], "github", connection.token_ciphertext) == "new-project-token"

    seen = []

    async def current_verifier(_settings, token, repo, events):
        seen.append(token)
        return "202"

    assert await verify_connections(onboarding.settings, onboarding.sessions, verifier=current_verifier) == 1
    assert seen == ["new-project-token"]
    connection = onboarding.client.get(f"/api/projects/{project['id']}/connection").json()
    assert connection["status"] == "active"
    assert connection["api_status"] == "ready"


async def test_webhooks_are_isolated_by_secret_and_repository_identity(onboarding):
    csrf = login(onboarding.client)
    one = create_project(onboarding, csrf, "acme/one", "one", events=["issues"], token="token-one")
    two = create_project(onboarding, csrf, "acme/two", "two", events=["issues"], token="token-two")
    secret_one = reveal_secret(onboarding, one["id"], csrf)
    secret_two = reveal_secret(onboarding, two["id"], csrf)
    endpoint_one = f"/api/webhooks/github/{one['webhook_url'].rsplit('/', 1)[-1]}"

    payload_one = {"repository": {"full_name": "acme/one", "id": 301}}
    raw, headers = signed(payload_one, secret_two, "cross-secret", "ping")
    assert onboarding.client.post(endpoint_one, content=raw, headers=headers).status_code == 401

    cross_repo = {"repository": {"full_name": "acme/two", "id": 302}}
    raw, headers = signed(cross_repo, secret_one, "cross-repo", "ping")
    assert onboarding.client.post(endpoint_one, content=raw, headers=headers).status_code == 403

    for delivery, payload in [
        ("no-repository", {}),
        ("no-id", {"repository": {"full_name": "acme/one"}}),
        ("string-id", {"repository": {"full_name": "acme/one", "id": "301"}}),
    ]:
        raw, headers = signed(payload, secret_one, delivery, "ping")
        assert onboarding.client.post(endpoint_one, content=raw, headers=headers).status_code == 400

    ids = {"acme/one": "301", "acme/two": "302"}

    async def verifier(_settings, _token, repo, _events):
        return ids[repo]

    assert await verify_connections(onboarding.settings, onboarding.sessions, verifier=verifier) == 0
    assert onboarding.client.get(f"/api/projects/{one['id']}/connection").json()["status"] == "pending"
    assert onboarding.client.get(f"/api/projects/{two['id']}/connection").json()["status"] == "pending"

    wrong_id = {"repository": {"full_name": "acme/one", "id": 999}}
    raw, headers = signed(wrong_id, secret_one, "wrong-id", "ping")
    assert onboarding.client.post(endpoint_one, content=raw, headers=headers).status_code == 403

    assert ping(onboarding, one, secret_one, 301, "one-ready").json()["accepted"] is True
    assert ping(onboarding, two, secret_two, 302, "two-ready").json()["accepted"] is True
    assert onboarding.client.get(f"/api/projects/{one['id']}/connection").json()["status"] == "active"
    assert onboarding.client.get(f"/api/projects/{two['id']}/connection").json()["status"] == "active"
    assert onboarding.client.post("/api/webhooks/github/" + "f" * 32, content=b"{}", headers={}).status_code == 404


def test_demo_login_allows_preview_but_all_mutations_refuse(tmp_path):
    config = tmp_path / "projects.yml"
    config.write_text("projects: []\n", encoding="utf-8")
    root = tmp_path / "storage"
    root.mkdir()
    settings = Settings(
        _env_file=None,
        app_mode="demo",
        database_url=f"sqlite:///{(tmp_path / 'demo.db').as_posix()}",
        config_path=str(config),
        data_root=tmp_path / "data",
        storage_roots=[str(root)],
        secrets_encryption_key=Fernet.generate_key().decode(),
        session_secure=False,
        webhook_public_base_url="https://hooks.example.test",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        with app.state.Session() as db:
            set_admin(db, ADMIN, PASSWORD)
        csrf = login(client)
        options = client.get("/api/project-options").json()
        assert options["mode"] == "demo"
        assert options["management_enabled"] is False
        projects = client.get("/api/projects").json()
        assert projects and all(project["status"] == "demo" for project in projects)
        demo_id = projects[0]["id"]
        headers = {"X-CSRF-Token": csrf}

        create = client.post(
            "/api/projects",
            json={"name": "Nope", "repo": "acme/nope", "storage_path": str(root / "nope"), "events": ["issues"]},
            headers=headers,
        )
        assert create.status_code == 409
        assert client.put(f"/api/projects/{demo_id}/github-token", json={"github_token": "token"}, headers=headers).status_code == 409
        assert client.put(f"/api/projects/{demo_id}/storage", json={"storage_path": str(root / "other")}, headers=headers).status_code == 409
        assert client.post(f"/api/projects/{demo_id}/verify", headers=headers).status_code == 409
        assert client.post(f"/api/projects/{demo_id}/webhook-secret/reveal", headers=headers).status_code == 409
        assert client.post("/api/webhooks/github/" + "a" * 32, json={}).status_code == 409


async def test_storage_update_lock_and_missing_root_remain_pending(onboarding):
    csrf = login(onboarding.client)
    movable = create_project(onboarding, csrf, "acme/movable", "movable", token="movable-token")
    moved_path = onboarding.root / "moved"
    moved = onboarding.client.put(
        f"/api/projects/{movable['id']}/storage",
        json={"storage_path": str(moved_path)},
        headers={"X-CSRF-Token": csrf},
    )
    assert moved.status_code == 200
    assert Path(moved.json()["storage_path"]) == moved_path.resolve()
    assert moved.json()["storage_status"] == "pending"

    with onboarding.sessions() as db:
        connection = db.get(ProjectConnection, movable["id"])
        connection.storage_locked = True
        db.commit()
    locked = onboarding.client.put(
        f"/api/projects/{movable['id']}/storage",
        json={"storage_path": str(onboarding.root / "locked-change")},
        headers={"X-CSRF-Token": csrf},
    )
    assert locked.status_code == 409
    assert "不支持迁移" in locked.json()["detail"]

    missing = create_project(onboarding, csrf, "acme/missing-root", "missing-root", token="missing-root-token")
    onboarding.root.rmdir()

    async def verifier(_settings, _token, repo, _events):
        return {"acme/movable": "401", "acme/missing-root": "402"}[repo]

    assert await verify_connections(onboarding.settings, onboarding.sessions, verifier=verifier) == 0
    state = onboarding.client.get(f"/api/projects/{missing['id']}/connection").json()
    assert state["storage_status"] == "error"
    assert state["api_status"] == "ready"
    assert state["status"] == "pending"
    with onboarding.sessions() as db:
        connection = db.get(ProjectConnection, missing["id"])
        assert connection.verify_requested is True


def _actions_secret_client(monkeypatch, *, puts, get_status=200, put_status=204, get_body=None):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None):
            assert "Authorization" in (headers or {})
            if get_status != 200:
                request = httpx.Request("GET", url)
                return httpx.Response(get_status, json={"message": "forbidden"}, request=request)
            return httpx.Response(200, json=get_body or {"key_id": "key-1", "key": "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE="}, request=httpx.Request("GET", url))

        def put(self, url, headers=None, json=None):
            puts.append({"url": str(url), "json": json, "headers": headers})
            request = httpx.Request("PUT", url)
            if put_status >= 400:
                return httpx.Response(put_status, json={"message": "denied"}, request=request)
            return httpx.Response(put_status, request=request)

    monkeypatch.setattr(httpx, "Client", FakeClient)


def test_create_project_writes_actions_secrets_to_github_and_does_not_store_them(onboarding, monkeypatch):
    csrf = login(onboarding.client)
    puts = []
    _actions_secret_client(monkeypatch, puts=puts)
    password = "acr-password-value"
    keystore = "dGVzdC1rZXlzdG9yZQ=="
    response = onboarding.client.post(
        "/api/projects",
        json={
            "name": "Mobile",
            "repo": "acme/mobile-ci",
            "storage_path": str(onboarding.root / "mobile-ci"),
            "events": ["release"],
            "github_token": "github_pat_ci_writer",
            "docker_registry_username": "acr-user",
            "docker_registry_password": password,
            "android_keystore_base64": keystore,
            "android_key_alias": "upload",
            "android_keystore_password": "store-pass",
            "android_key_password": "key-pass",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    dumped = json.dumps(body)
    assert password not in dumped
    assert keystore not in dumped
    assert "store-pass" not in dumped
    names = {item["url"].rstrip("/").rsplit("/", 1)[-1] for item in puts}
    assert names == {
        "DOCKER_REGISTRY_USERNAME",
        "DOCKER_REGISTRY_PASSWORD",
        "ANDROID_KEYSTORE_BASE64",
        "ANDROID_KEY_ALIAS",
        "ANDROID_KEYSTORE_PASSWORD",
        "ANDROID_KEY_PASSWORD",
    }
    assert all(item["json"].get("encrypted_value") and item["json"].get("key_id") == "key-1" for item in puts)
    assert all(password not in item["json"]["encrypted_value"] for item in puts)
    with onboarding.sessions() as db:
        connection = db.get(ProjectConnection, body["id"])
        assert password not in json.dumps(connection.__dict__, default=str)
        assert keystore not in json.dumps(connection.__dict__, default=str)


def test_create_project_without_docker_or_android_secrets_skips_github_secret_write(onboarding, monkeypatch):
    csrf = login(onboarding.client)
    called = []

    def fake_put(*args, **kwargs):
        called.append((args, kwargs))

    monkeypatch.setattr("backend.github.put_repository_secrets", fake_put)
    project = create_project(
        onboarding, csrf, "acme/notes-only", "notes-only", events=["release"], token="github_pat_notes"
    )
    assert project["id"]
    assert called == []


def test_project_create_does_not_require_docker_or_android_fields():
    from backend.projects import ProjectCreate

    body = ProjectCreate(name="Notes", repo="acme/notes", storage_path="/tmp/notes", events=["release"])
    assert body.actions_secrets() == {}
    required = set(ProjectCreate.model_json_schema().get("required", []))
    assert required.isdisjoint(
        {
            "docker_registry_username",
            "docker_registry_password",
            "android_keystore_base64",
            "android_key_alias",
            "android_keystore_password",
            "android_key_password",
        }
    )


def test_create_project_rejects_partial_ci_secrets_and_github_write_failures(onboarding, monkeypatch):
    csrf = login(onboarding.client)
    partial = onboarding.client.post(
        "/api/projects",
        json={
            "name": "Partial",
            "repo": "acme/partial-ci",
            "storage_path": str(onboarding.root / "partial-ci"),
            "events": ["release"],
            "github_token": "github_pat_partial",
            "docker_registry_username": "acr-user",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert partial.status_code == 422
    puts = []
    _actions_secret_client(monkeypatch, puts=puts, get_status=403)
    failed = onboarding.client.post(
        "/api/projects",
        json={
            "name": "Denied",
            "repo": "acme/denied-ci",
            "storage_path": str(onboarding.root / "denied-ci"),
            "events": ["release"],
            "github_token": "github_pat_denied",
            "docker_registry_username": "acr-user",
            "docker_registry_password": "acr-pass",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert failed.status_code == 422
    assert "Secrets" in failed.json()["detail"]
    with onboarding.sessions() as db:
        assert db.scalar(select(func.count()).select_from(ProjectConnection)) == 0
    assert puts == []


def test_create_project_registers_github_webhook(onboarding):
    csrf = login(onboarding.client)
    project = create_project(onboarding, csrf, "acme/mobile", "mobile", events=["issues", "release"], token="github_pat_hooks")
    assert project["webhook_registered"] is True
    assert project["github_hook_id"] == "101"
    assert project["webhook_error"] is None
    assert len(onboarding.hooks) == 1
    hook = onboarding.hooks[0]
    assert hook["repo"] == "acme/mobile"
    assert hook["events"] == ["issues", "release"]
    assert hook["url"] == project["webhook_url"]
    assert hook["url"].startswith("https://hooks.example.test/api/webhooks/github/")
    assert hook["secret"]


def test_create_project_without_token_does_not_register_webhook(onboarding):
    csrf = login(onboarding.client)
    project = create_project(onboarding, csrf, "acme/notoken", "notoken", events=["issues"])
    assert project["webhook_registered"] is False
    assert onboarding.hooks == []


def test_create_project_rejects_webhook_permission_denied(onboarding, monkeypatch):
    csrf = login(onboarding.client)

    def denied(**kwargs):
        request = httpx.Request("POST", "https://api.github.com/repos/acme/denied-hooks/hooks")
        raise httpx.HTTPStatusError("forbidden", request=request, response=httpx.Response(403, json={"message": "Resource not accessible"}, request=request))

    monkeypatch.setattr("backend.github.ensure_repository_webhook", denied)
    response = onboarding.client.post(
        "/api/projects",
        json={
            "name": "Denied Hooks",
            "repo": "acme/denied-hooks",
            "storage_path": str(onboarding.root / "denied-hooks"),
            "events": ["issues"],
            "github_token": "github_pat_no_webhooks",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 422
    assert "Webhook" in response.json()["detail"]
    with onboarding.sessions() as db:
        assert db.scalar(select(func.count()).select_from(ProjectConnection)) == 0


def test_replace_token_refreshes_webhook(onboarding):
    csrf = login(onboarding.client)
    project = create_project(onboarding, csrf, "acme/refresh-hooks", "refresh-hooks", events=["issues"], token="old-token")
    onboarding.hooks.clear()
    updated = onboarding.client.put(
        f"/api/projects/{project['id']}/github-token",
        json={"github_token": "new-token"},
        headers={"X-CSRF-Token": csrf},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["webhook_registered"] is True
    assert len(onboarding.hooks) == 1
    assert onboarding.hooks[0]["token"] == "new-token"

