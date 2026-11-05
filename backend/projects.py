from __future__ import annotations

from datetime import timedelta
import base64
import os
from pathlib import Path
import secrets
from fastapi import HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator, model_validator
import httpx
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from backend.auth import require_admin
from backend.config import PROJECT_RUNTIME_OVERRIDES, ProjectConfig, managers_for_load
from backend.credentials import cipher, seal, unseal
from backend.db import Job, Project, ProjectAutomation, ProjectConnection, Record, Timeline, identifier, utcnow
from backend.storage import _canonical, _root_for, normalize_storage_path, storage_roots
from backend.errors import ConfigurationNeeded
from backend.skills import uploaded_skill_options
from backend.service import redact
from backend.integrations import prune_dingtalk_project

EVENTS = [
    {"value": "issues", "label": "Issue", "description": "需求、Bug 标签、负责人、紧急程度"},
    {"value": "pull_request", "label": "Pull request", "description": "PR 创建、提交更新和合并"},
    {"value": "pull_request_review", "label": "PR 审核", "description": "人工审核结果"},
    {"value": "release", "label": "Release", "description": "版本发布"},
    {"value": "check_run", "label": "Check run", "description": "单项 CI 检查"},
    {"value": "check_suite", "label": "Check suite", "description": "CI 检查集合"},
    {"value": "status", "label": "Commit status", "description": "提交检查状态"},
    {"value": "workflow_run", "label": "Workflow run", "description": "GitHub Actions 流程状态"},
]
DEFAULT_EVENTS = [event["value"] for event in EVENTS]


def connection_active(connection) -> bool:
    return bool(connection and connection.storage_status == "ready" and connection.api_status == "ready"
                and connection.webhook_verified and connection.token_ciphertext
                and connection.repository_id and connection.repository_id == connection.webhook_repository_id
                and connection.verified_revision == connection.revision)


def refresh_activation(connection):
    if connection_active(connection) and not connection.activated_at:
        connection.activated_at = utcnow()
    connection.updated_at = utcnow()


def project_config(settings, db, repo: str, *, active_only: bool = True):
    project = db.scalar(select(Project).where(func.lower(Project.repo) == repo.lower(), Project.demo.is_(False)))
    if not project:
        return None
    connection = db.get(ProjectConnection, project.id)
    if active_only and not connection_active(connection):
        return None
    config = settings.project(project.repo) or ProjectConfig(repo=project.repo)
    automation = db.get(ProjectAutomation, project.id)
    from backend.release_prepare import adopt_prepare_release
    overrides = adopt_prepare_release(dict(automation.config or {}) if automation else {})
    data = config.model_dump()
    for key, value in overrides.items():
        if key == "prepare_release" and isinstance(value, dict):
            data[key] = {**data.get(key, {}), **value}
        elif key in PROJECT_RUNTIME_OVERRIDES:
            data[key] = value
    data.update({"repo": project.repo, "name": project.name, "description": project.description})
    data["managers"] = managers_for_load(data.get("managers"))
    return ProjectConfig.model_validate(data)


def project_token(settings, sessions, repo: str, require_active: bool = True) -> str:
    with sessions() as db:
        project = db.scalar(select(Project).where(func.lower(Project.repo) == repo.lower(), Project.demo.is_(False)))
        connection = db.get(ProjectConnection, project.id) if project else None
        if not connection or (require_active and not connection_active(connection)):
            raise ConfigurationNeeded("项目接入尚未就绪，请检查目录、Webhook 和 GitHub 授权")
        token = unseal(settings, project.id, "github", connection.token_ciphertext)
        if not token:
            raise ConfigurationNeeded("尚未配置此项目的 GitHub API Token")
        return token


