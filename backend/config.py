from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import re
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentRuleConfig(BaseModel):
    id: str = ""
    name: str = ""
    event: str = "pull_request"
    agent_id: str = ""
    prompt: str = ""
    include_repository: bool = False
    include_github_credentials: bool = False
    skill_ids: list[str] = Field(default_factory=list)
    enabled: bool = True


class PrepareReleaseConfig(BaseModel):
    enabled: bool = False
    agent_id: str = ""
    prompt: str = ""
    skill_ids: list[str] = Field(default_factory=list)

    @field_validator("agent_id")
    @classmethod
    def valid_agent_id(cls, value: str) -> str:
        value = (value or "").strip()
        if value and not re.fullmatch(r"[a-f0-9]{32}", value):
            raise ValueError("Agent 标识无效")
        return value

    @field_validator("prompt")
    @classmethod
    def stripped_prompt(cls, value: str) -> str:
        return (value or "").strip()

    @field_validator("skill_ids")
    @classmethod
    def valid_skills(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value or []:
            skill = str(item).strip()
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", skill):
                raise ValueError("技能名称无效")
            if skill not in cleaned:
                cleaned.append(skill)
        if len(cleaned) > 20:
            raise ValueError("最多选择 20 个 skill")
        return cleaned


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    repo: str
    name: str = ""
    description: str = ""
    base_branches: list[str] = Field(default_factory=lambda: ["main"])
    priority_labels: dict[str, str] = Field(default_factory=lambda: {"紧急": "紧急", "普通": "普通", "较低": "较低", "P0": "紧急", "P1": "普通", "P2": "普通", "P3": "较低"})
    managers: list[str] = Field(default_factory=list)
    required_checks: list[str] = Field(default_factory=list)
    review_enabled: bool = False
    merge_enabled: bool = False
    release_enabled: bool = False
    merge_method: str = "squash"
    previous_tag: str = ""
    review_rules: str = "只报告有证据的正确性、回归、数据一致性和安全问题。不要把个人风格偏好当作阻塞问题。"
    agent_id: str = ""
    agent_rules: list[AgentRuleConfig] = Field(default_factory=list)
    prepare_release: PrepareReleaseConfig = Field(default_factory=PrepareReleaseConfig)

    @model_validator(mode="after")
    def merge_policy(self):
        if self.merge_enabled and not self.review_enabled:
            raise ValueError("automatic merge requires agent review")
        if self.merge_enabled and self.required_checks and all(x == "flowdeck/agent-review" for x in self.required_checks):
            raise ValueError("automatic merge cannot use only Flowdeck review status as a required check")
        return self

    @field_validator("repo")
    @classmethod
    def valid_repo(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value) or any(x in (".", "..") for x in value.split("/")):
            raise ValueError("repo must be owner/repository")
        return value

    @field_validator("managers")
    @classmethod
    def valid_managers(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            number = str(item).strip()
            if not re.fullmatch(r"1[3-9]\d{9}", number):
                raise ValueError("钉钉通知人必须是中国大陆11位手机号")
            if number not in cleaned:
                cleaned.append(number)
        return cleaned

    @field_validator("merge_method")
    @classmethod
    def valid_merge(cls, value: str) -> str:
        if value not in {"merge", "squash", "rebase"}:
            raise ValueError("invalid merge method")
        return value


def managers_for_load(values: list | None) -> list[str]:
    cleaned: list[str] = []
    for item in values or []:
        number = str(item).strip()
        if re.fullmatch(r"1[3-9]\d{9}", number) and number not in cleaned:
            cleaned.append(number)
    return cleaned


PROJECT_RUNTIME_OVERRIDES = frozenset({
    "review_enabled", "merge_enabled", "release_enabled", "required_checks",
    "merge_method", "base_branches", "priority_labels", "managers",
    "previous_tag", "review_rules", "agent_id", "agent_rules",
})


class AppConfig(BaseModel):
    projects: list[ProjectConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_repos(self):
        repos = [p.repo.lower() for p in self.projects]
        if len(repos) != len(set(repos)):
            raise ValueError("repository configured more than once")
        return self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    app_mode: str = "demo"
    database_url: str = "sqlite:///./data/flowdeck.db"
    config_path: str = "config/projects.yml"
    data_root: Path = Path("data")
    workspace_host_root: str = ""
    github_token: str = ""
    github_webhook_secret: str = ""
    github_api_url: str = "https://api.github.com"
    github_git_origin: str = "https://github.com"
    executor_webhook_secret: str = ""
    worker_poll_seconds: float = 2
    sync_interval_seconds: int = 300
    initial_history_days: int = 90
    frontend_dist: Path = Path("frontend/dist")
    secrets_encryption_key: str = ""
    webhook_public_base_url: str = ""
    storage_roots: list[str] = Field(default_factory=list)
    session_secure: bool = True
    session_hours: int = Field(default=8, ge=1, le=24)
    require_login: bool = False

    @field_validator("webhook_public_base_url")
    @classmethod
    def public_url(cls, value: str) -> str:
        from urllib.parse import urlsplit
        if value:
            url = urlsplit(value)
            if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError("WEBHOOK_PUBLIC_BASE_URL must be an HTTPS URL without credentials, query or fragment")
        return value.rstrip("/")

    @field_validator("app_mode")
    @classmethod
    def mode(cls, value: str) -> str:
        if value not in {"demo", "live"}:
            raise ValueError("APP_MODE must be demo or live")
        return value

    def project_config(self) -> AppConfig:
        path = Path(self.config_path)
        if not path.exists():
            return AppConfig()
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for item in raw.get("projects") or []:
            if isinstance(item, dict):
                item["managers"] = managers_for_load(item.get("managers"))
        return AppConfig.model_validate(raw)

    def project(self, repo: str) -> ProjectConfig | None:
        return next((p for p in self.project_config().projects if p.repo.lower() == repo.lower()), None)


@lru_cache
def get_settings() -> Settings:
    return Settings()
