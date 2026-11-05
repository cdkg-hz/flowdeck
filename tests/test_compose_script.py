import json
import subprocess
from types import SimpleNamespace
import pytest
from scripts.deploy_compose import deploy


def test_compose_updates_only_selected_service_and_keeps_previous_override(tmp_path, monkeypatch):
    compose = tmp_path / "compose.yml"; compose.write_text("services: {}")
    override = tmp_path / "flowdeck-api.override.json"; override.write_text('{"old":"version"}')
    calls = []
    def run(argv, **kwargs):
        assert kwargs["check"] is True and not kwargs.get("shell")
        calls.append(argv)
        return SimpleNamespace(stdout="api\ndatabase\n")
    monkeypatch.setattr(subprocess, "run", run)
    result = deploy(compose, "api", "registry.example.com/api:v2")
    assert json.loads(override.read_text()) == {"services": {"api": {"image": "registry.example.com/api:v2"}}}
    assert json.loads(override.with_suffix(".previous.json").read_text()) == {"old": "version"}
    assert calls[-1][-5:] == ["up", "-d", "--no-deps", "--no-build", "api"]
    assert result["image"] == "registry.example.com/api:v2"


@pytest.mark.parametrize("image", ["registry:5000/api", "api:latest", "api:v2;whoami"])
def test_unversioned_or_unsafe_image_rejected_before_docker(tmp_path, monkeypatch, image):
    compose = tmp_path / "compose.yml"; compose.write_text("services: {}")
    def forbidden(*args, **kwargs): pytest.fail("Docker must not run")
    monkeypatch.setattr(subprocess, "run", forbidden)
    with pytest.raises(ValueError): deploy(compose, "api", image)
