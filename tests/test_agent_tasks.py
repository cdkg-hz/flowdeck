import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from backend.agent_tasks import (
    RELEASE_TASK_RULES,
    TASK_SYSTEM_RULES,
    AgentRuleInput,
    allowed_push_branch,
    choose_release_compare,
    latest_release_docs,
    matching_agent_rules,
    normalize_agent_rules,
    schedule_agent_rules,
    task_user_payload,
)
from backend.copilot_runtime import SYSTEM_RULES
from backend.config import AgentRuleConfig, ProjectConfig
from backend.db import Job, ProjectAutomation, Record
from backend.integrations import ProjectAutomationInput
from backend.schemas import AgentTaskResult
from backend.service import schedule_job, upsert_github
from backend.projects import project_config
from tests.conftest import pull_payload


AGENT_ID = "f" * 32
RULE_ID = "b" * 32


def _rule(**changes):
    value = {
        "id": RULE_ID,
        "name": "审查后合并",
        "event": "pull_request",
        "agent_id": AGENT_ID,
        "prompt": "审查这个 PR。通过则合并。",
        "include_repository": True,
        "include_github_credentials": False,
        "skill_ids": ["releasing-version-docs"],
        "enabled": True,
    }
    value.update(changes)
    return value


def test_system_rules_forbid_exec_command_and_outrank_skills():
    for rules in (TASK_SYSTEM_RULES, SYSTEM_RULES):
        assert "exec_command" in rules
        assert "不能覆盖本规则" in rules
        assert "系统命令" in rules
        assert "文件" in rules
    assert ".flowdeck-changes.diff" in TASK_SYSTEM_RULES
    assert ".flowdeck-commits.txt" in TASK_SYSTEM_RULES
    assert "node_modules" in TASK_SYSTEM_RULES
    assert "Task" in RELEASE_TASK_RULES
    assert "git" in RELEASE_TASK_RULES
    assert "baseline_docs" in RELEASE_TASK_RULES
    assert "exec_command" in RELEASE_TASK_RULES
    assert "文件读取与写入工具" in TASK_SYSTEM_RULES


def test_latest_release_docs_picks_newest_dated_manuals():
    info = latest_release_docs([
        "docs/updata_log/更新日志_2026-08-11.md",
        "docs/updata_log/产品功能介绍与使用手册_2026-09-10.md",
        "docs/updata_log/现状技术实现与架构说明书_2026-09-10.md",
        "docs/updata_log/changelog.html",
        "docs/updata_log/产品功能介绍与使用手册_2026-06-30.md",
    ])
    assert info["docs_date"] == "2026-09-10"
    assert info["files"] == [
        "docs/updata_log/产品功能介绍与使用手册_2026-09-10.md",
        "docs/updata_log/现状技术实现与架构说明书_2026-09-10.md",
    ]


def test_choose_release_compare_prefers_docs_baseline_over_previous_release():
    head = "a" * 40
    docs = "c" * 40
    prev = "b" * 40
    chosen = choose_release_compare(
        configured_sha="",
        docs_sha=docs,
        docs_date="2026-09-10",
        previous_sha=prev,
        previous_tag="1.0.1",
        head_sha=head,
        configured_tag="",
    )
    assert chosen["base_sha"] == docs
    assert chosen["compare_source"] == "docs_date"
    assert chosen["docs_date"] == "2026-09-10"

    same_docs = choose_release_compare(
        configured_sha="",
        docs_sha=head,
        docs_date="2026-09-15",
        previous_sha=prev,
        previous_tag="1.0.1",
        head_sha=head,
        configured_tag="",
    )
    assert same_docs["base_sha"] == prev
    assert same_docs["compare_source"] == "previous_release"

    forced = choose_release_compare(
        configured_sha=prev,
        docs_sha=docs,
        docs_date="2026-09-10",
        previous_sha=prev,
        previous_tag="v1",
        head_sha=head,
        configured_tag="v1",
    )
    assert forced["compare_source"] == "configured_tag"
    assert forced["base_sha"] == prev


def test_task_user_payload_release_includes_docs_baseline():
    record = SimpleNamespace(
        kind="release", number=0, title="v2", body="body", url="https://github.com/acme/mobile/releases/tag/v2",
        labels=[], repo="acme/mobile",
        details={"tag": "v2", "published_at": "2026-09-13T10:00:00Z", "head_ref": "", "base_ref": ""},
    )
    payload = task_user_payload(
        record=record, rule=_rule(event="release"),
        workspace={
            "head_sha": "a" * 40,
            "base_sha": "c" * 40,
            "host_workspace": "/secret",
            "baseline_date": "2026-09-10",
            "baseline_docs": ["docs/updata_log/产品功能介绍与使用手册_2026-09-10.md"],
            "compare_source": "docs_date",
            "previous_tag": "v1",
        },
        skills=[], include_repository=False, clone_url="", event_action="published",
    )
    assert payload["release"] == {
        "tag": "v2",
        "docs_date": "2026-09-13",
        "baseline_date": "2026-09-10",
        "baseline_docs": ["docs/updata_log/产品功能介绍与使用手册_2026-09-10.md"],
        "compare_source": "docs_date",
        "previous_tag": "v1",
    }
    assert "host_workspace" not in payload.get("workspace", {})


