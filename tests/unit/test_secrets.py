"""Unit tests for secret generation, validation, and permissions."""

import os

from community_stack.secrets import (
    create_env_file,
    generate_secret,
    get_required_secrets,
    parse_env_file,
    validate_secrets,
)


def test_generate_secret():
    s1 = generate_secret(32)
    s2 = generate_secret(32)
    assert len(s1) == 64
    assert len(s2) == 64
    assert s1 != s2


def test_get_required_secrets():
    starter_cfg = {
        "services": {"matrix": {"enabled": True}, "whatsapp_bridge": {"enabled": False}},
        "operations": {"backups": {"enabled": True, "target": "local"}},
    }
    reqs = get_required_secrets(starter_cfg)
    assert "POSTGRES_PASSWORD" in reqs
    assert "SYNAPSE_SECRET_KEY" in reqs
    assert "RESTIC_PASSWORD" in reqs
    assert "WHATSAPP_AS_TOKEN" not in reqs


def test_whatsapp_appservice_tokens_are_provisioned_automatically():
    config = {"services": {"whatsapp_bridge": {"enabled": True}}}

    required = get_required_secrets(config)

    assert "WHATSAPP_AS_TOKEN" not in required
    assert "WHATSAPP_HS_TOKEN" not in required


def test_get_required_secrets_includes_configured_matrix_announcement_token():
    config = {
        "services": {
            "announcements": {
                "channels": {
                    "matrix_room": {
                        "enabled": True,
                        "access_token_env": "MATRIX_ANNOUNCEMENTS_TOKEN",
                    }
                }
            }
        }
    }
    assert "MATRIX_ANNOUNCEMENTS_TOKEN" in get_required_secrets(config)


def test_create_env_file(tmp_path):
    env_file = tmp_path / ".env"
    create_env_file(env_file)
    assert env_file.is_file()

    parsed = parse_env_file(env_file)
    assert "POSTGRES_PASSWORD" in parsed
    assert "SYNAPSE_SECRET_KEY" in parsed
    assert "RESTIC_PASSWORD" in parsed

    if os.name == "posix":
        mode = env_file.stat().st_mode & 0o777
        assert mode == 0o600


def test_validate_secrets_detects_placeholders(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "POSTGRES_PASSWORD=replace_with_password\n"
        "SYNAPSE_SECRET_KEY=replace_with_key\n"
        "SYNAPSE_REGISTRATION_SHARED_SECRET=replace_with_key\n"
        "RESTIC_PASSWORD=replace_with_pass\n",
        encoding="utf-8",
    )
    cfg = {
        "services": {"matrix": {"enabled": True}},
        "operations": {"backups": {"enabled": True, "target": "local"}},
    }
    is_valid, messages = validate_secrets(cfg, env_path=env_file)
    assert is_valid is False
    assert any("placeholder" in m for m in messages)


def test_validate_secrets_valid(tmp_path):
    env_file = tmp_path / ".env"
    create_env_file(env_file)
    cfg = {
        "services": {"matrix": {"enabled": True}},
        "operations": {"backups": {"enabled": True, "target": "local"}},
    }
    is_valid, messages = validate_secrets(cfg, env_path=env_file)
    assert is_valid is True
    assert messages == []
