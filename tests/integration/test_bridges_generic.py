"""The generic bridge path: catalogue, templates, provisioning, operations and settings."""

import re
import subprocess
from pathlib import Path

import pytest
import yaml
from community_stack import provision
from community_stack.agent import bridges, settings
from community_stack.agent.operations import CATALOG, AgentContext
from community_stack.bridges.catalogue import BRIDGE_SERVICES, MESSENGER, SIGNAL, WHATSAPP
from community_stack.bridges.catalogue import CATALOG as BRIDGES
from community_stack.secrets import parse_env_file

SUPPORTED = (WHATSAPP, SIGNAL, MESSENGER)
DNS_SAFE = re.compile(r"^[a-z0-9-]+$")


@pytest.mark.parametrize("spec", SUPPORTED, ids=lambda spec: spec.key)
def test_every_supported_bridge_is_pinned_and_declared_consistently(spec):
    assert spec.image.endswith(":v26.10"), "images are pinned to a dated release, never latest"
    compose = yaml.safe_load(Path(f"services/{spec.service}/docker-compose.yml").read_text())
    service = compose["services"][spec.service]
    assert service["image"] == spec.image
    assert service["container_name"] == spec.container
    alias = service["networks"]["bridge_net"]["aliases"][0]
    assert DNS_SAFE.match(alias), "appservice hostnames must not contain underscores"
    template = yaml.safe_load(
        Path(f"services/{spec.service}/config.template.yaml").read_text(encoding="utf-8")
    )
    assert template["appservice"]["id"] == spec.registration_file.removesuffix(".yaml")
    assert template["appservice"]["bot"]["username"] == spec.bot_localpart
    assert template["appservice"]["address"] == f"http://{alias}:29318"
    assert template["homeserver"]["address"] == "http://matrix-synapse:8008"
    assert template["bridge"]["portal_create_filter"] == {"mode": "allow", "list": []}
    assert template["bridge"]["relay"] == {"enabled": True, "admin_only": True}


def test_the_bridge_service_list_comes_from_the_catalogue():
    assert set(BRIDGE_SERVICES) == {"whatsapp_bridge", "signal_bridge", "meta_bridge"}
    assert BRIDGES["discord"].status == "deferred"
    assert BRIDGES["instagram"].status == "planned"


def test_templates_leave_no_unrendered_placeholders_for_each_bridge(tmp_path):
    from string import Template

    for spec in SUPPORTED:
        text = Path(f"services/{spec.service}/config.template.yaml").read_text(encoding="utf-8")
        rendered = Template(text).substitute(
            MATRIX_DOMAIN="matrix.test", AS_TOKEN="a", HS_TOKEN="h"
        )
        yaml.safe_load(rendered)


@pytest.mark.parametrize("spec", (SIGNAL, MESSENGER), ids=lambda spec: spec.key)
def test_provisioning_registers_a_new_bridge_with_its_own_tokens(tmp_path, monkeypatch, spec):
    root = tmp_path / "stack"
    env_path = root / ".env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text("EXISTING=keep\n", encoding="utf-8")
    config = yaml.safe_load(Path("config/example.yaml").read_text(encoding="utf-8"))
    config["domain"]["matrix"] = "matrix.test"
    matrix_dir = root / "data" / "matrix"
    matrix_dir.mkdir(parents=True)
    (matrix_dir / "homeserver.yaml").write_text("server_name: matrix.test\n", encoding="utf-8")

    def run_container(args):
        if "--entrypoint" not in args:
            volume = Path(args[args.index("-v") + 1].split(":", 1)[0])
            (volume / "registration.yaml").write_text(
                f"id: {spec.key}\nas_token: generated\nhs_token: generated\n", encoding="utf-8"
            )
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(provision.docker, "run_container", run_container)
    result = provision.prepare_bridge(root, config, env_path, Path("services"), spec)
    assert result and spec.name in result
    env = parse_env_file(env_path)
    assert len(env[f"{spec.secret_prefix}_AS_TOKEN"]) == 64
    assert len(env[f"{spec.secret_prefix}_HS_TOKEN"]) == 64
    assert env["EXISTING"] == "keep"
    rendered = yaml.safe_load((root / "data" / spec.service / "config.yaml").read_text())
    assert rendered["appservice"]["as_token"] == env[f"{spec.secret_prefix}_AS_TOKEN"]
    assert (matrix_dir / "appservices" / spec.registration_file).is_file()
    registrations = yaml.safe_load((matrix_dir / "homeserver.yaml").read_text())
    assert registrations == {
        "server_name": "matrix.test",
        "app_service_config_files": [f"/data/appservices/{spec.registration_file}"],
    }


def test_each_bridge_has_its_own_command_chat_and_state_file(tmp_path):
    context = AgentContext(tmp_path)
    assert bridges._state_path(context, SIGNAL) != bridges._state_path(context, WHATSAPP)
    assert bridges.command_chat_name(SIGNAL) == "Signal bridge commands"


def test_only_listed_commands_and_named_flows_reach_a_bridge():
    assert bridges.is_allowed_command(SIGNAL, "help")
    assert bridges.is_allowed_command(SIGNAL, "login")
    assert bridges.is_allowed_command(SIGNAL, "login qr-code")
    assert not bridges.is_allowed_command(SIGNAL, "logout")
    assert not bridges.is_allowed_command(SIGNAL, "login ../../etc")
    assert bridges.is_allowed_command(WHATSAPP, "login qr")
    assert not bridges.is_allowed_command(WHATSAPP, "login phone")


def test_operations_exist_for_every_supported_bridge():
    for spec in SUPPORTED:
        for action in ("status", "command", "rooms", "login_state", "room_relay"):
            assert f"bridge.{spec.key}.{action}" in CATALOG
    assert CATALOG["bridge.signal.command"].params[0].pattern.fullmatch("login")
    assert not CATALOG["bridge.signal.command"].params[0].pattern.fullmatch("logout")


def test_settings_offer_each_bridge_as_a_dangerous_switch():
    for key in ("services.signal_bridge.enabled", "services.meta_bridge.enabled"):
        setting = settings.SETTINGS[key]
        assert setting.kind == "bool" and setting.default is False and setting.dangerous


def test_the_schema_accepts_the_new_bridge_services():
    from community_stack.config import validate_config

    config = yaml.safe_load(Path("config/example.yaml").read_text(encoding="utf-8"))
    config.setdefault("services", {})["signal_bridge"] = {"enabled": False}
    config["services"]["meta_bridge"] = {"enabled": False}
    assert validate_config(config) == []


def test_meta_release_notes_point_at_the_upstream_tag():
    from community_stack.agent import updates

    assert updates.release_notes("dock.mau.dev/mautrix/meta:v26.10").endswith(
        "mautrix/meta/releases/tag/v0.2610.0"
    )
