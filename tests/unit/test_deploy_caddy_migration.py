import subprocess
import sys
from pathlib import Path

DEPLOY_SCRIPT = Path("scripts/deploy-vps.sh")
INVOCATION = 'sudo -n -u community python3 - "$project_dir" "$deploy_id" <<\'PY\'\n'


def test_deploy_migration_persists_limits_for_existing_routes(tmp_path):
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    start = script.index(INVOCATION) + len(INVOCATION)
    end = script.index("\nPY\n", start)
    project_dir = tmp_path / "project"
    proxy_dir = project_dir / "services/proxy"
    proxy_dir.mkdir(parents=True)
    caddyfile = proxy_dir / "Caddyfile"
    original = """{
    admin off
}
example.test {
    handle /admin* {
        reverse_proxy community_announcements:8080
    }
    handle /ap/* {
        reverse_proxy community_announcements:8080
    }
    handle /.well-known/webfinger {
        reverse_proxy community_announcements:8080
    }
    handle /subscribe* {
        reverse_proxy community_announcements:8080
    }
    handle /feed* {
        reverse_proxy community_landing:80
    }
}
http://localhost, http://127.0.0.1 {
    handle /admin* {
        reverse_proxy community_announcements:8080
    }
    handle {
        reverse_proxy community_landing:80
    }
}
"""
    caddyfile.write_text(original, encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script[start:end],
            str(project_dir),
            "test-deploy",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    migrated = caddyfile.read_text(encoding="utf-8")
    assert "max_size 12MB" in migrated
    assert migrated.count("max_size 256KB") == 2
    assert "max_size 1MB" in migrated
    assert "handle /feed*" in migrated
    assert "import /etc/caddy/vikunja.caddy" in migrated
    assert "import /etc/caddy/facilmap.caddy" in migrated
    assert "http://localhost" not in migrated
    assert "admin 127.0.0.1:2019" in migrated
    assert migrated.count("Strict-Transport-Security") == 1
    assert (project_dir / "backups/Caddyfile.before-test-deploy").read_text(
        encoding="utf-8"
    ) == original
