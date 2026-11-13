"""First-run configuration for Synapse and Element, generated from community-stack.yaml.

Nothing here overwrites an existing file: operators' own configuration wins.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import string
from pathlib import Path
from typing import Any

import yaml

from community_stack import docker
from community_stack.bridges import config as bridge_config
from community_stack.bridges.catalogue import MESSENGER, SIGNAL, WHATSAPP, BridgeSpec
from community_stack.config_edit import set_env_values
from community_stack.secrets import generate_secret, parse_env_file

SYNAPSE_UID = 991
# Bridges provisioned by the generic path. Each one is off unless enabled in the configuration.
BRIDGE_SPECS: tuple[BridgeSpec, ...] = (WHATSAPP, SIGNAL, MESSENGER)
LOG_CONFIG = """version: 1
formatters:
  precise:
    format: '%(asctime)s - %(name)s - %(lineno)d - %(levelname)s - %(message)s'
handlers:
  console:
    class: logging.StreamHandler
    formatter: precise
root:
  level: INFO
  handlers: [console]
disable_existing_loggers: false
"""


class ProvisionError(Exception):
    """First-run configuration could not be generated; the message is shown to operators."""


def _domain(config: dict, key: str) -> str:
    value = config.get("domain", {}).get(key, "")
    if not value:
        raise ProvisionError(f"Set domain.{key} in community-stack.yaml before installing.")
    return value


def is_example(domain: str) -> bool:
    return domain.endswith(("example.org", ".test", ".local", "localhost"))


def homeserver_config(services_dir: Path, config: dict, env: dict[str, str]) -> str:
    template = (services_dir / "matrix" / "homeserver.template.yaml").read_text(encoding="utf-8")
    matrix = config.get("services", {}).get("matrix", {})
    missing = [
        key
        for key in (
            "POSTGRES_USER",
            "POSTGRES_PASSWORD",
            "POSTGRES_DB",
            "SYNAPSE_SECRET_KEY",
            "SYNAPSE_REGISTRATION_SHARED_SECRET",
        )
        if not env.get(key)
    ]
    if missing:
        raise ProvisionError(f"Missing in .env: {', '.join(missing)}.")
    domain = _domain(config, "matrix")
    form_secret = (
        env.get("SYNAPSE_FORM_SECRET")
        or hmac.new(
            env["SYNAPSE_SECRET_KEY"].encode(),
            b"community-stack:synapse-form-secret",
            hashlib.sha256,
        ).hexdigest()
    )
    rendered = string.Template(template).substitute(
        MATRIX_DOMAIN=domain,
        POSTGRES_USER=env["POSTGRES_USER"],
        POSTGRES_PASSWORD=env["POSTGRES_PASSWORD"],
        POSTGRES_DB=env["POSTGRES_DB"],
        SYNAPSE_SECRET_KEY=env["SYNAPSE_SECRET_KEY"],
        SYNAPSE_FORM_SECRET=form_secret,
        SYNAPSE_REGISTRATION_SHARED_SECRET=env["SYNAPSE_REGISTRATION_SHARED_SECRET"],
        MAX_UPLOAD_SIZE_MB=int(matrix.get("max_upload_size_mb", 50)),
        ALLOW_REGISTRATION="true" if matrix.get("allow_registration") else "false",
        # An empty allowlist turns federation off; no allowlist allows every server.
        FEDERATION_WHITELIST="null" if matrix.get("federation") else "[]",
    )
    rendered += (
        "\n# Added at first install\n"
        f'public_baseurl: "https://{domain}/"\n'
        'signing_key_path: "/data/signing.key"\n'
        "report_stats: false\n"
        "suppress_key_server_warning: true\n"
    )
    yaml.safe_load(rendered)
    return rendered


def prepare_matrix(
    project_dir: Path, config: dict, env_path: Path, services_dir: Path
) -> str | None:
    """Write Synapse's configuration and signing key if Matrix has never been set up."""
    matrix_dir = project_dir / "data" / "matrix"
    if (matrix_dir / "homeserver.yaml").exists():
        return None
    rendered = homeserver_config(services_dir, config, parse_env_file(env_path))
    matrix_dir.mkdir(parents=True, exist_ok=True)
    (matrix_dir / "media_store").mkdir(exist_ok=True)
    (matrix_dir / "homeserver.yaml").write_text(rendered, encoding="utf-8")
    (matrix_dir / "homeserver.yaml").chmod(0o640)
    (matrix_dir / "log.config").write_text(LOG_CONFIG, encoding="utf-8")

    image = synapse_image(services_dir)
    mount = f"{matrix_dir.resolve()}:/data"
    for args in (
        # Synapse generates its own signing key from the configuration.
        [
            "--entrypoint",
            "python",
            image,
            "-m",
            "synapse.app.homeserver",
            "--config-path",
            "/data/homeserver.yaml",
            "--generate-keys",
        ],
        # The container runs Synapse as UID 991; it must own its data.
        ["--entrypoint", "chown", image, "-R", f"{SYNAPSE_UID}:{SYNAPSE_UID}", "/data"],
    ):
        result = docker.run_container(["--rm", "-v", mount, *args])
        if result.returncode != 0:
            raise ProvisionError(
                "Synapse could not prepare its data: "
                + ((result.stderr or "").strip().splitlines() or ["no output"])[-1]
            )
    return f"Generated Matrix configuration for {_domain(config, 'matrix')}."