def test_normalize_agent_rules_rejects_bad_event_and_over_limit():
    with pytest.raises(ValidationError, match="issues"):
        AgentRuleInput(**_rule(event="push"))
    rules = [AgentRuleInput(**_rule(id="", name=f"规则{i}", prompt=f"做第 {i} 件事")) for i in range(21)]
    with pytest.raises(ValueError, match="最多 20"):
        normalize_agent_rules(rules)


def test_credentials_imply_repository_on_save():
    body = ProjectAutomationInput(
        agent_rules=[AgentRuleInput(**_rule(include_repository=False, include_github_credentials=True))]
    )
    assert body.agent_rules[0].include_repository is True
    assert body.agent_rules[0].include_github_credentials is True


def test_matching_agent_rules_skips_release_webhook():
    config = ProjectConfig(repo="acme/mobile", agent_rules=[AgentRuleConfig(**_rule(event="release", name="总结并发版", prompt="按技能总结并请求推送"))])
    assert matching_agent_rules(config, "release", "published", trigger=True) == []
    assert matching_agent_rules(config, "release", "released", trigger=True) == []
    assert matching_agent_rules(config, "pr", "opened", trigger=True) == []


def test_published_release_does_not_schedule_agent_task(setup_db):
    settings, sessions, _ = setup_db
    config = ProjectConfig(repo="acme/mobile", agent_rules=[AgentRuleConfig(**_rule(event="release", name="总结并发版", prompt="按技能总结并请求推送"))])
    with sessions() as db:
        upsert_github(db, config, "release", {
            "id": 99, "tag_name": "v2", "name": "v2", "draft": False, "prerelease": False,
            "created_at": "2026-09-13T10:00:00Z", "published_at": "2026-09-13T10:00:00Z", "html_url": "https://github.com/acme/mobile/releases/tag/v2", "assets": [],
        }, "published", canonical=True)
        db.commit()
        assert not list(db.scalars(select(Job).where(Job.kind == "agent_task")))


def test_allowed_push_branch_uses_prepare_default_branch():
    record = SimpleNamespace(kind="release", details={"tag": "v2", "base_ref": "main"})
    assert allowed_push_branch({"push_branch": "main"}, record) == "main"
    assert allowed_push_branch({}, record) == "flowdeck/docs-v2"


def test_matching_and_schedule_skip_closed_draft_and_sync(setup_db):
    settings, sessions, _ = setup_db
    config = ProjectConfig(repo="acme/mobile", agent_rules=[AgentRuleConfig(**_rule())])
    with sessions() as db:
        record = upsert_github(db, config, "pr", pull_payload(draft=True), "opened", canonical=True)
        db.commit()
        assert not list(db.scalars(select(Job).where(Job.kind == "agent_task")))

        upsert_github(db, config, "pr", pull_payload(), "ready_for_review", canonical=True)
        db.commit()
        jobs = list(db.scalars(select(Job).where(Job.kind == "agent_task")))
        assert len(jobs) == 1
        assert jobs[0].payload["github_action"] == "ready_for_review"
        assert jobs[0].payload["prompt"] == "审查这个 PR。通过则合并。"
        assert "github_token" not in json.dumps(jobs[0].payload)

        upsert_github(db, config, "pr", pull_payload("d" * 40), "synchronize", canonical=True)
        db.commit()
        assert len(list(db.scalars(select(Job).where(Job.kind == "agent_task")))) == 2

        upsert_github(db, config, "pr", pull_payload(state="closed"), "closed", canonical=True)
        db.commit()
        assert matching_agent_rules(config, "pr", "closed", trigger=True) == []
        closed_jobs = list(db.scalars(select(Job).where(Job.kind == "agent_task", Job.status == "queued")))
        assert not closed_jobs

        issue = upsert_github(db, config, "issue", {"id": 9, "number": 3, "title": "bug", "state": "open", "html_url": "https://github.com/acme/mobile/issues/3"}, "opened", canonical=True)
        schedule_agent_rules(db, config, issue, "opened", trigger=True)
        db.commit()
        assert not list(db.scalars(select(Job).where(Job.kind == "agent_task", Job.record_id == issue.id)))

        issue_config = ProjectConfig(repo="acme/mobile", agent_rules=[AgentRuleConfig(**_rule(event="issues", prompt="处理这个 Issue"))])
        tracked = upsert_github(db, issue_config, "issue", {"id": 10, "number": 4, "title": "fix crash", "state": "open", "html_url": "https://github.com/acme/mobile/issues/4"}, "opened")
        db.commit()
        issue_jobs = list(db.scalars(select(Job).where(Job.kind == "agent_task", Job.record_id == tracked.id)))
        assert len(issue_jobs) == 1
        upsert_github(db, issue_config, "issue", {"id": 10, "number": 4, "title": "fix crash", "state": "closed"}, "closed")
        db.commit()
        assert db.get(Job, issue_jobs[0].id).status == "superseded"
        assert matching_agent_rules(issue_config, "issue", "closed", trigger=True) == []


