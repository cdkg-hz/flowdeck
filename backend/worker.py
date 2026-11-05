from __future__ import annotations

import asyncio
from datetime import timedelta
import json
import logging
import re
import socket
import time
import uuid
from sqlalchemy import func, or_, select
import httpx
from backend.copilot_runtime import CopilotAdapter, SYSTEM_RULES, parse_agent_json
from backend.agent_tasks import RELEASE_TASK_RULES, TASK_SYSTEM_RULES, allowed_push_branch, choose_release_compare, execute_agent_actions, public_repository_url, task_user_payload
from backend.skills import NOTES_SKILL_INSTRUCTION, load_uploaded_skills
from backend.config import Settings, get_settings
from backend.db import InboxEvent, Job, Project, ProjectConnection, Record, Timeline, UploadedSkill, database, initialize_schema, utcnow
from backend.errors import ConfigurationNeeded, LostLease, OutcomeUnknown, Superseded, WaitForDependency
from backend.github import GitHub, previous_published_release_tag
from backend.schemas import AgentTaskResult, NotesResult, ReviewResult
from backend.service import add_timeline, aware, ensure_projects, redact, redact_nested, reusable_agent_checkpoint, schedule_job, timestamp, upsert_github
from backend.workspace import Workspaces, expose_task_context
from backend.credentials import unseal
from backend.projects import connection_active, import_connections, project_config, project_redactions, project_token, verify_connections
from backend.integrations import load_agent

log = logging.getLogger("flowdeck.worker")


class Context:
    def __init__(self, worker: "Worker", job_id: str):
        self.worker, self.id = worker, job_id
        with worker.Session() as db:
            job = db.get(Job, job_id)
            self.attempt = job.attempt

    def owned(self, job: Job, running: bool = True):
        if not job or job.worker_id != self.worker.identity or job.attempt != self.attempt:
            raise LostLease("任务已由其他执行器接管")
        if job.status == "running" and (not job.lease_until or aware(job.lease_until) <= utcnow()):
            raise LostLease("任务租约已到期")
        if running and job.status != "running":
            if job.status == "superseded":
                raise Superseded("任务已被新的状态替代")
            raise LostLease("任务已不处于执行状态")

    def snapshot(self):
        with self.worker.Session() as db:
            job = db.get(Job, self.id)
            self.owned(job, running=False)
            record = db.get(Record, job.record_id)
            return job, record

    def applicable(self, job: Job, record: Record):
        self.owned(job)
        if job.status == "superseded" or (job.kind in {"review", "merge"} and (record.details.get("head_sha") != job.payload.get("head_sha") or record.details.get("draft") or record.status == "closed")):
            raise Superseded("任务已不适用于当前 PR")
        if job.kind == "agent_task" and record.status in {"closed", "merged"}:
            raise Superseded("关联的 GitHub 记录已关闭或删除")
        if job.kind == "agent_task" and job.payload.get("head_sha") and record.kind == "pr" and (record.details.get("head_sha") != job.payload.get("head_sha") or record.details.get("draft") or record.status in {"closed", "merged"}):
            raise Superseded("任务已不适用于当前 PR")
        if job.kind == "release" and (record.status == "closed" or record.details.get("draft") or record.details.get("prerelease")):
            raise Superseded("Release 已撤回或不再是正式版本")

    def guard(self):
        job, record = self.snapshot()
        self.applicable(job, record)

    async def checkpoint(self, key: str, value):
        with self.worker.Session() as db:
            job = db.scalar(select(Job).where(Job.id == self.id).with_for_update())
            self.applicable(job, db.get(Record, job.record_id))
            job.result = {**job.result, key: value}
            job.updated_at = utcnow()
            db.commit()

    async def step(self, stage: str, status: str, title: str, message: str = "", actor: str = "执行服务", details: dict | None = None):
        message = self.worker.sanitize(message)
        with self.worker.Session() as db:
            job = db.scalar(select(Job).where(Job.id == self.id).with_for_update())
            record = db.get(Record, job.record_id)
            self.applicable(job, record)
            steps = {**record.details.get("steps", {}), stage: status}
            record.details = {**record.details, **(details or {}), "steps": steps}
            record.stage = stage
            record.status = "running" if status in {"success", "running"} else status
            job.step = stage
            event_signature = [stage, status, title, message]
            if job.result.get("last_event") != event_signature:
                add_timeline(db, record, stage, status, title, message, actor=actor, job_id=self.id)
                job.result = {**job.result, "last_event": event_signature}
            job.updated_at = utcnow()
            db.commit()

    async def finish(self, status: str, stage: str, details: dict | None = None):
        with self.worker.Session() as db:
            job = db.scalar(select(Job).where(Job.id == self.id).with_for_update())
            record = db.get(Record, job.record_id)
            self.applicable(job, record)
            record.status, record.stage = status, stage
            merged = {**record.details, **(details or {})}
            merged.pop("error", None)
            merged.pop("agent_activity", None)
            record.details = merged
            record.updated_at = utcnow()
            job.status, job.error, job.lease_until = "completed", None, None
            job.updated_at = utcnow()
            db.commit()

    async def note(self, stage: str, status: str, title: str, message: str = "", actor: str = "Agent"):
        message = self.worker.sanitize(message)
        with self.worker.Session() as db:
            job = db.scalar(select(Job).where(Job.id == self.id).with_for_update())
            record = db.get(Record, job.record_id)
            self.applicable(job, record)
            job.step = stage
            event_signature = [stage, status, title, message]
            if job.result.get("last_event") != event_signature:
                add_timeline(db, record, stage, status, title, message, actor=actor, job_id=self.id)
                job.result = {**job.result, "last_event": event_signature}
            job.updated_at = utcnow()
            db.commit()

    async def complete_side_job(self, details: dict | None = None):
        with self.worker.Session() as db:
            job = db.scalar(select(Job).where(Job.id == self.id).with_for_update())
            record = db.get(Record, job.record_id)
            self.applicable(job, record)
            merged = {**record.details, **(details or {})}
            merged.pop("agent_activity", None)
            record.details = merged
            record.updated_at = utcnow()
            job.status, job.error, job.lease_until = "completed", None, None
            job.updated_at = utcnow()
            db.commit()


