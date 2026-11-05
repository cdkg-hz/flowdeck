from types import SimpleNamespace
import asyncio
import json

import pytest
from cryptography.fernet import Fernet

from backend.config import Settings
from backend.credentials import seal
from backend.db import AgentConnection, Base, database, initialize_schema
from backend.errors import ConfigurationNeeded, OutcomeUnknown
from backend.integrations import AgentConnectionInput, AgentRuntime, load_agent, load_enabled_agents, save_agent


def test_agent_connection_reasoning_effort_column(tmp_path):
    engine, sessions = database(f"sqlite:///{(tmp_path / 'mig.db').as_posix()}")
    Base.metadata.create_all(engine)
    initialize_schema(engine)
    with sessions() as db:
        db.add(AgentConnection(id="a" * 32, name="A", base_url="https://api.example.com/v1",
                               model="gpt-5-codex", reasoning_effort="high"))
        db.commit()
    with sessions() as db:
        item = db.get(AgentConnection, "a" * 32)
        assert item.reasoning_effort == "high"
    engine.dispose()


def _settings(tmp_path):
    settings = Settings(_env_file=None, app_mode="live", database_url=f"sqlite:///{(tmp_path / 't.db').as_posix()}",
                        data_root=tmp_path / "data", config_path=str(tmp_path / "projects.yml"))
    return settings.model_copy(update={"secrets_encryption_key": Fernet.generate_key().decode()})


def test_agent_connection_input_requires_model_and_validates_effort():
    with pytest.raises(ValueError):
        AgentConnectionInput(name="A", base_url="https://api.example.com/v1", api_key="k", model="")
    with pytest.raises(ValueError):
        AgentConnectionInput(name="A", base_url="https://api.example.com/v1", api_key="k",
                             model="gpt-5-codex", reasoning_effort="ultra")
    with pytest.raises(ValueError):
        AgentConnectionInput(name="A", base_url="https://api.example.com/v1", api_key="k",
                             model="gpt-5-codex", reasoning_effort="minimal")
    body = AgentConnectionInput(name="A", base_url="https://api.example.com/v1", api_key="k",
                                model="gpt-5-codex", reasoning_effort="max")
    assert body.reasoning_effort == "max"
    assert not hasattr(body, "protocol")


def test_load_agent_requires_model_and_load_enabled_agents(tmp_path):
    settings = _settings(tmp_path)
    engine, sessions = database(f"sqlite:///{(tmp_path / 'agents.db').as_posix()}")
    Base.metadata.create_all(engine)
    with sessions() as db:
        db.add(AgentConnection(id="1" * 32, name="NoModel", base_url="https://api.example.com/v1",
                               api_key_ciphertext=seal(settings, "agent:" + "1" * 32, "api_key", "k1"), model=""))
        db.add(AgentConnection(id="2" * 32, name="Ready", base_url="https://api.example.com/v1",
                               api_key_ciphertext=seal(settings, "agent:" + "2" * 32, "api_key", "k2"),
                               model="gpt-5-codex", reasoning_effort="low"))
        db.add(AgentConnection(id="3" * 32, name="Disabled", base_url="https://api.example.com/v1",
                               api_key_ciphertext=seal(settings, "agent:" + "3" * 32, "api_key", "k3"),
                               model="gpt-5-codex", enabled=False))
        db.commit()
    with sessions() as db:
        with pytest.raises(ConfigurationNeeded, match="模型名"):
            load_agent(settings, db, "1" * 32)
        runtime = load_agent(settings, db, "2" * 32)
        assert runtime == AgentRuntime("2" * 32, "Ready", "https://api.example.com/v1", "k2", 180, "gpt-5-codex", "low")
        enabled = load_enabled_agents(settings, db)
        assert [item.id for item in enabled] == ["2" * 32]
    engine.dispose()


