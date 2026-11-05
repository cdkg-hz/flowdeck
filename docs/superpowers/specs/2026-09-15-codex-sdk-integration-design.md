# Codex SDK 集成设计:替换 HTTP Agent 调用

日期:2026-09-15
状态:已与需求方确认

## 背景与目标

Flowdeck 当前通过 HTTP(OpenAI Chat Completions 协议)调用外部 Agent 服务完成三类任务:

1. **PR 代码审查**(`worker.review`,结构化结果 `ReviewResult`);
2. **Release 更新说明分析**(`worker.release` 中的 notes 步骤,结构化结果 `NotesResult`);
3. **事件规则 Agent 任务**(`worker.run_agent_task`,结构化结果 `AgentTaskResult`,可配置 GitHub 写权限)。

三者共用 `backend/agents.py` 的 `OpenAIAdapter`:httpx 请求循环 + 自定义只读工具
(`list_files`/`read_file`/`search_code`/`read_diff`)+ 写场景工具(`ActionTools`),
会话消息保存在任务 checkpoint 中用于崩溃恢复。

本设计将三类任务**全部**迁移到官方 Codex Python SDK(`openai-codex`),
彻底移除 HTTP 调用方式,并保留 Web 端配置第三方模型接口的能力。

## 已确认的决策

| 决策点 | 结论 |
|---|---|
| 迁移范围 | 三类任务全部迁移,移除 HTTP OpenAIAdapter |
| 实现载体 | 官方 Python SDK `openai-codex`(方案 A),不引入 Node sidecar,不裸调 CLI |
| 第三方模型协议 | 只接受 Responses API 兼容地址;Chat Completions-only 服务(如 DeepSeek 直连)由用户自行部署 LiteLLM 等转换网关,文档给出示例 |
| 认证方式 | 仅 API Key(Web 端配置、Fernet 加密存储);不支持 ChatGPT 订阅登录 |
| 写权限任务 | Codex 只在本地沙箱改文件;合并 PR、推送文档由 Flowdeck 在任务完成后确定性执行,Agent 不接触 PAT |

### 关键外部约束(2026-09 官方文档核实)

- Codex `model_providers.<id>.wire_api` 唯一支持值为 `responses`
  (Chat Completions 支持已于 2026 年初移除)。
- Python SDK(beta)通过本地 app-server(JSON-RPC)控制 Codex 运行时,
  pip 包 `openai-codex` 自带锁定版本的 CLI 运行时依赖 `openai-codex-cli-bin`。
- SDK 支持 `AsyncCodex`、`thread_start(model=…, model_provider=…, sandbox=…, cwd=…)`、
  `output_schema` 结构化输出、thread resume、`Sandbox.read_only` / `Sandbox.workspace_write`。
- `model_providers` 属于用户级配置(`CODEX_HOME/config.toml`),项目级 `.codex/config.toml`
  无法覆盖;API Key 通过 `env_key` 指定的环境变量传入。

## 第 1 节 · 总体架构

移除 `backend/agents.py` 中的 `OpenAIAdapter`、`READ_TOOLS`/`WRITE_TOOLS` 工具定义、
HTTP probe 逻辑与 `WorkspaceTools`;新建 `backend/codex_runtime.py` 封装 SDK:

- Worker 进程持有**单例 `AsyncCodex`**(本地 app-server 子进程),生命周期与 Worker 一致;
  每个分析任务开一个 thread。
- **第三方模型接入机制**:Worker 在 `DATA_ROOT/codex-home/` 维护自动生成的 `config.toml`,
  每个启用的 Agent 连接渲染为一个 `[model_providers.agent_<id>]` 条目:
  `base_url`、`env_key = "FLOWDECK_AGENT_<ID>_KEY"`、`wire_api = "responses"`、
  `requires_openai_auth = false`。API Key 从数据库解密后**只通过 app-server 进程环境变量注入,
  不写入任何文件**。Agent 连接增删改时重新生成 config.toml 并重启 app-server 子进程。
