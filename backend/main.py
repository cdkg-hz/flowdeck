from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import uuid
from pathlib import Path
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from backend.config import Settings, get_settings
from backend.db import AgentConnection, DingTalkChannel, InboxEvent, Job, Project, ProjectConnection, Record, Timeline, UploadedSkill, database, initialize_schema, utcnow
from backend.demo import seed_demo
from backend.schemas import ExecutorEvent
from backend.service import add_timeline, assignee_clause, bug_record_clause, ensure_projects, retry_job, schedule_job, verify_signature, redact_nested, work_type
from backend.auth import install_auth, require_admin
from backend.credentials import cipher
from backend.dingtalk import send_dingtalk_message
from backend.integrations import AgentConnectionInput, DingTalkChannelInput, ProjectAutomationInput, agent_public, dingtalk_public, load_agent, load_dingtalk_secrets, save_agent, save_dingtalk_channel, save_project_automation
from backend.credentials import unseal
from backend.errors import ConfigurationNeeded
from backend.projects import DEFAULT_EVENTS, import_connections, install_projects, project_config, project_redactions, public_status, refresh_activation, connection_active
from backend.release_prepare import PrepareReleaseInput, prepare_release_ready, queue_prepare_release, validate_prepare_tag
from backend.github import GitHub
from backend.skills import DuplicateSkillError, SkillPackageError, delete_uploaded_skill_files, save_uploaded_skill, skill_public, skill_slug_in_use


def row_dict(row):
    result = {}
    for column in row.__table__.columns:
        value = getattr(row, column.name)
        if hasattr(value, "isoformat"):
            value = value.isoformat()
            if "+" not in value and not value.endswith("Z"):
                value += "Z"
        result[column.name] = value
    return result


def record_public(record):
    data = row_dict(record)
    data["work_type"] = work_type(record.kind, record.labels)
    return data