def test_load_agent_maps_legacy_minimal_effort(tmp_path):
    settings = _settings(tmp_path)
    engine, sessions = database(f"sqlite:///{(tmp_path / 'minimal.db').as_posix()}")
    Base.metadata.create_all(engine)
    with sessions() as db:
        db.add(AgentConnection(id="4" * 32, name="Legacy", base_url="https://api.example.com/v1",
                               api_key_ciphertext=seal(settings, "agent:" + "4" * 32, "api_key", "k4"),
                               model="gpt-5-codex", reasoning_effort="minimal"))
        db.commit()
    with sessions() as db:
        runtime = load_agent(settings, db, "4" * 32)
        assert runtime.reasoning_effort == "low"
    engine.dispose()


def test_save_agent_writes_copilot_protocol(tmp_path):
    settings = _settings(tmp_path)
    engine, sessions = database(f"sqlite:///{(tmp_path / 'save.db').as_posix()}")
    Base.metadata.create_all(engine)
    body = AgentConnectionInput(name="A", base_url="https://api.example.com/v1", api_key="k",
                                model="gpt-5-codex", reasoning_effort="max")
    with sessions() as db:
        item = save_agent(settings, db, body)
        db.commit()
        stored = db.get(AgentConnection, item.id)
        assert stored.protocol == "copilot"
        assert stored.reasoning_effort == "max"
    engine.dispose()


from backend.integrations import AgentRuntime as RT


def _agent(**kw):
    values = dict(id="ab12" * 8, name="Review Agent", base_url="https://gw.example.com/v1",
                  api_key="sk-secret-123", timeout_seconds=600, model="gpt-5-codex", reasoning_effort="high")
    values.update(kw)
    return RT(**values)


def test_copilot_environment_strips_github_tokens_and_skips_api_keys(tmp_path, monkeypatch):
    from backend.copilot_runtime import copilot_environment, provider_config
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_should_not_leak")
    monkeypatch.setenv("GH_TOKEN", "also_no")
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "nope")
    env = copilot_environment(tmp_path / "copilot-home")
    assert env["COPILOT_HOME"] == str(tmp_path / "copilot-home")
    assert env["PATH"] == "/usr/bin"
    assert "GITHUB_TOKEN" not in env and "GH_TOKEN" not in env
    assert "COPILOT_GITHUB_TOKEN" not in env
    assert "sk-secret-123" not in env.values()
    cfg = provider_config(_agent())
    assert cfg == {"type": "openai", "base_url": "https://gw.example.com/v1",
                   "api_key": "sk-secret-123", "wire_api": "completions"}


def test_describe_copilot_error_redacts_and_maps_chat_completions():
    from backend.copilot_runtime import describe_copilot_error
    agent = _agent()
    message = describe_copilot_error(RuntimeError("401 unauthorized sk-abcdefghijklmnop"), agent)
    assert "gw.example.com" in message and "sk-abcdefghijklmnop" not in message
    assert "API Key" in message
    assert "Chat Completions" in describe_copilot_error(RuntimeError("404 not found"), agent)
    assert "Responses" not in describe_copilot_error(RuntimeError("404 not found"), agent)


def test_parse_agent_json_strict():
    from backend.copilot_runtime import parse_agent_json
    from backend.schemas import ReviewResult
    valid = {"head_sha": "a" * 40, "verdict": "pass", "summary": "checked", "findings": []}
    assert parse_agent_json(json.dumps(valid), ReviewResult).verdict == "pass"
    extra = {**valid, "summary_note": ""}
    assert parse_agent_json(json.dumps(extra), ReviewResult).summary == "checked"
    nested = {
        "head_sha": "b" * 40, "verdict": "changes_requested", "summary": "needs a fix",
        "findings": [{"file": "src/a.py", "line": 3, "severity": "high", "title": "bug",
                      "explanation": "broken", "suggestion": "fix it", "code_snippet": "x = 1"}],
    }
    parsed = parse_agent_json(json.dumps(nested), ReviewResult)
    assert parsed.findings[0].file == "src/a.py"
    with pytest.raises(ValueError):
        parse_agent_json("```json\n" + json.dumps(valid) + "\n```", ReviewResult)