class Worker:
    def __init__(self, settings: Settings, sessions=None, github=None, agent=None, workspaces=None):
        self.settings = settings
        self.engine, self.Session = database(settings.database_url) if sessions is None else (None, sessions)
        self.github = github or GitHub(settings, sessions=self.Session)
        self.agent = agent or CopilotAdapter(settings, self.Session)
        self.workspaces = workspaces or Workspaces(settings, sessions=self.Session)
        self.identity = f"{socket.gethostname()}-{uuid.uuid4().hex[:10]}"
        self.active: asyncio.Task | None = None
        self.last_sync = 0.0

    def sanitize(self, value: str):
        return redact(value, project_redactions(self.settings, self.Session))

    def _agent_progress_callback(self, ctx: Context, *, stage: str, title: str):
        last_text = ""
        last_at = 0.0
        recent: list[str] = []

        async def on_progress(text: str):
            nonlocal last_text, last_at, recent
            text = self.sanitize(" ".join(str(text or "").split()))
            if not text:
                return
            now = time.monotonic()
            if text == last_text and (now - last_at) < 2:
                return
            last_text, last_at = text, now
            if not recent or recent[-1] != text:
                recent = (recent + [text])[-5:]
            with self.Session() as db:
                job = db.scalar(select(Job).where(Job.id == ctx.id).with_for_update())
                record = db.get(Record, job.record_id) if job else None
                try:
                    if not job or not record:
                        return
                    ctx.owned(job)
                except (LostLease, Superseded):
                    return
                record.details = {**record.details, "agent_activity": {
                    "stage": stage,
                    "title": title,
                    "text": text,
                    "recent": list(recent),
                    "updated_at": utcnow().isoformat(),
                }}
                record.updated_at = utcnow()
                db.commit()

        return on_progress

    async def _clear_agent_activity(self, ctx: Context):
        with self.Session() as db:
            job = db.scalar(select(Job).where(Job.id == ctx.id).with_for_update())
            record = db.get(Record, job.record_id) if job else None
            try:
                if not job or not record:
                    return
                ctx.owned(job, running=False)
            except LostLease:
                return
            if "agent_activity" not in (record.details or {}):
                return
            details = {**record.details}
            details.pop("agent_activity", None)
            record.details = details
            record.updated_at = utcnow()
            db.commit()

    def project(self, repo):
        with self.Session() as db:
            return project_config(self.settings, db, repo)

    async def initialize(self):
        if self.settings.app_mode != "live":
            raise ConfigurationNeeded("演示模式不会运行任务执行器。真实接入请使用独立数据库与 APP_MODE=live。")
        if self.engine:
            initialize_schema(self.engine)
        with self.Session() as db:
            if db.scalar(select(Project).where(Project.demo.is_(True)).limit(1)):
                raise ConfigurationNeeded("不能对演示数据库运行真实执行器")
            ensure_projects(db, self.settings.project_config().projects)
            import_connections(self.settings, db)

    async def process_events(self):
        with self.Session() as db:
            ready_repos = [p.repo.lower() for p in db.scalars(select(Project).where(Project.demo.is_(False))) if connection_active(db.get(ProjectConnection, p.id))]
            eligible = (InboxEvent.source != "github") | func.lower(InboxEvent.payload["repository"]["full_name"].as_string()).in_(ready_repos)
            ids = list(db.scalars(select(InboxEvent.id).where(eligible, InboxEvent.processed_at.is_(None), InboxEvent.available_at <= utcnow()).order_by(InboxEvent.received_at).limit(30)))
        for event_id in ids:
            try:
                await self.process_event(event_id)
            except Exception as exc:
                retryable = isinstance(exc, (httpx.TransportError, WaitForDependency, ConfigurationNeeded)) or (isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {408, 429, 500, 502, 503, 504})
                with self.Session() as db:
                    event = db.get(InboxEvent, event_id)
                    event.error = self.sanitize(str(exc))
                    event.attempt += 1
                    if retryable:
                        event.available_at = utcnow() + timedelta(seconds=min(900, 30 * 2 ** min(event.attempt - 1, 5)))
                    else:
                        event.processed_at = utcnow()
                    db.commit()
                log.warning("事件%s %s: %s", "等待重试" if retryable else "处理失败", event_id, self.sanitize(str(exc)))

    async def process_event(self, event_id: str):
        with self.Session() as db:
            event = db.get(InboxEvent, event_id)
            if not event or event.processed_at:
                return
            source, event_type, payload = event.source, event.event_type, event.payload
        if source == "executor":
            with self.Session() as db:
                event = db.get(InboxEvent, event_id)
                job = db.get(Job, payload["execution_id"])
                record = db.get(Record, job.record_id)
                sequence_key = f"{payload['producer']}:{payload['step']}"
                sequences = dict(job.result.get("external_sequences", {}))
                if payload["sequence"] > sequences.get(sequence_key, -1):
                    sequences[sequence_key] = payload["sequence"]
                    job.result = {**job.result, "external_sequences": sequences}
                    # Callbacks add observations, never authorize a merge or advance the job.
                    add_timeline(db, record, payload["step"], payload["status"], "执行器上报进度", self.sanitize(payload["message"]), payload["producer"], job.id, occurred_at=timestamp(payload["occurred_at"]))
                event.processed_at = utcnow()
                db.commit()
            return
        repo = (payload.get("repository") or {}).get("full_name", "")
        config = self.project(repo)
        if not config:
            with self.Session() as db:
                event = db.get(InboxEvent, event_id)
                event.available_at = utcnow() + timedelta(seconds=30)
                db.commit()
            return
        repo = config.repo
        action = payload.get("action", "")
        obj, kind = None, None
        if config and event_type == "issues":
            obj, kind = payload["issue"], "issue"
        elif config and event_type == "pull_request":
            # Webhook timestamps have only second precision. Reconcile against canonical state.
            obj, kind = await self.github.pull(repo, payload["number"]), "pr"
        elif config and event_type == "release":
            obj, kind = payload["release"], "release"
            if action != "deleted":
                obj = await self.github.request("GET", f"/repos/{repo}/releases/{obj['id']}")
        elif config and event_type in {"check_run", "check_suite", "status", "workflow_run", "pull_request_review"}:
            with self.Session() as db:
                for job in db.scalars(select(Job).join(Record, Job.record_id == Record.id).where(Record.repo == repo, Job.kind == "merge", Job.status == "waiting")):
                    job.available_at = utcnow()
                db.commit()
        previous_column = None
        previous_status = None
        record_id = None
        with self.Session() as db:
            event = db.get(InboxEvent, event_id)
            if obj and kind:
                external_id = str(obj.get("id") or obj.get("number"))
                existing = db.scalar(select(Record).where(Record.repo == repo, Record.kind == kind, Record.external_id == external_id))
                if existing is None and kind == "release":
                    tag = str(obj.get("tag_name") or "")
                    if tag:
                        existing = db.scalar(select(Record).where(Record.repo == repo, Record.kind == "release", Record.external_id == f"prepare:{tag}"))
                if existing is not None:
                    previous_column = self._column(existing)
                    previous_status = existing.status
                record = upsert_github(db, config, kind, obj, action=action, actor=(payload.get("sender") or {}).get("login", "GitHub"), canonical=kind in {"pr", "release"})
                record_id = record.id
            event.processed_at = utcnow()
            event.error = None
            db.commit()
        if record_id:
            with self.Session() as db:
                record = db.get(Record, record_id)
            events = []
            if record.status == "merged" and previous_status != "merged":
                events.append(("merged", "PR 已合并"))
            await self.emit_dingtalk(None, record, events, previous_column=previous_column)
        if kind == "pr":
            await self.refresh_links(config.repo, obj["number"])

    async def refresh_links(self, repo: str, number: int):
        try:
            refs = await self.github.linked_issues(repo, number)
            error = None
        except Exception as exc:
            refs, error = None, self.sanitize(str(exc))
        with self.Session() as db:
            record = db.scalar(select(Record).where(Record.repo == repo, Record.kind == "pr", Record.number == number))
            if record:
                details = {**record.details, "link_error": error}
                if refs is not None:
                    details["closing_issues"] = [{"repo": x["repository"]["nameWithOwner"].lower(), "number": x["number"], "title": x["title"], "url": x["url"]} for x in refs]
                record.details = details
                db.commit()

    @staticmethod
    def _issue_record_payload(record, external_id):
        return {
            "id": int(external_id) if str(external_id).isdigit() else record.number,
            "number": record.number,
            "title": record.title,
            "body": record.body,
            "html_url": record.url,
            "state": "closed",
            "labels": [{"name": item} for item in (record.labels or [])],
            "assignees": [{"login": item} for item in (record.assignees or [])],
        }

    async def reconcile_github_issues(self, config):
        open_ids = set()
        async for issue in self.github.pages(f"/repos/{config.repo}/issues", {"state": "open"}):
            if "pull_request" not in issue and issue.get("id") is not None:
                open_ids.add(str(issue["id"]))
        with self.Session() as db:
            missing = [
                (record.id, record.number, record.external_id, record.status)
                for record in db.scalars(select(Record).where(Record.repo == config.repo, Record.kind == "issue"))
                if record.number and not (record.details or {}).get("deleted") and record.external_id not in open_ids
            ]
        if not missing:
            return
        closed_by_id = {}
        if len(missing) > 15:
            needed = {external_id for _, _, external_id, _ in missing}
            try:
                async for issue in self.github.pages(f"/repos/{config.repo}/issues", {"state": "closed", "sort": "updated", "direction": "desc"}):
                    if "pull_request" in issue or issue.get("id") is None:
                        continue
                    issue_id = str(issue["id"])
                    if issue_id not in needed:
                        continue
                    closed_by_id[issue_id] = issue
                    if len(closed_by_id) == len(needed):
                        break
            except WaitForDependency:
                pass
        for record_id, number, external_id, status in missing:
            obj = closed_by_id.get(external_id)
            if obj is None:
                try:
                    obj = await self.github.request("GET", f"/repos/{config.repo}/issues/{number}")
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code not in {404, 410}:
                        raise
                    with self.Session() as db:
                        record = db.get(Record, record_id)
                        if not record or (record.details or {}).get("deleted"):
                            continue
                        upsert_github(db, config, "issue", self._issue_record_payload(record, external_id), action="deleted", snapshot=True, trigger=False)
                        db.commit()
                    continue
            if not obj or obj.get("pull_request") or obj.get("state") != "closed":
                continue
            if status in {"closed", "merged", "success"}:
                continue
            with self.Session() as db:
                upsert_github(db, config, "issue", obj, action="closed", snapshot=True, trigger=False)
                db.commit()

    async def sync_project(self, config):
        sync_started = utcnow()
        with self.Session() as db:
            project = db.scalar(select(Project).where(Project.repo == config.repo))
            connection = db.get(ProjectConnection, project.id)
            if not connection_active(connection):
                return
            selected = set(connection.events)
            last = aware(project.last_synced_at) if project.last_synced_at else None
        cutoff = last - timedelta(minutes=2) if last else utcnow() - timedelta(days=self.settings.initial_history_days)
        states = ["all"] if last else ["open", "closed"]
        for state in states if {"issues", "pull_request"} & selected else []:
            params = {"state": state, "sort": "updated", "direction": "desc"}
            if state != "open":
                params["since"] = cutoff.isoformat()
            only_pulls = "issues" not in selected
            endpoint = "pulls" if only_pulls else "issues"
            async for issue in self.github.pages(f"/repos/{config.repo}/{endpoint}", params):
                if only_pulls or "pull_request" in issue:
                    if "pull_request" not in selected:
                        continue
                    obj, kind = await self.github.pull(config.repo, issue["number"]), "pr"
                else:
                    obj, kind = issue, "issue"
                with self.Session() as db:
                    upsert_github(db, config, kind, obj, snapshot=True, canonical=kind == "pr")
                    db.commit()
                if kind == "pr":
                    await self.refresh_links(config.repo, obj["number"])
        if "issues" in selected:
            await self.reconcile_github_issues(config)
        async def releases():
            if "release" in selected:
                async for item in self.github.pages(f"/repos/{config.repo}/releases", max_pages=20):
                    yield item
        async for release in releases():
            published = timestamp(release.get("published_at") or release.get("created_at"))
            if published < cutoff:
                continue
            with self.Session() as db:
                missed = bool(last and published >= last and not release.get("draft") and not release.get("prerelease"))
                upsert_github(db, config, "release", release, action="published" if missed else "sync", snapshot=True, trigger=missed, canonical=True)
                db.commit()
        with self.Session() as db:
            project = db.scalar(select(Project).where(Project.repo == config.repo))
            project.last_synced_at, project.sync_error = sync_started, None
            db.commit()

    async def sync(self):
        with self.Session() as db:
            configs = [project_config(self.settings, db, p.repo) for p in db.scalars(select(Project).where(Project.demo.is_(False)))]
        for config in filter(None, configs):
            try:
                await self.sync_project(config)
            except Exception as exc:
                with self.Session() as db:
                    project = db.scalar(select(Project).where(Project.repo == config.repo))
                    if project:
                        project.sync_error = self.sanitize(str(exc))
                        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {401, 403, 404}:
                            connection = db.get(ProjectConnection, project.id)
                            connection.api_status, connection.api_error = "error", "GitHub 读取失败，请重新验证项目授权"
                            connection.verify_requested, connection.next_verify_at = True, utcnow() + timedelta(seconds=60)
                            connection.updated_at = utcnow()
                        db.commit()

    def claim(self):
        with self.Session() as db:
            for expired in db.scalars(select(Job).where(Job.status == "running", Job.lease_until < utcnow()).with_for_update(skip_locked=True)):
                expired.status = "queued"
                expired.worker_id, expired.lease_until = None, None
            ready_repos = [p.repo for p in db.scalars(select(Project).where(Project.demo.is_(False))) if connection_active(db.get(ProjectConnection, p.id))]
            query = select(Job).join(Record, Job.record_id == Record.id).where(Record.repo.in_(ready_repos), Job.status.in_(["queued", "waiting"]), Job.available_at <= utcnow()).order_by(Job.created_at).with_for_update(skip_locked=True).limit(1)
            job = db.scalar(query)
            if not job:
                db.commit()
                return None
            job.status, job.worker_id = "running", self.identity
            job.attempt += 1
            job.lease_until = utcnow() + timedelta(seconds=120)
            job.updated_at = utcnow()
            db.commit()
            return job.id

    async def heartbeat(self, ctx: Context):
        while True:
            await asyncio.sleep(20)
            with self.Session() as db:
                job = db.scalar(select(Job).where(Job.id == ctx.id).with_for_update())
                try:
                    ctx.owned(job)
                except (LostLease, Superseded):
                    return
                job.lease_until = utcnow() + timedelta(seconds=120)
                db.commit()

    async def analyze(self, ctx: Context, config, task_type: str | None = None, schema=None, payload: dict | None = None):
        # Keep the old custom-adapter call shape usable while migrating integrations.
        if payload is None:
            payload, schema, task_type, config = schema, task_type, config, None
        assert task_type is not None and schema is not None and payload is not None
        job, record = ctx.snapshot()
        key = "agent_" + task_type
        async def save(state):
            await ctx.checkpoint(key, state)
        safe_payload = {**payload}
        if isinstance(safe_payload.get("workspace"), dict):
            safe_payload["workspace"] = {key: value for key, value in safe_payload["workspace"].items() if key != "host_workspace"}
        if isinstance(self.agent, CopilotAdapter):
            with self.Session() as db:
                project = db.scalar(select(Project).where(Project.repo == record.repo))
                runtime_config = config or project_config(self.settings, db, record.repo)
                runtime = load_agent(self.settings, db, runtime_config.agent_id if runtime_config else "")
                if not project:
                    raise ConfigurationNeeded("项目不存在，无法加载 Agent 配置")
            title = {"review": "审查代码", "notes": "生成更新记录"}.get(task_type, "Agent 任务")
            progress = self._agent_progress_callback(ctx, stage=task_type, title=title)
            try:
                text = await self.agent.execute(task_type, f"{job.id}-{job.result.get('analysis_generation', 0)}", SYSTEM_RULES + "\nJSON Schema:\n" + json.dumps(schema.model_json_schema(), ensure_ascii=False), json.dumps(safe_payload, ensure_ascii=False), job.result.get(key, {}), save, agent=runtime, workspace=self.workspaces.task_dir(job.id), sandbox="read_only", output_schema=schema.model_json_schema(), secret_values=[runtime.api_key], on_progress=progress)
            finally:
                await self._clear_agent_activity(ctx)
        else:
            # Test and custom adapters keep the original narrow protocol.
            text = await self.agent.execute(task_type, f"{job.id}-{job.result.get('analysis_generation', 0)}", SYSTEM_RULES + "\nJSON Schema:\n" + json.dumps(schema.model_json_schema(), ensure_ascii=False), json.dumps(safe_payload, ensure_ascii=False), job.result.get(key, {}), save)
        try:
            result = parse_agent_json(text, schema)
        except ValueError as exc:
            raise RuntimeError("Agent 输出不符合约定结构，不能据此通过审查或发布") from exc
        return schema.model_validate(redact_nested(result.model_dump(), project_redactions(self.settings, self.Session)))

    async def current_pull(self, record: Record, job: Job):
        pull = await self.github.pull(record.repo, record.number)
        if pull["head"]["sha"] != job.payload["head_sha"] or pull.get("draft") or (pull.get("state") != "open" and not pull.get("merged")):
            raise Superseded("PR 提交或状态已变化，本次任务结论不再适用")
        if not pull.get("merged") and pull["base"]["sha"] != job.payload["base_sha"]:
            with self.Session() as db:
                config = project_config(self.settings, db, record.repo)
                if config:
                    upsert_github(db, config, "pr", pull, canonical=True)
                    db.commit()
            raise Superseded("PR 目标分支已更新，需要重新审查")
        return pull

    def _column(self, record):
        from backend.board import board_column
        return board_column(
            status=record.status, kind=record.kind, stage=record.stage,
            deleted=bool((record.details or {}).get("deleted")),
        )

    async def emit_dingtalk(self, ctx, record, events, job=None, previous_column=None):
        from backend.board import COLUMN_LABELS
        from backend.dingtalk import dispatch_dingtalk
        payload = list(events)
        current = self._column(record)
        if previous_column is not None and previous_column != current:
            payload.append((
                "board_column_change",
                f"看板从「{COLUMN_LABELS[previous_column]}」变为「{COLUMN_LABELS[current]}」",
            ))
        await dispatch_dingtalk(self.settings, self.Session, record, payload, job)

    async def review(self, ctx: Context, config):
        job, record = ctx.snapshot()
        if not config.review_enabled:
            raise ConfigurationNeeded("该项目尚未启用代码审查")
        pull = await self.current_pull(record, job)
        if pull.get("merged"):
            previous_column = self._column(ctx.snapshot()[1])
            await ctx.finish("merged", "merge")
            job, record = ctx.snapshot()
            await self.emit_dingtalk(ctx, record, [("merged", "PR 已合并")], job, previous_column)
            return
        if pull["base"]["ref"] not in config.base_branches:
            raise ConfigurationNeeded("PR 目标分支不在允许的审查范围")
        await ctx.step("workspace", "running", "准备本次提交的代码工作区")
        if not job.result.get("workspace"):
            workspace = await self.workspaces.prepare(job.id, record.repo, job.payload["base_sha"], job.payload["head_sha"], record.number)
            await ctx.checkpoint("workspace", workspace)
        else:
            workspace = job.result["workspace"]
        await ctx.step("workspace", "success", "代码工作区准备完成", f"提交 {job.payload['head_sha'][:12]}，{len(workspace['changed_files'])} 个改动文件")
        await self.github.set_review_status(record.repo, job.payload["head_sha"], "pending", "Agent 正在审查此提交")
        await ctx.step("review", "running", "Agent 正在审查代码", actor="Agent")
        result = await self.analyze(ctx, config, "review", ReviewResult, {"task": "review_pr", "workspace": workspace, "pr": {"number": record.number, "title": pull["title"], "description": pull.get("body") or ""}, "review_rules": config.review_rules, "instruction": "先阅读changes.diff，再阅读code和base中的相关实现、调用点与测试。findings.file必须是code下的真实相对路径，line必须定位到当前文件。无法访问代码时verdict必须为inconclusive。"})
        if result.head_sha != job.payload["head_sha"]:
            raise RuntimeError("Agent 返回的提交 SHA 与本次任务不一致")
        root = self.workspaces.task_dir(job.id) / "code"
        for finding in result.findings:
            file = (root / finding.file).resolve()
            if not file.is_relative_to(root.resolve()) or not file.is_file():
                raise RuntimeError("审查问题引用了不存在或不允许的文件")
            if finding.line > len(file.read_text(encoding="utf-8", errors="replace").splitlines()):
                raise RuntimeError("审查问题引用了不存在的行")
        await self.current_pull(record, job)
        ctx.guard()
        review = result.model_dump()
        review["base_sha"] = job.payload["base_sha"]
        await ctx.checkpoint("review", review)
        verdict_status = "success" if result.verdict == "pass" else "failure" if result.verdict == "changes_requested" else "error"
        await self.github.set_review_status(record.repo, result.head_sha, verdict_status, "审查通过" if result.verdict == "pass" else "发现问题，需要处理" if result.verdict == "changes_requested" else "无法形成可靠审查结论")
        body = f"### Flowdeck 代码审查\n\n提交：`{result.head_sha}`\n\n{result.summary}\n"
        for finding in result.findings:
            body += f"\n- **[{finding.severity}] {finding.title}** — `{finding.file}:{finding.line}`\n  {finding.explanation}\n  建议：{finding.suggestion}\n"
        ctx.guard()
        await self.github.comment_once(record.repo, record.number, f"<!-- flowdeck:review:{job.id} -->", body.replace("@", "@\u200b"))
        await ctx.step("review", "success" if result.verdict == "pass" else "changes_requested", "Agent 审查通过" if result.verdict == "pass" else "Agent 审查需要处理", result.summary, "Agent", {"review": review})
        previous_column = self._column(ctx.snapshot()[1])
        if result.verdict != "pass":
            await ctx.finish("changes_requested" if result.verdict == "changes_requested" else "uncertain", "review", {"review": review})
            job, record = ctx.snapshot()
            await self.emit_dingtalk(ctx, record, [("review_needs_attention", "PR 审查需要处理：" + result.summary)], job, previous_column)
            return
        if config.merge_enabled:
            with self.Session() as db:
                ctx.applicable(db.get(Job, job.id), db.get(Record, record.id))
                current = db.get(Record, record.id)
                schedule_job(db, current, "merge", f"merge:{job.id}", {**job.payload, "review_job_id": job.id})
                db.commit()
            await ctx.finish("waiting", "checks", {"review": review})
        else:
            await ctx.finish("review_passed", "review", {"review": review})
        job, record = ctx.snapshot()
        await self.emit_dingtalk(ctx, record, [("review_passed", "审查通过：" + result.summary)], job, previous_column)

    async def merge(self, ctx: Context, config):
        job, record = ctx.snapshot()
        previous_column = self._column(record)
        if not config.merge_enabled or not config.review_enabled:
            raise ConfigurationNeeded("自动合并需要先启用内置审查")
        pull = await self.current_pull(record, job)
        if pull.get("merged"):
            await ctx.finish("merged", "merge")
            job, record = ctx.snapshot()
            await self.emit_dingtalk(ctx, record, [("merged", "PR 已合并")], job, previous_column)
            return
        review = record.details.get("review", {})
        if review.get("verdict") != "pass" or review.get("head_sha") != pull["head"]["sha"]:
            raise WaitForDependency("当前提交尚无通过的 Agent 审查结论")
        if pull["base"]["sha"] != review.get("base_sha"):
            with self.Session() as db:
                upsert_github(db, config, "pr", pull, canonical=True)
                db.commit()
            raise Superseded("目标分支已更新，需要基于新基线重新审查")
        if config.required_checks:
            checks = await self.github.checks(record.repo, job.payload["head_sha"])
            await ctx.step("checks", "running", "核对当前提交的必需检查", details={"checks": checks})
            states = {x["name"]: x["state"] for x in checks}
            failed = [name for name in config.required_checks if states.get(name) == "failure"]
            if failed:
                job, record = ctx.snapshot()
                await self.emit_dingtalk(
                    ctx, record, [("checks_failed", "必需检查失败：" + "、".join(failed))], job, self._column(record),
                )
                raise WaitForDependency("必需检查失败：" + "、".join(failed))
            pending = [name for name in config.required_checks if states.get(name) != "success"]
            if pending:
                raise WaitForDependency("等待必需检查：" + "、".join(pending))
            await ctx.step("checks", "success", "当前提交的全部必需检查已通过")
        if pull.get("mergeable") is not True or pull.get("mergeable_state") not in {"clean", "unstable", "has_hooks"}:
            raise WaitForDependency("GitHub 的分支保护、审查或合并条件尚未满足")
        await self.current_pull(record, job)
        await ctx.checkpoint("merge_intent", {"sha": job.payload["head_sha"]})
        merged = await self.github.merge(record.repo, record.number, job.payload["head_sha"], config.merge_method)
        await ctx.checkpoint("merge", merged)
        await ctx.step("merge", "success", "GitHub 已确认代码合并")
        await ctx.finish("merged", "merge")
        job, record = ctx.snapshot()
        await self.emit_dingtalk(ctx, record, [("merged", "PR 已合并")], job, previous_column)

    async def current_release(self, ctx: Context, head_sha: str | None = None):
        job, record = ctx.snapshot()
        ctx.applicable(job, record)
        try:
            release = await self.github.request("GET", f"/repos/{record.repo}/releases/{job.payload['release_id']}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise Superseded("本次 Release 已删除或不再可访问") from exc
            raise
        if release.get("draft") or release.get("prerelease") or release["tag_name"] != job.payload["tag"]:
            raise Superseded("Release 已撤回、改为预发布或更换 Tag")
        if head_sha is not None:
            commit = await self.github.commit(record.repo, job.payload["tag"])
            if commit["sha"] != head_sha:
                raise Superseded("Release Tag 指向的代码已变化，本次版本分析不再适用")
        ctx.guard()
        return release

    async def release(self, ctx: Context, config):
        job, record = ctx.snapshot()
        previous_column = self._column(record)
        if not config.release_enabled:
            raise ConfigurationNeeded("该项目尚未启用发布流程")
        release = await self.current_release(ctx)
        job, record = ctx.snapshot()
        workspace = job.result.get("workspace")
        if not workspace:
            await ctx.step("workspace", "running", "准备 Release 对应的代码快照")
            previous = await previous_published_release_tag(
                self.github,
                record.repo,
                release_id=release["id"],
                published_at=release.get("published_at") or "",
                configured=config.previous_tag,
            )
            if not previous:
                raise ConfigurationNeeded("首次发布需要配置 previous_tag 作为更新记录的比较基线")
            base, head = await asyncio.gather(self.github.commit(record.repo, previous), self.github.commit(record.repo, job.payload["tag"]))
            workspace = await self.workspaces.prepare(job.id, record.repo, base["sha"], head["sha"])
            workspace["previous_tag"] = previous
            await ctx.checkpoint("workspace", workspace)
        await ctx.step("workspace", "success", "版本代码工作区已准备", details={"base_tag": workspace.get("previous_tag", ""), "head_sha": workspace["head_sha"], "base_sha": workspace["base_sha"]})
        job, _ = ctx.snapshot()
        notes = job.result.get("notes") or {"public_notes": "", "technical_notes": ""}
        if not job.result.get("notes"):
            await ctx.step("notes", "running", "Agent 正在分析版本变化", actor="Agent")
            result = await self.analyze(ctx, config, "notes", NotesResult, {
                "task": "release_notes",
                "workspace": workspace,
                "tag": job.payload["tag"],
                "version": {},
                "skills": [],
                "instruction": NOTES_SKILL_INSTRUCTION,
            })
            if result.head_sha != workspace["head_sha"] or result.base_sha != workspace["base_sha"]:
                raise RuntimeError("版本记录对应的提交范围不一致")
            evidence = set(workspace["changed_files"])
            commits_path = self.workspaces.task_dir(job.id) / "commits.txt"
            evidence.update(line.split(" ", 1)[0] for line in commits_path.read_text(encoding="utf-8").splitlines())
            if any(any(ref not in evidence for ref in change.evidence) for change in result.changes):
                raise RuntimeError("版本记录包含无法追溯到本次变更的依据")
            notes = result.model_dump()
            await ctx.checkpoint("notes", notes)
        await self.current_release(ctx, workspace["head_sha"])
        await self.github.update_notes(record.repo, job.payload["release_id"], notes["public_notes"], notes["technical_notes"])
        await ctx.step("notes", "success", "更新记录已生成并写入 GitHub", actor="Agent", details={"public_notes": notes["public_notes"], "technical_notes": notes["technical_notes"]})
        await ctx.step("verify", "success", "已完成启用的 Release 自动化步骤")
        await ctx.finish("success", "verify")
        job, record = ctx.snapshot()
        await self.emit_dingtalk(ctx, record, [("release_ready", "发版已完成")], job, previous_column)

    async def _agent_task_sha(self, record: Record, job: Job) -> str:
        if job.payload.get("head_sha"):
            return job.payload["head_sha"]
        if record.kind == "pr" and record.details.get("head_sha"):
            return record.details["head_sha"]
        ref = job.payload.get("default_branch") or record.details.get("base_ref") or record.details.get("tag")
        if not ref:
            repo = await self.github.request("GET", f"/repos/{record.repo}")
            ref = repo.get("default_branch") or "main"
        commit = await self.github.commit(record.repo, ref)
        return commit["sha"]

    async def _execute_agent_rule(self, ctx: Context, config, rule: dict) -> dict:
        job, record = ctx.snapshot()
        with self.Session() as db:
            runtime = load_agent(self.settings, db, rule.get("agent_id") or "")
        event = str(rule.get("event") or "")
        write = event in {"pull_request", "release"}
        root = self.workspaces.task_dir(job.id)
        workspace = job.result.get("workspace")
        sha = ""
        previous_tag = ""
        if record.kind in {"pr", "release"} or write:
            sha = await self._agent_task_sha(record, job)
            if record.kind == "release":
                configured = (config.previous_tag or "").strip()
                docs_fn = getattr(self.workspaces, "docs_baseline", None)
                docs = {"sha": "", "docs_date": "", "files": []}
                if callable(docs_fn):
                    docs = await docs_fn(job.id, record.repo, sha)
                github_previous = "" if configured else await previous_published_release_tag(
                    self.github,
                    record.repo,
                    release_id=record.details.get("release_id"),
                    published_at=str(record.details.get("published_at") or ""),
                )
                previous_tag = configured or github_previous
                previous_sha = ""
                configured_sha = ""
                if configured:
                    configured_sha = (await self.github.commit(record.repo, configured))["sha"]
                    previous_sha = configured_sha
                elif previous_tag:
                    previous_sha = (await self.github.commit(record.repo, previous_tag))["sha"]
                compare = choose_release_compare(
                    configured_sha=configured_sha,
                    docs_sha=str(docs.get("sha") or ""),
                    docs_date=str(docs.get("docs_date") or ""),
                    previous_sha=previous_sha,
                    previous_tag=previous_tag,
                    head_sha=sha,
                    configured_tag=configured,
                )
                base_sha = compare["base_sha"]
                previous_tag = compare["previous_tag"]
            else:
                base_sha = job.payload.get("base_sha") or sha
            if not workspace:
                await ctx.note("workspace", "running", "准备代码工作区")
                number = record.number if record.kind == "pr" else None
                workspace = await self.workspaces.prepare(job.id, record.repo, base_sha, sha, number)
                if record.kind == "release":
                    workspace = {
                        **workspace,
                        "previous_tag": previous_tag,
                        "baseline_date": str(docs.get("docs_date") or ""),
                        "baseline_docs": list(docs.get("files") or []),
                        "compare_source": compare["compare_source"],
                    }
                await ctx.checkpoint("workspace", workspace)
        clone_url = public_repository_url(self.settings.github_git_origin, record.repo)
        branch, git_env, token = "", {}, ""
        if write:
            branch = allowed_push_branch(rule, record)
            if not sha:
                sha = await self._agent_task_sha(record, job)
            await self.workspaces.prepare_worktree(job.id, record.repo, sha=sha, branch=branch)
            token = project_token(self.settings, self.Session, record.repo, require_active=True)
            git_env = self.workspaces.env(token)
            expose_task_context(root)
        skill_ids = list(rule.get("skill_ids") or [])
        with self.Session() as db:
            rows = list(db.scalars(select(UploadedSkill).where(UploadedSkill.slug.in_(skill_ids)))) if skill_ids else []
        skills = load_uploaded_skills(self.settings.data_root, rows, skill_ids)
        payload = task_user_payload(
            record=record, rule=rule, workspace=workspace, skills=skills,
            include_repository=True, clone_url=clone_url + ".git", event_action=str(rule.get("github_action") or ""),
        )
        await ctx.note("agent_task", "running", f"Agent「{rule.get('rule_name') or '任务'}」执行中", actor="Agent")
        instructions = TASK_SYSTEM_RULES
        if event == "release":
            instructions += "\n" + RELEASE_TASK_RULES
        instructions += "\nJSON Schema:\n" + json.dumps(AgentTaskResult.model_json_schema(), ensure_ascii=False)
        user = json.dumps(payload, ensure_ascii=False)
        task_id = f"{job.id}-{job.result.get('analysis_generation', 0)}"
        state = reusable_agent_checkpoint(job.result, "agent_agent_task")
        async def save(value):
            await ctx.checkpoint("agent_agent_task", value)
        if isinstance(self.agent, CopilotAdapter):
            progress = self._agent_progress_callback(ctx, stage="agent_task", title=str(rule.get("rule_name") or "任务"))
            try:
                text = await self.agent.execute(
                    "agent_task", task_id, instructions, user, state, save, agent=runtime,
                    workspace=(root / "work") if write else root,
                    sandbox="workspace_write" if write else "read_only",
                    output_schema=AgentTaskResult.model_json_schema(),
                    secret_values=[value for value in [runtime.api_key, token] if value],
                    on_progress=progress,
                )
            finally:
                await self._clear_agent_activity(ctx)
        else:
            text = await self.agent.execute("agent_task", task_id, instructions, user, state, save)
        try:
            parsed = parse_agent_json(text, AgentTaskResult)
        except ValueError as exc:
            raise RuntimeError("Agent 未返回约定的结构化结果，无法执行合并或文档推送") from exc
        if write and event == "pull_request" and parsed.pull_request and parsed.pull_request.merge_requested:
            await ctx.checkpoint("merge_intent", {"number": parsed.pull_request.number or record.number, "sha": job.payload.get("head_sha") or record.details.get("head_sha") or ""})
        if write and event == "release" and parsed.release_docs and parsed.release_docs.push_requested:
            await ctx.checkpoint("push_intent", {"branch": branch, "paths": list(parsed.release_docs.paths)})
        executed = await execute_agent_actions(
            self.github, record.repo, parsed, event=event,
            pull_number=record.number if record.kind == "pr" else None,
            head_sha=job.payload.get("head_sha") or record.details.get("head_sha") or "",
            merge_method=config.merge_method, work_root=root / "work", git_env=git_env,
            allowed_branch=branch, allow_write=write, rule_name=str(rule.get("rule_name") or ""),
        )
        job, record = ctx.snapshot()
        stored = dict(record.details.get("agent_results") or {})
        stored[rule.get("rule_id") or job.id] = {
            "name": rule.get("rule_name") or "",
            "outcome": parsed.outcome,
            "summary": parsed.summary,
            "executed": executed,
        }
        await ctx.note(
            "agent_task",
            "success" if parsed.outcome == "completed" else parsed.outcome,
            f"Agent「{rule.get('rule_name') or '任务'}」已返回结构化结果",
            parsed.summary,
        )
        with self.Session() as db:
            current = db.get(Record, record.id)
            current.details = {**current.details, "agent_results": stored}
            current.updated_at = utcnow()
            db.commit()
        return executed

    async def run_agent_task(self, ctx: Context, config):
        await self._execute_agent_rule(ctx, config, ctx.snapshot()[0].payload)
        _job, record = ctx.snapshot()
        await ctx.complete_side_job({"agent_results": dict(record.details.get("agent_results") or {})})

    async def prepare_release(self, ctx: Context, config):
        job, record = ctx.snapshot()
        if job.result.get("github_release"):
            await self._finalize_prepared_release(ctx, config, job.result["github_release"])
            return
        rules = list(job.payload.get("rules") or [])
        if not rules:
            raise ConfigurationNeeded("请先在项目接入中启用总结并发布")
        executed = None
        for item in rules:
            rule = {
                "rule_id": item.get("id") or item.get("rule_id") or "",
                "rule_name": item.get("name") or item.get("rule_name") or "",
                "event": "release",
                "github_action": "prepare",
                "agent_id": item.get("agent_id") or "",
                "prompt": item.get("prompt") or "",
                "include_repository": True,
                "include_github_credentials": True,
                "skill_ids": list(item.get("skill_ids") or []),
                "push_branch": job.payload.get("default_branch") or job.payload.get("push_branch") or "",
                "default_branch": job.payload.get("default_branch") or "",
                "tag": job.payload.get("tag") or record.details.get("tag") or "",
            }
            executed = await self._execute_agent_rule(ctx, config, rule)
            docs = (executed or {}).get("release_docs") or {}
            if not docs.get("pushed"):
                raise RuntimeError("总结并发版必须请求推送变更")
            await ctx.checkpoint("pushed", {"sha": docs.get("commit_sha") or "", "branch": docs.get("branch") or "", "paths": list(docs.get("paths") or [])})
            job, record = ctx.snapshot()
        sha = ((executed or {}).get("release_docs") or {}).get("commit_sha") or (job.result.get("pushed") or {}).get("sha") or ""
        if not re.fullmatch(r"[0-9a-f]{40}", str(sha)):
            raise RuntimeError("推送后缺少提交 SHA，无法打 tag")
        release = await self.github.release_by_tag(record.repo, job.payload["tag"])
        if not release:
            release = await self.github.create_release(record.repo, job.payload["tag"], sha, job.payload["tag"])
        await ctx.checkpoint("github_release", {
            "id": release.get("id"),
            "tag_name": release.get("tag_name") or job.payload["tag"],
            "name": release.get("name") or job.payload["tag"],
            "body": release.get("body") or "",
            "html_url": release.get("html_url") or "",
            "draft": bool(release.get("draft")),
            "prerelease": bool(release.get("prerelease")),
            "published_at": release.get("published_at") or release.get("created_at") or utcnow().isoformat(),
            "created_at": release.get("created_at") or release.get("published_at") or utcnow().isoformat(),
            "assets": release.get("assets") or [],
        })
        await self._finalize_prepared_release(ctx, config, release)

    async def _finalize_prepared_release(self, ctx: Context, config, release: dict):
        job, record = ctx.snapshot()
        payload = {
            "id": release.get("id"),
            "tag_name": release.get("tag_name") or job.payload.get("tag") or record.details.get("tag"),
            "name": release.get("name") or release.get("tag_name") or job.payload.get("tag"),
            "body": release.get("body") or "",
            "html_url": release.get("html_url") or "",
            "draft": bool(release.get("draft")),
            "prerelease": bool(release.get("prerelease")),
            "published_at": release.get("published_at") or release.get("created_at"),
            "created_at": release.get("created_at") or release.get("published_at"),
            "assets": release.get("assets") or [],
        }
        with self.Session() as db:
            upsert_github(db, config, "release", payload, "published", actor="Flowdeck", canonical=True)
            db.commit()
        await ctx.complete_side_job()

    async def run_job(self, job_id):
        ctx = Context(self, job_id)
        heartbeat = asyncio.create_task(self.heartbeat(ctx))
        config = None
        try:
            job, record = ctx.snapshot()
            ctx.applicable(job, record)
            config = self.project(record.repo)
            if not config:
                raise WaitForDependency("项目接入尚未就绪，等待验证通过")
            if job.kind == "review":
                await self.review(ctx, config)
            elif job.kind == "merge":
                await self.merge(ctx, config)
            elif job.kind == "release":
                await self.release(ctx, config)
            elif job.kind == "agent_task":
                await self.run_agent_task(ctx, config)
            elif job.kind == "prepare_release":
                await self.prepare_release(ctx, config)
            else:
                raise ConfigurationNeeded("未知任务类型")
        except LostLease:
            log.warning("执行器已失去任务租约，停止更新 %s", job_id)
        except Superseded as exc:
            with self.Session() as db:
                job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
                try:
                    ctx.owned(job, running=False)
                except LostLease:
                    return
                job.status, job.error, job.lease_until = "superseded", str(exc), None
                db.commit()
        except Exception as exc:
            message = self.sanitize(str(exc))
            transient = isinstance(exc, (WaitForDependency, httpx.TransportError)) or (isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {408, 429, 500, 502, 503, 504})
            status = "needs_configuration" if isinstance(exc, ConfigurationNeeded) else "uncertain" if isinstance(exc, OutcomeUnknown) else "waiting" if transient else "failed"
            previous_column = None
            with self.Session() as db:
                job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
                record = db.get(Record, job.record_id)
                try:
                    ctx.applicable(job, record)
                except LostLease:
                    return
                except Superseded:
                    job.status, job.lease_until = "superseded", None
                    db.commit()
                    return
                previous_column = self._column(record)
                job.status, job.error, job.lease_until = status, message, None
                job.available_at = utcnow() + timedelta(seconds=min(300, 30 * max(1, job.attempt)))
                if job.kind == "agent_task":
                    results = dict(record.details.get("agent_results") or {})
                    results[job.payload.get("rule_id") or job.id] = {"outcome": status, "error": message, "name": job.payload.get("rule_name") or ""}
                    record.details = {**record.details, "agent_results": results}
                else:
                    record.status = status
                    record.details = {**record.details, "error": message, "steps": {**record.details.get("steps", {}), job.step: status}}
                if "agent_activity" in (record.details or {}):
                    record.details = {key: value for key, value in record.details.items() if key != "agent_activity"}
                job.updated_at = record.updated_at = utcnow()
                signature = [job.step, status, "任务等待条件" if status == "waiting" else "任务需要处理", message]
                if job.result.get("last_event") != signature:
                    add_timeline(db, record, job.step, status, signature[2], message, job_id=job.id)
                    job.result = {**job.result, "last_event": signature}
                db.commit()
            if config and status in {"failed", "uncertain", "needs_configuration"}:
                job, record = ctx.snapshot()
                await self.emit_dingtalk(ctx, record, [("job_failed", message)], job, previous_column)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def run(self):
        await self.initialize()
        sync_task = None
        verification_task = None
        try:
            while True:
                if verification_task is None or verification_task.done():
                    if verification_task and await verification_task:
                        self.last_sync = 0.0
                    verification_task = asyncio.create_task(verify_connections(self.settings, self.Session))
                await self.process_events()
                if self.active and self.active.done():
                    await self.active
                    self.active = None
                if self.active is None:
                    job_id = self.claim()
                    if job_id:
                        self.active = asyncio.create_task(self.run_job(job_id))
                now = asyncio.get_running_loop().time()
                with self.Session() as db:
                    initial_sync = any(connection_active(db.get(ProjectConnection, p.id)) for p in db.scalars(select(Project).where(Project.last_synced_at.is_(None), Project.sync_error.is_(None))))
                if (not sync_task or sync_task.done()) and (initial_sync or now - self.last_sync >= self.settings.sync_interval_seconds):
                    self.last_sync = now
                    sync_task = asyncio.create_task(self.sync())
                await asyncio.sleep(self.settings.worker_poll_seconds)
        finally:
            tasks = [task for task in [self.active, sync_task, verification_task] if task]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.github.close()
            await self.agent.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(Worker(get_settings()).run())
