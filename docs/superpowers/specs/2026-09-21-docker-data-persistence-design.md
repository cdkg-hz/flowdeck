# NAS Docker 数据持久化设计

日期: 2026-09-21
状态: 已与需求方确认

## 背景与目标

Flowdeck 已有多阶段 `Dockerfile`（前端构建 + Python 运行时预拉 Copilot CLI）和 `compose.yml`（`db` / `api` / `worker`）。生产数据目前拆成两处：PostgreSQL 写在 Compose named volume `pgdata`；Git 缓存、工作区、Copilot 会话写在主机目录 `HOST_DATA_ROOT`。NAS 备份要分别处理 Docker 卷和主机目录，升级时也容易误清卷。

本设计把**数据库文件和业务文件都放到 NAS 上同一个主机目录**。镜像仍在 NAS 上 `docker compose up -d --build` 构建，不增加 GitHub Actions 推送。换镜像只重建容器，不碰该目录和 `.env`。

## 已确认的决策

| 决策点 | 结论 |
|---|---|
| 运行环境 | Linux / NAS；主机目录 bind mount，不是本机 Docker Desktop named volume 方案 |
| 数据布局 | 数据库与代码文件同属一个 `HOST_DATA_ROOT`（例如 `/vol2/workflow-data`） |
| 镜像分发 | 本次不在 GitHub 构建或推送镜像；NAS 上本地 build |
| 进程拆分 | 保持三个容器：`db`、`api`、`worker`（仅一个 worker）；网页可独立重启 |
| 数据根目录 | 操作者必须预先创建 `HOST_DATA_ROOT`；程序和 Compose **不得** `create_host_path` 自动创建该根路径 |
| 子目录 | 启动前自动创建 `postgres/` 与 `projects/`，并设置正确属主 |
| 旧 `pgdata` 卷 | **不自动迁移**；文档写手工拷贝步骤 |
| 加密密钥 | 仍只存在 `.env` 的 `SECRETS_ENCRYPTION_KEY`，不写入数据目录 |

## 第 1 节 · 总体架构

三个服务共用同一份主机数据，路径约定如下（以 `HOST_DATA_ROOT=/vol2/workflow-data` 为例）：

```text
/vol2/workflow-data/          操作者预先创建；bind 时 create_host_path: false
  postgres/                   PostgreSQL 数据文件（仅数据库容器写入）
  projects/                   默认项目存储根（Git 缓存、任务工作区）
  copilot-home/               Copilot 会话（程序需要时创建）
  copilot-home-probe/         模型探测用（程序需要时创建）
  skills/                     上传的 skill 包（程序需要时创建）
  artifacts/                  任务产物（若有，程序需要时创建）
```

挂载方式：

- `prepare-data`、`api`、`worker`：把 `HOST_DATA_ROOT` **同路径**挂进容器（源和目标都是该绝对路径）。网页里填写的项目目录因此与 NAS 上看到的路径一致。
- `db`：把 **同一个** `HOST_DATA_ROOT` 挂到容器内 `/var/lib/postgresql/data`，并设置 `PGDATA=/var/lib/postgresql/data/postgres`。这样数据库文件落在主机的 `HOST_DATA_ROOT/postgres/`。
- 继续 `./config:/app/config:ro`。项目运行时配置以数据库为准；该 YAML 几乎为空，仍不进数据目录。

必须把整棵 `HOST_DATA_ROOT` 挂到 Postgres 17 的 `/var/lib/postgresql/data`，并把 `PGDATA` 设成其子目录 `postgres`：

- 官方 `postgres:17` 镜像把 `/var/lib/postgresql/data` 声明为 `VOLUME`。这里必须有真实 bind，否则运行时会另造匿名卷，删容器后 NAS 上看不到库。
- 挂整棵根而不是只挂 `postgres/`，这样第一次启动时子目录还不存在，Compose 也不会因为 `create_host_path: false` 而起不来。
- `PGDATA` 必须是 `/var/lib/postgresql/data/postgres`。漏设的话 initdb 会把整个数据根（含 `projects/`）当成库目录。