def synapse_image(services_dir: Path) -> str:
    compose = yaml.safe_load(
        (services_dir / "matrix" / "docker-compose.yml").read_text(encoding="utf-8")
    )
    return compose["services"]["synapse"]["image"]


def element_config(config: dict) -> dict[str, Any]:
    domain = _domain(config, "matrix")
    # Development installs with placeholder domains reach Matrix through http://localhost.
    base_url, server_name = (
        ("http://localhost", "localhost") if is_example(domain) else (f"https://{domain}", domain)
    )
    return {
        "default_server_config": {
            "m.homeserver": {"base_url": base_url, "server_name": server_name},
            "m.identity_server": {"base_url": ""},
        },
        "disable_custom_urls": True,
        "brand": f"{config.get('project', {}).get('name', 'Community')} chat",
        "integrations_ui_url": "",
        "integrations_rest_url": "",
        "integrations_widgets_urls": [],
        "bug_report_endpoint_url": "",
        "default_theme": "light",
        "room_directory": {"servers": [server_name]},
        "setting_defaults": {"breadcrumbs": True},
    }


def prepare_element(project_dir: Path, config: dict) -> str | None:
    """Write Element's configuration, pointing at this stack's homeserver, if missing."""
    path = project_dir / "data" / "element" / "config.json"
    if path.is_file():
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(element_config(config), indent=2) + "\n", encoding="utf-8")
    path.chmod(0o644)
    return f"Generated Element configuration for {_domain(config, 'matrix')}."


