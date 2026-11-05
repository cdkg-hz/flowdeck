from __future__ import annotations

import re
import subprocess
from pathlib import Path, PurePosixPath

from backend.errors import ConfigurationNeeded


def _git(argv: list[str], *, cwd: Path | None, env: dict, timeout: int = 120) -> str:
    result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, timeout=timeout)
    if result.returncode:
        message = result.stderr.decode("utf-8", "replace")[:1500]
        raise RuntimeError(f"git 失败：{message}")
    return result.stdout.decode("utf-8", "replace")


def _unquote_git_path(name: str) -> str:
    """Decode Git's C-style quoted paths (octal bytes) back to UTF-8 POSIX paths."""
    text = name.strip()
    if not text:
        return ""
    quoted = len(text) >= 2 and text[0] == '"' and text[-1] == '"'
    if quoted:
        text = text[1:-1]
    if quoted or "\\" in text:
        out = bytearray()
        i = 0
        while i < len(text):
            ch = text[i]
            if ch != "\\" or i + 1 >= len(text):
                out.extend(ch.encode("utf-8"))
                i += 1
                continue
            nxt = text[i + 1]
            if nxt in "01234567":
                value, count, j = 0, 0, i + 1
                while j < len(text) and count < 3 and text[j] in "01234567":
                    value = value * 8 + int(text[j])
                    j += 1
                    count += 1
                out.append(value)
                i = j
                continue
            out.extend({"\\": b"\\", '"': b'"', "n": b"\n", "t": b"\t", "r": b"\r"}.get(nxt, nxt.encode("utf-8")))
            i += 2
        text = out.decode("utf-8", "replace")
    return PurePosixPath(text.replace("\\", "/")).as_posix()


def _indexed_paths(output: str) -> list[str]:
    chunks = output.split("\0") if "\0" in output else output.splitlines()
    return [path for chunk in chunks if (path := _unquote_git_path(chunk))]


def _relative_doc_paths(work: Path, paths: list[str]) -> list[str]:
    validated = []
    for item in paths:
        posix = str(item).strip().replace("\\", "/")
        relative = PurePosixPath(posix)
        if not posix or relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError(f"文档路径必须是工作目录内的相对路径：{item}")
        target = (work / posix).resolve()
        if not target.is_relative_to(work) or not target.is_file() or target.is_symlink():
            raise RuntimeError(f"Agent 声明的文档路径不存在或越界：{posix}")
        validated.append(relative.as_posix())
    return validated


def commit_and_push(work: Path, env: dict, branch: str, message: str, paths: list[str]) -> dict:
    """Stage only declared release_docs.paths, commit, and push HEAD. Flowdeck-only; the agent never holds credentials."""
    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", branch) or ".." in branch:
        raise ConfigurationNeeded("不允许推送到该分支")
    work = work.resolve()
    if not work.is_dir() or work.is_symlink():
        raise RuntimeError("尚未准备可推送工作区")
    staged_paths = _relative_doc_paths(work, paths)
    if not staged_paths:
        raise RuntimeError("没有可提交的文档路径")
    _git(["git", "add", "--", *staged_paths], cwd=work, env=env)
    indexed = _indexed_paths(_git(["git", "-c", "core.quotepath=false", "diff", "--cached", "--name-only", "-z"], cwd=work, env=env))
    extra = [name for name in indexed if name not in set(staged_paths)]
    if extra:
        raise RuntimeError("暂存区包含未声明的文件，已中止推送：" + "、".join(extra[:10]))
    if indexed:
        commit_env = {**env, "GIT_AUTHOR_NAME": "Flowdeck Agent", "GIT_AUTHOR_EMAIL": "flowdeck@local",
                      "GIT_COMMITTER_NAME": "Flowdeck Agent", "GIT_COMMITTER_EMAIL": "flowdeck@local"}
        _git(["git", "commit", "-m", message.strip()[:200]], cwd=work, env=commit_env)
    try:
        _git(["git", "push", "origin", f"HEAD:refs/heads/{branch}"], cwd=work, env=env, timeout=180)
    except RuntimeError as exc:
        text = str(exc).lower()
        if any(token in text for token in ("protected branch", "pre-receive hook declined", "cannot update ref", "not allowed to push")):
            raise ConfigurationNeeded("无法推送到默认分支，请检查 PAT 与分支保护") from exc
        raise
    sha = _git(["git", "rev-parse", "HEAD"], cwd=work, env=env).strip()
    return {"pushed": True, "branch": branch, "sha": sha}
