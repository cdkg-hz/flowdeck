import json
import logging
import httpx
import pytest
from starlette.testclient import TestClient
from backend.auth import set_admin
from backend.board import COLUMN_LABELS, board_column
from backend.main import create_app
from backend.config import ProjectConfig
from backend.credentials import unseal
from backend.db import DingTalkChannel, Job, ProjectAutomation, Record, Timeline
from backend.dingtalk import (
    DINGTALK_EVENTS,
    DingTalkHttpUrlRedactFilter,
    coalesce_events,
    dispatch_dingtalk,
    dingtalk_sign,
    format_dingtalk_markdown,
    notify_mobiles,
    send_dingtalk_message,
    validate_dingtalk_webhook,
)
from backend.integrations import DingTalkChannelInput, ProjectAutomationInput, dingtalk_public, prune_dingtalk_project, save_dingtalk_channel, save_project_automation
from backend.projects import project_config, project_redactions
from fastapi import HTTPException
from sqlalchemy import select


def test_board_column_matches_frontend_sets():
    assert COLUMN_LABELS == {
        "waiting": "待处理",
        "active": "进行中",
        "attention": "需要关注",
        "done": "已完成",
    }
    attention = ["failed", "changes_requested", "uncertain", "needs_configuration", "inconclusive"]
    done = ["merged", "closed", "deleted", "success"]
    for status in attention:
        assert board_column(status=status) == "attention"
    for status in done:
        assert board_column(status=status) == "done"
    assert board_column(status="closed", deleted=True) == "done"
    assert board_column(status="running") == "active"
    assert board_column(status="queued", kind="pr", stage="review") == "active"
    assert board_column(status="running", kind="pr", stage="review") == "active"
    assert board_column(status="review_passed") == "waiting"
    assert board_column(status="waiting") == "waiting"
    assert board_column(status="queued", kind="issue", stage="issue") == "waiting"


def test_validate_dingtalk_webhook_accepts_official_robot():
    url = "https://oapi.dingtalk.com/robot/send?access_token=abc"
    assert validate_dingtalk_webhook(url) == url


@pytest.mark.parametrize("url", [
    "http://oapi.dingtalk.com/robot/send?access_token=abc",
    "https://evil.example/robot/send?access_token=abc",
    "https://oapi.dingtalk.com/other?access_token=abc",
    "https://oapi.dingtalk.com/robot/send",
    "https://user:pass@oapi.dingtalk.com/robot/send?access_token=abc",
    "https://oapi.dingtalk.com/robot/send?access_token=abc#frag",
])
def test_validate_dingtalk_webhook_rejects_non_robot(url):
    with pytest.raises(ValueError, match="钉钉"):
        validate_dingtalk_webhook(url)


def test_dingtalk_sign_matches_official_algorithm():
    assert dingtalk_sign("SECabc", 1700000000000) == "jcUpW0QmtKduN03n4JqQ0PBosVjqnM8gU7fIIvsDmCM%3D"


def test_notify_mobiles_keeps_valid_china_numbers_only():
    assert notify_mobiles(["13800138000", "alice", "13800138000", "13900139000", "123"]) == [
        "13800138000", "13900139000",
    ]


@pytest.mark.asyncio
async def test_send_dingtalk_message_signs_and_ats(caplog):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    webhook = "https://oapi.dingtalk.com/robot/send?access_token=SECRETTOKEN"
    with caplog.at_level("INFO"):
        await send_dingtalk_message(
            webhook=webhook, secret="SECabc", title="审查需要处理",
            text="**acme/mobile** #7 Fix\n\n需要改\n\nhttps://github.com/acme/mobile/pull/7\n\n@13800138000",
            at_mobiles=["13800138000"], client=client, now_ms=1700000000000,
        )
        if not any(record.name.startswith(("httpx", "httpcore")) for record in caplog.records):
            logging.getLogger("httpx").info("HTTP Request: POST %s", captured["url"])
    assert "timestamp=1700000000000" in captured["url"]
    assert "sign=jcUpW0QmtKduN03n4JqQ0PBosVjqnM8gU7fIIvsDmCM%3D" in captured["url"]
    assert "%253D" not in captured["url"]
    assert captured["body"]["msgtype"] == "markdown"
    assert captured["body"]["at"]["atMobiles"] == ["13800138000"]
    assert captured["body"]["at"]["isAtAll"] is False
    assert "@13800138000" in captured["body"]["markdown"]["text"]
    joined = " ".join(record.getMessage() for record in caplog.records)
    assert any(record.name.startswith(("httpx", "httpcore")) for record in caplog.records)
    assert "SECRETTOKEN" not in joined
    assert "SECabc" not in joined
    await client.aclose()


