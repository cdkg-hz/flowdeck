# NAS Docker 数据持久化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 NAS 上 `docker compose up -d --build` 把 PostgreSQL 与业务文件都写进同一个 `HOST_DATA_ROOT`，换镜像不丢数据。

**Architecture:** 一次性 `prepare-data` 容器以 root 运行 `scripts/prepare_data_root.py`，在已存在的主机根目录下创建 `postgres/` 与 `projects/` 并设置属主。Postgres 17 把整棵根目录 bind 到 `/var/lib/postgresql/data` 且 `PGDATA=.../postgres`。API/Worker 仍同路径 bind。存储层拒绝把 `DATA_ROOT/postgres` 当项目目录。

**Tech Stack:** Python 3.12 / pytest / PyYAML / Docker Compose / postgres:17-bookworm。

**Spec:** `docs/superpowers/specs/2026-09-21-docker-data-persistence-design.md`

## Global Constraints

- 目标环境是 Linux / NAS 主机目录 bind；不改为 Docker Desktop named volume 方案。
- 操作者必须预先创建 `HOST_DATA_ROOT`；Compose 与程序都不得 `create_host_path` 自动创建该根路径。
- 数据库文件在 `HOST_DATA_ROOT/postgres/`；默认项目根是 `HOST_DATA_ROOT/projects/`。
- 保持三个容器：`prepare-data` 一次性、`db`、`api`、仅一个 `worker`。不合并 api/worker。
- Postgres 文件系统用户锁定 uid/gid **999**（`postgres:17-bookworm`）。`POSTGRES_USER=flowdeck` 只是数据库角色。
- `PGDATA` 必须是 `/var/lib/postgresql/data/postgres`。官方 `postgres:17` 必须把真实 bind 挂在 `/var/lib/postgresql/data`，禁止增加 Dockerfile `VOLUME`。
- 不自动迁移旧 named volume `flowdeck_pgdata`；不把 `SECRETS_ENCRYPTION_KEY` 写入数据目录；不加 GitHub Actions。
- 运行测试：`.venv\Scripts\python -m pytest --basetemp=./.pytest_temp -p no:cacheprovider`（下文 `pytest …` 均指此形式加文件参数）。不在本任务启动 Docker 守护进程。
- 提交用 `git commit --only <paths>`，禁止 `git commit -a`。

## File Structure

| 文件 | 职责 |
|---|---|
| `scripts/prepare_data_root.py` | 校验根目录、创建 `postgres/` 与 `projects/`、Unix 下设置权限 |
| `tests/test_prepare_data_root.py` | 准备脚本的失败/成功/符号链接/chown 行为 |
| `backend/storage.py` | 拒绝项目路径占用 `DATA_ROOT/postgres` |
| `tests/test_project_storage.py` | 保留目录校验 |
| `compose.yml` | prepare-data、删除 `pgdata`、Postgres bind + `PGDATA`、强制 `HOST_DATA_ROOT` |
| `tests/test_compose_layout.py` | 解析 compose.yml / Dockerfile 约束 |
| `.env.example` | 数据目录布局与备份说明 |
| `README.md` | NAS 部署、备份、旧卷手工迁移 |
| `Dockerfile` | 不改；测试锁定没有 `VOLUME` |

---

### Task 1: 数据根目录准备脚本

**Files:**
- Create: `scripts/prepare_data_root.py`
- Test: `tests/test_prepare_data_root.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `POSTGRES_FS_UID: int = 999`、`POSTGRES_FS_GID: int = 999`
  - `PrepareFailed(Exception)`
  - `prepare_data_root(root: Path, *, workflow_uid: int = 1000, workflow_gid: int = 1000) -> None`
  - `main() -> int`（读 `HOST_DATA_ROOT` / `WORKFLOW_UID` / `WORKFLOW_GID`，失败打印中文到 stderr 并返回 1）
  - `_on_unix() -> bool`、`_owner_uid(path: Path) -> int`、`_chmod(path: Path, mode: int) -> None`、`_chown(path: Path, uid: int, gid: int) -> None`、`_chown_tree(path: Path, uid: int, gid: int) -> None`（供测试 monkeypatch）

- [ ] **Step 1: Write the failing test**

创建 `tests/test_prepare_data_root.py`：

```python
from pathlib import Path
import pytest
from scripts.prepare_data_root import (
    POSTGRES_FS_UID,
    PrepareFailed,
    main,
    prepare_data_root,
)


