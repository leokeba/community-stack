"""Integration tests validating Docker Compose services, pinning, and network isolation."""

from pathlib import Path

import yaml


def get_compose_files() -> list[Path]:
    """Find all docker-compose.yml files in services/."""
    services_dir = Path("services")
    return list(services_dir.glob("*/docker-compose.yml"))


def test_compose_files_exist():
    files = get_compose_files()
    expected_services = {
        "proxy",
        "landing",
        "matrix",
        "element",
        "announcements",
        "vikunja",
        "facilmap",
    }
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


def test_landing_feed_mountpoint_exists_in_read_only_html_volume():
    assert Path("services/landing/html/feed").is_dir()


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
                if f.parent.name == "vikunja" and svc_name == "vikunja":
                    # The official Vikunja image is `scratch` and contains no shell or probe binary.
                    continue
                if f.parent.name == "facilmap" and svc_name == "facilmap":
                    # The pinned official image defines its own Socket.IO health check.
                    continue
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


def test_vikunja_database_and_application_are_private_and_pinned():
    with open("services/vikunja/docker-compose.yml", "r", encoding="utf-8") as stream:
        compose = yaml.safe_load(stream)
    database = compose["services"]["vikunja-db"]
    app = compose["services"]["vikunja"]
    assert database["image"] == "postgres:16-alpine"
    assert database["networks"] == ["vikunja_internal"]
    assert "ports" not in database
    assert app["image"] == "vikunja/vikunja:${VIKUNJA_VERSION:-2.7.1}"
    assert "ports" not in app
    assert set(app["networks"]) == {"proxy_net", "vikunja_internal"}
    assert app["depends_on"]["vikunja-db"]["condition"] == "service_healthy"
    assert database["healthcheck"]
    assert compose["networks"]["vikunja_internal"]["internal"] is True
    assert (
        "../../${VIKUNJA_FILES_DATA_PATH:-data/vikunja/files}:/app/vikunja/files" in app["volumes"]
    )
    assert "VIKUNJA_SERVICE_ENABLELINKSHARING" in app["environment"]


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
    assert "../../data/agent/run:/run/community-stack-agent" in app["volumes"]
    assert not any("docker.sock" in volume for volume in app["volumes"])
    assert set(app["environment"]).isdisjoint(
        {"POSTGRES_PASSWORD", "SYNAPSE_REGISTRATION_SHARED_SECRET", "RESTIC_PASSWORD"}
    )


def test_image_build_context_excludes_caches_secrets_and_data():
    ignored = Path(".dockerignore").read_text(encoding="utf-8").splitlines()
    for pattern in ("**/__pycache__", ".env", "data", "backups"):
        assert pattern in ignored


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
    assert "import /etc/caddy/vikunja.caddy" in content
    assert "reverse_proxy community_synapse:8008" in content
    assert "handle /admin*" in content
    assert "reverse_proxy community_announcements:8080" in content
    assert "max_size 12MB" in content
    assert "max_size 256KB" in content
    assert "max_size 1MB" in content
    landing = Path("services/landing/html/index.html").read_text(encoding="utf-8")
    assert 'href="/app/"' in landing


def test_element_uses_the_generated_config_and_caddy_gets_domains():
    with open("services/element/docker-compose.yml", "r", encoding="utf-8") as stream:
        element = yaml.safe_load(stream)["services"]["element"]
    assert "../../data/element/config.json:/app/config.json:ro" in element["volumes"]
    with open("services/proxy/docker-compose.yml", "r", encoding="utf-8") as stream:
        caddy = yaml.safe_load(stream)["services"]["caddy"]
    assert set(caddy["environment"]) == {"WEB_DOMAIN", "MATRIX_DOMAIN", "ACME_EMAIL"}
    assert "../../data/proxy/vikunja.caddy:/etc/caddy/vikunja.caddy:ro" in caddy["volumes"]
