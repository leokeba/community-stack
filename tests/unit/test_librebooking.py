"""LibreBooking provisioning and setup credential safety tests."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml
from community_stack import backup
from community_stack.agent.operations import (
    AgentContext,
    OperationError,
    librebooking_setup_details,
)
from community_stack.agent.settings import current_values
from community_stack.backup import BackupEngine
from community_stack.commands.doctor_cmd import audit_librebooking
from community_stack.commands.service_cmd import run_service
from community_stack.config import load_config
from community_stack.provision import ProvisionError, prepare_librebooking
from community_stack.secrets import parse_env_file


def enabled_config():
    config = load_config("config/example.yaml")
    config["services"]["librebooking"].update(
        enabled=True,
        hostname="booking.community.org",
        admin_email="spaces@community.org",
    )
    return config


def test_prepare_librebooking_creates_private_paths_credentials_and_route(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"COMMUNITY_STACK_GID={os.getgid()}\nRESTIC_PASSWORD=preserved\n", encoding="utf-8"
    )

    message = prepare_librebooking(tmp_path, enabled_config(), env_file)

    env = parse_env_file(env_file)
    assert len(env["LIBREBOOKING_DB_PASSWORD"]) >= 64
    assert len(env["LIBREBOOKING_DB_ROOT_PASSWORD"]) >= 64
    assert len(env["LIBREBOOKING_INSTALL_PASSWORD"]) >= 64
    assert env["LIBREBOOKING_VERSION"] == "7.2.0-3.5.2"
    assert env["LIBREBOOKING_DOMAIN"] == "booking.community.org"
    assert env["LIBREBOOKING_ADMIN_EMAIL"] == "spaces@community.org"
    assert env["LIBREBOOKING_ALLOW_REGISTRATION"] == "false"
    assert env["RESTIC_PASSWORD"] == "preserved"
    assert "https://booking.community.org/Web/" in message
    for path in ("mariadb", "config", "uploads", "logs"):
        storage = tmp_path / "data" / "librebooking" / path
        assert storage.is_dir()
        if os.name == "posix":
            assert storage.stat().st_mode & 0o777 == 0o770
    route = (tmp_path / "data/proxy/librebooking.caddy").read_text(encoding="utf-8")
    assert "booking.community.org" in route
    assert "reverse_proxy community_librebooking:8080" in route
    if os.name == "posix":
        assert env_file.stat().st_mode & 0o777 == 0o600


def test_prepare_disabled_librebooking_does_not_create_credentials(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("RESTIC_PASSWORD=preserved\n", encoding="utf-8")

    assert prepare_librebooking(tmp_path, load_config("config/example.yaml"), env_file) is None

    assert parse_env_file(env_file) == {"RESTIC_PASSWORD": "preserved"}
    route = (tmp_path / "data/proxy/librebooking.caddy").read_text(encoding="utf-8")
    assert "disabled" in route
    assert "reverse_proxy" not in route


def test_prepare_refuses_to_regenerate_librebooking_secrets_when_data_exists(tmp_path):
    database = tmp_path / "data/librebooking/mariadb"
    database.mkdir(parents=True)
    (database / "ibdata1").write_bytes(b"database")
    env_file = tmp_path / ".env"
    env_file.write_text("RESTIC_PASSWORD=preserved\n", encoding="utf-8")

    with pytest.raises(ProvisionError, match="credentials are missing"):
        prepare_librebooking(tmp_path, enabled_config(), env_file)


def test_setup_details_require_enabled_service_and_never_return_root_password(tmp_path):
    config = enabled_config()
    config_path = tmp_path / "community-stack.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text(
        "LIBREBOOKING_DB_PASSWORD=application-password\n"
        "LIBREBOOKING_DB_ROOT_PASSWORD=root-password\n"
        "LIBREBOOKING_INSTALL_PASSWORD=install-password\n",
        encoding="utf-8",
    )

    details = librebooking_setup_details(AgentContext(tmp_path), {})

    assert details["app_url"] == "https://booking.community.org/Web/"
    assert details["install_url"] == "https://booking.community.org/Web/install/"
    assert details["admin_email"] == "spaces@community.org"
    assert details["database_password"] == "application-password"
    assert details["install_password"] == "install-password"
    assert "root-password" not in str(details)

    config["services"]["librebooking"]["enabled"] = False
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    with pytest.raises(OperationError, match="Enable LibreBooking"):
        librebooking_setup_details(AgentContext(tmp_path), {})


def test_setup_details_refuse_missing_credentials(tmp_path):
    config_path = tmp_path / "community-stack.yaml"
    config_path.write_text(yaml.safe_dump(enabled_config()), encoding="utf-8")
    (tmp_path / ".env").write_text("LIBREBOOKING_DB_PASSWORD=only-one\n", encoding="utf-8")

    with pytest.raises(OperationError, match="credentials are not ready"):
        librebooking_setup_details(AgentContext(tmp_path), {})


def test_admin_email_defaults_to_the_stack_contact_address():
    config = load_config("config/example.yaml")
    values = current_values(config, {"ACME_EMAIL": "contact@community.org"})

    assert values["services.librebooking.admin_email"] == "contact@community.org"


def test_doctor_skips_disabled_librebooking():
    healthy, messages = audit_librebooking(
        {"services": {"librebooking": {"enabled": False}}}, "unused.yaml", ".env"
    )

    assert healthy
    assert "disabled" in messages[0]


def test_legacy_config_can_enable_librebooking_without_rewriting_comments(tmp_path, capsys):
    config = load_config("config/example.yaml")
    config["services"].pop("librebooking")
    config_path = tmp_path / "community-stack.yaml"
    config_path.write_text(yaml.safe_dump(config) + "# preserve this\n", encoding="utf-8")

    assert run_service("enable", "librebooking", config_path, tmp_path / ".env") == 0

    upgraded = load_config(config_path)
    assert upgraded["services"]["librebooking"]["enabled"] is True
    assert upgraded["services"]["librebooking"]["version"] == "7.2.0-3.5.2"
    assert "# preserve this" in config_path.read_text(encoding="utf-8")
    assert "community-stack install" in capsys.readouterr().out


def test_librebooking_backup_includes_logical_dump_and_restores_persistent_files(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    config = enabled_config()
    config["operations"]["backups"]["encryption"] = "none"
    config_path = project / "community-stack.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    env_path = project / ".env"
    env_path.write_text(
        "LIBREBOOKING_DB_PASSWORD=db-password\n"
        "LIBREBOOKING_DB_ROOT_PASSWORD=root-password\n"
        "LIBREBOOKING_INSTALL_PASSWORD=install-password\n",
        encoding="utf-8",
    )
    for path in ("config", "uploads", "logs", "mariadb"):
        (project / "data/librebooking" / path).mkdir(parents=True)
    (project / "data/librebooking/config/config.php").write_text(
        "<?php return [];\n", encoding="utf-8"
    )
    (project / "data/librebooking/mariadb/ibdata1").write_bytes(b"live database")

    monkeypatch.setattr(backup, "is_docker_available", lambda: True)
    monkeypatch.setattr(
        backup.subprocess,
        "run",
        lambda args, **_kwargs: subprocess.CompletedProcess(args, 0, "true\n", ""),
    )
    monkeypatch.setattr(backup, "dump_postgres_database", lambda *_args, **_kwargs: False)

    def create_dump(target):
        target.write_text("CREATE TABLE reservations (id int);\n", encoding="utf-8")
        return True

    monkeypatch.setattr(backup, "dump_librebooking_database", create_dump)
    lifecycle = []

    def compose(project_dir, args, env_file=None, capture_output=False):
        lifecycle.append((Path(project_dir).name, list(args)))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(backup.docker, "run_compose", compose)
    engine = BackupEngine(config_path=config_path, env_path=env_path)

    snapshot = engine.create_snapshot()

    assert snapshot.db_included
    assert lifecycle == [
        ("librebooking", ["stop", "librebooking"]),
        ("librebooking", ["start", "librebooking"]),
    ]
    assert engine.verify_backup(snapshot.id)[0]
    restored = tmp_path / "restore"
    assert engine.restore_to_target(restored, snapshot.id, confirm=True)
    dump = restored / "backups/mariadb_librebooking.sql"
    assert dump.read_text(encoding="utf-8").startswith("CREATE TABLE reservations")
    assert (restored / "data/librebooking/config/config.php").is_file()
    assert not (restored / "data/librebooking/mariadb/ibdata1").exists()
