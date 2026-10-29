"""First-run Synapse and Element configuration, generated from community-stack.yaml."""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import yaml
from community_stack import docker, provision

ENV = (
    "POSTGRES_DB=synapse\nPOSTGRES_USER=synapse_user\nPOSTGRES_PASSWORD=pg-secret-123\n"
    "SYNAPSE_SECRET_KEY=macaroon-secret\nSYNAPSE_REGISTRATION_SHARED_SECRET=reg-secret\n"
)


def config(matrix_domain="matrix.ourtown.org", federation=False, registration=False):
    return {
        "project": {"name": "ourtown"},
        "domain": {"matrix": matrix_domain, "web": "ourtown.org"},
        "services": {
            "matrix": {
                "federation": federation,
                "allow_registration": registration,
                "max_upload_size_mb": 25,
            }
        },
    }


def test_homeserver_config_uses_domains_secrets_and_policy():
    rendered = provision.homeserver_config(
        Path("services"),
        config(federation=False),
        dict(line.split("=", 1) for line in ENV.strip().splitlines()),
    )
    loaded = yaml.safe_load(rendered)
    assert loaded["server_name"] == "matrix.ourtown.org"
    assert loaded["public_baseurl"] == "https://matrix.ourtown.org/"
    assert loaded["database"]["args"]["password"] == "pg-secret-123"
    assert loaded["federation_domain_whitelist"] == []
    assert loaded["enable_registration"] is False
    assert loaded["max_upload_size"] == "25M"
    assert loaded["report_stats"] is False

    open_federation = yaml.safe_load(
        provision.homeserver_config(
            Path("services"),
            config(federation=True),
            dict(line.split("=", 1) for line in ENV.strip().splitlines()),
        )
    )
    assert open_federation["federation_domain_whitelist"] is None


def test_missing_secrets_stop_provisioning():
    with pytest.raises(provision.ProvisionError, match="POSTGRES_PASSWORD"):
        provision.homeserver_config(Path("services"), config(), {"POSTGRES_USER": "u"})


def test_prepare_matrix_writes_config_runs_synapse_and_never_overwrites(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(ENV, encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        docker,
        "run_container",
        lambda args: calls.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )

    message = provision.prepare_matrix(tmp_path, config(), env, Path("services"))

    assert "matrix.ourtown.org" in message
    homeserver = tmp_path / "data" / "matrix" / "homeserver.yaml"
    assert homeserver.is_file() and (tmp_path / "data" / "matrix" / "log.config").is_file()
    assert "--generate-keys" in calls[0]
    assert calls[1][-3:] == ["-R", "991:991", "/data"]

    homeserver.write_text("operator: edited\n", encoding="utf-8")
    assert provision.prepare_matrix(tmp_path, config(), env, Path("services")) is None
    assert homeserver.read_text(encoding="utf-8") == "operator: edited\n"


def test_failing_synapse_container_is_reported(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(ENV, encoding="utf-8")
    monkeypatch.setattr(
        docker,
        "run_container",
        lambda args: subprocess.CompletedProcess(args, 1, "", "Error: bad config\n"),
    )
    with pytest.raises(provision.ProvisionError, match="bad config"):
        provision.prepare_matrix(tmp_path, config(), env, Path("services"))


def test_element_points_at_this_homeserver(tmp_path):
    provision.prepare_element(tmp_path, config())
    data = json.loads((tmp_path / "data" / "element" / "config.json").read_text())
    assert data["default_server_config"]["m.homeserver"] == {
        "base_url": "https://matrix.ourtown.org",
        "server_name": "matrix.ourtown.org",
    }
    assert provision.prepare_element(tmp_path, config(matrix_domain="other.org")) is None


def test_development_installs_point_element_at_localhost():
    data = provision.element_config(config(matrix_domain="matrix.example.org"))
    assert data["default_server_config"]["m.homeserver"]["base_url"] == "http://localhost"


def run(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), check=False, capture_output=True, text=True, **kwargs)


@pytest.mark.skipif(
    not os.environ.get("COMMUNITY_STACK_DOCKER_TESTS"),
    reason="set COMMUNITY_STACK_DOCKER_TESTS=1 to start real Synapse and Postgres containers",
)
def test_generated_config_boots_a_real_synapse(tmp_path):
    """Opt-in: generate the configuration, then start Postgres and Synapse with it."""
    project = tmp_path / "stack"
    shutil.copytree(Path("services"), project / "services")
    # Use names that cannot collide with a real stack running on the same machine.
    compose_file = project / "services" / "matrix" / "docker-compose.yml"
    compose_file.write_text(
        compose_file.read_text(encoding="utf-8")
        .replace("community_", "csprovisiontest_")
        .replace("name: matrix_internal", "name: csprovisiontest_internal"),
        encoding="utf-8",
    )
    env = project / ".env"
    env.write_text(ENV, encoding="utf-8")
    services = project / "services"
    provision.prepare_matrix(project, config("matrix.localtest.me"), env, services)
    compose = ("docker", "compose", "-p", "csprovisiontest", "--env-file", str(env))
    run("docker", "network", "create", "proxy_net")
    try:
        assert run(*compose, "up", "-d", cwd=services / "matrix").returncode == 0
        deadline = time.monotonic() + 180
        status = ""
        while time.monotonic() < deadline and status not in ("healthy", "unhealthy"):
            time.sleep(3)
            status = run(
                "docker",
                "inspect",
                "csprovisiontest_synapse",
                "--format",
                "{{.State.Health.Status}}",
            ).stdout.strip()
        logs = run("docker", "logs", "--tail", "40", "csprovisiontest_synapse")
        assert status == "healthy", logs.stdout + logs.stderr
    finally:
        run(*compose, "down", "-v", cwd=services / "matrix")
        # Synapse and Postgres wrote these as other users; remove them from inside a container.
        run(
            "docker",
            "run",
            "--rm",
            "-v",
            f"{project / 'data'}:/d",
            "alpine",
            "rm",
            "-rf",
            "/d/matrix",
            "/d/postgres",
        )
