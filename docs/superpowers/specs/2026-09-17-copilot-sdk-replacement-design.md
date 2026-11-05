# Copilot SDK 替换设计:移除 Codex 运行时

日期:2026-09-17
状态:已与需求方确认

## 背景与目标

Flowdeck 当前用官方 Codex Python SDK(`openai-codex`)跑三类 Agent 任务:

1. **PR 代码审查**(`worker.review`,结构化结果 `ReviewResult`);
2. **Release 更新说明分析**(`worker.release` 中的 notes 步骤,结构化结果 `NotesResult`);
3. **事件规则 Agent 任务**(`worker.run_agent_task`,结构化结果 `AgentTaskResult`,可配置写权限请求)。

Worker 持有单例 `CodexAdapter`:`CODEX_HOME/config.toml` 把每条 Agent 连接渲染成 Responses API 的 `model_providers`,任务开 Codex thread,只读用 `Sandbox.read_only`,写任务用 `Sandbox.workspace_write` 且不联网。结构化输出走 SDK `output_schema`,崩溃恢复保存 `thread_id`。

本设计将三类任务**全部**迁到官方 GitHub Copilot Python SDK(`github-copilot-sdk`),**删除 Codex 运行时**,第三方模型改为 Chat Completions。合并 PR、推送文档仍由 Flowdeck 在任务返回后确定性执行。

## 已确认的决策

| 决策点 | 结论 |
|---|---|
| 与 Codex 的关系 | **只保留 Copilot**,不双运行时并存,不按连接选 protocol |
| 实现载体 | 官方 Python SDK `github-copilot-sdk`,不引入 Node sidecar,不裸调 Copilot CLI |
| 认证 | **仅第三方 BYOK**:Web 端配置 Base URL + API Key + 模型名;不支持 Copilot 订阅、GitHub Token、`gh` 登录、ChatGPT 登录 |
| 模型接口协议 | **只接受 Chat Completions**(`wire_api = "completions"`)。不再要求 `/v1/responses`,不再建议为 Chat-only 服务部署 LiteLLM |
| 写权限任务 | Copilot 只在本地工作区改文件;合并/推送由 Flowdeck 执行,Agent 不接触 PAT |
| 旧会话 | Codex `thread_id`、旧 HTTP `messages` 均不兼容;恢复时明确报错,要求 `retry JOB_ID --new-analysis` |

### 关键外部约束(2026-09 官方文档核实)

- 包名 `github-copilot-sdk`,导入 `from copilot import CopilotClient`。Python 3.11+;本项目继续 Python 3.12。
- SDK 通过本地 Copilot CLI 运行时做 JSON-RPC。pip 安装后需 `python -m copilot download-runtime`(或首次使用时自动下载)。可用 `COPILOT_CLI_PATH` / `COPILOT_CLI_EXTRACT_DIR` / `COPILOT_SKIP_CLI_DOWNLOAD` 控制。
- BYOK:`create_session(model=…, provider={type, base_url, api_key, wire_api})`。`type="openai"` 时 `base_url` 形如 `https://api.openai.com/v1`;`wire_api="completions"` 调用 `/chat/completions`(默认值)。
- 会话:`create_session` / `resume_session` / `send_and_wait`;`working_directory`、`system_message`、`reasoning_effort`(`low` / `medium` / `high` / `xhigh` / `max`)。
- **没有**与 Codex `output_schema` 对等的结构化输出 API。
- **没有**与 Codex `Sandbox.read_only` 对等的 OS 沙箱。权限靠 `on_permission_request`;`PermissionHandler.approve_all` 会放开 shell,**禁止使用**。
- CLI 会自动探测 `COPILOT_GITHUB_TOKEN` / `GH_TOKEN` / `GITHUB_TOKEN` 以及本机 `gh` 登录。Worker 进程里常有 GitHub PAT,必须关掉 logged-in-user,并用白名单环境变量拉起 CLI,避免 Copilot 误用 Flowdeck 的 GitHub 凭据。

## 第 1 节 · 总体架构

删除 `backend/codex_runtime.py` 与 `openai-codex` 依赖。新建 `backend/copilot_runtime.py`:

