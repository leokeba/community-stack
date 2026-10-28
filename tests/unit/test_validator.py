"""Unit tests for validator and edge-case checks."""

from community_stack.validator import (
    ValidationResult,
    check_dns_resolution,
    check_local_directories,
    check_tool_dependencies,
    run_checks,
)


def test_validation_result_methods():
    res = ValidationResult()
    assert res.ok is True
    res.add_info("some info")
    assert len(res.info) == 1
    assert res.ok is True

    res.add_warning("some warning")
    assert len(res.warnings) == 1
    assert res.ok is True

    res.add_error("some error")
    assert len(res.errors) == 1
    assert res.ok is False


def test_check_tool_dependencies():
    res = ValidationResult()
    check_tool_dependencies(res)
    # Should at least find info or warnings without crashing
    assert len(res.info) + len(res.warnings) > 0


def test_check_dns_resolution_test_domains():
    res = ValidationResult()
    check_dns_resolution("mycommunity.local", res)
    check_dns_resolution("mycommunity.test", res)
    check_dns_resolution("sub.example.org", res)
    assert len(res.info) == 3
    assert len(res.warnings) == 0


def test_check_local_directories(tmp_path):
    res = ValidationResult()
    check_local_directories(tmp_path, res)
    assert len(res.warnings) == 2  # data and backups missing

    (tmp_path / "data").mkdir()
    (tmp_path / "backups").mkdir()
    res2 = ValidationResult()
    check_local_directories(tmp_path, res2)
    assert len(res2.info) == 2
    assert len(res2.warnings) == 0


def test_run_checks_missing_config(tmp_path):
    res = run_checks(config_path=tmp_path / "absent.yaml")
    assert res.ok is False
    assert any("not found" in err for err in res.errors)
