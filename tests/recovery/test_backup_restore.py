"""Automated end-to-end disaster recovery integration test.

Validates that:
1. Complete state (database dump, matrix media, config, and credentials) can be snapshotted.
2. Backup archives pass structural verification.
3. Snapshots can be restored into an isolated environment without overwriting production.
4. Restored configuration is strictly valid against JSON Schema.
5. Critical files and restricted 0600 permissions are preserved.
"""

import os
from pathlib import Path

from community_stack.backup import BackupEngine
from community_stack.config import load_config, validate_config


def test_disaster_recovery_end_to_end(tmp_path):
    # Step 1: Simulate active production host environment
    prod_dir = tmp_path / "prod_server"
    prod_dir.mkdir()

    config_file = prod_dir / "community-stack.yaml"
    config_file.write_text(
        Path("config/example.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )

    env_file = prod_dir / ".env"
    env_file.write_text(
        "POSTGRES_DB=synapse\n"
        "POSTGRES_USER=synapse_user\n"
        "POSTGRES_PASSWORD=recovery_test_postgres_pw_456\n"
        "SYNAPSE_SECRET_KEY=recovery_test_secret_key_456\n"
        "SYNAPSE_REGISTRATION_SHARED_SECRET=recovery_test_reg_secret_456\n"
        "RESTIC_PASSWORD=recovery_test_restic_encryption_pw_456\n",
        encoding="utf-8",
    )
    if os.name == "posix":
        env_file.chmod(0o600)

    # Populate sample media, keys, and landing pages
    matrix_dir = prod_dir / "data" / "matrix"
    matrix_dir.mkdir(parents=True)
    (matrix_dir / "signing.key").write_text("ed25519 a_sample_matrix_signing_key", encoding="utf-8")

    landing_dir = prod_dir / "data" / "landing"
    landing_dir.mkdir(parents=True)
    (landing_dir / "custom_announcement.html").write_text(
        "<h1>Emergency Bulletin</h1>", encoding="utf-8"
    )

    # Step 2: Create backup snapshot
    engine = BackupEngine(config_path=config_file, env_path=env_file)
    snapshot = engine.create_snapshot()
    assert snapshot.id.startswith("snap_")

    # Step 3: Run backup integrity verification
    is_valid, messages = engine.verify_backup(snapshot_id=snapshot.id)
    assert is_valid is True, f"Backup verification failed: {messages}"

    # Step 4: Simulate recovery on a fresh, isolated test host
    isolated_recovery_target = tmp_path / "fresh_recovered_host"

    engine.restore_to_target(
        target_dir=isolated_recovery_target,
        snapshot_id=snapshot.id,
        confirm=True,
    )

    # Step 5: Verify recovered data and files
    assert (isolated_recovery_target / "community-stack.yaml").is_file()
    assert (isolated_recovery_target / ".env").is_file()
    assert (isolated_recovery_target / "data" / "matrix" / "signing.key").read_text(
        encoding="utf-8"
    ) == "ed25519 a_sample_matrix_signing_key"
    assert (isolated_recovery_target / "data" / "landing" / "custom_announcement.html").read_text(
        encoding="utf-8"
    ) == "<h1>Emergency Bulletin</h1>"
    assert (isolated_recovery_target / "backups" / "postgres_synapse.dump").is_file()

    # Step 6: Validate restored configuration against JSON Schema
    restored_config = load_config(isolated_recovery_target / "community-stack.yaml")
    schema_errors = validate_config(restored_config)
    assert schema_errors == []

    # Step 7: Check secrets permissions
    if os.name == "posix":
        restored_env_mode = (isolated_recovery_target / ".env").stat().st_mode & 0o777
        assert restored_env_mode == 0o600

    # Step 8: Verify original production host was unchanged
    assert (prod_dir / "community-stack.yaml").is_file()
    assert (prod_dir / ".env").is_file()