def create_app(settings: Settings | None = None):
    settings = settings or get_settings()
    engine, Session = database(settings.database_url)

    @asynccontextmanager
    async def lifespan(app):
        settings.data_root.mkdir(parents=True, exist_ok=True)
        initialize_schema(engine)
        with Session() as db:
            if settings.app_mode == "demo":
                seed_demo(db)
            else:
                if db.scalar(select(Project).where(Project.demo.is_(True)).limit(1)):
                    raise RuntimeError("Live mode requires a database without demo data. Use a separate DATABASE_URL.")
                ensure_projects(db, settings.project_config().projects)
                import_connections(settings, db)
        yield
        engine.dispose()

    app = FastAPI(title="Flowdeck", version="0.1.0", lifespan=lifespan)
    app.state.settings, app.state.Session = settings, Session
    install_auth(app, settings, Session)
    install_projects(app, settings, Session)

    @app.exception_handler(ConfigurationNeeded)
    async def configuration_error(request, exc):
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    def public_data(value):
        return redact_nested(value, project_redactions(settings, Session))

    def check_read_auth(request: Request, db):
        if settings.require_login:
            require_admin(request, db)

    @app.get("/api/health")
    def health():
        with Session() as db:
            db.execute(select(1))
        return {"status": "ok", "mode": settings.app_mode}

    @app.get("/api/integrations")
    def integrations(request: Request):
        with Session() as db:
            check_read_auth(request, db)
            configured = list(db.scalars(select(Project).where(Project.demo.is_(False))))
            projects = [project_config(settings, db, p.repo, active_only=False) for p in configured]
            ready = any(connection_active(db.get(ProjectConnection, p.id)) for p in configured)
            pending = db.scalar(select(func.count()).select_from(InboxEvent).where(InboxEvent.processed_at.is_(None)))
            failed = db.scalar(select(func.count()).select_from(InboxEvent).where(InboxEvent.error.is_not(None)))
            last_error = db.scalar(select(InboxEvent.error).where(InboxEvent.error.is_not(None)).order_by(InboxEvent.received_at.desc()).limit(1))
            return {"mode": settings.app_mode, "read_only_ui": False, "events": {"pending": pending, "failed": failed, "last_error": last_error}, "github": {"configured": ready, "webhook_path": "/api/webhooks/github/{webhook_id}", "sync_interval": settings.sync_interval_seconds}, "agent": {"configured": bool(db.scalar(select(func.count()).select_from(AgentConnection).where(AgentConnection.enabled.is_(True)))), "protocol": "copilot"}, "dingtalk": {"configured": bool(db.scalar(select(func.count()).select_from(DingTalkChannel).where(DingTalkChannel.enabled.is_(True), DingTalkChannel.webhook_ciphertext.is_not(None), DingTalkChannel.sign_secret_ciphertext.is_not(None))))}, "projects": [{"repo": p.repo, "review": p.review_enabled, "merge": p.merge_enabled, "release": p.release_enabled, "required_checks": p.required_checks, "managers": p.managers} for p in projects]}

    def admin_write(request: Request):
        if settings.app_mode != "live":
            raise HTTPException(409, "演示模式不保存真实集成配置")
        cipher(settings)
        return request

    @app.get("/api/admin/integrations")
    def admin_integrations(request: Request):
        with Session() as db:
            require_admin(request, db)
            return {"agents": [agent_public(value) for value in db.scalars(select(AgentConnection).order_by(AgentConnection.name))], "dingtalk_channels": [dingtalk_public(x) for x in db.scalars(select(DingTalkChannel).order_by(DingTalkChannel.name))]}

    @app.post("/api/admin/agents", status_code=201)
    def create_agent(body: AgentConnectionInput, request: Request):
        admin_write(request)
        with Session() as db:
            require_admin(request, db, write=True)
            value = save_agent(settings, db, body)
            db.commit()
            return agent_public(value)

    @app.put("/api/admin/agents/{agent_id}")
    def update_agent(agent_id: str, body: AgentConnectionInput, request: Request):
        admin_write(request)
        with Session() as db:
            require_admin(request, db, write=True)
            value = db.get(AgentConnection, agent_id)
            if not value:
                raise HTTPException(404, "Agent 不存在")
            value = save_agent(settings, db, body, value)
            db.commit()
            return agent_public(value)

    @app.delete("/api/admin/agents/{agent_id}")
    def delete_agent(agent_id: str, request: Request):
        admin_write(request)
        with Session() as db:
            require_admin(request, db, write=True)
            value = db.get(AgentConnection, agent_id)
            if not value:
                raise HTTPException(404, "Agent 不存在")
            db.delete(value)
            db.commit()
            return {"ok": True}

    @app.post("/api/admin/dingtalk-channels", status_code=201)
    def create_dingtalk_channel(body: DingTalkChannelInput, request: Request):
        admin_write(request)
        with Session() as db:
            require_admin(request, db, write=True)
            value = save_dingtalk_channel(settings, db, body)
            db.commit()
            return dingtalk_public(value)

    @app.put("/api/admin/dingtalk-channels/{channel_id}")
    def update_dingtalk_channel(channel_id: str, body: DingTalkChannelInput, request: Request):
        admin_write(request)
        with Session() as db:
            require_admin(request, db, write=True)
            value = db.get(DingTalkChannel, channel_id)
            if not value:
                raise HTTPException(404, "钉钉渠道不存在")
            value = save_dingtalk_channel(settings, db, body, value)
            db.commit()
            return dingtalk_public(value)

    @app.delete("/api/admin/dingtalk-channels/{channel_id}")
    def delete_dingtalk_channel(channel_id: str, request: Request):
        admin_write(request)
        with Session() as db:
            require_admin(request, db, write=True)
            value = db.get(DingTalkChannel, channel_id)
            if not value:
                raise HTTPException(404, "钉钉渠道不存在")
            db.delete(value)
            db.commit()
            return {"ok": True}

    @app.post("/api/admin/dingtalk-channels/{channel_id}/test")
    async def test_dingtalk_channel(channel_id: str, request: Request):
        with Session() as db:
            require_admin(request, db, write=True)
            item = db.get(DingTalkChannel, channel_id)
            if not item:
                raise HTTPException(404, "钉钉渠道不存在")
            webhook, secret = load_dingtalk_secrets(settings, item)
            name = item.name
        try:
            await send_dingtalk_message(
                webhook=webhook, secret=secret, title="Flowdeck 测试",
                text=f"Flowdeck 测试：渠道 {name}", at_mobiles=[],
            )
        except Exception as exc:
            raise HTTPException(422, "钉钉测试发送失败") from exc
        return {"ok": True, "message": f"已向「{name}」发送测试消息"}

    async def _read_skill_zip(file: UploadFile) -> tuple[bytes, str]:
        name = Path(file.filename or "").name
        if not name.lower().endswith(".zip"):
            raise HTTPException(422, "请上传 zip 文件")
        chunks, total = [], 0
        while True:
            chunk = await file.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > 1_000_000:
                raise HTTPException(422, "压缩包过大，最大 1MB")
            chunks.append(chunk)
        return b"".join(chunks), name

    @app.get("/api/admin/skills")
    def list_skills(request: Request):
        with Session() as db:
            require_admin(request, db)
            return {"skills": [skill_public(item, settings.data_root) for item in db.scalars(select(UploadedSkill).order_by(UploadedSkill.slug))]}

    @app.post("/api/admin/skills", status_code=201)
    async def create_skill(request: Request, file: UploadFile = File(...)):
        admin_write(request)
        data, name = await _read_skill_zip(file)
        with Session() as db:
            require_admin(request, db, write=True)
            try:
                value = save_uploaded_skill(settings, db, data, name)
            except DuplicateSkillError as exc:
                raise HTTPException(409, str(exc)) from None
            except SkillPackageError as exc:
                raise HTTPException(422, str(exc)) from None
            db.commit()
            return skill_public(value, settings.data_root)

    @app.put("/api/admin/skills/{skill_id}")
    async def update_skill(skill_id: str, request: Request, file: UploadFile = File(...)):
        admin_write(request)
        data, name = await _read_skill_zip(file)
        with Session() as db:
            require_admin(request, db, write=True)
            value = db.get(UploadedSkill, skill_id)
            if not value:
                raise HTTPException(404, "Skill 不存在")
            try:
                value = save_uploaded_skill(settings, db, data, name, value)
            except DuplicateSkillError as exc:
                raise HTTPException(409, str(exc)) from None
            except SkillPackageError as exc:
                raise HTTPException(422, str(exc)) from None
            db.commit()
            return skill_public(value, settings.data_root)

    @app.delete("/api/admin/skills/{skill_id}")
    def delete_skill(skill_id: str, request: Request):
        admin_write(request)
        with Session() as db:
            require_admin(request, db, write=True)
            value = db.get(UploadedSkill, skill_id)
            if not value:
                raise HTTPException(404, "Skill 不存在")
            if skill_slug_in_use(db, value.slug):
                raise HTTPException(409, "仍有项目勾选了该 skill")
            db.delete(value)
            db.commit()
            delete_uploaded_skill_files(settings, skill_id)
            return {"ok": True}

    @app.post("/api/admin/agents/{agent_id}/test")
    async def test_agent(agent_id: str, request: Request):
        with Session() as db:
            require_admin(request, db, write=True)
            runtime = load_agent(settings, db, agent_id)
        from backend import copilot_runtime
        try:
            message = await copilot_runtime.probe_agent(settings, Session, runtime)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return {"ok": True, "message": message}

    @app.put("/api/projects/{project_id}/automation")
    def update_project_automation(project_id: str, body: ProjectAutomationInput, request: Request):
        admin_write(request)
        with Session() as db:
            require_admin(request, db, write=True)
            project = db.get(Project, project_id)
            if not project or project.demo:
                raise HTTPException(404, "项目接入不存在")
            save_project_automation(settings, db, project_id, body)
            db.commit()
            from backend.projects import connection_details
            return connection_details(settings, db, project_id)

    @app.post("/api/releases/{record_id}/run", status_code=202)
    def run_release(record_id: str, request: Request):
        admin_write(request)
        with Session() as db:
            require_admin(request, db, write=True)
            record = db.get(Record, record_id)
            if not record or record.kind != "release" or record.demo:
                raise HTTPException(404, "正式 Release 不存在")
            if record.details.get("draft") or record.details.get("prerelease") or not record.details.get("release_id") or not record.details.get("tag"):
                raise HTTPException(409, "只能执行正式且信息完整的 Release")
            config = project_config(settings, db, record.repo, active_only=False)
            if not config or not config.release_enabled:
                raise HTTPException(422, "该项目尚未配置可手动执行的 Release 自动化")
            job = schedule_job(db, record, "release", f"manual-release:{record.id}:{uuid.uuid4().hex}",
                               {"release_id": record.details["release_id"], "tag": record.details["tag"], "manual": True})
            add_timeline(db, record, "release", "queued", "已手动创建 Release 执行", job_id=job.id, actor="管理员")
            db.commit()
            return {"id": job.id, "status": job.status}

    @app.post("/api/projects/{project_id}/prepare-release", status_code=202)
    async def prepare_release(project_id: str, body: PrepareReleaseInput, request: Request):
        admin_write(request)
        with Session() as db:
            require_admin(request, db, write=True)
            project = db.get(Project, project_id)
            if not project:
                raise HTTPException(404, "项目接入不存在")
            if project.demo:
                raise HTTPException(409, "演示项目不能发版")
            config = project_config(settings, db, project.repo, active_only=True)
            if not config:
                raise HTTPException(422, "项目接入尚未就绪")
            if not prepare_release_ready(config):
                raise HTTPException(422, "请先在项目接入中启用总结并发布")
            try:
                tag = validate_prepare_tag(body.tag)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
            repo = project.repo
            base_branches = list(config.base_branches)
        github = GitHub(settings, sessions=Session)
        try:
            try:
                from backend import release_prepare
                preflight = await release_prepare.inspect_github_prepare(github, repo, tag, base_branches)
            except ValueError as exc:
                status = 409 if "已存在" in str(exc) else 422
                raise HTTPException(status, str(exc)) from None
            except ConfigurationNeeded as exc:
                raise HTTPException(422, str(exc)) from None
        finally:
            await github.close()
        with Session() as db:
            require_admin(request, db, write=True)
            config = project_config(settings, db, repo, active_only=True)
            if not config:
                raise HTTPException(422, "项目接入尚未就绪")
            try:
                record, job = queue_prepare_release(db, config, tag=tag, default_branch=preflight["default_branch"])
            except LookupError as exc:
                raise HTTPException(409, str(exc)) from None
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
            db.commit()
            return {"id": job.id, "record_id": record.id, "status": job.status}

    @app.post("/api/jobs/{job_id}/retry", status_code=202)
    def retry_existing_job(job_id: str, request: Request):
        admin_write(request)
        with Session() as db:
            require_admin(request, db, write=True)
            job = db.get(Job, job_id)
            try:
                retry_job(db, job)
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from None
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
            db.commit()
            return {"id": job.id, "status": job.status}

    @app.get("/api/projects")
    def projects(request: Request):
        with Session() as db:
            check_read_auth(request, db)
            values = []
            for p in db.scalars(select(Project).order_by(Project.name)):
                data = {key: value for key, value in row_dict(p).items() if key in {"id", "repo", "name", "description", "last_synced_at", "sync_error", "demo"}}
                data.update(public_status(db.get(ProjectConnection, p.id), p.demo))
                data["record_count"] = db.scalar(select(func.count()).select_from(Record).where(Record.repo == p.repo))
                cfg = None if p.demo else project_config(settings, db, p.repo, active_only=False)
                data["prepare_release"] = prepare_release_ready(cfg)
                values.append(data)
            return public_data(values)

    @app.get("/api/overview")
    def overview(request: Request, repo: str | None = None):
        with Session() as db:
            check_read_auth(request, db)
            query = select(Record)
            if repo:
                query = query.where(Record.repo == repo)
            records = list(db.scalars(query))
            counts = {"total": len(records), "active": sum(x.status not in {"merged", "closed", "success"} for x in records), "attention": sum(x.status in {"failed", "changes_requested", "uncertain", "needs_configuration"} or (x.kind == "issue" and x.status != "closed" and not x.assignees) for x in records), "reviewing": sum(x.kind == "pr" and x.stage == "review" and x.status in {"queued", "running"} for x in records), "released": sum(x.kind == "release" and x.status == "success" for x in records)}
            activity = select(Timeline).join(Record, Timeline.record_id == Record.id)
            if repo:
                activity = activity.where(Record.repo == repo)
            events = []
            for event in db.scalars(activity.order_by(Timeline.occurred_at.desc()).limit(8)):
                data = row_dict(event)
                record = db.get(Record, event.record_id)
                data.update({"record_title": record.title, "repo": record.repo})
                events.append(data)
            return public_data({"counts": counts, "activity": events, "mode": settings.app_mode})

    @app.get("/api/records")
    def records(request: Request, repo: str | None = None, kind: str | None = None, status: str | None = None, priority: str | None = None, assignee: str | None = None, q: str = "", limit: int = 100, offset: int = 0):
        with Session() as db:
            check_read_auth(request, db)
            query = select(Record)
            if repo:
                query = query.where(Record.repo == repo)
            if kind == "bug":
                query = query.where(bug_record_clause())
            elif kind:
                query = query.where(Record.kind == kind)
            if status == "attention":
                query = query.where(Record.status.in_(["failed", "changes_requested", "uncertain", "needs_configuration"]) | ((Record.kind == "issue") & (Record.status != "closed") & (Record.assignees == [])))
            elif status == "reviewing":
                query = query.where(Record.kind == "pr", Record.stage == "review", Record.status.in_(["queued", "running"]))
            elif status == "active":
                query = query.where(Record.status.not_in(["merged", "closed", "success"]))
            elif status:
                query = query.where(Record.status == status)
            if priority == "none":
                query = query.where(Record.priority.is_(None) | (Record.priority == ""))
            elif priority:
                query = query.where(Record.priority == priority)
            if assignee:
                query = query.where(assignee_clause(assignee))
            if q:
                safe = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                query = query.where(Record.title.ilike(f"%{safe}%", escape="\\"))
            total = db.scalar(select(func.count()).select_from(query.subquery()))
            items = db.scalars(query.order_by(Record.updated_at.desc()).offset(max(0, offset)).limit(max(1, min(limit, 200))))
            return public_data({"items": [record_public(x) for x in items], "total": total})

    @app.get("/api/records/{record_id}")
    def record_detail(record_id: str, request: Request):
        with Session() as db:
            check_read_auth(request, db)
            record = db.get(Record, record_id)
            if not record:
                raise HTTPException(404, "Record not found")
            data = record_public(record)
            data["timeline"] = [row_dict(x) for x in db.scalars(select(Timeline).where(Timeline.record_id == record_id).order_by(Timeline.occurred_at))]
            # Do not expose provider payloads, filesystem locations or authentication data.
            data["jobs"] = [{k: v for k, v in row_dict(x).items() if k not in {"payload", "result", "worker_id", "lease_until"}} for x in db.scalars(select(Job).where(Job.record_id == record_id).order_by(Job.created_at.desc()))]
            linked = record.details.get("linked_records", [])
            related = {x.id: record_public(x) for x in db.scalars(select(Record).where(Record.id.in_(linked)))} if linked else {}
            for ref in record.details.get("closing_issues", []):
                issue = db.scalar(select(Record).where(Record.repo == ref["repo"], Record.kind == "issue", Record.number == ref["number"]))
                if issue:
                    related[issue.id] = record_public(issue)
            if record.kind == "issue":
                for pr in db.scalars(select(Record).where(Record.kind == "pr")):
                    if any(ref["repo"] == record.repo and ref["number"] == record.number for ref in pr.details.get("closing_issues", [])):
                        related[pr.id] = record_public(pr)
            data["linked_records"] = list(related.values())
            return public_data(data)

    async def read_body(request: Request):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 2 * 1024 * 1024:
                raise HTTPException(413, "Webhook body exceeds 2 MiB")
        return bytes(body)

    def persist_event(source: str, delivery: str, event_type: str, payload: dict):
        with Session() as db:
            db.add(InboxEvent(source=source, delivery_id=delivery, event_type=event_type, payload=payload))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                return {"accepted": True, "duplicate": True}
        return {"accepted": True, "duplicate": False}

    @app.post("/api/webhooks/github", status_code=202)
    async def legacy_webhook():
        if settings.app_mode != "live":
            raise HTTPException(409, "Demo mode does not accept real events")
        raise HTTPException(410, "共享入口已退役，请在网页项目接入详情获取独立 Webhook URL 和 Secret")

    @app.post("/webhooks/github-notifications", status_code=202)
    @app.post("/api/webhooks/github/{webhook_id}", status_code=202)
    async def github_webhook(request: Request, webhook_id: str | None = None):
        if settings.app_mode != "live":
            raise HTTPException(409, "Demo mode does not accept real events")
        with Session() as db:
            if webhook_id:
                connection = db.scalar(select(ProjectConnection).where(ProjectConnection.webhook_id == webhook_id))
                if not connection:
                    raise HTTPException(404, "Webhook not found")
                project_id = connection.project_id
                secret = unseal(settings, project_id, "webhook", connection.secret_ciphertext)
            else:
                connection, project_id, secret = None, None, None
        raw = await read_body(request)
        delivery = request.headers.get("x-github-delivery", "")
        event_type = request.headers.get("x-github-event", "")
        allowed_events = {"ping", *DEFAULT_EVENTS}
        if not delivery or len(delivery) > 128 or event_type not in allowed_events:
            raise HTTPException(400, "Missing or invalid delivery metadata")
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "Invalid JSON")
        if not isinstance(payload, dict):
            raise HTTPException(400, "Expected a JSON object")
        repository = payload.get("repository")
        if not isinstance(repository, dict):
            raise HTTPException(400, "Invalid repository")
        repo_name, repo_id = repository.get("full_name"), repository.get("id")
        if not isinstance(repo_name, str) or type(repo_id) is not int or repo_id < 1:
            raise HTTPException(400, "Missing repository identity")
        object_key = {"issues": "issue", "pull_request": "pull_request", "pull_request_review": "pull_request", "release": "release", "check_run": "check_run", "check_suite": "check_suite", "workflow_run": "workflow_run"}.get(event_type)
        if object_key and (not isinstance(payload.get(object_key), dict) or not isinstance(payload[object_key].get("id"), int)):
            raise HTTPException(400, "Missing event object")
        if event_type == "pull_request" and (not isinstance(payload.get("number"), int) or payload["number"] < 1):
            raise HTTPException(400, "Invalid PR number")
        if event_type == "status" and not isinstance(payload.get("sha"), str):
            raise HTTPException(400, "Missing status SHA")

        if not connection:
            with Session() as db:
                project_match = db.scalar(select(Project).where(func.lower(Project.repo) == repo_name.lower(), Project.demo.is_(False)))
                connection = db.get(ProjectConnection, project_match.id) if project_match else None
                if not connection:
                    raise HTTPException(404, "Webhook not found")
                project_id = connection.project_id
                secret = unseal(settings, project_id, "webhook", connection.secret_ciphertext)
                webhook_id = connection.webhook_id

        if not verify_signature(raw, request.headers.get("x-hub-signature-256", ""), secret):
            raise HTTPException(401, "Invalid signature")

        with Session() as db:
            connection = db.scalar(select(ProjectConnection).where(ProjectConnection.project_id == project_id).with_for_update())
            project = db.get(Project, project_id)
            if project.demo or project.repo.lower() != repo_name.lower() or any(value and value != str(repo_id) for value in [connection.repository_id, connection.webhook_repository_id]):
                raise HTTPException(403, "Webhook repository does not match this project")
            if event_type != "ping" and event_type not in connection.events:
                return {"accepted": True, "ignored": True}
            scoped_delivery = hashlib.sha256(f"{webhook_id}:{delivery}".encode()).hexdigest()
            db.add(InboxEvent(source="github", delivery_id=scoped_delivery, event_type=event_type, payload=payload,
                              processed_at=utcnow() if event_type == "ping" else None))
            connection.last_delivery_at = utcnow()
            if event_type == "ping":
                connection.webhook_verified = True
                connection.webhook_repository_id = str(repo_id)
            refresh_activation(connection)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                return {"accepted": True, "duplicate": True}
        return {"accepted": True, "duplicate": False}

    @app.post("/api/events/executor", status_code=202)
    async def executor_event(request: Request):
        if settings.app_mode != "live":
            raise HTTPException(409, "Demo mode does not accept real events")
        if not settings.executor_webhook_secret:
            raise HTTPException(503, "Executor reporting is not configured")
        raw = await read_body(request)
        if not verify_signature(raw, request.headers.get("x-executor-signature-256", ""), settings.executor_webhook_secret):
            raise HTTPException(401, "Invalid signature")
        try:
            data = ExecutorEvent.model_validate_json(raw)
        except ValueError:
            raise HTTPException(422, "Invalid executor event")
        with Session() as db:
            if not project_config(settings, db, data.repo, active_only=False):
                raise HTTPException(403, "Repository is not configured")
            job = db.get(Job, data.execution_id)
            record = db.get(Record, job.record_id) if job else None
            if not record or record.repo != data.repo:
                raise HTTPException(404, "Unknown execution for repository")
            allowed_steps = {"review": {"workspace", "review", "notification"}, "merge": {"notification"}, "release": {"workspace", "notes", "apk", "upload", "deploy", "verify", "notification"}}
            if data.step not in allowed_steps.get(job.kind, set()):
                raise HTTPException(422, "Step does not belong to this execution kind")
        return persist_event("executor", data.event_id, data.step, data.model_dump(mode="json"))

    @app.get("/api/stream")
    async def stream(request: Request):
        if settings.require_login:
            with Session() as db:
                require_admin(request, db)
        async def events():
            last = None
            while not await request.is_disconnected():
                with Session() as db:
                    values = (db.scalar(select(func.max(Record.updated_at))), db.scalar(select(func.count()).select_from(Timeline)), list(db.execute(select(Project.repo, Project.last_synced_at, Project.sync_error).order_by(Project.repo))), db.scalar(select(func.max(ProjectConnection.updated_at))), db.scalar(select(func.count()).select_from(ProjectConnection)), db.scalar(select(func.count()).select_from(InboxEvent).where(InboxEvent.error.is_not(None))))
                revision = hashlib.sha256(str(values).encode()).hexdigest()[:16]
                if revision != last:
                    yield f"data: {json.dumps({'revision': revision})}\n\n"
                    last = revision
                else:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(3)
        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/{path:path}")
    def frontend(path: str):
        root = settings.frontend_dist.resolve()
        candidate = (root / path).resolve()
        if path.startswith("api/"):
            raise HTTPException(404, "Unknown API endpoint")
        if candidate.is_relative_to(root) and candidate.is_file():
            return FileResponse(candidate)
        entry = root / "index.html"
        if entry.exists():
            return FileResponse(entry)
        raise HTTPException(503, "Frontend is not built. Run pnpm build in frontend/.")

    return app


app = create_app()
