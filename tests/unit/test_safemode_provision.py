"""Safe Mode provisioning and the host-side access command, run against a temporary project."""

from __future__ import annotations

import argparse
import subprocess

import pytest
import yaml
from community_stack import provision
from community_stack.commands import access_cmd
from community_stack.provision import ProvisionError
from community_stack.safemode import provision as safemode_provision
from community_stack.safemode.settings import BotConfig
from community_stack.safemode.store import ProtectedRoom, Store

CONFIG = {"services": {"matrix": {"enabled": True}}, "domain": {"matrix": "matrix.example.org"}}


@pytest.fixture
def docker_calls(monkeypatch):
    calls = []

    def fake_run(args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(provision.docker, "run_container", fake_run)
    monkeypatch.setattr(safemode_provision.docker, "run_container", fake_run)
    return calls


@pytest.fixture
def project(tmp_path, docker_calls):
    (tmp_path / "data" / "matrix").mkdir(parents=True)
    (tmp_path / "data" / "matrix" / "homeserver.yaml").write_text(
        "server_name: matrix.example.org\napp_service_config_files:\n  - /data/appservices/whatsapp.yaml\n",
        encoding="utf-8",
    )
    (tmp_path / "services" / "matrix").mkdir(parents=True)
    (tmp_path / "services" / "matrix" / "docker-compose.yml").write_text(
        "services:\n  synapse:\n    image: matrixdotorg/synapse:v1.163.2\n", encoding="utf-8"
    )
    return tmp_path


def run_prepare(project, env_path=None):
    return safemode_provision.prepare_safemode(
        project, CONFIG, env_path or project / ".env", project / "services"
    )


def test_first_run_creates_tokens_config_and_registration(project):
    env = project / ".env"
    message = run_prepare(project, env)
    assert "registered its bot" in message

    values = dict(line.split("=", 1) for line in env.read_text().splitlines() if "=" in line)
    assert len(values["SAFEMODE_AS_TOKEN"]) >= 40
    assert values["SAFEMODE_AS_TOKEN"] != values["SAFEMODE_HS_TOKEN"]

    config = BotConfig.load(project / "data" / "safemode" / "config.yaml")
    assert config.bot_user_id == "@safemode:matrix.example.org"
    assert config.homeserver_url == "http://matrix-synapse:8008"
    assert config.db_path == "/data/safemode.db"
    assert (project / "data" / "safemode" / "config.yaml").stat().st_mode & 0o777 == 0o600

    registration = yaml.safe_load(
        (project / "data" / "matrix" / "appservices" / "safemode.yaml").read_text()
    )
    assert registration["url"] == "http://community-safemode:8090"
    assert registration["sender_localpart"] == "safemode"
    assert registration["as_token"] == config.as_token
    assert registration["hs_token"] == config.hs_token
    assert registration["namespaces"]["users"][0]["exclusive"] is True
    assert "@safemode:matrix\\.example\\.org" in registration["namespaces"]["users"][0]["regex"]


def test_synapse_is_told_about_the_registration_exactly_once(project):
    run_prepare(project)
    homeserver = yaml.safe_load((project / "data" / "matrix" / "homeserver.yaml").read_text())
    assert homeserver["app_service_config_files"].count("/data/appservices/safemode.yaml") == 1
    assert "/data/appservices/whatsapp.yaml" in homeserver["app_service_config_files"]

    run_prepare(project)  # rerunning install must not duplicate or drop entries
    again = yaml.safe_load((project / "data" / "matrix" / "homeserver.yaml").read_text())
    assert again["app_service_config_files"].count("/data/appservices/safemode.yaml") == 1


def test_the_appservice_directory_is_handed_to_synapse_ownership(project, docker_calls):
    run_prepare(project)
    chown = [call for call in docker_calls if "chown" in call]
    assert chown, "appservice files must be owned by the Synapse user"
    assert "991:991" in chown[0]


def test_rerunning_keeps_the_same_tokens(project):
    env = project / ".env"
    run_prepare(project, env)
    first = BotConfig.load(project / "data" / "safemode" / "config.yaml").as_token
    run_prepare(project, env)
    assert BotConfig.load(project / "data" / "safemode" / "config.yaml").as_token == first


def test_mismatched_credentials_refuse_to_start(project):
    env = project / ".env"
    run_prepare(project, env)
    text = env.read_text().replace("SAFEMODE_AS_TOKEN=", "SAFEMODE_AS_TOKEN=changed-")
    env.write_text(text)
    with pytest.raises(ProvisionError, match="do not match"):
        run_prepare(project, env)


def test_matrix_must_be_enabled_and_provisioned(project):
    with pytest.raises(ProvisionError, match="Enable the Matrix"):
        safemode_provision.prepare_safemode(
            project,
            {"services": {"matrix": {"enabled": False}}},
            project / ".env",
            project / "services",
        )
    (project / "data" / "matrix" / "homeserver.yaml").unlink()
    with pytest.raises(ProvisionError, match="not been provisioned"):
        run_prepare(project)


def test_prepare_service_dispatches_safemode(project):
    message = provision.prepare_service(
        project, "safemode", CONFIG, project / ".env", project / "services"
    )
    assert message and "Safe Mode" in message


# Host access command -----------------------------------------------------


def _config(project):
    cfg = project / "data" / "safemode"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "config.yaml").write_text(
        yaml.safe_dump({"bot_user_id": "@safemode:matrix.example.org"})
    )
    return cfg / "safemode.db"


