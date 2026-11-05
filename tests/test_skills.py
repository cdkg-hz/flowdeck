import io
import stat
import zipfile

import pytest
from sqlalchemy import select
from starlette.testclient import TestClient

from backend.auth import set_admin
from backend.db import UploadedSkill
from backend.main import create_app
from backend.skills import DuplicateSkillError, SkillPackageError, load_uploaded_skills, parse_skill_zip, save_uploaded_skill

ADMIN_PASS = "admin-password-123"


def zip_bytes(entries: dict[str, str], *, extra: list[zipfile.ZipInfo] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, text in entries.items():
            archive.writestr(name, text)
        for info in extra or []:
            archive.writestr(info, "target")
    return buf.getvalue()


def symlink_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name)
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    return info


def test_parse_skill_zip_folder_layout():
    parsed = parse_skill_zip(zip_bytes({
        "releasing-version-docs/SKILL.md": "# 发版统御\n正文\n",
        "releasing-version-docs/changelog.md": "# 约定\n",
    }), "ignored.zip")
    assert parsed["slug"] == "releasing-version-docs"
    assert parsed["label"] == "发版统御"
    assert set(parsed["files"]) == {"SKILL.md", "changelog.md"}


def test_parse_skill_zip_root_skill_uses_filename_stem():
    parsed = parse_skill_zip(zip_bytes({"SKILL.md": "# Root\n"}), "my-skill.zip")
    assert parsed["slug"] == "my-skill"
    assert parsed["label"] == "Root"


def test_parse_skill_zip_rejects_zip_slip():
    with pytest.raises(SkillPackageError, match="路径无效"):
        parse_skill_zip(zip_bytes({"../SKILL.md": "# x\n"}), "evil.zip")


def test_parse_skill_zip_rejects_nested_and_missing_skill():
    with pytest.raises(SkillPackageError, match="一层目录"):
        parse_skill_zip(zip_bytes({"a/b/SKILL.md": "# x\n"}), "nested.zip")
    with pytest.raises(SkillPackageError, match="SKILL.md"):
        parse_skill_zip(zip_bytes({"notes.md": "# x\n"}), "notes.zip")


def test_parse_skill_zip_rejects_symlink_and_non_md():
    with pytest.raises(SkillPackageError, match="符号链接"):
        parse_skill_zip(zip_bytes({}, extra=[symlink_info("releasing-version-docs/SKILL.md")]), "link.zip")
    with pytest.raises(SkillPackageError, match="只允许"):
        parse_skill_zip(zip_bytes({
            "docs/SKILL.md": "# x\n",
            "docs/bin.exe": "nope",
        }), "bin.zip")


def test_save_uploaded_skill_duplicate_slug(setup_db):
    settings, sessions, _ = setup_db
    with sessions() as db:
        with pytest.raises(DuplicateSkillError):
            save_uploaded_skill(settings, db, zip_bytes({
                "releasing-version-docs/SKILL.md": "# 重复\n",
            }), "releasing-version-docs.zip")


def test_load_uploaded_skills_filters_by_slug(setup_db):
    settings, sessions, _ = setup_db
    with sessions() as db:
        rows = list(db.scalars(select(UploadedSkill)))
    assert load_uploaded_skills(settings.data_root, rows, []) == []
    loaded = load_uploaded_skills(settings.data_root, rows, ["releasing-version-docs", "missing"])
    assert [item["id"] for item in loaded] == ["releasing-version-docs"]
    assert "发版统御" in loaded[0]["files"][0]["content"]
    assert loaded[0]["files"][0]["path"].startswith("skills/releasing-version-docs/")


def _login(client: TestClient) -> dict:
    login = client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASS})
    assert login.status_code == 200
    return {"X-CSRF-Token": login.json()["csrf_token"]}


