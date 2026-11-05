import copy
import json
from pathlib import Path
from sqlalchemy import select
from backend.db import Job, Record
from backend.service import upsert_github
from backend.worker import Worker
from tests.conftest import pull_payload


class FakeGitHub:
    def __init__(self):
        self.pr = pull_payload()
        self.states = [{"name": "CI", "state": "success"}]
        self.merges = []
        self.comments = []
        self.statuses = []
    async def pull(self, repo, number): return copy.deepcopy(self.pr)
    async def linked_issues(self, repo, number): return []
    async def checks(self, repo, sha): return self.states
    async def set_review_status(self, repo, sha, state, description): self.statuses.append((sha, state))
    async def comment_once(self, repo, number, marker, body): self.comments.append((marker, body))
    async def merge(self, repo, number, sha, method):
        assert sha == self.pr["head"]["sha"]
        self.merges.append(sha)
        return {"merged": True, "sha": "c" * 40}


class FakeWorkspace:
    def __init__(self, root): self.root = root
    def task_dir(self, job_id): return self.root / job_id
    async def prepare(self, job_id, repo, base, head, pr_number=None):
        path = self.task_dir(job_id)
        (path / "code").mkdir(parents=True, exist_ok=True)
        (path / "code" / "file.py").write_text("print('ok')\n")
        (path / "commits.txt").write_text(head + " feature\n")
        return {"repo": repo, "head_sha": head, "base_sha": base, "changed_files": ["file.py"], "host_workspace": str(path)}


class FakeAgent:
    def __init__(self, github=None, change_head=False):
        self.github, self.change_head = github, change_head
        self.last_input = None
    async def execute(self, task_type, task_id, instructions, input_text, state, save):
        data = json.loads(input_text)
        self.last_input = data
        if self.change_head: self.github.pr["head"]["sha"] = "d" * 40
        if task_type == "review":
            return json.dumps({"head_sha": data["workspace"]["head_sha"], "verdict": "pass", "summary": "No correctness issues found", "findings": []})
        return json.dumps({"head_sha": data["workspace"]["head_sha"], "base_sha": data["workspace"]["base_sha"], "public_notes": "新增收藏功能", "technical_notes": "实现 file.py 收藏逻辑", "changes": [{"category": "feature", "description": "新增收藏", "evidence": ["file.py"]}]})


def make_worker(setup_db, tmp_path, change_head=False):
    settings, sessions, _ = setup_db
    gh = FakeGitHub()
    worker = Worker(settings, sessions=sessions, github=gh, agent=FakeAgent(gh, change_head), workspaces=FakeWorkspace(tmp_path / "workspaces"))
    with sessions() as db:
        upsert_github(db, settings.project("acme/mobile"), "pr", gh.pr, canonical=True)
        db.commit()
    return worker, gh, sessions


async def test_analyze_passes_api_key_as_secret_values(setup_db, tmp_path):
    from backend.copilot_runtime import CopilotAdapter
    settings, sessions, _ = setup_db
    gh = FakeGitHub()

    class Rec(CopilotAdapter):
        def __init__(self):
            self.kwargs = None

        async def execute(self, *args, **kwargs):
            self.kwargs = kwargs
            data = json.loads(args[3])
            return json.dumps({"head_sha": data["workspace"]["head_sha"], "verdict": "pass", "summary": "No correctness issues found", "findings": []})

        async def close(self):
            pass

    rec = Rec()
    worker = Worker(settings, sessions=sessions, github=gh, agent=rec, workspaces=FakeWorkspace(tmp_path / "workspaces"))
    with sessions() as db:
        upsert_github(db, settings.project("acme/mobile"), "pr", gh.pr, canonical=True)
        db.commit()
    await worker.run_job(worker.claim())
    assert rec.kwargs is not None
    assert rec.kwargs["secret_values"] == ["test-agent-key"]