def test_scrub_secrets_nested():
    from backend.copilot_runtime import scrub_secrets
    value = {"a": ["token-1 leaked", {"b": "safe"}]}
    assert scrub_secrets(value, ["token-1"]) == {"a": ["[redacted] leaked", {"b": "safe"}]}


def test_permission_handler_denies_shell_and_out_of_workspace(tmp_path):
    from backend.copilot_runtime import permission_handler
    root = tmp_path / "task"
    (root / "code").mkdir(parents=True)
    inside = str(root / "code" / "a.py")
    outside = str(tmp_path / "other" / "x.py")
    read_only = permission_handler(root, "read_only")
    writer = permission_handler(root, "workspace_write")
    assert getattr(read_only(SimpleNamespace(kind="shell", full_command_text="rm -rf /"), {}), "kind", None) == "reject"
    assert getattr(writer(SimpleNamespace(kind="shell", full_command_text="ls"), {}), "kind", None) == "reject"
    assert getattr(read_only(SimpleNamespace(kind="url", url="https://example.com"), {}), "kind", None) == "reject"
    assert getattr(read_only(SimpleNamespace(kind="read", path=outside), {}), "kind", None) == "reject"
    assert getattr(read_only(SimpleNamespace(kind="read", path=inside), {}), "kind", None) == "approve-once"
    assert getattr(read_only(SimpleNamespace(kind="write", file_name=inside), {}), "kind", None) == "reject"
    assert getattr(writer(SimpleNamespace(kind="write", file_name=inside), {}), "kind", None) == "approve-once"
    assert getattr(writer(SimpleNamespace(kind="write", file_name=outside), {}), "kind", None) == "reject"
    assert getattr(read_only(SimpleNamespace(kind="mystery"), {}), "kind", None) == "reject"


class FakeResponse:
    def __init__(self, content):
        self.data = SimpleNamespace(content=content)


class FakeSession:
    def __init__(self, session_id, content, delay=0.0, events=None):
        self.session_id = session_id
        self.id = session_id
        self.content, self.delay, self.events = content, delay, events or []
        self.aborted = False
        self.sent = None
        self.on_event = None

    async def send_and_wait(self, prompt, **kwargs):
        self.sent = prompt
        timeout = float(kwargs.get("timeout", 60))
        handler = self.on_event
        for event in self.events:
            if handler:
                handler(event)
        if self.delay:
            if self.delay > timeout:
                raise TimeoutError()
            await asyncio.sleep(self.delay)
        return FakeResponse(self.content)

    async def abort(self):
        self.aborted = True

    async def disconnect(self):
        self.aborted = True


class FakeCopilot:
    def __init__(self, home, env):
        self.home, self.env = home, env
        self.started, self.resumed = [], []
        self.next_content = "ok"
        self.delay = 0.0
        self.events = []
        self.started_flag = False

    async def start(self):
        self.started_flag = True

    async def stop(self):
        self.started_flag = False

    async def create_session(self, **kwargs):
        session = FakeSession(f"session-{len(self.started) + 1}", self.next_content, self.delay, self.events)
        session.on_event = kwargs.get("on_event")
        self.started.append((session, kwargs))
        return session

    async def resume_session(self, session_id, **kwargs):
        session = FakeSession(session_id, self.next_content, self.delay, self.events)
        session.on_event = kwargs.get("on_event")
        self.resumed.append((session, kwargs))
        return session


@pytest.fixture
def adapter_env(tmp_path):
    settings = _settings(tmp_path)
    engine, sessions = database(f"sqlite:///{(tmp_path / 'adapter.db').as_posix()}")
    Base.metadata.create_all(engine)
    agent_id = "9" * 32
    with sessions() as db:
        db.add(AgentConnection(id=agent_id, name="Copilot", base_url="https://gw.example.com/v1",
                               api_key_ciphertext=seal(settings, "agent:" + agent_id, "api_key", "sk-live"),
                               model="gpt-5-codex", reasoning_effort="high", timeout_seconds=60))
        db.commit()
    holder = {}

    def factory(home, env):
        holder["client"] = FakeCopilot(home, env)
        return holder["client"]

    from backend.copilot_runtime import CopilotAdapter
    with sessions() as db:
        runtime = load_agent(settings, db, agent_id)
    yield CopilotAdapter(settings, sessions, client_factory=factory), runtime, holder, tmp_path
    engine.dispose()


