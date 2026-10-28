"""Client-side encrypted backups through restic, and the settings that guard them."""

import os
import shutil
from pathlib import Path

import pytest
from community_stack.backup import BackupEngine, BackupError
from community_stack.validator import ValidationResult, check_backups

SECRET = "postgres_password_that_must_never_appear_in_plaintext"
needs_restic = pytest.mark.skipif(shutil.which("restic") is None, reason="restic not installed")


def make_project(tmp_path: Path, encryption: str = "restic", target: str = "local"):
    project = tmp_path / "project"
    (project / "data" / "matrix").mkdir(parents=True)
    (project / "data" / "matrix" / "homeserver.yaml").write_text(
        "server_name: matrix.example.org\n", encoding="utf-8"
    )
    config = project / "community-stack.yaml"
    config.write_text(
        Path("config/example.yaml")
        .read_text(encoding="utf-8")
        .replace('encryption: "restic"', f'encryption: "{encryption}"')
        .replace('target: "local"', f'target: "{target}"'),
        encoding="utf-8",
    )
    env = project / ".env"
    env.write_text(
        f"POSTGRES_PASSWORD={SECRET}\nRESTIC_PASSWORD=correct-horse-battery-staple\n"
        f"RESTIC_CACHE_DIR={tmp_path / 'restic-cache'}\n",
        encoding="utf-8",
    )
    env.chmod(0o600)
    return BackupEngine(config_path=config, env_path=env)


@needs_restic
def test_restic_snapshot_is_encrypted_verified_and_restorable(tmp_path):
    engine = make_project(tmp_path)

    snapshot = engine.create_snapshot()

    assert snapshot.encrypted
    assert not list(engine.backup_dir.glob("snap_*.tar.gz"))
    assert not list(engine.backup_dir.glob("staging_*"))
    repository = engine.backup_dir / "repository"
    assert repository.is_dir()
    for path in repository.rglob("*"):
        if path.is_file():
            assert SECRET.encode() not in path.read_bytes(), path

    ok, messages = engine.verify_backup()
    assert ok, messages
    assert any("encrypted snapshot" in message for message in messages)

    target = tmp_path / "restore"
    assert engine.restore_to_target(target, confirm=True)
    assert SECRET in (target / ".env").read_text(encoding="utf-8")
    assert (target / ".env").stat().st_mode & 0o777 == 0o600
    assert (target / "data" / "matrix" / "homeserver.yaml").is_file()
    assert (target / "community-stack.yaml").is_file()
    assert not (target / ".restic-restore").exists()


@needs_restic
def test_restic_verify_and_restore_pick_a_named_snapshot(tmp_path):
    engine = make_project(tmp_path)
    first = engine.create_snapshot()

    ok, messages = engine.verify_backup(snapshot_id=first.id)
    assert ok, messages
    ok, messages = engine.verify_backup(snapshot_id="snap_19990101")
    assert not ok
    assert "not found" in messages[0]


def test_restic_env_carries_only_backup_settings(tmp_path):
    engine = make_project(tmp_path)
    env = engine.get_backup_env()

    assert env["RESTIC_PASSWORD"] == "correct-horse-battery-staple"
    assert env["RESTIC_REPOSITORY"] == str(engine.backup_dir / "repository")
    assert SECRET not in env.values()
    assert "POSTGRES_PASSWORD" not in env


def test_sftp_target_requires_a_repository_url(tmp_path):
    engine = make_project(tmp_path, target="sftp")
    with pytest.raises(BackupError, match="RESTIC_REPOSITORY"):
        engine.get_backup_env()


def test_missing_restic_fails_with_install_hint(tmp_path, monkeypatch):
    engine = make_project(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    with pytest.raises(BackupError, match="restic is not installed"):
        engine.create_snapshot()
    assert not list(engine.backup_dir.glob("staging_*"))


def test_unencrypted_snapshot_warns(tmp_path):
    engine = make_project(tmp_path, encryption="none", target="s3")
    snapshot = engine.create_snapshot()

    assert not snapshot.encrypted
    assert any("not encrypted" in warning for warning in snapshot.warnings)
    assert any("only stored locally" in warning for warning in snapshot.warnings)


def test_check_warns_about_unencrypted_backups_and_missing_restic(monkeypatch):
    result = ValidationResult()
    check_backups({"operations": {"backups": {"enabled": True}}}, result)
    assert any("not encrypted" in warning for warning in result.warnings)

    monkeypatch.setattr(shutil, "which", lambda name, *args, **kwargs: None)
    result = ValidationResult()
    config = {"operations": {"backups": {"enabled": True, "encryption": "restic"}}}
    check_backups(config, result)
    assert any("restic is not installed" in warning for warning in result.warnings)
    assert result.ok


def test_restic_repository_is_owner_only(tmp_path):
    if shutil.which("restic") is None:
        pytest.skip("restic not installed")
    engine = make_project(tmp_path)
    engine.create_snapshot()
    assert os.stat(engine.backup_dir / "repository").st_mode & 0o077 == 0
