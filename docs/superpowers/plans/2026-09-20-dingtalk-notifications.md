# 钉钉通知渠道 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用加密存储的多条钉钉群机器人渠道替换 GitHub @ 通知，可按事件和项目过滤，并在消息里 @ 项目手机号。

**Architecture:** 新表 `dingtalk_channels` 对齐 `AgentConnection`。`backend/board.py` 计算看板列；`backend/dingtalk.py` 负责加签、POST、按渠道投递。Worker 在审查/检查/合并/失败/发版/GitHub 同步后调用 `dispatch_dingtalk`。管理页「集成配置」增删渠道；项目「通知人」改为手机号。

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy / httpx / Fernet / React (`frontend/src/App.tsx`) / pytest。

**Spec:** `docs/superpowers/specs/2026-09-20-dingtalk-notifications-design.md`

## Global Constraints

- 只做钉钉群自定义机器人；不加企业内部应用、单聊、Slack、邮件。
- Webhook 仅 `https://oapi.dingtalk.com/robot/send?access_token=…`；SEC 创建必填，更新留空表示不改。
- 密钥用现有 `seal`/`unseal`，owner 为 `dingtalk:{id}`；API/日志/异常不得出现 access_token 或 SEC。
- GitHub `notify()` 评论与「发布需处理」Issue 删除；审查评论保留。
- `managers` 字段名不变，校验改为 `1[3-9]\d{9}`；发送时非法项忽略；测试按钮不 @。
- 新项目不自动进入渠道；空事件或空项目列表的渠道永不匹配。
- 演示记录不发钉钉。同一次 `dispatch_dingtalk` 不重试。同批若有具体事件则丢掉 `board_column_change`。
- 运行测试：`.venv\Scripts\python -m pytest --basetemp=./.pytest_temp -p no:cacheprovider -q`（下文 `pytest …` 均指此形式加文件参数）。
- 提交用 `git commit --only <paths>`，禁止 `git commit -a`。

## File Structure

| 文件 | 职责 |
|---|---|
| `backend/board.py` | 看板四列，与 `RecordsBoard.tsx` 的 `boardColumn` 对齐 |
| `backend/dingtalk.py` | Webhook 校验、加签、POST、markdown、渠道过滤与投递 |
| `backend/db.py` | `DingTalkChannel` 表 |
| `backend/integrations.py` | `DingTalkChannelInput`、`save_dingtalk_channel`、`dingtalk_public` |
| `backend/config.py` | `managers` 改为手机号校验 |
| `backend/main.py` | 渠道 CRUD + 测试发送；公开 `dingtalk.configured` |
| `backend/worker.py` | 用 `dispatch_dingtalk` 替换 GitHub `notify()` |
| `backend/projects.py` | 删除项目时从渠道 `project_ids` 去掉该 ID；脱敏加入 Webhook/SEC |
| `frontend/src/types.ts` / `App.tsx` / `styles.css` | 集成页渠道 UI；接入页文案；阶段名「通知」 |
| `config/projects.example.yml` | `managers` 注释改为手机号 |
| `tests/test_dingtalk.py` | 加签、发送、过滤、去重、API、脱敏 |
| `tests/test_worker.py` | 审查/合并不再发 GitHub 通知评论 |
| `tests/test_project_deletion.py` | 删除项目会修剪渠道 |

---

### Task 1: 看板列映射

**Files:**
- Create: `backend/board.py`
- Test: `tests/test_dingtalk.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `COLUMN_LABELS: dict[str, str]` 键 `waiting`/`active`/`attention`/`done`，值 `待处理`/`进行中`/`需要关注`/`已完成`
  - `board_column(*, status: str, kind: str = "", stage: str = "", deleted: bool = False) -> str`

- [ ] **Step 1: Write the failing test**

在 `tests/test_dingtalk.py`：

```python
from backend.board import COLUMN_LABELS, board_column


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_dingtalk.py::test_board_column_matches_frontend_sets -v`

Expected: FAIL，`backend.board` 无法导入。

- [ ] **Step 3: Write minimal implementation**

`backend/board.py`：

```python
from __future__ import annotations

