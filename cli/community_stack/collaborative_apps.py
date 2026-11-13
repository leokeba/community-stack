"""Shared operational metadata for the optional collaborative applications."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CollaborativeApp:
    key: str
    label: str
    hostname_prefix: str
    aliases: tuple[str, ...]
    database_dump_file: str
    database_path_default: str
    database_engine: str
    database_port: int
    url_suffix: str = ""

    @property
    def container_name(self) -> str:
        return f"community_{self.key}"

    @property
    def database_container_name(self) -> str:
        return f"{self.container_name}_db"

    def is_enabled(self, config: dict) -> bool:
        return bool(config.get("services", {}).get(self.key, {}).get("enabled", False))

    def public_hostname(self, config: dict) -> str:
        settings = config.get("services", {}).get(self.key, {})
        return settings.get("hostname") or f"{self.hostname_prefix}.{config['domain']['base']}"

    def public_url(self, config: dict) -> str:
        return f"https://{self.public_hostname(config)}{self.url_suffix or '/'}"


COLLABORATIVE_APPS = (
    CollaborativeApp(
        key="vikunja",
        label="Vikunja",
        hostname_prefix="tasks",
        aliases=("tasks",),
        database_dump_file="postgres_vikunja.dump",
        database_path_default="data/vikunja/postgres",
        database_engine="PostgreSQL",
        database_port=5432,
    ),
    CollaborativeApp(
        key="facilmap",
        label="FacilMap",
        hostname_prefix="maps",
        aliases=("maps",),
        database_dump_file="mariadb_facilmap.sql",
        database_path_default="data/facilmap/mariadb",
        database_engine="MariaDB",
        database_port=3306,
    ),
    CollaborativeApp(
        key="librebooking",
        label="LibreBooking",
        hostname_prefix="booking",
        aliases=("booking", "reservations"),
        database_dump_file="mariadb_librebooking.sql",
        database_path_default="data/librebooking/mariadb",
        database_engine="MariaDB",
        database_port=3306,
        url_suffix="/Web/",
    ),
    CollaborativeApp(
        key="field_map",
        label="Field Map",
        hostname_prefix="field-map",
        aliases=("field-map", "fieldmap"),
        database_dump_file="postgres_field_map.dump",
        database_path_default="data/field_map/postgres",
        database_engine="PostgreSQL/PostGIS",
        database_port=5432,
    ),
)

COLLABORATIVE_APPS_BY_KEY = {app.key: app for app in COLLABORATIVE_APPS}
COLLABORATIVE_SERVICE_ALIASES = {
    alias: app.key for app in COLLABORATIVE_APPS for alias in (app.key, *app.aliases)
}