def test_missing_root_fails_without_creating(tmp_path):
    root = tmp_path / "missing"
    with pytest.raises(PrepareFailed, match="根目录不存在"):
        prepare_data_root(root)
    assert not root.exists()


def test_creates_postgres_and_projects_and_keeps_existing_files(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    kept = root / "projects" / "keep.txt"
    kept.parent.mkdir()
    kept.write_text("stay", encoding="utf-8")
    prepare_data_root(root)
    assert (root / "postgres").is_dir()
    assert (root / "projects").is_dir()
    assert kept.read_text(encoding="utf-8") == "stay"
    assert not (root / "copilot-home").exists()
    assert not (root / "skills").exists()


def test_rejects_symlink_root_and_children(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    root = tmp_path / "link"
    try:
        root.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this host")
    with pytest.raises(PrepareFailed, match="符号链接"):
        prepare_data_root(root)

    data = tmp_path / "data"
    data.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = data / "postgres"
    linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(PrepareFailed, match="符号链接"):
        prepare_data_root(data)
    assert list(outside.iterdir()) == []


def test_unix_chowns_root_and_projects_and_recurses_postgres_only_when_needed(tmp_path, monkeypatch):
    from scripts import prepare_data_root as module

    root = tmp_path / "data"
    root.mkdir()
    postgres = root / "postgres"
    postgres.mkdir()
    nested = postgres / "base" / "file"
    nested.parent.mkdir()
    nested.write_text("pg", encoding="utf-8")
    (root / "projects").mkdir()
    chown_calls = []
    chmod_calls = []
    tree_calls = []

    monkeypatch.setattr(module, "_on_unix", lambda: True)
    monkeypatch.setattr(module, "_chown", lambda path, uid, gid: chown_calls.append((Path(path), uid, gid)))
    monkeypatch.setattr(module, "_chmod", lambda path, mode: chmod_calls.append((Path(path), mode)))
    monkeypatch.setattr(module, "_chown_tree", lambda path, uid, gid: tree_calls.append((Path(path), uid, gid)))

    monkeypatch.setattr(module, "_owner_uid", lambda path: 1000 if Path(path) == postgres else 0)
    prepare_data_root(root, workflow_uid=2000, workflow_gid=2001)
    assert (root, 0o755) in chmod_calls
    assert (postgres, 0o700) in chmod_calls
    assert tree_calls == [(postgres, POSTGRES_FS_UID, POSTGRES_FS_GID)]
    assert (root / "projects", 2000, 2001) in chown_calls

    chown_calls.clear()
    chmod_calls.clear()
    tree_calls.clear()
    monkeypatch.setattr(module, "_owner_uid", lambda path: POSTGRES_FS_UID)
    prepare_data_root(root, workflow_uid=2000, workflow_gid=2001)
    assert tree_calls == []
    assert (postgres, POSTGRES_FS_UID, POSTGRES_FS_GID) in chown_calls
    assert (root / "projects", 2000, 2001) in chown_calls


def test_main_requires_absolute_host_data_root(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("HOST_DATA_ROOT", raising=False)
    assert main() == 1
    monkeypatch.setenv("HOST_DATA_ROOT", "relative/path")
    assert main() == 1
    monkeypatch.setenv("HOST_DATA_ROOT", str(tmp_path / "missing"))
    assert main() == 1
    err = capsys.readouterr().err
    assert "HOST_DATA_ROOT" in err or "根目录不存在" in err
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_prepare_data_root.py -v`

Expected: FAIL，`scripts.prepare_data_root` 无法导入。

- [ ] **Step 3: Write minimal implementation**

创建 `scripts/prepare_data_root.py`：

```python
from __future__ import annotations

import os
from pathlib import Path
import sys

POSTGRES_FS_UID = 999
POSTGRES_FS_GID = 999
ROOT_MISSING = "项目存储根目录不存在；请先创建或挂载该目录"
ROOT_UNSAFE = "项目存储根目录不能是符号链接或目录联接"
CHILD_UNSAFE = "数据子目录必须是普通目录，不能是符号链接"


class PrepareFailed(Exception):
    pass


def _linklike(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _on_unix() -> bool:
    return os.name != "nt"


def _owner_uid(path: Path) -> int:
    return path.stat().st_uid


def _chmod(path: Path, mode: int) -> None:
    os.chmod(path, mode)


def _chown(path: Path, uid: int, gid: int) -> None:
    os.chown(path, uid, gid)


def _chown_tree(path: Path, uid: int, gid: int) -> None:
    for current, _dirnames, filenames in os.walk(path, followlinks=False):
        _chown(Path(current), uid, gid)
        for name in filenames:
            child = Path(current) / name
            if not child.is_symlink():
                _chown(child, uid, gid)


def prepare_data_root(root: Path, *, workflow_uid: int = 1000, workflow_gid: int = 1000) -> None:
    if _linklike(root):
        raise PrepareFailed(ROOT_UNSAFE)
    if not root.exists() or not root.is_dir():
        raise PrepareFailed(ROOT_MISSING)
    postgres = root / "postgres"
    projects = root / "projects"
    for child in (postgres, projects):
        if child.exists() or child.is_symlink():
            if _linklike(child) or not child.is_dir():
                raise PrepareFailed(CHILD_UNSAFE)
        else:
            child.mkdir()
    if not _on_unix():
        return
    _chmod(root, 0o755)
    if _owner_uid(postgres) != POSTGRES_FS_UID:
        _chown_tree(postgres, POSTGRES_FS_UID, POSTGRES_FS_GID)
    else:
        _chown(postgres, POSTGRES_FS_UID, POSTGRES_FS_GID)
    _chmod(postgres, 0o700)
    _chown(projects, workflow_uid, workflow_gid)


def main() -> int:
    raw = (os.environ.get("HOST_DATA_ROOT") or "").strip()
    root = Path(raw) if raw else Path()
    if not raw or not root.is_absolute():
        print("HOST_DATA_ROOT 必须是绝对路径", file=sys.stderr)
        return 1
    try:
        uid = int(os.environ.get("WORKFLOW_UID") or "1000")
        gid = int(os.environ.get("WORKFLOW_GID") or "1000")
    except ValueError:
        print("WORKFLOW_UID 和 WORKFLOW_GID 必须是整数", file=sys.stderr)
        return 1
    try:
        prepare_data_root(root, workflow_uid=uid, workflow_gid=gid)
    except PrepareFailed as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_prepare_data_root.py -v`

Expected: PASS（符号链接不可用的主机跳过对应断言，其余通过）。

- [ ] **Step 5: Commit**

```bash
git add -- scripts/prepare_data_root.py tests/test_prepare_data_root.py
git commit --only scripts/prepare_data_root.py tests/test_prepare_data_root.py -m "feat: 启动前准备 NAS 数据根目录的 postgres 与 projects"
```

---

### Task 2: 拒绝占用数据库目录

**Files:**
- Modify: `backend/storage.py`（`normalize_storage_path`）
- Test: `tests/test_project_storage.py`

**Interfaces:**
- Consumes: 现有 `normalize_storage_path(settings, value, other_paths=()) -> str`、`Settings.data_root`
- Produces: 同一函数在候选路径等于 `_canonical(data_root / "postgres")` 或其子路径时抛出 `ConfigurationNeeded("不能使用数据库目录作为项目存储路径")`

- [ ] **Step 1: Write the failing test**

在 `tests/test_project_storage.py` 追加：

```python
def test_storage_rejects_reserved_postgres_directory(tmp_path):
    data_root = tmp_path / "data"
    data_root.mkdir()
    (data_root / "postgres").mkdir()
    settings = Settings(_env_file=None, data_root=data_root, storage_roots=[str(data_root)])
    with pytest.raises(ConfigurationNeeded, match="数据库目录"):
        normalize_storage_path(settings, data_root / "postgres")
    with pytest.raises(ConfigurationNeeded, match="数据库目录"):
        normalize_storage_path(settings, data_root / "postgres" / "team")
    allowed = data_root / "projects" / "app"
    assert normalize_storage_path(settings, allowed) == str(allowed.resolve())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_project_storage.py::test_storage_rejects_reserved_postgres_directory -v`

Expected: FAIL，路径被当成普通允许路径，没有「数据库目录」错误。

- [ ] **Step 3: Write minimal implementation**

在 `backend/storage.py` 的 `normalize_storage_path` 里，`candidate = _canonical(raw)` 之后、`_root_for` 之前插入：

```python
    reserved = _canonical(Path(settings.data_root) / "postgres")
    if candidate == reserved or candidate.is_relative_to(reserved):
        raise ConfigurationNeeded("不能使用数据库目录作为项目存储路径")
```

完整函数成为：

```python
def normalize_storage_path(settings: Settings, value: str | os.PathLike[str], other_paths: Iterable[str | os.PathLike[str]] = ()) -> str:
    """Normalize a project path and reject escapes or overlapping project trees."""
    raw = Path(value)
    if not raw.is_absolute():
        raise ConfigurationNeeded("项目存储路径必须是绝对路径")
    candidate = _canonical(raw)
    reserved = _canonical(Path(settings.data_root) / "postgres")
    if candidate == reserved or candidate.is_relative_to(reserved):
        raise ConfigurationNeeded("不能使用数据库目录作为项目存储路径")
    _root_for(settings, candidate)
    for other_value in other_paths:
        other_raw = Path(other_value)
        if not other_raw.is_absolute():
            raise ConfigurationNeeded("已有项目存储路径不是绝对路径")
        other = _canonical(other_raw)
        _root_for(settings, other)
        if candidate == other or candidate.is_relative_to(other) or other.is_relative_to(candidate):
            raise ConfigurationNeeded("项目存储路径不能与其他项目重复或互相嵌套")
    return str(candidate)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_project_storage.py::test_storage_rejects_reserved_postgres_directory tests/test_project_storage.py::test_storage_path_requires_existing_allowlisted_root tests/test_project_storage.py::test_storage_rejects_duplicate_roots_and_overlapping_project_paths -v`

Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add -- backend/storage.py tests/test_project_storage.py
git commit --only backend/storage.py tests/test_project_storage.py -m "fix: 禁止把数据库目录当作项目存储路径"
```

---

### Task 3: Compose 持久化布局

**Files:**
- Modify: `compose.yml`（整文件替换为下面给出的内容）
- Test: `tests/test_compose_layout.py`
- Verify unchanged: `Dockerfile` 仍无 `VOLUME`

**Interfaces:**
- Consumes: Task 1 的 `/app/scripts/prepare_data_root.py`（镜像内路径，Dockerfile 已 `COPY scripts/`）
- Produces: Compose 服务 `prepare-data`、`db`（`PGDATA=/var/lib/postgresql/data/postgres`）、`api`、`worker`；无 named volume `pgdata`；`HOST_DATA_ROOT` 用 `${HOST_DATA_ROOT:?Set HOST_DATA_ROOT in .env}`

- [ ] **Step 1: Write the failing test**

创建 `tests/test_compose_layout.py`：

```python
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]
HOST = "${HOST_DATA_ROOT:?Set HOST_DATA_ROOT in .env}"


def compose():
    return yaml.safe_load((ROOT / "compose.yml").read_text(encoding="utf-8"))


def data_binds(service: dict) -> list[dict]:
    found = []
    for item in service.get("volumes") or []:
        if isinstance(item, dict) and item.get("type") == "bind" and HOST in str(item.get("source", "")):
            found.append(item)
    return found


def test_compose_persists_postgres_under_host_data_root():
    doc = compose()
    assert "volumes" not in doc
    services = doc["services"]
    prepare = services["prepare-data"]
    db = services["db"]
    assert prepare["image"] == "flowdeck:local"
    assert prepare["user"] == "0:0"
    assert prepare["restart"] == "no"
    assert prepare["command"] == ["python", "/app/scripts/prepare_data_root.py"]
    assert prepare["environment"]["HOST_DATA_ROOT"] == HOST
    assert db["environment"]["PGDATA"] == "/var/lib/postgresql/data/postgres"
    assert db["depends_on"] == {"prepare-data": {"condition": "service_completed_successfully"}}
    db_binds = data_binds(db)
    assert len(db_binds) == 1
    assert db_binds[0]["target"] == "/var/lib/postgresql/data"
    assert db_binds[0]["bind"]["create_host_path"] is False
    for name in ("prepare-data", "api", "worker"):
        binds = data_binds(services[name])
        assert binds, name
        for item in binds:
            assert item["target"] == HOST
            assert item["bind"]["create_host_path"] is False
    assert services["api"]["environment"]["DATA_ROOT"] == HOST
    assert services["api"]["environment"]["WORKSPACE_HOST_ROOT"] == HOST
    assert "build" in services["api"]
    assert "build" not in services["worker"]
    assert "build" not in services["prepare-data"]
    assert services["worker"]["image"] == "flowdeck:local"


def test_dockerfile_does_not_declare_volume():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "VOLUME" not in text.split()
```

最后一条用 `text.split()` 避免误伤注释；Dockerfile 里当前没有 `VOLUME`，此测试现在就会过。布局测试应对旧 `compose.yml` 失败。

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_compose_layout.py::test_compose_persists_postgres_under_host_data_root -v`

Expected: FAIL，现有文件仍有顶层 `volumes: pgdata`，且没有 `prepare-data` / `PGDATA`。

- [ ] **Step 3: Write minimal implementation**

将 `compose.yml` 替换为：

```yaml
name: flowdeck
services:
  prepare-data:
    image: flowdeck:local
    user: "0:0"
    restart: "no"
    environment:
      HOST_DATA_ROOT: ${HOST_DATA_ROOT:?Set HOST_DATA_ROOT in .env}
      WORKFLOW_UID: ${WORKFLOW_UID:-1000}
      WORKFLOW_GID: ${WORKFLOW_GID:-1000}
    volumes:
      - type: bind
        source: ${HOST_DATA_ROOT:?Set HOST_DATA_ROOT in .env}
        target: ${HOST_DATA_ROOT:?Set HOST_DATA_ROOT in .env}
        bind:
          create_host_path: false
    command: [python, /app/scripts/prepare_data_root.py]
  db:
    image: postgres:17-bookworm
    restart: unless-stopped
    environment:
      POSTGRES_USER: flowdeck
      POSTGRES_DB: flowdeck
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?Set POSTGRES_PASSWORD in .env}
      PGDATA: /var/lib/postgresql/data/postgres
    volumes:
      - type: bind
        source: ${HOST_DATA_ROOT:?Set HOST_DATA_ROOT in .env}
        target: /var/lib/postgresql/data
        bind:
          create_host_path: false
    healthcheck:
      test: [CMD-SHELL, "pg_isready -U flowdeck -d flowdeck"]
      interval: 10s
      timeout: 5s
      retries: 10
    depends_on:
      prepare-data:
        condition: service_completed_successfully
  api:
    build: .
    image: flowdeck:local
    restart: unless-stopped
    user: "${WORKFLOW_UID:-1000}:${WORKFLOW_GID:-1000}"
    env_file: .env
    environment: &environment
      APP_MODE: live
      DATABASE_URL: postgresql+psycopg://flowdeck:${POSTGRES_PASSWORD}@db:5432/flowdeck
      DATA_ROOT: ${HOST_DATA_ROOT:?Set HOST_DATA_ROOT in .env}
      WORKSPACE_HOST_ROOT: ${HOST_DATA_ROOT:?Set HOST_DATA_ROOT in .env}
      CONFIG_PATH: /app/config/projects.yml
      COPILOT_SKIP_CLI_DOWNLOAD: "1"
    ports:
      - "${DASHBOARD_BIND:-127.0.0.1}:${DASHBOARD_PORT:-8080}:8080"
    volumes:
      - type: bind
        source: ${HOST_DATA_ROOT:?Set HOST_DATA_ROOT in .env}
        target: ${HOST_DATA_ROOT:?Set HOST_DATA_ROOT in .env}
        bind:
          create_host_path: false
      - ./config:/app/config:ro
    extra_hosts:
      - host.docker.internal:host-gateway
    depends_on:
      db:
        condition: service_healthy
    healthcheck:
      test: [CMD, python, -c, "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health')"]
      interval: 20s
      timeout: 5s
      retries: 5
  worker:
    image: flowdeck:local
    restart: unless-stopped
    user: "${WORKFLOW_UID:-1000}:${WORKFLOW_GID:-1000}"
    command: [python, -m, backend.worker]
    env_file: .env
    environment: *environment
    volumes:
      - type: bind
        source: ${HOST_DATA_ROOT:?Set HOST_DATA_ROOT in .env}
        target: ${HOST_DATA_ROOT:?Set HOST_DATA_ROOT in .env}
        bind:
          create_host_path: false
      - ./config:/app/config:ro
    extra_hosts:
      - host.docker.internal:host-gateway
    depends_on:
      api:
        condition: service_healthy
```

`restart: "no"` 和 `user: "0:0"` 必须加引号，避免 YAML 把 `no` 当成布尔、把 `0:0` 当成其它类型。不要加回顶层 `volumes:`。不要改 `Dockerfile`。

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_compose_layout.py tests/test_compose_script.py tests/test_prepare_data_root.py -v`

Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add -- compose.yml tests/test_compose_layout.py
git commit --only compose.yml tests/test_compose_layout.py -m "feat: 把 Postgres 和业务文件都持久化到 HOST_DATA_ROOT"
```

---

### Task 4: 部署文档与环境示例

**Files:**
- Modify: `README.md`（「在同一台 NAS / Linux 服务器部署」「代码和结果保存在哪里」）
- Modify: `.env.example`（`HOST_DATA_ROOT` 注释）

**Interfaces:**
- Consumes: Task 3 的目录布局与旧卷名称 `flowdeck_pgdata`
- Produces: 文档说明先建根目录、备份「数据目录 + `.env`」、不要删 `HOST_DATA_ROOT`、旧卷手工拷贝；`.env.example` 写明 `postgres/` 与 `projects/`

- [ ] **Step 1: Write the failing documentation assertion**

在 `tests/test_compose_layout.py` 追加：

```python
def test_readme_and_env_example_describe_host_data_layout():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "HOST_DATA_ROOT/postgres/" in readme
    assert "SECRETS_ENCRYPTION_KEY" in readme
    assert "flowdeck_pgdata" in readme
    assert "不要删除 HOST_DATA_ROOT" in readme or "不要删除数据目录" in readme
    assert "postgres/" in example
    assert "projects/" in example
    assert "SECRETS_ENCRYPTION_KEY" in example
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_compose_layout.py::test_readme_and_env_example_describe_host_data_layout -v`

Expected: FAIL，README 仍写 PostgreSQL 在独立卷，没有 `HOST_DATA_ROOT/postgres/` 与 `flowdeck_pgdata`。

- [ ] **Step 3: Write minimal documentation**

把 `README.md` 中「在同一台 NAS / Linux 服务器部署」整节替换为：

```markdown
## 在同一台 NAS / Linux 服务器部署

API 和 Worker 使用 Docker。**第一版只运行一个 Worker 实例**；API 可独立重启。SQLite 用于本地预览与测试，生产 Compose 使用 PostgreSQL。数据库文件和项目文件都在 `HOST_DATA_ROOT` 这一个主机目录里。

1. 将 `.env.example` 复制为 `.env`，填写 `POSTGRES_PASSWORD`（建议随机、仅字母数字以便用于连接 URL）。
2. 使用 `python -m backend.cli generate-key` 生成一次 `SECRETS_ENCRYPTION_KEY`，填入 `.env` 并备份。该密钥用于加密数据库中的项目凭据与 Agent API Key，重启时必须保留，不能每次重新生成。
3. 设置 `HOST_DATA_ROOT` 为主机绝对目录，例如 `/vol2/workflow-data`，**先创建该根目录**并让 `WORKFLOW_UID/WORKFLOW_GID` 对应的服务账号可写。程序不会自动创建这块盘的根目录。启动时会自动创建 `postgres/` 与 `projects/`。默认项目存储根是 `HOST_DATA_ROOT/projects`。不要把项目建在 `HOST_DATA_ROOT/postgres` 下。
4. 配置 `WEBHOOK_PUBLIC_BASE_URL`，例如 `https://hooks.example.com`；它决定生成的项目 Webhook 地址。未配置时网页明确提示公网入口待配置。`SESSION_SECURE=true` 用于 HTTPS 管理页面；只有内网 HTTP 开发环境才设为 `false`。
5. `docker compose up -d --build`，再运行 `docker compose exec api python -m backend.cli admin-init --username admin`，交互输入至少 12 字符的管理员密码。该命令也用于重置密码，会注销全部旧会话。默认页面只监听主机 `127.0.0.1:8080`；团队使用内网地址或内网反向代理访问。
6. 在公网 HTTPS 入口加入 `deploy/nginx-webhook.conf` 的路由，仅转发 `/api/webhooks/github/<32位标识>`。登录网页创建项目并填写 GitHub PAT；Flowdeck 会自动在仓库创建 Webhook 并订阅所选事件（PAT 需 Webhooks 读写权限）。GitHub 的有效 ping、API 读取验证和 Worker 目录检查全部通过后自动激活。未配置 `WEBHOOK_PUBLIC_BASE_URL` 时无法自动注册，需在接入详情中手动粘贴 URL。
7. 在网页端“集成管理”中配置模型接口；在项目接入详情中绑定 Agent，并按需启用总结并发布。创建项目时可填写 GitHub Actions Secrets（镜像仓库、Android 签名），供仓库自己的 Release workflow 使用。Docker 镜像构建时已预拉 Copilot CLI runtime，并设置 `COPILOT_SKIP_CLI_DOWNLOAD=1`。

备份时同时拷贝整个 `HOST_DATA_ROOT` 和 `.env`（至少 `SECRETS_ENCRYPTION_KEY` 与 `POSTGRES_PASSWORD`）。只恢复数据目录而密钥变了，库能打开，已加密的 Token 也无法解密，需要在网页重新填写。升级执行 `docker compose up -d --build`，只换镜像，不要删除 `HOST_DATA_ROOT`。`docker compose down -v` 不再对应数据库文件（库不在 Docker named volume 里），但仍不要删主机数据目录。

若这台机器以前用 named volume `flowdeck_pgdata` 跑过数据库，本次不会自动搬家。先 `docker compose stop`，把卷里的文件拷进 `HOST_DATA_ROOT/postgres/`，再 `docker compose up -d --build`（启动准备会在该目录属主不是 999 时改属主）。跳过拷贝会得到空库，两份库不会合并。
```

把「代码和结果保存在哪里」代码块里的树替换为下面这一棵（外层仍用 README 里原来的 ` ```text ` 围栏）：

~~~~text
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
~~~~

保留其后「项目目录必须是…」以及多磁盘 `STORAGE_ROOTS` 那段（仍只在 **api 和 worker** 增加同路径 bind，`create_host_path: false`，不要挂到 db / prepare-data）。

`.env.example` 把 Docker 段注释换成：

```
# Docker Compose on a NAS/Linux host. Create this absolute directory first.
# Layout: HOST_DATA_ROOT/postgres/ (database) and HOST_DATA_ROOT/projects/ (git workspaces).
# Back up this directory together with this .env file (SECRETS_ENCRYPTION_KEY).
# Do not onboard a project under HOST_DATA_ROOT/postgres.
HOST_DATA_ROOT=/srv/flowdeck
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_compose_layout.py tests/test_prepare_data_root.py tests/test_project_storage.py::test_storage_rejects_reserved_postgres_directory -v`

Expected: PASS。

再跑：`pytest tests/test_compose_script.py tests/test_project_storage.py -v`

Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add -- README.md .env.example tests/test_compose_layout.py
git commit --only README.md .env.example tests/test_compose_layout.py -m "docs: 说明 NAS 上单一数据目录的备份与迁移"
```
