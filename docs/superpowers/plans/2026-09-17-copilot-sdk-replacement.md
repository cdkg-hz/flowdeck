# Copilot SDK 替换实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用官方 GitHub Copilot Python SDK(`github-copilot-sdk`)替换 Flowdeck 的 Codex 运行时,覆盖 PR 审查、Release 更新说明、事件规则任务三类场景。第三方模型改为 Chat Completions BYOK;删除 `openai-codex`。

**Architecture:** Worker 进程内嵌单例 `CopilotClient`(本地 Copilot CLI 子进程)。每条任务 `create_session(provider={type:openai, wire_api:completions, base_url, api_key})`。只读/写权限靠自定义 `on_permission_request`(禁止 `approve_all`)。合并/推送仍由 Flowdeck 在任务返回后执行。API Key 只进 session 内存,不写 `COPILOT_HOME`。

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy / `github-copilot-sdk`(锁版本)/ React (`frontend/src/App.tsx`) / pytest。

**Spec:** `docs/superpowers/specs/2026-09-17-copilot-sdk-replacement-design.md`

## Global Constraints

- 只支持第三方 Chat Completions BYOK;不支持 Copilot 订阅、GitHub Token、`gh` 登录、Responses、Azure 原生、Anthropic。
- CLI 子进程环境必须白名单,并剔除 `GITHUB_TOKEN` / `GH_TOKEN` / `COPILOT_GITHUB_TOKEN` / `GITHUB_COPILOT_API_TOKEN`。`use_logged_in_user=False`。
- Agent 不接触 GitHub PAT;写任务允许工作区内写文件,仍拒绝 shell 与网络。
- 旧 checkpoint 含 `messages` 或 `thread_id` → `OutcomeUnknown`,提示 `retry JOB_ID --new-analysis`。新 checkpoint 用 `session_id`。
- 保留 Worker `self.agent` 窄协议注入缝。假 adapter 测试不改 `execute(task_type, task_id, instructions, input_text, state, save)` 位置参数形状。
- 结构化结果仍靠 prompt 中的 JSON Schema + `parse_agent_json`;`output_schema` 形参可留但不传给 Copilot SDK。
- 运行测试:`.venv\Scripts\python -m pytest --basetemp=./.pytest_temp -p no:cacheprovider -q`(下文 `pytest …` 均指此形式加文件参数)。CI 不下载 Copilot runtime、不访问模型网络。
- 提交用 `git commit --only <paths>`。仓库暂存区有无关垃圾,严禁裸 `git commit -a`。
- 实现前先对照已安装 SDK 的真实签名(`CopilotClient.__init__`、`create_session`、`resume_session`、`send_and_wait`、`PermissionRequest*`、session abort)。与下文伪代码冲突时**以安装包为准**,测试假对象对齐真实调用形状。

---

### Task 1: 用 github-copilot-sdk 替换 openai-codex

**Files:**
- Modify: `requirements.txt`
- Modify: `requirements.lock`
- Modify: `Dockerfile`

**Interfaces:**
- Produces: 可 `from copilot import CopilotClient` 的环境;镜像构建期预拉 CLI runtime。

- [ ] **Step 1: 安装并记录版本与构造签名**

```powershell
.venv\Scripts\python -m pip uninstall -y openai-codex openai-codex-cli-bin
.venv\Scripts\python -m pip install github-copilot-sdk
.venv\Scripts\python -m pip show github-copilot-sdk
.venv\Scripts\python -c "import inspect; from copilot import CopilotClient; print(inspect.signature(CopilotClient.__init__)); print(inspect.signature(CopilotClient.create_session)); print(inspect.signature(CopilotClient.resume_session))"
```

Expected: 包名 `github-copilot-sdk`,版本 X.Y.Z。把 `CopilotClient` / `create_session` / `resume_session` 的关键字参数记下来(后续 Task 4–5 使用)。再列出权限类型:

```powershell
.venv\Scripts\python -c "import copilot, pkgutil, copilot.session_events as e; print([n for n in dir(e) if n.startswith('PermissionRequest')])"
```

记下 shell / 读文件 / 写文件对应的类名。

- [ ] **Step 2: 写入 requirements.txt**

把 `openai-codex==0.154.0` 整行换成:

```text
github-copilot-sdk==X.Y.Z
```