def project_redactions(settings, sessions):
    values = [settings.github_token, settings.github_webhook_secret,
              settings.executor_webhook_secret, settings.secrets_encryption_key]
    paths = [str(settings.data_root.resolve()), settings.workspace_host_root]
    with sessions() as db:
        for connection in db.scalars(select(ProjectConnection)):
            paths.append(connection.storage_path)
            for kind, encrypted in [("github", connection.token_ciphertext), ("webhook", connection.secret_ciphertext)]:
                try:
                    values.append(unseal(settings, connection.project_id, kind, encrypted))
                except ConfigurationNeeded:
                    pass
        from backend.db import AgentConnection, DingTalkChannel, RegistryConnection, ProjectAutomation
        for agent in db.scalars(select(AgentConnection)):
            try:
                values.append(unseal(settings, "agent:" + agent.id, "api_key", agent.api_key_ciphertext))
            except ConfigurationNeeded:
                pass
        for channel in db.scalars(select(DingTalkChannel)):
            try:
                values.extend([
                    unseal(settings, "dingtalk:" + channel.id, "webhook", channel.webhook_ciphertext),
                    unseal(settings, "dingtalk:" + channel.id, "sign_secret", channel.sign_secret_ciphertext),
                ])
            except ConfigurationNeeded:
                pass
        for registry in db.scalars(select(RegistryConnection)):
            try:
                values.append(unseal(settings, "registry:" + registry.id, "password", registry.password_ciphertext))
            except ConfigurationNeeded:
                pass
        for automation in db.scalars(select(ProjectAutomation)):
            try:
                values.extend([unseal(settings, automation.project_id, "publisher_username", automation.publisher_username_ciphertext),
                               unseal(settings, automation.project_id, "publisher_password", automation.publisher_password_ciphertext),
                               unseal(settings, automation.project_id, "publisher_browser_api_key", automation.publisher_browser_api_key_ciphertext)])
            except ConfigurationNeeded:
                pass
    for path in paths:
        if path:
            values.extend([path, path.replace("\\", "/")])
    return [value for value in values if value]


def import_connections(settings, db):
    for project in db.scalars(select(Project).where(Project.demo.is_(False))):
        connection = db.get(ProjectConnection, project.id)
        if not connection:
            connection = ProjectConnection(project_id=project.id, events=list(DEFAULT_EVENTS))
            db.add(connection)
        if not connection.secret_ciphertext and settings.secrets_encryption_key:
            connection.secret_ciphertext = seal(settings, project.id, "webhook", secrets.token_urlsafe(32))
    db.commit()


def public_status(connection, demo=False):
    return {"status": "demo" if demo else "active" if connection_active(connection) else "pending",
            "storage_status": connection.storage_status if connection else "pending",
            "api_status": connection.api_status if connection else "missing",
            "webhook_status": "ready" if connection and connection.webhook_verified else "pending"}


def connection_details(settings, db, project_id):
    project = db.get(Project, project_id)
    connection = db.get(ProjectConnection, project_id)
    if not project or not connection or project.demo:
        raise HTTPException(404, "项目接入不存在；演示项目不包含真实凭据")
    cfg = project_config(settings, db, project.repo, active_only=False)
    automation = db.get(ProjectAutomation, project.id)
    if automation:
        from backend.release_prepare import adopt_prepare_release
        current = dict(automation.config or {})
        adopted = adopt_prepare_release(current)
        if adopted != current:
            automation.config = adopted
            db.add(automation)
            db.commit()
            cfg = project_config(settings, db, project.repo, active_only=False)
    path = Path(connection.storage_path) if connection.storage_path else None
    return {"id": project.id, "name": project.name, "description": project.description, "repo": project.repo,
            **public_status(connection), "events": connection.events,
            "storage_path": connection.storage_path, "repository_path": str(path / "repo.git") if path else "",
            "workspaces_path": str(path / "workspaces") if path else "", "storage_locked": connection.storage_locked,
            "storage_error": connection.storage_error, "api_error": connection.api_error,
            "webhook_url": f"{settings.webhook_public_base_url}/api/webhooks/github/{connection.webhook_id}" if settings.webhook_public_base_url else None,
            "webhook_id": connection.webhook_id,
            "webhook_path": f"/api/webhooks/github/{connection.webhook_id}",
            "github_hook_id": connection.github_hook_id,
            "webhook_registered": bool(connection.github_hook_id),
            "webhook_error": None,
            "github_settings_url": f"{settings.github_git_origin.rstrip('/')}/{project.repo}/settings/hooks",
            "token_configured": bool(connection.token_ciphertext), "last_delivery_at": connection.last_delivery_at,
            "last_synced_at": project.last_synced_at, "sync_error": project.sync_error,
            "public_url_configured": bool(settings.webhook_public_base_url),
            "automation": {"review": cfg.review_enabled, "merge": cfg.merge_enabled, "release": cfg.release_enabled},
            "automation_config": {"agent_id": cfg.agent_id,
                                  "agent_rules": [rule.model_dump() for rule in cfg.agent_rules],
                                  "prepare_release": cfg.prepare_release.model_dump(),
                                  "review_enabled": cfg.review_enabled,
                                  "merge_enabled": cfg.merge_enabled,
                                  "release_enabled": cfg.release_enabled,
                                  "required_checks": list(cfg.required_checks),
                                  "merge_method": cfg.merge_method,
                                  "base_branches": list(cfg.base_branches),
                                  "priority_labels": dict(cfg.priority_labels),
                                  "managers": list(cfg.managers),
                                  "previous_tag": cfg.previous_tag,
                                  "review_rules": cfg.review_rules},
            "skills": uploaded_skill_options(db)}