COLUMN_LABELS = {
    "waiting": "待处理",
    "active": "进行中",
    "attention": "需要关注",
    "done": "已完成",
}
_ATTENTION = {"failed", "changes_requested", "uncertain", "needs_configuration", "inconclusive"}
_DONE = {"merged", "closed", "deleted", "success"}


def board_column(*, status: str, kind: str = "", stage: str = "", deleted: bool = False) -> str:
    visible = "deleted" if deleted else status
    if status in _ATTENTION:
        return "attention"
    if visible in _DONE or status in _DONE:
        return "done"
    if status == "running" or (kind == "pr" and stage == "review" and status in {"queued", "running"}):
        return "active"
    return "waiting"
```

规则必须与 `frontend/src/RecordsBoard.tsx` 的 `boardColumn` 一致（`ATTENTION` 看 `record.status`；`DONE` 看 `recordStatus` 或 `record.status`）。

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_dingtalk.py::test_board_column_matches_frontend_sets -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git commit --only backend/board.py tests/test_dingtalk.py -m "feat: 对齐看板四列映射供钉钉通知使用"
```

---

### Task 2: 钉钉加签与发送

**Files:**
- Create: `backend/dingtalk.py`（本任务只放校验/加签/POST/手机号过滤；`dispatch_dingtalk` 在 Task 5）
- Modify: `tests/test_dingtalk.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `DINGTALK_EVENTS: frozenset[str]`
  - `EVENT_LABELS: dict[str, str]`
  - `validate_dingtalk_webhook(url: str) -> str`
  - `dingtalk_sign(secret: str, timestamp_ms: int) -> str`
  - `notify_mobiles(values: list[str] | None) -> list[str]`
  - `format_dingtalk_markdown(record, message: str, mobiles: list[str]) -> str`
  - `async send_dingtalk_message(*, webhook: str, secret: str, title: str, text: str, at_mobiles: list[str] | None = None, client: httpx.AsyncClient | None = None, now_ms: int | None = None) -> None`

- [ ] **Step 1: Write the failing tests**

追加到 `tests/test_dingtalk.py`：

```python
import json
from urllib.parse import parse_qs, urlsplit
import httpx
import pytest
from backend.dingtalk import (
    DINGTALK_EVENTS,
    dingtalk_sign,
    format_dingtalk_markdown,
    notify_mobiles,
    send_dingtalk_message,
    validate_dingtalk_webhook,
)


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
    query = parse_qs(urlsplit(captured["url"]).query)
    assert query["timestamp"] == ["1700000000000"]
    assert query["sign"] == ["jcUpW0QmtKduN03n4JqQ0PBosVjqnM8gU7fIIvsDmCM%3D"]
    assert captured["body"]["msgtype"] == "markdown"
    assert captured["body"]["at"]["atMobiles"] == ["13800138000"]
    assert captured["body"]["at"]["isAtAll"] is False
    assert "@13800138000" in captured["body"]["markdown"]["text"]
    joined = " ".join(record.message for record in caplog.records) + captured["url"]
    assert "SECRETTOKEN" not in joined
    assert "SECabc" not in joined
    await client.aclose()


@pytest.mark.asyncio
async def test_send_dingtalk_message_raises_on_dingtalk_errcode():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errcode": 310000, "errmsg": "sign not match"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="sign not match"):
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_dingtalk.py -v`

Expected: FAIL，`backend.dingtalk` 无法导入。

- [ ] **Step 3: Write minimal implementation**

`backend/dingtalk.py`：

```python
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time
from urllib.parse import parse_qs, quote_plus, urlencode, urlsplit, urlunsplit
import httpx

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
_MOBILE = re.compile(r"1[3-9]\d{9}")


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
    mobiles = []
    for item in values or []:
        number = str(item).strip()
        if _MOBILE.fullmatch(number) and number not in mobiles:
            mobiles.append(number)
    return mobiles


