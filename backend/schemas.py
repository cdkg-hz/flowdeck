from __future__ import annotations

from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Finding(BaseModel):
    model_config = ConfigDict(extra="ignore")
    file: str
    line: int = Field(ge=1)
    severity: Literal["critical", "high", "medium", "low"]
    title: str = Field(min_length=1, max_length=200)
    explanation: str = Field(min_length=1, max_length=10000)
    suggestion: str = Field(default="", max_length=10000)

    @field_validator("file")
    @classmethod
    def relative_file(cls, value: str) -> str:
        from pathlib import PurePosixPath
        if "\\" in value or PurePosixPath(value).is_absolute() or ".." in PurePosixPath(value).parts or ":" in value:
            raise ValueError("finding path must be relative to the repository")
        return value


class ReviewResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    verdict: Literal["pass", "changes_requested", "inconclusive"]
    summary: str = Field(min_length=1, max_length=20000)
    findings: list[Finding] = Field(max_length=200)

    @model_validator(mode="after")
    def consistent(self):
        if self.verdict == "pass" and self.findings:
            raise ValueError("a passing review cannot contain unresolved findings")
        if self.verdict == "changes_requested" and not self.findings:
            raise ValueError("changes_requested requires evidence")
        return self


class ReleaseChange(BaseModel):
    model_config = ConfigDict(extra="ignore")
    category: Literal["feature", "improvement", "fix", "breaking", "internal"]
    description: str = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)


class NotesResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    public_notes: str = Field(min_length=1, max_length=50000)
    technical_notes: str = Field(min_length=1, max_length=80000)
    changes: list[ReleaseChange] = Field(max_length=500)


class AgentPullResult(BaseModel):
    """Requested PR action; Flowdeck executes the merge itself after validation."""
    model_config = ConfigDict(extra="ignore")
    number: int | None = Field(default=None, ge=1)
    merge_requested: bool


class AgentDocsResult(BaseModel):
    """Doc files written in the sandbox; Flowdeck commits and pushes them itself."""
    model_config = ConfigDict(extra="ignore")
    paths: list[str] = Field(default_factory=list, max_length=40)
    push_requested: bool

    @field_validator("paths")
    @classmethod
    def relative_docs(cls, value: list[str]) -> list[str]:
        from pathlib import PurePosixPath
        cleaned = []
        for item in value:
            path = str(item).strip().replace("\\", "/")
            relative = PurePosixPath(path)
            if not path or relative.is_absolute() or ".." in relative.parts:
                raise ValueError("文档路径必须是工作目录内的相对路径")
            cleaned.append(relative.as_posix()[:500])
        return cleaned


class AgentTaskResult(BaseModel):
    """Required JSON object returned by event-triggered Agent tasks."""
    model_config = ConfigDict(extra="ignore")
    summary: str = Field(min_length=1, max_length=20000)
    outcome: Literal["completed", "failed", "inconclusive"]
    pull_request: AgentPullResult | None = None
    release_docs: AgentDocsResult | None = None


class ExecutorEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    event_id: str = Field(min_length=1, max_length=128)
    producer: str = Field(min_length=1, max_length=100)
    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    execution_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    step: Literal["workspace", "review", "notes", "apk", "upload", "deploy", "verify", "notification"]
    status: Literal["running", "success", "failed", "waiting", "uncertain"]
    sequence: int = Field(ge=0)
    occurred_at: datetime
    message: str = Field(default="", max_length=10000)
    result: dict = Field(default_factory=dict)
