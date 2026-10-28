"""Unit tests for diagnostic doctor command, disk audit, and network port audit."""

from pathlib import Path

from community_stack.commands.stubs import audit_disk_space, audit_network_isolation, run_doctor


def test_audit_disk_space(tmp_path):
    is_healthy, details = audit_disk_space(tmp_path)
    assert isinstance(is_healthy, bool)
    assert "GB free of" in details


def test_audit_network_isolation_no_docker(monkeypatch):
    monkeypatch.setattr("community_stack.commands.stubs.is_docker_available", lambda: False)
    findings = audit_network_isolation()
    assert len(findings) == 1
    assert "Docker not reachable" in findings[0]


def test_audit_network_isolation_secure(monkeypatch):
    monkeypatch.setattr("community_stack.commands.stubs.is_docker_available", lambda: True)
    monkeypatch.setattr(
        "community_stack.commands.stubs.list_community_containers",
        lambda: [
            {"name": "community_postgres", "status": "Up 2 hours"},
            {"name": "community_synapse", "status": "Up 2 hours"},
        ],
    )
    findings = audit_network_isolation()
    assert any("Internal network isolation verified" in f for f in findings)


def test_audit_network_isolation_flags_exposed_postgres(monkeypatch):
    monkeypatch.setattr("community_stack.commands.stubs.is_docker_available", lambda: True)
    monkeypatch.setattr(
        "community_stack.commands.stubs.list_community_containers",
        lambda: [
            {"name": "community_postgres", "status": "Up 2 hours, 0.0.0.0:5432->5432/tcp"},
        ],
    )
    findings = audit_network_isolation()
    assert any("CRITICAL" in f for f in findings)
    assert any("5432" in f for f in findings)


def test_run_doctor_redaction_and_output(tmp_path, capsys):
    cfg_file = tmp_path / "community-stack.yaml"
    cfg_file.write_text(Path("config/example.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text(
        "POSTGRES_PASSWORD=super_secret_raw_password\n"
        "SYNAPSE_SECRET_KEY=very_secret_signing_key\n"
        "SYNAPSE_REGISTRATION_SHARED_SECRET=reg_secret_12345\n"
        "RESTIC_PASSWORD=restic_secret_passphrase\n",
        encoding="utf-8",
    )

    ret = run_doctor(config_path=cfg_file, env_path=env_file)
    assert ret == 0
    captured = capsys.readouterr()

    assert "DIAGNOSTIC DOCTOR REPORT" in captured.out
    assert "STORAGE & DISK CAPACITY AUDIT" in captured.out
    assert "NETWORK ISOLATION & PORT AUDIT" in captured.out

    # Critical security assertion: raw passwords MUST NEVER be printed in doctor reports
    assert "super_secret_raw_password" not in captured.out
    assert "very_secret_signing_key" not in captured.out
    assert "reg_secret_12345" not in captured.out
    assert "restic_secret_passphrase" not in captured.out
    assert "[REDACTED]" in captured.out
    assert "very_secret_signing_key" not in captured.out
    assert "[REDACTED]" in captured.out