def test_task_payload_omits_credentials_and_optional_repo():
    record = SimpleNamespace(
        kind="pr", number=7, title="Fix", body="body", url="https://github.com/acme/mobile/pull/7",
        labels=["P1"], repo="acme/mobile",
        details={"head_ref": "fix/retry", "base_ref": "main", "tag": ""},
    )
    hidden = task_user_payload(
        record=record, rule=_rule(include_repository=False), workspace=None, skills=[],
        include_repository=False, clone_url="https://github.com/acme/mobile.git", event_action="opened",
    )
    assert "repository" not in hidden
    assert "github_credentials" not in hidden
    assert "github_token" not in json.dumps(hidden)
    shown = task_user_payload(
        record=record, rule=_rule(), workspace={"head_sha": "a" * 40, "host_workspace": "/secret"}, skills=[{"id": "releasing-version-docs"}],
        include_repository=True, clone_url="https://github.com/acme/mobile.git", event_action="opened",
    )
    assert shown["repository"]["clone_url"] == "https://github.com/acme/mobile.git"
    assert "github_credentials" not in shown
    assert "host_workspace" not in shown["workspace"]
    assert shown["action"] == "opened"
    assert "merge_requested" in shown["required_result"]["pull_request"]


class MergeGitHub:
    def __init__(self, fail_status: int | None = None, already_merged: bool = False):
        self.calls = []
        self.fail_status = fail_status
        self.already_merged = already_merged

    async def pull(self, repo, number):
        return {"number": number, "merged": self.already_merged, "merged_at": "2026-09-15T00:00:00Z" if self.already_merged else None, "merge_commit_sha": "c" * 40 if self.already_merged else ""}

    async def merge(self, repo, number, sha, method):
        self.calls.append((repo, number, sha, method))
        if self.fail_status:
            request = httpx.Request("PUT", f"https://api.github.com/repos/{repo}/pulls/{number}/merge")
            raise httpx.HTTPStatusError("blocked", request=request, response=httpx.Response(self.fail_status, request=request))
        return {"merged": True, "sha": "c" * 40}


async def test_execute_agent_actions_merges_with_exact_sha(tmp_path):
    from backend.agent_tasks import execute_agent_actions
    from backend.schemas import AgentTaskResult
    github = MergeGitHub()
    result = AgentTaskResult(summary="done", outcome="completed",
                             pull_request={"number": 7, "merge_requested": True})
    executed = await execute_agent_actions(github, "acme/mobile", result, event="pull_request", pull_number=7,
                                           head_sha="a" * 40, merge_method="squash", work_root=tmp_path / "work",
                                           git_env={}, allowed_branch="fix/retry", allow_write=True, rule_name="auto-merge")
    assert github.calls == [("acme/mobile", 7, "a" * 40, "squash")]
    assert executed["pull_request"] == {"number": 7, "merged": True, "merge_commit_sha": "c" * 40}

    already = MergeGitHub(already_merged=True)
    executed = await execute_agent_actions(already, "acme/mobile", result, event="pull_request", pull_number=7,
                                           head_sha="a" * 40, merge_method="squash", work_root=tmp_path / "work",
                                           git_env={}, allowed_branch="fix/retry", allow_write=True, rule_name="auto-merge")
    assert already.calls == []
    assert executed["pull_request"]["merged"] is True


async def test_execute_agent_actions_rejects_merge_without_write(tmp_path):
    from backend.agent_tasks import execute_agent_actions
    from backend.schemas import AgentTaskResult
    result = AgentTaskResult(summary="done", outcome="completed",
                             pull_request={"number": 7, "merge_requested": True})
    with pytest.raises(RuntimeError, match="写权限"):
        await execute_agent_actions(MergeGitHub(), "acme/mobile", result, event="pull_request", pull_number=7,
                                    head_sha="a" * 40, merge_method="squash", work_root=tmp_path / "work",
                                    git_env={}, allowed_branch="fix/retry", allow_write=False, rule_name="r")


async def test_execute_agent_actions_records_merge_refusal(tmp_path):
    from backend.agent_tasks import execute_agent_actions
    from backend.schemas import AgentTaskResult
    github = MergeGitHub(fail_status=405)
    result = AgentTaskResult(summary="done", outcome="completed",
                             pull_request={"number": 7, "merge_requested": True})
    executed = await execute_agent_actions(github, "acme/mobile", result, event="pull_request", pull_number=7,
                                           head_sha="a" * 40, merge_method="squash", work_root=tmp_path / "work",
                                           git_env={}, allowed_branch="fix/retry", allow_write=True, rule_name="r")
    assert executed["pull_request"]["merged"] is False
    assert "405" in executed["pull_request"]["message"]


async def test_execute_agent_actions_propagates_wait_and_unknown(tmp_path):
    from backend.agent_tasks import execute_agent_actions
    from backend.errors import OutcomeUnknown, WaitForDependency
    from backend.schemas import AgentTaskResult
    result = AgentTaskResult(summary="done", outcome="completed",
                             pull_request={"number": 7, "merge_requested": True})

    class WaitingGitHub(MergeGitHub):
        async def merge(self, repo, number, sha, method):
            self.calls.append((repo, number, sha, method))
            raise WaitForDependency("GitHub 尚未允许合并")

    with pytest.raises(WaitForDependency, match="尚未允许合并"):
        await execute_agent_actions(WaitingGitHub(), "acme/mobile", result, event="pull_request", pull_number=7,
                                    head_sha="a" * 40, merge_method="squash", work_root=tmp_path / "work",
                                    git_env={}, allowed_branch="fix/retry", allow_write=True, rule_name="r")

    class UnknownGitHub(MergeGitHub):
        async def merge(self, repo, number, sha, method):
            raise OutcomeUnknown("合并请求结果未知")

    with pytest.raises(OutcomeUnknown, match="结果未知"):
        await execute_agent_actions(UnknownGitHub(), "acme/mobile", result, event="pull_request", pull_number=7,
                                    head_sha="a" * 40, merge_method="squash", work_root=tmp_path / "work",
                                    git_env={}, allowed_branch="fix/retry", allow_write=True, rule_name="r")


