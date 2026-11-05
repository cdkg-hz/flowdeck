from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, quote_plus, urlsplit
import httpx
from backend.config import managers_for_load

_DINGTALK_HTTP_URL = re.compile(
    r"https://oapi\.dingtalk\.com/robot/send\?[^\s\"'<>]*",
    re.IGNORECASE,
)
_REDACTED_DINGTALK_URL = "https://oapi.dingtalk.com/robot/send?access_token=REDACTED"


class DingTalkHttpUrlRedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _redact_dingtalk_http_urls(record.msg)
        else:
            record.msg = _redact_dingtalk_arg(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {key: _redact_dingtalk_arg(value) for key, value in record.args.items()}
            else:
                record.args = tuple(_redact_dingtalk_arg(value) for value in record.args)
        if isinstance(getattr(record, "message", None), str):
            record.message = _redact_dingtalk_http_urls(record.message)
        return True


def _redact_dingtalk_http_urls(text: str) -> str:
    return _DINGTALK_HTTP_URL.sub(_REDACTED_DINGTALK_URL, text)


def _redact_dingtalk_arg(value):
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
    redacted = _redact_dingtalk_http_urls(text)
    return value if redacted == text else redacted


def _install_dingtalk_http_url_filter() -> None:
    filt = DingTalkHttpUrlRedactFilter()
    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        if not any(isinstance(existing, DingTalkHttpUrlRedactFilter) for existing in logger.filters):
            logger.addFilter(filt)


_install_dingtalk_http_url_filter()

DINGTALK_EVENTS = frozenset({
    "review_needs_attention", "review_passed", "checks_failed",
    "merged", "job_failed", "release_ready", "board_column_change",
})
EVENT_LABELS = {
    "review_needs_attention": "审查需要处理",
    "review_passed": "审查通过",
    "checks_failed": "必需检查失败",
    "merged": "PR 已合并",
    "job_failed": "任务需要处理",
    "release_ready": "发版已完成",
    "board_column_change": "看板状态变化",
}


def validate_dingtalk_webhook(url: str) -> str:
    value = (url or "").strip()
    parsed = urlsplit(value)
    token = (parse_qs(parsed.query).get("access_token") or [""])[0]
    if (
        parsed.scheme != "https"
        or parsed.hostname != "oapi.dingtalk.com"
        or parsed.path != "/robot/send"
        or parsed.username or parsed.password or parsed.fragment
        or not token
    ):
        raise ValueError("钉钉 Webhook 必须是 https://oapi.dingtalk.com/robot/send?access_token=…")
    return value


def dingtalk_sign(secret: str, timestamp_ms: int) -> str:
    payload = f"{timestamp_ms}\n{secret}".encode()
    digest = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    return quote_plus(base64.b64encode(digest))


def notify_mobiles(values: list[str] | None) -> list[str]:
    return managers_for_load(values)


def format_dingtalk_markdown(record, message: str, mobiles: list[str]) -> str:
    number = f"#{record.number}" if record.number else "-"
    parts = [f"**{record.repo}** {number} {record.title}", message, record.url or ""]
    if mobiles:
        parts.append(" ".join("@" + item for item in mobiles))
    return "\n\n".join(part for part in parts if part)


def _sent_markers(*buckets) -> list[str]:
    seen: list[str] = []
    for bucket in buckets:
        for item in list((bucket or {}).get("dingtalk_sent") or []):
            marker = str(item)
            if marker and marker not in seen:
                seen.append(marker)
    return seen


async def send_dingtalk_message(*, webhook: str, secret: str, title: str, text: str,
                                at_mobiles: list[str] | None = None, client: httpx.AsyncClient | None = None,
                                now_ms: int | None = None) -> None:
    webhook = validate_dingtalk_webhook(webhook)
    timestamp_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    target = f"{webhook}&timestamp={timestamp_ms}&sign={dingtalk_sign(secret, timestamp_ms)}"
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": text},
        "at": {"atMobiles": list(at_mobiles or []), "isAtAll": False},
    }
    own = client is None
    http = client or httpx.AsyncClient(timeout=10.0)
    try:
        try:
            response = await http.post(target, json=payload)
        except httpx.RequestError:
            raise RuntimeError("钉钉请求失败") from None
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        if response.is_error:
            raise RuntimeError(f"钉钉返回 {response.status_code}")
        if body.get("errcode") != 0:
            code = body.get("errcode")
            raise RuntimeError(f"钉钉返回 {code if code is not None else response.status_code}")
    finally:
        if own:
            await http.aclose()


