"""Unit tests for BackupEngine and backup verification."""

import os
from pathlib import Path

import pytest
from community_stack.backup import BackupEngine, BackupError, is_restic_available


@pytest.fixture
def test_env(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    config_file = project_dir / "community-stack.yaml"
    config_file.write_text(
        Path("config/example.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    env_file = project_dir / ".env"
    env_file.write_text(
        "POSTGRES_PASSWORD=test_postgres_pw_123\n"
        "SYNAPSE_SECRET_KEY=test_synapse_secret_123\n"
        "SYNAPSE_REGISTRATION_SHARED_SECRET=test_reg_secret_123\n"
        "RESTIC_PASSWORD=test_restic_encryption_key_123\n",
        encoding="utf-8",
    )
    if os.name == "posix":
        env_file.chmod(0o600)

    # Sample data directory
    data_dir = project_dir / "data" / "matrix"
    data_dir.mkdir(parents=True)
    (data_dir / "homeserver.yaml").write_text("server_name: matrix.example.org\n", encoding="utf-8")

    return project_dir, config_file, env_file


def test_is_restic_available():
    # Calling is_restic_available should return bool without error
    res = is_restic_available()
    assert isinstance(res, bool)


def test_get_backup_env_missing_password(tmp_path):
    cfg_file = tmp_path / "community-stack.yaml"
    cfg_file.write_text(Path("config/example.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=pass\n", encoding="utf-8")

    engine = BackupEngine(config_path=cfg_file, env_path=env_file)
    with pytest.raises(BackupError, match="RESTIC_PASSWORD is not set"):
        engine.get_backup_env()


def test_create_snapshot_and_verify(test_env):
    _project_dir, config_file, env_file = test_env
    engine = BackupEngine(config_path=config_file, env_path=env_file)

    snapshot = engine.create_snapshot()
    assert snapshot.id.startswith("snap_")
    assert snapshot.size_bytes > 0
    assert snapshot.target == "local"

    # Verify the created snapshot
    is_valid, messages = engine.verify_backup()
    assert is_valid is True
    assert any("verified present" in m for m in messages)


def test_restore_requires_confirm(test_env, tmp_path):
    _project_dir, config_file, env_file = test_env
    engine = BackupEngine(config_path=config_file, env_path=env_file)
    engine.create_snapshot()

    restore_target = tmp_path / "isolated_restore"
    with pytest.raises(BackupError, match="requires explicit confirmation"):
        engine.restore_to_target(target_dir=restore_target, confirm=False)


def test_restore_refuses_same_project_root(test_env):
    project_dir, config_file, env_file = test_env
    engine = BackupEngine(config_path=config_file, env_path=env_file)
    engine.create_snapshot()

    with pytest.raises(BackupError, match="Refusing to restore directly onto active project root"):
        engine.restore_to_target(target_dir=project_dir, confirm=True)


def test_restore_to_isolated_target(test_env, tmp_path):
    _project_dir, config_file, env_file = test_env
    engine = BackupEngine(config_path=config_file, env_path=env_file)
    snapshot = engine.create_snapshot()

    restore_target = tmp_path / "isolated_restore"
    success = engine.restore_to_target(
        target_dir=restore_target,
        snapshot_id=snapshot.id,
        confirm=True,
    )
    assert success is True
    assert (restore_target / "community-stack.yaml").is_file()
    assert (restore_target / ".env").is_file()
    assert (restore_target / "data" / "matrix" / "homeserver.yaml").is_file()
    assert (restore_target / "backups" / "postgres_synapse.dump").is_file()

    if os.name == "posix":
        mode = (restore_target / ".env").stat().st_mode & 0o777
        assert mode == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions only")
def test_snapshot_archive_is_owner_only_regardless_of_umask(test_env):
    _project_dir, config_file, env_file = test_env
    engine = BackupEngine(config_path=config_file, env_path=env_file)
    previous = os.umask(0o022)
    try:
        engine.create_snapshot()
    finally:
        os.umask(previous)
    archives = list(engine.backup_dir.glob("snap_*.tar.gz"))
    assert archives
    assert all(archive.stat().st_mode & 0o777 == 0o600 for archive in archives)
    assert engine.backup_dir.stat().st_mode & 0o077 == 0
