import base64
import os

import pytest

from backend.config import Settings
from backend.errors import ConfigurationNeeded
from backend import workspace
from backend.workspace import Workspaces, command, expose_task_context, minimal_environment


TASK_ID = "a" * 32


def settings(tmp_path, **values):
    return Settings(_env_file=None, data_root=tmp_path, **values)


def test_minimal_environment_excludes_unrequested_credentials(monkeypatch):
    monkeypatch.setenv("FLOWDECK_TEST_SECRET", "secret-value")
    monkeypatch.setenv("FLOWDECK_ALLOWED", "allowed-value")
    env = minimal_environment(["FLOWDECK_ALLOWED"], {"WORKFLOW_TAG": "v2"})

    assert env["FLOWDECK_ALLOWED"] == "allowed-value"
    assert env["WORKFLOW_TAG"] == "v2"
    assert "FLOWDECK_TEST_SECRET" not in env
    assert env["PATH"] == os.environ.get("PATH", os.defpath)


def test_git_environment_does_not_inherit_service_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOWDECK_AGENT_API_KEY", "agent-secret")
    env = Workspaces(settings(tmp_path, github_token="github-secret")).env()

    assert "FLOWDECK_AGENT_API_KEY" not in env
    encoded = env["GIT_CONFIG_VALUE_4"].removeprefix("Authorization: Basic ")
    assert base64.b64decode(encoded).decode() == "x-access-token:github-secret"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_KEY_2"] == "protocol.file.allow"
    assert env["GIT_CONFIG_VALUE_2"] == "never"
    assert env["GIT_CONFIG_KEY_3"] == "protocol.ext.allow"
    assert env["GIT_CONFIG_VALUE_3"] == "never"


async def test_command_uses_minimal_environment_by_default(monkeypatch):
    monkeypatch.setenv("FLOWDECK_AGENT_API_KEY", "agent-secret")
    captured = {}

    class Process:
        returncode = 0

        async def communicate(self):
            return b"ok", b""

    async def create_subprocess_exec(*_argv, **kwargs):
        captured.update(kwargs)
        return Process()

    monkeypatch.setattr(workspace.asyncio, "create_subprocess_exec", create_subprocess_exec)
    assert await command(["tool"]) == b"ok"
    assert "FLOWDECK_AGENT_API_KEY" not in captured["env"]
    assert "PATH" in captured["env"]


@pytest.mark.parametrize(
    "root,expected",
    [
        ("/srv/flowdeck", f"/srv/flowdeck/workspaces/{TASK_ID}"),
        (r"C:\flowdeck", rf"C:\flowdeck\workspaces\{TASK_ID}"),
        (r"\\server\share\flowdeck", rf"\\server\share\flowdeck\workspaces\{TASK_ID}"),
    ],
)
def test_host_dir_supports_posix_windows_and_unc_paths(tmp_path, root, expected):
    assert Workspaces(settings(tmp_path, workspace_host_root=root)).host_dir(TASK_ID) == expected


@pytest.mark.parametrize("root", ["relative/path", r"relative\path", "C:relative"])
def test_host_dir_rejects_relative_paths(tmp_path, root):
    with pytest.raises(ConfigurationNeeded, match="绝对路径"):
        Workspaces(settings(tmp_path, workspace_host_root=root)).host_dir(TASK_ID)


def test_host_dir_defaults_to_resolved_local_workspace(tmp_path):
    result = Workspaces(settings(tmp_path)).host_dir(TASK_ID)
    assert result == str(tmp_path.resolve() / "workspaces" / TASK_ID)


async def test_prepare_worktree_keeps_nonempty_nongit_work(tmp_path):
    manager = Workspaces(settings(tmp_path))
    work = manager.task_dir(TASK_ID) / "work"
    work.mkdir(parents=True)
    leftover = work / "keep.txt"
    leftover.write_text("do not delete", encoding="utf-8")
    with pytest.raises(ConfigurationNeeded, match="不是 Git"):
        await manager.prepare_worktree(TASK_ID, "acme/mobile", sha="a" * 40, branch="flowdeck/docs-v1")
    assert leftover.read_text(encoding="utf-8") == "do not delete"


def test_expose_task_context_copies_diff_into_work(tmp_path):
    root = tmp_path / "task"
    work = root / "work"
    work.mkdir(parents=True)
    (root / "changes.diff").write_text("diff --git a/file.py b/file.py\n", encoding="utf-8")
    (root / "commits.txt").write_text("a" * 40 + " feature\n", encoding="utf-8")
    expose_task_context(root)
    assert (work / ".flowdeck-changes.diff").read_text(encoding="utf-8") == "diff --git a/file.py b/file.py\n"
    assert (work / ".flowdeck-commits.txt").read_text(encoding="utf-8") == "a" * 40 + " feature\n"


def test_expose_task_context_skips_when_work_missing(tmp_path):
    root = tmp_path / "task"
    root.mkdir()
    (root / "changes.diff").write_text("diff\n", encoding="utf-8")
    expose_task_context(root)
    assert not (root / ".flowdeck-changes.diff").exists()