def format_dingtalk_markdown(record, message: str, mobiles: list[str]) -> str:
    number = f"#{record.number}" if record.number else "-"
    parts = [f"**{record.repo}** {number} {record.title}", message, record.url or ""]
    if mobiles:
        parts.append(" ".join("@" + item for item in mobiles))
    return "\n\n".join(part for part in parts if part)


async def send_dingtalk_message(*, webhook: str, secret: str, title: str, text: str,
                                at_mobiles: list[str] | None = None, client: httpx.AsyncClient | None = None,
                                now_ms: int | None = None) -> None:
    webhook = validate_dingtalk_webhook(webhook)
    timestamp_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    parsed = urlsplit(webhook)
    query = dict(parse_qs(parsed.query, keep_blank_values=True))
    query = {key: values[0] if isinstance(values, list) else values for key, values in query.items()}
    query["timestamp"] = str(timestamp_ms)
    query["sign"] = dingtalk_sign(secret, timestamp_ms)
    target = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": text},
        "at": {"atMobiles": list(at_mobiles or []), "isAtAll": False},
    }
    own = client is None
    http = client or httpx.AsyncClient(timeout=10.0)
    try:
        response = await http.post(target, json=payload)
        response.raise_for_status()
        body = response.json()
        if body.get("errcode") not in (0, None):
            raise RuntimeError(body.get("errmsg") or f"钉钉返回 {body.get('errcode')}")
    finally:
        if own:
            await http.aclose()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_dingtalk.py -v`

Expected: PASS。若 `parse_qs` 把 query 做成 list 导致 sign 重复，改成上面的扁平化 `urlencode`。

- [ ] **Step 5: Commit**

```bash
git commit --only backend/dingtalk.py tests/test_dingtalk.py -m "feat: 实现钉钉机器人加签与消息发送"
```

---

### Task 3: 渠道表、加密保存、手机号校验

**Files:**
- Modify: `backend/db.py`（在 `AgentConnection` 后增加 `DingTalkChannel`）
- Modify: `backend/integrations.py`
- Modify: `backend/config.py`（`valid_managers`）
- Modify: `backend/projects.py`（`project_redactions`）
- Modify: `config/projects.example.yml`
- Modify: `tests/test_dingtalk.py`
- Modify: `tests/test_ingestion.py`（若有 GitHub login 的 managers 断言则改为手机号；当前没有则不必改）

**Interfaces:**
- Consumes: `validate_dingtalk_webhook`、`DINGTALK_EVENTS`、`seal`/`unseal`
- Produces:
  - `class DingTalkChannel`（`id, name, enabled, webhook_ciphertext, sign_secret_ciphertext, events, project_ids, created_at, updated_at`）
  - `class DingTalkChannelInput`
  - `dingtalk_public(value: DingTalkChannel) -> dict`
  - `save_dingtalk_channel(settings, db, body: DingTalkChannelInput, item: DingTalkChannel | None = None) -> DingTalkChannel`
  - `load_dingtalk_secrets(settings, item: DingTalkChannel) -> tuple[str, str]`
  - `prune_dingtalk_project(db, project_id: str) -> None`
  - `ProjectConfig.valid_managers` 只接受 `1[3-9]\d{9}`，去重保序；错误文案含「手机号」

- [ ] **Step 1: Write the failing tests**

追加：

```python
from cryptography.fernet import Fernet
from backend.config import ProjectConfig, Settings
from backend.credentials import unseal
from backend.db import DingTalkChannel, Project
from backend.integrations import DingTalkChannelInput, dingtalk_public, save_dingtalk_channel
from backend.projects import project_redactions, prune_dingtalk_project