`STORAGE_ROOTS` 未配置时仍默认为 `DATA_ROOT/projects`，即 `HOST_DATA_ROOT/projects`。`DATA_ROOT` 与 `WORKSPACE_HOST_ROOT` 在 Compose 里继续设为 `HOST_DATA_ROOT`。

`HOST_DATA_ROOT/postgres` 为保留名。项目存储路径不得等于该目录，也不得位于其下。

`.env` 与 `SECRETS_ENCRYPTION_KEY` 不放进数据目录。备份必须同时拷贝数据目录和 `.env`（至少密钥与 `POSTGRES_PASSWORD`）。只恢复数据目录而密钥变更时，库能打开，已加密的 GitHub Token / Agent Key 无法解密，需要在网页重新填写。

## 第 2 节 · 启动准备与权限

新增一次性服务 `prepare-data`：

- 使用已构建的 `flowdeck:local` 镜像，`user: "0:0"`，`restart: "no"`。环境变量传入 `HOST_DATA_ROOT`、`WORKFLOW_UID`、`WORKFLOW_GID`（后两者默认 1000）。
- 入口：`python /app/scripts/prepare_data_root.py`（仓库内新增脚本，供 Compose 与 pytest 共用）。脚本只处理环境变量里的绝对路径，该路径必须已按同路径 bind 挂进容器。
- `db` `depends_on` 该服务 `condition: service_completed_successfully`。
- `api` 仍等 `db` healthy；`worker` 仍等 `api` healthy。Worker 实例数仍为 1。

脚本行为（失败即非 0 退出，中文错误信息）：

1. 读取绝对路径 `HOST_DATA_ROOT`。根路径不存在、不是目录、或是符号链接 / 目录联接：退出，提示先创建或挂载该目录。**不得创建该根路径。**
2. 在根下创建 `postgres/` 与 `projects/`（已存在则保留）。这两项若是符号链接：退出，不得跟随。
3. 仅在 Unix 上调整权限（Windows 测试跳过 `chown`）：
   - 每次将根目录 `chmod 755`，让数据库进程（容器内 uid 999）能进入 `postgres/`。本次不做组映射；不递归改整棵树。
   - `postgres/`：将**目录本身** `chown 999:999` 且 `chmod 700`。仅当该目录当前属主不是 999 时，才对**这一子目录**做 `chown -R 999:999`（覆盖刚从旧卷拷来的文件）。属主已是 999 时不递归，避免每次启动扫整个库。
   - `projects/`：将**目录本身** `chown` 为 `WORKFLOW_UID:WORKFLOW_GID`（默认 1000:1000）。已有内容不递归改属主。
4. 不删除任何文件，不修改 `copilot-home/`、`skills/` 等其它子目录。

Postgres 文件系统用户锁定为 **uid/gid 999**，与 `postgres:17-bookworm` 镜像内 `postgres` 用户一致。Compose 里 `POSTGRES_USER=flowdeck` 只表示数据库角色，不是 Linux 用户。

`copilot-home/`、`copilot-home-probe/`、`skills/` 仍由应用在需要时创建，不由准备脚本创建。

权限或根目录问题必须让数据库容器起不来并留下明确日志，禁止在错误属主下把库初始化成空目录。

## 第 3 节 · Compose 与镜像

`compose.yml`：

- 删除顶层 named volume `pgdata` 及 `db` 对其的引用。
- `HOST_DATA_ROOT` 与 `POSTGRES_PASSWORD` 一样用 `${VAR:?...}` 强制存在。
- `db` 环境变量增加 `PGDATA=/var/lib/postgresql/data/postgres`。数据挂载为 `HOST_DATA_ROOT` → `/var/lib/postgresql/data`，`create_host_path: false`。
- `api` / `worker` 保持现有同路径 bind、`create_host_path: false`、健康检查、`extra_hosts`、`COPILOT_SKIP_CLI_DOWNLOAD=1`。若配置了额外 `STORAGE_ROOTS`，仍须在 **api 和 worker**（不必在 db / prepare-data）为每个根增加同样的同路径 bind，`create_host_path: false`。
- `prepare-data` 与 `api`/`worker` 使用同一 `image: flowdeck:local`；仅 `api` 需要 `build: .`，避免重复构建。