def args(**kwargs):
    base = {"dir": ".", "access_action": None}
    base.update(kwargs)
    return argparse.Namespace(**base)


def test_enroll_and_recovery_code_flow(project, capsys, monkeypatch):
    monkeypatch.chdir(project)
    _config(project)
    assert (
        access_cmd.run_access(args(access_action="enroll", user_id="@alice:matrix.example.org"))
        == 0
    )
    assert (
        access_cmd.run_access(
            args(access_action="recovery-code", user_id="@alice:matrix.example.org")
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "shown once" in out
    code = next(line.strip() for line in out.splitlines() if line.startswith("  "))
    assert len(code.replace("-", "")) == 16

    store = Store(project / "data" / "safemode" / "safemode.db")
    assert store.is_enrolled("@alice:matrix.example.org")
    assert store.recovery_code("@alice:matrix.example.org") is not None
    store.close()


def test_status_and_rooms_never_show_members_of_other_accounts(project, capsys, monkeypatch):
    monkeypatch.chdir(project)
    db = _config(project)
    store = Store(db)
    store.upsert_room(
        ProtectedRoom("!a:matrix.example.org", "Ops room", True, True, "joined", "11")
    )
    store.enroll("@alice:matrix.example.org")
    store.close()

    assert access_cmd.run_access(args(access_action="rooms")) == 0
    assert "Ops room" in capsys.readouterr().out
    assert (
        access_cmd.run_access(args(access_action="status", user_id="@alice:matrix.example.org"))
        == 0
    )
    assert "State: ACTIVE" in capsys.readouterr().out


def test_invalid_identifiers_are_rejected_before_any_write(project, capsys, monkeypatch):
    monkeypatch.chdir(project)
    db = _config(project)
    assert access_cmd.run_access(args(access_action="enroll", user_id="alice")) == 1
    assert "not a valid" in capsys.readouterr().out
    store = Store(db)
    assert not store.is_enrolled("alice")
    store.close()


def test_administrative_restore_without_confirmation_is_refused(project, capsys, monkeypatch):
    monkeypatch.chdir(project)
    _config(project)
    code = access_cmd.run_access(
        args(access_action="restore", user_id="@alice:matrix.example.org", confirm=False)
    )
    assert code == 1
    assert "needs --confirm" in capsys.readouterr().out


def test_matrix_commands_run_inside_the_bot_container(project, monkeypatch):
    seen = []

    def fake_call(command, **_kwargs):
        seen.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(access_cmd.subprocess, "run", fake_call)
    access_cmd.run_access(args(access_action="doctor"))
    access_cmd.run_access(
        args(access_action="protect", room_id="!a:matrix.example.org", name="Ops")
    )
    assert seen[0][:4] == ["docker", "exec", "community_safemode", "python"]
    assert seen[0][-1] == "doctor"
    assert seen[1][-4:] == ["protect", "!a:matrix.example.org", "--name", "Ops"]