def prepare_vikunja(project_dir: Path, config: dict, env_path: Path) -> str | None:
    """Provision Vikunja's optional Caddy route, storage directories, and stable secrets."""
    settings = config.get("services", {}).get("vikunja", {})
    root = project_dir.resolve()
    hostname = settings.get("hostname") or f"tasks.{config['domain']['base']}"
    route_path = root / "data" / "proxy" / "vikunja.caddy"
    if settings.get("enabled", False):
        route_content = (
            f"{hostname} {{\n    encode zstd gzip\n    reverse_proxy community_vikunja:3456\n}}\n"
        )
    else:
        route_content = "# Vikunja is disabled; no public route is configured.\n"
    route_path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    if not route_path.is_file() or route_path.read_text(encoding="utf-8") != route_content:
        route_path.write_text(route_content, encoding="utf-8")
        route_path.chmod(0o644)

    if not settings.get("enabled", False):
        return None

    files_path = root / settings.get("files_path", "data/vikunja/files")
    database_path = root / settings.get("database", {}).get("data_path", "data/vikunja/postgres")
    for path in (files_path, database_path):
        if not path.resolve().is_relative_to(root):
            raise ProvisionError(
                "Vikunja persistent paths must remain inside the project directory."
            )

    env_values = parse_env_file(env_path)
    required = ("VIKUNJA_POSTGRES_PASSWORD", "VIKUNJA_SERVICE_SECRET")
    missing = [key for key in required if not env_values.get(key)]
    if missing:
        try:
            existing_data = (database_path / "PG_VERSION").is_file()
            if files_path.is_dir():
                existing_data = existing_data or any(files_path.iterdir())
        except OSError as exc:
            raise ProvisionError(
                "Vikunja secrets are missing and persistent storage cannot be inspected. "
                "Restore VIKUNJA_POSTGRES_PASSWORD and VIKUNJA_SERVICE_SECRET before starting it."
            ) from exc
        if existing_data:
            raise ProvisionError(
                "Vikunja secrets are missing from .env while persistent data exists. "
                "Restore VIKUNJA_POSTGRES_PASSWORD and VIKUNJA_SERVICE_SECRET before starting it."
            )
    generated = {
        "VIKUNJA_POSTGRES_PASSWORD": generate_secret(32),
        "VIKUNJA_SERVICE_SECRET": generate_secret(32),
    }
    for key in required:
        if env_values.get(key):
            generated.pop(key)
    if generated:
        env_path.parent.mkdir(parents=True, exist_ok=True)
        if not env_path.exists():
            env_path.touch(mode=0o600)
        set_env_values(env_path, generated)
        if env_path.stat().st_mode & 0o077:
            env_path.chmod(0o600)

    values = {
        "VIKUNJA_VERSION": settings.get("version", "2.7.1"),
        "VIKUNJA_DOMAIN": hostname,
        "VIKUNJA_FILES_DATA_PATH": settings.get("files_path", "data/vikunja/files"),
        "VIKUNJA_DATABASE_DATA_PATH": settings.get("database", {}).get(
            "data_path", "data/vikunja/postgres"
        ),
        "VIKUNJA_ALLOW_REGISTRATION": str(settings.get("allow_registration", False)).lower(),
        "VIKUNJA_ENABLE_LINK_SHARING": str(settings.get("enable_link_sharing", False)).lower(),
        "VIKUNJA_MEMORY_LIMIT": settings.get("memory_limit", "512M"),
        "VIKUNJA_CPU_LIMIT": str(settings.get("cpu_limit", 1)),
        "VIKUNJA_UID": str(os.getuid()) if hasattr(os, "getuid") else "1000",
        "VIKUNJA_GID": str(os.getgid()) if hasattr(os, "getgid") else "1000",
    }
    env_path.parent.mkdir(parents=True, exist_ok=True)
    if not env_path.exists():
        env_path.touch(mode=0o600)
    set_env_values(env_path, values)
    if os.name == "posix":
        env_path.chmod(0o600)
    if missing:
        try:
            for path in (files_path, database_path):
                path.mkdir(mode=0o750, parents=True, exist_ok=True)
        except OSError as exc:
            raise ProvisionError(f"Could not prepare Vikunja storage: {exc}") from exc
    return f"Prepared Vikunja storage and deployment settings for https://{hostname}/."


