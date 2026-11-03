"""Vikunja provisioning, lifecycle, diagnostics, and recovery tests."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml
from community_stack import backup
from community_stack.backup import BackupEngine, BackupError
from community_stack.cli import main
from community_stack.commands.doctor_cmd import audit_vikunja
from community_stack.commands.service_cmd import run_service
from community_stack.config import load_config
from community_stack.provision import ProvisionError, prepare_vikunja
from community_stack.secrets import parse_env_file


def test_prepare_vikunja_generates_secrets_and_persistent_paths(tmp_path):
    config = load_config("community-stack.yaml")
    config["services"]["vikunja"].update(enabled=True, hostname="tasks.community.org")
    env_file = tmp_path / ".env"
    env_file.write_text("RESTIC_PASSWORD=keep-this\n", encoding="utf-8")
    env_file.chmod(0o644)

    message = prepare_vikunja(tmp_path, config, env_file)

    secrets = parse_env_file(env_file)
    assert secrets["RESTIC_PASSWORD"] == "keep-this"
    assert len(secrets["VIKUNJA_POSTGRES_PASSWORD"]) >= 64
    assert len(secrets["VIKUNJA_SERVICE_SECRET"]) >= 64
    assert secrets["VIKUNJA_DOMAIN"] == "tasks.community.org"
    assert secrets["VIKUNJA_ALLOW_REGISTRATION"] == "false"
    assert secrets["VIKUNJA_ENABLE_LINK_SHARING"] == "false"
    assert "https://tasks.community.org/" in message
    assert (tmp_path / "data/vikunja/files").is_dir()
    assert (tmp_path / "data/vikunja/postgres").is_dir()
    assert env_file.stat().st_mode & 0o777 == 0o600


def test_prepare_vikunja_refuses_to_regenerate_secrets_for_existing_data(tmp_path):
    config = load_config("community-stack.yaml")
    config["services"]["vikunja"]["enabled"] = True
    database = tmp_path / "data/vikunja/postgres"
    database.mkdir(parents=True)
    (database / "PG_VERSION").write_text("16\n", encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text("RESTIC_PASSWORD=keep-this\n", encoding="utf-8")

    with pytest.raises(ProvisionError, match="persistent data exists"):
        prepare_vikunja(tmp_path, config, env_file)


def test_vikunja_doctor_skips_disabled_service():
    healthy, messages = audit_vikunja(
        {"services": {"vikunja": {"enabled": False}}}, "unused.yaml", ".env"
    )
    assert healthy
    assert "disabled" in messages[0]


def test_vikunja_can_be_enabled_from_an_older_configuration(tmp_path, capsys):
    config = yaml.safe_load(Path("config/example.yaml").read_text(encoding="utf-8"))
    config["services"].pop("vikunja")
    config_path = tmp_path / "community-stack.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    assert run_service("enable", "vikunja", config_path, tmp_path / ".env") == 0
    upgraded = load_config(config_path)
    assert upgraded["services"]["vikunja"]["enabled"] is True
    assert "community-stack install" in capsys.readouterr().out


def test_starting_disabled_vikunja_is_reported(tmp_path, capsys):
    config_path = tmp_path / "community-stack.yaml"
    config_path.write_text(Path("config/example.yaml").read_text(encoding="utf-8"))

    assert run_service("start", "vikunja", config_path, tmp_path / ".env") == 1
    assert "disabled" in capsys.readouterr().out


def test_disabling_vikunja_stops_it_and_removes_its_route(tmp_path, monkeypatch, capsys):
    config = yaml.safe_load(Path("config/example.yaml").read_text(encoding="utf-8"))
    config["services"]["vikunja"]["enabled"] = True
    config_path = tmp_path / "community-stack.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    route_path = tmp_path / "data/proxy/vikunja.caddy"
    route_path.parent.mkdir(parents=True)
    route_path.write_text(
        "tasks.example.org {\n    reverse_proxy community_vikunja:3456\n}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "community_stack.commands.service_cmd.docker.is_docker_available", lambda: True
    )
    calls: list[tuple[str, list[str]]] = []

    def compose(project_dir, args, env_file=None, capture_output=False):
        calls.append((Path(project_dir).name, args))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("community_stack.commands.service_cmd.docker.run_compose", compose)

    assert run_service("disable", "vikunja", config_path, tmp_path / ".env") == 0
    assert load_config(config_path)["services"]["vikunja"]["enabled"] is False
    assert "disabled" in route_path.read_text(encoding="utf-8")
    assert calls == [("vikunja", ["down", "--remove-orphans"]), ("proxy", ["restart", "caddy"])]
    assert "Persistent data was preserved" in capsys.readouterr().out


def test_install_generates_vikunja_secrets_before_preflight(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["init", "--dir", str(tmp_path)]) == 0
    config_path = tmp_path / "community-stack.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["services"]["vikunja"]["enabled"] = True
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    from community_stack import docker

    monkeypatch.setattr(docker, "is_docker_available", lambda: True)
    monkeypatch.setattr(docker, "ensure_network", lambda _network: True)
    monkeypatch.setattr(
        docker,
        "run_container",
        lambda args: subprocess.CompletedProcess(args, 0, "", ""),
    )
    deployed: list[str] = []

    def compose(project_dir, args, env_file=None, capture_output=False):
        if args and args[0] == "up":
            deployed.append(Path(project_dir).name)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(docker, "run_compose", compose)
    monkeypatch.setattr(docker, "list_community_containers", list)

    assert main(["install", "-c", str(config_path), "-e", str(tmp_path / ".env")]) == 0
    secrets = parse_env_file(tmp_path / ".env")
    assert secrets["VIKUNJA_POSTGRES_PASSWORD"]
    assert secrets["VIKUNJA_SERVICE_SECRET"]
    assert "vikunja" in deployed
    assert "https://tasks.example.org/" in capsys.readouterr().out


def test_vikunja_backup_and_isolated_restore_include_database_and_files(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    config = yaml.safe_load(Path("config/example.yaml").read_text(encoding="utf-8"))
    config["services"]["vikunja"]["enabled"] = True
    config["operations"]["backups"]["encryption"] = "none"
    config_path = project / "community-stack.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    env_file = project / ".env"
    env_file.write_text(
        "POSTGRES_PASSWORD=matrix-password\n"
        "RESTIC_PASSWORD=unused-test-restic-password\n"
        "VIKUNJA_POSTGRES_PASSWORD=vikunja-password\n"
        "VIKUNJA_SERVICE_SECRET=vikunja-service-secret\n",
        encoding="utf-8",
    )
    if os.name == "posix":
        env_file.chmod(0o600)
    attachment_dir = project / "data/vikunja/files"
    attachment_dir.mkdir(parents=True)
    (attachment_dir / "attachment.txt").write_text("task attachment", encoding="utf-8")
    database_dir = project / "data/vikunja/postgres"
    database_dir.mkdir(parents=True)
    (database_dir / "PG_VERSION").write_text("16\n", encoding="utf-8")

    monkeypatch.setattr(backup, "is_docker_available", lambda: True)
    monkeypatch.setattr(
        backup.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, "true\n", ""),
    )

    def create_vikunja_dump(target: Path) -> bool:
        target.write_bytes(b"custom-format-vikunja-dump")
        return True

    monkeypatch.setattr(backup, "dump_vikunja_database", create_vikunja_dump)
    lifecycle: list[list[str]] = []

    def compose(project_dir, args, env_file=None, capture_output=False):
        lifecycle.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(backup.docker, "run_compose", compose)
    engine = BackupEngine(config_path=config_path, env_path=env_file)

    snapshot = engine.create_snapshot()
    assert lifecycle == [["stop", "vikunja"], ["start", "vikunja"]]
    assert snapshot.id.startswith("snap_")
    assert engine.verify_backup(snapshot.id)[0]

    restored = tmp_path / "restore"
    assert engine.restore_to_target(restored, snapshot.id, confirm=True)
    assert (
        restored / "backups/postgres_vikunja.dump"
    ).read_bytes() == b"custom-format-vikunja-dump"
    assert (restored / "data/vikunja/files/attachment.txt").read_text(
        encoding="utf-8"
    ) == "task attachment"
    assert not (restored / "data/vikunja/postgres/PG_VERSION").exists()


def test_vikunja_backup_fails_if_database_dump_is_missing(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    config = yaml.safe_load(Path("config/example.yaml").read_text(encoding="utf-8"))
    config["services"]["vikunja"]["enabled"] = True
    config_path = project / "community-stack.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    env_file = project / ".env"
    env_file.write_text("RESTIC_PASSWORD=test-password\n", encoding="utf-8")
    if os.name == "posix":
        env_file.chmod(0o600)
    (project / "data/vikunja/files").mkdir(parents=True)
    monkeypatch.setattr(backup, "is_docker_available", lambda: True)
    monkeypatch.setattr(
        backup.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, "false\n", ""),
    )
    monkeypatch.setattr(backup, "dump_vikunja_database", lambda _target: False)

    engine = BackupEngine(config_path=config_path, env_path=env_file)
    with pytest.raises(BackupError, match="could not be stopped|PostgreSQL dump"):
        engine.create_snapshot()
