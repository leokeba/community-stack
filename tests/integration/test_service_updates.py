"""Updating services to the versions pinned in the installed release."""

import subprocess
from pathlib import Path

import pytest
from community_stack.admin.store import AdminStore
from community_stack.agent import settings as service_control
from community_stack.agent import updates
from community_stack.agent.operations import AgentContext
from community_stack.agent.server import AgentService

PASSWORD = "a-long-safe-test-password"


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "stack"
    (root / "data").mkdir(parents=True)
    import shutil

    shutil.copytree(Path("services"), root / "services")
    (root / "community-stack.yaml").write_text(
        Path("config/example.yaml")
        .read_text(encoding="utf-8")
        .replace('encryption: "restic"', 'encryption: "none"'),
        encoding="utf-8",
    )
    (root / ".env").write_text("RESTIC_PASSWORD=unused\n", encoding="utf-8")
    AdminStore(root / "data" / "announcements" / "announcements.db").create_admin("olga", PASSWORD)
    running = {
        "community_synapse": "matrixdotorg/synapse:v1.118.0",
        "community_postgres": "postgres:16-alpine",
        "community_element": "vectorim/element-web:v1.11.70",
        "community_proxy": "caddy:2.8.4-alpine",
        "community_landing": "nginx:1.27-alpine",
    }
    monkeypatch.setattr(
        updates,
        "_running",
        lambda container: (
            {"image": running[container], "image_id": f"sha256:{container}"}
            if container in running
            else None
        ),
    )
    return root, running


def rows(context):
    return {row["component"]: row for row in updates.versions(context)["components"]}


def test_versions_compare_running_images_with_release_pins(project):
    root, _ = project
    components = rows(AgentContext(root))
    assert components["synapse"]["update_available"]
    assert components["synapse"]["release_notes"] == (
        "https://github.com/element-hq/synapse/releases/tag/v1.119.0"
    )
    assert not components["element"]["update_available"]
    assert components["whatsapp_bridge"]["running"] is None


def test_postgres_major_upgrades_are_blocked(project):
    root, running = project
    running["community_postgres"] = "postgres:15-alpine"
    assert "migration" in rows(AgentContext(root))["postgres"]["blocked"]
    with pytest.raises(updates.UpdateError, match="migration"):
        updates.update(AgentContext(root), {"service": "matrix"})


def test_update_backs_up_pulls_and_restarts(project, monkeypatch):
    root, _ = project
    calls = []
    monkeypatch.setattr(
        updates.docker,
        "run_compose",
        lambda path, args, **kw: calls.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )
    monkeypatch.setattr(
        service_control, "set_service", lambda ctx, svc, on: calls.append(["up", svc])
    )

    result = updates.update(AgentContext(root), {"service": "matrix"})

    assert result["steps"][0].startswith("Safety backup snap_")
    assert calls == [["pull"], ["up", "matrix"]]
    assert updates.update(AgentContext(root), {"service": "element"})["steps"][0].endswith(
        "already on the versions of this release."
    )


def test_unhealthy_update_rolls_back_to_the_previous_images(project, monkeypatch):
    root, _ = project
    tagged = []
    attempts = []
    monkeypatch.setattr(
        updates.docker,
        "run_compose",
        lambda path, args, **kw: subprocess.CompletedProcess(args, 0, "", ""),
    )

    def flaky(ctx, svc, on):
        attempts.append(svc)
        if len(attempts) == 1:
            raise service_control.SettingsError("matrix did not become healthy in time.")

    monkeypatch.setattr(service_control, "set_service", flaky)
    real_run = subprocess.run
    monkeypatch.setattr(
        updates.subprocess,
        "run",
        lambda args, **kw: tagged.append(args) or real_run(["true"]),
    )

    with pytest.raises(updates.UpdateError, match="previous version was restored"):
        updates.update(AgentContext(root), {"service": "matrix"})
    assert ["docker", "tag", "sha256:community_synapse", "matrixdotorg/synapse:v1.119.0"] in tagged
    assert attempts == ["matrix", "matrix"]


def test_only_pinned_services_can_be_updated_through_the_agent(project):
    root, _ = project
    response = AgentService(AgentContext(root)).handle(
        {"op": "services.update", "actor": "olga", "params": {"service": "announcements"}}
    )
    assert "Invalid value" in response["error"]