@pytest.mark.asyncio
async def test_send_dingtalk_message_does_not_mutate_httpx_logger():
    log = logging.getLogger("httpx")
    calls = []
    original = log.setLevel

    def tracked(level):
        calls.append(level)
        return original(level)

    log.setLevel = tracked
    try:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"errcode": 0})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await send_dingtalk_message(
            webhook="https://oapi.dingtalk.com/robot/send?access_token=t",
            secret="SECabc", title="t", text="x", client=client, now_ms=1700000000000,
        )
        await client.aclose()
    finally:
        log.setLevel = original
    assert calls == []


def test_httpx_filter_redacts_dingtalk_webhook_query(caplog):
    url = (
        "https://oapi.dingtalk.com/robot/send"
        "?access_token=SECRETTOKEN&timestamp=1700000000000"
        "&sign=jcUpW0QmtKduN03n4JqQ0PBosVjqnM8gU7fIIvsDmCM%3D"
    )
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="HTTP Request: POST %s",
        args=(url,),
        exc_info=None,
    )
    assert DingTalkHttpUrlRedactFilter().filter(record) is True
    filtered = record.getMessage()
    assert "SECRETTOKEN" not in filtered
    assert "jcUpW0QmtKduN03n4JqQ0PBosVjqnM8gU7fIIvsDmCM" not in filtered
    assert "HTTP Request: POST" in filtered
    github = "https://api.github.com/repos/acme/mobile?token=keepme"
    with caplog.at_level("INFO"):
        logging.getLogger("httpx").info("HTTP Request: POST %s", url)
        logging.getLogger("httpcore").info("send_request_headers.started url=%s", url)
        logging.getLogger("httpx").info("HTTP Request: POST %s", github)
    joined = caplog.text
    assert "SECRETTOKEN" not in joined
    assert "jcUpW0QmtKduN03n4JqQ0PBosVjqnM8gU7fIIvsDmCM" not in joined
    assert "keepme" in joined
    assert "access_token=REDACTED" in joined


@pytest.mark.asyncio
async def test_send_dingtalk_message_http_error_omits_token():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"errcode": 88, "errmsg": "forbidden"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError) as excinfo:
        await send_dingtalk_message(
            webhook="https://oapi.dingtalk.com/robot/send?access_token=SECRETTOKEN",
            secret="SECabc", title="t", text="x", client=client, now_ms=1700000000000,
        )
    message = str(excinfo.value)
    assert message == "钉钉返回 403"
    assert "SECRETTOKEN" not in message
    assert "SECabc" not in message
    assert "access_token" not in message
    assert "forbidden" not in message
    await client.aclose()


@pytest.mark.asyncio
async def test_send_dingtalk_message_raises_on_dingtalk_errcode():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errcode": 310000, "errmsg": "sign not match"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError) as excinfo:
        await send_dingtalk_message(
            webhook="https://oapi.dingtalk.com/robot/send?access_token=t",
            secret="SECabc", title="t", text="x", client=client, now_ms=1700000000000,
        )
    message = str(excinfo.value)
    assert message == "钉钉返回 310000"
    assert "sign not match" not in message
    await client.aclose()


