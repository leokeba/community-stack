"""Field Map provisioning and backup coverage."""

import io
import subprocess
import tarfile
from pathlib import Path

import pytest
import yaml
from community_stack import backup
from community_stack.backup import BackupEngine
from community_stack.provision import ProvisionError, prepare_field_map
from community_stack.secrets import parse_env_file


def enabled_config():
    config = yaml.safe_load(Path("config/example.yaml").read_text(encoding="utf-8"))
    config["services"]["field_map"].update(
        enabled=True,
        hostname="maps.community.org",
        tile_url="https://tiles.example.org/{z}/{x}/{y}.png",
    )
    config["operations"]["backups"]["encryption"] = "none"
    return config


def test_prepare_field_map_generates_stable_secret_storage_and_route(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("ACME_EMAIL=maps-admin@example.org\nRESTIC_PASSWORD=preserved\n")
    env_file.chmod(0o644)

    message = prepare_field_map(tmp_path, enabled_config(), env_file)

    values = parse_env_file(env_file)
    assert len(values["FIELD_MAP_DB_PASSWORD"]) == 64
    assert values["FIELD_MAP_VERSION"] == "107595ae25d0ea197efb6ee8fddc29047bc6c623"
    assert values["FIELD_MAP_SOURCE_REF"] == values["FIELD_MAP_VERSION"]
    assert values["FIELD_MAP_DOMAIN"] == "maps.community.org"
    assert values["FIELD_MAP_ORIGIN"] == "https://maps.community.org"
    assert values["FIELD_MAP_TILE_URL"] == "https://tiles.example.org/{z}/{x}/{y}.png"
    assert values["RESTIC_PASSWORD"] == "preserved"
    assert "https://maps.community.org/" in message
    assert (tmp_path / "data/field_map/uploads").is_dir()
    assert (tmp_path / "data/field_map/postgres").is_dir()
    route = (tmp_path / "data/proxy/field_map.caddy").read_text(encoding="utf-8")
    assert "maps.community.org" in route
    assert "max_size 12MB" in route
    assert "reverse_proxy community_field_map:8000" in route
    assert env_file.stat().st_mode & 0o777 == 0o600


def test_prepare_disabled_field_map_creates_no_database_secret(tmp_path):
    config = enabled_config()
    config["services"]["field_map"]["enabled"] = False
    env_file = tmp_path / ".env"
    env_file.write_text("RESTIC_PASSWORD=preserved\n")

    assert prepare_field_map(tmp_path, config, env_file) is None
    assert parse_env_file(env_file) == {"RESTIC_PASSWORD": "preserved"}
    route = (tmp_path / "data/proxy/field_map.caddy").read_text(encoding="utf-8")
    assert "disabled" in route
    assert "reverse_proxy" not in route


def test_prepare_field_map_refuses_to_regenerate_secret_for_existing_database(tmp_path):
    config = enabled_config()
    database_path = tmp_path / "data/field_map/postgres"
    database_path.mkdir(parents=True)
    (database_path / "PG_VERSION").write_text("16\n", encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text("RESTIC_PASSWORD=preserved\n")

    with pytest.raises(ProvisionError, match="credentials are missing"):
        prepare_field_map(tmp_path, config, env_file)


def test_field_map_backup_includes_database_dump_and_photo_uploads(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    config = enabled_config()
    config_path = root / "community-stack.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    env_file = root / ".env"
    env_file.write_text(
        "POSTGRES_PASSWORD=matrix-password\n"
        "FIELD_MAP_DB_PASSWORD=field-map-password\n"
        "RESTIC_PASSWORD=test-restic-password\n",
        encoding="utf-8",
    )
    uploads = root / "data/field_map/uploads"
    uploads.mkdir(parents=True)
    (uploads / "photo.jpg").write_bytes(b"photo")
    database = root / "data/field_map/postgres"
    database.mkdir(parents=True)
    (database / "PG_VERSION").write_text("16\n", encoding="utf-8")

    monkeypatch.setattr(backup, "is_docker_available", lambda: True)
    monkeypatch.setattr(backup, "dump_postgres_database", lambda *_args, **_kwargs: False)

    def dump_field_map(target):
        target.write_bytes(b"custom-format-postgres-dump")
        return True

    monkeypatch.setattr(backup, "dump_field_map_database", dump_field_map)
    monkeypatch.setattr(
        backup.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "true\n", ""),
    )
    lifecycle = []

    def compose(project_dir, args, env_file=None, capture_output=False):
        lifecycle.append((Path(project_dir).name, list(args)))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(backup.docker, "run_compose", compose)
    engine = BackupEngine(config_path=config_path, env_path=env_file)

    snapshot = engine.create_snapshot()

    assert snapshot.db_included
    assert engine.verify_backup(snapshot.id)[0]
    assert lifecycle == [
        ("field_map", ["stop", "field_map"]),
        ("field_map", ["start", "field_map"]),
    ]
    with tarfile.open(engine.backup_dir / f"{snapshot.id}.tar.gz", "r:gz") as archive:
        dump_name = next(
            name for name in archive.getnames() if name.endswith("postgres_field_map.dump")
        )
        dump_file = archive.extractfile(dump_name)
        assert dump_file is not None
        with dump_file as dump:
            assert dump.read() == b"custom-format-postgres-dump"
        nested_name = next(name for name in archive.getnames() if name.endswith("/data.tar.gz"))
        data_file = archive.extractfile(nested_name)
        assert data_file is not None
        with (
            data_file as data_archive,
            tarfile.open(fileobj=io.BytesIO(data_archive.read()), mode="r:gz") as nested,
        ):
            nested_names = nested.getnames()
            assert any(name.endswith("data/field_map/uploads/photo.jpg") for name in nested_names)
            assert not any("data/field_map/postgres/PG_VERSION" in name for name in nested_names)


def test_field_map_dump_uses_its_private_postgis_container(tmp_path, monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        kwargs["stdout"].write(b"dump")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(backup, "is_docker_available", lambda: True)
    monkeypatch.setattr(backup.subprocess, "run", run)
    target = tmp_path / "field-map.dump"

    assert backup.dump_field_map_database(target)
    assert calls[0][:5] == ["docker", "exec", "community_field_map_db", "pg_dump", "-U"]
    assert calls[0][-2:] == ["-F", "c"]
    assert target.read_bytes() == b"dump"