def prepare_facilmap(project_dir: Path, config: dict, env_path: Path) -> str | None:
    """Provision FacilMap's Caddy route, persistent directories, and database secrets."""
    settings = config.get("services", {}).get("facilmap", {})
    root = project_dir.resolve()
    hostname = settings.get("hostname") or f"maps.{config['domain']['base']}"
    route_path = root / "data" / "proxy" / "facilmap.caddy"
    if settings.get("enabled", False):
        route_content = (
            f"{hostname} {{\n    encode zstd gzip\n    reverse_proxy community_facilmap:8080\n}}\n"
        )
    else:
        route_content = "# FacilMap is disabled; no public route is configured.\n"
    route_path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    if not route_path.is_file() or route_path.read_text(encoding="utf-8") != route_content:
        route_path.write_text(route_content, encoding="utf-8")
        route_path.chmod(0o644)

    if not settings.get("enabled", False):
        return None

    database = settings.get("database", {})
    cache_path = root / settings.get("cache_path", "data/facilmap/cache")
    database_path = root / database.get("data_path", "data/facilmap/mariadb")
    for path in (cache_path, database_path):
        if not path.resolve().is_relative_to(root):
            raise ProvisionError(
                "FacilMap persistent paths must remain inside the project directory."
            )

    env_values = parse_env_file(env_path)
    required = ("FACILMAP_DB_PASSWORD", "FACILMAP_DB_ROOT_PASSWORD")
    missing = [key for key in required if not env_values.get(key)]
    if missing:
        try:
            existing_data = database_path.is_dir() and any(database_path.iterdir())
        except OSError as exc:
            raise ProvisionError(
                "FacilMap credentials are missing and database storage cannot be inspected. "
                "Restore FACILMAP_DB_PASSWORD and FACILMAP_DB_ROOT_PASSWORD before starting it."
            ) from exc
        if existing_data:
            raise ProvisionError(
                "FacilMap database credentials are missing from .env while persistent data exists. "
                "Restore FACILMAP_DB_PASSWORD and FACILMAP_DB_ROOT_PASSWORD before starting it."
            )
    generated = {
        "FACILMAP_DB_PASSWORD": generate_secret(32),
        "FACILMAP_DB_ROOT_PASSWORD": generate_secret(32),
    }
    for key in required:
        if env_values.get(key):
            generated.pop(key)

    contact = env_values.get("ACME_EMAIL", "admin@example.org")
    values = {
        **generated,
        "FACILMAP_VERSION": settings.get(
            "version",
            "sha256:bcef0c14e23f8670ea21d18a15d4331348bf253550a5acf39b19d067546f629f",
        ),
        "FACILMAP_DOMAIN": hostname,
        "FACILMAP_USER_AGENT": f"CommunityStack-FacilMap/1.0-contact={contact}",
        "FACILMAP_CACHE_PATH": settings.get("cache_path", "data/facilmap/cache"),
        "FACILMAP_DATABASE_VERSION": database.get("version", "11.4.12"),
        "FACILMAP_DATABASE_DATA_PATH": database.get("data_path", "data/facilmap/mariadb"),
        "FACILMAP_MEMORY_LIMIT": settings.get("memory_limit", "512M"),
        "FACILMAP_CPU_LIMIT": str(settings.get("cpu_limit", 1)),
        "FACILMAP_DATABASE_MEMORY_LIMIT": database.get("memory_limit", "512M"),
        "FACILMAP_DATABASE_CPU_LIMIT": str(database.get("cpu_limit", 1)),
    }
    env_path.parent.mkdir(parents=True, exist_ok=True)
    if not env_path.exists():
        env_path.touch(mode=0o600)
    set_env_values(env_path, values)
    if os.name == "posix":
        env_path.chmod(0o600)
    if missing:
        for path in (cache_path, database_path):
            path.mkdir(mode=0o750, parents=True, exist_ok=True)
    return f"Prepared FacilMap storage and deployment settings for https://{hostname}/."