def coalesce_events(events: list[tuple[str, str]]) -> list[tuple[str, str]]:
    specific = [(key, message) for key, message in events if key != "board_column_change"]
    return specific or list(events)


async def dispatch_dingtalk(settings, sessions, record, events, job=None, *, client=None, now_ms=None):
    from sqlalchemy import select
    from backend.board import board_column as column_of
    from backend.db import DingTalkChannel, Job, Project, Record, utcnow
    from backend.integrations import load_dingtalk_secrets
    from backend.projects import project_config, project_redactions
    from backend.service import add_timeline, redact

    if getattr(record, "demo", False):
        return
    events = coalesce_events([(key, message) for key, message in events if key in DINGTALK_EVENTS])
    if not events:
        return
    record_id = record.id
    job_id = job.id if job is not None else None
    with sessions() as db:
        config = project_config(settings, db, record.repo, active_only=False)
        project = db.scalar(select(Project).where(Project.repo == record.repo))
        if not config or not project or project.demo:
            return
        current_record = db.get(Record, record_id)
        if current_record is None:
            return
        current_job = db.get(Job, job_id) if job_id else None
        project_id = project.id
        project_name = project.name or project.repo
        snapshot = SimpleNamespace(
            repo=current_record.repo, number=current_record.number, title=current_record.title,
            url=current_record.url, status=current_record.status, kind=current_record.kind,
            stage=current_record.stage, details=dict(current_record.details or {}),
        )
        channels = list(db.scalars(select(DingTalkChannel).where(DingTalkChannel.enabled.is_(True))))
        targets = []
        for channel in channels:
            try:
                webhook, secret = load_dingtalk_secrets(settings, channel)
            except Exception as exc:
                targets.append((channel.id, channel.name, list(channel.events or []), list(channel.project_ids or []), None, None, str(exc)))
                continue
            targets.append((channel.id, channel.name, list(channel.events or []), list(channel.project_ids or []), webhook, secret, None))
        sent = _sent_markers(snapshot.details, current_job.result if current_job is not None else None)
        current_column = column_of(
            status=snapshot.status, kind=snapshot.kind, stage=snapshot.stage,
            deleted=bool((snapshot.details or {}).get("deleted")),
        )
        mobiles = notify_mobiles(config.managers)
        board_marker = (snapshot.details or {}).get("dingtalk_board_column")
    secrets = project_redactions(settings, sessions)
    successes, failures = [], []
    new_board_column = None
    for event, message in events:
        if event == "board_column_change" and board_marker == current_column:
            continue
        for channel_id, name, channel_events, project_ids, webhook, secret, load_error in targets:
            if project_id not in project_ids or event not in channel_events:
                continue
            if event != "board_column_change":
                marker = f"{channel_id}:{event}"
                if marker in sent:
                    continue
            if load_error or not webhook or not secret:
                failures.append((name, redact(load_error or "钉钉渠道缺少 Webhook 或加签密钥", secrets)))
                continue
            try:
                title = f"{project_name} · {EVENT_LABELS[event]}"
                text = format_dingtalk_markdown(snapshot, message, mobiles)
                await send_dingtalk_message(
                    webhook=webhook, secret=secret, title=title, text=text,
                    at_mobiles=mobiles, client=client, now_ms=now_ms,
                )
                if event != "board_column_change":
                    sent.append(f"{channel_id}:{event}")
                successes.append(name)
                if event == "board_column_change":
                    new_board_column = current_column
            except Exception as exc:
                failures.append((name, redact(str(exc), secrets)))
    with sessions() as db:
        current_record = db.get(Record, record_id)
        if current_record is None:
            return
        current_job = db.get(Job, job_id) if job_id else None
        details = dict(current_record.details or {})
        merged_sent = list(dict.fromkeys(
            _sent_markers(details, current_job.result if current_job is not None else None) + sent
        ))
        details["dingtalk_sent"] = merged_sent
        if new_board_column is not None:
            details["dingtalk_board_column"] = new_board_column
        current_record.details = details
        if current_job is not None:
            current_job.result = {**dict(current_job.result or {}), "dingtalk_sent": merged_sent}
            current_job.updated_at = utcnow()
        if successes:
            add_timeline(db, current_record, "notification", "success", "已通过钉钉通知", "、".join(dict.fromkeys(successes)), job_id=current_job.id if current_job else None)
        for name, error in failures:
            add_timeline(db, current_record, "notification", "failed", "钉钉通知未确认发送", f"{name}：{error}", job_id=current_job.id if current_job else None)
        db.commit()