async def test_execute_agent_actions_ignores_cross_event_fields(tmp_path, monkeypatch):
    from backend import agent_tasks
    from backend.schemas import AgentTaskResult
    work = tmp_path / "work"
    (work / "docs").mkdir(parents=True)
    (work / "docs" / "notes.md").write_text("v1", encoding="utf-8")
    github = MergeGitHub()
    pushes = []
    monkeypatch.setattr("backend.agent_actions.commit_and_push", lambda *args, **kwargs: pushes.append(args) or {"pushed": True, "branch": "x", "sha": "d" * 40})
    mixed = AgentTaskResult(summary="done", outcome="completed",
                            pull_request={"number": 7, "merge_requested": True},
                            release_docs={"paths": ["docs/notes.md"], "push_requested": True})
    executed = await agent_tasks.execute_agent_actions(github, "acme/mobile", mixed, event="issues", pull_number=7,
                                                       head_sha="a" * 40, merge_method="squash", work_root=work,
                                                       git_env={}, allowed_branch="fix/retry", allow_write=True, rule_name="r")
    assert github.calls == []
    assert pushes == []
    assert executed == {"pull_request": None, "release_docs": None}

    executed = await agent_tasks.execute_agent_actions(github, "acme/mobile", mixed, event="pull_request", pull_number=7,
                                                       head_sha="a" * 40, merge_method="squash", work_root=work,
                                                       git_env={}, allowed_branch="fix/retry", allow_write=True, rule_name="r")
    assert github.calls == [("acme/mobile", 7, "a" * 40, "squash")]
    assert pushes == []
    assert executed["release_docs"] is None
    github.calls.clear()
    executed = await agent_tasks.execute_agent_actions(github, "acme/mobile", mixed, event="release", pull_number=None,
                                                       head_sha="", merge_method="squash", work_root=work,
                                                       git_env={}, allowed_branch="flowdeck/docs-v1", allow_write=True, rule_name="发布文档")
    assert github.calls == []
    assert executed["pull_request"] is None
    assert executed["release_docs"]["pushed"] is True


async def test_execute_agent_actions_validates_doc_paths(tmp_path, monkeypatch):
    from backend import agent_tasks
    from backend.schemas import AgentTaskResult
    work = tmp_path / "work"
    (work / "docs").mkdir(parents=True)
    (work / "docs" / "notes.md").write_text("v1", encoding="utf-8")
    pushes = []

    def fake_push(root, env, branch, message, paths):
        pushes.append((root, branch, message, list(paths)))
        return {"pushed": True, "branch": branch, "sha": "d" * 40}

    monkeypatch.setattr("backend.agent_actions.commit_and_push", fake_push)
    result = AgentTaskResult(summary="done", outcome="completed",
                             release_docs={"paths": ["docs/notes.md"], "push_requested": True})
    executed = await agent_tasks.execute_agent_actions(None, "acme/mobile", result, event="release", pull_number=None,
                                                       head_sha="", merge_method="squash", work_root=work,
                                                       git_env={}, allowed_branch="flowdeck/docs-v1", allow_write=True, rule_name="发布文档")
    assert pushes and pushes[0][1] == "flowdeck/docs-v1"
    assert pushes[0][3] == ["docs/notes.md"]
    assert executed["release_docs"] == {"paths": ["docs/notes.md"], "pushed": True, "branch": "flowdeck/docs-v1", "commit_sha": "d" * 40}

    missing = AgentTaskResult(summary="done", outcome="completed",
                              release_docs={"paths": ["docs/absent.md"], "push_requested": True})
    with pytest.raises(RuntimeError, match="不存在或越界"):
        await agent_tasks.execute_agent_actions(None, "acme/mobile", missing, event="release", pull_number=None,
                                                head_sha="", merge_method="squash", work_root=work,
                                                git_env={}, allowed_branch="flowdeck/docs-v1", allow_write=True, rule_name="发布文档")

    (work / ".flowdeck-changes.diff").write_text("diff\n", encoding="utf-8")
    reserved = AgentTaskResult(summary="done", outcome="completed",
                               release_docs={"paths": [".flowdeck-changes.diff"], "push_requested": True})
    with pytest.raises(RuntimeError, match="比较材料"):
        await agent_tasks.execute_agent_actions(None, "acme/mobile", reserved, event="release", pull_number=None,
                                                head_sha="", merge_method="squash", work_root=work,
                                                git_env={}, allowed_branch="flowdeck/docs-v1", allow_write=True, rule_name="发布文档")
    assert pushes[0][3] == ["docs/notes.md"]