- 任务执行:`thread_start(model_provider="agent_<id>", model=连接配置的模型名,
  sandbox=按任务类型, cwd=任务工作区)`;推理力度经 `effort` 参数传入。
- 依赖与部署:`requirements.txt` 增加锁定版本的 `openai-codex`;Docker 镜像经 pip
  自动获得 CLI 运行时,Dockerfile 无需额外步骤。`CODEX_HOME` 指向 DATA_ROOT 持久卷,
  保证会话数据跨重启保留。

## 第 2 节 · Web 端第三方模型配置(集成管理)

`AgentConnection`(数据库)与 `AgentConnectionInput`(接口)字段调整:

| 字段 | 变化 |
|---|---|
| `base_url` | 保留;语义为「Responses API 兼容地址」(必须提供 `/v1/responses`) |
| `api_key` | 保留;加密存储,必填 |
| `model` | **改为必填**(Codex 必须指定模型名) |
| `reasoning_effort` | **新增**;可选值 `minimal/low/medium/high/xhigh`,空字符串表示用模型默认 |
| `protocol` | 废弃;数据库列保留不删,保存时写入固定值 `codex` |
| `timeout_seconds` | 保留;作为单个 turn 的超时上限 |

数据库迁移:按项目现有 `_add_column` 启动迁移模式增加
`agent_connections.reasoning_effort VARCHAR(16) NOT NULL DEFAULT ''`。

前端 `App.tsx` 集成管理表单:

- 去掉协议下拉;模型名改为必填;新增推理力度下拉(含「模型默认」空选项)。
- 文案更新:调用地址必须为 Responses API 兼容;DeepSeek 等 Chat-only 服务
  需自行部署 LiteLLM 转换网关(README 附配置示例)。

**测试连接**(`POST /api/admin/agents/{id}/test`):替换 `probe_agent` 的 HTTP 探测,
改为用该连接启动一个 ephemeral thread 执行极小 prompt(如 "Reply with OK"),
验证 base_url、API Key、模型名全链路。失败时返回中文诊断:连接被拒/超时/
鉴权失败/模型不存在,并延续现有的密钥脱敏行为。

## 第 3 节 · PR 审查与 Release 摘要(只读任务)

工作区准备逻辑完全不变(`code/`、`base/`、`changes.diff`、`commits.txt`)。执行层变化:

- `thread_start(sandbox=Sandbox.read_only, cwd=任务目录)`。Codex 用自带的文件读取/
  搜索能力替代原自定义只读工具;`WorkspaceTools`、`CombinedTools` 删除。
- 系统指令沿用现有 `SYSTEM_RULES` 精神改写(只读、防 prompt 注入、不泄密、
  无法读取材料时明确返回 inconclusive),经 `base_instructions` 传入;
  任务 payload JSON 结构不变,作为 turn 输入,并指示 Agent 先读 `changes.diff`
  再读 `code/`、`base/` 中的相关实现。
- **结构化输出**:turn 传 `output_schema=ReviewResult.model_json_schema()`
  (Release 摘要用 `NotesResult`),不再靠 prompt 约定裸 JSON。
- **全部事后校验保留**:`parse_agent_json` 严格解析、`head_sha` 一致性、
  findings 文件与行号实体核验、Release evidence 追溯、结果脱敏(`redact_nested`)。
- 超时:`asyncio.wait_for(turn, timeout_seconds)`;超时后 interrupt 该 turn,
  按现有未知结果语义处理(不自动当成功,不越过审批)。

安全边界说明:原「只能读四类路径」的硬限制改由 **read_only 沙箱 + cwd 隔离**保证。
沙箱保证只读;Agent 可读任务目录内全部文件,而任务目录本来只含这四类内容,
实际暴露面一致。

## 第 4 节 · 事件规则任务(写场景采用 flowdeck_push 语义)

只读规则与第 3 节相同。勾选写权限(原 `include_github_credentials`)的规则:

- Flowdeck 照旧准备 `work/` worktree;`thread_start(sandbox=Sandbox.workspace_write,
  cwd=work/)`。**不注入 PAT,沙箱不开网络**。
- 删除:`ActionTools`、`WRITE_TOOLS`、payload 中的 `github_credentials` 注入、
  `extra_agent_body`。系统指令中关于「持 PAT clone/push」的段落全部移除,
  改为「你只能在本地工作区修改文件,提交与推送由系统执行」。
- `AgentTaskResult` schema 语义从「声称的事实」改为「请求的动作」:
  - `pull_request.merged: bool` → `pull_request.merge_requested: bool`;
  - `release_docs.generated/pushed: bool` → `release_docs.paths`(work/ 内相对路径列表)
    + `push_requested: bool`。
- Agent 返回后由 Flowdeck 确定性执行:
  - **文档**:校验 paths 均存在于 work/ 内(拒绝越界路径)→ Flowdeck 自己
    commit + push 到 `allowed_push_branch`(现有分支限制逻辑;PAT 仅在 Flowdeck 侧)
    → 记录真实 commit SHA。
  - **合并**:走现有 GitHub API 合并路径,使用项目配置的 `merge_method` 与精确
    head SHA 前置校验(与原 `ActionTools.merge_pr` 行为一致)→ 记录真实 merged 结果。
- `verify_agent_task` 的职责从「到 GitHub 核实 Agent 声称的事实」改为
  「执行请求的动作并记录真实结果」;PR 评论、管理人通知、去重 Issue 逻辑不变。
- 执行意图(intent)先落库再执行的既有模式沿用:push/merge 前记录意图,
  崩溃恢复时先核对外部状态。

## 第 5 节 · 崩溃恢复与旧任务迁移

- checkpoint 从「消息数组重放」改为保存 **`thread_id`**(及本次 `analysis_generation`)。
  Worker 重启后 `thread_resume(thread_id)`:turn 未完成则以相同输入重新发起
  (输入幂等);会话数据在 `CODEX_HOME`(DATA_ROOT 持久卷)。
- `analysis_generation` 与 `retry JOB_ID --new-analysis` 机制保留:新分析 = 新 thread。
- 旧格式 checkpoint(含 `messages` 的未完成任务)在新版本恢复时明确报错,
  提示使用 `retry JOB_ID --new-analysis`;不做旧会话兼容。
- 未知结果语义不变:turn 结果不可得时不自动当成功、不越过审批;
  Agent 任务失败不影响同任务内其他已完成步骤的状态。
- 输出大小限制与 `scrub_secrets` 秘密扫除保留,作用于 thread 事件文本与最终输出。

## 第 6 节 · 测试与文档

- Worker 的 `self.agent` 注入缝保留:新 `CodexAdapter` 提供与原 `execute(...)`
  相同形状的接口;现有测试假 adapter 继续可用;
  `isinstance(self.agent, OpenAIAdapter)` 分支改为 `isinstance(self.agent, CodexAdapter)`。
- 新增单测:config.toml 生成(provider 命名、env_key 映射、TOML 转义)、
  结构化结果解析、超时/中断路径、写场景 paths 越界拒绝、
  flowdeck 侧 push/merge 执行与意图记录。
- 现有 worker 测试改用假 adapter,CI 不需要真实 Codex 运行时与网络。
- 文档更新:README「Agent 接入池」章节与 Web 配置指南第 1 节(字段、Responses API
  要求、LiteLLM 网关示例)、`.env.example` 注释、失败处理章节中与 Agent
  会话恢复相关的表述。

## 不做的事(YAGNI)

- 不支持 ChatGPT 订阅登录。
- 不内置 LiteLLM 转换代理服务。
- 不做旧 HTTP 协议与 Codex 并存的双协议选择。
- 不做 Codex 事件到看板的细粒度流式展示(保持现有 step 级时间线)。
- 不迁移历史未完成任务的旧会话状态。