async def test_copilot_adapter_runs_session_and_checkpoints(adapter_env):
    adapter, runtime, holder, tmp_path = adapter_env
    saved = []

    async def save(value):
        saved.append(json.loads(json.dumps(value)))

    workspace = tmp_path / "task"
    workspace.mkdir()
    output = await adapter.execute("review", "job-1-0", "instructions", "payload", {}, save,
                                   agent=runtime, workspace=workspace, output_schema={"type": "object"})
    assert output == "ok"
    client = holder["client"]
    session, kwargs = client.started[0]
    assert kwargs["model"] == "gpt-5-codex"
    assert kwargs["provider"] == {"type": "openai", "base_url": runtime.base_url, "api_key": "sk-live", "wire_api": "completions"}
    assert kwargs["working_directory"] == str(workspace)
    assert kwargs["reasoning_effort"] == "high"
    assert kwargs["system_message"]["mode"] == "replace"
    assert "output_schema" not in kwargs
    assert saved[0]["session_id"] == "session-1"
    assert "thread_id" not in saved[0]
    assert saved[-1]["output"] == "ok"
    assert client.env["COPILOT_HOME"] == str(tmp_path / "data" / "copilot-home")
    assert "sk-live" not in json.dumps(client.env)
    secret_files = [path for path in (tmp_path / "data" / "copilot-home").rglob("*") if path.is_file() and "sk-live" in path.read_text(encoding="utf-8", errors="ignore")]
    assert secret_files == []
    await adapter.close()


async def test_copilot_adapter_recovers_resumes_and_rejects_legacy(adapter_env):
    adapter, runtime, holder, tmp_path = adapter_env
    workspace = tmp_path / "task"
    workspace.mkdir()

    async def save(value):
        pass

    done = await adapter.execute("review", "job-1-0", "i", "p", {"output": "cached"}, save,
                                 agent=runtime, workspace=workspace)
    assert done == "cached" and "client" not in holder

    with pytest.raises(OutcomeUnknown, match="new-analysis"):
        await adapter.execute("review", "job-1-0", "i", "p", {"messages": [{"role": "user"}]}, save,
                              agent=runtime, workspace=workspace)

    with pytest.raises(OutcomeUnknown, match="new-analysis"):
        await adapter.execute("review", "job-1-0", "i", "p", {"agent_id": runtime.id, "thread_id": "t"}, save,
                              agent=runtime, workspace=workspace)

    with pytest.raises(OutcomeUnknown, match="其他 Agent"):
        await adapter.execute("review", "job-1-0", "i", "p", {"agent_id": "x" * 32, "session_id": "s"}, save,
                              agent=runtime, workspace=workspace)

    out = await adapter.execute("review", "job-1-0", "i", "p",
                                {"task_id": "job-1-0", "agent_id": runtime.id, "session_id": "session-9"}, save,
                                agent=runtime, workspace=workspace)
    assert out == "ok"
    assert holder["client"].resumed[0][0].session_id == "session-9"
    await adapter.close()


async def test_copilot_adapter_timeout_interrupts(adapter_env):
    adapter, runtime, holder, tmp_path = adapter_env
    workspace = tmp_path / "task"
    workspace.mkdir()

    async def save(value):
        pass

    slow = RT(runtime.id, runtime.name, runtime.base_url, runtime.api_key, 0, runtime.model, runtime.reasoning_effort)

    def factory(home, env):
        client = FakeCopilot(home, env)
        client.delay = 0.5
        holder["client"] = client
        return client

    adapter.client_factory = factory
    with pytest.raises(OutcomeUnknown, match="超"):
        await adapter.execute("review", "job-2-0", "i", "p", {}, save, agent=slow, workspace=workspace)
    session, _ = holder["client"].started[0]
    assert session.aborted is True
    await adapter.close()


