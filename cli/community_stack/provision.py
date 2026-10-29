"""First-run configuration for Synapse and Element, generated from community-stack.yaml.

Nothing here overwrites an existing file: operators' own configuration wins.
"""

from __future__ import annotations

import json
import string
from pathlib import Path
from typing import Any

import yaml

from community_stack import docker
from community_stack.secrets import parse_env_file

SYNAPSE_UID = 991
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
    rendered = string.Template(template).substitute(
        MATRIX_DOMAIN=domain,
        POSTGRES_USER=env["POSTGRES_USER"],
        POSTGRES_PASSWORD=env["POSTGRES_PASSWORD"],
        POSTGRES_DB=env["POSTGRES_DB"],
        SYNAPSE_SECRET_KEY=env["SYNAPSE_SECRET_KEY"],
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
    return None