def prepare_librebooking(project_dir: Path, config: dict, env_path: Path) -> str | None:
    """Provision LibreBooking's private database, persistent paths, secrets, and Caddy route."""
    settings = config.get("services", {}).get("librebooking", {})
    root = project_dir.resolve()
    hostname = settings.get("hostname") or f"booking.{config['domain']['base']}"
    route_path = root / "data" / "proxy" / "librebooking.caddy"
    if settings.get("enabled", False):
        route_content = (
            f"{hostname} {{\n"
            "    encode zstd gzip\n"
            "    reverse_proxy community_librebooking:8080\n"
            "}\n"
        )
    else:
        route_content = "# LibreBooking is disabled; no public route is configured.\n"
    route_path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    if not route_path.is_file() or route_path.read_text(encoding="utf-8") != route_content:
        route_path.write_text(route_content, encoding="utf-8")
        route_path.chmod(0o644)

    if not settings.get("enabled", False):
        return None

    database = settings.get("database", {})
    paths = {
        "database": root / database.get("data_path", "data/librebooking/mariadb"),
        "configuration": root / settings.get("config_path", "data/librebooking/config"),
        "uploads": root / settings.get("uploads_path", "data/librebooking/uploads"),
        "logs": root / settings.get("logs_path", "data/librebooking/logs"),
    }
    for path in paths.values():
        if not path.resolve().is_relative_to(root):
            raise ProvisionError(
                "LibreBooking persistent paths must remain inside the project directory."
            )

    env_values = parse_env_file(env_path)
    required = (
        "LIBREBOOKING_DB_PASSWORD",
        "LIBREBOOKING_DB_ROOT_PASSWORD",
        "LIBREBOOKING_INSTALL_PASSWORD",
    )
    missing = [key for key in required if not env_values.get(key)]
    if missing:
        try:
            existing_data = any(path.is_dir() and any(path.iterdir()) for path in paths.values())
        except OSError as exc:
            raise ProvisionError(
                "LibreBooking credentials are missing and persistent storage cannot be inspected. "
                "Restore its original .env credentials before starting it."
            ) from exc
        if existing_data:
            raise ProvisionError(
                "LibreBooking credentials are missing from .env while persistent data exists. "
                "Restore LIBREBOOKING_DB_PASSWORD, LIBREBOOKING_DB_ROOT_PASSWORD, and "
                "LIBREBOOKING_INSTALL_PASSWORD before starting it."
            )

    generated = {key: generate_secret(32) for key in required if not env_values.get(key)}
    values = {
        **generated,
        "LIBREBOOKING_VERSION": settings.get("version", "7.2.0-3.5.2"),
        "LIBREBOOKING_DOMAIN": hostname,
        "LIBREBOOKING_ADMIN_EMAIL": (
            env_values.get("ACME_EMAIL", "admin@example.org")
            if settings.get("admin_email", "admin@example.org") == "admin@example.org"
            else settings["admin_email"]
        ),
        "LIBREBOOKING_ALLOW_REGISTRATION": str(settings.get("allow_registration", False)).lower(),
        "LIBREBOOKING_TIMEZONE": config.get("project", {}).get("timezone", "UTC"),
        "LIBREBOOKING_LANGUAGE": "fr_fr",
        "LIBREBOOKING_DATABASE_VERSION": database.get("version", "11.4.12"),
        "LIBREBOOKING_DATABASE_DATA_PATH": settings.get("database", {}).get(
            "data_path", "data/librebooking/mariadb"
        ),
        "LIBREBOOKING_CONFIG_PATH": settings.get("config_path", "data/librebooking/config"),
        "LIBREBOOKING_UPLOADS_PATH": settings.get("uploads_path", "data/librebooking/uploads"),
        "LIBREBOOKING_LOGS_PATH": settings.get("logs_path", "data/librebooking/logs"),
        "LIBREBOOKING_MEMORY_LIMIT": settings.get("memory_limit", "512M"),
        "LIBREBOOKING_CPU_LIMIT": str(settings.get("cpu_limit", 1)),
        "LIBREBOOKING_DATABASE_MEMORY_LIMIT": database.get("memory_limit", "512M"),
        "LIBREBOOKING_DATABASE_CPU_LIMIT": str(database.get("cpu_limit", 1)),
        "LIBREBOOKING_GID": env_values.get("COMMUNITY_STACK_GID")
        or str(os.getgid() if hasattr(os, "getgid") else 1000),
    }
    env_path.parent.mkdir(parents=True, exist_ok=True)
    if not env_path.exists():
        env_path.touch(mode=0o600)
    set_env_values(env_path, values)
    if os.name == "posix":
        env_path.chmod(0o600)

    gid = int(values["LIBREBOOKING_GID"])
    try:
        for path in paths.values():
            path.mkdir(mode=0o770, parents=True, exist_ok=True)
            if os.name == "posix":
                if path.stat().st_gid != gid:
                    os.chown(path, -1, gid)
                path.chmod(0o770)
    except (OSError, ValueError) as exc:
        raise ProvisionError(f"Could not prepare LibreBooking storage: {exc}") from exc

    return f"Prepared LibreBooking storage and deployment settings for https://{hostname}/Web/."


