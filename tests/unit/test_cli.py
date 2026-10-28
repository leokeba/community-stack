"""Unit tests for community-stack CLI commands."""

import pytest
from community_stack.cli import main


def test_cli_no_args(capsys):
    ret = main([])
    assert ret == 0
    captured = capsys.readouterr()
    assert "community-stack" in captured.out


def test_cli_help(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "community-stack" in captured.out
    assert "init" in captured.out
    assert "check" in captured.out
    assert "plan" in captured.out


def test_cli_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "0.1.0" in captured.out


def test_cli_init_command(tmp_path, capsys):
    ret = main(["init", "--dir", str(tmp_path)])
    assert ret == 0
    captured = capsys.readouterr()
    assert "[CREATE] community-stack.yaml" in captured.out
    assert (tmp_path / "community-stack.yaml").is_file()
    assert (tmp_path / ".env").is_file()
    assert (tmp_path / "backups").is_dir()
    assert (tmp_path / "data").is_dir()

    # Re-running without --force should skip
    ret_skip = main(["init", "--dir", str(tmp_path)])
    assert ret_skip == 0
    captured2 = capsys.readouterr()
    assert "[SKIP]" in captured2.out

    # Re-running with --force should overwrite
    ret_force = main(["init", "--dir", str(tmp_path), "--force"])
    assert ret_force == 0
    captured3 = capsys.readouterr()
    assert "[CREATE]" in captured3.out


def test_cli_check_command(tmp_path, capsys):
    main(["init", "--dir", str(tmp_path)])
    config_file = tmp_path / "community-stack.yaml"
    env_file = tmp_path / ".env"

    ret = main(["check", "-c", str(config_file), "-e", str(env_file), "--skip-dns"])
    assert ret == 0
    captured = capsys.readouterr()
    assert "All checks passed successfully" in captured.out


def test_cli_check_command_failure(tmp_path, capsys):
    main(["init", "--dir", str(tmp_path)])
    config_file = tmp_path / "community-stack.yaml"
    # Overwrite .env with empty file to trigger failure
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")

    ret = main(["check", "-c", str(config_file), "-e", str(env_file), "--skip-dns"])
    assert ret == 1
    captured = capsys.readouterr()
    assert "Check failed" in captured.out


def test_cli_plan_command(tmp_path, capsys):
    main(["init", "--dir", str(tmp_path)])
    config_file = tmp_path / "community-stack.yaml"
    env_file = tmp_path / ".env"

    ret = main(["plan", "-c", str(config_file), "-e", str(env_file)])
    assert ret == 0
    captured = capsys.readouterr()
    assert "COMMUNITY COMMUNICATIONS STACK PLAN" in captured.out
    assert "Matrix Homeserver (Synapse" in captured.out
    assert "Caddy" in captured.out


def test_cli_plan_invalid_config(tmp_path, capsys):
    ret = main(["plan", "-c", str(tmp_path / "nonexistent.yaml")])
    assert ret == 1
    captured = capsys.readouterr()
    assert "Error loading configuration" in captured.out


def test_cli_doctor_command(tmp_path, capsys):
    main(["init", "--dir", str(tmp_path)])
    config_file = tmp_path / "community-stack.yaml"
    env_file = tmp_path / ".env"

    ret = main(["doctor", "-c", str(config_file), "-e", str(env_file)])
    assert ret == 0
    captured = capsys.readouterr()
    assert "COMMUNITY STACK DIAGNOSTIC DOCTOR REPORT" in captured.out
    assert "[REDACTED]" in captured.out


def test_cli_stub_commands(tmp_path, capsys, monkeypatch):
    import subprocess

    from community_stack import docker

    # Mock docker calls so unit tests don't mutate host containers
    monkeypatch.setattr(docker, "is_docker_available", lambda: True)
    monkeypatch.setattr(docker, "ensure_network", lambda net="proxy_net": True)
    monkeypatch.setattr(
        docker,
        "run_compose",
        lambda project_dir, args, env_file=None, capture_output=False: subprocess.CompletedProcess(
            args=["docker", "compose"], returncode=0, stdout="mock success", stderr=""
        ),
    )
    monkeypatch.setattr(
        docker,
        "list_community_containers",
        lambda: [
            {
                "name": "community_proxy",
                "image": "caddy:2.8.4-alpine",
                "status": "Up 2 hours",
                "state": "running",
            },
            {
                "name": "community_synapse",
                "image": "matrixdotorg/synapse:v1.119.0",
                "status": "Up 2 hours",
                "state": "running",
            },
        ],
    )

    main(["init", "--dir", str(tmp_path)])
    config_file = tmp_path / "community-stack.yaml"
    env_file = tmp_path / ".env"

    # install
    assert main(["install", "-c", str(config_file), "-e", str(env_file)]) == 0

    # status
    assert main(["status"]) == 0

    # logs
    assert main(["logs", "matrix"]) == 0

    # backup
    assert main(["backup", "-c", str(config_file)]) == 0
    assert main(["backup", "verify"]) == 0

    # upgrade
    assert main(["upgrade", "matrix"]) == 0

    # restore without confirm should fail
    assert main(["restore", "--target", str(tmp_path / "restore_test")]) == 1
    # restore with confirm should succeed stub
    assert main(["restore", "--target", str(tmp_path / "restore_test"), "--confirm"]) == 0