- Worker 进程持有**单例 `CopilotClient`**,生命周期与 Worker 一致;`close()` 时 `stop()`。
- 客户端固定:`use_logged_in_user=False`,不传 `github_token`,`base_directory = DATA_ROOT/copilot-home`(即 `COPILOT_HOME`)。会话状态落在数据卷,跨重启可 resume。
- CLI 子进程环境只保留安全白名单(`PATH`/`HOME`/`USERPROFILE`/`SYSTEMROOT` 等,沿用 Codex 那套 `_BASE_ENV_KEYS` 思路)。**显式剔除** `GITHUB_TOKEN`、`GH_TOKEN`、`COPILOT_GITHUB_TOKEN`、`GITHUB_COPILOT_API_TOKEN` 以及数据库解出的项目 PAT。API Key **只经该次 session 的 `provider.api_key` 传入内存,不写文件、不进 `COPILOT_HOME` 配置**。
- **不再**生成 `config.toml`,也不因 Agent 增删改而重启客户端。provider 是按会话传的。
- 每个分析任务:`create_session(model=连接模型名, provider={type:"openai", base_url, api_key, wire_api:"completions"}, working_directory=任务目录, reasoning_effort=…, system_message=任务指令, on_permission_request=按 sandbox 绑定的处理器)`。随后 `send_and_wait(input_text)`。
- 依赖与部署:`requirements.txt` 用 `github-copilot-sdk==<锁定版本>` 替换 `openai-codex`。Docker 镜像在 `pip install` 之后、切到 `flowdeck` 用户可读取的缓存目录上执行 `python -m copilot download-runtime`;运行时设 `COPILOT_SKIP_CLI_DOWNLOAD=1`,禁止生产环境临时拉二进制。本地 README 补充同样的 download 步骤。

`CopilotAdapter.execute(...)` 保持与现 `CodexAdapter` 相同的调用形状(含 `agent` / `workspace` / `sandbox` / `secret_values` / `on_progress`)。`output_schema` 形参可保留但 **不传给 SDK**;结构化约束继续写在 `instructions` 里的 JSON Schema 段(Worker 现有拼法不变)。

## 第 2 节 · Web 端第三方模型配置(集成管理)

`AgentConnection` 列不新增。语义变化:

| 字段 | 变化 |
|---|---|
| `base_url` | 保留;语义改为「OpenAI 兼容 Chat Completions 根地址」(通常以 `/v1` 结尾,运行时请求 `/chat/completions`) |
| `api_key` | 保留;加密存储,必填 |
| `model` | 仍必填 |
| `reasoning_effort` | 可选值改为 Copilot 支持的 `low/medium/high/xhigh/max`;去掉 `minimal`;空字符串表示模型默认。库里存量 `minimal` 在 `load_agent` 时映射为 `low`,无需数据迁移 UI |
| `protocol` | 数据库列保留;保存时写入固定值 `copilot` |
| `timeout_seconds` | 保留;单次 `send_and_wait` 超时上限 |

`AgentConnectionInput.valid_effort` 与前端下拉同步。`save_agent` 把 `"codex"` 改为 `"copilot"`。`GET /api/integrations` 的 `"protocol"` 改为 `"copilot"`。错误文案里「尚未选择 Codex 模型接口」改为「尚未选择可用的模型接口」。

前端 `App.tsx`:

- 集成管理标题与帮助改为 **模型接口 + 本地 Copilot CLI 运行时**;明确 **不要 Copilot 订阅**,地址必须提供 Chat Completions,DeepSeek 等可直连。
- 去掉 Responses / LiteLLM 必经网关的说明。
- 看板、工作流页、项目接入里用户可见的「Codex」改为「Agent」(审查中、Agent 任务、Agent 结果)。避免「Copilot」出现在需要订阅的语境。
- 推理力度:minimal → max。

**测试连接**(`POST /api/admin/agents/{id}/test`):`probe_agent` 改为用该连接开 ephemeral Copilot session,prompt 仍为 "Reply with the single word OK."。失败返回中文诊断:连接被拒/超时/鉴权失败/找不到 Chat Completions 端点或模型;密钥脱敏不变。

**已有连接迁移**:URL 形态(`https://api.openai.com/v1`)多数仍可用,因为同时有 `/chat/completions`。仅暴露 Responses、没有 Chat Completions 的 LiteLLM/网关会在「测试连接」失败,需把地址改到 Chat Completions 根。升级说明写进 README。

## 第 3 节 · PR 审查与 Release 摘要(只读任务)

工作区准备不变(`code/`、`base/`、`changes.diff`、`commits.txt`)。执行层:

- `working_directory=任务目录`。系统指令仍用现有 `SYSTEM_RULES`(只读、禁 shell/git、防注入、读不到材料则 inconclusive、最终必须是裸 JSON)。
- Copilot 用自带文件工具读工作区;自定义 `WorkspaceTools` 保持不存在。
- **结构化输出**:不使用 SDK schema;最终文本经现有 `parse_agent_json` 严格解析。解析失败不得通过审查/发布。
- 其余事后校验全部保留:`head_sha`、findings 路径与行号、Release evidence、`redact_nested`。
- 超时:`asyncio.wait_for(send_and_wait, timeout_seconds)`;超时后尽最大努力 abort/disconnect 该 session,按 `OutcomeUnknown` 处理(不自动当成功,不越过审批)。
- 进度:订阅 session 事件,把读文件/写文件(只读任务不应出现写)总结成短中文,走现有 `on_progress` → `agent_activity`(step 级时间线,不做 token 流式上板)。

安全边界:Copilot 没有 OS 只读沙箱。只读任务的硬限制改为 **权限处理器 + cwd + 指令**:

- 拒绝一切 shell / 终端 / git / 网络 / MCP / 自定义工具。
- 拒绝工作区外路径。
- 拒绝写文件、打补丁(只读任务)。
- 仅允许对 `working_directory` 内的读/搜索/列目录。
- 实现时按 SDK 的 `PermissionRequest*` 变体与 `on_pre_tool_use` 双闸;若 SDK 提供 `excluded_tools`,把 shell/git/MCP 一并排除。测试用假请求覆盖「shell 被拒、越界写被拒、工作区内读被放行」。

## 第 4 节 · 事件规则任务(写场景仍是 flowdeck_push)

只读规则与第 3 节相同。勾选写权限的规则:

- Flowdeck 照旧准备 `work/` worktree;`working_directory=work/`。**不注入 PAT,不给 CLI 网络环境变量,权限处理器拒绝 shell 与网络。**
- 写任务额外允许工作区内写文件/打补丁;越界路径仍拒绝。
- `AgentTaskResult` 语义不变(请求动作,不是声称事实)。Flowdeck 侧 path 校验、commit+push、merge API、intent 先落库,全部保持。
- `TASK_SYSTEM_RULES` 继续禁止终端;可按 Copilot 实际文件工具名微调措辞(若 CLI 不用 `apply_patch` 这个名字,改成「使用文件写入工具」),语义不变。

## 第 5 节 · 崩溃恢复与旧任务迁移

- checkpoint 保存 `{"task_id", "agent_id", "session_id", "output"?}`。有 `session_id` 且无 `output` 时 `resume_session(session_id)` 再发**相同** `input_text`(输入幂等)。会话目录在 `DATA_ROOT/copilot-home`。
- 出现以下任一情况 → `OutcomeUnknown`,提示 `retry JOB_ID --new-analysis`,不做翻译:
  - `messages`(旧 HTTP)
  - `thread_id`(旧 Codex)
  - `agent_id` 与当前连接不一致
- `analysis_generation` 与 `--new-analysis` 保留:新分析 = 新 session。
- 未知结果语义不变。`scrub_secrets` 作用于事件文本与最终输出。输出大小上限 2MiB 保留。
- 磁盘上遗留的 `DATA_ROOT/codex-home/` **不在本变更中自动删除**;文档注明可手工清理。

## 第 6 节 · 测试与文档

- `self.agent` 注入缝保留。`CopilotAdapter` 提供同样的 `execute(...)`;测试假 adapter 继续走窄协议(四/六个位置参数)。`isinstance(..., CodexAdapter)` 改为 `CopilotAdapter`。
- `tests/test_codex_runtime.py` 重写为 `tests/test_copilot_runtime.py`:假 `CopilotClient`/`session`,覆盖 provider 形状(`wire_api=completions`、不写密钥文件)、session 创建/恢复、旧 `thread_id` 拒绝、超时中断、权限拒绝 shell、探测文案含 Chat Completions。
- CI 不下载真实 Copilot runtime、不访问网络。
- 删除 `openai-codex` / `openai-codex-cli-bin`;更新 `requirements.lock`。
- README、`.env.example` 注释、前端文案:去掉 Responses 与 LiteLLM 专章;写清 Chat Completions 根地址、download-runtime、旧 Codex 任务需 `--new-analysis`。

## 不做的事(YAGNI)

- 不支持 GitHub Copilot 订阅、OAuth、`github_token`、server-to-server Copilot 计费。
- 不支持 Responses、Azure 原生 `type=azure`、Anthropic `type=anthropic`(需要时可另开设计)。
- 不内置 LiteLLM,也不再把 LiteLLM 写成接入前提。
- 不保留 Codex / HTTP 双协议选择,不实现按连接切换运行时。
- 不做 Copilot 事件到看板的 token 级流式展示。
- 不迁移 Codex thread 或 HTTP messages。
- 不在升级时自动改写已保存的 Base URL。