def test_managers_must_be_china_mobile_numbers():
    ProjectConfig(repo="acme/mobile", managers=["13800138000"])
    with pytest.raises(ValueError, match="手机号"):
        ProjectConfig(repo="acme/mobile", managers=["octocat"])


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
        assert "TOKEN123" in redacted and "SECabc" in redacted


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
    with sessions() as db:
        with pytest.raises(HTTPException) as missing:
            save_dingtalk_channel(settings, db, DingTalkChannelInput(
                name="x", webhook="https://oapi.dingtalk.com/robot/send?access_token=t",
                sign_secret="SECabc", events=["review_needs_attention"], project_ids=["b" * 32],
            ))
        assert missing.value.status_code == 422
        with pytest.raises(HTTPException) as event:
            save_dingtalk_channel(settings, db, DingTalkChannelInput(
                name="y", webhook="https://oapi.dingtalk.com/robot/send?access_token=t",
                sign_secret="SECabc", events=["not_an_event"], project_ids=["a" * 32],
            ))
        assert event.value.status_code == 422


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
```

创建渠道缺 Webhook/SEC 时，`DingTalkChannelInput` 或 `save_dingtalk_channel` 必须 422。在 `test_save_dingtalk_channel_encrypts_and_hides_secrets` 同文件再加：

```python
def test_create_channel_requires_webhook_and_secret(setup_db):
    settings, sessions, _ = setup_db
    from fastapi import HTTPException
    with sessions() as db:
        with pytest.raises(HTTPException) as exc:
            save_dingtalk_channel(settings, db, DingTalkChannelInput(name="空", webhook="", sign_secret=""))
        assert exc.value.status_code == 422
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_dingtalk.py tests/test_ingestion.py -q`

Expected: FAIL（`DingTalkChannel` / `save_dingtalk_channel` 不存在；`managers` 仍接受 GitHub login）。

- [ ] **Step 3: Write minimal implementation**

1. `backend/db.py` 在 `AgentConnection` 后：

```python
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
```

`create_all` 会建新表；不必写 ALTER（新表）。

2. `backend/config.py` 的 `valid_managers`：

```python
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
```

3. `backend/integrations.py`：import `DingTalkChannel`、`DINGTALK_EVENTS`、`validate_dingtalk_webhook`。新增：

```python
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
```

`save_project_automation` 里现有 `ValidationError` 转中文处增加：若 `"手机号" in message`，则 `message = "钉钉通知人必须是中国大陆11位手机号"`。

4. `project_redactions`：import `DingTalkChannel`，在 agent 循环旁：

```python
        for channel in db.scalars(select(DingTalkChannel)):
            try:
                values.extend([
                    unseal(settings, "dingtalk:" + channel.id, "webhook", channel.webhook_ciphertext),
                    unseal(settings, "dingtalk:" + channel.id, "sign_secret", channel.sign_secret_ciphertext),
                ])
            except ConfigurationNeeded:
                pass
```

把 `prune_dingtalk_project` 放在 `integrations.py` 或 `projects.py` 均可，测试从实际模块导入；推荐放 `integrations.py`，`projects.py` 再 re-export 以免循环导入。若 `projects.py` 已 import `integrations`，则 prune 放 `integrations.py`，删除项目的 Task 7 从那里导入。

5. `config/projects.example.yml`：`managers: [] # 钉钉通知人手机号，不含国家码，可空`

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_dingtalk.py tests/test_ingestion.py tests/test_admin_automation.py -q`

Expected: PASS。`ProjectConfig` 校验失败不得拖垮 YAML 空 `managers`。

- [ ] **Step 5: Commit**

```bash
git commit --only backend/db.py backend/integrations.py backend/config.py backend/projects.py config/projects.example.yml tests/test_dingtalk.py -m "feat: 加密存储钉钉渠道并把通知人改为手机号"
```

---

### Task 4: 管理 API 与测试发送

**Files:**
- Modify: `backend/main.py`
- Modify: `tests/test_dingtalk.py`
- Modify: `tests/test_admin_automation.py`（`GET /api/admin/integrations` 增加 `dingtalk_channels` 键，空列表即可）

**Interfaces:**
- Consumes: `save_dingtalk_channel`、`dingtalk_public`、`load_dingtalk_secrets`、`send_dingtalk_message`
- Produces:
  - `GET /api/admin/integrations` → `{ agents, dingtalk_channels }`
  - `POST /api/admin/dingtalk-channels` 201
  - `PUT /api/admin/dingtalk-channels/{channel_id}`
  - `DELETE /api/admin/dingtalk-channels/{channel_id}`
  - `POST /api/admin/dingtalk-channels/{channel_id}/test` → `{ ok, message }`，不 @
  - `GET /api/integrations` → `dingtalk: { configured: bool }`

- [ ] **Step 1: Write the failing test**

```python
from starlette.testclient import TestClient
from backend.auth import set_admin
from backend.main import create_app

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
```

在 `tests/test_admin_automation.py` 的 integrations 断言旁加：`assert "dingtalk_channels" in listed.json()`。

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_dingtalk.py::test_dingtalk_channel_admin_api -v`