`requirements-dev.txt` 已是 `-r requirements.txt`,不用改。

- [ ] **Step 3: 更新 requirements.lock**

不要整文件 `pip freeze` 覆盖。只把 `openai-codex` 与 `openai-codex-cli-bin` 两行换成 `github-copilot-sdk==X.Y.Z` 及其**新引入**的直接依赖(用 `pip show github-copilot-sdk` 的 Requires,核对 freeze 里多出的包)。不要顺手升级 fastapi/sqlalchemy 等。

- [ ] **Step 4: Dockerfile 预拉 runtime**

在 `pip install --no-cache-dir -r requirements.lock` **之后**、`USER flowdeck` **之前**加入(目录必须对非 1000 的 `WORKFLOW_UID` 也可读):

```dockerfile
ENV COPILOT_CLI_EXTRACT_DIR=/opt/copilot-runtime
RUN python -m copilot download-runtime \
    && chmod -R a+rX /opt/copilot-runtime
ENV COPILOT_SKIP_CLI_DOWNLOAD=1
```

`download-runtime` 必须发生在设置 `COPILOT_SKIP_CLI_DOWNLOAD=1` 之前。`compose.yml` 的 `&environment` 增加 `COPILOT_SKIP_CLI_DOWNLOAD: "1"`,避免 `.env` 覆盖后运行时再拉二进制。

不要在本 Task 跑 `python -m copilot download-runtime` 到开发机全局缓存并提交该缓存。

- [ ] **Step 5: Commit**

```powershell
git add requirements.txt requirements.lock Dockerfile compose.yml
git commit --only requirements.txt requirements.lock Dockerfile compose.yml -m "build: 用 github-copilot-sdk 替换 openai-codex 并预拉 CLI runtime"
```

---

### Task 2: Agent 连接配置改为 Copilot / Chat Completions 语义

**Files:**
- Modify: `backend/integrations.py`
- Modify: `backend/main.py`(`"protocol": "codex"` → `"copilot"`)
- Modify: `tests/conftest.py`(`protocol="copilot"`)
- Modify: `tests/test_admin_automation.py`(protocol 断言)
- Modify: `tests/test_codex_runtime.py`(effort 校验;本 Task 末尾仍叫旧文件名,Task 7 再改名)

**Interfaces:**
- `AgentConnectionInput.valid_effort`:允许 `""` / `low` / `medium` / `high` / `xhigh` / `max`;拒绝 `minimal` 与未知值。
- `save_agent`:`item.protocol = "copilot"`。
- `load_agent`:文案改为「尚未为项目选择可用的模型接口」等,去掉「Codex」。`reasoning_effort == "minimal"` 时映射为 `"low"`。
- `save_project_automation`:「选择的 Codex 模型接口不存在」→「选择的模型接口不存在」。
- 公开集成摘要 `"protocol": "copilot"`。

- [ ] **Step 1: 写失败测试**

在 `tests/test_codex_runtime.py` 的 `test_agent_connection_input_requires_model_and_validates_effort` 中:

- `reasoning_effort="minimal"` 应变 `ValueError`
- `reasoning_effort="max"` 应成功
- 追加 `test_load_agent_maps_legacy_minimal_effort`:DB 存 `minimal`, `load_agent` 返回 `"low"`
- 追加 `test_save_agent_writes_copilot_protocol`(或扩现有 admin 测试):创建后 `item.protocol == "copilot"`

把 `test_admin_automation.py` 里 `assert integ.json()["agent"]["protocol"] == "codex"` 先改成 `"copilot"`(本步会 FAIL,Step 3 修 main.py)。

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_codex_runtime.py tests/test_admin_automation.py -q`
Expected: FAIL(`minimal` 仍合法,protocol 仍是 `codex`,文案仍含 Codex)。

- [ ] **Step 3: 实现**

`backend/integrations.py`:

```python
if value and value not in {"low", "medium", "high", "xhigh", "max"}:
    raise ValueError("推理力度必须是 low、medium、high、xhigh 或 max")
```

```python
item.name, item.base_url, item.protocol = body.name, body.base_url, "copilot"
```

`load_agent` 末尾:

```python
effort = item.reasoning_effort or ""
if effort == "minimal":
    effort = "low"
