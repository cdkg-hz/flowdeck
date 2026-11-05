from __future__ import annotations

import asyncio
import base64
import io
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import stat
import tarfile
from backend.config import Settings
from backend.errors import ConfigurationNeeded
from backend.storage import validate_project_marker

TASK_CONTEXT_FILES = (
    ("changes.diff", ".flowdeck-changes.diff"),
    ("commits.txt", ".flowdeck-commits.txt"),
)
RESERVED_TASK_CONTEXT_NAMES = frozenset(dest for _src, dest in TASK_CONTEXT_FILES)

_DOC_DATE = re.compile(r"(20\d{2}-\d{2}-\d{2})")


def latest_release_docs(paths: list[str]) -> dict:
    found: list[tuple[str, str]] = []
    for raw in paths:
        path = str(raw).replace("\\", "/").strip()
        if not path or not path.endswith(".md"):
            continue
        name = path.rsplit("/", 1)[-1]
        match = _DOC_DATE.search(name)
        if match:
            found.append((match.group(1), path))
    if not found:
        return {"docs_date": "", "files": []}
    date = max(item[0] for item in found)
    files = sorted(path for item_date, path in found if item_date == date)
    return {"docs_date": date, "files": files}


def expose_task_context(directory: Path) -> None:
    work = directory / "work"
    if not work.exists():
        return
    if work.is_symlink() or not work.is_dir():
        raise ConfigurationNeeded("可推送工作区路径不安全")
    for src_name, dest_name in TASK_CONTEXT_FILES:
        src = directory / src_name
        dest = work / dest_name
        if src.is_symlink() or dest.is_symlink():
            raise ConfigurationNeeded("任务比较材料路径不安全")
        if not src.is_file():
            continue
        if dest.exists() and not dest.is_file():
            raise ConfigurationNeeded("任务比较材料路径不安全")
        dest.write_bytes(src.read_bytes())


