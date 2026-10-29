#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

usage() {
    printf 'Usage: VPS_HOST=<host-or-ip> [VPS_USER=debian] [REMOTE_DIR=/opt/community-stack] %s\n' "$0"
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    usage
    exit 0
fi

vps_host="${VPS_HOST:-}"
vps_user="${VPS_USER:-debian}"
remote_dir="${REMOTE_DIR:-/opt/community-stack}"

if [[ -z "$vps_host" ]]; then
    usage >&2
    printf 'VPS_HOST is required.\n' >&2
    exit 2
fi
if [[ ! "$vps_host" =~ ^[A-Za-z0-9._-]+$ || ! "$vps_user" =~ ^[A-Za-z_][A-Za-z0-9_-]*$ ]]; then
    printf 'Invalid SSH host or user.\n' >&2
    exit 2
fi
if [[ ! "$remote_dir" =~ ^/[A-Za-z0-9._/-]+$ \
    || "$remote_dir" == *"/../"* \
    || "$remote_dir" == */.. \
    || "$remote_dir" == *"/./"* \
    || "$remote_dir" == */. ]]; then
    printf 'REMOTE_DIR must be an absolute path containing only path characters.\n' >&2
    exit 2
fi

for command in git rsync ssh tar uv docker; do
    command -v "$command" >/dev/null || {
        printf 'Required command not found: %s\n' "$command" >&2
        exit 1
    }
done

if [[ -n "$(git status --porcelain --untracked-files=all)" ]]; then
    printf 'Refusing to deploy a dirty worktree. Commit or discard changes first.\n' >&2
    exit 1
fi

commit="$(git rev-parse --verify HEAD)"
deploy_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
target="${vps_user}@${vps_host}"
# Fail within about a minute if the connection dies (for example when the laptop
# sleeps) instead of hanging forever.
ssh_opts=(-o ServerAliveInterval=15 -o ServerAliveCountMax=4)

printf 'Running local tests and deployment configuration checks...\n'
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
docker compose -f services/announcements/docker-compose.yml config --quiet
docker compose -f services/proxy/docker-compose.yml config --quiet

printf 'Checking SSH, backup, and Docker prerequisites on %s...\n' "$target"
ssh "${ssh_opts[@]}" -o BatchMode=yes -o ConnectTimeout=10 "$target" bash -s -- "$remote_dir" <<'REMOTE'
set -eu
project_dir=$1
test -d "$project_dir"
test -f "$project_dir/community-stack.yaml"
test -f "$project_dir/.env"
test -x "$project_dir/.venv/bin/community-stack"
sudo -n true
sudo -n docker info >/dev/null
REMOTE

printf 'Creating and verifying a restrictive pre-deployment backup...\n'
ssh "${ssh_opts[@]}" "$target" bash -s -- "$remote_dir" <<'REMOTE'
set -eu
project_dir=$1
sudo -n env PYTHONDONTWRITEBYTECODE=1 sh -c "umask 077; cd '$project_dir' && .venv/bin/community-stack backup create"
sudo -n env PYTHONDONTWRITEBYTECODE=1 sh -c "cd '$project_dir' && .venv/bin/community-stack backup verify"
REMOTE

stage="$(mktemp -d "${TMPDIR:-/tmp}/community-stack-deploy.XXXXXX")"
trap 'rm -rf -- "$stage"' EXIT
git archive --format=tar "$commit" | tar -xf - -C "$stage"

printf 'Transferring committed source while preserving production config and data...\n'
rsync -a -e "ssh ${ssh_opts[*]}" \
    --exclude='/.env' \
    --exclude='/community-stack.yaml' \
    --exclude='/data/***' \
    --exclude='/backups/***' \
    --exclude='/services/proxy/Caddyfile' \
    "$stage/" "$target:$remote_dir/"

printf 'Building and checking the announcements service on the VPS...\n'
ssh "${ssh_opts[@]}" "$target" bash -s -- "$remote_dir" "$deploy_id" <<'REMOTE'
set -eu
project_dir=$1
deploy_id=$2
cd "$project_dir"

# The host CLI and the control agent run from this editable install; keep its
# dependencies in step with the code that was just copied.
uv_bin="$(command -v uv || echo "$HOME/.local/bin/uv")"
if [ -x "$uv_bin" ]; then
    "$uv_bin" sync --frozen --no-dev --quiet
else
    # Servers set up with scripts/bootstrap.sh use pip.
    .venv/bin/pip install --quiet -e .
fi
.venv/bin/community-stack --help >/dev/null

docker_cmd=(sudo -n docker)
compose_app=("${docker_cmd[@]}" compose --env-file .env -f services/announcements/docker-compose.yml)
compose_proxy=("${docker_cmd[@]}" compose --env-file .env -f services/proxy/docker-compose.yml)
rollback_tag="community-stack-announcements:rollback-$deploy_id"
old_image="$("${docker_cmd[@]}" image inspect community-stack-announcements:0.1.0 --format '{{.Id}}' 2>/dev/null || true)"
if [ -n "$old_image" ]; then
    "${docker_cmd[@]}" image tag "$old_image" "$rollback_tag"
else
    rollback_tag="unavailable"
fi

"${compose_app[@]}" config --quiet
"${compose_app[@]}" build announcements
"${compose_app[@]}" up -d --force-recreate --no-build announcements

healthy=false
for attempt in $(seq 1 36); do
    status="$("${docker_cmd[@]}" inspect community_announcements --format '{{.State.Health.Status}}' 2>/dev/null || echo missing)"
    if [ "$status" = healthy ]; then
        healthy=true
        break
    fi
    state="$("${docker_cmd[@]}" inspect community_announcements --format '{{.State.Status}}' 2>/dev/null || echo missing)"
    if [ "$state" = exited ] || [ "$state" = dead ]; then
        break
    fi
    sleep 5
done

if [ "$healthy" != true ]; then
    "${docker_cmd[@]}" logs --tail 80 community_announcements || true
    if [ -n "$old_image" ]; then
        "${docker_cmd[@]}" image tag "$rollback_tag" community-stack-announcements:0.1.0
        "${compose_app[@]}" up -d --force-recreate --no-build announcements
    fi
    echo "Announcements health check failed; the prior image was restored when available." >&2
    exit 1
fi

python3 - "$deploy_id" <<'PY'
from pathlib import Path
import shutil
import sys

path = Path("services/proxy/Caddyfile")
text = path.read_text(encoding="utf-8")
routes = [
    "    handle /admin* {\n        reverse_proxy community_announcements:8080\n    }\n\n",
    "    handle /ap/* {\n        reverse_proxy community_announcements:8080\n    }\n"
    "    handle /.well-known/webfinger {\n        reverse_proxy community_announcements:8080\n    }\n\n",
    "    handle /subscribe* {\n        reverse_proxy community_announcements:8080\n    }\n\n",
    "    @element_assets path /bundles/* /fonts/* /img/* /media/* /vector-icons/* /manifest.json /config.json\n"
    "    handle @element_assets {\n        reverse_proxy community_element:80\n    }\n\n",
]
backup = Path(f"backups/Caddyfile.before-{sys.argv[1]}")
backup.parent.mkdir(parents=True, exist_ok=True)
if backup.exists():
    raise SystemExit(f"Refusing to overwrite rollback file {backup}.")
shutil.copy2(path, backup)
backup.chmod(0o600)
missing = [route for route in routes if route not in text]
if missing:
    anchor = "    handle /feed* {\n        reverse_proxy community_landing:80\n    }\n"
    if text.count(anchor) < 1:
        raise SystemExit("Cannot safely locate the live public-feed route in Caddyfile.")
    path.write_text(text.replace(anchor, "".join(missing) + anchor), encoding="utf-8")
    path.chmod(0o644)
PY

caddy_backup="backups/Caddyfile.before-$deploy_id"
if ! "${docker_cmd[@]}" exec community_proxy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile; then
    install -m 0644 "$caddy_backup" services/proxy/Caddyfile
    exit 1
fi
if ! "${compose_proxy[@]}" up -d --force-recreate --no-deps caddy; then
    install -m 0644 "$caddy_backup" services/proxy/Caddyfile
    "${compose_proxy[@]}" up -d --force-recreate --no-deps caddy || true
    exit 1
fi

"${docker_cmd[@]}" inspect community_announcements --format '{{.State.Health.Status}}' | grep -qx healthy
"${docker_cmd[@]}" inspect community_proxy --format '{{.State.Status}}' | grep -qx running
# Pick up new agent code if the control agent is installed; does nothing otherwise.
sudo -n systemctl try-restart community-stack-agent.service 2>/dev/null || true
printf 'Deployment healthy. Prior image tag: %s\n' "${rollback_tag:-unavailable}"
REMOTE

printf 'Deployed commit %s to %s:%s\n' "$commit" "$target" "$remote_dir"