return AgentRuntime(..., effort)
```

同步修改 `load_enabled_agents` 的 effort。错误字符串去掉「Codex」。

`backend/main.py` 集成摘要 `"protocol": "copilot"`。`tests/conftest.py` 的 `protocol="copilot"`。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_codex_runtime.py tests/test_admin_automation.py tests/test_admin.py -q`
Expected: 与 protocol/effort 相关的断言 PASS。`test_admin_automation` 里 probe monkeypatch 仍指向 `backend.codex_runtime` 的可以先保持(Task 5 改)。

- [ ] **Step 5: Commit**

```powershell
git add backend/integrations.py backend/main.py tests/conftest.py tests/test_admin_automation.py tests/test_codex_runtime.py
git commit --only backend/integrations.py backend/main.py tests/conftest.py tests/test_admin_automation.py tests/test_codex_runtime.py -m "feat: Agent 连接改为 Copilot 协议与 Chat Completions 推理力度"
```

---

### Task 3: copilot_runtime 基础——环境、provider、权限、诊断

**Files:**
- Create: `backend/copilot_runtime.py`
- Modify: `tests/test_codex_runtime.py`(先加新测试;删掉 `render_config_toml` / `CODEX_HOME` 那些会在本 Task 失效的断言)

**Interfaces:**

```python
SYSTEM_RULES: str  # 从现 backend/codex_runtime.py 原样搬过来

def parse_agent_json(text: str, schema): ...  # 原样搬
def scrub_secrets(value, secrets: list[str]): ...  # 原样搬

_BASE_ENV_KEYS = ("PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL")
_STRIP_ENV_KEYS = ("GITHUB_TOKEN", "GH_TOKEN", "COPILOT_GITHUB_TOKEN", "GITHUB_COPILOT_API_TOKEN")

def copilot_environment(home: Path) -> dict[str, str]:
    """白名单环境 + COPILOT_HOME=home。不注入任何 API Key,不复制 GitHub token。"""

def provider_config(agent: AgentRuntime) -> dict:
    """{'type':'openai','base_url':agent.base_url,'api_key':agent.api_key,'wire_api':'completions'}"""

def describe_copilot_error(exc: BaseException, agent: AgentRuntime) -> str:
    """脱敏后映射 401/403/404/timeout/connect;404 文案指向 Chat Completions 而非 Responses。"""

def permission_handler(workspace: Path, sandbox: str):
    """返回 on_permission_request 回调。sandbox 为 read_only | workspace_write。"""

def summarize_copilot_event(event, *, started: bool = True) -> str | None:
    """把读/写文件变成短中文;推理与最终 JSON 消息返回 None。实现时按 Task 1 记下的事件类型匹配,测 SimpleNamespace 即可。"""
```

权限规则(测试必须锁住):

| 请求 | read_only | workspace_write |
|---|---|---|
| shell / 任意命令 | 拒绝 | 拒绝 |
| workspace 内读/搜索/列目录 | 允许 | 允许 |
| workspace 内写/打补丁 | 拒绝 | 允许 |
| workspace 外任意路径 | 拒绝 | 拒绝 |
| 未知类型 | 拒绝 | 拒绝 |

回调返回 SDK 的 reject/approve-once 对象。若测试不便 import 真实类,可让 handler 在测中返回简单对象,但生产路径必须用 Task 1 记下的 `PermissionDecisionReject` / `PermissionDecisionApproveOnce`。

- [ ] **Step 1: 写失败测试**

替换 `test_render_config_toml_and_environment` 为例如:

```python
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


def test_permission_handler_denies_shell_and_out_of_workspace(tmp_path):
    from backend.copilot_runtime import permission_handler
    root = tmp_path / "task"
    (root / "code").mkdir(parents=True)
    read_only = permission_handler(root, "read_only")
    writer = permission_handler(root, "workspace_write")
    # 用 SimpleNamespace 或 Task 1 的 PermissionRequestShell 构造三类请求:
    # shell、root/'code/a.py' 读、root 之外的写、root 内写。
    # 断言: shell 双模式都拒;越界拒;只读拒写;写模式放行工作区内写。
```

保留 `test_parse_agent_json_strict` / `test_scrub_secrets_nested`,导入改为 `backend.copilot_runtime`。