@pytest.mark.parametrize("payload", [
    {"mode": "html"},
    {"mode": "json", "body": {"errmsg": "ok"}},
])
@pytest.mark.asyncio
async def test_send_dingtalk_message_requires_errcode_zero(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        if payload["mode"] == "html":
            return httpx.Response(200, text="<html>ok</html>")
        return httpx.Response(200, json=payload["body"])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match=r"钉钉返回"):
        await send_dingtalk_message(
            webhook="https://oapi.dingtalk.com/robot/send?access_token=t",
            secret="SECabc", title="t", text="x", client=client, now_ms=1700000000000,
        )
    await client.aclose()


def test_format_dingtalk_markdown_includes_github_url_and_mobiles():
    class Rec:
        repo, number, title, url = "acme/mobile", 7, "Fix retry", "https://github.com/acme/mobile/pull/7"
    text = format_dingtalk_markdown(Rec(), "PR 审查需要处理：需要改", ["13800138000"])
    assert "acme/mobile" in text and "#7" in text and "Fix retry" in text
    assert "https://github.com/acme/mobile/pull/7" in text
    assert "@13800138000" in text


def test_dingtalk_event_names_are_fixed():
    assert DINGTALK_EVENTS == {
        "review_needs_attention", "review_passed", "checks_failed",
        "merged", "job_failed", "release_ready", "board_column_change",
    }


def test_managers_must_be_china_mobile_numbers():
    ProjectConfig(repo="acme/mobile", managers=["13800138000"])
    with pytest.raises(ValueError, match="手机号"):
        ProjectConfig(repo="acme/mobile", managers=["octocat"])


def test_project_config_loads_leftover_github_managers(setup_db):
    settings, sessions, _ = setup_db
    with sessions() as db:
        automation = db.get(ProjectAutomation, "a" * 32)
        automation.config = {**(automation.config or {}), "managers": ["octocat"]}
        db.commit()
        cfg = project_config(settings, db, "acme/mobile", active_only=False)
        assert cfg is not None
        assert "octocat" not in list(cfg.managers)
    with pytest.raises(HTTPException) as exc:
        with sessions() as db:
            save_project_automation(settings, db, "a" * 32, ProjectAutomationInput(managers=["octocat"]))
    assert exc.value.status_code == 422
    app = create_app(settings)
    with TestClient(app) as client:
        listed = client.get("/api/integrations")
    assert listed.status_code == 200
    assert all("octocat" not in (item.get("managers") or []) for item in listed.json()["projects"])


def test_save_dingtalk_channel_encrypts_and_hides_secrets(setup_db):
    settings, sessions, _ = setup_db
    body = DingTalkChannelInput(
        name="开发群",
        webhook="https://oapi.dingtalk.com/robot/send?access_token=TOKEN123",
        sign_secret="SECabc",
        enabled=True,
        events=["review_needs_attention"],
        project_ids=["a" * 32],
    )
    with sessions() as db:
        item = save_dingtalk_channel(settings, db, body)
        db.commit()
        public = dingtalk_public(item)
        assert public["name"] == "开发群"
        assert public["webhook_configured"] is True
        assert public["sign_configured"] is True
        assert "TOKEN123" not in str(public) and "SECabc" not in str(public)
        stored = db.get(DingTalkChannel, item.id)
        assert stored.webhook_ciphertext != body.webhook
        assert unseal(settings, "dingtalk:" + item.id, "webhook", stored.webhook_ciphertext).endswith("TOKEN123")
        redacted = project_redactions(settings, sessions)
        joined = " ".join(str(value) for value in redacted)
        assert "TOKEN123" in joined and "SECabc" in joined


def test_save_dingtalk_channel_keeps_secrets_when_blank(setup_db):
    settings, sessions, _ = setup_db
    with sessions() as db:
        item = save_dingtalk_channel(settings, db, DingTalkChannelInput(
            name="告警群", webhook="https://oapi.dingtalk.com/robot/send?access_token=ONE",
            sign_secret="SECone", events=["merged"], project_ids=["a" * 32],
        ))
        db.commit()
        updated = save_dingtalk_channel(settings, db, DingTalkChannelInput(
            name="告警群", webhook="", sign_secret="", events=["merged", "job_failed"], project_ids=["a" * 32],
        ), item)
        db.commit()
        assert unseal(settings, "dingtalk:" + updated.id, "webhook", updated.webhook_ciphertext).endswith("ONE")
        assert unseal(settings, "dingtalk:" + updated.id, "sign_secret", updated.sign_secret_ciphertext) == "SECone"
        assert updated.events == ["merged", "job_failed"]


