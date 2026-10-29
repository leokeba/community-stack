"""Verify that the unified installer includes the configured publishing service."""

from pathlib import Path
from types import SimpleNamespace

from community_stack.commands import install_cmd


def test_install_deploys_announcements_when_enabled(tmp_path, monkeypatch):
    config = tmp_path / "community-stack.yaml"
    config.write_text(Path("config/example.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text("test=true\n", encoding="utf-8")
    deployed = []

    monkeypatch.setattr(
        install_cmd,
        "run_checks",
        lambda **kwargs: SimpleNamespace(ok=True, errors=[]),
    )
    monkeypatch.setattr(install_cmd.docker, "is_docker_available", lambda: True)
    monkeypatch.setattr(install_cmd.docker, "ensure_network", lambda network: True)
    monkeypatch.setattr(install_cmd, "get_services_dir", lambda: Path("services"))
    monkeypatch.setattr(install_cmd, "prepare_service", lambda *args: None)
    monkeypatch.setattr(
        install_cmd.docker,
        "run_compose",
        lambda project_dir, **kwargs: (
            deployed.append(Path(project_dir).name) or SimpleNamespace(returncode=0)
        ),
    )
    monkeypatch.setattr(
        "community_stack.commands.status_cmd.run_status",
        lambda: 0,
    )

    result = install_cmd.run_install(config_path=config, env_path=env_file)

    assert result == 0
    assert "announcements" in deployed
