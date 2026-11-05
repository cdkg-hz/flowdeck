from __future__ import annotations

import io
import re
import shutil
import stat
import zipfile
from pathlib import Path

from sqlalchemy import select

from backend.db import ProjectAutomation, UploadedSkill, utcnow

SLUG_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
MAX_FILE_BYTES = 80_000
MAX_TOTAL_BYTES = 250_000
MAX_ZIP_BYTES = 1_000_000


class SkillPackageError(ValueError):
    pass


class DuplicateSkillError(SkillPackageError):
    pass


NOTES_SKILL_INSTRUCTION = (
    "阅读 changes.diff、commits.txt 和 code/base。"
    "若 payload.skills 非空，必须先遵守其中的发版文档技能："
    "产品说明面向非技术读者、禁止代码路径和接口；技术说明必须有文件或提交依据；"
    "只写相对上一版的增量，不要把文档新写的内容当成产品新增；证据不足写无法确认。"
    "你不能写仓库文件，也不能派发子任务；不要执行技能里的 P0–P4 落盘流程。"
    "把对外用户说明写入 public_notes（分类可用：新功能、问题修正、注意事项），"
    "把带代码依据的研发记录写入 technical_notes。"
    "changes.evidence 必须引用 changed_files 中的路径或 commits.txt 中的完整提交 SHA，不推测未实现功能。"
)


def skill_dir(data_root: Path, skill_id: str) -> Path:
    return Path(data_root) / "skills" / skill_id


def skill_label(skill_md: str, slug: str) -> str:
    for line in skill_md.splitlines():
        title = line.strip().lstrip("#").strip()
        if line.strip().startswith("#") and title:
            return title[:120]
    return slug


def parse_skill_zip(data: bytes, zip_name: str = "") -> dict:
    if len(data) > MAX_ZIP_BYTES:
        raise SkillPackageError("压缩包过大，最大 1MB")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise SkillPackageError("不是有效的 zip 文件") from exc
    files: dict[tuple[str, ...], bytes] = {}
    try:
        for info in archive.infolist():
            raw = info.filename.replace("\\", "/")
            if not raw.strip() or raw.endswith("/"):
                continue
            parts = [part for part in raw.split("/") if part and part != "."]
            if not parts or any(part == ".." for part in parts) or raw.startswith("/"):
                raise SkillPackageError("压缩包路径无效")
            unix_mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(unix_mode):
                raise SkillPackageError("不允许符号链接")
            if len(parts) > 2:
                raise SkillPackageError("skill zip 只能包含一层目录")
            name = parts[-1]
            if name.startswith(".") or not name.endswith(".md"):
                raise SkillPackageError("只允许上传 .md 文件")
            content = archive.read(info)
            if len(content) > MAX_FILE_BYTES:
                raise SkillPackageError(f"{name} 超过 80KB")
            files[tuple(parts)] = content
    finally:
        archive.close()
    skill_mds = [key for key in files if key[-1] == "SKILL.md"]
    if len(skill_mds) != 1:
        raise SkillPackageError("必须恰好包含一个 SKILL.md")
    key = skill_mds[0]
    if len(key) == 1:
        slug = Path(zip_name or "").name
        if slug.lower().endswith(".zip"):
            slug = slug[:-4]
        parent: tuple[str, ...] = ()
    else:
        slug = key[0]
        parent = (slug,)
    if not SLUG_PATTERN.fullmatch(slug):
        raise SkillPackageError("skill 标识无效，请使用字母、数字、点、下划线或连字符")
    for path_key in files:
        if path_key[:-1] != parent:
            raise SkillPackageError("所有 .md 必须与 SKILL.md 在同一目录")
    total = sum(len(content) for content in files.values())
    if total > MAX_TOTAL_BYTES:
        raise SkillPackageError("skill 正文合计超过 250KB")
    named = {path_key[-1]: content for path_key, content in files.items()}
    text = named["SKILL.md"].decode("utf-8", "replace")
    return {"slug": slug, "label": skill_label(text, slug), "files": named}


