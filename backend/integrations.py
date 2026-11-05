from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import urlsplit

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import select

from backend.config import PROJECT_RUNTIME_OVERRIDES, PrepareReleaseConfig, ProjectConfig
from backend.agent_tasks import AgentRuleInput, normalize_agent_rules
from backend.release_prepare import adopt_prepare_release
from backend.credentials import seal, unseal
from backend.db import AgentConnection, DingTalkChannel, Project, ProjectAutomation, utcnow
from backend.dingtalk import DINGTALK_EVENTS, validate_dingtalk_webhook
from backend.errors import ConfigurationNeeded
from backend.skills import SkillPackageError, collected_skill_slugs, require_uploaded_slugs


def _http_url(value: str, label: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"{label}必须是没有嵌入凭据的 HTTP(S) 地址")
    return value.rstrip("/")


class AgentConnectionInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    base_url: str = Field(min_length=8, max_length=1000)
    api_key: str = Field(default="", max_length=4000)
    timeout_seconds: int = Field(default=1800, ge=15, le=3600)
    model: str = Field(min_length=1, max_length=80)
    reasoning_effort: str = Field(default="", max_length=16)
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def trimmed(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("不能为空")
        return value

    @field_validator("model")
    @classmethod
    def valid_model(cls, value: str) -> str:
        value = value.strip()
        if not value or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}", value):
            raise ValueError("模型名无效")
        return value

    @field_validator("reasoning_effort")
    @classmethod
    def valid_effort(cls, value: str) -> str:
        value = value.strip()
        if value and value not in {"low", "medium", "high", "xhigh", "max"}:
            raise ValueError("推理力度必须是 low、medium、high、xhigh 或 max")
        return value

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        return _http_url(value.strip(), "模型接口地址")


class DingTalkChannelInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    webhook: str = Field(default="", max_length=2000)
    sign_secret: str = Field(default="", max_length=400)
    enabled: bool = True
    events: list[str] = Field(default_factory=list)
    project_ids: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def trimmed_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("不能为空")
        return value

    @field_validator("events")
    @classmethod
    def valid_events(cls, value: list[str]) -> list[str]:
        cleaned, seen = [], set()
        for item in value:
            key = str(item).strip()
            if key in seen:
                continue
            if key not in DINGTALK_EVENTS:
                raise ValueError("未知的钉钉通知事件")
            seen.add(key)
            cleaned.append(key)
        return cleaned

    @field_validator("project_ids")
    @classmethod
    def valid_project_ids(cls, value: list[str]) -> list[str]:
        cleaned, seen = [], set()
        for item in value:
            key = str(item).strip()
            if not re.fullmatch(r"[a-f0-9]{32}", key):
                raise ValueError("项目标识无效")
            if key not in seen:
                seen.add(key)
                cleaned.append(key)
        return cleaned