def test_task_user_payload_has_no_credentials(setup_db):
    from types import SimpleNamespace
    from backend.agent_tasks import task_user_payload
    record = SimpleNamespace(kind="pr", number=7, title="t", body="b", url="u", labels=[], repo="acme/mobile",
                             details={"head_ref": "fix/retry", "base_ref": "main", "tag": ""})
    payload = task_user_payload(record=record, rule={"prompt": "p", "event": "pull_request"}, workspace=None, skills=[],
                                include_repository=True, clone_url="https://github.com/acme/mobile.git", event_action="opened")
    assert "github_credentials" not in payload
    assert payload["repository"]["clone_url"] == "https://github.com/acme/mobile.git"
    assert "merge_requested" in payload["required_result"]["pull_request"]


def test_commit_and_push_protected_branch_needs_configuration(monkeypatch, tmp_path):
    from backend.agent_actions import commit_and_push
    from backend.errors import ConfigurationNeeded

    def fake_git(argv, **kwargs):
        if "diff" in argv:
            return "docs/notes.md\0"
        if "push" in argv:
            raise RuntimeError("git 失败：protected branch hook declined")
        return "a" * 40 + "\n"

    monkeypatch.setattr("backend.agent_actions._git", fake_git)
    work = tmp_path / "work"
    work.mkdir()
    (work / "docs").mkdir()
    (work / "docs" / "notes.md").write_text("notes\n", encoding="utf-8")
    with pytest.raises(ConfigurationNeeded, match="默认分支"):
        commit_and_push(work, {}, "main", "docs: notes", ["docs/notes.md"])


def test_commit_and_push_never_force(monkeypatch, tmp_path):
    from backend.agent_actions import commit_and_push
    calls = []

    def fake_git(argv, **kwargs):
        calls.append(list(argv))
        if "diff" in argv:
            return "docs/notes.md\0"
        if argv[:2] == ["git", "status"]:
            return "M  docs/notes.md\n"
        return "a" * 40 + "\n"

    monkeypatch.setattr("backend.agent_actions._git", fake_git)
    work = tmp_path / "work"
    work.mkdir()
    (work / "docs").mkdir()
    (work / "docs" / "notes.md").write_text("notes\n", encoding="utf-8")
    result = commit_and_push(work, {}, "fix/retry", "docs: notes", ["docs/notes.md"])
    assert result == {"pushed": True, "branch": "fix/retry", "sha": "a" * 40}
    add_calls = [argv for argv in calls if argv[:2] == ["git", "add"]]
    assert add_calls == [["git", "add", "--", "docs/notes.md"]]
    assert any("push" in argv for argv in calls)
    assert all("-A" not in argv and "--force" not in argv and "-f" not in argv for argv in calls)
    diff_calls = [argv for argv in calls if "diff" in argv]
    assert diff_calls
    assert "-z" in diff_calls[0]
    assert "core.quotepath=false" in diff_calls[0]


