import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path("scripts/restart-agent.sh")


def test_deploy_script_uses_the_verified_agent_restart_helper():
    deploy_script = Path("scripts/deploy-vps.sh").read_text(encoding="utf-8")

    assert 'sudo -n bash "$project_dir/scripts/restart-agent.sh"' in deploy_script
    assert "try-restart community-stack-agent.service" not in deploy_script


def test_deploy_script_verifies_caddy_route_mounts_and_stable_running_state():
    deploy_script = Path("scripts/deploy-vps.sh").read_text(encoding="utf-8")

    assert 'grep -qx "$route"' in deploy_script
    assert "/etc/caddy/vikunja.caddy" in deploy_script
    assert "{{.State.Status}} {{.RestartCount}}" in deploy_script
    assert "Caddy did not remain running after recreation" in deploy_script


def run_restart_helper(tmp_path, state):
    state_path = tmp_path / "systemctl-state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(
        f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

path = Path(os.environ["SYSTEMCTL_STATE"])
state = json.loads(path.read_text(encoding="utf-8"))
args = sys.argv[1:]
if args[0] == "show":
    prop = next(arg.split("=", 1)[1] for arg in args if arg.startswith("--property="))
    values = {{
        "LoadState": state["load_state"],
        "ExecMainStartTimestampMonotonic": str(state["start_time"]),
    }}
    print(values[prop])
elif args[0] == "restart":
    if state.get("restart_fails"):
        raise SystemExit(1)
    if not state.get("restart_without_new_start_time"):
        state["start_time"] += 1
    state["active"] = True
    path.write_text(json.dumps(state), encoding="utf-8")
elif args[0] == "is-active":
    raise SystemExit(0 if state["active"] else 3)
else:
    raise SystemExit(f"unexpected systemctl command: {{args}}")
""",
        encoding="utf-8",
    )
    systemctl.chmod(0o755)
    sudo = tmp_path / "sudo"
    sudo.write_text(
        '#!/bin/sh\n[ "$1" = "-n" ] && shift\nexec "$@"\n',
        encoding="utf-8",
    )
    sudo.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "SYSTEMCTL_STATE": str(state_path),
    }
    return subprocess.run(
        ["bash", str(SCRIPT.resolve())],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_agent_restart_helper_verifies_a_new_active_process(tmp_path):
    result = run_restart_helper(
        tmp_path,
        {
            "load_state": "loaded",
            "start_time": 100,
            "active": True,
        },
    )

    assert result.returncode == 0
    assert "restarted and is active" in result.stdout


def test_agent_restart_helper_reports_restart_failure(tmp_path):
    result = run_restart_helper(
        tmp_path,
        {
            "load_state": "loaded",
            "start_time": 100,
            "active": True,
            "restart_fails": True,
        },
    )

    assert result.returncode != 0
    assert "Failed to restart" in result.stderr


def test_agent_restart_helper_rejects_a_restart_without_new_process_start(tmp_path):
    result = run_restart_helper(
        tmp_path,
        {
            "load_state": "loaded",
            "start_time": 100,
            "active": True,
            "restart_without_new_start_time": True,
        },
    )

    assert result.returncode != 0
    assert "did not report a newer process start time" in result.stderr


def test_agent_restart_helper_skips_an_uninstalled_unit(tmp_path):
    result = run_restart_helper(
        tmp_path,
        {
            "load_state": "not-found",
            "start_time": 0,
            "active": False,
        },
    )

    assert result.returncode == 0
    assert "not installed; restart skipped" in result.stdout
