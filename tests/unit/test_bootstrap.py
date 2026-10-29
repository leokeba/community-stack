"""The fresh-server bootstrap script (dry run) and the installer options of `init`."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from community_stack.commands.init_cmd import run_init
from community_stack.secrets import parse_env_file

SCRIPT = Path("scripts/bootstrap.sh")
ARGS = ["--web-domain", "ourtown.org", "--matrix-domain", "matrix.ourtown.org"]


def supported_os() -> bool:
    try:
        release = Path("/etc/os-release").read_text()
    except OSError:
        return False
    return any(
        f'VERSION_ID="{v}"' in release and f"ID={i}" in release
        for i, v in (("debian", "12"), ("debian", "13"), ("ubuntu", "22.04"), ("ubuntu", "24.04"))
    )


def bootstrap(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), *args], capture_output=True, text=True, check=False, timeout=60
    )


@pytest.mark.skipif(not supported_os(), reason="the dry run checks the operating system")
def test_dry_run_lists_every_step_without_changing_anything():
    result = bootstrap(*ARGS, "--email", "admin@ourtown.org", "--dry-run")
    assert result.returncode == 0, result.stderr
    for step in (
        "apt-get install",
        "useradd",
        "git clone",
        "community-stack init",
        "ufw allow 443/tcp",
        "community-stack install",
        "community-stack-agent",
    ):
        assert step in result.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize(
    "args",
    [
        ("--web-domain", "Bad_Domain", "--matrix-domain", "m.ourtown.org", "--email", "a@b.org"),
        (*ARGS, "--email", "not-an-email"),
        (*ARGS,),
    ],
)
def test_bad_input_is_refused_before_anything_runs(args):
    result = bootstrap(*args, "--dry-run")
    assert result.returncode != 0
    assert "[dry run]" not in result.stdout


def test_init_applies_installer_options(tmp_path, capsys):
    code = run_init(
        dest_dir=tmp_path,
        project_name="ourtown",
        base_domain="ourtown.org",
        web_domain="OurTown.org",
        matrix_domain="matrix.ourtown.org",
        timezone="Europe/Paris",
        acme_email="admin@ourtown.org",
        console_uid=1234,
        console_gid=1234,
        setup_token=True,
    )
    assert code == 0
    config = yaml.safe_load((tmp_path / "community-stack.yaml").read_text())
    assert config["domain"] == {
        "base": "ourtown.org",
        "matrix": "matrix.ourtown.org",
        "web": "ourtown.org",
    }
    assert config["project"]["timezone"] == "Europe/Paris"
    env = parse_env_file(tmp_path / ".env")
    assert env["WEB_DOMAIN"] == "ourtown.org"
    assert env["COMMUNITY_STACK_UID"] == "1234"
    assert env["COMMUNITY_STACK_ADMIN_SETUP_TOKEN"] in capsys.readouterr().out
    assert os.stat(tmp_path / ".env").st_mode & 0o777 == 0o600
    assert (tmp_path / "data" / "landing" / "feed").is_dir()


def test_init_refuses_invalid_domains(tmp_path):
    assert run_init(dest_dir=tmp_path, web_domain="not a domain") == 1