def test_admin_skill_crud_and_delete_in_use(setup_db):
    settings, sessions, _ = setup_db
    with sessions() as db:
        set_admin(db, "admin", ADMIN_PASS)
        db.commit()
    app = create_app(settings)
    with TestClient(app) as client:
        headers = _login(client)
        listed = client.get("/api/admin/skills", headers=headers)
        assert listed.status_code == 200
        assert listed.json()["skills"][0]["slug"] == "releasing-version-docs"
        created = client.post(
            "/api/admin/skills",
            files={"file": ("other-skill.zip", zip_bytes({"other-skill/SKILL.md": "# Other\n细节\n"}), "application/zip")},
            headers=headers,
        )
        assert created.status_code == 201, created.text
        skill_id = created.json()["id"]
        assert created.json()["slug"] == "other-skill"
        assert created.json()["label"] == "Other"
        duplicate = client.post(
            "/api/admin/skills",
            files={"file": ("other-skill.zip", zip_bytes({"other-skill/SKILL.md": "# Other\n"}), "application/zip")},
            headers=headers,
        )
        assert duplicate.status_code == 409
        replaced = client.put(
            f"/api/admin/skills/{skill_id}",
            files={"file": ("other-skill.zip", zip_bytes({"other-skill/SKILL.md": "# 替换后\n"}), "application/zip")},
            headers=headers,
        )
        assert replaced.status_code == 200
        assert replaced.json()["label"] == "替换后"
        slip = client.post(
            "/api/admin/skills",
            files={"file": ("evil.zip", zip_bytes({"../SKILL.md": "# x\n"}), "application/zip")},
            headers=headers,
        )
        assert slip.status_code == 422
        assert client.delete(f"/api/admin/skills/{skill_id}", headers=headers).status_code == 200
        seeded = listed.json()["skills"][0]["id"]
        used = client.delete(f"/api/admin/skills/{seeded}", headers=headers)
        assert used.status_code == 200
        agent_id = "f" * 32
        client.post(
            "/api/admin/skills",
            files={"file": ("releasing-version-docs.zip", zip_bytes({"releasing-version-docs/SKILL.md": "# 发版统御\n"}), "application/zip")},
            headers=headers,
        )
        saved = client.put(
            f"/api/projects/{'a' * 32}/automation",
            json={"agent_rules": [{
                "name": "审查后合并",
                "event": "pull_request",
                "agent_id": agent_id,
                "prompt": "审查这个 PR。",
                "skill_ids": ["releasing-version-docs"],
                "enabled": True,
            }]},
            headers=headers,
        )
        assert saved.status_code == 200, saved.text
        occupied = client.get("/api/admin/skills").json()["skills"]
        occupied_id = next(item["id"] for item in occupied if item["slug"] == "releasing-version-docs")
        blocked = client.delete(f"/api/admin/skills/{occupied_id}", headers=headers)
        assert blocked.status_code == 409


def test_connection_details_lists_uploaded_skills_without_git(setup_db):
    settings, sessions, _ = setup_db
    with sessions() as db:
        set_admin(db, "admin", ADMIN_PASS)
        db.commit()
    git_cache = settings.data_root / "projects" / "mobile" / "repo.git"
    if git_cache.exists():
        import shutil
        shutil.rmtree(git_cache)
    with TestClient(create_app(settings)) as client:
        headers = _login(client)
        body = client.get(f"/api/projects/{'a' * 32}/connection", headers=headers)
        assert body.status_code == 200
        data = body.json()
        assert "repository_skills" not in data
        assert data["skills"] == [{"id": "releasing-version-docs", "label": "发版统御"}]
        missing = client.put(
            f"/api/projects/{'a' * 32}/automation",
            json={"agent_rules": [{
                "name": "审查后合并",
                "event": "pull_request",
                "agent_id": "f" * 32,
                "prompt": "审查这个 PR。",
                "skill_ids": ["not-uploaded"],
                "enabled": True,
            }]},
            headers=headers,
        )
        assert missing.status_code == 422