删掉本文件里所有 `from backend.codex_runtime import render_config_toml` 等已失效测试(Adapter 测试留到 Task 4 一起换,若本步 import 失败可暂时 skip/`pytest.importorskip` 不采用——直接改导入,Task 4 前 Adapter 测试会 FAIL,允许本 Task 只跑新基础测试):

Run 本 Task 验证时用:

`pytest tests/test_codex_runtime.py -q -k "environment or describe_copilot or permission_handler or parse_agent_json or scrub_secrets or reasoning_effort or load_agent or connection_input"`

- [ ] **Step 2: 运行确认失败**

Expected: `ModuleNotFoundError: backend.copilot_runtime`。

- [ ] **Step 3: 实现 `backend/copilot_runtime.py`**

从 `codex_runtime.py` 复制 `SYSTEM_RULES`、`parse_agent_json`、`scrub_secrets`。新建上述函数。`copilot_environment` **不要**接受 agents 列表、不要写 `FLOWDECK_AGENT_*_KEY`。

`describe_copilot_error` 404 示例文案:

`模型接口 {host} 找不到 Chat Completions 端点或模型。地址必须是 OpenAI 兼容根路径(提供 /chat/completions)`

未知错误:`Copilot 调用 {host} 失败：{message}`

- [ ] **Step 4: 运行确认通过**

Run: 同上 `-k` 过滤。Expected: PASS。

- [ ] **Step 5: Commit**

```powershell
git add backend/copilot_runtime.py tests/test_codex_runtime.py
git commit --only backend/copilot_runtime.py tests/test_codex_runtime.py -m "feat: copilot_runtime 基础——BYOK provider、环境白名单与权限闸"
```

---

### Task 4: CopilotAdapter 执行器(session、恢复、超时、进度)

**Files:**
- Modify: `backend/copilot_runtime.py`
- Modify: `tests/test_codex_runtime.py`

**Interfaces:**

```python
class CopilotAdapter:
    def __init__(self, settings, sessions, client_factory: Callable | None = None): ...
    async def close(self) -> None: ...
    async def execute(self, task_type, task_id, instructions, input_text, state, save, *,
                      agent: AgentRuntime, workspace: Path, sandbox: str = "read_only",
                      output_schema: dict | None = None, secret_values: list[str] | None = None,
                      on_progress: Callable | None = None) -> str: ...
```

行为:

- 单例 client:`base_directory=DATA_ROOT/copilot-home`,`env=copilot_environment(home)`,`use_logged_in_user=False`,不传 github_token。配置变更**不必**重启 client(provider 在 session 上)。
- `state["output"]` 已有 → 直接返回。
- `state["messages"]` 或 `state["thread_id"]` → `OutcomeUnknown`(`new-analysis`)。
- `state` 非空且 `agent_id` 不匹配 → `OutcomeUnknown`。
- 否则 `create_session` 或 `resume_session(state["session_id"])`;新建时先 `save({task_id, agent_id, session_id})`。
- `create_session` 传入 `model`、`provider=provider_config(agent)`、`working_directory=str(workspace)`、`reasoning_effort` 或省略、`system_message=instructions`(按 SDK 实际类型包装)、`on_permission_request=permission_handler(workspace, sandbox)`。
- **不要**把 `output_schema` 传给 Copilot。
- 订阅进度事件 → `summarize_copilot_event` → `on_progress` + `scrub_secrets`。
- `asyncio.wait_for(send_and_wait(input_text), timeout=agent.timeout_seconds)`;超时则 abort/disconnect(SDK 有什么用什么),抛 `OutcomeUnknown`。
- 最终文本空/超 2MiB/`error` → `RuntimeError`。成功则 `save({...state, output})`。
- client 启动失败 → `ConfigurationNeeded(describe_copilot_error(...))`。

- [ ] **Step 1: 写 Fake Copilot 与失败测试**

用假对象对齐 Task 1 的真实形状,例如:

```python
class FakeResponse:
    def __init__(self, content):
        self.data = SimpleNamespace(content=content)

class FakeSession:
    def __init__(self, session_id, content, delay=0.0, events=None):
        self.id = session_id
        self.content, self.delay, self.events = content, delay, events or []
        self.aborted = False
        self.sent = None
        self.kwargs = None  # create/resume kwargs 由 client 填

    async def send_and_wait(self, prompt, **kwargs):
        self.sent = prompt
        if self.delay:
            await asyncio.sleep(self.delay)
        return FakeResponse(self.content)

    def on(self, handler):
        for event in self.events:
            handler(event)
        return lambda: None

    async def disconnect(self):
        self.aborted = True

    # 若 SDK 用 abort/cancel,假对象同时实现并让 adapter 调用存在的那个

class FakeCopilot:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started, self.resumed = [], []
        self.next_content = "ok"
        self.delay = 0.0
        self.events = []
        self.started_flag = False

    async def start(self):
        self.started_flag = True

    async def stop(self):
        self.started_flag = False

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *args):
        await self.stop()

    async def create_session(self, **kwargs):
        session = FakeSession(f"session-{len(self.started)+1}", self.next_content, self.delay, self.events)
        self.started.append((session, kwargs))
        return session

    async def resume_session(self, session_id, **kwargs):
        session = FakeSession(session_id, self.next_content, self.delay, self.events)
        self.resumed.append((session, kwargs))
        return session
```

fixture `adapter_env` 改为 `CopilotAdapter(..., client_factory=factory)`。`factory` 只接收 adapter 真正传入的参数(Task 1 签名);断言:

- `create_session` 的 `provider["wire_api"]=="completions"` 且含 api_key
- `working_directory == str(workspace)`
- `use_logged_in_user is False`
- `COPILOT_HOME` 在 env 里;home 下**没有**把 api_key 写成文件
- checkpoint 第一笔含 `session_id`,不含 `thread_id`
- 旧 `thread_id` / `messages` 拒绝
- `session_id` 走 `resume_session`
- timeout 时 `aborted is True`
- 进度事件能进 `on_progress`

删除 FakeCodex / `thread_start` / `config.toml` 测试。

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_codex_runtime.py -q`
Expected: FAIL(`CopilotAdapter` 不存在或旧 Codex 测试已删导致 import 错)。

- [ ] **Step 3: 实现 CopilotAdapter**

懒加载 `from copilot import CopilotClient`,测试不 start 真 CLI。默认 factory 构造参数以 Task 1 为准,大致:

```python
def _default_client_factory(home: Path, env: dict[str, str]):
    from copilot import CopilotClient
    return CopilotClient(use_logged_in_user=False, base_directory=str(home), env=env)
```

若构造器吃 dict 而不是 kwargs,按安装包写。`_ensure_client` 只建一次; `close()` 调 `stop()` / `__aexit__`。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_codex_runtime.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```powershell
git add backend/copilot_runtime.py tests/test_codex_runtime.py
git commit --only backend/copilot_runtime.py tests/test_codex_runtime.py -m "feat: CopilotAdapter——会话执行、恢复、超时中断与进度"
```

---

### Task 5: 测试连接改为 Copilot ephemeral probe

**Files:**
- Modify: `backend/copilot_runtime.py`(`probe_agent`)
- Modify: `backend/main.py`
- Modify: `tests/test_admin_automation.py`
- Modify: `tests/test_codex_runtime.py`

**Interfaces:**
- `async def probe_agent(settings, sessions, agent, client_factory=None) -> str`
- 失败抛 `ValueError(describe_copilot_error(...))`
- 成功文案:`已验证模型接口、凭据与模型({model})`(不要写 Codex,可写 Chat Completions)

- [ ] **Step 1: 写失败测试**

`test_probe_agent_ephemeral_roundtrip` 改为 FakeCopilot:`create_session` 的 provider `wire_api=="completions"`,prompt 含 `OK`。factory 抛 `connection refused` → `ValueError` 匹配 `连不上`。

`test_admin_automation.py`:

```python
async def fake_probe(settings_, sessions_, runtime_, client_factory=None):
    return f"已验证模型接口、凭据与模型（{runtime_.model}）"