def test_commit_and_push_aborts_if_index_has_other_files(monkeypatch, tmp_path):
    from backend.agent_actions import commit_and_push
    calls = []

    def fake_git(argv, **kwargs):
        calls.append(list(argv))
        if "diff" in argv:
            return "docs/notes.md\0secret.env\0"
        return ""

    monkeypatch.setattr("backend.agent_actions._git", fake_git)
    work = tmp_path / "work"
    work.mkdir()
    (work / "docs").mkdir()
    (work / "docs" / "notes.md").write_text("notes\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="未声明"):
        commit_and_push(work, {}, "fix/retry", "docs: notes", ["docs/notes.md"])
    assert any("commit" in argv for argv in calls) is False
    assert any("push" in argv for argv in calls) is False


def test_commit_and_push_matches_octal_quoted_unicode_paths(monkeypatch, tmp_path):
    from backend.agent_actions import commit_and_push
    relative = "docs/updata_log/产品功能介绍与使用手册_2026-09-15.md"
    quoted = '"docs/updata_log/\\344\\272\\247\\345\\223\\201\\345\\212\\237\\350\\203\\275\\344\\273\\213\\347\\273\\215\\344\\270\\216\\344\\275\\277\\347\\224\\250\\346\\211\\213\\345\\206\\214_2026-09-15.md"'

    def fake_git(argv, **kwargs):
        if "diff" in argv:
            return quoted + "\n"
        return "a" * 40 + "\n"

    monkeypatch.setattr("backend.agent_actions._git", fake_git)
    work = tmp_path / "work"
    (work / "docs" / "updata_log").mkdir(parents=True)
    (work / relative).write_text("notes\n", encoding="utf-8")
    result = commit_and_push(work, {}, "main", "docs: notes", [relative])
    assert result["pushed"] is True


def test_unquote_git_path_decodes_c_style_octal():
    from backend.agent_actions import _unquote_git_path
    quoted = '"docs/updata_log/\\344\\272\\247\\345\\223\\201\\345\\212\\237\\350\\203\\275\\344\\273\\213\\347\\273\\215\\344\\270\\216\\344\\275\\277\\347\\224\\250\\346\\211\\213\\345\\206\\214_2026-09-15.md"'
    assert _unquote_git_path(quoted) == "docs/updata_log/产品功能介绍与使用手册_2026-09-15.md"
    assert _unquote_git_path("docs/updata_log/changelog.html") == "docs/updata_log/changelog.html"


class TaskAgent:
    def __init__(self, payload, *, invalid=False):
        self.payload = payload
        self.invalid = invalid
        self.last_input = None
        self.last_instructions = ""

    async def execute(self, task_type, task_id, instructions, input_text, state, save):
        self.last_input = json.loads(input_text)
        self.last_instructions = instructions
        await save({"messages": [{"role": "user", "content": input_text}]})
        if self.invalid:
            return "not-json"
        return json.dumps(self.payload)


class FakeWorkspace:
    def __init__(self, root):
        self.root = root
        self.prepared = []
        self.docs_sha = ""
        self.docs_date = ""
        self.docs_files = []

    def task_dir(self, job_id):
        return self.root / job_id

    async def docs_baseline(self, task_id, repo, head_sha):
        return {"sha": self.docs_sha, "docs_date": self.docs_date, "files": list(self.docs_files)}

    async def prepare(self, job_id, repo, base, head, pr_number=None):
        self.prepared.append({"base": base, "head": head, "pr_number": pr_number})
        path = self.task_dir(job_id)
        (path / "code").mkdir(parents=True, exist_ok=True)
        (path / "code" / "file.py").write_text("print('ok')\n")
        (path / "changes.diff").write_text(f"{base} {head}\n", encoding="utf-8")
        (path / "commits.txt").write_text(head + " feature\n")
        return {"repo": repo, "head_sha": head, "base_sha": base, "changed_files": ["file.py"], "diff_bytes": 12, "host_workspace": str(path)}


class TaskWorkspace(FakeWorkspace):
    def env(self, token_override=None):
        return {"PATH": "x"}

    async def prepare_worktree(self, task_id, repo, *, sha, branch):
        (self.task_dir(task_id) / "work").mkdir(parents=True, exist_ok=True)
        return {"branch": branch, "sha": sha}


class WorkspaceWithRepoSkills(TaskWorkspace):
    async def prepare(self, job_id, repo, base, head, pr_number=None):
        workspace = await super().prepare(job_id, repo, base, head, pr_number)
        decoy = self.task_dir(job_id) / "code" / ".cursor" / "skills" / "releasing-version-docs"
        decoy.mkdir(parents=True)
        (decoy / "SKILL.md").write_text("# 仓库里的 decoy\n", encoding="utf-8")
        return workspace


def _install_rule(sessions, **changes):
    with sessions() as db:
        item = db.get(ProjectAutomation, "a" * 32)
        item.config = {**(item.config or {}), "agent_rules": [_rule(**changes)]}
        db.commit()


async def test_agent_task_payload_uses_uploaded_skills_not_workspace(setup_db, tmp_path):
    from backend.worker import Worker
    from tests.test_worker import FakeGitHub
    settings, sessions, config = setup_db
    config.write_text("projects:\n  - repo: acme/mobile\n", encoding="utf-8")
    _install_rule(sessions)
    gh = FakeGitHub()
    agent = TaskAgent({"summary": "ok", "outcome": "completed", "pull_request": {"merge_requested": False, "number": 7}})
    worker = Worker(settings, sessions=sessions, github=gh, agent=agent, workspaces=WorkspaceWithRepoSkills(tmp_path / "workspaces"))
    with sessions() as db:
        upsert_github(db, project_config(settings, db, "acme/mobile"), "pr", gh.pr, "opened", canonical=True)
        db.commit()
    await worker.run_job(worker.claim())
    skills = agent.last_input["skills"]
    assert [item["id"] for item in skills] == ["releasing-version-docs"]
    body = skills[0]["files"][0]["content"]
    assert "发版统御" in body
    assert "decoy" not in body
    assert ".cursor/skills" not in skills[0]["files"][0]["path"]


async def test_agent_task_stores_executed_merge(setup_db, tmp_path):
    from backend.worker import Worker
    from tests.test_worker import FakeGitHub
    settings, sessions, config = setup_db
    config.write_text("projects:\n  - repo: acme/mobile\n", encoding="utf-8")
    _install_rule(sessions)
    gh = FakeGitHub()
    agent = TaskAgent({"summary": "请求合并 PR", "outcome": "completed", "pull_request": {"merge_requested": True, "number": 7}})
    worker = Worker(settings, sessions=sessions, github=gh, agent=agent, workspaces=TaskWorkspace(tmp_path / "workspaces"))
    with sessions() as db:
        upsert_github(db, project_config(settings, db, "acme/mobile"), "pr", gh.pr, "opened", canonical=True)
        db.commit()
    job_id = worker.claim()
    await worker.run_job(job_id)
    assert "github_credentials" not in agent.last_input
    assert agent.last_input["repository"]["full_name"] == "acme/mobile"
    assert gh.merges == [gh.pr["head"]["sha"]]
    with sessions() as db:
        record = db.scalar(select(Record))
        job = db.get(Job, job_id)
        result = record.details["agent_results"][RULE_ID]
        assert job.status == "completed"
        assert result["executed"]["pull_request"]["merged"] is True
        assert "test-token" not in json.dumps(job.result)


async def test_agent_task_passes_secret_values_to_codex(setup_db, tmp_path):
    from backend.copilot_runtime import CopilotAdapter
    from backend.worker import Worker
    from tests.test_worker import FakeGitHub
    settings, sessions, config = setup_db
    config.write_text("projects:\n  - repo: acme/mobile\n", encoding="utf-8")
    _install_rule(sessions)

    class Rec(CopilotAdapter):
        def __init__(self):
            self.kwargs = None

        async def execute(self, *args, **kwargs):
            self.kwargs = kwargs
            return json.dumps({"summary": "ok", "outcome": "completed", "pull_request": {"merge_requested": False, "number": 7}})

        async def close(self):
            pass

    rec = Rec()
    gh = FakeGitHub()
    worker = Worker(settings, sessions=sessions, github=gh, agent=rec, workspaces=TaskWorkspace(tmp_path / "workspaces"))
    with sessions() as db:
        upsert_github(db, project_config(settings, db, "acme/mobile"), "pr", gh.pr, "opened", canonical=True)
        db.commit()
    await worker.run_job(worker.claim())
    assert rec.kwargs is not None
    assert "test-agent-key" in rec.kwargs["secret_values"]
    assert "test-token" in rec.kwargs["secret_values"]


async def test_agent_task_records_and_clears_agent_activity(setup_db, tmp_path):
    from backend.copilot_runtime import CopilotAdapter
    from backend.worker import Worker
    from tests.test_worker import FakeGitHub
    settings, sessions, config = setup_db
    config.write_text("projects:\n  - repo: acme/mobile\n", encoding="utf-8")
    _install_rule(sessions)

    class Rec(CopilotAdapter):
        def __init__(self):
            self.seen = None

        async def execute(self, *args, **kwargs):
            await kwargs["on_progress"]("正在写入 docs/notes.md")
            with sessions() as db:
                self.seen = dict(db.scalar(select(Record)).details.get("agent_activity") or {})
            return json.dumps({"summary": "ok", "outcome": "completed", "pull_request": {"merge_requested": False, "number": 7}})

        async def close(self):
            pass

    rec = Rec()
    gh = FakeGitHub()
    worker = Worker(settings, sessions=sessions, github=gh, agent=rec, workspaces=TaskWorkspace(tmp_path / "workspaces"))
    with sessions() as db:
        upsert_github(db, project_config(settings, db, "acme/mobile"), "pr", gh.pr, "opened", canonical=True)
        db.commit()
    await worker.run_job(worker.claim())
    assert rec.seen["text"] == "正在写入 docs/notes.md"
    assert rec.seen["title"] == "审查后合并"
    with sessions() as db:
        assert "agent_activity" not in (db.scalar(select(Record)).details or {})


async def test_agent_task_failure_does_not_fail_record(setup_db, tmp_path):
    from backend.worker import Worker
    from tests.test_worker import FakeGitHub
    settings, sessions, config = setup_db
    config.write_text("projects:\n  - repo: acme/mobile\n", encoding="utf-8")
    _install_rule(sessions)
    gh = FakeGitHub()
    worker = Worker(settings, sessions=sessions, github=gh, agent=TaskAgent({}, invalid=True), workspaces=TaskWorkspace(tmp_path / "workspaces"))
    with sessions() as db:
        upsert_github(db, project_config(settings, db, "acme/mobile"), "pr", gh.pr, "opened", canonical=True)
        db.commit()
        status_before = db.scalar(select(Record)).status
    job_id = worker.claim()
    await worker.run_job(job_id)
    with sessions() as db:
        record = db.scalar(select(Record))
        job = db.get(Job, job_id)
        assert record.status == status_before
        assert "error" not in record.details
        assert job.status == "failed"
        assert "结构化结果" in record.details["agent_results"][RULE_ID]["error"]


def _release_payload(**changes):
    value = {
        "id": 99,
        "tag_name": "v2",
        "name": "v2",
        "body": "release body",
        "html_url": "https://github.com/acme/mobile/releases/tag/v2",
        "published_at": "2026-09-13T10:00:00Z",
        "created_at": "2026-09-13T10:00:00Z",
        "draft": False,
        "prerelease": False,
        "assets": [],
    }
    value.update(changes)
    return value


class ReleaseGitHub:
    def __init__(self, releases, shas):
        self.releases = releases
        self.shas = shas

    async def pages(self, path, params=None, max_pages=100, list_key=None):
        for item in self.releases:
            yield item

    async def commit(self, repo, ref):
        return {"sha": self.shas[ref]}


def _queue_release_agent(db, record, prompt="根据版本变化写文档"):
    schedule_job(db, record, "agent_task", f"agent-task:{RULE_ID}:{record.id}:{record.details.get('tag') or record.external_id}", {
        "rule_id": RULE_ID,
        "rule_name": "release更新",
        "event": "release",
        "github_action": "published",
        "agent_id": AGENT_ID,
        "prompt": prompt,
        "include_repository": True,
        "include_github_credentials": True,
        "skill_ids": ["releasing-version-docs"],
        "head_sha": "",
        "base_sha": "",
        "head_ref": "",
        "base_ref": "",
        "tag": record.details.get("tag", ""),
    })


async def _run_release_agent_task(setup_db, tmp_path, github):
    from backend.worker import Worker
    settings, sessions, config = setup_db
    config.write_text("projects:\n  - repo: acme/mobile\n", encoding="utf-8")
    _install_rule(sessions, event="release", name="release更新", prompt="根据版本变化写文档")
    workspaces = TaskWorkspace(tmp_path / "workspaces")
    agent = TaskAgent({"summary": "ok", "outcome": "completed", "release_docs": {"paths": [], "push_requested": False}})
    worker = Worker(settings, sessions=sessions, github=github, agent=agent, workspaces=workspaces)
    with sessions() as db:
        record = upsert_github(db, project_config(settings, db, "acme/mobile"), "release", _release_payload(), "published", canonical=True)
        _queue_release_agent(db, record)
        db.commit()
    job_id = worker.claim()
    await worker.run_job(job_id)
    return workspaces, agent, job_id, sessions


async def test_agent_task_release_uses_previous_published_tag(setup_db, tmp_path):
    previous = {"id": 88, "tag_name": "v1", "published_at": "2026-09-01T10:00:00Z", "draft": False, "prerelease": False}
    github = ReleaseGitHub([previous, _release_payload()], {"v1": "b" * 40, "v2": "a" * 40})
    workspaces, agent, job_id, sessions = await _run_release_agent_task(setup_db, tmp_path, github)
    assert workspaces.prepared == [{"base": "b" * 40, "head": "a" * 40, "pr_number": None}]
    assert agent.last_input["workspace"]["previous_tag"] == "v1"
    assert agent.last_input["workspace"]["base_sha"] == "b" * 40
    assert agent.last_input["workspace"]["head_sha"] == "a" * 40
    copied = workspaces.task_dir(job_id) / "work" / ".flowdeck-changes.diff"
    assert copied.read_text(encoding="utf-8") == f"{'b' * 40} {'a' * 40}\n"
    with sessions() as db:
        assert db.get(Job, job_id).status == "completed"


async def test_agent_task_release_first_release_keeps_same_sha(setup_db, tmp_path):
    github = ReleaseGitHub([_release_payload()], {"v2": "a" * 40})
    workspaces, agent, job_id, sessions = await _run_release_agent_task(setup_db, tmp_path, github)
    assert workspaces.prepared == [{"base": "a" * 40, "head": "a" * 40, "pr_number": None}]
    assert agent.last_input["workspace"]["previous_tag"] == ""
    with sessions() as db:
        assert db.get(Job, job_id).status == "completed"


async def test_agent_task_release_uses_docs_baseline_without_previous_tag(setup_db, tmp_path):
    github = ReleaseGitHub([_release_payload()], {"v2": "a" * 40})
    settings, sessions, config = setup_db
    config.write_text("projects:\n  - repo: acme/mobile\n", encoding="utf-8")
    _install_rule(sessions, event="release", name="release更新", prompt="根据版本变化写文档")
    workspaces = TaskWorkspace(tmp_path / "workspaces")
    workspaces.docs_sha = "c" * 40
    workspaces.docs_date = "2026-09-10"
    workspaces.docs_files = ["docs/updata_log/manual_2026-09-10.md"]
    agent = TaskAgent({"summary": "ok", "outcome": "completed", "release_docs": {"paths": [], "push_requested": False}})
    from backend.worker import Worker
    worker = Worker(settings, sessions=sessions, github=github, agent=agent, workspaces=workspaces)
    with sessions() as db:
        record = upsert_github(db, project_config(settings, db, "acme/mobile"), "release", _release_payload(), "published", canonical=True)
        _queue_release_agent(db, record)
        db.commit()
    await worker.run_job(worker.claim())
    assert workspaces.prepared == [{"base": "c" * 40, "head": "a" * 40, "pr_number": None}]
    assert agent.last_input["workspace"]["compare_source"] == "docs_date"
    assert agent.last_input["release"]["previous_tag"] == ""


async def test_agent_task_release_prefers_docs_baseline_over_previous_tag(setup_db, tmp_path):
    previous = {"id": 88, "tag_name": "v1", "published_at": "2026-09-01T10:00:00Z", "draft": False, "prerelease": False}
    github = ReleaseGitHub([previous, _release_payload()], {"v1": "b" * 40, "v2": "a" * 40})
    settings, sessions, config = setup_db
    config.write_text("projects:\n  - repo: acme/mobile\n", encoding="utf-8")
    _install_rule(sessions, event="release", name="release更新", prompt="根据版本变化写文档")
    workspaces = TaskWorkspace(tmp_path / "workspaces")
    workspaces.docs_sha = "c" * 40
    workspaces.docs_date = "2026-09-10"
    workspaces.docs_files = ["docs/updata_log/产品功能介绍与使用手册_2026-09-10.md"]
    agent = TaskAgent({"summary": "ok", "outcome": "completed", "release_docs": {"paths": [], "push_requested": False}})
    from backend.worker import Worker
    worker = Worker(settings, sessions=sessions, github=github, agent=agent, workspaces=workspaces)
    with sessions() as db:
        record = upsert_github(db, project_config(settings, db, "acme/mobile"), "release", _release_payload(), "published", canonical=True)
        _queue_release_agent(db, record)
        db.commit()
    job_id = worker.claim()
    await worker.run_job(job_id)
    assert workspaces.prepared == [{"base": "c" * 40, "head": "a" * 40, "pr_number": None}]
    assert agent.last_input["workspace"]["compare_source"] == "docs_date"
    assert agent.last_input["workspace"]["baseline_date"] == "2026-09-10"
    assert agent.last_input["release"]["tag"] == "v2"
    assert agent.last_input["release"]["docs_date"] == "2026-09-13"
    assert agent.last_input["release"]["baseline_docs"] == ["docs/updata_log/产品功能介绍与使用手册_2026-09-10.md"]
    assert RELEASE_TASK_RULES in agent.last_instructions
    with sessions() as db:
        assert db.get(Job, job_id).status == "completed"