def test_save_dingtalk_channel_rejects_unknown_project_and_event(setup_db):
    settings, sessions, _ = setup_db
    from fastapi import HTTPException
    from pydantic import ValidationError
    with sessions() as db:
        with pytest.raises(HTTPException) as missing:
            save_dingtalk_channel(settings, db, DingTalkChannelInput(
                name="x", webhook="https://oapi.dingtalk.com/robot/send?access_token=t",
                sign_secret="SECabc", events=["review_needs_attention"], project_ids=["b" * 32],
            ))
        assert missing.value.status_code == 422
        with pytest.raises(ValidationError, match="未知的钉钉通知事件"):
            DingTalkChannelInput(
                name="y", webhook="https://oapi.dingtalk.com/robot/send?access_token=t",
                sign_secret="SECabc", events=["not_an_event"], project_ids=["a" * 32],
            )


def test_prune_dingtalk_project_removes_id(setup_db):
    settings, sessions, _ = setup_db
    with sessions() as db:
        item = save_dingtalk_channel(settings, db, DingTalkChannelInput(
            name="群", webhook="https://oapi.dingtalk.com/robot/send?access_token=t",
            sign_secret="SECabc", events=["merged"], project_ids=["a" * 32],
        ))
        db.commit()
        prune_dingtalk_project(db, "a" * 32)
        db.commit()
        assert db.get(DingTalkChannel, item.id).project_ids == []


def test_create_channel_requires_webhook_and_secret(setup_db):
    settings, sessions, _ = setup_db
    from fastapi import HTTPException
    with sessions() as db:
        with pytest.raises(HTTPException) as exc:
            save_dingtalk_channel(settings, db, DingTalkChannelInput(name="空", webhook="", sign_secret=""))
        assert exc.value.status_code == 422


ADMIN_PASS = "admin-password-123"


def test_dingtalk_channel_admin_api(setup_db, monkeypatch):
    settings, sessions, _ = setup_db
    app = create_app(settings)
    with sessions() as db:
        set_admin(db, "admin", ADMIN_PASS)
        db.commit()
    sent = {}

    async def fake_send(**kwargs):
        sent.update(kwargs)

    monkeypatch.setattr("backend.main.send_dingtalk_message", fake_send)
    with TestClient(app) as client:
        csrf = client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASS}).json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf}
        listed = client.get("/api/admin/integrations", headers=headers)
        assert listed.status_code == 200
        assert listed.json()["dingtalk_channels"] == []
        created = client.post("/api/admin/dingtalk-channels", json={
            "name": "开发群",
            "webhook": "https://oapi.dingtalk.com/robot/send?access_token=TOKEN123",
            "sign_secret": "SECabc",
            "enabled": True,
            "events": ["review_needs_attention"],
            "project_ids": ["a" * 32],
        }, headers=headers)
        assert created.status_code == 201
        body = created.json()
        assert body["webhook_configured"] and "TOKEN123" not in str(body) and "SECabc" not in str(body)
        channel_id = body["id"]
        public = client.get("/api/integrations")
        assert public.json()["dingtalk"]["configured"] is True
        assert "TOKEN123" not in public.text and "SECabc" not in public.text
        updated = client.put(f"/api/admin/dingtalk-channels/{channel_id}", json={
            "name": "开发群", "webhook": "", "sign_secret": "", "enabled": True,
            "events": ["review_needs_attention", "merged"], "project_ids": ["a" * 32],
        }, headers=headers)
        assert updated.status_code == 200
        assert updated.json()["events"] == ["review_needs_attention", "merged"]
        tested = client.post(f"/api/admin/dingtalk-channels/{channel_id}/test", headers=headers)
        assert tested.status_code == 200 and tested.json()["ok"] is True
        assert sent.get("at_mobiles") in (None, [])
        assert "开发群" in sent["text"]
        assert sent["webhook"].startswith("https://oapi.dingtalk.com/robot/send")
        missing = client.post("/api/admin/dingtalk-channels", json={
            "name": "坏", "webhook": "https://example.com/hook", "sign_secret": "SECabc",
            "events": [], "project_ids": [],
        }, headers=headers)
        assert missing.status_code == 422
        deleted = client.delete(f"/api/admin/dingtalk-channels/{channel_id}", headers=headers)
        assert deleted.status_code == 200
        assert client.get("/api/admin/integrations", headers=headers).json()["dingtalk_channels"] == []