monkeypatch.setattr("backend.copilot_runtime.probe_agent", fake_probe)
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_codex_runtime.py tests/test_admin_automation.py -q`
Expected: FAIL(仍 import `codex_runtime.probe_agent` 或文案不对)。

- [ ] **Step 3: 实现**

`probe_agent` 开独立 client(不要用 Worker 单例),`working_directory` 用 `DATA_ROOT/copilot-home-probe`,session 权限用 `read_only`,prompt `"Reply with the single word OK."`,超时 `min(max(agent.timeout_seconds, 15), 60)`。`main.py` 的 test 端点改 `from backend import copilot_runtime`。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_codex_runtime.py tests/test_admin_automation.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```powershell
git add backend/copilot_runtime.py backend/main.py tests/test_admin_automation.py tests/test_codex_runtime.py
git commit --only backend/copilot_runtime.py backend/main.py tests/test_admin_automation.py tests/test_codex_runtime.py -m "feat: 测试连接改为 Copilot Chat Completions 探测"
```

---

### Task 6: Worker 切换到 CopilotAdapter 并删除 Codex 运行时

**Files:**
- Modify: `backend/worker.py`
- Modify: `backend/agent_tasks.py`(`TASK_SYSTEM_RULES` 若仍写死 `apply_patch`,改为「使用文件写入工具」)
- Modify: `tests/test_worker.py`
- Modify: `tests/test_agent_tasks.py`
- Modify: `tests/test_recovery.py`(若仍 import CodexAdapter)
- Delete: `backend/codex_runtime.py`
- Rename: `tests/test_codex_runtime.py` → `tests/test_copilot_runtime.py`

**Interfaces:**
- `from backend.copilot_runtime import CopilotAdapter, SYSTEM_RULES, parse_agent_json`
- `self.agent = agent or CopilotAdapter(...)`
- `isinstance(self.agent, CopilotAdapter)` 两处(analyze / agent_task)
- 全仓库不得再 `import backend.codex_runtime` 或 `openai_codex`

- [ ] **Step 1: 改测试导入**

`tests/test_worker.py`、`tests/test_agent_tasks.py` 里 `class Rec(CodexAdapter)` 改为 `CopilotAdapter`。`test_agent_tasks.py` 的 `SYSTEM_RULES` 改从 `copilot_runtime` 导入。

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_worker.py tests/test_agent_tasks.py tests/test_recovery.py -q`
Expected: FAIL(Worker 仍引用 CodexAdapter)。

- [ ] **Step 3: 改 Worker、删 Codex 模块、改名测试文件**

`backend/worker.py` 顶部与两处 `isinstance`、默认 adapter 全部换成 Copilot。删除 `backend/codex_runtime.py`。`git mv tests/test_codex_runtime.py tests/test_copilot_runtime.py`。

```powershell
rg -n "codex_runtime|openai_codex|CodexAdapter|openai-codex" backend tests
```

Expected: 无输出(文档目录除外)。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_worker.py tests/test_agent_tasks.py tests/test_copilot_runtime.py tests/test_recovery.py tests/test_admin_automation.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```powershell
git add backend/worker.py backend/agent_tasks.py backend/codex_runtime.py tests/test_worker.py tests/test_agent_tasks.py tests/test_codex_runtime.py tests/test_copilot_runtime.py tests/test_recovery.py
git commit --only backend/worker.py backend/agent_tasks.py backend/codex_runtime.py tests/test_worker.py tests/test_agent_tasks.py tests/test_copilot_runtime.py tests/test_recovery.py -m "feat: Worker 改用 CopilotAdapter 并移除 Codex 运行时"
```

(若 `test_recovery.py` 未改则从 pathspec 去掉;删除文件必须进这次提交。)

---

### Task 7: 前端文案——Codex → Agent,Responses → Chat Completions

**Files:**
- Modify: `frontend/src/App.tsx`

原则:用户可见「Codex」改为「Agent」;集成管理可说明「本地 Copilot CLI 运行时调用你配置的 Chat Completions 接口,不需要 Copilot 订阅」。不要把看板统计写成「Copilot 审查中」。

必改点(用搜索核对,勿漏):

| 现文案 | 改为 |
|---|---|
| `STAGES.review`「Codex 审查」 | 「代码审查」或「Agent 审查」 |
| `STAGES.agent_task` / `JOB_KIND.agent_task`「Codex 任务」 | 「Agent 任务」 |
| 统计「Codex 审查中」 | 「审查中」 |
| 迷你流程「Codex 审查」 | 「代码审查」 |
| Tab「Codex 结果」、空态「Codex 结构化结果」 | 「Agent 结果」 |
| 工作流页步骤里的 Codex | Agent / 本地运行时 |
| 服务卡片 `name: 'Codex'` | `name: 'Agent'`,detail 写 Copilot CLI + Chat Completions |
| 集成管理标题「Codex 模型接口」 | 「模型接口」;帮助去掉 Responses/LiteLLM,改为 Chat Completions 可直连 |
| 推理力度 `minimal` option | 删掉,增加 `max` |
| 接入详情 `tiny-label: CODEX` / 「不使用 Codex」/「保存 Codex 配置」 | AGENT / 「不使用 Agent」/「保存 Agent 配置」 |
| 「不会注入 Codex」 | 「不会注入 Agent 运行时」 |