def prepare_field_map(project_dir: Path, config: dict, env_path: Path) -> str | None:
    """Provision Field Map's route, stable database credentials, and persistent storage."""
    settings = config.get("services", {}).get("field_map", {})
    root = project_dir.resolve()
    hostname = settings.get("hostname") or f"field-map.{config['domain']['base']}"
    route_path = root / "data" / "proxy" / "field_map.caddy"
    if settings.get("enabled", False):
        route_content = (
            f"{hostname} {{\n"
            "    encode zstd gzip\n"
            "    request_body {\n"
            "        max_size 12MB\n"
            "    }\n"
            "    reverse_proxy community_field_map:8000\n"
            "}\n"
        )
    else:
        route_content = "# Field Map is disabled; no public route is configured.\n"
    route_path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    if not route_path.is_file() or route_path.read_text(encoding="utf-8") != route_content:
        route_path.write_text(route_content, encoding="utf-8")
        route_path.chmod(0o644)

    if not settings.get("enabled", False):
        return None

    database = settings.get("database", {})
    uploads_path = root / settings.get("uploads_path", "data/field_map/uploads")
    database_path = root / database.get("data_path", "data/field_map/postgres")
    for path in (uploads_path, database_path):
        if not path.resolve().is_relative_to(root):
            raise ProvisionError(
                "Field Map persistent paths must remain inside the project directory."
            )

    env_values = parse_env_file(env_path)
    required = ("FIELD_MAP_DB_PASSWORD",)
    missing = [key for key in required if not env_values.get(key)]
    if missing:
        try:
            existing_data = (database_path / "PG_VERSION").is_file()
            if uploads_path.is_dir():
                existing_data = existing_data or any(uploads_path.iterdir())
        except OSError as exc:
            raise ProvisionError(
                "Field Map credentials are missing and persistent storage cannot be inspected. "
                "Restore FIELD_MAP_DB_PASSWORD before starting it."
            ) from exc
        if existing_data:
            raise ProvisionError(
                "Field Map database credentials are missing from .env while persistent data "
                "exists. Restore FIELD_MAP_DB_PASSWORD before starting it."
            )

    generated = {key: generate_secret(32) for key in missing}
    stack_uid = env_values.get("COMMUNITY_STACK_UID") or str(
        os.getuid() if hasattr(os, "getuid") else 1000
    )
    stack_gid = env_values.get("COMMUNITY_STACK_GID") or str(
        os.getgid() if hasattr(os, "getgid") else 1000
    )
    if not stack_uid.isdigit() or not stack_gid.isdigit():
        raise ProvisionError("COMMUNITY_STACK_UID and COMMUNITY_STACK_GID must be numeric.")
    values = {
        **generated,
        "FIELD_MAP_VERSION": settings.get("version", "107595ae25d0ea197efb6ee8fddc29047bc6c623"),
        "FIELD_MAP_SOURCE_REF": settings.get("version", "107595ae25d0ea197efb6ee8fddc29047bc6c623"),
        "FIELD_MAP_DOMAIN": hostname,
        "FIELD_MAP_ORIGIN": f"https://{hostname}",
        "FIELD_MAP_TILE_URL": settings.get(
            "tile_url", "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
        ),
        "FIELD_MAP_ANONYMOUS_LINKS_ENABLED": str(
            settings.get("anonymous_links_enabled", True)
        ).lower(),
        "FIELD_MAP_UPLOADS_PATH": settings.get("uploads_path", "data/field_map/uploads"),
        "FIELD_MAP_DATABASE_DATA_PATH": database.get("data_path", "data/field_map/postgres"),
        "FIELD_MAP_DATABASE_VERSION": database.get("version", "16-3.4"),
        "FIELD_MAP_MEMORY_LIMIT": settings.get("memory_limit", "512M"),
        "FIELD_MAP_CPU_LIMIT": str(settings.get("cpu_limit", 1)),
        "FIELD_MAP_DATABASE_MEMORY_LIMIT": database.get("memory_limit", "512M"),
        "FIELD_MAP_DATABASE_CPU_LIMIT": str(database.get("cpu_limit", 1)),
        "FIELD_MAP_UID": stack_uid,
        "FIELD_MAP_GID": stack_gid,
    }
    env_path.parent.mkdir(parents=True, exist_ok=True)
    if not env_path.exists():
        env_path.touch(mode=0o600)
    set_env_values(env_path, values)
    if os.name == "posix":
        env_path.chmod(0o600)
    try:
        uploads_path.mkdir(mode=0o770, parents=True, exist_ok=True)
        if os.name == "posix":
            gid = int(stack_gid)
            if uploads_path.stat().st_gid != gid:
                os.chown(uploads_path, -1, gid)
            uploads_path.chmod(0o770)
        database_path.mkdir(mode=0o750, parents=True, exist_ok=True)
    except (OSError, ValueError) as exc:
        raise ProvisionError(f"Could not prepare Field Map storage: {exc}") from exc

    return f"Prepared Field Map storage and deployment settings for https://{hostname}/."