class ProjectAutomationInput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    agent_id: str = Field(default="", max_length=32)
    agent_rules: list[AgentRuleInput] | None = None
    prepare_release: PrepareReleaseConfig | None = None
    review_enabled: bool | None = None
    merge_enabled: bool | None = None
    release_enabled: bool | None = None
    required_checks: list[str] | None = None
    merge_method: str | None = None
    base_branches: list[str] | None = None
    priority_labels: dict[str, str] | None = None
    managers: list[str] | None = None
    previous_tag: str | None = None
    review_rules: str | None = None

    @field_validator("agent_id")
    @classmethod
    def valid_agent_id(cls, value: str) -> str:
        if value and not re.fullmatch(r"[a-f0-9]{32}", value):
            raise ValueError("Agent 标识无效")
        return value

    @field_validator("required_checks", "base_branches", "managers")
    @classmethod
    def stripped_names(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        cleaned = []
        for item in value:
            name = str(item).strip()
            if name and name not in cleaned:
                cleaned.append(name)
        return cleaned

    @field_validator("previous_tag", "review_rules", "merge_method")
    @classmethod
    def stripped_text(cls, value: str | None) -> str | None:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def consistent(self):
        if self.agent_rules:
            for rule in self.agent_rules:
                if rule.include_github_credentials and not rule.include_repository:
                    rule.include_repository = True
        return self


@dataclass(frozen=True)
class AgentRuntime:
    id: str
    name: str
    base_url: str
    api_key: str
    timeout_seconds: int
    model: str = ""
    reasoning_effort: str = ""


def agent_public(value: AgentConnection) -> dict:
    return {
        "id": value.id,
        "name": value.name,
        "base_url": value.base_url,
        "api_key_configured": bool(value.api_key_ciphertext),
        "model": value.model or "",
        "reasoning_effort": value.reasoning_effort or "",
        "timeout_seconds": value.timeout_seconds,
        "enabled": value.enabled,
        "updated_at": value.updated_at,
    }


def save_agent(settings, db, body: AgentConnectionInput, item: AgentConnection | None = None) -> AgentConnection:
    duplicate = db.scalar(select(AgentConnection).where(AgentConnection.name == body.name, AgentConnection.id != (item.id if item else "")))
    if duplicate:
        raise HTTPException(409, "已存在同名 Agent")
    item = item or AgentConnection()
    item.name, item.base_url, item.protocol = body.name, body.base_url, "copilot"
    item.model, item.reasoning_effort = body.model, body.reasoning_effort
    item.timeout_seconds, item.enabled, item.updated_at = body.timeout_seconds, body.enabled, utcnow()
    db.add(item)
    db.flush()
    if body.api_key:
        item.api_key_ciphertext = seal(settings, "agent:" + item.id, "api_key", body.api_key)
    return item


def dingtalk_public(value: DingTalkChannel) -> dict:
    return {
        "id": value.id,
        "name": value.name,
        "enabled": value.enabled,
        "events": list(value.events or []),
        "project_ids": list(value.project_ids or []),
        "webhook_configured": bool(value.webhook_ciphertext),
        "sign_configured": bool(value.sign_secret_ciphertext),
        "updated_at": value.updated_at,
    }


def save_dingtalk_channel(settings, db, body: DingTalkChannelInput, item: DingTalkChannel | None = None) -> DingTalkChannel:
    duplicate = db.scalar(select(DingTalkChannel).where(DingTalkChannel.name == body.name, DingTalkChannel.id != (item.id if item else "")))
    if duplicate:
        raise HTTPException(409, "已存在同名钉钉渠道")
    unknown = [project_id for project_id in body.project_ids if not db.get(Project, project_id)]
    if unknown:
        raise HTTPException(422, "选择的项目不存在")
    item = item or DingTalkChannel()
    item.name, item.enabled = body.name, body.enabled
    item.events, item.project_ids = list(body.events), list(body.project_ids)
    item.updated_at = utcnow()
    db.add(item)
    db.flush()
    if body.webhook.strip():
        try:
            item.webhook_ciphertext = seal(settings, "dingtalk:" + item.id, "webhook", validate_dingtalk_webhook(body.webhook))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    if body.sign_secret.strip():
        item.sign_secret_ciphertext = seal(settings, "dingtalk:" + item.id, "sign_secret", body.sign_secret.strip())
    if not item.webhook_ciphertext or not item.sign_secret_ciphertext:
        raise HTTPException(422, "钉钉渠道必须填写 Webhook 和加签密钥")
    return item


def load_dingtalk_secrets(settings, item: DingTalkChannel) -> tuple[str, str]:
    webhook = unseal(settings, "dingtalk:" + item.id, "webhook", item.webhook_ciphertext)
    secret = unseal(settings, "dingtalk:" + item.id, "sign_secret", item.sign_secret_ciphertext)
    if not webhook or not secret:
        raise ConfigurationNeeded("钉钉渠道缺少 Webhook 或加签密钥")
    return webhook, secret


def prune_dingtalk_project(db, project_id: str) -> None:
    for channel in db.scalars(select(DingTalkChannel)):
        ids = [item for item in (channel.project_ids or []) if item != project_id]
        if ids != list(channel.project_ids or []):
            channel.project_ids = ids


def project_automation_config(db, project_id: str) -> dict:
    automation = db.get(ProjectAutomation, project_id)
    return dict(automation.config or {}) if automation else {}


def save_project_automation(settings, db, project_id: str, body: ProjectAutomationInput) -> ProjectAutomation:
    if body.agent_id and not db.get(AgentConnection, body.agent_id):
        raise HTTPException(422, "选择的模型接口不存在")
    for rule in body.agent_rules or []:
        agent = db.get(AgentConnection, rule.agent_id)
        if not agent or not agent.enabled:
            raise HTTPException(422, f"规则「{rule.name}」选择的模型接口不存在或未启用")
    if body.prepare_release and body.prepare_release.enabled:
        if not body.prepare_release.prompt:
            raise HTTPException(422, "启用总结并发布时必须填写任务说明")
        if not body.prepare_release.agent_id:
            raise HTTPException(422, "启用总结并发布时必须选择模型接口")
        agent = db.get(AgentConnection, body.prepare_release.agent_id)
        if not agent or not agent.enabled:
            raise HTTPException(422, "总结并发布选择的模型接口不存在或未启用")
    item = db.get(ProjectAutomation, project_id) or ProjectAutomation(project_id=project_id)
    config = dict(item.config or {})
    dumped = body.model_dump(exclude_unset=True)
    if "agent_id" in dumped:
        config["agent_id"] = body.agent_id
    if body.agent_rules is not None:
        try:
            config["agent_rules"] = normalize_agent_rules(body.agent_rules)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    if body.prepare_release is not None:
        config["prepare_release"] = body.prepare_release.model_dump()
    for key in PROJECT_RUNTIME_OVERRIDES:
        if key in {"agent_id", "agent_rules"}:
            continue
        if key in dumped:
            config[key] = dumped[key]
    config = adopt_prepare_release(config)
    project = db.get(Project, project_id)
    repo = project.repo if project else ""
    base = settings.project(repo) if repo else None
    try:
        merged = {**(base.model_dump() if base else {}), **config, "repo": repo or "owner/repo"}
        if project:
            merged.update({"name": project.name, "description": project.description})
        policy = ProjectConfig.model_validate(merged)
    except ValidationError as exc:
        message = "; ".join(error.get("msg", "") for error in exc.errors())
        if "automatic merge requires agent review" in message:
            message = "自动合并需要先启用内置审查"
        elif "automatic merge" in message:
            message = "自动合并不能只用 Flowdeck 自己的审查状态；仓库没有 CI 时请把检查名留空"
        elif "手机号" in message:
            message = "钉钉通知人必须是中国大陆11位手机号"
        raise HTTPException(422, message or "项目策略无效") from exc
    if policy.review_enabled and not policy.agent_id:
        raise HTTPException(422, "启用内置审查时必须选择模型接口")
    if policy.release_enabled and not policy.agent_id:
        raise HTTPException(422, "启用 Release 分析时必须选择模型接口")
    try:
        require_uploaded_slugs(db, list(collected_skill_slugs(config)))
    except SkillPackageError as exc:
        raise HTTPException(422, str(exc)) from exc
    item.config = config
    item.revision = (item.revision or 0) + 1
    item.updated_at = utcnow()
    db.add(item)
    return item


def _runtime_effort(value: str | None) -> str:
    effort = value or ""
    return "low" if effort == "minimal" else effort


def load_agent(settings, db, agent_id: str) -> AgentRuntime:
    item = db.get(AgentConnection, agent_id) if agent_id else None
    if not item or not item.enabled:
        raise ConfigurationNeeded("尚未为项目选择可用的模型接口")
    key = unseal(settings, "agent:" + item.id, "api_key", item.api_key_ciphertext)
    if not key:
        raise ConfigurationNeeded("选中的模型接口尚未配置 API Key")
    if not item.model:
        raise ConfigurationNeeded("选中的模型接口尚未配置模型名")
    return AgentRuntime(item.id, item.name, item.base_url, key, item.timeout_seconds, item.model, _runtime_effort(item.reasoning_effort))


def load_enabled_agents(settings, db) -> list[AgentRuntime]:
    values = []
    for item in db.scalars(select(AgentConnection).where(AgentConnection.enabled.is_(True)).order_by(AgentConnection.id)):
        key = unseal(settings, "agent:" + item.id, "api_key", item.api_key_ciphertext)
        if not key or not item.model:
            continue
        values.append(AgentRuntime(item.id, item.name, item.base_url, key, item.timeout_seconds, item.model, _runtime_effort(item.reasoning_effort)))
    return values
