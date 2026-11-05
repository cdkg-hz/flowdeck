from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import uuid
from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, event, inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.dialects.postgresql import JSONB

JSON_DATA = JSON().with_variant(JSONB, "postgresql")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def identifier() -> str:
    return uuid.uuid4().hex


def _add_column(connection, ddl: str) -> None:
    try:
        connection.execute(text(ddl))
    except OperationalError as exc:
        if "duplicate column" not in str(exc).lower():
            raise


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    repo: Mapped[str] = mapped_column(String(240), unique=True)
    name: Mapped[str] = mapped_column(String(240), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    demo: Mapped[bool] = mapped_column(Boolean, default=False)


class Record(Base):
    __tablename__ = "records"
    __table_args__ = (UniqueConstraint("repo", "kind", "external_id"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    repo: Mapped[str] = mapped_column(String(240), index=True)
    kind: Mapped[str] = mapped_column(String(24))
    external_id: Mapped[str] = mapped_column(String(80))
    number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="waiting")
    stage: Mapped[str] = mapped_column(String(40), default="issue")
    priority: Mapped[str | None] = mapped_column(String(8), nullable=True)
    assignees: Mapped[list] = mapped_column(JSON_DATA, default=list)
    labels: Mapped[list] = mapped_column(JSON_DATA, default=list)
    details: Mapped[dict] = mapped_column(JSON_DATA, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    demo: Mapped[bool] = mapped_column(Boolean, default=False)


class ProjectConnection(Base):
    __tablename__ = "project_connections"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    webhook_id: Mapped[str] = mapped_column(String(32), unique=True, default=identifier)
    secret_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    events: Mapped[list] = mapped_column(JSON_DATA, default=list)
    storage_path: Mapped[str] = mapped_column(Text, default="")
    storage_key: Mapped[str | None] = mapped_column(String(1000), unique=True, nullable=True)
    storage_status: Mapped[str] = mapped_column(String(20), default="pending")
    storage_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    storage_locked: Mapped[bool] = mapped_column(Boolean, default=False)
    api_status: Mapped[str] = mapped_column(String(20), default="missing")
    api_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    webhook_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    github_hook_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    repository_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    webhook_repository_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    verified_revision: Mapped[int] = mapped_column(Integer, default=0)
    verify_requested: Mapped[bool] = mapped_column(Boolean, default=True)
    next_verify_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_delivery_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentConnection(Base):
    __tablename__ = "agent_connections"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    base_url: Mapped[str] = mapped_column(Text)
    protocol: Mapped[str] = mapped_column(String(80), default="openai_chat_completions")
    api_key_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=180)
    model: Mapped[str] = mapped_column(String(80), default="")
    reasoning_effort: Mapped[str] = mapped_column(String(16), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DingTalkChannel(Base):
    __tablename__ = "dingtalk_channels"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    webhook_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    sign_secret_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    events: Mapped[list] = mapped_column(JSON_DATA, default=list)
    project_ids: Mapped[list] = mapped_column(JSON_DATA, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UploadedSkill(Base):
    __tablename__ = "uploaded_skills"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    label: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RegistryConnection(Base):
    __tablename__ = "registry_connections"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    registry: Mapped[str] = mapped_column(String(500))
    username: Mapped[str] = mapped_column(String(500), default="")
    password_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProjectAutomation(Base):
    __tablename__ = "project_automations"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    config: Mapped[dict] = mapped_column(JSON_DATA, default=dict)
    publisher_username_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    publisher_password_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    publisher_browser_api_key_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AdminAccount(Base):
    __tablename__ = "admin_accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    username: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(Text)
    # Writing this row serializes admin mutations, including overlapping storage paths.
    revision: Mapped[int] = mapped_column(Integer, default=0)


class AdminSession(Base):
    __tablename__ = "admin_sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    csrf_token: Mapped[str] = mapped_column(String(100))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    address_hash: Mapped[str] = mapped_column(String(64), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Timeline(Base):
    __tablename__ = "timeline"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    record_id: Mapped[str] = mapped_column(ForeignKey("records.id"), index=True)
    job_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    stage: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(300))
    message: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(100), default="system")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    data: Mapped[dict] = mapped_column(JSON_DATA, default=dict)


class InboxEvent(Base):
    __tablename__ = "inbox_events"
    __table_args__ = (UniqueConstraint("source", "delivery_id"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    source: Mapped[str] = mapped_column(String(20))
    delivery_id: Mapped[str] = mapped_column(String(128))
    event_type: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict] = mapped_column(JSON_DATA)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    key: Mapped[str] = mapped_column(String(500), unique=True)
    record_id: Mapped[str] = mapped_column(ForeignKey("records.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    step: Mapped[str] = mapped_column(String(40), default="queued")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[dict] = mapped_column(JSON_DATA, default=dict)
    result: Mapped[dict] = mapped_column(JSON_DATA, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(100), nullable=True)


def initialize_schema(engine):
    Base.metadata.create_all(engine)
    # Upgrade the early local preview schema. Production starts with the full v1 schema.
    inbox_columns = {c["name"] for c in inspect(engine).get_columns("inbox_events")}
    automation_columns = {c["name"] for c in inspect(engine).get_columns("project_automations")}
    connection_columns = {c["name"] for c in inspect(engine).get_columns("project_connections")}
    agent_columns = {c["name"] for c in inspect(engine).get_columns("agent_connections")} if inspect(engine).has_table("agent_connections") else set()
    with engine.begin() as connection:
        if "attempt" not in inbox_columns:
            _add_column(connection, "ALTER TABLE inbox_events ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0")
        if "available_at" not in inbox_columns:
            _add_column(connection, "ALTER TABLE inbox_events ADD COLUMN available_at TIMESTAMP")
            connection.execute(text("UPDATE inbox_events SET available_at = received_at WHERE available_at IS NULL"))
        if "publisher_browser_api_key_ciphertext" not in automation_columns:
            _add_column(connection, "ALTER TABLE project_automations ADD COLUMN publisher_browser_api_key_ciphertext TEXT")
        if "github_hook_id" not in connection_columns:
            _add_column(connection, "ALTER TABLE project_connections ADD COLUMN github_hook_id VARCHAR(40)")
        if "model" not in agent_columns:
            _add_column(connection, "ALTER TABLE agent_connections ADD COLUMN model VARCHAR(80) NOT NULL DEFAULT ''")
        if "reasoning_effort" not in agent_columns:
            _add_column(connection, "ALTER TABLE agent_connections ADD COLUMN reasoning_effort VARCHAR(16) NOT NULL DEFAULT ''")


def database(url: str):
    if url.startswith("sqlite"):
        db_path = url.removeprefix("sqlite:///")
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, connect_args={"check_same_thread": False} if url.startswith("sqlite") else {}, pool_pre_ping=True)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def configure_sqlite(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=10000")
            connection.execute("PRAGMA journal_mode=WAL")
    return engine, sessionmaker(engine, expire_on_commit=False)