def register_project_webhook(settings, connection, repo: str, token: str, secret: str):
    if not token.strip() or not settings.webhook_public_base_url:
        return
    from backend.github import ensure_repository_webhook
    url = f"{settings.webhook_public_base_url}/api/webhooks/github/{connection.webhook_id}"
    try:
        connection.github_hook_id = ensure_repository_webhook(
            api_url=settings.github_api_url,
            token=token.strip(),
            repo=repo,
            url=url,
            secret=secret,
            events=list(connection.events),
        )
    except httpx.HTTPStatusError as exc:
        if exc.response is not None and exc.response.status_code in {401, 403, 404}:
            raise HTTPException(422, "无法自动创建 GitHub Webhook，请确认 Token 具有 Webhooks 读写权限") from None
        raise HTTPException(422, "无法自动创建 GitHub Webhook") from None
    except httpx.RequestError:
        raise HTTPException(422, "无法连接 GitHub 以创建 Webhook") from None
    except (ValueError, OSError, ConfigurationNeeded) as exc:
        raise HTTPException(422, f"无法自动创建 GitHub Webhook：{exc}") from None


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=240)
    description: str = Field(default="", max_length=5000)
    repo: str = Field(min_length=3, max_length=240)
    storage_path: str = Field(min_length=1, max_length=900)
    events: list[str] = Field(default_factory=lambda: list(DEFAULT_EVENTS), min_length=1, max_length=8)
    github_token: str = Field(default="", max_length=1000)
    docker_registry_username: str = Field(default="", max_length=1000)
    docker_registry_password: str = Field(default="", max_length=4000)
    android_keystore_base64: str = Field(default="", max_length=64000)
    android_key_alias: str = Field(default="", max_length=500)
    android_keystore_password: str = Field(default="", max_length=4000)
    android_key_password: str = Field(default="", max_length=4000)

    @field_validator("repo")
    @classmethod
    def repo_name(cls, value):
        return ProjectConfig(repo=value.strip()).repo.lower()

    @field_validator("events")
    @classmethod
    def event_selection(cls, value):
        if not set(value).issubset(DEFAULT_EVENTS) or len(set(value)) != len(value):
            raise ValueError("请选择受支持且不重复的 GitHub 事件")
        return value

    @field_validator("name")
    @classmethod
    def trimmed_name(cls, value):
        if not value.strip():
            raise ValueError("项目名称不能为空")
        return value.strip()

    @model_validator(mode="after")
    def ci_secret_groups(self):
        docker = [self.docker_registry_username.strip(), self.docker_registry_password.strip()]
        android = [
            self.android_keystore_base64.strip(),
            self.android_key_alias.strip(),
            self.android_keystore_password.strip(),
            self.android_key_password.strip(),
        ]
        if any(docker) and not all(docker):
            raise ValueError("启用镜像推送时需要同时填写仓库用户名和密码")
        if any(android) and not all(android):
            raise ValueError("启用安卓签名时需要同时填写密钥文件、别名和两个密码")
        if self.android_keystore_base64.strip():
            try:
                data = base64.b64decode(self.android_keystore_base64.strip(), validate=True)
            except Exception as exc:
                raise ValueError("安卓密钥文件不是有效的 Base64") from exc
            if not data or len(data) > 48 * 1024:
                raise ValueError("安卓密钥文件过大或为空")
        if (any(docker) or any(android)) and not self.github_token.strip():
            raise ValueError("写入 GitHub Secrets 需要先填写 GitHub PAT")
        return self

    def actions_secrets(self) -> dict[str, str]:
        values: dict[str, str] = {}
        if self.docker_registry_username.strip():
            values["DOCKER_REGISTRY_USERNAME"] = self.docker_registry_username.strip()
            values["DOCKER_REGISTRY_PASSWORD"] = self.docker_registry_password.strip()
        if self.android_keystore_base64.strip():
            values["ANDROID_KEYSTORE_BASE64"] = self.android_keystore_base64.strip()
            values["ANDROID_KEY_ALIAS"] = self.android_key_alias.strip()
            values["ANDROID_KEYSTORE_PASSWORD"] = self.android_keystore_password.strip()
            values["ANDROID_KEY_PASSWORD"] = self.android_key_password.strip()
        return values