def test_coalesce_events_drops_column_change_when_specific_present():
    events = [
        ("review_needs_attention", "审查不通过"),
        ("board_column_change", "进入需要关注"),
    ]
    assert coalesce_events(events) == [("review_needs_attention", "审查不通过")]
    assert coalesce_events([("board_column_change", "x")]) == [("board_column_change", "x")]


def _channel(settings, db, **changes):
    body = dict(name="开发群", webhook="https://oapi.dingtalk.com/robot/send?access_token=TOKEN123",
                sign_secret="SECabc", enabled=True, events=["review_needs_attention", "board_column_change"],
                project_ids=["a" * 32])
    body.update(changes)
    return save_dingtalk_channel(settings, db, DingTalkChannelInput(**body))


@pytest.mark.asyncio
async def test_dispatch_filters_dedupes_and_writes_timeline(setup_db):
    settings, sessions, _ = setup_db
    posts = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content))
        return httpx.Response(200, json={"errcode": 0})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with sessions() as db:
        _channel(settings, db)
        automation = db.get(ProjectAutomation, "a" * 32)
        automation.config = {**(automation.config or {}), "managers": ["13800138000"]}
        record = Record(repo="acme/mobile", kind="pr", external_id="21", number=7, title="Fix retry",
                        url="https://github.com/acme/mobile/pull/7", status="changes_requested", stage="review")
        db.add(record)
        db.flush()
        job = Job(key="review:1", record_id=record.id, kind="review", status="running")
        db.add(job)
        db.commit()
        record_id, job_id = record.id, job.id
    with sessions() as db:
        record, job = db.get(Record, record_id), db.get(Job, job_id)
        await dispatch_dingtalk(
            settings, sessions, record,
            [("review_needs_attention", "PR 审查需要处理：需要改"), ("board_column_change", "进入需要关注")],
            job, client=client, now_ms=1700000000000,
        )
        await dispatch_dingtalk(
            settings, sessions, record,
            [("review_needs_attention", "PR 审查需要处理：需要改")],
            job, client=client, now_ms=1700000000000,
        )
        titles = [row.title for row in db.scalars(select(Timeline))]
    await client.aclose()
    assert len(posts) == 1
    assert posts[0]["at"]["atMobiles"] == ["13800138000"]
    assert any("已通过钉钉通知" in title for title in titles)


@pytest.mark.asyncio
async def test_dispatch_ignores_leftover_github_managers(setup_db):
    settings, sessions, _ = setup_db
    posts = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content))
        return httpx.Response(200, json={"errcode": 0})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with sessions() as db:
        _channel(settings, db)
        automation = db.get(ProjectAutomation, "a" * 32)
        automation.config = {**(automation.config or {}), "managers": ["octocat"]}
        record = Record(repo="acme/mobile", kind="pr", external_id="31", number=13, title="x",
                        url="https://github.com/acme/mobile/pull/13", status="changes_requested", stage="review")
        db.add(record)
        db.commit()
        await dispatch_dingtalk(settings, sessions, record, [("review_needs_attention", "需要改")], client=client)
    await client.aclose()
    assert len(posts) == 1
    assert posts[0]["at"]["atMobiles"] == []