Expected: FAIL，404 或缺少 `dingtalk_channels`。

- [ ] **Step 3: Write minimal implementation**

`backend/main.py`：

- import `DingTalkChannel`、`DingTalkChannelInput`、`dingtalk_public`、`save_dingtalk_channel`、`load_dingtalk_secrets`、`send_dingtalk_message`。
- `GET /api/integrations` 返回增加 `"dingtalk": {"configured": bool(db.scalar(select(func.count()).select_from(DingTalkChannel).where(DingTalkChannel.enabled.is_(True), DingTalkChannel.webhook_ciphertext.is_not(None), DingTalkChannel.sign_secret_ciphertext.is_not(None))))}`。
- `admin_integrations` 返回增加 `"dingtalk_channels": [dingtalk_public(x) for x in db.scalars(select(DingTalkChannel).order_by(DingTalkChannel.name))]`。
- 复制 Agent 三条路由的结构，路径换成 `/api/admin/dingtalk-channels`，404 文案「钉钉渠道不存在」，删除返回 `{"ok": True}`。
- 测试发送：

```python
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
```

测试失败不要把 `exc` 原文（可能含 URL）返回给浏览器。Worker 投递时间线仍用 `sanitize`（Task 5）。

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_dingtalk.py tests/test_admin_automation.py -q`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git commit --only backend/main.py tests/test_dingtalk.py tests/test_admin_automation.py -m "feat: 增加钉钉渠道管理接口与测试发送"
```

---

### Task 5: dispatch_dingtalk（过滤、去重、时间线）

**Files:**
- Modify: `backend/dingtalk.py`（增加 `coalesce_events` 与 `dispatch_dingtalk`）
- Modify: `tests/test_dingtalk.py`

**Interfaces:**
- Consumes: Task 1–3 的函数与表；`project_config`；`add_timeline`；`notify_mobiles`；`EVENT_LABELS`；`COLUMN_LABELS`
- Produces:
  - `coalesce_events(events: list[tuple[str, str]]) -> list[tuple[str, str]]`
  - `async dispatch_dingtalk(settings, sessions, record, events: list[tuple[str, str]], job=None, *, client=None, now_ms=None) -> None`
  - 去重键 `"{channel_id}:{event}"` 写入 `job.result["dingtalk_sent"]` 或 `record.details["dingtalk_sent"]`
  - 列切换写入 `record.details["dingtalk_board_column"]`

- [ ] **Step 1: Write the failing tests**

```python
from backend.db import Job, ProjectAutomation, Record, Timeline
from backend.dingtalk import coalesce_events, dispatch_dingtalk
from backend.integrations import DingTalkChannelInput, save_dingtalk_channel
from sqlalchemy import select


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
    assert "TOKEN123" not in message
    assert "钉钉通知未确认发送"


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
```