class TokenUpdate(BaseModel):
    github_token: str = Field(min_length=1, max_length=1000)


class StorageUpdate(BaseModel):
    storage_path: str = Field(min_length=1, max_length=900)


def request_verification(connection):
    connection.revision += 1
    connection.verify_requested = True
    connection.next_verify_at = utcnow()
    connection.api_status = "pending" if connection.token_ciphertext else "missing"
    connection.api_error = None
    connection.updated_at = utcnow()


def install_projects(app, settings, sessions):
    from backend.storage import normalize_storage_path, storage_roots

    def live():
        if settings.app_mode != "live":
            raise HTTPException(409, "演示模式不保存真实项目或凭据，请使用独立的 live 数据库")
        cipher(settings)

    def existing(db, project_id):
        connection = db.get(ProjectConnection, project_id)
        if not connection:
            raise HTTPException(404, "项目接入不存在")
        return connection

    def path_for(db, value, exclude=None):
        paths = db.scalars(select(ProjectConnection.storage_path).where(ProjectConnection.project_id != (exclude or ""), ProjectConnection.storage_path != ""))
        try:
            return normalize_storage_path(settings, value, list(paths))
        except (ValueError, ConfigurationNeeded) as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/project-options")
    def options(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        with sessions() as db:
            require_admin(request, db)
        return {"events": EVENTS, "default_events": DEFAULT_EVENTS, "storage_roots": [str(x) for x in storage_roots(settings)],
                "webhook_public_base_url": settings.webhook_public_base_url, "mode": settings.app_mode,
                "management_enabled": settings.app_mode == "live" and bool(settings.secrets_encryption_key)}

    @app.post("/api/projects", status_code=201)
    def create(body: ProjectCreate, request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        with sessions() as db:
            require_admin(request, db, write=True)
            live()
            if db.scalar(select(Project.id).where(func.lower(Project.repo) == body.repo.lower())):
                raise HTTPException(409, "该仓库已经创建项目")
            path = path_for(db, body.storage_path)
            secrets_to_write = body.actions_secrets()
            if secrets_to_write:
                from backend.github import put_repository_secrets
                try:
                    put_repository_secrets(settings.github_api_url, body.github_token.strip(), body.repo, secrets_to_write)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code in {401, 403, 404}:
                        raise HTTPException(422, "GitHub 拒绝写入 Actions Secrets，请确认 Token 具有 Secrets 读写权限") from None
                    raise HTTPException(422, "无法写入 GitHub Actions Secrets") from None
                except httpx.RequestError:
                    raise HTTPException(422, "无法连接 GitHub 以写入 Actions Secrets") from None
                except (ValueError, OSError, ConfigurationNeeded) as exc:
                    raise HTTPException(422, f"无法写入 GitHub Actions Secrets：{exc}") from None
            project = Project(id=identifier(), repo=body.repo, name=body.name, description=body.description)
            db.add(project)
            db.flush()
            secret = secrets.token_urlsafe(32)
            connection = ProjectConnection(project_id=project.id, storage_path=path, storage_key=os.path.normcase(path),
                events=body.events, secret_ciphertext=seal(settings, project.id, "webhook", secret),
                token_ciphertext=seal(settings, project.id, "github", body.github_token.strip()) if body.github_token.strip() else None,
                api_status="pending" if body.github_token.strip() else "missing")
            db.add(connection)
            db.flush()
            try:
                register_project_webhook(settings, connection, body.repo, body.github_token, secret)
                db.commit()
            except HTTPException:
                db.rollback()
                raise
            except IntegrityError:
                db.rollback()
                raise HTTPException(409, "仓库或目录已被其他项目使用") from None
            return connection_details(settings, db, project.id)

    @app.get("/api/projects/{project_id}/connection")
    def details(project_id: str, request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        with sessions() as db:
            require_admin(request, db)
            return connection_details(settings, db, project_id)

    @app.put("/api/projects/{project_id}/github-token")
    def replace_token(project_id: str, body: TokenUpdate, request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        with sessions() as db:
            require_admin(request, db, write=True)
            live()
            connection = existing(db, project_id)
            if not body.github_token.strip():
                raise HTTPException(422, "Token 不能为空")
            token = body.github_token.strip()
            connection.token_ciphertext = seal(settings, project_id, "github", token)
            project = db.get(Project, project_id)
            secret = unseal(settings, project_id, "webhook", connection.secret_ciphertext)
            try:
                register_project_webhook(settings, connection, project.repo, token, secret)
            except HTTPException:
                db.rollback()
                raise
            request_verification(connection)
            db.commit()
            return connection_details(settings, db, project_id)

    @app.put("/api/projects/{project_id}/storage")
    def replace_storage(project_id: str, body: StorageUpdate, request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        with sessions() as db:
            require_admin(request, db, write=True)
            live()
            # Acquires the same write lock as workspace preparation before checking storage_locked.
            db.execute(update(ProjectConnection).where(ProjectConnection.project_id == project_id).values(updated_at=utcnow()))
            connection = existing(db, project_id)
            db.refresh(connection)
            if connection.storage_locked:
                raise HTTPException(409, "此项目已开始拉取代码，本期不支持迁移目录")
            connection.storage_path = path_for(db, body.storage_path, project_id)
            connection.storage_key = os.path.normcase(connection.storage_path)
            connection.storage_status, connection.storage_error = "pending", None
            request_verification(connection)
            db.commit()
            return connection_details(settings, db, project_id)

    @app.post("/api/projects/{project_id}/verify")
    def verify(project_id: str, request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        with sessions() as db:
            require_admin(request, db, write=True)
            live()
            request_verification(existing(db, project_id))
            db.commit()
            return connection_details(settings, db, project_id)

    @app.post("/api/projects/{project_id}/webhook-secret/reveal")
    def reveal(project_id: str, request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        with sessions() as db:
            require_admin(request, db, write=True)
            live()
            connection = existing(db, project_id)
            if not connection.secret_ciphertext:
                connection.secret_ciphertext = seal(settings, project_id, "webhook", secrets.token_urlsafe(32))
            secret = unseal(settings, project_id, "webhook", connection.secret_ciphertext)
            db.commit()
            return {"secret": secret}

    @app.delete("/api/projects/{project_id}")
    def delete_project(project_id: str, request: Request, response: Response, delete_files: bool = True):
        response.headers["Cache-Control"] = "no-store"
        with sessions() as db:
            require_admin(request, db, write=True)
            live()
            project = db.get(Project, project_id)
            if not project:
                raise HTTPException(404, "项目不存在")
            if project.demo:
                raise HTTPException(400, "演示项目为内置示例，不能删除")

            connection = db.get(ProjectConnection, project_id)
            storage_path = connection.storage_path if connection else None
            project_name = project.name or project.repo

            record_ids = list(db.scalars(select(Record.id).where(Record.repo == project.repo)))
            if record_ids:
                db.execute(delete(Job).where(Job.record_id.in_(record_ids)))
                db.execute(delete(Timeline).where(Timeline.record_id.in_(record_ids)))
                db.execute(delete(Record).where(Record.id.in_(record_ids)))

            db.execute(delete(ProjectAutomation).where(ProjectAutomation.project_id == project_id))
            db.execute(delete(ProjectConnection).where(ProjectConnection.project_id == project_id))
            prune_dingtalk_project(db, project_id)
            db.execute(delete(Project).where(Project.id == project_id))
            db.commit()

            deleted_dir = False
            if delete_files and storage_path:
                try:
                    path_obj = _canonical(Path(storage_path))
                    _root_for(settings, path_obj)
                    if path_obj.exists() and path_obj.is_dir():
                        import shutil
                        import stat

                        def _on_error(func, path, exc_info):
                            try:
                                os.chmod(path, stat.S_IWRITE)
                                func(path)
                            except Exception:
                                pass

                        shutil.rmtree(path_obj, onerror=_on_error)
                        deleted_dir = True
                except Exception:
                    pass

            return {"ok": True, "message": f"项目 {project_name} 已成功删除", "deleted_files": deleted_dir}


async def verify_api_access(settings, token, repo, events):
    from backend.github import GitHub
    github = GitHub(settings.model_copy(update={"github_token": token}))
    try:
        await github.request("GET", "/user")
        repository = await github.request("GET", f"/repos/{repo}")
        if repository.get("full_name", "").lower() != repo.lower():
            raise ConfigurationNeeded("Token 返回的仓库身份与项目不一致")
        if "issues" in events:
            await github.request("GET", f"/repos/{repo}/issues", params={"per_page": 1, "state": "all"})
        if {"pull_request", "pull_request_review"} & set(events):
            await github.request("GET", f"/repos/{repo}/pulls", params={"per_page": 1, "state": "all"})
        if "release" in events:
            await github.request("GET", f"/repos/{repo}/releases", params={"per_page": 1})
        try:
            commits = await github.request("GET", f"/repos/{repo}/commits", params={"per_page": 1})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 409:  # GitHub's empty-repository response.
                raise
            commits = []
        if commits:
            sha = commits[0]["sha"]
            if {"check_run", "check_suite"} & set(events):
                await github.request("GET", f"/repos/{repo}/commits/{sha}/check-runs", params={"per_page": 1})
            if "status" in events:
                await github.request("GET", f"/repos/{repo}/commits/{sha}/status", params={"per_page": 1})
        return str(repository["id"])
    finally:
        await github.close()


async def verify_connections(settings, sessions, verifier=None):
    """Executed by Worker; API never claims that an unmounted directory is ready."""
    from backend.storage import prepare_project_storage
    verifier = verifier or verify_api_access
    activated = 0
    with sessions() as db:
        ids = list(db.scalars(select(ProjectConnection.project_id).where(ProjectConnection.verify_requested.is_(True), ProjectConnection.next_verify_at <= utcnow()).limit(10)))
    for project_id in ids:
        token, api_error, repository_id = "", None, None
        claim_until = utcnow() + timedelta(minutes=5)
        with sessions() as db:
            claim = db.execute(update(ProjectConnection).where(ProjectConnection.project_id == project_id,
                ProjectConnection.verify_requested.is_(True), ProjectConnection.next_verify_at <= utcnow()).values(next_verify_at=claim_until))
            if not claim.rowcount:
                db.rollback()
                continue
            connection = db.get(ProjectConnection, project_id)
            project = db.get(Project, project_id)
            revision, repo, events = connection.revision, project.repo, list(connection.events)
            try:
                prepare_project_storage(settings, project_id, connection.storage_path)
                connection.storage_status, connection.storage_error = "ready", None
            except (ValueError, OSError, ConfigurationNeeded) as exc:
                connection.storage_status, connection.storage_error = "error", "目录不可用：" + str(exc)[:500]
            try:
                token = unseal(settings, project_id, "github", connection.token_ciphertext)
            except ConfigurationNeeded as exc:
                api_error = str(exc)
            connection.updated_at = utcnow()
            db.commit()
        if token:
            try:
                repository_id = await verifier(settings, token, repo, events)
            except httpx.HTTPStatusError as exc:
                api_error = f"GitHub 读取验证失败（HTTP {exc.response.status_code}），请检查 Token、仓库授权和读取权限"
            except httpx.TransportError:
                api_error = "无法连接 GitHub，请检查网络后重试"
            except Exception as exc:
                api_error = redact(str(exc), [token, settings.secrets_encryption_key])[:500] or "GitHub 授权验证失败"
        with sessions() as db:
            # Fence an obsolete validation response after any admin credential/path change.
            changed = db.execute(update(ProjectConnection).where(ProjectConnection.project_id == project_id,
                ProjectConnection.revision == revision, ProjectConnection.next_verify_at == claim_until).values(updated_at=utcnow()))
            if not changed.rowcount:
                db.rollback()
                continue
            connection = db.get(ProjectConnection, project_id)
            if repository_id and connection.webhook_repository_id and repository_id != connection.webhook_repository_id:
                api_error = "API Token 和 Webhook 指向不同仓库，请核对绑定"
            connection.api_status = "error" if api_error else "ready" if repository_id else "missing"
            connection.api_error = api_error
            if repository_id and not api_error:
                connection.repository_id = repository_id
            connection.verified_revision = revision
            connection.verify_requested = bool(api_error or connection.storage_status == "error")
            connection.next_verify_at = utcnow() + timedelta(seconds=60)
            refresh_activation(connection)
            activated += int(connection_active(connection))
            db.commit()
    return activated