def _bridge_secrets(env_path: Path, spec: BridgeSpec, create_missing: bool) -> dict[str, str]:
    """The bridge's appservice tokens, kept in .env under its own prefix."""
    keys = (f"{spec.secret_prefix}_AS_TOKEN", f"{spec.secret_prefix}_HS_TOKEN")
    values = parse_env_file(env_path)
    missing = [key for key in keys if not values.get(key)]
    if missing and not create_missing:
        raise ProvisionError(
            f"{spec.name} bridge credentials are missing from .env while bridge data already "
            "exists. Restore the original values before starting the bridge."
        )
    if not missing:
        return values

    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []
    for key in missing:
        value = generate_secret()
        replaced = False
        for index, line in enumerate(lines):
            if line.partition("=")[0].strip() == key:
                lines[index] = f"{key}={value}"
                replaced = True
                break
        if not replaced:
            lines.append(f"{key}={value}")
        values[key] = value
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    env_path.chmod(0o600)
    return values


def _add_appservice_registration(homeserver_path: Path, registration: str) -> bool:
    text = homeserver_path.read_text(encoding="utf-8")
    config = yaml.safe_load(text)
    if not isinstance(config, dict):
        raise ProvisionError("Synapse homeserver.yaml must contain a YAML mapping.")
    registrations = config.get("app_service_config_files", [])
    if registrations is None:
        registrations = []
    if not isinstance(registrations, list) or not all(
        isinstance(path, str) for path in registrations
    ):
        raise ProvisionError("Synapse app_service_config_files must be a list of file paths.")
    if registration in registrations:
        return False

    registrations.append(registration)
    block = ["app_service_config_files:", *[f"  - {path}" for path in registrations]]
    lines = text.splitlines()
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if re.match(r"^app_service_config_files\s*:", line)
        ),
        None,
    )
    if start is None:
        updated = text.rstrip() + "\n\n" + "\n".join(block) + "\n"
    else:
        end = next(
            (
                index
                for index in range(start + 1, len(lines))
                if lines[index] and not lines[index][0].isspace()
            ),
            len(lines),
        )
        lines[start:end] = block
        updated = "\n".join(lines) + "\n"
    yaml.safe_load(updated)
    homeserver_path.write_text(updated, encoding="utf-8")
    homeserver_path.chmod(0o640)
    return True