- [ ] **Step 1: 改 App.tsx**

- [ ] **Step 2: 确认无残留用户可见 Codex/Responses/LiteLLM**

```powershell
rg -n "Codex|/v1/responses|LiteLLM|minimal" frontend/src/App.tsx
```

Expected: 无用户可见残留(代码注释也不要留过时协议说明)。

- [ ] **Step 3: 前端构建**

```powershell
pnpm --dir frontend build
```

Expected: 成功。

- [ ] **Step 4: Commit**

```powershell
git add frontend/src/App.tsx
git commit --only frontend/src/App.tsx -m "feat: 前端将 Codex/Responses 文案改为 Agent 与 Chat Completions"
```

---

### Task 8: README 与 .env.example

**Files:**
- Modify: `README.md`
- Modify: `.env.example`

- [ ] **Step 1: README**

- 「已实现功能」Codex 段改为:本地 Copilot CLI 运行时 + Web 配置 Chat Completions 接口;API Key 加密且只进 session;权限闸代替 OS 沙箱;结构化输出事后校验;崩溃按 `session_id` 恢复;旧 HTTP/`thread_id` 用 `--new-analysis`。
- 部署步骤 7:「配置模型接口」而非「Codex 模型接口」。
- 配置指南第 1 节:Base URL 必须提供 `/chat/completions`;DeepSeek 等可直连;删除整段 LiteLLM yaml 示例。补充本地/镜像需 `python -m copilot download-runtime`(镜像已预拉)。注明遗留 `DATA_ROOT/codex-home/` 可手工删。
- 失败处理:「Codex thread」改为 Copilot session;`thread_id` 与 `messages` 均不兼容。

- [ ] **Step 2: .env.example**

```text
# Model providers (Chat Completions base URL + API key + model) are configured
# via the Web Admin UI and encrypted with SECRETS_ENCRYPTION_KEY.
# The worker talks to them through the local Copilot CLI runtime (no Copilot subscription).
```

- [ ] **Step 3: Commit**

```powershell
git add README.md .env.example
git commit --only README.md .env.example -m "docs: Agent 接入改为 Copilot CLI 与 Chat Completions"
```

---

### Task 9: 全量回归

- [ ] **Step 1: 代码残留检查**

```powershell
rg -n "openai_codex|codex_runtime|CodexAdapter|openai-codex|/v1/responses|wire_api = \"responses\"" backend tests frontend requirements.txt requirements.lock Dockerfile
```

Expected: 无命中。`DATA_ROOT/codex-home` 只允许出现在 README 的手工清理说明。

- [ ] **Step 2: 测试与前端**

```powershell
.venv\Scripts\python -m pytest --basetemp=./.pytest_temp -p no:cacheprovider -q
pnpm --dir frontend build
```

Expected: 全绿。

- [ ] **Step 3: 若 Step 2 有失败,修在本 Task 并补测,不要静默删断言**

- [ ] **Step 4: 若还有未提交的本计划改动,按文件含义分 commit,仍然 `--only`**

---

## 实施时注意

- Fake Copilot 必须捕获 `create_session` 的 `provider`,以便断言 `wire_api` 与 api_key 在内存里、不在 `copilot-home` 文件中。
- 不要调用 `PermissionHandler.approve_all`。
- 不要给 Copilot CLI 传入 Worker 进程的完整 `os.environ`。
- `tests/test_admin.py` / `test_prepare_release.py` 里 checkpoint 示例仍可用 `thread_id` 字段测 `reusable_agent_checkpoint`(该函数不解析字段名);不必为改名而动,除非新代码开始依赖 `session_id` 键。
- 不实现双运行时、不自动改写已存 Base URL、不删用户磁盘上的 `codex-home`。