async def test_review_records_and_clears_agent_activity(setup_db, tmp_path):
    from backend.copilot_runtime import CopilotAdapter
    settings, sessions, _ = setup_db
    gh = FakeGitHub()

    class Rec(CopilotAdapter):
        def __init__(self):
            self.seen = None

        async def execute(self, *args, **kwargs):
            await kwargs["on_progress"]("正在读取 code/file.py")
            with sessions() as db:
                self.seen = dict(db.scalar(select(Record)).details.get("agent_activity") or {})
            data = json.loads(args[3])
            return json.dumps({"head_sha": data["workspace"]["head_sha"], "verdict": "pass", "summary": "No correctness issues found", "findings": []})

        async def close(self):
            pass

    rec = Rec()
    worker = Worker(settings, sessions=sessions, github=gh, agent=rec, workspaces=FakeWorkspace(tmp_path / "workspaces"))
    with sessions() as db:
        upsert_github(db, settings.project("acme/mobile"), "pr", gh.pr, canonical=True)
        db.commit()
    await worker.run_job(worker.claim())
    assert rec.seen["text"] == "正在读取 code/file.py"
    assert rec.seen["title"] == "审查代码"
    assert rec.seen["stage"] == "review"
    assert "code/file.py" in rec.seen["recent"][0]
    with sessions() as db:
        assert "agent_activity" not in (db.scalar(select(Record)).details or {})


async def test_review_accepts_extra_json_fields(setup_db, tmp_path):
    settings, sessions, _ = setup_db
    gh = FakeGitHub()

    class ExtraAgent:
        async def execute(self, task_type, task_id, instructions, input_text, state, save):
            data = json.loads(input_text)
            return json.dumps({
                "head_sha": data["workspace"]["head_sha"],
                "verdict": "changes_requested",
                "summary": "needs a fix",
                "summary_note": "",
                "findings": [{
                    "file": "file.py", "line": 1, "severity": "high", "title": "bug",
                    "explanation": "broken", "suggestion": "fix it", "code_snippet": "print('ok')",
                }],
            })

    worker = Worker(settings, sessions=sessions, github=gh, agent=ExtraAgent(), workspaces=FakeWorkspace(tmp_path / "workspaces"))
    with sessions() as db:
        upsert_github(db, settings.project("acme/mobile"), "pr", gh.pr, canonical=True)
        db.commit()
    await worker.run_job(worker.claim())
    with sessions() as db:
        record = db.scalar(select(Record))
        assert record.status == "changes_requested"
        assert record.details["review"]["verdict"] == "changes_requested"
        assert "summary_note" not in record.details["review"]
        assert record.details["review"]["findings"][0]["file"] == "file.py"
    assert gh.comments and gh.merges == []
    assert all("flowdeck:notification" not in (marker or "") for marker, _ in gh.comments)
    assert any("flowdeck:review" in (marker or "") for marker, _ in gh.comments)


async def test_review_then_merge_exact_commit(setup_db, tmp_path):
    worker, gh, sessions = make_worker(setup_db, tmp_path)
    await worker.run_job(worker.claim())
    with sessions() as db:
        record = db.scalar(select(Record))
        assert record.details["review"]["head_sha"] == "a" * 40
        assert record.status == "waiting" and record.stage == "checks"
    await worker.run_job(worker.claim())
    assert gh.merges == ["a" * 40]
    with sessions() as db:
        assert db.scalar(select(Record)).status == "merged"


async def test_merged_emits_merged_event(setup_db, tmp_path, monkeypatch):
    posts = []

    async def fake_dispatch(settings_, sessions_, record, events, job=None, **kwargs):
        posts.append(list(events))

    monkeypatch.setattr("backend.worker.dispatch_dingtalk", fake_dispatch, raising=False)
    monkeypatch.setattr("backend.dingtalk.dispatch_dingtalk", fake_dispatch)
    worker, gh, sessions = make_worker(setup_db, tmp_path)
    await worker.run_job(worker.claim())
    await worker.run_job(worker.claim())
    assert gh.merges == ["a" * 40]
    with sessions() as db:
        assert db.scalar(select(Record)).status == "merged"
    keys = [event for batch in posts for event, _ in batch]
    assert "merged" in keys


async def test_review_then_merge_without_required_checks(setup_db, tmp_path):
    settings, sessions, config = setup_db
    config.write_text("projects:\n  - repo: acme/mobile\n    review_enabled: true\n    merge_enabled: true\n", encoding="utf-8")
    worker, gh, sessions = make_worker(setup_db, tmp_path)
    gh.states = []
    await worker.run_job(worker.claim())
    await worker.run_job(worker.claim())
    assert gh.merges == ["a" * 40]
    with sessions() as db:
        assert db.scalar(select(Record)).status == "merged"


async def test_missing_ci_never_merges(setup_db, tmp_path):
    worker, gh, sessions = make_worker(setup_db, tmp_path)
    gh.states = []
    await worker.run_job(worker.claim())
    await worker.run_job(worker.claim())
    assert gh.merges == []
    with sessions() as db:
        record = db.scalar(select(Record))
        assert record.status == "waiting" and "CI" in record.details["error"]