测试文件顶部增加 `from backend.db import Job, ProjectAutomation, Record, Timeline`。

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_dingtalk.py -k dispatch -v`

Expected: FAIL，`dispatch_dingtalk` 不存在。

- [ ] **Step 3: Write minimal implementation**

在 `backend/dingtalk.py` 增加。为避免循环导入，`project_config` / `load_dingtalk_secrets` / `add_timeline` 在函数内 import。

```python
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
    with sessions() as db:
        config = project_config(settings, db, record.repo, active_only=False)
        project = db.scalar(select(Project).where(Project.repo == record.repo))
        if not config or not project or project.demo:
            return
        project_id = project.id
        project_name = project.name or project.repo
        channels = list(db.scalars(select(DingTalkChannel).where(DingTalkChannel.enabled.is_(True))))
        targets = []
        for channel in channels:
            try:
                webhook, secret = load_dingtalk_secrets(settings, channel)
            except Exception as exc:
                targets.append((channel.id, channel.name, list(channel.events or []), list(channel.project_ids or []), None, None, str(exc)))
                continue
            targets.append((channel.id, channel.name, list(channel.events or []), list(channel.project_ids or []), webhook, secret, None))
        current_record = db.get(Record, record.id)
        current_job = db.get(Job, job.id) if job is not None else None
        bucket = current_job.result if current_job is not None else current_record.details
        sent = list((bucket or {}).get("dingtalk_sent") or [])
        current_column = column_of(
            status=current_record.status, kind=current_record.kind,
            stage=current_record.stage, deleted=bool((current_record.details or {}).get("deleted")),
        )
        mobiles = notify_mobiles(config.managers)
        secrets = project_redactions(settings, sessions)
        successes, failures = [], []
        for event, message in events:
            if event == "board_column_change" and (current_record.details or {}).get("dingtalk_board_column") == current_column:
                continue
            for channel_id, name, channel_events, project_ids, webhook, secret, load_error in targets:
                if project_id not in project_ids or event not in channel_events:
                    continue
                marker = f"{channel_id}:{event}"
                if marker in sent:
                    continue
                if load_error or not webhook or not secret:
                    failures.append((name, redact(load_error or "钉钉渠道缺少 Webhook 或加签密钥", secrets)))
                    continue
                try:
                    title = f"{project_name} · {EVENT_LABELS[event]}"
                    text = format_dingtalk_markdown(current_record, message, mobiles)
                    await send_dingtalk_message(
                        webhook=webhook, secret=secret, title=title, text=text,
                        at_mobiles=mobiles, client=client, now_ms=now_ms,
                    )
                    sent.append(marker)
                    successes.append(name)
                    if event == "board_column_change":
                        details = dict(current_record.details or {})
                        details["dingtalk_board_column"] = current_column
                        current_record.details = details
                except Exception as exc:
                    failures.append((name, redact(str(exc), secrets)))
        if current_job is not None:
            current_job.result = {**dict(current_job.result or {}), "dingtalk_sent": sent}
            current_job.updated_at = utcnow()
        else:
            details = dict(current_record.details or {})
            details["dingtalk_sent"] = sent
            current_record.details = details
        if successes:
            add_timeline(db, current_record, "notification", "success", "已通过钉钉通知", "、".join(dict.fromkeys(successes)), job_id=current_job.id if current_job else None)
        for name, error in failures:
            add_timeline(db, current_record, "notification", "failed", "钉钉通知未确认发送", f"{name}：{error}", job_id=current_job.id if current_job else None)
        db.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_dingtalk.py -q`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git commit --only backend/dingtalk.py tests/test_dingtalk.py -m "feat: 按项目和事件投递钉钉通知并去重"
```

---

### Task 6: Worker 替换 GitHub 通知

**Files:**
- Modify: `backend/worker.py`（删除 `notify` 的 GitHub 评论/Issue；接入 `dispatch_dingtalk`）
- Modify: `tests/test_worker.py`
- Modify: `tests/test_dingtalk.py`（增加审查不通过的 Worker 集成测试）

**Interfaces:**
- Consumes: `dispatch_dingtalk`、`board_column`、`COLUMN_LABELS`
- Produces: Worker 在下列时机调用 `dispatch_dingtalk`，且 GitHub 不再出现 `<!-- flowdeck:notification:`

事件接线：

| 时机 | `events` |
|---|---|
| 审查 `verdict != pass`，`finish` 之后 | `review_needs_attention` + 可能的列切换 |
| 审查 `verdict == pass`，`finish` 之前或之后 | `review_passed` + 可能的列切换 |
| 必需检查 `failure` | `checks_failed` |
| `merge` 成功 `finish("merged")` 之后 | `merged` |
| `run_job` 异常收尾 `failed`/`uncertain`/`needs_configuration` | `job_failed` |
| `release` 的 `finish("success")` 之后 | `release_ready` |
| `process_event` 里 `upsert_github` 之后（非 snapshot） | 若 status 变为 `merged` 则 `merged`；外加列切换 |

