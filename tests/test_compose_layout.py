from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]
HOST = "${HOST_DATA_ROOT:?Set HOST_DATA_ROOT in .env}"


def compose():
    return yaml.safe_load((ROOT / "compose.yml").read_text(encoding="utf-8"))


def data_binds(service: dict) -> list[dict]:
    found = []
    for item in service.get("volumes") or []:
        if isinstance(item, dict) and item.get("type") == "bind" and HOST in str(item.get("source", "")):
            found.append(item)
    return found


def test_compose_persists_postgres_under_host_data_root():
    doc = compose()
    assert "volumes" not in doc
    services = doc["services"]
    prepare = services["prepare-data"]
    db = services["db"]
    assert prepare["image"] == "flowdeck:local"
    assert prepare["user"] == "0:0"
    assert prepare["restart"] == "no"
    assert prepare["command"] == ["python", "/app/scripts/prepare_data_root.py"]
    assert prepare["environment"]["HOST_DATA_ROOT"] == HOST
    assert db["environment"]["PGDATA"] == "/var/lib/postgresql/data/postgres"
    assert db["depends_on"] == {"prepare-data": {"condition": "service_completed_successfully"}}
    db_binds = data_binds(db)
    assert len(db_binds) == 1
    assert db_binds[0]["target"] == "/var/lib/postgresql/data"
    assert db_binds[0]["bind"]["create_host_path"] is False
    for name in ("prepare-data", "api", "worker"):
        binds = data_binds(services[name])
        assert binds, name
        for item in binds:
            assert item["target"] == HOST
            assert item["bind"]["create_host_path"] is False
    assert services["api"]["environment"]["DATA_ROOT"] == HOST
    assert services["api"]["environment"]["WORKSPACE_HOST_ROOT"] == HOST
    assert "build" in services["api"]
    assert "build" not in services["worker"]
    assert "build" not in services["prepare-data"]
    assert services["worker"]["image"] == "flowdeck:local"


def test_dockerfile_does_not_declare_volume():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "VOLUME" not in text.split()


def test_readme_and_env_example_describe_host_data_layout():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "HOST_DATA_ROOT/postgres/" in readme
    assert "SECRETS_ENCRYPTION_KEY" in readme
    assert "flowdeck_pgdata" in readme
    assert "不要删除 HOST_DATA_ROOT" in readme or "不要删除数据目录" in readme
    assert "postgres/" in example
    assert "projects/" in example
    assert "SECRETS_ENCRYPTION_KEY" in example