async def test_new_push_invalidates_inflight_review(setup_db, tmp_path):
    worker, gh, sessions = make_worker(setup_db, tmp_path, change_head=True)
    await worker.run_job(worker.claim())
    with sessions() as db:
        assert db.scalar(select(Job)).status == "superseded"
        assert not list(db.scalars(select(Job).where(Job.kind == "merge")))
        assert not db.scalar(select(Record)).details.get("review")
    assert not gh.merges and not gh.comments


async def test_target_branch_change_requires_new_review(setup_db, tmp_path):
    worker, gh, sessions = make_worker(setup_db, tmp_path)
    await worker.run_job(worker.claim())
    gh.pr["base"]["sha"] = "e" * 40
    await worker.run_job(worker.claim())
    assert not gh.merges
    with sessions() as db:
        assert not db.scalar(select(Record)).details.get("review")
        current = list(db.scalars(select(Job).where(Job.kind == "review", Job.status == "queued")))
        assert len(current) == 1 and current[0].payload["base_sha"] == "e" * 40


async def test_release_notes_succeed_and_ignore_leftover_deploy_yaml(setup_db, tmp_path):
    settings, sessions, config = setup_db
    config.write_text('projects:\n  - repo: acme/mobile\n    release_enabled: true\n    previous_tag: v1\n    build:\n      enabled: true\n    deploy:\n      enabled: true\n', encoding="utf-8")
    release = {"id": 99, "tag_name": "v2", "name": "v2", "published_at": "2026-09-13T10:00:00Z", "created_at": "2026-09-13T10:00:00Z", "draft": False, "prerelease": False, "assets": []}
    class GH(FakeGitHub):
        notes = []
        async def request(self, method, path, **kwargs): return release
        async def commit(self, repo, ref): return {"sha": "b" * 40 if ref == "v1" else "a" * 40}
        async def update_notes(self, repo, release_id, public_notes, technical_notes):
            self.notes.append((repo, release_id, public_notes, technical_notes))
    agent = FakeAgent()
    worker = Worker(settings, sessions=sessions, github=GH(), agent=agent, workspaces=FakeWorkspace(tmp_path / "workspaces"))
    with sessions() as db:
        upsert_github(db, settings.project("acme/mobile"), "release", release, "published", canonical=True)
        db.commit()
    await worker.run_job(worker.claim())
    assert agent.last_input["skills"] == []
    with sessions() as db:
        record, job = db.scalar(select(Record)), db.scalar(select(Job))
        assert record.status == "success" and record.stage == "verify"
        assert record.details["steps"]["notes"] == "success"
        assert job.result["notes"]["public_notes"] == "新增收藏功能"
        assert "build" not in (job.result or {})
        assert "deploy" not in (job.result or {})
        assert "deploy_intent" not in (job.result or {})
        assert "upload" not in (record.details.get("steps") or {})
        assert "build" not in (record.details.get("steps") or {})
        assert "deploy" not in (record.details.get("steps") or {})


async def test_release_notes_use_previous_published_tag_as_base(setup_db, tmp_path):
    settings, sessions, config = setup_db
    config.write_text('projects:\n  - repo: acme/mobile\n    release_enabled: true\n', encoding="utf-8")
    current = {"id": 99, "tag_name": "v2", "name": "v2", "published_at": "2026-09-13T10:00:00Z", "created_at": "2026-09-13T10:00:00Z", "draft": False, "prerelease": False, "assets": []}
    previous = {"id": 88, "tag_name": "v1", "published_at": "2026-09-01T10:00:00Z", "draft": False, "prerelease": False}
    class GH(FakeGitHub):
        async def request(self, method, path, **kwargs): return current
        async def commit(self, repo, ref): return {"sha": "b" * 40 if ref == "v1" else "a" * 40}
        async def update_notes(self, *args): pass
        async def pages(self, path, params=None, max_pages=100, list_key=None):
            for item in (previous, current):
                yield item
    spaces = FakeWorkspace(tmp_path / "workspaces")
    worker = Worker(settings, sessions=sessions, github=GH(), agent=FakeAgent(), workspaces=spaces)
    with sessions() as db:
        upsert_github(db, settings.project("acme/mobile"), "release", current, "published", canonical=True)
        db.commit()
    await worker.run_job(worker.claim())
    with sessions() as db:
        job = db.scalar(select(Job))
        assert job.status == "completed"
        assert job.result["workspace"]["previous_tag"] == "v1"
        assert job.result["workspace"]["base_sha"] == "b" * 40
        assert job.result["workspace"]["head_sha"] == "a" * 40