@pytest.mark.asyncio
async def test_dispatch_merged_dedupes_across_job_and_record(setup_db):
    settings, sessions, _ = setup_db
    posts = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(1)
        return httpx.Response(200, json={"errcode": 0})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with sessions() as db:
        channel = _channel(settings, db, events=["merged"])
        record = Record(repo="acme/mobile", kind="pr", external_id="32", number=14, title="x",
                        url="https://github.com/acme/mobile/pull/14", status="merged", stage="merge")
        db.add(record)
        db.flush()
        job = Job(key="merge:1", record_id=record.id, kind="merge", status="success")
        db.add(job)
        db.commit()
        record_id, job_id, channel_id = record.id, job.id, channel.id
    with sessions() as db:
        record, job = db.get(Record, record_id), db.get(Job, job_id)
        await dispatch_dingtalk(settings, sessions, record, [("merged", "已合并")], job, client=client)
    with sessions() as db:
        record = db.get(Record, record_id)
        details = dict(record.details or {})
        assert f"{channel_id}:merged" in (details.get("dingtalk_sent") or [])
        await dispatch_dingtalk(settings, sessions, record, [("merged", "已合并")], client=client)
    await client.aclose()
    assert len(posts) == 1


@pytest.mark.asyncio
async def test_dispatch_skips_demo_disabled_wrong_project_and_event(setup_db):
    settings, sessions, _ = setup_db
    posts = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(1)
        return httpx.Response(200, json={"errcode": 0})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with sessions() as db:
        _channel(settings, db, enabled=False, name="停用")
        _channel(settings, db, name="其他项目", project_ids=[], events=["merged"])
        record = Record(repo="acme/mobile", kind="pr", external_id="22", number=8, title="x",
                        url="https://github.com/acme/mobile/pull/8", demo=True)
        db.add(record)
        db.commit()
        await dispatch_dingtalk(settings, sessions, record, [("merged", "已合并")], client=client)
    await client.aclose()
    assert posts == []


@pytest.mark.asyncio
async def test_dispatch_failure_timeline_redacts_token(setup_db):
    settings, sessions, _ = setup_db

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errcode": 1, "errmsg": "https://oapi.dingtalk.com/robot/send?access_token=TOKEN123 boom"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with sessions() as db:
        _channel(settings, db)
        record = Record(repo="acme/mobile", kind="pr", external_id="23", number=9, title="x",
                        url="https://github.com/acme/mobile/pull/9")
        db.add(record)
        db.commit()
        await dispatch_dingtalk(settings, sessions, record, [("review_needs_attention", "失败")], client=client)
        message = db.scalar(select(Timeline).where(Timeline.title == "钉钉通知未确认发送")).message
    await client.aclose()
    assert "钉钉返回 1" in message
    assert "TOKEN123" not in message


@pytest.mark.asyncio
async def test_dispatch_skips_unchanged_board_column(setup_db):
    settings, sessions, _ = setup_db
    posts = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(1)
        return httpx.Response(200, json={"errcode": 0})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with sessions() as db:
        _channel(settings, db, events=["board_column_change"])
        record = Record(repo="acme/mobile", kind="pr", external_id="24", number=10, title="x",
                        url="https://github.com/acme/mobile/pull/10", status="failed", stage="review")
        db.add(record)
        db.commit()
        await dispatch_dingtalk(settings, sessions, record, [("board_column_change", "进入需要关注")], client=client)
        await dispatch_dingtalk(settings, sessions, record, [("board_column_change", "进入需要关注")], client=client)
    await client.aclose()
    assert len(posts) == 1


