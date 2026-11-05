from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable
import uuid

from backend.config import Settings
from backend.errors import ConfigurationNeeded


PROJECT_MARKER = ".flowdeck-project.json"


def _canonical(path: Path) -> Path:
    return path.resolve(strict=False)


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def storage_roots(settings: Settings) -> list[Path]:
    """Return the canonical allow-listed roots for project-owned storage."""
    configured = getattr(settings, "storage_roots", None) or []
    values = [Path(value) for value in configured] if configured else [Path(settings.data_root).resolve() / "projects"]
    roots: list[Path] = []
    for value in values:
        if not value.is_absolute():
            raise ConfigurationNeeded("项目存储根目录必须是绝对路径")
        root = _canonical(value)
        if any(root == existing for existing in roots):
            raise ConfigurationNeeded("项目存储根目录不能重复")
        if any(root.is_relative_to(existing) or existing.is_relative_to(root) for existing in roots):
            raise ConfigurationNeeded("项目存储根目录不能互相嵌套")
        roots.append(root)
    return roots


def _root_for(settings: Settings, candidate: Path) -> Path:
    for root in storage_roots(settings):
        if candidate != root and candidate.is_relative_to(root):
            return root
    raise ConfigurationNeeded("项目存储路径必须位于允许的存储根目录内，且不能等于根目录")


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


def _validate_existing_components(root: Path, target: Path) -> None:
    current = root
    for part in target.relative_to(root).parts:
        current = current / part
        if current.exists() or current.is_symlink():
            if _is_link_or_junction(current):
                raise ConfigurationNeeded("项目存储路径不能经过符号链接或目录联接")
            if not current.is_dir():
                raise ConfigurationNeeded("项目存储路径的父级不是目录")
            if not _canonical(current).is_relative_to(root):
                raise ConfigurationNeeded("项目存储路径逃逸了允许的根目录")


def _read_marker(marker: Path) -> dict:
    if _is_link_or_junction(marker) or not marker.is_file():
        raise ConfigurationNeeded("项目存储所有权标记无效")
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigurationNeeded("项目存储所有权标记无效") from exc
    if not isinstance(value, dict):
        raise ConfigurationNeeded("项目存储所有权标记无效")
    return value


def validate_project_marker(settings: Settings, project_id: str, path: str | os.PathLike[str]) -> Path:
    """Resolve an existing project directory and verify its ownership marker."""
    target = Path(normalize_storage_path(settings, path))
    root = _root_for(settings, target)
    if not root.exists() or not root.is_dir():
        raise ConfigurationNeeded("项目存储根目录不存在；请检查宿主机挂载")
    _validate_existing_components(root, target)
    if not target.is_dir():
        raise ConfigurationNeeded("项目存储目录不存在")
    marker = _read_marker(target / PROJECT_MARKER)
    if marker.get("project_id") != project_id:
        raise ConfigurationNeeded("项目存储目录属于其他项目")
    return target


def prepare_project_storage(settings: Settings, project_id: str, path: str | os.PathLike[str]) -> str:
    """Create or verify a safe, writable project-owned storage directory."""
    target = Path(normalize_storage_path(settings, path))
    root = _root_for(settings, target)
    if not root.exists() or not root.is_dir():
        raise ConfigurationNeeded("项目存储根目录不存在；请先创建或挂载该目录")
    _validate_existing_components(root, target)

    current = root
    for part in target.relative_to(root).parts:
        current = current / part
        if not current.exists():
            try:
                current.mkdir()
            except FileExistsError:
                pass
        if _is_link_or_junction(current) or not current.is_dir() or not _canonical(current).is_relative_to(root):
            raise ConfigurationNeeded("项目存储路径包含不安全的目录跳转")

    marker = target / PROJECT_MARKER
    if marker.exists() or marker.is_symlink():
        if _read_marker(marker).get("project_id") != project_id:
            raise ConfigurationNeeded("项目存储目录属于其他项目")
    else:
        try:
            has_contents = any(target.iterdir())
        except OSError as exc:
            raise ConfigurationNeeded("无法读取项目存储目录") from exc
        if has_contents:
            raise ConfigurationNeeded("项目存储目录非空且没有 FlowDeck 所有权标记")
        payload = json.dumps({"project_id": project_id, "version": 1}, ensure_ascii=False) + "\n"
        try:
            with marker.open("x", encoding="utf-8") as output:
                output.write(payload)
        except FileExistsError:
            if _read_marker(marker).get("project_id") != project_id:
                raise ConfigurationNeeded("项目存储目录属于其他项目")
        except OSError as exc:
            raise ConfigurationNeeded("无法写入项目存储所有权标记") from exc

    probe = target / f".flowdeck-write-probe-{os.getpid()}-{uuid.uuid4().hex}"
    probe_created = False
    try:
        with probe.open("x", encoding="utf-8") as output:
            probe_created = True
            output.write("ok")
    except OSError as exc:
        raise ConfigurationNeeded("项目存储目录不可写") from exc
    finally:
        if probe_created:
            try:
                probe.unlink(missing_ok=True)
            except OSError:
                pass
    return str(target)
