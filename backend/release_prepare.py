from __future__ import annotations

import re

from pydantic import BaseModel, Field
from sqlalchemy import select

from backend.config import PrepareReleaseConfig, ProjectConfig
from backend.db import Job, Record, utcnow
from backend.errors import ConfigurationNeeded
from backend.service import RETRYABLE_JOB_STATUSES, add_timeline, retry_job, schedule_job

PREPARE_TAG_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
ACTIVE_PREPARE_STATUSES = {"queued", "waiting", "running"}
DEFAULT_PREPARE_PROMPT = "按已选 Skill 总结当前功能并改文件。完成后列出变更路径并请求推送。"


class PrepareReleaseInput(BaseModel):
    tag: str = Field(min_length=1, max_length=64)


def validate_prepare_tag(tag: str) -> str:
    value = str(tag or "").strip()
    if not PREPARE_TAG_PATTERN.fullmatch(value):
        raise ValueError("tag 格式无效，只能使用字母、数字、点、下划线和短横线")
    return value


def adopt_prepare_release(overrides: dict | None) -> dict:
    data = dict(overrides or {})
    if "prepare_release" in data:
        data["prepare_release"] = PrepareReleaseConfig.model_validate(data.get("prepare_release") or {}).model_dump()
        return data
    rules = [item for item in (data.get("agent_rules") or []) if isinstance(item, dict)]
    release_rules = [item for item in rules if item.get("event") == "release"]
    source = next((item for item in release_rules if item.get("enabled")), release_rules[0] if release_rules else None)
    if source:
        data["agent_rules"] = [item for item in rules if item.get("event") != "release"]
        data["prepare_release"] = PrepareReleaseConfig(
            enabled=bool(source.get("enabled")),
            agent_id=str(source.get("agent_id") or ""),
            prompt=str(source.get("prompt") or "").strip() or DEFAULT_PREPARE_PROMPT,
            skill_ids=list(source.get("skill_ids") or []),
        ).model_dump()
    else:
        data["prepare_release"] = PrepareReleaseConfig().model_dump()
    return data


def prepare_release_ready(config: ProjectConfig | None) -> bool:
    feature = getattr(config, "prepare_release", None) if config else None
    return bool(feature and feature.enabled and feature.agent_id and feature.prompt)


def prepare_record_external_id(tag: str) -> str:
    return f"prepare:{validate_prepare_tag(tag)}"


async def inspect_github_prepare(github, repo: str, tag: str, base_branches: list[str]) -> dict:
    info = await github.request("GET", f"/repos/{repo}")
    default_branch = str(info.get("default_branch") or "main")
    allowed = {str(item) for item in (base_branches or []) if str(item).strip()}
    if default_branch not in allowed:
        raise ConfigurationNeeded(f"默认分支 {default_branch} 不在项目 base_branches 中")
    if await github.tag_exists(repo, tag):
        raise ValueError("远程已存在该 tag")
    if await github.release_by_tag(repo, tag):
        raise ValueError("远程已存在该 Release")
    return {"default_branch": default_branch}


def queue_prepare_release(db, config: ProjectConfig, *, tag: str, default_branch: str) -> tuple[Record, Job]:
    tag = validate_prepare_tag(tag)
    branch = str(default_branch or "").strip()
    if not branch:
        raise ValueError("缺少默认分支")
    if not prepare_release_ready(config):
        raise ValueError("请先在项目接入中启用总结并发布")
    feature = config.prepare_release
    active = db.scalar(
        select(Job).join(Record, Job.record_id == Record.id).where(
            Record.repo == config.repo,
            Job.kind == "prepare_release",
            Job.status.in_(tuple(ACTIVE_PREPARE_STATUSES)),
        )
    )
    if active:
        raise LookupError("已有进行中的总结并发版任务")
    key = f"prepare-release:{config.repo}:{tag}"
    existing = db.scalar(select(Job).where(Job.key == key))
    record = db.scalar(select(Record).where(Record.repo == config.repo, Record.kind == "release", Record.external_id == prepare_record_external_id(tag)))
    if existing:
        if existing.status in ACTIVE_PREPARE_STATUSES:
            raise LookupError("该 tag 已有进行中的总结并发版任务")
        if existing.status == "completed":
            raise LookupError("该 tag 已创建过发版任务")
        if existing.status in RETRYABLE_JOB_STATUSES:
            return db.get(Record, existing.record_id), retry_job(db, existing, actor="管理员")
        raise LookupError("该 tag 已有发版任务")
    if record is None:
        record = Record(
            repo=config.repo,
            kind="release",
            external_id=prepare_record_external_id(tag),
            title=tag,
            status="queued",
            stage="release",
            details={
                "tag": tag,
                "preparing": True,
                "base_ref": branch,
                "docs_date": utcnow().date().isoformat(),
            },
        )
        db.add(record)
        db.flush()
    payload = {
        "tag": tag,
        "default_branch": branch,
        "event": "release",
        "github_action": "prepare",
        "push_branch": branch,
        "rules": [{
            "id": "prepare_release",
            "name": "总结并发布",
            "event": "release",
            "agent_id": feature.agent_id,
            "prompt": feature.prompt,
            "include_repository": True,
            "include_github_credentials": True,
            "skill_ids": list(feature.skill_ids or []),
        }],
        "rule_id": "prepare_release",
        "rule_name": "总结并发布",
        "agent_id": feature.agent_id,
        "prompt": feature.prompt,
        "include_repository": True,
        "include_github_credentials": True,
        "skill_ids": list(feature.skill_ids or []),
    }
    job = schedule_job(db, record, "prepare_release", key, payload)
    add_timeline(db, record, "release", "queued", "已创建总结并发版任务", f"tag {tag}", actor="管理员", job_id=job.id)
    return record, job