def minimal_environment(allowlist=(), extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build a child environment without unrelated service credentials."""
    essentials = {
        "PATH",
        "SystemRoot",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
    names = essentials | set(allowlist)
    env = {name: os.environ[name] for name in names if name in os.environ}
    env.setdefault("PATH", os.environ.get("PATH", os.defpath))
    if extra:
        env.update({key: str(value) for key, value in extra.items()})
    return env


async def command(argv: list[str], cwd: Path | None = None, env: dict | None = None, timeout: int = 180):
    process = await asyncio.create_subprocess_exec(*argv, cwd=cwd, env=env if env is not None else minimal_environment(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        process.kill()
        await process.wait()
        raise
    if process.returncode:
        # Git errors are useful, but never include supplied secrets or command arguments.
        message = stderr.decode("utf-8", "replace")[:3000]
        from backend.service import redact
        secrets = [v for k, v in (env or {}).items() if len(v) > 8 and (any(x in k.upper() for x in ["TOKEN", "SECRET", "PASSWORD", "API_KEY"]) or k == "GIT_CONFIG_VALUE_4")]
        authorization = (env or {}).get("GIT_CONFIG_VALUE_4", "")
        if authorization.startswith("Authorization: Basic "):
            try:
                decoded = base64.b64decode(authorization.removeprefix("Authorization: Basic ")).decode()
                secrets.append(decoded.partition(":")[2])
            except (ValueError, UnicodeDecodeError):
                pass
        raise RuntimeError(f"命令 {Path(argv[0]).name} 执行失败：{redact(message, secrets)}")
    return stdout


class Workspaces:
    def __init__(self, settings: Settings, sessions=None):
        self.settings = settings
        self.sessions = sessions
        self.root = settings.data_root.resolve()

    @staticmethod
    def _valid_task_id(task_id: str) -> None:
        if not re.fullmatch(r"[a-f0-9]{32}", task_id):
            raise ValueError("invalid task id")

    @staticmethod
    def _linklike(path: Path) -> bool:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction and is_junction())

    def _project_for_repo(self, repo: str, *, lock: bool) -> tuple[str, Path]:
        from sqlalchemy import func, select, update
        from backend.db import Project, ProjectConnection
        from backend.projects import connection_active

        with self.sessions() as session:
            project = session.scalar(select(Project).where(func.lower(Project.repo) == repo.lower()))
            if project is None:
                raise ConfigurationNeeded("任务仓库没有对应的项目")
            if lock:
                # Updating the same row as the admin storage endpoint serializes a
                # first checkout against a concurrent path replacement.
                changed = session.execute(
                    update(ProjectConnection)
                    .where(ProjectConnection.project_id == project.id)
                    .values(storage_locked=True)
                )
                if not changed.rowcount:
                    session.rollback()
                    raise ConfigurationNeeded("项目连接尚未创建")
                connection = session.get(ProjectConnection, project.id)
                session.refresh(connection)
            else:
                connection = session.get(ProjectConnection, project.id)
            if connection is None or (lock and not connection_active(connection)):
                session.rollback()
                raise ConfigurationNeeded("项目连接尚未激活")
            if not connection.storage_path:
                session.rollback()
                raise ConfigurationNeeded("项目尚未配置代码存储路径")
            if lock:
                session.commit()
            project_id = project.id
            storage_path = connection.storage_path
        return project_id, validate_project_marker(self.settings, project_id, storage_path)

    def _repo_for_task(self, task_id: str) -> str:
        from sqlalchemy import select
        from backend.db import Job, Record

        with self.sessions() as session:
            row = session.execute(
                select(Record.repo).join(Job, Job.record_id == Record.id).where(Job.id == task_id)
            ).scalar_one_or_none()
        if row is None:
            raise ConfigurationNeeded("找不到任务对应的项目存储")
        return row

    def _safe_child(self, project_root: Path, *parts: str, create: bool = False) -> Path:
        current = project_root
        for part in parts:
            current = current / part
            if create and not current.exists():
                try:
                    current.mkdir()
                except FileExistsError:
                    pass
            if current.exists() or current.is_symlink():
                if self._linklike(current) or not current.is_dir() or not current.resolve().is_relative_to(project_root):
                    raise ConfigurationNeeded("项目工作区包含不安全的目录跳转")
        return current

    def task_dir(self, task_id: str) -> Path:
        self._valid_task_id(task_id)
        legacy = self.root / "workspaces" / task_id
        if self._linklike(legacy.parent):
            raise ConfigurationNeeded("旧任务工作区根目录不能是符号链接或目录联接")
        if self.sessions is None or legacy.exists():
            if (legacy.exists() or legacy.is_symlink()) and (self._linklike(legacy) or not legacy.is_dir()):
                raise ConfigurationNeeded("任务工作区路径不安全")
            if legacy.resolve().parent != self.root / "workspaces":
                raise ConfigurationNeeded("旧任务目录逃逸了工作区根目录")
            return legacy
        repo = self._repo_for_task(task_id)
        _, project_root = self._project_for_repo(repo, lock=False)
        return self._safe_child(project_root, "workspaces", task_id)

    def host_dir(self, task_id: str) -> str:
        local = self.task_dir(task_id)
        if self.sessions is not None and local != self.root / "workspaces" / task_id:
            return str(local)
        if self.settings.workspace_host_root:
            root = self.settings.workspace_host_root
            windows = PureWindowsPath(root)
            if windows.is_absolute():
                return str(windows / "workspaces" / task_id)
            posix = PurePosixPath(root)
            if posix.is_absolute():
                return str(posix / "workspaces" / task_id)
            raise ConfigurationNeeded("WORKSPACE_HOST_ROOT 必须是 Windows 或 POSIX 绝对路径")
        return str(local)

    def env(self, token_override: str | None = None):
        env = minimal_environment()
        env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_LFS_SKIP_SMUDGE": "1", "GIT_CONFIG_COUNT": "4", "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": "", "GIT_CONFIG_KEY_1": "core.hooksPath", "GIT_CONFIG_VALUE_1": str(self.root / "empty-hooks"), "GIT_CONFIG_KEY_2": "protocol.file.allow", "GIT_CONFIG_VALUE_2": "never", "GIT_CONFIG_KEY_3": "protocol.ext.allow", "GIT_CONFIG_VALUE_3": "never"})
        token = token_override if self.sessions is not None else (token_override or self.settings.github_token)
        if token:
            env["GIT_CONFIG_COUNT"] = "5"
            env["GIT_CONFIG_KEY_4"] = f"http.{self.settings.github_git_origin.rstrip('/')}/.extraheader"
            env["GIT_CONFIG_VALUE_4"] = "Authorization: Basic " + base64.b64encode(("x-access-token:" + token).encode()).decode()
        return env

    async def docs_baseline(self, task_id: str, repo: str, head_sha: str) -> dict:
        self._valid_task_id(task_id)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or any(p in {".", ".."} for p in repo.split("/")):
            raise ValueError("invalid repo")
        if not re.fullmatch(r"[0-9a-f]{40}", head_sha):
            raise ValueError("only pinned 40-character commit SHAs are allowed")
        if self.sessions is None:
            project_root = self.root
            cache = self.root / "repos" / (repo.replace("/", "--") + ".git")
            cache.parent.mkdir(parents=True, exist_ok=True)
            token = None
        else:
            from backend.projects import project_token
            _, project_root = self._project_for_repo(repo, lock=True)
            cache = project_root / "repo.git"
            token = project_token(self.settings, self.sessions, repo, require_active=True)
        if (cache.exists() or cache.is_symlink()) and (self._linklike(cache) or not cache.is_dir() or not cache.resolve().is_relative_to(project_root.resolve())):
            raise ConfigurationNeeded("项目代码缓存路径不安全")
        env = self.env(token)
        origin = self.settings.github_git_origin.rstrip("/") + "/" + repo + ".git"
        if not origin.startswith("https://"):
            raise ConfigurationNeeded("生产代码源必须使用 HTTPS")
        if not cache.exists():
            await command(["git", "init", "--bare", str(cache)], env=env)
            await command(["git", "--git-dir", str(cache), "remote", "add", "origin", origin], env=env)
        await command(["git", "--git-dir", str(cache), "remote", "set-url", "origin", origin], env=env)
        try:
            await command(["git", "--git-dir", str(cache), "cat-file", "-e", f"{head_sha}^{{commit}}"], env=env)
        except RuntimeError:
            await command(["git", "--git-dir", str(cache), "fetch", "--no-tags", "origin", head_sha], env=env, timeout=300)
        listing = await command(["git", "--git-dir", str(cache), "ls-tree", "-r", "--name-only", "-z", head_sha, "--", "docs/updata_log"], env=env)
        info = latest_release_docs(listing.decode("utf-8", "replace").split("\x00"))
        if not info["docs_date"]:
            return {"sha": "", "docs_date": "", "files": []}
        before = info["docs_date"] + " 23:59:59 +0000"
        sha = (await command(["git", "--git-dir", str(cache), "rev-list", "-1", f"--before={before}", head_sha], env=env)).decode().strip()
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            return {"sha": "", "docs_date": info["docs_date"], "files": info["files"]}
        return {"sha": sha, "docs_date": info["docs_date"], "files": info["files"]}

    async def prepare(self, task_id: str, repo: str, base_sha: str, head_sha: str, pr_number: int | None = None):
        self._valid_task_id(task_id)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or any(p in {".", ".."} for p in repo.split("/")):
            raise ValueError("invalid repo")
        for sha in [base_sha, head_sha]:
            if not re.fullmatch(r"[0-9a-f]{40}", sha):
                raise ValueError("only pinned 40-character commit SHAs are allowed")
        if self.sessions is None:
            project_root = self.root
            cache = self.root / "repos" / (repo.replace("/", "--") + ".git")
            cache.parent.mkdir(parents=True, exist_ok=True)
            directory = self.task_dir(task_id)
            token = None
        else:
            from backend.projects import project_token

            _, project_root = self._project_for_repo(repo, lock=True)
            cache = project_root / "repo.git"
            legacy = self.root / "workspaces" / task_id
            if legacy.exists():
                if self._linklike(legacy) or not legacy.is_dir():
                    raise ConfigurationNeeded("任务工作区路径不安全")
                directory = legacy
            else:
                directory = self._safe_child(project_root, "workspaces", task_id, create=True)
            token = project_token(self.settings, self.sessions, repo, require_active=True)
        if (cache.exists() or cache.is_symlink()) and (self._linklike(cache) or not cache.is_dir() or not cache.resolve().is_relative_to(project_root.resolve())):
            raise ConfigurationNeeded("项目代码缓存路径不安全")
        env = self.env(token)
        origin = self.settings.github_git_origin.rstrip("/") + "/" + repo + ".git"
        if not origin.startswith("https://"):
            raise ConfigurationNeeded("生产代码源必须使用 HTTPS")
        if not cache.exists():
            await command(["git", "init", "--bare", str(cache)], env=env)
            await command(["git", "--git-dir", str(cache), "remote", "add", "origin", origin], env=env)
        # Origin is reset from trusted server config, never from webhook clone_url.
        await command(["git", "--git-dir", str(cache), "remote", "set-url", "origin", origin], env=env)
        refs = [base_sha, head_sha]
        if pr_number is not None:
            refs.append(f"+refs/pull/{int(pr_number)}/head:refs/flowdeck/pr/{int(pr_number)}")
        await command(["git", "--git-dir", str(cache), "fetch", "--no-tags", "origin", *refs], env=env, timeout=300)
        # Repository attributes must not hide or rewrite source in analysis snapshots.
        if self.sessions is None:
            info_dir = cache / "info"
            info_dir.mkdir(exist_ok=True)
        else:
            info_dir = self._safe_child(project_root, "repo.git", "info", create=True)
        attributes = info_dir / "attributes"
        if attributes.is_symlink():
            raise ConfigurationNeeded("项目代码缓存属性文件路径不安全")
        attributes.write_text("* -export-ignore -export-subst\n", encoding="utf-8")
        directory.mkdir(parents=True, exist_ok=True)
        manifest = directory / "task.json"
        if manifest.is_symlink():
            raise ConfigurationNeeded("任务清单路径不安全")
        if manifest.exists():
            old = json.loads(manifest.read_text())
            if old.get("head_sha") != head_sha or old.get("base_sha") != base_sha or old.get("repo") != repo:
                raise RuntimeError("任务工作区已绑定其他提交")
        merge_base = base_sha
        if pr_number is not None:
            merge_base = (await command(["git", "--git-dir", str(cache), "merge-base", base_sha, head_sha], env=env)).decode().strip()
            if not re.fullmatch(r"[0-9a-f]{40}", merge_base):
                raise RuntimeError("Git 返回了无效的合并基准提交")
        for name, sha in [("code", head_sha), ("base", merge_base)]:
            target = directory / name
            ready = directory / f".{name}-ready"
            if ready.is_symlink():
                raise ConfigurationNeeded("任务快照标记路径不安全")
            if target.exists() or target.is_symlink():
                if self._linklike(target) or not target.is_dir() or not target.resolve().is_relative_to(directory.resolve()):
                    raise ConfigurationNeeded("任务快照目录路径不安全")
            if ready.exists() and not target.is_dir():
                raise ConfigurationNeeded("任务快照不完整")
            if not ready.exists():
                if self.sessions is None or directory == self.root / "workspaces" / task_id:
                    target.mkdir(exist_ok=True)
                else:
                    target = self._safe_child(project_root, "workspaces", task_id, name, create=True)
                archive = await command(["git", "--git-dir", str(cache), "archive", "--format=tar", sha], env=env, timeout=300)
                with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                    for member in tar:
                        destination = (target / member.name).resolve()
                        if not destination.is_relative_to(target.resolve()) or member.issym() or member.islnk() or member.isdev():
                            continue
                        tar.extract(member, target, filter="data")
                ready.write_text(sha)
        diff = await command(["git", "--git-dir", str(cache), "diff", "--no-ext-diff", "--no-textconv", merge_base, head_sha, "--"], env=env)
        changes = directory / "changes.diff"
        if changes.is_symlink():
            raise ConfigurationNeeded("任务差异文件路径不安全")
        changes.write_bytes(diff)
        file_names = (await command(["git", "--git-dir", str(cache), "diff", "--name-only", "-z", merge_base, head_sha, "--"], env=env)).decode("utf-8", "replace").split("\x00")
        commits = (await command(["git", "--git-dir", str(cache), "log", "--format=%H %s", f"{base_sha}..{head_sha}", "--"], env=env)).decode("utf-8", "replace")
        commits_file = directory / "commits.txt"
        if commits_file.is_symlink():
            raise ConfigurationNeeded("任务提交文件路径不安全")
        commits_file.write_text(commits, encoding="utf-8")
        if self.sessions is None or directory == self.root / "workspaces" / task_id:
            host_workspace = self.host_dir(task_id)
        else:
            host_workspace = str(directory)
        info = {"repo": repo, "base_sha": base_sha, "head_sha": head_sha, "merge_base": merge_base, "changed_files": [x for x in file_names if x], "diff_bytes": len(diff), "host_workspace": host_workspace}
        manifest.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        return info

    def _remove_tree(self, path: Path, *, root: Path) -> None:
        if self._linklike(path) or not path.is_dir() or not path.resolve().is_relative_to(root.resolve()):
            raise ConfigurationNeeded("可推送工作区路径不安全")

        def _on_error(func, item, exc_info):
            try:
                os.chmod(item, stat.S_IWRITE)
                func(item)
            except Exception:
                pass

        shutil.rmtree(path, onerror=_on_error)

    def _linked_worktree(self, work: Path) -> bool:
        git = work / ".git"
        if not git.is_file() or git.is_symlink():
            return False
        text = git.read_text(encoding="utf-8", errors="replace").strip()
        if not text.lower().startswith("gitdir:"):
            return False
        gitdir = Path(text.split(":", 1)[1].strip())
        if not gitdir.is_absolute():
            gitdir = work / gitdir
        try:
            resolved = gitdir.resolve()
        except OSError:
            return False
        return (resolved / "HEAD").is_file() and not self._linklike(resolved)

    def _ready_work_checkout(self, work: Path, directory: Path) -> bool:
        """True if work/ is a linked worktree. Empty or failed-clone leftovers are removed."""
        if not (work.exists() or work.is_symlink()):
            return False
        if self._linklike(work) or not work.is_dir() or not work.resolve().is_relative_to(directory.resolve()):
            raise ConfigurationNeeded("可推送工作区路径不安全")
        if self._linked_worktree(work):
            return True
        git_marker = work / ".git"
        if git_marker.exists() or git_marker.is_symlink():
            self._remove_tree(work, root=directory)
            return False
        try:
            next(work.iterdir())
        except StopIteration:
            work.rmdir()
            return False
        raise ConfigurationNeeded("可推送工作区已存在且不是 Git 仓库")

    async def prepare_worktree(self, task_id: str, repo: str, *, sha: str, branch: str):
        """Add a pushable Git worktree from the trusted project cache. Used only when a rule grants GitHub credentials."""
        self._valid_task_id(task_id)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or any(p in {".", ".."} for p in repo.split("/")):
            raise ValueError("invalid repo")
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("only pinned 40-character commit SHAs are allowed")
        if not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", branch) or ".." in branch:
            raise ConfigurationNeeded("不允许在该分支创建可推送工作区")
        directory = self.task_dir(task_id)
        work = directory / "work"
        if self._ready_work_checkout(work, directory):
            return {"branch": branch, "sha": sha}
        if self.sessions is None:
            project_root = self.root
            cache = self.root / "repos" / (repo.replace("/", "--") + ".git")
            token = None
        else:
            from backend.projects import project_token
            _, project_root = self._project_for_repo(repo, lock=True)
            cache = project_root / "repo.git"
            token = project_token(self.settings, self.sessions, repo, require_active=True)
        if (cache.exists() or cache.is_symlink()) and (self._linklike(cache) or not cache.is_dir() or not cache.resolve().is_relative_to(project_root.resolve())):
            raise ConfigurationNeeded("项目代码缓存路径不安全")
        if not cache.is_dir():
            raise ConfigurationNeeded("项目代码缓存不存在")
        env = self.env(token)
        origin = self.settings.github_git_origin.rstrip("/") + "/" + repo + ".git"
        if not origin.startswith("https://"):
            raise ConfigurationNeeded("生产代码源必须使用 HTTPS")
        # Origin is reset from trusted server config so later push does not use file transport.
        await command(["git", "--git-dir", str(cache), "remote", "set-url", "origin", origin], env=env)
        try:
            await command(["git", "--git-dir", str(cache), "cat-file", "-e", f"{sha}^{{commit}}"], env=env)
        except RuntimeError:
            await command(["git", "--git-dir", str(cache), "fetch", "--no-tags", "origin", sha], env=env, timeout=300)
        await command(["git", "--git-dir", str(cache), "worktree", "prune"], env=env)
        directory.mkdir(parents=True, exist_ok=True)
        local_branch = f"flowdeck/task/{task_id}"
        await command(
            ["git", "--git-dir", str(cache), "worktree", "add", "--force", "-B", local_branch, str(work), sha],
            env=env,
            timeout=300,
        )
        if self._linklike(work) or not work.is_dir() or not work.resolve().is_relative_to(directory.resolve()):
            raise ConfigurationNeeded("可推送工作区路径不安全")
        return {"branch": branch, "sha": sha}