辅助方法（放在 `Worker` 上）：

```python
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
```

`notify(self, ctx, config, message)` **整段删除**。所有 `await self.notify(...)` 换成 `emit_dingtalk`。

审查函数在改 record 前：`previous_column = self._column(ctx.snapshot()[1])`。检查失败时 record 可能仍是 waiting：只发 `checks_failed`，把当时 column 传入以便若变为 attention 也合并进同一批。

`process_event`：upsert 前读出旧 record 的 column 与 status；commit 后若不是 snapshot，再 `emit_dingtalk`（无 job）。跳过 `action == "sync"` 的快照导入（`upsert_github(..., snapshot=True)` 的路径）。Webhook 处理里 `snapshot` 默认 False，会发通知。

- [ ] **Step 1: Write the failing tests**

在 `tests/test_dingtalk.py`：

```python
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

    monkeypatch.setattr("backend.worker.dispatch_dingtalk", fake_dispatch)
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
```

在 `tests/test_worker.py` 的 `test_review_accepts_extra_json_fields` 后断言：`assert all("flowdeck:notification" not in (marker or "") for marker, _ in gh.comments)`。审查评论 `flowdeck:review` 仍应存在。

再写 `test_merged_emits_merged_event`：现有 `test_review_then_merge_exact_commit` 路径上 monkeypatch `dispatch_dingtalk`，第二次 `run_job` 后 events 含 `merged`。

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_dingtalk.py::test_review_failure_sends_dingtalk_not_github_notification tests/test_worker.py -q`

Expected: FAIL（仍走 GitHub `notify`，或未调用 `dispatch_dingtalk`）。

- [ ] **Step 3: Write minimal implementation**

按本任务 Interfaces 改 `backend/worker.py`。删除「管理人尚未配置」分支。`emit_dingtalk` 在 `finish` 之后用 `ctx.snapshot()` 取最新 record（`owned(..., running=False)` 已允许 completed job）。

异常收尾里，在写入 `record.status = status` **之前**记下 `previous_column`，写库 commit 后再 `await self.emit_dingtalk(ctx, record, [("job_failed", message)], job, previous_column)`。此时 `ctx.snapshot` 的 job 已不是 running，与 `emit` 一致。

Patch 位置与 Worker 的 import 方式保持一致：若 `from backend.dingtalk import dispatch_dingtalk`，测试 patch `backend.worker.dispatch_dingtalk`。

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_dingtalk.py tests/test_worker.py tests/test_recovery.py tests/test_agent_tasks.py -q`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git commit --only backend/worker.py tests/test_worker.py tests/test_dingtalk.py -m "feat: Worker 改为按渠道发送钉钉通知"
```

---

### Task 7: 删除项目修剪、管理页、文案

**Files:**
- Modify: `backend/projects.py`（`delete_project` 在删 Project 前调用 `prune_dingtalk_project`）
- Modify: `tests/test_project_deletion.py`
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles.css`（仅当现有 `event-picker`/`agent-row` 不够用时加 `.check-grid`）
- Modify: `README.md`（集成配置一节补一句钉钉渠道；没有该节则只改工作流页文案）

**Interfaces:**
- Consumes: `prune_dingtalk_project`、渠道 API、`EVENT_LABELS` 的中文（前端写一份常量即可，不必从后端拉）
- Produces: 删除项目后渠道不再引用该 ID；集成页可增删改测渠道；接入页「钉钉通知人」；阶段名「通知」

- [ ] **Step 1: Write the failing deletion test**

