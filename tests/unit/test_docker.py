"""Unit tests for Docker command execution and helpers."""

import subprocess

from community_stack.docker import (
    ensure_network,
    get_compose_command,
    is_docker_available,
    list_community_containers,
    run_compose,
)


def test_is_docker_available_true(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/docker")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args=[], returncode=0),
    )
    assert is_docker_available() is True


def test_is_docker_available_missing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cmd: None)
    assert is_docker_available() is False


def test_get_compose_command_v2(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/docker" if cmd == "docker" else None)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args=[], returncode=0),
    )
    cmd = get_compose_command()
    assert cmd == ["docker", "compose"]


def test_get_compose_command_v1(monkeypatch):
    def fake_which(cmd):
        if cmd == "docker-compose":
            return "/usr/bin/docker-compose"
        return None

    monkeypatch.setattr("shutil.which", fake_which)
    cmd = get_compose_command()
    assert cmd == ["docker-compose"]


def test_run_compose_invokes_correct_args(tmp_path, monkeypatch):
    calls = []

    def fake_run(cmd, *args, **kwargs):
        calls.append({"cmd": cmd, "cwd": kwargs.get("cwd")})
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/docker")

    env_file = tmp_path / ".env"
    env_file.write_text("FOO=BAR\n", encoding="utf-8")

    res = run_compose(
        project_dir=tmp_path,
        args=["up", "-d"],
        env_file=env_file,
    )
    assert res.returncode == 0
    assert len(calls) == 2
    assert calls[1]["cwd"] == str(tmp_path)
    assert "--env-file" in calls[1]["cmd"]


def test_ensure_network(monkeypatch):
    # Network already exists
    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/docker")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args=[], returncode=0),
    )
    assert ensure_network("proxy_net") is True


def test_list_community_containers(monkeypatch):
    raw_output = "community_proxy\tcaddy:2.8.4-alpine\tUp 10m (healthy)\trunning\ncommunity_synapse\tmatrixdotorg/synapse:v1.119.0\tUp 10m\trunning"

    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/docker")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout=raw_output
        ),
    )

    containers = list_community_containers()
    assert len(containers) == 2
    assert containers[0]["name"] == "community_proxy"
    assert containers[0]["state"] == "running"
    assert containers[1]["name"] == "community_synapse"
