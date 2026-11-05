from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import re
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import Session
from backend.config import ProjectConfig
from backend.db import Job, Project, Record, Timeline, utcnow


def timestamp(value: str | None) -> datetime:
    if not value:
        return utcnow()
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def verify_signature(raw: bytes, signature: str, secret: str) -> bool:
    if not secret or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def redact(value: str, secrets: list[str] | None = None) -> str:
    for secret in secrets or []:
        if secret:
            value = value.replace(secret, "[REDACTED]")
    value = re.sub(r"(?i)(authorization[\s:=]+(?:bearer|basic)\s+)\S+", r"\1[REDACTED]", value)
    value = re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]+)\b", "[REDACTED]", value)
    return value[:20000]


def ensure_projects(db: Session, projects: list[ProjectConfig]):
    for config in projects:
        project = db.scalar(select(Project).where(func.lower(Project.repo) == config.repo.lower()))
        if not project:
            project = Project(repo=config.repo.lower(), name=config.name or config.repo.split("/")[-1], description=config.description)
            db.add(project)
    db.commit()


def redact_nested(value, secrets):
    if isinstance(value, str):
        return redact(value, secrets)
    if isinstance(value, list):
        return [redact_nested(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: redact_nested(item, secrets) for key, item in value.items()}
    return value


def add_timeline(db: Session, record: Record, stage: str, status: str, title: str, message: str = "", actor: str = "system", job_id: str | None = None, data: dict | None = None, occurred_at: datetime | None = None):
    db.add(Timeline(record_id=record.id, job_id=job_id, stage=stage, status=status, title=title, message=message, actor=actor, data=data or {}, occurred_at=occurred_at or utcnow()))
    record.updated_at = utcnow()


def schedule_job(db: Session, record: Record, kind: str, key: str, payload: dict) -> Job:
    job = db.scalar(select(Job).where(Job.key == key))
    if not job:
        job = Job(record_id=record.id, key=key, kind=kind, payload=payload)
        db.add(job)
        db.flush()
    return job


RETRYABLE_JOB_STATUSES = {"failed", "uncertain", "needs_configuration", "waiting"}


def reusable_agent_checkpoint(result: dict | None, key: str) -> dict:
    result = dict(result or {})
    state = dict(result.get(key) or {})
    if state.get("output") is None:
        return state
    if any(result.get(item) for item in ("pushed", "github_release", "push_intent", "merge_intent")):
        return state
    return {}


def retry_job(db: Session, job: Job | None, *, new_analysis: bool = False, acknowledge_uncertain: bool = False, actor: str = "管理员") -> Job:
    if not job:
        raise LookupError("任务不存在")
    if job.status not in RETRYABLE_JOB_STATUSES:
        raise ValueError("只能重试失败、结果未知、待配置或等待中的任务")
    record = db.get(Record, job.record_id)
    if not record or record.demo:
        raise ValueError("演示记录不能重试")
    result = dict(job.result or {})
    if not new_analysis and job.kind in {"prepare_release", "agent_task"} and not any(result.get(key) for key in ("pushed", "github_release", "push_intent", "merge_intent")):
        new_analysis = True
    if new_analysis:
        if result.get("upload") or result.get("upload_intent"):
            raise ValueError("发布已开始，不能改变同一任务的版本说明；请建立新的 Release")
        for key in ["agent_review", "agent_notes", "agent_agent_task", "review", "notes"]:
            result.pop(key, None)
        result["analysis_generation"] = result.get("analysis_generation", 0) + 1
    if acknowledge_uncertain:
        if not result.get("upload"):
            result.pop("upload_intent", None)
        if not result.get("deploy"):
            result.pop("deploy_intent", None)
    job.result = result
    job.status, job.error = "queued", None
    job.available_at, job.updated_at = utcnow(), utcnow()
    job.worker_id, job.lease_until = None, None
    add_timeline(db, record, job.step or "queued", "queued", "已手动重试任务", job_id=job.id, actor=actor)
    return job


BUG_LABELS = {"bug", "缺陷"}


def is_bug_record(labels: list | None) -> bool:
    return any(str(item).casefold() in BUG_LABELS for item in (labels or []))


def work_type(kind: str, labels: list | None) -> str:
    return "bug" if kind == "issue" and is_bug_record(labels) else kind


def like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def bug_record_clause():
    labels = cast(Record.labels, String)
    return (Record.kind == "issue") & or_(Record.details["bug"].as_boolean().is_(True), labels.ilike('%"bug"%'))


def assignee_clause(assignee: str):
    if assignee == "none":
        return Record.assignees == []
    return cast(Record.assignees, String).ilike(f'%"{like_escape(assignee)}"%', escape="\\")


PRIORITY_LEVELS = ("紧急", "普通", "较低")
PRIORITY_ALIASES = {
    "紧急": "紧急", "严重": "紧急", "p0": "紧急",
    "普通": "普通", "一般": "普通", "中": "普通", "高": "普通", "重要": "普通", "p1": "普通", "p2": "普通",
    "较低": "较低", "低": "较低", "p3": "较低",
}
TITLE_PRIORITY = {"p0": "紧急", "p1": "普通", "p2": "普通", "p3": "较低"}


def priority(labels: list[str], config: ProjectConfig, title: str = "") -> tuple[str | None, bool]:
    mapped: set[str] = set()
    configured = {str(key).casefold(): value for key, value in config.priority_labels.items() if value in PRIORITY_LEVELS}
    for label in labels or []:
        key = str(label).strip().casefold()
        level = configured.get(key) or PRIORITY_ALIASES.get(key)
        if level in PRIORITY_LEVELS:
            mapped.add(level)
    if not mapped:
        match = re.search(r"(?i)\[(p[0-3])\]", title or "")
        if match:
            mapped.add(TITLE_PRIORITY[match.group(1).lower()])
    levels = sorted(mapped, key=PRIORITY_LEVELS.index)
    return (levels[0] if levels else None, len(levels) > 1)


def upsert_github(db: Session, config: ProjectConfig, kind: str, obj: dict, action: str = "sync", actor: str = "GitHub", snapshot: bool = False, trigger: bool = True, canonical: bool = False) -> Record:
    external_id = str(obj.get("id") or obj.get("number"))
    record = db.scalar(select(Record).where(Record.repo == config.repo, Record.kind == kind, Record.external_id == external_id))
    if record is None and kind == "release":
        tag = str(obj.get("tag_name") or "")
        if tag:
            record = db.scalar(select(Record).where(Record.repo == config.repo, Record.kind == "release", Record.external_id == f"prepare:{tag}"))
            if record is not None:
                record.external_id = external_id
    if record and (record.details or {}).get("deleted") and action != "deleted":
        return record
    updated = timestamp(obj.get("updated_at") or obj.get("published_at") or obj.get("created_at"))
    if action == "deleted":
        updated = utcnow()
    if record and record.source_updated_at and aware(record.source_updated_at) > updated and action != "deleted":
        return record
    if record and kind == "pr" and not canonical and record.source_updated_at and aware(record.source_updated_at) == updated and (obj.get("head") or {}).get("sha") != record.details.get("head_sha"):
        return record  # Conflicting same-second events require an authoritative API fetch.
    is_new = record is None
    if record is None:
        record = Record(repo=config.repo, kind=kind, external_id=external_id, title="", created_at=timestamp(obj.get("created_at")))
        db.add(record)
        db.flush()
    previous_sha = record.details.get("head_sha") if record.details else None
    previous_details = dict(record.details or {})
    previous_deleted = bool(previous_details.get("deleted"))
    previous_signature = (record.title, record.status, record.stage, record.assignees, record.labels, previous_sha)
    record.title = obj.get("title") or obj.get("name") or obj.get("tag_name") or "未命名记录"
    record.body = obj.get("body") or ""
    record.url = obj.get("html_url") or ""
    record.number = obj.get("number")
    record.source_updated_at = updated
    record.labels = [x["name"] if isinstance(x, dict) else x for x in obj.get("labels", [])]
    record.assignees = [x["login"] for x in obj.get("assignees", [])]
    record.priority, conflict = priority(record.labels, config, record.title)
    details = {**(record.details or {}), "priority_conflict": conflict, "snapshot": snapshot, "author": (obj.get("user") or obj.get("author") or {}).get("login", ""), "bug": kind == "issue" and is_bug_record(record.labels)}
    if kind == "issue":
        deleted = action == "deleted"
        closed = deleted or obj.get("state") == "closed"
        record.status = "closed" if closed else "waiting"
        record.stage = "closed" if closed else "issue"
        details["github_state"] = "deleted" if deleted else (obj.get("state") or ("closed" if closed else "open"))
        details["deleted"] = deleted
        if closed:
            for old_job in db.scalars(select(Job).where(Job.record_id == record.id, Job.status.in_(["queued", "waiting", "running"]))):
                old_job.status = "superseded"
    elif kind == "pr":
        head = obj.get("head") or {}
        base = obj.get("base") or {}
        sha = head.get("sha") or details.get("head_sha", "")
        details.update({"head_sha": sha, "base_sha": base.get("sha") or details.get("base_sha", ""), "base_ref": base.get("ref") or details.get("base_ref", ""), "head_ref": head.get("ref") or details.get("head_ref", ""), "draft": bool(obj.get("draft")), "changed_files": obj.get("changed_files", details.get("changed_files")), "additions": obj.get("additions", details.get("additions")), "deletions": obj.get("deletions", details.get("deletions"))})
        if obj.get("merged") or obj.get("merged_at"):
            record.status, record.stage = "merged", "merge"
            details["merged_at"] = obj.get("merged_at")
        elif obj.get("state") == "closed":
            record.status, record.stage = "closed", "closed"
        elif details["draft"]:
            record.status, record.stage = "waiting", "draft"
        needs_review = is_new or previous_sha != sha or previous_details.get("base_sha") != details["base_sha"] or (previous_details.get("draft") and not details["draft"]) or (previous_details.get("github_state") == "closed" and obj.get("state") == "open")
        if obj.get("state") == "open" and not details["draft"] and needs_review:
            record.status, record.stage = ("queued", "review") if config.review_enabled else ("not_connected", "review")
            details["review_round"] = previous_details.get("review_round", 0) + (0 if is_new else 1)
            if not is_new:
                details.pop("review", None)
                details.pop("checks", None)
                details["superseded_sha"] = previous_sha
                for old_job in db.scalars(select(Job).where(Job.record_id == record.id, Job.status.in_(["queued", "waiting", "running"]))):
                    old_job.status = "superseded"
        if details["draft"] or obj.get("state") == "closed":
            states = ["queued", "waiting"] if obj.get("merged") or obj.get("merged_at") else ["queued", "waiting", "running"]
            for old_job in db.scalars(select(Job).where(Job.record_id == record.id, Job.status.in_(states))):
                old_job.status = "superseded"
        details["github_state"] = obj.get("state")
        if trigger and config.review_enabled and obj.get("state") != "closed" and not details["draft"] and sha and details["base_ref"] in config.base_branches:
            schedule_job(db, record, "review", f"review:{config.repo}:{record.number}:{sha}:{details['base_sha']}:{details.get('review_round', 0)}", {"head_sha": sha, "base_sha": details["base_sha"], "base_ref": details["base_ref"]})
    else:
        details.update({"tag": obj.get("tag_name", ""), "release_id": obj.get("id"), "prerelease": bool(obj.get("prerelease")), "draft": bool(obj.get("draft")), "assets": [{"id": a["id"], "name": a["name"], "size": a.get("size", 0), "state": a.get("state", "uploaded")} for a in obj.get("assets", [])], "published_at": obj.get("published_at")})
        release_automation = config.release_enabled
        if is_new or previous_details.get("draft") != details["draft"] or previous_details.get("prerelease") != details["prerelease"]:
            record.status, record.stage = ("waiting", "release") if details["draft"] or details["prerelease"] else (("queued", "release") if release_automation else ("not_connected", "release"))
        if details["draft"] or details["prerelease"]:
            for old_job in db.scalars(select(Job).where(Job.record_id == record.id, Job.status.in_(["queued", "waiting", "running"]))):
                old_job.status = "superseded"
        if action == "deleted":
            record.status, record.stage = "closed", "closed"
            for old_job in db.scalars(select(Job).where(Job.record_id == record.id, Job.status.in_(["queued", "waiting", "running"]))):
                old_job.status = "superseded"
        elif trigger and release_automation and action in {"published", "released"} and not details["draft"] and not details["prerelease"]:
            schedule_job(db, record, "release", f"release:{config.repo}:{external_id}", {"release_id": obj.get("id"), "tag": details["tag"]})
    record.details = details
    changed = previous_signature != (record.title, record.status, record.stage, record.assignees, record.labels, details.get("head_sha")) or previous_deleted != bool(details.get("deleted"))
    if is_new or changed or (not snapshot and action not in {"sync", "edited", "labeled", "unlabeled"}):
        titles = {"opened": "创建记录", "synchronize": "PR 收到新的提交", "ready_for_review": "PR 已就绪", "published": "发布 Release", "closed": "Issue 已关闭" if kind == "issue" else "GitHub 状态更新", "deleted": "GitHub 已删除", "reopened": "重新打开", "assigned": "更新负责人", "labeled": "更新标签"}
        if snapshot and action not in {"closed", "deleted"}:
            title = "导入 GitHub 快照"
        else:
            title = titles.get(action, "导入 GitHub 快照" if snapshot else "同步 GitHub 状态")
        add_timeline(db, record, record.stage, record.status, title, "历史快照，仅表示同步时的状态。" if snapshot else "", actor=actor, occurred_at=utcnow() if snapshot else updated)
    from backend.agent_tasks import schedule_agent_rules
    schedule_agent_rules(db, config, record, action, trigger=trigger and not snapshot)
    return record
