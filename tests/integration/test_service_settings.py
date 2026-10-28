"""Service settings through the agent: YAML edits, plan, apply, rollback, and the console."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest
import yaml
from community_stack.admin.app import create_app
from community_stack.admin.store import AdminStore
from community_stack.agent import settings as agent_settings
from community_stack.agent.operations import AgentContext
from community_stack.agent.server import AgentService, AgentSocketServer
from community_stack.backup import BackupError
from community_stack.config_edit import set_value, write_in_place
from fastapi.testclient import TestClient

PASSWORD = "a-long-safe-test-password"


class FakeDocker:
    """Stands in for docker compose: records calls and reports a chosen container state."""

    def __init__(self, state: str = "running", health: str = "healthy"):
        self.state = state
        self.health = health
        self.calls: list[tuple[str, list[str]]] = []
        self.running: dict[str, bool] = {}

    def run_compose(self, project_dir, args, env_file=None, capture_output=False):
        service = Path(project_dir).name
        self.calls.append((service, list(args)))
        if args[0] == "up":
            self.running[service] = True
        elif args[0] == "down":
            self.running[service] = False
        if args[0] == "ps":
            if not self.running.get(service):
                return subprocess.CompletedProcess(args, 0, "", "")
            row = {"Name": f"community_{service}", "State": self.state, "Health": self.health}
            return subprocess.CompletedProcess(args, 0, json.dumps(row) + "\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "stack"
    (root / "data" / "matrix").mkdir(parents=True)
    (root / "community-stack.yaml").write_text(
        Path("config/example.yaml")
        .read_text(encoding="utf-8")
        .replace('encryption: "restic"', 'encryption: "none"'),
        encoding="utf-8",
    )
    (root / ".env").write_text("RESTIC_PASSWORD=unused\n", encoding="utf-8")
    store = AdminStore(root / "data" / "announcements" / "announcements.db")
    store.create_admin("olga", PASSWORD)
    fake = FakeDocker()
    fake.running = {"element": True, "matrix": True}
    monkeypatch.setattr(agent_settings.docker, "run_compose", fake.run_compose)
    monkeypatch.setattr(agent_settings.docker, "ensure_network", lambda name: True)
    monkeypatch.setattr(agent_settings, "HEALTH_POLL_SECONDS", 0)
    return root, fake


def context(root: Path) -> AgentContext:
    return AgentContext(project_dir=root)


def test_yaml_edits_keep_comments_and_the_file_inode(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        'project:\n  name: "x" # keep me\n  profile: "starter"\n'
        "operations:\n  backups:\n    enabled: true # and me\n",
        encoding="utf-8",
    )
    inode = os.stat(path).st_ino
    text = set_value(path.read_text(encoding="utf-8"), ("project", "timezone"), "Europe/Paris")
    text = set_value(text, ("operations", "backups", "enabled"), False)
    write_in_place(path, text)

    assert os.stat(path).st_ino == inode
    assert "# keep me" in text and "enabled: false # and me" in text
    assert yaml.safe_load(text)["project"]["timezone"] == "Europe/Paris"
    with pytest.raises(ValueError, match="missing"):
        set_value(text, ("services", "element", "enabled"), True)


def test_plan_shows_the_diff_without_changing_anything(project):
    root, fake = project
    before = (root / "community-stack.yaml").read_text(encoding="utf-8")

    plan = agent_settings.build_plan(
        context(root),
        {"project.timezone": "Europe/Paris", "operations.backups.retention_days": "14"},
    )

    assert [change["key"] for change in plan["changes"]] == ["project.timezone"]
    assert '+  timezone: "Europe/Paris"' in plan["diff"]
    assert not plan["needs_password"]
    assert (root / "community-stack.yaml").read_text(encoding="utf-8") == before
    assert fake.calls == []


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"project.timezone": "Mars/Olympus"}, "not a known time zone"),
        ({"operations.backups.retention_days": "0"}, "between 1 and 3650"),
        ({"services.matrix.allow_registration": "true"}, "cannot be changed"),
        ({"services.element.enabled": "maybe"}, "on or off"),
        ({}, "No changes"),
    ],
)
def test_invalid_changes_are_explained(project, changes, message):
    root, _ = project
    with pytest.raises(agent_settings.SettingsError, match=message):
        agent_settings.build_plan(context(root), changes)


def test_restic_encryption_needs_restic_on_the_server(project, monkeypatch):
    root, _ = project
    monkeypatch.setattr(agent_settings.shutil, "which", lambda name: None)
    with pytest.raises(agent_settings.SettingsError, match="Install restic"):
        agent_settings.build_plan(context(root), {"operations.backups.encryption": "restic"})


def test_apply_backs_up_saves_and_stops_the_service(project):
    root, fake = project

    result = agent_settings.apply_changes(context(root), {"services.element.enabled": "false"})

    assert result["applied"]
    assert result["steps"][0].startswith("Safety backup snap_")
    assert ("element", ["down"]) in fake.calls
    config = yaml.safe_load((root / "community-stack.yaml").read_text(encoding="utf-8"))
    assert config["services"]["element"]["enabled"] is False
    assert (root / "community-stack.yaml.previous").stat().st_mode & 0o777 == 0o600
    assert list((root / "backups").glob("snap_*.tar.gz"))


def test_unhealthy_service_rolls_back_settings_and_state(project):
    root, fake = project
    fake.running["element"] = False
    config_path = root / "community-stack.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "  element:\n    enabled: true", "  element:\n    enabled: false"
        ),
        encoding="utf-8",
    )
    before = config_path.read_text(encoding="utf-8")
    fake.state = "exited"

    with pytest.raises(agent_settings.SettingsError, match="previous configuration was restored"):
        agent_settings.apply_changes(context(root), {"services.element.enabled": "true"})

    assert config_path.read_text(encoding="utf-8") == before
    lifecycle = [call for call in fake.calls if call[1][0] in ("up", "down")]
    assert lifecycle == [("element", ["up", "-d", "--remove-orphans"]), ("element", ["down"])]


def test_failed_safety_backup_changes_nothing(project, monkeypatch):
    root, fake = project
    before = (root / "community-stack.yaml").read_text(encoding="utf-8")

    def broken(self):
        raise BackupError("disk full")

    monkeypatch.setattr("community_stack.backup.BackupEngine.create_snapshot", broken)
    with pytest.raises(agent_settings.SettingsError, match="nothing was changed: disk full"):
        agent_settings.apply_changes(context(root), {"project.timezone": "Europe/Paris"})
    assert (root / "community-stack.yaml").read_text(encoding="utf-8") == before
    assert fake.calls == []


def test_stopping_matrix_requires_the_password_at_the_agent(project):
    root, fake = project
    service = AgentService(context(root))
    request = {
        "op": "config.apply",
        "actor": "olga",
        "params": {"changes": {"services.matrix.enabled": "false"}},
    }
    assert "Password" in service.handle(request)["error"]
    assert fake.calls == []

    applied = service.handle({**request, "password": PASSWORD})
    assert applied["ok"], applied
    assert ("matrix", ["down"]) in fake.calls

    timezone = service.handle(
        {"op": "config.apply", "actor": "olga", "params": {"changes": {"project.timezone": "UTC"}}}
    )
    assert timezone["ok"], timezone


@pytest.fixture
def console(project):
    root, _ = project
    directory = Path(tempfile.mkdtemp(prefix="css-"))
    server = AgentSocketServer(directory / "agent.sock", AgentService(context(root)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    app = create_app(
        db_path=root / "data" / "announcements" / "announcements.db",
        feed_dir=root / "feed",
        config_path=root / "community-stack.yaml",
        setup_token="unused",
        run_worker=False,
        agent_socket=directory / "agent.sock",
    )
    client = TestClient(app, base_url="https://community.test", follow_redirects=False)
    page = client.get("/admin/login")
    client.post(
        "/admin/login", data={"csrf_token": csrf(page), "username": "olga", "password": PASSWORD}
    )
    yield root, client
    server.shutdown()
    server.server_close()
    shutil.rmtree(directory, ignore_errors=True)


def csrf(response) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)


def form_values(page) -> dict[str, str]:
    values = dict(re.findall(r'name="(setting:[^"]+)"[^>]*value="([^"]*)"', page.text))
    for name, body in re.findall(
        r'<select[^>]*name="(setting:[^"]+)">(.*?)</select>', page.text, re.DOTALL
    ):
        values[name] = re.search(r'value="([^"]+)" selected', body).group(1)
    return values


def test_console_reviews_and_applies_a_change(console):
    root, client = console
    page = client.get("/admin/services")
    assert "Community time zone" in page.text
    values = form_values(page)
    values["setting:project.timezone"] = "Europe/Paris"

    review = client.post("/admin/services/review", data={"csrf_token": csrf(page), **values})
    assert review.status_code == 200
    assert "Europe/Paris" in review.text
    assert "Confirm with your password" not in review.text

    hidden = dict(re.findall(r'type="hidden" name="(setting:[^"]+)" value="([^"]*)"', review.text))
    applied = client.post("/admin/services/apply", data={"csrf_token": csrf(review), **hidden})
    assert applied.status_code == 200
    assert "Changes applied" in applied.text
    config = yaml.safe_load((root / "community-stack.yaml").read_text(encoding="utf-8"))
    assert config["project"]["timezone"] == "Europe/Paris"

    unchanged = client.post("/admin/services/review", data={"csrf_token": csrf(page), **hidden})
    assert "Nothing was changed." in unchanged.text


def test_console_asks_for_the_password_before_stopping_matrix(console):
    _, client = console
    page = client.get("/admin/services")
    values = {**form_values(page), "setting:services.matrix.enabled": "false"}
    review = client.post("/admin/services/review", data={"csrf_token": csrf(page), **values})
    assert "Confirm with your password" in review.text

    refused = client.post(
        "/admin/services/apply",
        data={"csrf_token": csrf(review), "setting:services.matrix.enabled": "false"},
    )
    assert refused.status_code == 400
    assert "Password confirmation failed." in refused.text


def test_get_started_checklist_tracks_progress(console):
    _, client = console
    dashboard = client.get("/admin")
    assert "Finish setting up" in dashboard.text
    start = client.get("/admin/start")
    # Owner account, the running agent, and the example's explicit time zone.
    assert "3 of 8 steps done." in start.text
    assert 'href="/admin/channels">Connect a publishing channel' in start.text