def write_skill_dir(dest: Path, files: dict[str, bytes]) -> None:
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for name, content in files.items():
        (dest / name).write_bytes(content)


def save_uploaded_skill(settings, db, data: bytes, zip_name: str, item: UploadedSkill | None = None) -> UploadedSkill:
    parsed = parse_skill_zip(data, zip_name)
    duplicate = db.scalar(select(UploadedSkill).where(UploadedSkill.slug == parsed["slug"], UploadedSkill.id != (item.id if item else "")))
    if duplicate:
        raise DuplicateSkillError("已存在同名 skill")
    item = item or UploadedSkill()
    item.slug, item.label, item.updated_at = parsed["slug"], parsed["label"], utcnow()
    db.add(item)
    db.flush()
    write_skill_dir(skill_dir(settings.data_root, item.id), parsed["files"])
    return item


def delete_uploaded_skill_files(settings, skill_id: str) -> None:
    dest = skill_dir(settings.data_root, skill_id)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)


def skill_public(item: UploadedSkill, data_root: Path | None = None) -> dict:
    files = []
    if data_root:
        folder = skill_dir(data_root, item.id)
        if folder.is_dir() and not folder.is_symlink():
            files = [path.name for path in sorted(folder.glob("*.md")) if path.is_file() and not path.is_symlink()]
    return {"id": item.id, "slug": item.slug, "label": item.label, "files": files, "updated_at": item.updated_at}


def uploaded_skill_options(db) -> list[dict]:
    return [{"id": item.slug, "label": item.label} for item in db.scalars(select(UploadedSkill).order_by(UploadedSkill.slug))]


def collected_skill_slugs(config: dict | None) -> set[str]:
    slugs: set[str] = set()
    data = config or {}
    for rule in data.get("agent_rules") or []:
        if isinstance(rule, dict):
            slugs.update(str(item) for item in (rule.get("skill_ids") or []))
    prepare = data.get("prepare_release") or {}
    if isinstance(prepare, dict):
        slugs.update(str(item) for item in (prepare.get("skill_ids") or []))
    return slugs


def skill_slug_in_use(db, slug: str) -> bool:
    for automation in db.scalars(select(ProjectAutomation)):
        if slug in collected_skill_slugs(automation.config):
            return True
    return False


def require_uploaded_slugs(db, slugs: list[str]) -> None:
    wanted = [slug for slug in slugs if slug]
    if not wanted:
        return
    found = set(db.scalars(select(UploadedSkill.slug).where(UploadedSkill.slug.in_(wanted))))
    missing = [slug for slug in wanted if slug not in found]
    if missing:
        raise SkillPackageError("未上传的 skill：" + "、".join(missing))


def load_uploaded_skills(data_root: Path, rows: list[UploadedSkill], skill_ids: list[str] | None) -> list[dict]:
    if not skill_ids:
        return []
    by_slug = {row.slug: row for row in rows}
    skills = []
    total = 0
    for slug in skill_ids:
        row = by_slug.get(slug)
        if not row:
            continue
        folder = skill_dir(data_root, row.id)
        if not folder.is_dir() or folder.is_symlink():
            continue
        files = []
        for item in sorted(folder.glob("*.md")):
            if item.is_symlink() or not item.is_file():
                continue
            path = f"skills/{slug}/{item.name}"
            size = item.stat().st_size
            if size > MAX_FILE_BYTES or total + size > MAX_TOTAL_BYTES:
                files.append({"path": path, "name": item.name, "truncated": True})
                continue
            text = item.read_text(encoding="utf-8", errors="replace")
            total += len(text.encode("utf-8"))
            files.append({"path": path, "name": item.name, "content": text})
        if files:
            skills.append({"id": slug, "files": files})
    return skills
