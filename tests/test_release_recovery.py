from sqlalchemy import select
from backend.db import Job
from backend.service import upsert_github
from backend.worker import Worker
from tests.test_worker import FakeAgent, FakeWorkspace


def release_worker(setup_db, tmp_path, monkeypatch, withdraw=False):
    settings, sessions, config_path = setup_db
    config_path.write_text('projects:\n  - repo: acme/mobile\n    release_enabled: true\n    previous_tag: v1\n', encoding="utf-8")
    release = {"id": 99, "tag_name": "v2", "name": "v2", "published_at": "2026-09-13T10:00:00Z", "draft": False, "prerelease": False, "assets": []}
    class GH:
        notes = 0
        async def request(self, *args, **kwargs): return release
        async def commit(self, repo, ref): return {"sha": "b" * 40 if ref == "v1" else "a" * 40}
        async def update_notes(self, *args): self.notes += 1
    class Agent(FakeAgent):
        async def execute(self, *args):
            result = await super().execute(*args)
            if withdraw:
                release["draft"] = True
            return result
    gh = GH()
    worker = Worker(settings, sessions=sessions, github=gh, agent=Agent(), workspaces=FakeWorkspace(tmp_path / "workspaces"))
    with sessions() as db:
        upsert_github(db, settings.project("acme/mobile"), "release", release, "published", canonical=True)
        db.commit()
    return worker, gh, sessions


async def test_release_withdrawn_during_analysis_stops_external_writes(setup_db, tmp_path, monkeypatch):
    worker, gh, sessions = release_worker(setup_db, tmp_path, monkeypatch, withdraw=True)
    job_id = worker.claim()
    await worker.run_job(job_id)
    assert gh.notes == 0
    with sessions() as db:
        job = db.get(Job, job_id)
        assert job.status == "superseded" and not job.result.get("upload_intent")