async def test_probe_agent_ephemeral_roundtrip(tmp_path):
    from backend.copilot_runtime import probe_agent
    settings = _settings(tmp_path)
    agent = _agent()
    holder = {}

    def factory(home, env):
        holder["client"] = FakeCopilot(home, env)
        holder["client"].next_content = "OK"
        return holder["client"]

    message = await probe_agent(settings, None, agent, client_factory=factory)
    assert "已验证" in message
    _, kwargs = holder["client"].started[0]
    assert kwargs["provider"]["wire_api"] == "completions"
    assert kwargs["model"] == agent.model

    def broken(home, env):
        raise RuntimeError("connection refused")

    with pytest.raises(ValueError, match="连不上"):
        await probe_agent(settings, None, agent, client_factory=broken)


async def test_copilot_adapter_wraps_client_start_failures(adapter_env):
    adapter, runtime, holder, tmp_path = adapter_env
    workspace = tmp_path / "task"
    workspace.mkdir()

    async def save(value):
        pass

    def boom(home, env):
        raise RuntimeError("connection refused")

    adapter.client_factory = boom
    with pytest.raises(ConfigurationNeeded, match="连不上"):
        await adapter.execute("review", "job-start-0", "i", "p", {}, save, agent=runtime, workspace=workspace)

    class BrokenEnter:
        def __init__(self, home, env):
            self.home, self.env = home, env

        async def start(self):
            raise RuntimeError("failed to spawn copilot runtime")

        async def stop(self):
            return None

    adapter.client_factory = BrokenEnter
    adapter._client = None
    with pytest.raises(ConfigurationNeeded, match="Copilot 调用"):
        await adapter.execute("review", "job-start-1", "i", "p", {}, save, agent=runtime, workspace=workspace)


def test_summarize_copilot_event_hides_reasoning_and_commands():
    from backend.copilot_runtime import summarize_copilot_event
    read = SimpleNamespace(type="tool.execution_start", tool_name="read", arguments={"path": "docs/updata_log/产品功能介绍与使用手册_很长的文件名.md"})
    assert summarize_copilot_event(read, started=True) == "正在读取 docs/updata_log/产品功能介绍与使用手册_很长的文件名.md"
    assert summarize_copilot_event(read, started=False) == "已读取 docs/updata_log/产品功能介绍与使用手册_很长的文件名.md"
    write = SimpleNamespace(type="session.workspace_file_changed", path="code/file.py")
    assert summarize_copilot_event(write, started=True) == "正在写入 code/file.py"
    assert summarize_copilot_event(SimpleNamespace(type="session.plan_changed", text="secret plan"), started=True) == "正在整理任务"
    assert summarize_copilot_event(SimpleNamespace(type="tool.execution_start", tool_name="shell")) == "忽略了命令执行"
    assert summarize_copilot_event(SimpleNamespace(type="assistant.reasoning", text="内部思考")) is None
    assert summarize_copilot_event(SimpleNamespace(type="assistant.message", content='{"verdict":"pass"}')) is None
    long_path = "docs/" + ("very-long-name-" * 8) + ".md"
    summary = summarize_copilot_event(SimpleNamespace(type="session.workspace_file_changed", path=long_path), started=True)
    assert summary.startswith("正在写入 …")
    assert len(summary) <= len("正在写入 ") + 80


async def test_copilot_adapter_reports_progress(adapter_env):
    adapter, runtime, holder, tmp_path = adapter_env
    workspace = tmp_path / "task"
    workspace.mkdir()
    seen = []

    async def save(value):
        pass

    async def on_progress(text):
        seen.append(text)

    events = [SimpleNamespace(type="tool.execution_start", tool_name="read", arguments={"path": "code/file.py"})]

    def factory(home, env):
        client = FakeCopilot(home, env)
        client.events = events
        holder["client"] = client
        return client

    adapter.client_factory = factory
    output = await adapter.execute("review", "job-progress-0", "i", "p", {}, save,
                                   agent=runtime, workspace=workspace, on_progress=on_progress)
    assert output == "ok"
    assert "正在读取 code/file.py" in seen
    await adapter.close()
