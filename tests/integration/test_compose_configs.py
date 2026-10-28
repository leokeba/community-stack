"""Integration tests validating Docker Compose services, pinning, and network isolation."""

import json
from pathlib import Path

import yaml


def get_compose_files() -> list[Path]:
    """Find all docker-compose.yml files in services/."""
    services_dir = Path("services")
    return list(services_dir.glob("*/docker-compose.yml"))


def test_compose_files_exist():
    files = get_compose_files()
    expected_services = {"proxy", "landing", "matrix", "element", "announcements"}
    found_services = {f.parent.name for f in files}
    assert expected_services.issubset(found_services), (
        f"Missing services: {expected_services - found_services}"
    )


def test_compose_files_are_valid_yaml():
    for f in get_compose_files():
        with open(f, "r", encoding="utf-8") as stream:
            data = yaml.safe_load(stream)
            assert isinstance(data, dict), f"{f} must parse to a dictionary"
            assert "services" in data, f"{f} must contain a 'services' block"


def test_images_are_strictly_pinned():
    """Ensure no service uses 'latest' or untagged images (ADR 0001, ADR 0002, Security Policy)."""
    for f in get_compose_files():
        with open(f, "r", encoding="utf-8") as stream:
            data = yaml.safe_load(stream)
            for svc_name, svc_cfg in data.get("services", {}).items():
                image = svc_cfg.get("image", "")
                assert image, f"Service '{svc_name}' in {f} must specify an image"
                assert not image.endswith(":latest"), (
                    f"Service '{svc_name}' in {f} uses forbidden 'latest' tag: {image}"
                )
                assert ":" in image or "@sha256:" in image, (
                    f"Service '{svc_name}' in {f} has untagged image: {image}"
                )


def test_all_services_have_healthchecks():
    """All production containers must define a healthcheck for monitoring and recovery."""
    for f in get_compose_files():
        with open(f, "r", encoding="utf-8") as stream:
            data = yaml.safe_load(stream)
            for svc_name, svc_cfg in data.get("services", {}).items():
                assert "healthcheck" in svc_cfg, f"Service '{svc_name}' in {f} lacks a healthcheck"


def test_database_is_on_internal_network_only():
    """PostgreSQL must only be on matrix_internal, never on proxy_net or host ports."""
    matrix_compose = Path("services/matrix/docker-compose.yml")
    with open(matrix_compose, "r", encoding="utf-8") as stream:
        data = yaml.safe_load(stream)
        postgres_cfg = data["services"]["postgres"]

        # Ensure no host port exposure
        assert "ports" not in postgres_cfg, "PostgreSQL must never expose host ports directly"

        # Ensure network is only matrix_internal
        networks = postgres_cfg.get("networks", [])
        assert networks == ["matrix_internal"], (
            f"PostgreSQL must only belong to matrix_internal, got {networks}"
        )


def test_admin_app_is_internal_and_has_no_host_ports():
    app_compose = Path("services/announcements/docker-compose.yml")
    with open(app_compose, "r", encoding="utf-8") as stream:
        data = yaml.safe_load(stream)
    app = data["services"]["announcements"]
    assert "ports" not in app
    assert app["user"] == "${COMMUNITY_STACK_UID:-1000}:${COMMUNITY_STACK_GID:-1000}"
    assert "proxy_net" in app["networks"]
    assert "announcements_internal" in app["networks"]
    assert data["networks"]["announcements_internal"]["internal"] is True
    assert "env_file" not in app, "The public admin app must not receive every secret"
    assert set(app["environment"]).isdisjoint(
        {"POSTGRES_PASSWORD", "SYNAPSE_REGISTRATION_SHARED_SECRET", "RESTIC_PASSWORD"}
    )


def test_proxy_does_not_expose_plain_http_federation_port():
    with open("services/proxy/docker-compose.yml", "r", encoding="utf-8") as stream:
        ports = yaml.safe_load(stream)["services"]["caddy"]["ports"]
    assert ports == ["80:80", "443:443"]
    caddyfile = Path("services/proxy/Caddyfile").read_text(encoding="utf-8")
    assert not any(line.startswith(":8448") for line in caddyfile.splitlines())


def test_caddyfile_exists_and_contains_routes():
    caddyfile = Path("services/proxy/Caddyfile")
    assert caddyfile.is_file()
    content = caddyfile.read_text(encoding="utf-8")
    assert "handle_path /app*" in content
    assert "handle /.well-known/matrix/client" in content
    assert "reverse_proxy community_synapse:8008" in content
    assert "handle /admin*" in content
    assert "reverse_proxy community_announcements:8080" in content


def test_element_config_is_valid_json():
    element_cfg = Path("services/element/config.json")
    assert element_cfg.is_file()
    with open(element_cfg, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert "default_server_config" in data
    assert "brand" in data