def prepare_bridge(
    project_dir: Path, config: dict, env_path: Path, services_dir: Path, spec: BridgeSpec
) -> str | None:
    """Prepare a bridge's config, its registration, and Synapse's appservice entry."""
    services = config.get("services", {})
    if not services.get("matrix", {}).get("enabled", False):
        raise ProvisionError(f"Enable the Matrix server before enabling the {spec.name} bridge.")

    matrix_dir = project_dir / "data" / "matrix"
    homeserver_path = matrix_dir / "homeserver.yaml"
    if not homeserver_path.is_file():
        raise ProvisionError(
            "Matrix has not been provisioned yet. Start the Matrix server before the bridge."
        )

    data_dir = project_dir / "data" / spec.service
    bridge_config_path = data_dir / "config.yaml"
    registration_file = data_dir / "registration.yaml"
    if registration_file.is_file() and not bridge_config_path.is_file():
        raise ProvisionError(
            f"{spec.name} bridge data is incomplete: config.yaml is missing while "
            "registration.yaml exists. Restore the bridge config before starting the service."
        )
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    data_dir.chmod(0o700)

    secrets = _bridge_secrets(env_path, spec, create_missing=not bridge_config_path.exists())
    if not bridge_config_path.exists():
        template_path = services_dir / spec.service / "config.template.yaml"
        template = template_path.read_text(encoding="utf-8")
        rendered = string.Template(template).substitute(
            MATRIX_DOMAIN=_domain(config, "matrix"),
            AS_TOKEN=secrets[f"{spec.secret_prefix}_AS_TOKEN"],
            HS_TOKEN=secrets[f"{spec.secret_prefix}_HS_TOKEN"],
        )
        yaml.safe_load(rendered)
        bridge_config_path.write_text(rendered, encoding="utf-8")
        bridge_config_path.chmod(0o600)

    try:
        bridge_config.ensure_safeguards(bridge_config_path, spec)
    except bridge_config.BridgeConfigError as exc:
        raise ProvisionError(str(exc)) from exc

    if not registration_file.is_file():
        compose = yaml.safe_load(
            (services_dir / spec.service / "docker-compose.yml").read_text(encoding="utf-8")
        )
        image = compose["services"][spec.service]["image"]
        result = docker.run_container(
            [
                "--rm",
                "--network",
                "none",
                "-v",
                f"{data_dir.resolve()}:/data",
                image,
            ]
        )
        if result.returncode != 0 or not registration_file.is_file():
            raise ProvisionError(
                f"The {spec.name} bridge could not generate its appservice registration. "
                "Check Docker and the bridge image."
            )
        registration_file.chmod(0o600)

    appservices_dir = matrix_dir / "appservices"
    appservices_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
    appservice_path = appservices_dir / spec.registration_file
    appservice_path.write_text(registration_file.read_text(encoding="utf-8"), encoding="utf-8")
    appservice_path.chmod(0o640)

    synapse_image_name = synapse_image(services_dir)
    ownership = docker.run_container(
        [
            "--rm",
            "-v",
            f"{matrix_dir.resolve()}:/data",
            "--entrypoint",
            "chown",
            synapse_image_name,
            "-R",
            f"{SYNAPSE_UID}:{SYNAPSE_UID}",
            "/data/appservices",
        ]
    )
    if ownership.returncode != 0:
        raise ProvisionError(f"Synapse could not read the {spec.name} appservice registration.")

    registered = _add_appservice_registration(
        homeserver_path, f"/data/appservices/{spec.registration_file}"
    )
    if registered:
        return f"Generated {spec.name} bridge configuration and registered it with Synapse."
    return None


def ensure_whatsapp_privacy_config(path: Path) -> bool:
    """Apply the WhatsApp bridge's safeguards; kept for the deployment script."""
    try:
        return bridge_config.ensure_safeguards(path, WHATSAPP)
    except bridge_config.BridgeConfigError as exc:
        raise ProvisionError(str(exc)) from exc


def prepare_whatsapp(
    project_dir: Path, config: dict, env_path: Path, services_dir: Path
) -> str | None:
    return prepare_bridge(project_dir, config, env_path, services_dir, WHATSAPP)


def prepare_service(
    project_dir: Path,
    service: str,
    config: dict,
    env_path: Path,
    services_dir: Path | None = None,
) -> str | None:
    """Run the first-run step for a service, if it has one."""
    if service == "matrix":
        return prepare_matrix(
            project_dir, config, env_path, services_dir or project_dir / "services"
        )
    if service == "element":
        return prepare_element(project_dir, config)
    if service == "vikunja":
        return prepare_vikunja(project_dir, config, env_path)
    if service == "facilmap":
        return prepare_facilmap(project_dir, config, env_path)
    if service == "librebooking":
        return prepare_librebooking(project_dir, config, env_path)
    if service == "field_map":
        return prepare_field_map(project_dir, config, env_path)
    if service == "safemode":
        from community_stack.safemode.provision import prepare_safemode

        return prepare_safemode(
            project_dir, config, env_path, services_dir or project_dir / "services"
        )
    for spec in BRIDGE_SPECS:
        if service == spec.service:
            return prepare_bridge(
                project_dir, config, env_path, services_dir or project_dir / "services", spec
            )
    return None