`Dockerfile`：

- 继续多阶段构建、预拉 Copilot runtime、非 root 默认用户。
- **不**增加 `VOLUME` 指令，避免 `docker run` 在未声明挂载时创建匿名卷、造成「数据在容器里其实没挂上」的假象。
- 已 `COPY scripts/`，准备脚本随镜像携带。不改业务应用入口。

`.env.example` 补充：`HOST_DATA_ROOT` 下将出现 `postgres/` 与 `projects/`；备份该目录时同时备份 `.env`；不要把项目建在 `postgres/` 下。

## 第 4 节 · 升级、备份与失败

升级：在 NAS 上更新仓库后 `docker compose up -d --build`。只重建镜像与容器。不得把数据目录或 `.env` 当作构建上下文的可丢弃部分（现有 `.dockerignore` 已排除 `.env` 与 `data/`）。不要用会删除主机 bind 内容的流程；`docker compose down -v` 不再对应本设计的数据库文件（库不在 named volume），但文档仍写明：不要删除 `HOST_DATA_ROOT`。

备份：拷贝整个 `HOST_DATA_ROOT` 和 `.env`。恢复时先放回这两份再 `up`。已有 `postgres/` 时 Postgres 入口不得当成空库重新 init。

失败：

- 根目录缺失：`prepare-data` 失败，`db`/`api`/`worker` 不启动。
- `postgres/` 属主不是 999：准备脚本会纠正该子目录属主；若之后 Postgres 仍拒绝启动，日志应指向数据目录权限，而不是静默建空库。
- `api` 与 `worker` 生命周期独立：重启 `api` 不停止 worker；worker 重启后从 `DATA_ROOT/copilot-home` 按已有 `session_id` 恢复。
- 旧部署若使用 named volume `flowdeck_pgdata`：本次不自动拷贝。README 写手工步骤：停止服务 → 将卷内文件拷入 `HOST_DATA_ROOT/postgres/` → 确认属主 999:999 → 再 `up`。拷错或跳过则新目录为空库，属于操作错误，程序不合并两份库。

## 第 5 节 · 测试与文档

不在本任务中启动真实 Docker 守护进程做集成测试（开发机是 Windows，目标是 NAS）。用 pytest 锁住可在本仓库运行的行为：

- `scripts/prepare_data_root.py`：根缺失失败且不创建根；成功时创建 `postgres/` 与 `projects/`；拒绝符号链接根或子目录；不删除已有文件；Unix 下才调用 `chown`（测试可用 monkeypatch 断言调用参数：根不递归；`postgres/` 仅当属主不是 999 才递归）。
- `compose.yml` 解析：无 `pgdata` named volume；`db` 的 `PGDATA` 为 `/var/lib/postgresql/data/postgres`；`db`/`api`/`worker`/`prepare-data` 的数据 bind 均为 `create_host_path: false`；`prepare-data` 以 root 跑准备脚本且 `db` 依赖其成功结束。
- 项目存储：路径落在 `DATA_ROOT/postgres` 或其子路径时拒绝。

README「在同一台 NAS / Linux 服务器部署」改为与上述布局一致：先建根目录 → 写 `.env` → `up --build` → `admin-init`；写明备份范围、保留目录名、旧卷手工迁移、密钥不可更换后仍想读旧凭据。

## 范围外

- GitHub Actions 构建/推送镜像
- 合并 `api` 与 `worker`
- 自动创建 `HOST_DATA_ROOT` 或 NAS 共享文件夹本身
- SQLite 作为生产库
- 把 `SECRETS_ENCRYPTION_KEY` 写入数据目录
- 自动从 `flowdeck_pgdata` 迁移
- 改 Agent / Webhook / 看板等业务逻辑（除「拒绝占用 postgres 目录」这一存储校验）
