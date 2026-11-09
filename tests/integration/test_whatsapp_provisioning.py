import os
import subprocess
from pathlib import Path

import yaml
from community_stack import provision
from community_stack.secrets import parse_env_file


def test_prepare_whatsapp_provisions_secrets_config_and_synapse_registration(tmp_path, monkeypatch):
    root = tmp_path / "stack"
    env_path = root / ".env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text("EXISTING_SETTING=keep\n", encoding="utf-8")
    config = yaml.safe_load(Path("config/example.yaml").read_text(encoding="utf-8"))
    config["domain"]["matrix"] = "matrix.community.test"
    matrix_dir = root / "data" / "matrix"
    matrix_dir.mkdir(parents=True)
    homeserver_path = matrix_dir / "homeserver.yaml"
    homeserver_path.write_text(
        "server_name: matrix.community.test\n"
        "app_service_config_files:\n"
        "  - /data/appservices/existing.yaml\n",
        encoding="utf-8",
    )
    calls = []

    def run_container(args):
        calls.append(args)
        if "--entrypoint" not in args:
            volume = Path(args[args.index("-v") + 1].split(":", 1)[0])
            (volume / "registration.yaml").write_text(
                "id: whatsapp\nas_token: generated-as-token\nhs_token: generated-hs-token\n",
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(provision.docker, "run_container", run_container)

    result = provision.prepare_whatsapp(root, config, env_path, Path("services"))

    assert result and "registered it with Synapse" in result
    env = parse_env_file(env_path)
    assert len(env["WHATSAPP_AS_TOKEN"]) == 64
    assert len(env["WHATSAPP_HS_TOKEN"]) == 64
    assert env["EXISTING_SETTING"] == "keep"
    assert (
        yaml.safe_load((root / "data/whatsapp_bridge/config.yaml").read_text())["homeserver"][
            "domain"
        ]
        == "matrix.community.test"
    )
    bridge_config = yaml.safe_load(
        (root / "data/whatsapp_bridge/config.yaml").read_text(encoding="utf-8")
    )
    assert bridge_config["history_sync"]["max_initial_conversations"] == 0
    assert bridge_config["history_sync"]["request_full_sync"] is False
    assert bridge_config["bridge"]["portal_create_filter"] == {"mode": "allow", "list": []}
    registrations = yaml.safe_load(homeserver_path.read_text())["app_service_config_files"]
    assert registrations == [
        "/data/appservices/existing.yaml",
        "/data/appservices/whatsapp.yaml",
    ]
    assert (matrix_dir / "appservices/whatsapp.yaml").is_file()
    assert len(calls) == 2
    if os.name == "posix":
        assert env_path.stat().st_mode & 0o777 == 0o600

    assert provision.prepare_whatsapp(root, config, env_path, Path("services")) is None
    assert len(calls) == 3


def test_prepare_whatsapp_migrates_existing_config_without_losing_allowlist(tmp_path, monkeypatch):
    root = tmp_path / "stack"
    bridge_dir = root / "data" / "whatsapp_bridge"
    bridge_dir.mkdir(parents=True)
    bridge_config_path = bridge_dir / "config.yaml"
    bridge_config_path.write_text(
        yaml.safe_dump(
            {
                "homeserver": {"domain": "community.test"},
                "bridge": {
                    "history_sync": {"max_initial_conversations": 0},
                    "portal_create_filter": {
                        "mode": "allow",
                        "list": ["123456789@g.us"],
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    env_path = root / ".env"
    env_path.write_text(
        "WHATSAPP_AS_TOKEN=existing-as-token\nWHATSAPP_HS_TOKEN=existing-hs-token\n",
        encoding="utf-8",
    )
    config = yaml.safe_load(Path("config/example.yaml").read_text(encoding="utf-8"))
    config["domain"]["matrix"] = "community.test"
    matrix_dir = root / "data" / "matrix"
    matrix_dir.mkdir(parents=True)
    homeserver_path = matrix_dir / "homeserver.yaml"
    homeserver_path.write_text(
        "server_name: community.test\napp_service_config_files:\n  - /data/appservices/whatsapp.yaml\n",
        encoding="utf-8",
    )
    (bridge_dir / "registration.yaml").write_text(
        "id: whatsapp\nas_token: generated-as-token\nhs_token: generated-hs-token\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        provision.docker,
        "run_container",
        lambda args: subprocess.CompletedProcess(args, 0, "", ""),
    )

    provision.prepare_whatsapp(root, config, env_path, Path("services"))

    migrated = yaml.safe_load(bridge_config_path.read_text(encoding="utf-8"))
    assert migrated["history_sync"]["max_initial_conversations"] == 0
    assert migrated["history_sync"]["request_full_sync"] is False
    assert "history_sync" not in migrated["bridge"]
    assert migrated["bridge"]["portal_create_filter"] == {
        "mode": "allow",
        "list": ["123456789@g.us"],
    }


def test_prepare_whatsapp_requires_matrix_and_does_not_replace_existing_bridge_data(tmp_path):
    root = tmp_path / "stack"
    config = yaml.safe_load(Path("config/example.yaml").read_text(encoding="utf-8"))
    config["services"]["matrix"]["enabled"] = False

    try:
        provision.prepare_whatsapp(root, config, root / ".env", Path("services"))
    except provision.ProvisionError as exc:
        assert "Enable the Matrix server" in str(exc)
    else:
        raise AssertionError("Expected WhatsApp provisioning to require Matrix.")
