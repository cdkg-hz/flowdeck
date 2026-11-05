# Flowdeck · 研发工作流与发布管理

Flowdeck 是一个可自行部署的 GitHub 研发工作流看板，适合希望集中管理代码审查、任务与发布记录的个人和小团队。

由本程序接收 GitHub Webhook、维护项目代码和任务，按需调用配置的 Agent 接口进行代码审查与发布日志分析。网页端实时展示 Issue、PR、代码审查结果、GitHub Release 及每一步的执行记录。接入仓库如需镜像构建与后端部署，应自行配置相应 GitHub Actions；Flowdeck 负责分析与记录。

[本地体验](#本地启动) · [服务器部署](#在同一台-nas--linux-服务器部署) · [架构说明](docs/ARCHITECTURE.md) · [参与贡献](CONTRIBUTING.md) · [问题反馈](SUPPORT.md) · [更新记录](CHANGELOG.md)

当前代码版本为 `0.1.0`，处于早期阶段，配置和数据结构可能变化。生产升级前请备份数据与加密主密钥。部署需要自行准备 GitHub 仓库权限及模型服务；模型调用可能产生费用。

## 已实现功能

- **React 中文看板**：项目筛选、事项搜索、状态筛选、审查问题详情、发布步骤与事件时间线、手动执行发布按钮等。
- **管理员网页创建与管理项目**：自选服务器代码目录、事件订阅、独立 Webhook URL/Secret、独立 GitHub API Token、目录与授权验证、自动激活。`REQUIRE_LOGIN=true` 时未登录不能访问任何界面与看板接口；GitHub Webhook 仍可匿名投递。
- **FastAPI + 持久数据库**：Webhook 验签、仓库白名单、投递去重、后台任务、租约恢复、SSE 实时更新。
- **GitHub 深度集成**：Issue/PR/Release 与检查事件、API 补漏、Assignees、紧急/普通/较低优先级标签、明确的 Issue/PR 关联、审查评论、状态回写、满足条件后的自动合并。
- **Agent 运行时（Web 端配置）**：
  - 内嵌官方 GitHub Copilot Python SDK：Worker 通过本地 Copilot CLI 运行时执行代码审查、发布分析与事件任务。不需要 Copilot 订阅或 `gh` 登录。只读任务仅允许读取工作区文件；写任务允许工作区内写文件，仍拒绝 shell 与网络。权限由自定义权限闸控制。
  - **第三方模型接口**：网页端「集成管理」配置模型接口名称、调用地址 (Base URL)、API Key、模型名与推理力度；地址必须兼容 OpenAI Chat Completions（提供 `/chat/completions`）。DeepSeek 等 Chat Completions 服务可直连。API Key 加密存储，运行时只经本次 session 内存传入，不写入 `COPILOT_HOME`。
  - 结构化输出经 prompt 中的 JSON Schema 约束并事后校验；任务崩溃后按 Copilot `session_id` 恢复。升级前遗留的 HTTP `messages` 或 Codex `thread_id` 不再兼容（用 `retry JOB_ID --new-analysis` 重新分析）。
  - Agent 不持有 GitHub 凭据：合并 PR、推送文档由 Flowdeck 在任务返回结构化「动作请求」后校验并执行。
- **版本发布流与手动触发**：
  - 启用 Release 分析后，Worker 对比上次正式发布生成用户说明与研发记录，并写回 GitHub Release。
  - 创建项目时可把镜像仓库用户名/密码、Android 签名密钥写入 GitHub Actions Secrets，供仓库自己的 Release workflow 使用；Flowdeck Worker 不构建镜像、不部署后端。
  - Release 详情面板提供“执行发布”按钮，支持在权限范围内手动触发或重试更新记录。
- **代码工作区安全**：服务端 Git 缓存；每个任务导出固定 SHA 的 `code/`、`base/`、差异与提交记录，不携带 Git 凭据或依赖 Agent 记忆。
- **凭据加密**：GitHub Token、Agent API Key 均使用主密钥（Fernet）在数据库中加密保存，前端和日志中自动脱敏。

## 本地启动

需要 Python 3.12+、Node.js 24（与 Docker / CI 一致）、pnpm 11.19.0、Git。先下载或克隆本仓库，在项目根目录执行。以下使用 PowerShell；Linux / macOS 的对应命令见[贡献指南](CONTRIBUTING.md)。

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt -c requirements.lock
npm install --global pnpm@11.19.0
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend build
.venv\Scripts\python -m uvicorn backend.main:app --host 127.0.0.1 --port 8765
```

打开 `http://127.0.0.1:8765`。首次体验无需创建 `.env`、填写 Token 或启动 Worker，应用默认使用演示数据，SQLite 位于 `data/flowdeck.db`。只有运行真实 Agent 任务时才需先执行 `.venv\Scripts\python -m copilot download-runtime`。

若已有 `.env` 或复制了 `.env.example`，请为本地演示设置 `APP_MODE=demo`、`REQUIRE_LOGIN=false`、`SESSION_SECURE=false`，使用独立的 `DATABASE_URL` 和 `DATA_ROOT`，不要指向生产数据。示例文件默认启用登录保护，正式部署时应保留该设置；演示数据库不能直接切换为 live 模式。

需要接收 GitHub Webhook 时，配置能被 GitHub 访问的 HTTPS 反向代理和 `WEBHOOK_PUBLIC_BASE_URL`。代理与服务在同一主机时可继续使用回环地址；仅当代理需跨主机或容器访问时，才按实际网络配置改为 `--host 0.0.0.0`，并限制允许访问的来源。通用 Nginx 路由见 [`deploy/nginx-webhook.conf`](deploy/nginx-webhook.conf)。

开发前端可运行 `pnpm --dir frontend dev --port 5173`，其 `/api` 代理到 8765。运行测试：

```powershell
.venv\Scripts\python -m pytest --basetemp=./.pytest_temp -p no:cacheprovider
pnpm --dir frontend build
```

完整开发流程和可选浏览器验收见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 在同一台 NAS / Linux 服务器部署

API 和 Worker 使用 Docker。**第一版只运行一个 Worker 实例**；API 可独立重启。SQLite 用于本地预览与测试，生产 Compose 使用 PostgreSQL。数据库文件和项目文件都在 `HOST_DATA_ROOT` 这一个主机目录里。

1. 将 `.env.example` 复制为 `.env`，填写 `POSTGRES_PASSWORD`（建议随机、仅字母数字以便用于连接 URL）。
2. 使用 `python -m backend.cli generate-key` 生成一次 `SECRETS_ENCRYPTION_KEY`，填入 `.env` 并备份。该密钥用于加密数据库中的项目凭据与 Agent API Key，重启时必须保留，不能每次重新生成。
3. 设置 `HOST_DATA_ROOT` 为主机绝对目录，例如 `/vol2/workflow-data`，**先创建该根目录**并让 `WORKFLOW_UID/WORKFLOW_GID` 对应的服务账号可写。程序不会自动创建这块盘的根目录。启动时会自动创建 `postgres/` 与 `projects/`。默认项目存储根是 `HOST_DATA_ROOT/projects`。不要把项目建在 `HOST_DATA_ROOT/postgres` 下。
4. 配置 `WEBHOOK_PUBLIC_BASE_URL`，例如 `https://hooks.example.com`；它决定生成的项目 Webhook 地址。未配置时网页明确提示公网入口待配置。`SESSION_SECURE=true` 用于 HTTPS 管理页面；只有内网 HTTP 开发环境才设为 `false`。
5. `docker compose up -d --build`，再运行 `docker compose exec api python -m backend.cli admin-init --username admin`，交互输入至少 12 字符的管理员密码。该命令也用于重置密码，会注销全部旧会话。默认页面只监听主机 `127.0.0.1:8080`；团队使用内网地址或内网反向代理访问。
6. 在公网 HTTPS 入口加入 `deploy/nginx-webhook.conf` 的路由，仅转发 `/api/webhooks/github/<32位标识>`。登录网页创建项目并填写 GitHub PAT；Flowdeck 会自动在仓库创建 Webhook 并订阅所选事件（PAT 需 Webhooks 读写权限）。GitHub 的有效 ping、API 读取验证和 Worker 目录检查全部通过后自动激活。未配置 `WEBHOOK_PUBLIC_BASE_URL` 时无法自动注册，需在接入详情中手动粘贴 URL。
7. 在网页端“集成管理”中配置模型接口；在项目接入详情中绑定 Agent，并按需启用总结并发布。创建项目时可填写 GitHub Actions Secrets（镜像仓库、Android 签名），供仓库自己的 Release workflow 使用。Docker 镜像构建时已预拉 Copilot CLI runtime，并设置 `COPILOT_SKIP_CLI_DOWNLOAD=1`。

GitHub PAT 应仅授权需要接入的仓库，按启用功能授予读取内容、Issues、Pull requests、Checks / Commit statuses、Webhooks 等权限；创建 Actions Secrets 时还需 Secrets 写权限，写回发布或推送文档需 Contents 写权限。具体权限以 GitHub 的端点要求及网页验证结果为准，不需要的写权限不要授予。连接模型服务前，确认允许向该服务发送相应源码与任务内容。更多部署边界见 [SECURITY.md](SECURITY.md)。

备份时同时拷贝整个 `HOST_DATA_ROOT` 和 `.env`（至少 `SECRETS_ENCRYPTION_KEY` 与 `POSTGRES_PASSWORD`）。只恢复数据目录而密钥变了，库能打开，已加密的 Token 也无法解密，需要在网页重新填写。升级执行 `docker compose up -d --build`，只换镜像，不要删除 HOST_DATA_ROOT。`docker compose down -v` 不再对应数据库文件（库不在 Docker named volume 里），但仍不要删主机数据目录。

若这台机器以前用 named volume `flowdeck_pgdata` 跑过数据库，本次不会自动搬家。先 `docker compose stop`，把卷里的文件拷进 `HOST_DATA_ROOT/postgres/`，再 `docker compose up -d --build`（启动准备会在该目录属主不是 999 时改属主）。跳过拷贝会得到空库，两份库不会合并。

## Web 界面配置指南

### 1. Agent 接入配置（模型接口）
1. 登录管理员账号，进入顶部导航 **“集成管理”**。
2. 在 **“Agent 接入”** 区域点击 **“添加 Agent”**，填写：
   - **名称**：自定义标识，如 `代码审查 Agent`。
   - **调用地址 (Base URL)**：Chat Completions 兼容根地址，如 `https://api.openai.com/v1` 或 `https://api.deepseek.com/v1`；必须提供 `/chat/completions` 端点。
   - **API Key**：模型接口凭据（加密存储，必填）。
   - **模型名**：必填，如 `gpt-4.1` 或服务商模型名。
   - **推理力度**：可选 low/medium/high/xhigh/max，留空用模型默认。
   - **超时时间**：单次分析任务的最大等待时间（默认 1800 秒）。
3. 点击 **“测试连接”**：系统会用该配置启动一次极小的 Agent 会话，验证地址、Key 与模型名全链路。
4. 打开项目 **接入详情**，在 **AGENT → 内置审查** 中打开开关并选择模型接口。收到 PR 后会自动审查；不要靠添加事件任务来开启审查。

DeepSeek 等只提供 Chat Completions 的服务可把调用地址直接填该服务的 `/v1` 根，无需转换网关。本地安装依赖后需执行一次 `python -m copilot download-runtime`；Docker 镜像已预拉。升级前若磁盘上仍有 `DATA_ROOT/codex-home/`，可手工删除，程序不再使用该目录。

### 2. 手动触发版本发布
- 在网页看板点击任意已创建的 Release 进入详情面板。
- 在右侧详情面板中点击 **“执行发布”** 按钮，系统将创建发布任务并由 Worker 生成更新记录并写回 GitHub Release。镜像构建与后端部署不在本程序执行。

## 代码和结果保存在哪里

```text
HOST_DATA_ROOT/  例如 /vol2/workflow-data/
  postgres/              PostgreSQL 数据文件
  projects/<项目>/
    .flowdeck-project.json  项目归属标记
    repo.git/              此项目的 Git 对象缓存
    workspaces/<任务ID>/
      code/                当前提交的代码快照
      base/                比较基线的代码快照
      changes.diff         PR 使用 merge-base 差异，Release 使用两次版本差异
      commits.txt
      task.json            仓库、SHA、文件列表与主机工作区路径
  copilot-home/            Agent 会话（重启后可继续）
  skills/                  上传的技能包
  artifacts/<任务ID>/      任务产物（如有）
```

项目目录必须是 Worker 服务器上允许的根目录内的绝对路径，各项目不得重复或嵌套；现有无关文件不会被覆盖。项目第一次准备代码前可以在接入详情修正目录，开始拉取后不提供网页迁移。

多磁盘可设置 `STORAGE_ROOTS=["/vol2/projects","/vol3/projects"]`，并在 Compose 的 **api 和 worker 两个服务**为每个根目录增加同路径 bind mount，使用 `create_host_path: false`。

## 失败、重启与人工处理

- 数据库先保存事件再返回 202。Worker 独立处理，失败事件可恢复，不占用 Webhook 请求连接。
- 当前提交改变或 PR 撤回草稿后，旧任务不可继续授权合并。自动合并携带精确 head SHA。
- Agent 分析以 Copilot session 运行并保存 `session_id`；Worker 重启后恢复原会话继续。升级前遗留的 HTTP `messages` 或 Codex `thread_id` 无法继续，会明确报错，使用 `retry JOB_ID --new-analysis` 重新分析。未知结果不自动当作成功，也不越过审批。
- `retry JOB_ID` 保留执行意图，只核对未知步骤的实际结果；`--acknowledge-uncertain` 表示管理员已核对外部状态，才允许清除未确认步骤的意图并重新提交。
- 管理人通知写入对应 PR 评论，发布失败则创建去重的 GitHub Issue 并 @配置人员。未配置管理人或发送失败会留下明确事件。
- 管理命令在服务器执行：

```sh
python -m backend.cli validate
python -m backend.cli sync
python -m backend.cli events
python -m backend.cli retry-event EVENT_ID
python -m backend.cli retry JOB_ID
# 仅在已核对外部状态、确认可以重新提交时：
python -m backend.cli retry JOB_ID --acknowledge-uncertain
# 需要重新进行一次分析，而非继续查询原 Agent 任务时：
python -m backend.cli retry JOB_ID --new-analysis
python -m backend.cli cleanup --older-than-days 14
```

服务端环境或 Docker 挂载修改后重启 API 和 Worker；网页项目设置无需重启。Docker 中使用 `docker compose exec worker python -m backend.cli ...`。清理按项目目录和旧任务目录检查归属，只移除已完成/被替代任务的代码副本，数据库、Git 缓存与发布产物保留。

## 外部执行器接口

`POST /api/events/executor` 接收带 `X-Executor-Signature-256` 的 HMAC-SHA256 JSON 事件。示例发送工具见 `scripts/report_event.py`。上报必须对应本程序已有任务 ID，且步骤与任务类型匹配。上报只记录观察结果，不直接授予合并或发布权限。

事件字段：`version`、`event_id`、`producer`、`repo`、`execution_id`、`step`、`status`、`sequence`、`occurred_at`、`message`、`result`。重复 event ID 去重，同一执行器/步骤使用单调序号避免乱序覆盖。

## 开源与社区

Flowdeck 的原创代码和文档按 [MIT License](LICENSE) 提供。第三方依赖及运行时遵循各自许可证与使用条款；本项目不附带 GitHub、Copilot 或模型服务的账户与授权，也不代表这些服务的官方产品。

- 开发与提交改动：[贡献指南](CONTRIBUTING.md)。
- 社区交流：[行为准则](CODE_OF_CONDUCT.md)。
- 使用问题与 Bug：[支持说明](SUPPORT.md)，在本仓库 Issues 中选择模板。
- 私密漏洞报告：[安全政策](SECURITY.md)。
- 首次公开仓库与发布版本：[开源发布清单](docs/OPEN_SOURCE_RELEASE.md)。

仓库附带的 CI 会执行后端测试、前端构建、Chromium 接入验收和 Docker 构建，不需要配置真实服务凭据，也不会自动发布或部署。
