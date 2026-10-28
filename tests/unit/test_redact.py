"""Unit tests for secret redaction helpers."""

from community_stack.redact import is_sensitive_key, redact_dict, redact_text, redact_value


def test_is_sensitive_key():
    assert is_sensitive_key("POSTGRES_PASSWORD") is True
    assert is_sensitive_key("synapse_secret_key") is True
    assert is_sensitive_key("API_TOKEN") is True
    assert is_sensitive_key("domain_name") is False
    assert is_sensitive_key("port") is False


def test_redact_value():
    assert redact_value("my_super_secret") == "[REDACTED]"
    assert redact_value("") == ""
    assert redact_value(None) is None


def test_redact_dict():
    raw = {
        "project": {"name": "test"},
        "credentials": {
            "postgres_password": "supersecretpassword",
            "normal_user": "synapse",
        },
        "tokens": [
            {"token": "secret123", "id": 1},
            {"name": "plain"},
        ],
    }
    redacted = redact_dict(raw)
    assert redacted["project"]["name"] == "test"
    assert redacted["credentials"]["postgres_password"] == "[REDACTED]"
    assert redacted["credentials"]["normal_user"] == "synapse"
    assert redacted["tokens"][0]["token"] == "[REDACTED]"
    assert redacted["tokens"][0]["id"] == 1
    assert redacted["tokens"][1]["name"] == "plain"


def test_redact_text():
    secret = "a9b8c7d6e5f412345678"
    log_line = f"Connecting to db with password={secret} on host 127.0.0.1"
    cleaned = redact_text(log_line, known_secrets=[secret])
    assert secret not in cleaned
    assert "[REDACTED]" in cleaned