在 `tests/test_project_deletion.py` 创建项目并 `POST /api/admin/dingtalk-channels`（`project_ids: [proj_id]`）后删除项目，再 `GET /api/admin/integrations`，断言该渠道 `project_ids == []`（渠道本身还在）。

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_project_deletion.py -q`

Expected: FAIL，删除后 `project_ids` 仍含旧 ID（或 500）。

- [ ] **Step 3: Implement backend prune + frontend**

`delete_project` 在 `db.execute(delete(Project)...)` 之前：`prune_dingtalk_project(db, project_id)`。

前端：

`types.ts`：

```typescript
export type DingTalkChannel = {
  id: string; name: string; enabled: boolean; events: string[]; project_ids: string[];
  webhook_configured: boolean; sign_configured: boolean; updated_at: string;
};
export type AdminIntegrations = { agents: AgentConnection[]; dingtalk_channels: DingTalkChannel[] };
export type Integrations = { /* 现有字段 */ dingtalk?: { configured: boolean } } & { /* 保持其余字段不变 */ };
```

`App.tsx`：

- `STAGES.notification` 改为 `'通知'`。
- 工作流 `development` 的 review `failure`：`发现问题发钉钉；结果格式异常或分析未完成时不视为通过。`
- `exception-path` 文案里「通知管理人」改为「发钉钉通知」。
- `IntegrationsPage` 的 `services` 增加 `{ name: '钉钉', icon: Bell, configured: integrations?.dingtalk?.configured, text: '按渠道把审查、合并和失败发到群机器人', detail: '自定义机器人 Webhook + 加签，可选事件和项目' }`。
- `AgentConnections` 下方渲染 `<DingTalkChannels session={session} projects={projects.filter(p => !p.demo)} />`（把 `projects` 传入 `IntegrationsPage` 已有）。
- 接入详情 label：`钉钉通知人`，placeholder `手机号，逗号分隔，可空`，`aria-label="钉钉通知人"`。

`DingTalkChannels` 对齐 `AgentConnections`：列表用 `agent-row`；表单字段名称、Webhook、SEC（password + 显示切换）、启用、事件多选、项目多选。事件选项固定：

```typescript
const DINGTALK_EVENT_OPTIONS = [
  ['review_needs_attention', '审查需要处理'],
  ['review_passed', '审查通过'],
  ['checks_failed', '必需检查失败'],
  ['merged', 'PR 已合并'],
  ['job_failed', '任务失败 / 不确定 / 缺配置'],
  ['release_ready', '发版已完成'],
  ['board_column_change', '看板列切换'],
];
```

空事件且空项目时在表单显示 `当前不会发出任何消息`。测试按钮调用 `POST /api/admin/dingtalk-channels/${id}/test`。删除 `window.confirm('删除这个钉钉渠道？')`。编辑不回填 webhook/SEC；更新时传空字符串。创建时两个密钥 `required`。

- [ ] **Step 4: Run tests and frontend build**

Run: `pytest tests/test_project_deletion.py tests/test_dingtalk.py tests/test_admin_automation.py tests/test_worker.py -q`

Run: `npm --prefix frontend run build`

Expected: pytest PASS；build 成功。

在浏览器打开集成配置：新增渠道、测试、编辑留空密钥、勾选事件/项目、删除；打开接入详情确认「钉钉通知人」文案。若本环境没有已启动的 `8765`，用 `pytest` 作为行为证据，并在实现记录里写明未做浏览器点击。

- [ ] **Step 5: Commit**

```bash
git commit --only backend/projects.py tests/test_project_deletion.py frontend/src/types.ts frontend/src/App.tsx frontend/src/styles.css README.md -m "feat: 在集成页配置钉钉渠道并修剪已删项目"
```

只 add 实际改动的路径。

---

## Self-Review

1. **Spec coverage:** 渠道表与加密、官方 Webhook + 必填加签、事件七项、列切换与同批去重、替换 GitHub @、手机号 at、演示跳过、删除项目修剪、管理 API/测试按钮、时间线成功/失败、脱敏、测试矩阵均有对应 Task。非目标（企业应用、渠道级 @ 名单、自动重试、YAML 渠道、深链）未列入任务。
2. **Placeholders:** 无 TBD/TODO；Task 2 的 timestamp 与 Task 5 的 dispatch 均为完整可粘贴实现。
3. **Types:** `dispatch_dingtalk(settings, sessions, record, events, job=None, *, client=None, now_ms=None)` 在 Task 5 定义，Task 6 Worker 与测试按此签名 patch。
