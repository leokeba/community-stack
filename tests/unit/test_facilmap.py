"""FacilMap deployment, lifecycle, diagnostics, and recovery tests."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml
from community_stack import backup
from community_stack.backup import BackupEngine, BackupError
from community_stack.commands.doctor_cmd import audit_facilmap, audit_network_isolation
from community_stack.commands.service_cmd import run_service
from community_stack.config import load_config
from community_stack.provision import ProvisionError, prepare_facilmap
from community_stack.secrets import parse_env_file


def enabled_config():
    config = load_config("config/example.yaml")
    config["services"]["facilmap"].update(enabled=True, hostname="maps.community.org")
    return config


def test_prepare_facilmap_generates_secrets_paths_and_route(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("ACME_EMAIL=maps-admin@example.org\nRESTIC_PASSWORD=preserved\n")
    env_file.chmod(0o644)

    message = prepare_facilmap(tmp_path, enabled_config(), env_file)

    secrets = parse_env_file(env_file)
    assert len(secrets["FACILMAP_DB_PASSWORD"]) >= 64
    assert len(secrets["FACILMAP_DB_ROOT_PASSWORD"]) >= 64
    assert secrets["FACILMAP_VERSION"].startswith("sha256:")
    assert secrets["FACILMAP_DOMAIN"] == "maps.community.org"
    assert secrets["FACILMAP_USER_AGENT"].endswith("contact=maps-admin@example.org")
    assert secrets["RESTIC_PASSWORD"] == "preserved"
    assert "https://maps.community.org/" in message
    assert (tmp_path / "data/facilmap/cache").is_dir()
    assert (tmp_path / "data/facilmap/mariadb").is_dir()
    assert "reverse_proxy community_facilmap:8080" in (
        tmp_path / "data/proxy/facilmap.caddy"
    ).read_text(encoding="utf-8")
    if os.name == "posix":
        assert env_file.stat().st_mode & 0o777 == 0o600


def test_prepare_disabled_facilmap_writes_no_public_route_or_secrets(tmp_path):
    config = load_config("config/example.yaml")
    env_file = tmp_path / ".env"
    env_file.write_text("RESTIC_PASSWORD=keep\n", encoding="utf-8")

    assert prepare_facilmap(tmp_path, config, env_file) is None

    route = (tmp_path / "data/proxy/facilmap.caddy").read_text(encoding="utf-8")
    assert "disabled" in route
    assert "reverse_proxy" not in route
    assert parse_env_file(env_file) == {"RESTIC_PASSWORD": "keep"}


def test_prepare_facilmap_refuses_to_regenerate_database_credentials(tmp_path):
    config = enabled_config()
    database = tmp_path / "data/facilmap/mariadb"
    database.mkdir(parents=True)
    (database / "ibdata1").write_bytes(b"database")
    env_file = tmp_path / ".env"
    env_file.write_text("RESTIC_PASSWORD=keep\n", encoding="utf-8")

    with pytest.raises(ProvisionError, match="credentials are missing"):
        prepare_facilmap(tmp_path, config, env_file)


def test_facilmap_compose_keeps_database_private_and_waits_for_health():
    compose = yaml.safe_load(
        Path("services/facilmap/docker-compose.yml").read_text(encoding="utf-8")
    )
    app = compose["services"]["facilmap"]
    database = compose["services"]["facilmap-db"]

    assert "ports" not in app and "ports" not in database
    assert app["depends_on"]["facilmap-db"]["condition"] == "service_healthy"
    assert "facilmap_internal" in app["networks"]
    assert "proxy_net" in app["networks"]
    assert database["networks"] == ["facilmap_internal"]
    assert database["command"] == [
        "--character-set-server=utf8mb4",
        "--collation-server=utf8mb4_unicode_ci",
    ]
    assert "healthcheck" in database
    assert "@${FACILMAP_VERSION" in app["image"]


def test_facilmap_can_be_enabled_from_an_older_configuration(tmp_path, capsys):
    config = yaml.safe_load(Path("config/example.yaml").read_text(encoding="utf-8"))
    config["services"].pop("facilmap")
    config_path = tmp_path / "community-stack.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    assert run_service("enable", "facilmap", config_path, tmp_path / ".env") == 0

    upgraded = load_config(config_path)
    assert upgraded["services"]["facilmap"]["enabled"] is True
    assert upgraded["services"]["facilmap"]["database"]["version"] == "11.4.12"
    assert "community-stack install" in capsys.readouterr().out


def test_starting_disabled_facilmap_is_reported(tmp_path, capsys):
    config_path = tmp_path / "community-stack.yaml"
    config_path.write_text(Path("config/example.yaml").read_text(encoding="utf-8"))

    assert run_service("start", "facilmap", config_path, tmp_path / ".env") == 1
    assert "disabled" in capsys.readouterr().out


def test_disabling_facilmap_stops_it_and_removes_its_route(tmp_path, monkeypatch, capsys):
    config = enabled_config()
    config_path = tmp_path / "community-stack.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    route_path = tmp_path / "data/proxy/facilmap.caddy"
    route_path.parent.mkdir(parents=True)
    route_path.write_text(
        "maps.community.org {\n    reverse_proxy community_facilmap:8080\n}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "community_stack.commands.service_cmd.docker.is_docker_available", lambda: True
    )
    calls = []

    def compose(project_dir, args, env_file=None, capture_output=False):
        calls.append((Path(project_dir).name, list(args)))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("community_stack.commands.service_cmd.docker.run_compose", compose)

    assert run_service("disable", "facilmap", config_path, tmp_path / ".env") == 0
    assert load_config(config_path)["services"]["facilmap"]["enabled"] is False
    assert "disabled" in route_path.read_text(encoding="utf-8")
    assert calls == [
        ("facilmap", ["down", "--remove-orphans"]),
        ("proxy", ["restart", "caddy"]),
    ]
    assert "Persistent data was preserved" in capsys.readouterr().out


def test_facilmap_backup_and_isolated_restore_include_database_and_cache(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    config = enabled_config()
    config["operations"]["backups"]["encryption"] = "none"
    config_path = project / "community-stack.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    env_file = project / ".env"
    env_file.write_text(
        "POSTGRES_PASSWORD=matrix-password\n"
        "RESTIC_PASSWORD=test-restic-password\n"
        "FACILMAP_DB_PASSWORD=facilmap-password\n"
        "FACILMAP_DB_ROOT_PASSWORD=facilmap-root-password\n",
        encoding="utf-8",
    )
    cache = project / "data/facilmap/cache"
    cache.mkdir(parents=True)
    (cache / "geoip.dat").write_text("cached data", encoding="utf-8")
    database = project / "data/facilmap/mariadb"
    database.mkdir(parents=True)
    (database / "ibdata1").write_bytes(b"live database")

    monkeypatch.setattr(backup, "is_docker_available", lambda: True)
    monkeypatch.setattr(backup, "dump_postgres_database", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        backup.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, "true\n", ""),
    )

    def create_facilmap_dump(target):
        target.write_text("CREATE TABLE maps (id varchar(64));\n", encoding="utf-8")
        return True

    monkeypatch.setattr(backup, "dump_facilmap_database", create_facilmap_dump)
    lifecycle = []

    def compose(project_dir, args, env_file=None, capture_output=False):
        lifecycle.append((Path(project_dir).name, list(args)))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(backup.docker, "run_compose", compose)
    engine = BackupEngine(config_path=config_path, env_path=env_file)

    snapshot = engine.create_snapshot()

    assert snapshot.db_included
    assert lifecycle == [
        ("facilmap", ["stop", "facilmap"]),
        ("facilmap", ["start", "facilmap"]),
    ]
    assert engine.verify_backup(snapshot.id)[0]
    restored = tmp_path / "restore"
    assert engine.restore_to_target(restored, snapshot.id, confirm=True)
    assert (
        (restored / "backups/mariadb_facilmap.sql")
        .read_text(encoding="utf-8")
        .startswith("CREATE TABLE maps")
    )
    assert (restored / "data/facilmap/cache/geoip.dat").read_text(encoding="utf-8") == "cached data"
    assert not (restored / "data/facilmap/mariadb/ibdata1").exists()


def test_facilmap_backup_fails_without_a_database_dump(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    config = enabled_config()
    config["operations"]["backups"]["encryption"] = "none"
    config_path = project / "community-stack.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    env_file = project / ".env"
    env_file.write_text("RESTIC_PASSWORD=test-password\n", encoding="utf-8")
    monkeypatch.setattr(backup, "is_docker_available", lambda: True)
    monkeypatch.setattr(backup, "dump_postgres_database", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        backup.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, "false\n", ""),
    )
    monkeypatch.setattr(backup, "dump_facilmap_database", lambda _target: False)
    monkeypatch.setattr(
        backup.docker,
        "run_compose",
        lambda project_dir, args, **kwargs: subprocess.CompletedProcess(args, 0, "", ""),
    )

    with pytest.raises(BackupError, match="MariaDB dump"):
        BackupEngine(config_path=config_path, env_path=env_file).create_snapshot()


def test_facilmap_doctor_skips_disabled_service():
    healthy, messages = audit_facilmap(
        {"services": {"facilmap": {"enabled": False}}}, "unused.yaml", ".env"
    )
    assert healthy
    assert "disabled" in messages[0]


def test_facilmap_doctor_checks_public_app_socket_and_backup(tmp_path, monkeypatch):
    project = tmp_path / "project"
    cache = project / "data/facilmap/cache"
    database = project / "data/facilmap/mariadb"
    cache.mkdir(parents=True)
    database.mkdir(parents=True)
    config_path = project / "community-stack.yaml"
    config_path.write_text(yaml.safe_dump(enabled_config()), encoding="utf-8")
    env_path = project / ".env"
    env_path.write_text("RESTIC_PASSWORD=unused\n", encoding="utf-8")
    monkeypatch.setattr(
        "community_stack.commands.doctor_cmd.list_community_containers",
        lambda: [
            {"name": "community_facilmap", "state": "running", "status": "Up (healthy)"},
            {"name": "community_facilmap_db", "state": "running", "status": "Up (healthy)"},
        ],
    )
    monkeypatch.setattr(BackupEngine, "verify_backup", lambda *_args, **_kwargs: (True, []))
    requested_urls = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size=-1):
            return b"0{}"

    def open_url(url, timeout):
        requested_urls.append((url, timeout))
        return Response()

    monkeypatch.setattr("community_stack.commands.doctor_cmd.urllib.request.urlopen", open_url)

    healthy, messages = audit_facilmap(enabled_config(), config_path, env_path)

    assert healthy
    assert any("Socket.IO polling handshake" in message for message in messages)
    assert requested_urls == [
        ("https://maps.community.org/", 10),
        ("https://maps.community.org/socket.io/?EIO=4&transport=polling", 10),
    ]


def test_network_audit_flags_a_published_facilmap_database_port(monkeypatch):
    monkeypatch.setattr("community_stack.commands.doctor_cmd.is_docker_available", lambda: True)
    monkeypatch.setattr(
        "community_stack.commands.doctor_cmd.list_community_containers",
        lambda: [
            {
                "name": "community_facilmap_db",
                "status": "Up 3 minutes (healthy) 0.0.0.0:3306->3306/tcp",
            }
        ],
    )

    messages = audit_network_isolation()

    assert any("3306 publicly" in message for message in messages)
