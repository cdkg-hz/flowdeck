import yaml
from pathlib import Path
from backend.config import AppConfig, ProjectConfig


def test_example_config_loads_without_flowdeck_image_or_deploy():
    data = yaml.safe_load(Path("config/projects.example.yml").read_text(encoding="utf-8"))
    config = AppConfig.model_validate(data)
    project = config.projects[0]
    dumped = project.model_dump()
    assert "build" not in dumped
    assert "deploy" not in dumped
    assert "script_publisher" not in dumped
    assert "publisher" not in dumped
    assert "apk_pattern" not in dumped


def test_leftover_yaml_build_and_deploy_are_ignored():
    project = ProjectConfig.model_validate({
        "repo": "acme/mobile",
        "release_enabled": True,
        "previous_tag": "v1",
        "build": {"enabled": True, "registry_id": "x", "repository": "acme/api"},
        "deploy": {"enabled": True, "command": ["deploy"]},
        "script_publisher": {"enabled": True},
    })
    dumped = project.model_dump()
    assert project.release_enabled is True
    assert "build" not in dumped
    assert "deploy" not in dumped
    assert not hasattr(project, "build")
    assert not hasattr(project, "deploy")