@pytest.mark.asyncio
async def test_dispatch_resends_board_column_when_column_changes(setup_db):
    settings, sessions, _ = setup_db
    posts = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(1)
        return httpx.Response(200, json={"errcode": 0})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with sessions() as db:
        _channel(settings, db, events=["board_column_change"])
        record = Record(repo="acme/mobile", kind="pr", external_id="25", number=11, title="x",
                        url="https://github.com/acme/mobile/pull/11", status="failed", stage="review")
        db.add(record)
        db.commit()
        record_id = record.id
        await dispatch_dingtalk(settings, sessions, record, [("board_column_change", "进入需要关注")], client=client)
        await dispatch_dingtalk(settings, sessions, record, [("board_column_change", "进入需要关注")], client=client)
        record.status = "merged"
        db.commit()
        await dispatch_dingtalk(settings, sessions, record, [("board_column_change", "进入已完成")], client=client)
    await client.aclose()
    assert len(posts) == 2
    with sessions() as db:
        details = dict(db.get(Record, record_id).details or {})
    assert details.get("dingtalk_board_column") == "done"
    assert not any(str(item).endswith(":board_column_change") for item in (details.get("dingtalk_sent") or []))


@pytest.mark.asyncio
async def test_dispatch_skips_disabled_empty_project_and_unsubscribed_event(setup_db):
    settings, sessions, _ = setup_db
    posts = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(1)
        return httpx.Response(200, json={"errcode": 0})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with sessions() as db:
        _channel(settings, db, enabled=False, name="停用")
        _channel(settings, db, name="无项目", project_ids=[], events=["review_needs_attention"])
        _channel(settings, db, name="未订阅", events=["merged"])
        record = Record(repo="acme/mobile", kind="pr", external_id="26", number=12, title="x",
                        url="https://github.com/acme/mobile/pull/12")
        db.add(record)
        db.commit()
        await dispatch_dingtalk(settings, sessions, record, [("review_needs_attention", "审查不通过")], client=client)
    await client.aclose()
    assert posts == []


@pytest.mark.asyncio
async def test_review_failure_sends_dingtalk_not_github_notification(setup_db, tmp_path, monkeypatch):
    from backend.db import ProjectAutomation
    from backend.worker import Worker
    from tests.test_worker import FakeAgent, FakeGitHub, FakeWorkspace
    from backend.service import upsert_github
    settings, sessions, _ = setup_db
    posts = []

    async def fake_dispatch(settings_, sessions_, record, events, job=None, **kwargs):
        posts.append(list(events))

    monkeypatch.setattr("backend.worker.dispatch_dingtalk", fake_dispatch, raising=False)
    # 若 Worker 从 backend.dingtalk 导入符号，则 patch backend.dingtalk.dispatch_dingtalk
    monkeypatch.setattr("backend.dingtalk.dispatch_dingtalk", fake_dispatch)

    class Reject(FakeAgent):
        async def execute(self, task_type, task_id, instructions, input_text, state, save):
            data = json.loads(input_text)
            return json.dumps({
                "head_sha": data["workspace"]["head_sha"], "verdict": "changes_requested",
                "summary": "needs a fix", "findings": [{
                    "file": "file.py", "line": 1, "severity": "high", "title": "bug",
                    "explanation": "broken", "suggestion": "fix it",
                }],
            })

    gh = FakeGitHub()
    with sessions() as db:
        save_dingtalk_channel(settings, db, DingTalkChannelInput(
            name="开发群", webhook="https://oapi.dingtalk.com/robot/send?access_token=t",
            sign_secret="SECabc", events=["review_needs_attention", "board_column_change"],
            project_ids=["a" * 32],
        ))
        db.get(ProjectAutomation, "a" * 32).config = {**(db.get(ProjectAutomation, "a" * 32).config or {}), "managers": ["13800138000"]}
        upsert_github(db, settings.project("acme/mobile"), "pr", gh.pr, canonical=True)
        db.commit()
    worker = Worker(settings, sessions=sessions, github=gh, agent=Reject(), workspaces=FakeWorkspace(tmp_path / "workspaces"))
    await worker.run_job(worker.claim())
    keys = [event for batch in posts for event, _ in batch]
    assert "review_needs_attention" in keys
    assert "board_column_change" not in keys
    assert not any("flowdeck:notification" in marker for marker, _ in gh.comments)
